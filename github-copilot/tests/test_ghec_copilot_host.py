"""#41: GHEC Copilot must exchange and send headers on its own hosts."""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from copilot.credentials import _token_exchange_url, _TOKEN_EXCHANGE_URL
from copilot.wire import is_copilot_host, CopilotWireMixin


def test_public_host_unchanged(monkeypatch):
    monkeypatch.delenv("COPILOT_GH_HOST", raising=False)
    assert _token_exchange_url() == _TOKEN_EXCHANGE_URL


def test_ghec_host_uses_its_own_api(monkeypatch):
    monkeypatch.setenv("COPILOT_GH_HOST", "acme.ghe.com")
    assert _token_exchange_url() == "https://api.acme.ghe.com/copilot_internal/v2/token"


def test_ghec_host_with_scheme_and_trailing_slash(monkeypatch):
    monkeypatch.setenv("COPILOT_GH_HOST", "https://acme.ghe.com/")
    assert _token_exchange_url() == "https://api.acme.ghe.com/copilot_internal/v2/token"


def test_ghec_copilot_api_host_recognised():
    assert is_copilot_host("https://copilot-api.acme.ghe.com")
    assert not is_copilot_host("https://copilot-api.acme.ghe.com.evil.test")
    assert is_copilot_host("https://api.business.githubcopilot.com")


def test_ghec_client_headers_present():
    wire = CopilotWireMixin()
    h = wire.client_headers(base_url="https://copilot-api.acme.ghe.com", api_key="ghu_x")
    assert h is not None
    assert h["Editor-Version"]
    assert h["Copilot-Integration-Id"] == "vscode-chat"
