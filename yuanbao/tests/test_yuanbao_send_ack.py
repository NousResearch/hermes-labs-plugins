"""Yuanbao send ACK: business rejection must not look like successful delivery.

Covers issue #107227 — Yuanbao can return head.status=0 with a nonzero
business code (observed production: code=999 / inner 9992501) while
``MessageSender._dispatch_encoded`` still reported success. Also pins the
conservative 1200-char outbound chunk
target.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from yuanbao_plugin.adapter import MessageSender, YuanbaoAdapter
from yuanbao_plugin.yuanbao_proto import _s, _v


def _run(coro):
    return asyncio.run(coro)


def _adapter_for_dispatch(response):
    async def send_biz_request(encoded, req_id, timeout=None):
        return response

    return SimpleNamespace(
        name="yuanbao",
        _connection=SimpleNamespace(send_biz_request=send_biz_request),
    )


def _dispatch(response, encoded=b"encoded", req_id="c2c_1"):
    return _run(MessageSender._dispatch_encoded(_adapter_for_dispatch(response), encoded, req_id))


def test_business_rejection_status0_code999_is_not_success():
    """THE bug: head.status=0 + business code=999 must fail closed."""
    biz = _v(1, 999) + _s(2, '{"code":9992501,"message":"请求云 IM 服务返回失败"}')
    resp = {"head": {"status": 0, "msg_id": "c2c_1"}, "data": biz}
    result = _dispatch(resp)
    assert result["success"] is False
    assert "999" in str(result.get("error", ""))


def test_protocol_header_rejection_nonzero_status():
    resp = {"head": {"status": 1, "msg_id": "c2c_2"}, "data": _v(1, 0)}
    result = _dispatch(resp, req_id="c2c_2")
    assert result["success"] is False


def test_valid_success_ack_uses_head_msg_id():
    resp = {"head": {"status": 0, "msg_id": "c2c_ok"}, "data": _v(1, 0) + _s(2, "ok")}
    result = _dispatch(resp, req_id="c2c_ok")
    assert result["success"] is True
    assert result["msg_key"] == "c2c_ok"


@pytest.mark.parametrize("data_type", [bytes, bytearray])
@pytest.mark.parametrize("payload", [
    b"\x12\x00",  # Empty message, with the default zero code omitted.
    _s(2, "ok"),
    _s(2, "ok") + _v(3, 1),  # Unknown fields do not change the default code.
])
def test_omitted_business_code_is_success(payload, data_type):
    resp = {"head": {"status": 0, "msg_id": "omitted_code"}, "data": data_type(payload)}
    assert _dispatch(resp) == {"success": True, "msg_key": "omitted_code"}


@pytest.mark.parametrize("chat_id", ["direct:user_1", "group:group_1"])
@pytest.mark.parametrize("payload", [b"\x12\x00", _v(1, 0)])
def test_success_ack_does_not_retry_text_chunk(chat_id, payload, monkeypatch):
    response = {"head": {"status": 0, "msg_id": "sent_once"}, "data": payload}
    send_biz_request = AsyncMock(return_value=response)
    adapter = SimpleNamespace(
        name="yuanbao",
        _bot_id="bot_1",
        _member_cache={},
        _connection=SimpleNamespace(send_biz_request=send_biz_request),
    )
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())
    result = _run(MessageSender(adapter).send_text_chunk(chat_id, "Hello"))

    assert send_biz_request.await_count == 1
    assert result.success is True
    assert result.message_id == "sent_once"


@pytest.mark.parametrize("payload", [b"", b"\x12\x00", _v(1, 999)])
def test_nonzero_status_is_not_overridden_by_business_code(payload):
    resp = {"head": {"status": 1}, "data": payload}
    result = _dispatch(resp)
    assert result["success"] is False
    assert "status=1" in result["error"]


def test_empty_payload_is_success():
    assert _dispatch({"head": {"status": 0}, "data": b""}) == {"success": True, "msg_key": ""}


def test_fail_open_status0_no_data_is_success():
    resp = {"head": {"status": 0, "msg_id": "c2c_empty"}}
    result = _dispatch(resp, req_id="c2c_empty")
    assert result["success"] is True


def test_fail_open_empty_dict_is_success():
    result = _dispatch({})
    assert result == {"success": True, "msg_key": ""}


@pytest.mark.parametrize("response", [None, [], "unexpected", 42])
def test_non_dict_send_response_is_malformed(response):
    result = _dispatch(response)
    assert result == {"success": False, "error": "malformed send response"}


@pytest.mark.parametrize("payload", [
    b"\x08\x80",  # Truncated code varint.
    b"\x80",  # Truncated field tag.
    b"\x12\x80",  # Truncated string length.
    b"\x12\x02x",  # Truncated string value.
    b"\x19\x00",  # Truncated fixed64 field.
    b"\x1d\x00",  # Truncated fixed32 field.
    b"\x0f",  # Invalid wire type.
    _v(1, 0) + b"\x12\x02x",  # An explicit zero cannot hide truncation.
    "invalid",
])
def test_malformed_nonempty_payload_is_not_success(payload):
    resp = {"head": {"status": 0, "msg_id": "c2c_bad"}, "data": payload}
    result = _dispatch(resp, req_id="c2c_bad")
    assert result == {"success": False, "error": "malformed send response"}


def test_chunk_target_is_1200_and_splits_2491_chars():
    chunks = MessageSender.truncate_message("汉" * 2491, YuanbaoAdapter.MAX_TEXT_CHUNK)
    assert len(chunks) > 1
    assert all(len(c) <= 1200 for c in chunks)

