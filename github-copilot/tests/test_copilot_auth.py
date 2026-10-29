"""Tests for github_copilot_plugin.copilot.credentials — Copilot token validation and resolution."""

import pytest
import providers as _providers
from conftest import record_io
_providers.get_provider_profile("copilot")  # imports the copilot plugin package (credentials module)

class TestTokenValidation:
    """Token type validation."""

    def test_classic_pat_rejected(self):
        from github_copilot_plugin.copilot.credentials import validate_copilot_token
        valid, _msg = validate_copilot_token("ghp_abcdefghijklmnop1234")
        assert valid is False

    @pytest.mark.parametrize("token", ["gho_abcdefghijklmnop1234", "github_pat_abcdefghijklmnop1234", "ghu_abcdefghijklmnop1234"])
    def test_supported_token_families_accepted(self, token):
        from github_copilot_plugin.copilot.credentials import validate_copilot_token
        assert validate_copilot_token(token) == (True, "OK")

    def test_arbitrary_string_rejected(self):
        """A non-GitHub value in GITHUB_TOKEN must fail validation instead of reaching the API (#12650)."""
        from github_copilot_plugin.copilot.credentials import validate_copilot_token
        valid, _msg = validate_copilot_token("not_a_github_token")
        assert valid is False


class TestResolveToken:
    """Token resolution with env var priority."""


    def test_gh_token_second_priority(self, monkeypatch):
        from github_copilot_plugin.copilot.credentials import resolve_copilot_token
        monkeypatch.delenv("COPILOT_GITHUB_TOKEN", raising=False)
        monkeypatch.setenv("GH_TOKEN", "gho_gh_second")
        monkeypatch.setenv("GITHUB_TOKEN", "gho_github_third")
        token, source = resolve_copilot_token()
        assert token == "gho_gh_second"
        assert source == "GH_TOKEN"


    def test_invalid_env_var_resolves_nothing_and_runs_no_subprocess(self, monkeypatch):
        """An env var holding an unsupported classic PAT resolves to nothing, and nothing else is
        tried: the ``gh auth token`` fallback is gone (catalog rule 11; it also cost up to 5s on
        Windows cold starts, #60800)."""
        from github_copilot_plugin.copilot.credentials import resolve_copilot_token
        monkeypatch.delenv("COPILOT_GITHUB_TOKEN", raising=False)
        monkeypatch.delenv("GH_TOKEN", raising=False)
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_classic_pat_nope")
        with record_io() as events:
            token, source = resolve_copilot_token()
        assert token == ""
        assert source == ""
        assert [e for e in events if e[0] == "spawn"] == []

    def test_all_env_vars_invalid_resolves_nothing_and_runs_no_subprocess(self, monkeypatch):
        """All three env vars set to classic PATs → nothing, and no gh CLI call."""
        from github_copilot_plugin.copilot.credentials import resolve_copilot_token
        monkeypatch.setenv("COPILOT_GITHUB_TOKEN", "ghp_one")
        monkeypatch.setenv("GH_TOKEN", "ghp_two")
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_three")
        with record_io() as events:
            token, source = resolve_copilot_token()
        assert token == ""
        assert source == ""
        assert [e for e in events if e[0] == "spawn"] == []


class TestRequestHeaders:
    """Copilot API header generation."""

    def test_default_headers_include_openai_intent(self):
        from github_copilot_plugin.copilot.credentials import copilot_request_headers
        headers = copilot_request_headers()
        assert headers["Openai-Intent"] == "conversation-edits"
        assert headers["User-Agent"] == "HermesAgent/1.0"
        assert "Editor-Version" in headers


    def test_no_vision_header_by_default(self):
        from github_copilot_plugin.copilot.credentials import copilot_request_headers
        headers = copilot_request_headers()
        assert "Copilot-Vision-Request" not in headers

    def test_exchanged_token_keeps_vscode_attribution(self):
        from github_copilot_plugin.copilot.credentials import copilot_request_headers
        headers = copilot_request_headers(token="tid=abc;exp=1")
        assert headers["Copilot-Integration-Id"] == "vscode-chat"
        assert headers["Editor-Version"].startswith("vscode/")

    def test_fine_grained_pat_claims_the_cli_attribution(self):
        """The VS Code ids answer a fine-grained PAT with 400 "Personal Access Tokens are not
        supported for this endpoint"; the CLI identity is the one the API serves it under."""
        from github_copilot_plugin.copilot.credentials import copilot_request_headers
        headers = copilot_request_headers(token="github_pat_abc")
        assert headers["Copilot-Integration-Id"] == "copilot-developer-cli"
        assert headers["Editor-Version"].startswith("copilot/")
        assert headers["x-initiator"] == "agent"

    def test_env_token_selects_the_attribution_without_a_token_argument(self, monkeypatch):
        from github_copilot_plugin.copilot.credentials import copilot_request_headers
        monkeypatch.setenv("COPILOT_GITHUB_TOKEN", "github_pat_env")
        assert copilot_request_headers()["Copilot-Integration-Id"] == "copilot-developer-cli"


class TestCopilotDefaultHeaders:
    """The catalog module's copilot_default_headers uses copilot_credentials."""


    def test_param_passthrough_both_values(self):
        """is_agent_turn param correctly maps to x-initiator for both True and False."""
        from github_copilot_plugin.copilot.catalog import copilot_default_headers
        for is_agent, expected in [(True, "agent"), (False, "user")]:
            headers = copilot_default_headers(is_agent_turn=is_agent)
            assert headers["x-initiator"] == expected, (
                f"is_agent_turn={is_agent} should produce x-initiator={expected!r}, "
                f"got {headers['x-initiator']!r}"
            )


class TestEnvVarOrder:
    """PROVIDER_REGISTRY has correct env var order."""

    def test_copilot_env_vars_include_copilot_github_token(self):
        from hermes_cli.auth import PROVIDER_REGISTRY
        copilot = PROVIDER_REGISTRY["copilot"]
        assert "COPILOT_GITHUB_TOKEN" in copilot.api_key_env_vars
        # COPILOT_GITHUB_TOKEN should be first
        assert copilot.api_key_env_vars[0] == "COPILOT_GITHUB_TOKEN"

