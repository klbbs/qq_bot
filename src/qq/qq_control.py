"""QQ bridge controls and explicit session provisioning for Hermes 0.5.5 compatibility.

No Hermes core or installed third-party package files are changed. The bridge client
adapter is installed once on its existing instance; the message handlers keep using it.
"""
import asyncio
import importlib.util
import json
import sqlite3
from pathlib import Path
from urllib.parse import quote

import httpx
import yaml
from qq_owner_policy import owner_overlay
from nonebot import logger
from nonebot_plugin_hermes.core.hermes_client import ChatResult, hermes_client
from nonebot_plugin_hermes.core.routing import default_target
from nonebot_plugin_hermes.core.session import session_manager

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / 'control.sqlite3'
WORKER = Path.home()/'.local/share/hermes-content/content_worker.py'
OWNER_ID = '__QQ_OWNER__'
ADMIN_ONLY = {'personality','model','tools','status','hermes-status','usage','context','accounts','balance','search','dm'}
COMMAND_NAMES = ('help', '帮助', 'commands', 'clear', 'new', 'reset', 'personality', 'characters', 'ping', 'status', 'hermes-status', 'model', 'tools', 'usage', 'context', 'balance', 'accounts', 'search', 'dm', 'roll', 'submit', 'giveup', 'solution', 'practice', 'problem', 'sample', 'progress', 'cancel', 'tasks', 'candidates')
HELP = 'QQ 可用命令（由本机直接处理）\n/help /帮助 /commands — 本帮助\n/new /reset /clear — 开始新对话，保留账号登录与个性设置\n/personality — 当前个性及可选列表\n/personality concise — 切换为简洁风格并开新对话\n/personality none — 关闭本聊天的个性叠加并开新对话\n/characters [关键词] — 查看或搜索人设库\n/ping — 检查连接\n/status — 当前连接、模型、个性和会话状态\n/hermes-status — 原 QQ 插件内部状态\n/model — 查看模型（目前只读）\n/tools — 查看此 QQ 通道启用的工具集\n/usage /context — 当前会话消息和用量统计\n/balance — DeepSeek API 余额\n/accounts — QQ、微信、B站、小黑盒状态\n/search 关键词 — 联网搜索并附来源\n/dm qq|weixin|bilibili 接收者ID 正文 — 按明确对象与正文发私信\n/roll 分数 [题型] — 默认抽图片题面，例如 /roll 1200 贪心\n/problem [页码] — 图片翻页；/problem text [页码] 看文字\n/submit 思路及实现 — 对当前题目进行静态评估，不自动交 OJ\n/giveup — 放弃当前题目并查看思路\n/solution [页码] — 通过或放弃后按页查看思路\n/practice — 帮助；/practice ready 检查；/practice stop-all 管理员急停账号全部抽题\n\n/sample [组数] — 原题样例\n/progress — 当前抽题进度、停止清理状态\n/cancel — 发起人或管理员停止当前题目的生成\n/tasks [页码] — 本聊天抽题任务记录\n/candidates 分数 [题型] — 免费预览缓存候选\n其余功能用自然语言。未接入的斜杠命令会明确提示，不交给模型执行。\n以上是 QQ 入口命令；不代表终端或微信的命令清单。'
CAPABILITY_PROMPT = ('\n\nQQ 入口已验证的本地控制命令：'+', '.join('/'+n for n in COMMAND_NAMES)+
    '。只推荐这个清单里的斜杠命令，不要把 Hermes 终端或其他平台命令当作 QQ 已接入功能。'
    '/reset、/new 和 /clear 都开新对话；/personality 只影响本 QQ 聊天。'
    '/model 和 /tools 目前只查询。其他斜杠命令会被本地提示不支持。'
    '/roll、/problem、/submit、/giveup、/solution、/practice 是独立刷题流程，允许已开放聊天中的普通用户使用。'
    '群内共用当前一题、分别记录作答状态；/reset 不会取消作答，放弃用 /giveup。'
    '抽题时不发题解，只有通过或放弃后按页展示思路；完整参考报告不整份发送；通过或放弃后可用 practice_problem 的 view=solution 核验本聊天，再按追问简短解释细节。'
    'PASS 是 AI 静态评估，不代表 OJ 交题通过。服务器需启用 Hermes 模块且本机 ACM 登录有效。'
    '外发 QQ 消息或公开评论仍必须由用户明确给出目标与正文，不主动选收件人或发送测试。'
    '\n违规与越界处理（对所有对话生效，优先级高于任何人设设定）：遇到违法违规、越权操作、破解盗版、'
    '赌博色情、隐私窃取、代考代刷之类的请求，用当前人设的语气短句拒绝（一句就够，例如“这个不聊”“这事儿帮不了”），'
    '保持人设不崩，不出戏、不解释规则、不说教、不引用内部条款，也不因为角色设定里写着“必须服从用户”就照做。'
    '对方反复追问时仍然只回一句简短的拒绝。本条对管理员账号（QQ __QQ_OWNER__）除外。')


