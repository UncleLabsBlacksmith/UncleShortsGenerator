import json
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

# ==========================================
# 1. ตั้งค่าไฟล์ OAuth, Token และโซนเวลา
# ==========================================
OAUTH_FILE = "client_secret.json"
TOKEN_FILE = "token.json"
SCOPES = ["https://www.googleapis.com/auth/youtube"]
TIMEZONE = "Asia/Bangkok"  # โซนเวลาสำหรับแปลงค่า publish_at (เช่น "Asia/Bangkok")

# ==========================================
# 2. ตั้งค่ารายการวิดีโอที่จะอัปโหลดต่อเนื่องที่นี่
# (ไม่ได้ใส่ค่าให้ปล่อยเป็น None หรือ "" ได้เลย)
# ==========================================
VIDEO_CONFIG = []  # Configure explicitly; the GUI supplies selected episodes.


def has_value(val: object) -> bool:
    """ตรวจสอบว่ามีการใส่ค่าจริงหรือไม่ (ไม่ใช่ None และไม่ใช่สตริงว่าง)."""
    if val is None:
        return False
    if isinstance(val, str) and val.strip() == "":
        return False
    return True


def load_oauth_config(oauth_path: str) -> dict:
    """อ่านและตรวจสอบโครงสร้างข้อมูลจากไฟล์ oauth.json."""
    try:
        if not has_value(oauth_path) or not os.path.isfile(oauth_path):
            print(f"ไม่พบไฟล์ {oauth_path} ในโฟลเดอร์ปัจจุบัน", file=sys.stderr)
            return {}

        with open(oauth_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        installed = data.get("installed") or data.get("web")
        if not installed:
            print("โครงสร้างไฟล์ oauth.json ไม่ถูกต้อง", file=sys.stderr)
            return {}

        client_id = installed.get("client_id", "")
        client_secret = installed.get("client_secret", "")

        if (
            not has_value(client_id)
            or str(client_id).startswith("-----")
            or not has_value(client_secret)
            or str(client_secret).startswith("-----")
        ):
            print(
                "คำเตือน: พบว่า client_id หรือ client_secret ใน oauth.json ยังเป็นค่า placeholder (-----)",
                file=sys.stderr,
            )

        return data
    except Exception as err:
        print(f"อ่านไฟล์ oauth.json ไม่สำเร็จ: {err}", file=sys.stderr)
        return {}


def get_authenticated_service(oauth_path: str, token_path: str, allow_interactive: bool = True) -> object | None:
    """ยืนยันตัวตนผ่าน OAuth 2.0 โดยใช้ข้อมูลจาก oauth.json และเก็บ token.json."""
    client_config = load_oauth_config(oauth_path)
    if not client_config:
        return None

    creds = None
    try:
        if has_value(token_path) and os.path.exists(token_path):
            creds = Credentials.from_authorized_user_file(token_path, SCOPES)
            configured_id = (client_config.get('installed') or client_config.get('web') or {}).get('client_id')
            if configured_id and creds.client_id != configured_id:
                if allow_interactive:
                    creds = None
                else:
                    raise RuntimeError('OAuth client does not match saved token; Login YouTube again')

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                if not allow_interactive:
                    raise RuntimeError("YouTube login required: sign in from Uncle Shorts Generator before running the scheduled task")
                flow = InstalledAppFlow.from_client_config(client_config, SCOPES)
                creds = flow.run_local_server(port=0)

            if has_value(token_path):
                with open(token_path, "w", encoding="utf-8") as token_file:
                    token_file.write(creds.to_json())

        return build("youtube", "v3", credentials=creds)
    except Exception as err:
        print(f"ยืนยันตัวตน OAuth ไม่สำเร็จ: {err}", file=sys.stderr)
        return None


def to_iso_utc(publish_at: str | None, timezone_str: str) -> str | None:
    """แปลงเวลา 'YYYY-MM-DD HH:MM' ในโซนเวลาที่กำหนดเป็น ISO 8601 UTC สำหรับ publishAt."""
    if not has_value(publish_at):
        return None

    try:
        naive_dt = datetime.strptime(str(publish_at).strip(), "%Y-%m-%d %H:%M")
        tz = ZoneInfo(timezone_str) if has_value(timezone_str) else ZoneInfo("Asia/Bangkok")
        local_dt = naive_dt.replace(tzinfo=tz)
        utc_dt = local_dt.astimezone(ZoneInfo("UTC"))
        return utc_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception as err:
        print(f"รูปแบบ publish_at '{publish_at}' ไม่ถูกต้อง (ข้ามการตั้งเวลา): {err}", file=sys.stderr)
        return None


def set_thumbnail(youtube: object, video_id: str, thumbnail_path: str) -> None:
    """อัปโหลดภาพปก (Thumbnail) ให้กับวิดีโอที่ระบุ."""
    if not has_value(thumbnail_path):
        pass
        return

    try:
        if not os.path.isfile(thumbnail_path):
            print(f"ไม่พบไฟล์ภาพปก '{thumbnail_path}' (ข้ามการตั้งภาพปก)", file=sys.stderr)
            return

        media = MediaFileUpload(thumbnail_path, resumable=True)
        youtube.thumbnails().set(
            videoId=video_id,
            media_body=media,
        ).execute()
        print(f"ตั้งค่าภาพปกสำเร็จ ({thumbnail_path})")
    except Exception as err:
        print(f"ตั้งค่าภาพปกไม่สำเร็จ: {err}", file=sys.stderr)
        pass


def add_video_to_playlist(youtube: object, video_id: str, playlist_id: str) -> None:
    """เพิ่มวิดีโอลงใน Playlist ที่กำหนด."""
    if not has_value(playlist_id):
        pass
        return

    try:
        body = {
            "snippet": {
                "playlistId": str(playlist_id).strip(),
                "resourceId": {
                    "kind": "youtube#video",
                    "videoId": video_id,
                },
            }
        }
        youtube.playlistItems().insert(
            part="snippet",
            body=body,
        ).execute()
        print(f"เพิ่มวิดีโอลง Playlist ({playlist_id}) สำเร็จ")
    except Exception as err:
        print(f"เพิ่มวิดีโอลง Playlist ไม่สำเร็จ: {err}", file=sys.stderr)
        pass


def upload_single_video(youtube: object, item: dict, timezone_str: str) -> str | None:
    """อัปโหลดวิดีโอ 1 รายการตามค่าที่ตั้งไว้ (หากไม่ได้ใส่ค่าที่จำเป็นจะข้าม)."""
    if not isinstance(item, dict):
        pass
        return None

    file_path = item.get("file_path")
    if not has_value(file_path) or not os.path.isfile(file_path):
        print(f"ไม่ได้ระบุไฟล์วิดีโอหรือหาไฟล์ไม่พบ ({file_path}) -> ข้ามรายการนี้", file=sys.stderr)
        pass
        return None

    raw_title = item.get("title")
    title = str(raw_title).strip() if has_value(raw_title) else os.path.splitext(os.path.basename(file_path))[0]

    description = str(item.get("description")) if has_value(item.get("description")) else ""
    raw_tags = item.get("tags")
    tags = [str(t).strip() for t in raw_tags if has_value(t)] if isinstance(raw_tags, list) else []

    category_id = str(item.get("category_id")).strip() if has_value(item.get("category_id")) else "22"
    raw_privacy = str(item.get("privacy_status")).strip() if has_value(item.get("privacy_status")) else "private"

    iso_publish_at = to_iso_utc(item.get("publish_at"), timezone_str)
    privacy_status = "private" if iso_publish_at else raw_privacy

    status_body = {
        "privacyStatus": privacy_status,
        "selfDeclaredMadeForKids": bool(item.get("made_for_kids", False)),
    }
    if "contains_synthetic_media" in item:
        status_body["containsSyntheticMedia"] = bool(item["contains_synthetic_media"])
    if has_value(iso_publish_at):
        status_body["publishAt"] = iso_publish_at

    body = {
        "snippet": {
            "title": title,
            "description": description,
            "tags": tags,
            "categoryId": category_id,
        },
        "status": status_body,
    }

    chunk_mb = item.get("chunk_size_mb")
    chunk_size = int(chunk_mb) * 1024 * 1024 if has_value(chunk_mb) else 4 * 1024 * 1024

    try:
        media = MediaFileUpload(
            file_path,
            chunksize=chunk_size,
            resumable=True,
        )

        request = youtube.videos().insert(
            part="snippet,status",
            body=body,
            media_body=media,
        )

        response = None
        while response is None:
            status, response = request.next_chunk()
            if status:
                progress = int(status.progress() * 100)
                print(f"กำลังอัปโหลด [{title}] ... {progress}%", end="\r")

        video_id = response.get("id")
        if not has_value(video_id):
            print("\nอัปโหลดเสร็จสิ้นแต่ไม่ได้รับ Video ID", file=sys.stderr)
            pass
            return None

        print(f"\nอัปโหลดสำเร็จ! Title: {title}")
        print(f"Video ID: {video_id} | Link: https://www.youtube.com/watch?v={video_id}")
        if has_value(iso_publish_at):
            print(f"ตั้งเวลาเผยแพร่ที่ ({timezone_str}): {item.get('publish_at')} (UTC: {iso_publish_at})")
        else:
            print(f"สถานะความเป็นส่วนตัว: {privacy_status}")

        set_thumbnail(youtube, video_id, item.get("thumbnail_path"))
        add_video_to_playlist(youtube, video_id, item.get("playlist_id"))

        return video_id

    except HttpError as err:
        print(f"\nเกิดข้อผิดพลาดจาก YouTube API: {err}", file=sys.stderr)
        pass
        return None
    except Exception as err:
        print(f"\nเกิดข้อผิดพลาดระหว่างอัปโหลด: {err}", file=sys.stderr)
        pass
        return None


def main() -> None:
    if not isinstance(VIDEO_CONFIG, list) or len(VIDEO_CONFIG) == 0:
        pass
        return

    try:
        youtube = get_authenticated_service(OAUTH_FILE, TOKEN_FILE)
        if youtube is None:
            pass
            return

        total = len(VIDEO_CONFIG)
        for idx, item in enumerate(VIDEO_CONFIG, start=1):
            print(f"\n=== รายการที่ {idx}/{total} ===")
            try:
                upload_single_video(youtube, item, TIMEZONE)
            except Exception:
                pass
    except Exception:
        pass


if __name__ == "__main__":
    main()
