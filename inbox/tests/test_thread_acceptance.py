"""Operator acceptance of component-local truth, exact links and safe projection."""
from inbox_plugin import threads
from test_threads import _edge


def test_one_conflict_does_not_poison_an_unrelated_thread():
    a = _edge("a", trace="a")
    conflicting = dict(a, status="completed")
    independent = _edge("b", trace="b", status="completed")
    result = {t["id"]: t for t in threads.build_threads([a, conflicting, independent])}
    assert result["trace:a"]["execution"] == "unknown"
    assert result["trace:b"]["execution"] == "completed"


def test_unresolved_parent_is_local_to_its_component():
    result = {t["id"]: t for t in threads.build_threads([
        _edge("a", trace="a", parent="missing"), _edge("b", trace="b")])}
    assert result["trace:a"]["unresolved_parents"] == ["missing"]
    assert result["trace:b"]["unresolved_parents"] == []


def test_null_or_whitespace_traces_do_not_group_unrelated_edges():
    assert len(threads.build_threads([_edge("a", trace=None), _edge("b", trace=None)])) == 2
    assert len(threads.build_threads([_edge("a", trace=" "), _edge("b", trace=" ")])) == 2


def test_adapter_conflict_flag_is_honored():
    assert threads.build_threads([dict(_edge("a", status="completed"), source_conflict=True)])[0]["execution"] == "unknown"


def test_safe_projection_does_not_echo_a_prompt_or_reasoning():
    thread = threads.build_threads([dict(_edge("a"), message="secret prompt", reasoning="private", instructions="bad")])[0]
    assert not {"message", "reasoning", "instructions"} & set(thread["edges"][0])
