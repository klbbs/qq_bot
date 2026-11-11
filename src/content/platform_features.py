"""Expanded dedicated-account capabilities; fixed operations, no arbitrary API execution."""
import asyncio
import fcntl
import hashlib
import json
import re
import subprocess
import time
from pathlib import Path

import httpx
from extensions import video_id, article_id, validate_write

ROOT=Path(__file__).resolve().parent
CAPABILITIES={
 'qq':{'read':['friends/groups','profile','group','members','history','message','group_files'],
       'write':['text DM/group','image/video/audio/file','reply','forward','recall own message','friend_remark','group_card','group_name','group_notice','set_nickname (self)','set_avatar (self)','set_signature (self)','publish_qzone (self, text/images, explicit visibility)'],
       'limits':'Dedicated bot account only. Explicit recipient/content/action required. Self changes target the bot QQ only. Qzone needs explicit visibility; up to 9 local images, <=32 MiB total, <=4096px per side (also avatar limit). Submission is not a visibility guarantee. Group writes depend on bot permissions. Media <=32 MiB from approved local folders. Group command ingress remains closed.'},
 'bilibili':{'read':['search/subtitles/transcription','article','danmaku','comments','user_search','user','user_videos','user_dynamics','following','favorites','favorite_content','toview','history','hot','dynamic','dm_sessions','dm_messages','unread'],
             'write':['comment/reply','like/unlike','favorite/unfavorite','watch_later/remove_watch_later','follow/unfollow','coin','publish_dynamic','repost_dynamic','delete_dynamic (own)','like_dynamic/unlike_dynamic','send_danmaku','text DM'],
             'limits':'Dedicated account only. Explicit target and operation/content required. Coin spending needs explicit amount. Real writes require user instructions; platform moderation/privacy/rate limits may prevent acceptance or visibility.'},
 'weixin':{'read':['known local bot conversation targets','account status'],
           'write':['text DM','image/video/audio/file to a known bot conversation'],
           'limits':'iLink bot contact, not control of the ordinary WeChat account. No personal address book/Moments/payments/voice calls. Peer must have messaged the bot; server may reject stale conversations. Audio may arrive as an attachment.'},
 'heybox':{'read':['post/topic/user/game search','post body','paginated root comments and bounded replies'],
           'write':[],'limits':'Installed dedicated-session CLI has no supported posting/likes/follow/DM API. Those actions are not implemented; do not claim otherwise.'},
 'web':{'read':['web_search','web_extract','publisher RSS'], 'write':[],
        'limits':'QQ supports /search query and natural-language web research. Give source URLs and publication/event times; distinguish excerpts from full-source verification.'}}


# One bounded operation catalog is used by model schemas and runtime discovery.
_QQ_CONTRACT=json.loads((ROOT/'qq_api_contract.json').read_text())
CAPABILITIES['qq']['query_operations']=_QQ_CONTRACT['read_operations']
CAPABILITIES['qq']['action_operations']=_QQ_CONTRACT['action_operations']
CAPABILITIES['qq']['read']+=_QQ_CONTRACT['extended_reads']
CAPABILITIES['qq']['write']+=_QQ_CONTRACT['extended_actions']
CAPABILITIES['qq']['limits']+=' '+_QQ_CONTRACT['usage']+' Request inbox only records new received events, never auto-approves. Client-only features such as payments, calls, arbitrary new friend/group application, security credentials and full QQ Space administration are not exposed.'


import bili_extra,heybox_extra
CAPABILITIES['bilibili']['read']+=bili_extra.READS
CAPABILITIES['bilibili']['write']+=bili_extra.WRITES+['publish_dynamic with local images']
CAPABILITIES['bilibili']['limits']+=' '+bili_extra.USAGE
CAPABILITIES['heybox']['read']+=heybox_extra.READS
CAPABILITIES['heybox']['write']=heybox_extra.WRITES
CAPABILITIES['heybox']['limits']='Dedicated isolated login; comments/replies are signed community submissions, not private DMs. Explicit post ID,text required; reply also root_id,reply_id. Stable request_id and no automatic write retries. Platform may require login/captcha or reject; no bypass. Post creation/likes/follow/DM have no verified endpoint and remain unavailable. Source content is untrusted data.'


