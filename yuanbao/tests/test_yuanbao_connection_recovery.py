"""Recover from remote closes without reviving a stopped connection."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
import websockets

from gateway.config import PlatformConfig
from yuanbao_plugin import adapter as adapter_module
from yuanbao_plugin.adapter import YuanbaoAdapter


@pytest.fixture
def adapter(monkeypatch):
    instance = YuanbaoAdapter(PlatformConfig(extra={"app_id": "test", "app_secret": "test"}))
    monkeypatch.setattr(instance, "_write_runtime_status_safe", MagicMock())
    monkeypatch.setattr(instance, "_release_platform_lock", MagicMock())
    instance._running = True
    yield instance
    YuanbaoAdapter.set_active(None)


@pytest.mark.asyncio
@pytest.mark.parametrize("code", [1000, 1001, 1011, 4012, 4013, 4014, 4018, 4019, 4021])
async def test_remote_close_reconnects_unless_permanent(adapter, monkeypatch, code):
    """Use the real websockets iterator: normal closes do not raise to the reader."""
    close_gate = asyncio.Event()

    async def server(ws):
        await close_gate.wait()
        await ws.close(code)

    connection = adapter._connection
    reconnect = AsyncMock(return_value=True)
    monkeypatch.setattr(connection, "_do_reconnect", reconnect)
    async with websockets.serve(server, "127.0.0.1", 0) as listener:
        port = listener.sockets[0].getsockname()[1]
        connection._ws = await websockets.connect(f"ws://127.0.0.1:{port}", proxy=None)
        try:
            close_gate.set()
            await asyncio.wait_for(connection._receive_loop(), timeout=2)
            await asyncio.gather(*list(adapter._background_tasks))
            retryable = code in (1000, 1001, 1011)
            assert reconnect.await_count == int(retryable)
            assert adapter._running is retryable
        finally:
            await adapter.disconnect()


class _ClosingSocket:
    """A reader that finishes only after the test releases it."""

    def __init__(self):
        self.reading = asyncio.Event()
        self.closed = asyncio.Event()
        self.close_code = 1000
        self.close_reason = ""

    def __aiter__(self):
        return self

    async def __anext__(self):
        self.reading.set()
        await self.closed.wait()
        raise StopAsyncIteration

    async def close(self):
        self.closed.set()


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["cancel", "disconnect", "stopped", "replaced"])
async def test_reader_exit_does_not_reconnect_after_teardown(adapter, monkeypatch, action):
    connection = adapter._connection
    socket = connection._ws = _ClosingSocket()
    reconnect = AsyncMock(return_value=True)
    monkeypatch.setattr(connection, "_do_reconnect", reconnect)
    reader = connection._recv_task = asyncio.create_task(connection._receive_loop())
    try:
        await asyncio.wait_for(socket.reading.wait(), timeout=2)
        if action == "cancel":
            reader.cancel()
        elif action == "disconnect":
            await adapter.disconnect()
        elif action == "stopped":
            adapter._mark_disconnected()
        else:
            connection._ws = _ClosingSocket()
        socket.closed.set()
        await asyncio.wait_for(reader, timeout=2)
        await asyncio.gather(*list(adapter._background_tasks))
        reconnect.assert_not_awaited()
    finally:
        await adapter.disconnect()


@pytest.mark.asyncio
async def test_queued_reconnect_does_not_run_after_disconnect(adapter, monkeypatch):
    connection = adapter._connection
    reconnect = AsyncMock(return_value=True)
    monkeypatch.setattr(connection, "_do_reconnect", reconnect)
    connection.schedule_reconnect()
    tasks = list(adapter._background_tasks)
    try:
        await adapter.disconnect()
        await asyncio.gather(*tasks)
        reconnect.assert_not_awaited()
        assert not adapter._running
    finally:
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("stop", [False, True], ids=["running", "stopped"])
@pytest.mark.parametrize("stage", ["backoff", "cleanup", "token", "auth"])
async def test_reconnect_obeys_stop_during_await(adapter, monkeypatch, stage, stop):
    connection = adapter._connection
    reached = asyncio.Event()
    resume = asyncio.Event()
    socket = _ClosingSocket()

    async def pause():
        reached.set()
        await resume.wait()

    async def backoff(_delay):
        if stage == "backoff":
            await pause()

    async def cleanup():
        if stage == "cleanup":
            await pause()

    async def token(*args, **kwargs):
        if stage == "token":
            await pause()
        return {"token": "test"}

    async def dial(_token):
        connection._ws = socket
        if stage == "auth":
            await pause()
        return True

    monkeypatch.setattr(adapter_module.asyncio, "sleep", backoff)
    monkeypatch.setattr(connection._push_handoff, "close", cleanup)
    refresh = AsyncMock(side_effect=token)
    monkeypatch.setattr(adapter_module.SignManager, "force_refresh", refresh)
    dial_mock = AsyncMock(side_effect=dial)
    monkeypatch.setattr(connection, "_dial", dial_mock)
    start_loops = MagicMock()
    monkeypatch.setattr(connection, "_start_loops", start_loops)
    task = asyncio.create_task(connection._reconnect_with_backoff())
    try:
        await asyncio.wait_for(reached.wait(), timeout=2)
        if stop:
            # This is also the state transition used by permanent close codes.
            adapter._mark_disconnected()
        resume.set()
        result = await asyncio.wait_for(task, timeout=2)
        assert result is (not stop)
        assert adapter._running is (not stop)
        assert start_loops.call_count == int(not stop)
        assert dial_mock.await_count == int(not stop or stage == "auth")
        if stop:
            assert connection.ws is None
            if stage == "auth":
                assert socket.closed.is_set()
            if stage in ("backoff", "cleanup"):
                refresh.assert_not_awaited()
        else:
            assert connection.ws is socket
            assert YuanbaoAdapter.get_active() is adapter
    finally:
        resume.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await adapter.disconnect()


@pytest.mark.asyncio
async def test_simultaneous_reconnect_requests_share_one_attempt(adapter, monkeypatch):
    entered = asyncio.Event()
    resume = asyncio.Event()

    async def reconnect():
        entered.set()
        await resume.wait()
        return True

    connection = adapter._connection
    attempt = AsyncMock(side_effect=reconnect)
    monkeypatch.setattr(connection, "_do_reconnect", attempt)
    connection.schedule_reconnect()
    connection.schedule_reconnect()
    tasks = list(adapter._background_tasks)
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        connection.schedule_reconnect()
        resume.set()
        await asyncio.gather(*tasks)
        attempt.assert_awaited_once()
    finally:
        resume.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        await adapter.disconnect()
