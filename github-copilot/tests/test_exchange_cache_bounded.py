"""#51: per-token exchange caches must not grow without bound."""

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from copilot import credentials as c


def test_expired_jwt_evicted(monkeypatch):
    c._jwt_cache.clear(); c._exchange_failure_cache.clear()
    c._jwt_cache["dead"] = ("tok", time.time() - 10, None)
    c._jwt_cache["live"] = ("tok2", time.time() + 1800, None)
    c._prune_exchange_caches()
    assert "dead" not in c._jwt_cache and "live" in c._jwt_cache


def test_expired_failure_deadline_evicted():
    c._exchange_failure_cache.clear()
    c._exchange_failure_cache["dead"] = time.time() - 10
    c._exchange_failure_cache["live"] = time.time() + 100
    c._prune_exchange_caches()
    assert "dead" not in c._exchange_failure_cache and "live" in c._exchange_failure_cache


def test_lock_table_bounded():
    c._jwt_cache.clear(); c._exchange_failure_cache.clear(); c._exchange_locks.clear()
    for i in range(200):
        c._exchange_locks[f"fp{i}"] = c.threading.Lock()
        c._prune_exchange_caches()
    assert len(c._exchange_locks) <= c._EXCHANGE_CACHE_MAX_ENTRIES


def test_live_jwt_lock_kept():
    c._jwt_cache.clear(); c._exchange_failure_cache.clear(); c._exchange_locks.clear()
    c._jwt_cache["active"] = ("tok", time.time() + 1800, None)
    c._exchange_locks["active"] = c.threading.Lock()
    for i in range(100):
        c._exchange_locks[f"x{i}"] = c.threading.Lock()
    c._prune_exchange_caches()
    assert "active" in c._exchange_locks


def test_held_lock_survives_pruning():
    """An exchange in flight holds its lock; pruning must not hand the next caller a new one."""
    c._jwt_cache.clear(); c._exchange_failure_cache.clear(); c._exchange_locks.clear()
    held = c._exchange_lock_for("inflight")
    held.acquire()
    try:
        for i in range(200):
            c._exchange_locks[f"y{i}"] = c.threading.Lock()
        c._prune_exchange_caches()
        assert c._exchange_lock_for("inflight") is held
    finally:
        held.release()
