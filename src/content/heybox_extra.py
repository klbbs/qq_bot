"""Fixed Heybox community endpoints using the isolated CLI account.

Protocol references: shadowfish07/heybox-cli (MIT), internal/api/signer.go,
client.go; XiaHouSheng/heybox-core (MIT), Net/builder.py, Message/builder.py.
No background polling, unsolicited replies, arbitrary URLs or captcha bypass.
"""
import hashlib
import json
import re
import secrets
import stat
import time
from http.cookies import SimpleCookie

import httpx

READS = ['post', 'comment_replies', 'mentions']
WRITES = ['comment', 'reply']
FIELDS = {'root_id': {'type': 'string', 'pattern': '^[1-9][0-9]{0,24}$'},
          'reply_id': {'type': 'string', 'pattern': '^[1-9][0-9]{0,24}$'},
          'cursor': {'type': 'string', 'maxLength': 200}}
ENDPOINTS = {'mentions': ('GET', '/bbs/app/user/message'),
             'comment_replies': ('GET', '/bbs/app/comment/sub/comments'),
             'comment': ('POST', '/bbs/app/comment/create'),
             'reply': ('POST', '/bbs/app/comment/create')}


def cli_signature(path, timestamp, nonce):
    # Compatibility port of heybox-cli's MIT-licensed signer; vectors in tests.
    alphabet = 'JKMNPQRTX1234OABCDFG56789H'
    md5 = lambda s: hashlib.md5(s.encode()).hexdigest()
    digits = lambda s: ''.join(c for c in s if c in '0123456789')
    canonical = '/' + '/'.join(p for p in path.split('/') if p) + '/'
    n = int(digits(md5(str(timestamp + 1) + canonical + md5(digits(nonce + alphabet))))[:9].ljust(9, '0'))
    encoded = ''
    for _ in range(5): encoded += alphabet[n % len(alphabet)]; n //= len(alphabet)
    e = lambda v: ((v << 1) ^ (27 if v & 128 else 0)) & 255
    z = lambda v: e(v) ^ v
    j = lambda v: z(e(v))
    aa = lambda v: j(z(e(v)))
    o = lambda v: aa(v) ^ j(v) ^ z(v)
    a, b, c, d = map(ord, encoded[-4:])
    mixed = [o(a)^aa(b)^j(c)^z(d), z(a)^o(b)^aa(c)^j(d), j(a)^z(b)^o(c)^aa(d), aa(a)^j(b)^z(c)^o(d)]
    return encoded + f'{sum(mixed) % 100:02d}'


def signature(path, timestamp, nonce):
    """Community protocol from heybox-core/process_origin.js (not CLI search)."""
    alphabet = 'AB45STUVWZEFGJ6CH01D237IXYPQRKLMN89'
    canonical = '/' + '/'.join(p for p in path.split('/') if p) + '/'
    def mapped(value, chars): return ''.join(chars[ord(c) % len(chars)] for c in value)
    parts = [mapped(str(timestamp + 1), alphabet[:-2]), mapped(canonical, alphabet), mapped(nonce, alphabet)]
    interleaved = ''.join(part[i] for i in range(max(map(len, parts))) for part in parts if i < len(part))[:20]
    digest = hashlib.md5(interleaved.encode()).hexdigest()
    e = lambda v: ((v << 1) ^ (27 if v & 128 else 0)) & 255
    z = lambda v: e(v) ^ v
    j = lambda v: z(e(v))
    aa = lambda v: j(z(e(v)))
    o = lambda v: aa(v) ^ j(v) ^ z(v)
    values = list(map(ord, digest[-6:])); a,b,c,d = values[:4]
    mixed = [o(a)^aa(b)^j(c)^z(d), z(a)^o(b)^aa(c)^j(d), j(a)^z(b)^o(c)^aa(d), aa(a)^j(b)^z(c)^o(d)] + values[4:]
    return mapped(digest[:5], alphabet[:-4]) + f'{sum(mixed) % 100:02d}'


def identity(worker):
    path = worker.credential_file('heybox')
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077: raise ValueError('Heybox session must be a private regular file; login again')
    row = json.loads(path.read_text())
    if not row.get('heybox_id') or not row.get('pkey'): raise ValueError('Heybox login required')
    cookie = SimpleCookie()
    for k, value in {'heybox_id': row['heybox_id'], 'user_heybox_id': row['heybox_id'], 'pkey': row['pkey'],
                     'user_pkey': row['pkey'], 'x_xhh_heyboxid': row.get('x_xhh_heyboxid') or row['heybox_id']}.items():
        cookie[k] = str(value)
    return str(row['heybox_id']), '; '.join(m.OutputString() for m in cookie.values())


