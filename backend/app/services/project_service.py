"""Projection service: arrival waves, watermark movement, late policy.

The main path never takes an event whose seq is <= the stream watermark.  Late
events are handled by the configured policy, either rejected with a recorded
counter or routed through a separately numbered bypass batch.  Only the main
path may move the watermark, and only forward.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from app.config import FIXTURES_DIR
from app.db import connect
from app.engines.project import LATE_POLICIES, apply_increment
from app.repositories import batches as batch_repo
from app.repositories import projections as proj_repo
from app.repositories import streams as stream_repo


class ProjectError(ValueError):
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _load_fixture(name: str) -> list[dict[str, Any]]:
    path = FIXTURES_DIR / name
    with path.open(encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise ValueError(f"fixture {name} must be a JSON array")
    out: list[dict[str, Any]] = []
    for i, e in enumerate(data):
        if not isinstance(e, dict):
            raise ValueError(f"fixture {name} event #{i} is not an object")
        item = dict(e)
        item.setdefault("wave", 1)
        out.append(item)
    return out


def fixture_waves(stream: dict[str, Any]) -> list[dict[str, Any]]:
    """Describe the arrival waves of a stream's fixture for the UI."""
    fixture = _load_fixture(stream["fixture"])
    waves: dict[int, list[dict[str, Any]]] = {}
    for e in fixture:
        waves.setdefault(int(e["wave"]), []).append(e)
    current = int(stream["arrival_wave"])
    out: list[dict[str, Any]] = []
    for no in sorted(waves):
        events = sorted(waves[no], key=lambda e: (int(e["seq"]), str(e["event_id"])))
        out.append(
            {
                "wave": no,
                "status": "done" if no <= current else "pending",
                "events": [
                    {
                        "event_id": str(e["event_id"]),
                        "entity_key": str(e["entity_key"]),
                        "op": str(e.get("op", "upsert")),
                        "seq": int(e["seq"]),
                        "event_time": str(e.get("event_time", "")),
                    }
                    for e in events
                ],
            }
        )
    return out



def _next_batch_no(conn, kind: str) -> str:
    """Main and bypass batches are numbered independently and prefixed
    differently so they can never be mistaken for each other.  The suffix is
    derived from the highest existing one, not from a count, so deleting a
    stream's history cannot make a new batch reuse a live number."""
    now = _now()
    prefix = "PB" if kind == "main" else "LC"
    day = now[:10].replace("-", "")
    head = f"{prefix}-{day}-"
    existing = [
        r["batch_no"]
        for r in conn.execute(
            "SELECT batch_no FROM project_batches WHERE batch_no LIKE ?", (head + "%",)
        ).fetchall()
    ]
    used = 0
    for no in existing:
        try:
            used = max(used, int(no.rsplit("-", 1)[1]))
        except (IndexError, ValueError):
            continue
    candidate = used + 1
    taken = set(existing)
    while f"{head}{candidate:04d}" in taken:
        candidate += 1
    return f"{head}{candidate:04d}"


def dashboard_stats() -> dict[str, Any]:
    conn = connect()
    try:
        return {
            "stream_count": conn.execute("SELECT COUNT(*) c FROM project_streams").fetchone()["c"],
            "enabled_streams": conn.execute(
                "SELECT COUNT(*) c FROM project_streams WHERE enabled=1"
            ).fetchone()["c"],
            "batch_count": conn.execute(
                "SELECT COUNT(*) c FROM project_batches WHERE batch_kind='main'"
            ).fetchone()["c"],
            "late_batch_count": conn.execute(
                "SELECT COUNT(*) c FROM project_batches WHERE batch_kind='late'"
            ).fetchone()["c"],
            "projection_alive": conn.execute(
                "SELECT COUNT(*) c FROM projection_rows WHERE deleted=0"
            ).fetchone()["c"],
            "projection_deleted": conn.execute(
                "SELECT COUNT(*) c FROM projection_rows WHERE deleted=1"
            ).fetchone()["c"],
            "late_rejected": conn.execute(
                "SELECT COALESCE(SUM(late_rejected), 0) v FROM project_batches"
            ).fetchone()["v"],
            "late_applied": conn.execute(
                "SELECT COALESCE(SUM(late_applied), 0) v FROM project_batches"
            ).fetchone()["v"],
            "late_ineffective": conn.execute(
                "SELECT COALESCE(SUM(late_ineffective), 0) v FROM project_batches"
            ).fetchone()["v"],
            "watermark_max": conn.execute(
                "SELECT COALESCE(MAX(watermark_seq), 0) v FROM project_streams"
            ).fetchone()["v"],
            "ooo_buffered": conn.execute("SELECT COUNT(*) c FROM ooo_buffer").fetchone()["c"],
            "apply_order": conn.execute(
                "SELECT value FROM settings WHERE key='apply_order'"
            ).fetchone()["value"],
        }
    finally:
        conn.close()


