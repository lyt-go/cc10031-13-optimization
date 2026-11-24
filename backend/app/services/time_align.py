"""时间对齐领域服务：维护多测点时序按统一粒度与时区基准的时间对齐与插值规则。

对齐语义：
- 发起对齐时冻结一份“拟对齐测点 + 各测点当前认可标定版本”的快照（草稿）。
- 提交时才按各测点最新获准的标定版本重算，生成唯一一份对齐结果，并把每个时格的
  取值出处（标定记录、版本键、系数、源文件摘要）写进结果与运行清单。
- 提交后的结果可复验：按运行清单重读版本产物，逐条核对取值和出处是否一致。
"""
from __future__ import annotations

import hashlib
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from app.services.base import BaseModuleService
from app.store import store

_GRANULARITY_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(ms|s|m|min|h|d)\s*$")
_UNIT_DELTA = {
    "ms": timedelta(milliseconds=1),
    "s": timedelta(seconds=1),
    "m": timedelta(minutes=1),
    "min": timedelta(minutes=1),
    "h": timedelta(hours=1),
    "d": timedelta(days=1),
}


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


def parse_granularity(value: Any) -> timedelta:
    match = _GRANULARITY_RE.match(str(value or ""))
    if not match:
        return timedelta(seconds=1)
    amount, unit = match.groups()
    return _UNIT_DELTA[unit] * float(amount)


