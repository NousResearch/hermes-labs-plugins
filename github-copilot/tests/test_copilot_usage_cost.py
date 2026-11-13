"""Copilot usage cost estimates (``CopilotProfile.get_usage_cost``).

Core has no Copilot rate card, so every Copilot call used to be recorded as ``unknown``. Copilot charges
Claude at Anthropic's list rates and GPT at OpenAI's (its ``/v1/messages`` responses carry the per-token
cost in ``copilot_usage.token_details``), so the profile prices a call at the vendor's list rate through
core's own tables and reports it as an estimate. These tests pin the dollar amounts for the models the
plugin is used with, the 1h cache-write premium, and that unknown models stay ``None`` (core: unknown).
"""

from __future__ import annotations

from decimal import Decimal

import pytest


@pytest.fixture
def copilot_profile():
    import providers

    profile = providers.get_provider_profile("copilot")
    assert profile is not None, "copilot provider profile must be registered"
    return profile


def _usage(**kwargs):
    from agent.usage_pricing import CanonicalUsage

    return CanonicalUsage(**kwargs)


def test_claude_priced_at_anthropic_list_rates(copilot_profile):
    # Opus 5.5: $4 in, $20 out, $0.20 cache read, $5 (5m) cache write per 1M tokens.
    result = copilot_profile.get_usage_cost(
        "claude-opus-5.5", _usage(input_tokens=1_000, output_tokens=2_000, cache_read_tokens=100_000,
                                  cache_write_tokens=10_000))
    assert result.amount_usd == Decimal("0.114")
    assert result.status == "estimated"
    assert result.label == "~$0.11"
    assert any("estimate" in note for note in result.notes)


def test_claude_one_hour_cache_writes_bill_at_twice_input(copilot_profile):
    # A 1h write bills at 2x input ($8/M on Opus 5.5), not the 5m rate ($5/M) core applies to every write.
    raw = {"input_tokens": 1_000, "cache_creation": {"ephemeral_1h_input_tokens": 10_000,
                                                     "ephemeral_5m_input_tokens": 0}}
    result = copilot_profile.get_usage_cost(
        "claude-opus-5.5", _usage(input_tokens=1_000, output_tokens=2_000, cache_read_tokens=100_000,
                                  cache_write_tokens=10_000, raw_usage=raw))
    assert result.amount_usd == Decimal("0.144")


def test_claude_on_chat_completions_notes_missing_write_premium(copilot_profile):
    raw = {"prompt_tokens": 101_000, "completion_tokens": 2_000}
    result = copilot_profile.get_usage_cost(
        "claude-opus-5.5", _usage(input_tokens=1_000, output_tokens=2_000, cache_read_tokens=100_000, raw_usage=raw))
    assert result.amount_usd == Decimal("0.064")
    assert any("chat_completions" in note for note in result.notes)


def test_gpt_priced_at_openai_list_rates(copilot_profile):
    # GPT-6.1 Sol: $2 in, $10 out, $0.10 cache read per 1M tokens (prompt under the 272K tier).
    result = copilot_profile.get_usage_cost(
        "gpt-6.1-sol", _usage(input_tokens=10_000, output_tokens=1_000, cache_read_tokens=50_000))
    assert result.amount_usd == Decimal("0.035")
    assert result.status == "estimated"


def test_gpt_long_context_tier(copilot_profile):
    # Above 272K prompt tokens the whole request bills at $4 in / $15 out.
    result = copilot_profile.get_usage_cost("gpt-6.1-sol", _usage(input_tokens=300_000, output_tokens=1_000))
    assert result.amount_usd == Decimal("1.215")


def test_sonnet_5_5_uses_the_rate_copilot_reports(copilot_profile):
    # Not in core's snapshot yet: $2 in / $10 out / $0.10 read / $2.50 write, from Copilot's cost details.
    result = copilot_profile.get_usage_cost(
        "claude-sonnet-5.5", _usage(input_tokens=1_000_000, output_tokens=100_000, cache_read_tokens=1_000_000,
                                    cache_write_tokens=100_000))
    assert result.amount_usd == Decimal("3.35")
    assert result.status == "estimated"


