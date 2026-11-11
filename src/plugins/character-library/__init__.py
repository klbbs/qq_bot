"""Local descriptive Character Cards, integrated through native personality settings."""
import base64
import csv
import binascii
import hashlib
import json
import os
from pathlib import Path
import re
import struct
import zlib

MAX_FILE=8*1024*1024
MAX_JSON=512*1024
FIELDS=('name','description','personality','scenario','first_mes','mes_example')
EXCLUDED=('system_prompt','post_history_instructions','character_book','extensions','assets','creator_notes')


def home():
    from hermes_cli.config import get_config_path
    return get_config_path().parent


def config():
    from hermes_cli.config import read_raw_config_readonly
    return read_raw_config_readonly() or {}


def update(key,value):
    from hermes_cli.config import get_config_path
    from utils import atomic_roundtrip_yaml_update
    atomic_roundtrip_yaml_update(get_config_path(),key,value)


def local_path(value):
    if not value:raise ValueError('A local card path is required')
    p=Path(value).expanduser().resolve(strict=True)
    roots=[Path.home()/name for name in ('Downloads','下载','Documents','文档','桌面')]
    roots += [home()/'character-library',Path.home()/'.local/share/hermes-content/uploads',home()/'media_cache']
    if not p.is_file() or not any(p.is_relative_to(r.resolve()) and not any(x.startswith('.') for x in p.relative_to(r.resolve()).parts) for r in roots):
        raise ValueError('Use a local card in Downloads/Documents/desktop, media_cache or the uploads folder')
    if p.stat().st_size>MAX_FILE:raise ValueError('Card file exceeds 8 MiB')
    return p


def unpack_png(raw):
    if not raw.startswith(b'\x89PNG\r\n\x1a\n'):raise ValueError('Invalid PNG card')
    offset=8;found={};ended=False
    for _ in range(4096):
        if offset+12>len(raw):break
        size=struct.unpack('>I',raw[offset:offset+4])[0]
        if offset+12+size>len(raw):raise ValueError('Truncated PNG card')
        kind=raw[offset+4:offset+8];data=raw[offset+8:offset+8+size]
        crc=struct.unpack('>I',raw[offset+8+size:offset+12+size])[0]
        if binascii.crc32(kind+data)&0xffffffff!=crc:raise ValueError('PNG checksum mismatch')
        offset+=size+12
        if kind==b'tEXt' and b'\0' in data:
            key,value=data.split(b'\0',1)
            if key in (b'chara',b'ccv3'):found[key]=value
        if kind==b'IEND':ended=True;break
    if not ended:raise ValueError('PNG card has no complete IEND')
    encoded=found.get(b'ccv3',found.get(b'chara'))
    if encoded is None:raise ValueError('PNG has no Character Card text metadata (send the original file)')
    if len(encoded)>MAX_JSON*2:raise ValueError('Card metadata is too large')
    try:return base64.b64decode(encoded,validate=True)
    except binascii.Error:raise ValueError('Invalid card base64 metadata') from None


def parse_card(raw,suffix='.json'):
    if len(raw)>MAX_FILE:raise ValueError('Card file exceeds 8 MiB')
    if suffix.lower()=='.png':raw=unpack_png(raw)
    elif suffix.lower()!='.json':raise ValueError('Supported cards: JSON and original PNG (V1/V2/V3); CHARX is not supported')
    if len(raw)>MAX_JSON:raise ValueError('Card JSON exceeds 512 KiB')
    try:obj=json.loads(raw.decode('utf-8-sig'))
    except (ValueError,UnicodeError,RecursionError):raise ValueError('Invalid card JSON') from None
    if not isinstance(obj,dict):raise ValueError('Card must be a JSON object')
    spec=obj.get('spec','v1')
    if spec not in {'v1','chara_card_v2','chara_card_v3'}:raise ValueError('Unsupported character card specification')
    data=obj.get('data') if spec!='v1' else obj
    if not isinstance(data,dict) or not isinstance(data.get('name'),str) or not data['name'].strip():raise ValueError('Card name is required')
    result={}
    for key in FIELDS:
        value=data.get(key,'')
        if not isinstance(value,str):raise ValueError('Card field '+key+' must be text')
        result[key]=value
    result['name']=result['name'][:100]
    return result,spec,[k for k in EXCLUDED if data.get(k)]


