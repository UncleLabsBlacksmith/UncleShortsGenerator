"""Persistent cost ledger: actual API charges, estimates, and unknowns stay distinct."""
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
import uuid
from studio_store import connect
from model_picker import model_rates


def amount(value):
    try:
        result = Decimal(str(value))
        return result if result.is_finite() and result >= 0 else None
    except InvalidOperation:
        return None


class Ledger:
    def __init__(self, project):
        self.path = Path(project) if Path(project).suffix == '.sqlite3' else Path(project).with_name('costs.sqlite3')
        with connect(self.path) as db:
            db.execute('CREATE TABLE IF NOT EXISTS cost_events (id TEXT PRIMARY KEY, day INTEGER, category TEXT, model TEXT, actual TEXT, estimate TEXT, created TEXT)')

    def record(self, category, model='', day=None, actual=None, estimate=None, identifier=None):
        identifier = identifier or uuid.uuid4().hex
        actual, estimate = amount(actual), amount(estimate)
        with connect(self.path) as db:
            db.execute('INSERT INTO cost_events VALUES (?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET actual=COALESCE(excluded.actual,cost_events.actual), estimate=COALESCE(excluded.estimate,cost_events.estimate)',
                (identifier, day, category, model, str(actual) if actual is not None else None,
                 str(estimate) if estimate is not None else None, datetime.now(timezone.utc).isoformat()))
        return identifier

    def summary(self, day=None, episode_count=1):
        result = {}
        with connect(self.path) as db:
            rows = db.execute('SELECT day,category,actual,estimate FROM cost_events').fetchall()
        for scope, category, actual, estimate in rows:
            if day is not None and scope is not None and scope != day:
                continue
            weight = Decimal(1) / max(1, episode_count) if day is not None and scope is None else Decimal(1)
            item = result.setdefault(category, {'actual': Decimal(0), 'estimate': Decimal(0), 'unknown': 0, 'count': 0})
            item['count'] += 1
            if actual is not None:
                item['actual'] += Decimal(actual) * weight
            elif estimate is not None:
                item['estimate'] += Decimal(estimate) * weight
            else:
                item['unknown'] += 1
        return result

    def refresh(self, router, log=print, stop=None):
        from urllib.parse import quote
        with connect(self.path) as db:
            rows = db.execute('SELECT id,day,category,model FROM cost_events WHERE actual IS NULL').fetchall()
        for identifier, day, category, model in rows:
            if stop and stop.is_set():
                break
            if not identifier.startswith('gen-'):
                continue
            try:
                data = router.request('/generation?id=' + quote(identifier, safe=''))['data']
                cost = amount(data.get('total_cost'))
                if cost is not None:
                    self.record(category, model, day, actual=cost, identifier=identifier)
                    log(f'อัปเดตต้นทุน {category}: ${cost}')
            except Exception:
                log(f'ยังอ่านต้นทุน {category} ไม่ได้ คงสถานะไม่ทราบไว้')


def estimate_request(route, payload, response, catalog):
    models = [m for group in catalog.values() if isinstance(group, list) for m in group if isinstance(m, dict)]
    model = next((m for m in models if m.get('id') == payload.get('model') and model_rates(m)), {})
    prices = model_rates(model)
    if route == '/videos':
        rate = prices.get('per-video-second-' + str(payload.get('resolution', '')), prices.get('per-video-second'))
        return rate * Decimal(str(payload.get('duration', 0))) if rate is not None else None
    usage = response.get('usage', {}) if isinstance(response, dict) else {}
    if all(k in usage for k in ('prompt_tokens', 'completion_tokens')) and all(k in prices for k in ('prompt', 'completion')):
        return prices['prompt'] * Decimal(usage['prompt_tokens']) + prices['completion'] * Decimal(usage['completion_tokens'])
    return None


def project_summary(project, document, catalog, day=None):
    episodes = document.get('episodes', [])
    result = Ledger(project).summary(day, len(episodes))
    selected = [e for e in episodes if day is None or e['day'] == day]
    counts = {'video': 0, 'speech': 0, 'music': 0, 'text': 0}
    for episode in selected:
        folder = Path(project).parent / f"day_{episode['day']:02}"
        counts['video'] += sum(bool(s.get('job_id') or s.get('file_path') or (folder / f'{i:02}.mp4').is_file()) for i, s in enumerate(episode.get('scenes', []), 1))
        counts['speech'] += sum(max(len(s.get('audio_history', [])), int(bool(s.get('audio_file') or (folder / f'{i:02}.mp3').is_file()))) for i, s in enumerate(episode.get('scenes', []), 1))
        counts['music'] += max(len(list(folder.glob('background_*.*'))), int(bool(episode.get('music_file'))))
        counts['text'] += int(bool(episode.get('scenes')))
    for category, total in counts.items():
        item = result.setdefault(category, {'actual': Decimal(0), 'estimate': Decimal(0), 'unknown': 0, 'count': 0})
        historical = max(0, total - item['count'])
        if category == 'video' and historical:
            estimate = estimate_request('/videos', {'model': document['settings'].get('video_model'),
                'resolution': document['settings'].get('resolution'), 'duration': document['settings'].get('duration', 0)}, {}, catalog)
            if estimate is not None:
                item['estimate'] += estimate * historical
                historical = 0
        item['unknown'] += historical
    return result
