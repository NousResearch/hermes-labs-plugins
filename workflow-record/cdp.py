"""CDP transport for ``hermes record``: endpoint resolution and the recording loop.

Endpoint precedence mirrors ``/browser connect`` and the ``browser_cdp`` tool, minus the
live in-process override (``hermes record`` is its own process, so a ``/browser connect``
made inside a chat is invisible here):

1. ``--cdp URL`` on the command line
2. ``BROWSER_CDP_URL`` (environment or the profile's ``.env``)
3. ``browser.cdp_url`` in ``config.yaml``
4. the ``/browser connect`` default, ``http://127.0.0.1:9222``, when something answers there

The resolution is vendored rather than imported from ``tools.browser_tool_cdp``: those
helpers are core internals and not part of the plugin API.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import threading
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

from .recording import BINDING_NAME, RECORDER_JS, handle_cdp_message

DEFAULT_CDP_URL = "http://127.0.0.1:9222"


def _is_loopback(url: str) -> bool:
    host = (urllib.parse.urlsplit(url).hostname or "").strip("[]").lower()
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _http_get_json(url: str, timeout: float) -> Dict[str, Any]:
    # Loopback dials skip any system/env proxy (a proxy cannot reach the user's own browser).
    handlers = [urllib.request.ProxyHandler({})] if _is_loopback(url) else []
    opener = urllib.request.build_opener(*handlers)
    with opener.open(url, timeout=timeout) as resp:  # noqa: S310 - http(s) only, checked by caller
        return json.loads(resp.read().decode("utf-8"))


def discovery_url(raw: str) -> Optional[str]:
    """The ``/json/version`` URL for a CDP endpoint, or None when ``raw`` is already a full
    ``ws(s)://.../devtools/browser/<id>`` URL (or a ws URL with a path, used as given)."""
    value = (raw or "").strip()
    low = value.lower()
    if not value or "/devtools/" in low:
        return None
    if low.startswith(("ws://", "wss://")):
        parts = urllib.parse.urlsplit(value)
        if parts.path not in ("", "/"):
            return None
        scheme = "http" if low.startswith("ws://") else "https"
        # Relays in front of a browser carry their credential in the query
        # (``ws://host:9222/?token=...``); keep it, and keep it after the path.
        return f"{scheme}://{parts.netloc}/json/version" + (f"?{parts.query}" if parts.query else "")
    elif not low.startswith(("http://", "https://")):
        value = "http://" + value
    base, sep, query = value.partition("?")
    if not base.lower().endswith("/json/version"):
        base = base.rstrip("/") + "/json/version"
    return base + sep + query


def to_websocket_url(raw: str, timeout: float = 5.0) -> Tuple[str, str]:
    """``(ws_url, "")`` for a CDP endpoint, or ``("", reason)`` when it cannot be reached."""
    value = (raw or "").strip()
    version_url = discovery_url(value)
    if version_url is None:
        if value.lower().startswith(("ws://", "wss://")):
            return value, ""
        return "", f"not a CDP endpoint: {value!r}"
    try:
        payload = _http_get_json(version_url, timeout)
    except Exception as exc:  # noqa: BLE001 - any failure means "not reachable"; reported to the user
        return "", f"no browser answered at {version_url} ({exc})"
    ws_url = str(payload.get("webSocketDebuggerUrl") or "").strip()
    if not ws_url:
        return "", f"{version_url} did not return webSocketDebuggerUrl"
    return ws_url, ""


def configured_endpoint(explicit: Optional[str] = None) -> Tuple[str, str]:
    """``(raw endpoint, where it came from)``; falls back to the ``/browser connect`` default."""
    if explicit and explicit.strip():
        return explicit.strip(), "--cdp"
    from hermes_cli.config import get_env_value, load_config

    env = (get_env_value("BROWSER_CDP_URL") or "").strip()
    if env:
        return env, "BROWSER_CDP_URL"
    browser_cfg = (load_config() or {}).get("browser") or {}
    cfg = str(browser_cfg.get("cdp_url") or "").strip() if isinstance(browser_cfg, dict) else ""
    if cfg:
        return cfg, "browser.cdp_url"
    return DEFAULT_CDP_URL, "default"


def resolve_endpoint(explicit: Optional[str] = None) -> Tuple[str, str, str]:
    """``(ws_url, source, error)``; ``ws_url`` is '' with a human-readable ``error`` on failure."""
    raw, source = configured_endpoint(explicit)
    ws_url, error = to_websocket_url(raw)
    return ws_url, source, error


def pick_page_target(targets: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """The tab to record: the first web page (http/https/file), else the first other ``page``
    target that is not DevTools or an extension page (a blank new tab, say)."""
    pages = [t for t in targets if t.get("type") == "page"
             and not str(t.get("url", "")).startswith(("devtools://", "chrome-extension://"))]
    web = [t for t in pages if str(t.get("url", "")).startswith(("http://", "https://", "file://"))]
    return (web or pages or [None])[0]


async def record_over_cdp(
    ws_url: str,
    events: List[Dict[str, Any]],
    stop: threading.Event,
    state: Dict[str, Any],
) -> None:
    """Attach to the active tab, inject the recorder, buffer raw events until ``stop`` is set.

    Sets ``state["url"]`` (the tab's URL) and ``state["ready"]`` once the recorder is live.
    Normalization happens at save time (``recording.build_recording``).
    """
    import websockets  # declared in pyproject.toml; core ships it too

    connect_kwargs: Dict[str, Any] = {"max_size": None, "open_timeout": 15, "close_timeout": 5,
                                      "ping_interval": None}
    if _is_loopback(ws_url):
        connect_kwargs["proxy"] = None
    async with websockets.connect(ws_url, **connect_kwargs) as ws:
        next_id = 0

        async def call(method: str, params: Optional[Dict[str, Any]] = None,
                       session_id: Optional[str] = None, timeout: float = 15.0) -> Dict[str, Any]:
            nonlocal next_id
            next_id += 1
            call_id = next_id
            msg: Dict[str, Any] = {"id": call_id, "method": method, "params": params or {}}
            if session_id:
                msg["sessionId"] = session_id
            await ws.send(json.dumps(msg))
            loop = asyncio.get_running_loop()
            deadline = loop.time() + timeout
            while True:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    raise TimeoutError(f"CDP {method} timed out")
                reply = json.loads(await asyncio.wait_for(ws.recv(), timeout=remaining))
                if reply.get("id") == call_id:
                    if "error" in reply:
                        raise RuntimeError(f"CDP {method} failed: {reply['error']}")
                    return reply.get("result", {})
                handle_cdp_message(reply, events)

        targets = (await call("Target.getTargets")).get("targetInfos", [])
        page = pick_page_target(targets)
        if page is None:
            raise RuntimeError("no open tab in the connected browser; open one first")
        state["url"] = page.get("url", "")

        sid = (await call("Target.attachToTarget", {"targetId": page["targetId"], "flatten": True})).get("sessionId")
        if not sid:
            raise RuntimeError("Target.attachToTarget returned no sessionId")
        await call("Runtime.enable", session_id=sid)
        await call("Page.enable", session_id=sid)
        await call("Runtime.addBinding", {"name": BINDING_NAME}, session_id=sid)
        await call("Page.addScriptToEvaluateOnNewDocument", {"source": RECORDER_JS}, session_id=sid)
        await call("Runtime.evaluate", {"expression": RECORDER_JS}, session_id=sid)
        state["ready"] = True

        while not stop.is_set():
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=0.5)
            except asyncio.TimeoutError:
                continue
            handle_cdp_message(json.loads(raw), events)