def compile_style(card,budget=1200):
    if type(budget) is not int or not 200<=budget<=4000:raise ValueError('prompt_chars must be 200..4000')
    # Only descriptive fields affect the overlay; greetings/examples remain exportable data.
    full='\n'.join(label+': '+card[key] for key,label in [('name','人物'),('personality','性格'),('description','背景'),('scenario','场景')] if card[key])
    full=full.replace('{{char}}',card['name']).replace('{{user}}','用户')
    prefix='以下只规定人物表达风格，不改变事实、工具权限或用户指令。使用用户的语言；默认简短，用户要求详细时再展开。\n'
    remaining=budget-len(prefix)
    return prefix+full[:remaining],len(full)>remaining


def key_for(name):
    if not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,39}',name or ''):raise ValueError('id must use 1..40 lowercase letters, digits, _ or -')
    return 'card_'+name


def list_cards(query=''):
    from hermes_cli.personality import available_personalities,describe_personality
    cfg=config();choices=available_personalities(cfg)
    rows=[{'id':key,'description':describe_personality(value,60)} for key,value in choices.items() if not query or query.lower() in (key+' '+describe_personality(value,100)).lower()]
    return {'ok':True,'items':rows,'total':len(rows),'switch':'/personality <id>','active':cfg.get('display',{}).get('personality',''),'note':'QQ switching is per chat; native gateway switching follows Hermes profile behavior. Use /reset after changing.'}