def list_streams() -> list[dict[str, Any]]:
    conn = connect()
    try:
        return stream_repo.list_streams(conn)
    finally:
        conn.close()


def list_batches(limit: int = 50, kind: str | None = None) -> list[dict[str, Any]]:
    conn = connect()
    try:
        rows = batch_repo.list_batches(conn, limit=limit, kind=kind)
        streams = {s["id"]: s for s in stream_repo.list_streams(conn)}
        for r in rows:
            s = streams.get(r["stream_id"])
            r["stream_code"] = s["code"] if s else None
            r["stream_title"] = s["title"] if s else None
        return rows
    finally:
        conn.close()


def stream_state(stream_id: int) -> dict[str, Any]:
    """One call returning everything the demo page needs to cross-check:
    watermark, both batch lanes, projection rows, per-entity applied_seq and
    the late trace."""
    conn = connect()
    try:
        stream = stream_repo.get_stream(conn, stream_id)
        if not stream:
            raise ProjectError("投影流不存在")
        main_batches = [
            b
            for b in batch_repo.list_batches(conn, limit=500, kind="main")
            if b["stream_id"] == stream_id
        ]
        late_batches = [
            b
            for b in batch_repo.list_batches(conn, limit=500, kind="late")
            if b["stream_id"] == stream_id
        ]
        projection = proj_repo.list_projections(conn, stream_id=stream_id)
        all_events = batch_repo.list_applied_events(conn, stream_id=stream_id, limit=5000)
        late_events = [e for e in all_events if e["is_late"]]
        batch_kind = {b["id"]: "main" for b in main_batches}
        batch_kind.update({b["id"]: "late" for b in late_batches})
        for row in projection:
            row["applied_lane"] = batch_kind.get(row["applied_batch_id"])
        totals = {
            "late_seen": sum(b["late_seen"] for b in main_batches + late_batches),
            "late_rejected": sum(b["late_rejected"] for b in main_batches + late_batches),
            "late_routed": sum(b["late_routed"] for b in main_batches + late_batches),
            "late_applied": sum(b["late_applied"] for b in main_batches + late_batches),
            "late_ineffective": sum(
                b["late_ineffective"] for b in main_batches + late_batches
            ),
        }
        return {
            "stream": stream,
            "waves": fixture_waves(stream),
            "main_batches": main_batches,
            "late_batches": late_batches,
            "projection": projection,
            "late_events": late_events,
            "totals": totals,
            "checks": _cross_checks(
                stream, main_batches, late_batches, projection, all_events, totals
            ),
        }
    finally:
        conn.close()


