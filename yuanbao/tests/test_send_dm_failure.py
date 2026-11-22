"""A failed primary text DM must not be reported as a successful DM."""
import asyncio
import json

import yuanbao_plugin.tools as tools
from gateway.platforms.base import BasePlatformAdapter


class Result:
    def __init__(self, success, error=""):
        self.success, self.error, self.message_id = success, error, ''


class Adapter:
    def __init__(self):
        self.text = 0
        self.media = 0

    async def send_dm(self, user_id, message, group_code=""):
        self.text += 1
        return Result(False, "text send failed")

    async def send_image_file(self, chat_id, path, group_code=""):
        self.media += 1
        return Result(True)

    async def send_document(self, chat_id, path, group_code=""):
        self.media += 1
        return Result(True)


def test_failed_text_with_successful_media_is_a_failure(monkeypatch):
    adapter = Adapter()
    monkeypatch.setattr(tools, "_adapter", lambda: adapter)
    monkeypatch.setattr(tools, "_session_env", lambda *_a, **_k: "")
    monkeypatch.setattr(BasePlatformAdapter, "extract_media", staticmethod(lambda text: ([], text)))
    monkeypatch.setattr(BasePlatformAdapter, "filter_media_delivery_paths",
                        staticmethod(lambda files, session_key="", **_k: files))
    out = json.loads(asyncio.run(tools.send_dm({
        "user_id": "123", "message": "hello", "media_files": [{"path": "report.png"}]})))
    assert out["success"] is False
    assert adapter.text == 1 and adapter.media == 1
