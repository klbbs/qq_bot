"""Fixed, read-only ACM Lab client. Password entry is local and never stored."""
import argparse
import getpass
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from urllib.parse import quote

import httpx

ROOT = Path(__file__).resolve().parent
ORIGIN = '__ACM_ORIGIN__'
COOKIE = 'acm_user_session'
OPERATIONS = ('status','overview','submissions','accounts','training','daily','catalog','reports','report','problem','problem_history','jobs')
PRIVATE_KEYS = {'password','password_hash','token','access_token','api_key','key','cookie','cookies','csrf_token','credentials','credential','session','session_token','headers','authorization'}


def save(name, data):
    ROOT.mkdir(mode=0o700, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=ROOT, prefix='.acm-')
    try:
        with os.fdopen(fd, 'w') as stream: json.dump(data, stream, ensure_ascii=False)
        os.chmod(temp, 0o600)
        os.replace(temp, ROOT / name)
    finally:
        Path(temp).unlink(missing_ok=True)


def load(name):
    path = ROOT / name
    if not path.exists(): return {}
    return json.loads(path.read_text())


def read_json(response):
    if response.headers.get('content-type', '').split(';')[0] != 'application/json':
        raise ValueError('unexpected_response')
    buf = bytearray()
    for chunk in response.iter_bytes():
        buf.extend(chunk)
        if len(buf) > 2 * 1024 * 1024: raise ValueError('response_too_large')
    return json.loads(buf)


def fetch(client, path, params=None):
    # Callers can select only named operations; arbitrary URLs, methods or headers are absent.
    with client.stream('GET', ORIGIN + path, params=params) as response:
        if response.status_code != 200:
            return response.status_code, None
        return response.status_code, read_json(response)


def safe_data(value, limit, flags):
    if isinstance(value, dict):
        return {k: safe_data(v, limit, flags) for k, v in value.items() if k.lower().replace('-', '_') not in PRIVATE_KEYS}
    if isinstance(value, list):
        if len(value) > limit: flags.append('list')
        return [safe_data(v, limit, flags) for v in value[:limit]]
    if isinstance(value, str) and len(value) > 12000:
        flags.append('text');return value[:12000]
    return value


def integer(args, key, default, low, high):
    value = args.get(key, default)
    if type(value) is not int or not low <= value <= high:
        raise ValueError('invalid_' + key)
    return value


def route(args):
    allowed = {'operation','platform','days','search','verdict','offset','limit','id','minimum','maximum','tags'}
    if set(args) - allowed: raise ValueError('unknown_fields')
    op = args.get('operation', 'status')
    if op not in OPERATIONS: raise ValueError('unsupported_operation')
    platform = args.get('platform', '')
    if platform not in {'', 'codeforces','atcoder','luogu','nowcoder'}: raise ValueError('invalid_platform')
    limit = integer(args, 'limit', 10, 1, 30)
    params = {}
    if op in {'overview','submissions'}:
        params.update(days=integer(args,'days',30 if op=='overview' else 0,0,90), platform=platform)
    if op == 'submissions':
        for key in ['search','verdict']:
            value=args.get(key,'')
            if not isinstance(value,str) or len(value)>100:raise ValueError('invalid_'+key)
            params[key]=value
        params.update(offset=integer(args,'offset',0,0,10000),limit=limit)
    if op == 'catalog':
        minimum=integer(args,'minimum',800,800,3500);maximum=integer(args,'maximum',1600,800,3500)
        if minimum>maximum:raise ValueError('invalid_difficulty_range')
        tags=args.get('tags',[])
        if not isinstance(tags,list) or len(tags)>10 or any(not isinstance(t,str) or len(t)>60 for t in tags):raise ValueError('invalid_tags')
        params.update(minimum=minimum,maximum=maximum,tags=','.join(tags))
    paths={'overview':'/api/overview','submissions':'/api/submissions','accounts':'/api/accounts',
           'training':'/api/training','daily':'/api/daily','catalog':'/api/catalog','reports':'/api/reports','jobs':'/api/jobs'}
    if op in {'report','problem','problem_history'}:
        value=args.get('id','')
        if not isinstance(value,str) or not re.fullmatch(r'[A-Za-z0-9_:.~-]{1,150}',value) or '..' in value:raise ValueError('invalid_id')
        encoded=quote(value,safe='')
        path='/api/reports/'+encoded if op=='report' else '/api/workbench/problem/'+encoded+('/history' if op=='problem_history' else '')
    else:path=paths.get(op,'/api/auth/status')
    return op,path,params,limit


