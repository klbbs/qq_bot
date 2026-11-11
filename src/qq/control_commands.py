"""Actual QQ slash commands; unsupported commands never reach the language model."""
import hashlib
import httpx
import nonebot_plugin_alconna as alconna
from nonebot import on_message
from nonebot.adapters import Bot, Event
from nonebot.matcher import Matcher
from nonebot.rule import Rule
from nonebot_plugin_hermes.config import plugin_config
from nonebot_plugin_hermes.utils import check_isolation, get_adapter_name

import qq_control


async def is_slash(event: Event) -> bool:
    return event.get_plaintext().strip().startswith('/')


commands = on_message(rule=Rule(is_slash), priority=80, block=True)


@commands.handle()
async def handle(bot: Bot, event: Event, matcher: Matcher):
    target = alconna.get_target()
    if not check_isolation(event, target):
        matcher.skip()
    adapter = get_adapter_name(target)
    user_id = event.get_user_id()
    # Configuration, status and account commands are local owner controls.
    if f'{adapter}:{user_id}' not in plugin_config.hermes_admin_users:
        await matcher.finish('此 QQ 入口的控制命令仅供已配置的管理员使用。')
    args = {'adapter_name':adapter, 'is_private':target.private, 'user_id':user_id,
            'group_id':None if target.private else target.id}
    try:
        request_id='qq-'+hashlib.sha256((str(bot.self_id)+':'+str(getattr(event,'message_id',''))+':'+event.get_session_id()).encode()).hexdigest()[:40]
        reply = await qq_control.handle(event.get_plaintext(), args, request_id=request_id)
    except (qq_control.SessionUnavailable, httpx.HTTPError):
        reply = '⚠️ Hermes 会话连接或创建失败。请稍后用 /ping 检查连接，再用 /reset 开新会话。'
    except Exception:
        reply = '⚠️ 本机命令处理失败，没有把该命令交给模型。请检查本机桥接服务。'
    await matcher.finish(reply)
