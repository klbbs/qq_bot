"""Recover a QQ transcript only after a structured, pre-admission refusal.

The local module proxies observe their own HTTP clients; global httpx, Hermes
core, permissions, overlays, other scopes, and the independent grader are intact.
"""
from __future__ import annotations

import asyncio
import copy
from contextvars import ContextVar
from dataclasses import dataclass
import json
import logging
import re
import sys
from typing import Any, Callable
from weakref import WeakValueDictionary


_CHAT_PATH = re.compile(r"(?:/p/[^/]+)?/v1/chat/completions\Z")
_CREATE_PATH = re.compile(r"(?:/p/[^/]+)?/api/sessions\Z")
_CURRENT: ContextVar["Capture | None"] = ContextVar("qq_session_recovery_capture", default=None)
_LOCKS: WeakValueDictionary[tuple, asyncio.Lock] = WeakValueDictionary()
_LOGGER = logging.getLogger(__name__)


@dataclass
class Capture:
    session_key: str
    refusal: str | None = None
    chat_status: int | None = None
    guard: Callable[[], bool] | None = None
    stale: bool = False


def _request_matches(request: Any, capture: Capture) -> str | None:
    if request.method != "POST":
        return None
    if _CHAT_PATH.fullmatch(request.url.path):
        return "chat" if request.headers.get("X-Hermes-Session-Id") == capture.session_key else None
    if _CREATE_PATH.fullmatch(request.url.path):
        try:
            body = json.loads(request.content)
        except (ValueError, UnicodeError):
            return None
        return "create" if isinstance(body, dict) and body.get("id") == capture.session_key else None
    return None


async def _observe_response(response: Any) -> None:
    capture = _CURRENT.get()
    if capture is None:
        return
    kind = _request_matches(response.request, capture)
    if kind == "chat":
        capture.chat_status = response.status_code
    if kind is None or response.status_code != 409:
        return
    # HTTPX response hooks run before body consumption; reading here makes the
    # original client's normal response.text/json calls work exactly as before.
    await response.aread()
    try:
        body = response.json()
    except (ValueError, UnicodeError):
        return
    error = body.get("error") if isinstance(body, dict) else None
    code = error.get("code") if isinstance(error, dict) else None
    if isinstance(code, str) and (kind, code) in {("chat", "not_found"), ("create", "session_deleted")}:
        capture.refusal = code


class HTTPXProxy:
    """Delegate everything, adding coroutine-local observers to new clients."""
    def __init__(self, original: Any):
        self._original = original

    def __getattr__(self, name: str) -> Any:
        return getattr(self._original, name)

    def AsyncClient(self, *args: Any, **kwargs: Any):
        hooks = {name: list(callbacks) for name, callbacks in (kwargs.get("event_hooks") or {}).items()}
        hooks.setdefault("response", []).append(_observe_response)

        async def guard_request(request):
            capture = _CURRENT.get()
            if (capture is not None and capture.guard is not None
                    and _request_matches(request, capture) == "chat" and not capture.guard()):
                capture.stale = True
                # RequestError is caught by the existing control/client paths;
                # no network request is sent after a concurrent /reset.
                raise self._original.RequestError("QQ session changed before recovery retry", request=request)

        hooks.setdefault("request", []).append(guard_request)
        kwargs["event_hooks"] = hooks
        return self._original.AsyncClient(*args, **kwargs)


def _scope(kwargs: dict[str, Any]) -> tuple[dict[str, Any], tuple] | None:
    if kwargs.get("adapter_name") != "onebotv11" or type(kwargs.get("is_private")) is not bool:
        return None

    def platform_id(value):
        return isinstance(value, (str, int)) and not isinstance(value, bool) and str(value).isascii() and str(value).isdigit()

    if not platform_id(kwargs.get("user_id")):
        return None
    private = kwargs["is_private"]
    if not private and not platform_id(kwargs.get("group_id")):
        return None
    args = {key: kwargs.get(key) for key in ("adapter_name", "is_private", "user_id", "group_id")}
    # A shared group is one lock even if two speakers observed its old key.
    # In per-user mode this only serializes rare recovery; mapping checks still
    # use the standard SessionManager's exact configured scope.
    identity = str(args["user_id"] if private else args["group_id"])
    return args, ("onebotv11", private, identity)


