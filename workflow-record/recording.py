"""Recording model: the in-page recorder, secret masking, normalization, save/list.

Everything here except ``save_recording`` / ``list_recordings`` is a pure function of its
inputs. The CDP transport lives in ``cdp.py`` and the ``hermes record`` command in ``cli.py``.

Recording JSON (``RECORDING_VERSION``)::

    {
      "version": 1,
      "started_at": "2026-10-21T12:00:00+00:00",
      "url": "https://example.com/start",
      "steps": [
        {"t": 0.0, "type": "click", "selector": "#login > button", "text": "Sign in"},
        {"t": 1.2, "type": "input", "selector": "input[name=\\"user\\"]", "value": "alice"},
        {"t": 2.0, "type": "input", "selector": "input[name=\\"pw\\"]", "value": "{SECRET:pw}"},
        {"t": 2.5, "type": "enter", "selector": "input[name=\\"pw\\"]"},
        {"t": 3.1, "type": "navigate", "url": "https://example.com/home"}
      ]
    }

``t`` is seconds since the first step. Step types: ``click``, ``input`` (final value on
``change``, not per keystroke), ``enter``, ``navigate`` (top frame) and ``manual``
(``hermes record --manual`` narration).

Secrets are masked twice. The recorder JS never reads the value of a password or
secret-autocomplete field: it ships a ``{SECRET:<field name>}`` placeholder, so the value
never crosses the CDP socket. ``normalize_event`` masks again from the field type and
autocomplete hint the recorder reports, so a page that swaps an input's type mid-flight, or
a hand-made event, still cannot put a raw password on disk.
"""

from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

RECORDING_VERSION = 1

# Anything else arriving over the binding is dropped (forward-compat with newer recorder JS).
KNOWN_EVENT_TYPES = ("click", "input", "enter", "navigate", "manual")

SECRET_INPUT_TYPES = ("password",)
SECRET_AUTOCOMPLETE = ("current-password", "new-password", "one-time-code", "cc-number", "cc-csc")

# ``autocomplete`` is a SPACE-SEPARATED token list in HTML, so a field that names a secret among
# other tokens ("username current-password", "section-login one-time-code") is a secret field too.
# Comparing the whole attribute string missed those: the recorder read the value, it crossed the
# CDP socket, and the second masking pass missed it too, so the plaintext secret landed in the
# recording file. One pattern for both sides - the recorder JS and the Python pass agree by
# construction and cannot drift apart.
_SECRET_AUTOCOMPLETE_RE = re.compile(
    # ``\-`` is an identity escape in Python but not in a strict JS RegExp, so the dash stays plain.
    r"(?:^|\s)(?:%s)(?:\s|$)" % "|".join(
        re.escape(token).replace(r"\-", "-") for token in SECRET_AUTOCOMPLETE),
    re.IGNORECASE,
)

BINDING_NAME = "__hermesRecord"

_SECRET_PLACEHOLDER_RE = re.compile(r"^\{SECRET:[^{}]*\}$")

