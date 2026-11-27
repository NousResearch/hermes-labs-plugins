"""A pool-only Copilot sign-in (``hermes auth add copilot``) recovers from an expired exchanged token
through core's runtime resolver and ``AIAgent``, for both provider spellings a session can carry, and
never saves or sends the GitHub grant as a bearer.

Harness adapted from @recoilburner's review on NousResearch/hermes-agent#154360. Only the network
exchange (``_fetch_exchange_with_retry``), the catalog read and the chat transport are replaced.
"""

from __future__ import annotations

import json
import socket
import time
from unittest.mock import patch

import pytest

from github_copilot_plugin.copilot import catalog, credentials

SIGN_IN = "ghu_FAKE" + "0" * 32
OLD_SIGN_IN = "ghu_FAKEold" + "9" * 29
STALE = "tid=FAKEstale;exp=1;proxy-ep=proxy.enterprise.githubcopilot.com"
FRESH = "tid=FAKEfresh;exp=9999999999;proxy-ep=proxy.enterprise.githubcopilot.com"
OLD_PASS = "tid=FAKEoldseat;exp=9999999999;proxy-ep=proxy.enterprise.githubcopilot.com"
API = "https://api.enterprise.githubcopilot.com"
MODEL = "claude-opus-5.5"
EXPIRED = "IDE token expired: unauthorized: token expired"
# The canonical name and the profile alias ``/model`` stores (``github-copilot``).
SPELLINGS = ("copilot", "github-copilot")


@pytest.fixture(autouse=True)
def catalog_reads(monkeypatch):
    """No socket leaves the process; catalog reads are recorded (and find nothing) instead of fetched.
    The home selects ``copilot`` in config.yaml, so core's left-core migration would fetch the catalog's
    released plugin into it; the hermetic-harness switch keeps that off."""
    def refuse(self, address):
        raise OSError(f"network disabled in this test: {address!r}")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setenv("HERMES_DISABLE_LAZY_INSTALLS", "1")
    reads = []
    monkeypatch.setattr(catalog, "fetch_github_model_catalog",
                        lambda api_key=None, timeout=5.0: reads.append(api_key))
    return reads


@pytest.fixture
def exchanges(monkeypatch):
    """The first exchange of SIGN_IN hands out STALE (the startup token, expired by the time it is
    sent); every later one hands out FRESH; OLD_SIGN_IN gets its own token."""
    calls = []

    def exchange(req, timeout, fp):
        grant = req.get_header("Authorization").split(" ", 1)[1]
        calls.append(grant)
        token = (STALE if calls.count(SIGN_IN) == 1 else FRESH) if grant == SIGN_IN else OLD_PASS
        return {"token": token, "expires_at": time.time() + 86400, "endpoints": {"api": API}}

    monkeypatch.setattr(credentials, "_fetch_exchange_with_retry", exchange)
    return calls


def _save_rows(home, grants):
    rows = [{"id": f"pool-{i}", "label": "seat", "auth_type": "api_key", "priority": i,
             "source": "manual:device_code", "access_token": grant, "base_url": "https://api.githubcopilot.com"}
            for i, grant in enumerate(grants)]
    (home / "auth.json").write_text(json.dumps({"version": 1, "credential_pool": {"copilot": rows}}))
    (home / "config.yaml").write_text(f"model:\n  provider: copilot\n  default: {MODEL}\n")
    from hermes_cli import config

    config._LOAD_CONFIG_CACHE.clear()
    config._RAW_CONFIG_CACHE.clear()


def _saved_grants(home):
    return [row["access_token"]
            for row in json.loads((home / "auth.json").read_text())["credential_pool"]["copilot"]]


