"""The adapter's user-facing strings, in every language Hermes core shipped them in.

Core carried these keys in its bundled ``locales/*.yaml`` until Yuanbao left core; the plugin keeps the
same text and the same language choice (``HERMES_LANGUAGE`` from the profile's secrets, else
``display.language`` from its config.yaml, else English) without importing core's private i18n module.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

_MESSAGES: dict[str, dict[str, str]] = {
    'en': {
        'platform.yuanbao.slow_response_notice': "This task is a bit involved — still working on it, please hang on...",
        'platform.yuanbao.owner_command_denied': "⚠️ {command} is only available to the creator in private chat mode",
        'platform.shared.truncated_suffix': "\n...(truncated)",
    },
    'af': {
        'platform.yuanbao.slow_response_notice': "Hierdie taak is 'n bietjie omvattend — ek werk nog daaraan, hou asseblief aan...",
        'platform.yuanbao.owner_command_denied': "⚠️ {command} is slegs vir die skepper beskikbaar in privaatgesprek-modus",
        'platform.shared.truncated_suffix': "\n...(afgekap)",
    },
    'ar': {
        'platform.yuanbao.slow_response_notice': "هذه المهمة معقّدة قليلًا — ما زلت أعمل عليها، يُرجى الانتظار...",
        'platform.yuanbao.owner_command_denied': "⚠️ الأمر {command} متاح للمُنشئ فقط في وضع المحادثة الخاصة",
        'platform.shared.truncated_suffix': "\n...(مقتطع)",
    },
    'de': {
        'platform.yuanbao.slow_response_notice': "Diese Aufgabe ist etwas aufwendiger — ich arbeite noch daran, bitte einen Moment Geduld...",
        'platform.yuanbao.owner_command_denied': "⚠️ {command} ist im privaten Chatmodus nur für den Ersteller verfügbar",
        'platform.shared.truncated_suffix': "\n...(gekürzt)",
    },
    'es': {
        'platform.yuanbao.slow_response_notice': "Esta tarea es algo compleja — sigo trabajando en ella, un momento por favor...",
        'platform.yuanbao.owner_command_denied': "⚠️ {command} solo está disponible para el creador en modo de chat privado",
        'platform.shared.truncated_suffix': "\n...(truncado)",
    },
    'fr': {
        'platform.yuanbao.slow_response_notice': "Cette tâche est un peu complexe — je m'en occupe encore, merci de patienter...",
        'platform.yuanbao.owner_command_denied': "⚠️ {command} n'est disponible que pour le créateur en mode discussion privée",
        'platform.shared.truncated_suffix': "\n...(tronqué)",
    },
    'ga': {
        'platform.yuanbao.slow_response_notice': "Tá an tasc seo beagán casta — táim fós ag obair air, fan go fóill le do thoil...",
        'platform.yuanbao.owner_command_denied': "⚠️ Níl {command} ar fáil ach don chruthaitheoir i mód comhrá phríobháidigh",
        'platform.shared.truncated_suffix': "\n...(giorraithe)",
    },
    'hu': {
        'platform.yuanbao.slow_response_notice': "Ez a feladat kicsit összetettebb — még dolgozom rajta, kis türelmet...",
        'platform.yuanbao.owner_command_denied': "⚠️ A(z) {command} privát csevegés módban csak a létrehozó számára érhető el",
        'platform.shared.truncated_suffix': "\n...(csonkolva)",
    },
    'it': {
        'platform.yuanbao.slow_response_notice': "Questa attività è un po' impegnativa — ci sto ancora lavorando, attendi un attimo...",
        'platform.yuanbao.owner_command_denied': "⚠️ {command} è disponibile solo per il creatore in modalità chat privata",
        'platform.shared.truncated_suffix': "\n...(troncato)",
    },
    'ja': {
        'platform.yuanbao.slow_response_notice': "このタスクは少し手間がかかります — 処理を続けていますので、しばらくお待ちください...",
        'platform.yuanbao.owner_command_denied': "⚠️ {command} はプライベートチャットモードで作成者のみが使用できます",
        'platform.shared.truncated_suffix': "\n...(省略)",
    },
    'ko': {
        'platform.yuanbao.slow_response_notice': "이 작업은 조금 복잡해서 아직 진행 중이에요. 잠시만 기다려 주세요...",
        'platform.yuanbao.owner_command_denied': "⚠️ {command}은(는) 비공개 채팅 모드에서 생성자만 사용할 수 있어요",
        'platform.shared.truncated_suffix': "\n...(잘림)",
    },
    'pt': {
        'platform.yuanbao.slow_response_notice': "Esta tarefa é um pouco complexa — ainda estou trabalhando nela, aguarde um momento...",
        'platform.yuanbao.owner_command_denied': "⚠️ {command} só está disponível para o criador no modo de chat privado",
        'platform.shared.truncated_suffix': "\n...(truncado)",
    },
    'ru': {
        'platform.yuanbao.slow_response_notice': "Задача довольно объёмная — всё ещё работаю над ней, пожалуйста, подождите...",
        'platform.yuanbao.owner_command_denied': "⚠️ {command} доступна только создателю в режиме личного чата",
        'platform.shared.truncated_suffix': "\n...(обрезано)",
    },
    'tr': {
        'platform.yuanbao.slow_response_notice': "Bu görev biraz kapsamlı — hâlâ üzerinde çalışıyorum, lütfen bekleyin...",
        'platform.yuanbao.owner_command_denied': "⚠️ {command} yalnızca özel sohbet modunda oluşturucu tarafından kullanılabilir",
        'platform.shared.truncated_suffix': "\n...(kısaltıldı)",
    },
    'uk': {
        'platform.yuanbao.slow_response_notice': "Це завдання трохи складніше — я ще працюю над ним, зачекайте, будь ласка...",
        'platform.yuanbao.owner_command_denied': "⚠️ {command} доступна лише творцеві в режимі приватного чату",
        'platform.shared.truncated_suffix': "\n...(скорочено)",
    },
    'zh': {
        'platform.yuanbao.slow_response_notice': "任务有点复杂，正在努力处理中，请耐心等待...",
        'platform.yuanbao.owner_command_denied': "⚠️ {command} 仅在私聊模式下对创建者可用",
        'platform.shared.truncated_suffix': "\n...（已截断）",
    },
    'zh-hant': {
        'platform.yuanbao.slow_response_notice': "這個任務比較複雜 — 仍在處理中，請稍候...",
        'platform.yuanbao.owner_command_denied': "⚠️ {command} 僅限建立者在私聊模式下使用",
        'platform.shared.truncated_suffix': "\n...（已截斷）",
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
    key = _ALIASES.get(key, key)
    if key in _MESSAGES:
        return key
    base = key.split("-", 1)[0]
    return base if base in _MESSAGES else "en"


def t(key: str, **format_kwargs) -> str:
    """``key`` in the active language (English fallback, then the bare key), ``str.format``-ed."""
    value = _MESSAGES.get(language(), {}).get(key) or _MESSAGES["en"].get(key, key)
    if not format_kwargs:
        return value
    try:
        return value.format(**format_kwargs)
    except (KeyError, IndexError, ValueError):
        return value
