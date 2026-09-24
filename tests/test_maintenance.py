from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from power_dispatch.api import JsonApplication
from power_dispatch.clock import FrozenClock
from power_dispatch.errors import Conflict, Forbidden, InvalidState
from power_dispatch.maintenance_service import MaintenanceService
from power_dispatch.storage import connect


SEGMENTS = [
    {"starts_at": "2026-09-25T00:00:00Z", "ends_at": "2026-09-25T08:00:00Z", "kind": "valley", "load_mw": "80", "reserve_threshold_mw": "20"},
    {"starts_at": "2026-09-25T08:00:00Z", "ends_at": "2026-09-25T20:00:00Z", "kind": "peak", "load_mw": "150", "reserve_threshold_mw": "50"},
    {"starts_at": "2026-09-25T20:00:00Z", "ends_at": "2026-09-26T00:00:00Z", "kind": "valley", "load_mw": "80", "reserve_threshold_mw": "20"},
    {"starts_at": "2026-09-26T00:00:00Z", "ends_at": "2026-09-26T20:00:00Z", "kind": "peak", "load_mw": "150", "reserve_threshold_mw": "50"},
    {"starts_at": "2026-09-26T20:00:00Z", "ends_at": "2026-09-27T00:00:00Z", "kind": "valley", "load_mw": "80", "reserve_threshold_mw": "20"},
    {"starts_at": "2026-09-27T00:00:00Z", "ends_at": "2026-09-27T20:00:00Z", "kind": "peak", "load_mw": "150", "reserve_threshold_mw": "50"},
    {"starts_at": "2026-09-27T20:00:00Z", "ends_at": "2026-09-28T00:00:00Z", "kind": "valley", "load_mw": "80", "reserve_threshold_mw": "20"},
]


class MaintenanceTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.clock = FrozenClock(datetime(2026, 9, 24, 0, 0, tzinfo=timezone.utc))
        self.service = MaintenanceService(self.connection, self.clock)
        for uid, role in (("plan", "planner"), ("grid", "grid"), ("maint", "maintenance"),
                          ("risk", "risk"), ("audit", "auditor"), ("dispatch", "dispatcher")):
            self.service.create_user(uid, uid, role)
        for unit in ("u1", "u2", "u3"):
            self.service.register_unit("maint", {"unit_id": unit, "name": unit, "region": "north", "rated_capacity_mw": "100"})
        self.service.define_segments("grid", {"region": "north", "segments": SEGMENTS})

    def tearDown(self) -> None:
        self.connection.close()

    def request(self, request_id: str, unit: str, start: str, end: str, *, reason: str = "例行检修"):
        return self.service.request_maintenance("maint", {
            "request_id": request_id, "unit_id": unit, "starts_at": start, "ends_at": end,
            "reason": reason, "kind": "scheduled",
        })

    def assess(self, request_id: str, version: int = 1):
        return self.service.assess_maintenance("risk", request_id, version)


class WorkflowTests(MaintenanceTestBase):
    def test_full_lifecycle_locks_snapshot_and_recovers_unit(self) -> None:
        self.request("m1", "u1", "2026-09-25T00:00:00Z", "2026-09-26T00:00:00Z")
        assessed = self.assess("m1")
        self.assertTrue(assessed["assessment"]["feasible"])
        snapshot_id = assessed["snapshot_id"]
        approved = self.service.approve_maintenance("grid", "m1", 1)
        self.assertEqual(approved["locked_snapshot_id"], snapshot_id)

        # 批准后快照内容不可变。
        before = self.connection.execute("SELECT input_json FROM capability_snapshots WHERE snapshot_id=?", (snapshot_id,)).fetchone()["input_json"]
        self.clock.advance(hours=25)
        self.service.activate_maintenance("grid", "m1")
        self.assertEqual(self.connection.execute("SELECT state FROM maintenance_units WHERE unit_id='u1'").fetchone()["state"], "maintenance")
        after = self.connection.execute("SELECT input_json FROM capability_snapshots WHERE snapshot_id=?", (snapshot_id,)).fetchone()["input_json"]
        self.assertEqual(before, after)

        self.service.complete_maintenance("maint", "m1")
        self.assertEqual(self.connection.execute("SELECT state FROM maintenance_units WHERE unit_id='u1'").fetchone()["state"], "available")
        detail = self.service.maintenance_request("audit", "m1")
        self.assertEqual(detail["request"]["state"], "completed")
        self.assertEqual([d["action"] for d in detail["decisions"]],
                         ["request", "assess", "approve", "activate", "complete"])

    def test_cannot_activate_before_window_start(self) -> None:
        self.request("m1", "u1", "2026-09-25T00:00:00Z", "2026-09-26T00:00:00Z")
        self.assess("m1")
        self.service.approve_maintenance("grid", "m1", 1)
        with self.assertRaises(InvalidState):
            self.service.activate_maintenance("grid", "m1")


