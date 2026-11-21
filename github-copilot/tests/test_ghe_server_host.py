"""#39: self-hosted GitHub Enterprise Server uses its /api/v3/ routes and its own device login."""
import providers as _providers

_providers.get_provider_profile("copilot")
from github_copilot_plugin.copilot import credentials as cr  # noqa: E402
from github_copilot_plugin.copilot import wire  # noqa: E402


def _set(monkeypatch, host):
    monkeypatch.setattr(cr, "get_secret", lambda name, *a, **k: host if name == "COPILOT_GH_HOST" else "")


def test_ghe_server_exchange_uses_api_v3(monkeypatch):
    _set(monkeypatch, "ghe.example.com")
    assert cr._token_exchange_url() == "https://ghe.example.com/api/v3/copilot_internal/v2/token"


def test_ghec_still_uses_api_subdomain(monkeypatch):
    _set(monkeypatch, "acme.ghe.com")
    assert cr._token_exchange_url() == "https://api.acme.ghe.com/copilot_internal/v2/token"


def test_public_and_scheme_inputs(monkeypatch):
    _set(monkeypatch, "")
    assert cr._token_exchange_url() == cr._TOKEN_EXCHANGE_URL
    _set(monkeypatch, "https://ghe.example.com/")
    assert cr._token_exchange_url() == "https://ghe.example.com/api/v3/copilot_internal/v2/token"


def test_ghe_server_copilot_api_host_is_recognised(monkeypatch):
    _set(monkeypatch, "ghe.example.com")
    monkeypatch.setattr(wire, "get_secret", lambda name, *a, **k: "ghe.example.com")
    assert wire.is_copilot_host("https://copilot-api.ghe.example.com")
    assert not wire.is_copilot_host("https://copilot-api.ghe.example.com.evil.test")


def test_device_login_uses_the_configured_host(monkeypatch):
    seen = {}

    def _post(url, fields, timeout):
        seen["url"] = url
        return {"error": "authorization_pending"}

    monkeypatch.setattr(cr, "get_secret", lambda name, *a, **k: "ghe.example.com" if name == "COPILOT_GH_HOST" else "")
    monkeypatch.setattr(cr, "_post_form", _post)
    monkeypatch.setattr(cr.time, "sleep", lambda *_: None)
    cr.copilot_device_code_login(timeout_seconds=0.01)
    assert seen["url"] == "https://ghe.example.com/login/device/code"
