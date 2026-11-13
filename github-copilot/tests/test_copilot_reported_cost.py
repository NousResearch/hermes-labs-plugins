"""Copilot's own reported cost (``copilot_usage.total_nano_aiu``).

Copilot responses carry a top-level ``copilot_usage`` object on ``/chat/completions``, ``/responses`` and
``/v1/messages``, streamed or not; ``total_nano_aiu`` / 1e11 is read as the call's charge in USD. On a core
with ``ProviderProfile.usage_response_fields`` the profile declares that field, core copies it into
``usage.raw_usage``, and the call is recorded as ``actual``. On an older core (or a response without the
field) the vendor-list-rate estimate from ``copilot/pricing.py`` applies.

The samples below are recorded from live Copilot responses (token counts and rates as Copilot sent them).
"""

from __future__ import annotations

import dataclasses
import json
from decimal import Decimal

import pytest

from providers.base import ProviderProfile

CORE_HAS_USAGE_FIELDS = any(f.name == "usage_response_fields" for f in dataclasses.fields(ProviderProfile))
needs_usage_fields = pytest.mark.skipif(
    not CORE_HAS_USAGE_FIELDS, reason="needs ProviderProfile.usage_response_fields in hermes-agent")
COPILOT_URL = "https://api.githubcopilot.com"


def _details(model, inp, read, write, out, *, read_rate=20_000_000_000):
    rows = (("input", inp, 200_000_000_000), ("cache_read", read, read_rate), ("cache_write", write, 250_000_000_000),
            ("output", out, 1_000_000_000_000))
    return [{"batch_size": 1_000_000, "cost_per_batch": rate, "model": model, "token_count": n, "token_type": kind}
            for kind, n, rate in rows]


# Recorded non-streamed responses: (wire, model, api_mode, usage, copilot_usage, expected USD).
NON_STREAMED = [
    ("chat_completions", "claude-sonnet-5", "chat_completions",
     {"completion_tokens": 4, "prompt_tokens": 3156, "prompt_tokens_details": {"cached_tokens": 0},
      "total_tokens": 3160},
     {"token_details": _details("claude-sonnet-5", 9, 0, 3147, 4), "total_nano_aiu": 792_550_000},
     Decimal("0.0079255")),
    ("responses", "gpt-6.1-sol", "codex_responses",
     {"input_tokens": 2913, "input_tokens_details": {"cache_write_tokens": 2910, "cached_tokens": 0},
      "output_tokens": 5, "output_tokens_details": {"reasoning_tokens": 0}, "total_tokens": 2918},
     {"token_details": _details("gpt-6.1-sol", 3, 0, 2910, 5, read_rate=10_000_000_000),
      "total_nano_aiu": 733_100_000},
     Decimal("0.007331")),
    ("messages", "claude-sonnet-5", "anthropic_messages",
     {"cache_creation": {"ephemeral_1h_input_tokens": 0, "ephemeral_5m_input_tokens": 3389},
      "cache_creation_input_tokens": 3389, "cache_read_input_tokens": 0, "input_tokens": 9, "output_tokens": 5,
      "speed": "standard"},
     {"token_details": _details("claude-sonnet-5", 9, 0, 3389, 5), "total_nano_aiu": 854_050_000},
     Decimal("0.0085405")),
]

# Recorded streamed frames carrying copilot_usage: the final usage chunk (chat), response.completed
# (responses) and message_delta (messages).
STREAMED_CHAT_USAGE_CHUNK = {
    "id": "c", "object": "chat.completion.chunk", "created": 0, "model": "claude-sonnet-5", "choices": [],
    "usage": {"completion_tokens": 8, "prompt_tokens": 11, "prompt_tokens_details": {"cached_tokens": 0},
              "total_tokens": 19},
    "copilot_usage": {"token_details": _details("claude-sonnet-5", 11, 0, 0, 8), "total_nano_aiu": 10_200_000},
}
STREAMED_RESPONSES_COMPLETED_NANO_AIU = 6_800_000
STREAMED_MESSAGE_DELTA_NANO_AIU = 6_200_000


@pytest.fixture
def copilot_profile():
    import providers

    return providers.get_provider_profile("copilot")


def _canonical(usage, api_mode, copilot_usage=None):
    from agent.usage_pricing import normalize_usage

    canonical = normalize_usage(usage, provider="copilot", api_mode=api_mode)
    if copilot_usage is not None:
        canonical = dataclasses.replace(canonical, raw_usage={**(canonical.raw_usage or {}),
                                                              "copilot_usage": copilot_usage})
    return canonical


@pytest.mark.parametrize("wire,model,api_mode,usage,copilot_usage,expected", NON_STREAMED, ids=[s[0] for s in NON_STREAMED])
def test_reported_cost_is_actual_on_every_wire(copilot_profile, wire, model, api_mode, usage, copilot_usage, expected):
    result = copilot_profile.get_usage_cost(model, _canonical(usage, api_mode, copilot_usage))
    assert (result.status, result.source, result.amount_usd) == ("actual", "provider_cost_api", expected)
    assert not result.label.startswith("~")
    assert any("GitHub-reported cost" in note for note in result.notes)
    # The reported total equals the sum of token_count x cost_per_batch / batch_size over the buckets.
    per_bucket = sum(Decimal(d["token_count"]) * d["cost_per_batch"] / d["batch_size"] for d in copilot_usage["token_details"])
    assert per_bucket / Decimal(10) ** 11 == expected


