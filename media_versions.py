"""Immutable audio and clip versions; remix existing assets without video API calls."""
from datetime import datetime, timezone
import hashlib
from pathlib import Path
import shutil
import shorts_core as core


def episode_folder(path, episode):
    folder = Path(path).parent / f"day_{episode['day']:02}"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def next_path(folder, prefix, suffix):
    folder.mkdir(parents=True, exist_ok=True)
    versions = [int(p.stem.split('_v')[-1]) for p in folder.glob(prefix + '_v*' + suffix)
                if p.stem.split('_v')[-1].isdigit()]
    number = max(versions, default=0) + 1
    return folder / f'{prefix}_v{number:03}{suffix}', number


def snapshot(owner, key, source, folder, prefix, force=False, **metadata):
    source = Path(source)
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    history = owner.setdefault(key, [])
    if not force and history and history[-1].get('sha256') == digest and Path(history[-1]['file_path']).is_file():
        return history[-1]
    destination, version = next_path(folder, prefix, source.suffix)
    with destination.open('xb') as stream, source.open('rb') as incoming:
        shutil.copyfileobj(incoming, stream)
    record = dict(version=version, file_path=str(destination), sha256=digest,
                  created_at=datetime.now(timezone.utc).isoformat(), **metadata)
    history.append(record)
    return record


def archive_audio(path, document, episode, scene, index):
    folder = episode_folder(path, episode)
    source = Path(scene.get('audio_file') or folder / f'{index:02}.mp3')
    if source.is_file():
        record = snapshot(scene, 'audio_history', source, folder / 'audio', f'scene_{index:02}',
                          model=document['settings'].get('tts_model', ''), voice=document['settings'].get('voice', ''),
                          narration=scene['narration'])
        scene['audio_file'] = record['file_path']
        core.save_json(path, document)
        return Path(record['file_path'])
    return None


def create_audio(path, document, episode, scene, index, router, log=print):
    archive_audio(path, document, episode, scene, index)
    if hasattr(router, 'ledger'):
        router.day = episode['day']
    c = document['settings']
    if not c.get('tts_model') or not c.get('voice') or not scene.get('narration', '').strip():
        raise ValueError('กรุณาเลือกโมเดลเสียง, Voice ID และกรอกบทพากย์ก่อน')
    log(f"EP {episode['day']:02} / Scene {index:02}: สร้างเสียงเวอร์ชันใหม่")
    data = router.request('/audio/speech', {'model': c['tts_model'], 'input': scene['narration'],
                                          'voice': c['voice'], 'response_format': 'mp3'}, binary=True)
    if not data:
        raise ValueError('API ส่งเสียงว่างกลับมา')
    folder = episode_folder(path, episode)
    destination, version = next_path(folder / 'audio', f'scene_{index:02}', '.mp3')
    with destination.open('xb') as stream:
        stream.write(data)
    # Do not select a failed/invalid audio generation as the latest usable take.
    if core.media_duration(destination) <= 0:
        raise ValueError('ไฟล์เสียงใหม่ไม่ถูกต้อง')
    scene.setdefault('audio_history', []).append(dict(version=version, file_path=str(destination),
        created_at=datetime.now(timezone.utc).isoformat(), model=c['tts_model'], voice=c['voice'],
        narration=scene['narration'], sha256=hashlib.sha256(data).hexdigest()))
    scene['audio_file'] = str(destination)
    episode['final_stale'] = True
    core.save_json(path, document)
    log(f'เสียง v{version:03} พร้อมใช้งาน')
    return destination


def current_audio(path, document, episode, scene, index, router, log=print, force_legacy=False):
    known = bool(scene.get('audio_history'))
    voice = archive_audio(path, document, episode, scene, index)
    take = next((r for r in reversed(scene.get('audio_history', [])) if r.get('file_path') == str(voice)), {})
    settings = document['settings']
    changed = any(take.get(k) != value for k, value in (
        ('narration', scene['narration']), ('model', settings['tts_model']), ('voice', settings['voice'])))
    if voice is None or changed or (force_legacy and not known):
        return create_audio(path, document, episode, scene, index, router, log)
    return voice


