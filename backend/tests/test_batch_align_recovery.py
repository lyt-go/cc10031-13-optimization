"""批量标定续做与时间对齐版本锁定：A/B/C 三测点场景端到端验证。"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from app.services.time_align import TimeAlignService
from app.services.unit_calibrate import UnitCalibrateService
from app.store import store

CAL = UnitCalibrateService()
ALIGN = TimeAlignService()


A, B, C = "SENS-A", "SENS-B", "SENS-C"
ALIGN_TS = "2026-11-24T10:00:00"


@pytest.fixture()
def env():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        from app.services import base as base_mod
        with patch.object(base_mod, "DATA_ROOT", root):
            # 每个用例一份干净的内存仓库，避免用例之间串数据。
            saved = {name: [dict(row) for row in rows] for name, rows in store._tables.items()}
            for name in list(store._tables):
                store._tables[name] = []
            store._tables["unit_calibrate"] = [
                {"id": 1, "标定编号": "UNIT-A", "测点编号": A, "原始单位": "mV",
                 "目标单位": "V", "换算系数": "0.01", "零点偏移": "0",
                 "标定方式": "线性换算", "标定时间": "2026-11-20T09:00:00",
                 "status": "待标定", "pending": True, "abnormal": False},
                {"id": 2, "标定编号": "UNIT-B", "测点编号": B, "原始单位": "mV",
                 "目标单位": "V", "换算系数": "0.01", "零点偏移": "0",
                 "标定方式": "线性换算", "标定时间": "2026-11-20T09:00:00",
                 "status": "待标定", "pending": True, "abnormal": False},
                {"id": 3, "标定编号": "UNIT-C", "测点编号": C, "原始单位": "mV",
                 "目标单位": "V", "换算系数": "0.01", "零点偏移": "0",
                 "标定方式": "线性换算", "标定时间": "2026-11-20T09:00:00",
                 "status": "已标定", "pending": False, "abnormal": False},
            ]
            store._tables["time_align"] = [
                {"id": 1, "规则编号": "TIME-1", "对齐粒度": "1h", "时区基准": "UTC+8",
                 "插值方式": "取整点前最后一条", "容许偏移": "60s", "对齐条数": "0",
                 "未对齐条数": "0", "规则版本": "v1", "status": "待生效",
                 "pending": True, "abnormal": False},
            ]
            try:
                yield root
            finally:
                store._tables.clear()
                store._tables.update(saved)


def write_samples(root: Path, point: str, rows: list[tuple[str, float]]) -> None:
    path = root / "edge_samples" / f"{point}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps({"point": point, "timestamp": ts, "raw_value_mV": raw}) + "\n"
                for ts, raw in rows),
        encoding="utf-8",
    )


def test_batch_resume_and_alignment_version_lock(env):
    root = env
    rows = [("2026-11-24T09:59:58", 120.0), (ALIGN_TS, 120.0)]
    for point in (A, B, C):
        write_samples(root, point, rows)
    # B 的源文件暂不可读：用目录占位，模拟“文件暂不可读”。
    b_file = root / "edge_samples" / f"{B}.jsonl"
    b_file.unlink()
    b_file.mkdir()

    # -- 1. 班长一次多选 A/B/C 发起批量标定 --
    first = CAL.batch_calibrate([A, B, C])
    assert first["ok"], first
    summary = first["summary"]
    per_point = {item["point"]: item for item in first["results"]}
    assert A in summary["succeeded_points"]
    assert per_point[B]["status"] == "缺源" and per_point[B]["records"] == 0
    assert C in (summary["already_ready_points"] + summary["succeeded_points"])
    assert per_point[A]["value_sample"] == 1.2
    batch_key = first["batch_key"]

    # A 只有一份可用版本：120 * 0.01 = 1.2，产物文件不重复追加。
    assert CAL.point_versions(A)["usable_version_count"] == 1
    assert CAL.point_versions(A)["active_version"]["value_sample"] == 1.2
    assert len((root / "calibrated" / f"{A}_calibrated.jsonl")
               .read_text(encoding="utf-8").strip().splitlines()) == 2

    # -- 2. 值班员发起 A、C 两点的对齐（此时 C 仍按 0.01 快照） --
    proposed = ALIGN.propose_alignment(1, [A, C])
    assert proposed["ok"], proposed
    draft_id = proposed["draft"]["draft_id"]
    snapshot = {item["point"]: item for item in proposed["draft"]["version_snapshot"]}
    assert snapshot[A]["scale"] == 0.01 and snapshot[C]["scale"] == 0.01

    # -- 3. B 的文件恢复；C 更正为 0.02 并在提交前获准 --
    b_file.rmdir()
    write_samples(root, B, rows)
    created, missing = CAL.create_entry({
        "标定编号": "UNIT-C-02", "测点编号": C, "原始单位": "mV",
        "目标单位": "V", "换算系数": "0.02", "零点偏移": "0",
        "标定方式": "线性换算", "标定时间": "2026-11-24T09:00:00",
    })
    assert created and not missing
    CAL.run_action(created["id"], "提交标定")
    approved, _ = CAL.run_action(created["id"], "确认标定")
    assert approved and approved["status"] == "已标定"
    assert CAL.adopted_record(C)["id"] == created["id"]

    # -- 4. 先完成批次续做：B 独立补做，A 跳过，C 规则刚改→需复核 --
    resumed = CAL.batch_calibrate([A, B, C], batch_key=batch_key)
    assert resumed["ok"], resumed
    rsummary = resumed["summary"]
    rpoint = {item["point"]: item for item in resumed["results"]}
    assert rsummary["missing_source_points"] == []
    assert B in rsummary["succeeded_points"], "B 恢复后应在续做中独立补做"
    assert rpoint[B]["value_sample"] == 1.2
    assert A in rsummary["already_ready_points"], "A 已成功，续做应跳过而不是重算"
    assert rpoint[C]["status"] == "需复核"
    assert resumed["batch_key"] == batch_key and rsummary["counts"]["需复核"] == 1

    # A 仍然只有一份可用 1.2；C 也仍然只有一份可用版本（0.01），没有被悄悄覆盖。
    assert CAL.point_versions(A)["usable_version_count"] == 1
    assert CAL.point_versions(A)["active_version"]["value_sample"] == 1.2
    c_versions = CAL.point_versions(C)
    assert c_versions["usable_version_count"] == 1
    assert c_versions["active_version"]["scale"] == 0.01

    # -- 5. 再提交这次对齐：C 按提交前最新获准的 0.02 重算 --
    submitted = ALIGN.submit_alignment(1, [A, C], draft_id=draft_id)
    assert submitted["ok"], submitted
    assert submitted["requested_points"] == [A, C], "B 不在本次对齐范围内"
    assert [p["point"] for p in submitted["points"]] == [A, C]
    by_point = {item["point"]: item for item in submitted["points"]}
    assert by_point[A]["value_sample"] == 1.2
    assert by_point[C]["value_sample"] == 2.4, "C 提交前已获准 0.02，时格应采用 2.4"
    assert by_point[C]["scale"] == 0.02
    assert by_point[C]["version_changed_since_draft"] is True
    assert {item["point"] for item in submitted["revalidated"]} == {C}

    # 时格实际取值：A=1.2，C=2.4；各带自己的版本出处。
    slot = next(s for s in ALIGN.latest_result(1)["slots"]
                if s["aligned_timestamp"] == ALIGN_TS)
    assert slot["points"][A]["value"] == 1.2
    assert slot["points"][A]["calibrate_version"] == by_point[A]["version_key"]
    assert slot["points"][C]["value"] == 2.4
    assert slot["points"][C]["scale"] == 0.02
    assert slot["points"][C]["calibrate_version"] == by_point[C]["version_key"]

    # C 只剩一份当前可用版本（0.02），旧 0.01 在清单里标记为被接管。
    c_versions = CAL.point_versions(C)
    assert c_versions["usable_version_count"] == 1
    assert c_versions["active_version"]["scale"] == 0.02
    assert [v["state"] for v in c_versions["versions"]] == ["superseded", "active"]

    # -- 6. 用提交的对齐结果再复验一次该时间格：取值与出处一致 --
    verified = ALIGN.verify_alignment(1, aligned_timestamp=ALIGN_TS)
    assert verified["ok"], verified
    checks = {item["point"]: item for item in verified["checks"]}
    assert checks[A]["value"] == checks[A]["recomputed_value"] == 1.2
    assert checks[C]["value"] == checks[C]["recomputed_value"] == 2.4
    assert all(item["ok"] and item["provenance_ok"] for item in verified["checks"])
    assert verified["stale_points"] == []


def test_rerun_same_rule_keeps_single_usable_version(env):
    root = env
    rows = [("2026-11-24T10:00:00", 120.0)]
    write_samples(root, A, rows)
    first = CAL.batch_calibrate([A])
    second = CAL.batch_calibrate([A])
    third = CAL.calibrate_values(1)
    assert first["results"][0]["status"] == "成功"
    assert second["results"][0]["status"] == "已就绪"
    assert third["status"] == "已就绪"
    versions = CAL.point_versions(A)
    assert versions["usable_version_count"] == 1
    assert len(versions["versions"]) == 1
    assert versions["active_version"]["value_sample"] == 1.2
    assert len((root / "calibrated" / f"{A}_calibrated.jsonl")
               .read_text(encoding="utf-8").strip().splitlines()) == 1


def test_missing_source_keeps_previous_artifact_and_reports_resumable(env):
    root = env
    write_samples(root, A, [("2026-11-24T10:00:00", 120.0)])
    CAL.batch_calibrate([A])
    (root / "edge_samples" / f"{A}.jsonl").unlink()
    snapshot = CAL.batch_calibrate([A])
    item = snapshot["results"][0]
    assert item["status"] == "缺源"
    assert item["artifact_version"] == CAL.point_versions(A)["active_version"]["version_key"]
    assert snapshot["summary"]["resumable_scope"][0]["point"] == A
    # 旧产物仍然可用，且仍是一份。
    assert CAL.point_versions(A)["usable_version_count"] == 1
    assert len((root / "calibrated" / f"{A}_calibrated.jsonl")
               .read_text(encoding="utf-8").strip().splitlines()) == 1


def test_alignment_excludes_missing_source_point(env):
    root = env
    rows = [("2026-11-24T10:00:00", 120.0)]
    write_samples(root, A, rows)
    write_samples(root, B, rows)
    CAL.batch_calibrate([A, B])
    b_file = root / "edge_samples" / f"{B}.jsonl"
    b_file.unlink()
    b_file.mkdir()
    outcome = ALIGN.submit_alignment(1, [A, B])
    assert outcome["ok"], outcome
    assert [p["point"] for p in outcome["points"]] == [A]
    assert outcome["excluded"][0]["point"] == B
    assert outcome["excluded"][0]["status"] == "缺源"

    # 恢复后重提：B 独立补上，同一时格里两点都在，且 B 的产物是本次新生成的。
    b_file.rmdir()
    write_samples(root, B, rows)
    again = ALIGN.submit_alignment(1, [A, B])
    assert again["ok"], again
    slot = next(s for s in ALIGN.latest_result(1)["slots"]
                if s["aligned_timestamp"] == ALIGN_TS)
    assert slot["points"][A]["value"] == 1.2
    assert slot["points"][B]["value"] == 1.2
    assert again["run_id"] != outcome["run_id"]


def test_verify_detects_tampered_value(env):
    root = env
    write_samples(root, A, [("2026-11-24T10:00:00", 120.0)])
    CAL.batch_calibrate([A])
    ALIGN.submit_alignment(1, [A])
    artifact = next((root / "aligned" / "runs").glob("*_aligned.jsonl"))
    lines = artifact.read_text(encoding="utf-8").strip().splitlines()
    record = json.loads(lines[0])
    record["value"] = 9.9  # 篡改取值，摘要不再匹配
    artifact.write_text(json.dumps(record, ensure_ascii=False) + "\n", encoding="utf-8")
    verified = ALIGN.verify_alignment(1, aligned_timestamp=ALIGN_TS)
    assert verified["ok"] is False
    assert verified["checks"][0]["digest_ok"] is False