@pytest.mark.parametrize("wire,model,api_mode,usage,copilot_usage,expected", NON_STREAMED, ids=[s[0] for s in NON_STREAMED])
def test_without_copilot_usage_the_estimate_applies(copilot_profile, wire, model, api_mode, usage, copilot_usage, expected):
    result = copilot_profile.get_usage_cost(model, _canonical(usage, api_mode))
    assert result.status == "estimated"


@pytest.mark.parametrize("bad", [
    None, "792550000", -1, True, float("nan"), float("inf"), 1.5, {"x": 1},
    10**320, 1e300, 10**15, 10**14 + 1,  # astronomic or above USD 1000 for one call
])
def test_malformed_reports_fall_back_to_the_estimate(copilot_profile, bad):
    usage = _canonical({"prompt_tokens": 100, "completion_tokens": 10}, "chat_completions",
                       {"total_nano_aiu": bad} if bad is not None else {})
    assert copilot_profile.get_usage_cost("claude-sonnet-5", usage).status == "estimated"
    # ...and a model with no vendor rate stays None (core: unknown), never an exception.
    assert copilot_profile.get_usage_cost("gemini-3.1-pro", usage) is None


@pytest.mark.parametrize("total,expected", [(10**14, Decimal(1000)), (1e9, Decimal("0.01")), (0, Decimal(0))])
def test_whole_totals_up_to_the_cap_are_reported(copilot_profile, total, expected):
    usage = _canonical({"prompt_tokens": 10, "completion_tokens": 1}, "chat_completions", {"total_nano_aiu": total})
    result = copilot_profile.get_usage_cost("claude-sonnet-5", usage)
    assert (result.status, result.amount_usd) == ("actual", expected)


def test_reported_cost_needs_no_known_model(copilot_profile):
    usage = _canonical({"prompt_tokens": 10, "completion_tokens": 1}, "chat_completions", {"total_nano_aiu": 0})
    result = copilot_profile.get_usage_cost("gemini-3.1-pro", usage)
    assert (result.status, result.amount_usd, result.label) == ("actual", Decimal(0), "$0.00")
    # A core that also passes the request's base URL to the hook still gets the reported cost.
    assert copilot_profile.get_usage_cost("gemini-3.1-pro", usage, base_url=COPILOT_URL).status == "actual"


def test_github_models_routes_skip_the_copilot_rate_estimate_when_the_host_is_known(copilot_profile):
    """The hook can't tell Copilot from GitHub Models unless core passes ``base_url``; when it does, a
    GitHub Models call without a reported amount gets core pricing (``None``), not Copilot's vendor rates."""
    plain = _canonical({"prompt_tokens": 100, "completion_tokens": 10}, "chat_completions")
    assert copilot_profile.get_usage_cost("gpt-6.1-sol", plain, base_url="https://models.github.ai/inference") is None
    assert copilot_profile.get_usage_cost("gpt-6.1-sol", plain, base_url=COPILOT_URL).status == "estimated"
    estimate = copilot_profile.get_usage_cost("gpt-6.1-sol", plain)  # today's core: no host
    assert any("GitHub Models routes may bill differently" in note for note in estimate.notes)
    reported = _canonical({"prompt_tokens": 10, "completion_tokens": 1}, "chat_completions", {"total_nano_aiu": 5})
    assert copilot_profile.get_usage_cost("gpt-6.1-sol", reported,
                                          base_url="https://models.github.ai/inference").status == "actual"


def test_the_profile_declares_copilot_usage_only_when_core_supports_it(copilot_profile):
    declared = getattr(copilot_profile, "usage_response_fields", None)
    assert declared == (("copilot_usage",) if CORE_HAS_USAGE_FIELDS else None)


@needs_usage_fields
def test_core_carries_the_field_from_each_wire_into_the_hook():
    """Core's own capture (``with_response_usage_fields``) on the recorded response shapes."""
    from types import SimpleNamespace

    from agent.usage_pricing import estimate_usage_cost, normalize_usage, with_response_usage_fields

    for _wire, model, api_mode, usage, copilot_usage, expected in NON_STREAMED:
        response = SimpleNamespace(usage=usage, copilot_usage=copilot_usage)
        canonical = with_response_usage_fields(normalize_usage(usage, provider="copilot", api_mode=api_mode),
                                               response, "copilot")
        result = estimate_usage_cost(model, canonical, provider="copilot", base_url=COPILOT_URL)
        assert (result.status, result.amount_usd) == ("actual", expected)


def _sse_response(body: str):
    import httpx

    return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})


