"""One request-level Sheets read budget. Never retries mutations or SMTP."""
import random
import sys
import threading
import time
from functools import wraps

import requests
from .sheets import SheetIntegrityError


class ReadScheduler:
    def __init__(self, *, clock=None, sleep=None, jitter=None, progress=None,
                 interval=1.25, attempts=5, budget=120.0):
        self.clock, self.sleep = clock or time.monotonic, sleep or time.sleep
        self.jitter = jitter or random.uniform
        self.progress = progress or (lambda message: print(message, file=sys.stderr, flush=True))
        self.interval, self.attempts, self.budget = interval, attempts, budget
        self.last = None
        self.generation = self.requests = self.retries = 0
        self.lock = threading.RLock()

    @staticmethod
    def retryable(exc):
        response = getattr(exc, 'response', None)
        code = getattr(response, 'status_code', None)
        error = getattr(exc, 'error', {})
        error = error if isinstance(error, dict) else {}
        code = code if type(code) is int else error.get('code')
        errors = error.get('errors', [])
        reasons = [e.get('reason') for e in errors if isinstance(e, dict)] if isinstance(errors, list) else []
        reasons.append(error.get('status'))
        quota = code == 429 or (code == 403 and any(r in (
            'read_quota_exceeded', 'quotaExceeded', 'rateLimitExceeded', 'userRateLimitExceeded',
            'RESOURCE_EXHAUSTED') for r in reasons))
        return quota, quota or code in (408, 500, 502, 503, 504) or isinstance(
            exc, (requests.exceptions.Timeout, requests.exceptions.ConnectionError))

    def wait(self, delay, message=None):
        while delay > 0:
            chunk = min(10.0, delay)
            if message: self.progress(message + ' Waiting %.1fs.' % delay)
            self.sleep(chunk)
            delay -= chunk

    def read(self, request):
        with self.lock:
            started = self.clock()
            for attempt in range(self.attempts):
                if self.last is not None:
                    self.wait(max(0.0, self.last + self.interval - self.clock()))
                self.last = self.clock()
                self.requests += 1
                try:
                    return request()
                except Exception as exc:
                    quota, retryable = self.retryable(exc)
                    if not retryable: raise
                    if attempt + 1 == self.attempts:
                        self.progress('Google Sheets read retry limit exhausted; stopping for review.')
                        raise
                    delay = min(30.0, 2.0 ** (attempt + 1) + self.jitter(0.0, 1.0))
                    response = getattr(exc, 'response', None)
                    try:
                        retry_after = float(getattr(response, 'headers', {}).get('Retry-After', '0'))
                    except (ValueError, TypeError): retry_after = 0.0
                    delay = max(delay, retry_after)
                    if self.clock() - started + delay + self.interval > self.budget:
                        self.progress('Google Sheets read waiting budget exhausted; stopping for review.')
                        raise
                    self.generation += 1
                    self.retries += 1
                    kind = 'quota' if quota else 'temporary failure'
                    self.wait(delay, 'Google Sheets read %s; retry %d/%d.' % (kind, attempt + 2, self.attempts))


_schedulers = {}
_lock = threading.Lock()


def shared_scheduler(settings):
    # Quota belongs to the credential, not a worksheet, source or API client.
    key = str(settings.service_account.resolve())
    with _lock:
        if key not in _schedulers: _schedulers[key] = ReadScheduler()
        return _schedulers[key]


def pace_http(http, scheduler):
    """Intercept actual gspread HTTP attempts, including constructor GETs."""
    installed = vars(http).get('_event_read_scheduler')
    if installed is scheduler: return http
    if installed is not None:
        raise RuntimeError('Read client already has a different request scheduler.')
    original = http.request
    def request(method, *args, **kwargs):
        if method.lower() == 'get':
            return scheduler.read(lambda: original(method, *args, **kwargs))
        return original(method, *args, **kwargs)  # Exactly one write attempt.
    http.request = request
    http._event_read_scheduler = scheduler
    return http


def fresh_read(function):
    """Invalidate a composite read after backoff, never replay a mutation."""
    @wraps(function)
    def invoke(self, *args, **kwargs):
        scheduler = vars(self).get('read_scheduler')
        for sweep in range(2):
            generation = scheduler.generation if scheduler else None
            result = function(self, *args, **kwargs)
            if not scheduler or generation == scheduler.generation:
                return result
            scheduler.progress('Google Sheets read waiting invalidated the operation; refreshing required read checks.')
        raise SheetIntegrityError('Repeated read waiting prevented fresh read checks; stopped for review.')
    return invoke
