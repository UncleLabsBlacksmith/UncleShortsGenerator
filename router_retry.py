"""Bounded, cancellable retries for rejected requests and read-only polling."""
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import json
import math
import random
import urllib.error
import urllib.request


class RouterHTTPError(RuntimeError):
    def __init__(self, status, body, headers=None):
        super().__init__(f'OpenRouter HTTP {status}: {body[:1500]}')
        self.status, self.body = status, body
        try:
            result = json.loads(body)
            metadata = (result.get('error') or {}).get('metadata') or {}
        except (ValueError, AttributeError):
            result, metadata = {}, {}
        self.headers = {str(k).lower(): v for k, v in (metadata.get('headers') or {}).items()}
        self.headers.update({str(k).lower(): v for k, v in (headers or {}).items()})
        self.limit_source = metadata.get('limit_source', '')
        self.rejected = status in (400, 401, 402, 403, 429) and not result.get('id') and not result.get('generation_id')

    def delay(self, retry):
        delays = [min(60, 10 * 2**retry)]
        value = self.headers.get('retry-after')
        if value is not None:
            try:
                delays.append(float(value))
            except (TypeError, ValueError):
                try:
                    delays.append(parsedate_to_datetime(str(value)).timestamp() - datetime.now(timezone.utc).timestamp())
                except (ValueError, TypeError, OverflowError):
                    pass
        reset = self.headers.get('x-ratelimit-reset')
        if reset is not None:
            try:
                timestamp = float(reset)
                if timestamp > 1e12:
                    timestamp /= 1000
                delays.append(timestamp - datetime.now(timezone.utc).timestamp())
            except (TypeError, ValueError, OverflowError):
                pass
        return max(d for d in delays if math.isfinite(d) and d >= 0) + random.uniform(0, 1)


class RetryCancelled(InterruptedError):
    rejected = True


def open_with_retry(request, timeout, stop, log, max_retries=4, abort_video_429=False):
    for attempt in range(max_retries + 1):
        if stop.is_set():
            raise RetryCancelled('หยุดก่อนส่งคำขอแล้ว')
        try:
            return urllib.request.urlopen(request, timeout=timeout)
        except urllib.error.HTTPError as error:
            body = error.read().decode('utf-8', errors='replace')
            failure = RouterHTTPError(error.code, body, error.headers)
            error.close()
            read_only = request.get_method() == 'GET'
            if abort_video_429 and read_only and failure.status == 429:
                raise failure from None
            safe = (failure.status == 429 and (read_only or failure.rejected)) or (read_only and failure.status == 503)
            if not safe or attempt >= max_retries:
                raise failure from None
            delay = failure.delay(attempt)
            log(f'OpenRouter HTTP {failure.status} • รอ {delay:.1f} วินาที แล้วลองใหม่ {attempt+1}/{max_retries} ({request.get_method()})')
            if stop.wait(delay):
                raise RetryCancelled('หยุดระหว่างรอลองใหม่แล้ว คำขอที่ถูกปฏิเสธยังไม่ได้เริ่มสร้าง')
