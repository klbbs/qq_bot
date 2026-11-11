"""把 QQ 分享卡片(json / ark / light_app 段)转成纯文本,让 Hermes 看得到链接。

背景:桥接层只认文本、图片、表情等段,分享卡片(如 B 站视频分享)进来是
`json` 段,extract_plain_text() 得到空串,随后被 "if not msg_text and not
image_urls: return" 直接丢掉。这里在事件预处理阶段把卡片拆成
「[分享] 标题 URL」,后面所有流程就不用改。
"""
from __future__ import annotations

import json as _json

from nonebot.adapters.onebot.v11 import Message, MessageSegment
from nonebot.message import event_preprocessor

_SHARE_TYPES = ("json", "ark", "light_app")
_URL_PRIORITY = ("jumpurl", "jump_url", "qqdocurl", "detailurl", "url", "preview")
_TITLE_KEYS = ("title", "desc", "prompt", "summary", "tag")


def _walk(obj):
    if isinstance(obj, dict):
        for key, value in obj.items():
            yield key, value
            yield from _walk(value)
    elif isinstance(obj, list):
        for value in obj:
            yield from _walk(value)


def share_text(raw) -> str | None:
    """从卡片原始 data 里抽「[分享] 标题 URL」,抽不到返回 None。"""
    payload = raw
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return None
        try:
            payload = _json.loads(text)
        except ValueError:
            return None
    if not isinstance(payload, (dict, list)):
        return None

    found = {}
    titles = []
    for key, value in _walk(payload):
        if not isinstance(value, str):
            continue
        name = key.lower()
        cleaned = value.strip()
        if name in _URL_PRIORITY and cleaned.startswith(("http://", "https://")):
            # 同一个卡片里 preview 是缩略图 CDN,排在 jumpUrl 后面,别让图抢了正片链接。
            found.setdefault(name, cleaned)
        elif name in _TITLE_KEYS:
            # 标题、描述常常一个说 App 名一个说内容,两个都留着才能看出分享的是什么。
            if cleaned and cleaned not in titles and len(titles) < 2:
                titles.append(cleaned)

    url = next((found[k] for k in _URL_PRIORITY if k in found), None)
    if not url and not titles:
        return None
    parts = ["[分享]"]
    if titles:
        parts.append(" · ".join(t[:120] for t in titles))
    if url:
        parts.append(url)
    return " ".join(parts)


async def _share_card_to_text(event) -> None:
    message = getattr(event, "message", None)
    if not isinstance(message, Message):
        return
    changed = False
    for index, segment in enumerate(list(message)):
        if segment.type not in _SHARE_TYPES:
            continue
        text = share_text(segment.data.get("data") or segment.data)
        if not text:
            continue
        message[index] = MessageSegment.text(text)
        changed = True
    if changed:
        event.message = message


def install() -> None:
    """在 nonebot.init() 之后调用,注册事件预处理器。"""
    event_preprocessor(_share_card_to_text)
