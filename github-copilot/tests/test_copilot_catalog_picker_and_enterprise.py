"""Regression tests for Copilot /models catalog filtering and enterprise base_url routing.

Covers:
1. ``model_picker_enabled: false`` chat models (e.g. ``gpt-5.4-nano``, ``gpt-4.1``, ``gpt-4o``)
   remain visible behind primary ``model_picker_enabled: true`` models instead of vanishing whenever
   at least one primary model exists, while ``policy.state == "disabled"`` and non-chat rows stay out.
2. ``fetch_github_model_catalog``, ``probe_models``, ``get_copilot_model_context``, ``setup``, and
   ``runtime_base_url`` honor the account's exchanged ``endpoints.api`` / ``proxy-ep`` host (including
   ``api.enterprise.githubcopilot.com`` and ``copilot-api.<tenant>.ghe.com``) as well as
   ``COPILOT_API_BASE_URL``.
"""

from __future__ import annotations

import time
from unittest.mock import patch

import pytest

import providers

providers.get_provider_profile("copilot")
from github_copilot_plugin.copilot import catalog, credentials, responses, setup, wire  # noqa: E402
from github_copilot_plugin.copilot.activation import copilot_user_flow  # noqa: E402


class TestModelPickerEnabledAndPolicyFiltering:
    def test_retains_picker_disabled_chat_models_after_primary_models(self, monkeypatch):
        payload = {
            "data": [
                {
                    "id": "gpt-5.4",
                    "model_picker_enabled": True,
                    "capabilities": {"type": "chat", "limits": {"max_context_window_tokens": 400000}},
                    "supported_endpoints": ["/responses"],
                },
                {
                    "id": "gpt-5.4-nano",
                    "model_picker_enabled": False,
                    "capabilities": {"type": "chat", "limits": {"max_context_window_tokens": 400000}},
                    "supported_endpoints": ["/responses", "ws:/responses"],
                },
                {
                    "id": "claude-sonnet-4.6",
                    "model_picker_enabled": True,
                    "capabilities": {"type": "chat", "limits": {"max_context_window_tokens": 200000}},
                    "supported_endpoints": ["/chat/completions"],
                },
                {
                    "id": "gpt-4.1",
                    "model_picker_enabled": False,
                    "policy": {"state": "enabled"},
                    "capabilities": {"type": "chat", "limits": {"max_context_window_tokens": 128000}},
                    "supported_endpoints": [" /V1/responses/ "],
                },
                {
                    "id": "legacy-list-caps-model",
                    "model_picker_enabled": False,
                    "capabilities": ["chat", "reasoning"],
                    "supported_endpoints": ["/chat/completions"],
                },
                {
                    "id": "org-blocked-model",
                    "model_picker_enabled": True,
                    "policy": {"state": "disabled"},
                    "capabilities": {"type": "chat"},
                    "supported_endpoints": ["/chat/completions"],
                },
                {
                    "id": "org-blocked-hidden-model",
                    "model_picker_enabled": False,
                    "policy": {"state": "DISABLED"},
                    "capabilities": {"type": "chat"},
                    "supported_endpoints": ["/chat/completions"],
                },
                {
                    "id": "text-embedding-3-small",
                    "model_picker_enabled": False,
                    "capabilities": {"type": "embeddings"},
                },
                {
                    "id": "ws-only-model",
                    "model_picker_enabled": False,
                    "capabilities": {"type": "chat"},
                    "supported_endpoints": ["ws:/responses"],
                },
            ]
        }
        monkeypatch.setattr(catalog, "get_json", lambda url, *, timeout, headers=None: payload)

        rows = catalog.fetch_github_model_catalog(api_key="tid=test")
        assert [r["id"] for r in rows] == [
            "gpt-5.4",
            "claude-sonnet-4.6",
            "gpt-5.4-nano",
            "gpt-4.1",
            "legacy-list-caps-model",
        ]

        # Hidden picker models keep their live context windows and HTTP endpoint routing too,
        # including when a hidden model advertises an endpoint that differs from its offline default
        # and when a sibling row uses list-shaped legacy capabilities.
        assert catalog.get_copilot_model_context("gpt-5.4-nano", api_key="tid=test") == 400000
        assert catalog.get_copilot_model_context("gpt-4.1", api_key="tid=test") == 128000
        assert catalog.get_copilot_model_context("legacy-list-caps-model", api_key="tid=test") is None
        assert wire.model_api_mode("gpt-5.4-nano", api_key="tid=test") == "codex_responses"
        assert wire.model_api_mode("gpt-4.1", api_key="tid=test") == "codex_responses"

    def test_deduplicates_when_same_id_appears_twice(self, monkeypatch):
        payload = {
            "data": [
                {"id": "gpt-4.1", "model_picker_enabled": True, "capabilities": {"type": "chat"}},
                {"id": "gpt-4.1", "model_picker_enabled": False, "capabilities": {"type": "chat"}},
                {"id": "gpt-4o-mini", "model_picker_enabled": False, "capabilities": {"type": "chat"}},
            ]
        }
        monkeypatch.setattr(catalog, "get_json", lambda url, *, timeout, headers=None: payload)
        rows = catalog.fetch_github_model_catalog(api_key="tid=dedup")
        assert [r["id"] for r in rows] == ["gpt-4.1", "gpt-4o-mini"]


