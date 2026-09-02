"""DingTalk platform adapter (Stream Mode via dingtalk-stream >=0.20; replies via session
webhook markdown or AI Cards). Requires ``pip install "dingtalk-stream>=0.20" httpx``.

Configuration in config.yaml:
    platforms:
      dingtalk:
        enabled: true
        # Optional group-chat gating (mirrors Slack/Telegram/Discord):
        require_mention: true            # or DINGTALK_REQUIRE_MENTION env var
        # free_response_chats:           # conversations that skip require_mention
        #   - cidABC==
        # mention_patterns:              # regex wake-words (e.g. Chinese bot names)
        #   - "^小马"
        # allowed_users:                 # staff_id or sender_id list; "*" = any
        #   - "manager1234"
        extra:
          client_id: "your-app-key"      # or DINGTALK_CLIENT_ID env var
          client_secret: "your-secret"   # or DINGTALK_CLIENT_SECRET env var
"""

import asyncio
import json
import logging
import os
import re
import traceback
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set

# Optional SDKs: catch broad Exception, not just ImportError — their transitive
# cryptography dependency can raise AttributeError on version skew, and a broken
# optional SDK must degrade gracefully instead of killing the plugin import.
try:
    import dingtalk_stream
    from dingtalk_stream import ChatbotMessage
    from dingtalk_stream.frames import CallbackMessage, AckMessage

    DINGTALK_STREAM_AVAILABLE = True
except Exception:  # noqa: BLE001
    DINGTALK_STREAM_AVAILABLE = False
    dingtalk_stream = None  # type: ignore[assignment]
    ChatbotMessage = None  # type: ignore[assignment]
    CallbackMessage = None  # type: ignore[assignment]
    AckMessage = type("AckMessage", (), {"STATUS_OK": 200, "STATUS_SYSTEM_EXCEPTION": 500})  # type: ignore[assignment]

try:
    import httpx

    HTTPX_AVAILABLE = True
except ImportError:
    HTTPX_AVAILABLE = False
    httpx = None  # type: ignore[assignment]

try:
    from alibabacloud_dingtalk.card_1_0 import client as dingtalk_card_client, models as dingtalk_card_models
    from alibabacloud_dingtalk.robot_1_0 import client as dingtalk_robot_client, models as dingtalk_robot_models
    from alibabacloud_tea_openapi import models as open_api_models
    from alibabacloud_tea_util import models as tea_util_models

    CARD_SDK_AVAILABLE = True
except Exception:
    CARD_SDK_AVAILABLE = False
    dingtalk_card_client = dingtalk_card_models = dingtalk_robot_client = dingtalk_robot_models = None
    open_api_models = tea_util_models = None

from gateway.config import Platform, PlatformConfig
from gateway.platforms.helpers import MessageDeduplicator, compile_mention_patterns
from gateway.platforms.base import BasePlatformAdapter, MessageEvent, SendResult
from gateway.platforms._shared import get_scoped_secret as _get_scoped_secret
from plugins.platforms.dingtalk.inbound import (  # noqa: F401 — re-exported names
    DINGTALK_TYPE_MAPPING,
    EXT_MAP,
    collect_download_codes,
    extract_media,
    extract_text,
)


logger = logging.getLogger(__name__)

MAX_MESSAGE_LENGTH = 20000
RECONNECT_BACKOFF = [2, 5, 10, 30, 60]
_SESSION_WEBHOOKS_MAX = 500
_DINGTALK_WEBHOOK_RE = re.compile(r'^https://(?:api|oapi)\.dingtalk\.com/')
_TRUTHY = {"true", "1", "yes", "on"}
_EMOTION_ID = "2659900"
_EMOTION_BG = "im_bg_1"
# recall? -> (TextEmotion model, Request model, Headers model, robot SDK method) — resolved
# on ``dingtalk_robot_models`` at call time.
_EMOTION_SDK = {
    True: ("RobotRecallEmotionRequestTextEmotion", "RobotRecallEmotionRequest", "RobotRecallEmotionHeaders", "robot_recall_emotion_with_options_async"),
    False: ("RobotReplyEmotionRequestTextEmotion", "RobotReplyEmotionRequest", "RobotReplyEmotionHeaders", "robot_reply_emotion_with_options_async"),
}


def _csv_set(raw: Any) -> Set[str]:
    """Split a list or comma-separated string into a set of stripped, non-empty items."""
    if isinstance(raw, list):
        return {str(part).strip() for part in raw if str(part).strip()}
    return {part.strip() for part in str(raw).split(",") if part.strip()}


def dingtalk_deps_present() -> bool:
    """PASSIVE registry ``check_fn``: are dingtalk-stream/httpx importable right now?

    Called from status displays and config loading, so it must never install anything;
    ``ensure_dingtalk_deps`` is the ACTIVE installer. Credentials are gated separately.
    """
    return DINGTALK_STREAM_AVAILABLE and HTTPX_AVAILABLE


def ensure_dingtalk_deps() -> bool:
    """ACTIVE deps-only installer (registry ``ensure_deps_fn``); rebinds module globals.

    Deliberately does NOT check credentials — otherwise a platform configured via
    ``PlatformConfig.extra`` would pass enablement and then be vetoed on env-var grounds
    before ever installing (deadlock). Credentials are gated by ``is_connected``.
    """
    global DINGTALK_STREAM_AVAILABLE, dingtalk_stream, ChatbotMessage, CallbackMessage, AckMessage
    global HTTPX_AVAILABLE, httpx
    if DINGTALK_STREAM_AVAILABLE and HTTPX_AVAILABLE:
        return True
    try:
        from tools.lazy_deps import ensure as _lazy_ensure
        _lazy_ensure("platform.dingtalk", prompt=False)
    except Exception:
        return False
    try:
        import dingtalk_stream as _ds
        from dingtalk_stream import ChatbotMessage as _CM
        from dingtalk_stream.frames import CallbackMessage as _CBM, AckMessage as _AM
        import httpx as _httpx
    except Exception:
        return False
    dingtalk_stream, ChatbotMessage, CallbackMessage, AckMessage, httpx = _ds, _CM, _CBM, _AM, _httpx
    DINGTALK_STREAM_AVAILABLE = HTTPX_AVAILABLE = True
    return True


