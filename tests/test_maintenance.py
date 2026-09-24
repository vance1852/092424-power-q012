from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from power_dispatch.api import JsonApplication
from power_dispatch.clock import FrozenClock
from power_dispatch.errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from power_dispatch.service import SupplyService
from power_dispatch.storage import connect


class MaintenanceFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.clock = FrozenClock(datetime(2026, 9, 24, 8, 0, tzinfo=timezone.utc))
        self.service = SupplyService(self.connection, self.clock)
        for user_id, role in (("plan", "planner"), ("dispatch", "dispatcher"), ("risk", "risk"), ("audit", "auditor")):
            self.service.create_user(user_id, user_id, role)
        self.service.create_facility("plan", {"facility_id": "plant-a", "name": "北部电厂", "kind": "storage", "timezone": "Asia/Shanghai", "capacity_mwh": "500000"})
        self.service.register_unit("plan", {"unit_id": "unit-1", "facility_id": "plant-a", "region": "north", "capacity_mw": "300", "committed_mw": "50"})
        self.service.register_unit("plan", {"unit_id": "unit-2", "facility_id": "plant-a", "region": "north", "capacity_mw": "300", "committed_mw": "50"})
        self.service.register_reserve_requirement("risk", {"requirement_id": "peak-am", "region": "north", "label": "peak", "starts_at": "2026-10-01T08:00:00Z", "ends_at": "2026-10-01T12:00:00Z", "min_reserve_mw": "200"})
        self.service.register_reserve_requirement("risk", {"requirement_id": "peak-pm", "region": "north", "label": "peak", "starts_at": "2026-10-01T13:00:00Z", "ends_at": "2026-10-01T17:00:00Z", "min_reserve_mw": "200"})

    def tearDown(self) -> None:
        self.connection.close()

    def submit(self, request_id: str = "mnt-1", unit_id: str = "unit-1",
               starts: str = "2026-10-01T08:00:00Z", ends: str = "2026-10-01T12:00:00Z") -> dict[str, object]:
        return self.service.submit_maintenance("plan", {"request_id": request_id, "unit_id": unit_id, "starts_at": starts, "ends_at": ends, "reason": "定检"})

    def approve_window(self, request_id: str = "mnt-1", unit_id: str = "unit-1",
                       starts: str = "2026-10-01T08:00:00Z", ends: str = "2026-10-01T12:00:00Z") -> dict[str, object]:
        self.submit(request_id, unit_id, starts, ends)
        assessment = self.service.assess_maintenance("risk", request_id)
        self.assertEqual(assessment["verdict"], "pass")
        return self.service.approve_maintenance("risk", request_id, 1)

    def test_full_lifecycle_locks_capability_and_audits_every_step(self) -> None:
        self.submit()
        first = self.service.assess_maintenance("risk", "mnt-1")
        self.assertEqual(first["verdict"], "pass")
        self.assertFalse(first["replayed"])
        self.assertEqual(len(first["slices"]), 1)
        slice_row = first["slices"][0]
        self.assertEqual(slice_row["requirement_id"], "peak-am")
        self.assertEqual(slice_row["reserve_mw"], "250.000")
        self.assertEqual(slice_row["maintenance_units"], ["unit-1"])
        second = self.service.assess_maintenance("risk", "mnt-1")
        self.assertTrue(second["replayed"])
        self.assertEqual(second["assessment_id"], first["assessment_id"])
        approved = self.service.approve_maintenance("risk", "mnt-1", 1)
        self.assertEqual(approved["state"], "approved")
        self.assertEqual(len(approved["capability_sha256"]), 64)
        self.clock.advance(days=7, hours=1)
        activated = self.service.activate_maintenance("dispatch", "mnt-1")
        self.assertEqual(activated["state"], "effective")
        restored = self.service.restore_maintenance("dispatch", "mnt-1")
        self.assertEqual(restored["state"], "restored")
        request = self.service.maintenance_request("mnt-1")
        self.assertEqual(request["state"], "restored")
        self.assertIsNotNone(request["closed_at"])
        history = self.service.maintenance_history("mnt-1")
        self.assertEqual(len(history["versions"]), 1)
        version = history["versions"][0]
        self.assertEqual(version["kind"], "initial")
        self.assertEqual(version["capability_sha256"], approved["capability_sha256"])
        self.assertEqual(version["approved_by"], "risk")
        self.assertEqual(len(version["assessments"]), 1)
        event_types = [event["event_type"] for event in history["audit_trail"]]
        self.assertEqual(event_types, [
            "maintenance.submitted",
            "maintenance.assessed",
            "maintenance.approved",
            "maintenance.activated",
            "maintenance.restored",
        ])
        self.assertTrue(self.service.audit_chain("audit")["valid"])

    def test_overlapping_outage_returns_affected_slices_and_reasons(self) -> None:
        self.approve_window("mnt-1", "unit-1", "2026-10-01T08:00:00Z", "2026-10-01T17:00:00Z")
        self.submit("mnt-2", "unit-2", "2026-10-01T09:00:00Z", "2026-10-01T15:00:00Z")
        assessment = self.service.assess_maintenance("risk", "mnt-2")
        self.assertEqual(assessment["verdict"], "conflict")
        self.assertEqual(assessment["approved_considered"], ["mnt-1"])
        self.assertEqual(len(assessment["conflicts"]), 2)
        morning, afternoon = assessment["conflicts"]
        self.assertEqual(morning["requirement_id"], "peak-am")
        self.assertEqual(morning["starts_at"], "2026-10-01T09:00:00Z")
        self.assertEqual(morning["ends_at"], "2026-10-01T12:00:00Z")
        self.assertEqual(morning["reserve_mw"], "0.000")
        self.assertEqual(morning["deficit_mw"], "200.000")
        self.assertIn("备用能力低于安全阈值", morning["reason"])
        self.assertEqual(afternoon["requirement_id"], "peak-pm")
        self.assertEqual(afternoon["starts_at"], "2026-10-01T13:00:00Z")
        self.assertEqual(afternoon["ends_at"], "2026-10-01T15:00:00Z")
        with self.assertRaises(InvalidState):
            self.service.approve_maintenance("risk", "mnt-2", 1)
        self.assertEqual(self.service.maintenance_request("mnt-2")["latest_verdict"], "conflict")

    def test_staggered_outages_pass_and_coexist(self) -> None:
        self.approve_window("mnt-1", "unit-1", "2026-10-01T08:00:00Z", "2026-10-01T12:00:00Z")
        approved = self.approve_window("mnt-2", "unit-2", "2026-10-01T13:00:00Z", "2026-10-01T17:00:00Z")
        self.assertEqual(approved["state"], "approved")
        capability = self.service.regional_capability("north", "2026-10-01T00:00:00Z", "2026-10-02T00:00:00Z")
        self.assertEqual(len(capability["slices"]), 2)
        self.assertTrue(all(row["within_threshold"] for row in capability["slices"]))
        self.assertEqual({item["request_id"] for item in capability["approved_maintenance"]}, {"mnt-1", "mnt-2"})

    def test_approval_fails_when_capability_version_changed_since_assessment(self) -> None:
        self.submit()
        first = self.service.assess_maintenance("risk", "mnt-1")
        self.service.register_reserve_requirement("risk", {"requirement_id": "peak-mid", "region": "north", "label": "peak", "starts_at": "2026-10-01T10:00:00Z", "ends_at": "2026-10-01T11:00:00Z", "min_reserve_mw": "200"})
        with self.assertRaises(Conflict):
            self.service.approve_maintenance("risk", "mnt-1", 1)
        second = self.service.assess_maintenance("risk", "mnt-1")
        self.assertFalse(second["replayed"])
        self.assertNotEqual(first["input_sha256"], second["input_sha256"])
        approved = self.service.approve_maintenance("risk", "mnt-1", 1)
        self.assertEqual(approved["capability_sha256"], second["input_sha256"])

    def test_commitment_is_locked_while_capability_version_open(self) -> None:
        self.approve_window()
        with self.assertRaises(Conflict):
            self.service.update_unit_commitment("dispatch", "unit-1", "80", 1)
        self.service.cancel_maintenance("plan", "mnt-1", "取消检修")
        updated = self.service.update_unit_commitment("dispatch", "unit-1", "80", 1)
        self.assertEqual(updated["committed_mw"], "80")
        self.assertEqual(updated["revision"], 2)
        with self.assertRaises(InvalidState):
            self.service.update_unit_commitment("dispatch", "unit-1", "90", 1)

    def test_extension_creates_successor_version_and_keeps_locked_window(self) -> None:
        approved = self.approve_window()
        self.clock.advance(days=7, hours=1)
        self.service.activate_maintenance("dispatch", "mnt-1")
        extended = self.service.extend_maintenance("plan", "mnt-1", "2026-10-01T14:00:00Z", "发现缺陷需要延长")
        self.assertEqual(extended["state"], "submitted")
        self.assertEqual(extended["current_version"], 2)
        locked_v1 = self.connection.execute(
            "SELECT ends_at,capability_sha256 FROM maintenance_versions WHERE request_id='mnt-1' AND version_no=1"
        ).fetchone()
        self.assertEqual(locked_v1["ends_at"], "2026-10-01T12:00:00Z")
        self.assertEqual(locked_v1["capability_sha256"], approved["capability_sha256"])
        capability = self.service.regional_capability("north", "2026-10-01T00:00:00Z", "2026-10-02T00:00:00Z")
        self.assertEqual(capability["slices"][0]["maintenance_units"], ["unit-1"])
        assessment = self.service.assess_maintenance("risk", "mnt-1")
        self.assertEqual(assessment["verdict"], "pass")
        self.assertEqual(assessment["version_no"], 2)
        self.assertEqual([row["requirement_id"] for row in assessment["slices"]], ["peak-am", "peak-pm"])
        reapproved = self.service.approve_maintenance("risk", "mnt-1", 2)
        self.assertEqual(reapproved["state"], "effective")
        self.assertNotEqual(reapproved["capability_sha256"], approved["capability_sha256"])
        self.assertTrue(self.service.replay_maintenance("audit", "mnt-1", 1)["all_matched"])
        self.assertTrue(self.service.replay_maintenance("audit", "mnt-1", 2)["all_matched"])
        history = self.service.maintenance_history("mnt-1")
        self.assertEqual([row["kind"] for row in history["versions"]], ["initial", "extension"])
        self.assertEqual(history["versions"][1]["prior_state"], "effective")

    def test_cancel_only_appends_successor_version(self) -> None:
        self.approve_window()
        cancelled = self.service.cancel_maintenance("plan", "mnt-1", "计划取消")
        self.assertEqual(cancelled["state"], "cancelled")
        self.assertEqual(cancelled["current_version"], 2)
        history = self.service.maintenance_history("mnt-1")
        self.assertEqual([row["kind"] for row in history["versions"]], ["initial", "cancellation"])
        self.assertEqual(history["versions"][1]["ends_at"], "2026-10-01T12:00:00Z")
        capability = self.service.regional_capability("north", "2026-10-01T00:00:00Z", "2026-10-02T00:00:00Z")
        self.assertEqual(capability["slices"][0]["maintenance_units"], [])
        self.approve_window("mnt-2", "unit-2")
        self.clock.advance(days=7, hours=1)
        self.service.activate_maintenance("dispatch", "mnt-2")
        early = self.service.cancel_maintenance("plan", "mnt-2", "缺陷消除提前恢复")
        self.assertEqual(early["state"], "restored")
        versions = self.service.maintenance_history("mnt-2")["versions"]
        self.assertEqual(versions[-1]["kind"], "cancellation")
        self.assertEqual(versions[-1]["ends_at"], "2026-10-01T09:00:00Z")
        self.assertEqual(versions[0]["ends_at"], "2026-10-01T12:00:00Z")

    def test_replay_reproduces_assessment_and_detects_tampering(self) -> None:
        self.submit()
        self.service.assess_maintenance("risk", "mnt-1")
        replay = self.service.replay_maintenance("audit", "mnt-1", 1)
        self.assertTrue(replay["all_matched"])
        self.assertEqual(len(replay["replays"]), 1)
        self.connection.execute("UPDATE maintenance_assessments SET result_json='{}' WHERE request_id='mnt-1'")
        self.assertFalse(self.service.replay_maintenance("audit", "mnt-1", 1)["all_matched"])

    def test_restart_restores_decisions_and_audit_basis(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "maintenance.sqlite3"
            clock = FrozenClock(datetime(2026, 9, 24, 8, 0, tzinfo=timezone.utc))
            first = SupplyService(connect(path), clock)
            for user_id, role in (("plan", "planner"), ("risk", "risk"), ("audit", "auditor")):
                first.create_user(user_id, user_id, role)
            first.create_facility("plan", {"facility_id": "plant-a", "name": "北部电厂", "kind": "storage", "timezone": "Asia/Shanghai", "capacity_mwh": "500000"})
            first.register_unit("plan", {"unit_id": "unit-1", "facility_id": "plant-a", "region": "north", "capacity_mw": "300", "committed_mw": "50"})
            first.register_unit("plan", {"unit_id": "unit-2", "facility_id": "plant-a", "region": "north", "capacity_mw": "300", "committed_mw": "50"})
            first.register_reserve_requirement("risk", {"requirement_id": "peak-am", "region": "north", "label": "peak", "starts_at": "2026-10-01T08:00:00Z", "ends_at": "2026-10-01T12:00:00Z", "min_reserve_mw": "200"})
            first.submit_maintenance("plan", {"request_id": "mnt-1", "unit_id": "unit-1", "starts_at": "2026-10-01T08:00:00Z", "ends_at": "2026-10-01T12:00:00Z", "reason": "定检"})
            first.assess_maintenance("risk", "mnt-1")
            approved = first.approve_maintenance("risk", "mnt-1", 1)
            first.connection.close()
            second = SupplyService(connect(path), clock)
            request = second.maintenance_request("mnt-1")
            self.assertEqual(request["state"], "approved")
            self.assertEqual(request["capability_sha256"], approved["capability_sha256"])
            self.assertTrue(second.replay_maintenance("audit", "mnt-1", 1)["all_matched"])
            history = second.maintenance_history("mnt-1")
            self.assertEqual(len(history["versions"]), 1)
            self.assertEqual(len(history["audit_trail"]), 3)
            self.assertTrue(second.audit_chain("audit")["valid"])
            second.connection.close()

    def test_permissions_are_enforced(self) -> None:
        self.submit()
        with self.assertRaises(Forbidden):
            self.service.assess_maintenance("dispatch", "mnt-1")
        self.service.assess_maintenance("risk", "mnt-1")
        with self.assertRaises(Forbidden):
            self.service.approve_maintenance("plan", "mnt-1", 1)
        with self.assertRaises(Forbidden):
            self.service.submit_maintenance("risk", {"request_id": "mnt-2", "unit_id": "unit-2", "starts_at": "2026-10-01T08:00:00Z", "ends_at": "2026-10-01T12:00:00Z", "reason": "越权"})
        with self.assertRaises(Forbidden):
            self.service.register_unit("dispatch", {"unit_id": "unit-3", "facility_id": "plant-a", "region": "north", "capacity_mw": "100", "committed_mw": "0"})
        with self.assertRaises(Forbidden):
            self.service.register_reserve_requirement("plan", {"requirement_id": "peak-x", "region": "north", "label": "peak", "starts_at": "2026-10-01T08:00:00Z", "ends_at": "2026-10-01T09:00:00Z", "min_reserve_mw": "100"})
        with self.assertRaises(Forbidden):
            self.service.activate_maintenance("plan", "mnt-1")
        with self.assertRaises(Forbidden):
            self.service.replay_maintenance("plan", "mnt-1", 1)

    def test_duplicate_open_request_and_closed_unit_guards(self) -> None:
        self.submit()
        with self.assertRaises(Conflict):
            self.submit("mnt-2", "unit-1")
        self.service.cancel_maintenance("plan", "mnt-1", "取消")
        self.submit("mnt-2", "unit-1")
        with self.assertRaises(NotFound):
            self.service.maintenance_request("missing")
        with self.assertRaises(NotFound):
            self.service.assess_maintenance("risk", "missing")

    def test_validation_and_state_boundaries(self) -> None:
        with self.assertRaises(ValidationFailed):
            self.submit("mnt-bad", "unit-1", "2026-10-01T12:00:00Z", "2026-10-01T08:00:00Z")
        with self.assertRaises(ValidationFailed):
            self.service.register_reserve_requirement("risk", {"requirement_id": "bad", "region": "north", "label": "shoulder", "starts_at": "2026-10-01T08:00:00Z", "ends_at": "2026-10-01T09:00:00Z", "min_reserve_mw": "100"})
        with self.assertRaises(ValidationFailed):
            self.service.register_unit("plan", {"unit_id": "unit-bad", "facility_id": "plant-a", "region": "north", "capacity_mw": "100", "committed_mw": "200"})
        self.submit()
        with self.assertRaises(ValidationFailed):
            self.service.extend_maintenance("plan", "mnt-1", "2026-10-01T10:00:00Z", "提前结束不算延长")
        self.service.assess_maintenance("risk", "mnt-1")
        self.service.approve_maintenance("risk", "mnt-1", 1)
        with self.assertRaises(InvalidState):
            self.service.assess_maintenance("risk", "mnt-1")
        with self.assertRaises(InvalidState):
            self.service.activate_maintenance("dispatch", "mnt-1")
        with self.assertRaises(InvalidState):
            self.service.restore_maintenance("dispatch", "mnt-1")
        with self.assertRaises(Conflict):
            self.service.register_reserve_requirement("risk", {"requirement_id": "peak-am", "region": "north", "label": "peak", "starts_at": "2026-10-01T08:00:00Z", "ends_at": "2026-10-01T12:00:00Z", "min_reserve_mw": "200"})


class MaintenanceApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.clock = FrozenClock(datetime(2026, 9, 24, 8, 0, tzinfo=timezone.utc))
        self.service = SupplyService(self.connection, self.clock)
        for user_id, role in (("plan", "planner"), ("dispatch", "dispatcher"), ("risk", "risk"), ("audit", "auditor")):
            self.service.create_user(user_id, user_id, role)
        self.service.create_facility("plan", {"facility_id": "plant-a", "name": "北部电厂", "kind": "storage", "timezone": "Asia/Shanghai", "capacity_mwh": "500000"})
        self.app = JsonApplication(self.service)

    def tearDown(self) -> None:
        self.connection.close()

    def post(self, path: str, actor: str, payload: dict[str, object]) -> object:
        return self.app.handle("POST", path, {"X-Actor-Id": actor}, json.dumps(payload).encode("utf-8"))

    def test_maintenance_endpoints(self) -> None:
        planner = {"X-Actor-Id": "plan"}
        risk = {"X-Actor-Id": "risk"}
        response = self.post("/maintenance/units", "plan", {"unit_id": "unit-1", "facility_id": "plant-a", "region": "north", "capacity_mw": "300", "committed_mw": "50"})
        self.assertEqual(response.status, 201)
        response = self.post("/maintenance/units", "plan", {"unit_id": "unit-2", "facility_id": "plant-a", "region": "north", "capacity_mw": "300", "committed_mw": "50"})
        self.assertEqual(response.status, 201)
        response = self.post("/maintenance/requirements", "risk", {"requirement_id": "peak-am", "region": "north", "label": "peak", "starts_at": "2026-10-01T08:00:00Z", "ends_at": "2026-10-01T12:00:00Z", "min_reserve_mw": "200"})
        self.assertEqual(response.status, 201)
        response = self.post("/maintenance/requests", "plan", {"request_id": "mnt-1", "unit_id": "unit-1", "starts_at": "2026-10-01T08:00:00Z", "ends_at": "2026-10-01T12:00:00Z", "reason": "定检"})
        self.assertEqual(response.status, 201)
        response = self.app.handle("POST", "/maintenance/requests/mnt-1/assess", risk)
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body["verdict"], "pass")
        response = self.post("/maintenance/requests/mnt-1/approve", "risk", {"expected_version": 1})
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body["state"], "approved")
        response = self.app.handle("GET", "/maintenance/requests/mnt-1", planner)
        self.assertEqual(response.body["state"], "approved")
        response = self.app.handle("GET", "/maintenance/requests/mnt-1/history", planner)
        self.assertEqual(len(response.body["versions"]), 1)
        self.assertEqual(len(response.body["audit_trail"]), 3)
        response = self.app.handle("GET", "/maintenance/capability?region=north&starts_at=2026-10-01T00:00:00Z&ends_at=2026-10-02T00:00:00Z", risk)
        self.assertEqual(response.status, 200)
        self.assertEqual(len(response.body["slices"]), 1)
        self.assertEqual(response.body["slices"][0]["maintenance_units"], ["unit-1"])
        response = self.post("/maintenance/requests/mnt-1/extend", "plan", {"new_ends_at": "2026-10-01T14:00:00Z"})
        self.assertEqual(response.status, 422)
        response = self.post("/maintenance/requests/mnt-1/replay", "audit", {"version_no": 1})
        self.assertEqual(response.status, 200)
        self.assertTrue(response.body["all_matched"])
        response = self.app.handle("POST", "/maintenance/requests", body=json.dumps({"request_id": "mnt-9", "unit_id": "unit-1", "starts_at": "2026-10-01T08:00:00Z", "ends_at": "2026-10-01T12:00:00Z", "reason": "缺操作者"}).encode("utf-8"))
        self.assertEqual(response.status, 422)
        response = self.app.handle("GET", "/maintenance/requests/missing", planner)
        self.assertEqual(response.status, 404)


if __name__ == "__main__":
    unittest.main()