class TestEnterpriseCatalogAndBaseUrlRouting:
    def test_exchanged_enterprise_token_queries_enterprise_models_endpoint(self, monkeypatch):
        raw_token = "ghu_enterpriseToken000000000000000000"
        exchanged = "tid=ent123;exp=9999999999"
        ent_base = "https://api.enterprise.githubcopilot.com"
        fp = credentials._token_fingerprint(raw_token)
        credentials._jwt_cache[fp] = (exchanged, time.time() + 1800, ent_base)

        seen_urls: list[str] = []

        def fake_get_json(url, *, timeout, headers=None):
            seen_urls.append(url)
            return {"data": [{"id": "claude-opus-4.6", "capabilities": {"type": "chat"}}]}

        monkeypatch.setenv("COPILOT_GITHUB_TOKEN", raw_token)
        monkeypatch.setattr(catalog, "get_json", fake_get_json)

        assert catalog.copilot_picker_catalog() == ["claude-opus-4.6"]
        assert seen_urls == [f"{ent_base}/models"]

    def test_proxy_ep_token_derives_enterprise_models_url_without_memory_cache(self, monkeypatch):
        token = "tid=abc;proxy-ep=proxy.enterprise.githubcopilot.com;exp=9999999999"
        seen_urls: list[str] = []
        monkeypatch.setattr(
            catalog,
            "get_json",
            lambda url, *, timeout, headers=None: (
                seen_urls.append(url) or {"data": [{"id": "gpt-5.4", "capabilities": {"type": "chat"}}]}
            ),
        )

        rows = catalog.fetch_github_model_catalog(api_key=token)
        assert [r["id"] for r in rows] == ["gpt-5.4"]
        assert seen_urls == ["https://api.enterprise.githubcopilot.com/models"]

    def test_persisted_disk_jwt_supplies_ghec_base_url_after_restart(self, hermes_home, monkeypatch):
        raw_token = "ghu_ghecToken0000000000000000000000"
        exchanged = "tid=ghec_opaque_without_proxy_ep"
        ghec_base = "https://copilot-api.acme.ghe.com"
        fp = credentials._token_fingerprint(raw_token)
        credentials._save_jwt_to_disk(fp, exchanged, time.time() + 1800, ghec_base)
        credentials._jwt_cache.clear()

        seen_urls: list[str] = []
        monkeypatch.setattr(
            catalog,
            "get_json",
            lambda url, *, timeout, headers=None: (
                seen_urls.append(url) or {"data": [{"id": "gpt-5.4", "capabilities": {"type": "chat"}}]}
            ),
        )

        rows = catalog.fetch_github_model_catalog(api_key=exchanged)
        assert [r["id"] for r in rows] == ["gpt-5.4"]
        assert seen_urls == [f"{ghec_base}/models"]

    def test_copilot_api_base_url_override_wins_and_keys_cache(self, monkeypatch):
        seen_urls: list[str] = []
        monkeypatch.setattr(
            catalog,
            "get_json",
            lambda url, *, timeout, headers=None: (
                seen_urls.append(url)
                or {"data": [{"id": "enterprise-only" if "enterprise" in url else "public-only",
                              "capabilities": {"type": "chat"}}]}
            ),
        )

        public_rows = catalog.fetch_github_model_catalog(api_key="github_pat_same")
        assert [r["id"] for r in public_rows] == ["public-only"]

        monkeypatch.setenv("COPILOT_API_BASE_URL", "https://api.enterprise.githubcopilot.com/")
        ent_rows = catalog.fetch_github_model_catalog(api_key="github_pat_same")
        assert [r["id"] for r in ent_rows] == ["enterprise-only"]
        assert seen_urls == [
            "https://api.githubcopilot.com/models",
            "https://api.enterprise.githubcopilot.com/models",
        ]

    def test_invalidate_catalog_cache_and_force_refresh_drop_in_process_cache(self, monkeypatch):
        calls = 0

        def fake_get_json(url, *, timeout, headers=None):
            nonlocal calls
            calls += 1
            return {"data": [{"id": f"model-{calls}", "capabilities": {"type": "chat"}}]}

        monkeypatch.setattr(catalog, "get_json", fake_get_json)
        with copilot_user_flow(), patch.object(
            catalog, "_resolve_copilot_catalog_api_key_candidates", return_value=["tid=refresh"],
        ):
            assert catalog.copilot_picker_catalog() == ["model-1"]
            assert catalog.copilot_picker_catalog() == ["model-1"]
            assert catalog.copilot_picker_catalog(force_refresh=True) == ["model-2"]

            profile = providers.get_provider_profile("copilot")
            profile.invalidate_catalog_cache()
            assert catalog.copilot_picker_catalog() == ["model-3"]

    @pytest.mark.parametrize("url, expected_base", [
        ("https://api.enterprise.githubcopilot.com", "https://api.enterprise.githubcopilot.com"),
        ("https://copilot-api.acme.ghe.com", "https://copilot-api.acme.ghe.com"),
        ("https://models.github.ai/inference", "https://api.githubcopilot.com"),
    ])
    def test_probe_models_owns_enterprise_and_ghec_hosts_and_reports_target_url(
        self, monkeypatch, url, expected_base,
    ):
        seen_urls: list[str] = []
        monkeypatch.setattr(
            catalog,
            "get_json",
            lambda target, *, timeout, headers=None: (
                seen_urls.append(target) or {"data": [{"id": "gpt-5.4", "capabilities": {"type": "chat"}}]}
            ),
        )
        probed = catalog.CopilotCatalogMixin().probe_models(base_url=url, api_key="tid=probe")
        assert probed == (["gpt-5.4"], f"{expected_base}/models", expected_base)
        assert seen_urls == [f"{expected_base}/models"]

    def test_pool_only_enterprise_login_resolves_enterprise_runtime_base_url(
        self, hermes_home, monkeypatch,
    ):
        from agent.credential_pool import AUTH_TYPE_API_KEY, PooledCredential, load_pool
        from hermes_cli.auth import resolve_api_key_provider_credentials
        from hermes_cli import config, runtime_provider

        (hermes_home / "config.yaml").write_text("model:\n  provider: copilot\n  default: gpt-5.4\n")
        config._LOAD_CONFIG_CACHE.clear()
        config._RAW_CONFIG_CACHE.clear()
        load_pool("copilot").add_entry(PooledCredential(
            provider="copilot", id="entpool", label="ent", auth_type=AUTH_TYPE_API_KEY, priority=0,
            source="manual:device_code", access_token="ghu_poolEnterprise00000000000000",
            base_url="https://api.githubcopilot.com",
        ))
        monkeypatch.setattr(
            credentials,
            "get_copilot_api_token",
            lambda raw: ("tid=ent_pool", "https://api.enterprise.githubcopilot.com"),
        )
        monkeypatch.setattr(catalog, "fetch_github_model_catalog", lambda **kwargs: [])

        creds = resolve_api_key_provider_credentials("copilot")
        assert creds["api_key"] == "tid=ent_pool"
        assert creds["base_url"] == "https://api.enterprise.githubcopilot.com"

        runtime = runtime_provider.resolve_runtime_provider(requested="copilot", target_model="gpt-5.4")
        assert runtime["api_key"] == "tid=ent_pool"
        assert runtime["base_url"] == "https://api.enterprise.githubcopilot.com"

        # Profile-wide COPILOT_API_BASE_URL overrides the pool row's endpoint too.
        monkeypatch.setenv("COPILOT_API_BASE_URL", "https://copilot-api.acme.ghe.com/")
        runtime_override = runtime_provider.resolve_runtime_provider(requested="copilot", target_model="gpt-5.4")
        assert runtime_override["base_url"] == "https://copilot-api.acme.ghe.com"

    def test_setup_flow_overwrites_stale_previous_provider_base_url_with_enterprise_endpoint(
        self, hermes_home, monkeypatch,
    ):
        from hermes_cli import config
        from hermes_cli.config import read_user_config_raw, save_config

        # Seed a stale base_url from a previous provider so setup migration must overwrite it.
        save_config({"model": {"provider": "custom", "default": "old-model",
                               "base_url": "https://old-relay.example.test/v1"}})
        config._LOAD_CONFIG_CACHE.clear()
        config._RAW_CONFIG_CACHE.clear()

        raw_token = "ghu_setupEntToken000000000000000000"
        exchanged = "tid=setup_ent"
        ent_base = "https://api.enterprise.githubcopilot.com"
        fp = credentials._token_fingerprint(raw_token)
        credentials._jwt_cache[fp] = (exchanged, time.time() + 1800, ent_base)

        monkeypatch.setenv("COPILOT_GITHUB_TOKEN", raw_token)
        monkeypatch.setattr(
            credentials, "get_copilot_api_token", lambda raw: (exchanged, ent_base),
        )
        seen_urls: list[str] = []
        monkeypatch.setattr(
            catalog,
            "get_json",
            lambda url, *, timeout, headers=None: (
                seen_urls.append(url)
                or {"data": [{"id": "gpt-5.4-nano", "model_picker_enabled": False,
                              "capabilities": {"type": "chat"}, "supported_endpoints": ["/responses"]}]}
            ),
        )
        captured_confirm: dict = {}

        def fake_pick(models, prompt, **kwargs):
            captured_confirm.update(kwargs)
            assert models == ["gpt-5.4-nano"]
            return "gpt-5.4-nano"

        monkeypatch.setattr(setup, "pick_model_or_prompt", fake_pick)
        setup.model_flow_copilot({}, current_model="")

        assert seen_urls == [f"{ent_base}/models"]
        assert captured_confirm["confirm_base_url"] == ent_base
        saved_model = read_user_config_raw().get("model") or {}
        assert saved_model.get("provider") == "copilot"
        assert saved_model.get("default") == "gpt-5.4-nano"
        assert saved_model.get("api_mode") == "codex_responses"
        assert saved_model.get("base_url") == ent_base

    def test_setup_raw_model_ids_fallback_uses_enterprise_url_and_editor_headers(self, monkeypatch):
        token = "github_pat_enterprise_test"
        monkeypatch.setenv("COPILOT_API_BASE_URL", "https://copilot-api.acme.ghe.com")
        seen: list[tuple[str, dict]] = []
        monkeypatch.setattr(
            setup,
            "get_json",
            lambda url, *, timeout, headers=None: (
                seen.append((url, dict(headers or {})))
                or {"data": [
                    {"id": "custom-ent-model"},
                    {"id": "disabled-ent-model", "policy": {"state": "disabled"}},
                ]}
            ),
        )

        assert setup._raw_model_ids(token) == ["custom-ent-model"]
        assert seen[0][0] == "https://copilot-api.acme.ghe.com/models"
        assert seen[0][1]["Copilot-Integration-Id"] == "copilot-developer-cli"
        assert seen[0][1]["Authorization"] == f"Bearer {token}"

    def test_untrusted_or_non_https_endpoints_are_ignored_for_automatic_catalog_routing(self, monkeypatch):
        evil_token = "tid=abc;proxy-ep=evil.example.test;exp=9999999999"
        http_ghec = "http://copilot-api.acme.ghe.com"
        assert credentials.exchanged_base_url_for_token(evil_token) is None
        assert catalog.copilot_catalog_models_url(evil_token) == catalog.COPILOT_MODELS_URL
        assert catalog.copilot_catalog_models_url("tid=ok", base_url=http_ghec) == catalog.COPILOT_MODELS_URL
        assert catalog.copilot_catalog_models_url("tid=ok", base_url="https://evil.example.test") == (
            catalog.COPILOT_MODELS_URL
        )

    def test_runtime_base_url_does_not_cross_contaminate_unmatched_caller_token(
        self, hermes_home, monkeypatch,
    ):
        (hermes_home / "config.yaml").write_text("model:\n  provider: copilot\n  default: gpt-5.4\n")
        monkeypatch.setenv("COPILOT_GITHUB_TOKEN", "ghu_envEnterprise0000000000000000")
        monkeypatch.setattr(
            credentials,
            "get_copilot_api_token",
            lambda raw: ("tid=env_ent", "https://api.enterprise.githubcopilot.com"),
        )
        profile = providers.get_provider_profile("copilot")
        # Another account's opaque token must not inherit the env account's enterprise endpoint.
        assert profile.runtime_base_url(
            api_key="tid=other_pooled_individual", default="https://api.githubcopilot.com", env_url="",
        ) == "https://api.githubcopilot.com"
        # Matching env account token does receive its enterprise endpoint.
        assert profile.runtime_base_url(
            api_key="tid=env_ent", default="https://api.githubcopilot.com", env_url="",
        ) == "https://api.enterprise.githubcopilot.com"

    def test_ghec_wire_and_responses_hooks_match_copilot_api_ghe_host(self):
        ghec_url = "https://copilot-api.acme.ghe.com"
        evil_url = "https://copilot-api.acme.ghe.com.evil.test"

        w = wire.CopilotWireMixin()
        assert w.max_tokens_param(base_url=ghec_url, model="gpt-4.1") == "max_completion_tokens"
        assert w.max_tokens_param(base_url=evil_url, model="gpt-4.1") is None

        r = responses.CopilotResponsesMixin()
        carriers = {"reasoning_opaque": "blob", "reasoning_text": "thought"}
        assert r.replay_message_carriers(carriers, model="claude-sonnet-4.6", base_url=ghec_url) == carriers
        assert r.replay_message_carriers(carriers, model="claude-sonnet-4.6", base_url=evil_url) == {}
        assert r.replays_function_thought_signature(model="gemini-3.1-pro-preview", base_url=ghec_url) is True
        assert r.replays_function_thought_signature(model="gemini-3.1-pro-preview", base_url=evil_url) is False