def archive_final(path, document, episode, source=None, force=False):
    folder = episode_folder(path, episode)
    source = Path(source or episode.get('file_path') or folder / 'short.mp4')
    if source.is_file():
        record = snapshot(episode, 'video_history', source, folder / 'versions', 'final',
                          force=force,
                          audio_files=[s.get('audio_file', '') for s in episode.get('scenes', [])],
                          music_file=episode.get('music_file', ''))
        episode.update(file_path=record['file_path'], video_version=record['version'])
        core.save_json(path, document)
        return Path(record['file_path'])


def render_scene(path, document, episode, scene, index):
    folder = episode_folder(path, episode)
    raw = Path(scene.get('raw_video_file') or folder / f'{index:02}.mp4')
    voice = archive_audio(path, document, episode, scene, index)
    if not raw.is_file() or voice is None:
        raise ValueError(f'Scene {index}: ต้องมีวิดีโอต้นฉบับและเสียงก่อนรวมคลิป')
    take = next((r for r in reversed(scene.get('audio_history', [])) if r.get('file_path') == str(voice)), {})
    if take.get('narration') != scene['narration']:
        raise ValueError(f'Scene {index}: บทพากย์เปลี่ยนแล้ว กรุณากดสร้างเสียงใหม่ก่อนรวมคลิป')
    c = document['settings']
    duration = max(float(c['seconds']) / len(episode['scenes']), core.media_duration(voice) + .15)
    width, height = core.output_size(c)
    previous = Path(scene.get('file_path') or folder / f'{index:02}.render.mp4')
    if previous.is_file() and not scene.get('video_history'):
        snapshot(scene, 'video_history', previous, folder / 'versions', f'scene_{index:02}')
    destination, version = next_path(folder / 'versions', f'scene_{index:02}', '.mp4')
    temp = destination.with_name(destination.stem + '.pending.mp4')
    core.run_ffmpeg(['-i', raw, '-i', voice, '-map', '0:v:0', '-map', '1:a:0',
        '-vf', f'scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height},setsar=1,fps=30,tpad=stop_mode=clone:stop_duration={duration}',
        '-af', 'apad', '-t', duration, '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-c:a', 'aac',
        '-ar', '48000', '-ac', '2', '-movflags', '+faststart', temp])
    if core.media_duration(temp) <= 0:
        raise ValueError('คลิปที่รวมใหม่ไม่ถูกต้อง')
    temp.replace(destination)
    scene.setdefault('video_history', []).append(dict(version=version, file_path=str(destination),
        audio_file=str(voice), narration=next((r.get('narration', scene['narration']) for r in scene.get('audio_history', []) if r['file_path'] == str(voice)), scene['narration']),
        created_at=datetime.now(timezone.utc).isoformat()))
    scene['file_path'] = str(destination)
    core.save_json(path, document)
    return destination, duration


def latest_music(path, episode):
    """Only search known music assets for this episode, never narration takes."""
    folder = episode_folder(path, episode)
    candidates = {Path(episode['music_file'])} if episode.get('music_file') else set()
    candidates.update(Path(r['file_path']) for r in episode.get('music_history', []) if r.get('file_path'))
    candidates.update(p for p in folder.glob('background_*.*') if p.suffix.lower() in ('.wav', '.mp3', '.m4a', '.aac', '.flac', '.ogg'))
    candidates = [p for p in candidates if p.is_file() and p.stat().st_size > 0]
    if not candidates:
        raise ValueError('ยังไม่มีเพลงเบื้องหลังของ EP นี้ กรุณาสร้างเพลงในแท็บ Music ก่อนรวมคลิป')
    music = max(candidates, key=lambda p: p.stat().st_mtime_ns)
    if core.media_duration(music) <= 0:
        raise ValueError('ไฟล์เพลงล่าสุดไม่ถูกต้อง กรุณาสร้างเพลงใหม่')
    return music