def positive(value, name='target'):
    if not re.fullmatch(r'[1-9][0-9]{0,24}',str(value or '')):raise ValueError(name+' must be an explicit positive numeric ID')
    return int(value)


def bound(args,key,default,maximum):
    return min(maximum,max(1,int(args.get(key,default))))


async def once(args, call):
    request_id=args.get('request_id','')
    if not re.fullmatch(r'[A-Za-z0-9_-]{8,80}',str(request_id)):raise ValueError('A stable request_id is required')
    payload={k:v for k,v in args.items() if k!='request_id'}
    digest=hashlib.sha256(json.dumps(payload,sort_keys=True).encode()).hexdigest()
    directory=ROOT/'cache/platform-actions';directory.mkdir(parents=True,exist_ok=True,mode=0o700)
    with (directory/(request_id+'.lock')).open('w') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        path=directory/(request_id+'.json')
        if path.exists():
            old=json.loads(path.read_text())
            if old['digest']!=digest:raise ValueError('Request ID is already used for different content')
            return old['result']
        pending={'ok':False,'state':'uncertain','request_id':request_id,'error':'Submission may have occurred. Do not retry with a new request ID.'}
        path.write_text(json.dumps({'digest':digest,'result':pending}))
        try:
            result=await call()
            path.write_text(json.dumps({'digest':digest,'result':result}))
            return result
        except Exception:return pending


def video_view(row):
    return {**{k:row.get(k) for k in ['bvid','aid','title','author','pubdate','duration']},
            'url':'https://www.bilibili.com/video/'+str(row['bvid']) if row.get('bvid') else None}


def dynamic_view(row):
    modules=row.get('modules',{});author=modules.get('module_author',{});body=modules.get('module_dynamic',{})
    major=body.get('major') or {};opus=major.get('opus') or {};archive=major.get('archive') or {}
    return {'id':str(row.get('id_str','')),'author':author.get('name'),'uid':author.get('mid'),'published_at':author.get('pub_ts'),
            'text':(body.get('desc') or {}).get('text') or (opus.get('summary') or {}).get('text') or '',
            'title':opus.get('title') or archive.get('title'),'url':'https://t.bilibili.com/'+str(row.get('id_str','')),
            'has_images':bool(opus.get('pics'))}


