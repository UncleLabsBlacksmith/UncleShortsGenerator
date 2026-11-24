"""OpenRouter video pipeline and reviewable Windows upload task exports."""
from __future__ import annotations
import json
from app_info import APP_NAME
import base64
import hashlib
import math
import os
import shutil
from pathlib import Path
import subprocess
import sys
import time
import threading
from router_retry import RouterHTTPError, RetryCancelled, open_with_retry
import urllib.request
import urllib.error
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from urllib.parse import quote
from xml.sax.saxutils import escape

RESOURCE_ROOT = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent))
INSTALLED = bool(getattr(sys, 'frozen', False)) or os.environ.get('SHORTS_STUDIO_INSTALLED') == '1'
_user_root = Path(os.environ.get('LOCALAPPDATA', Path.home())) / 'UncleEngineer'
_legacy_data = _user_root / 'ShortsStudio'
_default_data = _legacy_data if _legacy_data.exists() else _user_root / 'UncleShortsGenerator'
BASE = Path(os.environ.get('SHORTS_STUDIO_DATA_DIR') or _default_data) if INSTALLED else RESOURCE_ROOT
API = 'https://openrouter.ai/api/v1'


def read_json(path):
    if Path(path).suffix == '.sqlite3':
        from studio_store import load_document
        return load_document(path)
    return json.loads(Path(path).read_text(encoding='utf-8'))


def save_json(path, data):
    if Path(path).suffix == '.sqlite3':
        from studio_store import save_document
        save_document(path, data)
        return
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    temp.replace(path)


class Router:
    def __init__(self, key=None, project=None, catalog=None, day=None, category='text', stop=None, retry_log=print, max_retries=4, progress=None):
        self.key = key or os.environ.get('OPENROUTER_API_KEY', '')
        from clip_costs import Ledger
        self.ledger = Ledger(project) if project else None
        self.cost_catalog = catalog or {}
        self.day, self.cost_category = day, category
        self.stop, self.retry_log, self.max_retries = stop if stop is not None else threading.Event(), retry_log, max_retries
        self.progress = progress
        self.restart_video_on_429 = False

    def record_cost(self, route, payload, result, identifier=None):
        if not self.ledger:
            return
        from clip_costs import estimate_request
        category = 'video' if route == '/videos' else 'speech' if route == '/audio/speech' else self.cost_category
        usage = result.get('usage') or {} if isinstance(result, dict) else {}
        actual = usage.get('cost')
        if actual is None and isinstance(result, dict):
            actual = result.get('cost')
        self.ledger.record(category, payload.get('model', ''), self.day, actual,
            estimate_request(route, payload, result, self.cost_catalog), identifier or (result.get('id') if isinstance(result, dict) else None))

    def request(self, route, payload=None, binary=False):
        if payload is not None and not self.key:
            raise ValueError('กรุณาใส่ OpenRouter API key')
        headers = {'Content-Type': 'application/json', 'X-Title': APP_NAME}
        if self.key:
            headers['Authorization'] = 'Bearer ' + self.key
        req = urllib.request.Request(API + route, data=None if payload is None else json.dumps(payload).encode(), headers=headers)
        try:
            with open_with_retry(req, 180, self.stop, self.retry_log, self.max_retries,
                abort_video_429=self.restart_video_on_429 and route.startswith('/videos/') and route != '/videos/models') as response:
                data = response.read()
                generation_id = response.headers.get('X-Generation-Id')
        except RouterHTTPError as error:
            if payload is not None and not error.rejected:
                self.record_cost(route, payload, {})
            raise
        result = data if binary else json.loads(data)
        if payload is not None and route in ('/videos', '/audio/speech', '/chat/completions'):
            self.record_cost(route, payload, result, generation_id if binary else None)
        elif self.ledger and route.startswith('/videos/') and not binary:
            identifier = route.split('/')[2]
            with __import__('studio_store').connect(self.ledger.path) as db:
                exists = db.execute('SELECT 1 FROM cost_events WHERE id=?', (identifier,)).fetchone()
            if exists:
                self.record_cost('/videos', {}, result, identifier)
        return result

    def catalog(self):
        catalog = {name: self.request(route)['data'] for name, route in (
            ('text', '/models?output_modalities=text'), ('video', '/videos/models'), ('speech', '/audio/speech/models'))}
        catalog['audio'] = self.request('/models?output_modalities=audio')['data']
        catalog['music'] = music_models(catalog['audio'])
        catalog['all'] = self.request('/models?output_modalities=all')['data']
        return catalog

    def check_key(self):
        if not self.key.strip():
            raise ValueError('กรุณากรอก OpenRouter API key ก่อนกด Check API Key')
        result = self.request('/key')
        if not isinstance(result.get('data'), dict):
            raise ValueError('OpenRouter ส่งผลตรวจสอบ API key ไม่ถูกต้อง')
        return result['data']

    def music(self, model, prompt, audio_format='wav', stop=None):
        if not self.key:
            raise ValueError('กรุณาใส่ OpenRouter API key')
        payload = {'model': model, 'messages': [{'role': 'user', 'content': prompt}],
                   'modalities': ['text', 'audio'], 'audio': {'format': audio_format}, 'stream': True}
        req = urllib.request.Request(API + '/chat/completions', data=json.dumps(payload).encode(),
            headers={'Content-Type': 'application/json', 'Accept': 'text/event-stream',
                     'Authorization': 'Bearer ' + self.key, 'X-Title': APP_NAME})
        try:
            with open_with_retry(req, 300, stop if stop is not None else self.stop, self.retry_log, self.max_retries) as response:
                event_id = self.ledger.record('music', model, self.day, identifier=response.headers.get('X-Generation-Id')) if self.ledger else None
                def usage(chunk):
                    if self.ledger:
                        from clip_costs import estimate_request
                        self.ledger.record('music', model, self.day, (chunk.get('usage') or {}).get('cost'),
                            estimate_request('/chat/completions', {'model': model}, chunk, self.cost_catalog), event_id)
                return decode_music_stream(response, stop, usage)
        except RouterHTTPError:
            raise

    def json_chat(self, model, prompt):
        result = self.request('/chat/completions', {'model': model, 'messages': [
            {'role': 'system', 'content': 'You are a factual short documentary producer. Return valid JSON only. Do not invent facts, quotes or sources. State uncertainties.'},
            {'role': 'user', 'content': prompt}], 'max_tokens': 12000})
        content = result['choices'][0]['message']['content'].strip()
        if content.startswith('```'):
            content = content.split('\n', 1)[1].rsplit('```', 1)[0]
        return json.loads(content)


