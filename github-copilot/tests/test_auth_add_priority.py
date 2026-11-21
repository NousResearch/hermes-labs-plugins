"""#87: each copilot account added by device login gets the next priority."""
from types import SimpleNamespace

import providers as _providers

_providers.get_provider_profile("copilot")
from github_copilot_plugin.copilot import setup as copilot_setup  # noqa: E402
from agent import credential_pool  # noqa: E402
from github_copilot_plugin.copilot import credentials as copilot_credentials  # noqa: E402
from github_copilot_plugin.copilot import catalog  # noqa: E402


class _Pool:
    def __init__(self, priorities):
        self._entries = [SimpleNamespace(priority=p) for p in priorities]
        self.added = []

    def entries(self):
        return self._entries

    def add_entry(self, entry):
        self.added.append(entry)
        self._entries.append(entry)


def _run(monkeypatch, priorities, *, label=""):
    pool = _Pool(priorities)
    monkeypatch.setattr(credential_pool, "load_pool", lambda _p: pool)
    monkeypatch.setattr(copilot_credentials, "copilot_device_code_login", lambda: "ghu_test")
    monkeypatch.setattr(copilot_credentials, "validate_copilot_token", lambda t: (True, ""))
    monkeypatch.setattr(catalog, "COPILOT_BASE_URL", "https://api.githubcopilot.com")
    monkeypatch.setattr(copilot_setup, "_has_terminal", lambda: True)
    args = SimpleNamespace(api_key=None, auth_type="", label=label)
    assert copilot_setup.auth_handler("add", args) is True
    return pool.added[0]


def test_first_account_keeps_priority_zero(monkeypatch):
    assert _run(monkeypatch, []).priority == 0


def test_second_account_gets_the_next_priority(monkeypatch):
    assert _run(monkeypatch, [0]).priority == 1


def test_gaps_do_not_reuse_a_taken_priority(monkeypatch):
    assert _run(monkeypatch, [0, 1, 3]).priority == 2


def test_explicit_api_key_skips_device_login(monkeypatch):
    monkeypatch.setattr(copilot_credentials, "copilot_device_code_login",
                        lambda: (_ for _ in ()).throw(AssertionError()))
    args = SimpleNamespace(api_key="ghu_key", auth_type="api-key", label="")
    assert copilot_setup.auth_handler("add", args) is False
