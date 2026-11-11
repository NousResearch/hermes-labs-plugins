"""Copilot selected at runtime exchanges the raw GitHub token held in its pool row
(``ProviderProfile.prepare_pool_entry``), moved from tests/hermes_cli/test_runtime_provider_resolution.py."""

from __future__ import annotations

import json

import providers as _providers
from hermes_cli import runtime_provider as rp

_providers.get_provider_profile("copilot")  # imports the copilot plugin package (credentials module)
from github_copilot_plugin.copilot import credentials as copilot_credentials
from github_copilot_plugin.copilot import wire as copilot_wire


def test_runtime_selected_copilot_exchanges_ambient_pool_token(tmp_path, monkeypatch):
    """Copilot picked at runtime without a config write (`/model copilot/<m> --session`,
    `--provider copilot`) must still hand the EXCHANGED token and the enterprise base_url to the
    client: the seeder leaves an ambient env token raw while copilot is not configured
    (#114740), and a raw token 400s on enterprise-only models."""
    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    (hermes_home / "auth.json").write_text(json.dumps({"version": 1, "credential_pool": {}}))
    (hermes_home / "config.yaml").write_text("model:\n  provider: deepseek\n  default: deepseek-chat\n")
    from hermes_cli import config as _cfg
    _cfg._LOAD_CONFIG_CACHE.clear()
    _cfg._RAW_CONFIG_CACHE.clear()
    monkeypatch.setattr(copilot_credentials, "resolve_copilot_token", lambda: ("ghu_raw_gh_token", "GH_TOKEN"))
    monkeypatch.setattr(copilot_credentials, "get_copilot_api_token",
                        lambda tok: ("tid=exchanged;exp=1", "https://api.enterprise.githubcopilot.com"))
    monkeypatch.setattr(copilot_wire, "model_api_mode", lambda *a, **k: "chat_completions")
    monkeypatch.setattr(rp, "resolve_provider", lambda *a, **k: "copilot")

    resolved = rp.resolve_runtime_provider(requested="copilot", target_model="gpt-4.1")

    assert resolved["provider"] == "copilot"
    assert resolved["api_key"] == "tid=exchanged;exp=1"
    assert resolved["base_url"] == "https://api.enterprise.githubcopilot.com"
    row = resolved["credential_pool"].entries()[0]
    assert row.access_token == "ghu_raw_gh_token"
    assert row.runtime_api_key == "tid=exchanged;exp=1"