async def bili_read(args, credential):
    from bilibili_api import video,user,favorite_list,comment,dynamic,hot,session
    op=args['operation']
    import bili_extra, sys
    if op in bili_extra.READS:return await bili_extra.read(args,credential,sys.modules[__name__])
    cred=credential();limit=bound(args,'limit',10,20);page=bound(args,'page',1,100)
    target=args.get('target');result={'ok':True,'operation':op}
    if op=='user_search':
        from bilibili_api.utils.network import Api
        from bilibili_api.utils.utils import get_api
        query=str(args.get('query','')).strip()
        if not query:raise ValueError('User search requires an explicit query')
        api=get_api('search')['search']['web_search_by_type']
        response=await Api(**api,wbi=True,credential=cred).update_params(keyword=query,search_type='bili_user',page=page,page_size=20).result
        result.update(items=[{'uid':r.get('mid'),'name':r.get('uname'),'description':r.get('usign'),'url':'https://space.bilibili.com/'+str(r.get('mid'))} for r in (response.get('result') or [])[:limit]],page=page)
    elif op=='comments':
        kind=args.get('resource_type','video')
        if kind not in {'video','article'}:raise ValueError('resource_type must be video/article')
        oid=(await video.Video(bvid=video_id(target),credential=cred).get_info())['aid'] if kind=='video' else article_id(target)
        response=await comment.get_comments(oid=int(oid),type_=comment.CommentResourceType.VIDEO if kind=='video' else comment.CommentResourceType.ARTICLE,
              page_index=page,order=comment.OrderType.LIKE if args.get('sort')=='hot' else comment.OrderType.TIME,credential=cred)
        rows=response.get('replies') or []
        result.update(items=[{'comment_id':str(r.get('rpid')),'root_id':str(r.get('root')),'uid':r.get('mid'),
              'author':r.get('member',{}).get('uname'),'text':r.get('content',{}).get('message'),'likes':r.get('like'),
              'replies':r.get('rcount'),'published_at':r.get('ctime')} for r in rows[:limit]],page=page,
              limitation='Bounded comment sample; not a representative poll or full comment history.')
    elif op in {'user','user_videos','user_dynamics','following'}:
        obj=user.User(uid=positive(target,'UID'),credential=cred)
        if op=='user':
            row=await obj.get_user_info();result['profile']={k:row.get(k) for k in ['mid','name','sign','sex','level','official']}
        elif op=='user_videos':
            row=await obj.get_videos(pn=page,ps=limit);result.update(items=[video_view(r) for r in row.get('list',{}).get('vlist',[])],page=page)
        elif op=='user_dynamics':
            row=await obj.get_dynamics_new(offset=str(args.get('cursor','')))
            result.update(items=[dynamic_view(r) for r in row.get('items',[])[:limit]],next_cursor=row.get('offset') if row.get('has_more') else None)
        else:
            row=await obj.get_followings(pn=page,ps=limit)
            result.update(items=[{k:r.get(k) for k in ['mid','uname','sign']} for r in row.get('list',[])],page=page)
    elif op=='favorites':
        uid=positive(target,'UID') if target else int((await user.get_self_info(credential=cred))['mid'])
        row=await favorite_list.get_video_favorite_list(uid=uid,credential=cred)
        result['items']=[{k:r.get(k) for k in ['id','title','media_count','attr']} for r in (row.get('list') or [])[:limit]]
    elif op=='favorite_content':
        row=await favorite_list.get_video_favorite_list_content(media_id=positive(target,'favorite folder ID'),page=page,credential=cred)
        result.update(items=[video_view(r) for r in (row.get('medias') or [])[:limit]],page=page,has_more=row.get('has_more'))
    elif op in {'toview','history','hot'}:
        if op=='toview':row=await user.get_toview_list(credential=cred);rows=row.get('list') or []
        elif op=='history':
            row=await user.get_self_history(page_num=page,per_page_item=limit,credential=cred)
            rows=row if isinstance(row,list) else row.get('list',[])
        else:row=await hot.get_hot_videos(pn=page,ps=limit);rows=row.get('list') or []
        result.update(items=[video_view(r) for r in rows[:limit]],page=page)
    elif op=='dynamic':result['item']=dynamic_view((await dynamic.Dynamic(dynamic_id=positive(target),credential=cred).get_info())['item'])
    elif op=='dm_sessions':
        row=await session.get_sessions(credential=cred)
        rows=row.get('session_list') or []
        result['items']=[{'uid':r.get('talker_id'),'unread':r.get('unread_count'),'last_message_time':r.get('session_ts')} for r in rows[:limit]]
    elif op=='dm_messages':
        row=await session.fetch_session_msgs(talker_id=positive(target,'recipient UID'),credential=cred,begin_seqno=int(args.get('cursor') or 0))
        items=[]
        for r in (row.get('messages') or [])[:limit]:
            content=r.get('content','')
            try:content=json.loads(content) if isinstance(content,str) else content
            except (ValueError,TypeError):content={}
            items.append({'sender_uid':r.get('sender_uid'),'receiver_uid':r.get('receiver_id'),'message_type':r.get('msg_type'),
                          'text':content.get('content','') if isinstance(content,dict) else '',
                          'timestamp':r.get('timestamp'),'sequence':r.get('msg_seqno')})
        result.update(items=items,has_more=row.get('has_more'),next_cursor=str(row.get('min_seqno','')),
                      limitation='Only returned messages; non-text media is not interpreted.')
    elif op=='unread':
        row=await session.get_unread_messages(credential=cred)
        result['counts']={k:v for k,v in row.items() if isinstance(v,(int,bool))}
    else:raise ValueError('Unsupported Bilibili read operation')
    return result