def validate_settings(c):
    for key in ('days', 'seconds', 'duration'):
        c[key] = int(c[key])
    for name in ('idea', 'series', 'text_model', 'video_model', 'tts_model', 'voice'):
        if not str(c.get(name, '')).strip():
            raise ValueError(f'กรุณาระบุ {name}')
    if not 1 <= int(c['days']) <= 365:
        raise ValueError('จำนวนวันต้องเป็น 1–365')
    if not 5 <= int(c['seconds']) <= 180 or int(c['duration']) <= 0:
        raise ValueError('คลิปต้องยาว 5–180 วินาที และความยาวฉากต้องมากกว่า 0')
    datetime.strptime(c['start'], '%Y-%m-%d %H:%M')
    ZoneInfo(c['timezone'])
    if c['language'] not in ('Thai', 'English'):
        raise ValueError('เลือกภาษา Thai หรือ English')
    if not isinstance(c.get('advanced', {}), dict):
        raise ValueError('Advanced settings ต้องเป็น JSON object')
    if {'model', 'prompt', 'duration', 'resolution', 'aspect_ratio', 'generate_audio'}.intersection(c.get('advanced', {})):
        raise ValueError('Advanced JSON ห้ามทับ model/prompt/duration/resolution/aspect_ratio/generate_audio')
    if c.get('music_enabled', False):
        if not c.get('music_model', '').strip():
            raise ValueError('กรุณาเลือกโมเดล Music จาก OpenRouter')
        volume = float(c.get('music_volume', 15))
        c['music_volume'] = volume
        if not math.isfinite(volume) or not 0 <= volume <= 100:
            raise ValueError('Music volume ต้องอยู่ระหว่าง 0–100%')
        if c.get('music_format', 'wav') not in ('wav', 'mp3'):
            raise ValueError('Music format ต้องเป็น wav หรือ mp3')


def music_models(models):
    """Audio output alone is not proof of music capability; use catalog descriptions."""
    return [m for m in models if any(word in (m.get('id', '') + ' ' + m.get('name', '') + ' ' + m.get('description', '')).lower()
                                    for word in ('music', 'lyria', 'instrumental', 'song generation'))]


def decode_music_stream(lines, stop=None, on_usage=None):
    fragments, finished, event = [], False, []
    def consume(data):
        nonlocal finished
        if data.strip() == '[DONE]':
            finished = True
            return
        chunk = json.loads(data)
        if on_usage and chunk.get('usage') is not None:
            on_usage(chunk)
        if chunk.get('error'):
            raise RuntimeError('Music API: ' + str(chunk['error']))
        choices = chunk.get('choices') or []
        if choices:
            choice = choices[0]
            if choice.get('finish_reason') in ('error', 'length', 'content_filter'):
                raise RuntimeError('Music generation incomplete: ' + choice['finish_reason'])
            audio = (choice.get('delta') or {}).get('audio') or {}
            if audio.get('data'):
                fragments.append(audio['data'])
    for raw in lines:
        if stop and stop.is_set():
            raise InterruptedError('หยุดรับเพลงแล้ว คำขอที่ส่งอาจยังมีค่าใช้จ่าย')
        line = raw.decode('utf-8').rstrip('\r\n') if isinstance(raw, bytes) else raw.rstrip('\r\n')
        if line.startswith('data:'):
            event.append(line[5:].lstrip())
        elif not line and event:
            consume('\n'.join(event))
            event = []
            if finished:
                break
    if event:
        consume('\n'.join(event))
    if not finished or not fragments:
        raise RuntimeError('Music API ส่งเสียงไม่ครบหรือไม่มีเสียง กรุณาตรวจ Activity ก่อนลองใหม่')
    data = base64.b64decode(''.join(fragments), validate=True)
    if not data:
        raise ValueError('Music API ส่งไฟล์เสียงว่าง')
    return data


