"""Recall acts on the message whose turn is running, and on a follow-up still queued behind it.

Addressed group messages go straight to BasePlatformAdapter (#78166). Dispatch used to record the
follow-up as the "processing" message before core decided to queue it, so a recall of the message
actually running no longer matched and the turn ran on.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from gateway.config import PlatformConfig
from yuanbao_plugin.adapter import (
    DispatchMiddleware, InboundContext, RecallGuardMiddleware, YuanbaoAdapter,
)
from waiting import async_wait_until


async def _noop(*_args, **_kwargs):
    return None


def _rig():
    adapter = YuanbaoAdapter(PlatformConfig(enabled=True, extra={
        "app_id": "fake", "app_secret": "fake", "group_policy": "open"}))
    adapter._bot_id = "bot"
    adapter.send_typing = _noop
    adapter._send_with_retry = _noop
    running, release = asyncio.Event(), asyncio.Event()
    started: list[str] = []

    async def handler(event):
        started.append(event.message_id)
        if event.message_id == "first":
            running.set()
            await release.wait()

    adapter.set_message_handler(handler)
    source = adapter.build_source(chat_id="group:test", chat_type="group", user_id="alice", thread_id="main")
    key = adapter._source_session_key(source)
    mention = {"msg_type": "TIMCustomElem",
               "msg_content": {"data": json.dumps({"elem_type": 1002, "user_id": "bot", "text": "@bot"})}}

    def addressed(message_id: str) -> InboundContext:
        return InboundContext(adapter=adapter, source=source, chat_type="group", chat_id="group:test",
                              msg_id=message_id, raw_text=message_id, msg_body=[mention])

    return adapter, key, DispatchMiddleware(), addressed, release, running, started


def _recall(adapter, message_id: str) -> InboundContext:
    return InboundContext(adapter=adapter, push={
        "callback_command": "Group.CallbackAfterRecallMsg", "group_code": "test",
        "from_account": "alice", "recall_msg_seq_list": [{"msg_id": message_id}]})


async def _cancel_all(adapter) -> None:
    tasks = list(adapter._inbound_tasks) + list(adapter._session_tasks.values())
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


async def _first_running_second_queued(adapter, key, middleware, addressed, running) -> None:
    await middleware.handle(addressed("first"), _noop)
    await asyncio.wait_for(running.wait(), 5)
    await middleware.handle(addressed("second"), _noop)
    await async_wait_until(lambda: key in adapter._pending_messages,
                           what="the follow-up to be queued behind the running turn")


@pytest.mark.asyncio
async def test_recall_of_running_message_interrupts_after_addressed_follow_up():
    adapter, key, middleware, addressed, release, running, started = _rig()
    try:
        await _first_running_second_queued(adapter, key, middleware, addressed, running)
        assert adapter._processing_msg_ids.get(key) == "first"
        assert not adapter._active_sessions[key].is_set()

        await RecallGuardMiddleware().handle(_recall(adapter, "first"), _noop)

        assert adapter._active_sessions[key].is_set()
    finally:
        release.set()
        await _cancel_all(adapter)
    assert started[0] == "first"


@pytest.mark.asyncio
async def test_recall_of_queued_follow_up_drops_it_and_spares_the_running_turn():
    adapter, key, middleware, addressed, release, running, started = _rig()
    try:
        await _first_running_second_queued(adapter, key, middleware, addressed, running)

        await RecallGuardMiddleware().handle(_recall(adapter, "second"), _noop)

        assert key not in adapter._pending_messages
        assert not adapter._active_sessions[key].is_set()
        assert adapter._processing_msg_ids.get(key) == "first"
        release.set()
        await async_wait_until(lambda: key not in adapter._active_sessions, what="the running turn to finish")
        assert started == ["first"]
    finally:
        release.set()
        await _cancel_all(adapter)