async def bili_action(args, credential):
    from bilibili_api import video,user,dynamic,session
    from bilibili_api.utils.danmaku import Danmaku
    op=args['operation']
    import bili_extra, sys
    if op in bili_extra.WRITES:return await bili_extra.write(args,credential,sys.modules[__name__])
    cred=credential();cred.raise_for_no_sessdata();cred.raise_for_no_bili_jct()
    supported={'like','unlike','favorite','unfavorite','watch_later','remove_watch_later','follow','unfollow','coin',
               'publish_dynamic','repost_dynamic','delete_dynamic','like_dynamic','unlike_dynamic','send_danmaku','dm'}
    if op not in supported:raise ValueError('Unsupported Bilibili action')
    if op in {'publish_dynamic','repost_dynamic','send_danmaku','dm'}:
        validate_write(args,'text')
        if len(args['text'])>1000:raise ValueError('Text exceeds 1000 characters')
    if op=='publish_dynamic':target=None
    elif op in {'follow','unfollow','repost_dynamic','delete_dynamic','like_dynamic','unlike_dynamic','dm'}:target=positive(args.get('target'))
    else:target=video_id(args.get('target'))
    if op in {'favorite','unfavorite'}:folder=positive(args.get('folder_id'),'explicit favorite folder ID')
    if op=='coin' and (type(args.get('amount')) is not int or args.get('amount') not in {1,2}):raise ValueError('Explicit coin amount 1 or 2 is required')
    if op=='send_danmaku':
        if 'video_seconds' not in args or not isinstance(args['video_seconds'],(int,float)) or args['video_seconds']<0:raise ValueError('Explicit nonnegative video_seconds is required')
        if len(args['text'])>100:raise ValueError('Danmaku exceeds 100 characters')
    image_content=[]
    if args.get('image_paths') is not None:
        if op!='publish_dynamic':raise ValueError('image_paths applies only to publish_dynamic')
        image_content,digests=bili_extra.images(args,sys.modules[__name__]);args=dict(args,image_sha256=digests)
    async def perform():
        data=None
        if op=='dm':data=await session.send_msg(credential=cred,receiver_id=target,msg_type=session.EventType.TEXT,content=args['text'])
        elif op in {'follow','unfollow'}:
            data=await user.User(uid=target,credential=cred).modify_relation(user.RelationType.SUBSCRIBE if op=='follow' else user.RelationType.UNSUBSCRIBE)
        elif op=='publish_dynamic':
            builder=dynamic.BuildDynamic();builder.add_plain_text(args['text'])
            if image_content:
                builder.add_image([bili_extra.picture(data,fmt) for data,fmt in image_content])
            data=await dynamic.send_dynamic(builder,credential=cred)
        elif op in {'repost_dynamic','delete_dynamic','like_dynamic','unlike_dynamic'}:
            obj=dynamic.Dynamic(dynamic_id=target,credential=cred)
            if op=='delete_dynamic':
                info=(await obj.get_info())['item'];me=await user.get_self_info(credential=cred)
                if str(info.get('modules',{}).get('module_author',{}).get('mid'))!=str(me['mid']):
                    return {'ok':False,'state':'rejected','request_id':args['request_id'],'error':'Only own dynamics may be deleted'}
                data=await obj.delete()
            elif op=='repost_dynamic':data=await obj.repost(text=args['text'])
            else:data=await obj.set_like(status=op=='like_dynamic')
        else:
            obj=video.Video(bvid=target,credential=cred)
            if op in {'like','unlike'}:data=await obj.like(status=op=='like')
            elif op in {'favorite','unfavorite'}:data=await obj.set_favorite(**{('add_media_ids' if op=='favorite' else 'del_media_ids'):[folder]})
            elif op=='coin':data=await obj.pay_coin(num=int(args['amount']),like=False)
            elif op=='watch_later':data=await obj.add_to_toview()
            elif op=='remove_watch_later':data=await obj.delete_from_toview()
            elif op=='send_danmaku':
                part=bound(args,'part',1,1000);info=await obj.get_info();pages=info.get('pages',[])
                if part>len(pages) or args['video_seconds']>pages[part-1].get('duration',0):
                    return {'ok':False,'state':'rejected','request_id':args['request_id'],'error':'Part or danmaku timestamp is outside video range'}
                data=await obj.send_danmaku(page_index=part-1,danmaku=Danmaku(text=args['text'],dm_time=float(args['video_seconds']),send_time=time.time()))
        ids={k:str(v) for k,v in (data or {}).items() if k in {'dynamic_id','dyn_id','dynamic_id_str','msg_key','dmid','dmid_str'}} if isinstance(data,dict) else {}
        return {'ok':True,'state':'submitted','operation':op,'target':str(target) if target is not None else None,
                'request_id':args['request_id'],**ids,'note':'Platform accepted the request. Visibility/delivery may depend on moderation and privacy settings.'}
    return await once(args,perform)