def episode_settings(c, e):
    result = dict(c)
    result.update(e.get('music_config', {}))
    return result


def music_request_prompt(c, e):
    c = episode_settings(c, e)
    return (f"Create original instrumental background music for a narrated documentary short, approximately {c['seconds']} seconds. "
            'No vocals, lyrics, speech, chanting or spoken words. Keep the arrangement spacious and unobtrusive under narration. '
            'Use a smooth opening, subtle development, and a gentle ending. '
            + e['music_prompt'].strip())


def music_signatures(c, e):
    c = episode_settings(c, e)
    signature = hashlib.sha256(json.dumps([c['music_model'], c.get('music_format', 'wav'), music_request_prompt(c, e)]).encode()).hexdigest()
    mixed = hashlib.sha256(json.dumps([signature, float(c.get('music_volume', 15)), c.get('music_duck', True)]).encode()).hexdigest()
    return signature, mixed


def export_music_prompts(path, p):
    rows = []
    for e in p['episodes']:
        if e.get('music_prompt'):
            rows.append({'day': e['day'], 'title': e['title'], 'model': episode_settings(p['settings'], e).get('music_model', ''),
                         'summary': e.get('music_summary', ''), 'prompt': e['music_prompt'],
                         'request_prompt': music_request_prompt(p['settings'], e)})
    root = Path(path).parent
    save_json(root / 'music_prompts.json', rows)
    content = '# Background music prompts\n\n'
    for row in rows:
        content += f"## Day {row['day']}: {row['title']}\n\nModel: {row['model']}\n\nSummary: {row['summary']}\n\n### Exact request prompt\n\n{row['request_prompt']}\n\n"
    (root / 'music_prompts.md').write_text(content, encoding='utf-8')


def ensure_music_prompt(path, p, e, router):
    if not e.get('music_prompt'):
        c = episode_settings(p['settings'], e)
        result = router.json_chat(c['text_model'], f'''Design instrumental background music for this documentary.
Title: {e['title']}. Subject: {e.get('subject', '')}. Duration: {c['seconds']} seconds.
Narration: {json.dumps([s['narration'] for s in e.get('scenes', [])], ensure_ascii=False)}
User music direction: {c.get('music_style', 'Choose music appropriate to the story')}.
Return {{"music_prompt":"English music generation prompt specifying mood, genre, instruments, approximate BPM, development and ending; instrumental only, no vocals; unobtrusive under narration; no named artists or existing songs",
"music_summary":"Concise Thai explanation of mood, instruments and why this fits the story"}}.''')
        if not isinstance(result.get('music_prompt'), str) or not result['music_prompt'].strip():
            raise ValueError('AI ไม่ได้ส่ง music_prompt กรุณาลองเขียน prompt ที่เหลืออีกครั้ง')
        e['music_prompt'] = result['music_prompt'].strip()
        e['music_summary'] = str(result.get('music_summary', ''))
        save_json(path, p)
    export_music_prompts(path, p)


def validate_video(c, model):
    for field, supported in [('duration', 'supported_durations'), ('resolution', 'supported_resolutions'), ('aspect_ratio', 'supported_aspect_ratios')]:
        options = model.get(supported, [])
        if options and c[field] not in options:
            raise ValueError(f'{field}={c[field]} ไม่รองรับ; เลือกจาก {options}')


def speech_parameters(model):
    for output in model.get('output_modalities', []):
        if isinstance(output, dict) and output.get('type') == 'speech':
            return output.get('supported_parameters', {})
    return {}


def output_size(c):
    short = {'480p': 480, '720p': 720, '768p': 768, '1080p': 1080, '1K': 1024, '2K': 1440, '4K': 2160}.get(c['resolution'], 720)
    a, b = map(int, c['aspect_ratio'].split(':'))
    if min(a, b) <= 0:
        raise ValueError('aspect ratio ไม่ถูกต้อง')
    return (round(short*a/min(a, b)/2)*2, round(short*b/min(a, b)/2)*2)


def create_plan(c, root, router, log=print, stop=None, project_path=None, write_prompts=True):
    validate_settings(c)
    path = Path(project_path).resolve() if project_path else Path(root).resolve() / 'project.json'
    if path.exists() and (not project_path or read_json(path).get('episodes')):
        raise ValueError('มี project.json แล้ว กรุณาเปิดโปรเจกต์เดิมหรือเลือกโฟลเดอร์ใหม่')
    if hasattr(router, 'ledger'):
        router.day = None
    emit_progress(router, 0)
    log('กำลังคิดหัวข้อซีรีส์...')
    result = router.json_chat(c['text_model'], f'''Create exactly {c['days']} distinct documentary episodes.
Series: {c['series']}. Idea: {c['idea']}. Language: {c['language']}. Each lasts {c['seconds']} seconds.
Return {{"episodes":[{{"subject":"specific unique subject","title":"title"}}]}}. No repeated subjects.''')
    episodes = result.get('episodes', [])
    if len(episodes) != int(c['days']) or any(not isinstance(e, dict) or not e.get('subject') or not e.get('title') for e in episodes):
        raise ValueError('AI ส่งหัวข้อไม่ครบหรือรูปแบบไม่ถูกต้อง')
    if len({e['subject'].strip().casefold() for e in episodes}) != len(episodes):
        raise ValueError('AI ส่งหัวข้อซ้ำ กรุณาลองใหม่')
    start = datetime.strptime(c['start'], '%Y-%m-%d %H:%M')
    for i, e in enumerate(episodes):
        e.update(day=i+1, publish_at=(start+timedelta(days=i)).strftime('%Y-%m-%d %H:%M'))
    save_json(path, {'version': 1, 'settings': c, 'episodes': episodes})
    if write_prompts:
        complete_prompts(path, router, log, stop)
    emit_progress(router, 100)
    return path


