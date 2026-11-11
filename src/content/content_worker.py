"""Dedicated-account content access for Hermes. JSON over stdin/stdout."""
import asyncio
import base64
import fcntl
import html
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urlparse, parse_qs

ROOT = Path(__file__).resolve().parent
os.umask(0o077)


def dump(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix='.'+path.name)
    temp = Path(temporary)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream, ensure_ascii=False)
        temp.chmod(0o600)
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def load(path):
    return json.loads(path.read_text()) if path.exists() else {}


def account_dir(platform):
    if platform not in {'bilibili', 'heybox'}:
        raise ValueError('platform must be bilibili or heybox')
    return ROOT / 'accounts' / platform


def heybox_env():
    env = dict(os.environ)
    env.pop('HEYBOX_COOKIE', None)
    env['XDG_CONFIG_HOME'] = str(account_dir('heybox'))
    return env


def credential_file(platform):
    d = account_dir(platform)
    return d / ('session.json' if platform == 'bilibili' else 'heybox-cli/session.json')


def status(platform, include_qr=True):
    d = account_dir(platform)
    cred = load(credential_file(platform))
    valid_fields = bool(cred.get('sessdata')) if platform == 'bilibili' else bool(cred.get('pkey') and cred.get('heybox_id'))
    result = {'platform': platform, 'credential_saved': valid_fields,
              'account_id': cred.get('dedeuserid') or cred.get('heybox_id'),
              'note': 'Saved session; validity is checked by actual site requests.'}
    state = load(d / 'login-state.json')
    if state:
        result['login'] = {k: v for k, v in state.items() if k != 'pid'}
        image = ROOT / 'cache' / (platform + '-login.png')
        if state.get('state') == 'waiting_scan' and time.time() - state.get('created_at', 0) < 170 and image.exists() and include_qr:
            result['qr_image_path'] = str(image)
            data = 'data:image/png;base64,' + base64.b64encode(image.read_bytes()).decode()
            result['qr_markdown'] = '![扫码登录](' + data + ')'
    return result


def native_status(platform):
    if platform == 'weixin':
        gateway = load(Path.home() / '.hermes/gateway_state.json')
        state = gateway.get('platforms', {}).get('weixin', {})
        accounts = Path.home() / '.hermes/weixin/accounts'
        identities = [p.stem for p in accounts.glob('*.json')
                      if not any(s in p.name for s in ['.sync.', '.context-tokens.'])]
        return {'platform': platform, 'state': state.get('state', 'not_configured'),
                'accounts': identities, 'error': state.get('error_message'),
                'session_management': 'Hermes restores the saved Weixin session automatically; phone confirmation is required for a new login.'}
    import httpx
    config = load(Path.home() / '.local/share/qq-hermes/napcat/opt/QQ/resources/app/app_launcher/napcat/config/webui.json')
    with httpx.Client(timeout=10) as client:
        auth = client.post('http://127.0.0.1:6099/api/auth/login', json={
            'hash': hashlib.sha256((config['token']+'.napcat').encode()).hexdigest()}).json()
        headers = {'Authorization': 'Bearer '+auth['data']['Credential']}
        login = client.post('http://127.0.0.1:6099/api/QQLogin/CheckLoginStatus', json={}, headers=headers).json()['data']
        info = client.post('http://127.0.0.1:6099/api/QQLogin/GetQQLoginInfo', json={}, headers=headers).json().get('data', {})
    return {'platform': 'qq', 'account_id': info.get('uin'), 'connected': login.get('isLogin'),
            'state': login.get('loginPhase'), 'session_management': 'NapCat restores this dedicated QQ session automatically.'}


