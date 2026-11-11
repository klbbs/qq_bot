"""Private, durable ACM receipt inbox. Never creates a draw or invokes a model.

ACK means this client saved the exact result and optional durable preparation,
not that a QQ recipient read it. The prepare callback must be idempotent and
must not send chat messages; it may validate/download files and enqueue work.
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
import hashlib
import inspect
import json
import os
from pathlib import Path
import re
import sqlite3
import time
from urllib.parse import urlsplit

import httpx

PROTOCOL = 'acm-hermes-receipt-v1'
MAX_RESULT_BYTES = 1024 * 1024
MAX_FRAME_BYTES = 4 * 1024 * 1024
_ID = re.compile(r'[a-f0-9]{32}\Z')
_DIGEST = re.compile(r'[a-f0-9]{64}\Z')


class ReceiptError(ValueError):
    """Keep the original paid request pending; never auto-roll a replacement."""
    pending = True

    def __init__(self, message, *, status_code=None, retry_after=None):
        super().__init__(message)
        self.status_code = status_code
        self.retry_after = retry_after


def _object(text):
    try:
        def invalid(_):
            raise ValueError('non-finite number')
        value = json.loads(text, parse_constant=invalid)
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise ReceiptError('ACM 交付内容不是有效 JSON；原任务保留，未确认。') from None
    if not isinstance(value, dict):
        raise ReceiptError('ACM 交付内容格式异常；原任务保留，未确认。')
    return value


class Receipts:
    def __init__(self, root, *, origin='__ACM_ORIGIN__'):
        parsed = urlsplit(origin)
        if parsed.scheme != 'https' or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ('', '/'):
            raise ValueError('receipt origin must be an HTTPS origin')
        self.origin = origin.rstrip('/')
        self.root = Path(root)
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path = self.root / 'receipt-inbox.sqlite3'
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        os.fchmod(fd, 0o600)
        os.close(fd)
        with self.db() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS receipt_inbox (
                origin TEXT NOT NULL, scope TEXT NOT NULL, job_id TEXT NOT NULL,
                request_key TEXT NOT NULL, delivery_id TEXT NOT NULL,
                result_sha256 TEXT NOT NULL, result_json TEXT NOT NULL,
                state TEXT NOT NULL, stored_at REAL NOT NULL, prepared_json TEXT, forward_state TEXT,
                PRIMARY KEY(origin,scope,delivery_id),
                UNIQUE(origin,scope,job_id), UNIQUE(origin,scope,request_key))''')

    @contextmanager
    def db(self):
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        except sqlite3.Error:
            raise ReceiptError('本地结果收件箱无法提交；原任务保留，未发送回执。') from None
        finally:
            connection.close()

    @staticmethod
    def _arguments(job_id, request_key, scope):
        if not isinstance(job_id, str) or not _ID.fullmatch(job_id):
            raise ValueError('invalid job id')
        if not isinstance(request_key, str) or not _ID.fullmatch(request_key):
            raise ValueError('invalid request key')
        if not isinstance(scope, str) or not scope.strip() or len(scope) > 200:
            raise ValueError('invalid workspace scope')

    def _load(self, job_id, request_key, scope):
        with self.db() as db:
            rows = db.execute('SELECT * FROM receipt_inbox WHERE origin=? AND scope=? AND (job_id=? OR request_key=?)',
                              (self.origin, scope, job_id, request_key)).fetchall()
        if not rows:
            return None
        if len(rows) != 1 or rows[0]['job_id'] != job_id or rows[0]['request_key'] != request_key:
            raise ReceiptError('本地回执与原任务编号不一致；已停止，未另抽题。')
        record = dict(rows[0])
        self._document(record)
        return record

    @staticmethod
    def _document(record):
        text = record['result_json']
        try:
            raw = text.encode('utf-8')
        except (AttributeError, UnicodeError):
            raise ReceiptError('ACM 交付原始内容编码异常；未确认。') from None
        if len(raw) > MAX_RESULT_BYTES or hashlib.sha256(raw).hexdigest() != record['result_sha256']:
            raise ReceiptError('ACM 结果摘要不匹配；原任务保留，未确认。')
        return _object(text)

    @staticmethod
    def _event(message, job_id):
        if (not isinstance(message, dict) or message.get('protocol') != PROTOCOL or message.get('job_id') != job_id
                or not isinstance(message.get('delivery_id'), str) or not _ID.fullmatch(message['delivery_id'])
                or not isinstance(message.get('result_sha256'), str) or not _DIGEST.fullmatch(message['result_sha256'])):
            raise ReceiptError('ACM 回执协议或任务身份不一致；已停止，未确认。')
        kind = message.get('type')
        if kind not in {'result', 'acknowledged', 'pending', 'waiting'}:
            raise ReceiptError('ACM 返回未知交付状态；原任务保留。')
        if kind == 'acknowledged' and message.get('state') != 'acknowledged':
            raise ReceiptError('ACM 未明确确认回执；原任务保留。')
        if kind == 'result':
            if not isinstance(message.get('result_json'), str):
                raise ReceiptError('ACM 未交付原始结果字符串；未确认。')
            Receipts._document(message)
        return kind

    def _save(self, message, job_id, request_key, scope):
        # Complete raw bytes are committed before any optional preparation/ACK.
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT * FROM receipt_inbox WHERE origin=? AND scope=? AND (job_id=? OR delivery_id=? OR request_key=?)',
                             (self.origin, scope, job_id, message['delivery_id'], request_key)).fetchall()
            if old:
                if len(old) != 1 or any(old[0][name] != value for name, value in (
                    ('job_id', job_id), ('request_key', request_key), ('delivery_id', message['delivery_id']),
                    ('result_sha256', message['result_sha256']), ('result_json', message['result_json']))):
                    raise ReceiptError('同一 ACM 消息的编号或原始内容发生变化；已停止，未确认。')
            else:
                db.execute('INSERT INTO receipt_inbox(origin,scope,job_id,request_key,delivery_id,result_sha256,result_json,state,stored_at) VALUES(?,?,?,?,?,?,?,?,?)',
                           (self.origin, scope, job_id, request_key, message['delivery_id'],
                            message['result_sha256'], message['result_json'], 'stored', time.time()))
        return self._load(job_id, request_key, scope)

    def _state(self, record, state):
        with self.db() as db:
            changed=db.execute('UPDATE receipt_inbox SET state=? WHERE origin=? AND scope=? AND job_id=? AND delivery_id=? AND result_sha256=?',
                       (state, self.origin, record['scope'], record['job_id'], record['delivery_id'], record['result_sha256'])).rowcount
            if changed != 1:
                raise ReceiptError('本地回执记录已变化；保留原任务，未标记确认。')
        record['state'] = state

    def _prepared(self, record):
        text = record.get('prepared_json')
        if not isinstance(text, str):
            raise ReceiptError('本地没有已提交的完整接收缓存；原任务保留，未伪造回执。')
        result = _object(text)
        result['_receipt'] = {name:record[name] for name in ('job_id','request_key','scope','delivery_id','result_sha256')}
        result['_receipt'].update(protocol=PROTOCOL, origin=self.origin, forward_state=record.get('forward_state') or 'pending')
        return result

    def mark_sent(self, delivery_id, scope):
        """Only the caller's confirmed chat-channel send updates this local flag.

        This does not send an ACM ACK and does not assert the human read it.
        """
        if not isinstance(delivery_id,str) or not _ID.fullmatch(delivery_id):
            raise ValueError('invalid delivery id')
        if not isinstance(scope,str) or not scope.strip() or len(scope)>200:
            raise ValueError('invalid scope')
        with self.db() as db:
            changed=db.execute("UPDATE receipt_inbox SET forward_state='sent' WHERE origin=? AND scope=? AND delivery_id=? AND prepared_json IS NOT NULL",
                               (self.origin,scope,delivery_id)).rowcount
        if changed != 1:
            raise ReceiptError('本地没有对应的已持久接收计划；未标记聊天渠道已发送。')

    async def _prepare(self, record, prepare):
        if record['state'] in {'prepared', 'acknowledged'}:
            return
        result = self._document(record)
        if prepare is not None:
            delivery = {key: record[key] for key in ('job_id', 'request_key', 'delivery_id', 'result_sha256')}
            delivery.update(protocol=PROTOCOL, scope=record['scope'])
            outcome = prepare(result, delivery)
            if inspect.isawaitable(outcome):
                outcome = await outcome
            if outcome is not True:
                raise ReceiptError('题目结果已保存，附加接收步骤尚未确认；保留原任务，未发送回执。')
        try:
            prepared_json = json.dumps(result, ensure_ascii=False, allow_nan=False, separators=(',',':'))
            if len(prepared_json.encode('utf-8')) > 3*1024*1024:
                raise ValueError('prepared result too large')
        except (ValueError, TypeError, UnicodeError):
            raise ReceiptError('本地完整接收结果无法持久保存；原文保留，未发送回执。') from None
        # Prepared content and the downstream pending outbox flag are one commit.
        with self.db() as db:
            changed=db.execute("UPDATE receipt_inbox SET state='prepared',prepared_json=?,forward_state='pending' WHERE origin=? AND scope=? AND job_id=? AND delivery_id=? AND result_sha256=?",
                       (prepared_json,self.origin,record['scope'],record['job_id'],record['delivery_id'],record['result_sha256'])).rowcount
            if changed != 1:
                raise ReceiptError('本地接收记录已变化；未发送回执。')
        record.update(state='prepared',prepared_json=prepared_json,forward_state='pending')

    @staticmethod
    def _status(response):
        if response.status_code != 200:
            raise ReceiptError('ACM 结果交付未获确认（HTTP '+str(response.status_code)+'）；请检查登录或稍后恢复原任务，不会重抽。',
                               status_code=response.status_code, retry_after=response.headers.get('Retry-After'))

    async def _ack(self, client, record):
        response = await client.post(self.origin+'/api/hermes/draws/'+record['job_id']+'/delivery/ack',
                                     json={name: record[name] for name in ('delivery_id','result_sha256')} | {'received':True},
                                     headers={'X-ACM-Request':'1','Origin':self.origin}, follow_redirects=False, timeout=25)
        self._status(response)
        if len(response.content) > MAX_FRAME_BYTES:
            raise ReceiptError('ACM 回执响应过大；原任务保留。')
        message = _object(response.content)
        if (self._event(message, record['job_id']) != 'acknowledged'
                or message['delivery_id'] != record['delivery_id'] or message['result_sha256'] != record['result_sha256']):
            raise ReceiptError('ACM 回执与本地已存消息不一致；原任务保留。')
        self._state(record, 'acknowledged')
        return self._prepared(record)

    async def receive(self, client, job_id, request_key, scope, *, prepare=None, connections=3, should_stop=None):
        """Commit exact result, run optional durable preparation, then ACK.

        prepare(result, delivery) may be async and must return exactly True.
        Once recorded as prepared, it is not repeated across transport retries
        or restarts. To survive a crash between callback and this marker, the
        callback itself must be idempotent; it must never directly send QQ.
        All non-200 statuses stop immediately, including 401/403/409/429.
        """
        self._arguments(job_id, request_key, scope)
        if type(connections) is not int or not 1 <= connections <= 10:
            raise ValueError('connections must be 1..10')
        if prepare is not None and not callable(prepare):
            raise ValueError('prepare must be callable')
        base = urlsplit(str(client.base_url))
        if (base.scheme, base.netloc) != (urlsplit(self.origin).scheme, urlsplit(self.origin).netloc):
            raise ReceiptError('ACM 客户端站点与结果收件箱不一致；未发送请求。')
        record = self._load(job_id, request_key, scope)
        identity = (record['delivery_id'], record['result_sha256']) if record else None
        for attempt in range(connections):
            if should_stop is not None and should_stop():
                raise ReceiptError('结果接收已暂停；已保存内容和原任务编号保留，未重抽。')
            if record and record['state'] == 'acknowledged':
                return self._prepared(record)
            try:
                if record:
                    await self._prepare(record, prepare)
                    return await self._ack(client, record)
                async with client.stream('POST', self.origin+'/api/hermes/draws/'+job_id+'/delivery/stream',
                                         json={}, headers={'X-ACM-Request':'1','Origin':self.origin}, follow_redirects=False, timeout=75) as response:
                    self._status(response)
                    if response.headers.get('content-type','').split(';',1)[0].strip().lower() != 'application/x-ndjson':
                        raise ReceiptError('服务器未提供可靠结果交付流；原任务保留，未伪造回执。')
                    buffer = b''
                    async for chunk in response.aiter_bytes():
                        buffer += chunk
                        while b'\n' in buffer:
                            line, buffer = buffer.split(b'\n', 1)
                            if len(line) > MAX_FRAME_BYTES:
                                raise ReceiptError('ACM 交付消息过大；未确认。')
                            if not line.strip():
                                continue
                            message = _object(line)
                            kind = self._event(message, job_id)
                            current = (message['delivery_id'], message['result_sha256'])
                            if identity is not None and identity != current:
                                raise ReceiptError('ACM 重连返回了不同交付；原任务保留，未确认。')
                            identity = current
                            if kind == 'acknowledged':
                                raise ReceiptError('服务器称结果已确认，但本地没有对应原始结果；请管理员检查原任务。')
                            if kind == 'result':
                                record = self._save(message, job_id, request_key, scope)
                                await self._prepare(record, prepare)
                                return await self._ack(client, record)
                        if len(buffer) > MAX_FRAME_BYTES:
                            raise ReceiptError('ACM 交付消息过大；未确认。')
                    if buffer.strip():
                        raise httpx.ReadError('incomplete delivery frame')
            except httpx.TransportError:
                # Unknown ACK result: retry the exact persisted ACK, never draw.
                pass
            if attempt + 1 < connections:
                await asyncio.sleep(min(attempt + 1, 2))
        raise ReceiptError('ACM 结果交付暂未确认；原消息和任务编号已保留，请稍后恢复，不会重抽。')