def complete_prompts(path, router, log=print, stop=None, only_day=None):
    p = read_json(path)
    c = p['settings']
    count = math.ceil(int(c['seconds']) / int(c['duration']))
    selected = [e for e in p['episodes'] if only_day is None or e['day'] == only_day]
    done = 0
    emit_progress(router, 0)
    for e in p['episodes']:
        if only_day is not None and e['day'] != only_day:
            continue
        if hasattr(router, 'ledger'):
            router.day = e['day']
        if stop and stop.is_set():
            return
        if e.get('scenes'):
            if episode_settings(c, e).get('music_enabled', False):
                ensure_music_prompt(path, p, e, router)
            done += 1
            emit_progress(router, 100*done/max(1, len(selected)))
            continue
        log(f"Day {e['day']}: เขียนบท {e['subject']}")
        result = router.json_chat(c['text_model'], f'''Write a {c['seconds']}-second short documentary about {e['subject']}.
Series {c['series']}; idea {c['idea']}; style {c.get('style', 'cinematic documentary')}.
Exactly {count} scenes. Narration MUST be in {c['language']}. Visual prompts in English with consistent historical appearance.
Budget {max(3, int(int(c['seconds'])*1.7/count))} spoken words per scene (Thai: equivalent speaking time).
Careful factual claims, no fake quotes. Visuals are dramatizations, without speech, captions or text.
Return {{"description":"YouTube description with #Shorts and AI dramatization disclosure","tags":["history"],
"fact_check_notes":"claims to verify before publishing", "scenes":[{{"visual_prompt":"detailed setting, camera, subject, movement","narration":"spoken words"}}]}}.''')
        scenes = result.get('scenes', [])
        if len(scenes) != count or any(not isinstance(s, dict) or not s.get('visual_prompt') or not s.get('narration') for s in scenes):
            raise ValueError(f"Day {e['day']}: AI ส่งฉากไม่ครบ เลือก EP นี้แล้วกดเขียนบทตอนนี้เพื่อลองใหม่")
        for key in ('description', 'tags', 'fact_check_notes', 'scenes'):
            e[key] = result.get(key, [] if key == 'tags' else '')
        save_json(path, p)
        log(f"Day {e['day']}: บันทึกบทแล้ว ({len(scenes)} scenes)")
        if episode_settings(c, e).get('music_enabled', False):
            ensure_music_prompt(path, p, e, router)
        done += 1
        emit_progress(router, 100*done/max(1, len(selected)))
    log('บันทึก prompt แล้ว เปิดแก้ project.json ได้ก่อนสร้างวิดีโอ')


def run_ffmpeg(args, cwd=None):
    result = subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y', *map(str, args)], cwd=cwd, capture_output=True, text=True, encoding='utf-8', errors='replace', creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if result.returncode:
        raise RuntimeError('FFmpeg: ' + result.stderr[-2500:])


def media_duration(path):
    result = subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'default=noprint_wrappers=1:nokey=1', str(path)], capture_output=True, text=True, check=True, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    return float(result.stdout.strip())


def ensure_thumbnail(video):
    """Small PNG works with Tk PhotoImage without an extra Pillow dependency."""
    video = Path(video)
    thumbnail = video.with_name(video.stem + '_thumbnail.png')
    if not thumbnail.exists() or thumbnail.stat().st_mtime_ns < video.stat().st_mtime_ns:
        position = min(1.0, media_duration(video) / 4)
        temp = thumbnail.with_name(thumbnail.stem + '.tmp.png')
        run_ffmpeg(['-ss', position, '-i', video, '-frames:v', '1',
                    '-vf', 'scale=192:256:force_original_aspect_ratio=decrease,pad=192:256:(ow-iw)/2:(oh-ih)/2',
                    '-update', '1', temp])
        temp.replace(thumbnail)
    return thumbnail


def timestamp(seconds):
    ms = round(seconds * 1000)
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f'{h:02}:{m:02}:{s:02},{ms:03}'


