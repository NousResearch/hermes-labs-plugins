"""Usage cost for GitHub Copilot turns.

Copilot responses (``/chat/completions``, ``/responses``, ``/v1/messages``; streamed or not) carry a
top-level ``copilot_usage`` object: per-bucket ``token_details`` and ``total_nano_aiu``, the call's charge
(read as 1 USD = 1e11 nano-AIU). On a core that copies that field into ``usage.raw_usage``
(``ProviderProfile.usage_response_fields``), the call is recorded at that amount with cost status ``actual``.

Otherwise (an older core, or a response without the field) the call is priced at the model vendor's list
rate through core's own pricing tables (Copilot charges Claude at Anthropic's list rates and GPT at
OpenAI's) and reported as an estimate. Core has no Copilot rate card of its own
(``agent/usage_pricing.resolve_billing_route`` leaves provider ``copilot`` on billing mode ``unknown``).

The same profile serves the ``github-models`` aliases (``models.github.ai``). The cost hook is not told the
host, so the notes say "GitHub" rather than naming one product, and the estimate says it may not match
GitHub Models billing; a core that passes ``base_url`` lets the profile skip the estimate there.

This module imports nothing from the plugin package (only core), so scripts can load it by file path.
"""

from __future__ import annotations

import logging
import math
import re
from decimal import Decimal
from typing import Any, Optional

logger = logging.getLogger(__name__)

# The top-level response field carrying Copilot's own per-call accounting.
COPILOT_USAGE_FIELD = "copilot_usage"
REPORTED_NOTE = "GitHub-reported cost (copilot_usage.total_nano_aiu)"
_NANO_AIU_PER_USD = Decimal(10) ** 11
# A reported total above this (USD 1000 for one call) is treated as malformed: the estimate applies.
_MAX_REPORTED_NANO_AIU = 10**14
ESTIMATE_NOTE = "vendor list rate (estimate; GitHub Models routes may bill differently)"
ONE_HOUR_WRITE_NOTE = "1h cache writes priced at 2x input (Anthropic 1h TTL rate)"
CHAT_ROUTE_NOTE = "chat_completions route reports no cache writes; write premium not included"

# Anthropic bills a 1h cache write at 2x input and core prices every write at the 5m rate (1.25x
# input), so a 1h write needs another 0.75x input on top.
_ONE_HOUR_EXTRA_INPUT = Decimal("0.75")

# Copilot-observed list rates (USD per 1M tokens: input, output, cache read, 5m cache write) for
# models core's snapshot does not carry yet, read from ``copilot_usage.token_details[].cost_per_batch``
# (/ 1e11) on live ``/v1/messages`` responses. Used only when core cannot price the model itself.
_OBSERVED_VERSION = "copilot-observed-2026-10-29"
_OBSERVED_RATES: dict[tuple[str, str], tuple[str, str, str, str]] = {
    ("anthropic", "claude-sonnet-5-5"): ("2.00", "10.00", "0.10", "2.50"),
}
_OBSERVED_NOTE = "rate observed in Copilot's per-request cost details (not in core's pricing snapshot)"
_ONE_MILLION = Decimal(1_000_000)


def _bare(model: str) -> str:
    return (model or "").strip().lower().rsplit("/", 1)[-1]


def vendor_for_model(model: str) -> Optional[str]:
    """Billing route of the vendor whose list price Copilot charges for *model*, else None."""
    name = _bare(model)
    if name.startswith("claude-"):
        return "anthropic"
    if name.startswith("gpt-") or re.match(r"^o\d", name):
        return "openai"
    return None


def _dashed(model: str) -> str:
    """``claude-sonnet-5.5`` -> ``claude-sonnet-5-5`` (the vendor's own id spelling)."""
    return re.sub(r"(\d+)\.(\d+)", r"\1-\2", _bare(model))


def _observed_cost(model: str, vendor: str, usage: Any):
    from agent.usage_pricing import CostResult, format_cost_label

    rates = _OBSERVED_RATES.get((vendor, _dashed(model)))
    if rates is None:
        return None
    if isinstance(usage.raw_usage, dict) and usage.raw_usage.get("speed") == "fast":
        return None  # fast mode is a separate rate card
    inp, out, read, write = (Decimal(r) for r in rates)
    amount = sum((Decimal(tokens) * rate / _ONE_MILLION for tokens, rate in (
        (usage.input_tokens, inp), (usage.output_tokens, out),
        (usage.cache_read_tokens, read), (usage.cache_write_tokens, write))), Decimal(0))
    # A hand-kept rate table, not a provider cost API: ``custom_contract`` is core's source for that.
    return CostResult(amount_usd=amount, status="estimated", source="custom_contract",
                      label=format_cost_label(amount), pricing_version=_OBSERVED_VERSION, notes=(_OBSERVED_NOTE,))


