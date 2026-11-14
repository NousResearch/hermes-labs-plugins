"""Tests for ``CopilotProfile.fetch_account_usage`` (issue #36: GitHub Copilot quota in ``/usage``)."""

from __future__ import annotations

import json
import os
import stat
import time
import urllib.error
from pathlib import Path
from typing import Any

import pytest

from agent.account_usage import (
    fetch_account_usage,
    render_account_usage_lines,
)
from agent.account_usage_cache import has_account_usage
from conftest import record_io
from hermes_cli.subcommands.usage import usage_snapshot_document

RAW_TOKEN_1 = "ghu_accountOneRawToken00000000000001"
RAW_TOKEN_2 = "gho_accountTwoRawToken00000000000002"


def _stub_user_endpoint(monkeypatch, responder):
    """Replace ``usage.get_json`` with *responder(url, timeout, headers) -> payload* and record calls."""
    import providers

    providers.get_provider_profile("copilot")
    from github_copilot_plugin.copilot import usage as copilot_usage

    calls: list[dict[str, Any]] = []

    def fake_get_json(url: str, *, timeout: float, headers: dict[str, str] | None = None):
        calls.append({"url": url, "timeout": timeout, "headers": dict(headers or {})})
        return responder(url, timeout, headers or {})

    monkeypatch.setattr(copilot_usage, "get_json", fake_get_json)
    return calls


def test_copilot_advertises_account_usage_support():
    assert has_account_usage("copilot") is True
    assert has_account_usage("github-copilot") is True
    assert has_account_usage("copilot-acp") is False


def test_free_plan_quota_snapshots_renders_chat_and_completions_and_skips_inactive_premium(
    hermes_home, monkeypatch,
):
    monkeypatch.setenv("COPILOT_GITHUB_TOKEN", RAW_TOKEN_2)
    payload = {
        "access_type_sku": "free_limited_copilot",
        "quota_reset_date": "2026-12-01",
        "quota_snapshots": {
            "chat": {"entitlement": 200, "remaining": 150, "percent_remaining": 75.0, "unlimited": False},
            "completions": {"entitlement": 2000, "remaining": 1800, "percent_remaining": 90.0, "unlimited": False},
            "premium_interactions": {"has_quota": False, "entitlement": 0, "remaining": 0, "unlimited": False},
        },
    }
    calls = _stub_user_endpoint(monkeypatch, lambda *_a: payload)

    snapshot = fetch_account_usage("copilot")

    assert snapshot is not None and snapshot.available
    assert len(calls) == 1
    assert calls[0]["url"] == "https://api.github.com/copilot_internal/user"
    # Must send ``token <raw_gh_token>`` (never ``Bearer`` or an exchanged JWT).
    assert calls[0]["headers"]["Authorization"] == f"token {RAW_TOKEN_2}"
    assert "Editor-Version" in calls[0]["headers"]
    assert "X-GitHub-Api-Version" in calls[0]["headers"]

    assert snapshot.provider == "copilot"
    assert snapshot.plan == "Free"
    assert [(w.label, w.used_percent, w.scope, w.detail) for w in snapshot.windows] == [
        ("Chat", 25.0, "account", "150/200 remaining"),
        ("Completions", 10.0, "model", "1800/2000 remaining"),
    ]
    assert all(w.reset_at is not None and w.reset_at.year == 2026 and w.reset_at.month == 12 for w in snapshot.windows)
    assert "Quota remaining: Chat 150/200 • Completions 1800/2000" in snapshot.details

    rendered = "\n".join(render_account_usage_lines(snapshot))
    assert "Provider: copilot (Free)" in rendered
    assert "Chat: 75% remaining (25% used)" in rendered
    assert "Completions: 90% remaining (10% used)" in rendered
    assert "Premium requests" not in rendered

    doc = usage_snapshot_document(snapshot)
    assert doc["provider"] == "copilot"
    assert doc["plan"] == "Free"
    assert len(doc["windows"]) == 2


