"""机组检修申请、风险评估、批准、生效、恢复与延长/取消用例。

关键约束：
- 风险评估输入在批准时固化为能力快照（内容寻址），审批后不可改写；
- 延长与取消不覆盖旧版本，只插入后继版本，版本链完整保留；
- 每次决定都写入决策台账和全局哈希审计日志，重放评估时用锁定快照
  重新计算并核对结果哈希，进程重启后状态与审计依据仍可还原。
"""

from __future__ import annotations

import json
import sqlite3
from decimal import Decimal
from typing import Any, Mapping

from .clock import parse_utc, utc_text
from .errors import Conflict, InvalidState, NotFound, ValidationFailed
from .maintenance import assess_capability
from .models import decimal_value, identifier, required_text
from .planning import canonical_json, decimal_text, digest
from .service import ROLE_PERMISSIONS, SupplyService
from .storage import transaction

# 新角色与权限在原有集合上追加，保持既有调度用例不变。
ROLE_PERMISSIONS.setdefault("maintenance", set())
ROLE_PERMISSIONS.setdefault("grid", set())
ROLE_PERMISSIONS["planner"] |= {"unit.write", "segment.write"}
ROLE_PERMISSIONS["dispatcher"] |= {"commitment.write"}
ROLE_PERMISSIONS["risk"] |= {"maintenance.assess"}
ROLE_PERMISSIONS["maintenance"] |= {
    "unit.write",
    "maintenance.request",
    "maintenance.lifecycle",
    "report.read",
}
ROLE_PERMISSIONS["grid"] |= {
    "segment.write",
    "commitment.write",
    "maintenance.approve",
    "report.read",
    "audit.read",
}

MAINTENANCE_KINDS = {"scheduled", "forced", "trial"}