async def request(operation, params, worker, data=None):
    method, path = ENDPOINTS[operation]
    try: uid, cookie = identity(worker)
    except (OSError, ValueError, KeyError): return {'ok': False, 'state': 'needs_login', 'error': 'Use content_account heybox login'}
    timestamp = int(time.time()); nonce = hashlib.md5((str(timestamp) + secrets.token_hex(16)).encode()).hexdigest().upper()
    query = {'os_type': 'web', 'app': 'web', 'client_type': 'web', 'version': '999.0.4', 'web_version': '2.5',
             'x_client_type': 'web', 'x_app': 'heybox_website', 'x_os_type': 'Windows', 'heybox_id': uid,
             'hkey': signature(path, timestamp, nonce), '_time': timestamp, 'nonce': nonce, **params}
    headers = {'Cookie': cookie, 'User-Agent': 'Mozilla/5.0', 'Referer': 'https://xiaoheihe.cn/',
               'Origin': 'https://www.xiaoheihe.cn', 'Accept': 'application/json'}
    try:
        async with httpx.AsyncClient(timeout=25, follow_redirects=False) as client:
            async with client.stream(method, 'https://api.xiaoheihe.cn' + path, params=query, data=data, headers=headers) as response:
                if response.status_code in {401, 403}: return {'ok': False, 'state': 'needs_login'}
                if response.status_code == 429: return {'ok': False, 'state': 'rate_limited'}
                if response.status_code != 200: return {'ok': False, 'state': 'uncertain' if method == 'POST' else 'unavailable', 'http_status': response.status_code}
                payload = bytearray()
                async for chunk in response.aiter_bytes():
                    payload.extend(chunk)
                    if len(payload) > 2 * 1024 * 1024: return {'ok': False, 'state': 'uncertain' if method == 'POST' else 'incompatible'}
        row = json.loads(payload)
    except (httpx.HTTPError, ValueError):
        return {'ok': False, 'state': 'uncertain' if method == 'POST' else 'unavailable', 'error': 'No automatic write retry; reuse the original request_id.'}
    if row.get('status') != 'ok':
        state = {'show_captcha': 'captcha_required', 'need_captcha': 'captcha_required', 'lack_token': 'needs_login',
                 'need_login': 'needs_login', 'unauthorized': 'needs_login'}.get(row.get('status'), 'rejected')
        return {'ok': False, 'state': state, 'error': 'Platform did not accept the request. Do not bypass verification or automatically retry.'}
    return {'ok': True, 'result': row.get('result') or {}}


def comment_view(row):
    user = row.get('user') or {}
    return {'comment_id': str(row.get('commentid') or row.get('comment_id') or row.get('id') or ''),
            'root_id': str(row.get('root_comment_id') or ''), 'reply_id': str(row.get('replyid') or row.get('reply_id') or ''),
            'author': str(user.get('username') or '')[:100], 'uid': str(user.get('userid') or row.get('userid') or ''),
            'text': str(row.get('text') or row.get('content') or '')[:4000], 'likes': row.get('up'), 'timestamp': row.get('create_at')}


async def read(args, worker, p):
    op = args['operation']; limit = p.bound(args, 'limit', 10, 20); page = p.bound(args, 'page', 1, 50)
    if op == 'post':
        target = str(p.positive(args.get('target'), 'Heybox post ID'))
        return await __import__('asyncio').to_thread(worker.heybox_request, dict(args, target=target), 'read')
    if op == 'mentions': params = {'message_type': '16', 'offset': (page - 1) * limit, 'limit': limit, 'no_more': 'false'}
    elif op == 'comment_replies':
        params = {'link_id': p.positive(args.get('target'), 'post ID'), 'root_comment_id': p.positive(args.get('root_id'), 'root_id'), 'limit': limit}
        if args.get('cursor'): params['lastval'] = str(args['cursor'])[:200]
    else: raise ValueError('Unsupported Heybox extra read')
    response = await request(op, params, worker)
    if not response['ok']: return response
    row = response['result']
    if not isinstance(row, dict): return {'ok': False, 'state': 'incompatible'}
    if op == 'comment_replies':
        return {'ok': True, 'items': [comment_view(r) for r in (row.get('comments') or [])[:limit]],
                'has_more': row.get('has_more'), 'next_cursor': row.get('lastval')}
    return {'ok': True, 'page': page, 'items': [{'message_id': str(r.get('message_id') or ''), 'post_id': str(r.get('linkid') or ''),
            'root_id': str(r.get('root_comment_id') or ''), 'text': str(r.get('comment_a_text') or '')[:4000],
            'title': str(r.get('link_title') or '')[:300], 'uid': str(r.get('userid_a') or ''),
            'author': str((r.get('user_a') or {}).get('username') or '')[:100], 'timestamp': r.get('timestamp')}
            for r in (row.get('messages') or [])[:limit]], 'limitation': 'Account @mentions only; no automatic replies or private DM inbox.'}


async def write(args, worker, p):
    op = args.get('operation')
    if op not in WRITES: raise ValueError('Heybox supports comment/reply only; no likes, posting, follow or private DM endpoint verified')
    target = p.positive(args.get('target'), 'post ID'); p.validate_write(args, 'text')
    if len(args['text']) > 1000: raise ValueError('Comment text <=1000 characters')
    body = {'is_cy': 0, 'link_id': target, 'root_id': -1, 'reply_id': -1, 'text': args['text']}
    if op == 'reply':
        body.update(root_id=p.positive(args.get('root_id'), 'root_id'), reply_id=p.positive(args.get('reply_id'), 'reply_id'))
    elif args.get('root_id') or args.get('reply_id'): raise ValueError('Use reply with explicit root_id and reply_id')
    async def perform():
        response = await request(op, {}, worker, body)
        result = {'operation': op, 'target': str(target), 'request_id': args['request_id']}
        if not response['ok']: return {**result, **response}
        row = response['result']
        cid = (row.get('commentid') or row.get('comment_id') or (row.get('comment') or {}).get('commentid')) if isinstance(row, dict) else None
        # An acknowledgement without an identifier is not proof of a visible comment.
        return {**result, 'ok': True, 'state': 'submitted', 'comment_id': str(cid) if cid else None,
                'note': 'Platform acknowledged submission; moderation/visibility not verified.'}
    return await p.once(args, perform)
