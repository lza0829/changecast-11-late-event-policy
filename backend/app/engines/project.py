"""Incremental CDC projection with an explicit late-arrival policy.

A projection stream keeps a *main watermark*: every seq at or below it has
already been through the regular, forward-only path.  An event whose seq is
at or below that watermark is **late**.  That is a different situation from
a high-seq event still waiting for a gap to close — the base does not
implement gap waiting, and lateness is never inferred from arrival timing
alone.

Late events never enter the regular increment:

* ``reject`` — recorded as a late rejection; the rejection counter goes up,
  the projection and the main watermark stay untouched.
* ``bypass`` — routed into a separately numbered late batch.  The bypass may
  correct projection rows, but it must never move the main watermark
  backwards.  Per entity the terminal rule is strict: overwrite only when
  ``late seq > entity applied_seq``; otherwise the late event is recorded as
  ineffective and the projection is left alone.

This module is pure: the caller persists batches, trace rows and projections.
"""

from __future__ import annotations

from typing import Any

LATE_POLICIES = ("reject", "bypass")

# An entity with no projection row has nothing to overwrite.  Reading its
# applied_seq as 0 makes the per-entity rule "late seq > 0", i.e. a late event
# may create the row.  That is the only sensible terminal reading.
NO_ROW_APPLIED_SEQ = 0

RESULT_UPSERT = "applied_upsert"
RESULT_DELETE = "applied_delete"
RESULT_DUPLICATE = "skipped_duplicate"
RESULT_LATE_REJECTED = "late_rejected"
RESULT_LATE_APPLIED = "late_applied"
RESULT_LATE_INEFFECTIVE = "late_ineffective"


def _normalize(event: dict[str, Any]) -> dict[str, Any]:
    return {
        "event_id": str(event["event_id"]),
        "entity_key": str(event["entity_key"]),
        "op": str(event.get("op", "upsert")),
        "seq": int(event.get("seq", 0)),
        "event_time": str(event.get("event_time", "")),
        "payload": event.get("payload"),
        "wave": int(event.get("wave", 1)),
    }


def _sort_key(event: dict[str, Any]) -> tuple[int, str]:
    return (event["seq"], event["event_id"])


