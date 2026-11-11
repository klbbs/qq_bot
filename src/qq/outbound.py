"""Authenticated local outbound QQ bridge; never selects a recipient."""
import asyncio
import random
import hashlib
import hmac
import json
import sqlite3
from pathlib import Path
from typing import Literal

import nonebot
from fastapi import Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field, StrictStr
from qq_delivery_guard import collapse_text, SendBlocked

ROOT = Path(__file__).resolve().parent
TOKEN = (ROOT / '.outbound-token').read_text().strip()
LOCK = asyncio.Lock()


class SendRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    target_type: Literal['private', 'group']
    target_id: StrictStr = Field(pattern=r'^[1-9][0-9]{4,19}$')
    message: StrictStr = Field(min_length=1, max_length=4000)
    request_id: StrictStr = Field(pattern=r'^[A-Za-z0-9_-]{8,80}$')


def authorize(authorization):
    if not hmac.compare_digest(authorization or '', 'Bearer ' + TOKEN):
        raise HTTPException(401, 'Unauthorized')


def database():
    db = sqlite3.connect(ROOT / 'outbound.sqlite3')
    db.execute('CREATE TABLE IF NOT EXISTS sends (id TEXT PRIMARY KEY, digest TEXT, result TEXT)')
    return db


def register():
    app = nonebot.get_driver().server_app

    @app.get('/hermes-outbound/contacts')
    async def contacts(kind: Literal['private', 'group'], authorization: str = Header(default='')):
        authorize(authorization)
        try:
            bot = nonebot.get_bot('__QQ_BOT__')
            rows = await (bot.get_friend_list() if kind == 'private' else bot.get_group_list())
        except Exception:
            raise HTTPException(503, 'QQ contact list is unavailable') from None
        id_key, name_key = ('user_id', 'nickname') if kind == 'private' else ('group_id', 'group_name')
        return {'ok': True, 'target_type': kind, 'contacts': [
            {'id': str(row[id_key]), 'name': row.get(name_key, ''), 'remark': row.get('remark', '')}
            for row in rows]}

    @app.post('/hermes-outbound/send')
    async def send(body: SendRequest, authorization: str = Header(default='')):
        authorize(authorization)
        if not body.message.strip():
            raise HTTPException(422, 'Message must contain text')
        digest = hashlib.sha256(json.dumps(body.model_dump(exclude={'request_id'}), sort_keys=True).encode()).hexdigest()
        async with LOCK:
            with database() as db:
                prior = db.execute('SELECT digest,result FROM sends WHERE id=?', (body.request_id,)).fetchone()
                if prior:
                    if prior[0] != digest:
                        raise HTTPException(409, 'Request ID already used for different content')
                    return json.loads(prior[1])
                try:
                    bot = nonebot.get_bot('__QQ_BOT__')
                except KeyError:
                    raise HTTPException(503, 'Dedicated QQ bot is offline') from None
                pending = {'ok': False, 'state': 'uncertain', 'request_id': body.request_id,
                           'error': 'Delivery may have occurred. Do not retry with a new request ID.'}
                db.execute('INSERT INTO sends VALUES (?,?,?)', (body.request_id, digest, json.dumps(pending)))
                db.commit()
                try:
                    message = collapse_text(body.message)
                    # 多气泡：<SPLIT> 分段，每段一条独立消息
                    if '<SPLIT>' in message:
                        chunks = [seg.strip() for seg in message.split('<SPLIT>') if seg.strip()]
                        if len(chunks) > 1:
                            first = None
                            for seg in chunks:
                                data = {'message': [{'type': 'text', 'data': {'text': seg}}]}
                                r = await (bot.send_private_msg(user_id=int(body.target_id), **data)
                                           if body.target_type == 'private' else
                                           bot.send_group_msg(group_id=int(body.target_id), **data))
                                first = first or r
                                await asyncio.sleep(random.uniform(1.2, 2.6))
                            mid = (first or {}).get('message_id')
                            if mid is None:
                                return pending
                            answer = {'ok': True, 'state': 'sent', 'message_id': str(mid),
                                      'target_type': body.target_type, 'target_id': body.target_id,
                                      'request_id': body.request_id}
                            db.execute('UPDATE sends SET result=? WHERE id=?', (json.dumps(answer), body.request_id))
                            db.commit()
                            return answer
                    # Explicit text segments prevent CQ codes embedded in user text from becoming actions.
                    data = {'message': [{'type': 'text', 'data': {'text': message}}]}
                    result = await (bot.send_private_msg(user_id=int(body.target_id), **data)
                                    if body.target_type == 'private' else
                                    bot.send_group_msg(group_id=int(body.target_id), **data))
                    message_id = result.get('message_id')
                    if message_id is None:
                        return pending
                    answer = {'ok': True, 'state': 'sent', 'message_id': str(message_id),
                              'target_type': body.target_type, 'target_id': body.target_id, 'request_id': body.request_id}
                    db.execute('UPDATE sends SET result=? WHERE id=?', (json.dumps(answer), body.request_id))
                    db.commit()
                    return answer
                except SendBlocked as exc:
                    if '<SPLIT>' in message:
                        return pending
                    answer = {'ok': False, 'state': 'not_sent', 'reason': exc.reason,
                              'retry_after': exc.retry_after, 'request_id': body.request_id}
                    db.execute('UPDATE sends SET result=? WHERE id=?', (json.dumps(answer), body.request_id))
                    db.commit()
                    return answer
                except Exception:
                    return pending
