"""Allowlisted QQ reads, media and explicitly requested account/group actions."""
import hashlib
import json
import warnings
from pathlib import Path
from typing import Annotated, Literal

import nonebot
from fastapi import Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field, StrictStr, StrictBool, StrictInt
from PIL import Image, UnidentifiedImageError
import outbound
from qq_delivery_guard import SendBlocked
import qq_management as management
from nonebot.adapters.onebot.v11 import ActionFailed


class ReadRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    operation: Literal['profile','group','members','history','message','group_files','account_status','member','group_notices','group_essence','group_honors','group_muted','group_file_folder','group_file_info','group_file_url','recent_contacts','pending_requests','forward_message','friend_categories','one_way_friends','profile_likes','user_status','group_at_all_remaining','group_extended','group_system_messages','custom_stickers']
    target_type: Literal['private','group'] | None = None
    target_id: StrictStr | None = Field(default=None, pattern=r'^[1-9][0-9]{4,19}$')
    message_id: StrictStr | None = Field(default=None, pattern=r'^-?[0-9]{1,25}$')
    limit: int = Field(default=20, ge=1, le=50)
    offset: int = Field(default=0, ge=0, le=10000)

    user_id: StrictStr | None = Field(default=None, pattern=r'^[1-9][0-9]{4,19}$')
    folder_id: StrictStr | None = Field(default=None, min_length=1, max_length=200)
    file_id: StrictStr | None = Field(default=None, min_length=1, max_length=200)
    busid: StrictInt | None = Field(default=None, ge=0, le=2147483647)


class ActionRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    operation: Literal['send_media','reply','forward','recall','friend_remark','group_card','group_name','group_notice','set_nickname','set_avatar','set_signature','publish_qzone','group_leave','group_dismiss','group_kick','group_ban','group_whole_ban','group_admin','group_special_title','group_avatar','group_remark','group_sign','group_notice_delete','essence_set','essence_delete','group_poke','friend_delete','friend_like','friend_poke','friend_request','group_request','mark_read','group_folder_create','group_folder_delete','group_file_delete','group_file_rename','group_file_move','group_file_upload','message_reaction','send_at','contact_card','online_status','music_card']
    target_type: Literal['private','group'] | None = None
    target_id: StrictStr | None = Field(default=None, pattern=r'^[1-9][0-9]{4,19}$')
    message_id: StrictStr | None = Field(default=None, pattern=r'^-?[0-9]{1,25}$')
    user_id: StrictStr | None = Field(default=None, pattern=r'^[1-9][0-9]{4,19}$')
    text: StrictStr | None = Field(default=None, max_length=4000)
    media_type: Literal['image','video','audio','file'] | None = None
    as_sticker: bool | None = None
    path: StrictStr | None = None
    image_paths: list[StrictStr] | None = Field(default=None, max_length=9)
    visibility: Literal['public','friends','private','include','exclude'] | None = None
    audience_ids: list[Annotated[StrictStr, Field(pattern=r'^[1-9][0-9]{4,19}$')]] | None = Field(default=None, max_length=100)
    enable: StrictBool | None = None
    approve: StrictBool | None = None
    reject_add_request: StrictBool | None = None
    duration: StrictInt | None = Field(default=None, ge=0, le=2592000)
    times: StrictInt | None = Field(default=None, ge=1, le=10)
    pending_id: StrictStr | None = Field(default=None, pattern=r'^qreq_[a-f0-9]{32}$')
    notice_id: StrictStr | None = Field(default=None, min_length=1, max_length=200)
    folder_id: StrictStr | None = Field(default=None, min_length=1, max_length=200)
    file_id: StrictStr | None = Field(default=None, min_length=1, max_length=200)
    busid: StrictInt | None = Field(default=None, ge=0, le=2147483647)
    emoji_id: StrictStr | None = Field(default=None, pattern=r'^[0-9]{1,20}$')
    time: StrictInt | None = Field(default=None, ge=0, le=9999999999999)
    mention_ids: list[Annotated[StrictStr, Field(pattern=r'^[1-9][0-9]{4,19}$')]] | None = Field(default=None, max_length=20)
    mention_all: StrictBool | None = None
    contact_kind: Literal['qq','group'] | None = None
    contact_id: StrictStr | None = Field(default=None, pattern=r'^[1-9][0-9]{4,19}$')
    online_state: Literal['online','away','busy','qme','dnd','invisible'] | None = None
    music_provider: Literal['qq','163','kugou','migu','kuwo'] | None = None
    music_id: StrictStr | None = Field(default=None, pattern=r'^[A-Za-z0-9_-]{1,100}$')
    request_id: StrictStr = Field(pattern=r'^[A-Za-z0-9_-]{8,80}$')