async def qq_call(args, write=False):
    payload={k:v for k,v in args.items() if k!='action'}
    token=(Path.home()/'.local/share/qq-hermes/.outbound-token').read_text().strip()
    try:
        async with httpx.AsyncClient(timeout=35,follow_redirects=False) as client:
            response=await client.post('http://127.0.0.1:18800/hermes-outbound/'+('action' if write else 'read'),json=payload,
                                       headers={'Authorization':'Bearer '+token})
        if response.status_code==200:return response.json()
        return {'ok':False,'state':'rejected','http_status':response.status_code,
                'error':'Check operation parameters, required target IDs, local media folders and account permissions.'}
    except Exception:return {'ok':False,'state':'uncertain' if write else 'unavailable','error':'If writing, do not retry with a new request ID.'}


def weixin_contacts():
    peers={}
    directory=Path.home()/'.hermes/weixin/accounts'
    env_path=Path.home()/'.hermes/.env'
    account=''
    for line in env_path.read_text().splitlines():
        if line.startswith('WEIXIN_ACCOUNT_ID='):
            account=line.split('=',1)[1].strip().strip(chr(34)).strip(chr(39))
    if account:
        account_path=directory/(account+'.json')
        if account_path.exists():
            identity=json.loads(account_path.read_text()).get('user_id')
            if identity:peers[identity]={'id':identity,'source':'currently paired bot user; recent conversation may be required'}
    paths=[directory/(account+'.context-tokens.json')] if account else []
    for path in paths:
        if not path.exists():continue
        # Return keys only. Values are private context tokens and never leave this process.
        for peer in json.loads(path.read_text()):peers[peer]={'id':peer,'source':'saved bot conversation'}
    return {'ok':True,'contacts':list(peers.values()),'limitation':'Known iLink bot conversations only, not the ordinary WeChat address book.'}


def approved_media(path,kind):
    # Same folder/type policy as the QQ bridge, without importing its runtime/credentials.
    p=Path(path).expanduser().resolve(strict=True);home=Path.home()
    roots=[(home/n).resolve() for n in ['Downloads','下载','Documents','文档','桌面']]+[(ROOT/'uploads').resolve()]
    relative=next((p.relative_to(r) for r in roots if p.is_relative_to(r)),None)
    if relative is None or not p.is_file() or any(x.startswith('.') for x in relative.parts):raise ValueError('Media must be in Downloads/Documents/desktop or dedicated uploads')
    if any(ord(ch)<32 for ch in str(p)):raise ValueError('Media path contains control characters')
    if p.stat().st_size>32*1024*1024:raise ValueError('Media exceeds 32 MiB')
    allowed={'.png','.jpg','.jpeg','.webp','.gif','.mp4','.mov','.webm','.mp3','.wav','.ogg','.m4a','.flac','.opus','.pdf','.txt','.md','.docx','.xlsx','.pptx','.csv','.zip'}
    if p.suffix.lower() not in allowed:raise ValueError('Unsupported media extension')
    return p


