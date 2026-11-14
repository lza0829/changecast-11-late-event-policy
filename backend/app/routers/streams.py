from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.engines.project import LATE_POLICIES
from app.services import project_service as svc

router = APIRouter(tags=["streams"])


class LatePolicyBody(BaseModel):
    late_policy: str


@router.get("/streams")
def list_streams():
    return svc.list_streams()


@router.get("/late-policies")
def late_policies():
    return [
        {
            "code": "reject",
            "title": "拒绝迟到",
            "summary": "拒绝纳入常规应用，记迟到拒绝次数，投影与主水位不动",
        },
        {
            "code": "bypass",
            "title": "迟到旁路批",
            "summary": "旁路单独编号，按每实体 applied_seq 规则修正投影，主水位不回拨",
        },
    ]


@router.put("/streams/{stream_id}/late-policy")
def set_late_policy(stream_id: int, body: LatePolicyBody):
    if body.late_policy not in LATE_POLICIES:
        raise HTTPException(400, f"late_policy must be one of {list(LATE_POLICIES)}")
    try:
        return svc.set_late_policy(stream_id, body.late_policy)
    except svc.ProjectError as e:
        raise HTTPException(400, e.message) from e


@router.post("/streams/{stream_id}/reset")
def reset_stream(stream_id: int):
    try:
        return svc.reset_stream(stream_id)
    except svc.ProjectError as e:
        raise HTTPException(400, e.message) from e


@router.get("/streams/{stream_id}/state")
def stream_state(stream_id: int):
    try:
        return svc.stream_state(stream_id)
    except svc.ProjectError as e:
        raise HTTPException(400, e.message) from e