def run(args, transport=None):
    try:
        op,path,params,limit=route(args)
        cookie=load('session.json').get('cookie')
        cookies={COOKIE:cookie} if cookie else {}
        with httpx.Client(timeout=20,trust_env=False,follow_redirects=False,cookies=cookies,transport=transport) as client:
            if op=='status':
                health_code,health=fetch(client,'/api/health')
                code,data=fetch(client,path)
                return {'ok':health_code==200,'source':ORIGIN,'authenticated':bool(code==200 and data.get('authenticated')),
                        'health':(health or {}).get('status'),'login_command':'hermes acm login',
                        'scope':'Current authenticated ACM user only; read-only connector. No server deployment or external judge submission.'}
            code,data=fetch(client,path,params)
        if code==401:return {'ok':False,'state':'needs_login','message':'请在本机终端运行 hermes acm login；密码不发聊天。会话过期也需重新登录。'}
        if code==404:return {'ok':False,'state':'unsupported_by_server','operation':op,'message':'当前服务器没有此接口；本地新版本功能不代表服务器已部署。'}
        if code!=200:return {'ok':False,'state':'server_rejected','http_status':code,'operation':op}
        flags=[];data=safe_data(data,limit,flags)
        result={'ok':True,'source':ORIGIN,'operation':op,'data':data,'partial':bool(flags),'untrusted_source':True}
        if flags:result['limitation']='Lists/text are bounded; not a complete archive or full document. Submissions support offset/limit; narrow days/platform/difficulty for other queries.'
        if len(json.dumps(result,ensure_ascii=False))>50000:
            return {'ok':False,'state':'response_too_large','message':'请缩小日期、平台或难度范围；不会返回截断的无效 JSON。'}
        return result
    except (ValueError,TypeError,KeyError):return {'ok':False,'state':'invalid_request_or_response'}
    except httpx.HTTPError:return {'ok':False,'state':'connection_failed','message':'连接失败；保持 HTTPS 证书校验，请稍后检查服务器网络。'}
    except OSError:return {'ok':False,'state':'local_session_unavailable'}


def login(username):
    if not re.fullmatch(r'[A-Za-z0-9_]{3,32}',username):raise SystemExit('用户名格式不正确')
    password=getpass.getpass('ACM Lab 当前密码（输入不会显示，也不会保存密码）：')
    try:
        with httpx.Client(timeout=20,trust_env=False,follow_redirects=False) as client:
            response=client.post(ORIGIN+'/api/auth/login',json={'username':username,'password':password},
                                 headers={'Origin':ORIGIN,'X-Acm-Request':'1'})
            if response.status_code!=200:
                print('登录未通过（HTTP '+str(response.status_code)+'），没有重试，也没有保存密码。');return 1
            code,state=fetch(client,'/api/auth/status')
            cookie=client.cookies.get(COOKIE)
            if code!=200 or not state.get('authenticated') or not cookie:
                print('服务器未建立有效登录会话。');return 1
            save('session.json',{'cookie':cookie})
            save('connection.json',{'base_url':ORIGIN,'username':username})
        print('ACM Lab 已连接。仅保存本站登录会话，密码未保存。QQ/微信/飞书开新对话后可查询刷题数据。')
        return 0
    except httpx.HTTPError:
        print('连接失败，保持 HTTPS 校验，请稍后再试。');return 1
    finally:password=''


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--login',action='store_true');parser.add_argument('--username',default='kendas')
    args=parser.parse_args()
    if args.login:return login(args.username)
    print(json.dumps(run(json.load(sys.stdin)),ensure_ascii=False));return 0


if __name__=='__main__':
    os.umask(0o077)
    raise SystemExit(main())