class ConflictTests(MaintenanceTestBase):
    def test_conflict_returns_affected_slices_and_reasons(self) -> None:
        # u1 先停 25 日并批准。
        self.request("m1", "u1", "2026-09-25T00:00:00Z", "2026-09-26T00:00:00Z")
        self.assess("m1")
        self.service.approve_maintenance("grid", "m1", 1)
        # u2 同时段停机：峰段只剩 u3 100MW，无法覆盖负荷 150 + 阈值 50。
        self.request("m2", "u2", "2026-09-25T00:00:00Z", "2026-09-26T00:00:00Z")
        result = self.assess("m2")
        self.assertFalse(result["assessment"]["feasible"])
        peak_conflicts = [s for s in result["assessment"]["slices"] if s["kind"] == "peak" and s["conflicts"]]
        self.assertEqual(len(peak_conflicts), 1)
        slice_ = peak_conflicts[0]
        self.assertEqual(slice_["starts_at"], "2026-09-25T08:00:00Z")
        self.assertEqual(slice_["available_capacity_mw"], "100.000")
        self.assertEqual(slice_["conflicts"][0]["code"], "reserve_below_threshold")
        self.assertEqual(slice_["conflicts"][0]["required_mw"], "200.000")
        self.assertEqual(slice_["conflicts"][0]["shortfall_mw"], "100.000")
        # 谷段（停两台仍有 100MW，负荷 80 + 阈值 20 = 100）不冲突。
        self.assertTrue(all(not s["conflicts"] for s in result["assessment"]["slices"] if s["kind"] == "valley"))
        with self.assertRaises(InvalidState):
            self.service.approve_maintenance("grid", "m2", 1)

    def test_staggered_outage_on_another_day_is_allowed(self) -> None:
        self.request("m1", "u1", "2026-09-25T00:00:00Z", "2026-09-26T00:00:00Z")
        self.assess("m1")
        self.service.approve_maintenance("grid", "m1", 1)
        self.request("m2", "u2", "2026-09-26T00:00:00Z", "2026-09-27T00:00:00Z")
        self.assertTrue(self.assess("m2")["assessment"]["feasible"])

    def test_candidate_with_committed_output_in_window_is_blocked(self) -> None:
        self.service.register_commitment("dispatch", {
            "unit_id": "u2", "starts_at": "2026-09-25T09:00:00Z", "ends_at": "2026-09-25T12:00:00Z",
            "committed_mw": "40", "plan_ref": "day-ahead-0925",
        })
        self.request("m1", "u2", "2026-09-25T00:00:00Z", "2026-09-26T00:00:00Z")
        result = self.assess("m1")
        self.assertFalse(result["assessment"]["feasible"])
        codes = {c["code"] for s in result["assessment"]["slices"] for c in s["conflicts"]}
        self.assertIn("committed_output_unmet", codes)

    def test_extension_into_committed_output_is_blocked(self) -> None:
        self.request("m1", "u1", "2026-09-25T00:00:00Z", "2026-09-26T00:00:00Z")
        self.assess("m1")
        self.service.approve_maintenance("grid", "m1", 1)
        # 机组原定 26 日恢复后已有承诺出力，临时延长到 27 日不得改掉它。
        self.service.register_commitment("dispatch", {
            "unit_id": "u1", "starts_at": "2026-09-26T08:00:00Z", "ends_at": "2026-09-26T18:00:00Z",
            "committed_mw": "30", "plan_ref": "day-ahead-0926",
        })
        self.service.extend_maintenance("maint", "m1", 1, "2026-09-27T00:00:00Z", "备件延迟")
        result = self.assess("m1", 2)
        self.assertFalse(result["assessment"]["feasible"])
        codes = {c["code"] for s in result["assessment"]["slices"] for c in s["conflicts"]}
        self.assertIn("extension_changes_committed_output", codes)


