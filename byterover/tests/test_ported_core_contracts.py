"""Cases ported from hermes-agent core's shared memory test files when core stopped bundling ByteRover
(tests/plugins/memory/test_provider_multiplex_contract.py, test_provider_threads_inherit_profile.py).

On a multiplexed gateway every profile's agents share one process: the provider binds its store from
the ``hermes_home`` ``initialize`` receives and its background threads run under the spawner's profile.
Only the network/CLI backend is faked.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path

import byterover_plugin as byterover
import pytest
from agent.memory_manager import MemoryManager
from hermes_constants import (
    get_hermes_home,
    reset_hermes_home_override,
    set_hermes_home_override,
)
from waiting import wait_until


@pytest.fixture
def homes(tmp_path, monkeypatch):
    root = tmp_path / ".hermes"
    alpha, beta = root / "profiles" / "alpha", root / "profiles" / "beta"
    for home in (alpha, beta):
        home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(root))  # the launch profile: neither alpha nor beta
    return alpha, beta


class _Scope:
    def __init__(self, home):
        self.home = home

    def __enter__(self):
        self.token = set_hermes_home_override(str(self.home))

    def __exit__(self, *exc):
        reset_hermes_home_override(self.token)


def _run_interleaved(managers, tool, args_template, rounds=5):
    """Each profile's turn, on its own thread and scope: a tool call, a turn sync and a built-in
    memory-write mirror per round, all carrying that profile's name in their content."""
    barrier, errors = threading.Barrier(len(managers)), []

    def turns(home, manager):
        try:
            with _Scope(home):
                barrier.wait(timeout=5)
                for i in range(rounds):
                    args = {k: (v if v is not None else f"{home.name}-tool-{i}") for k, v in args_template.items()}
                    result = json.loads(manager.handle_tool_call(tool, args))
                    assert "error" not in result, result
                    manager.sync_all(f"{home.name}-turn-{i} what did we decide", "the plan", session_id="s")
                    manager.on_memory_write("add", "memory", f"{home.name}-write-{i}")
                assert manager.flush_pending(timeout=10)
        except Exception as exc:  # noqa: BLE001 -- re-raised by the assert below
            errors.append(exc)

    threads = [threading.Thread(target=turns, args=item) for item in managers.items()]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert not errors, errors


def _background_home(tmp_path, monkeypatch, spawn):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "default"))
    profile_home = tmp_path / "profiles" / "b"
    profile_home.mkdir(parents=True)
    seen: dict = {}
    token = set_hermes_home_override(profile_home)
    try:
        threads = spawn(seen)
    finally:
        reset_hermes_home_override(token)
    for t in threads:
        if t is not None:
            t.join(timeout=10)
    return profile_home, seen.get("home")


def test_byterover_binds_the_home_initialize_receives(homes):
    alpha, _ = homes
    provider = byterover.ByteRoverMemoryProvider({})
    provider.initialize("s", hermes_home=str(alpha))  # no context override: the ambient home is the root
    assert Path(provider._cwd) == alpha / "byterover"


def test_byterover_two_profiles_interleaving_stay_in_their_own_trees(homes, monkeypatch):
    calls: list = []
    lock = threading.Lock()

    def fake_run_brv(args, timeout=0, cwd=None):
        with lock:
            calls.append((args[0], args[-1], cwd))
        return {"success": True, "output": "x" * 40}

    monkeypatch.setattr(byterover, "_run_brv", fake_run_brv)
    monkeypatch.setattr(byterover, "_resolve_brv_path", lambda: "/bin/true")
    managers = {}
    for home in homes:
        manager = MemoryManager()
        with _Scope(home):
            manager.add_provider(byterover.ByteRoverMemoryProvider({}))
            manager.initialize_all(session_id=f"s-{home.name}", platform="api_server", hermes_home=str(home))
        managers[home] = manager
    _run_interleaved(managers, "brv_curate", {"content": None})
    wait_until(lambda: len(calls) >= 2 * 3 * 5, what="every brv call from both profiles")
    for home in homes:
        mine = [c for c in calls if c[1].endswith(f"{home.name}-content") or f"{home.name}-" in c[1]]
        assert mine and all(Path(cwd) == home / "byterover" for _, _, cwd in mine), mine


def test_byterover_background_thread_sees_spawner_profile_home(tmp_path, monkeypatch):
    def spawn(seen):
        p = byterover.ByteRoverMemoryProvider()
        p._curate = lambda *_a, **_kw: seen.__setitem__("home", get_hermes_home())
        return [p._curate_in_background("content", name="brv-test", what="test")]

    profile_home, seen_home = _background_home(tmp_path, monkeypatch, spawn)
    assert seen_home == profile_home
