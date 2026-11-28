"""Cases ported from hermes-agent core's shared memory test files when core stopped bundling RetainDB
(tests/plugins/memory/test_provider_multiplex_contract.py, test_provider_threads_inherit_profile.py, test_multiplex_memory_identity_scope.py, test_secret_scope_plugin_families.py).

On a multiplexed gateway every profile's agents share one process: the provider binds its store from
the ``hermes_home`` ``initialize`` receives and its background threads run under the spawner's profile.
Only the network/CLI backend is faked.
"""
from __future__ import annotations

import json
import threading
from typing import ClassVar
from unittest.mock import MagicMock

import pytest
import retaindb_plugin as retaindb
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


class _FakeClient:
    calls: ClassVar[list] = []
    lock = threading.Lock()

    def __init__(self, api_key, base_url, project):
        self.project = project

    def _record(self, what, content):
        with self.lock:
            type(self).calls.append((what, self.project, content))

    def add_memory(self, user_id, session_id, content, **kwargs):
        self._record("add_memory", content)
        return {"ok": True}

    def ingest_session(self, user_id, session_id, messages, timeout=15.0):
        self._record("ingest_session", messages[0]["content"])
        return {"ok": True}


@pytest.fixture
def fake_retaindb(monkeypatch):
    _FakeClient.calls = []
    monkeypatch.setattr(retaindb, "_Client", _FakeClient)
    monkeypatch.setattr(retaindb, "get_secret", lambda name, default=None: "rdb-key" if name == "RETAINDB_API_KEY" else default)
    monkeypatch.setattr(retaindb, "_load_retaindb_config", dict)
    return retaindb


def test_retaindb_binds_the_home_initialize_receives(homes, fake_retaindb):
    alpha, _ = homes
    provider = fake_retaindb.RetainDBMemoryProvider()
    provider.initialize("s", hermes_home=str(alpha))  # no context override: the ambient home is the root
    try:
        assert provider._queue._db_path == alpha / "retaindb_queue.db"
        assert provider._client.project == "hermes-alpha"
    finally:
        provider.shutdown()


def test_retaindb_two_profiles_interleaving_stay_in_their_own_projects(homes, fake_retaindb):
    managers = {}
    for home in homes:
        manager = MemoryManager()
        with _Scope(home):
            manager.add_provider(fake_retaindb.RetainDBMemoryProvider())
            manager.initialize_all(session_id=f"s-{home.name}", platform="api_server", hermes_home=str(home))
        managers[home] = manager
    try:
        _run_interleaved(managers, "retaindb_remember", {"content": None})
        wait_until(lambda: len(_FakeClient.calls) >= 2 * 3 * 5, what="every RetainDB write from both profiles")
    finally:
        for manager in managers.values():
            manager.shutdown_all()
    for home in homes:
        mine = [c for c in _FakeClient.calls if f"{home.name}-" in c[2]]
        assert {c[0] for c in mine} == {"add_memory", "ingest_session"}, mine
        assert all(project == f"hermes-{home.name}" for _, project, _ in mine), mine
        assert (home / "retaindb_queue.db").exists()


def test_retaindb_background_thread_sees_spawner_profile_home(tmp_path, monkeypatch):
    def spawn(seen):
        p = retaindb.RetainDBMemoryProvider()
        p._client = MagicMock()
        p._context_overlay = lambda query: {"context": seen.setdefault("home", get_hermes_home()) and "ctx"}
        p._client.ask_user.return_value = {"answer": ""}
        p._client.get_agent_model.return_value = {}
        p.queue_prefetch("what do you know")
        return list(p._prefetch_threads)

    profile_home, seen_home = _background_home(tmp_path, monkeypatch, spawn)
    assert seen_home == profile_home


# Ported from test_multiplex_memory_identity_scope.py and the RetainDB pair in
# tests/plugins/test_secret_scope_plugin_families.py.

@pytest.fixture
def multiplex_scope():
    from agent import secret_scope

    tokens = []

    def _enter(scope):
        secret_scope.set_multiplex_active(True)
        tokens.append(secret_scope.set_secret_scope(scope))

    yield _enter
    for token in reversed(tokens):
        secret_scope.reset_secret_scope(token)
    secret_scope.set_multiplex_active(False)


def test_secondary_profile_memory_identity_never_inherits_default_environ(tmp_path, monkeypatch, multiplex_scope):
    """Under multiplexing os.environ is the DEFAULT profile's .env: a secondary profile without its own
    RETAINDB_PROJECT / RETAINDB_BASE_URL gets its per-profile default, never the default's account."""
    monkeypatch.setenv("RETAINDB_PROJECT", "proj-default")
    monkeypatch.setenv("RETAINDB_BASE_URL", "https://rdb.default")
    prof_b = tmp_path / ".hermes" / "profiles" / "b"
    prof_b.mkdir(parents=True)
    (prof_b / "config.yaml").write_text("{}\n")
    monkeypatch.setenv("HERMES_HOME", str(prof_b))
    multiplex_scope({"RETAINDB_API_KEY": "rdb-b"})
    provider = retaindb.RetainDBMemoryProvider()
    provider.initialize("s1", hermes_home=str(prof_b))
    try:
        assert provider._client.project == "hermes-b"
        assert "default" not in provider._client.base_url
    finally:
        provider.shutdown()


def test_retaindb_scoped_key_wins(multiplex_scope, monkeypatch):
    monkeypatch.setenv("RETAINDB_API_KEY", "env-other-profile")
    multiplex_scope({"RETAINDB_API_KEY": "scoped-key"})
    assert retaindb.RetainDBMemoryProvider().is_available() is True


def test_retaindb_scoped_miss_does_not_borrow_environ(multiplex_scope, monkeypatch):
    monkeypatch.setenv("RETAINDB_API_KEY", "env-other-profile")  # another profile's key; this scope has none
    multiplex_scope({})
    assert retaindb.RetainDBMemoryProvider().is_available() is False
