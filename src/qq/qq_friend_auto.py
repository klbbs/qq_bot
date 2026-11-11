"""Optional friend acceptance; default requests remain available for owner review."""
from nonebot import on_request
from nonebot.log import logger
from nonebot.rule import Rule
from nonebot.adapters.onebot.v11 import Bot, FriendRequestEvent
from qq_delivery_guard import get_policy

if get_policy().friend_auto_accept:
    async def is_friend(event: FriendRequestEvent):
        return True

    _friend_auto = on_request(rule=Rule(is_friend), priority=1, block=True)

    @_friend_auto.handle()
    async def _auto_accept_friend(bot: Bot, event: FriendRequestEvent):
        try:
            await bot.set_friend_add_request(flag=event.flag, approve=True, remark="")
            logger.info("Friend request accepted by explicit local policy")
        except Exception:
            logger.warning("Friend request was not confirmed; no automatic retry")
