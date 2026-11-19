"""The WebSocket reader keeps resolving control frames while inbound admission is full.

Bounded admission (#145139) made the single reader wait for capacity, so at 64 retained pushes it
stopped reading: PONGs and RPC responses queued behind it, heartbeats timed out and sends stalled.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from yuanbao_plugin.adapter import ConnectionManager
from yuanbao_plugin.inbound_capacity import INBOUND_MAX_RETAINED_FRAMES
from yuanbao_plugin.yuanbao_proto import CMD_TYPE, decode_conn_msg, encode_conn_msg_full
from waiting import async_wait_until


class _Socket:
    def __init__(self) -> None:
        self.incoming: asyncio.Queue = asyncio.Queue()
        self.sent: list[bytes] = []
        self.read_count = 0

    def __aiter__(self):
        return self

    async def __anext__(self):
        raw = await self.incoming.get()
        self.read_count += 1
        return raw

    async def send(self, data: bytes) -> None:
        self.sent.append(data)

    async def close(self) -> None:
        pass


def _push(index: int) -> bytes:
    data = json.dumps({
        "From_Account": f"user-{index}", "MsgKey": str(index),
        "MsgBody": [{"MsgType": "TIMTextElem", "MsgContent": {"text": "hello"}}],
    }).encode()
    return encode_conn_msg_full(CMD_TYPE["Push"], "push", index, str(index), "", data, need_ack=True)


def _acked_ids(ws: _Socket) -> list[str]:
    return [decode_conn_msg(frame)["head"]["msg_id"] for frame in ws.sent]


@pytest.mark.asyncio
async def test_pong_and_rpc_response_resolve_while_admission_is_full():
    release = asyncio.Event()
    tasks: set[asyncio.Task] = set()

    async def execute(_ctx):
        await release.wait()

    def track(task):
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    adapter = SimpleNamespace(name="probe", _running=False,
                              _inbound_pipeline=SimpleNamespace(execute=execute), _track_task=track)
    connection = ConnectionManager(adapter)
    connection._DEBOUNCE_WINDOW = 0
    ws = connection._ws = _Socket()
    loop = asyncio.get_running_loop()
    pong = connection._pending_pong = loop.create_future()
    rpc = connection._pending_acks["rpc"] = loop.create_future()

    pushes = INBOUND_MAX_RETAINED_FRAMES + 1
    for index in range(pushes):
        ws.incoming.put_nowait(_push(index))
    ws.incoming.put_nowait(encode_conn_msg_full(CMD_TYPE["Response"], "ping", 100, "pong", "", b""))
    ws.incoming.put_nowait(encode_conn_msg_full(CMD_TYPE["Response"], "send", 101, "rpc", "", b""))

    reader = asyncio.create_task(connection._receive_loop())
    try:
        await async_wait_until(lambda: pong.done() and rpc.done(),
                               what="PONG and RPC response to resolve behind a full admission budget")
        assert ws.read_count == pushes + 2
        await async_wait_until(lambda: connection._inbound_admission.retained_frames == INBOUND_MAX_RETAINED_FRAMES,
                               what="the admission budget to fill")
        # Admit-before-ACK still holds: the push over the budget is not acknowledged yet.
        assert str(pushes - 1) not in _acked_ids(ws)
        assert len(ws.sent) == INBOUND_MAX_RETAINED_FRAMES

        release.set()
        assert connection._push_handoff.shed == 0
        await async_wait_until(lambda: str(pushes - 1) in _acked_ids(ws),
                               what="the waiting push to be admitted and ACKed once capacity frees")
        assert _acked_ids(ws) == [str(index) for index in range(pushes)]
    finally:
        release.set()
        reader.cancel()
        await asyncio.gather(reader, return_exceptions=True)
        await connection._push_handoff.close()
        await connection._inbound_admission.close()
        for task in list(tasks):
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_pushes_past_the_handoff_bound_are_shed_unacknowledged():
    release = asyncio.Event()

    async def execute(_ctx):
        await release.wait()

    adapter = SimpleNamespace(name="probe", _running=False,
                              _inbound_pipeline=SimpleNamespace(execute=execute), _track_task=lambda _t: None)
    connection = ConnectionManager(adapter)
    connection._DEBOUNCE_WINDOW = 0
    ws = connection._ws = _Socket()
    total = 3 * INBOUND_MAX_RETAINED_FRAMES
    for index in range(total):
        ws.incoming.put_nowait(_push(index))
    pong = connection._pending_pong = asyncio.get_running_loop().create_future()
    ws.incoming.put_nowait(encode_conn_msg_full(CMD_TYPE["Response"], "ping", 100, "pong", "", b""))

    reader = asyncio.create_task(connection._receive_loop())
    try:
        await async_wait_until(pong.done, what="PONG to resolve behind a flooded handoff")
        handoff = connection._push_handoff
        assert len(handoff.entries) <= INBOUND_MAX_RETAINED_FRAMES
        assert handoff.shed > 0
        retained = connection._inbound_admission.retained_frames + len(handoff.entries)
        assert retained + handoff.shed == total
        release.set()
        await async_wait_until(lambda: not handoff.entries, what="the handoff to drain")
        acked = set(_acked_ids(ws))
        assert len(acked) == total - handoff.shed  # every shed push stayed unACKed for redelivery
    finally:
        release.set()
        reader.cancel()
        await asyncio.gather(reader, return_exceptions=True)
        await connection._push_handoff.close()
        await connection._inbound_admission.close()
