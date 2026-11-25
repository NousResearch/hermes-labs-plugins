"""#51: per-token exchange caches must not grow without bound.

#107: pruning must also keep a lock entry while a caller holds it between lookup and
acquire, or while a waiter is queued on it — not only while the lock is held.
"""

import sys
import time

import pytest
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
        c._exchange_locks[f"fp{i}"] = c._ExchangeLease()
        c._prune_exchange_caches()
    assert len(c._exchange_locks) <= c._EXCHANGE_CACHE_MAX_ENTRIES


def test_live_jwt_lock_kept():
    c._jwt_cache.clear(); c._exchange_failure_cache.clear(); c._exchange_locks.clear()
    c._jwt_cache["active"] = ("tok", time.time() + 1800, None)
    c._exchange_locks["active"] = c._ExchangeLease()
    for i in range(100):
        c._exchange_locks[f"x{i}"] = c._ExchangeLease()
    c._prune_exchange_caches()
    assert "active" in c._exchange_locks


def test_held_lock_survives_pruning():
    """An exchange in flight holds its lock; pruning must not hand the next caller a new one."""
    c._jwt_cache.clear(); c._exchange_failure_cache.clear(); c._exchange_locks.clear()
    held = c._exchange_lock_for("inflight")
    held.lock.acquire()
    try:
        for i in range(200):
            c._exchange_locks[f"y{i}"] = c._ExchangeLease()
        c._prune_exchange_caches()
        again = c._exchange_lock_for("inflight")
        assert again is held
        c._release_exchange_lock(again)
    finally:
        held.lock.release()
        c._release_exchange_lock(held)


def test_handed_out_unacquired_lock_survives_pruning():
    """#107: a caller between lookup and acquire holds the lock object but has not locked it.

    Pruning used to evict the entry in exactly that window, so the next lookup minted a
    second lock for the same token and two exchanges ran concurrently. With the handout
    counted, the second lookup must return the SAME lock.
    """
    c._jwt_cache.clear(); c._exchange_failure_cache.clear(); c._exchange_locks.clear()
    try:
        a = c._exchange_lock_for("fixture")
        for i in range(64):
            c._exchange_locks[f"pad{i}"] = c._ExchangeLease()
        b = c._exchange_lock_for("fixture")
        assert b is a
    finally:
        c._release_exchange_lock(a)
        c._release_exchange_lock(b)


def test_waiter_pin_survives_pruning():
    """#107: a waiter registered on a locked fingerprint keeps the entry while it queues."""
    c._jwt_cache.clear(); c._exchange_failure_cache.clear(); c._exchange_locks.clear()
    holder = c._exchange_lock_for("busy")
    holder.lock.acquire()
    waiter = c._exchange_lock_for("busy")  # would block on acquire; the handout alone pins
    try:
        for i in range(64):
            c._exchange_locks[f"pad{i}"] = c._ExchangeLease()
        c._prune_exchange_caches()
        assert c._exchange_locks.get("busy") is holder
    finally:
        holder.lock.release()
        c._release_exchange_lock(holder)
        c._release_exchange_lock(waiter)


def test_released_entry_prunable_again():
    """Once the last handout is released and the lock is free, the entry is evictable."""
    c._jwt_cache.clear(); c._exchange_failure_cache.clear(); c._exchange_locks.clear()
    lease = c._exchange_lock_for("gone")
    c._release_exchange_lock(lease)
    for i in range(64):
        c._exchange_locks[f"pad{i}"] = c._ExchangeLease()
    c._prune_exchange_caches()
    assert "gone" not in c._exchange_locks


def test_exchange_lease_pins_entry_for_its_whole_scope():
    """The production path: the entry survives the whole ``with`` scope and refs balance."""
    c._jwt_cache.clear(); c._exchange_failure_cache.clear(); c._exchange_locks.clear()
    with c._exchange_lock_lease("ctx") as lease:
        assert c._exchange_locks["ctx"] is lease and lease.refs == 1
        held = c._exchange_lock_for("ctx")
        assert held is lease and lease.refs == 2
        for i in range(64):
            c._exchange_locks[f"pad{i}"] = c._ExchangeLease()
        c._prune_exchange_caches()
        assert c._exchange_locks.get("ctx") is lease
        c._release_exchange_lock(held)
    assert lease.refs == 0


def test_exchange_lease_releases_on_exception():
    """A failing exchange must still drop its handout and unlock the entry."""
    c._jwt_cache.clear(); c._exchange_failure_cache.clear(); c._exchange_locks.clear()
    lease = {}
    with pytest.raises(RuntimeError):
        with c._exchange_lock_lease("boom") as l:
            lease["l"] = l
            raise RuntimeError("exchange failed")
    assert lease["l"].refs == 0
    assert not lease["l"].lock.locked()


def test_lease_release_allows_pruning():
    """Entries handed out and released leave the table free to shrink again."""
    c._jwt_cache.clear(); c._exchange_failure_cache.clear(); c._exchange_locks.clear()
    for i in range(200):
        lease = c._exchange_lock_for(f"fp{i}")
        c._release_exchange_lock(lease)
    c._prune_exchange_caches()
    assert len(c._exchange_locks) <= c._EXCHANGE_CACHE_MAX_ENTRIES
