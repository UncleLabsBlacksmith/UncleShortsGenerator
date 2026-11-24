# Release file policy — v1.0.1

## Included in GitHub source

- Application: `app_info.py`, `shorts_studio.py`, `shorts_core.py`, `studio_store.py`, `datetime_picker.py`, `model_picker.py`, `media_versions.py`, `router_retry.py`, `scene_generation.py`, `youtube_actions.py`, `clip_costs.py`, `upload_auto.py`.
- Build/dependencies: `build_installer.py`, `requirements.txt`.
- Regression tests: `test_installer.py`, `test_scene_generation.py`, `test_router_retry.py`, `test_studio.py`, `test_shorts.py`, `test_models.py`, `test_media_versions.py`, `test_youtube_costs.py`.
- Documentation: `README.md`, `USER_GUIDE.md`, `PUBLISHING.md`, `THIRD_PARTY_NOTICES.md`, `LICENSE`, `.gitignore`.
- Reviewed screenshot/branding: `SCREENSHOT1.png`, `assets/app-icon.png`, `assets/uncle.ico`. The icon was created from the supplied Uncle Engineer logo.

This is an explicit source allowlist. Historical notes `README_SHORTS.md` and restoration backups are kept locally, outside the release source set. Screenshots must be reviewed for visible keys and personal data before replacing them.

## Excluded from source and installer application files

| Files/folders | Reason |
| --- | --- |
| `.env*`, API keys, `client_secret*.json`, OAuth JSON, tokens, credentials, service accounts, private keys | Secrets and account access |
| `data/`, `projects/`, SQLite/DB/WAL files, `project.json`, settings | Personal projects, prompts, usage and account settings |
| Generated videos/audio, thumbnails, subtitles, upload manifests/receipts/tasks | User media and publication history |
| `venv/`, `.venv/`, bytecode, caches | Machine-specific dependencies and temporary files |
| `.build/`, `build/`, `dist/` | Bundled runtimes, intermediate files and binary outputs |
| Logs, `.part`, `.tmp`, lock files, backups | Runtime details and recovery data |
| Editor settings, `.agents/`, `.codex/`, `.aws/` | Local configuration and machine-specific instructions |

## GitHub release assets

`UncleShortsGenerator-Setup-1.0.1-x64.exe` is uploaded only to Releases. It contains the allowlisted runtime application, public docs/license/icon, a clean Python runtime with required dependencies, FFmpeg/ffprobe, and their license notices. It contains no user's OAuth JSON, tokens, API keys, project databases or generated media. A `SHA256SUMS.txt` accompanies the installer.

## Before publishing

Review the explicit source set for secret literals and accidental absolute user paths. Compare `.gitignore` with local generated/credential files, inspect the screenshot, run the offline regression suite, and smoke-test the fresh bundled runtime. Verify the release asset name/version, payload contents and SHA-256. Never publish the whole working folder or an old build stage.
