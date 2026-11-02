"""Small process-local admission controls; global quotas belong at the gateway."""
import hmac
import os
from pathlib import Path
import time


def read_secret(name: str) -> str | None:
    """Load one configured application secret without logging its value or path.

    Environment injection and a mounted secret file are supported alternatives.
    Rotation takes effect when the application is restarted. Files are bounded
    before reading so an invalid mount cannot allocate an unbounded buffer.
    """
    value = os.getenv(name) or None
    filename = os.getenv(name + '_FILE') or None
    if value is not None and filename is not None:
        raise ValueError(f'Configure either {name} or {name}_FILE, not both')
    if filename is None:
        return value
    try:
        with Path(filename).open('rb') as handle:
            raw = handle.read(16385)
        if len(raw) > 16384:
            raise ValueError
        value = raw.decode('utf-8').strip()
    except (OSError, ValueError):
        raise ValueError(f'{name}_FILE must contain a readable UTF-8 secret of at most 16 KiB') from None
    if not value:
        raise ValueError(f'{name}_FILE must not be empty')
    return value


def matches_bearer(incoming: str | None, key: str) -> bool:
    return hmac.compare_digest((incoming or '').encode('utf-8'), f'Bearer {key}'.encode('utf-8'))


class Admission:
    def __init__(self, concurrent: int, per_minute: int):
        if concurrent < 1 or per_minute < 1:
            raise ValueError('Admission limits must be positive')
        self.concurrent = concurrent
        self.per_minute = per_minute
        self.active = 0
        self.tokens = float(per_minute)
        self.updated = time.monotonic()

    def acquire(self) -> int | None:
        """Called only on the ASGI event loop; no await points during mutation."""
        now = time.monotonic()
        self.tokens = min(float(self.per_minute), self.tokens + (now - self.updated) * self.per_minute / 60)
        self.updated = now
        if self.active >= self.concurrent:
            return 503
        if self.tokens < 1:
            return 429
        self.tokens -= 1
        self.active += 1
        return None

    def release(self) -> None:
        self.active = max(0, self.active - 1)