class SuccessorVersionTests(MaintenanceTestBase):
    def test_extension_creates_successor_and_keeps_history(self) -> None:
        self.request("m1", "u1", "2026-09-25T00:00:00Z", "2026-09-26T00:00:00Z")
        self.assess("m1")
        self.service.approve_maintenance("grid", "m1", 1)
        self.clock.advance(hours=25)
        self.service.activate_maintenance("grid", "m1")
        successor = self.service.extend_maintenance("maint", "m1", 1, "2026-09-27T00:00:00Z", "需要复检")
        self.assertEqual((successor["version"], successor["change_type"], successor["supersedes_version"]), (2, "extension", 1))

        # 后继版本待决期间旧版本仍然生效，省调视图不中断。
        windows = self.service.effective_windows("north", "2026-09-25T00:00:00Z", "2026-09-28T00:00:00Z")
        self.assertEqual([(w["version"], w["ends_at"]) for w in windows["windows"]], [(1, "2026-09-26T00:00:00Z")])

        self.assess("m1", 2)
        self.service.approve_maintenance("grid", "m1", 2)
        versions = self.service.maintenance_request("audit", "m1")["versions"]
        self.assertEqual([(v["version"], v["change_type"], v["state"]) for v in versions],
                         [(1, "initial", "superseded"), (2, "extension", "active")])
        windows = self.service.effective_windows("north", "2026-09-25T00:00:00Z", "2026-09-28T00:00:00Z")
        self.assertEqual([w["version"] for w in windows["windows"]], [2])
        self.service.complete_maintenance("maint", "m1")

    def test_rejected_extension_rolls_back_to_previous_version(self) -> None:
        self.request("m1", "u1", "2026-09-25T00:00:00Z", "2026-09-26T00:00:00Z")
        self.assess("m1")
        self.service.approve_maintenance("grid", "m1", 1)
        self.service.extend_maintenance("maint", "m1", 1, "2026-09-27T00:00:00Z", "申请")
        # 让延长不可行：26 日峰段 u2 也已批准检修，u1 延长后只剩 u3 100MW。
        self.request("m2", "u2", "2026-09-26T00:00:00Z", "2026-09-27T00:00:00Z")
        self.assess("m2")
        self.service.approve_maintenance("grid", "m2", 1)
        extension = self.assess("m1", 2)
        self.assertFalse(extension["assessment"]["feasible"])
        self.service.reject_maintenance("grid", "m1", 2, "备用不足")
        detail = self.service.maintenance_request("audit", "m1")
        self.assertEqual(detail["request"]["current_version"], 1)
        self.assertEqual(detail["request"]["state"], "approved")
        self.assertEqual(detail["versions"][0]["state"], "approved")
        self.assertEqual(detail["versions"][1]["state"], "rejected")
        windows = self.service.effective_windows("north", "2026-09-25T00:00:00Z", "2026-09-28T00:00:00Z")
        m1_window = [w for w in windows["windows"] if w["request_id"] == "m1"]
        self.assertEqual([w["version"] for w in m1_window], [1])

    def test_cancellation_restores_unit_and_removes_window_without_overwrite(self) -> None:
        self.request("m1", "u1", "2026-09-25T00:00:00Z", "2026-09-26T00:00:00Z")
        self.assess("m1")
        self.service.approve_maintenance("grid", "m1", 1)
        self.clock.advance(hours=25)
        self.service.activate_maintenance("grid", "m1")
        self.service.cancel_maintenance("maint", "m1", 1, "故障消除提前恢复")
        self.assertTrue(self.assess("m1", 2)["assessment"]["feasible"])
        self.service.approve_maintenance("grid", "m1", 2)
        self.assertEqual(self.connection.execute("SELECT state FROM maintenance_units WHERE unit_id='u1'").fetchone()["state"], "available")
        detail = self.service.maintenance_request("audit", "m1")
        self.assertEqual(detail["request"]["state"], "cancelled")
        self.assertEqual([(v["version"], v["state"]) for v in detail["versions"]],
                         [(1, "superseded"), (2, "cancelled")])
        windows = self.service.effective_windows("north", "2026-09-25T00:00:00Z", "2026-09-28T00:00:00Z")
        self.assertEqual(windows["windows"], [])


