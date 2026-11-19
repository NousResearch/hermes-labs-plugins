"""#43: an Individual-plan account keeps its own API host when the exchange 404s."""

from __future__ import annotations

import json

import pytest

import providers as _providers

_providers.get_provider_profile("copilot")
from github_copilot_plugin.copilot import credentials as cr  # noqa: E402


class _Resp:
    def __init__(self, payload):
        self._payload = json.dumps(payload).encode()

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    cr._account_host_cache.clear()
    monkeypatch.setattr(cr, "exchange_copilot_token",
                        lambda *a, **k: (_ for _ in ()).throw(ValueError("HTTP Error 404: Not Found")))
    yield
    cr._account_host_cache.clear()


def test_individual_host_is_used_when_exchange_fails(monkeypatch):
    seen = []

    def _bounded(req, timeout):
        seen.append(req.full_url)
        return _Resp({"copilot_plan": "individual",
                      "endpoints": {"api": "https://api.individual.githubcopilot.com"}})

    monkeypatch.setattr(cr, "_urlopen_bounded", _bounded)
    token, base = cr.get_copilot_api_token("gho_example")
    assert token == "gho_example"
    assert base == "https://api.individual.githubcopilot.com"
    assert seen and "copilot_internal/user" in seen[0]


def test_host_lookup_is_cached(monkeypatch):
    calls = []

    def _bounded(req, timeout):
        calls.append(req.full_url)
        return _Resp({"endpoints": {"api": "https://api.individual.githubcopilot.com/"}})

    monkeypatch.setattr(cr, "_urlopen_bounded", _bounded)
    for _ in range(3):
        assert cr.get_copilot_api_token("gho_example")[1] == "https://api.individual.githubcopilot.com"
    assert len(calls) == 1


def test_introspection_failure_keeps_old_behaviour(monkeypatch):
    def _boom(req, timeout):
        raise OSError("network down")

    monkeypatch.setattr(cr, "_urlopen_bounded", _boom)
    assert cr.get_copilot_api_token("gho_example") == ("gho_example", None)


def test_missing_endpoints_keeps_old_behaviour(monkeypatch):
    monkeypatch.setattr(cr, "_urlopen_bounded", lambda req, t: _Resp({"copilot_plan": "individual"}))
    assert cr.get_copilot_api_token("gho_example") == ("gho_example", None)


def test_successful_exchange_does_not_introspect(monkeypatch):
    monkeypatch.setattr(cr, "exchange_copilot_token",
                        lambda *a, **k: ("jwt", 9999999999.0, "https://api.business.githubcopilot.com"))

    def _fail(req, t):
        raise AssertionError("must not introspect when the exchange worked")

    monkeypatch.setattr(cr, "_urlopen_bounded", _fail)
    assert cr.get_copilot_api_token("gho_example") == ("jwt", "https://api.business.githubcopilot.com")


def test_empty_token_short_circuits():
    assert cr.get_copilot_api_token("") == ("", None)