def check_dingtalk_requirements() -> bool:
    """Combined deps (lazy-installed) + credentials check for setup/status callers."""
    if not ensure_dingtalk_deps():
        return False
    return bool(os.getenv("DINGTALK_CLIENT_ID") and _get_scoped_secret("DINGTALK_CLIENT_SECRET"))


class DingTalkAdapter(BasePlatformAdapter):
    """DingTalk chatbot adapter using Stream Mode.

    The dingtalk-stream SDK maintains a long-lived WebSocket; incoming messages arrive via a
    ChatbotHandler callback. Replies go through the message's session_webhook (httpx) or,
    when ``card_template_id`` is configured, streaming AI Cards.
    """

    MAX_MESSAGE_LENGTH = MAX_MESSAGE_LENGTH

    @property
    def SUPPORTS_MESSAGE_EDITING(self) -> bool:  # noqa: N802
        """Edits only exist with AI Cards; the gateway gates streaming cursor/edit on this."""
        return bool(self._card_template_id and self._card_sdk)

    @property
    def REQUIRES_EDIT_FINALIZE(self) -> bool:  # noqa: N802
        """AI Cards need an explicit ``finalize=True`` edit to close the streaming indicator."""
        return bool(self._card_template_id and self._card_sdk)

    def __init__(self, config: PlatformConfig):
        super().__init__(config, Platform.DINGTALK)

        extra = config.extra or {}
        self._client_id: str = extra.get("client_id") or os.getenv("DINGTALK_CLIENT_ID", "")
        self._client_secret: str = extra.get("client_secret") or _get_scoped_secret("DINGTALK_CLIENT_SECRET", "")

        # Group-chat gating; mention state is the SDK's structured ``is_in_at_list``, not text parsing.
        self._mention_patterns: List[re.Pattern] = self._compile_mention_patterns()
        self._allowed_users: Set[str] = self._load_allowed_users()

        self._stream_client: Any = None
        self._stream_task: Optional[asyncio.Task] = None
        self._http_client: Optional["httpx.AsyncClient"] = None
        self._card_sdk: Optional[Any] = None
        self._robot_sdk: Optional[Any] = None
        self._robot_code: str = extra.get("robot_code") or self._client_id

        self._dedup = MessageDeduplicator(max_size=1000)
        self._session_webhooks: Dict[str, tuple[str, int]] = {}  # chat_id -> (webhook, expired_time_ms)
        self._message_contexts: Dict[str, Any] = {}  # chat_id -> last inbound ChatbotMessage (per-chat: no clobber)
        self._card_template_id: Optional[str] = extra.get("card_template_id")
        # Chats whose Done reaction already fired this turn — prevents double-firing across
        # segment boundaries / parallel flows. Reset on each inbound message.
        self._done_emoji_fired: Set[str] = set()
        # Cards left open in streaming state: chat_id -> {out_track_id: last_content}.
        # ``edit_message(finalize=False)`` re-opens a finalized card (DingTalk allows it), so we
        # track them and auto-close as siblings on the next ``send()`` — otherwise tool-progress
        # cards stay stuck in streaming state forever.
        self._streaming_cards: Dict[str, Dict[str, str]] = {}
        # Fire-and-forget emoji tasks, kept referenced (GC) and cancellable on disconnect.
        self._bg_tasks: Set[asyncio.Task] = set()

    # -- Connection lifecycle -----------------------------------------------

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        """Connect to DingTalk via Stream Mode."""
        for ok, problem in (
            (DINGTALK_STREAM_AVAILABLE, "dingtalk-stream not installed. Run: pip install 'dingtalk-stream>=0.20'"),
            (HTTPX_AVAILABLE, "httpx not installed. Run: pip install httpx"),
            (self._client_id and self._client_secret, "DINGTALK_CLIENT_ID and DINGTALK_CLIENT_SECRET required"),
        ):
            if not ok:
                logger.warning("[%s] " + problem, self.name)
                return False

        try:
            # Tighter keepalive so idle CLOSE_WAIT drains promptly.
            from gateway.platforms._http_client_limits import platform_httpx_limits
            self._http_client = httpx.AsyncClient(timeout=30.0, limits=platform_httpx_limits())

            credential = dingtalk_stream.Credential(self._client_id, self._client_secret)
            self._stream_client = dingtalk_stream.DingTalkStreamClient(credential)

            if CARD_SDK_AVAILABLE:
                sdk_config = open_api_models.Config()
                sdk_config.protocol = "https"
                sdk_config.region_id = "central"
                if self._card_template_id:
                    self._card_sdk = dingtalk_card_client.Client(sdk_config)
                    self._robot_sdk = dingtalk_robot_client.Client(sdk_config)
                    logger.info("[%s] Card SDK initialized with template: %s", self.name, self._card_template_id)
                else:
                    # Robot SDK alone is still needed for media download.
                    self._robot_sdk = dingtalk_robot_client.Client(sdk_config)
                    logger.info("[%s] Robot SDK initialized (media download)", self.name)

            # Capture the current event loop for cross-thread dispatch
            handler = _IncomingHandler(self, asyncio.get_running_loop())
            self._stream_client.register_callback_handler(dingtalk_stream.ChatbotMessage.TOPIC, handler)

            self._stream_task = asyncio.create_task(self._run_stream())
            self._mark_connected()
            logger.info("[%s] Connected via Stream Mode", self.name)
            # Plugin-registered native handlers (DingTalkStreamClient — register_callback_handler()).
            self._wire_plugin_handlers(self._stream_client)
            return True
        except Exception as e:
            logger.error("[%s] Failed to connect: %s", self.name, e)
            return False

    async def _run_stream(self) -> None:
        """Run the async stream client with auto-reconnection."""
        backoff_idx = 0
        while self._running:
            try:
                logger.debug("[%s] Starting stream client...", self.name)
                await self._stream_client.start()
            except asyncio.CancelledError:
                return
            except Exception as e:
                if not self._running:
                    return
                logger.warning("[%s] Stream client error: %s", self.name, e)
            if not self._running:
                return
            delay = RECONNECT_BACKOFF[min(backoff_idx, len(RECONNECT_BACKOFF) - 1)]
            logger.info("[%s] Reconnecting in %ds...", self.name, delay)
            await asyncio.sleep(delay)
            backoff_idx += 1

    async def disconnect(self) -> None:
        """Disconnect from DingTalk."""
        self._running = False
        self._mark_disconnected()

        # Close the websocket first so the stream task sees the disconnect instead of
        # awaiting frames that never arrive.
        websocket = getattr(self._stream_client, "websocket", None) if self._stream_client else None
        if websocket is not None:
            try:
                await websocket.close()
            except Exception as e:
                logger.debug("[%s] websocket close during disconnect failed: %s", self.name, e)

        if self._stream_task:
            if hasattr(self._stream_client, "close"):
                try:
                    await asyncio.to_thread(self._stream_client.close)  # sync close() may block on I/O
                except Exception:
                    pass
            self._stream_task.cancel()
            try:
                await asyncio.wait_for(self._stream_task, timeout=5.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                logger.debug("[%s] stream task did not exit cleanly during disconnect", self.name)
            self._stream_task = None

        if self._bg_tasks:
            for task in list(self._bg_tasks):
                task.cancel()
            await asyncio.gather(*self._bg_tasks, return_exceptions=True)
            self._bg_tasks.clear()

        # Finalize open streaming cards BEFORE the HTTP client closes so they don't stay stuck
        # in streaming state after a gateway restart. Outer try guards the token fetch.
        for _chat_id in list(self._streaming_cards):
            try:
                await self._close_streaming_siblings(_chat_id)
            except Exception as _exc:
                logger.debug("[%s] Failed to finalize streaming card on disconnect for %s: %s", self.name, _chat_id, _exc)

        if self._http_client:
            await self._http_client.aclose()
            self._http_client = None

        self._stream_client = None
        self._session_webhooks.clear()
        self._message_contexts.clear()
        self._streaming_cards.clear()
        self._done_emoji_fired.clear()
        self._dedup.clear()
        logger.info("[%s] Disconnected", self.name)

    # -- Group gating --------------------------------------------------------

    def _dingtalk_require_mention(self) -> bool:
        """Whether group chats require an explicit bot trigger."""
        configured = self.config.extra.get("require_mention")
        if configured is not None:
            if isinstance(configured, str):
                return configured.lower() in _TRUTHY
            return bool(configured)
        return os.getenv("DINGTALK_REQUIRE_MENTION", "false").lower() in _TRUTHY

    def _dingtalk_free_response_chats(self) -> Set[str]:
        raw = self.config.extra.get("free_response_chats")
        if raw is None:
            raw = os.getenv("DINGTALK_FREE_RESPONSE_CHATS", "")
        return _csv_set(raw)

    def _extra_get(self, key: str):
        return self.config.extra.get(key) if self.config.extra else None

    def _csv_setting(self, key: str, env_name: str) -> Set[str]:
        """List/CSV setting from config.extra[key], falling back to the env var."""
        raw = self._extra_get(key)
        return _csv_set(os.getenv(env_name, "") if raw is None else raw)

    def _dingtalk_allowed_chats(self) -> Set[str]:
        """Group chat whitelist; non-empty = hard gate even when @mentioned. DMs never filtered."""
        return self._csv_setting("allowed_chats", "DINGTALK_ALLOWED_CHATS")

    def _compile_mention_patterns(self) -> List[re.Pattern]:
        """Compile optional regex wake-word patterns for group triggers."""
        patterns = self._extra_get("mention_patterns")
        if patterns is None:
            raw = os.getenv("DINGTALK_MENTION_PATTERNS", "").strip()
            if raw:
                try:
                    loaded = json.loads(raw)
                except Exception:
                    loaded = [part.strip() for part in raw.splitlines() if part.strip()]
                    if not loaded:
                        loaded = [part.strip() for part in raw.split(",") if part.strip()]
                patterns = loaded
        if patterns is None:
            # Return before touching ``self.name`` on the no-patterns path (historical parity).
            return []
        return compile_mention_patterns(
            patterns, log_prefix=self.name, platform_label="dingtalk", display_label="DingTalk", logger_=logger,
        )

    def _load_allowed_users(self) -> Set[str]:
        """Allowed-users from config.extra or env; matched case-insensitively; ``*`` disables."""
        return {item.lower() for item in self._csv_setting("allowed_users", "DINGTALK_ALLOWED_USERS")}

    def _is_user_allowed(self, sender_id: str, sender_staff_id: str) -> bool:
        if not self._allowed_users or "*" in self._allowed_users:
            return True
        candidates = {(sender_id or "").lower(), (sender_staff_id or "").lower()}
        candidates.discard("")
        return bool(candidates & self._allowed_users)

    def _message_mentions_bot(self, message: "ChatbotMessage") -> bool:
        """True if the bot was @-mentioned (SDK sets ``is_in_at_list``)."""
        return bool(getattr(message, "is_in_at_list", False))

    def _message_matches_mention_patterns(self, text: str) -> bool:
        return bool(text and self._mention_patterns) and any(p.search(text) for p in self._mention_patterns)

    def _should_process_message(self, message: "ChatbotMessage", text: str, is_group: bool, chat_id: str) -> bool:
        """Group trigger rules (DMs always pass; ``allowed_users`` is enforced earlier).

        Group messages are accepted when the chat passes ``allowed_chats`` (hard gate when set)
        and any of: chat in ``free_response_chats``, ``require_mention`` off, bot @mentioned,
        or text matches a wake-word pattern.
        """
        if not is_group:
            return True
        allowed = self._dingtalk_allowed_chats()
        if allowed and chat_id and chat_id not in allowed:
            return False
        if chat_id and chat_id in self._dingtalk_free_response_chats():
            return True
        if not self._dingtalk_require_mention():
            return True
        if self._message_mentions_bot(message):
            return True
        return self._message_matches_mention_patterns(text)

    def _spawn_bg(self, coro) -> None:
        """Start a fire-and-forget coroutine and track it for cleanup."""
        task = asyncio.create_task(coro)
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    # -- AI Card lifecycle helpers ------------------------------------------

    async def _close_streaming_siblings(self, chat_id: str) -> None:
        """Finalize previously-open streaming cards for this chat.

        Called at the start of every ``send()`` — there is no explicit "turn end" signal from
        the gateway, so this is what closes lingering tool-progress cards.
        """
        cards = self._streaming_cards.pop(chat_id, None)
        if not cards:
            return
        token = await self._get_access_token()
        if not token:
            return
        for out_track_id, last_content in list(cards.items()):
            try:
                await self._stream_card_content(out_track_id, token, last_content, finalize=True)
                logger.debug("[%s] AI Card sibling closed: %s", self.name, out_track_id)
            except Exception as e:
                logger.debug("[%s] Sibling close failed for %s: %s", self.name, out_track_id, e)

    def _fire_done_reaction(self, chat_id: str) -> None:
        """Swap 🤔Thinking → 🥳Done on the original user message; idempotent per chat_id."""
        if chat_id in self._done_emoji_fired:
            return
        self._done_emoji_fired.add(chat_id)
        msg = self._message_contexts.get(chat_id)
        if not msg:
            return
        msg_id = getattr(msg, "message_id", "") or ""
        conversation_id = getattr(msg, "conversation_id", "") or ""
        if not (msg_id and conversation_id):
            return

        async def _swap() -> None:
            await self._send_emotion(msg_id, conversation_id, "🤔Thinking", recall=True)
            await self._send_emotion(msg_id, conversation_id, "🥳Done", recall=False)

        self._spawn_bg(_swap())

    # -- Inbound message processing -----------------------------------------

    async def _on_message(self, message: "ChatbotMessage") -> None:
        """Process an incoming DingTalk chatbot message."""
        msg_id = getattr(message, "message_id", None) or uuid.uuid4().hex
        if self._dedup.is_duplicate(msg_id):
            logger.debug("[%s] Duplicate message %s, skipping", self.name, msg_id)
            return

        conversation_id = getattr(message, "conversation_id", "") or ""
        is_group = str(getattr(message, "conversation_type", "1")) == "2"
        sender_id = getattr(message, "sender_id", "") or ""
        sender_nick = getattr(message, "sender_nick", "") or sender_id
        sender_staff_id = getattr(message, "sender_staff_id", "") or ""
        chat_id = conversation_id or sender_id
        chat_type = "group" if is_group else "dm"

        if not self._is_user_allowed(sender_id, sender_staff_id):
            logger.debug(
                "[%s] Dropping message from non-allowlisted user staff_id=%s sender_id=%s",
                self.name, sender_staff_id, sender_id,
            )
            return

        # Group mention/pattern gate needs the text early for wake-word matching.
        _early_text = self._extract_text(message) or ""
        if not self._should_process_message(message, _early_text, is_group, chat_id):
            logger.debug(
                "[%s] Dropping group message that failed mention gate message_id=%s chat_id=%s",
                self.name, msg_id, chat_id,
            )
            return

        # Per-chat context; reset the Done marker so this message gets its own Thinking→Done cycle.
        if chat_id:
            self._message_contexts[chat_id] = message
            self._done_emoji_fired.discard(chat_id)

        session_webhook = getattr(message, "session_webhook", None) or ""
        session_webhook_expired_time = getattr(message, "session_webhook_expired_time", 0) or 0
        if session_webhook and chat_id and _DINGTALK_WEBHOOK_RE.match(session_webhook):
            if len(self._session_webhooks) >= _SESSION_WEBHOOKS_MAX:
                self._session_webhooks.pop(next(iter(self._session_webhooks)))  # evict oldest (dict is non-empty here)
            self._session_webhooks[chat_id] = (session_webhook, session_webhook_expired_time)

        # Resolve media download codes to URLs so vision tools can use them
        await self._resolve_media_codes(message)
        text = self._extract_text(message)
        msg_type, media_urls, media_types = self._extract_media(message)
        if not text and not media_urls:
            logger.debug("[%s] Empty message, skipping", self.name)
            return

        source = self.build_source(
            chat_id=chat_id, chat_name=getattr(message, "conversation_title", None), chat_type=chat_type,
            user_id=sender_id, user_name=sender_nick, user_id_alt=sender_staff_id if sender_staff_id else None,
        )

        create_at = getattr(message, "create_at", None)
        try:
            timestamp = (
                datetime.fromtimestamp(int(create_at) / 1000, tz=timezone.utc)
                if create_at
                else datetime.now(tz=timezone.utc)
            )
        except (ValueError, OSError, TypeError):
            timestamp = datetime.now(tz=timezone.utc)

        event = MessageEvent(
            text=text, message_type=msg_type, source=source, message_id=msg_id, raw_message=message,
            media_urls=media_urls, media_types=media_types, timestamp=timestamp,
        )
        logger.debug(
            "[%s] Message from %s in %s: %s",
            self.name, sender_nick, chat_id[:20] if chat_id else "?", text[:80] if text else "(media)",
        )
        await self.handle_message(event)

    _extract_text = staticmethod(extract_text)

    def _extract_media(self, message: "ChatbotMessage"):
        return extract_media(message)

    # -- Outbound messaging -------------------------------------------------

    async def send(
        self, chat_id: str, content: str, reply_to: Optional[str] = None, metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send a reply via AI Card (when configured) or DingTalk session webhook markdown."""
        metadata = metadata or {}
        logger.debug(
            "[%s] send() chat_id=%s card_enabled=%s",
            self.name, chat_id, bool(self._card_template_id and self._card_sdk),
        )

        session_webhook = metadata.get("session_webhook")
        if not session_webhook:
            webhook_info = self._get_valid_webhook(chat_id)
            if not webhook_info:
                logger.warning("[%s] No valid session_webhook for chat_id=%s", self.name, chat_id)
                return SendResult(
                    success=False,
                    error="No valid session_webhook available. Reply must follow an incoming message.",
                )
            session_webhook, _ = webhook_info

        if not self._http_client:
            return SendResult(success=False, error="HTTP client not initialized")

        current_message = self._message_contexts.get(chat_id)

        # ``reply_to`` is only set by base.py:_send_with_retry for the FINAL reply to an inbound
        # message; tool-progress, commentary and stream first-sends leave it None. It decides
        # (1) finalize-on-create (intermediate cards stay open so edits don't flicker) and
        # (2) whether to fire the Done reaction.
        is_final_reply = reply_to is not None

        if self._card_template_id and current_message and self._card_sdk:
            # Close lingering open cards (tool-progress → final handoff) before creating a new one.
            await self._close_streaming_siblings(chat_id)
            result = await self._create_and_stream_card(chat_id, current_message, content, finalize=is_final_reply)
            if result and result.success:
                if is_final_reply:
                    self._fire_done_reaction(chat_id)
                else:
                    # Keep open + track so the next send() auto-closes it, or edit_message(finalize=True) does.
                    self._streaming_cards.setdefault(chat_id, {})[result.message_id] = content
                return result
            logger.warning("[%s] AI Card send failed, falling back to webhook", self.name)

        logger.debug("[%s] Sending via webhook", self.name)
        normalized = self._normalize_markdown(content[: self.MAX_MESSAGE_LENGTH])
        payload = {
            "msgtype": "markdown",
            "markdown": {"title": "Hermes", "text": normalized},
        }
        try:
            resp = await self._http_client.post(session_webhook, json=payload, timeout=15.0)
            if resp.status_code < 300:
                if is_final_reply:
                    self._fire_done_reaction(chat_id)
                return SendResult(success=True, message_id=uuid.uuid4().hex[:12])
            body = resp.text
            logger.warning("[%s] Send failed HTTP %d: %s", self.name, resp.status_code, body[:200])
            return SendResult(success=False, error=f"HTTP {resp.status_code}: {body[:200]}")
        except httpx.TimeoutException:
            return SendResult(success=False, error="Timeout sending message to DingTalk")
        except Exception as e:
            logger.error("[%s] Send error: %s", self.name, e)
            return SendResult(success=False, error=str(e))

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        """DingTalk does not support typing indicators."""
        pass

    async def send_image(
        self, chat_id: str, image_url: str, caption: Optional[str] = None, reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Render a remote image inline via markdown (session webhook has no native attachments)."""
        image_block = f"![image]({image_url})"
        content = f"{caption}\n\n{image_block}" if caption else image_block
        return await self.send(chat_id=chat_id, content=content, reply_to=reply_to, metadata=metadata)

    async def send_image_file(
        self, chat_id: str, image_path: str, caption: Optional[str] = None, reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None, **kwargs,
    ) -> SendResult:
        """DingTalk webhook replies cannot send local image files directly."""
        return SendResult(
            success=False,
            error=(
                "DingTalk session webhook replies do not support local image uploads. "
                "Only markdown/text replies are supported without OpenAPI media upload."
            ),
        )

    async def send_document(
        self, chat_id: str, file_path: str, caption: Optional[str] = None, file_name: Optional[str] = None,
        reply_to: Optional[str] = None, metadata: Optional[Dict[str, Any]] = None, **kwargs,
    ) -> SendResult:
        """DingTalk webhook replies cannot send local file attachments directly."""
        return SendResult(
            success=False,
            error=(
                "DingTalk session webhook replies do not support local file attachments. "
                "Only markdown/text replies are supported without OpenAPI message send."
            ),
        )

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        """Return basic info about a DingTalk conversation."""
        return {"name": chat_id, "type": "group" if "group" in chat_id.lower() else "dm"}

    def _get_valid_webhook(self, chat_id: str) -> Optional[tuple[str, int]]:
        """Get a non-expired session webhook for chat_id (5-minute safety margin)."""
        info = self._session_webhooks.get(chat_id)
        if not info:
            return None
        webhook, expired_time_ms = info
        if expired_time_ms and expired_time_ms > 0:
            now_ms = int(datetime.now(tz=timezone.utc).timestamp() * 1000)
            if now_ms + 5 * 60 * 1000 >= expired_time_ms:
                self._session_webhooks.pop(chat_id, None)
                return None
        return info

    async def _create_and_stream_card(
        self, chat_id: str, message: Any, content: str, *, finalize: bool = True,
    ) -> Optional[SendResult]:
        """Create an AI Card, deliver it to the conversation, and stream initial content.

        ``finalize=False`` leaves the card open for ``edit_message`` streaming updates keyed by
        the returned out_track_id.
        """
        try:
            token = await self._get_access_token()
            if not token:
                return None

            out_track_id = f"hermes_{uuid.uuid4().hex[:12]}"
            conversation_id = getattr(message, "conversation_id", "") or ""
            is_group = str(getattr(message, "conversation_type", "1")) == "2"
            sender_staff_id = getattr(message, "sender_staff_id", "") or ""
            runtime = tea_util_models.RuntimeOptions()

            # Step 1: Create card with STREAM callback type
            create_request = dingtalk_card_models.CreateCardRequest(
                card_template_id=self._card_template_id,
                out_track_id=out_track_id,
                card_data=dingtalk_card_models.CreateCardRequestCardData(card_param_map={"content": ""}),
                callback_type="STREAM",
                im_group_open_space_model=dingtalk_card_models.CreateCardRequestImGroupOpenSpaceModel(support_forward=True),
                im_robot_open_space_model=dingtalk_card_models.CreateCardRequestImRobotOpenSpaceModel(support_forward=True),
            )
            create_headers = dingtalk_card_models.CreateCardHeaders(x_acs_dingtalk_access_token=token)
            await self._card_sdk.create_card_with_options_async(create_request, create_headers, runtime)

            # Step 2: Deliver card to the conversation
            if is_group:
                open_space_id = f"dtv1.card//IM_GROUP.{conversation_id}"
                deliver_model = {"im_group_open_deliver_model": (
                    dingtalk_card_models.DeliverCardRequestImGroupOpenDeliverModel(robot_code=self._robot_code)
                )}
            else:
                if not sender_staff_id:
                    logger.warning("[%s] AI Card skipped: missing sender_staff_id for DM", self.name)
                    return None
                open_space_id = f"dtv1.card//IM_ROBOT.{sender_staff_id}"
                deliver_model = {"im_robot_open_deliver_model": (
                    dingtalk_card_models.DeliverCardRequestImRobotOpenDeliverModel(space_type="IM_ROBOT")
                )}
            deliver_request = dingtalk_card_models.DeliverCardRequest(
                out_track_id=out_track_id, user_id_type=1, open_space_id=open_space_id, **deliver_model,
            )
            deliver_headers = dingtalk_card_models.DeliverCardHeaders(x_acs_dingtalk_access_token=token)
            await self._card_sdk.deliver_card_with_options_async(deliver_request, deliver_headers, runtime)

            # Step 3: Stream initial content (finalize=True closes the card immediately).
            await self._stream_card_content(out_track_id, token, content, finalize=finalize)
            logger.info(
                "[%s] AI Card %s: %s",
                self.name, "created+finalized" if finalize else "created (streaming)", out_track_id,
            )
            return SendResult(success=True, message_id=out_track_id)
        except Exception as e:
            logger.warning("[%s] AI Card create failed: %s\n%s", self.name, e, traceback.format_exc())
            return None

    async def edit_message(
        self, chat_id: str, message_id: str, content: str, *, finalize: bool = False,
    ) -> SendResult:
        """Edit an AI Card by streaming updated content.

        ``message_id`` is the out_track_id returned by the ``send()`` that created the card;
        callers track their own ids so parallel flows on one chat don't interfere.
        """
        if not message_id:
            return SendResult(success=False, error="message_id required")
        token = await self._get_access_token()
        if not token:
            return SendResult(success=False, error="No access token")
        try:
            await self._stream_card_content(message_id, token, content, finalize=finalize)
            if finalize:
                # Canonical "response ended" signal from the stream consumer's final edit.
                self._streaming_cards.get(chat_id, {}).pop(message_id, None)
                if not self._streaming_cards.get(chat_id):
                    self._streaming_cards.pop(chat_id, None)
                logger.debug("[%s] AI Card finalized (edit): %s", self.name, message_id)
                self._fire_done_reaction(chat_id)
            else:
                # Non-final edit reopens the card into streaming state — track for sibling close.
                self._streaming_cards.setdefault(chat_id, {})[message_id] = content
            return SendResult(success=True, message_id=message_id)
        except Exception as e:
            logger.warning("[%s] Card edit failed: %s", self.name, e)
            return SendResult(success=False, error=str(e))

    async def _stream_card_content(self, out_track_id: str, token: str, content: str, finalize: bool = False) -> None:
        """Stream content to an existing AI Card."""
        stream_request = dingtalk_card_models.StreamingUpdateRequest(
            out_track_id=out_track_id,
            guid=str(uuid.uuid4()),
            key="content",
            content=content[: self.MAX_MESSAGE_LENGTH],
            is_full=True,
            is_finalize=finalize,
            is_error=False,
        )
        stream_headers = dingtalk_card_models.StreamingUpdateHeaders(x_acs_dingtalk_access_token=token)
        runtime = tea_util_models.RuntimeOptions()
        await self._card_sdk.streaming_update_with_options_async(stream_request, stream_headers, runtime)

    async def _get_access_token(self) -> Optional[str]:
        """Get access token via the SDK's cached (sync, requests-based) getter."""
        if not self._stream_client:
            return None
        try:
            return await asyncio.to_thread(self._stream_client.get_access_token)
        except Exception as e:
            logger.error("[%s] Failed to get access token: %s", self.name, e)
            return None

    async def _send_emotion(
        self, open_msg_id: str, open_conversation_id: str, emoji_name: str, *, recall: bool = False,
    ) -> None:
        """Add (or recall) an emoji reaction on a message."""
        if not self._robot_sdk or not open_msg_id or not open_conversation_id:
            return
        action = "recall" if recall else "reply"
        try:
            token = await self._get_access_token()
            if not token:
                return
            text_emotion_cls, request_cls, headers_cls, sdk_method = _EMOTION_SDK[recall]
            emotion_kwargs = {
                "robot_code": self._robot_code,
                "open_msg_id": open_msg_id,
                "open_conversation_id": open_conversation_id,
                "emotion_type": 2,
                "emotion_name": emoji_name,
            }
            runtime = tea_util_models.RuntimeOptions()
            emotion_kwargs["text_emotion"] = getattr(dingtalk_robot_models, text_emotion_cls)(
                emotion_id=_EMOTION_ID,
                emotion_name=emoji_name,
                text=emoji_name,
                background_id=_EMOTION_BG,
            )
            request = getattr(dingtalk_robot_models, request_cls)(**emotion_kwargs)
            sdk_headers = getattr(dingtalk_robot_models, headers_cls)(x_acs_dingtalk_access_token=token)
            await getattr(self._robot_sdk, sdk_method)(request, sdk_headers, runtime)
            logger.info("[%s] _send_emotion: %s %s on msg=%s", self.name, action, emoji_name, open_msg_id[:24])
        except Exception:
            logger.debug("[%s] _send_emotion %s failed", self.name, action, exc_info=True)

    async def _resolve_media_codes(self, message: "ChatbotMessage") -> None:
        """Resolve download codes in the message to real URLs (in place, in parallel)."""
        token = await self._get_access_token()
        if not token:
            return
        robot_code = getattr(message, "robot_code", None) or self._client_id
        codes_to_resolve = collect_download_codes(message)
        if not codes_to_resolve:
            return
        tasks = []
        for obj, key in codes_to_resolve:
            code = getattr(obj, key, None) if hasattr(obj, key) else obj.get(key)
            if code:
                tasks.append(self._fetch_download_url(code, robot_code, token, obj, key))
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _fetch_download_url(self, code: str, robot_code: str, token: str, obj, key: str) -> None:
        """Fetch the download URL for one code via the robot SDK and write it back to ``obj[key]``."""
        if not self._robot_sdk:
            logger.warning("[%s] Robot SDK not initialized, cannot resolve media code", self.name)
            return
        try:
            request = dingtalk_robot_models.RobotMessageFileDownloadRequest(download_code=code, robot_code=robot_code)
            headers = dingtalk_robot_models.RobotMessageFileDownloadHeaders(x_acs_dingtalk_access_token=token)
            runtime = tea_util_models.RuntimeOptions()
            response = await self._robot_sdk.robot_message_file_download_with_options_async(request, headers, runtime)
            body = response.body if response else None
            if body:
                url = getattr(body, "download_url", None)
                if url:
                    if hasattr(obj, key):
                        setattr(obj, key, url)
                    elif isinstance(obj, dict):
                        obj[key] = url
            else:
                logger.warning("[%s] Failed to download media: empty response for code %s", self.name, code)
        except Exception as e:
            logger.error("[%s] Error resolving media code %s: %s", self.name, code, e)

    @staticmethod
    def _normalize_markdown(text: str) -> str:
        """Work around DingTalk renderer quirks: blank line before numbered lists, dedent ``` fences."""
        lines = text.split("\n")
        out = []
        for i, line in enumerate(lines):
            is_numbered = re.match(r"^\d+\.\s", line.strip())
            if is_numbered and i > 0:
                prev = lines[i - 1]
                if prev.strip() and not re.match(r"^\d+\.\s", prev.strip()):
                    out.append("")
            if line.strip().startswith("```") and line != line.lstrip():
                line = line[len(line) - len(line.lstrip()):]
            out.append(line)
        return "\n".join(out)


# ---------------------------------------------------------------------------
# Internal stream handler
# ---------------------------------------------------------------------------


class _IncomingHandler(dingtalk_stream.ChatbotHandler if DINGTALK_STREAM_AVAILABLE else object):
    """dingtalk-stream ChatbotHandler that forwards messages to the adapter.

    SDK >= 0.20: ``process()`` is async and receives a CallbackMessage whose ``.data`` dict we
    parse into a ChatbotMessage before forwarding.
    """

    def __init__(self, adapter: DingTalkAdapter, loop: Optional[asyncio.AbstractEventLoop] = None):
        if DINGTALK_STREAM_AVAILABLE:
            super().__init__()
        self._adapter = adapter
        self._loop = loop

    def pre_start(self) -> None:
        """No-op hook the SDK calls on every handler before opening the WebSocket (missing → AttributeError)."""
        return

    async def process(self, message: "CallbackMessage"):
        """SDK callback: convert to ChatbotMessage, then ACK immediately.

        Processing is dispatched as a background task — blocking here would stall the SDK's
        heartbeats and eventually disconnect the stream.
        """
        try:
            data = message.data
            if isinstance(data, str):
                data = json.loads(data)
            chatbot_msg = ChatbotMessage.from_dict(data)

            # Backfill fields from_dict() may not map (field names vary across SDK versions).
            if not getattr(chatbot_msg, "session_webhook", None):
                webhook = (data.get("sessionWebhook") or data.get("session_webhook") or "") if isinstance(data, dict) else ""
                if webhook:
                    chatbot_msg.session_webhook = webhook
            if not getattr(chatbot_msg, "is_in_at_list", False) and isinstance(data, dict) and data.get("isInAtList"):
                chatbot_msg.is_in_at_list = True

            msg_id = getattr(chatbot_msg, "message_id", None) or ""
            conversation_id = getattr(chatbot_msg, "conversation_id", None) or ""
            if msg_id and conversation_id:
                self._adapter._spawn_bg(self._adapter._send_emotion(msg_id, conversation_id, "🤔Thinking", recall=False))

            # _safe_on_message surfaces exceptions in logs instead of losing them in the loop.
            asyncio.create_task(self._safe_on_message(chatbot_msg))
        except Exception:
            logger.exception("[%s] Error preparing incoming message", self._adapter.name)
            return AckMessage.STATUS_SYSTEM_EXCEPTION, "error"
        return AckMessage.STATUS_OK, "OK"

    async def _safe_on_message(self, chatbot_msg: "ChatbotMessage") -> None:
        try:
            await self._adapter._on_message(chatbot_msg)
        except Exception:
            logger.exception("[%s] Error processing incoming message", self._adapter.name)


# ---------------------------------------------------------------------------
# Plugin glue: register(ctx) + hook implementations replacing the former
# per-platform core touchpoints (gateway/run.py, gateway/config.py,
# hermes_cli/gateway.py, tools/send_message_tool.py).
# ---------------------------------------------------------------------------


async def _standalone_send(pconfig, chat_id, message, *, thread_id=None, media_files=None, force_document=False):
    """Out-of-process delivery (standalone_sender_fn) via the static robot webhook URL.

    Per-session webhooks aren't available out-of-process (deliver=dingtalk cron jobs), so this
    uses DINGTALK_WEBHOOK_URL / extra ``webhook_url``.
    """
    extra = getattr(pconfig, "extra", {}) or {}
    try:
        import httpx
    except ImportError:
        return {"error": "httpx not installed"}
    try:
        webhook_url = extra.get("webhook_url") or os.getenv("DINGTALK_WEBHOOK_URL", "")
        if not webhook_url:
            return {"error": "DingTalk not configured. Set DINGTALK_WEBHOOK_URL env var or webhook_url in dingtalk platform extra config."}
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(webhook_url, json={"msgtype": "text", "text": {"content": message}})
            resp.raise_for_status()
            data = resp.json()
            if data.get("errcode", 0) != 0:
                return {"error": f"DingTalk API error: {data.get('errmsg', 'unknown')}"}
        return {"success": True, "platform": "dingtalk", "chat_id": chat_id}
    except Exception as e:
        # Redact access_token from webhook URLs in the exception text via send_message_tool._error
        # (lazy import avoids a circular at module load).
        try:
            from tools.send_message_tool import _error as _redact_error
            return _redact_error(f"DingTalk send failed: {e}")
        except Exception:
            return {"error": f"DingTalk send failed: {e}"}


def interactive_setup() -> None:
    """Configure DingTalk — QR scan (recommended) or manual credential entry."""
    from hermes_cli.config import get_env_value, save_env_value
    from hermes_cli.setup import prompt_choice
    from hermes_cli.cli_output import prompt, prompt_yes_no, print_header, print_success, print_warning

    print_header("DingTalk")
    existing = get_env_value("DINGTALK_CLIENT_ID")
    if existing:
        print_success(f"DingTalk is already configured (Client ID: {existing}).")
        if not prompt_yes_no("Reconfigure DingTalk?", False):
            return

    method = prompt_choice(
        "Choose setup method",
        ["QR Code Scan (Recommended, auto-obtain Client ID and Client Secret)", "Manual Input (Client ID and Client Secret)"],
        default=0,
    )
    if method == 0:
        try:
            from hermes_cli.dingtalk_auth import dingtalk_qr_auth
        except ImportError as exc:
            print_warning(f"QR auth module failed to load ({exc}), falling back to manual input.")
            _manual_credential_entry(prompt, save_env_value, print_success)
            return
        result = dingtalk_qr_auth()
        if result is None:
            print_warning("QR auth incomplete, falling back to manual input.")
            _manual_credential_entry(prompt, save_env_value, print_success)
            return
        client_id, client_secret = result
        save_env_value("DINGTALK_CLIENT_ID", client_id)
        save_env_value("DINGTALK_CLIENT_SECRET", client_secret)
        print_success("DingTalk configured via QR scan!")
    else:
        _manual_credential_entry(prompt, save_env_value, print_success)


def _manual_credential_entry(prompt, save_env_value, print_success) -> None:
    client_id = prompt("DingTalk Client ID (app key)")
    if not client_id:
        return
    save_env_value("DINGTALK_CLIENT_ID", client_id)
    client_secret = prompt("DingTalk Client Secret", password=True)
    if client_secret:
        save_env_value("DINGTALK_CLIENT_SECRET", client_secret)
    print_success("DingTalk credentials saved")


def _bridge_list_env(env_name: str, value) -> None:
    """Export a YAML list/scalar as a comma-joined env var unless the env var is already set."""
    if value is not None and not os.getenv(env_name):
        if isinstance(value, list):
            value = ",".join(str(v) for v in value)
        os.environ[env_name] = str(value)


def _apply_yaml_config(yaml_cfg: dict, dingtalk_cfg: dict) -> dict | None:
    """Translate config.yaml dingtalk: keys into DINGTALK_* env vars (apply_yaml_config_fn).

    Env vars take precedence over YAML. Returns None — everything flows through env.
    """
    import json as _json
    if "require_mention" in dingtalk_cfg and not os.getenv("DINGTALK_REQUIRE_MENTION"):
        os.environ["DINGTALK_REQUIRE_MENTION"] = str(dingtalk_cfg["require_mention"]).lower()
    if "mention_patterns" in dingtalk_cfg and not os.getenv("DINGTALK_MENTION_PATTERNS"):
        os.environ["DINGTALK_MENTION_PATTERNS"] = _json.dumps(dingtalk_cfg["mention_patterns"])
    _bridge_list_env("DINGTALK_FREE_RESPONSE_CHATS", dingtalk_cfg.get("free_response_chats"))
    _bridge_list_env("DINGTALK_ALLOWED_CHATS", dingtalk_cfg.get("allowed_chats"))
    allowed = dingtalk_cfg.get("allowed_users")
    if allowed is None:
        # The docs configure the allowlist at gateway.platforms.dingtalk.extra.allowed_users; the
        # adapter reads PlatformConfig.extra but gateway authz only consults DINGTALK_ALLOWED_USERS,
        # so bridge nested-only allowlists too: this block's own extra first (the dispatch loop
        # passes the platforms block when no top-level ``dingtalk:`` exists), then both containers.
        _extra = dingtalk_cfg.get("extra")
        if isinstance(_extra, dict):
            allowed = _extra.get("allowed_users")
        if allowed is None:
            _gw = yaml_cfg.get("gateway")
            _gw_platforms = _gw.get("platforms") if isinstance(_gw, dict) else None
            for _container in (_gw_platforms, yaml_cfg.get("platforms")):
                if not isinstance(_container, dict):
                    continue
                _dt = _container.get("dingtalk")
                _dt_extra = _dt.get("extra") if isinstance(_dt, dict) else None
                if isinstance(_dt_extra, dict) and _dt_extra.get("allowed_users") is not None:
                    allowed = _dt_extra.get("allowed_users")
                    break
    _bridge_list_env("DINGTALK_ALLOWED_USERS", allowed)
    return None


def _is_connected(config) -> bool:
    """Connected when client_id + client_secret are present (PlatformConfig.extra first, then env)."""
    extra = getattr(config, "extra", {}) or {}
    return bool(
        (extra.get("client_id") or os.getenv("DINGTALK_CLIENT_ID"))
        and (extra.get("client_secret") or _get_scoped_secret("DINGTALK_CLIENT_SECRET"))
    )


def _build_adapter(config):
    """Factory wrapper that constructs DingTalkAdapter from a PlatformConfig."""
    return DingTalkAdapter(config)


def register(ctx) -> None:
    """Plugin entry point — called by the Hermes plugin system."""
    ctx.register_platform(
        name="dingtalk",
        label="DingTalk",
        adapter_factory=_build_adapter,
        check_fn=dingtalk_deps_present,
        ensure_deps_fn=ensure_dingtalk_deps,
        is_connected=_is_connected,
        validate_config=_is_connected,
        required_env=["DINGTALK_CLIENT_ID", "DINGTALK_CLIENT_SECRET"],
        install_hint="pip install 'dingtalk-stream>=0.20' httpx",
        setup_fn=interactive_setup,
        apply_yaml_config_fn=_apply_yaml_config,
        allowed_users_env="DINGTALK_ALLOWED_USERS",
        allow_all_env="DINGTALK_ALLOW_ALL_USERS",
        cron_deliver_env_var="DINGTALK_HOME_CHANNEL",
        standalone_sender_fn=_standalone_send,
        emoji="🐳",
        allow_update_command=True,
    )
