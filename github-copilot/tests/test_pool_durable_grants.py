"""Selected Copilot credentials must not replace the durable GitHub grant."""

import json
from io import BytesIO

import pytest

from agent.credential_pool import load_pool
from hermes_cli import config as cfg, runtime_provider
from github_copilot_plugin.copilot import credentials, wire


GRANT = "ghu_fake_pool_grant"
BEARER = "tid=fake_pool_bearer"
URL = "https://api.business.githubcopilot.com"


def test_configured_env_pool_seeds_raw_without_exchange(hermes_home, monkeypatch):
    (hermes_home / "config.yaml").write_text("model:\n  provider: copilot\n  default: gpt-4.1\n")
    cfg._LOAD_CONFIG_CACHE.clear()
    cfg._RAW_CONFIG_CACHE.clear()
    monkeypatch.setenv("COPILOT_GITHUB_TOKEN", GRANT)
    calls = []
    monkeypatch.setattr(credentials, "get_copilot_api_token", lambda token: (calls.append(token) or BEARER, URL))

    pool = load_pool("copilot")

    assert calls == []
    assert pool.entries()[0].access_token == GRANT


def test_selected_manual_row_stays_raw_after_persist_and_reload(hermes_home, monkeypatch):
    (hermes_home / "config.yaml").write_text("model:\n  provider: copilot\n  default: gpt-4.1\n")
    (hermes_home / "auth.json").write_text(json.dumps({"version": 1, "credential_pool": {"copilot": [{
        "id": "manual-one", "label": "first", "auth_type": "api_key", "priority": 0,
        "source": "manual:device_code", "access_token": GRANT,
        "base_url": "https://api.githubcopilot.com"}]}}))
    cfg._LOAD_CONFIG_CACHE.clear()
    cfg._RAW_CONFIG_CACHE.clear()
    monkeypatch.setattr(credentials, "get_copilot_api_token", lambda token: (BEARER, URL))
    monkeypatch.setattr(wire, "model_api_mode", lambda *a, **k: "chat_completions")

    resolved = runtime_provider.resolve_runtime_provider(requested="copilot", target_model="gpt-4.1")
    assert resolved["api_key"] == BEARER
    assert resolved["base_url"] == URL
    pool = resolved["credential_pool"]
    pool._persist()
    reloaded = load_pool("copilot").entries()[0]
    assert (reloaded.id, reloaded.label, reloaded.access_token) == ("manual-one", "first", GRANT)


@pytest.mark.parametrize("reason", ["unauthorized", "stale", "auxiliary_unauthorized"])
def test_failed_manual_row_refresh_exchanges_only_that_grant(hermes_home, monkeypatch, reason):
    rows = [
        {"id": "first", "label": "First", "priority": 0, "source": "manual", "auth_type": "api_key",
         "access_token": "ghu_fake_other", "base_url": "https://api.githubcopilot.com"},
        {"id": "failed", "label": "Failed", "priority": 1, "source": "manual", "auth_type": "api_key",
         "access_token": GRANT, "base_url": "https://api.githubcopilot.com"},
    ]
    (hermes_home / "auth.json").write_text(json.dumps({"version": 1, "credential_pool": {"copilot": rows}}))
    calls = []

    def exchange(req, timeout):
        calls.append(req.get_header("Authorization"))
        return BytesIO(json.dumps({"token": BEARER, "expires_at": 4102444800,
                                   "endpoints": {"api": URL}}).encode())

    monkeypatch.setattr(credentials, "_urlopen_bounded", exchange)
    pool = load_pool("copilot")
    monkeypatch.setenv("GH_TOKEN", "ghu_fake_ambient")
    fp = credentials._token_fingerprint(GRANT)
    other_fp = credentials._token_fingerprint("ghu_fake_other")
    stale = ("tid=revoked", 4102444800, URL)
    credentials._jwt_cache.update({fp: stale, other_fp: stale})
    credentials._save_jwt_to_disk(fp, *stale)
    credentials._save_jwt_to_disk(other_fp, *stale)
    credentials._exchange_failure_cache.update({fp: 4102444800, other_fp: 4102444800})
    refreshed = pool.try_refresh_runtime_matching(
        api_key_hint=GRANT, credential_id="failed", reason=reason)

    assert calls == [f"token {GRANT}"]
    assert refreshed is not None
    assert refreshed.runtime_api_key == BEARER
    assert refreshed.runtime_base_url == URL
    assert credentials._jwt_cache[other_fp] == stale
    assert credentials._load_jwt_from_disk(other_fp) == stale
    assert credentials._exchange_failure_cache[other_fp] == 4102444800
    assert fp not in credentials._exchange_failure_cache
    pool._persist()
    stored = json.loads((hermes_home / "auth.json").read_text())["credential_pool"]["copilot"]
    assert next(row for row in stored if row["id"] == "failed")["access_token"] == GRANT


