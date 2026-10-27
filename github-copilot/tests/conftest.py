"""Import the plugin as the package ``github_copilot_plugin`` (its profiles register in the process-wide
provider registry, as on a core without bundled Copilot) and give every test an empty HERMES_HOME and
HOME, with no GitHub token in the environment, so nothing reads or writes the real ~/.hermes.

On a core that still bundles Copilot or lacks the provider seams the plugin is inert; every test not
marked ``any_core`` is then skipped with the reason (hermes-agent#144788 adds the seams)."""

from __future__ import annotations

import contextlib
import importlib.util
import os
import sys
import threading
from pathlib import Path
from typing import NamedTuple

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
# ``--import-mode=importlib`` keeps tests/ off sys.path; strip the repo root too, so a stray
# top-level module there can never shadow core's packages.
sys.path[:] = [p for p in sys.path if Path(p or ".").resolve() not in (PLUGIN_ROOT, PLUGIN_ROOT.parent)]
sys.path.insert(0, str(Path(__file__).resolve().parent))  # tests share helpers via ``from conftest import``

TOKEN_ENV_VARS = ("COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN")


def load_plugin_package(name: str = "github_copilot_plugin"):
    """Execute the plugin's ``__init__`` under *name* (the guard runs, profiles register when active)."""
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(
        name, PLUGIN_ROOT / "__init__.py", submodule_search_locations=[str(PLUGIN_ROOT)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(name, None)
        raise
    return module


_RECORDERS: dict[int, list[tuple[str, str]]] = {}
_SPAWN_EVENTS = ("subprocess.Popen", "os.posix_spawn", "os.exec", "os.spawn", "os.system")


def _audit(event: str, args: tuple) -> None:
    events = _RECORDERS.get(threading.get_ident())
    if events is None:
        return
    if event == "open" and args and isinstance(args[0], (str, bytes, os.PathLike)):
        events.append(("open", os.fsdecode(args[0])))
    elif event in _SPAWN_EVENTS:
        events.append(("spawn", repr(args[:2])))


sys.addaudithook(_audit)  # cannot be removed; records only inside ``record_io()`` on that thread


@contextlib.contextmanager
def record_io():
    """Files opened and processes spawned by THIS thread inside the block (other threads' work, e.g. a
    previous test's lingering subprocess reader, never lands here)."""
    events: list[tuple[str, str]] = []
    _RECORDERS[threading.get_ident()] = events
    try:
        yield events
    finally:
        _RECORDERS.pop(threading.get_ident(), None)


class SentRequest(NamedTuple):
    headers: dict[str, str]
    body: dict


def offline_chat_turn(base_url: str, *, model: str = "gpt-5.4", message: str = "hi",
                      **agent_kwargs) -> list[SentRequest]:
    """Run one real ``AIAgent.chat`` turn against *base_url* with the HTTP transport replaced by a stand-in
    that answers every chat request with "ok"; return the chat requests as they went on the wire."""
    import json
    from unittest.mock import patch

    import httpx
    import openai

    from run_agent import AIAgent

    sent: list[SentRequest] = []

    def answer(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content or b"{}")
        if "messages" in body:
            sent.append(SentRequest({k.lower(): v for k, v in request.headers.items()}, body))
        delta = {"role": "assistant", "content": "ok"}
        if body.get("stream"):
            chunks = [{"choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
                      {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}]
            sse = "".join(
                "data: " + json.dumps({"id": "c", "object": "chat.completion.chunk", "created": 0,
                                       "model": model, **c}) + "\n\n" for c in chunks) + "data: [DONE]\n\n"
            return httpx.Response(200, text=sse, headers={"content-type": "text/event-stream"})
        return httpx.Response(200, json={
            "id": "c", "object": "chat.completion", "created": 0, "model": model,
            "choices": [{"index": 0, "message": delta, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}})

    def offline_openai(**kwargs):
        return openai.OpenAI(**{**kwargs, "http_client": httpx.Client(transport=httpx.MockTransport(answer))})

    options = {"api_key": "tid=offline", "provider": "copilot", "api_mode": "chat_completions",
               "quiet_mode": True, "skip_context_files": True, "skip_memory": True, "enabled_toolsets": [],
               **agent_kwargs}
    with patch("agent.process_bootstrap.OpenAI", side_effect=offline_openai):
        AIAgent(base_url=base_url, model=model, **options).chat(message)
    return sent


import providers  # noqa: E402

providers.get_provider_profile("openrouter")  # run discovery first: later registrations stay put
PLUGIN = load_plugin_package()


def pytest_configure(config):
    config.addinivalue_line("markers", "any_core: runs even when the plugin is inert on this core")


# Modules that import the plugin's profiles at import time. A core without the seams rejects the profile
# kwargs (``TypeError`` at collection), so on an inert core they are not collected at all.
_ANY_CORE_MODULES = frozenset({"conftest.py", "test_plugin_registration.py"})


def pytest_ignore_collect(collection_path, config):
    if not PLUGIN.ACTIVE and collection_path.suffix == ".py" and collection_path.name not in _ANY_CORE_MODULES:
        return True
    return None


def pytest_report_header(config):
    return None if PLUGIN.ACTIVE else f"github-copilot plugin inert: {PLUGIN.inert_reason()}; only any_core tests run"


def pytest_collection_modifyitems(config, items):
    if PLUGIN.ACTIVE:
        return
    skip = pytest.mark.skip(reason=f"plugin inert: {PLUGIN.inert_reason()} (needs hermes-agent#144788)")
    for item in items:
        if "any_core" not in item.keywords:
            item.add_marker(skip)


@pytest.fixture(autouse=True)
def hermes_home(tmp_path, monkeypatch):
    home = tmp_path / "hermes-home"
    home.mkdir()
    user_home = tmp_path / "home"
    user_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HOME", str(user_home))
    monkeypatch.setenv("USERPROFILE", str(user_home))
    for var in (*TOKEN_ENV_VARS, "COPILOT_API_BASE_URL", "COPILOT_ACP_BASE_URL", "HERMES_COPILOT_ACP_COMMAND",
                "COPILOT_CLI_PATH", "HERMES_COPILOT_ACP_ARGS", "HERMES_COPILOT_API_VERSION",
                "HERMES_SESSION_KEY", "HERMES_SESSION_ID",
                "HERMES_DELEGATED_CHILD_CONTEXT"):
        monkeypatch.delenv(var, raising=False)
    return home


@pytest.fixture(autouse=True)
def _reset_plugin_caches():
    """Token-exchange and catalog caches are process-wide; no test may see another's."""
    if not PLUGIN.ACTIVE:
        yield
        return
    from github_copilot_plugin.copilot import catalog, credentials

    def reset():
        credentials._jwt_cache.clear()
        credentials._exchange_failure_cache.clear()
        credentials._reset_raw_degradation_warned()
        catalog._github_model_catalog_cache = None
        catalog._github_model_catalog_cache_key = None
        catalog._github_model_catalog_cache_time = 0.0
        catalog._copilot_context_cache = {}
        catalog._copilot_context_cache_time = 0.0
        catalog._copilot_context_cache_key = None

    reset()
    yield
    reset()