def _chat(spelling, accepted, turns=1):
    """Resolve the runtime the way the gateway does, then run ``turns`` chats on one agent. The chat
    API accepts only bearers in ``accepted`` and answers any other with Copilot's expiry 401."""
    import httpx
    import openai

    from agent.credential_pool import credential_pool_matches_provider
    from hermes_cli.runtime_provider import resolve_runtime_provider
    from run_agent import AIAgent

    runtime = resolve_runtime_provider(requested=spelling, target_model=MODEL)
    pool = runtime.get("credential_pool")
    assert pool is not None and pool.provider == "copilot"
    # Capability, not version: a core whose agent drops the pool for this spelling cannot recover.
    if not credential_pool_matches_provider(pool, spelling, base_url=runtime["base_url"]):
        assert spelling != "copilot"
        pytest.skip(f"this core drops the copilot credential pool for the {spelling!r} spelling "
                    "(credential_pool_matches_provider); needs core's alias binding")
    bearers = []

    def answer(request):
        body = json.loads(request.content or b"{}")
        if "messages" not in body:
            return httpx.Response(404, text="not found")
        bearer = request.headers.get("authorization", "").removeprefix("Bearer ")
        bearers.append(bearer)
        if bearer not in accepted:
            return httpx.Response(401, text=EXPIRED)
        delta = {"role": "assistant", "content": "ok"}
        if body.get("stream"):
            chunks = [{"choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
                      {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}]
            sse = "".join("data: " + json.dumps({"id": "c", "object": "chat.completion.chunk", "created": 0,
                                                 "model": MODEL, **c}) + "\n\n" for c in chunks)
            return httpx.Response(200, text=sse + "data: [DONE]\n\n", headers={"content-type": "text/event-stream"})
        return httpx.Response(200, json={
            "id": "c", "object": "chat.completion", "created": 0, "model": MODEL,
            "choices": [{"index": 0, "message": delta, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}})

    def offline_openai(**kwargs):
        return openai.OpenAI(**{**kwargs, "http_client": httpx.Client(transport=httpx.MockTransport(answer))})

    with patch("agent.process_bootstrap.OpenAI", side_effect=offline_openai):
        agent = AIAgent(base_url=runtime["base_url"], model=MODEL, api_key=runtime["api_key"], provider=spelling,
                        api_mode="chat_completions", quiet_mode=True, skip_context_files=True, skip_memory=True,
                        enabled_toolsets=[], credential_pool=pool)
        replies = [agent.chat(f"hi {turn}") for turn in range(turns)]
    return replies, bearers


@pytest.mark.parametrize("spelling", SPELLINGS)
@pytest.mark.parametrize("grants", [[SIGN_IN], [SIGN_IN, OLD_SIGN_IN]], ids=["1row", "2rows"])
def test_expired_token_renews_the_selected_grant_once(hermes_home, exchanges, catalog_reads, spelling, grants):
    _save_rows(hermes_home, grants)

    replies, bearers = _chat(spelling, {FRESH, OLD_PASS}, turns=2)

    assert replies == ["ok", "ok"]
    assert bearers == [STALE, FRESH, FRESH]
    assert exchanges == [SIGN_IN, SIGN_IN]  # the startup exchange and one renewal, of the selected grant
    assert _saved_grants(hermes_home) == grants
    assert not set(grants) & set(bearers + catalog_reads)


@pytest.mark.parametrize("spelling", SPELLINGS)
def test_refused_renewed_token_does_not_loop(hermes_home, exchanges, catalog_reads, spelling):
    _save_rows(hermes_home, [SIGN_IN])

    replies, bearers = _chat(spelling, set())

    assert replies[0] != "ok"
    # Startup exchange plus core's capped same-row renewals: the turn ends instead of looping.
    assert len(bearers) <= 3 and len(exchanges) <= 4
    assert bearers[0] == STALE and set(bearers[1:]) == {FRESH} and set(exchanges) == {SIGN_IN}
    assert _saved_grants(hermes_home) == [SIGN_IN]
    assert SIGN_IN not in bearers + catalog_reads


@pytest.mark.parametrize("spelling", SPELLINGS)
def test_exchange_outage_never_sends_the_grant(hermes_home, monkeypatch, catalog_reads, spelling):
    _save_rows(hermes_home, [SIGN_IN])
    calls = []

    def exchange(req, timeout, fp):
        calls.append(fp)
        if len(calls) == 1:
            return {"token": STALE, "expires_at": time.time() + 86400, "endpoints": {"api": API}}
        raise ValueError("exchange down")

    monkeypatch.setattr(credentials, "_fetch_exchange_with_retry", exchange)

    replies, bearers = _chat(spelling, {FRESH})

    assert replies[0] != "ok"
    assert bearers == [STALE]
    assert _saved_grants(hermes_home) == [SIGN_IN]
    assert SIGN_IN not in bearers + catalog_reads
