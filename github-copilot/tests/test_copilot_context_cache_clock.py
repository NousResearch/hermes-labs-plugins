"""The context-window cache must not be defeated by a wall-clock jump."""
from github_copilot_plugin.copilot import catalog


def test_wall_clock_jump_backwards_does_not_serve_a_stale_window(monkeypatch):
    catalog._copilot_context_cache = {"m": 1000}
    catalog._copilot_context_cache_time = 100.0
    catalog._copilot_context_cache_key = None
    calls = []

    def fetch(*_a, **_k):
        calls.append(1)
        return [{"id": "m", "capabilities": {"limits": {"max_context_window_tokens": 2000}}}]

    monkeypatch.setattr(catalog, "fetch_github_model_catalog", fetch)
    monkeypatch.setattr(catalog.time, "time", lambda: 100.0)
    monkeypatch.setattr(catalog.time, "monotonic", lambda: 100.0)
    assert catalog.get_copilot_model_context("m") == 1000  # fresh under either clock

    # NTP correction / suspend-resume: wall clock goes back 2h, monotonic moves on 2h.
    monkeypatch.setattr(catalog.time, "time", lambda: -7100.0)
    monkeypatch.setattr(catalog.time, "monotonic", lambda: 7300.0)
    assert catalog.get_copilot_model_context("m") == 2000
    assert calls == [1]
