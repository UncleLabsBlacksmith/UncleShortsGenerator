"""Generate or resume one scene, keeping its previous raw/video/audio versions."""
from datetime import datetime, timezone
import copy
from pathlib import Path
import shorts_core as core
import media_versions as media
from router_retry import RouterHTTPError, RetryCancelled


def regenerate(path, day, index, router, log=print, stop=None, force_new=False):
    if isinstance(router, core.Router):
        router.restart_video_on_429 = True
    core.emit_progress(router, 0)
    document = core.read_json(path)
    episode = next(e for e in document['episodes'] if e['day'] == day)
    scene = episode['scenes'][index-1]
    if force_new and (scene.get('submission_pending') or scene.get('pending_video_generation')):
        scene.setdefault('bypassed_submissions', []).append({
            'created_at': datetime.now(timezone.utc).isoformat(),
            'submission_pending': scene.get('submission_pending'),
            'pending_video_generation': copy.deepcopy(scene.get('pending_video_generation'))})
        scene.pop('submission_pending', None)
        scene.pop('pending_video_generation', None)
        core.save_json(path, document)
        log(f'Scene {index:02}: ข้ามคำขอเดิมที่ไม่ทราบผลแล้ว ส่งสร้างซีนใหม่ตามคำสั่งผู้ใช้')
    c = dict(document['settings'])
    core.validate_settings(c)
    if not scene.get('visual_prompt', '').strip() or not scene.get('narration', '').strip():
        raise ValueError('กรุณากรอก Prompt และบทพากย์ก่อนสร้างซีน')
    if hasattr(router, 'ledger'):
        router.day = day
    models = router.request('/videos/models')['data']
    model = next((m for m in models if m['id'] == c['video_model']), None)
    if not model:
        raise ValueError('ไม่พบโมเดลวิดีโอที่เลือก กรุณาอัปเดตรายการโมเดล')
    core.validate_video(c, model)
    folder = media.episode_folder(path, episode)
    attempt = scene.get('pending_video_generation')
    def save():
        if attempt:
            for record in scene.get('video_generations', []):
                if record.get('file_path') == attempt['file_path']:
                    record.update(attempt)
        core.save_json(path, document)
    if attempt and attempt.get('prompt') != scene['visual_prompt']:
        raise ValueError('ซีนนี้มีงานเดิมค้างอยู่ กรุณาใช้ Prompt เดิมเพื่อทำงานนั้นต่อก่อนส่ง Prompt ใหม่')
    if not attempt:
        if scene.get('submission_pending') and not scene.get('job_id'):
            raise ValueError('คำขอเดิมไม่ทราบผล กรุณาตรวจ OpenRouter Activity ก่อนสร้างซีนซ้ำ')
        previous = Path(scene.get('raw_video_file') or folder / f'{index:02}.mp4')
        if previous.is_file() and not scene.get('raw_video_history'):
            media.snapshot(scene, 'raw_video_history', previous, folder / 'raw', f'scene_{index:02}',
                           prompt=scene.get('generated_visual_prompt', ''), job_id=scene.get('job_id'), legacy=True)
        destination, version = media.next_path(folder / 'raw', f'scene_{index:02}', '.mp4')
        version = max(version, 1 + max((r.get('version', 0) for r in scene.get('video_generations', [])), default=0))
        destination = destination.with_name(f'scene_{index:02}_v{version:03}.mp4')
        payload = dict(c.get('advanced', {}), model=c['video_model'], prompt=scene['visual_prompt'],
            duration=int(c['duration']), resolution=c['resolution'], aspect_ratio=c['aspect_ratio'], generate_audio=False)
        attempt = dict(version=version, file_path=str(destination), prompt=scene['visual_prompt'],
                       payload=payload, state='submitting', created_at=datetime.now(timezone.utc).isoformat())
        scene['pending_video_generation'] = attempt
        scene.setdefault('video_generations', []).append(attempt)
        save()
        try:
            if stop and stop.is_set():
                raise RetryCancelled('หยุดก่อนส่งสร้างซีน')
            log(f'EP {day:02} / Scene {index:02}: ส่งสร้างวิดีโอซีนใหม่ v{version:03}')
            job = router.request('/videos', payload)
            attempt.update(job_id=job['id'], state='submitted')
            save()
        except (RouterHTTPError, RetryCancelled) as error:
            if error.rejected:
                attempt['state'] = 'rejected'
                scene.pop('pending_video_generation', None)
                save()
            raise
    if not attempt.get('job_id') and not attempt.get('abandoned_jobs'):
        raise ValueError('การส่งสร้างซีนครั้งก่อนยังไม่ทราบผล กรุณาตรวจ OpenRouter Activity ก่อนส่งซ้ำ')
    destination = Path(attempt['file_path'])
    core.download_video(path, document, attempt, destination, attempt['payload'], router, log, stop)
    core.emit_progress(router, 50)
    attempt['state'] = 'downloaded'
    if not any(r.get('file_path') == str(destination) for r in scene.get('raw_video_history', [])):
        scene.setdefault('raw_video_history', []).append({k: v for k, v in attempt.items() if k != 'payload'})
    scene.update(raw_video_file=str(destination), job_id=attempt['job_id'], generated_visual_prompt=attempt['prompt'])
    episode['final_stale'] = True
    save()
    media.current_audio(path, document, episode, scene, index, router, log, force_legacy=True)
    core.emit_progress(router, 75)
    if stop and stop.is_set():
        raise InterruptedError('เก็บวิดีโอ/เสียงใหม่แล้ว กดทำซีนนี้ต่อเพื่อรวมเสียง')
    output, _ = media.render_scene(path, document, episode, scene, index)
    attempt['state'] = 'complete'
    scene.pop('pending_video_generation', None)
    save()
    core.emit_progress(router, 100)
    log(f'Scene {index:02} ใหม่พร้อมเล่น: {output.name} • ไป Summary เพื่อรวม Final ใหม่')
    return output