def allowed_roots():
    home=Path.home()
    return [home/n for n in ['Downloads','下载','Documents','文档','桌面']] + [home/'.local/share/hermes-content/uploads']


def media_file(path, media_type):
    p=Path(path or '').expanduser().resolve(strict=True)
    roots=[x.resolve() for x in allowed_roots()]
    if not p.is_file() or not any(p.is_relative_to(root) for root in roots):
        raise ValueError('File must be in Downloads/Documents/desktop or the dedicated uploads folder')
    relative=next(p.relative_to(root) for root in roots if p.is_relative_to(root))
    if any(part.startswith('.') for part in relative.parts):
        raise ValueError('Hidden configuration files cannot be sent')
    if p.stat().st_size > 32*1024*1024:
        raise ValueError('File exceeds 32 MiB')
    formats={'image':{'.jpg','.jpeg','.png','.gif','.webp'},'video':{'.mp4','.mov','.webm'},
             'audio':{'.mp3','.wav','.ogg','.m4a','.flac','.opus'},
             'file':{'.txt','.md','.pdf','.doc','.docx','.xls','.xlsx','.ppt','.pptx','.csv','.tsv','.zip','.7z','.png','.jpg','.mp4','.mp3'}}
    if media_type not in formats or p.suffix.lower() not in formats[media_type]:
        raise ValueError('File type does not match the requested media type')
    return p


def message_view(row):
    segments=row.get('message',[])
    if isinstance(segments,str):
        segments=[{'type':'text','data':{'text':segments}}]
    return {'message_id':str(row.get('message_id','')), 'time':row.get('time'),
            'sender_id':str(row.get('sender',{}).get('user_id',row.get('user_id',''))),
            'sender_name':row.get('sender',{}).get('nickname',''),
            'segments':[{'type':s.get('type'),'text':str(s.get('data',{}).get('text',''))[:4000]}
                        for s in segments[:30]]}


def image_file(path):
    if not path:raise ValueError('Explicit local image path is required')
    p=media_file(path,'image')
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error',Image.DecompressionBombWarning)
            with Image.open(p) as image:
                if image.format not in {'JPEG','PNG','GIF','WEBP'} or max(image.size)>4096:
                    raise ValueError('Image must be JPEG/PNG/GIF/WebP, at most 4096 pixels per side')
                image.verify()
            with Image.open(p) as image:image.load()
    except (OSError,UnidentifiedImageError,Image.DecompressionBombError,Image.DecompressionBombWarning):
        raise ValueError('Image must be a valid, readable image') from None
    return p


