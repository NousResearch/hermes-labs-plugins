"""#63: fine-grained PATs need the developer-cli integration id."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from copilot.credentials import copilot_request_headers
from copilot.catalog import copilot_default_headers
from copilot.wire import CopilotWireMixin

PAT = "github_pat_" + "a" * 40
OAUTH = "ghu_" + "b" * 40


def test_pat_uses_developer_cli():
    assert copilot_request_headers(api_key=PAT)["Copilot-Integration-Id"] == "copilot-developer-cli"


def test_pat_vision_keeps_developer_cli():
    h = copilot_request_headers(api_key=PAT, is_vision=True)
    assert h["Copilot-Integration-Id"] == "copilot-developer-cli"
    assert h["Copilot-Vision-Request"] == "true"


def test_oauth_and_exchanged_keep_vscode_chat():
    assert copilot_request_headers(api_key=OAUTH)["Copilot-Integration-Id"] == "vscode-chat"
    assert copilot_request_headers(api_key="eyJabc")["Copilot-Integration-Id"] == "vscode-chat"


def test_catalog_defaults_follow_api_key():
    assert copilot_default_headers(api_key=PAT)["Copilot-Integration-Id"] == "copilot-developer-cli"
    assert copilot_default_headers(api_key=OAUTH)["Copilot-Integration-Id"] == "vscode-chat"


def test_client_headers_follow_api_key():
    wire = CopilotWireMixin()
    h = wire.client_headers(base_url="https://api.githubcopilot.com", api_key=PAT, has_images=True)
    assert h["Copilot-Integration-Id"] == "copilot-developer-cli"
    assert h["Copilot-Vision-Request"] == "true"
    h2 = wire.client_headers(base_url="https://api.githubcopilot.com", api_key=OAUTH)
    assert h2["Copilot-Integration-Id"] == "vscode-chat"