def mix_music(narration, music, destination, volume=15, duck=True):
    duration = media_duration(narration)
    fade = min(1.5, duration / 4)
    bed = (f'[1:a]atrim=duration={duration},asetpts=PTS-STARTPTS,'
           f'loudnorm=I=-16:TP=-2:LRA=7,volume={float(volume)/100},'
           f'afade=t=in:st=0:d={fade},afade=t=out:st={max(0, duration-fade)}:d={fade}[bed];')
    if duck:
        graph = ('[0:a]loudnorm=I=-16:TP=-1.5:LRA=7,asplit=2[voice][key];' + bed +
                 '[bed][key]sidechaincompress=threshold=0.1:ratio=3:attack=30:release=450[quiet];'
                 '[voice][quiet]amix=inputs=2:duration=first:normalize=0,alimiter=limit=0.95:level=0[out]')
    else:
        graph = ('[0:a]loudnorm=I=-16:TP=-1.5:LRA=7[voice];' + bed +
                 '[voice][bed]amix=inputs=2:duration=first:normalize=0,alimiter=limit=0.95:level=0[out]')
    run_ffmpeg(['-i', narration, '-stream_loop', '-1', '-i', music, '-filter_complex', graph,
                '-map', '0:v:0', '-map', '[out]', '-c:v', 'copy', '-c:a', 'aac', '-ar', '48000', '-ac', '2',
                '-t', duration, '-movflags', '+faststart', destination])


def generate_music_asset(path, p, e, router, log=print, stop=None):
    if hasattr(router, 'ledger'):
        router.day = e['day']
    c = episode_settings(p['settings'], e)
    folder = Path(path).parent / f"day_{e['day']:02}"
    folder.mkdir(parents=True, exist_ok=True)
    if not c.get('music_enabled', False):
        raise ValueError('กรุณาเปิดเพลงสำหรับตอนนี้ก่อน')
    ensure_music_prompt(path, p, e, router)
    prompt = music_request_prompt(c, e)
    fmt = c.get('music_format', 'wav')
    signature, mix_signature = music_signatures(c, e)
    if c.get('music_review_required') and e.get('music_approved') != signature:
        raise ValueError('กรุณาตรวจ Prompt เพลง แล้วกดสร้างเพลงจาก Prompt นี้ ก่อนสร้างคลิป')
    music = folder / f'background_{signature[:12]}.{fmt}'
    if not music.exists():
        if e.get('music_submission_pending'):
            raise RuntimeError('คำขอเพลงก่อนหน้าไม่ทราบผล ตรวจ OpenRouter Activity แล้วใช้ปุ่มล้างสถานะเพลงค้างในตอนนี้ก่อนลองใหม่')
        if stop and stop.is_set():
            raise InterruptedError('หยุดก่อนส่งคำขอเพลง')
        log(f"Day {e['day']}: สร้างเพลง {c['music_model']}")
        e['music_submission_pending'] = True
        save_json(path, p)
        try:
            data = router.music(c['music_model'], prompt, fmt, stop)
        except (RouterHTTPError, RetryCancelled) as error:
            if error.rejected:
                e.pop('music_submission_pending', None)
                save_json(path, p)
            raise
        temp = music.with_name('music_download.' + fmt)
        temp.write_bytes(data)
        if media_duration(temp) <= 0:
            raise ValueError('Music API ส่งไฟล์เพลงไม่ถูกต้อง')
        temp.replace(music)
        e.pop('music_submission_pending', None)
        save_json(path, p)
    elif e.pop('music_submission_pending', None):
        save_json(path, p)
    (folder / 'music_prompt.txt').write_text(prompt, encoding='utf-8')
    e.update(music_file=str(music), music_model_used=c['music_model'])
    save_json(path, p)
    return music


def finish_episode_music(path, p, e, router, log=print, stop=None):
    c = episode_settings(p['settings'], e)
    folder = Path(path).parent / f"day_{e['day']:02}"
    final, narration = folder / 'short.mp4', folder / 'narration.mp4'
    if not c.get('music_enabled', False):
        if e.get('music_mix') and narration.exists():
            shutil.copyfile(narration, final)
            e.pop('music_mix', None)
            save_json(path, p)
        return
    music = generate_music_asset(path, p, e, router, log, stop)
    _, mix_signature = music_signatures(c, e)
    if e.get('music_mix') == mix_signature and final.exists():
        return
    if not narration.exists():
        # Also allows adding music to an existing narration-only project.
        shutil.copyfile(final, narration)
    log(f"Day {e['day']}: ผสมเพลง {c.get('music_volume', 15)}% / duck={c.get('music_duck', True)}")
    temp = folder / 'mixing.mp4'
    mix_music(narration, music, temp, c.get('music_volume', 15), c.get('music_duck', True))
    temp.replace(final)
    e.update(music_mix=mix_signature, music_file=str(music), music_model_used=c['music_model'])
    save_json(path, p)


def emit_progress(router, percent):
    callback = getattr(router, 'progress', None)
    if callable(callback):
        callback(max(0, min(100, percent)))


