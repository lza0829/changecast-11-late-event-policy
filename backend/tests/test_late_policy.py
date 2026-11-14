from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))

_TMP = Path(tempfile.mkdtemp(prefix="changecast-test-"))
os.environ["DATA_DIR"] = str(_TMP)

from app.db import connect  # noqa: E402
from app.seed import init_db  # noqa: E402
from app.services import project_service as svc  # noqa: E402


@pytest.fixture()
def stream_id():
    init_db()
    conn = connect()
    try:
        conn.executescript(
            """
            DELETE FROM applied_events;
            DELETE FROM project_batches;
            DELETE FROM projection_rows;
            DELETE FROM ooo_buffer;
            UPDATE project_streams SET watermark_seq=0, arrival_wave=0, late_policy='reject';
            """
        )
        conn.commit()
        return conn.execute(
            "SELECT id FROM project_streams WHERE code='member_profile'"
        ).fetchone()["id"]
    finally:
        conn.close()


def _state(sid):
    return svc.stream_state(sid)


def _entity(state, key):
    return next(r for r in state["projection"] if r["entity_key"] == key)


def test_reject_keeps_projection_and_watermark(stream_id):
    svc.run_project(stream_id, wave=1)
    svc.set_late_policy(stream_id, "reject")
    before = _state(stream_id)
    wm_before = before["stream"]["watermark_seq"]
    m1001_before = _entity(before, "M1001")

    result = svc.run_project(stream_id, wave=2)

    after = _state(stream_id)
    # main path advanced to seq 9
    assert result["main"]["counts"]["max_seq"] == 9
    assert after["stream"]["watermark_seq"] == 9
    assert result["watermark_before"] == wm_before
    assert result["watermark_after"] == 9
    # two late events, both rejected, nothing applied
    assert result["late"]["counts"]["late_seen"] == 2
    assert result["late"]["counts"]["late_rejected"] == 2
    assert result["late"]["counts"]["late_applied"] == 0
    # M1001 untouched by the late seq=4 event
    assert _entity(after, "M1001")["applied_seq"] == m1001_before["applied_seq"]
    assert _entity(after, "M1001")["payload"] == m1001_before["payload"]
    # late batch exists, separately numbered, counters split from the main batch
    late_batch = result["late_batch"]
    assert late_batch["batch_kind"] == "late"
    assert late_batch["batch_no"].startswith("LC-")
    assert result["batch_no"].startswith("PB-")
    assert late_batch["late_rejected"] == 2
    assert late_batch["late_applied"] == 0
    # late trace rows are marked and categorized
    late_ids = {e["event_id"] for e in late_batch["events"]}
    assert late_ids == {"mp-008", "mp-009"}
    assert all(e["result"] == "late_rejected" for e in late_batch["events"])
    assert all(e["is_late"] == 1 for e in late_batch["events"])


def test_bypass_applies_only_above_entity_applied_seq(stream_id):
    svc.run_project(stream_id, wave=1)
    svc.set_late_policy(stream_id, "bypass")
    before = _state(stream_id)
    assert _entity(before, "M1001")["applied_seq"] == 3
    assert _entity(before, "M1003")["applied_seq"] == 6
    wm_before = before["stream"]["watermark_seq"]

    result = svc.run_project(stream_id, wave=2)

    after = _state(stream_id)
    # watermark only follows the main path: max main seq is 9, never rewound
    assert after["stream"]["watermark_seq"] == 9
    assert result["watermark_after"] == 9
    assert result["watermark_before"] == wm_before
    assert result["late"]["counts"]["late_seen"] == 2
    assert result["late"]["counts"]["late_routed"] == 2
    assert result["late"]["counts"]["late_applied"] == 1
    assert result["late"]["counts"]["late_ineffective"] == 1
    # mp-008 seq=4 > M1001 applied_seq=3 -> overwritten
    m1001 = _entity(after, "M1001")
    assert m1001["applied_seq"] == 4
    assert m1001["applied_event_id"] == "mp-008"
    assert m1001["payload"]["tier"] == "platinum"
    # mp-009 seq=6 <= M1003 applied_seq=6 -> ineffective, row unchanged
    m1003 = _entity(after, "M1003")
    assert m1003["applied_seq"] == 6
    assert m1003["applied_event_id"] == "mp-006"
    assert m1003["payload"]["tier"] == "gold"
    # both late rows are on the bypass batch, and the entity applied_seq is
    # never above the main watermark
    late_batch = result["late_batch"]
    assert late_batch["batch_kind"] == "late"
    assert late_batch["late_applied"] == 1
    assert late_batch["late_ineffective"] == 1
    assert late_batch["watermark_before"] == wm_before
    assert late_batch["watermark_after"] == 9
    for row in after["projection"]:
        assert row["applied_seq"] <= after["stream"]["watermark_seq"]


