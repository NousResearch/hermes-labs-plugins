"""Group query business errors must not become successful tool results or cached members."""

import json
from unittest.mock import AsyncMock

import pytest

from gateway.config import PlatformConfig
from yuanbao_plugin.adapter import YuanbaoAdapter
from yuanbao_plugin import tools as yuanbao_tools
from yuanbao_plugin.yuanbao_proto import _m, _s, _v


@pytest.fixture
def adapter(monkeypatch):
    instance = YuanbaoAdapter(PlatformConfig(extra={"app_id": "test", "app_secret": "test"}))
    instance._connection._ws = object()
    monkeypatch.setattr(yuanbao_tools, "_get_active_adapter", lambda: instance)
    return instance


@pytest.mark.asyncio
@pytest.mark.parametrize("query", ["info", "members"])
@pytest.mark.parametrize("status, code", [
    (0, None),  # Proto3 omits the default zero business code.
    (0, 0),
    (0, 403),
    (503, 0),
])
async def test_query_tools_respect_both_status_and_business_code(adapter, monkeypatch, query, status, code):
    if query == "info":
        details = _m(3, _s(1, "Test group") + _s(2, "owner") + _s(3, "Owner") + _v(4, 2))
        tool = yuanbao_tools.get_group_info
    else:
        details = _m(3, _s(1, "new-user") + _s(2, "New member") + _v(3, 1))
        tool = yuanbao_tools.query_group_members

    old_cache = (123.0, [{"user_id": "old-user", "nickname": "Cached member"}])
    adapter._member_cache["g1"] = old_cache
    payload = (b"" if code is None else _v(1, code)) + details
    send = AsyncMock(return_value={"head": {"status": status}, "data": payload})
    monkeypatch.setattr(adapter._connection, "send_biz_request", send)

    result = json.loads(await tool({"group_code": "g1"}))
    send.assert_awaited_once()
    success = status == 0 and code in (None, 0)
    assert result["success"] is success
    if success:
        if query == "info":
            assert result["group_name"] == "Test group"
            assert result["member_count"] == 2
            assert result["owner"] == {"user_id": "owner", "nickname": "Owner"}
        else:
            assert result["members"] == [{"user_id": "new-user", "nickname": "New member", "role": "user"}]
            assert adapter._member_cache["g1"][1] == [{"user_id": "new-user", "nickname": "New member", "role": 1}]
    else:
        assert result["error"]
        assert adapter._member_cache["g1"] == old_cache


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["query_group_info", "get_group_member_list"])
async def test_business_rejection_returns_none_and_logs_reason(adapter, monkeypatch, caplog, method):
    # Query response: code=403, message="not allowed", no result data.
    payload = b"\x08\x93\x03\x12\x0bnot allowed"
    monkeypatch.setattr(adapter._connection, "send_biz_request", AsyncMock(
        return_value={"head": {"status": 0}, "data": payload},
    ))

    assert await getattr(adapter, method)("g1") is None
    assert "code=403" in caplog.text
    assert "not allowed" in caplog.text
    assert "g1" not in adapter._member_cache
