"""The adapter's user-facing strings, in every language Hermes core shipped them in.

Core carried these keys in its bundled ``locales/*.yaml`` until DingTalk left core. The plugin
keeps those texts as defaults and honors the core catalog's profile-specific translations.
"""

from __future__ import annotations

import logging
import os

from agent.i18n import resolve_language_id
from agent.i18n import t as core_t

logger = logging.getLogger(__name__)

_MESSAGES: dict[str, dict[str, str]] = {
    'en': {
        'platform.dingtalk.emotion.thinking': "🤔Thinking",
        'platform.dingtalk.emotion.done': "🥳Done",
    },
    'af': {
        'platform.dingtalk.emotion.thinking': "🤔Dink",
        'platform.dingtalk.emotion.done': "🥳Klaar",
    },
    'ar': {
        'platform.dingtalk.emotion.thinking': "🤔يفكّر",
        'platform.dingtalk.emotion.done': "🥳تم",
    },
    'de': {
        'platform.dingtalk.emotion.thinking': "🤔Denke nach",
        'platform.dingtalk.emotion.done': "🥳Fertig",
    },
    'es': {
        'platform.dingtalk.emotion.thinking': "🤔Pensando",
        'platform.dingtalk.emotion.done': "🥳Listo",
    },
    'fr': {
        'platform.dingtalk.emotion.thinking': "🤔Réflexion",
        'platform.dingtalk.emotion.done': "🥳Terminé",
    },
    'ga': {
        'platform.dingtalk.emotion.thinking': "🤔Ag smaoineamh",
        'platform.dingtalk.emotion.done': "🥳Déanta",
    },
    'hu': {
        'platform.dingtalk.emotion.thinking': "🤔Gondolkodom",
        'platform.dingtalk.emotion.done': "🥳Kész",
    },
    'it': {
        'platform.dingtalk.emotion.thinking': "🤔Sto ragionando",
        'platform.dingtalk.emotion.done': "🥳Fatto",
    },
    'ja': {
        'platform.dingtalk.emotion.thinking': "🤔考え中",
        'platform.dingtalk.emotion.done': "🥳完了",
    },
    'ko': {
        'platform.dingtalk.emotion.thinking': "🤔생각 중",
        'platform.dingtalk.emotion.done': "🥳완료",
    },
    'pt': {
        'platform.dingtalk.emotion.thinking': "🤔Pensando",
        'platform.dingtalk.emotion.done': "🥳Concluído",
    },
    'ru': {
        'platform.dingtalk.emotion.thinking': "🤔Думаю",
        'platform.dingtalk.emotion.done': "🥳Готово",
    },
    'tr': {
        'platform.dingtalk.emotion.thinking': "🤔Düşünüyor",
        'platform.dingtalk.emotion.done': "🥳Tamamlandı",
    },
    'uk': {
        'platform.dingtalk.emotion.thinking': "🤔Думаю",
        'platform.dingtalk.emotion.done': "🥳Готово",
    },
    'zh-hant': {
        'platform.dingtalk.emotion.thinking': "🤔思考中",
        'platform.dingtalk.emotion.done': "🥳完成",
    },
    'zh': {
        'platform.dingtalk.emotion.thinking': "🤔思考中",
        'platform.dingtalk.emotion.done': "🥳完成",
    },
}

# Spellings core's language picker accepted for these packs (``agent.i18n._LANGUAGE_ALIASES``).
_ALIASES = {
    "chinese": "zh", "mandarin": "zh", "zh-cn": "zh", "zh-hans": "zh", "zh-sg": "zh",
    "traditional-chinese": "zh-hant", "traditional_chinese": "zh-hant",
    "zh-tw": "zh-hant", "zh-hk": "zh-hant", "zh-mo": "zh-hant",
}


def _language_setting() -> str:
    from agent.secret_scope import UnscopedSecretError, get_secret
    try:
        value = get_secret("HERMES_LANGUAGE")
    except UnscopedSecretError:  # unscoped default-profile path: the process env is that profile's own value
        value = os.environ.get("HERMES_LANGUAGE")
    if value:
        return str(value)
    try:
        from hermes_cli.config import load_config_readonly
        return str((load_config_readonly().get("display") or {}).get("language") or "")
    except Exception:  # an unreadable config.yaml must never break a reply; core's t() degrades the same way
        logger.debug("Could not read display.language", exc_info=True)
        return ""


def language() -> str:
    """The active profile's language id among the ones this module carries, else ``en``."""
    key = _language_setting().strip().lower().replace("_", "-")
    key = resolve_language_id(key) or _ALIASES.get(key, key)
    if key in _MESSAGES:
        return key
    base = key.split("-", 1)[0]
    return base if base in _MESSAGES else "en"


def t(key: str, **format_kwargs) -> str:
    """Honor the core catalog before falling back to DingTalk's bundled defaults."""
    value = core_t(key)
    if value == key:
        value = _MESSAGES.get(language(), {}).get(key) or _MESSAGES["en"].get(key, key)
    if not format_kwargs:
        return value
    try:
        return value.format(**format_kwargs)
    except (KeyError, IndexError, ValueError):
        return value
