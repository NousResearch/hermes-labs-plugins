from __future__ import annotations

import pytest
from inbox_plugin import threads as t


def _edge(edge_id, origin="alex", target="felix", trace="", parent="", status="dispatched",
          delivery="pending", run_id="run-1"):
    return {
        "edge_id": edge_id,
        "run_id": run_id,
        "origin_profile": origin,
        "target_profile": target,
        "dispatch_type": "dispatch",
        "delivery": delivery,
        "status": status,
        "dispatched_at": "2026-10-22T09:00:00+00:00",
        "updated_at": "2026-10-22T09:00:05+00:00",
        "trace_id": trace,
        "parent_edge_id": parent,
        "hop_count": 1,
        "max_hops": None,
        "origin_session_id": "",
        "observed_in": ["local"],
        "source_conflict": False,
    }


def test_trace_group():
    edges = [_edge("e1", trace="m1"), _edge("e2", trace="m1")]
    result = t.build_threads(edges)
    assert len(result) == 1
    assert result[0]["id"] == "trace:m1"
    assert result[0]["edge_ids"] == ["alex:e1", "alex:e2"]


def test_parent_link():
    parent = _edge("p", trace="m1")
    child = _edge("c", trace="m2", parent="p")
    result = t.build_threads([parent, child])
    assert len(result) == 1
    assert result[0]["unresolved_parents"] == []


def test_separate_titles_not_grouped():
    e1 = _edge("e1", trace="m1", status="dispatched")
    e2 = _edge("e2", trace="m2", status="completed")
    result = t.build_threads([e1, e2])
    assert len(result) == 2


def test_same_run_id_across_origins_not_merged():
    e1 = _edge("e1", origin="alex", target="felix", trace="", run_id="run-1")
    e2 = _edge("e2", origin="felix", target="alex", trace="", run_id="run-1")
    result = t.build_threads([e1, e2])
    assert len(result) == 2


def test_empty_trace_unlinked():
    e1 = _edge("e1", trace="")
    e2 = _edge("e2", trace="", parent="")
    result = t.build_threads([e1, e2])
    assert len(result) == 2
    assert result[0]["unresolved_parents"] == []


def test_ambiguous_parent_not_linked():
    e1 = _edge("p1", trace="")
    e2 = _edge("p1", origin="sam", trace="")
    child = _edge("c", trace="", parent="p1")
    result = t.build_threads([e1, e2, child])
    assert len(result) == 3
    assert next(t for t in result if t["id"] == "edge:alex:c")["unresolved_parents"] == ["p1"]


def test_source_conflict_unknown():
    e1 = _edge("e1", trace="m1", status="dispatched")
    e2 = _edge("e1", origin="alex", trace="m1", status="completed")  # conflicting duplicate
    result = t.build_threads([e1, e2])
    assert len(result) == 1
    assert result[0]["execution"] == "unknown"
    assert result[0]["source_conflict"] is True


def test_stable_order():
    e1 = _edge("e1", trace="m1")
    e2 = _edge("e2", trace="m2")
    result = t.build_threads([e2, e1])
    assert [r["id"] for r in result] == ["trace:m1", "trace:m2"]


def test_execution_completed_does_not_prove_outcome_or_delivery():
    e = _edge("e1", trace="m1", status="completed", delivery="pending")
    result = t.build_threads([e])
    assert result[0]["execution"] == "completed"
    assert result[0]["outcome"] == "unknown"
    assert result[0]["delivery"] == ["unknown"]
    assert result[0]["evidence"] == []