"""Owner-private turns run through /v1/runs so a new message can stop the live turn.

Why: the plugin coalesces in-flight messages (core/inflight.py) — a second message just
waits for the first turn to end — and chat/completions only interrupts the agent when an
SSE client disconnects. /v1/runs hands back a run_id, so POST /v1/runs/{id}/stop is a
first-class interrupt that also reports a terminal status.

Scope: private chats of QQ_OWNER_IDS only. Group turns keep the stock transport.
"""
from __future__ import annotations

import asyncio
import os
import time

import httpx
from nonebot import logger

ENABLED = os.environ.get("QQ_OWNER_INTERRUPT", "1").strip().lower() not in {"0", "false", "no", "off"}
OWNER_IDS = tuple(x for x in os.environ.get("QQ_OWNER_IDS", "__QQ_OWNER__").replace(" ", "").split(",") if x)
TERMINAL = {"completed", "failed", "cancelled", "interrupted"}

_ACTIVE: dict[str, str] = {}
_installed = False


def _key(adapter: str, is_private: bool, user_id: str, group_id: str | None) -> str:
    return f"{adapter}|private|{user_id}" if is_private else f"{adapter}|group|{group_id or ''}"


def _target():
    from nonebot_plugin_hermes.core.hermes_client import default_target
    return default_target()


def _headers(tgt, memory_key: str | None = None) -> dict[str, str]:
    h = {"Content-Type": "application/json"}
    if tgt.api_key:
        h["Authorization"] = f"Bearer {tgt.api_key}"
        if memory_key:
            h["X-Hermes-Session-Key"] = memory_key
    return h


async def _await_run(tgt, run_id: str, memory_key: str | None, budget: float) -> dict:
    """Poll GET /v1/runs/{id} until a terminal status, or stop the run at the deadline."""
    deadline = time.monotonic() + max(30.0, budget)
    url = f"{tgt.base_url}/v1/runs/{run_id}"
    state: dict = {}
    async with httpx.AsyncClient(timeout=20) as client:
        while time.monotonic() < deadline:
            try:
                resp = await client.get(url, headers=_headers(tgt, memory_key))
                if resp.status_code == 200:
                    state = resp.json() or {}
                    if str(state.get("status") or "") in TERMINAL:
                        return state
                else:
                    logger.warning(f"[OWNER-INTERRUPT] run poll HTTP {resp.status_code} for {run_id}")
            except Exception as exc:  # noqa: BLE001 - polling must survive a blip
                logger.warning(f"[OWNER-INTERRUPT] run poll failed for {run_id}: {exc}")
            await asyncio.sleep(0.7)
        logger.warning(f"[OWNER-INTERRUPT] run {run_id} hit the {budget:.0f}s budget; stopping it")
        try:
            await client.post(f"{tgt.base_url}/v1/runs/{run_id}/stop", json={},
                              headers=_headers(tgt))
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[OWNER-INTERRUPT] deadline stop failed for {run_id}: {exc}")
    return state or {"status": "failed", "error": "run timed out"}


