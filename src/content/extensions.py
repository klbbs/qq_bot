"""Bounded content reads and explicitly requested publishing, isolated from Hermes core."""
import asyncio
import email.utils
import fcntl
import hashlib
import json
import math
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

import httpx

ROOT = Path(__file__).resolve().parent
CST = timezone(timedelta(hours=8))
FEEDS = {
    'un_zh': ('联合国新闻（中文）', 'https://news.un.org/feed/subscribe/zh/news/all/rss.xml'),
    'bbc_world': ('BBC World', 'https://feeds.bbci.co.uk/news/world/rss.xml'),
    'bbc_zh': ('BBC 中文', 'https://feeds.bbci.co.uk/zhongwen/simp/rss.xml'),
}


def timestamp(value):
    try:
        dt = email.utils.parsedate_to_datetime(value)
    except (ValueError, TypeError):
        try:
            dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
        except (ValueError, AttributeError):
            return None
    return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).astimezone(CST).isoformat(timespec='seconds')


def parse_feed(content, name, query='', max_age_hours=72):
    from bs4 import BeautifulSoup
    root = ET.fromstring(content)
    rows = root.findall('.//item') or root.findall('{http://www.w3.org/2005/Atom}entry')
    items = []
    cutoff = datetime.now(CST) - timedelta(hours=max_age_hours)
    for row in rows:
        def field(key):
            return row.findtext(key) or row.findtext('{http://www.w3.org/2005/Atom}'+key) or ''
        title = BeautifulSoup(field('title'), 'html.parser').get_text(' ', strip=True)
        summary = BeautifulSoup(field('description') or field('summary'), 'html.parser').get_text(' ', strip=True)
        url = field('link')
        if not url:
            link = row.find('{http://www.w3.org/2005/Atom}link')
            url = link.get('href', '') if link is not None else ''
        published = timestamp(field('pubDate') or field('published') or field('updated'))
        if not published or datetime.fromisoformat(published) < cutoff:
            continue
        if query and query.casefold() not in (title+' '+summary).casefold():
            continue
        if urlparse(url).scheme not in {'http', 'https'}:
            continue
        items.append({'title': title[:500], 'source': name, 'url': url,
                      'published_at': published, 'summary': summary[:1600], 'source_kind': 'rss_excerpt'})
    return items


async def news_collect(args):
    sources = args.get('sources') or list(FEEDS)
    if not isinstance(sources, list) or any(s not in FEEDS for s in sources):
        raise ValueError('Choose configured news sources')
    age = min(720, max(1, int(args.get('max_age_hours', 72))))
    limit = min(30, max(1, int(args.get('limit', 12))))
    async def fetch(key):
        name, url = FEEDS[key]
        try:
            async with httpx.AsyncClient(timeout=18, follow_redirects=True) as client:
                async with client.stream('GET', url, headers={'User-Agent': 'Hermes-Content/1.1 (personal RSS reader)'}) as response:
                    response.raise_for_status()
                    content = bytearray()
                    async for chunk in response.aiter_bytes():
                        content.extend(chunk)
                        if len(content) > 3_000_000:
                            raise ValueError('Feed exceeds size limit')
            items = parse_feed(bytes(content), name, str(args.get('query', '')).strip(), age)
            return items, {'source': key, 'status': 'ok', 'matched_items': len(items)}
        except Exception:
            return [], {'source': key, 'status': 'unavailable'}
    results = await asyncio.gather(*(fetch(key) for key in sources))
    merged = {item['url']: item for rows, _ in results for item in rows}
    items = sorted(merged.values(), key=lambda x:x['published_at'], reverse=True)
    return {'ok': True, 'collected_at': datetime.now(CST).isoformat(timespec='seconds'),
            'max_age_hours': age, 'query': args.get('query', ''), 'sources': [s for _, s in results],
            'items': items[:limit], 'matched_items': len(items),
            'limitation': 'Recent RSS coverage, not a popularity ranking. Excerpts are not full articles. '
                          'Use web_search for current cross-source news searches; report source failures and dates.'}