def apply_increment(
    events: list[dict[str, Any]],
    *,
    projection: dict[str, dict[str, Any]],
    watermark: int,
    late_policy: str,
    known_event_ids: set[str] | None = None,
) -> dict[str, Any]:
    """Classify and apply one arrival wave against the current stream state.

    Lateness is decided against the watermark *as it was before this wave*, so
    the main path cannot reclassify an event that arrived below the line.
    """
    if late_policy not in LATE_POLICIES:
        raise ValueError(f"unsupported late policy: {late_policy}")

    known = set(known_event_ids or ())
    proj: dict[str, dict[str, Any]] = {k: dict(v) for k, v in projection.items()}
    mutations: list[dict[str, Any]] = []
    watermark_before = int(watermark)

    normalized = [_normalize(e) for e in events]
    main_events = sorted((e for e in normalized if e["seq"] > watermark_before), key=_sort_key)
    late_events = sorted((e for e in normalized if e["seq"] <= watermark_before), key=_sort_key)

    main_log: list[dict[str, Any]] = []
    late_log: list[dict[str, Any]] = []
    main_counts = {
        "source_events": len(main_events),
        "applied_upsert": 0,
        "applied_delete": 0,
        "skipped_duplicate": 0,
        "max_seq": 0,
    }
    late_counts = {
        "source_events": len(late_events),
        "skipped_duplicate": 0,
        "late_seen": 0,
        "late_rejected": 0,
        "late_routed": 0,
        "late_applied": 0,
        "late_ineffective": 0,
        "max_seq": 0,
    }

    # ---- main path: strictly forward, only seq above the watermark --------
    for ev in main_events:
        event_id = ev["event_id"]
        entity_key = ev["entity_key"]
        seq = ev["seq"]
        main_counts["max_seq"] = max(main_counts["max_seq"], seq)

        existing = proj.get(entity_key)
        entity_seq_before = _seq(existing)
        if event_id in known:
            main_counts["skipped_duplicate"] += 1
            main_log.append(
                {
                    **ev,
                    "result": RESULT_DUPLICATE,
                    "is_late": False,
                    "stage": "main",
                    "entity_seq_before": entity_seq_before,
                    "detail": "event_id 已处理，不重复纳入主路径",
                }
            )
            continue

        if existing is not None and seq < int(existing["applied_seq"]):
            # A main event is above the watermark, and entity applied_seq can
            # never be above the watermark.  If this fires the classification
            # upstream is broken; fail loudly rather than silently rewind.
            # Equal seq is allowed: same-seq events are ordered by event_id and
            # the later one legitimately overwrites the earlier.
            raise ValueError(
                f"主路径事件 {event_id} seq={seq} 小于实体 {entity_key} "
                f"的 applied_seq={existing['applied_seq']}"
            )

        known.add(event_id)
        if ev["op"] == "delete":
            row = _row(entity_key, None, seq, event_id, deleted=True)
            main_counts["applied_delete"] += 1
            result = RESULT_DELETE
            detail = "主路径按序应用删除"
        else:
            row = _row(entity_key, ev.get("payload") or {}, seq, event_id, deleted=False)
            main_counts["applied_upsert"] += 1
            result = RESULT_UPSERT
            detail = "主路径按序应用写入"
        proj[entity_key] = row
        mutations.append({"lane": "main", "row": row})
        main_log.append(
            {
                **ev,
                "result": result,
                "is_late": False,
                "stage": "main",
                "entity_seq_before": entity_seq_before,
                "detail": detail,
            }
        )

    watermark_after = max(watermark_before, main_counts["max_seq"])

    # ---- late arrivals: seq at or below the pre-wave watermark ------------
    for ev in late_events:
        event_id = ev["event_id"]
        entity_key = ev["entity_key"]
        seq = ev["seq"]
        late_counts["max_seq"] = max(late_counts["max_seq"], seq)

        current = proj.get(entity_key)
        current_seq = _seq(current)

        if event_id in known:
            late_counts["skipped_duplicate"] += 1
            late_log.append(
                {
                    **ev,
                    "result": RESULT_DUPLICATE,
                    "is_late": True,
                    "stage": "late",
                    "entity_seq_before": current_seq,
                    "detail": "该 event_id 已有留痕，跳过重复，不计迟到",
                }
            )
            continue

        known.add(event_id)
        late_counts["late_seen"] += 1

        if late_policy == "reject":
            late_counts["late_rejected"] += 1
            late_log.append(
                {
                    **ev,
                    "result": RESULT_LATE_REJECTED,
                    "is_late": True,
                    "stage": "late",
                    "entity_seq_before": current_seq,
                    "detail": (
                        f"seq {seq} ≤ 主水位 {watermark_before}：拒绝纳入常规应用，"
                        "投影与主水位不动"
                    ),
                }
            )
            continue

        late_counts["late_routed"] += 1

        if seq <= current_seq:
            late_counts["late_ineffective"] += 1
            late_log.append(
                {
                    **ev,
                    "result": RESULT_LATE_INEFFECTIVE,
                    "is_late": True,
                    "stage": "late",
                    "entity_seq_before": current_seq,
                    "detail": (
                        f"迟到 seq {seq} ≤ 实体当前 applied_seq {current_seq}："
                        "无效迟到，不改投影，主水位不回拨"
                    ),
                }
            )
            continue

        if ev["op"] == "delete":
            row = _row(entity_key, None, seq, event_id, deleted=True)
            action = "盖写删除"
        else:
            row = _row(entity_key, ev.get("payload") or {}, seq, event_id, deleted=False)
            action = "盖写更新"
        proj[entity_key] = row
        mutations.append({"lane": "late", "row": row})
        late_counts["late_applied"] += 1
        late_log.append(
            {
                **ev,
                "result": RESULT_LATE_APPLIED,
                "is_late": True,
                "stage": "late",
                "entity_seq_before": current_seq,
                "detail": (
                    f"迟到 seq {seq} > 实体当前 applied_seq {current_seq}："
                    f"旁路{action}；主水位保持 {watermark_after}，不回拨"
                ),
            }
        )

    alive = sum(1 for r in proj.values() if not r["deleted"])
    deleted = sum(1 for r in proj.values() if r["deleted"])
    main_counts["projected_alive"] = alive
    main_counts["projected_deleted"] = deleted
    late_counts["projected_alive"] = alive
    late_counts["projected_deleted"] = deleted

    return {
        "watermark_before": watermark_before,
        "watermark_after": watermark_after,
        "late_policy": late_policy,
        "main": {"counts": main_counts, "log": main_log},
        "late": {"counts": late_counts, "log": late_log},
        "mutations": mutations,
        "projection": list(proj.values()),
    }


def _seq(row: dict[str, Any] | None) -> int:
    return int(row["applied_seq"]) if row is not None else NO_ROW_APPLIED_SEQ


def _row(
    entity_key: str,
    payload: dict[str, Any] | None,
    seq: int,
    event_id: str,
    *,
    deleted: bool,
) -> dict[str, Any]:
    return {
        "entity_key": entity_key,
        "payload": payload,
        "applied_seq": seq,
        "applied_event_id": event_id,
        "deleted": deleted,
    }