async def read(body):
    if body.operation=='pending_requests':return await management.read(body,None,message_view)
    bot=nonebot.get_bot('__QQ_BOT__')
    if body.operation in management.EXTRA_READS:return await management.read(body,bot,message_view)
    op=body.operation
    if op=='message':
        if body.message_id is None:raise ValueError('message_id is required')
        return {'ok':True,'message':message_view(await bot.get_msg(message_id=int(body.message_id)))}
    if not body.target_id:raise ValueError('An explicit target_id is required')
    target=int(body.target_id)
    if op=='profile':
        row=await bot.get_stranger_info(user_id=target,no_cache=False)
        return {'ok':True,'profile':{k:row.get(k) for k in ['user_id','nickname','sex','age']}}
    if op=='group':
        row=await bot.get_group_info(group_id=target,no_cache=False)
        return {'ok':True,'group':{k:row.get(k) for k in ['group_id','group_name','member_count','max_member_count']}}
    if op=='members':
        rows=await bot.get_group_member_list(group_id=target,no_cache=False)
        return {'ok':True,'total':len(rows),'offset':body.offset,
                'members':[{k:r.get(k) for k in ['user_id','nickname','card','role','join_time','last_sent_time']}
                           for r in rows[body.offset:body.offset+body.limit]],
                'next_offset':body.offset+body.limit if body.offset+body.limit<len(rows) else None}
    if op=='group_files':
        row=await bot.call_api('get_group_root_files',group_id=target)
        return {'ok':True,'files':[{k:r.get(k) for k in ['file_id','file_name','file_size','busid','upload_time','uploader']}
                                 for r in (row.get('files') or [])[body.offset:body.offset+body.limit]],
                'folders':[{k:r.get(k) for k in ['folder_id','folder_name','total_file_count']}
                           for r in (row.get('folders') or [])[:body.limit]],'limitation':'Root folder listing only.'}
    if body.target_type not in {'private','group'}:raise ValueError('history requires target_type')
    name,key=('get_friend_msg_history','user_id') if body.target_type=='private' else ('get_group_msg_history','group_id')
    result=await bot.call_api(name,**{key:target,'count':body.limit})
    rows=result.get('messages',[]) if isinstance(result,dict) else result
    return {'ok':True,'source_kind':'available_recent_messages','messages':[message_view(r) for r in rows[:body.limit]],
            'limitation':'Only messages returned by the dedicated QQ session; not guaranteed complete history. Media is represented by type, not its visual/audio content.'}


