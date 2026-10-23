"""Shared, persisted reading age limit for Readings and automation."""
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import threading
import time

from core.paths import scripts_path

DEFAULT_STALE_MS = 2000
MIN_STALE_MS = 100
MAX_STALE_MS = 60000
_lock = threading.RLock()
_value = DEFAULT_STALE_MS
_checked_at = float('-inf')
_signature = None


def validate_stale_ms(value):
    if type(value) is not int or not MIN_STALE_MS <= value <= MAX_STALE_MS:
        raise ValueError(f'Stale time must be an integer from {MIN_STALE_MS} to {MAX_STALE_MS} ms')
    return value


def get_stale_ms():
    """Cache settings in memory; notice changes from a standalone monitor."""
    global _value, _checked_at, _signature
    now = time.monotonic()
    with _lock:
        if now-_checked_at < 1:
            return _value
        _checked_at = now
        path = scripts_path('readings.json')
        try:
            stat = path.stat()
            signature = (stat.st_mtime_ns, stat.st_size)
            if signature != _signature:
                data = json.loads(path.read_text(encoding='utf-8'))
                value = validate_stale_ms(data['stale_time_ms'])
                _value, _signature = value, signature
        except (OSError, ValueError, KeyError, TypeError):
            # A missing or half-written external file never changes a valid
            # setting. The UI's own write is atomic and reports failures.
            pass
        return _value


def get_stale_seconds():
    return get_stale_ms()/1000.0


def set_stale_ms(value):
    global _value, _checked_at, _signature
    value = validate_stale_ms(value)
    path = scripts_path('readings.json')
    with _lock:
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                             prefix='readings-', suffix='.tmp', delete=False) as stream:
                temporary = Path(stream.name)
                json.dump({'stale_time_ms': value}, stream, indent=2)
                stream.write('\n')
            os.replace(temporary, path)
            stat = path.stat()
            _signature = (stat.st_mtime_ns, stat.st_size)
            _value, _checked_at = value, time.monotonic()
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    return value
