"""Asynchronous local audio transcription for Bilibili video parts."""
import asyncio
import fcntl
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

from content_worker import ROOT, bili_credential, dump, load


def job_dir(target, part):
    match = re.search(r'BV[0-9A-Za-z]{10}', str(target))
    if not match or not 1 <= int(part) <= 10000:
        raise ValueError('Provide a valid BV identifier and video part.')
    return ROOT / 'cache/jobs' / (match.group() + '-p' + str(int(part)))


def result(args):
    d = job_dir(args['target'], args.get('part', 1))
    state = load(d / 'state.json')
    state.pop('pid', None)
    full = load(d / 'transcript.json')
    if full:
        offset = max(0, int(args.get('offset', 0)))
        body = full['segments']
        state.update(source_kind='local_audio_transcript', language=full['language'],
                     subtitle=body[offset:offset+300], total_segments=len(body), offset=offset,
                     complete=len(body) <= offset+300,
                     note='Speech recognition of audio; visual-only content is not represented.')
        if not state['complete']:
            state['next_offset'] = offset+300
    return state or {'state': 'not_started'}


def start(args):
    d = job_dir(args['target'], args.get('part', 1))
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (d / 'lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = load(d / 'state.json')
        if state.get('state') == 'complete':
            return result(args)
        if state.get('pid'):
            try:
                os.kill(state['pid'], 0)
                return result(args)
            except ProcessLookupError:
                pass
        dump(d / 'state.json', {'state': 'starting', 'created_at': time.time()})
        with (d / 'worker.log').open('w') as log:
            process = subprocess.Popen([sys.executable, str(__file__), str(d)], stdin=subprocess.DEVNULL,
                stdout=log, stderr=log, start_new_session=True)
        dump(d / 'state.json', {'state': 'starting', 'created_at': time.time(), 'pid': process.pid})
    return result(args)


async def download_audio(bvid, part, d):
    import aiohttp
    from bilibili_api import video
    obj = video.Video(bvid=bvid, credential=bili_credential())
    info = await obj.get_info()
    pages = info.get('pages', [])
    if not 1 <= part <= len(pages):
        raise ValueError('Video part is out of range.')
    duration = pages[part-1].get('duration', 0)
    if duration > 4*3600:
        raise ValueError('This video part exceeds four hours; use a shorter part or provide subtitles.')
    streams = await obj.get_download_url(page_index=part-1)
    audio = streams.get('dash', {}).get('audio', [])
    if not audio:
        raise RuntimeError('No accessible standalone audio stream was provided by Bilibili.')
    selected = min(audio, key=lambda a: a.get('bandwidth', 100000))
    url = selected.get('baseUrl') or selected.get('base_url')
    host = (urlparse(url).hostname or '').lower()
    if urlparse(url).scheme != 'https' or not any(host.endswith('.'+domain) or host == domain for domain in ['bilivideo.com', 'bilivideo.cn', 'bilibili.com']):
        raise ValueError('Unexpected audio CDN host.')
    dest = d / 'audio.m4a'
    headers = {'Referer': 'https://www.bilibili.com/', 'User-Agent': 'Mozilla/5.0'}
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=600), headers=headers) as session:
        async with session.get(url, allow_redirects=False) as response:
            response.raise_for_status()
            count = 0
            with dest.open('wb') as f:
                async for chunk in response.content.iter_chunked(1024*1024):
                    count += len(chunk)
                    if count > 400*1024*1024:
                        raise RuntimeError('Audio exceeds the 400 MB per-job limit.')
                    f.write(chunk)
    return dest, info.get('title')


def worker(d):
    created = time.time()
    state = {'state': 'queued', 'created_at': created, 'pid': os.getpid()}
    try:
        # One CPU transcription at a time; all callers can still query their job status.
        with (ROOT / 'cache/asr.lock').open('w') as lock:
            dump(d / 'state.json', state)
            fcntl.flock(lock, fcntl.LOCK_EX)
            bvid, part = d.name.rsplit('-p', 1)
            dump(d / 'state.json', {**state, 'state': 'downloading_audio'})
            audio, title = asyncio.run(download_audio(bvid, int(part), d))
            from faster_whisper import WhisperModel
            dump(d / 'state.json', {**state, 'state': 'transcribing', 'title': title})
            model = WhisperModel(str(ROOT / 'models/faster-whisper-base'), device='cpu', compute_type='int8', cpu_threads=4)
            segments, info = model.transcribe(str(audio), beam_size=3, vad_filter=True)
            text = []
            for segment in segments:
                text.append({'from': round(segment.start, 2), 'to': round(segment.end, 2), 'content': segment.text.strip()})
                dump(d / 'state.json', {**state, 'state': 'transcribing', 'title': title, 'processed_seconds': segment.end})
            dump(d / 'transcript.json', {'language': info.language, 'segments': text})
            dump(d / 'state.json', {**state, 'state': 'complete', 'title': title, 'url': 'https://www.bilibili.com/video/'+bvid})
            audio.unlink(missing_ok=True)
    except Exception as exc:
        dump(d / 'state.json', {**state, 'state': 'failed', 'error': str(exc)[:600]})


if __name__ == '__main__':
    os.umask(0o077)
    worker(Path(sys.argv[1]))