def action_parameters(body):
    op=body.operation
    if op in management.EXTRA_ACTIONS:return management.parameters(body,image_file,media_file)
    if any(getattr(body,k) is not None for k in management.ACTION_FIELDS):
        raise ValueError('Management fields do not apply to this legacy operation')
    if op=='recall':
        if body.message_id is None:raise ValueError('message_id is required')
        return 'delete_msg',{'message_id':int(body.message_id)}
    if not body.target_id:raise ValueError('Explicit target_id is required')
    target=int(body.target_id)
    if op in {'set_nickname','set_avatar','set_signature','publish_qzone'}:
        if body.target_id!='__QQ_BOT__' or body.target_type not in {None,'private'}:
            raise ValueError('Self-account actions only support the dedicated bot QQ, with target_type private')
        if body.message_id is not None or body.user_id is not None:
            raise ValueError('Self-account actions do not accept message_id or user_id')
        if op=='publish_qzone':
            if body.path is not None or body.media_type is not None:
                raise ValueError('publish_qzone uses image_paths, not path/media_type')
            if body.text is None:raise ValueError('Explicit text is required (empty only for an image post)')
            if not body.text.strip() and not body.image_paths:raise ValueError('Qzone post needs text or images')
            rights={'public':1,'friends':4,'include':16,'private':64,'exclude':128}
            if body.visibility is None:raise ValueError('Explicit visibility is required: public/friends/private/include/exclude')
            if body.visibility in {'include','exclude'}:
                if not body.audience_ids:raise ValueError('Selected audience requires explicit audience_ids')
            elif body.audience_ids is not None:
                raise ValueError('audience_ids only applies to include/exclude visibility')
            images=[image_file(path) for path in body.image_paths or []]
            if sum(p.stat().st_size for p in images)>32*1024*1024:raise ValueError('Qzone images exceed 32 MiB in total')
            return 'send_qzone_msg',{'content':body.text,'images':[p.as_uri() for p in images],
                                     'ugc_right':rights[body.visibility],'target_uins':body.audience_ids or []}
        if body.image_paths is not None or body.visibility is not None or body.audience_ids is not None:
            raise ValueError('Profile changes do not accept Qzone fields')
        if op=='set_avatar':
            if body.text is not None or body.media_type not in {None,'image'}:
                raise ValueError('set_avatar requires path only; text is not accepted')
            return 'set_qq_avatar',{'file':image_file(body.path).as_uri()}
        if body.path is not None or body.media_type is not None:
            raise ValueError('Nickname/signature changes require text only')
        if body.text is None:raise ValueError('Explicit text is required')
        if op=='set_nickname':
            if not body.text.strip():raise ValueError('Nickname must not be empty')
            return 'set_qq_profile',{'nickname':body.text}
        return 'set_self_longnick',{'longNick':body.text}
    if op in {'send_media','reply','forward'}:
        if body.target_type not in {'private','group'}:raise ValueError('target_type is required')
        key='user_id' if body.target_type=='private' else 'group_id'
        if op=='forward':
            if body.message_id is None:raise ValueError('message_id is required')
            return ('forward_friend_single_msg' if key=='user_id' else 'forward_group_single_msg'),{key:target,'message_id':body.message_id}
        if op=='send_media':
            if body.as_sticker is not None and body.media_type!='image':
                raise ValueError('as_sticker applies only to media_type image')
            path=media_file(body.path,body.media_type)
            if body.media_type=='file':
                if body.text:raise ValueError('File uploads do not support captions; send the requested text separately')
                return ('upload_private_file' if key=='user_id' else 'upload_group_file'),{key:target,'file':str(path),'name':path.name}
            # sub_type=1 marks the picture as an animated sticker (表情) instead of a normal image.
            image_data={'file':path.as_uri()}
            if body.as_sticker:image_data['sub_type']=1
            segments=[{'type':{'audio':'record'}.get(body.media_type,body.media_type),'data':image_data}]
            if body.text:segments.append({'type':'text','data':{'text':body.text}})
        else:
            if body.message_id is None or not body.text or not body.text.strip():raise ValueError('Reply requires message_id and exact text')
            segments=[{'type':'reply','data':{'id':body.message_id}},{'type':'text','data':{'text':body.text}}]
        return ('send_private_msg' if key=='user_id' else 'send_group_msg'),{key:target,'message':segments}
    if body.text is None:raise ValueError('Explicit text is required')
    if op=='friend_remark':
        if body.target_type not in {None,'private'}:raise ValueError('friend_remark requires a private recipient')
        return 'set_friend_remark',{'user_id':target,'remark':body.text}
    if body.target_type!='group':raise ValueError('Group actions require target_type group')
    if op=='group_card':
        if not body.user_id:raise ValueError('Explicit member user_id is required')
        return 'set_group_card',{'group_id':target,'user_id':int(body.user_id),'card':body.text}
    if not body.text.strip():raise ValueError('Text must not be empty')
    if op=='group_name':return 'set_group_name',{'group_id':target,'group_name':body.text}
    return '_send_group_notice',{'group_id':target,'content':body.text}