async def _chat_runs(orig, self, *, text, image_urls=None, session_key, memory_key=None,
                     user_id, group_id, adapter_name, is_private, mode="passive",
                     expect_structured=False, structured_tool_name=None, system_prompt=None,
                     user_content_override=None, target=None):
    from nonebot_plugin_hermes.core.hermes_client import ChatResult, _DECISION_HINT

    if user_content_override is not None:
        content = user_content_override
    else:
        imgs = image_urls or []
        if not imgs:
            content = text
        else:
            content = [{"type": "text", "text": text}]
            content += [{"type": "image_url", "image_url": {"url": u}} for u in imgs]

    if system_prompt is None:
        ctx = [f"Platform: {adapter_name or 'unknown'}",
               "Chat Type: " + ("Private" if is_private else "Group")]
        if user_id:
            ctx.append(f"User ID: {user_id}")
        if not is_private and group_id:
            ctx.append(f"Group ID: {group_id}")
        system_prompt = "Message Context:\n" + "\n".join(ctx)
    if expect_structured and structured_tool_name == "submit_decision":
        system_prompt = system_prompt + _DECISION_HINT

    tgt = target or _target()
    body = {
        "model": "hermes-agent",
        "input": [{"role": "user", "content": content}],
        "session_id": session_key,
        "instructions": system_prompt,
    }
    key = _key(adapter_name, is_private, user_id, group_id)
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(f"{tgt.base_url}/v1/runs", json=body,
                                     headers=_headers(tgt, memory_key))
        if resp.status_code not in (200, 202):
            logger.error(f"[OWNER-INTERRUPT] /v1/runs HTTP {resp.status_code}: {resp.text[:200]}")
            return ChatResult(raw_text="⚠️ AI 服务暂时不可用", parse_failed=True, is_transport_error=True)
        run_id = str((resp.json() or {}).get("run_id") or "")
    except Exception as exc:  # noqa: BLE001
        logger.error(f"[OWNER-INTERRUPT] /v1/runs request failed: {exc}")
        return ChatResult(raw_text="⚠️ AI 服务暂时不可用", parse_failed=True, is_transport_error=True)

    if not run_id:
        return ChatResult(raw_text="⚠️ AI 服务暂时不可用", parse_failed=True, is_transport_error=True)

    _ACTIVE[key] = run_id
    logger.info(f"[OWNER-INTERRUPT] run {run_id} started key={key}")
    try:
        state = await _await_run(tgt, run_id, memory_key, float(tgt.timeout or 300))
    finally:
        if _ACTIVE.get(key) == run_id:
            _ACTIVE.pop(key, None)

    status = str(state.get("status") or "")
    out = state.get("output") or ""
    result = ChatResult(raw_text=out)
    result.effective_session_key = str(state.get("session_id") or session_key)
    if status == "cancelled":
        # A stopped turn must end silently: the reply for THIS turn is the new message's.
        result.raw_text = ""
        result.parse_failed = True
    elif status != "completed":
        err = str(state.get("error") or "")
        result.raw_text = out or "⚠️ 上游执行失败"
        result.is_transport_error = True
        if err:
            logger.warning(f"[OWNER-INTERRUPT] run {run_id} ended {status}: {err[:200]}")
    return result


async def stop_active(adapter_name: str, user_id: str, group_id: str | None = None,
                      is_private: bool = True) -> bool:
    key = _key(adapter_name, is_private, user_id, group_id)
    run_id = _ACTIVE.get(key)
    if not run_id:
        # Adapter naming can drift between the chat call and the handler; fall back to
        # any registered run for this private user.
        tail = f"|private|{user_id}" if is_private else f"|group|{group_id or ''}"
        run_id = next((rid for k, rid in _ACTIVE.items() if k.endswith(tail)), "")
        if run_id:
            logger.info(f"[OWNER-INTERRUPT] stop lookup missed {key}; matched by suffix {tail}")
    logger.info(f"[OWNER-INTERRUPT] stop lookup key={key} active={list(_ACTIVE)} -> {run_id or 'none'}")
    if not run_id:
        return False
    tgt = _target()
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(f"{tgt.base_url}/v1/runs/{run_id}/stop", json={},
                                     headers=_headers(tgt))
        logger.info(f"[OWNER-INTERRUPT] stop {run_id} -> HTTP {resp.status_code}")
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[OWNER-INTERRUPT] stop failed for {run_id}: {exc}")
        return False
    return True


