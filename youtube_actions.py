"""Explicit GUI uploads and read-only OAuth checks; never invoked on startup."""
from datetime import datetime, timezone
import hashlib
from pathlib import Path
import shorts_core as core


def authenticated_channel(oauth, token):
    import upload_auto
    youtube = upload_auto.get_authenticated_service(oauth, token, allow_interactive=False)
    if youtube is None:
        raise ValueError('OAuth ใช้ไม่ได้ กรุณา Login YouTube ใหม่')
    result = youtube.channels().list(part='snippet', mine=True).execute()
    if not result.get('items'):
        raise ValueError('OAuth ผ่าน แต่ไม่พบช่อง YouTube ของบัญชีนี้')
    channel = result['items'][0]
    return youtube, {'id': channel['id'], 'title': channel.get('snippet', {}).get('title', channel['id'])}


def publish_now(path, day, privacy='public', log=print):
    from googleapiclient.http import MediaFileUpload
    if privacy not in ('public', 'unlisted', 'private'):
        raise ValueError('สถานะการเผยแพร่ไม่ถูกต้อง')
    document = core.read_json(path)
    episode = next(e for e in document['episodes'] if e['day'] == day)
    video = Path(episode.get('file_path', ''))
    if not video.is_file():
        raise ValueError('ยังไม่มี Final ของ EP ที่เลือก กรุณารวมคลิปก่อนอัปโหลด')
    youtube, channel = authenticated_channel(document['settings']['oauth'], document['settings']['token'])
    digest = hashlib.sha256(video.read_bytes()).hexdigest()
    history = episode.setdefault('youtube_uploads', [])
    previous = next((r for r in history if r.get('sha256') == digest and r.get('channel_id') == channel['id']), None)
    if previous:
        if previous.get('state') == 'uploaded' and previous.get('url'):
            if previous.get('privacy') != privacy:
                result = youtube.videos().update(part='status', body={'id': previous['video_id'],
                    'status': {'privacyStatus': privacy, 'selfDeclaredMadeForKids': False, 'containsSyntheticMedia': True}}).execute()
                previous.update(privacy=result.get('status', {}).get('privacyStatus', 'unknown'), requested_privacy=privacy)
                core.save_json(path, document)
            log('คลิปเวอร์ชันนี้อัปโหลดแล้ว ใช้ลิงก์เดิม ไม่อัปโหลดซ้ำ')
            return previous, channel
        raise ValueError('การอัปโหลดครั้งก่อนยังไม่ทราบผล กรุณาตรวจ YouTube Studio ก่อนลองใหม่ เพื่อป้องกันคลิปซ้ำ')
    record = dict(state='uploading', sha256=digest, file_path=str(video), channel_id=channel['id'],
                  requested_privacy=privacy, created_at=datetime.now(timezone.utc).isoformat())
    history.append(record)
    core.save_json(path, document)
    body = {'snippet': {'title': episode['title'], 'description': episode.get('description', ''),
                        'tags': episode.get('tags', []), 'categoryId': '27'},
            'status': {'privacyStatus': privacy, 'selfDeclaredMadeForKids': False, 'containsSyntheticMedia': True}}
    media = None
    try:
        log(f"อัปโหลด EP {day:02} → {channel['title']} • {privacy} • ไม่ตั้งเวลา")
        media = MediaFileUpload(str(video), chunksize=4*1024*1024, resumable=True)
        request = youtube.videos().insert(part='snippet,status', body=body,
            media_body=media)
        response = None
        while response is None:
            progress, response = request.next_chunk()
            if progress:
                log(f'อัปโหลด {progress.progress()*100:.0f}%')
        video_id = response.get('id')
        if not video_id:
            raise ValueError('YouTube ไม่ส่ง Video ID กลับมา')
        record.update(state='uploaded', video_id=video_id, url=f'https://www.youtube.com/watch?v={video_id}',
                      privacy=response.get('status', {}).get('privacyStatus', 'unknown'))
        core.save_json(path, document)
        log('อัปโหลดสำเร็จ: ' + record['url'])
        if record['privacy'] != privacy:
            log(f"สถานะที่ YouTube ตอบกลับ: {record['privacy']} • ตรวจสถานะใน YouTube Studio อีกครั้ง")
        return record, channel
    except Exception:
        if record['state'] != 'uploaded':
            record['state'] = 'needs_review'
            core.save_json(path, document)
        raise
    finally:
        if media is not None:
            media.stream().close()