def _copy_kwargs(kwargs: dict[str, Any]) -> dict[str, Any]:
    cloned = dict(kwargs)
    for key in ("user_content_override", "image_urls"):
        if key in cloned:
            cloned[key] = copy.deepcopy(cloned[key])
    return cloned


def _error(control: Any, text: str):
    return control.ChatResult(raw_text=text, parse_failed=True, is_transport_error=True)


async def _call(original: Any, kwargs: dict[str, Any], *, guard=None):
    capture = Capture(kwargs["session_key"], guard=guard)
    token = _CURRENT.set(capture)
    try:
        return await original(**_copy_kwargs(kwargs)), capture
    finally:
        _CURRENT.reset(token)


async def _provision(control: Any, key: str, target: Any):
    capture = Capture(key)
    token = _CURRENT.set(capture)
    try:
        try:
            await control.ensure_session(key, target)
            return True, capture
        except (control.SessionUnavailable, control.httpx.HTTPError):
            return False, capture
    finally:
        _CURRENT.reset(token)


def install(control: Any = None, *, client_module: Any = None) -> None:
    """Install after qq_control's client adapter; existing overlay wrappers stay live."""
    if control is None:
        import qq_control as control
    client = control.hermes_client
    if getattr(client, "_qq_session_recovery_installed", False):
        return
    if not getattr(client, "_qq_control_installed", False):
        raise RuntimeError("install qq_control client adapter before QQ session recovery")
    if client_module is None:
        client_module = sys.modules[type(client).__module__]
    # Do not mutate the shared imported module object or httpx.AsyncClient.
    client_module.httpx = HTTPXProxy(client_module.httpx)
    control.httpx = HTTPXProxy(control.httpx)
    original = client.chat
    manager = control.session_manager

    async def chat(**kwargs):
        scoped = _scope(kwargs)
        if scoped is None or not isinstance(kwargs.get("session_key"), str):
            return await original(**kwargs)
        args, lock_key = scoped
        rejected_key = kwargs["session_key"]
        result, first = await _call(original, kwargs)
        if first.refusal is None:
            return result
        lock = _LOCKS.get(lock_key)
        if lock is None:
            lock = _LOCKS[lock_key] = asyncio.Lock()
        async with lock:
            if manager.get_session_key(**args) != rejected_key:
                return _error(control, "当前会话已更新，这条消息未重发；请重新发送。")
            # Only a confirmed HTTP 409 refusal reaches this point. Standard
            # clear_session persists the generation; no tombstone/data is edited.
            manager.clear_session(**args)
            candidate = manager.get_session_key(**args)
            for attempt in range(4):
                if manager.get_session_key(**args) != candidate:
                    return _error(control, "当前会话已更新，这条消息未重发；请重新发送。")
                ready, observed = await _provision(control, candidate, kwargs.get("target"))
                if manager.get_session_key(**args) != candidate:
                    return _error(control, "当前会话已更新，这条消息未重发；请重新发送。")
                if ready:
                    break
                if observed.refusal != "session_deleted" or attempt == 3:
                    return _error(control, "会话恢复暂未完成，原请求未重发；请稍后再试。")
                manager.clear_session(**args)
                candidate = manager.get_session_key(**args)
            replay_kwargs = dict(kwargs, session_key=candidate)
            current = lambda: manager.get_session_key(**args) == candidate
            replay, observed = await _call(original, replay_kwargs, guard=current)
            if observed.stale:
                return _error(control, "当前会话已更新，这条消息未重发；请重新发送。")
            # At most ONE chat retry. A second refusal is returned unchanged;
            # unknown/timeout failures never cause another send or rotation.
            if observed.chat_status == 200:
                effective = getattr(replay, "effective_session_key", None)
                if effective:
                    manager.adopt_session_key(candidate, effective)
                else:
                    replay.effective_session_key = candidate
                # Fixed labels and one bounded integer; never log chat/user IDs,
                # message bodies, headers, URLs, or credentials.
                getattr(control, "logger", _LOGGER).info(
                    "QQ session recovery: replay_http_200=1 candidate_index=" + str(attempt + 1))
            return replay

    client.chat = chat
    client._qq_session_recovery_installed = True