def _kill_local_jobs(reason: str = "owner interrupt") -> int:
    """SIGTERM every long job that registered a pidfile under hermes-content/jobs.

    Stopping a /v1/runs turn interrupts the agent but does not reap the terminal
    processes it started, so a media send loop would keep going after 停.
    """
    import glob
    import signal

    jobs = os.path.expanduser("~/.local/share/hermes-content/jobs")
    killed = 0
    for path in glob.glob(os.path.join(jobs, "*.pid")):
        try:
            pid = int(open(path).read().strip())
        except (OSError, ValueError):
            try:
                os.remove(path)
            except OSError:
                pass
            continue
        try:
            os.kill(pid, signal.SIGTERM)
            killed += 1
            logger.info(f"[OWNER-INTERRUPT] {reason}: SIGTERM pid {pid} ({os.path.basename(path)})")
        except ProcessLookupError:
            pass
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[OWNER-INTERRUPT] kill pid {pid} failed: {exc}")
        try:
            os.remove(path)
        except OSError:
            pass
    return killed


async def _wait_slot_free(adapter_name: str, user_id: str, timeout: float = 20.0) -> None:
    """Wait until the plugin's in-flight slot for this private chat is released."""
    from nonebot_plugin_hermes import mcp as _mcp
    deadline = time.monotonic() + timeout
    key = (adapter_name, f"private:{user_id}")
    while time.monotonic() < deadline:
        reg = _mcp.inflight
        if reg is None or key not in getattr(reg, "_slots", {}):
            return
        await asyncio.sleep(0.2)


async def preempt_if_needed(adapter_name: str, user_id: str, is_private: bool, text: str) -> None:
    """Called from the plugin's passive path before a turn starts.

    Any owner message stops the live run; stop words also SIGTERM local jobs.
    """
    if not ENABLED or not is_private or str(user_id) not in OWNER_IDS:
        return
    stopped = await stop_active(adapter_name, str(user_id))
    if _looks_like_stop(text or ""):
        killed = _kill_local_jobs("stop command")
        logger.info(f"[OWNER-INTERRUPT] stop words from {user_id}: run_stopped={stopped} jobs_killed={killed}")
    elif stopped:
        logger.info(f"[OWNER-INTERRUPT] new message from {user_id}: stopped the live turn")
    if stopped:
        await _wait_slot_free(adapter_name, str(user_id))


async def chat_via_runs(self, **kwargs):
    return await _chat_runs(None, self, **kwargs)


STOP_WORDS = ("停", "stop", "cancel", "取消", "别发", "够了", "不用了")


def _looks_like_stop(text: str) -> bool:
    t = (text or "").strip().lower().replace(" ", "")
    return bool(t) and len(t) <= 12 and any(t.startswith(w) or t == w for w in STOP_WORDS)


def install() -> None:
    global _installed
    if _installed:
        return
    from nonebot_plugin_hermes.core import hermes_client as hc
    from nonebot_plugin_hermes.handlers import message as msg

    orig_chat = hc.HermesClient.chat

    async def chat(self, **kwargs):
        uid = str(kwargs.get("user_id") or "")
        if ENABLED and kwargs.get("is_private") and uid in OWNER_IDS:
            return await _chat_runs(orig_chat, self, **kwargs)
        return await orig_chat(self, **kwargs)

    hc.HermesClient.chat = chat

    orig_passive = msg._handle_passive_path

    async def passive(*args, **kwargs):
        adapter = kwargs.get("adapter_name") or ""
        uid = str(kwargs.get("user_id") or "")
        group_id = kwargs.get("group_id")
        if ENABLED and kwargs.get("is_private") and uid in OWNER_IDS:
            stopped = await stop_active(adapter, uid)
            if _looks_like_stop(kwargs.get("text") or ""):
                killed = _kill_local_jobs("stop command")
                logger.info(f"[OWNER-INTERRUPT] stop words from {uid}: run_stopped={stopped} jobs_killed={killed}")
            elif stopped:
                logger.info(f"[OWNER-INTERRUPT] new message from {uid}: stopped the live turn")
            if stopped:
                await _wait_slot_free(adapter, uid)
        return await orig_passive(*args, **kwargs)

    msg._handle_passive_path = passive
    hc.HermesClient.chat._owner_interrupt = True
    msg._handle_passive_path._owner_interrupt = True
    _installed = True
    logger.info(f"[OWNER-INTERRUPT] installed (enabled={ENABLED}, owners={OWNER_IDS})")
