"""#61: a pool-only login (``hermes auth add copilot``) is what core's api-key resolver returns
for copilot, and an unsupported classic PAT in GITHUB_TOKEN never shadows it."""

import pytest

import providers


def _add_pool_row(token):
    from agent.credential_pool import AUTH_TYPE_API_KEY, PooledCredential, load_pool

    load_pool("copilot").add_entry(PooledCredential(
        provider="copilot", id="pool61", label="dev", auth_type=AUTH_TYPE_API_KEY, priority=0,
        source="manual:device_code", access_token=token, base_url="https://api.githubcopilot.com"))


@pytest.mark.parametrize("classic_pat", [None, "ghp_classicPATxxxxxxxxxxxxxxxxxxxxxxxx"])
def test_pool_only_login_resolves_through_core(monkeypatch, hermes_home, classic_pat):
    if classic_pat:
        monkeypatch.setenv("GITHUB_TOKEN", classic_pat)
    providers.get_provider_profile("copilot")
    from github_copilot_plugin.copilot import credentials

    # A pool login means Copilot was chosen, so the row's raw token is exchanged.
    _add_pool_row("ghu_poolOnlyTokenXXXXXXXXXXXXXXXX")
    seen = []
    monkeypatch.setattr(credentials, "get_copilot_api_token", lambda raw: (seen.append(raw), ("tid=exchanged", None))[1])
    from hermes_cli.auth import resolve_api_key_provider_credentials

    creds = resolve_api_key_provider_credentials("copilot")
    assert creds["api_key"] == "tid=exchanged"
    assert creds["source"] == "credential_pool:copilot"
    assert seen == ["ghu_poolOnlyTokenXXXXXXXXXXXXXXXX"]


def test_no_env_and_no_pool_is_still_no_key(hermes_home):
    copilot = providers.get_provider_profile("copilot")
    assert copilot.resolve_api_key_secret() == ("", "")