def test_paid_plan_renders_premium_requests_unlimited_allowances_and_overage(hermes_home, monkeypatch):
    monkeypatch.setenv("COPILOT_GITHUB_TOKEN", RAW_TOKEN_1)
    payload = {
        "copilot_plan": "pro",
        "access_type_sku": "monthly_subscriber_quota",
        "quota_reset_date_utc": "2026-12-01T00:00:00.000Z",
        "quota_snapshots": {
            "premium_interactions": {
                "entitlement": 300,
                "remaining": 125.7,
                "percent_remaining": 41.9,
                "unlimited": False,
                "overage_permitted": True,
                "overage_count": 2,
            },
            "chat": {"unlimited": True, "percent_remaining": 100.0},
            "completions": {"unlimited": True, "percent_remaining": 100.0},
        },
    }
    _stub_user_endpoint(monkeypatch, lambda *_a: payload)

    snapshot = fetch_account_usage("github-copilot")

    assert snapshot is not None
    assert snapshot.plan == "Pro"
    assert len(snapshot.windows) == 1
    win = snapshot.windows[0]
    assert win.label == "Premium requests"
    assert win.used_percent == pytest.approx(58.1)
    # Exhausting premium requests leaves 0x included models working, so scope is "model", not "account".
    assert win.scope == "model"
    assert win.detail == "125.7/300 remaining"
    assert snapshot.details == (
        "Quota remaining: Premium requests 125.7/300",
        "Unlimited: Chat, Completions",
        "Overage: enabled (2 used)",
    )


def test_legacy_limited_user_quotas_and_credits_used_fallbacks(hermes_home, monkeypatch):
    from github_copilot_plugin.copilot.usage import build_copilot_usage_snapshot

    legacy = build_copilot_usage_snapshot({
        "access_type_sku": "free_limited_copilot",
        "limited_user_reset_date": "2026-12-01",
        "monthly_quotas": {"chat": 50, "completions": 2000},
        "limited_user_quotas": {"chat": 38, "completions": 1500},
    })
    assert legacy is not None
    assert [(w.label, w.used_percent, w.detail) for w in legacy.windows] == [
        ("Chat", 24.0, "38/50 remaining"),
        ("Completions", 25.0, "1500/2000 remaining"),
    ]

    credits_based = build_copilot_usage_snapshot({
        "copilot_plan": "business",
        "quota_snapshots": {
            "premium_interactions": {"entitlement": 300, "credits_used": 75, "unlimited": False},
            "premium_zero": {"entitlement": 0, "remaining": 0, "percent_remaining": 0.0, "unlimited": False},
        },
    })
    assert credits_based is not None
    assert [(w.label, w.used_percent, w.detail) for w in credits_based.windows] == [
        ("Premium requests", 25.0, "225/300 remaining"),
    ]
    # Without reset_at, render_account_usage_lines already prints window.detail inline (no duplicate detail line).
    assert credits_based.details == ()


def test_exchanged_session_token_resolves_matching_pooled_account_only(hermes_home, monkeypatch):
    """A live session passes its EXCHANGED ``tid=...`` token as ``api_key``. In a multi-account pool
    the fetcher must query GitHub with THAT session's raw token, never a sibling account's."""
    from github_copilot_plugin.copilot import credentials

    (hermes_home / "auth.json").write_text(json.dumps({
        "version": 1,
        "credential_pool": {
            "copilot": [
                {"id": "acct1", "label": "work", "auth_type": "api_key", "priority": 0,
                 "source": "manual:device_code", "access_token": RAW_TOKEN_1},
                {"id": "acct2", "label": "personal", "auth_type": "api_key", "priority": 1,
                 "source": "manual:device_code", "access_token": RAW_TOKEN_2},
            ],
        },
    }))
    exchanged_2 = "tid=sessionAccountTwo;exp=9999999999"
    credentials._jwt_cache[credentials._token_fingerprint(RAW_TOKEN_2)] = (
        exchanged_2, time.time() + 1800, "https://api.individual.githubcopilot.com",
    )
    calls = _stub_user_endpoint(monkeypatch, lambda *_a: {
        "copilot_plan": "individual",
        "quota_snapshots": {"premium_interactions": {"entitlement": 300, "remaining": 240, "percent_remaining": 80.0}},
    })

    snap = fetch_account_usage(
        "copilot", base_url="https://api.individual.githubcopilot.com", api_key=exchanged_2,
    )
    assert snap is not None
    assert calls[0]["headers"]["Authorization"] == f"token {RAW_TOKEN_2}"

    # Unknown exchanged token in a multi-account pool fails open rather than guessing acct1.
    calls.clear()
    assert fetch_account_usage("copilot", api_key="tid=belongsToNeitherAccount") is None
    assert calls == []