def test_bypass_ineffective_does_not_claim_success(stream_id):
    svc.run_project(stream_id, wave=1)
    svc.set_late_policy(stream_id, "bypass")
    svc.run_project(stream_id, wave=2)  # applies mp-008 at seq 4 to M1001
    result = svc.run_project(stream_id, wave=3)  # mp-011 seq 7 > M1002 seq 5

    after = _state(stream_id)
    # M1002 was deleted at seq 5; seq 7 is still above, so the bypass may
    # resurrect it — but the main watermark stays on the main path (12).
    assert after["stream"]["watermark_seq"] == 12
    m1002 = _entity(after, "M1002")
    assert m1002["applied_seq"] == 7
    assert m1002["deleted"] is False
    assert result["late"]["counts"]["late_applied"] == 1

    # wave 4 is a fresh late event mp-012 at seq 4 for M1001, whose applied_seq
    # is already 4 (set by the bypass of mp-008).  It must be ineffective.
    before_m1001 = _entity(_state(stream_id), "M1001")
    replay = svc.run_project(stream_id, wave=4, late_policy="bypass")
    assert replay["main"]["counts"]["source_events"] == 0
    assert replay["late"]["counts"]["late_seen"] == 1
    assert replay["late"]["counts"]["late_routed"] == 1
    assert replay["late"]["counts"]["late_applied"] == 0
    assert replay["late"]["counts"]["late_ineffective"] == 1
    after_m1001 = _entity(_state(stream_id), "M1001")
    assert after_m1001["applied_seq"] == 4
    assert after_m1001["payload"] == before_m1001["payload"]
    # the replay moved neither the watermark nor the projection
    assert replay["watermark_before"] == 12
    assert replay["watermark_after"] == 12


def test_main_batch_never_contains_late_events(stream_id):
    svc.run_project(stream_id, wave=1)
    svc.set_late_policy(stream_id, "bypass")
    svc.run_project(stream_id, wave=2)
    state = _state(stream_id)

    main_ids = set()
    for b in state["main_batches"]:
        main_ids |= {e["event_id"] for e in svc.list_events(batch_id=b["id"])}
    assert "mp-008" not in main_ids
    assert "mp-009" not in main_ids
    # every main-batch event has a seq strictly above the watermark it entered at
    for b in state["main_batches"]:
        for e in svc.list_events(batch_id=b["id"]):
            if e["result"] != "skipped_duplicate":
                assert e["seq"] > b["watermark_before"]


def test_counters_cross_check_projection(stream_id):
    svc.run_project(stream_id, wave=1)
    svc.set_late_policy(stream_id, "reject")
    result = svc.run_project(stream_id, wave=2)
    state = _state(stream_id)

    main = result["main"]["counts"]
    late = result["late"]["counts"]
    assert main["source_events"] + late["source_events"] == 3  # wave 2 has 3 events
    assert late["late_seen"] == late["late_rejected"] + late["late_routed"]
    assert late["late_routed"] == late["late_applied"] + late["late_ineffective"]
    alive = sum(1 for r in state["projection"] if not r["deleted"])
    assert main["projected_alive"] == alive
    assert state["totals"]["late_rejected"] == 2


