"""CDP endpoint resolution: precedence (flag > BROWSER_CDP_URL > browser.cdp_url > default) and
/json/version discovery against a real loopback HTTP server."""

from __future__ import annotations

import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from workflow_record_plugin.cdp import (
    DEFAULT_CDP_URL,
    configured_endpoint,
    discovery_url,
    pick_page_target,
    resolve_endpoint,
    to_websocket_url,
)


@pytest.mark.parametrize("raw, expected", [
    ("http://127.0.0.1:9222", "http://127.0.0.1:9222/json/version"),
    ("http://127.0.0.1:9222/", "http://127.0.0.1:9222/json/version"),
    ("127.0.0.1:9333", "http://127.0.0.1:9333/json/version"),
    ("ws://localhost:9222", "http://localhost:9222/json/version"),
    ("wss://browser.example:443", "https://browser.example:443/json/version"),
    ("http://h:1/json/version", "http://h:1/json/version"),
    ("ws://127.0.0.1:9222/devtools/browser/abc", None),
    ("ws://cloud.example/session/xyz", None),
    ("ws://127.0.0.1:9222/?token=abc", "http://127.0.0.1:9222/json/version?token=abc"),
    ("wss://relay.example:443?token=abc", "https://relay.example:443/json/version?token=abc"),
])
def test_discovery_url(raw, expected):
    assert discovery_url(raw) == expected


@pytest.fixture
def fake_devtools():
    """A loopback server answering /json/version like Chrome does."""
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            if self.path != "/json/version":
                self.send_error(404)
                return
            port = self.server.server_address[1]
            body = json.dumps({"webSocketDebuggerUrl": f"ws://127.0.0.1:{port}/devtools/browser/fake"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_http_root_resolves_to_the_browser_websocket(fake_devtools):
    port = fake_devtools.rsplit(":", 1)[1]
    assert to_websocket_url(fake_devtools) == (f"ws://127.0.0.1:{port}/devtools/browser/fake", "")


def test_full_websocket_url_is_used_as_given():
    url = "ws://127.0.0.1:1/devtools/browser/abc"
    assert to_websocket_url(url) == (url, "")


def test_unreachable_endpoint_reports_why():
    ws_url, error = to_websocket_url(f"http://127.0.0.1:{_free_port()}", timeout=1)
    assert ws_url == "" and "no browser answered" in error


def test_default_endpoint_when_nothing_is_configured():
    assert configured_endpoint() == (DEFAULT_CDP_URL, "default")


def test_config_yaml_cdp_url(hermes_home):
    (hermes_home / "config.yaml").write_text("browser:\n  cdp_url: http://127.0.0.1:9444\n", encoding="utf-8")
    assert configured_endpoint() == ("http://127.0.0.1:9444", "browser.cdp_url")


def test_env_beats_config(hermes_home):
    (hermes_home / "config.yaml").write_text("browser:\n  cdp_url: http://127.0.0.1:9444\n", encoding="utf-8")
    (hermes_home / ".env").write_text("BROWSER_CDP_URL=http://127.0.0.1:9555\n", encoding="utf-8")
    assert configured_endpoint() == ("http://127.0.0.1:9555", "BROWSER_CDP_URL")


def test_flag_beats_everything(hermes_home, monkeypatch):
    monkeypatch.setenv("BROWSER_CDP_URL", "http://127.0.0.1:9555")
    assert configured_endpoint("ws://127.0.0.1:1/devtools/browser/x") == ("ws://127.0.0.1:1/devtools/browser/x", "--cdp")


def test_resolve_endpoint_end_to_end(hermes_home, fake_devtools):
    (hermes_home / "config.yaml").write_text(f"browser:\n  cdp_url: {fake_devtools}\n", encoding="utf-8")
    ws_url, source, error = resolve_endpoint()
    assert (source, error) == ("browser.cdp_url", "")
    assert ws_url.endswith("/devtools/browser/fake")


def test_pick_page_target_prefers_web_pages():
    targets = [
        {"type": "service_worker", "url": "https://x.test/sw.js", "targetId": "0"},
        {"type": "page", "url": "devtools://devtools/inspector.html", "targetId": "1"},
        {"type": "page", "url": "chrome-extension://abc/popup.html", "targetId": "2"},
        {"type": "page", "url": "about:blank", "targetId": "3"},
        {"type": "page", "url": "https://shop.test/login", "targetId": "4"},
    ]
    assert pick_page_target(targets)["targetId"] == "4"
    assert pick_page_target(targets[:4])["targetId"] == "3"
    assert pick_page_target([]) is None
