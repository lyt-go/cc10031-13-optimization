"""批量标定与时间对齐的领域动作接口。

通用资源路由只覆盖增删改查和状态流转；批量续做、对齐发起/提交/复验是跨测点的
领域动作，单独挂在模块子路径下，避免把业务判断塞进通用路由。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.schemas import ActionResult
from app.services import service_for

router = APIRouter(prefix="/api", tags=["领域动作"])


class PointSelection(BaseModel):
    point_keys: list[str] = Field(default_factory=list)
    batch_key: str | None = None
    resume: bool = True
    operator: str | None = None


class AlignProposal(BaseModel):
    point_keys: list[str] = Field(default_factory=list)
    operator: str | None = None


class AlignSubmission(BaseModel):
    point_keys: list[str] | None = None
    draft_id: str | None = None
    operator: str | None = None


class AlignVerification(BaseModel):
    run_id: str | None = None
    point: str | None = None
    aligned_timestamp: str | None = None


def _calibrate():
    service = service_for("unit_calibrate")
    if service is None:
        raise HTTPException(status_code=404, detail="单位标定服务未注册")
    return service


def _align():
    service = service_for("time_align")
    if service is None:
        raise HTTPException(status_code=404, detail="时间对齐服务未注册")
    return service


# -- 单位标定：选点、批量续做、版本清单 --

@router.get("/unit_calibrate/points")
def list_calibration_points() -> dict[str, Any]:
    return {"items": _calibrate().list_points()}


@router.get("/unit_calibrate/points/{point_key}/versions")
def point_calibration_versions(point_key: str) -> dict[str, Any]:
    return _calibrate().point_versions(point_key)


@router.post("/unit_calibrate/batch", response_model=ActionResult)
def batch_calibrate(payload: PointSelection) -> ActionResult:
    result = _calibrate().batch_calibrate(
        payload.point_keys, batch_key=payload.batch_key,
        resume=payload.resume, operator=payload.operator)
    return ActionResult(
        ok=bool(result.get("ok")), message=result.get("message") or _batch_message(result),
        detail=result)


@router.get("/unit_calibrate/batch/{batch_key}")
def batch_calibrate_state(batch_key: str) -> dict[str, Any]:
    result = _calibrate().batch_state(batch_key)
    if "error" in result:
        raise HTTPException(status_code=404, detail=result["error"])
    return result


# -- 时间对齐：发起、提交、复验 --

@router.get("/time_align/points")
def list_alignment_points() -> dict[str, Any]:
    return {"items": _calibrate().list_points()}


@router.post("/time_align/{align_id}/propose")
def propose_alignment(align_id: int, payload: AlignProposal) -> dict[str, Any]:
    return _align().propose_alignment(align_id, payload.point_keys, payload.operator)


@router.post("/time_align/{align_id}/submit", response_model=ActionResult)
def submit_alignment(align_id: int, payload: AlignSubmission) -> ActionResult:
    result = _align().submit_alignment(
        align_id, payload.point_keys, draft_id=payload.draft_id,
        operator=payload.operator)
    return ActionResult(
        ok=bool(result.get("ok")), message=result.get("message") or result.get("error", ""),
        detail=result)


@router.get("/time_align/{align_id}/results/latest")
def latest_alignment_result(align_id: int) -> dict[str, Any]:
    result = _align().latest_result(align_id)
    if not result.get("ok"):
        raise HTTPException(status_code=404, detail=result.get("error", "没有对齐结果"))
    return result


@router.post("/time_align/{align_id}/verify", response_model=ActionResult)
def verify_alignment(align_id: int, payload: AlignVerification) -> ActionResult:
    result = _align().verify_alignment(
        align_id, payload.run_id, point=payload.point,
        aligned_timestamp=payload.aligned_timestamp)
    return ActionResult(
        ok=bool(result.get("ok")), message=result.get("message") or result.get("error", ""),
        detail=result)


def _batch_message(result: dict[str, Any]) -> str:
    summary = result.get("summary", {})
    counts = summary.get("counts", {})
    return (
        f"批次 {result.get('batch_key')}：成功 {counts.get('成功', 0)}，"
        f"已就绪 {counts.get('已就绪', 0)}，缺源 {counts.get('缺源', 0)}，"
        f"需复核 {counts.get('需复核', 0)}"
    )