# Injected with Page.addScriptToEvaluateOnNewDocument (survives navigations) and
# Runtime.evaluate (the page already loaded). Events go to Python through the
# Runtime.addBinding channel. Navigations are observed CDP-side (Page.frameNavigated).
RECORDER_JS = r"""
(() => {
  if (window.__hermesRecorderInstalled) return;
  window.__hermesRecorderInstalled = true;

  const SECRET_TYPES = %(secret_types)s;
  const SECRET_AUTOCOMPLETE = new RegExp(%(secret_autocomplete_pattern)s, "i");
  const BINDING = %(binding)s;

  const send = (ev) => {
    try {
      if (typeof window[BINDING] === "function") window[BINDING](JSON.stringify(ev));
    } catch (e) { /* the recorder must never break the page */ }
  };

  // CSS path: id, else a stable attribute hook, else nth-of-type up to the nearest id/body.
  const cssPath = (el) => {
    if (!(el instanceof Element)) return "";
    const parts = [];
    while (el && el.nodeType === Node.ELEMENT_NODE) {
      let part = el.nodeName.toLowerCase();
      if (el.id) {
        parts.unshift(part + "#" + CSS.escape(el.id));
        break;
      }
      const name = el.getAttribute("name");
      const testId = el.getAttribute("data-testid") || el.getAttribute("data-test-id");
      const aria = el.getAttribute("aria-label");
      if (testId) part += `[data-testid="${testId}"]`;
      else if (name) part += `[name="${name}"]`;
      else if (aria) part += `[aria-label="${aria}"]`;
      else {
        let sib = el, nth = 1;
        while ((sib = sib.previousElementSibling)) {
          if (sib.nodeName === el.nodeName) nth++;
        }
        if (nth > 1 || el.nextElementSibling) part += `:nth-of-type(${nth})`;
      }
      parts.unshift(part);
      el = el.parentElement;
    }
    return parts.join(" > ");
  };

  const lower = (v) => String(v || "").toLowerCase();
  const autocompleteOf = (el) => lower(el.getAttribute && el.getAttribute("autocomplete"));
  const isSecretField = (el) =>
    SECRET_TYPES.includes(lower(el.type)) || SECRET_AUTOCOMPLETE.test(autocompleteOf(el));
  const fieldName = (el) =>
    el.name || el.id || el.getAttribute("aria-label") || el.placeholder || el.type || "secret";

  // Visible label of a clicked element. Field values are never read here: a click on a
  // filled text or password field would otherwise copy what was typed into the step.
  const BUTTON_TYPES = ["button", "submit", "reset"];
  const clickText = (el) => {
    const tag = el.nodeName.toLowerCase();
    if (tag === "input") return BUTTON_TYPES.includes(lower(el.type)) ? String(el.value || "") : "";
    if (tag === "textarea" || tag === "select") return "";
    return String(el.innerText || "");
  };

  document.addEventListener("click", (e) => {
    const el = e.target instanceof Element ? e.target : null;
    if (!el) return;
    send({
      t: Date.now(),
      type: "click",
      selector: cssPath(el),
      tag: el.nodeName.toLowerCase(),
      inputType: lower(el.type),
      autocomplete: autocompleteOf(el),
      text: clickText(el).trim().slice(0, 120),
    });
  }, true);

  // Final value on change, not per keystroke. Secret fields are masked HERE: their value is never read.
  document.addEventListener("change", (e) => {
    const el = e.target;
    if (!el || typeof el.value === "undefined") return;
    const secret = isSecretField(el);
    send({
      t: Date.now(),
      type: "input",
      selector: cssPath(el),
      tag: lower(el.nodeName),
      inputType: lower(el.type),
      autocomplete: autocompleteOf(el),
      name: fieldName(el),
      value: secret ? "{SECRET:" + fieldName(el) + "}" : String(el.value).slice(0, 500),
    });
  }, true);

  document.addEventListener("keydown", (e) => {
    if (e.key !== "Enter") return;
    const el = e.target instanceof Element ? e.target : null;
    send({ t: Date.now(), type: "enter", selector: el ? cssPath(el) : "" });
  }, true);
})();
""" % {
    "secret_types": json.dumps(list(SECRET_INPUT_TYPES)),
    "secret_autocomplete_pattern": json.dumps(_SECRET_AUTOCOMPLETE_RE.pattern),
    "binding": json.dumps(BINDING_NAME),
}


def is_secret_placeholder(value: Any) -> bool:
    """True when ``value`` is already a ``{SECRET:name}`` placeholder."""
    return isinstance(value, str) and bool(_SECRET_PLACEHOLDER_RE.match(value))


def is_secret_field(input_type: str = "", autocomplete: str = "") -> bool:
    """Whether this field holds a secret: a secret input type, or a secret ``autocomplete`` token.

    ``autocomplete`` is a token list, so a field that names a secret among other tokens
    ("username current-password") is one too.
    """
    return (input_type or "").strip().lower() in SECRET_INPUT_TYPES or bool(
        _SECRET_AUTOCOMPLETE_RE.search(autocomplete or ""))


def mask_value(value: Any, input_type: str = "", autocomplete: str = "", name: str = "") -> str:
    """Storable form of an input value: a secret field's value becomes ``{SECRET:<name>}``."""
    text = "" if value is None else str(value)
    if is_secret_placeholder(text) or not is_secret_field(input_type, autocomplete):
        return text
    label = (name or "").strip() or (input_type or "").strip().lower() or "secret"
    return "{SECRET:" + label + "}"


def selector_for(event: Dict[str, Any]) -> str:
    """Selector with fallbacks: explicit selector, then tag (+ text hint), then ``*``; never ''."""
    selector = str(event.get("selector") or "").strip()
    if selector:
        return selector
    tag = str(event.get("tag") or "").strip().lower()
    text = str(event.get("text") or "").strip()
    if tag and text:
        return f"{tag}:contains({text[:40]!r})"
    return tag or "*"


_BUTTON_INPUT_TYPES = ("button", "submit", "reset")


def _is_field_click(raw: Dict[str, Any], input_type: str, autocomplete: str) -> bool:
    """A click on a form field, whose "text" could only be what was typed into it."""
    tag = str(raw.get("tag") or "").strip().lower()
    if tag in ("textarea", "select"):
        return True
    if tag == "input" and input_type.strip().lower() not in _BUTTON_INPUT_TYPES:
        return True
    return is_secret_field(input_type, autocomplete)