def deepseek_balance():
    import yaml
    config = yaml.safe_load((Path.home()/'.hermes/config.yaml').read_text())
    model = config.get('model', {})
    base = str(model.get('base_url', '')).rstrip('/')
    key = model.get('api_key')
    official_bases = {'https://api.deepseek.com', 'https://api.deepseek.com/v1'}
    # 主模型的 api_key 可能为空，真正的 key 也可能放在 custom_providers 里。
    if base in official_bases and (not isinstance(key, str) or not key or key.startswith('${')):
        for provider in config.get('custom_providers', []) or []:
            if not isinstance(provider, dict):
                continue
            pbase = str(provider.get('base_url', '')).rstrip('/')
            pkey = provider.get('api_key')
            if pbase in official_bases and isinstance(pkey, str) and pkey and not pkey.startswith('${'):
                base, key = pbase, pkey
                break
    if base not in official_bases:
        return {'status': 'not_official_deepseek_configuration'}
    if not isinstance(key, str) or not key or key.startswith('${'):
        return {'status': 'credential_unavailable'}
    try:
        with httpx.Client(timeout=20, follow_redirects=False) as client:
            response = client.get('https://api.deepseek.com/user/balance', headers={'Authorization': 'Bearer '+key})
        if response.status_code != 200:
            return {'status': 'authentication_failed' if response.status_code == 401 else 'query_failed'}
        data = response.json()
        balances = [{'balance': str(x['total_balance']), 'currency': x['currency']}
                    for x in data['balance_infos'] if x.get('currency') in {'CNY', 'USD'}]
        return {'status': 'available' if data['is_available'] else 'unavailable', 'balances': balances}
    except Exception:
        return {'status': 'query_failed'}


async def qq_request(args, send=False):
    # No admin or current-chat fallback: all destinations must be explicit.
    kind = args.get('target_type')
    if kind not in {'private', 'group'}:
        raise ValueError('Explicit target_type private/group is required')
    if send:
        validate_write(args, 'message')
        if not re.fullmatch(r'[1-9][0-9]{4,19}', str(args.get('target_id', ''))):
            raise ValueError('Explicit numeric target_id is required')
        if not isinstance(args['target_id'], str):
            raise ValueError('target_id must be a string')
        if len(args['message']) > 4000:
            raise ValueError('Message exceeds 4000 characters')
    token = (Path.home()/'.local/share/qq-hermes/.outbound-token').read_text().strip()
    try:
        async with httpx.AsyncClient(timeout=25, follow_redirects=False) as client:
            headers = {'Authorization': 'Bearer '+token}
            if send:
                payload = {k:args[k] for k in ['target_type','target_id','message','request_id']}
                response = await client.post('http://127.0.0.1:18800/hermes-outbound/send', json=payload, headers=headers)
            else:
                response = await client.get('http://127.0.0.1:18800/hermes-outbound/contacts', params={'kind':kind}, headers=headers)
        if response.status_code != 200:
            return {'ok': False, 'status': 'bridge_rejected', 'http_status': response.status_code}
        data = response.json()
        if not send:
            query = str(args.get('query', '')).casefold()
            data['contacts'] = [x for x in data['contacts'] if query in (x['id']+' '+x['name']+' '+x['remark']).casefold()][:100]
        return data
    except Exception:
        return {'ok': False, 'status': 'uncertain' if send else 'unavailable',
                'error': 'If sending, do not retry with a new request ID. Delivery status is unknown.'}


def validate_write(args, field):
    if not isinstance(args.get(field), str) or not args[field].strip():
        raise ValueError('Explicit non-empty content is required')
    if not re.fullmatch(r'[A-Za-z0-9_-]{8,80}', str(args.get('request_id', ''))):
        raise ValueError('A stable request_id of 8-80 letters/digits/_/- is required')


def video_id(target):
    value = str(target)
    match = re.fullmatch(r'BV[0-9A-Za-z]{10}', value) or re.fullmatch(
        r'https://(?:www\.)?bilibili\.com/video/(BV[0-9A-Za-z]{10})/?(?:\?[^\s]*)?', value)
    if not match:
        raise ValueError('Provide a BV ID or full bilibili.com/video/BV URL')
    return match.group(1) if match.lastindex else match.group()


def article_id(target):
    value = str(target)
    match = re.fullmatch(r'cv([1-9][0-9]*)', value, re.I) or re.fullmatch(
        r'https://(?:www\.)?bilibili\.com/read/(?:cv|mobile[/?].*?id=)([1-9][0-9]*)(?:[/?&][^\s]*)?', value)
    if not match:
        raise ValueError('Provide a cv ID or full bilibili.com/read/cv URL')
    return int(match.group(1))


async def bili_article(args, credential):
    from bilibili_api import article
    cvid = article_id(args['target'])
    obj = article.Article(cvid=cvid, credential=credential())
    await obj.fetch_content()
    text = obj.markdown()
    if text.startswith('---\n'):
        text = text.split('\n---\n', 1)[-1].lstrip()
    # Library markdown embeds article metadata in YAML; return only article metadata/body, never cookies.
    meta = obj.json().get('meta', {})
    offset = max(0, int(args.get('offset', 0)))
    body = text[offset:offset+18000]
    result = {'ok': True, 'source_kind': 'article_body', 'cvid': cvid,
              'title': meta.get('title'), 'author': meta.get('author', {}).get('name'),
              'published_at': datetime.fromtimestamp(meta['publish_time'], CST).isoformat(timespec='seconds') if meta.get('publish_time') else None,
              'url': f'https://www.bilibili.com/read/cv{cvid}',
              'text': body, 'offset': offset, 'total_characters': len(text), 'complete': offset+len(body)>=len(text)}
    if not result['complete']:
        result['next_offset'] = offset+len(body)
    return result