def test_forced_renewal_failure_never_uses_ambient_or_logs_grants(hermes_home, monkeypatch, caplog):
    import logging
    from agent.credential_pool import PooledCredential, CredentialPool

    row = PooledCredential(id="failed", label="Failed", priority=0,
                           provider="copilot", auth_type="api_key",
                           source="manual", access_token=GRANT,
                           base_url="https://api.githubcopilot.com")
    pool = CredentialPool("copilot", [row])
    monkeypatch.setenv("COPILOT_GITHUB_TOKEN", "ghu_fake_ambient")
    calls = []

    def reject(req, timeout):
        calls.append(req.get_header("Authorization"))
        raise RuntimeError(f"transport accidentally echoed {GRANT}")

    monkeypatch.setattr(credentials, "_urlopen_bounded", reject)
    monkeypatch.setattr(credentials.time, "sleep", lambda seconds: None)
    with caplog.at_level(logging.DEBUG):
        refreshed = pool.try_refresh_runtime_matching(
            credential_id=row.id, api_key_hint=GRANT, reason="stale")

    assert calls == [f"token {GRANT}"] * credentials._EXCHANGE_MAX_ATTEMPTS
    assert refreshed is None
    assert row.runtime_api_key == GRANT
    assert GRANT not in caplog.text
    assert "ghu_fake_ambient" not in caplog.text


@pytest.mark.parametrize("endpoint", [
    "https://api.githubcopilot.com",
    "https://api.individual.githubcopilot.com",
    "https://api.business.githubcopilot.com",
    "https://api.enterprise.githubcopilot.com",
])
def test_selected_pair_is_transient_for_every_official_origin(monkeypatch, endpoint):
    from agent.credential_pool import PooledCredential, CredentialPool
    from providers import get_provider_profile
    from providers.base import PoolRuntimeCredential

    row = PooledCredential(id="one", label="One", priority=0, provider="copilot",
                           auth_type="api_key", source="manual", access_token=GRANT,
                           base_url="https://api.githubcopilot.com")
    before = row.to_dict()
    monkeypatch.setattr(credentials, "get_copilot_api_token", lambda token: (BEARER, endpoint))
    profile = get_provider_profile("copilot")
    result = profile.prepare_pool_entry(row, GRANT)
    assert isinstance(result, PoolRuntimeCredential)
    assert (result.api_key, result.base_url) == (BEARER, endpoint)
    assert row.to_dict() == before
    prepared = CredentialPool("copilot", [row]).prepare_runtime_entry(row)
    assert (prepared.runtime_api_key, prepared.runtime_base_url) == (BEARER, endpoint)
    assert row.to_dict() == before


@pytest.mark.parametrize("endpoint", [
    "https://api.enterprise.ghe.example",
    "https://api.githubcopilot.com.evil.example",
    "http://api.githubcopilot.com",
    "https://api.githubcopilot.com/relay",
    "https://api.githubcopilot.com:8443",
])
def test_foreign_exchange_route_cannot_retarget_pool(monkeypatch, endpoint):
    from agent.credential_pool import PooledCredential, CredentialPool

    row = PooledCredential(id="one", label="One", priority=0, provider="copilot",
                           auth_type="api_key", source="manual", access_token=GRANT,
                           base_url="https://api.githubcopilot.com")
    before = row.to_dict()
    monkeypatch.setattr(credentials, "get_copilot_api_token", lambda token: (BEARER, endpoint))
    assert CredentialPool("copilot", [row]).prepare_runtime_entry(row) is None
    assert row.to_dict() == before


def test_normal_preparation_keeps_same_account_raw_fallback(monkeypatch, caplog):
    from agent.credential_pool import PooledCredential, CredentialPool

    row = PooledCredential(id="one", label="One", priority=0, provider="copilot",
                           auth_type="api_key", source="manual", access_token=GRANT,
                           base_url="https://api.githubcopilot.com")
    calls = []
    monkeypatch.setenv("GH_TOKEN", "ghu_fake_ambient")
    monkeypatch.setattr(credentials, "get_copilot_api_token", lambda token: (calls.append(token) or token, None))
    prepared = CredentialPool("copilot", [row]).prepare_runtime_entry(row)
    assert calls == [GRANT]
    assert prepared.runtime_api_key == GRANT
    assert prepared.access_token == GRANT
    assert "degraded to RAW token" in caplog.text


def test_forced_renewal_declines_exchange_that_returns_same_raw_grant(monkeypatch):
    from types import SimpleNamespace
    from providers import get_provider_profile

    monkeypatch.setattr(credentials, "evict_cached_exchanged_token", lambda token: None)
    monkeypatch.setattr(credentials, "exchange_copilot_token", lambda token: (token, 4102444800, None))
    result = get_provider_profile("copilot").refresh_pool_runtime_credentials(
        SimpleNamespace(access_token=GRANT), reason="unauthorized")
    assert result is None
