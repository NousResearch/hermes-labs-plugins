"""#50: the device-code and token-exchange JSON reads are capped, not unbounded ``resp.read()``."""

import io
import json
from unittest.mock import patch

import pytest


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_device_code_form_post_rejects_an_oversized_body():
    from github_copilot_plugin.copilot import credentials as mod

    body = b'{"pad": "' + b"x" * (2 * 1024 * 1024) + b'"}'
    with patch("urllib.request.urlopen", return_value=_Resp(body)):
        with pytest.raises(ValueError, match="exceeds"):
            mod._post_form("https://github.com/login/device/code", {"client_id": "x"}, 5)


def test_device_code_form_post_parses_a_normal_body():
    from github_copilot_plugin.copilot import credentials as mod

    with patch("urllib.request.urlopen", return_value=_Resp(json.dumps({"device_code": "d"}).encode())):
        assert mod._post_form("https://github.com/login/device/code", {"client_id": "x"}, 5) == {"device_code": "d"}
