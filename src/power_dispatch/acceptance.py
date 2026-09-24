"""贯通电价、送出线路、燃料库存、提名和情景分析的离线验收。"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .clock import FrozenClock
from .service import SupplyService


def run(workspace: Path) -> dict[str, object]:
    connection = sqlite3.connect(":memory:", isolation_level=None)
    connection.row_factory = sqlite3.Row
    clock = FrozenClock(datetime(2026, 9, 24, 8, 0, tzinfo=timezone.utc))
    service = SupplyService(connection, clock)
    for user_id, role in (("plan", "planner"), ("dispatch", "dispatcher"), ("risk", "risk"), ("audit", "auditor")):
        service.create_user(user_id, user_id, role)
    for index, close in enumerate(("108", "105", "102", "100", "98", "96"), start=18):
        service.record_quote("plan", {"market_index": "PEAK_VALLEY", "trade_date": f"2026-09-{index}", "close_cny": close, "source_revision": f"rev-{index}", "observed_at": f"2026-09-{index}T21:00:00Z"})
    service.create_facility("plan", {"facility_id": "field-a", "name": "北部电厂", "kind": "storage", "timezone": "Asia/Shanghai", "capacity_mwh": "500000"})
    service.create_facility("plan", {"facility_id": "terminal-b", "name": "沿海终端", "kind": "terminal", "timezone": "Asia/Shanghai", "capacity_mwh": "800000"})
    service.create_route("plan", {"route_id": "pipe-a-b", "origin_id": "field-a", "destination_id": "terminal-b", "product": "crude", "daily_capacity": "100000", "loss_basis_points": 25, "transit_hours": 36})
    service.add_inventory_lot("dispatch", {"lot_id": "lot-001", "facility_id": "field-a", "product": "crude", "grade": "PEAK_VALLEY", "quantity_mwh": "150000", "unit_cost_cny": "91.25", "received_at": "2026-09-24T06:00:00Z"})
    service.submit_nomination("dispatch", {"nomination_id": "nom-001", "route_id": "pipe-a-b", "shipper_id": "refinery-east", "service_date": "2026-09-25", "requested_mwh": "80000", "priority": 10, "idempotency_key": "nom-key-001"})
    allocation = service.allocate("dispatch", "pipe-a-b", "2026-09-25")
    transfer = service.dispatch_transfer("dispatch", "transfer-001", "nom-001", "lot-001", 2)
    service.create_scenario("plan", {"scenario_id": "pipeline-restart", "name": "关键机组检修恢复与需求回落", "market_index_drop_percent": "9", "route_capacity_changes": {"pipe-a-b": "20"}, "demand_changes": {"field-a:crude": "-5"}})
    service.approve_scenario("risk", "pipeline-restart", 1)
    scenario = service.run_scenario("plan", "pipeline-restart", "2026-09-23")
    service.register_unit("plan", {"unit_id": "unit-1", "facility_id": "field-a", "region": "north", "capacity_mw": "300", "committed_mw": "50"})
    service.register_unit("plan", {"unit_id": "unit-2", "facility_id": "field-a", "region": "north", "capacity_mw": "300", "committed_mw": "50"})
    service.register_reserve_requirement("risk", {"requirement_id": "peak-1001", "region": "north", "label": "peak", "starts_at": "2026-10-01T08:00:00Z", "ends_at": "2026-10-01T12:00:00Z", "min_reserve_mw": "200"})
    service.register_reserve_requirement("risk", {"requirement_id": "peak-1002", "region": "north", "label": "peak", "starts_at": "2026-10-02T08:00:00Z", "ends_at": "2026-10-02T12:00:00Z", "min_reserve_mw": "200"})
    service.submit_maintenance("plan", {"request_id": "mnt-001", "unit_id": "unit-1", "starts_at": "2026-10-01T08:00:00Z", "ends_at": "2026-10-01T12:00:00Z", "reason": "机组定检"})
    service.submit_maintenance("plan", {"request_id": "mnt-002", "unit_id": "unit-2", "starts_at": "2026-10-02T08:00:00Z", "ends_at": "2026-10-02T12:00:00Z", "reason": "错峰定检"})
    first_assessment = service.assess_maintenance("risk", "mnt-001")
    service.approve_maintenance("risk", "mnt-001", 1)
    second_assessment = service.assess_maintenance("risk", "mnt-002")
    service.approve_maintenance("risk", "mnt-002", 1)
    clock.advance(days=7, hours=1)
    service.activate_maintenance("dispatch", "mnt-001")
    service.restore_maintenance("dispatch", "mnt-001")
    service.extend_maintenance("plan", "mnt-002", "2026-10-02T14:00:00Z", "发现缺陷需要延长两小时")
    extended_assessment = service.assess_maintenance("risk", "mnt-002")
    service.approve_maintenance("risk", "mnt-002", 2)
    replay = service.replay_maintenance("audit", "mnt-002", 2)
    capability = service.regional_capability("north", "2026-10-02T00:00:00Z", "2026-10-03T00:00:00Z")
    maintenance = {
        "first_verdict": first_assessment["verdict"],
        "second_verdict": second_assessment["verdict"],
        "extended_verdict": extended_assessment["verdict"],
        "mnt_002_state": service.maintenance_request("mnt-002")["state"],
        "replay_all_matched": replay["all_matched"],
        "capability_slices": len(capability["slices"]),
    }
    result = {"status": "ok", "price": service.price_summary("PEAK_VALLEY"), "allocation_id": allocation["allocation_id"], "transfer": transfer, "scenario_run_id": scenario["run_id"], "maintenance": maintenance, "audit": service.audit_chain("audit"), "workspace": workspace.name}
    connection.close()
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="运行电厂调度服务离线验收")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    print(json.dumps(run(args.workspace), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