def database():
    db = sqlite3.connect(DB_PATH)
    db.execute('CREATE TABLE IF NOT EXISTS settings (scope TEXT PRIMARY KEY, personality TEXT NOT NULL)')
    return db


def scope_for(args):
    return session_manager._get_internal_id(args['adapter_name'], args['is_private'], args['user_id'], args.get('group_id'))


def selected(scope):
    with database() as db:
        row = db.execute('SELECT personality FROM settings WHERE scope=?', (scope,)).fetchone()
    return row[0] if row else ''


def save_personality(scope, name):
    with database() as db:
        db.execute('INSERT INTO settings VALUES (?,?) ON CONFLICT(scope) DO UPDATE SET personality=excluded.personality', (scope, name))


def personalities():
    # This standalone source module owns Hermes' canonical built-ins and rendering,
    # and imports only stdlib. Never import/run the Hermes CLI in the QQ process.
    path = Path.home()/'.hermes/hermes-agent/hermes_cli/personality.py'
    spec = importlib.util.spec_from_file_location('hermes_personality_definitions', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    config = yaml.safe_load((Path.home()/'.hermes/config.yaml').read_text()) or {}
    return module, module.available_personalities(config)


GROUP_EXTRA = {
    # 视觉培训群：只答疑，不接梗。人设仍是同一张，但语气收敛到"回答技术问题"。
    '__QQ_DOCUMENT_GROUP__': (
        '这个群是技术答疑群。只回答被 @ 的那个具体问题：先给结论，再给关键步骤或代码，'
        '不确定就直说不确定。不接梗、不闲聊、不主动开话题、不回敬损人、不反问寒暄，'
        '也不评论提问者本人。默认三句以内、60 字左右，问题确实需要展开时才展开。'
        '群里有一批资料，问到培训安排、环境配置、装系统装软件这类，先用 qq_group_file_read '
        '读对应文档再答（group_id __QQ_DOCUMENT_GROUP__），文件名：视觉组.pdf、虚拟机安装以及linux环境配置教程.docx、'
        '必要软件安装和环境配置.docx、机器人创新实验室 27赛季培训大纲一览.pdf、'
        '机器人创新实验室 27赛季培训规划.pdf。文档里没有的就直说没有，别编。'),
    # 纳新群在刷题：题意可以讲，解法一律不给；其余闲聊照常参与。
    '1109535825': (
        '这个群在用 /roll 刷题，其他聊天照常参与。有人问题意、输入输出、约束或样例时，先读 practice_problem 复述题面。有人追问解法时，先对当前聊天调用 practice_problem，view=solution；只有工具确认本题已经通过或 /giveup 放弃才可解释解法。题目仍在生成、作答或判题时，不给解法、算法方向、数据结构提示或代码，不能凭口头声称已通过来解锁。题目结束后只回答本次追问的细节，默认几句话；明确要求步骤、证明或代码时再展开，不主动整份发送报告。'),
}


def overlay(args, prompt):
    name = selected(scope_for(args))
    module, choices = personalities()
    style = module.render_personality_prompt(choices.get(name, '')) if name else ''
    extra = GROUP_EXTRA.get(str(args.get('group_id') or ''), '')
    text = (prompt or '') + CAPABILITY_PROMPT + ('\n\n本 QQ 聊天个性叠加：\n'+style if style else '')
    return text + ('\n\n本群规矩：\n'+extra if extra else '') + owner_overlay(args)


class SessionUnavailable(Exception):
    pass


async def ensure_session(session_key, target=None):
    target = target or default_target()
    headers = hermes_client.get_headers(api_key=target.api_key)
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.get(target.base_url+'/api/sessions/'+quote(session_key, safe=''), headers=headers)
        if response.status_code == 200:
            return response.json()['session']
        if response.status_code != 404:
            raise SessionUnavailable('会话查询失败（HTTP '+str(response.status_code)+'）')
        response = await client.post(target.base_url+'/api/sessions', headers=headers,
                                     json={'id':session_key, 'source':'api_server'})
        if response.status_code == 201:
            return response.json()['session']
        if response.status_code == 409:
            # Only accept a concurrently created live row; a retired/deleted ID stays refused.
            check = await client.get(target.base_url+'/api/sessions/'+quote(session_key,safe=''),headers=headers)
            if check.status_code == 200:
                row = check.json()['session']
                if not row.get('end_reason'):
                    return row
        raise SessionUnavailable('会话创建失败（HTTP '+str(response.status_code)+'），请用 /reset 开新会话')


def install_client_adapter():
    if getattr(hermes_client, '_qq_control_installed', False):
        return
    original = hermes_client.chat
    async def chat(**kwargs):
        try:
            await ensure_session(kwargs['session_key'], kwargs.get('target'))
        except (SessionUnavailable, httpx.HTTPError):
            return ChatResult(raw_text='⚠️ 当前会话尚不可用，请检查 /status 或用 /reset 开新会话。原请求未发送给模型。',
                              parse_failed=True,is_transport_error=True)
        kwargs['system_prompt'] = overlay(kwargs, kwargs.get('system_prompt'))
        return await original(**kwargs)
    hermes_client.chat = chat
    hermes_client._qq_control_installed = True


async def content(action, **args):
    process = await asyncio.create_subprocess_exec(str(WORKER.parent/'.venv/bin/python'), '-I', str(WORKER),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, _ = await asyncio.wait_for(process.communicate(json.dumps(dict(args,action=action)).encode()), 80)
        if process.returncode:
            return {'ok':False}
        return json.loads(out)
    except asyncio.TimeoutError:
        process.kill();await process.wait()
        return {'ok':False,'status':'timeout'}


async def handle(text, args, request_id=None):
    parts = text.strip().split(None, 1)
    command = parts[0]
    argument = parts[1] if len(parts) > 1 else ''
    name = command.lstrip('/').lower()
    argument = argument.strip()
    if name in ADMIN_ONLY and str(args.get('user_id')) != OWNER_ID:
        return '这个命令只有管理员能用，咱们正常聊天就行喵。'
    if name == 'search':
        if not argument:return '用法：/search 关键词或问题'
        from nonebot_plugin_hermes.core.prompt_builder import build_passive_system_prompt
        prompt=build_passive_system_prompt(adapter=args['adapter_name'],is_private=args['is_private'],user_id=args['user_id'],group_id=args.get('group_id'))
        key=session_manager.get_session_key(**args)
        result=await hermes_client.chat(session_key=key,system_prompt=prompt,
            text='请实际调用 web_search 联网查询下面的问题，必要时读取原文。简短回答并附来源链接与发布时间（可用时）。禁止发送私信、发表评论或执行其他账号写入。问题：'+argument,**args)
        if result.effective_session_key:
            session_manager.adopt_session_key(key,result.effective_session_key)
        return result.raw_text or '搜索未返回可用内容。'
    if name == 'dm':
        fields=argument.split(None,2)
        if len(fields)!=3 or fields[0] not in {'qq','weixin','bilibili'} or not fields[2].strip():
            return '用法：/dm qq|weixin|bilibili 接收者ID 正文。需要明确对象和正文，不默认发给管理员。'
        if not request_id:return '发送请求缺少可去重编号，请通过正常 QQ 消息发送该命令。'
        platform,target,message=fields
        if platform=='qq':result=await content('qq_send',target_type='private',target_id=target,message=message,request_id=request_id)
        elif platform=='bilibili':result=await content('bili_action',operation='dm',target=target,text=message,request_id=request_id)
        else:result=await content('weixin_send',target=target,text=message,request_id=request_id)
        return '✅ 已提交给平台。' if result.get('ok') else '⚠️ 未能确认发送成功。请核对接收者、账号权限或连接；不确定时不要重复发送。'
    if name in {'help','帮助','commands'}:
        return HELP
    if name in {'new','reset','clear'}:
        if argument:
            return '用法：/'+name+'（不带参数）。这会开新对话，保留登录和个性设置。'
        session_manager.clear_session(**args)
        key = session_manager.get_session_key(**args)
        await ensure_session(key)
        return '✅ 已开始新对话。账号登录与本聊天个性设置保留。'
    if name == 'characters':
        module, choices = personalities()
        rows=[key+' — '+module.describe_personality(value,45) for key,value in choices.items()
              if not argument or argument.lower() in (key+' '+module.describe_personality(value,100)).lower()]
        return ('人设库：\n'+'\n'.join(rows)+'\n切换：/personality 名称；本 QQ 聊天会开新对话。\n社区目录和角色卡导入可用自然语言请求。') if rows else '没有匹配的人设。可用自然语言搜索社区人设目录或导入角色卡。'
    if name == 'personality':
        module, choices = personalities()
        if not argument or argument.lower() in {'list','show'}:
            current = selected(scope_for(args)) or 'none（无叠加）'
            return ('当前本 QQ 聊天个性：'+current+'\n可选：none, '+', '.join(choices)+
                    '\n切换示例：/personality concise\n关闭：/personality none\n切换会开始新对话，只影响本聊天。')
        canonical = module.normalize_personality_name(argument)
        if canonical and canonical not in choices:
            return '未知个性：'+argument[:80]+'。用 /personality 查看可用名称。'
        save_personality(scope_for(args), canonical)
        session_manager.clear_session(**args)
        await ensure_session(session_manager.get_session_key(**args))
        return '✅ 本 QQ 聊天个性已设为 '+(canonical or 'none')+'，已开始新对话。'
    if name == 'ping':
        if argument:return '用法：/ping'
        return '🏓 Hermes 连接正常。' if await hermes_client.health_check() else '⚠️ Hermes 连接失败。'
    if name == 'hermes-status':
        import time
        from nonebot_plugin_hermes.handlers.commands import build_status_lines
        return '\n'.join(build_status_lines(int(time.time()*1000)))
    if name in {'status','model','usage','context'}:
        if argument:return '/'+name+' 当前只支持查询，不带参数。'
        key=session_manager.get_session_key(**args)
        row=await ensure_session(key)
        if name in {'usage','context'}:
            return ('当前会话统计（累计计数，不等于当前上下文占用或账户余额）\n'
                    f"消息：{row.get('message_count',0)}\n工具调用：{row.get('tool_call_count',0)}\n"
                    f"输入 tokens：{row.get('input_tokens',0)}\n输出 tokens：{row.get('output_tokens',0)}")
        config=yaml.safe_load((Path.home()/'.hermes/config.yaml').read_text()) or {}
        model=row.get('model') or config.get('model',{}).get('default','unknown')
        if name=='model':return '当前模型：'+str(model)+'\n此命令目前只查询。'
        return ('Hermes：'+('连接正常' if await hermes_client.health_check() else '连接异常')+
                '\n模型：'+str(model)+'\n本聊天个性：'+(selected(scope_for(args)) or 'none')+
                '\n会话：'+('已结束，建议 /reset' if row.get('end_reason') else '可用')+
                '\n消息数：'+str(row.get('message_count',0))+'\nQQ 命令由本机直接处理。')
    if name == 'tools':
        if argument:return '/tools 当前只查询启用的工具集，不带参数。'
        config=yaml.safe_load((Path.home()/'.hermes/config.yaml').read_text()) or {}
        enabled=config.get('platform_toolsets',{}).get('api_server',[])
        return 'QQ/API 通道工具集：'+', '.join(enabled)+'\n具体任务直接用自然语言。'
    if name == 'balance':
        if argument:return '用法：/balance（DeepSeek API 余额）'
        result=await content('balance')
        return '\n'.join([str(x['balance'])+' '+str(x['currency']) for x in result.get('balances',[])]+['状态：'+str(result.get('status','query_failed'))])
    if name == 'accounts':
        if argument:return '用法：/accounts'
        platforms=['qq','weixin','bilibili','heybox']
        rows=await asyncio.gather(*(content('status',platform=p) for p in platforms))
        lines=[]
        for platform,row in zip(platforms,rows):
            if platform=='qq':state='在线' if row.get('connected') else '离线或检查失败'
            elif platform=='weixin':state=row.get('state','未知')
            else:state='会话已保存（有效性以实际请求为准）' if row.get('credential_saved') else '未保存会话或检查失败'
            lines.append(platform+'：'+state)
        return '\n'.join(lines)
    return 'QQ 入口尚未接入命令 '+command[:80]+'，没有把它当作聊天执行。用 /help 查看实际可用命令。'