def _cross_checks(
    stream: dict[str, Any],
    main_batches: list[dict[str, Any]],
    late_batches: list[dict[str, Any]],
    projection: list[dict[str, Any]],
    events: list[dict[str, Any]],
    totals: dict[str, int],
) -> list[dict[str, Any]]:
    """Invariants the page shows verbatim.  They can pass trivially when
    nothing has run yet; each carries the numbers it was checked against."""
    checks: list[dict[str, Any]] = []

    rewinds = [b["batch_no"] for b in main_batches + late_batches if b["watermark_after"] < b["watermark_before"]]
    checks.append(
        {
            "key": "watermark_never_rewinds",
            "label": "主水位从不回拨",
            "ok": not rewinds,
            "detail": (
                f"检查了 {len(main_batches) + len(late_batches)} 个批次的时间前后水位"
                if not rewinds
                else f"回拨批次：{', '.join(rewinds)}"
            ),
        }
    )

    main_ids = {b["id"] for b in main_batches}
    late_in_main = [e for e in events if e["batch_id"] in main_ids and e["is_late"]]
    checks.append(
        {
            "key": "no_late_in_main",
            "label": "主批次不含迟到事件",
            "ok": not late_in_main,
            "detail": (
                f"主批次留痕 {sum(1 for e in events if e['batch_id'] in main_ids)} 条，"
                "其中迟到 0 条"
                if not late_in_main
                else f"主批次出现 {len(late_in_main)} 条迟到留痕"
            ),
        }
    )

    split_ok = totals["late_seen"] == totals["late_rejected"] + totals["late_routed"]
    routed_ok = totals["late_routed"] == totals["late_applied"] + totals["late_ineffective"]
    checks.append(
        {
            "key": "late_counter_split",
            "label": "迟到计数分列且对得上",
            "ok": split_ok and routed_ok,
            "detail": (
                f"迟到 {totals['late_seen']} = 拒绝 {totals['late_rejected']} + "
                f"旁路 {totals['late_routed']}；旁路 {totals['late_routed']} = "
                f"盖写 {totals['late_applied']} + 无效 {totals['late_ineffective']}"
            ),
        }
    )

    wm = int(stream["watermark_seq"])
    over = [r for r in projection if int(r["applied_seq"]) > wm]
    checks.append(
        {
            "key": "entity_seq_within_watermark",
            "label": "实体 applied_seq 不超过主水位",
            "ok": not over,
            "detail": (
                f"{len(projection)} 行投影的 applied_seq 均 ≤ 主水位 {wm}"
                if not over
                else f"{len(over)} 行超过主水位 {wm}"
            ),
        }
    )

    by_event = {e["event_id"]: e for e in events}
    mismatched: list[str] = []
    late_lane_ok = True
    for row in projection:
        e = by_event.get(row["applied_event_id"])
        if e is None or int(e["seq"]) != int(row["applied_seq"]):
            mismatched.append(row["entity_key"])
            continue
        if e["is_late"] and row.get("applied_lane") != "late":
            late_lane_ok = False
    checks.append(
        {
            "key": "projection_matches_trace",
            "label": "投影行与留痕互证",
            "ok": not mismatched,
            "detail": (
                f"{len(projection)} 行的 applied_event_id / applied_seq 都能在留痕中"
                "找到同一事件"
                if not mismatched
                else f"对不上：{', '.join(mismatched)}"
            ),
        }
    )
    checks.append(
        {
            "key": "late_applied_stays_in_bypass",
            "label": "旁路盖写只记在旁路批次",
            "ok": late_lane_ok,
            "detail": (
                "所有 is_late 的留痕都来自旁路批；投影按批次的来源可追溯"
                if late_lane_ok
                else "有迟到留痕挂在了主批次上"
            ),
        }
    )

    late_batch_numbers = {b["batch_no"] for b in late_batches}
    mixed = [
        b["batch_no"]
        for b in main_batches
        if b["batch_no"] in late_batch_numbers
    ]
    checks.append(
        {
            "key": "separate_batch_numbering",
            "label": "主批次与旁路批分开编号",
            "ok": not mixed,
            "detail": (
                f"主批次 {len(main_batches)} 个、旁路批 {len(late_batches)} 个，编号前缀分别为 PB-/LC-"
            ),
        }
    )
    return checks


def list_projections(stream_id: int | None = None, include_deleted: bool = True) -> list[dict[str, Any]]:
    conn = connect()
    try:
        rows = proj_repo.list_projections(
            conn, stream_id=stream_id, include_deleted=include_deleted
        )
        streams = {s["id"]: s for s in stream_repo.list_streams(conn)}
        for r in rows:
            s = streams.get(r["stream_id"])
            r["stream_code"] = s["code"] if s else None
            r["stream_title"] = s["title"] if s else None
        return rows
    finally:
        conn.close()


