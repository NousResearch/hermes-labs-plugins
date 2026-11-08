"""Small self-contained helpers vendored from Hermes core, so the plugin depends only on documented plugin
surfaces (catalog rule 9). Each keeps the behaviour of the core function it copies (named per section);
none of them is part of any Hermes API.
"""

from __future__ import annotations

import getpass
import json
import urllib.request
from typing import Any, Optional, Sequence
from urllib.parse import urlparse

# ─── base URL host checks (core ``utils.base_url_hostname`` / ``base_url_host_matches``) ───


def _parse_base_url(base_url: str):
    raw = (base_url or "").strip()
    return urlparse(raw if "://" in raw else f"//{raw}") if raw else None


def base_url_hostname(base_url: str) -> str:
    """Lowercased hostname for a base URL (bare ``host[:port][/path]`` accepted), or ``""``."""
    parsed = _parse_base_url(base_url)
    return (parsed.hostname or "").lower().rstrip(".") if parsed else ""


def base_url_host_matches(base_url: str, domain: str) -> bool:
    """True when the base URL's hostname is ``domain`` or a subdomain (never a substring match:
    ``evil.com/githubcopilot.com`` and ``githubcopilot.com.evil`` do not match)."""
    hostname = base_url_hostname(base_url)
    domain = (domain or "").strip().lower().rstrip(".")
    return bool(hostname and domain) and (hostname == domain or hostname.endswith("." + domain))


# ─── reasoning-effort ladder (core ``agent.reasoning_effort``) ───

EFFORT_LADDER: tuple[str, ...] = ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra")
ASTRA_EFFORTS: tuple[str, ...] = ("low", "medium", "high", "xhigh", "max")
_ASTRA_MODEL_IDS = frozenset({"gpt-6-astra", "gpt-6-astra-900k"})


def is_astra_model(model: Optional[str]) -> bool:
    """``gpt-6-astra`` or its ``-900k`` alias, with or without a ``vendor/`` prefix."""
    return (model or "").strip().lower().rsplit("/", 1)[-1] in _ASTRA_MODEL_IDS


# ─── per-generation Responses ladders (core ``agent.reasoning_effort.codex_supported_efforts``) ───

GPT56_EFFORTS: tuple[str, ...] = ("none", "low", "medium", "high", "xhigh", "max")
# GPT-6 Sol/Luna: ``-pro``/``-900k``/dated snapshots share the prefix. GPT-6.1 Sol takes Astra's ladder, as
# does GPT-6.1 Terra: core does not list it, Copilot's /models serves it with Sol's ``low``..``max``.
_GPT6_TIER_PREFIXES = ("gpt-6-sol", "gpt-6-luna")
_NO_DISABLE_TIER_PREFIXES = ("gpt-6.1-sol", "gpt-6.1-terra")


def tier_efforts(model: Optional[str]) -> Optional[tuple[str, ...]]:
    """The effort ladder of a GPT-5.6 / GPT-6 tier model, or None for an older model."""
    bare = (model or "").strip().lower().rsplit("/", 1)[-1]
    if is_astra_model(bare) or bare.startswith(_NO_DISABLE_TIER_PREFIXES):
        return ASTRA_EFFORTS
    if "gpt-5.6" in bare or bare.startswith(_GPT6_TIER_PREFIXES):
        return GPT56_EFFORTS
    return None


def clamp_effort(effort: Optional[str], supported: Optional[Sequence[str]]) -> Optional[str]:
    """Clamp a requested effort onto *supported*: unchanged when supported, when the set is unknown, or
    when the request is not a ladder level; else the nearest WEAKER supported level (never "none"),
    or the weakest supported one when nothing weaker exists."""
    requested = str(effort or "").strip().lower()
    if not requested or not supported:
        return effort
    supported_norm = [lvl for lvl in (str(s).strip().lower() for s in supported) if lvl in EFFORT_LADDER]
    if not supported_norm or requested in supported_norm or requested not in EFFORT_LADDER:
        return effort
    candidates = [level for level in supported_norm if level != "none"]
    if not candidates:
        return effort
    requested_idx = EFFORT_LADDER.index(requested)
    below = [level for level in candidates if EFFORT_LADDER.index(level) < requested_idx]
    return max(below, key=EFFORT_LADDER.index) if below else min(candidates, key=EFFORT_LADDER.index)


# ─── catalog GET (core ``hermes_cli.models._get_json``) ───


def _origin(url: str) -> tuple[str, str, int]:
    parsed = urlparse(url)
    scheme = (parsed.scheme or "").lower()
    try:
        port = parsed.port or (443 if scheme == "https" else 80)
    except ValueError:
        port = 0
    return scheme, (parsed.hostname or "").lower(), port


class _NoCredentialHopRedirect(urllib.request.HTTPRedirectHandler):
    """Follow redirects, but never carry the ``Authorization`` header to another origin."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None and _origin(newurl) != _origin(req.full_url):
            for name in [h for h in new.headers if h.lower() == "authorization"]:
                del new.headers[name]
        return new


def open_url(req: urllib.request.Request, *, timeout: float):
    """Open a credentialed GET without forwarding its bearer token across origins."""
    return urllib.request.build_opener(_NoCredentialHopRedirect).open(req, timeout=timeout)


def get_json(url: str, *, timeout: float, headers: Optional[dict[str, str]] = None) -> Any:
    """GET ``url`` and parse the JSON body. Raises on any failure."""
    req = urllib.request.Request(url, headers=headers or {})
    with open_url(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


# ─── setup prompts (core ``hermes_cli.model_setup_flows_common._say`` / ``_ask``) ───


def say(*lines: str) -> None:
    """``print`` each line (``""`` = blank line); one call per banner block."""
    print("\n".join(lines))


def ask(prompt: str, *, secret: bool = False, cancel_msg: str | None = "\nCancelled."):
    """One stripped answer from builtin ``input`` (``secret``: no echo). Ctrl-C/EOF prints *cancel_msg*
    (``None`` = silent) and returns None."""
    try:
        return (getpass.getpass(prompt) if secret else input(prompt)).strip()
    except (KeyboardInterrupt, EOFError):
        if cancel_msg is not None:
            print(cancel_msg)
        return None