def test_pool_only_login_resolves_non_dead_entry_without_env_vars(hermes_home, monkeypatch):
    """``hermes usage`` (``api_key=None``) works for ``hermes auth add copilot`` pool-only homes and skips dead rows."""
    (hermes_home / "auth.json").write_text(json.dumps({
        "version": 1,
        "credential_pool": {
            "copilot": [
                {"id": "dead1", "label": "revoked", "auth_type": "api_key", "priority": 0,
                 "source": "manual:device_code", "access_token": RAW_TOKEN_1, "last_status": "dead"},
                {"id": "live2", "label": "active", "auth_type": "api_key", "priority": 1,
                 "source": "manual:device_code", "access_token": RAW_TOKEN_2, "last_status": "ok"},
            ],
        },
    }))
    calls = _stub_user_endpoint(monkeypatch, lambda *_a: {
        "copilot_plan": "pro",
        "quota_snapshots": {"premium_interactions": {"entitlement": 300, "remaining": 300, "percent_remaining": 100.0}},
    })

    snap = fetch_account_usage("copilot")
    assert snap is not None
    assert calls[0]["headers"]["Authorization"] == f"token {RAW_TOKEN_2}"


def test_ambient_gh_token_and_github_models_never_call_user_endpoint(hermes_home, monkeypatch):
    monkeypatch.setenv("GH_TOKEN", RAW_TOKEN_2)
    (hermes_home / "config.yaml").write_text("model:\n  provider: nous\n  default: Hermes-4-405B\n")
    calls = _stub_user_endpoint(monkeypatch, lambda *_a: pytest.fail("should not query GitHub"))

    # Ambient GH_TOKEN in a home that never chose Copilot: silent None.
    assert fetch_account_usage("copilot") is None
    assert calls == []

    # Even with COPILOT_GITHUB_TOKEN set, GitHub Models base URLs do not share Copilot subscription quota.
    monkeypatch.setenv("COPILOT_GITHUB_TOKEN", RAW_TOKEN_1)
    assert fetch_account_usage("copilot", base_url="https://models.github.ai/inference", api_key=RAW_TOKEN_1) is None
    assert fetch_account_usage("copilot", base_url="https://models.inference.ai.azure.com", api_key=RAW_TOKEN_1) is None
    assert calls == []


def test_rule11_fetch_account_usage_never_reads_cli_token_stores(hermes_home, tmp_path, monkeypatch):
    home = Path(os.environ["HOME"])
    cli_token = "gho_cliOnlyToken00000000000000000"
    stores = {
        home / ".copilot" / "config.json": json.dumps({"copilotTokens": {"https://github.com:u": cli_token}}),
        home / ".config" / "github-copilot" / "hosts.json": json.dumps({"github.com": {"oauth_token": cli_token}}),
        home / ".config" / "gh" / "hosts.yml": f"github.com:\n    oauth_token: {cli_token}\n",
    }
    for path, body in stores.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    gh = bindir / ("gh.cmd" if os.name == "nt" else "gh")
    gh.write_text(f"@echo {cli_token}\n" if os.name == "nt" else f"#!/bin/sh\necho {cli_token}\n")
    gh.chmod(gh.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", str(bindir) + os.pathsep + os.environ.get("PATH", ""))
    (hermes_home / "config.yaml").write_text("model:\n  provider: copilot\n  default: gpt-5.4\n")

    import providers

    profile = providers.get_provider_profile("copilot")
    with record_io() as events:
        result = profile.fetch_account_usage()

    assert result is None
    store_paths = {str(p) for p in stores}
    assert [p for kind, p in events if kind == "open" and p in store_paths] == []
    assert [a for kind, a in events if kind == "spawn"] == []


def test_http_errors_and_malformed_payloads_fail_open(hermes_home, monkeypatch):
    monkeypatch.setenv("COPILOT_GITHUB_TOKEN", RAW_TOKEN_1)

    def raise_401(url, timeout, headers):
        raise urllib.error.HTTPError(url, 401, "Unauthorized", {}, None)

    _stub_user_endpoint(monkeypatch, raise_401)
    assert fetch_account_usage("copilot") is None

    _stub_user_endpoint(monkeypatch, lambda *_a: {
        "quota_snapshots": {
            "premium_interactions": {
                "entitlement": True,
                "remaining": float("nan"),
                "percent_remaining": float("inf"),
            },
        },
    })
    assert fetch_account_usage("copilot") is None
