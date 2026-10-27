"""Copilot endpoint identity, owned by the copilot plugin's profile hooks.

The ``x-initiator: user`` header (issue #3040) goes only to api.githubcopilot.com and GitHub Models
(the copilot profile's ``request_headers``), the hosts Hermes core matched before Copilot left it, and
host-based routing must match URL *hostnames*, never substrings (``providers.profile_for_host``): a
proxy path or a lookalike domain that merely contains a Copilot host is not Copilot.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def copilot_profile():
    import providers

    profile = providers.get_provider_profile("copilot")
    assert profile is not None
    return profile


def _marks_user_turn(profile, url: str) -> bool:
    return "x-initiator" in profile.request_headers(base_url=url, user_initiated=True)


def _is_copilot_host(url: str) -> bool:
    from providers import profile_for_host

    profile = profile_for_host(url)
    return profile is not None and profile.name == "copilot"


class TestInitiatorHosts:
    """The x-initiator hook targets GitHub Copilot endpoints only."""

    def test_standard_copilot_url(self, copilot_profile):
        assert _marks_user_turn(copilot_profile, "https://api.githubcopilot.com") is True

    def test_github_models_url(self, copilot_profile):
        assert _marks_user_turn(copilot_profile, "https://models.github.ai/inference") is True

    def test_openrouter_url(self, copilot_profile):
        assert _marks_user_turn(copilot_profile, "https://openrouter.ai/api/v1") is False

    @pytest.mark.parametrize("plan", ["individual", "business", "enterprise"])
    def test_plan_hosts_from_the_token_exchange(self, copilot_profile, plan):
        # The exchange's ``endpoints.api`` is api.<plan>.githubcopilot.com, the host real accounts hit.
        # By design they keep the client default ``agent``; see README § Premium requests.
        assert _marks_user_turn(copilot_profile, f"https://api.{plan}.githubcopilot.com") is False

    @pytest.mark.parametrize("url", [
        "https://api.githubcopilot.com.evil.test/v1", "https://evil.test/api.individual.githubcopilot.com",
        "https://notgithubcopilot.com/v1",
    ])
    def test_lookalike_hosts(self, copilot_profile, url):
        assert _marks_user_turn(copilot_profile, url) is False


class TestTurnAttribution:
    """Through core's endpoint routing (``agent.provider_route_hooks.route_answer``, what the turn loop
    asks per API call). On api.githubcopilot.com the first call of a user turn says ``user`` and
    follow-ups send nothing extra, so the client's default ``x-initiator: agent`` stands. On the plan
    hosts no call says ``user``, by design (as in Hermes core); see README § Premium requests."""

    @staticmethod
    def _headers(provider, url, *, user_initiated):
        from agent.provider_route_hooks import route_answer

        return route_answer("request_headers", provider=provider, base_url=url, model="gpt-5.4",
                            user_initiated=user_initiated)

    @pytest.mark.parametrize("provider", ["copilot", "custom"])
    @pytest.mark.parametrize("url, first_call", [
        ("https://api.githubcopilot.com", {"x-initiator": "user"}),
        ("https://api.individual.githubcopilot.com", None),
        ("https://api.business.githubcopilot.com", None),
    ])
    def test_first_call_and_follow_up(self, provider, url, first_call):
        from agent.provider_route_hooks import route_answer

        assert (self._headers(provider, url, user_initiated=True) or None) == first_call
        assert not self._headers(provider, url, user_initiated=False)
        client = route_answer("client_headers", provider=provider, base_url=url, api_key="tid=x")
        assert client["x-initiator"] == "agent"


@pytest.mark.parametrize("url, initiator", [
    ("https://api.githubcopilot.com", "user"),
    ("https://api.individual.githubcopilot.com", "agent"),
])
def test_x_initiator_on_the_wire_of_a_user_turn(url, initiator):
    """What a real ``AIAgent.chat`` turn sends: ``user`` on api.githubcopilot.com, the client default
    ``agent`` on a plan host, by design (as in Hermes core); see README § Premium requests. Every
    request also carries the pinned Copilot API version header (#46)."""
    from conftest import offline_chat_turn

    requests = offline_chat_turn(url, model="gpt-4.1")
    assert requests, "the turn sent no chat request"
    assert requests[0].headers.get("x-initiator") == initiator
    assert requests[0].headers.get("copilot-integration-id") == "vscode-chat"
    assert requests[0].headers.get("x-github-api-version") == "2026-08-01"


@pytest.mark.parametrize("url, owned", [
    ("https://api.githubcopilot.com", True),
    ("https://api.business.githubcopilot.com", True),
    ("https://models.github.ai/inference", True),
    ("https://models.inference.ai.azure.com", True),
    ("https://api.githubcopilot.com.evil.test", False),
    ("https://evil.test/api.githubcopilot.com", False),
])
def test_models_probe_owns_the_copilot_host_family(url, owned, monkeypatch):
    """``/models`` probes (``hermes model`` validation) of a Copilot host read the account catalog with
    the editor headers; anything else falls back to core's generic probe."""
    from github_copilot_plugin.copilot import catalog

    monkeypatch.setattr(catalog, "_fetch_github_models", lambda api_key=None, timeout=5.0: ["gpt-5.4"])
    answer = catalog.CopilotCatalogMixin().probe_models(base_url=url, api_key="tid=x")
    assert (answer is not None) is owned
    if owned:
        assert answer[0] == ["gpt-5.4"]


def test_copilot_host_identity_is_host_anchored():
    assert _is_copilot_host("https://api.githubcopilot.com/v1") is True
    assert _is_copilot_host("https://proxy.test/api.githubcopilot.com/v1") is False
    assert _is_copilot_host("https://models.github.ai/inference") is True
    assert _is_copilot_host("https://models.github.ai.evil.com/v1") is False