@pytest.mark.parametrize("model", ["gemini-3.1-pro", "grok-4", "claude-unreleased-9", "gpt-unknown-1"])
def test_unknown_models_return_none(copilot_profile, model):
    assert copilot_profile.get_usage_cost(model, _usage(input_tokens=1_000, output_tokens=1_000)) is None


def test_core_cost_path_uses_the_profile():
    from agent.usage_pricing import estimate_usage_cost

    for provider in ("copilot", "github-copilot"):
        result = estimate_usage_cost("claude-opus-5.5", _usage(input_tokens=1_000_000), provider=provider,
                                     base_url="https://api.githubcopilot.com")
        assert (result.status, result.amount_usd) == ("estimated", Decimal("4"))
    unknown = estimate_usage_cost("gemini-3.1-pro", _usage(input_tokens=1_000), provider="copilot",
                                  base_url="https://api.githubcopilot.com")
    assert (unknown.status, unknown.amount_usd) == ("unknown", None)


def test_pricing_errors_never_raise(copilot_profile, monkeypatch):
    from github_copilot_plugin.copilot import pricing

    def boom(*_a, **_k):
        raise RuntimeError("pricing table broken")

    monkeypatch.setattr(pricing, "_vendor_cost", boom)
    assert copilot_profile.get_usage_cost("claude-opus-5.5", _usage(input_tokens=1)) is None


def test_broken_pricing_module_import_returns_none(copilot_profile, monkeypatch):
    # A pricing module that fails to import (syntax error, missing core symbol) must not break a turn.
    import sys

    monkeypatch.setitem(sys.modules, "github_copilot_plugin.copilot.pricing", None)
    assert copilot_profile.get_usage_cost("claude-opus-5.5", _usage(input_tokens=1)) is None


def test_core_is_asked_for_the_vendor_dashed_id_before_the_observed_table(copilot_profile, monkeypatch):
    # Core's tables may carry a model only under the vendor's own spelling (claude-sonnet-5-5); that
    # beats the hand-kept observed rate.
    import agent.usage_pricing as usage_pricing

    real = usage_pricing.estimate_usage_cost
    asked = []

    def only_dashed(model, usage, **kwargs):
        asked.append(model)
        if model == "claude-sonnet-5-5":
            return real("claude-opus-5.5", usage, **kwargs)  # any priced result
        return usage_pricing.CostResult(amount_usd=None, status="unknown", source="none", label="n/a")

    monkeypatch.setattr(usage_pricing, "estimate_usage_cost", only_dashed)
    result = copilot_profile.get_usage_cost("claude-sonnet-5.5", _usage(input_tokens=1_000_000))
    assert asked[:2] == ["claude-sonnet-5.5", "claude-sonnet-5-5"]
    assert result.amount_usd == Decimal("4") and result.source != "custom_contract"


def test_observed_fallback_is_reported_as_custom_contract(copilot_profile, monkeypatch):
    import agent.usage_pricing as usage_pricing

    monkeypatch.setattr(usage_pricing, "estimate_usage_cost", lambda *_a, **_k: usage_pricing.CostResult(
        amount_usd=None, status="unknown", source="none", label="n/a"))
    result = copilot_profile.get_usage_cost("claude-sonnet-5.5", _usage(input_tokens=1_000_000))
    assert (result.amount_usd, result.status, result.source) == (Decimal("2"), "estimated", "custom_contract")
    assert result.pricing_version.startswith("copilot-observed-")


def test_estimate_note_says_it_is_an_estimate(copilot_profile):
    result = copilot_profile.get_usage_cost("gpt-6.1-sol", _usage(input_tokens=1_000))
    assert result.status == "estimated"
    assert any("vendor list rate" in note and "estimate" in note for note in result.notes)
