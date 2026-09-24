"""检修计划的备用能力切片、冲突检测与确定性重放。

本模块只包含纯函数：所有时间必须是 ``clock.utc_text`` 规范化后的 UTC 文本，
字符串比较与时间比较一致；金额和能力值使用 Decimal，保证同一输入永远得到同一输出。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Mapping, Sequence

from .planning import ZERO, decimal_text, quantize_volume


@dataclass(frozen=True, slots=True)
class UnitCapability:
    unit_id: str
    region: str
    capacity_mw: Decimal
    committed_mw: Decimal


@dataclass(frozen=True, slots=True)
class RequirementWindow:
    requirement_id: str
    region: str
    label: str
    starts_at: str
    ends_at: str
    min_reserve_mw: Decimal


@dataclass(frozen=True, slots=True)
class ApprovedWindow:
    request_id: str
    unit_id: str
    starts_at: str
    ends_at: str


def _overlaps(a_start: str, a_end: str, b_start: str, b_end: str) -> bool:
    """判断两个半开区间 [start, end) 是否重叠。"""
    return a_start < b_end and a_end > b_start


def _mw(value: Decimal) -> str:
    return decimal_text(quantize_volume(value))


def reserve_slices(
    *,
    units: Sequence[UnitCapability],
    requirements: Sequence[RequirementWindow],
    approved: Sequence[ApprovedWindow],
    window_start: str,
    window_end: str,
    extra_outages: frozenset[str] = frozenset(),
) -> list[dict[str, object]]:
    """按峰谷时段要求逐个计算区域备用能力切片。

    ``units`` 为区域内可用机组，``approved`` 为已锁定能力版本的检修窗口，
    ``extra_outages`` 为额外视为停机的机组（例如正在评估的候选机组）。
    """
    rows: list[dict[str, object]] = []
    ordered = sorted(requirements, key=lambda item: (item.starts_at, item.requirement_id))
    for requirement in ordered:
        start = max(window_start, requirement.starts_at)
        end = min(window_end, requirement.ends_at)
        if not start < end:
            continue
        out_unit_ids = set(extra_outages)
        for window in approved:
            if _overlaps(window.starts_at, window.ends_at, start, end):
                out_unit_ids.add(window.unit_id)
        total = sum((unit.capacity_mw for unit in units), ZERO)
        unavailable = sum((unit.capacity_mw for unit in units if unit.unit_id in out_unit_ids), ZERO)
        committed = sum((unit.committed_mw for unit in units if unit.unit_id not in out_unit_ids), ZERO)
        available = total - unavailable
        reserve = available - committed
        rows.append({
            "requirement_id": requirement.requirement_id,
            "label": requirement.label,
            "starts_at": start,
            "ends_at": end,
            "total_capacity_mw": _mw(total),
            "maintenance_units": sorted(out_unit_ids),
            "unavailable_mw": _mw(unavailable),
            "available_mw": _mw(available),
            "committed_mw": _mw(committed),
            "reserve_mw": _mw(reserve),
            "min_reserve_mw": _mw(requirement.min_reserve_mw),
            "margin_mw": _mw(reserve - requirement.min_reserve_mw),
            "within_threshold": reserve >= requirement.min_reserve_mw,
        })
    return rows


def assess_maintenance_window(
    *,
    request_id: str,
    version_no: int,
    candidate: UnitCapability,
    starts_at: str,
    ends_at: str,
    units: Sequence[UnitCapability],
    requirements: Sequence[RequirementWindow],
    approved: Sequence[ApprovedWindow],
) -> dict[str, object]:
    """评估一个检修版本窗口，返回时间切片、冲突列表和结论。"""
    slices = reserve_slices(
        units=units,
        requirements=requirements,
        approved=approved,
        window_start=starts_at,
        window_end=ends_at,
        extra_outages=frozenset({candidate.unit_id}),
    )
    conflicts: list[dict[str, object]] = []
    for window in sorted(approved, key=lambda item: (item.starts_at, item.request_id)):
        if window.unit_id != candidate.unit_id:
            continue
        if not _overlaps(window.starts_at, window.ends_at, starts_at, ends_at):
            continue
        conflicts.append({
            "requirement_id": None,
            "label": None,
            "starts_at": max(starts_at, window.starts_at),
            "ends_at": min(ends_at, window.ends_at),
            "conflicting_request_id": window.request_id,
            "reason": "同一机组已存在重叠的已批准检修",
        })
    for row in slices:
        if row["within_threshold"]:
            continue
        deficit = Decimal(str(row["min_reserve_mw"])) - Decimal(str(row["reserve_mw"]))
        conflicts.append({
            "requirement_id": row["requirement_id"],
            "label": row["label"],
            "starts_at": row["starts_at"],
            "ends_at": row["ends_at"],
            "reserve_mw": row["reserve_mw"],
            "min_reserve_mw": row["min_reserve_mw"],
            "deficit_mw": _mw(deficit),
            "reason": "峰段备用能力低于安全阈值",
        })
    return {
        "request_id": request_id,
        "version_no": version_no,
        "unit_id": candidate.unit_id,
        "region": candidate.region,
        "starts_at": starts_at,
        "ends_at": ends_at,
        "verdict": "pass" if not conflicts else "conflict",
        "slices": slices,
        "conflicts": conflicts,
        "approved_considered": sorted({window.request_id for window in approved}),
    }


def _unit_capability(raw: Mapping[str, object]) -> UnitCapability:
    return UnitCapability(
        unit_id=str(raw["unit_id"]),
        region=str(raw["region"]),
        capacity_mw=Decimal(str(raw["capacity_mw"])),
        committed_mw=Decimal(str(raw["committed_mw"])),
    )


def assessment_from_input(payload: Mapping[str, object]) -> dict[str, object]:
    """从评估输入快照重建结论；实时评估与离线重放共用同一代码路径。"""
    request = payload["request"]
    assert isinstance(request, Mapping)
    requirements = [
        RequirementWindow(
            requirement_id=str(item["requirement_id"]),
            region=str(item["region"]),
            label=str(item["label"]),
            starts_at=str(item["starts_at"]),
            ends_at=str(item["ends_at"]),
            min_reserve_mw=Decimal(str(item["min_reserve_mw"])),
        )
        for item in payload["requirements"]  # type: ignore[union-attr]
    ]
    approved = [
        ApprovedWindow(
            request_id=str(item["request_id"]),
            unit_id=str(item["unit_id"]),
            starts_at=str(item["starts_at"]),
            ends_at=str(item["ends_at"]),
        )
        for item in payload["approved"]  # type: ignore[union-attr]
    ]
    candidate = payload["candidate"]
    assert isinstance(candidate, Mapping)
    return assess_maintenance_window(
        request_id=str(request["request_id"]),
        version_no=int(request["version_no"]),  # type: ignore[arg-type]
        candidate=_unit_capability(candidate),
        starts_at=str(request["starts_at"]),
        ends_at=str(request["ends_at"]),
        units=[_unit_capability(item) for item in payload["units"]],  # type: ignore[union-attr]
        requirements=requirements,
        approved=approved,
    )