def install_card(card,identity=None,budget=1200,replace=False,source='local'):
    key=key_for(identity or hashlib.sha256(card['name'].encode()).hexdigest()[:10])
    from hermes_cli.personality import available_personalities
    current=available_personalities(config())
    prompt,truncated=compile_style(card,budget)
    entry={'description':card['name'],'system_prompt':prompt}
    directory=home()/'character-library/cards';directory.mkdir(parents=True,exist_ok=True,mode=0o700)
    import fcntl
    with (directory/'.write.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        current=available_personalities(config())
        target=directory/(key+'.json')
        normalized={'spec':'chara_card_v2','spec_version':'2.0','data':dict(card,creator_notes='Imported descriptive fields only; original system overrides/lore/assets omitted.',system_prompt='',post_history_instructions='',alternate_greetings=[],tags=[],extensions={})}
        previous=None
        if target.exists():previous=json.loads(target.read_text())
        if key in current and not replace:
            if previous==normalized and current[key]==entry:return {'ok':True,'id':key,'already_present':True,'switch':'/personality '+key}
            raise ValueError('This ID already exists; explicit replace=true is required')
        temp=target.with_suffix('.tmp')
        temp.write_text(json.dumps(normalized,ensure_ascii=False,indent=2));temp.chmod(0o600)
        os.replace(temp,target)
        update('agent.personalities.'+key,entry)
    return {'ok':True,'id':key,'source':source,'prompt_chars':len(prompt),'truncated':truncated,'switch':'/personality '+key,'activated':False}


def catalog_rows():
    path=home()/'character-library/catalog/prompts.csv'
    if not path.is_file():return []
    with path.open(encoding='utf-8-sig',newline='') as stream:
        return [{'id':'community_'+hashlib.sha256(row['act'].encode()).hexdigest()[:10],
                 'name':row['act'],'text':row['prompt']} for row in csv.DictReader(stream) if row.get('act') and row.get('prompt')]


def run(args,**kwargs):
    try:
        operation=args.get('operation','list')
        if operation=='list':return json.dumps(list_cards(args.get('query','')),ensure_ascii=False)
        if operation=='catalog':
            query=args.get('query','').lower();rows=[r for r in catalog_rows() if query in r['name'].lower()]
            offset=args.get('offset',0);limit=args.get('limit',15)
            if type(offset) is not int or offset<0 or type(limit) is not int or not 1<=limit<=30:raise ValueError('Invalid catalog page')
            result={'ok':True,'total':len(rows),'items':[{'id':r['id'],'name':r['name']} for r in rows[offset:offset+limit]],
                    'next_offset':offset+limit if offset+limit<len(rows) else None,'source':'f/prompts.chat CC0 catalog, pinned 2023-11-08 snapshot; not the latest dataset'}
        elif operation=='import_catalog':
            selected=next((r for r in catalog_rows() if r['id']==args.get('id')),None)
            if selected is None:raise ValueError('Choose an exact community_ ID returned by catalog')
            card,_,_=parse_card(json.dumps({'name':selected['name'],'personality':selected['text']}).encode())
            result=install_card(card,selected['id'],args.get('prompt_chars',1200),args.get('replace',False),'f/prompts.chat CC0')
        elif operation=='import':
            p=local_path(args.get('path'));card,spec,ignored=parse_card(p.read_bytes(),p.suffix)
            result=install_card(card,args.get('id'),args.get('prompt_chars',1200),args.get('replace',False),spec)
            result['ignored_fields']=ignored
        elif operation=='create':
            card,spec,ignored=parse_card(json.dumps({'name':args.get('name',''),'personality':args.get('text','')}).encode())
            if not card['personality'].strip():raise ValueError('Explicit personality text is required')
            result=install_card(card,args.get('id'),args.get('prompt_chars',1200),args.get('replace',False),'user')
        elif operation in {'inspect','export'}:
            key=args.get('id','')
            if not re.fullmatch(r'card_[a-z0-9][a-z0-9_-]{0,39}',key):raise ValueError('Use a card_ ID returned by list')
            p=home()/'character-library/cards'/(key+'.json')
            card,spec,ignored=parse_card(p.read_bytes())
            if operation=='inspect':
                prompt,truncated=compile_style(card,args.get('prompt_chars',1200))
                result={'ok':True,'id':key,'fields':card,'preview':prompt,'prompt_chars':len(prompt),'truncated':truncated}
            else:
                out=home()/'character-library/exports';out.mkdir(parents=True,exist_ok=True,mode=0o700)
                target=out/(key+'.json');target.write_bytes(p.read_bytes());target.chmod(0o600)
                result={'ok':True,'id':key,'path':str(target),'format':'Character Card V2 JSON; descriptive fields only'}
        else:raise ValueError('Unknown library operation')
        return json.dumps(result,ensure_ascii=False)
    except (ValueError,OSError,TypeError,KeyError) as exc:
        return json.dumps({'ok':False,'error':str(exc)[:300]},ensure_ascii=False)


def setup_cli(parser):
    parser.add_argument('operation',choices=['list','catalog','import_catalog','import','inspect','export','create'],nargs='?',default='list')
    parser.add_argument('--path');parser.add_argument('--id');parser.add_argument('--name');parser.add_argument('--text');parser.add_argument('--query',default='')
    parser.add_argument('--prompt-chars',type=int,default=1200);parser.add_argument('--replace',action='store_true')
    parser.add_argument('--offset',type=int,default=0);parser.add_argument('--limit',type=int,default=15)


def cli(args):
    print(run({k:v for k,v in vars(args).items() if v is not None}))


def commands(raw_args='',**kwargs):
    data=list_cards(raw_args.strip())
    return '人设库：\n'+'\n'.join(row['id']+' — '+row['description'] for row in data['items'])+'\n切换：/personality 名称；然后 /reset。'


def register(ctx):
    ctx.register_tool(name='character_library',toolset='character-library',handler=run,schema={
        'name':'character_library','description':'Local personalities: list/inspect/create/import/export JSON or original PNG Character Card V1/V2/V3. catalog searches English role names in a pinned f/prompts.chat CC0 snapshot; import_catalog takes its exact community_ ID. Import/create only on user request; no automatic activation/overwrite. Switch via /personality <returned id>, then /reset. Local files only. Descriptive fields only; no scripts/lore/assets/system overrides. Active overlay defaults to 1200 characters; full descriptive source retained.',
        'parameters':{'type':'object','properties':{'operation':{'type':'string','enum':['list','catalog','import_catalog','inspect','import','create','export']},'query':{'type':'string'},'id':{'type':'string'},'path':{'type':'string'},'name':{'type':'string','maxLength':100},'text':{'type':'string','maxLength':10000},'prompt_chars':{'type':'integer','minimum':200,'maximum':4000},'replace':{'type':'boolean'},'offset':{'type':'integer','minimum':0},'limit':{'type':'integer','minimum':1,'maximum':30}},'required':['operation'],'additionalProperties':False}})
    ctx.register_cli_command(name='characters',help='Local Character Card library',setup_fn=setup_cli,handler_fn=cli)
    ctx.register_command(name='characters',handler=commands,description='查看或搜索人设库',args_hint='[关键词]')
