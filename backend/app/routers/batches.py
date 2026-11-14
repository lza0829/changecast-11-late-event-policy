from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.engines.project import LATE_POLICIES
from app.services import project_service as svc

router = APIRouter(tags=["batches"])


class RunBody(BaseModel):
    stream_id: int
    note: str | None = None
    late_policy: str | None = None
    wave: int | None = None


class LateDropBody(BaseModel):
    stream_id: int
    late_policy: str | None = None
    note: str | None = None
    events: list[dict]


@router.get("/batches")
def list_batches(limit: int = 50, kind: str | None = None):
    if kind not in (None, "main", "late"):
        raise HTTPException(400, "kind must be main or late")
    return svc.list_batches(limit=limit, kind=kind)


@router.post("/project")
def run_project(body: RunBody):
    if body.late_policy is not None and body.late_policy not in LATE_POLICIES:
        raise HTTPException(400, f"late_policy must be one of {list(LATE_POLICIES)}")
    try:
        return svc.run_project(
            body.stream_id,
            body.note,
            late_policy=body.late_policy,
            wave=body.wave,
        )
    except svc.ProjectError as e:
        raise HTTPException(400, e.message) from e


@router.post("/late-drop")
def late_drop(body: LateDropBody):
    if body.late_policy is not None and body.late_policy not in LATE_POLICIES:
        raise HTTPException(400, f"late_policy must be one of {list(LATE_POLICIES)}")
    try:
        return svc.apply_events(
            body.stream_id,
            body.events,
            late_policy=body.late_policy,
            note=body.note,
        )
    except svc.ProjectError as e:
        raise HTTPException(400, e.message) from e
