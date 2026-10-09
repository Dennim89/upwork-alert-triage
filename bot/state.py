"""Small JSON state in data/state.json: owner chat, getUpdates offset, cross-run backoff. Atomic writes.

A corrupt state file fails closed (StateError): no new owner can be bound until it is fixed or deleted.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

BACKOFF_BASE_S = 300       # after the n-th consecutive failure wait 5, 10, 20, 40, 80, 120 min
BACKOFF_CAP_S = 7200
BACKOFF_SLACK_S = 30       # scheduler jitter: a run 4m50s later still counts as "after 5 min"


class StateError(Exception):
    """state.json exists but cannot be used."""


def load(path: Path | str) -> dict:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as e:
        raise StateError(f"cannot read {path}: {e.__class__.__name__}") from None
    try:
        data = json.loads(text)
    except ValueError:
        raise StateError(f"corrupt JSON in {path}; fix or delete it") from None
    if not isinstance(data, dict):
        raise StateError(f"unexpected content in {path}; fix or delete it")
    return data


def save(path: Path | str, data: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".state-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1, sort_keys=True)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def backoff_wait(state: dict, key: str, now: float) -> float:
    """Seconds left before `key` may be tried again (0 = go)."""
    b = (state.get("backoff") or {}).get(key) or {}
    return max(0.0, float(b.get("next_try", 0)) - BACKOFF_SLACK_S - now)


def backoff_fail(state: dict, key: str, now: float) -> int:
    """Record a failure; returns the pause in seconds (grows x2 per consecutive failure, capped)."""
    b = state.setdefault("backoff", {}).setdefault(key, {})
    fails = int(b.get("fails", 0)) + 1
    delay = min(BACKOFF_CAP_S, BACKOFF_BASE_S * 2 ** min(fails - 1, 10))
    b.update(fails=fails, next_try=now + delay)
    return delay


def backoff_ok(state: dict, key: str) -> None:
    (state.get("backoff") or {}).pop(key, None)