async def action(body):
    name,params=action_parameters(body)
    bot=nonebot.get_bot('__QQ_BOT__')
    if body.operation=='recall':
        message=await bot.get_msg(message_id=int(body.message_id))
        if str(message.get('sender',{}).get('user_id',message.get('user_id','')))!='__QQ_BOT__':
            raise ValueError('Only messages sent by the dedicated bot may be recalled')
    # Include file bytes, not just its name, in deduplication to reject changed content.
    payload=body.model_dump(exclude={'request_id'})
    # Preserve hashes for requests recorded before Qzone fields were introduced.
    for key in ('image_paths','visibility','audience_ids',*management.ACTION_FIELDS):
        if payload[key] is None:payload.pop(key)
    if body.operation in {'send_media','set_avatar','group_avatar','group_file_upload'}:
        payload['file_sha256']=hashlib.sha256(media_file(body.path,'image' if body.operation in {'set_avatar','group_avatar'} else 'file' if body.operation=='group_file_upload' else body.media_type).read_bytes()).hexdigest()
    if body.operation=='publish_qzone':
        payload['image_sha256']=[hashlib.sha256(media_file(path,'image').read_bytes()).hexdigest() for path in body.image_paths or []]
    digest=hashlib.sha256(json.dumps(payload,sort_keys=True).encode()).hexdigest()
    async with outbound.LOCK:
        with outbound.database() as db:
            old=db.execute('SELECT digest,result FROM sends WHERE id=?',(body.request_id,)).fetchone()
            if old:
                if old[0]!=digest:raise HTTPException(409,'Request ID conflict')
                return json.loads(old[1])
            await management.preflight(body,bot)
            pending={'ok':False,'state':'uncertain','request_id':body.request_id,'error':'Do not retry with a new request ID.'}
            db.execute('INSERT INTO sends VALUES (?,?,?)',(body.request_id,digest,json.dumps(pending)))
            if body.operation in {'friend_request','group_request'}:
                db.execute('UPDATE qq_requests SET state=? WHERE id=?',('uncertain',body.pending_id))
            db.commit()
            try:
                data=await bot.call_api(name,**params)
                answer={'ok':True,'state':'submitted','operation':body.operation,'request_id':body.request_id}
                if body.operation in {'set_nickname','set_avatar','set_signature'} and isinstance(data,dict) and data.get('result') not in (None,0,'0'):
                    answer={'ok':False,'state':'rejected','operation':body.operation,'request_id':body.request_id,'error':'QQ rejected the profile change'}
                if isinstance(data,dict) and data.get('message_id') is not None:answer['message_id']=str(data['message_id'])
                if body.operation=='publish_qzone':
                    if not isinstance(data,dict) or not data.get('tid'):return pending
                    answer['tid']=str(data['tid'])
                    answer['visibility']=body.visibility
                if name in {'send_private_msg','send_group_msg'} and 'message_id' not in answer:return pending
                if body.operation in management.EXTRA_ACTIONS and isinstance(data,dict) and (data.get('result') is False or data.get('retcode',0) not in (0,'0',None)):
                    answer={'ok':False,'state':'rejected','operation':body.operation,'request_id':body.request_id,'error':'QQ rejected the requested action'}
                db.execute('UPDATE sends SET result=? WHERE id=?',(json.dumps(answer),body.request_id));db.commit()
                if answer['ok']:management.request_submitted(body)
                return answer
            except SendBlocked as exc:
                answer={'ok':False,'state':'not_sent','operation':body.operation,'request_id':body.request_id,
                        'reason':exc.reason,'retry_after':exc.retry_after}
                db.execute('UPDATE sends SET result=? WHERE id=?',(json.dumps(answer),body.request_id));db.commit()
                return answer
            except ActionFailed:
                answer={'ok':False,'state':'rejected','operation':body.operation,'request_id':body.request_id,'error':'QQ rejected the action or the account lacks permission'}
                db.execute('UPDATE sends SET result=? WHERE id=?',(json.dumps(answer),body.request_id));db.commit()
                return answer
            except Exception:return pending


def register():
    management.register_requests()
    app=nonebot.get_driver().server_app
    @app.post('/hermes-outbound/read')
    async def read_route(body:ReadRequest,authorization:str=Header(default='')):
        outbound.authorize(authorization)
        try:return await read(body)
        except ValueError as exc:raise HTTPException(422,str(exc)) from None
        except Exception:raise HTTPException(503,'QQ read unavailable or permission denied') from None
    @app.post('/hermes-outbound/action')
    async def action_route(body:ActionRequest,authorization:str=Header(default='')):
        outbound.authorize(authorization)
        try:return await action(body)
        except (ValueError,FileNotFoundError) as exc:raise HTTPException(422,str(exc)) from None
        except HTTPException:raise
        except Exception:raise HTTPException(503,'QQ action unavailable or permission denied') from None