class ReplayAndPersistenceTests(MaintenanceTestBase):
    def test_assessment_replays_from_locked_snapshot(self) -> None:
        self.request("m1", "u1", "2026-09-25T00:00:00Z", "2026-09-26T00:00:00Z")
        assessed = self.assess("m1")
        self.service.approve_maintenance("grid", "m1", 1)
        # 事后再登记新机组和时段，锁定版本的重放结果必须保持不变。
        self.service.register_unit("maint", {"unit_id": "u4", "name": "u4", "region": "north", "rated_capacity_mw": "500"})
        self.service.define_segments("grid", {"region": "north", "segments": [
            {"starts_at": "2026-10-01T00:00:00Z", "ends_at": "2026-10-02T00:00:00Z", "kind": "peak", "load_mw": "10", "reserve_threshold_mw": "5"},
        ]})
        replay = self.service.replay_assessment("audit", "m1", 1)
        self.assertTrue(replay["matches"])
        self.assertEqual(replay["recomputed_result_sha256"], assessed["result_sha256"])

    def test_state_and_audit_survive_process_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "dispatch.sqlite3"
            connection = connect(path)
            service = MaintenanceService(connection, FrozenClock(datetime(2026, 9, 24, 0, 0, tzinfo=timezone.utc)))
            for uid, role in (("plan", "planner"), ("grid", "grid"), ("maint", "maintenance"),
                              ("risk", "risk"), ("audit", "auditor"), ("dispatch", "dispatcher")):
                service.create_user(uid, uid, role)
            for unit in ("u1", "u2", "u3"):
                service.register_unit("maint", {"unit_id": unit, "name": unit, "region": "north", "rated_capacity_mw": "100"})
            service.define_segments("grid", {"region": "north", "segments": SEGMENTS})
            service.request_maintenance("maint", {"request_id": "m1", "unit_id": "u1",
                                                  "starts_at": "2026-09-25T00:00:00Z", "ends_at": "2026-09-26T00:00:00Z",
                                                  "reason": "例行", "kind": "scheduled"})
            service.assess_maintenance("risk", "m1", 1)
            service.approve_maintenance("grid", "m1", 1)
            connection.close()

            restarted = connect(path)
            service2 = MaintenanceService(restarted)
            detail = service2.maintenance_request("audit", "m1")
            self.assertEqual(detail["request"]["state"], "approved")
            replay = service2.replay_assessment("audit", "m1", 1)
            self.assertTrue(replay["matches"])
            self.assertTrue(service2.audit_chain("audit")["valid"])
            windows = service2.effective_windows("north", "2026-09-25T00:00:00Z", "2026-09-26T00:00:00Z")
            self.assertEqual(len(windows["windows"]), 1)
            restarted.close()


class PermissionAndApiTests(MaintenanceTestBase):
    def test_role_separation(self) -> None:
        with self.assertRaises(Forbidden):
            self.service.request_maintenance("plan", {"request_id": "x", "unit_id": "u1",
                                                      "starts_at": "2026-09-25T00:00:00Z", "ends_at": "2026-09-26T00:00:00Z", "reason": "x"})
        self.request("m1", "u1", "2026-09-25T00:00:00Z", "2026-09-26T00:00:00Z")
        with self.assertRaises(Forbidden):
            self.service.assess_maintenance("maint", "m1", 1)
        self.assess("m1")
        with self.assertRaises(Forbidden):
            self.service.approve_maintenance("risk", "m1", 1)

    def test_stale_expected_version_conflicts(self) -> None:
        self.request("m1", "u1", "2026-09-25T00:00:00Z", "2026-09-26T00:00:00Z")
        self.assess("m1")
        self.service.approve_maintenance("grid", "m1", 1)
        self.service.extend_maintenance("maint", "m1", 1, "2026-09-27T00:00:00Z", "延长")
        with self.assertRaises(Conflict):
            self.service.assess_maintenance("risk", "m1", 1)

    def test_api_end_to_end(self) -> None:
        app = JsonApplication(self.service)

        def call(method: str, target: str, actor: str, payload: dict | None = None):
            body = json.dumps(payload).encode() if payload is not None else b""
            return app.handle(method, target, {"X-Actor-Id": actor}, body)

        response = call("POST", "/maintenance/requests", "maint", {
            "request_id": "m1", "unit_id": "u1", "starts_at": "2026-09-25T00:00:00Z",
            "ends_at": "2026-09-26T00:00:00Z", "reason": "例行",
        })
        self.assertEqual(response.status, 201)
        response = call("POST", "/maintenance/requests/m1/assess", "risk", {"expected_version": 1})
        self.assertEqual(response.status, 200)
        self.assertTrue(response.body["assessment"]["feasible"])
        response = call("POST", "/maintenance/requests/m1/approve", "grid", {"expected_version": 1})
        self.assertEqual(response.status, 200)
        response = app.handle("GET", "/maintenance/windows?region=north&starts_at=2026-09-25T00:00:00Z&ends_at=2026-09-26T00:00:00Z",
                              {"X-Actor-Id": "grid"})
        self.assertEqual(response.status, 200)
        self.assertEqual(len(response.body["windows"]), 1)


if __name__ == "__main__":
    unittest.main()
