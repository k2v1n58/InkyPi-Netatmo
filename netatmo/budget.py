"""Strict Netatmo API call rate limiting and persistent cache.

Netatmo reduces polling call limits for developer integrations.
The plugin enforces a strict rate limit (4 calls per rolling hour,
including token refresh) and preserves recent responses on disk to
continue displaying data when the cap is reached.
"""

import json
import logging
import os
import threading
import time

logger = logging.getLogger(__name__)


def _read_json(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        logger.warning("Unreadable file %s (%s), ignored.", os.path.basename(path), e)
        return {}


def _write_json(path, data):
    tmp = f"{path}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, path)
    except OSError as e:
        logger.warning("Failed to write %s: %s", os.path.basename(path), e)
        try:
            os.remove(tmp)
        except OSError:
            pass


class CallBudget:
    """At most `max_calls` calls across a rolling window of `window` seconds."""

    def __init__(self, path, max_calls=4, window=3600):
        self.path = path
        self.max_calls = max_calls
        self.window = window
        self._lock = threading.Lock()
        self._calls = [float(t) for t in _read_json(path).get("calls", []) if isinstance(t, (int, float))]

    def _prune(self, now):
        self._calls = sorted(t for t in self._calls if now - self.window < t <= now + 60)

    def remaining(self, now=None):
        with self._lock:
            now = time.time() if now is None else now
            self._prune(now)
            return max(self.max_calls - len(self._calls), 0)

    def remaining_at(self, when, extra=0, now=None):
        """Calls still available at timestamp `when`, if making `extra` more calls now."""
        with self._lock:
            now = time.time() if now is None else now
            self._prune(now)
            pending = [t for t in self._calls if t > when - self.window] + [now] * extra
            return max(self.max_calls - len([t for t in pending if t > when - self.window]), 0)

    def try_acquire(self, now=None):
        """Reserves one call. Returns False if budget is exhausted."""
        with self._lock:
            now = time.time() if now is None else now
            self._prune(now)
            if len(self._calls) >= self.max_calls:
                return False
            self._calls.append(now)
            _write_json(self.path, {"calls": self._calls})
            return True

    def exhaust(self, now=None):
        """Netatmo returned HTTP 429: exhaust remaining calls until the window expires."""
        with self._lock:
            now = time.time() if now is None else now
            self._prune(now)
            self._calls += [now] * (self.max_calls - len(self._calls))
            _write_json(self.path, {"calls": self._calls})

    def seconds_until_free(self, now=None):
        with self._lock:
            now = time.time() if now is None else now
            self._prune(now)
            if len(self._calls) < self.max_calls:
                return 0
            return int(self._calls[0] + self.window - now) + 1


class DataCache:
    """Latest Netatmo data (station readings, history) persisted on disk."""

    def __init__(self, path):
        self.path = path
        self._lock = threading.Lock()
        self._data = _read_json(path)

    def get(self, key):
        with self._lock:
            entry = self._data.get(key)
            if isinstance(entry, dict) and "ts" in entry:
                return entry
            return None

    def age(self, key, now=None):
        entry = self.get(key)
        if not entry:
            return None
        return (time.time() if now is None else now) - float(entry["ts"])

    def keys(self):
        with self._lock:
            return list(self._data)

    def put(self, key, value, ts=None):
        with self._lock:
            self._data[key] = {"ts": time.time() if ts is None else ts, "value": value}
            _write_json(self.path, self._data)
