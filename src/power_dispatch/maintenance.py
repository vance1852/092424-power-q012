"""检修风险评估的确定性纯函数。

输入和输出都是可 JSON 序列化的字典（时间为带时区的 ISO 8601 文本），
同一份能力输入永远得到同一份结果。批准时锁定的能力版本可以在进程重启、
计划重放时重新计算并核对哈希。

联动要素：
- 机组可用性：候选检修机组在整个窗口内停机，其他已批准/生效/已完成的检修
  版本按各自有效窗口占用机组；
- 区域约束：只统计候选机组所在区域的机组与峰谷时段；
- 峰谷时段：窗口被切成与峰谷时段对齐的时间片；
- 已批准计划：机组承诺出力按时间片汇总，剩余机组必须同时覆盖负荷、
  承诺出力和峰段备用安全阈值。
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Mapping, Sequence

from .planning import decimal_text, quantize_volume

ZERO = Decimal("0")
KIND_SEVERITY = {"peak": 3, "flat": 2, "valley": 1, "unclassified": 0}


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _dec(value: object) -> Decimal:
    return quantize_volume(Decimal(str(value)))


def _clip_segments(
    segments: Sequence[Mapping[str, Any]],
    window_start: datetime,
    window_end: datetime,
) -> list[dict[str, Any]]:
    """把区域峰谷时段裁剪到检修窗口，合并重叠并补齐未分类空档。"""
    clipped: list[tuple[datetime, datetime, Mapping[str, Any]]] = []
    for row in sorted(segments, key=lambda item: (item["starts_at"], item["ends_at"])):
        start = max(_parse(str(row["starts_at"])), window_start)
        end = min(_parse(str(row["ends_at"])), window_end)
        if start < end:
            clipped.append((start, end, row))
    merged: list[dict[str, Any]] = []
    for start, end, row in clipped:
        if merged and start < merged[-1]["_end"]:
            current = merged[-1]
            current["_end"] = max(current["_end"], end)
            current["load_mw"] = max(current["load_mw"], _dec(row["load_mw"]))
            current["reserve_threshold_mw"] = max(
                current["reserve_threshold_mw"], _dec(row["reserve_threshold_mw"])
            )
            if KIND_SEVERITY[str(row["kind"])] > KIND_SEVERITY[current["kind"]]:
                current["kind"] = str(row["kind"])
        else:
            merged.append(
                {
                    "_start": start,
                    "_end": end,
                    "kind": str(row["kind"]),
                    "load_mw": _dec(row["load_mw"]),
                    "reserve_threshold_mw": _dec(row["reserve_threshold_mw"]),
                }
            )
    slices: list[dict[str, Any]] = []
    cursor = window_start
    for item in merged:
        if cursor < item["_start"]:
            slices.append(
                {
                    "_start": cursor,
                    "_end": item["_start"],
                    "kind": "unclassified",
                    "load_mw": ZERO,
                    "reserve_threshold_mw": ZERO,
                }
            )
        slices.append(item)
        cursor = item["_end"]
    if cursor < window_end:
        slices.append(
            {
                "_start": cursor,
                "_end": window_end,
                "kind": "unclassified",
                "load_mw": ZERO,
                "reserve_threshold_mw": ZERO,
            }
        )
    return slices


def _overlaps(start: datetime, end: datetime, other_start: datetime, other_end: datetime) -> bool:
    return start < other_end and end > other_start


def assess_capability(capability_input: Mapping[str, Any]) -> dict[str, Any]:
    """根据锁定的能力输入计算逐时间片备用与冲突。

    返回的每个冲突都带 ``code``、``message`` 和关联编号，调用方据此把
    受影响的时间片和原因返回给省调。
    """
    candidate = capability_input["candidate"]
    window_start = _parse(str(candidate["starts_at"]))
    window_end = _parse(str(candidate["ends_at"]))
    if window_end <= window_start:
        raise ValueError("检修窗口必须晚于开始时间")

    units = {
        str(row["unit_id"]): {
            "region": str(row["region"]),
            "rated_capacity_mw": _dec(row["rated_capacity_mw"]),
            "state": str(row.get("state", "available")),
        }
        for row in capability_input["units"]
    }
    candidate_unit = str(candidate["unit_id"])
    if candidate_unit not in units:
        raise ValueError("候选机组不存在")
    region = units[candidate_unit]["region"]
    is_cancellation = str(candidate.get("change_type")) == "cancellation"

    region_segments = [
        row for row in capability_input["segments"] if str(row["region"]) == region
    ]
    time_slices = _clip_segments(region_segments, window_start, window_end)

    region_units = sorted(
        (
            (unit_id, item["rated_capacity_mw"])
            for unit_id, item in units.items()
            if item["region"] == region and item["state"] != "retired"
        ),
        key=lambda item: item[0],
    )
    windows = [
        (
            str(row["request_id"]),
            int(row["version"]),
            str(row["unit_id"]),
            _parse(str(row["starts_at"])),
            _parse(str(row["ends_at"])),
        )
        for row in capability_input.get("maintenance_windows", [])
    ]
    commitments = [
        (
            str(row["unit_id"]),
            _parse(str(row["starts_at"])),
            _parse(str(row["ends_at"])),
            _dec(row["committed_mw"]),
            str(row.get("plan_ref", "")),
        )
        for row in capability_input.get("commitments", [])
    ]

    predecessor = candidate.get("predecessor_window")
    predecessor_start = _parse(str(predecessor["starts_at"])) if predecessor else None
    predecessor_end = _parse(str(predecessor["ends_at"])) if predecessor else None

    result_slices: list[dict[str, Any]] = []
    min_reserve: Decimal | None = None
    conflict_count = 0
    for item in time_slices:
        slice_start, slice_end = item["_start"], item["_end"]
        load = item["load_mw"]
        threshold = item["reserve_threshold_mw"]

        unavailable: list[dict[str, str]] = []
        if not is_cancellation:
            unavailable.append(
                {
                    "unit_id": candidate_unit,
                    "rated_capacity_mw": decimal_text(units[candidate_unit]["rated_capacity_mw"]),
                    "due_to": "candidate",
                }
            )
        blocking_windows: list[str] = []
        for request_id, version, unit_id, other_start, other_end in windows:
            if not _overlaps(slice_start, slice_end, other_start, other_end):
                continue
            if unit_id not in units or units[unit_id]["region"] != region:
                continue
            if unit_id == candidate_unit:
                blocking_windows.append(f"{request_id}/v{version}")
            unavailable.append(
                {
                    "unit_id": unit_id,
                    "rated_capacity_mw": decimal_text(units[unit_id]["rated_capacity_mw"]),
                    "due_to": f"maintenance:{request_id}/v{version}",
                }
            )
        unavailable_ids = {entry["unit_id"] for entry in unavailable}
        available = [
            {"unit_id": unit_id, "rated_capacity_mw": decimal_text(rated)}
            for unit_id, rated in region_units
            if unit_id not in unavailable_ids
        ]
        available_capacity = sum(
            (rated for unit_id, rated in region_units if unit_id not in unavailable_ids),
            ZERO,
        )
        # 只有停机机组（候选 + 窗口内机组）的承诺出力需要由剩余机组顶替；
        # 正常机组的承诺由它们自己兑现，不占用顶替能力。
        committed_rows = [
            {
                "unit_id": unit_id,
                "plan_ref": plan_ref,
                "committed_mw": decimal_text(mw),
            }
            for unit_id, c_start, c_end, mw, plan_ref in commitments
            if unit_id in unavailable_ids
            and _overlaps(slice_start, slice_end, c_start, c_end)
        ]
        committed_total = sum(
            (Decimal(str(row["committed_mw"])) for row in committed_rows), ZERO
        )
        reserve = quantize_volume(available_capacity - load)
        if min_reserve is None or reserve < min_reserve:
            min_reserve = reserve

        conflicts: list[dict[str, Any]] = []
        for blocker in blocking_windows:
            conflicts.append(
                {
                    "code": "unit_already_maintained",
                    "message": f"机组在时间片内已被 {blocker} 批准检修占用",
                    "related": blocker,
                }
            )
        # 初始检修窗口内，候选机组自己已承诺的出力会因停机而无法兑现，
        # 这属于悄悄改掉承诺出力，直接判冲突。延长版本的新增时间片由
        # 下方 extension_changes_committed_output 专门检查，不在此重复。
        if not is_cancellation and predecessor_start is None:
            own_commitments_now = [
                (plan_ref, mw)
                for unit_id, c_start, c_end, mw, plan_ref in commitments
                if unit_id == candidate_unit
                and mw > ZERO
                and _overlaps(slice_start, slice_end, c_start, c_end)
            ]
            if own_commitments_now:
                conflicts.append(
                    {
                        "code": "committed_output_unmet",
                        "message": "候选机组在时间片内存在已承诺出力，停机将改掉已经承诺的出力",
                        "commitments": [
                            {"plan_ref": plan_ref, "committed_mw": decimal_text(mw)}
                            for plan_ref, mw in own_commitments_now
                        ],
                    }
                )
        # 总量平衡：剩余机组能力必须同时覆盖区域负荷、停机机组需要顶替的
        # 承诺出力和峰段备用安全阈值。
        required = load + committed_total + threshold
        if available_capacity < required:
            shortfall = quantize_volume(required - available_capacity)
            conflicts.append(
                {
                    "code": "reserve_below_threshold",
                    "message": (
                        f"{item['kind']}时间片剩余能力 {decimal_text(available_capacity)}MW 低于"
                        f"负荷 {decimal_text(load)}MW、顶替承诺 {decimal_text(committed_total)}MW "
                        f"与备用安全阈值 {decimal_text(threshold)}MW 之和"
                    ),
                    "required_mw": decimal_text(required),
                    "shortfall_mw": decimal_text(shortfall),
                }
            )
        # 延长只能在旧版本窗口之外增加停机时间；新纳入的时间片如果候选机组
        # 自己已有承诺出力，延长就会改掉已经承诺的出力，按硬性冲突返回。
        if predecessor_start is not None and predecessor_end is not None:
            added_ranges = [
                (slice_start, min(slice_end, predecessor_start)),
                (max(slice_start, predecessor_end), slice_end),
            ]
            own_commitments = []
            for unit_id, c_start, c_end, mw, plan_ref in commitments:
                if unit_id != candidate_unit or mw <= ZERO:
                    continue
                if any(
                    range_start < range_end and _overlaps(range_start, range_end, c_start, c_end)
                    for range_start, range_end in added_ranges
                ):
                    own_commitments.append((plan_ref, mw))
            if own_commitments:
                conflicts.append(
                    {
                        "code": "extension_changes_committed_output",
                        "message": "延长部分覆盖机组已有承诺出力的时间片，承诺出力不得被悄悄修改",
                        "commitments": [
                            {"plan_ref": plan_ref, "committed_mw": decimal_text(mw)}
                            for plan_ref, mw in own_commitments
                        ],
                    }
                )

        conflict_count += len(conflicts)
        result_slices.append(
            {
                "starts_at": _iso(slice_start),
                "ends_at": _iso(slice_end),
                "region": region,
                "kind": item["kind"],
                "load_mw": decimal_text(load),
                "reserve_threshold_mw": decimal_text(threshold),
                "available_units": available,
                "unavailable_units": unavailable,
                "available_capacity_mw": decimal_text(available_capacity),
                "committed_mw": decimal_text(committed_total),
                "reserve_mw": decimal_text(reserve),
                "conflicts": conflicts,
            }
        )

    return {
        "request_id": str(candidate["request_id"]),
        "version": int(candidate["version"]),
        "unit_id": candidate_unit,
        "region": region,
        "kind": str(candidate.get("kind", "scheduled")),
        "window": {
            "starts_at": _iso(window_start),
            "ends_at": _iso(window_end),
        },
        "feasible": conflict_count == 0,
        "slice_count": len(result_slices),
        "conflict_count": conflict_count,
        "min_reserve_mw": None if min_reserve is None else decimal_text(min_reserve),
        "slices": result_slices,
    }