def remix(path, day, scene_index=None, log=print, stop=None, include_music=None):
    document = core.read_json(path)
    episode = next(e for e in document['episodes'] if e['day'] == day)
    folder = episode_folder(path, episode)
    c = core.episode_settings(document['settings'], episode)
    use_music = bool(c.get('music_enabled')) if include_music is None else include_music
    music = latest_music(path, episode) if use_music else None
    if use_music:
        volume = float(c.get('music_volume', 15))
        if not 0 < volume <= 100:
            raise ValueError('ความดังเพลงต้องมากกว่า 0 และไม่เกิน 100% ปรับค่าในแท็บ Music ก่อนรวม')
        log(f'เพลงเบื้องหลังล่าสุด: {music.name} • {volume:g}% • duck={c.get("music_duck", True)}')
    else:
        log('รวมเฉพาะเสียงพากย์: ปิดเพลงเบื้องหลังอยู่')
    if scene_index is not None:
        scene = episode['scenes'][scene_index-1]
        output, _ = render_scene(path, document, episode, scene, scene_index)
        if use_music:
            destination, version = next_path(folder / 'versions', f'scene_{scene_index:02}', '.mp4')
            temp = destination.with_name(destination.stem + '.pending.mp4')
            core.mix_music(output, music, temp, volume, c.get('music_duck', True))
            temp.replace(destination)
            scene.setdefault('video_history', []).append(dict(version=version, file_path=str(destination),
                audio_file=scene.get('audio_file', ''), music_file=str(music), music_volume=volume,
                created_at=datetime.now(timezone.utc).isoformat()))
            scene['file_path'] = str(destination)
            episode['music_file'] = str(music)
            if include_music is True:
                episode.setdefault('music_config', {})['music_enabled'] = True
            core.save_json(path, document)
            output = destination
        log(f'รวมฉากแล้ว: {output.name}')
        return output
    # Preserve the previously selected final before replacing any working output.
    archive_final(path, document, episode)
    clips, subtitles, elapsed = [], [], 0.0
    for index, scene in enumerate(episode.get('scenes', []), 1):
        if stop and stop.is_set():
            raise InterruptedError('หยุดรวมคลิปแล้ว เวอร์ชันเดิมยังอยู่')
        log(f"EP {day:02} / Scene {index:02}: รวมวิดีโอกับเสียงล่าสุด")
        clip, duration = render_scene(path, document, episode, scene, index)
        clips.append(clip)
        spoken = scene['video_history'][-1].get('narration', scene['narration'])
        subtitles.append(f'{index}\n{core.timestamp(elapsed)} --> {core.timestamp(elapsed+duration)}\n{spoken}\n')
        elapsed += duration
    if not clips or elapsed > 180:
        raise ValueError('ต้องมีฉาก และความยาวรวมไม่เกิน 180 วินาที')
    listing = folder / 'versions' / 'concat_latest.txt'
    listing.write_text('\n'.join(f"file '{clip.name}'" for clip in clips), encoding='utf-8')
    narration = folder / 'versions' / 'narration_latest.pending.mp4'
    core.run_ffmpeg(['-f', 'concat', '-safe', '0', '-i', listing.name, '-c', 'copy', '-movflags', '+faststart', narration.name], cwd=listing.parent)
    temp = narration
    if use_music:
        temp = folder / 'versions' / 'mixed_latest.pending.mp4'
        core.mix_music(narration, music, temp, c.get('music_volume', 15), c.get('music_duck', True))
        episode['music_file'] = str(music)
        if include_music is True:
            episode.setdefault('music_config', {})['music_enabled'] = True
        # Offline mixes may use imported/cached music without generation metadata.
        episode.pop('music_mix', None)
        if c.get('music_model') and episode.get('music_prompt'):
            signature, mix_signature = core.music_signatures(c, episode)
            if music.stem == f'background_{signature[:12]}':
                episode['music_mix'] = mix_signature
    else:
        episode.pop('music_mix', None)
    output = archive_final(path, document, episode, temp, force=True)
    shutil.copyfile(narration, folder / 'narration.mp4')
    shutil.copyfile(output, folder / 'short.mp4')
    episode['actual_seconds'] = round(elapsed, 2)
    episode.pop('render_incomplete', None)
    episode.pop('final_stale', None)
    (folder / 'captions.srt').write_text('\n'.join(subtitles), encoding='utf-8')
    core.save_json(path, document)
    log(f'Final v{episode["video_version"]:03} พร้อมเล่น: {output.name}')
    return output