async def bili_danmaku(args, credential):
    from bilibili_api import video
    bvid = video_id(args['target'])
    obj = video.Video(bvid=bvid, credential=credential())
    info = await obj.get_info()
    part = int(args.get('part', 1))
    if not 1 <= part <= len(info['pages']):
        raise ValueError('Video part is out of range')
    page = info['pages'][part-1]
    total = max(1, math.ceil(page.get('duration', 0)/360))
    segment = int(args.get('segment', 1))
    if not 1 <= segment <= total:
        raise ValueError('Danmaku segment is out of range')
    offset = max(0, int(args.get('offset', 0)))
    rows = await obj.get_danmakus(cid=page['cid'], from_seg=segment-1, to_seg=segment-1)
    rows.sort(key=lambda x:x.dm_time)
    batch = rows[offset:offset+200]
    result = {'ok': True, 'source_kind': 'danmaku_sample', 'bvid': bvid, 'title': info.get('title'),
              'url': 'https://www.bilibili.com/video/'+bvid, 'part': part, 'segment': segment,
              'segment_start_seconds': (segment-1)*360, 'total_segments': total, 'offset': offset,
              'segment_items': len(rows), 'items': [{'text': x.text, 'video_seconds': x.dm_time,
                   'posted_at': datetime.fromtimestamp(x.send_time, CST).isoformat(timespec='seconds') if x.send_time else None} for x in batch],
              'complete_segment': offset+len(batch)>=len(rows),
              'limitation': 'Platform-returned viewer comments only; not all historical danmaku or a representative poll.'}
    if not result['complete_segment']:
        result['next_offset'] = offset+len(batch)
    elif segment < total:
        result['next_segment'] = segment+1
    return result


async def bili_comment(args, credential):
    from bilibili_api import comment, video
    validate_write(args, 'text')
    if len(args['text']) > 1000:
        raise ValueError('Comment exceeds 1000 characters')
    kind = args.get('resource_type')
    if kind not in {'video', 'article'}:
        raise ValueError('Explicit resource_type video/article is required')
    target = video_id(args['target']) if kind == 'video' else article_id(args['target'])
    cred = credential()
    cred.raise_for_no_sessdata()
    cred.raise_for_no_bili_jct()
    oid = (await video.Video(bvid=target, credential=cred).get_info())['aid'] if kind == 'video' else target
    root, parent = args.get('root_id'), args.get('parent_id')
    if parent and not root:
        raise ValueError('parent_id requires root_id')
    for item in [root, parent]:
        if item is not None and (not str(item).isdigit() or int(item) <= 0):
            raise ValueError('Comment reply IDs must be positive integers')
    digest = hashlib.sha256(json.dumps({k:v for k,v in args.items() if k!='request_id'}, sort_keys=True).encode()).hexdigest()
    ledger = ROOT/'cache/comment-sends';ledger.mkdir(parents=True,exist_ok=True)
    with (ledger/(args['request_id']+'.lock')).open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        path = ledger/(args['request_id']+'.json')
        if path.exists():
            previous = json.loads(path.read_text())
            if previous['digest'] != digest:
                raise ValueError('Request ID already used for different content')
            return previous['result']
        pending = {'ok': False, 'state': 'uncertain', 'request_id': args['request_id'],
                   'error': 'Submission may have occurred. Do not retry with a new request ID.'}
        path.write_text(json.dumps({'digest':digest,'result':pending}))
        try:
            response = await comment.send_comment(text=args['text'], oid=int(oid),
                type_=comment.CommentResourceType.VIDEO if kind=='video' else comment.CommentResourceType.ARTICLE,
                root=int(root) if root else None, parent=int(parent) if parent else None, credential=cred)
            rpid = response.get('rpid') or response.get('reply', {}).get('rpid')
            result = {'ok': bool(rpid), 'state': 'submitted' if rpid else 'uncertain',
                      'comment_id': str(rpid) if rpid else None, 'request_id': args['request_id'],
                      'target': str(target), 'note': 'Platform acceptance does not guarantee public visibility; moderation may apply.'}
            path.write_text(json.dumps({'digest':digest,'result':result}))
            return result
        except Exception:
            return pending


async def run(args, credential):
    action = args['action']
    if action == 'balance':
        return deepseek_balance()
    if action == 'news':
        return await news_collect(args)
    if action in {'qq_contacts', 'qq_send'}:
        return await qq_request(args, send=action=='qq_send')
    handlers = {'article': bili_article, 'danmaku': bili_danmaku, 'comment': bili_comment}
    return await handlers[action](args, credential)
