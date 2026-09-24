"""电价、燃料库存、送出线路和提名的事务用例。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import timedelta
from decimal import Decimal
from typing import Any, Iterable, Mapping

from .clock import SystemClock, parse_utc, utc_text
from .errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from .maintenance import (
    ApprovedWindow,
    RequirementWindow,
    UnitCapability,
    assessment_from_input,
    reserve_slices,
)
from .models import (
    IndexQuote,
    Facility,
    InventoryLot,
    MaintenanceRequestInput,
    MaintenanceUnit,
    NominationRequest,
    ReserveRequirement,
    Route,
    SupplyScenario,
    decimal_value,
    required_text,
)
from .planning import (
    AllocationRequest,
    PricePoint,
    allocate_capacity,
    canonical_json,
    decimal_text,
    delivered_after_loss,
    digest,
    effective_capacity,
    latest_streak,
    moving_average,
    quantize_volume,
    scenario_projection,
    weighted_inventory_cost,
)
from .storage import initialize, transaction


ROLE_PERMISSIONS = {
    "planner": {"quote.write", "catalog.write", "scenario.write", "scenario.run",
                "unit.write", "maintenance.write"},
    "dispatcher": {"nomination.write", "allocation.run", "transfer.write", "inventory.write",
                   "commitment.write", "maintenance.operate"},
    "risk": {"outage.write", "scenario.approve", "report.read",
             "requirement.write", "maintenance.assess", "maintenance.approve", "maintenance.replay"},
    "auditor": {"report.read", "audit.read", "maintenance.replay"},
}


class SupplyService:
    def __init__(self, connection: sqlite3.Connection, clock=None) -> None:
        self.connection = connection
        self.clock = clock or SystemClock()
        initialize(connection)

    def _now(self) -> str:
        return utc_text(self.clock.now())

    def _user(self, user_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM supply_users WHERE user_id=?", (user_id,)
        ).fetchone()
        if row is None:
            raise NotFound("用户不存在")
        if not row["active"]:
            raise Forbidden("用户已停用")
        return row

    def _require(self, user_id: str, permission: str) -> sqlite3.Row:
        user = self._user(user_id)
        if permission not in ROLE_PERMISSIONS[user["role"]]:
            raise Forbidden(f"角色 {user['role']} 无权执行 {permission}")
        return user

    def _audit(
        self,
        entity_type: str,
        entity_id: str,
        event_type: str,
        actor_id: str,
        payload: Mapping[str, Any],
    ) -> None:
        previous = self.connection.execute(
            "SELECT event_hash FROM supply_audit_events ORDER BY event_id DESC LIMIT 1"
        ).fetchone()
        previous_hash = "0" * 64 if previous is None else previous["event_hash"]
        body = {
            "entity_type": entity_type,
            "entity_id": entity_id,
            "event_type": event_type,
            "actor_id": actor_id,
            "payload": payload,
            "created_at": self._now(),
            "previous_hash": previous_hash,
        }
        event_hash = hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()
        self.connection.execute(
            "INSERT INTO supply_audit_events(entity_type,entity_id,event_type,actor_id,payload_json,"
            "previous_hash,event_hash,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                entity_type,
                entity_id,
                event_type,
                actor_id,
                canonical_json(payload),
                previous_hash,
                event_hash,
                body["created_at"],
            ),
        )

    def create_user(self, user_id: str, display_name: str, role: str) -> dict[str, Any]:
        if role not in ROLE_PERMISSIONS:
            raise ValidationFailed("未知角色")
        if not user_id.strip() or not display_name.strip():
            raise ValidationFailed("用户编号和名称不能为空")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO supply_users(user_id,display_name,role,created_at) VALUES(?,?,?,?)",
                    (user_id.strip(), display_name.strip(), role, self._now()),
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("用户已经存在") from exc
        return {"user_id": user_id.strip(), "role": role}

    def record_quote(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "quote.write")
        quote = IndexQuote.from_dict(raw)
        previous = self.connection.execute(
            "SELECT quote_id,source_revision FROM market_index_quotes WHERE market_index=? AND trade_date=? "
            "ORDER BY quote_id DESC LIMIT 1",
            (quote.market_index, quote.trade_date),
        ).fetchone()
        if previous is not None and previous["source_revision"] == quote.source_revision:
            raise Conflict("同一来源修订已登记")
        try:
            with transaction(self.connection, immediate=True):
                cursor = self.connection.execute(
                    "INSERT INTO market_index_quotes(market_index,trade_date,close_cny,source_revision,observed_at,"
                    "supersedes_quote_id,recorded_by,recorded_at) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        quote.market_index,
                        quote.trade_date,
                        decimal_text(quote.close_cny),
                        quote.source_revision,
                        quote.observed_at,
                        None if previous is None else previous["quote_id"],
                        actor_id,
                        self._now(),
                    ),
                )
                quote_id = int(cursor.lastrowid)
                self._audit(
                    "quote",
                    str(quote_id),
                    "quote.recorded",
                    actor_id,
                    {"market_index": quote.market_index, "trade_date": quote.trade_date},
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("电价版本冲突") from exc
        return {"quote_id": quote_id, "market_index": quote.market_index, "trade_date": quote.trade_date}

    def price_summary(self, market_index: str, sessions: int = 20) -> dict[str, Any]:
        rows = self.connection.execute(
            "SELECT q.trade_date,q.close_cny FROM market_index_quotes q "
            "JOIN (SELECT trade_date,max(quote_id) quote_id FROM market_index_quotes "
            "WHERE market_index=? GROUP BY trade_date) latest ON latest.quote_id=q.quote_id "
            "ORDER BY q.trade_date DESC LIMIT ?",
            (market_index.upper(), sessions),
        ).fetchall()
        points = [PricePoint(row["trade_date"], Decimal(row["close_cny"])) for row in rows]
        if not points:
            raise NotFound("没有基准电价")
        streak = latest_streak(points)
        average = moving_average(points, min(5, len(points)))
        latest = max(points, key=lambda item: item.trade_date)
        return {
            "market_index": market_index.upper(),
            "latest": {"trade_date": latest.trade_date, "close_cny": decimal_text(latest.close)},
            "latest_streak": None if streak is None else streak.as_dict(),
            "moving_average": None if average is None else decimal_text(average),
            "observations": len(points),
        }

    def create_facility(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "catalog.write")
        facility = Facility.from_dict(raw)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO facilities(facility_id,name,kind,timezone,capacity_mwh,created_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (
                        facility.facility_id,
                        facility.name,
                        facility.kind,
                        facility.timezone,
                        decimal_text(facility.capacity_mwh),
                        self._now(),
                    ),
                )
                self._audit("facility", facility.facility_id, "facility.created", actor_id, raw)
        except sqlite3.IntegrityError as exc:
            raise Conflict("设施编号已经存在") from exc
        return dict(raw)

    def create_route(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "catalog.write")
        route = Route.from_dict(raw)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO routes(route_id,origin_id,destination_id,product,daily_capacity,"
                    "loss_basis_points,transit_hours,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        route.route_id,
                        route.origin_id,
                        route.destination_id,
                        route.product,
                        decimal_text(route.daily_capacity),
                        route.loss_basis_points,
                        route.transit_hours,
                        self._now(),
                    ),
                )
                self._audit("route", route.route_id, "route.created", actor_id, raw)
        except sqlite3.IntegrityError as exc:
            raise Conflict("送出线路编号冲突或设施不存在") from exc
        return self.route(route.route_id)

    def route(self, route_id: str) -> dict[str, Any]:
        row = self.connection.execute("SELECT * FROM routes WHERE route_id=?", (route_id,)).fetchone()
        if row is None:
            raise NotFound("送出线路不存在")
        return dict(row)

    def announce_outage(
        self,
        actor_id: str,
        route_id: str,
        starts_at: str,
        ends_at: str | None,
        capacity_percent: object,
        reason: str,
    ) -> dict[str, Any]:
        self._require(actor_id, "outage.write")
        self.route(route_id)
        try:
            start = parse_utc(starts_at, "starts_at")
            end = None if ends_at is None else parse_utc(ends_at, "ends_at")
        except ValueError as exc:
            raise ValidationFailed(str(exc)) from exc
        if end is not None and end <= start:
            raise ValidationFailed("ends_at 必须晚于 starts_at")
        percentage = Decimal(str(capacity_percent))
        if percentage < 0 or percentage > 100:
            raise ValidationFailed("capacity_percent 必须在 0 到 100 之间")
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "INSERT INTO route_outages(route_id,starts_at,ends_at,capacity_percent,reason,created_by,created_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (route_id, utc_text(start), None if end is None else utc_text(end), decimal_text(percentage), reason, actor_id, self._now()),
            )
            outage_id = int(cursor.lastrowid)
            self._audit("route", route_id, "outage.announced", actor_id, {"outage_id": outage_id})
        return {"outage_id": outage_id, "route_id": route_id, "state": "announced"}

    def add_inventory_lot(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "inventory.write")
        lot = InventoryLot.from_dict(raw)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO inventory_lots(lot_id,facility_id,product,grade,quantity_mwh,available_mwh,"
                    "unit_cost_cny,received_at,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        lot.lot_id,
                        lot.facility_id,
                        lot.product,
                        lot.grade,
                        decimal_text(lot.quantity_mwh),
                        decimal_text(lot.quantity_mwh),
                        decimal_text(lot.unit_cost_cny),
                        lot.received_at,
                        actor_id,
                        self._now(),
                    ),
                )
                self._audit("inventory_lot", lot.lot_id, "inventory.received", actor_id, raw)
        except sqlite3.IntegrityError as exc:
            raise Conflict("燃料批次冲突或设施不存在") from exc
        return self.inventory_lot(lot.lot_id)

    def inventory_lot(self, lot_id: str) -> dict[str, Any]:
        row = self.connection.execute("SELECT * FROM inventory_lots WHERE lot_id=?", (lot_id,)).fetchone()
        if row is None:
            raise NotFound("燃料批次不存在")
        return dict(row)

    def inventory_summary(self, facility_id: str, product: str) -> dict[str, Any]:
        rows = self.connection.execute(
            "SELECT * FROM inventory_lots WHERE facility_id=? AND product=? ORDER BY received_at,lot_id",
            (facility_id, product),
        ).fetchall()
        return {"facility_id": facility_id, "product": product, **weighted_inventory_cost(rows)}

    def submit_nomination(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "nomination.write")
        nomination = NominationRequest.from_dict(raw)
        request_digest = digest(raw)
        stored = self.connection.execute(
            "SELECT request_sha256,response_json FROM supply_idempotency WHERE scope='nomination' AND idempotency_key=?",
            (nomination.idempotency_key,),
        ).fetchone()
        if stored is not None:
            if stored["request_sha256"] != request_digest:
                raise Conflict("幂等键对应不同提名内容")
            return json.loads(stored["response_json"])
        route = self.route(nomination.route_id)
        if route["state"] != "active":
            raise InvalidState("送出线路当前不可提名")
        response = {
            "nomination_id": nomination.nomination_id,
            "route_id": nomination.route_id,
            "state": "submitted",
            "revision": 1,
        }
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO nominations(nomination_id,route_id,shipper_id,service_date,requested_mwh,"
                    "priority,idempotency_key,submitted_by,submitted_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        nomination.nomination_id,
                        nomination.route_id,
                        nomination.shipper_id,
                        nomination.service_date,
                        decimal_text(nomination.requested_mwh),
                        nomination.priority,
                        nomination.idempotency_key,
                        actor_id,
                        self._now(),
                    ),
                )
                self.connection.execute(
                    "INSERT INTO supply_idempotency(scope,idempotency_key,request_sha256,response_json,created_at) "
                    "VALUES('nomination',?,?,?,?)",
                    (nomination.idempotency_key, request_digest, canonical_json(response), self._now()),
                )
                self._audit("nomination", nomination.nomination_id, "nomination.submitted", actor_id, raw)
        except sqlite3.IntegrityError as exc:
            raise Conflict("提名编号或幂等键冲突") from exc
        return response

    def _capacity_for_date(self, route: sqlite3.Row, service_date: str) -> Decimal:
        start = service_date + "T00:00:00Z"
        end = service_date + "T23:59:59Z"
        rows = self.connection.execute(
            "SELECT capacity_percent FROM route_outages WHERE route_id=? AND state IN ('announced','active') "
            "AND starts_at<=? AND (ends_at IS NULL OR ends_at>=?) ORDER BY outage_id",
            (route["route_id"], end, start),
        ).fetchall()
        percentages = [Decimal(row["capacity_percent"]) for row in rows]
        return effective_capacity(Decimal(route["daily_capacity"]), percentages)

    def allocate(self, actor_id: str, route_id: str, service_date: str) -> dict[str, Any]:
        self._require(actor_id, "allocation.run")
        route = self.connection.execute("SELECT * FROM routes WHERE route_id=?", (route_id,)).fetchone()
        if route is None:
            raise NotFound("送出线路不存在")
        nominations = self.connection.execute(
            "SELECT * FROM nominations WHERE route_id=? AND service_date=? AND state='submitted' "
            "ORDER BY priority,submitted_at,nomination_id",
            (route_id, service_date),
        ).fetchall()
        if not nominations:
            raise InvalidState("没有待分配提名")
        requests = [
            AllocationRequest(
                row["nomination_id"],
                Decimal(row["requested_mwh"]),
                int(row["priority"]),
                row["submitted_at"],
            )
            for row in nominations
        ]
        available = self._capacity_for_date(route, service_date)
        input_value = [dict(row) for row in nominations]
        input_sha256 = digest({"route": dict(route), "nominations": input_value, "capacity": str(available)})
        result_rows = allocate_capacity(available, requests)
        result = {
            "route_id": route_id,
            "service_date": service_date,
            "available_capacity": decimal_text(available),
            "allocations": result_rows,
        }
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "INSERT INTO allocation_runs(route_id,service_date,input_sha256,available_capacity,result_json,"
                "created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                (route_id, service_date, input_sha256, decimal_text(available), canonical_json(result), actor_id, self._now()),
            )
            for item in result_rows:
                state = "allocated" if Decimal(item["allocated_mwh"]) > 0 else "cancelled"
                self.connection.execute(
                    "UPDATE nominations SET allocated_mwh=?,state=?,revision=revision+1 "
                    "WHERE nomination_id=? AND state='submitted'",
                    (item["allocated_mwh"], state, item["nomination_id"]),
                )
            allocation_id = int(cursor.lastrowid)
            self._audit("route", route_id, "allocation.completed", actor_id, {"allocation_id": allocation_id})
        return {"allocation_id": allocation_id, **result}

    def dispatch_transfer(
        self,
        actor_id: str,
        transfer_id: str,
        nomination_id: str,
        lot_id: str,
        expected_revision: int,
    ) -> dict[str, Any]:
        self._require(actor_id, "transfer.write")
        nomination = self.connection.execute(
            "SELECT n.*,r.loss_basis_points,r.transit_hours,r.origin_id FROM nominations n "
            "JOIN routes r ON r.route_id=n.route_id WHERE n.nomination_id=?",
            (nomination_id,),
        ).fetchone()
        if nomination is None:
            raise NotFound("提名不存在")
        if nomination["state"] != "allocated" or nomination["revision"] != expected_revision:
            raise InvalidState("提名不是当前可送电版本")
        lot = self.connection.execute("SELECT * FROM inventory_lots WHERE lot_id=?", (lot_id,)).fetchone()
        if lot is None:
            raise NotFound("燃料批次不存在")
        allocated = Decimal(nomination["allocated_mwh"])
        available = Decimal(lot["available_mwh"])
        if lot["facility_id"] != nomination["origin_id"] or lot["product"] != self.route(nomination["route_id"])["product"]:
            raise Conflict("燃料批次与送出线路起点或电源类型不匹配")
        if available < allocated:
            raise Conflict("燃料库存不足以完成分配")
        expected_delivery = delivered_after_loss(allocated, int(nomination["loss_basis_points"]))
        departed_at = self._now()
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "UPDATE inventory_lots SET available_mwh=?,revision=revision+1 WHERE lot_id=? AND revision=?",
                (decimal_text(quantize_volume(available - allocated)), lot_id, lot["revision"]),
            )
            self.connection.execute(
                "UPDATE nominations SET state='in_transit',revision=revision+1 WHERE nomination_id=? AND revision=?",
                (nomination_id, expected_revision),
            )
            self.connection.execute(
                "INSERT INTO transfers(transfer_id,nomination_id,inventory_lot_id,loaded_mwh,"
                "expected_delivered_mwh,departed_at,created_by,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (
                    transfer_id,
                    nomination_id,
                    lot_id,
                    decimal_text(allocated),
                    decimal_text(expected_delivery),
                    departed_at,
                    actor_id,
                    departed_at,
                ),
            )
            self._audit("transfer", transfer_id, "transfer.dispatched", actor_id, {"nomination_id": nomination_id})
        return {
            "transfer_id": transfer_id,
            "state": "in_transit",
            "loaded_mwh": decimal_text(allocated),
            "expected_delivered_mwh": decimal_text(expected_delivery),
            "expected_arrival": utc_text(parse_utc(departed_at) + timedelta(hours=int(nomination["transit_hours"]))),
        }

    def create_scenario(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "scenario.write")
        scenario = SupplyScenario.from_dict(raw)
        definition = canonical_json(raw)
        content_sha256 = hashlib.sha256(definition.encode("utf-8")).hexdigest()
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO supply_scenarios(scenario_id,name,definition_json,content_sha256,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (scenario.scenario_id, scenario.name, definition, content_sha256, actor_id, self._now()),
                )
                self._audit("scenario", scenario.scenario_id, "scenario.created", actor_id, {"sha256": content_sha256})
        except sqlite3.IntegrityError as exc:
            raise Conflict("情景编号或内容已经存在") from exc
        return {"scenario_id": scenario.scenario_id, "state": "draft", "sha256": content_sha256}

    def approve_scenario(self, actor_id: str, scenario_id: str, expected_revision: int) -> dict[str, Any]:
        self._require(actor_id, "scenario.approve")
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "UPDATE supply_scenarios SET state='approved',revision=revision+1 "
                "WHERE scenario_id=? AND state='draft' AND revision=?",
                (scenario_id, expected_revision),
            )
            if cursor.rowcount != 1:
                raise InvalidState("情景不是当前草稿版本")
            self._audit("scenario", scenario_id, "scenario.approved", actor_id, {})
        return {"scenario_id": scenario_id, "state": "approved", "revision": expected_revision + 1}

    def run_scenario(self, actor_id: str, scenario_id: str, as_of_date: str) -> dict[str, Any]:
        self._require(actor_id, "scenario.run")
        row = self.connection.execute(
            "SELECT * FROM supply_scenarios WHERE scenario_id=?", (scenario_id,)
        ).fetchone()
        if row is None:
            raise NotFound("情景不存在")
        if row["state"] != "approved":
            raise InvalidState("只有已批准情景可以运行")
        scenario = SupplyScenario.from_dict(json.loads(row["definition_json"]))
        price_row = self.connection.execute(
            "SELECT close_cny FROM market_index_quotes WHERE trade_date<=? ORDER BY trade_date DESC,quote_id DESC LIMIT 1",
            (as_of_date,),
        ).fetchone()
        if price_row is None:
            raise InvalidState("截止日期没有可用电价")
        routes = self.connection.execute("SELECT * FROM routes WHERE state='active' ORDER BY route_id").fetchall()
        inventory = self.connection.execute(
            "SELECT facility_id,product,sum(CAST(available_mwh AS REAL)) available_mwh "
            "FROM inventory_lots GROUP BY facility_id,product ORDER BY facility_id,product"
        ).fetchall()
        input_value = {
            "scenario_sha256": row["content_sha256"],
            "as_of_date": as_of_date,
            "price": price_row["close_cny"],
            "routes": [dict(item) for item in routes],
            "inventory": [dict(item) for item in inventory],
        }
        input_sha256 = digest(input_value)
        existing = self.connection.execute(
            "SELECT run_id,result_json FROM scenario_runs WHERE scenario_id=? AND as_of_date=? AND input_sha256=?",
            (scenario_id, as_of_date, input_sha256),
        ).fetchone()
        if existing is not None:
            return {"run_id": existing["run_id"], **json.loads(existing["result_json"]), "replayed": True}
        result = scenario_projection(
            current_price=Decimal(price_row["close_cny"]),
            market_index_drop_percent=scenario.market_index_drop_percent,
            routes=routes,
            inventory=inventory,
            route_capacity_changes=scenario.route_capacity_changes,
            demand_changes=scenario.demand_changes,
        )
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "INSERT INTO scenario_runs(scenario_id,as_of_date,input_sha256,result_json,created_by,created_at) "
                "VALUES(?,?,?,?,?,?)",
                (scenario_id, as_of_date, input_sha256, canonical_json(result), actor_id, self._now()),
            )
            run_id = int(cursor.lastrowid)
            self._audit("scenario", scenario_id, "scenario.executed", actor_id, {"run_id": run_id})
        return {"run_id": run_id, **result, "replayed": False}

    def register_unit(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "unit.write")
        unit = MaintenanceUnit.from_dict(raw)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO maintenance_units(unit_id,facility_id,region,capacity_mw,committed_mw,created_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (
                        unit.unit_id,
                        unit.facility_id,
                        unit.region,
                        decimal_text(unit.capacity_mw),
                        decimal_text(unit.committed_mw),
                        self._now(),
                    ),
                )
                self._audit("maintenance_unit", unit.unit_id, "unit.registered", actor_id, dict(raw))
        except sqlite3.IntegrityError as exc:
            raise Conflict("机组编号冲突或设施不存在") from exc
        return self.unit(unit.unit_id)

    def _unit_row(self, unit_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM maintenance_units WHERE unit_id=?", (unit_id,)
        ).fetchone()
        if row is None:
            raise NotFound("机组不存在")
        return row

    def unit(self, unit_id: str) -> dict[str, Any]:
        return dict(self._unit_row(unit_id))

    def update_unit_commitment(
        self,
        actor_id: str,
        unit_id: str,
        committed_mw: object,
        expected_revision: int,
    ) -> dict[str, Any]:
        self._require(actor_id, "commitment.write")
        unit = self._unit_row(unit_id)
        committed = decimal_value(committed_mw, "committed_mw", minimum=Decimal("0"))
        if committed > Decimal(unit["capacity_mw"]):
            raise ValidationFailed("committed_mw 不能超过 capacity_mw")
        locked = self.connection.execute(
            "SELECT r.request_id FROM maintenance_requests r "
            "JOIN maintenance_versions v ON v.request_id=r.request_id "
            "WHERE r.unit_id=? AND r.state IN ('submitted','assessed','approved','effective') "
            "AND v.capability_sha256 IS NOT NULL LIMIT 1",
            (unit_id,),
        ).fetchone()
        if locked is not None:
            raise Conflict("机组存在已锁定能力版本的检修计划，承诺出力不可修改")
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "UPDATE maintenance_units SET committed_mw=?,revision=revision+1 WHERE unit_id=? AND revision=?",
                (decimal_text(committed), unit_id, expected_revision),
            )
            if cursor.rowcount != 1:
                raise InvalidState("机组版本已变化")
            self._audit(
                "maintenance_unit",
                unit_id,
                "unit.commitment_updated",
                actor_id,
                {"previous_committed_mw": unit["committed_mw"], "committed_mw": decimal_text(committed)},
            )
        return self.unit(unit_id)

    def register_reserve_requirement(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "requirement.write")
        requirement = ReserveRequirement.from_dict(raw)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO reserve_requirements(requirement_id,region,label,starts_at,ends_at,"
                    "min_reserve_mw,created_by,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        requirement.requirement_id,
                        requirement.region,
                        requirement.label,
                        requirement.starts_at,
                        requirement.ends_at,
                        decimal_text(requirement.min_reserve_mw),
                        actor_id,
                        self._now(),
                    ),
                )
                self._audit("reserve_requirement", requirement.requirement_id, "requirement.registered", actor_id, dict(raw))
        except sqlite3.IntegrityError as exc:
            raise Conflict("峰谷时段编号已经存在") from exc
        return {
            "requirement_id": requirement.requirement_id,
            "region": requirement.region,
            "label": requirement.label,
            "starts_at": requirement.starts_at,
            "ends_at": requirement.ends_at,
            "min_reserve_mw": decimal_text(requirement.min_reserve_mw),
        }

    def submit_maintenance(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "maintenance.write")
        request = MaintenanceRequestInput.from_dict(raw)
        unit = self._unit_row(request.unit_id)
        if unit["state"] != "available":
            raise InvalidState("机组已退役")
        try:
            with transaction(self.connection, immediate=True):
                open_request = self.connection.execute(
                    "SELECT request_id FROM maintenance_requests WHERE unit_id=? "
                    "AND state IN ('submitted','assessed','approved','effective')",
                    (request.unit_id,),
                ).fetchone()
                if open_request is not None:
                    raise Conflict("机组已存在未关闭的检修申请")
                self.connection.execute(
                    "INSERT INTO maintenance_requests(request_id,unit_id,reason,created_by,created_at) "
                    "VALUES(?,?,?,?,?)",
                    (request.request_id, request.unit_id, request.reason, actor_id, self._now()),
                )
                self.connection.execute(
                    "INSERT INTO maintenance_versions(request_id,version_no,kind,starts_at,ends_at,note,"
                    "created_by,created_at) VALUES(?,1,'initial',?,?,?,?,?)",
                    (request.request_id, request.starts_at, request.ends_at, request.reason, actor_id, self._now()),
                )
                self._audit(
                    "maintenance",
                    request.request_id,
                    "maintenance.submitted",
                    actor_id,
                    {"unit_id": request.unit_id, "version_no": 1,
                     "starts_at": request.starts_at, "ends_at": request.ends_at},
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("检修申请编号已经存在") from exc
        return {"request_id": request.request_id, "state": "submitted", "current_version": 1}

    def _request_row(self, request_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM maintenance_requests WHERE request_id=?", (request_id,)
        ).fetchone()
        if row is None:
            raise NotFound("检修申请不存在")
        return row

    def _version_row(self, request_id: str, version_no: int) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM maintenance_versions WHERE request_id=? AND version_no=?",
            (request_id, version_no),
        ).fetchone()
        if row is None:
            raise NotFound("检修版本不存在")
        return row

    def _operative_locked_versions(self) -> list[sqlite3.Row]:
        """仍处于开放状态且已锁定能力版本的检修窗口（延长审批期间旧版本继续生效）。"""
        return self.connection.execute(
            "SELECT r.request_id,r.unit_id,u.region,v.starts_at,v.ends_at "
            "FROM maintenance_requests r "
            "JOIN maintenance_versions v ON v.request_id=r.request_id "
            "JOIN maintenance_units u ON u.unit_id=r.unit_id "
            "WHERE r.state IN ('submitted','assessed','approved','effective') "
            "AND v.capability_sha256 IS NOT NULL "
            "AND v.version_no=(SELECT MAX(v2.version_no) FROM maintenance_versions v2 "
            "                  WHERE v2.request_id=r.request_id AND v2.capability_sha256 IS NOT NULL) "
            "ORDER BY r.request_id"
        ).fetchall()

    def _assessment_input(self, request: sqlite3.Row, version: sqlite3.Row) -> dict[str, Any]:
        unit = self._unit_row(request["unit_id"])
        units = self.connection.execute(
            "SELECT unit_id,region,capacity_mw,committed_mw FROM maintenance_units "
            "WHERE region=? AND state='available' ORDER BY unit_id",
            (unit["region"],),
        ).fetchall()
        requirements = self.connection.execute(
            "SELECT requirement_id,region,label,starts_at,ends_at,min_reserve_mw FROM reserve_requirements "
            "WHERE region=? AND starts_at<? AND ends_at>? ORDER BY starts_at,requirement_id",
            (unit["region"], version["ends_at"], version["starts_at"]),
        ).fetchall()
        approved = [
            window
            for window in self._operative_locked_versions()
            if window["request_id"] != request["request_id"]
            and window["region"] == unit["region"]
            and window["starts_at"] < version["ends_at"]
            and window["ends_at"] > version["starts_at"]
        ]
        return {
            "request": {
                "request_id": request["request_id"],
                "version_no": version["version_no"],
                "unit_id": unit["unit_id"],
                "starts_at": version["starts_at"],
                "ends_at": version["ends_at"],
            },
            "candidate": {
                "unit_id": unit["unit_id"],
                "region": unit["region"],
                "capacity_mw": unit["capacity_mw"],
                "committed_mw": unit["committed_mw"],
            },
            "units": [dict(row) for row in units],
            "requirements": [dict(row) for row in requirements],
            "approved": [
                {"request_id": w["request_id"], "unit_id": w["unit_id"],
                 "starts_at": w["starts_at"], "ends_at": w["ends_at"]}
                for w in approved
            ],
        }

    def assess_maintenance(self, actor_id: str, request_id: str) -> dict[str, Any]:
        self._require(actor_id, "maintenance.assess")
        request = self._request_row(request_id)
        if request["state"] not in ("submitted", "assessed"):
            raise InvalidState("当前状态不可评估")
        version = self._version_row(request_id, request["current_version"])
        payload = self._assessment_input(request, version)
        input_sha256 = digest(payload)
        existing = self.connection.execute(
            "SELECT assessment_id,result_json FROM maintenance_assessments "
            "WHERE request_id=? AND version_no=? AND input_sha256=?",
            (request_id, version["version_no"], input_sha256),
        ).fetchone()
        if existing is not None:
            return {
                "assessment_id": existing["assessment_id"],
                "input_sha256": input_sha256,
                **json.loads(existing["result_json"]),
                "replayed": True,
            }
        result = assessment_from_input(payload)
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "INSERT INTO maintenance_assessments(request_id,version_no,input_sha256,input_json,verdict,"
                "result_json,created_by,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (
                    request_id,
                    version["version_no"],
                    input_sha256,
                    canonical_json(payload),
                    result["verdict"],
                    canonical_json(result),
                    actor_id,
                    self._now(),
                ),
            )
            assessment_id = int(cursor.lastrowid)
            self.connection.execute(
                "UPDATE maintenance_requests SET state='assessed' WHERE request_id=? AND state='submitted'",
                (request_id,),
            )
            self._audit(
                "maintenance",
                request_id,
                "maintenance.assessed",
                actor_id,
                {"version_no": version["version_no"], "assessment_id": assessment_id,
                 "verdict": result["verdict"], "input_sha256": input_sha256},
            )
        return {"assessment_id": assessment_id, "input_sha256": input_sha256, **result, "replayed": False}

    def approve_maintenance(self, actor_id: str, request_id: str, expected_version: int) -> dict[str, Any]:
        self._require(actor_id, "maintenance.approve")
        request = self._request_row(request_id)
        if request["state"] != "assessed":
            raise InvalidState("检修申请不在待批准状态")
        if int(request["current_version"]) != int(expected_version):
            raise InvalidState("检修版本已变化")
        version = self._version_row(request_id, request["current_version"])
        assessment = self.connection.execute(
            "SELECT * FROM maintenance_assessments WHERE request_id=? AND version_no=? "
            "ORDER BY assessment_id DESC LIMIT 1",
            (request_id, version["version_no"]),
        ).fetchone()
        if assessment is None:
            raise InvalidState("缺少风险评估")
        if assessment["verdict"] != "pass":
            raise InvalidState("风险评估存在未解决冲突")
        current_sha256 = digest(self._assessment_input(request, version))
        if current_sha256 != assessment["input_sha256"]:
            raise Conflict("能力版本已变化，需要重新评估")
        new_state = "effective" if version["prior_state"] == "effective" else "approved"
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "UPDATE maintenance_versions SET capability_sha256=?,approved_by=?,approved_at=? "
                "WHERE request_id=? AND version_no=? AND capability_sha256 IS NULL",
                (current_sha256, actor_id, self._now(), request_id, version["version_no"]),
            )
            if cursor.rowcount != 1:
                raise InvalidState("检修版本已锁定")
            self.connection.execute(
                "UPDATE maintenance_requests SET state=? WHERE request_id=?",
                (new_state, request_id),
            )
            self._audit(
                "maintenance",
                request_id,
                "maintenance.approved",
                actor_id,
                {"version_no": version["version_no"], "capability_sha256": current_sha256,
                 "assessment_id": assessment["assessment_id"]},
            )
        return {
            "request_id": request_id,
            "state": new_state,
            "current_version": version["version_no"],
            "capability_sha256": current_sha256,
        }

    def activate_maintenance(self, actor_id: str, request_id: str) -> dict[str, Any]:
        self._require(actor_id, "maintenance.operate")
        request = self._request_row(request_id)
        if request["state"] != "approved":
            raise InvalidState("检修申请未批准")
        version = self._version_row(request_id, request["current_version"])
        now = self._now()
        if now < version["starts_at"]:
            raise InvalidState("检修窗口尚未开始")
        if now >= version["ends_at"]:
            raise InvalidState("检修窗口已结束")
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "UPDATE maintenance_requests SET state='effective' WHERE request_id=? AND state='approved'",
                (request_id,),
            )
            if cursor.rowcount != 1:
                raise InvalidState("检修申请状态已变化")
            self._audit("maintenance", request_id, "maintenance.activated", actor_id,
                        {"version_no": version["version_no"]})
        return {"request_id": request_id, "state": "effective", "current_version": version["version_no"]}

    def extend_maintenance(
        self,
        actor_id: str,
        request_id: str,
        new_ends_at: object,
        note: object,
    ) -> dict[str, Any]:
        self._require(actor_id, "maintenance.write")
        request = self._request_row(request_id)
        if request["state"] not in ("submitted", "assessed", "approved", "effective"):
            raise InvalidState("检修已关闭，不能延长")
        current = self._version_row(request_id, request["current_version"])
        try:
            new_end = parse_utc(required_text(new_ends_at, "new_ends_at", 40), "new_ends_at")
        except ValueError as exc:
            raise ValidationFailed(str(exc)) from exc
        new_end_text = utc_text(new_end)
        if new_end_text <= current["ends_at"]:
            raise ValidationFailed("新的结束时间必须晚于当前版本")
        note_text = required_text(note, "note")
        new_version_no = int(request["current_version"]) + 1
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "INSERT INTO maintenance_versions(request_id,version_no,kind,starts_at,ends_at,note,"
                "prior_state,created_by,created_at) VALUES(?,?,'extension',?,?,?,?,?,?)",
                (request_id, new_version_no, current["starts_at"], new_end_text, note_text,
                 request["state"], actor_id, self._now()),
            )
            cursor = self.connection.execute(
                "UPDATE maintenance_requests SET state='submitted',current_version=? "
                "WHERE request_id=? AND current_version=?",
                (new_version_no, request_id, request["current_version"]),
            )
            if cursor.rowcount != 1:
                raise InvalidState("检修版本已变化")
            self._audit(
                "maintenance",
                request_id,
                "maintenance.extended",
                actor_id,
                {"version_no": new_version_no, "previous_version": request["current_version"],
                 "new_ends_at": new_end_text},
            )
        return {"request_id": request_id, "state": "submitted", "current_version": new_version_no}

    def cancel_maintenance(self, actor_id: str, request_id: str, note: object) -> dict[str, Any]:
        self._require(actor_id, "maintenance.write")
        request = self._request_row(request_id)
        if request["state"] in ("restored", "cancelled"):
            raise InvalidState("检修已关闭")
        current = self._version_row(request_id, request["current_version"])
        note_text = required_text(note, "note")
        now = self._now()
        was_effective = request["state"] == "effective" or current["prior_state"] == "effective"
        if was_effective:
            locked = self.connection.execute(
                "SELECT starts_at FROM maintenance_versions WHERE request_id=? AND capability_sha256 IS NOT NULL "
                "ORDER BY version_no DESC LIMIT 1",
                (request_id,),
            ).fetchone()
            starts_at = locked["starts_at"] if locked is not None else current["starts_at"]
            ends_at = now if now > starts_at else starts_at
            new_state = "restored"
        else:
            starts_at = current["starts_at"]
            ends_at = current["ends_at"]
            new_state = "cancelled"
        new_version_no = int(request["current_version"]) + 1
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "INSERT INTO maintenance_versions(request_id,version_no,kind,starts_at,ends_at,note,"
                "prior_state,created_by,created_at) VALUES(?,?,'cancellation',?,?,?,?,?,?)",
                (request_id, new_version_no, starts_at, ends_at, note_text,
                 request["state"], actor_id, self._now()),
            )
            self.connection.execute(
                "UPDATE maintenance_requests SET state=?,current_version=?,closed_at=? WHERE request_id=?",
                (new_state, new_version_no, now, request_id),
            )
            self._audit(
                "maintenance",
                request_id,
                "maintenance.cancelled",
                actor_id,
                {"version_no": new_version_no, "resulting_state": new_state},
            )
        return {"request_id": request_id, "state": new_state, "current_version": new_version_no}

    def restore_maintenance(self, actor_id: str, request_id: str) -> dict[str, Any]:
        self._require(actor_id, "maintenance.operate")
        request = self._request_row(request_id)
        if request["state"] != "effective":
            raise InvalidState("检修未生效")
        now = self._now()
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "UPDATE maintenance_requests SET state='restored',closed_at=? "
                "WHERE request_id=? AND state='effective'",
                (now, request_id),
            )
            if cursor.rowcount != 1:
                raise InvalidState("检修申请状态已变化")
            self._audit("maintenance", request_id, "maintenance.restored", actor_id,
                        {"version_no": request["current_version"]})
        return {"request_id": request_id, "state": "restored", "current_version": request["current_version"]}

    def replay_maintenance(self, actor_id: str, request_id: str, version_no: int) -> dict[str, Any]:
        self._require(actor_id, "maintenance.replay")
        self._request_row(request_id)
        self._version_row(request_id, int(version_no))
        rows = self.connection.execute(
            "SELECT * FROM maintenance_assessments WHERE request_id=? AND version_no=? ORDER BY assessment_id",
            (request_id, int(version_no)),
        ).fetchall()
        if not rows:
            raise NotFound("该版本没有风险评估")
        replays = []
        for row in rows:
            recomputed = assessment_from_input(json.loads(row["input_json"]))
            replays.append({
                "assessment_id": row["assessment_id"],
                "input_sha256": row["input_sha256"],
                "verdict": row["verdict"],
                "matches": canonical_json(recomputed) == row["result_json"],
            })
        return {
            "request_id": request_id,
            "version_no": int(version_no),
            "replays": replays,
            "all_matched": all(item["matches"] for item in replays),
        }

    def maintenance_request(self, request_id: str) -> dict[str, Any]:
        request = self._request_row(request_id)
        version = self._version_row(request_id, request["current_version"])
        assessment = self.connection.execute(
            "SELECT verdict FROM maintenance_assessments WHERE request_id=? AND version_no=? "
            "ORDER BY assessment_id DESC LIMIT 1",
            (request_id, version["version_no"]),
        ).fetchone()
        return {
            "request_id": request["request_id"],
            "unit_id": request["unit_id"],
            "reason": request["reason"],
            "state": request["state"],
            "current_version": request["current_version"],
            "starts_at": version["starts_at"],
            "ends_at": version["ends_at"],
            "capability_sha256": version["capability_sha256"],
            "latest_verdict": None if assessment is None else assessment["verdict"],
            "created_by": request["created_by"],
            "created_at": request["created_at"],
            "closed_at": request["closed_at"],
        }

    def maintenance_history(self, request_id: str) -> dict[str, Any]:
        request = self._request_row(request_id)
        versions = self.connection.execute(
            "SELECT * FROM maintenance_versions WHERE request_id=? ORDER BY version_no",
            (request_id,),
        ).fetchall()
        assessments = self.connection.execute(
            "SELECT assessment_id,version_no,verdict,input_sha256,created_by,created_at "
            "FROM maintenance_assessments WHERE request_id=? ORDER BY assessment_id",
            (request_id,),
        ).fetchall()
        by_version: dict[int, list[dict[str, Any]]] = {}
        for row in assessments:
            by_version.setdefault(row["version_no"], []).append({
                "assessment_id": row["assessment_id"],
                "verdict": row["verdict"],
                "input_sha256": row["input_sha256"],
                "created_by": row["created_by"],
                "created_at": row["created_at"],
            })
        events = self.connection.execute(
            "SELECT event_id,event_type,actor_id,payload_json,event_hash,created_at FROM supply_audit_events "
            "WHERE entity_type='maintenance' AND entity_id=? ORDER BY event_id",
            (request_id,),
        ).fetchall()
        return {
            "request": self.maintenance_request(request_id),
            "versions": [
                {
                    "version_no": row["version_no"],
                    "kind": row["kind"],
                    "starts_at": row["starts_at"],
                    "ends_at": row["ends_at"],
                    "note": row["note"],
                    "prior_state": row["prior_state"],
                    "capability_sha256": row["capability_sha256"],
                    "approved_by": row["approved_by"],
                    "approved_at": row["approved_at"],
                    "created_by": row["created_by"],
                    "created_at": row["created_at"],
                    "assessments": by_version.get(row["version_no"], []),
                }
                for row in versions
            ],
            "audit_trail": [
                {
                    "event_id": row["event_id"],
                    "event_type": row["event_type"],
                    "actor_id": row["actor_id"],
                    "payload": json.loads(row["payload_json"]),
                    "event_hash": row["event_hash"],
                    "created_at": row["created_at"],
                }
                for row in events
            ],
        }

    def regional_capability(self, region: object, starts_at: object, ends_at: object) -> dict[str, Any]:
        region_text = required_text(region, "region", 64)
        try:
            start = parse_utc(required_text(starts_at, "starts_at", 40), "starts_at")
            end = parse_utc(required_text(ends_at, "ends_at", 40), "ends_at")
        except ValueError as exc:
            raise ValidationFailed(str(exc)) from exc
        if end <= start:
            raise ValidationFailed("ends_at 必须晚于 starts_at")
        start_text = utc_text(start)
        end_text = utc_text(end)
        units = self.connection.execute(
            "SELECT unit_id,region,capacity_mw,committed_mw FROM maintenance_units "
            "WHERE region=? AND state='available' ORDER BY unit_id",
            (region_text,),
        ).fetchall()
        requirements = self.connection.execute(
            "SELECT requirement_id,region,label,starts_at,ends_at,min_reserve_mw FROM reserve_requirements "
            "WHERE region=? AND starts_at<? AND ends_at>? ORDER BY starts_at,requirement_id",
            (region_text, end_text, start_text),
        ).fetchall()
        approved = [
            window
            for window in self._operative_locked_versions()
            if window["region"] == region_text
            and window["starts_at"] < end_text
            and window["ends_at"] > start_text
        ]
        slices = reserve_slices(
            units=[
                UnitCapability(u["unit_id"], u["region"], Decimal(u["capacity_mw"]), Decimal(u["committed_mw"]))
                for u in units
            ],
            requirements=[
                RequirementWindow(r["requirement_id"], r["region"], r["label"], r["starts_at"], r["ends_at"],
                                  Decimal(r["min_reserve_mw"]))
                for r in requirements
            ],
            approved=[
                ApprovedWindow(w["request_id"], w["unit_id"], w["starts_at"], w["ends_at"])
                for w in approved
            ],
            window_start=start_text,
            window_end=end_text,
        )
        return {
            "region": region_text,
            "starts_at": start_text,
            "ends_at": end_text,
            "slices": slices,
            "approved_maintenance": [
                {"request_id": w["request_id"], "unit_id": w["unit_id"],
                 "starts_at": w["starts_at"], "ends_at": w["ends_at"]}
                for w in approved
            ],
        }

    def audit_chain(self, actor_id: str) -> dict[str, Any]:
        self._require(actor_id, "audit.read")
        rows = self.connection.execute("SELECT * FROM supply_audit_events ORDER BY event_id").fetchall()
        previous_hash = "0" * 64
        valid = True
        for row in rows:
            body = {
                "entity_type": row["entity_type"],
                "entity_id": row["entity_id"],
                "event_type": row["event_type"],
                "actor_id": row["actor_id"],
                "payload": json.loads(row["payload_json"]),
                "created_at": row["created_at"],
                "previous_hash": row["previous_hash"],
            }
            calculated = hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()
            if row["previous_hash"] != previous_hash or row["event_hash"] != calculated:
                valid = False
                break
            previous_hash = row["event_hash"]
        return {"valid": valid, "events": len(rows), "head_hash": previous_hash}