def download_video(path, document, record, raw, payload, router, log=print, stop=None):
    """Replace an unfinished job after 429, retaining its ID in history; never touch completed files."""
    restarts = 0
    while not raw.is_file():
        if stop and stop.is_set():
            raise InterruptedError('หยุดสร้างซีนแล้ว')
        if not record.get('job_id'):
            if record.get('submission_pending'):
                raise RuntimeError('คำขอก่อนหน้าไม่ทราบผล กรุณาตรวจ OpenRouter Activity ก่อนส่งสร้างซ้ำ')
            record['submission_pending'] = True
            save_json(path, document)
            try:
                job = router.request('/videos', payload)
            except (RouterHTTPError, RetryCancelled) as error:
                if error.rejected:
                    record.pop('submission_pending', None)
                    save_json(path, document)
                raise
            record.update(job_id=job['id'], generated_visual_prompt=payload['prompt'])
            record.pop('submission_pending', None)
            save_json(path, document)
            log('เริ่ม job ใหม่: ' + job['id'])
        deadline = time.monotonic() + 3600
        try:
            while True:
                if stop and stop.is_set():
                    raise InterruptedError('หยุดแล้ว บันทึก job ID ไว้')
                job = router.request('/videos/' + quote(record['job_id'], safe=''))
                status = job.get('status')
                if status == 'completed':
                    data = router.request('/videos/' + quote(record['job_id'], safe='') + '/content?index=0', binary=True)
                    temp = raw.with_suffix('.part.mp4')
                    temp.write_bytes(data)
                    if media_duration(temp) <= 0:
                        raise ValueError('ไฟล์วิดีโอที่ดาวน์โหลดไม่ถูกต้อง')
                    temp.replace(raw)
                    return raw
                if status in ('failed', 'cancelled', 'expired'):
                    raise RuntimeError(f"Video job {record['job_id']}: {status}; {job.get('error', '')}")
                if time.monotonic() > deadline:
                    raise TimeoutError('รอซีนเกิน 1 ชั่วโมง เก็บ job ID ไว้แล้ว')
                if stop:
                    stop.wait(10)
                else:
                    time.sleep(10)
        except RouterHTTPError as error:
            if error.status != 429:
                raise
            old_job = record.pop('job_id')
            record.setdefault('abandoned_jobs', []).append({'job_id': old_job, 'reason': 'HTTP 429', 'time': datetime.now().isoformat()})
            record.pop('submission_pending', None)
            save_json(path, document)
            if restarts >= 2:
                raise RuntimeError('ซีนนี้เจอ 429 ซ้ำ หยุดสร้างใหม่หลังเปลี่ยน job 2 ครั้งแล้ว กรุณารอสักครู่ก่อนกดสร้างต่อ') from error
            delay = error.delay(restarts)
            log(f'HTTP 429 • นำ job เดิมออก: {old_job} • รอ {delay:.1f} วินาทีก่อนสร้างซีนใหม่ด้วย job ID ใหม่')
            event = stop if stop is not None else getattr(router, 'stop', None)
            if event is not None:
                if event.wait(delay):
                    raise InterruptedError('หยุดระหว่างรอสร้าง job ใหม่แล้ว')
            else:
                time.sleep(delay)
            restarts += 1
    return raw


def needs_render_resume(path, episode):
    if not episode:
        return False
    if episode.get('render_incomplete') or episode.get('final_stale'):
        return True
    if episode.get('file_path') and Path(episode['file_path']).is_file():
        return False
    folder = Path(path).parent / f"day_{episode['day']:02}"
    return any(scene.get('job_id') or scene.get('submission_pending') or scene.get('file_path')
               or (folder / f'{index:02}.mp4').is_file()
               for index, scene in enumerate(episode.get('scenes', []), 1))