def login_start(platform):
    d = account_dir(platform)
    with (d / 'login.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = load(d / 'login-state.json')
        process_state = load(d / 'login-process.json')
        if process_state.get('pid'):
            state['pid'] = process_state['pid']
        alive = False
        if state.get('pid') and time.time() - state.get('created_at', 0) < 175:
            try:
                os.kill(state['pid'], 0)
                alive = True
            except ProcessLookupError:
                pass
        if not alive:
            image = ROOT / 'cache' / (platform + '-login.png')
            image.unlink(missing_ok=True)
            dump(d / 'login-state.json', {'state': 'starting', 'created_at': time.time()})
            with (d / 'login.log').open('w') as log:
                process = subprocess.Popen([sys.executable, '-I', str(__file__), 'login-worker', platform],
                    stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
            dump(d / 'login-process.json', {'created_at': time.time(), 'pid': process.pid})
        for _ in range(50):
            state = load(d / 'login-state.json')
            if state.get('state') != 'starting':
                break
            time.sleep(.2)
    return status(platform)


async def bili_login():
    import httpx
    import qrcode
    state_path = account_dir('bilibili') / 'login-state.json'
    state = {'state': 'waiting_scan', 'created_at': time.time(), 'pid': os.getpid()}
    async with httpx.AsyncClient(timeout=20, headers={'User-Agent': 'Mozilla/5.0', 'Referer': 'https://www.bilibili.com/'}) as client:
        response = (await client.get('https://passport.bilibili.com/x/passport-login/web/qrcode/generate')).json()
        if response.get('code') != 0:
            raise RuntimeError('Bilibili QR generation failed: ' + str(response.get('message')))
        challenge = response['data']
        qrcode.make(challenge['url']).save(ROOT / 'cache/bilibili-login.png')
        dump(state_path, state)
        for _ in range(85):
            response = await client.get('https://passport.bilibili.com/x/passport-login/web/qrcode/poll', params={'qrcode_key': challenge['qrcode_key']})
            payload = response.json()
            data = payload.get('data', {})
            code = data.get('code')
            if payload.get('code') != 0:
                raise RuntimeError('Bilibili QR polling failed: ' + str(payload.get('message')))
            if code == 0:
                query = parse_qs(urlparse(data.get('url', '')).query)
                cookies = {c.name: c.value for c in client.cookies.jar}
                cookies.update({k: v[-1] for k, v in query.items()})
                fields = {'sessdata': cookies.get('SESSDATA', ''), 'bili_jct': cookies.get('bili_jct', ''),
                          'dedeuserid': cookies.get('DedeUserID', ''), 'buvid3': cookies.get('buvid3', ''),
                          'buvid4': cookies.get('buvid4', ''), 'ac_time_value': data.get('refresh_token', '')}
                if not fields['sessdata']:
                    raise RuntimeError('QR confirmed but SESSDATA was absent; no account session was saved.')
                dump(credential_file('bilibili'), fields)
                dump(state_path, {**state, 'state': 'authenticated'})
                return
            if code == 86038:
                break
            if code not in {86101, 86090}:
                raise RuntimeError('Unexpected Bilibili QR state: ' + str(code))
            await asyncio.sleep(2)
    dump(state_path, {**state, 'state': 'expired'})


def heybox_login():
    state_path = account_dir('heybox') / 'login-state.json'
    state = {'state': 'starting', 'created_at': time.time(), 'pid': os.getpid()}
    process = subprocess.Popen([str(ROOT / 'bin/heybox'), 'login', '--timeout', '175s'],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=heybox_env())
    errors = []
    for line in process.stdout:
        if '二维码图片：' in line:
            source = Path(line.split('二维码图片：', 1)[1].strip())
            shutil.copyfile(source, ROOT / 'cache/heybox-login.png')
            dump(state_path, {**state, 'state': 'waiting_scan'})
        elif '错误:' in line or '错误：' in line:
            errors.append(line.strip()[:250])
    code = process.wait(timeout=190)
    dump(state_path, {**state, 'state': 'authenticated' if code == 0 else 'failed', 'error': ' '.join(errors)})


def clean(text):
    return html.unescape(re.sub('<[^>]*>', '', str(text or '')))


def bili_credential():
    from bilibili_api import Credential
    values = load(credential_file('bilibili'))
    return Credential(**values) if values else Credential()


async def bili_search(args):
    from bilibili_api import search
    from bilibili_api.utils.network import Api
    from bilibili_api.utils.utils import get_api
    orders = {'relevance': search.OrderVideo.TOTALRANK, 'latest': search.OrderVideo.PUBDATE, 'hot': search.OrderVideo.CLICK}
    api = get_api('search')['search']['web_search_by_type']
    data = await Api(**api, wbi=True, credential=bili_credential()).update_params(
        keyword=args['query'], search_type='video', order=orders[args.get('sort', 'relevance')].value,
        page=int(args.get('page', 1)), page_size=20).result
    items = data.get('result', [])[:int(args.get('limit', 5))]
    return {'platform': 'bilibili', 'query': args['query'], 'results': [
        {**{k: item.get(k) for k in ['bvid', 'author', 'duration', 'pubdate', 'play']},
         'title': clean(item.get('title')), 'description': clean(item.get('description')),
         'url': 'https://www.bilibili.com/video/' + item['bvid']} for item in items]}


async def bili_read(args):
    import aiohttp
    from bilibili_api import video
    match = re.search(r'BV[0-9A-Za-z]{10}', str(args['target']))
    if not match:
        raise ValueError('Provide a BV identifier or full Bilibili video URL.')
    bvid = match.group()
    obj = video.Video(bvid=bvid, credential=bili_credential())
    info = await obj.get_info()
    pages = info.get('pages', [])
    part = int(args.get('part', 1))
    if not 1 <= part <= len(pages):
        raise ValueError('Video part is out of range.')
    result = {'platform': 'bilibili', 'bvid': bvid, 'title': info.get('title'),
        'author': info.get('owner', {}).get('name'), 'description': info.get('desc'),
        'url': 'https://www.bilibili.com/video/' + bvid, 'part': part,
        'parts': [{'part': i+1, 'title': p.get('part')} for i, p in enumerate(pages)],
        'source_kind': 'metadata_only', 'subtitle': [], 'complete': False}
    try:
        player = await obj.get_player_info(cid=pages[part-1]['cid'])
        subtitles = player.get('subtitle', {}).get('subtitles', [])
        if not subtitles:
            result['limitation'] = 'No accessible subtitles. Do not claim to have watched or summarized the full video.'
            return result
        selected = next((s for s in subtitles if 'zh' in s.get('lan', '')), subtitles[0])
        url = selected['subtitle_url']
        if url.startswith('//'):
            url = 'https:' + url
        host = (urlparse(url).hostname or '').lower()
        if urlparse(url).scheme != 'https' or not any(host.endswith('.'+d) or host == d for d in ['hdslb.com', 'bilibili.com', 'bilivideo.com']):
            raise ValueError('Subtitle endpoint is outside Bilibili domains.')
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as session:
            async with session.get(url, allow_redirects=False) as response:
                response.raise_for_status()
                body = (await response.json(content_type=None)).get('body', [])
        offset = max(0, int(args.get('offset', 0)))
        result.update(source_kind='subtitle', language=selected.get('lan'), subtitle=body[offset:offset+300],
            total_segments=len(body), offset=offset, complete=len(body) <= offset+300)
        if not result['complete']:
            result['next_offset'] = offset+300
    except Exception as exc:
        result['limitation'] = 'Subtitle retrieval failed: ' + str(exc)[:300]
    return result


def heybox_request(args, action):
    command = [str(ROOT / 'bin/heybox')]
    if action == 'search':
        command += ['search', args['query'], '--type', 'post', '--sort', args.get('sort', 'relevance'),
                    '--page', str(args.get('page', 1)), '--limit', str(args.get('limit', 5))]
    else:
        match = re.search(r'(?:link_id=|/bbs/link/|/app/bbs/link/)?(\d{5,})', str(args['target']))
        if not match:
            raise ValueError('Provide the Heybox post numeric ID or a URL containing link_id.')
        command += ['comments' if args.get('comments') else 'post', match.group(1)]
        if args.get('comments'):
            command += ['--limit', '5', '--replies=false']
    command += ['--json', '--timeout', '20s']
    process = subprocess.run(command, env=heybox_env(), capture_output=True, text=True, timeout=45)
    if process.returncode:
        return {'ok': False, 'platform': 'heybox', 'error': process.stderr.strip()[:600],
                'needs_login': process.returncode in {3, 4}, 'exit_code': process.returncode}
    return {'ok': True, 'platform': 'heybox', 'data': json.loads(process.stdout)}


async def run(args):
    action = args['action']
    if action in {'capabilities','bili_query','bili_action','qq_query','qq_action','weixin_contacts','weixin_send','heybox_query','heybox_action'}:
        sys.path.insert(0, str(ROOT))
        import platform_features
        return await platform_features.run(args, sys.modules[__name__])
    if action in {'balance', 'news', 'qq_contacts', 'qq_send', 'article', 'danmaku', 'comment'}:
        sys.path.insert(0, str(ROOT))
        import extensions
        return await extensions.run(args, bili_credential)
    platform = args['platform']
    if platform in {'qq', 'weixin'} and action in {'status', 'login'}:
        result = native_status(platform)
        if action == 'login':
            result['login_note'] = 'This channel is managed by its existing native service. A new login needs the platform phone confirmation; no login was changed.'
        return result
    account_dir(platform)
    if action == 'status':
        return status(platform)
    if action == 'login':
        return login_start(platform)
    if action == 'transcribe':
        if platform != 'bilibili':
            raise ValueError('Audio transcription is only available for Bilibili videos.')
        sys.path.insert(0, str(ROOT))
        import transcription
        if args.get('operation', 'start') == 'start':
            return transcription.start(args)
        for _ in range(10):
            state = transcription.result(args)
            if state.get('state') in {'complete', 'failed', 'not_started'}:
                return state
            await asyncio.sleep(1)
        return transcription.result(args)
    if action not in {'search', 'read'}:
        raise ValueError('Unsupported action')
    if action == 'search':
        if not str(args.get('query', '')).strip():
            raise ValueError('A topic or keyword is required.')
        args['limit'] = min(10, max(1, int(args.get('limit', 5))))
        args['page'] = min(10, max(1, int(args.get('page', 1))))
        if args.get('sort', 'relevance') not in {'relevance', 'latest', 'hot'}:
            raise ValueError('Invalid sort')
    if platform == 'heybox':
        return heybox_request(args, action)
    return await (bili_search(args) if action == 'search' else bili_read(args))


if __name__ == '__main__':
    try:
        if sys.argv[1:2] == ['login-worker']:
            platform = sys.argv[2]
            try:
                asyncio.run(bili_login()) if platform == 'bilibili' else heybox_login()
            except Exception as exc:
                dump(account_dir(platform) / 'login-state.json', {'state': 'failed', 'error': str(exc)[:400], 'created_at': time.time()})
        else:
            print(json.dumps(asyncio.run(asyncio.wait_for(run(json.load(sys.stdin)), 70)), ensure_ascii=False))
    except Exception as exc:
        print(json.dumps({'ok': False, 'error': str(exc)[:600], 'error_type': type(exc).__name__}, ensure_ascii=False))
