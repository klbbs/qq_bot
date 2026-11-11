"""Bounded, complete chat-completion collection; reasoning never enters feedback."""
import json

import httpx

LIMIT = 2 * 1024 * 1024
# Streaming adds a JSON envelope to every token. Bound its wire bytes without
# shrinking the configured 32K/128K thinking budget to a few thousand tokens.
STREAM_LIMIT = 32 * 1024 * 1024


async def completion(client, endpoint, key, body):
    headers = {'Authorization': 'Bearer ' + key}
    if not body.get('stream'):
        return await client.post(endpoint, headers=headers, json=body)
    async with client.stream('POST', endpoint, headers=headers, json=body) as response:
        if response.status_code != 200:
            return httpx.Response(response.status_code, json={})
        # Some compatible services return normal JSON even when stream=true.
        if 'text/event-stream' not in response.headers.get('content-type', '').lower():
            data = bytearray()
            async for chunk in response.aiter_bytes():
                data.extend(chunk)
                if len(data) > LIMIT: raise ValueError('oversized completion')
            return httpx.Response(200, content=bytes(data))
        content = []; content_size = 0; consumed = 0; finish = None; usage = None
        event = []; done = False
        def process(lines):
            nonlocal finish, usage, content_size, done
            if not lines: return
            payload = '\n'.join(lines)
            if payload == '[DONE]': done = True; return
            chunk = json.loads(payload)
            if not isinstance(chunk, dict) or 'error' in chunk: raise ValueError('invalid chunk')
            if isinstance(chunk.get('usage'), dict): usage = chunk['usage']
            choices = chunk.get('choices')
            if not isinstance(choices, list): raise ValueError('invalid choices')
            if len(choices) > 1: raise ValueError('multiple choices')
            if not choices: return
            choice = choices[0]
            if not isinstance(choice, dict) or choice.get('index', 0) != 0: raise ValueError('invalid choice')
            delta = choice.get('delta') or {}
            if not isinstance(delta, dict) or delta.get('tool_calls'): raise ValueError('invalid delta')
            text = delta.get('content')
            if text is not None:
                if not isinstance(text, str) or (text and finish is not None): raise ValueError('invalid content')
                content_size += len(text)
                if content_size > 64000: raise ValueError('oversized content')
                content.append(text)
            # reasoning_content is deliberately neither stored nor returned.
            if choice.get('finish_reason') is not None:
                if finish is not None: raise ValueError('duplicate finish')
                finish = choice['finish_reason']
        async for line in response.aiter_lines():
            consumed += len(line.encode('utf-8')) + 1
            if consumed > STREAM_LIMIT: raise ValueError('oversized stream')
            if not line:
                process(event); event = []
                if done: break
            elif line.startswith('data:'): event.append(line[5:].lstrip(' '))
            elif line.startswith((':', 'event:', 'id:', 'retry:')): continue
            else: raise ValueError('invalid SSE line')
        if event and not done: process(event)
        if not done or finish is None: raise ValueError('incomplete stream')
        data = {'choices': [{'finish_reason': finish, 'message': {'content': ''.join(content)}}]}
        if usage is not None: data['usage'] = usage
        return httpx.Response(200, json=data)