class MaintenanceService(SupplyService):
    # ---- 基础数据：机组、峰谷时段、承诺出力 -----------------------------

    def register_unit(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "unit.write")
        unit_id = identifier(raw.get("unit_id"), "unit_id")
        name = required_text(raw.get("name"), "name")
        region = identifier(raw.get("region"), "region")
        rated = decimal_value(raw.get("rated_capacity_mw"), "rated_capacity_mw", minimum=Decimal("0.001"))
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO maintenance_units(unit_id,name,region,rated_capacity_mw,created_at) "
                    "VALUES(?,?,?,?,?)",
                    (unit_id, name, region, decimal_text(rated), self._now()),
                )
                self._audit("maintenance_unit", unit_id, "unit.registered", actor_id, raw)
        except sqlite3.IntegrityError as exc:
            raise Conflict("机组编号已经存在") from exc
        return {"unit_id": unit_id, "name": name, "region": region, "rated_capacity_mw": decimal_text(rated)}

    def define_segments(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "segment.write")
        region = identifier(raw.get("region"), "region")
        segments = raw.get("segments")
        if not isinstance(segments, list) or not segments:
            raise ValidationFailed("segments 必须是非空数组")
        parsed: list[tuple[str, str, str, str, str, str]] = []
        for index, item in enumerate(segments):
            if not isinstance(item, Mapping):
                raise ValidationFailed(f"segments[{index}] 必须是对象")
            kind = required_text(item.get("kind"), f"segments[{index}].kind", 16)
            if kind not in {"peak", "flat", "valley"}:
                raise ValidationFailed(f"segments[{index}].kind 必须是 peak、flat 或 valley")
            try:
                start = parse_utc(required_text(item.get("starts_at"), f"segments[{index}].starts_at"), "starts_at")
                end = parse_utc(required_text(item.get("ends_at"), f"segments[{index}].ends_at"), "ends_at")
            except ValueError as exc:
                raise ValidationFailed(str(exc)) from exc
            if end <= start:
                raise ValidationFailed(f"segments[{index}] 结束时间必须晚于开始时间")
            load = decimal_value(item.get("load_mw"), f"segments[{index}].load_mw", minimum=Decimal("0"))
            threshold = decimal_value(
                item.get("reserve_threshold_mw"),
                f"segments[{index}].reserve_threshold_mw",
                minimum=Decimal("0"),
            )
            parsed.append((region, utc_text(start), utc_text(end), kind, decimal_text(load), decimal_text(threshold)))
        with transaction(self.connection, immediate=True):
            for row in parsed:
                self.connection.execute(
                    "INSERT INTO peak_valley_segments(region,starts_at,ends_at,kind,load_mw,"
                    "reserve_threshold_mw,created_at) VALUES(?,?,?,?,?,?,?)",
                    (*row, self._now()),
                )
            self._audit("peak_valley_segments", region, "segments.defined", actor_id,
                        {"region": region, "count": len(parsed)})
        return {"region": region, "stored": len(parsed)}

    def register_commitment(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "commitment.write")
        unit_id = identifier(raw.get("unit_id"), "unit_id")
        if self.connection.execute("SELECT 1 FROM maintenance_units WHERE unit_id=?", (unit_id,)).fetchone() is None:
            raise NotFound("机组不存在")
        try:
            start = parse_utc(required_text(raw.get("starts_at"), "starts_at"), "starts_at")
            end = parse_utc(required_text(raw.get("ends_at"), "ends_at"), "ends_at")
        except ValueError as exc:
            raise ValidationFailed(str(exc)) from exc
        if end <= start:
            raise ValidationFailed("ends_at 必须晚于 starts_at")
        committed = decimal_value(raw.get("committed_mw"), "committed_mw", minimum=Decimal("0"))
        plan_ref = identifier(raw.get("plan_ref"), "plan_ref")
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "INSERT INTO committed_outputs(unit_id,starts_at,ends_at,committed_mw,plan_ref,"
                "created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                (unit_id, utc_text(start), utc_text(end), decimal_text(committed), plan_ref, actor_id, self._now()),
            )
            commitment_id = int(cursor.lastrowid)
            self._audit("committed_output", str(commitment_id), "commitment.registered", actor_id,
                        {"unit_id": unit_id, "plan_ref": plan_ref})
        return {"commitment_id": commitment_id, "unit_id": unit_id, "plan_ref": plan_ref,
                "committed_mw": decimal_text(committed)}

    # ---- 申请 -------------------------------------------------------------

    def request_maintenance(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "maintenance.request")
        request_id = identifier(raw.get("request_id"), "request_id")
        unit_id = identifier(raw.get("unit_id"), "unit_id")
        unit = self.connection.execute("SELECT * FROM maintenance_units WHERE unit_id=?", (unit_id,)).fetchone()
        if unit is None:
            raise NotFound("机组不存在")
        try:
            start = parse_utc(required_text(raw.get("starts_at"), "starts_at"), "starts_at")
            end = parse_utc(required_text(raw.get("ends_at"), "ends_at"), "ends_at")
        except ValueError as exc:
            raise ValidationFailed(str(exc)) from exc
        if end <= start:
            raise ValidationFailed("ends_at 必须晚于 starts_at")
        kind = required_text(raw.get("kind", "scheduled"), "kind", 24)
        if kind not in MAINTENANCE_KINDS:
            raise ValidationFailed("kind 必须是 scheduled、forced 或 trial")
        reason = required_text(raw.get("reason"), "reason", 512)
        now = self._now()
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO maintenance_requests(request_id,unit_id,kind,current_version,state,"
                    "created_by,created_at) VALUES(?,?,?,1,'requested',?,?)",
                    (request_id, unit_id, kind, actor_id, now),
                )
                self.connection.execute(
                    "INSERT INTO maintenance_versions(request_id,version,change_type,supersedes_version,"
                    "starts_at,ends_at,reason,state,created_by,created_at) "
                    "VALUES(?,1,'initial',NULL,?,?,?, 'requested',?,?)",
                    (request_id, utc_text(start), utc_text(end), reason, actor_id, now),
                )
                self._record_decision(request_id, 1, "request", actor_id, None, None, None,
                                      {"window": {"starts_at": utc_text(start), "ends_at": utc_text(end)},
                                       "unit_id": unit_id, "kind": kind, "reason": reason})
                self._audit("maintenance", request_id, "maintenance.requested", actor_id,
                            {"unit_id": unit_id, "version": 1})
        except sqlite3.IntegrityError as exc:
            raise Conflict("检修申请编号已经存在") from exc
        return {"request_id": request_id, "unit_id": unit_id, "version": 1, "state": "requested"}

    # ---- 风险评估 ---------------------------------------------------------

    def _load_version(self, request_id: str, version: int) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM maintenance_versions WHERE request_id=? AND version=?",
            (request_id, version),
        ).fetchone()
        if row is None:
            raise NotFound("检修申请版本不存在")
        return row

    def _latest_version(self, request_id: str) -> sqlite3.Row:
        request = self.connection.execute(
            "SELECT * FROM maintenance_requests WHERE request_id=?", (request_id,)
        ).fetchone()
        if request is None:
            raise NotFound("检修申请不存在")
        return self._load_version(request_id, int(request["current_version"]))

    def _effective_other_windows(self, unit: sqlite3.Row, exclude_request_id: str) -> list[dict[str, Any]]:
        """同区域其他申请当前生效的批准窗口（每个申请至多一个）。

        取每个申请最近有结论的版本：延长批准取代旧窗口，取消批准删除窗口，
        驳回则回退到上一个批准版本。正在评估的申请自身的旧版本不参与，
        否则延长评估会把自己已批准的窗口误判为占用冲突。这样两台机组错峰
        停机时，先批准机组的窗口会占用第二台评估时的备用能力。
        """
        rows = self.connection.execute(
            "SELECT v.*, r.unit_id AS unit_id FROM maintenance_versions v "
            "JOIN maintenance_requests r ON r.request_id=v.request_id "
            "JOIN maintenance_units u ON u.unit_id=r.unit_id "
            "WHERE u.region=? AND v.request_id<>? AND v.decision IS NOT NULL "
            "ORDER BY v.request_id,v.version",
            (unit["region"], exclude_request_id),
        ).fetchall()
        by_request: dict[str, list[sqlite3.Row]] = {}
        for row in rows:
            by_request.setdefault(row["request_id"], []).append(row)
        windows: list[dict[str, Any]] = []
        for request_id, versions in by_request.items():
            chosen = versions[-1]
            if chosen["decision"] == "rejected":
                approved = [item for item in versions if item["decision"] == "approved"]
                if not approved:
                    continue
                chosen = approved[-1]
            if chosen["change_type"] == "cancellation":
                continue
            windows.append({
                "request_id": request_id,
                "version": int(chosen["version"]),
                "unit_id": chosen["unit_id"],
                "starts_at": chosen["starts_at"],
                "ends_at": chosen["ends_at"],
            })
        return windows

    def _build_capability_input(self, version: sqlite3.Row, unit: sqlite3.Row) -> dict[str, Any]:
        start, end = version["starts_at"], version["ends_at"]
        units_rows = self.connection.execute(
            "SELECT unit_id,region,rated_capacity_mw,state FROM maintenance_units ORDER BY unit_id"
        ).fetchall()
        segment_rows = self.connection.execute(
            "SELECT segment_id,region,starts_at,ends_at,kind,load_mw,reserve_threshold_mw "
            "FROM peak_valley_segments WHERE region=? AND starts_at<? AND ends_at>? ORDER BY starts_at,segment_id",
            (unit["region"], end, start),
        ).fetchall()
        commitment_rows = self.connection.execute(
            "SELECT c.unit_id,c.starts_at,c.ends_at,c.committed_mw,c.plan_ref "
            "FROM committed_outputs c JOIN maintenance_units u ON u.unit_id=c.unit_id "
            "WHERE u.region=? AND c.starts_at<? AND c.ends_at>? AND c.superseded_at IS NULL "
            "ORDER BY c.commitment_id",
            (unit["region"], end, start),
        ).fetchall()
        candidate: dict[str, Any] = {
            "request_id": version["request_id"],
            "version": int(version["version"]),
            "unit_id": unit["unit_id"],
            "change_type": version["change_type"],
            "starts_at": start,
            "ends_at": end,
            "kind": self.connection.execute(
                "SELECT kind FROM maintenance_requests WHERE request_id=?", (version["request_id"],)
            ).fetchone()["kind"],
        }
        if version["change_type"] == "extension" and version["supersedes_version"] is not None:
            previous = self._load_version(version["request_id"], int(version["supersedes_version"]))
            candidate["predecessor_window"] = {
                "starts_at": previous["starts_at"],
                "ends_at": previous["ends_at"],
            }
        return {
            "schema": "maintenance-capability/1",
            "units": [dict(row) for row in units_rows],
            "segments": [dict(row) for row in segment_rows],
            "commitments": [dict(row) for row in commitment_rows],
            "maintenance_windows": self._effective_other_windows(unit, version["request_id"]),
            "candidate": candidate,
        }

    def _store_snapshot(self, capability_input: Mapping[str, Any], actor_id: str) -> tuple[int, str]:
        content = canonical_json(capability_input)
        content_sha = digest(capability_input)
        existing = self.connection.execute(
            "SELECT snapshot_id FROM capability_snapshots WHERE content_sha256=?", (content_sha,)
        ).fetchone()
        if existing is not None:
            return int(existing["snapshot_id"]), content_sha
        cursor = self.connection.execute(
            "INSERT INTO capability_snapshots(content_sha256,input_json,created_by,created_at) "
            "VALUES(?,?,?,?)",
            (content_sha, content, actor_id, self._now()),
        )
        return int(cursor.lastrowid), content_sha

    def assess_maintenance(self, actor_id: str, request_id: str, expected_version: int) -> dict[str, Any]:
        self._require(actor_id, "maintenance.assess")
        request = self.connection.execute(
            "SELECT * FROM maintenance_requests WHERE request_id=?", (request_id,)
        ).fetchone()
        if request is None:
            raise NotFound("检修申请不存在")
        if int(request["current_version"]) != expected_version:
            raise Conflict("expected_version 不是最新版本")
        version = self._load_version(request_id, expected_version)
        if version["state"] not in {"requested", "assessed"}:
            raise InvalidState("当前版本已经锁定结论，不能重新评估")
        unit = self.connection.execute(
            "SELECT * FROM maintenance_units WHERE unit_id=?", (request["unit_id"],)
        ).fetchone()
        with transaction(self.connection, immediate=True):
            capability_input = self._build_capability_input(version, unit)
            result = assess_capability(capability_input)
            snapshot_id, input_sha = self._store_snapshot(capability_input, actor_id)
            result_json = canonical_json(result)
            result_sha = digest(result)
            self.connection.execute(
                "UPDATE maintenance_versions SET state='assessed',snapshot_id=?,input_sha256=?,"
                "assessment_json=? WHERE request_id=? AND version=?",
                (snapshot_id, input_sha, result_json, request_id, expected_version),
            )
            self.connection.execute(
                "UPDATE maintenance_requests SET state='assessed' WHERE request_id=? AND state='requested'",
                (request_id,),
            )
            self._record_decision(request_id, expected_version, "assess", actor_id, snapshot_id,
                                  input_sha, result_sha, {"feasible": result["feasible"],
                                                          "conflict_count": result["conflict_count"]})
            self._audit("maintenance", request_id, "maintenance.assessed", actor_id,
                        {"version": expected_version, "snapshot_id": snapshot_id,
                         "feasible": result["feasible"], "conflicts": result["conflict_count"]})
        return {"snapshot_id": snapshot_id, "input_sha256": input_sha, "result_sha256": result_sha,
                "assessment": result}

    # ---- 批准 / 驳回 / 生效 / 恢复 ---------------------------------------

    def _decide(self, actor_id: str, request_id: str, expected_version: int, decision: str, reason: str = "") -> dict[str, Any]:
        request = self.connection.execute(
            "SELECT * FROM maintenance_requests WHERE request_id=?", (request_id,)
        ).fetchone()
        if request is None:
            raise NotFound("检修申请不存在")
        if int(request["current_version"]) != expected_version:
            raise Conflict("expected_version 不是最新版本")
        version = self._load_version(request_id, expected_version)
        if version["state"] != "assessed":
            raise InvalidState("只有完成风险评估的版本可以给出结论")
        if decision == "approved" and not json.loads(version["assessment_json"])["feasible"]:
            raise InvalidState("风险评估仍有冲突时间片，不能批准")
        snapshot_id = int(version["snapshot_id"])
        now = self._now()
        with transaction(self.connection, immediate=True):
            if decision == "rejected":
                new_state = "rejected"
                self.connection.execute(
                    "UPDATE maintenance_versions SET decision='rejected',decided_by=?,decided_at=?,"
                    "state='rejected' WHERE request_id=? AND version=?",
                    (actor_id, now, request_id, expected_version),
                )
                if version["change_type"] == "initial":
                    request_state = "rejected"
                else:
                    # 后继版本被驳回：旧版本继续生效，current_version 回退。
                    predecessor = self._load_version(request_id, int(version["supersedes_version"]))
                    request_state = predecessor["state"] if predecessor["state"] != "superseded" else "approved"
                self.connection.execute(
                    "UPDATE maintenance_requests SET current_version=?,state=? WHERE request_id=?",
                    (expected_version if version["change_type"] == "initial" else int(version["supersedes_version"]),
                     request_state, request_id),
                )
                self._record_decision(request_id, expected_version, "reject", actor_id, snapshot_id,
                                      version["input_sha256"], digest(json.loads(version["assessment_json"])),
                                      {"reason": reason})
                self._audit("maintenance", request_id, "maintenance.rejected", actor_id,
                            {"version": expected_version, "reason": reason})
                return {"request_id": request_id, "version": expected_version, "state": request_state,
                        "locked_snapshot_id": snapshot_id}

            # 批准：锁定快照不可变，旧版本在此刻才被后继版本取代。
            if version["change_type"] == "cancellation":
                new_state = "cancelled"
                request_state = "cancelled"
            elif version["change_type"] == "extension":
                predecessor = self._load_version(request_id, int(version["supersedes_version"]))
                was_active = predecessor["state"] == "active"
                new_state = "active" if was_active else "approved"
                request_state = new_state
                effective_from = predecessor["effective_from"] if was_active else None
            else:
                new_state = "approved"
                request_state = "approved"
                effective_from = None
            self.connection.execute(
                "UPDATE maintenance_versions SET decision='approved',decided_by=?,decided_at=?,"
                "state=? WHERE request_id=? AND version=?",
                (actor_id, now, new_state, request_id, expected_version),
            )
            if version["change_type"] == "extension":
                self.connection.execute(
                    "UPDATE maintenance_versions SET effective_from=? WHERE request_id=? AND version=?",
                    (effective_from, request_id, expected_version),
                )
            if version["supersedes_version"] is not None:
                self.connection.execute(
                    "UPDATE maintenance_versions SET state='superseded' "
                    "WHERE request_id=? AND version=? AND state IN ('approved','active','completed')",
                    (request_id, int(version["supersedes_version"])),
                )
            self.connection.execute(
                "UPDATE maintenance_requests SET state=? WHERE request_id=?",
                (request_state, request_id),
            )
            if version["change_type"] == "cancellation":
                # 取消批准：若机组正在检修，立即恢复可用；未来窗口直接作废。
                self.connection.execute(
                    "UPDATE maintenance_units SET state='available',revision=revision+1 "
                    "WHERE unit_id=? AND state='maintenance'",
                    (request["unit_id"],),
                )
            event = {
                "initial": "maintenance.approved",
                "extension": "maintenance.extension_approved",
                "cancellation": "maintenance.cancelled",
            }[version["change_type"]]
            self._record_decision(request_id, expected_version, "approve", actor_id, snapshot_id,
                                  version["input_sha256"], digest(json.loads(version["assessment_json"])),
                                  {"locked_snapshot_id": snapshot_id, "change_type": version["change_type"]})
            self._audit("maintenance", request_id, event, actor_id,
                        {"version": expected_version, "snapshot_id": snapshot_id})
        return {"request_id": request_id, "version": expected_version, "state": request_state,
                "locked_snapshot_id": snapshot_id}

    def approve_maintenance(self, actor_id: str, request_id: str, expected_version: int) -> dict[str, Any]:
        self._require(actor_id, "maintenance.approve")
        return self._decide(actor_id, request_id, expected_version, "approved")

    def reject_maintenance(self, actor_id: str, request_id: str, expected_version: int, reason: str) -> dict[str, Any]:
        self._require(actor_id, "maintenance.approve")
        reason_text = required_text(reason, "reason", 512)
        return self._decide(actor_id, request_id, expected_version, "rejected", reason_text)

    def activate_maintenance(self, actor_id: str, request_id: str) -> dict[str, Any]:
        self._require(actor_id, "maintenance.approve")
        version = self._latest_version(request_id)
        if version["state"] != "approved":
            raise InvalidState("只有已批准版本可以生效")
        now = self.clock.now()
        if utc_text(now) < version["starts_at"]:
            raise InvalidState("尚未到检修开始时间，不能提前生效")
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "UPDATE maintenance_versions SET state='active',effective_from=? "
                "WHERE request_id=? AND version=?",
                (self._now(), request_id, version["version"]),
            )
            self.connection.execute(
                "UPDATE maintenance_requests SET state='active' WHERE request_id=?", (request_id,)
            )
            self.connection.execute(
                "UPDATE maintenance_units SET state='maintenance',revision=revision+1 "
                "WHERE unit_id=(SELECT unit_id FROM maintenance_requests WHERE request_id=?)",
                (request_id,),
            )
            self._record_decision(request_id, int(version["version"]), "activate", actor_id,
                                  int(version["snapshot_id"]), version["input_sha256"], None,
                                  {"effective_from": self._now()})
            self._audit("maintenance", request_id, "maintenance.activated", actor_id,
                        {"version": version["version"]})
        return {"request_id": request_id, "version": int(version["version"]), "state": "active",
                "effective_from": self._now()}

    def complete_maintenance(self, actor_id: str, request_id: str) -> dict[str, Any]:
        self._require(actor_id, "maintenance.lifecycle")
        version = self._latest_version(request_id)
        if version["state"] != "active":
            raise InvalidState("只有生效中的检修可以恢复")
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "UPDATE maintenance_versions SET state='completed',recovered_at=? "
                "WHERE request_id=? AND version=?",
                (self._now(), request_id, version["version"]),
            )
            self.connection.execute(
                "UPDATE maintenance_requests SET state='completed' WHERE request_id=?", (request_id,)
            )
            self.connection.execute(
                "UPDATE maintenance_units SET state='available',revision=revision+1 "
                "WHERE unit_id=(SELECT unit_id FROM maintenance_requests WHERE request_id=?)",
                (request_id,),
            )
            self._record_decision(request_id, int(version["version"]), "complete", actor_id,
                                  int(version["snapshot_id"]), version["input_sha256"], None,
                                  {"recovered_at": self._now()})
            self._audit("maintenance", request_id, "maintenance.completed", actor_id,
                        {"version": version["version"], "recovered_at": self._now()})
        return {"request_id": request_id, "version": int(version["version"]), "state": "completed",
                "recovered_at": self._now()}

    # ---- 延长 / 取消：只产生后继版本 -------------------------------------

    def _successor(self, actor_id: str, request_id: str, expected_version: int,
                   change_type: str, starts_at: str | None, ends_at: str | None, reason: str) -> dict[str, Any]:
        version = self._latest_version(request_id)
        if int(version["version"]) != expected_version:
            raise Conflict("expected_version 不是最新版本")
        if version["state"] not in {"approved", "active"}:
            raise InvalidState("只有已批准或生效中的计划可以延长或取消")
        if version["decision"] != "approved":
            raise InvalidState("前序版本尚未批准，不能产生后继版本")
        new_version = int(version["version"]) + 1
        now = self._now()
        # 取消后继版本沿用旧窗口，纯函数据此计算“恢复可用后”的能力；
        # 评估窗口内机组重新可用，只会增加备用，不会改掉承诺出力。
        next_start = starts_at or version["starts_at"]
        next_end = ends_at or version["ends_at"]
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "INSERT INTO maintenance_versions(request_id,version,change_type,supersedes_version,"
                "starts_at,ends_at,reason,state,created_by,created_at) "
                "VALUES(?,?,?,?,?,?,?,'requested',?,?)",
                (request_id, new_version, change_type, expected_version, next_start, next_end,
                 reason, actor_id, now),
            )
            # 后继版本待决期间，旧版本继续生效（其 state 不变，仍为 approved/active），
            # current_version 指向待决版本以驱动评估和批准。
            self.connection.execute(
                "UPDATE maintenance_requests SET current_version=? WHERE request_id=?",
                (new_version, request_id),
            )
            action = "extend" if change_type == "extension" else "cancel"
            self._record_decision(request_id, new_version, action, actor_id,
                                  None, None, None,
                                  {"supersedes_version": expected_version, "reason": reason,
                                   "starts_at": next_start, "ends_at": next_end})
            self._audit("maintenance", request_id, f"maintenance.{change_type}_requested", actor_id,
                        {"version": new_version, "supersedes_version": expected_version})
        return {"request_id": request_id, "version": new_version, "state": "requested",
                "change_type": change_type, "supersedes_version": expected_version,
                "starts_at": next_start, "ends_at": next_end}

    def extend_maintenance(self, actor_id: str, request_id: str, expected_version: int,
                           new_ends_at: str, reason: str) -> dict[str, Any]:
        self._require(actor_id, "maintenance.lifecycle")
        version = self._latest_version(request_id)
        try:
            new_end = parse_utc(new_ends_at, "new_ends_at")
        except ValueError as exc:
            raise ValidationFailed(str(exc)) from exc
        if utc_text(new_end) <= version["ends_at"]:
            raise ValidationFailed("延长后的结束时间必须晚于当前版本结束时间")
        return self._successor(actor_id, request_id, expected_version, "extension",
                               version["starts_at"], utc_text(new_end), reason)

    def cancel_maintenance(self, actor_id: str, request_id: str, expected_version: int, reason: str) -> dict[str, Any]:
        self._require(actor_id, "maintenance.lifecycle")
        return self._successor(actor_id, request_id, expected_version, "cancellation",
                               None, None, reason)

    # ---- 查询、重放与审计 -------------------------------------------------

    def maintenance_request(self, actor_id: str, request_id: str) -> dict[str, Any]:
        self._require(actor_id, "report.read")
        request = self.connection.execute(
            "SELECT * FROM maintenance_requests WHERE request_id=?", (request_id,)
        ).fetchone()
        if request is None:
            raise NotFound("检修申请不存在")
        versions = self.connection.execute(
            "SELECT * FROM maintenance_versions WHERE request_id=? ORDER BY version", (request_id,)
        ).fetchall()
        decisions = self.connection.execute(
            "SELECT * FROM maintenance_decisions WHERE request_id=? ORDER BY decision_id", (request_id,)
        ).fetchall()
        return {
            "request": dict(request),
            "versions": [self._version_view(row) for row in versions],
            "decisions": [dict(row) for row in decisions],
        }

    @staticmethod
    def _version_view(row: sqlite3.Row) -> dict[str, Any]:
        view = dict(row)
        if view.get("assessment_json"):
            view["assessment"] = json.loads(view["assessment_json"])
        return view

    def replay_assessment(self, actor_id: str, request_id: str, version: int) -> dict[str, Any]:
        self._require(actor_id, "report.read")
        version_row = self._load_version(request_id, version)
        if version_row["snapshot_id"] is None:
            raise InvalidState("该版本没有锁定的能力快照")
        snapshot = self.connection.execute(
            "SELECT * FROM capability_snapshots WHERE snapshot_id=?", (version_row["snapshot_id"],)
        ).fetchone()
        capability_input = json.loads(snapshot["input_json"])
        recomputed = assess_capability(capability_input)
        recomputed_sha = digest(recomputed)
        stored_sha = digest(json.loads(version_row["assessment_json"]))
        return {
            "request_id": request_id,
            "version": version,
            "snapshot_id": int(snapshot["snapshot_id"]),
            "input_sha256": snapshot["content_sha256"],
            "stored_result_sha256": stored_sha,
            "recomputed_result_sha256": recomputed_sha,
            "matches": recomputed_sha == stored_sha,
            "assessment": recomputed,
        }

    def effective_windows(self, region: str, starts_at: str, ends_at: str) -> dict[str, Any]:
        """省调视角：查询区间内当前生效的检修窗口。"""
        try:
            start = parse_utc(starts_at, "starts_at")
            end = parse_utc(ends_at, "ends_at")
        except ValueError as exc:
            raise ValidationFailed(str(exc)) from exc
        if end <= start:
            raise ValidationFailed("ends_at 必须晚于 starts_at")
        requests = self.connection.execute(
            "SELECT r.request_id,r.unit_id FROM maintenance_requests r "
            "JOIN maintenance_units u ON u.unit_id=r.unit_id WHERE u.region=? ORDER BY r.request_id",
            (region,),
        ).fetchall()
        windows: list[dict[str, Any]] = []
        for request in requests:
            # 生效版本 = 最新一个仍未被取代的批准版本；后继版本待决或被驳回时
            # 自动回落到旧版本，取消批准后没有符合条件的行。
            version = self.connection.execute(
                "SELECT * FROM maintenance_versions WHERE request_id=? AND decision='approved' "
                "AND state IN ('approved','active','completed') ORDER BY version DESC LIMIT 1",
                (request["request_id"],),
            ).fetchone()
            if version is None:
                continue
            if version["ends_at"] <= utc_text(start) or version["starts_at"] >= utc_text(end):
                continue
            windows.append({
                "request_id": request["request_id"],
                "version": int(version["version"]),
                "unit_id": request["unit_id"],
                "starts_at": version["starts_at"],
                "ends_at": version["ends_at"],
                "state": version["state"],
                "locked_snapshot_id": version["snapshot_id"],
            })
        return {"region": region, "starts_at": utc_text(start), "ends_at": utc_text(end),
                "windows": windows}

    def _record_decision(
        self,
        request_id: str,
        version: int,
        action: str,
        actor_id: str,
        snapshot_id: int | None,
        input_sha: str | None,
        result_sha: str | None,
        detail: Mapping[str, Any],
    ) -> None:
        self.connection.execute(
            "INSERT INTO maintenance_decisions(request_id,version,action,actor_id,snapshot_id,"
            "input_sha256,result_sha256,detail_json,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (request_id, version, action, actor_id, snapshot_id, input_sha, result_sha,
             canonical_json(detail), self._now()),
        )