def parse_ts(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
            try:
                return datetime.strptime(text, fmt)
            except ValueError:
                continue
    return None


def floor_ts(moment: datetime, step: timedelta) -> datetime:
    origin = datetime(1970, 1, 1, tzinfo=moment.tzinfo)
    steps = int((moment - origin) / step)
    return origin + steps * step


def fmt_ts(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds")


def record_digest(record: dict[str, Any]) -> str:
    canonical = "|".join(str(record.get(key, "")) for key in (
        "point", "aligned_timestamp", "source_timestamp", "value",
        "calibrate_id", "calibrate_version", "scale", "offset", "source_digest",
    ))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


class TimeAlignService(BaseModuleService):
    MODULE = "time_align"
    ENTITY = "对齐规则"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["规则编号", "对齐粒度", "时区基准", "插值方式", "容许偏移", "对齐条数", "未对齐条数", "规则版本"]
    REQUIRED_FIELDS = ["规则编号", "对齐粒度", "时区基准"]
    STATUS_ORDER = ["草稿", "待生效", "已生效", "已废弃"]
    ACTION_RULES = {"提交生效": "待生效", "确认生效": "已生效", "标记废弃": "已废弃"}
    NEGATIVE_ACTIONS = ["标记废弃"]

    # -- 路径 --

    def _draft_path(self, align_id: int) -> Any:
        return self._data_dir("aligned") / "drafts" / f"align-{align_id}.json"

    def _runs_dir(self) -> Any:
        return self._data_dir("aligned") / "runs"

    def _run_path(self, run_id: str) -> Any:
        return self._runs_dir() / f"{run_id}.json"

    def _calibrate_service(self):
        from app.services.unit_calibrate import UnitCalibrateService
        return UnitCalibrateService()

    # -- 发起：冻结拟对齐测点与当时的版本快照 --

    def propose_alignment(
        self, align_id: int, point_keys: list[str], operator: str | None = None,
    ) -> dict[str, Any]:
        rule = store.find(self.MODULE, align_id)
        if rule is None:
            return {"ok": False, "error": f"对齐规则 {align_id} 不存在"}
        points = self._normalize_points(point_keys)
        if not points:
            return {"ok": False, "error": "没有选择要参与对齐的测点"}
        cal = self._calibrate_service()
        snapshot = []
        for point in points:
            adopted = cal.adopted_record(point)
            source = cal._source_state(point)
            snapshot.append({
                "point": point,
                "status": "待标定" if adopted is None else (
                    "缺源" if source["status"] != "可读" else "可对齐"),
                "calibrate_id": adopted.get("id") if adopted else None,
                "标定编号": adopted.get("标定编号") if adopted else None,
                "scale": (
                    float(str(adopted.get("换算系数")).strip())
                    if adopted and str(adopted.get("换算系数", "")).strip() else None
                ) if adopted else None,
                "source_digest": source["fingerprint"],
                "decided_at": _now(),
            })
        draft = {
            "draft_id": f"ALIGN-DRAFT-{align_id}-{int(time.time())}",
            "align_id": align_id,
            "规则编号": rule.get("规则编号"),
            "对齐粒度": rule.get("对齐粒度"),
            "points": points,
            "version_snapshot": snapshot,
            "operator": operator,
            "created_at": _now(),
            "status": "草稿",
        }
        self._atomic_write_json(self._draft_path(align_id), draft)
        return {
            "ok": True,
            "draft": draft,
            "message": (
                "已发起对齐草稿并冻结版本快照；提交时会按各测点最新获准的标定版本重算，"
                "版本变化的测点会在结果中标记为需复核。"
            ),
        }

    # -- 提交：按最新获准版本重算，写出唯一结果并锁定出处 --

    def submit_alignment(
        self, align_id: int, point_keys: list[str] | None = None,
        *, draft_id: str | None = None, operator: str | None = None,
    ) -> dict[str, Any]:
        rule = store.find(self.MODULE, align_id)
        if rule is None:
            return {"ok": False, "error": f"对齐规则 {align_id} 不存在"}
        draft = self._read_json(self._draft_path(align_id), None)
        if draft_id and (draft is None or draft.get("draft_id") != draft_id):
            return {"ok": False, "error": "对齐草稿不存在或已过期，请重新发起"}
        points = self._normalize_points(point_keys) or [
            str(p) for p in (draft or {}).get("points", [])]
        if not points:
            return {"ok": False, "error": "没有选择要参与对齐的测点"}

        snapshot = {item["point"]: item for item in (draft or {}).get("version_snapshot", [])}
        cal = self._calibrate_service()
        granularity = parse_granularity(rule.get("对齐粒度"))

        resolved: list[dict[str, Any]] = []
        excluded: list[dict[str, Any]] = []
        revalidated: list[dict[str, Any]] = []
        artifacts: dict[str, list[dict[str, Any]]] = {}

        for point in points:
            resolution = cal.resolve_for_alignment(point)
            status = resolution.get("status")
            if status in ("缺源", "待标定", "规则无效"):
                excluded.append({
                    "point": point, "status": status,
                    "reason": resolution.get("message", "该测点当前不可对齐"),
                    "calibrate_id": resolution.get("calibrate_id"),
                })
                continue
            version_key = resolution.get("version_key")
            version_file = (self._data_dir("calibrated") / "versions" / point
                            / f"{version_key}.jsonl")
            if version_file.exists():
                records = self._read_jsonl(version_file)
            else:  # 兼容历史产物：没有版本目录时回落到当前可用产物
                records = self._read_jsonl(
                    self._data_dir("calibrated") / f"{point}_calibrated.jsonl")
            if not records:
                excluded.append({
                    "point": point, "status": "缺源",
                    "reason": "标定产物为空，无法对齐",
                    "calibrate_id": resolution.get("calibrate_id"),
                })
                continue
            artifacts[point] = records
            item = {
                "point": point,
                "status": "可对齐",
                "calibrate_id": resolution.get("calibrate_id"),
                "标定编号": resolution.get("标定编号"),
                "scale": resolution.get("scale"),
                "offset": resolution.get("offset"),
                "version_key": version_key,
                "source_digest": resolution.get("source_digest"),
                "records": len(records),
                "value_sample": records[0].get("calibrated_value_V"),
                "raw_sample": records[0].get("raw_value_mV"),
            }
            before = snapshot.get(point)
            if before is None:
                item["version_changed_since_draft"] = False
                item["change_reason"] = "发起时未包含该测点"
            elif before.get("calibrate_id") != item["calibrate_id"]:
                item["version_changed_since_draft"] = True
                item["change_reason"] = (
                    f"发起时采用标定记录 {before.get('calibrate_id')}（系数 {before.get('scale')}），"
                    f"提交前已获准 {item['calibrate_id']}（系数 {item['scale']}），按最新获准版本重算"
                )
                revalidated.append({
                    "point": point, "before": before, "after": item,
                    "reason": item["change_reason"],
                })
            else:
                item["version_changed_since_draft"] = False
            resolved.append(item)

        if not resolved:
            return {
                "ok": False,
                "error": "所选测点均不可对齐，未生成产物",
                "excluded": excluded,
            }

        grid, coverage, value_map = self._build_grid(artifacts, granularity)
        run_id = f"ALIGN-RUN-{rule['规则编号']}-{time.strftime('%Y%m%d%H%M%S', time.localtime())}"
        suffix = 1
        while self._run_path(run_id).exists():
            suffix += 1
            run_id = (f"ALIGN-RUN-{rule['规则编号']}-"
                      f"{time.strftime('%Y%m%d%H%M%S', time.localtime())}-{suffix}")
        info = {item["point"]: item for item in resolved}

        aligned_records: list[dict[str, Any]] = []
        aligned_map: dict[str, dict[str, Any]] = {}
        for slot in grid:
            for point in artifacts:
                entry = value_map.get(point, {}).get(slot)
                if entry is None:
                    continue
                meta = info[point]
                record = {
                    "align_run_id": run_id,
                    "align_rule": rule["规则编号"],
                    "align_granularity": rule.get("对齐粒度"),
                    "point": point,
                    "aligned_timestamp": fmt_ts(slot),
                    "source_timestamp": entry.get("timestamp"),
                    "raw_value_mV": entry.get("raw_value_mV"),
                    "value": entry.get("calibrated_value_V"),
                    "calibrate_id": meta["calibrate_id"],
                    "标定编号": meta["标定编号"],
                    "calibrate_version": meta["version_key"],
                    "scale": meta["scale"],
                    "offset": meta["offset"],
                    "source_digest": meta["source_digest"],
                    "align_status": "按最新获准版本对齐" if meta["version_changed_since_draft"]
                    else "按发起时版本对齐",
                }
                record["record_digest"] = record_digest(record)
                aligned_records.append(record)
                aligned_map[f"{point}@{record['aligned_timestamp']}"] = record

        output_file = self._data_dir("aligned") / f"{rule['规则编号']}_aligned.jsonl"
        self._atomic_write_jsonl(output_file, aligned_records)
        # 每次提交保留独立结果文件，重跑不会把上一版结果覆盖掉；通用文件只指向最新一次。
        run_artifact = self._runs_dir() / f"{run_id}_aligned.jsonl"
        self._atomic_write_jsonl(run_artifact, aligned_records)

        unaligned = sum(
            coverage.get(point, {}).get("missing_slots", 0) for point in artifacts)
        manifest = {
            "run_id": run_id,
            "align_id": align_id,
            "规则编号": rule["规则编号"],
            "对齐粒度": rule.get("对齐粒度"),
            "时区基准": rule.get("时区基准"),
            "draft_id": (draft or {}).get("draft_id"),
            "version_snapshot": list(snapshot.values()),
            "requested_points": points,
            "resolved_points": resolved,
            "excluded_points": excluded,
            "revalidated_points": revalidated,
            "grid": {
                "start": fmt_ts(grid[0]) if grid else None,
                "end": fmt_ts(grid[-1]) if grid else None,
                "slot_count": len(grid),
                "coverage": coverage,
            },
            "records": len(aligned_records),
            "unaligned_slots": unaligned,
            "output_file": str(output_file),
            "artifact_file": str(run_artifact),
            "operator": operator,
            "submitted_at": _now(),
            "status": "已提交",
        }
        self._atomic_write_json(self._run_path(run_id), manifest)
        self._atomic_write_json(
            self._data_dir("aligned") / f"{rule['规则编号']}_latest_run.json",
            {"run_id": run_id, "submitted_at": manifest["submitted_at"]})

        entry, _ = self.run_action(align_id, "确认生效")
        if entry:
            entry["对齐条数"] = str(len(aligned_records))
            entry["未对齐条数"] = str(unaligned)
        if draft is not None:
            draft["status"] = "已提交"
            draft["submitted_run_id"] = run_id
            self._atomic_write_json(self._draft_path(align_id), draft)

        return {
            "ok": True,
            "run_id": run_id,
            "align_rule": rule["规则编号"],
            "align_granularity": rule.get("对齐粒度"),
            "grid": manifest["grid"],
            "requested_points": points,
            "points": resolved,
            "excluded": excluded,
            "revalidated": revalidated,
            "aligned_count": len(aligned_records),
            "unaligned": unaligned,
            "output_file": str(output_file),
            "artifact_file": str(run_artifact),
            "manifest_file": str(self._run_path(run_id)),
            "message": self._submit_message(resolved, excluded, revalidated),
            "records": aligned_records if len(aligned_records) <= 50 else aligned_records[:50],
        }

    # -- 兼容旧调用：直接按点执行一次对齐 --

    def align_series(self, align_id: int, point_keys: list[str]) -> dict[str, Any]:
        outcome = self.submit_alignment(align_id, point_keys)
        if not outcome.get("ok"):
            return {"error": outcome.get("error", "对齐失败"),
                    "excluded": outcome.get("excluded", [])}
        return {
            "align_rule": outcome["align_rule"],
            "run_id": outcome["run_id"],
            "points": len(outcome["points"]),
            "aligned_count": outcome["aligned_count"],
            "unaligned": outcome["unaligned"],
            "excluded": outcome["excluded"],
            "revalidated": outcome["revalidated"],
            "grid": outcome["grid"],
            "output_file": outcome["output_file"],
        }

    # -- 已提交结果查询 --

    def latest_result(
        self, align_id: int, *, point: str | None = None,
        aligned_timestamp: str | None = None,
    ) -> dict[str, Any]:
        """读取该规则最后一次提交的结果与清单，可按测点/时格过滤。"""
        rule = store.find(self.MODULE, align_id)
        if rule is None:
            return {"ok": False, "error": f"对齐规则 {align_id} 不存在"}
        latest = self._read_json(
            self._data_dir("aligned") / f"{rule['规则编号']}_latest_run.json", None)
        run_id = (latest or {}).get("run_id")
        if not run_id:
            return {"ok": False, "error": "该规则还没有已提交的对齐结果"}
        manifest = self._read_json(self._run_path(run_id), None)
        if manifest is None:
            return {"ok": False, "error": f"对齐运行 {run_id} 的清单不存在"}
        records = self._read_jsonl(Path(manifest.get("artifact_file", "")))
        if aligned_timestamp:
            records = [r for r in records
                       if str(r.get("aligned_timestamp")) == str(aligned_timestamp)]
        if point:
            records = [r for r in records if r.get("point") == point]
        slots: dict[str, dict[str, Any]] = {}
        for record in records:
            slot = str(record.get("aligned_timestamp"))
            slots.setdefault(slot, {
                "aligned_timestamp": slot,
                "points": {},
            })["points"][record["point"]] = {
                "value": record.get("value"),
                "raw_value_mV": record.get("raw_value_mV"),
                "source_timestamp": record.get("source_timestamp"),
                "calibrate_id": record.get("calibrate_id"),
                "标定编号": record.get("标定编号"),
                "calibrate_version": record.get("calibrate_version"),
                "scale": record.get("scale"),
                "offset": record.get("offset"),
                "source_digest": record.get("source_digest"),
                "record_digest": record.get("record_digest"),
            }
        return {
            "ok": True,
            "run_id": run_id,
            "align_rule": rule["规则编号"],
            "align_granularity": manifest.get("对齐粒度"),
            "submitted_at": manifest.get("submitted_at"),
            "points": manifest.get("resolved_points", []),
            "excluded": manifest.get("excluded_points", []),
            "revalidated": manifest.get("revalidated_points", []),
            "coverage": manifest.get("grid", {}).get("coverage", {}),
            "slots": [slots[key] for key in sorted(slots)],
            "records": records,
            "output_file": manifest.get("output_file"),
        }

    # -- 复验：按提交的清单核对取值与出处 --
    def verify_alignment(
        self, align_id: int, run_id: str | None = None,
        *, point: str | None = None, aligned_timestamp: str | None = None,
    ) -> dict[str, Any]:
        rule = store.find(self.MODULE, align_id)
        if rule is None:
            return {"ok": False, "error": f"对齐规则 {align_id} 不存在"}
        if not run_id:
            latest = self._read_json(
                self._data_dir("aligned") / f"{rule['规则编号']}_latest_run.json", None)
            run_id = (latest or {}).get("run_id")
        if not run_id:
            return {"ok": False, "error": "该规则还没有已提交的对齐结果"}
        manifest = self._read_json(self._run_path(run_id), None)
        if manifest is None:
            return {"ok": False, "error": f"对齐运行 {run_id} 的清单不存在"}
        artifact = manifest.get("artifact_file")
        if artifact:
            records = self._read_jsonl(Path(artifact))
        else:
            records = self._read_jsonl(
                self._data_dir("aligned") / f"{rule['规则编号']}_aligned.jsonl")
        records = [r for r in records if r.get("align_run_id") == run_id]
        if aligned_timestamp:
            records = [r for r in records
                       if str(r.get("aligned_timestamp")) == str(aligned_timestamp)
                       or str(r.get("aligned_timestamp")).startswith(str(aligned_timestamp))]
        if point:
            records = [r for r in records if r.get("point") == point]
        if not records:
            return {"ok": False, "error": "没有匹配的对齐记录", "run_id": run_id}

        resolved = {item["point"]: item for item in manifest.get("resolved_points", [])}
        adopted_now = {
            item["point"]: (self._calibrate_service().adopted_record(item["point"]) or {}).get("id")
            for item in manifest.get("resolved_points", [])
        }
        checks: list[dict[str, Any]] = []
        for record in records:
            point_key = record.get("point")
            meta = resolved.get(point_key, {})
            version_key = record.get("calibrate_version")
            version_file = (self._data_dir("calibrated") / "versions" / point_key
                            / f"{version_key}.jsonl")
            artifact_status = "missing"
            artifact_record = None
            artifact_digest = None
            if version_file.exists():
                artifact_status = "present"
                artifact_digest = hashlib.sha256(version_file.read_bytes()).hexdigest()[:16]
                for candidate in self._read_jsonl(version_file):
                    if str(candidate.get("timestamp")) == str(record.get("source_timestamp")):
                        artifact_record = candidate
                        break
            recomputed = None
            if artifact_record is not None:
                raw = float(artifact_record.get("raw_value_mV", 0) or 0)
                recomputed = round(
                    raw * float(record.get("scale", 1) or 1)
                    + float(record.get("offset", 0) or 0), 4)
            digest_ok = record_digest(record) == record.get("record_digest")
            value_ok = recomputed is not None and abs(
                float(recomputed) - float(record.get("value", 0))) < 1e-9
            provenance_ok = (
                artifact_status == "present"
                and meta.get("version_key") == version_key
                and meta.get("calibrate_id") == record.get("calibrate_id")
                and meta.get("source_digest") == record.get("source_digest")
            )
            still_current = adopted_now.get(point_key) == record.get("calibrate_id")
            checks.append({
                "point": point_key,
                "aligned_timestamp": record.get("aligned_timestamp"),
                "source_timestamp": record.get("source_timestamp"),
                "value": record.get("value"),
                "calibrate_id": record.get("calibrate_id"),
                "标定编号": record.get("标定编号"),
                "calibrate_version": version_key,
                "scale": record.get("scale"),
                "source_digest": record.get("source_digest"),
                "artifact_status": artifact_status,
                "artifact_digest": artifact_digest,
                "recomputed_value": recomputed,
                "digest_ok": digest_ok,
                "value_ok": value_ok,
                "provenance_ok": provenance_ok,
                "version_still_current": still_current,
                "ok": bool(digest_ok and value_ok and provenance_ok),
            })
        all_ok = all(item["ok"] for item in checks)
        stale = [item for item in checks if not item["version_still_current"]]
        return {
            "ok": all_ok,
            "run_id": run_id,
            "align_rule": rule["规则编号"],
            "checked": len(checks),
            "all_ok": all_ok,
            "stale_points": sorted({item["point"] for item in stale}),
            "checks": checks,
            "message": (
                "取值与出处一致" if all_ok else "存在取值或出处不一致的记录"
            ) + ("" if not stale else "；部分测点已有更新获准版本，建议重新提交对齐"),
        }

    # -- 内部工具 --

    @staticmethod
    def _normalize_points(point_keys: list[str] | None) -> list[str]:
        points: list[str] = []
        for key in point_keys or []:
            point = str(key or "").strip()
            if point and point not in points:
                points.append(point)
        return points

    def _build_grid(
        self, artifacts: dict[str, list[dict[str, Any]]], step: timedelta,
    ) -> tuple[list[datetime], dict[str, dict[str, int]], dict[str, dict[datetime, dict[str, Any]]]]:
        """按各测点实际时间戳构造跨测点时格，每格每点取该格内最后一条采样。"""
        value_map: dict[str, dict[datetime, dict[str, Any]]] = {}
        slots: set[datetime] = set()
        for point, records in artifacts.items():
            buckets: dict[datetime, dict[str, Any]] = {}
            for record in records:
                moment = parse_ts(record.get("timestamp"))
                if moment is None:
                    continue
                slot = floor_ts(moment, step)
                existing = buckets.get(slot)
                if existing is None or moment >= (parse_ts(existing.get("timestamp"))
                                                  or moment):
                    buckets[slot] = record
                slots.add(slot)
            value_map[point] = buckets
        grid = sorted(slots)
        coverage: dict[str, dict[str, int]] = {}
        for point in artifacts:
            present = len(value_map.get(point, {}))
            coverage[point] = {
                "slots_with_value": present,
                "missing_slots": max(len(grid) - present, 0),
            }
        return grid, coverage, value_map

    @staticmethod
    def _submit_message(
        resolved: list[dict[str, Any]], excluded: list[dict[str, Any]],
        revalidated: list[dict[str, Any]],
    ) -> str:
        parts = [f"已按 {len(resolved)} 个测点提交对齐"]
        if revalidated:
            parts.append(
                "、".join(
                    f"{item['point']} 版本变化后按最新获准版本重算"
                    for item in revalidated))
        if excluded:
            parts.append(
                "未纳入本次对齐：" + "、".join(
                    f"{item['point']}（{item['status']}）" for item in excluded))
        return "；".join(parts)

    def detect_misaligned(self, point_key: str) -> dict[str, Any]:
        """检测指定测点序列中的时间不对齐数据点到异常日志字段文件。"""
        cal_file = self._data_dir("calibrated") / f"{point_key}_calibrated.jsonl"
        records = self._read_jsonl(cal_file)
        misaligned = []
        prev_ts = None
        for r in records:
            ts = r.get("timestamp", "")
            if prev_ts and ts < prev_ts:
                misaligned.append(r)
            prev_ts = ts
        # 将未对齐数据写入异常检测数据文件
        if misaligned:
            self._write_jsonl(
                self._data_dir("aligned") / f"{point_key}_misaligned.jsonl",
                misaligned
            )
        return {"point": point_key, "total": len(records),
                "misaligned_count": len(misaligned)}
