"""``/api/resource/v1/download`` answers JSON; a redirect from it must not carry the bot's X-Token to the
redirect target (real loopback servers, real httpx client)."""

import asyncio
import http.server
import json
import threading
from contextlib import contextmanager

import httpx
import pytest

from yuanbao_plugin.adapter import MediaResolveMiddleware

from test_yuanbao_pipeline import make_adapter


@contextmanager
def _server(handler_cls):
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def test_redirect_does_not_forward_the_token():
    seen: list[dict] = []

    class Elsewhere(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append(dict(self.headers))
            body = json.dumps({"code": 0, "data": {"url": "https://cdn.example/f"}}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    with _server(Elsewhere) as elsewhere:
        class Api(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(302)
                self.send_header("Location", f"{elsewhere}/collect")
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *args):
                pass

        with _server(Api) as api:
            adapter = make_adapter()
            adapter._api_domain = api

            async def _token():
                return {"token": "tok-secret", "bot_id": "bot_123", "source": "web"}

            adapter._get_cached_token = _token
            with pytest.raises(httpx.HTTPStatusError):
                asyncio.run(MediaResolveMiddleware._fetch_resource_url(adapter, "rid-1"))
    assert not any("tok-secret" in v for headers in seen for v in headers.values()), seen
