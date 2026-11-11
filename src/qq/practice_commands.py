"""QQ practice entry point: allowed chats only, no admin tools granted to players."""
import asyncio
import hashlib
import json
import sqlite3
from pathlib import Path

import nonebot
from nonebot import on_message
from nonebot.rule import Rule
from nonebot.adapters.onebot.v11 import Bot, Message, MessageEvent, MessageSegment
from nonebot.matcher import Matcher
import nonebot_plugin_alconna as alconna
from nonebot_plugin_hermes.utils import check_isolation
from practice_core import Game, Store, PracticeError
from practice_backend import ACM, grade, summarize_solution

ROOT=Path.home()/'.local/share/hermes-acm/practice'
api=ACM(ROOT);game=Game(Store(ROOT),api,grade,summarizer=summarize_solution)
NAMES={'/roll','/submit','/giveup','/solution','/practice','/problem','/sample','/progress','/cancel','/tasks','/candidates'}
OWNER_ID='__QQ_OWNER__'

async def is_practice(event:MessageEvent):
    text=event.get_plaintext().strip()
    return bool(text and text.split(None,1)[0].lower() in NAMES)

commands=on_message(rule=Rule(is_practice),priority=79,block=True)

# Persisted deadlines survive restarts; background expiry never sends messages
# or invokes an AI summary. Commands also enforce the deadline synchronously.
_task=None
async def cleanup_loop():
    while True:
        try:
            game.store.expire()
            api.cleanup()
        except (OSError, sqlite3.Error):
            nonebot.logger.warning('刷题到期清理暂未完成，将自动重试。')
        await asyncio.sleep(10)
@nonebot.get_driver().on_startup
async def startup():
    global _task
    game.store.expire()
    api.cleanup()
    _task=asyncio.create_task(cleanup_loop())
@nonebot.get_driver().on_shutdown
async def shutdown():
    if _task:
        _task.cancel()
        try:await _task
        except asyncio.CancelledError:pass

@commands.handle()
async def handle(bot:Bot,event:MessageEvent,matcher:Matcher):
    target=alconna.get_target()
    if not check_isolation(event,target):matcher.skip()
    private=target.private
    chat=('private:'+event.get_user_id()) if private else 'group:'+str(target.id)
    scope=game.scope(bot.self_id,chat,event.get_user_id())
    shared=game.scope(bot.self_id,chat,'*')
    eventid=hashlib.sha256(json.dumps([bot.self_id,chat,event.get_user_id(),event.message_id]).encode()).hexdigest()
    async def notice(text):
        prefix=Message() if private else Message(MessageSegment.at(event.get_user_id()))+MessageSegment.text(' ')
        await matcher.send(prefix+MessageSegment.text(text))
    try:
        text=event.get_plaintext().strip()
        reply=await game.handle(scope,eventid,text,notice=notice,shared=shared,user=event.get_user_id(),admin=event.get_user_id()==OWNER_ID)
    except PracticeError as exc:
        from practice_core import Reply
        reply=Reply(str(exc))
    except Exception:
        from practice_core import Reply
        reply=Reply('刷题服务暂时异常，作答记录仍保留。请稍后 /practice 查看；没有把命令交给普通聊天。')
    if reply is None:return
    prefix=Message() if private else Message(MessageSegment.at(event.get_user_id()))+MessageSegment.text(' ')
    message=prefix+MessageSegment.text(reply.text)
    # NapCat may run in a container with a different filesystem. Inline only
    # the selected private PNG; never expose its authenticated download URL.
    for path in reply.images:message+=MessageSegment.image(Path(path).read_bytes())
    await matcher.send(message)
    if reply.delivery:
        try:
            api.receipts.mark_sent(reply.delivery['delivery_id'],reply.delivery['scope'])
        except Exception:
            # The server ACK meant durable local receipt. Do not duplicate a
            # successful QQ send just because its local bookkeeping failed.
            nonebot.logger.warning('题面已发出，但本地渠道发送标记未更新；保留收件记录。')