def generate(path, router, log=print, stop=None, only_day=None):
    from media_versions import current_audio, archive_final, snapshot
    if isinstance(router, Router):
        router.restart_video_on_429 = True
    path = Path(path).resolve()
    p = read_json(path)
    for episode in p['episodes']:
        if (only_day is None or episode['day'] == only_day) and episode.get('scenes'):
            episode['render_incomplete'] = True
    save_json(path, p)
    targets = [e for e in p['episodes'] if only_day is None or e['day'] == only_day]
    total_units = sum(3*len(e.get('scenes', [])) + 1 for e in targets)
    completed_units = 0
    def advance(units=1):
        nonlocal completed_units
        completed_units += units
        emit_progress(router, 100*completed_units/max(1, total_units))
    emit_progress(router, 0)
    c = p['settings']
    validate_settings(c)
    for executable in ('ffmpeg', 'ffprobe'):
        if not shutil.which(executable):
            raise ValueError(f'ไม่พบ {executable} กรุณาติดตั้ง FFmpeg และเพิ่มใน PATH')
    models = router.request('/videos/models')['data']
    model = next((m for m in models if m['id'] == c['video_model']), None)
    if not model:
        raise ValueError('ไม่พบ video model ในรายการปัจจุบัน')
    validate_video(c, model)
    music_episodes = [e for e in p['episodes'] if (not only_day or e['day'] == only_day) and episode_settings(c, e).get('music_enabled', False)]
    if music_episodes:
        available_music = music_models(router.request('/models?output_modalities=audio')['data'])
        for e in music_episodes:
            ec = episode_settings(c, e)
            validate_settings(ec)
            if not any(m['id'] == ec['music_model'] for m in available_music):
                raise ValueError('ไม่พบ music model ที่เลือกในรายการ Audio/Music ปัจจุบัน')
            if ec.get('music_review_required') and (not e.get('music_prompt') or e.get('music_approved') != music_signatures(c, e)[0]):
                raise ValueError(f"ตอน {e['day']}: ตรวจ Prompt เพลงและกดสร้างเพลงก่อนสร้างคลิป")
    speech = router.request('/audio/speech/models')['data']
    tts = next((m for m in speech if m['id'] == c['tts_model']), None)
    if tts:
        formats = speech_parameters(tts).get('response_format', {}).get('values', [])
        if formats and 'mp3' not in formats:
            raise ValueError('โมเดล TTS นี้ไม่รองรับ MP3 กรุณาเลือกโมเดลอื่นก่อนสร้างวิดีโอ')
    for e in p['episodes']:
        if hasattr(router, 'ledger'):
            router.day = e['day']
        if only_day and e['day'] != only_day:
            continue
        if stop and stop.is_set():
            return
        folder = path.parent / f"day_{e['day']:02}"
        folder.mkdir(exist_ok=True)
        final = folder / 'short.mp4'
        if e.get('file_path') and final.exists() and not e.get('final_stale'):
            archive_final(path, p, e)
            finish_episode_music(path, p, e, router, log, stop)
            e['thumbnail_path'] = str(ensure_thumbnail(final))
            archive_final(path, p, e, final)
            e.pop('render_incomplete', None)
            advance(3*len(e.get('scenes', [])) + 1)
            save_json(path, p)
            log(f"Day {e['day']}: ใช้วิดีโอเดิมเรียบร้อย")
            continue
        if not e.get('scenes'):
            raise ValueError('กรุณาสร้าง prompt ให้ครบก่อน')
        rendered, subtitles, elapsed = [], [], 0.0
        for i, s in enumerate(e['scenes'], 1):
            if stop and stop.is_set():
                return
            log(f"Day {e['day']} / Scene {i}: กำลังสร้าง")
            raw, voice, clip = (folder / f'{i:02}.{suffix}' for suffix in ('mp4', 'mp3', 'render.mp4'))
            raw = Path(s.get('raw_video_file') or raw)
            if raw.is_file():
                log(f"Day {e['day']} / Scene {i}: ใช้วิดีโอเดิม ไม่ส่งสร้างวิดีโอซ้ำ")
            elif s.get('job_id'):
                log(f"Day {e['day']} / Scene {i}: ตรวจสถานะ job เดิมต่อ")
            if not raw.exists():
                payload = dict(c.get('advanced', {}), model=c['video_model'], prompt=s['visual_prompt'], duration=int(c['duration']), resolution=c['resolution'], aspect_ratio=c['aspect_ratio'], generate_audio=False)
                download_video(path, p, s, raw, payload, router, log, stop)
            advance()
            voice = current_audio(path, p, e, s, i, router, log)
            advance()
            duration = max(float(c['seconds']) / len(e['scenes']), media_duration(voice) + .15)
            width, height = output_size(c)
            if clip.is_file() and not s.get('video_history'):
                snapshot(s, 'video_history', clip, folder / 'versions', f'scene_{i:02}')
                save_json(path, p)
            run_ffmpeg(['-i', raw, '-i', voice, '-map', '0:v:0', '-map', '1:a:0', '-vf', f'scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height},setsar=1,fps=30,tpad=stop_mode=clone:stop_duration={duration}', '-af', 'apad', '-t', duration, '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-ar', '48000', '-ac', '2', clip])
            advance()
            rendered.append(clip)
            version = snapshot(s, 'video_history', clip, folder / 'versions', f'scene_{i:02}', audio_file=str(voice))
            s['file_path'] = version['file_path']
            save_json(path, p)
            log(f"Day {e['day']} / Scene {i}: พร้อมเล่น")
            subtitles.append(f'{i}\n{timestamp(elapsed)} --> {timestamp(elapsed+duration)}\n{s["narration"]}\n')
            elapsed += duration
        if elapsed > 180:
            raise ValueError('เสียงพากย์เกิน 180 วินาที กรุณาย่อบทแล้วลบไฟล์เสียงเพื่อสร้างใหม่')
        listing = folder / 'concat.txt'
        listing.write_text('\n'.join("file '" + r.name + "'" for r in rendered), encoding='utf-8')
        temp = folder / 'finalizing.mp4'
        run_ffmpeg(['-f', 'concat', '-safe', '0', '-i', listing.name, '-c', 'copy', '-movflags', '+faststart', temp.name], cwd=folder)
        temp.replace(folder / 'narration.mp4')
        shutil.copyfile(folder / 'narration.mp4', final)
        (folder / 'captions.srt').write_text('\n'.join(subtitles), encoding='utf-8')
        e.update(file_path=str(final), actual_seconds=round(elapsed, 2))
        save_json(path, p)
        finish_episode_music(path, p, e, router, log, stop)
        e['thumbnail_path'] = str(ensure_thumbnail(final))
        archive_final(path, p, e, final)
        e.pop('render_incomplete', None)
        e.pop('final_stale', None)
        advance()
        save_json(path, p)
        log(f"Day {e['day']}: เสร็จ {elapsed:.1f} วินาที (SRT แยก)")