def test_cross_checks_pass_after_both_policies(stream_id):
    svc.run_project(stream_id, wave=1)
    svc.set_late_policy(stream_id, "reject")
    svc.run_project(stream_id, wave=2)
    for c in _state(stream_id)["checks"]:
        assert c["ok"], c

    svc.set_late_policy(stream_id, "bypass")
    svc.run_project(stream_id, wave=3)
    svc.run_project(stream_id, wave=4)
    state = _state(stream_id)
    for c in state["checks"]:
        assert c["ok"], c
    # wave 2 was rejected, so M1001 was still at seq 3 when wave 4 arrived;
    # both late events are therefore legitimately above the entity seq.
    assert state["totals"]["late_applied"] == 2
    assert state["totals"]["late_ineffective"] == 0
    assert _entity(state, "M1001")["applied_seq"] == 4


def test_manual_late_drop_uses_bypass(stream_id):
    svc.run_project(stream_id, wave=1)
    svc.set_late_policy(stream_id, "bypass")
    svc.run_project(stream_id, wave=2)
    state = _state(stream_id)
    wm = state["stream"]["watermark_seq"]
    m1001 = _entity(state, "M1001")

    result = svc.apply_events(
        stream_id,
        [
            {
                "event_id": "manual-late-1",
                "entity_key": "M1001",
                "op": "upsert",
                "seq": 4,
                "event_time": "2026-03-01T09:05:00Z",
                "payload": {"display_name": "林晓", "tier": "diamond", "region": "east"},
            }
        ],
    )
    assert result["main"]["counts"]["source_events"] == 0
    assert result["late"]["counts"]["late_routed"] == 1
    # M1001 applied_seq is already 4 from the earlier bypass, so seq 4 is
    # ineffective and the watermark does not move.
    assert result["late"]["counts"]["late_ineffective"] == 1
    assert result["watermark_before"] == wm
    assert result["watermark_after"] == wm
    assert _entity(_state(stream_id), "M1001")["payload"] == m1001["payload"]


def test_rejection_is_terminal_for_event_id(stream_id):
    """A rejected late event is recorded and does not get applied later just
    because the policy was switched; the trace stays one row per event."""
    svc.set_late_policy(stream_id, "reject")
    svc.run_project(stream_id, wave=1)
    svc.run_project(stream_id, wave=2)
    before = _state(stream_id)
    assert before["totals"]["late_rejected"] == 2

    svc.set_late_policy(stream_id, "bypass")
    again = svc.run_project(stream_id, wave=2)
    assert again["late"]["counts"]["late_applied"] == 0
    # all three wave-2 events already have a trace row (mp-007 on the main
    # path, mp-008/mp-009 as rejections), so none is re-adjudicated
    assert again["late"]["counts"]["skipped_duplicate"] == 3
    assert again["late"]["counts"]["late_seen"] == 0
    after = _state(stream_id)
    assert after["totals"]["late_rejected"] == 2
    assert after["totals"]["late_applied"] == 0
    # exactly one trace row per rejected event, no silent second application
    rejected = [e for e in after["late_events"] if e["result"] == "late_rejected"]
    assert len(rejected) == 2
    assert {e["event_id"] for e in rejected} == {"mp-008", "mp-009"}
    assert _entity(after, "M1001")["applied_seq"] == 3
    for c in after["checks"]:
        assert c["ok"], c


def test_replaying_older_wave_does_not_rewind_arrival_wave(stream_id):
    svc.run_project(stream_id, wave=1)
    svc.run_project(stream_id, wave=2)
    svc.run_project(stream_id, wave=3)
    assert _state(stream_id)["stream"]["arrival_wave"] == 3
    svc.run_project(stream_id, wave=1)
    assert _state(stream_id)["stream"]["arrival_wave"] == 3
    assert _state(stream_id)["stream"]["watermark_seq"] == 12


def test_same_seq_main_events_are_ordered_by_event_id(stream_id):
    svc.set_late_policy(stream_id, "reject")
    result = svc.apply_events(
        stream_id,
        [
            {"event_id": "aa-1", "entity_key": "X1", "op": "upsert", "seq": 2, "payload": {"v": "first"}},
            {"event_id": "aa-2", "entity_key": "X1", "op": "upsert", "seq": 2, "payload": {"v": "second"}},
        ],
    )
    assert result["main"]["counts"]["applied_upsert"] == 2
    assert result["watermark_after"] == 2
    row = _entity(_state(stream_id), "X1")
    assert row["applied_event_id"] == "aa-2"
    assert row["payload"] == {"v": "second"}
