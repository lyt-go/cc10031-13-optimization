"""单位标定领域服务：维护传感器原始值到目标计量单位的换算系数与零点偏移标定。

恢复语义（批量标定续做）：
- 每个测点只保留一份“当前可用”标定产物；重跑同一规则时按版本键原地覆盖，
  规则变化时新版本接管、旧版本在清单里标记为 superseded，操作员不会看到两个可选项。
- 批量任务逐台独立计算：成功、缺源（源文件不存在/暂不可读）、规则版本刚改（需复核）
  互不影响，任务状态落盘，可按键续做，已成功的测点不会被重复计算。
"""
from __future__ import annotations

import hashlib
import time
from pathlib import Path
from typing import Any

from app.services.base import BaseModuleService
from app.store import store

APPROVED_STATUS = "已标定"
# 上送批次链路自动生成的标定种子用“已生效”表示可直接采用的规则，一并视为已获准。
APPROVED_STATUSES = (APPROVED_STATUS, "已生效")
PENDING_STATUSES = ("待标定", "草稿")
READABLE = "可读"
MISSING = "缺源"


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


def _to_float(value: Any, default: float | None = None) -> float | None:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return default


def _fmt_num(value: float) -> str:
    if abs(value - round(value)) < 1e-12:
        return str(int(round(value)))
    return repr(value)