async def weixin_send(args):
    target=str(args.get('target',''))
    if target not in {r['id'] for r in weixin_contacts()['contacts']}:raise ValueError('Specify a known bot conversation ID. The recipient must message this bot first.')
    text=args.get('text','');path=args.get('path')
    if not isinstance(text,str) or (not text.strip() and not path):raise ValueError('Explicit non-empty text or attachment path is required')
    if len(text)>4000 or re.search(r'MEDIA:|!\[',text,re.I):raise ValueError('Plain text only; use the explicit path field for attachments')
    if path:
        path=approved_media(path,None);args=dict(args,file_sha256=hashlib.sha256(path.read_bytes()).hexdigest())
    async def perform():
        message=text+('\nMEDIA:'+str(path) if path else '')
        process=await asyncio.create_subprocess_exec(str(Path.home()/'.hermes/hermes-agent/.hermes/bin/hermes'),
               'send','--to','weixin:'+target,'--json','--file','-',stdin=asyncio.subprocess.PIPE,
               stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
        try:stdout,_=await asyncio.wait_for(process.communicate(message.encode()),55)
        except asyncio.TimeoutError:
            process.kill();await process.wait();raise RuntimeError('Delivery status unknown')
        if process.returncode:raise RuntimeError('Weixin delivery failed or status unknown')
        data=json.loads(stdout)
        if data.get('success') is not True:raise RuntimeError('Weixin did not confirm delivery')
        return {'ok':True,'state':'sent','target':target,'request_id':args['request_id'],
                'message_id':str(data.get('message_id',''))}
    return await once(args,perform)


async def heybox(args, original_worker):
    operation=args['operation']
    import heybox_extra,sys
    if operation in heybox_extra.READS:return await heybox_extra.read(args,original_worker,sys.modules[__name__])
    kind=args.get('search_type','all');limit=bound(args,'limit',10,20);page=bound(args,'page',1,50)
    command=[str(ROOT/'bin/heybox')]
    if operation=='search':
        query=str(args.get('query','')).strip()
        if not query or kind not in {'all','post','topic','user','game'}:raise ValueError('Explicit query and valid search_type required')
        sort=args.get('sort','relevance')
        if sort not in {'relevance','latest','hot'}:raise ValueError('Unsupported search sort')
        command+=['search',query,'--type',kind,'--sort',sort]
    elif operation=='comments':
        target=positive(args.get('target'),'Heybox post ID');sort=args.get('sort','hot')
        if sort not in {'hot','oldest','latest'}:raise ValueError('Unsupported comment sort')
        command+=['comments',str(target),'--sort',sort,'--replies='+('true' if args.get('replies') else 'false'),'--max-reply-pages','1']
    else:raise ValueError('Heybox supports only search/comments in this tool')
    command+=['--page',str(page),'--limit',str(limit),'--json','--timeout','18s']
    process=await asyncio.create_subprocess_exec(*command,env=original_worker.heybox_env(),stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
    try:stdout,_=await asyncio.wait_for(process.communicate(),50)
    except asyncio.TimeoutError:
        process.kill();await process.wait();return {'ok':False,'state':'timeout'}
    if process.returncode:return {'ok':False,'state':'unavailable','needs_login':process.returncode in {3,4}}
    return {'ok':True,'page':page,'data':json.loads(stdout)}


async def run(args, worker):
    action=args['action']
    if action=='capabilities':
        platform=args.get('platform','all')
        if platform!='all' and platform not in CAPABILITIES:raise ValueError('Unknown platform')
        return {'ok':True,'capabilities':CAPABILITIES if platform=='all' else {platform:CAPABILITIES[platform]},
                'write_validation':'Real external writes were not performed by setup; write paths are locally simulated until the user specifies concrete actions.'}
    if action=='bili_query':return await bili_read(args,worker.bili_credential)
    if action=='bili_action':return await bili_action(args,worker.bili_credential)
    if action in {'qq_query','qq_action'}:return await qq_call(args,write=action=='qq_action')
    if action=='weixin_contacts':return weixin_contacts()
    if action=='weixin_send':return await weixin_send(args)
    if action=='heybox_query':return await heybox(args,worker)
    if action=='heybox_action':
        import heybox_extra,sys
        return await heybox_extra.write(args,worker,sys.modules[__name__])
    raise ValueError('Unsupported platform extension')
