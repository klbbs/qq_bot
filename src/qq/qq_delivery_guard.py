"""Bounded QQ delivery, duplicate suppression and persistent error backoff.

No simulated human behaviour, protocol changes, automatic re-login or retries.
The adapter wrapper covers normal chat, commands, media, forwards and local API.
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from dataclasses import dataclass, fields
import functools
import hashlib
import hmac
import json
import math
from pathlib import Path
import secrets
import sqlite3
import time


SEND_APIS = frozenset({
    "send_msg", "send_private_msg", "send_group_msg", "send_group_forward_msg",
    "send_private_forward_msg", "send_forward_msg", "upload_group_file", "upload_private_file",
})


@dataclass(frozen=True)
class Policy:
    enabled: bool = True
    collapse_split: bool = True
    friend_auto_accept: bool = False
    global_interval: float = 1.0
    private_interval: float = 2.0
    group_interval: float = 3.0
    global_per_minute: int = 30
    private_per_minute: int = 20
    group_per_minute: int = 12
    duplicate_seconds: float = 20.0
    uncertain_seconds: float = 600.0
    network_backoff: float = 30.0
    risk_backoff: float = 600.0
    max_backoff: float = 1800.0
    max_wait: float = 20.0
    max_pending: int = 12

    @classmethod
    def load(cls, path: Path):
        if not path.exists():
            return cls()
        data = json.loads(path.read_text())
        if not isinstance(data, dict) or set(data) - {f.name for f in fields(cls)}:
            raise ValueError("Unknown QQ delivery policy fields")
        defaults = cls()
        for key, value in data.items():
            expected = getattr(defaults, key)
            if isinstance(expected, bool):
                valid = type(value) is bool
            elif isinstance(expected, int):
                valid = type(value) is int and 1 <= value <= 100000
            else:
                valid = type(value) in {int, float} and math.isfinite(value) and 0 < value <= 86400
            if not valid:
                raise ValueError("Invalid QQ delivery policy field: " + key)
        return cls(**data)


class SendBlocked(Exception):
    """Admission failed before a network send; never means delivery succeeded."""
    delivery_not_started = True

    def __init__(self, reason: str, retry_after: float = 0):
        self.reason = reason
        self.retry_after = max(0, math.ceil(retry_after))
        super().__init__("QQ send paused: " + reason)


class DeliveryUnconfirmed(Exception):
    """The transport returned no message acknowledgement; do not resend."""
    delivery_not_started = False


def api_name(api: str) -> str:
    for suffix in ("_rate_limited", "_async"):
        if api.endswith(suffix):
            api = api[:-len(suffix)]
    return api


def collapse_text(text: str) -> str:
    if "<SPLIT>" in text and get_policy().collapse_split:
        return "\n\n".join(piece.strip() for piece in text.split("<SPLIT>") if piece.strip())
    return text


def failure_kind(exc: BaseException) -> str:
    # Error wording is inspected in memory only. It may contain private data.
    info = getattr(exc, "info", {})
    message = " ".join(str(info.get(k, "")) for k in ("message", "wording")) if isinstance(info, dict) else ""
    message += " " + type(exc).__name__
    if any(word in message.lower() for word in ("风控", "频繁", "禁言", "封禁", "risk", "rate limit", "too many", "频率", "限制发送")):
        return "restricted"
    if any(word in message.lower() for word in ("登录态", "未登录", "掉线", "not logged", "offline")):
        return "login_required"
    return "network_or_unknown"


class Guard:
    def __init__(self, path: Path, policy: Policy | None = None, *, clock=time.time, sleep=asyncio.sleep):
        self.path, self.policy = path, policy or Policy()
        self.clock, self.sleep = clock, sleep
        self.lock = asyncio.Lock()
        self.pending = 0
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.db() as db:
            db.execute("CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS attempts (id INTEGER PRIMARY KEY, scope TEXT, digest TEXT, created REAL, outcome TEXT, receipt TEXT)")
            db.execute("CREATE INDEX IF NOT EXISTS attempts_time ON attempts(created)")
            db.execute("CREATE INDEX IF NOT EXISTS attempts_digest ON attempts(digest,created)")
            db.execute("INSERT OR IGNORE INTO state VALUES ('salt',?)", (json.dumps(secrets.token_hex(32)),))
            self.salt = self.get(db, "salt", "")

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=5)
        self.path.chmod(0o600)
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def get(db, key, default=0):
        row = db.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    @staticmethod
    def put(db, key, value):
        db.execute("INSERT OR REPLACE INTO state VALUES (?,?)", (key, json.dumps(value)))

    def fingerprint(self, value):
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode()
        return hmac.new(self.salt.encode(), encoded, hashlib.sha256).hexdigest()

    def inbound(self):
        with self.db() as db:
            self.put(db, "last_inbound_at", self.clock())

    def status(self):
        now = self.clock()
        with self.db() as db:
            until = self.get(db, "paused_until")
            last_success = self.get(db, "last_confirmed_send_at")
            last_error = self.get(db, "last_error_at")
            return {
                "enabled": self.policy.enabled,
                "send_paused": until > now,
                "retry_after": max(0, math.ceil(until - now)),
                "last_error_kind": self.get(db, "last_error_kind", ""),
                "delivery_state": "degraded" if last_error > last_success else "confirmed" if last_success else "unverified",
                "last_confirmed_send_at": last_success or None,
                "last_inbound_at": self.get(db, "last_inbound_at") or None,
                "pending": self.pending,
            }

    def _admit(self, bot_id, name, data):
        now, p = self.clock(), self.policy
        group = data.get("group_id") is not None or data.get("message_type") == "group"
        target = data.get("group_id") if group else data.get("user_id")
        scope = self.fingerprint([str(bot_id), "group" if group else "private", str(target)])
        digest = self.fingerprint([scope, name, data])
        with self.db() as db:
            until = self.get(db, "paused_until")
            if until > now:
                raise SendBlocked("backoff", until - now)
            keep = max(3600, p.uncertain_seconds, p.duplicate_seconds)
            db.execute("DELETE FROM attempts WHERE created<?", (now - keep,))
            prior = db.execute("SELECT created,outcome,receipt FROM attempts WHERE digest=? ORDER BY id DESC LIMIT 1", (digest,)).fetchone()
            if prior:
                ttl = p.duplicate_seconds if prior[1] == "sent" else p.uncertain_seconds
                if prior[0] + ttl > now:
                    if prior[1] == "sent":
                        return ("duplicate", json.loads(prior[2]))
                    raise SendBlocked("uncertain_duplicate", prior[0] + ttl - now)
            rows = db.execute("SELECT scope,created FROM attempts WHERE created>? ORDER BY created", (now - max(60, p.global_interval, p.group_interval, p.private_interval),)).fetchall()
            same = [stamp for bucket, stamp in rows if bucket == scope]
            minute = [stamp for _, stamp in rows if stamp > now - 60]
            same_minute = [stamp for stamp in same if stamp > now - 60]
            wait = max(0, (rows[-1][1] + p.global_interval - now) if rows else 0,
                       (same[-1] + (p.group_interval if group else p.private_interval) - now) if same else 0)
            if len(minute) >= p.global_per_minute:
                wait = max(wait, minute[-p.global_per_minute] + 60 - now)
            limit = p.group_per_minute if group else p.private_per_minute
            if len(same_minute) >= limit:
                wait = max(wait, same_minute[-limit] + 60 - now)
            if wait > 0:
                return ("wait", wait)
            ident = db.execute("INSERT INTO attempts(scope,digest,created,outcome) VALUES (?,?,?,'pending')", (scope, digest, now)).lastrowid
            return ("admitted", ident)

    def _success(self, ident, result):
        # Cache only message receipt fields, never content, credentials or full responses.
        receipt = {k: result[k] for k in ("message_id", "res_id", "file_id") if k in result} if isinstance(result, dict) else None
        with self.db() as db:
            db.execute("UPDATE attempts SET outcome='sent',receipt=? WHERE id=?", (json.dumps(receipt), ident))
            self.put(db, "last_confirmed_send_at", self.clock())
            self.put(db, "failures", 0)

    def _failure(self, ident, exc):
        p, now = self.policy, self.clock()
        kind = failure_kind(exc)
        with self.db() as db:
            count = min(12, self.get(db, "failures") + 1)
            duration = min(p.max_backoff, (p.risk_backoff if kind in {"restricted", "login_required"} else p.network_backoff) * 2 ** (count - 1))
            db.execute("UPDATE attempts SET outcome='uncertain' WHERE id=?", (ident,))
            self.put(db, "failures", count)
            self.put(db, "last_error_at", now)
            self.put(db, "last_error_kind", kind)
            self.put(db, "paused_until", now + duration)

    async def call(self, bot_id, api, data, invoke):
        name = api_name(api)
        if name not in SEND_APIS or not self.policy.enabled:
            return await invoke()
        if self.pending >= self.policy.max_pending:
            raise SendBlocked("queue_full")
        self.pending += 1
        acquired = False
        deadline = asyncio.get_running_loop().time() + self.policy.max_wait
        try:
            try:
                await asyncio.wait_for(self.lock.acquire(), timeout=self.policy.max_wait)
                acquired = True
            except TimeoutError:
                raise SendBlocked("queue_timeout") from None
            while True:
                kind, value = self._admit(bot_id, name, data)
                if kind == "duplicate":
                    return value
                if kind == "admitted":
                    break
                if asyncio.get_running_loop().time() + value > deadline:
                    raise SendBlocked("rate_limit", value)
                await self.sleep(value)
            try:
                result = await invoke()
                if name.startswith("send_") and (not isinstance(result, dict) or result.get("message_id") is None):
                    raise DeliveryUnconfirmed("QQ message acknowledgement missing")
            except BaseException as exc:
                self._failure(value, exc)
                raise
            self._success(value, result)
            return result
        finally:
            if acquired:
                self.lock.release()
            self.pending -= 1


_ROOT = Path(__file__).resolve().parent
_policy = None
_guard = None


def get_policy():
    global _policy
    if _policy is None:
        _policy = Policy.load(_ROOT / "delivery-policy.json")
    return _policy


def install():
    """Wrap the transport rather than hooks that can swallow admission failures."""
    global _guard
    from nonebot.adapters.onebot.v11 import Adapter, MessageEvent
    from nonebot.exception import IgnoredException
    from nonebot.message import event_preprocessor
    import nonebot
    import outbound
    from fastapi import Header

    original = Adapter._call_api
    if getattr(original, "_qq_delivery_guard", False):
        return
    _guard = Guard(_ROOT / ".delivery-guard.sqlite3", get_policy())

    @functools.wraps(original)
    async def wrapped(adapter, bot, api, **data):
        return await _guard.call(bot.self_id, api, data, lambda: original(adapter, bot, api, **data))

    wrapped._qq_delivery_guard = True
    Adapter._call_api = wrapped
    seen = {}

    async def inbound(bot, event: MessageEvent):
        now = time.monotonic()
        key = (str(bot.self_id), str(getattr(event, "group_id", "private")), str(event.user_id), str(event.message_id))
        if seen.get(key, 0) > now:
            raise IgnoredException("Duplicate QQ event")
        if len(seen) >= 8192:
            for old in [k for k, until in seen.items() if until <= now]:
                seen.pop(old, None)
            if len(seen) >= 8192:
                seen.pop(next(iter(seen)))
        seen[key] = now + 120
        _guard.inbound()

    inbound.__annotations__["event"] = MessageEvent
    event_preprocessor(inbound)

    app = nonebot.get_driver().server_app

    @app.get("/hermes-outbound/health")
    async def health(authorization: str = Header(default="")):
        outbound.authorize(authorization)
        online = False
        try:
            bot = nonebot.get_bot()
            status = await bot.get_status()
            online = status.get("online") is True and status.get("good") is True
        except Exception:
            pass
        return {"transport_reported_online": online, **_guard.status()}
