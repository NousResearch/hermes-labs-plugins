"""Behavioral gates for the actual lane projection, not placeholder tests."""
from copy import deepcopy

import pytest

from inbox_plugin import lanes


def project(items, **extra):
    return lanes.build_lanes({"profile": "operator", "attention": items, **extra})


def flatten(result):
    return [item for section in result.values() for item in section]


@pytest.mark.parametrize("fields,expected", [
    ({"needs_owner": True}, "Needs owner"),
    ({"approval_required": True}, "Needs owner"),
    ({"actionable": True, "owner": "builder"}, "Agent can act"),
    ({"state": "running"}, "Waiting"),
    ({"state": "blocked"}, "Waiting"),
    ({"state": "completed", "outcome": "accepted"}, "Done"),
    ({"state": "completed"}, "Unknown"),
    ({"state": "failed"}, "Unknown"),
    ({"state": "unknown"}, "Unknown"),
    ({"needs_owner": "true", "state": "failed"}, "Unknown"),
])
def test_real_items_enter_exactly_one_expected_lane(fields, expected):
    result = project([{"kind": "kanban", "id": "work-1", **fields}])
    assert len(flatten(result)) == 1
    assert result[expected][0]["id"] == "work-1"
    assert set(result) == {"Needs owner", "Agent can act", "Waiting", "Done", "Unknown"}


def test_unknown_owner_cannot_be_promoted_to_agent_can_act():
    result = project([{"kind": "kanban", "id": "work", "actionable": True}])
    assert result["Unknown"][0]["owner"] is None
    assert not result["Agent can act"]


def test_native_owned_home_is_not_a_shared_kanban_assignee():
    result = project([{"kind": "delegation", "id": "local", "state": "running"},
                      {"kind": "kanban", "id": "shared", "status": "blocked"}])
    by_id = {i["id"]: i for i in flatten(result)}
    assert by_id["local"]["owner"] == "operator"
    assert by_id["shared"]["owner"] is None
    assert by_id["local"]["execution_permission"] == "not_granted_by_inbox"


def test_unlinked_identical_titles_are_not_correlated_and_duplicate_ids_are():
    first = {"kind": "delegation", "id": "one", "state": "running", "title": "same"}
    other = {**first, "id": "two"}
    result = project([first, other], in_progress=[first])
    assert {i["id"] for i in flatten(result)} == {"one", "two"}
    assert len(flatten(result)) == 2


def test_next_step_is_only_explicit_and_evidence_never_becomes_verified():
    result = project([{"kind": "kanban", "id": "one", "state": "failed",
                       "evidence_refs": ["artifact.json", {"path": "result.txt", "verified": True}],
                       "next_step": "Inspect the supplied report", "message": "SECRET PROMPT"},
                      {"kind": "kanban", "id": "two", "state": "failed"}])
    by_id = {i["id"]: i for i in flatten(result)}
    assert by_id["one"]["next_step"] == "Inspect the supplied report"
    assert by_id["two"]["next_step"] is None
    assert by_id["one"]["evidence_refs"] == [
        {"reference": "artifact.json", "verified": False},
        {"reference": "result.txt", "verified": False}]
    assert "SECRET PROMPT" not in repr(result)
    assert by_id["one"]["provenance"]


def test_ack_and_snooze_are_local_and_leave_native_source_unchanged():
    item = {"kind": "delegation", "id": "one", "state": "failed"}
    snapshot = {"profile": "operator", "attention": [item], "attention_state": {
        "delegation:one": {"occurrence": 7, "visible": False, "acknowledged": True}}}
    original = deepcopy(snapshot)
    result = lanes.build_lanes(snapshot)
    observed = result["Unknown"][0]
    assert observed["local_state"] == "acked"
    assert observed["occurrences"] == [7]
    assert observed["execution"] == "failed"
    assert snapshot == original


def thread_snapshot():
    return {"threads": [{"id": "trace:one", "execution": "running", "outcome": "unknown",
                         "targets": ["builder"], "delivery": ["unknown"], "edges": [
                             {"origin_profile": "operator", "edge_id": "a"},
                             {"origin_profile": "builder", "edge_id": "b"}]}],
            "attention_state": {"herald:operator:a": {"occurrence": 3, "visible": False}}}


def test_one_unobserved_edge_prevents_marking_whole_thread():
    result = lanes.build_lanes(thread_snapshot())
    item = result["Waiting"][0]
    assert item["local_state"] is None
    assert item["owner"] == "builder"
    assert item["occurrences"] == [3]


def test_all_thread_edges_must_be_locally_handled_to_mark_thread():
    snapshot = thread_snapshot()
    snapshot["attention_state"]["herald:builder:b"] = {"occurrence": 8, "visible": False}
    item = lanes.build_lanes(snapshot)["Waiting"][0]
    assert item["local_state"] == "snoozed"
    assert item["occurrences"] == [3, 8]


@pytest.mark.parametrize("acks,expected", [((True, True), "acked"), ((True, False), "snoozed")])
def test_thread_is_acked_only_when_every_edge_is_acknowledged(acks, expected):
    snapshot = thread_snapshot()
    snapshot["attention_state"] = {
        "herald:operator:a": {"occurrence": 3, "visible": False, "acknowledged": acks[0]},
        "herald:builder:b": {"occurrence": 8, "visible": False, "acknowledged": acks[1]}}
    assert lanes.build_lanes(snapshot)["Waiting"][0]["local_state"] == expected


def test_expired_snooze_is_unmarked():
    state = {"delegation:one": {"occurrence": 7, "visible": True, "acknowledged": False,
                                "snooze_until": 1.0}}
    item = project([{"kind": "delegation", "id": "one", "state": "failed"}], attention_state=state)
    assert flatten(item)[0]["local_state"] is None


def test_source_conflict_overrules_completed_and_accepted():
    snapshot = thread_snapshot()
    snapshot["threads"][0].update(execution="completed", outcome="accepted", source_conflict=True)
    result = lanes.build_lanes(snapshot)
    assert result["Unknown"][0]["source_conflict"] is True
    assert not result["Done"]


def test_unidentified_records_are_not_falsely_joined_by_title():
    result = project([{"kind": "kanban", "title": "same"}, {"kind": "kanban", "title": "same"}])
    assert len(flatten(result)) == 2


def test_real_thread_and_unlinked_native_task_are_both_preserved():
    snapshot = thread_snapshot()
    snapshot["in_progress"] = [{"kind": "delegation", "id": "native-one", "state": "running"}]
    assert {i["id"] for i in flatten(lanes.build_lanes(snapshot))} == {"trace:one", "native-one"}