def app_command():
    if getattr(sys, 'frozen', False):
        return [sys.executable]
    if INSTALLED:
        return [sys.executable, '-I', str(RESOURCE_ROOT / 'launch_studio.py')]
    return [sys.executable, str(RESOURCE_ROOT / 'shorts_studio.py')]


def export_upload(path, oauth, token):
    path = Path(path).resolve()
    p = read_json(path)
    if not Path(oauth).is_file():
        raise ValueError('กรุณาเลือก OAuth client JSON ที่มีอยู่จริง')
    items = []
    for e in p['episodes']:
        if not e.get('file_path') or not Path(e['file_path']).is_file():
            raise ValueError(f"Day {e['day']} ยังไม่มีคลิป กรุณาสร้างให้ครบก่อน export")
        if episode_settings(p['settings'], e).get('music_enabled', False):
            if not e.get('music_prompt') or e.get('music_mix') != music_signatures(p['settings'], e)[1]:
                raise ValueError(f"Day {e['day']} ยังไม่ได้ผสมเพลงตามค่าปัจจุบัน กดสร้างคลิปอีกครั้งก่อน export")
        items.append({'day': e['day'], 'file_path': e['file_path'], 'title': e['title'], 'description': e.get('description', ''), 'tags': e.get('tags', []), 'category_id': '27', 'privacy_status': 'private', 'publish_at': e['publish_at'], 'made_for_kids': False, 'contains_synthetic_media': True})
    manifest = path.parent / 'upload_manifest.json'
    save_json(manifest, {'oauth': str(Path(oauth).resolve()), 'token': str(Path(token).resolve()), 'timezone': p['settings']['timezone'], 'items': items})
    command = app_command()
    arguments = subprocess.list2cmdline(command[1:] + ['upload', '--manifest', str(manifest)])
    xml = f'''<?xml version="1.0" encoding="UTF-8"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
<Triggers><CalendarTrigger><Repetition><Interval>PT1H</Interval><Duration>P1D</Duration><StopAtDurationEnd>false</StopAtDurationEnd></Repetition><StartBoundary>{datetime.now().strftime('%Y-%m-%dT%H:%M:%S')}</StartBoundary><Enabled>true</Enabled><ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay></CalendarTrigger></Triggers>
<Principals><Principal id="Author"><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>
<Settings><MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy><DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries><StopIfGoingOnBatteries>false</StopIfGoingOnBatteries><StartWhenAvailable>true</StartWhenAvailable><ExecutionTimeLimit>PT4H</ExecutionTimeLimit></Settings>
<Actions Context="Author"><Exec><Command>{escape(command[0])}</Command><Arguments>{escape(arguments)}</Arguments><WorkingDirectory>{escape(str(RESOURCE_ROOT))}</WorkingDirectory></Exec></Actions></Task>'''
    (path.parent / 'upload_task.xml').write_text(xml, encoding='utf-8')
    launcher = f'''from pathlib import Path
import subprocess
import sys
raise SystemExit(subprocess.call({command!r} + ['upload', '--manifest', str(Path(__file__).with_name('upload_manifest.json'))]))
'''
    (path.parent / 'run_upload.py').write_text(launcher, encoding='utf-8')
    return manifest


def upload_manifest(path, authenticate_only=False):
    import upload_auto as uploader
    path = Path(path).resolve()
    m = read_json(path)
    lock = path.with_suffix('.lock')
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        raise RuntimeError('มี upload runner ทำงานอยู่ หรือ lock ค้าง ตรวจสอบก่อนลบ .lock')
    try:
        os.close(fd)
        if not authenticate_only and not Path(m['token']).exists():
            raise RuntimeError('ต้อง Login YouTube ก่อนตั้งเวลา')
        youtube = uploader.get_authenticated_service(m['oauth'], m['token'], allow_interactive=authenticate_only)
        if youtube is None:
            raise RuntimeError('YouTube authentication failed')
        if authenticate_only:
            return
        receipt_path = path.with_name('upload_receipts.json')
        receipts = read_json(receipt_path) if receipt_path.exists() else {}
        failures = []
        for item in m['items']:
            key = str(item['day'])
            if key in receipts:
                if receipts[key].get('state') != 'uploaded':
                    failures.append(f'Day {key}: previous attempt needs manual review')
                continue
            schedule = datetime.strptime(item['publish_at'], '%Y-%m-%d %H:%M').replace(tzinfo=ZoneInfo(m['timezone']))
            if schedule <= datetime.now(ZoneInfo(m['timezone'])) + timedelta(minutes=15):
                failures.append(f'Day {key}: schedule must be at least 15 minutes in the future')
                continue
            if not Path(item['file_path']).is_file():
                failures.append(f'Day {key}: missing video')
                continue
            receipts[key] = {'state': 'uploading', 'started': datetime.now().isoformat()}
            save_json(receipt_path, receipts)
            video_id = uploader.upload_single_video(youtube, item, m['timezone'])
            receipts[key] = {'state': 'uploaded' if video_id else 'needs_review', 'video_id': video_id}
            save_json(receipt_path, receipts)
            if not video_id:
                failures.append(f'Day {key}: upload failed; review receipts before retry')
        if failures:
            raise RuntimeError('\n'.join(failures))
    finally:
        lock.unlink(missing_ok=True)
