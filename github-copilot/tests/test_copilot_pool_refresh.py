"""#87: mid-turn refresh must stay on the selected credential-pool account."""

import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from copilot import credentials as c


def test_runtime_token_prefers_pinned_pool_entry(monkeypatch):
    monkeypatch.setattr(c, "_active_pool_raw_token", "ghu_pool")
    monkeypatch.setattr(c, "resolve_copilot_token", lambda: ("ghu_env", "env"))
    assert c.resolve_copilot_runtime_token() == ("ghu_pool", "credential_pool")


def test_runtime_token_falls_back_to_env(monkeypatch):
    monkeypatch.setattr(c, "_active_pool_raw_token", "")
    monkeypatch.setattr(c, "resolve_copilot_token", lambda: ("ghu_env", "env"))
    assert c.resolve_copilot_runtime_token() == ("ghu_env", "env")


def test_prepare_pool_entry_pins_the_raw_token(monkeypatch):
    monkeypatch.setattr(c, "_active_pool_raw_token", "")
    monkeypatch.setattr(c, "validate_copilot_token", lambda t: (True, "OK"))
    monkeypatch.setattr(c, "get_copilot_api_token", lambda t: (t + "_exchanged", None))
    entry = SimpleNamespace(access_token="", base_url="")
    out = c.CopilotCredentialsMixin().prepare_pool_entry(entry, "ghu_pool")
    assert out == "ghu_pool_exchanged"
    assert c._active_pool_raw_token == "ghu_pool"
    assert entry.access_token == "ghu_pool_exchanged"


def test_refresh_uses_pinned_token(monkeypatch):
    monkeypatch.setattr(c, "_active_pool_raw_token", "ghu_pool")
    monkeypatch.setattr(c, "resolve_copilot_token", lambda: ("ghu_env", "env"))
    seen = []
    monkeypatch.setattr(c, "evict_cached_exchanged_token", lambda t: seen.append(t))
    monkeypatch.setattr(c, "get_copilot_api_token", lambda t: (t + "_new", None))
    out = c._refresh_exchanged_token()
    assert seen == ["ghu_pool"]
    assert out[0] == "ghu_pool_new"
