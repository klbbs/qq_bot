"""Privileged QQ calls require an exact trusted private-session route.

Session IDs are supplied by the gateway hook, never model arguments. Display
masks, substring matches and claimed sender IDs do not authorize computer use.
Shared QQ groups lack trusted per-turn sender metadata and remain read-only.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

OWNER_QQ = "__QQ_OWNER__"
BRIDGE_PREFIX = "hermes-onebotv11+"

#: Tools a non-owner may still use: read-only research + chatting.
SAFE_TOOLS = frozenset({
    "web_search",
    "web_extract",
    "content_search",
    "content_read",
    "skill_view",
    "skills_list",
    "tool_search",
    "tool_describe",
})

LOG_PATH = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes")) / "logs" / "owner-guard.log"

#: 群聊里额外放开的只读工具：能看群资料、群文件、题库和公开内容，但仍然不能发消息、不能碰电脑。
GROUP_READ_TOOLS = frozenset({
    "qq_group_file_read",
    "practice_problem",
    "qq_query",
    "qq_contacts",
    "platform_capabilities",
    "acm_lab",
    "bilibili_query",
    "heybox_query",
    "bilibili_article",
    "bilibili_danmaku",
    "content_transcribe",
})

#: 这些操作会把主人私聊 / 私信内容读出来，群聊里一律不给。
PRIVATE_READ_OPS = frozenset({
    "dm_sessions", "dm_messages", "dm", "private_history",
})

_BLOCK_MESSAGE = (
    "仅主人账号可以使用这个能力。我这边只做聊天和资料查询，"
    "需要执行命令的话请让你自己的账号来找我。"
)
_PRIVATE_BLOCK = "涉及别人私聊的内容我不能读。群里的资料、群文件这类可以帮你查。"


_PRIVATE_PREFIX = "hermes-onebotv11+private+"
_GROUP_PREFIX = "hermes-onebotv11+group+"

#: B 站私信看护会话（由第三方私信唤醒）：只给 B 站读写和通知 owner 的能力。
BILI_WATCH_KEY = "hermes-bili-dm-watch"
BILI_TOOLS = frozenset({
    "bilibili_query",
    "bilibili_action",
    "bilibili_article",
    "bilibili_comment",
    "bilibili_danmaku",
    "qq_send",
    "qq_query",
    "web_search",
    "web_extract",
    "content_search",
    "content_read",
    "platform_capabilities",
    "skill_view",
    "skills_list",
    "tool_search",
    "tool_describe",
})


def _log(line: str) -> None:
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:  # logging must never break a tool call
        pass


# Only these real gateway wrappers are accepted; no substring identity parsing.
_WRAPPER = r"(?:agent:[A-Za-z0-9_.~-]+:api_server:dm:)?"
_OWNER_ROUTE = re.compile(_WRAPPER + re.escape(_PRIVATE_PREFIX + OWNER_QQ) + r"(?:-g[0-9]+)?\Z")
_GROUP_ROUTE = re.compile(_WRAPPER + re.escape(_GROUP_PREFIX) + r"[0-9]+(?:\+[0-9]+)?(?:-g[0-9]+)?\Z")


def _verdict(session_id: str) -> tuple[str, str]:
    if not isinstance(session_id, str) or not session_id:
        return "pass", "no session id"
    if _OWNER_ROUTE.fullmatch(session_id):
        return "allow", "owner private chat"
    if session_id.startswith(BILI_WATCH_KEY):
        return "block", "bilibili dm watcher (third-party driven)"
    if _GROUP_ROUTE.fullmatch(session_id):
        return "block", "group chat"
    if BRIDGE_PREFIX in session_id:
        return "block", "other or malformed QQ route"
    return "pass", "not a bridge session"


def _is_desktop_tool(tool_name: str) -> bool:
    return tool_name in {"computer_use", "desktop", "desktop_ui", "desktop_control", "cua"} or tool_name.startswith(("computer_use.", "desktop_", "cua_"))


def _authorize_owner_desktop(session_id: str) -> bool:
    """Apply the operator's explicit desktop grant to this owner session only.

    Native computer_use also checks approval_context's routing key. A distinct
    key is accepted only when it independently identifies the same owner-private
    route. No model/tool argument supplies this key, and no global switch changes.
    """
    try:
        from tools.approval import enable_session_yolo
        from tools.approval_context import get_current_session_key
        current = get_current_session_key(default="")
        if current and (not isinstance(current, str) or not _OWNER_ROUTE.fullmatch(current)):
            return False
        if current and current[current.index(_PRIVATE_PREFIX):] != session_id[session_id.index(_PRIVATE_PREFIX):]:
            return False
        enable_session_yolo(session_id)
        if current and current != session_id:
            enable_session_yolo(current)
        return True
    except Exception:
        # Never let a missing/mismatched authorization service fall through to
        # an unreachable gateway approval prompt or an unguarded desktop call.
        return False


def _group_read_ok(tool_name: str, args: dict | None) -> tuple[bool, str]:
    """群聊只放只读工具，且不许借它读私聊。"""
    if tool_name in SAFE_TOOLS or tool_name in GROUP_READ_TOOLS:
        pass
    else:
        return False, "not a read-only tool"
    data = args if isinstance(args, dict) else {}
    target_type = str(data.get("target_type") or "").lower()
    operation = str(data.get("operation") or "").lower()
    if target_type == "private" and tool_name in {"qq_query", "qq_contacts"}:
        return False, "private target"
    if tool_name == 'bilibili_query' and (operation in {'toview','history','unread','dynamic_feed'} or (operation == 'favorites' and not data.get('target'))):
        return False, 'private account collection'
    if tool_name == 'heybox_query' and operation == 'mentions':
        return False, 'private account mentions'
    if tool_name == 'qq_query' and operation in {'account_status','recent_contacts','pending_requests','friend_categories','one_way_friends','profile_likes','group_system_messages','custom_stickers'}:
        return False, 'private account contacts or requests'
    if operation in PRIVATE_READ_OPS:
        return False, f"private operation ({operation})"
    return True, "group read-only"


def on_pre_tool_call(tool_name: str = "", args=None, **kwargs) -> dict[str, str] | None:
    session_id = str(kwargs.get("session_id") or "")
    decision, reason = _verdict(session_id)

    # A dynamically discovered desktop tool still passes this hook. Unknown,
    # local and other-platform sessions cannot borrow the owner's desktop access.
    if _is_desktop_tool(tool_name) and decision != "allow":
        return {"action": "block", "message": "电脑桌面操作仅限管理员 QQ 私聊；请在管理员私聊中操作。"}
    if decision == "pass":
        return None
    if decision == "allow":
        if _is_desktop_tool(tool_name) and not _authorize_owner_desktop(session_id):
            return {"action": "block", "message": "管理员桌面授权上下文尚未就绪，请稍后重试。"}
        return None
    if decision == "block" and reason == "group chat":
        ok, why = _group_read_ok(tool_name, args)
        if ok:
            _log(f"allow-group-read tool={tool_name} scope={reason}")
            return None
        if why.startswith("private"):
            _log(f"BLOCK tool={tool_name} scope={reason} reason=group/{why}")
            return {"action": "block", "message": _PRIVATE_BLOCK}
    if tool_name in SAFE_TOOLS:
        _log(f"allow-tool tool={tool_name} scope={reason} reason={reason}")
        return None
    # Another account's private chat: same read-only set as a group (practice_problem
    # is itself chat-scoped), but never private-target reads.
    if decision == "block" and reason == "other or malformed QQ route":
        ok, why = _group_read_ok(tool_name, args)
        if ok:
            _log(f"allow-nonowner-read tool={tool_name} scope={reason}")
            return None
        if why.startswith("private"):
            _log(f"BLOCK tool={tool_name} scope={reason} reason=nonowner/{why}")
            return {"action": "block", "message": _PRIVATE_BLOCK}
    if reason.startswith("bilibili dm watcher") and tool_name in BILI_TOOLS:
        _log(f"allow-bili-tool tool={tool_name} scope={reason}")
        return None

    _log(f"BLOCK tool={tool_name} scope={reason} reason={reason}")
    return {"action": "block", "message": _BLOCK_MESSAGE}


def register(ctx) -> None:
    _log("register: hooks wired")
    ctx.register_hook("pre_tool_call", on_pre_tool_call)