def _offline_turn(api_mode: str, model: str, answer, *, anthropic_client=None):
    """One real offline ``AIAgent`` turn on the Copilot route (streamed, as Hermes sends it)."""
    from unittest.mock import patch

    import httpx
    import openai

    from run_agent import AIAgent

    def offline_openai(**kwargs):
        return openai.OpenAI(**{**kwargs, "http_client": httpx.Client(transport=httpx.MockTransport(answer))})

    with patch("agent.process_bootstrap.OpenAI", side_effect=offline_openai):
        agent = AIAgent(base_url=COPILOT_URL, model=model, api_key="tid=offline", provider="copilot",
                        api_mode=api_mode, quiet_mode=True, skip_context_files=True, skip_memory=True,
                        enabled_toolsets=[])
        if anthropic_client is not None:
            agent._anthropic_client = anthropic_client
            agent._create_request_anthropic_client = lambda *a, **k: anthropic_client
        try:
            agent.chat("hi")
            return agent.session_cost_status, agent.session_cost_source, agent.session_estimated_cost_usd
        finally:
            agent.close()


@needs_usage_fields
def test_streamed_chat_turn_records_the_reported_cost():
    """The final usage chunk's copilot_usage is what the session records, as ``actual``."""

    def answer(request):
        assert json.loads(request.content or b"{}").get("stream") is True
        chunks = [{"id": "c", "object": "chat.completion.chunk", "created": 0, "model": "claude-sonnet-5",
                   "choices": [{"index": 0, "delta": {"role": "assistant", "content": "ok"}, "finish_reason": None}]},
                  {"id": "c", "object": "chat.completion.chunk", "created": 0, "model": "claude-sonnet-5",
                   "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
                  STREAMED_CHAT_USAGE_CHUNK]
        return _sse_response("".join("data: " + json.dumps(c) + "\n\n" for c in chunks) + "data: [DONE]\n\n")

    status, source, cost = _offline_turn("chat_completions", "claude-sonnet-5", answer)
    assert (status, source) == ("actual", "provider_cost_api")
    assert cost == pytest.approx(0.000102)


@needs_usage_fields
def test_streamed_responses_turn_records_the_reported_cost():
    """``response.completed`` carries copilot_usage beside ``response``; the SDK keeps it in ``model_extra``."""

    def answer(request):
        item = {"type": "message", "id": "m1", "status": "completed", "role": "assistant",
                "content": [{"type": "output_text", "text": "ok", "annotations": []}]}
        completed = {"type": "response.completed", "sequence_number": 2,
                     "copilot_usage": {"token_details": _details("gpt-6.1-sol", 9, 0, 0, 5, read_rate=10_000_000_000),
                                       "total_nano_aiu": STREAMED_RESPONSES_COMPLETED_NANO_AIU},
                     "response": {"id": "r", "object": "response", "created_at": 0, "status": "completed",
                                  "model": "gpt-6.1-sol", "output": [], "parallel_tool_calls": False,
                                  "tool_choice": "auto", "tools": [],
                                  "usage": {"input_tokens": 9, "output_tokens": 5, "total_tokens": 14,
                                            "input_tokens_details": {"cached_tokens": 0},
                                            "output_tokens_details": {"reasoning_tokens": 0}}}}
        events = [{"type": "response.output_item.done", "output_index": 0, "sequence_number": 1, "item": item},
                  completed]
        return _sse_response("".join("data: " + json.dumps(e) + "\n\n" for e in events))

    status, source, cost = _offline_turn("codex_responses", "gpt-6.1-sol", answer)
    assert (status, source) == ("actual", "provider_cost_api")
    assert cost == pytest.approx(STREAMED_RESPONSES_COMPLETED_NANO_AIU / 1e11)


@needs_usage_fields
def test_streamed_messages_turn_records_the_reported_cost():
    """``message_delta`` carries copilot_usage; the Anthropic SDK's final snapshot drops it, core keeps it."""
    import anthropic
    import httpx

    events = [
        {"type": "message_start", "message": {"id": "msg", "type": "message", "role": "assistant", "content": [],
                                              "model": "claude-sonnet-5", "stop_reason": None, "stop_sequence": None,
                                              "usage": {"input_tokens": 9, "output_tokens": 0}}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "ok"}},
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None},
         "usage": {"output_tokens": 5},
         "copilot_usage": {"token_details": _details("claude-sonnet-5", 9, 0, 0, 5),
                           "total_nano_aiu": STREAMED_MESSAGE_DELTA_NANO_AIU}},
        {"type": "message_stop"},
    ]
    sse = "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events)
    client = anthropic.Anthropic(api_key="tid=offline", base_url=COPILOT_URL,
                                 http_client=httpx.Client(transport=httpx.MockTransport(lambda r: _sse_response(sse))))
    try:
        status, source, cost = _offline_turn("anthropic_messages", "claude-sonnet-5", lambda r: _sse_response(""),
                                             anthropic_client=client)
    finally:
        client.close()
    assert (status, source) == ("actual", "provider_cost_api")
    assert cost == pytest.approx(STREAMED_MESSAGE_DELTA_NANO_AIU / 1e11)