def _vendor_cost(model: str, vendor: str, usage: Any):
    """Core's vendor-route estimate (Copilot's dotted id, then the vendor's dashed id), else the
    observed fallback; None when none of them prices it."""
    from agent.usage_pricing import estimate_usage_cost

    for name in dict.fromkeys((model, _dashed(model))):
        result = estimate_usage_cost(name, usage, provider=vendor)
        if result.amount_usd is not None and result.status == "estimated":
            return result
    return _observed_cost(model, vendor, usage)


def _one_hour_write_tokens(usage: Any) -> int:
    """Anthropic ``usage.cache_creation.ephemeral_1h_input_tokens`` (capped at the write bucket)."""
    raw = usage.raw_usage if isinstance(usage.raw_usage, dict) else None
    creation = raw.get("cache_creation") if raw else None
    if not isinstance(creation, dict):
        return 0
    try:
        tokens = int(creation.get("ephemeral_1h_input_tokens") or 0)
    except (TypeError, ValueError):
        return 0
    return max(0, min(tokens, int(usage.cache_write_tokens or 0)))


def _chat_route(usage: Any) -> bool:
    return isinstance(usage.raw_usage, dict) and "prompt_tokens" in usage.raw_usage


def copilot_usage_cost(model: str, usage: Any, *, estimate: bool = True):
    """``CostResult`` for one Copilot call: the amount GitHub reported (``actual``) when the usage carries
    ``copilot_usage.total_nano_aiu``, else (with ``estimate``) an estimate at the vendor's list rate, else
    None when the model's vendor or rate is unknown (core then reports ``unknown`` as before). Never raises:
    the cost hook runs on every API call, so a pricing bug must not break a turn."""
    try:
        return _reported_cost(usage) or (_copilot_usage_cost(model, usage) if estimate else None)
    except Exception:
        logger.debug("Copilot usage cost failed for %r", model, exc_info=True)
        return None


def reported_nano_aiu(usage: Any) -> Optional[int]:
    """``copilot_usage.total_nano_aiu`` from ``usage.raw_usage`` when it is a whole number (an int or an
    integral float) between 0 and ``_MAX_REPORTED_NANO_AIU``; None otherwise, so the estimate applies."""
    raw = getattr(usage, "raw_usage", None)
    report = raw.get(COPILOT_USAGE_FIELD) if isinstance(raw, dict) else None
    total = report.get("total_nano_aiu") if isinstance(report, dict) else None
    if isinstance(total, bool) or not isinstance(total, (int, float)):
        return None
    if isinstance(total, float) and (not math.isfinite(total) or not total.is_integer()):
        return None
    return int(total) if 0 <= total <= _MAX_REPORTED_NANO_AIU else None


def _reported_cost(usage: Any):
    """The amount GitHub reported for this call (``total_nano_aiu`` / 1e11 USD), status ``actual``."""
    from agent.usage_pricing import CostResult, format_cost_label

    total = reported_nano_aiu(usage)
    if total is None:
        return None
    amount = Decimal(total) / _NANO_AIU_PER_USD
    if not amount.is_finite():
        return None
    # Reported, not estimated: the label drops the "~" core puts on estimates.
    return CostResult(amount_usd=amount, status="actual", source="provider_cost_api",
                      label=format_cost_label(amount).lstrip("~"), notes=(REPORTED_NOTE,))


def _copilot_usage_cost(model: str, usage: Any):
    from agent.usage_pricing import CanonicalUsage, CostResult, format_cost_label

    vendor = vendor_for_model(model)
    if vendor is None or usage is None:
        return None
    base = _vendor_cost(model, vendor, usage)
    if base is None:
        return None
    amount, notes = base.amount_usd, [*base.notes, ESTIMATE_NOTE]
    if vendor == "anthropic":
        one_hour = _one_hour_write_tokens(usage)
        if one_hour:
            speed = usage.raw_usage.get("speed") if isinstance(usage.raw_usage, dict) else None
            extra = _vendor_cost(model, vendor, CanonicalUsage(
                input_tokens=Decimal(one_hour) * _ONE_HOUR_EXTRA_INPUT, raw_usage={"speed": speed} if speed else None))
            if extra is not None:
                amount += extra.amount_usd
                notes.append(ONE_HOUR_WRITE_NOTE)
        elif _chat_route(usage):
            notes.append(CHAT_ROUTE_NOTE)
    return CostResult(amount_usd=amount, status="estimated", source=base.source, label=format_cost_label(amount),
                      fetched_at=base.fetched_at, pricing_version=base.pricing_version, notes=tuple(notes))