class UnitCalibrateService(BaseModuleService):
    MODULE = "unit_calibrate"
    ENTITY = "标定记录"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["标定编号", "测点编号", "原始单位", "目标单位", "换算系数", "零点偏移", "标定方式", "标定时间"]
    REQUIRED_FIELDS = ["标定编号", "测点编号", "原始单位"]
    STATUS_ORDER = ["草稿", "待标定", "已标定", "已失效"]
    ACTION_RULES = {"提交标定": "待标定", "确认标定": "已标定", "标记失效": "已失效"}
    NEGATIVE_ACTIONS = ["标记失效"]

    # -- 基础动作：确认标定即“获准采用”，同时刷新标定时间用于确定最新获准版本 --

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry, message = super().run_action(entry_id, action)
        if entry is not None and action == "确认标定":
            entry["标定时间"] = _now()
        return entry, message

    # -- 标定记录查询 --

    def approved_records(self, point_key: str) -> list[dict[str, Any]]:
        """返回测点已获准采用的标定记录，按（标定时间, id）升序。"""
        rows = [
            row for row in store.rows(self.MODULE)
            if row.get("测点编号") == point_key and row.get("status") in APPROVED_STATUSES
        ]
        rows.sort(key=lambda r: (str(r.get("标定时间", "")), int(r.get("id", 0))))
        return rows

    def adopted_record(self, point_key: str) -> dict[str, Any] | None:
        """当前采纳的标定规则：最新获准的那一条。"""
        rows = self.approved_records(point_key)
        return rows[-1] if rows else None

    def pending_records(self, point_key: str) -> list[dict[str, Any]]:
        rows = [
            row for row in store.rows(self.MODULE)
            if row.get("测点编号") == point_key and row.get("status") in PENDING_STATUSES
        ]
        rows.sort(key=lambda r: int(r.get("id", 0)))
        return rows

    # -- 源文件状态 --

    def _source_state(self, point_key: str) -> dict[str, Any]:
        path = self._data_dir("edge_samples") / f"{point_key}.jsonl"
        state: dict[str, Any] = {
            "point": point_key, "path": str(path), "status": MISSING,
            "detail": "", "records": None, "rows": 0, "fingerprint": "",
        }
        if not path.exists():
            state["detail"] = f"源文件不存在：{path.name}"
            return state
        try:
            raw_bytes = path.read_bytes()
            records, bad_lines = self._read_jsonl_checked(path)
        except OSError as exc:
            state["detail"] = f"源文件暂不可读：{exc}"
            return state
        if bad_lines:
            state["detail"] = f"源文件有 {bad_lines} 行无法解析，暂按不可读处理"
            return state
        if not records:
            state["detail"] = "源文件为空，没有可标定采样"
            return state
        state.update({
            "status": READABLE,
            "detail": "源文件可读",
            "records": records,
            "rows": len(records),
            "fingerprint": hashlib.sha256(raw_bytes).hexdigest()[:16],
        })
        return state

    # -- 版本清单 --

    def _manifest_path(self) -> Path:
        return self._data_dir("calibrated") / "manifest.json"

    def _load_manifest(self) -> dict[str, Any]:
        payload = self._read_json(self._manifest_path(), None)
        if not isinstance(payload, dict) or "points" not in payload:
            payload = {"points": {}}
        return payload

    def _save_manifest(self, manifest: dict[str, Any]) -> None:
        manifest["updated_at"] = _now()
        self._atomic_write_json(self._manifest_path(), manifest)

    @staticmethod
    def _version_key(calibrate_id: Any, scale: float, offset: float, fingerprint: str) -> str:
        digest = hashlib.sha256(
            f"{calibrate_id}|{_fmt_num(scale)}|{_fmt_num(offset)}|{fingerprint}".encode("utf-8")
        ).hexdigest()[:12]
        return f"VC-{calibrate_id}-{digest}"

    def _build_rows(
        self, point_key: str, record: dict[str, Any], scale: float, offset: float,
        samples: list[dict[str, Any]], version_key: str, source: dict[str, Any],
    ) -> list[dict[str, Any]]:
        rows = []
        for sample in samples:
            raw = _to_float(sample.get("raw_value", sample.get("raw_value_mV", 0)), 0.0) or 0.0
            rows.append({
                "point": sample.get("point", point_key),
                "timestamp": sample["timestamp"],
                "raw_value_mV": raw,
                "calibrated_value_V": round(raw * scale + offset, 4),
                "scale": scale,
                "offset": offset,
                "calibrate_id": record.get("id"),
                "标定编号": record.get("标定编号"),
                "calibrate_version": version_key,
                "source_file": source["path"],
                "source_digest": source["fingerprint"],
                "produced_at": _now(),
            })
        return rows

    def _write_version(
        self, point_key: str, record: dict[str, Any], scale: float, offset: float,
        source: dict[str, Any],
    ) -> tuple[dict[str, Any], str]:
        """写入（或复用）一个标定版本，并保证该测点只有一份当前可用产物。"""
        rows = self._build_rows(
            point_key, record, scale, offset, source["records"] or [],
            self._version_key(record.get("id"), scale, offset, source["fingerprint"]),
            source,
        )
        version_key = rows[0]["calibrate_version"] if rows else self._version_key(
            record.get("id"), scale, offset, source["fingerprint"])
        manifest = self._load_manifest()
        point_meta = manifest["points"].setdefault(point_key, {"active": None, "versions": []})
        active = point_meta.get("active")
        previous_key = active.get("version_key") if active else None
        reused = previous_key == version_key

        version_file = self._data_dir("calibrated") / "versions" / point_key / f"{version_key}.jsonl"
        self._atomic_write_jsonl(version_file, rows)
        # 当前可用产物固定为这一份，重跑覆盖而不是追加。
        self._atomic_write_jsonl(
            self._data_dir("calibrated") / f"{point_key}_calibrated.jsonl", rows)

        if not reused:
            for entry in point_meta.get("versions", []):
                if entry.get("version_key") == previous_key and entry.get("state") == "active":
                    entry["state"] = "superseded"
                    entry["superseded_at"] = _now()
                    entry["superseded_by"] = version_key
            meta = {
                "version_key": version_key,
                "calibrate_id": record.get("id"),
                "标定编号": record.get("标定编号"),
                "scale": scale,
                "offset": offset,
                "records": len(rows),
                "value_sample": rows[0]["calibrated_value_V"] if rows else None,
                "raw_sample": rows[0]["raw_value_mV"] if rows else None,
                "source_file": source["path"],
                "source_digest": source["fingerprint"],
                "produced_at": _now(),
                "state": "active",
                "supersedes": previous_key,
                "note": "" if previous_key else "首个可用版本",
            }
            point_meta.setdefault("versions", []).append(meta)
            point_meta["active"] = meta
            self._save_manifest(manifest)
            return meta, ("updated" if previous_key else "created")

        # 同一版本重跑：只刷新产物文件，不新增清单项，也不改变“唯一可用版本”。
        active["records"] = len(rows)
        active["value_sample"] = rows[0]["calibrated_value_V"] if rows else None
        active["source_digest"] = source["fingerprint"]
        active["produced_at"] = _now()
        point_meta["active"] = active
        self._save_manifest(manifest)
        return active, "reused"

    def _ensure_version(self, point_key: str, record: dict[str, Any]) -> dict[str, Any]:
        scale = _to_float(record.get("换算系数"), 1.0)
        offset = _to_float(record.get("零点偏移"), 0.0)
        if scale is None or offset is None:
            return {"status": "规则无效", "message": "标定记录的换算系数/零点偏移不是数字",
                    "calibrate_id": record.get("id")}
        source = self._source_state(point_key)
        if source["status"] != READABLE:
            return {"status": MISSING, "message": source["detail"],
                    "calibrate_id": record.get("id"), "source": source}
        meta, action = self._write_version(point_key, record, scale, offset, source)
        return {
            "status": "已就绪" if action == "reused" else "成功",
            "action": action,
            "message": "复用已完成的标定产物" if action == "reused"
            else ("按新规则生成标定产物并接管旧版本" if action == "updated" else "生成标定产物"),
            "calibrate_id": record.get("id"),
            "标定编号": record.get("标定编号"),
            "scale": scale,
            "offset": offset,
            "version_key": meta["version_key"],
            "records": meta["records"],
            "value_sample": meta.get("value_sample"),
            "raw_sample": meta.get("raw_sample"),
            "output_file": str(self._data_dir("calibrated") / f"{point_key}_calibrated.jsonl"),
            "source_file": source["path"],
            "source_digest": source["fingerprint"],
            "supersedes": meta.get("supersedes"),
        }

    # -- 单条标定 --

    def calibrate_values(self, calibrate_id: int) -> dict[str, Any]:
        """对单条标定记录执行换算并写出唯一可用产物；缺源时不改状态、不产出版本。"""
        cal = store.find(self.MODULE, calibrate_id)
        if cal is None:
            return {"error": f"标定记录 {calibrate_id} 不存在"}
        point_key = cal.get("测点编号", "")
        scale = _to_float(cal.get("换算系数"), 1.0)
        offset = _to_float(cal.get("零点偏移"), 0.0)
        if scale is None or offset is None:
            return {"error": "换算系数/零点偏移不是有效数字", "calibrate_id": cal.get("标定编号")}
        source = self._source_state(point_key)
        if source["status"] != READABLE:
            return {
                "calibrate_id": cal.get("标定编号"), "point": point_key,
                "records_calibrated": 0, "scale": scale, "offset": offset,
                "status": MISSING, "message": source["detail"],
                "source_file": source["path"],
            }
        entry, _ = self.run_action(calibrate_id, "确认标定")
        entry = entry or cal
        meta, action = self._write_version(point_key, entry, scale, offset, source)
        return {
            "calibrate_id": entry.get("标定编号"),
            "point": point_key,
            "records_calibrated": meta["records"],
            "scale": scale,
            "offset": offset,
            "status": "成功" if action != "reused" else "已就绪",
            "version_key": meta["version_key"],
            "value_sample": meta.get("value_sample"),
            "raw_sample": meta.get("raw_sample"),
            "output_file": str(self._data_dir("calibrated") / f"{point_key}_calibrated.jsonl"),
            "source_digest": source["fingerprint"],
            "supersedes": meta.get("supersedes"),
        }

    # -- 批量标定（可续做） --

    def _batch_path(self, batch_key: str) -> Path:
        return self._data_dir("calibrated") / "batches" / f"{batch_key}.json"

    def _next_batch_key(self) -> str:
        base = f"BATCH-{time.strftime('%Y%m%d%H%M%S', time.localtime())}"
        path = self._batch_path(base)
        suffix = 1
        while path.exists():
            suffix += 1
            path = self._batch_path(f"{base}-{suffix}")
        return path.stem

    def batch_calibrate(
        self, point_keys: list[str], *, batch_key: str | None = None,
        resume: bool = True, operator: str | None = None,
    ) -> dict[str, Any]:
        """逐台独立计算并落盘批次状态；可按键续做，已成功的测点不重复计算。"""
        points: list[str] = []
        for key in point_keys:
            point = str(key or "").strip()
            if point and point not in points:
                points.append(point)
        if not points:
            return {"ok": False, "message": "未选择任何待标定测点", "points": []}

        created = False
        job: dict[str, Any] | None = None
        if resume and batch_key:
            job = self._read_json(self._batch_path(batch_key), None)
        if job is None:
            batch_key = batch_key or self._next_batch_key()
            job = {
                "batch_key": batch_key,
                "created_at": _now(),
                "created_by": operator,
                "points": [],
                "results": {},
                "run_count": 0,
            }
            created = True
        job["points"] = list(dict.fromkeys([*job.get("points", []), *points]))

        per_point: list[dict[str, Any]] = []
        for point in points:
            previous = (job.get("results") or {}).get(point)
            per_point.append(self._batch_point(point, previous))
            job["results"][point] = per_point[-1]

        job["run_count"] = int(job.get("run_count", 0)) + 1
        job["last_run_at"] = _now()
        job["last_scope"] = points
        self._atomic_write_json(self._batch_path(batch_key), job)

        success = [r for r in per_point if r["status"] == "成功"]
        ready = [r for r in per_point if r["status"] == "已就绪"]
        missing = [r for r in per_point if r["status"] == MISSING]
        review = [r for r in per_point if r["status"] == "需复核"]
        waiting = [r for r in per_point if r["status"] in ("待标定", "无规则", "规则无效")]

        return {
            "ok": True,
            "batch_key": batch_key,
            "created": created,
            "resumed": not created,
            "run_count": job["run_count"],
            "created_at": job["created_at"],
            "updated_at": job["last_run_at"],
            "requested_points": points,
            "total_points": len(points),
            "results": per_point,
            "summary": {
                "counts": {
                    "成功": len(success), "已就绪": len(ready), "缺源": len(missing),
                    "需复核": len(review), "待标定": len(waiting),
                },
                "complete_data": [
                    {"point": r["point"], "calibrate_id": r["calibrate_id"],
                     "version_key": r["version_key"], "scale": r["scale"],
                     "records": r["records"], "value_sample": r.get("value_sample"),
                     "output_file": r["output_file"]}
                    for r in success + ready
                ],
                "resumable_scope": [
                    {"point": r["point"], "status": r["status"], "reason": r["message"]}
                    for r in missing + waiting
                ],
                "review_records": [
                    {"point": r["point"], "reason": r["message"],
                     "artifact_version": r.get("artifact_version"),
                     "adopted_version": r.get("version_key"),
                     "previous": r.get("previous")}
                    for r in review
                ],
                "succeeded_points": [r["point"] for r in success],
                "already_ready_points": [r["point"] for r in ready],
                "missing_source_points": [r["point"] for r in missing],
                "review_points": [r["point"] for r in review],
                "waiting_rule_points": [r["point"] for r in waiting],
            },
            "state_file": str(self._batch_path(batch_key)),
        }

    def _batch_point(self, point: str, previous: dict[str, Any] | None) -> dict[str, Any]:
        source = self._source_state(point)
        adopted = self.adopted_record(point)
        base: dict[str, Any] = {
            "point": point,
            "at": _now(),
            "source_file": source["path"],
            "source_status": source["status"],
            "source_digest": source["fingerprint"],
            "calibrate_id": adopted.get("id") if adopted else None,
            "标定编号": adopted.get("标定编号") if adopted else None,
            "scale": None, "offset": None, "version_key": None,
            "records": 0, "value_sample": None,
            "output_file": str(self._data_dir("calibrated") / f"{point}_calibrated.jsonl"),
            "artifact_version": None,
            "artifact_scale": None,
            "adopted_scale": None,
            "previous": None,
        }

        # 1) 源文件不可读：保留既有成功产物，仅报告缺源，可继续重试。
        if source["status"] != READABLE:
            active = (self._load_manifest()["points"].get(point) or {}).get("active")
            base.update({"status": MISSING, "message": source["detail"]})
            if adopted:
                base["scale"] = _to_float(adopted.get("换算系数"))
                base["offset"] = _to_float(adopted.get("零点偏移"), 0.0)
            if active:
                base["artifact_version"] = active.get("version_key")
                base["records"] = active.get("records", 0)
                base["value_sample"] = active.get("value_sample")
                if active.get("calibrate_id") == (adopted or {}).get("id"):
                    base["version_key"] = active.get("version_key")
                base["message"] = f"{source['detail']}；保留上次成功产物，待源文件恢复后可续做"
            return base

        # 2) 没有已获准规则，但有待标定记录：本次批量直接提交并确认。
        if adopted is None:
            pending = self.pending_records(point)
            if not pending:
                base.update({"status": "无规则", "message": "该测点没有待标定或已标定的标定记录"})
                return base
            record = pending[-1]
            outcome = self.calibrate_values(record["id"])
            if outcome.get("status") == MISSING:
                base.update({"status": MISSING, "message": outcome.get("message", "源文件不可读")})
                return base
            if "error" in outcome:
                base.update({
                    "status": "规则无效", "message": str(outcome["error"]),
                    "calibrate_id": record.get("id"), "标定编号": record.get("标定编号"),
                })
                return base
            base.update({
                "status": outcome.get("status", "成功"),
                "message": "按待标定记录完成本轮标定" if outcome.get("status") == "成功"
                else outcome.get("message", "已有可用标定产物"),
                "calibrate_id": record.get("id"),
                "标定编号": record.get("标定编号"),
                "scale": outcome.get("scale"),
                "offset": outcome.get("offset"),
                "version_key": outcome.get("version_key"),
                "records": outcome.get("records_calibrated", 0),
                "value_sample": outcome.get("value_sample"),
                "source_digest": outcome.get("source_digest", source["fingerprint"]),
                "supersedes": outcome.get("supersedes"),
            })
            return base

        manifest = self._load_manifest()
        point_meta = manifest["points"].get(point) or {}
        active = point_meta.get("active")
        base.update({
            "scale": _to_float(adopted.get("换算系数")),
            "offset": _to_float(adopted.get("零点偏移"), 0.0),
            "artifact_version": (active or {}).get("version_key"),
            "artifact_calibrate_id": (active or {}).get("calibrate_id"),
        })

        # 3) 之前已经算过：规则版本在批次中途变更 → 需复核，不改写产物。
        if previous and previous.get("status") in ("成功", "已就绪"):
            base["previous"] = {
                "status": previous.get("status"),
                "calibrate_id": previous.get("calibrate_id"),
                "version_key": previous.get("version_key"),
                "value_sample": previous.get("value_sample"),
            }
            if previous.get("calibrate_id") != adopted.get("id"):
                artifact = active or {
                    "version_key": previous.get("version_key"),
                    "records": previous.get("records", 0),
                    "value_sample": previous.get("value_sample"),
                    "scale": previous.get("scale"),
                    "calibrate_id": previous.get("calibrate_id"),
                }
                base.update({
                    "status": "需复核",
                    # 产物口径仍按旧规则，获准口径单列，避免“系数 0.02 却给出 1.2”的歧义。
                    "scale": artifact.get("scale"),
                    "artifact_scale": artifact.get("scale"),
                    "adopted_scale": _to_float(adopted.get("换算系数")),
                    "version_key": artifact.get("version_key"),
                    "records": artifact.get("records", 0),
                    "value_sample": artifact.get("value_sample"),
                    "message": (
                        f"标定规则在批次中途变更：现有产物按标定记录 {previous.get('calibrate_id')} "
                        f"（系数 {previous.get('scale')}，首值 {previous.get('value_sample')}）生成，"
                        f"当前获准记录为 {adopted.get('id')}（系数 {adopted.get('换算系数')}）。"
                        "未自动覆盖产物，请复核后重新对齐。"
                    ),
                })
                if active and active.get("calibrate_id") == adopted.get("id"):
                    base["message"] = (
                        "标定规则在批次中途变更，但当前可用产物已由最新获准记录生成，需人工确认后放行。"
                    )
                return base
            if previous.get("source_digest") == source["fingerprint"]:
                artifact = active or {
                    "version_key": previous.get("version_key"),
                    "records": previous.get("records", 0),
                    "value_sample": previous.get("value_sample"),
                    "scale": previous.get("scale"),
                }
                base.update({
                    "status": "已就绪",
                    "message": "已完成且源文件未变化，续做跳过，不重复计算",
                    "scale": artifact.get("scale", base["scale"]),
                    "artifact_scale": artifact.get("scale", base["scale"]),
                    "version_key": artifact.get("version_key"),
                    "records": artifact.get("records", 0),
                    "value_sample": artifact.get("value_sample"),
                })
                return base

        # 4) 规则一致（或首次/上次缺源）：按当前获准规则生成或复用版本。
        if active and active.get("calibrate_id") == adopted.get("id") \
                and active.get("source_digest") == source["fingerprint"]:
            base.update({
                "status": "已就绪",
                "message": "已有可用产物且与获准规则、源文件一致",
                "version_key": active.get("version_key"),
                "records": active.get("records", 0),
                "value_sample": active.get("value_sample"),
            })
            return base

        outcome = self._ensure_version(point, adopted)
        if outcome.get("status") == MISSING:
            base.update({"status": MISSING, "message": outcome.get("message", "源文件不可读")})
            return base
        base.update({
            "status": outcome.get("status", "成功"),
            "message": outcome.get("message", "按获准规则生成本轮标定产物"),
            "version_key": outcome.get("version_key"),
            "records": outcome.get("records", 0),
            "value_sample": outcome.get("value_sample"),
            "source_digest": outcome.get("source_digest", source["fingerprint"]),
            "supersedes": outcome.get("supersedes"),
        })
        return base

    def batch_state(self, batch_key: str) -> dict[str, Any]:
        job = self._read_json(self._batch_path(batch_key), None)
        if job is None:
            return {"error": f"批量标定任务 {batch_key} 不存在"}
        return job

    # -- 对外查询：测点版本清单 --

    def point_versions(self, point_key: str) -> dict[str, Any]:
        manifest = self._load_manifest()
        point_meta = manifest["points"].get(point_key)
        active = (point_meta or {}).get("active")
        versions = (point_meta or {}).get("versions", [])
        return {
            "point": point_key,
            "active_version": active,
            "usable_version_count": 1 if active else 0,
            "versions": versions,
            "approved_records": [
                {"id": r.get("id"), "标定编号": r.get("标定编号"),
                 "换算系数": r.get("换算系数"), "零点偏移": r.get("零点偏移"),
                 "标定时间": r.get("标定时间"), "status": r.get("status")}
                for r in self.approved_records(point_key)
            ],
            "adopted_calibrate_id": (self.adopted_record(point_key) or {}).get("id"),
            "source": {
                "path": str(self._data_dir("edge_samples") / f"{point_key}.jsonl"),
                "status": self._source_state(point_key)["status"],
            },
        }

    def list_points(self) -> list[dict[str, Any]]:
        """汇总标定记录涉及的测点，供前端多选与对齐选点。"""
        points: dict[str, dict[str, Any]] = {}
        for row in store.rows(self.MODULE):
            point = str(row.get("测点编号") or "").strip()
            if not point:
                continue
            item = points.setdefault(point, {
                "point": point, "approved": 0, "pending": 0,
                "adopted_calibrate_id": None, "active_version": None,
                "source_status": None,
            })
            if row.get("status") in APPROVED_STATUSES:
                item["approved"] += 1
            elif row.get("status") in PENDING_STATUSES:
                item["pending"] += 1
        manifest = self._load_manifest()
        for point, item in points.items():
            adopted = self.adopted_record(point)
            item["adopted_calibrate_id"] = adopted.get("id") if adopted else None
            item["adopted_scale"] = adopted.get("换算系数") if adopted else None
            active = (manifest["points"].get(point) or {}).get("active")
            item["active_version"] = active.get("version_key") if active else None
            item["usable_version_count"] = 1 if active else 0
            item["source_status"] = self._source_state(point)["status"]
        return sorted(points.values(), key=lambda item: item["point"])

    # -- 与时间对齐的衔接 --

    def resolve_for_alignment(self, point_key: str) -> dict[str, Any]:
        """对齐提交时解析该测点当前认可的标定版本，必要时重建唯一可用产物。"""
        adopted = self.adopted_record(point_key)
        if adopted is None:
            pending = self.pending_records(point_key)
            return {
                "status": "待标定",
                "point": point_key,
                "message": "没有已获准的标定规则，不能进入对齐",
                "pending_records": [r.get("id") for r in pending],
            }
        outcome = self._ensure_version(point_key, adopted)
        outcome["point"] = point_key
        outcome["calibrate_id"] = adopted.get("id")
        outcome["标定编号"] = adopted.get("标定编号")
        outcome["scale"] = _to_float(adopted.get("换算系数"), 1.0)
        outcome["offset"] = _to_float(adopted.get("零点偏移"), 0.0)
        return outcome
