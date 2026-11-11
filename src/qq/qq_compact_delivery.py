"""Fold long QQ group replies into one forward card without changing their content.

This changes delivery only: no summarization, token calls, truncation, or retry on
uncertain delivery. Call install() after NoneBot has loaded its plugins.
"""

from __future__ import annotations

import functools
import importlib
import sys

THRESHOLD = 400
_OUTBOUND = "nonebot_plugin_hermes.core.outbound"
_CONSUMERS = (
    "nonebot_plugin_hermes.handlers.message",
    "nonebot_plugin_hermes.mcp.tools.push_message",
)
_MARKER = "_qq_compact_delivery_installed"


def _nodes(outbound, text: str, bot_id: str) -> list[dict]:
    # Reuse the sender's nickname cache without an extra get_login_info RPC.
    nickname = getattr(outbound, "_bot_nickname_cache", {}).get(bot_id) or "Bot"
    nodes = outbound._split_into_forward_nodes(
        text, bot_self_id=bot_id, bot_nickname=nickname
    )
    reconstructed = "".join(
        segment["data"]["text"]
        for node in nodes
        for segment in node["data"]["content"]
        if segment.get("type") == "text"
    )
    if reconstructed == text:
        return nodes
    # Some upstream versions normalize paragraph/newline separators when a
    # paragraph crosses nodes. Exact slicing preserves code and whitespace.
    return [
        {
            "type": "node",
            "data": {
                "name": nickname,
                "uin": bot_id,
                "content": [{"type": "text", "data": {"text": text[i:i + 3500]}}],
            },
        }
        for i in range(0, len(text), 3500)
    ]


def install() -> bool:
    """Patch the shared sender and already-imported call sites; safe to repeat."""
    outbound = importlib.import_module(_OUTBOUND)
    original = outbound.send_text_with_media
    installed = getattr(original, _MARKER, False) is True
    unwrapped = getattr(original, "_qq_compact_delivery_original", original) if installed else original
    if installed:
        wrapped = original
    else:
        @functools.wraps(original)
        async def wrapped(*args, **kwargs):
            from qq_delivery_guard import collapse_text
            if isinstance(kwargs.get("text"), str):
                kwargs["text"] = collapse_text(kwargs["text"])
            text = kwargs.get("text", "")
            target = kwargs.get("target")
            if (
                args
                or kwargs.get("adapter_name") != "onebotv11"
                or target is None
                or getattr(target, "private", True)
                or not isinstance(text, str)
                or len(text) <= THRESHOLD
            ):
                return await original(*args, **kwargs)
            try:
                bot = kwargs["bot"]
                nodes = _nodes(outbound, text, str(bot.self_id))
                # A mention stays inside the card. Never send a separate ping.
                if kwargs.get("at_user_id"):
                    nodes[0]["data"]["content"].insert(
                        0, {"type": "at", "data": {"qq": str(kwargs["at_user_id"])}}
                    )
                content = nodes[-1]["data"]["content"]
                for url in kwargs.get("media_urls") or ():
                    if not isinstance(url, str):
                        continue
                    if url.startswith(("http://", "https://")):
                        content.append({"type": "image", "data": {"url": url}})
                    elif url.startswith("data:"):
                        parsed = outbound._parse_image_data_url(url)
                        if parsed is not None:
                            content.append({"type": "image", "data": {"file": "base64://" + parsed[2]}})
                await bot.call_api(
                    "send_group_forward_msg", group_id=int(target.id), messages=nodes
                )
                return True
            except Exception:
                # Delivery may already have succeeded before its acknowledgement
                # failed. Retrying as plain text can duplicate the entire reply.
                outbound.logger.warning("[QQ_COMPACT] Forward delivery was not confirmed; no automatic resend.")
                return False

        setattr(wrapped, _MARKER, True)
        wrapped._qq_compact_delivery_original = original
        outbound.send_text_with_media = wrapped
    for name in _CONSUMERS:
        module = sys.modules.get(name)
        if module is not None and hasattr(module, "send_text_with_media"):
            current = module.send_text_with_media
            if current is original or current is unwrapped or getattr(current, _MARKER, False) is True:
                module.send_text_with_media = wrapped
    return True