def normalize_event(raw: Any) -> Optional[Dict[str, Any]]:
    """One recorder/CDP event as a recording step, or None to drop it."""
    if not isinstance(raw, dict):
        return None
    etype = str(raw.get("type") or "").strip().lower()
    if etype not in KNOWN_EVENT_TYPES:
        return None
    try:
        t = float(raw.get("t") or 0.0)
    except (TypeError, ValueError):
        t = 0.0
    step: Dict[str, Any] = {"t": t, "type": etype}

    if etype in ("navigate", "manual"):
        key = "url" if etype == "navigate" else "text"
        value = str(raw.get(key) or "").strip()
        if not value:
            return None
        step[key] = value
        return step

    step["selector"] = selector_for(raw)
    input_type = str(raw.get("inputType") or "")
    autocomplete = str(raw.get("autocomplete") or "")
    if etype == "click":
        text = str(raw.get("text") or "").strip()
        if text and not _is_field_click(raw, input_type, autocomplete):
            step["text"] = text
    elif etype == "input":
        step["value"] = mask_value(raw.get("value"), input_type, autocomplete, str(raw.get("name") or ""))
    return step


def build_recording(url: str, started_at: str, events: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Normalize, order by capture time, and rebase ``t`` so the first step is 0.0 s.

    Epoch-millisecond inputs (what the recorder ships) become relative seconds; small
    already-relative values (manual steps) pass through.
    """
    steps = [s for s in (normalize_event(e) for e in events) if s is not None]
    steps.sort(key=lambda s: s["t"])
    if steps:
        base = steps[0]["t"]
        for s in steps:
            rel = s["t"] - base
            s["t"] = round(rel / 1000.0, 3) if base > 1e10 else round(rel, 3)
    return {"version": RECORDING_VERSION, "started_at": started_at, "url": url, "steps": steps}


def handle_cdp_message(msg: Dict[str, Any], events: List[Dict[str, Any]]) -> None:
    """Route one CDP message into the raw event buffer.

    ``Runtime.bindingCalled`` for our binding appends the recorder payload; a top-frame
    ``Page.frameNavigated`` appends a ``navigate`` event. Everything else is ignored.
    """
    method = msg.get("method")
    params = msg.get("params") or {}
    if method == "Runtime.bindingCalled" and params.get("name") == BINDING_NAME:
        try:
            payload = json.loads(params.get("payload") or "{}")
        except (json.JSONDecodeError, TypeError):
            return
        if isinstance(payload, dict):
            events.append(payload)
    elif method == "Page.frameNavigated":
        frame = params.get("frame") or {}
        if frame.get("parentId"):
            return
        url = str(frame.get("url") or "").strip()
        if url and not url.startswith(("about:", "chrome://", "devtools://", "chrome-error://")):
            events.append({"t": time.time() * 1000.0, "type": "navigate", "url": url})


def recordings_dir() -> Path:
    """``<HERMES_HOME>/recordings``, resolved per call so the active profile wins."""
    from hermes_constants import get_hermes_home

    return get_hermes_home() / "recordings"


def _restrict_to_owner(path: Path) -> None:
    """Make ``path`` readable and writable by its owner only (a no-op on Windows).

    Best-effort: a filesystem that does not carry POSIX modes (FAT, a network share) keeps the
    default permissions rather than failing the save.
    """
    try:
        os.chmod(path, 0o700 if path.is_dir() else 0o600)
    except OSError:
        pass


def save_recording(recording: Dict[str, Any], slug: str, directory: Optional[Path] = None) -> Path:
    """Write ``<dir>/<slug>-<UTC timestamp>.json`` and return its path.

    Owner-only: a recording holds what the user typed (a password the masking missed, an API key
    typed into a plain field, the prompts themselves), so neither the directory nor the file may be
    readable by another local account.
    """
    rec_dir = directory or recordings_dir()
    rec_dir.mkdir(parents=True, exist_ok=True)
    _restrict_to_owner(rec_dir)
    safe = re.sub(r"[^a-z0-9-]+", "-", (slug or "recording").strip().lower()).strip("-") or "recording"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    path = rec_dir / f"{safe}-{stamp}.json"
    n = 2
    while path.exists():
        path = rec_dir / f"{safe}-{stamp}-{n}.json"
        n += 1
    path.write_text(json.dumps(recording, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    _restrict_to_owner(path)
    return path


def list_recordings(directory: Optional[Path] = None) -> List[Dict[str, Any]]:
    """Saved recordings, newest first, with step counts and start URLs."""
    rec_dir = directory or recordings_dir()
    if not rec_dir.is_dir():
        return []
    out: List[Dict[str, Any]] = []
    for path in sorted(rec_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        entry: Dict[str, Any] = {"path": str(path), "name": path.name}
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
            entry.update(url=doc.get("url", ""), started_at=doc.get("started_at", ""),
                         steps=len(doc.get("steps") or []))
        except (OSError, ValueError, AttributeError):
            entry["error"] = "unreadable"
        out.append(entry)
    return out