def list_events(
    batch_id: int | None = None,
    stream_id: int | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    conn = connect()
    try:
        return batch_repo.list_applied_events(
            conn, batch_id=batch_id, stream_id=stream_id, limit=limit
        )
    finally:
        conn.close()


def list_ooo_buffer(stream_id: int | None = None) -> list[dict[str, Any]]:
    conn = connect()
    try:
        return proj_repo.list_ooo_buffer(conn, stream_id=stream_id)
    finally:
        conn.close()


def list_settings() -> dict[str, str]:
    conn = connect()
    try:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
        return {r["key"]: r["value"] for r in rows}
    finally:
        conn.close()


def set_late_policy(stream_id: int, policy: str) -> dict[str, Any]:
    if policy not in LATE_POLICIES:
        raise ProjectError(f"迟到策略只能是 {', '.join(LATE_POLICIES)}")
    conn = connect()
    try:
        stream = stream_repo.get_stream(conn, stream_id)
        if not stream:
            raise ProjectError("投影流不存在")
        stream_repo.set_late_policy(conn, stream_id, policy)
        conn.commit()
        return stream_repo.get_stream(conn, stream_id) or {}
    finally:
        conn.close()


def _persist_increment(
    conn,
    *,
    stream: dict[str, Any],
    result: dict[str, Any],
    late_policy: str,
    note: str | None,
    now: str,
) -> dict[str, Any]:
    """Persist one increment's two lanes.

    A lane that received no events at all does not get a batch row, so the
    batch list shows only real shipments.  Every projection mutation is
    attributed to the batch of the lane that produced it, and the watermark
    is written from the main path's result only.
    """
    stream_id = int(stream["id"])
    main_counts = result["main"]["counts"]
    late_counts = result["late"]["counts"]
    main_id: int | None = None
    late_id: int | None = None

    if main_counts["source_events"] > 0:
        main_id = batch_repo.insert_batch(
            conn,
            batch_no=_next_batch_no(conn, "main"),
            stream_id=stream_id,
            status="completed",
            counts=main_counts,
            batch_kind="main",
            late_policy=late_policy,
            watermark_before=result["watermark_before"],
            watermark_after=result["watermark_after"],
            note=note,
            created_at=now,
        )
    if late_counts["source_events"] > 0:
        late_id = batch_repo.insert_batch(
            conn,
            batch_no=_next_batch_no(conn, "late"),
            stream_id=stream_id,
            status="completed",
            counts=late_counts,
            batch_kind="late",
            late_policy=late_policy,
            watermark_before=result["watermark_before"],
            watermark_after=result["watermark_after"],
            note=note,
            created_at=now,
        )

    for mut in result["mutations"]:
        lane_batch = late_id if mut["lane"] == "late" else main_id
        if lane_batch is None:  # pragma: no cover - engine never emits this
            raise ProjectError(f"{mut['lane']} 车道没有批次可挂")
        proj_repo.upsert_projection_row(
            conn, stream_id=stream_id, row=mut["row"], batch_id=lane_batch, updated_at=now
        )
    if main_id is not None:
        for entry in result["main"]["log"]:
            batch_repo.insert_applied_event(
                conn, batch_id=main_id, stream_id=stream_id, event=entry, created_at=now
            )
    if late_id is not None:
        for entry in result["late"]["log"]:
            batch_repo.insert_applied_event(
                conn, batch_id=late_id, stream_id=stream_id, event=entry, created_at=now
            )

    stream_repo.set_watermark(conn, stream_id, result["watermark_after"])
    conn.commit()

    main_batch = batch_repo.get_batch(conn, main_id) if main_id is not None else None
    late_batch = batch_repo.get_batch(conn, late_id) if late_id is not None else None
    if main_batch is not None:
        main_batch["stream_code"] = stream["code"]
        main_batch["stream_title"] = stream["title"]
        main_batch["apply_order"] = "by_seq"
    if late_batch is not None:
        late_batch["stream_code"] = stream["code"]
        late_batch["stream_title"] = stream["title"]
        late_batch["events"] = batch_repo.list_applied_events(
            conn, batch_id=late_id, limit=200
        )
    return {"main_batch": main_batch, "late_batch": late_batch}


def apply_events(
    stream_id: int,
    events: list[dict[str, Any]],
    *,
    late_policy: str | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    """Apply a caller-supplied arrival slice, e.g. a manual late-event drop.

    Same classification as a fixture wave: events above the watermark go on
    the main path, events at or below it follow the configured late policy.
    """
    conn = connect()
    try:
        stream = stream_repo.get_stream(conn, stream_id)
        if not stream:
            raise ProjectError("投影流不存在")
        if not stream["enabled"]:
            raise ProjectError("投影流已停用")
        policy = late_policy or stream["late_policy"]
        if policy not in LATE_POLICIES:
            raise ProjectError(f"迟到策略只能是 {', '.join(LATE_POLICIES)}")
        if not events:
            raise ProjectError("没有要应用的事件")

        normalized: list[dict[str, Any]] = []
        required = ("event_id", "entity_key", "seq", "op")
        for i, e in enumerate(events):
            missing = [k for k in required if k not in e]
            if missing:
                raise ProjectError(f"第 {i + 1} 个事件缺少字段：{', '.join(missing)}")
            normalized.append(
                {
                    "event_id": str(e["event_id"]),
                    "entity_key": str(e["entity_key"]),
                    "op": str(e["op"]),
                    "seq": int(e["seq"]),
                    "event_time": str(e.get("event_time") or _now()),
                    "payload": e.get("payload"),
                    "wave": int(e.get("wave", 0)),
                }
            )

        watermark_before = int(stream["watermark_seq"])
        projection = proj_repo.projection_map(conn, stream_id)
        known = proj_repo.list_known_event_ids(conn, stream_id)
        result = apply_increment(
            normalized,
            projection=projection,
            watermark=watermark_before,
            late_policy=policy,
            known_event_ids=known,
        )

        now = _now()
        persisted = _persist_increment(
            conn,
            stream=stream,
            result=result,
            late_policy=policy,
            note=note or "手工投递到达片",
            now=now,
        )
        result["main_batch"] = persisted["main_batch"]
        result["late_batch"] = persisted["late_batch"]
        result["batch_no"] = (
            persisted["main_batch"]["batch_no"] if persisted["main_batch"] else None
        )
        return result
    finally:
        conn.close()


def reset_stream(stream_id: int) -> dict[str, Any]:
    """Wipe a stream's applied state so the page demo can be replayed."""
    conn = connect()
    try:
        stream = stream_repo.get_stream(conn, stream_id)
        if not stream:
            raise ProjectError("投影流不存在")
        conn.execute("DELETE FROM applied_events WHERE stream_id=?", (stream_id,))
        conn.execute("DELETE FROM project_batches WHERE stream_id=?", (stream_id,))
        proj_repo.delete_stream_projection(conn, stream_id)
        stream_repo.set_watermark(conn, stream_id, 0)
        stream_repo.set_arrival_wave(conn, stream_id, 0)
        conn.commit()
        return stream_repo.get_stream(conn, stream_id) or {}
    finally:
        conn.close()


def run_project(
    stream_id: int,
    note: str | None = None,
    late_policy: str | None = None,
    wave: int | None = None,
) -> dict[str, Any]:
    """Apply the next arrival wave of a stream's fixture.

    A run classifies ``seq <= watermark`` as late and never lets those events
    into the main increment.  The main batch and any bypass batch are two
    distinct rows with separate numbering, counters and trace entries.
    """
    conn = connect()
    try:
        stream = stream_repo.get_stream(conn, stream_id)
        if not stream:
            raise ProjectError("投影流不存在")
        if not stream["enabled"]:
            raise ProjectError("投影流已停用")

        policy = late_policy or stream["late_policy"]
        if policy not in LATE_POLICIES:
            raise ProjectError(f"迟到策略只能是 {', '.join(LATE_POLICIES)}")

        fixture = _load_fixture(stream["fixture"])
        fixture_waves = sorted({int(e["wave"]) for e in fixture})

        if wave is not None:
            next_wave = int(wave)
        else:
            candidates = [w for w in fixture_waves if w > int(stream["arrival_wave"])]
            next_wave = candidates[0] if candidates else fixture_waves[-1] + 1

        events = [e for e in fixture if int(e["wave"]) == next_wave]
        if not events:
            raise ProjectError(
                f"夹具中没有第 {next_wave} 波事件；"
                f"当前已推进到第 {stream['arrival_wave']} 波，全部波次已投递"
            )

        watermark_before = int(stream["watermark_seq"])
        projection = proj_repo.projection_map(conn, stream_id)
        known = proj_repo.list_known_event_ids(conn, stream_id)

        result = apply_increment(
            events,
            projection=projection,
            watermark=watermark_before,
            late_policy=policy,
            known_event_ids=known,
        )

        now = _now()
        persisted = _persist_increment(
            conn,
            stream=stream,
            result=result,
            late_policy=policy,
            note=note,
            now=now,
        )
        # Replaying an older wave for demonstration must not make the stream
        # look like it has delivered less than it actually has.
        stream_repo.set_arrival_wave(
            conn, stream_id, max(int(stream["arrival_wave"]), next_wave)
        )
        conn.commit()

        main_batch = persisted["main_batch"]
        out: dict[str, Any] = dict(main_batch or {})
        out["stream_id"] = stream_id
        out["stream_code"] = stream["code"]
        out["stream_title"] = stream["title"]
        out["status"] = "completed"
        out["apply_order"] = "by_seq"
        out["main"] = result["main"]
        out["late"] = result["late"]
        out["counts"] = result["main"]["counts"]
        out["watermark_before"] = result["watermark_before"]
        out["watermark_after"] = result["watermark_after"]
        out["late_batch"] = persisted["late_batch"]
        out["main_batch_id"] = main_batch["id"] if main_batch else None
        out["projection_preview"] = proj_repo.list_projections(
            conn, stream_id=stream_id
        )[:20]
        out["event_preview"] = (
            batch_repo.list_applied_events(conn, batch_id=main_batch["id"], limit=20)
            if main_batch
            else []
        )
        return out
    finally:
        conn.close()
