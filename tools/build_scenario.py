#!/usr/bin/env python3
"""生成区域协调场景的三个资料版本与候选方案清单。

数据是合成的，仅用于方法核对，不代表任何现实机构。每个版本按
docs/data-contract.md 的 schema 2 写 JSONL 与清单摘要；旧版本不覆盖。

场景要点：
* 西岸与东岸之间只有北侧一座桥，河湾村到西岸服务点没有直路；
* 集镇中心与河湾村服务点共用同一组工作人员（同一个能力池）；
* 西岸居民可跨河到集镇办理许可事项（跨行政边界）；
* 王姓老人家同时有卡与照护两项独立事项，人口只计一次；
* 劳动年龄居民只在周六办事，基线网点周末不开放。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data" / "coordination"

NODES = [
    {"id": "w1", "name": "西岸上村"},
    {"id": "w2", "name": "西岸下村"},
    {"id": "bw", "name": "西桥头"},
    {"id": "be", "name": "东桥头"},
    {"id": "e1", "name": "集镇中心"},
    {"id": "e2", "name": "河湾村"},
    {"id": "h1", "name": "北坡村"},
    {"id": "m1", "name": "山坳村"},
    {"id": "mb", "name": "山坳口"},
]

BASE_ROADS = [
    # 西岸内部
    {"id": "r_w1_w2", "source": "w1", "target": "w2", "mode": "walk",
     "minutes": 12, "one_way": False, "name": "西岸村道"},
    {"id": "r_w1_bw", "source": "w1", "target": "bw", "mode": "walk",
     "minutes": 26, "one_way": False, "name": "西岸到桥头"},
    # 跨河唯一通道
    {"id": "r_bw_be", "source": "bw", "target": "be", "mode": "walk",
     "minutes": 14, "one_way": False, "name": "北桥（绕行）"},
    {"id": "r_be_e1", "source": "be", "target": "e1", "mode": "walk",
     "minutes": 10, "one_way": False, "name": "桥头到集镇"},
    {"id": "r_e1_e2", "source": "e1", "target": "e2", "mode": "walk",
     "minutes": 16, "one_way": False, "name": "集镇到河湾"},
    {"id": "r_e2_h1", "source": "e2", "target": "h1", "mode": "walk",
     "minutes": 30, "one_way": False, "name": "河湾到北坡"},
    # 公交在岸两边分别运行，不直接跨河
    {"id": "b_e1_e2", "source": "e1", "target": "e2", "mode": "bus",
     "minutes": 7, "one_way": False, "name": "集镇—河湾班线"},
    {"id": "b_e2_h1", "source": "e2", "target": "h1", "mode": "bus",
     "minutes": 10, "one_way": False, "name": "河湾—北坡班线"},
    # 山坳：只有一条上山步道接集镇，班车到山坳口后仍需步行
    {"id": "r_e1_mb", "source": "e1", "target": "mb", "mode": "walk",
     "minutes": 34, "one_way": False, "name": "集镇到山坳口"},
    {"id": "r_mb_m1", "source": "mb", "target": "m1", "mode": "walk",
     "minutes": 12, "one_way": False, "name": "上山步道"},
    {"id": "b_e1_mb", "source": "e1", "target": "mb", "mode": "bus",
     "minutes": 22, "one_way": False, "name": "集镇—山坳口班线"},
]

BUS_LINES = [
    {"id": "L_e1_e2", "road": "b_e1_e2", "headway_minutes": 30,
     "first_departure": "06:30", "last_departure": "18:30",
     "days": ["mon", "tue", "wed", "thu", "fri", "sat"],
     "wait_rule": "timetable", "name": "集镇—河湾"},
    {"id": "L_e2_h1", "road": "b_e2_h1", "headway_minutes": 60,
     "first_departure": "07:00", "last_departure": "17:00",
     "days": ["mon", "tue", "wed", "thu", "fri"],
     "wait_rule": "timetable", "name": "河湾—北坡"},
    {"id": "L_e1_mb", "road": "b_e1_mb", "headway_minutes": 120,
     "first_departure": "08:00", "last_departure": "14:00",
     "days": ["mon", "tue", "wed", "thu", "fri"],
     "wait_rule": "timetable", "name": "集镇—山坳口（每日4班）"},
]

WEEKDAY = [
    {"day": "mon", "start": "08:30", "end": "12:00"},
    {"day": "mon", "start": "13:30", "end": "17:00"},
    {"day": "tue", "start": "08:30", "end": "12:00"},
    {"day": "tue", "start": "13:30", "end": "17:00"},
    {"day": "wed", "start": "08:30", "end": "12:00"},
    {"day": "wed", "start": "13:30", "end": "17:00"},
    {"day": "thu", "start": "08:30", "end": "12:00"},
    {"day": "thu", "start": "13:30", "end": "17:00"},
    {"day": "fri", "start": "08:30", "end": "12:00"},
    {"day": "fri", "start": "13:30", "end": "17:00"},
]
TOWN_HOURS = [
    {"day": d, "start": "08:30", "end": "17:00"}
    for d in ("mon", "tue", "wed", "thu", "fri")
]
CARE_HOURS = [
    {"day": d, "start": "09:00", "end": "16:00"}
    for d in ("mon", "tue", "wed", "thu", "fri")
]
EAST_TUE_THU = [
    {"day": "tue", "start": "09:00", "end": "12:00"},
    {"day": "thu", "start": "09:00", "end": "12:00"},
]
NORTH_WED = [{"day": "wed", "start": "09:00", "end": "12:00"}]

BASE_SITES = [
    {"id": "s_west", "node": "w2", "area": "西岸", "name": "西岸服务站（只办卡）",
     "services": ["card"], "pool": "p_west",
     "service_rates": {"card": 6}, "open_hours": WEEKDAY},
    {"id": "s_east", "node": "e2", "area": "东岸", "name": "河湾服务点",
     "services": ["card"], "pool": "p_town_a",
     "service_rates": {"card": 5}, "open_hours": EAST_TUE_THU},
    {"id": "s_town_card", "node": "e1", "area": "东岸", "name": "集镇综合窗口",
     "services": ["card", "permit"], "pool": "p_town_a",
     "service_rates": {"card": 5, "permit": 10}, "open_hours": TOWN_HOURS},
    {"id": "s_town_care", "node": "e1", "area": "东岸", "name": "集镇照护窗口",
     "services": ["care"], "pool": "p_town_b",
     "service_rates": {"care": 15}, "open_hours": CARE_HOURS},
    {"id": "s_north", "node": "h1", "area": "北坡", "name": "北坡代办点",
     "services": ["care"], "pool": "p_north",
     "service_rates": {"care": 20}, "open_hours": NORTH_WED},
    # 山坳代办点与集镇窗口共用同一组人员；山路耗时长，单件耗时更高
    {"id": "s_mountain", "node": "m1", "area": "山坳", "name": "山坳代办点（共用集镇人员）",
     "services": ["card"], "pool": "p_town_a",
     "service_rates": {"card": 20}, "open_hours": EAST_TUE_THU},
]

BASE_POOLS = [
    {"id": "p_west", "weekly_minutes": 70, "name": "西岸服务站人员"},
    {"id": "p_town_a", "weekly_minutes": 20, "name": "集镇综合窗口、河湾点与山坳代办点共用人员"},
    {"id": "p_town_b", "weekly_minutes": 36, "name": "集镇照护窗口人员"},
    {"id": "p_north", "weekly_minutes": 20, "name": "北坡代办员"},
]

# (resident, person, group, area, node, people, age, [demands])
RESIDENTS = [
    ("r_w1_wang", "p_wang", "老年", "西岸", "w1", 2, "70-79",
     [("card", 2, False, (), None, None),
      ("care", 1, True, ("mon", "tue", "wed", "thu", "fri"), "09:00", "15:00")]),
    ("r_w1_li", "p_li", "劳动年龄", "西岸", "w1", 1, "40-49",
     [("card", 1, False, ("sat",), "09:00", "12:00"),
      ("permit", 1, True, ("sat",), "09:00", "12:00")]),
    ("r_w2_zhao", "p_zhao", "老年", "西岸", "w2", 1, "80+",
     [("care", 1, True, ("mon", "tue", "wed", "thu", "fri"), "09:00", "15:00")]),
    ("r_w2_chen", "p_chen", "劳动年龄", "西岸", "w2", 1, "30-39",
     [("card", 1, False, ("sat",), "09:00", "12:00")]),
    ("r_e1_sun", "p_sun", "老年", "东岸", "e1", 1, "70-79",
     [("card", 1, False, ("mon", "tue", "wed", "thu", "fri"), "09:00", "12:00"),
      ("care", 1, True, ("mon", "tue", "wed", "thu", "fri"), "09:00", "15:00")]),
    ("r_e1_zhou", "p_zhou", "劳动年龄", "东岸", "e1", 1, "40-49",
     [("card", 1, False, ("sat",), "09:00", "12:00"),
      ("permit", 1, True, ("sat",), "09:00", "12:00")]),
    ("r_e2_wu", "p_wu", "老年", "东岸", "e2", 1, "80+",
     [("card", 1, False, ("tue", "thu"), "09:00", "12:00")]),
    ("r_e2_zheng", "p_zheng", "劳动年龄", "东岸", "e2", 1, "30-39",
     [("permit", 1, True, ("sat",), "09:00", "12:00")]),
    ("r_h1_feng", "p_feng", "老年", "北坡", "h1", 1, "70-79",
     [("card", 1, False, ("tue", "thu"), "09:00", "12:00"),
      ("care", 1, True, ("wed",), "09:00", "12:00")]),
    ("r_h1_qian", "p_qian", "劳动年龄", "北坡", "h1", 1, "40-49",
     [("card", 1, False, ("sat",), "09:00", "12:00")]),
    ("r_m1_ma", "p_ma", "老年", "山坳", "m1", 1, "70-79",
     [("card", 1, False, ("tue", "thu"), "09:00", "12:00")]),
    ("r_m1_mo", "p_mo", "老年", "山坳", "m1", 1, "80+",
     [("card", 1, False, ("tue", "thu"), "09:00", "12:00")]),
]

STANDARDS = [
    {"id": "st_card_all", "service": "card", "group": "*",
     "max_travel_minutes": 60, "modes": ["walk", "bus"]},
    {"id": "st_card_old", "service": "card", "group": "老年",
     "max_travel_minutes": 45, "modes": ["walk", "bus"],
     "mode_limits": {"walk": 45, "bus": 60},
     "note": "老人步行门槛更紧，公交按候车后的总时间给60分钟"},
    {"id": "st_care_all", "service": "care", "group": "*",
     "max_travel_minutes": 45, "modes": ["walk", "bus"],
     "mode_limits": {"walk": 45, "bus": 45}},
    {"id": "st_permit_all", "service": "permit", "group": "*",
     "max_travel_minutes": 90, "modes": ["walk", "bus"]},
]


def _demand_rows():
    rows = []
    for index, (rid, person, group, area, node, people, age, demands) in enumerate(RESIDENTS, 1):
        for offset, (service, units, atomic, days, earliest, latest) in enumerate(demands, 1):
            rows.append({
                "id": f"d_{rid}_{service}",
                "resident": rid,
                "service": service,
                "units": units,
                "atomic": atomic,
                "days": list(days),
                "earliest": earliest,
                "latest": latest,
                "note": ("整单办理" if atomic else "按人逐件办理"),
            })
    return rows


def _resident_rows():
    return [
        {"id": rid, "person": person, "group": group, "area": area, "node": node,
         "people": people, "age": age}
        for rid, person, group, area, node, people, age, _ in RESIDENTS
    ]


def _tables(version: int):
    roads = [dict(r) for r in BASE_ROADS]
    pools = [dict(p) for p in BASE_POOLS]
    if version >= 2:
        roads.append({
            "id": "r_w2_e2", "source": "w2", "target": "e2", "mode": "walk",
            "minutes": 6, "one_way": False, "name": "河湾人行桥",
        })
    if version >= 3:
        for pool in pools:
            if pool["id"] == "p_west":
                pool["weekly_minutes"] = 84
                pool["name"] = "西岸服务站人员（增员）"
            if pool["id"] == "p_town_a":
                pool["weekly_minutes"] = 40
                pool["name"] = "集镇综合窗口、河湾点与山坳代办点共用人员（增员）"
            if pool["id"] == "p_town_b":
                pool["weekly_minutes"] = 54
                pool["name"] = "集镇照护窗口人员（增员）"
    return {
        "nodes": [{"id": n["id"]} for n in NODES],
        "residents": _resident_rows(),
        "demands": _demand_rows(),
        "sites": [dict(s) for s in BASE_SITES],
        "pools": pools,
        "roads": roads,
        "bus_lines": BUS_LINES,
        "standards": STANDARDS,
    }


DEFAULTS = {
    "days": [], "earliest": None, "latest": None, "note": "", "group": "*",
    "person": None, "age": "", "name": "", "area": "", "active": True,
    "open_hours": None, "weekly_minutes": None, "service_rates": None,
    "services": None, "service": None, "modes": ["walk", "bus"],
    "mode_limits": None, "wait_rule": "half_headway", "one_way": False,
    "minutes_per_unit": None,
}


def _write_table(path: Path, rows):
    fields = sorted({key for row in rows for key in row})
    normalised = []
    for row in rows:
        filled = dict(row)
        for name in fields:
            if name not in filled:
                filled[name] = DEFAULTS.get(name)
        normalised.append(filled)
    text = "".join(
        json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in normalised
    )
    path.write_text(text, encoding="utf-8")
    raw = text.encode("utf-8")
    return {
        "file": path.name,
        "sha256": hashlib.sha256(raw).hexdigest(),
        "bytes": len(raw),
        "rows": len(rows),
        "fields": fields,
    }


def build_version(version: int, created: str, note: str, dataset="coordination"):
    directory = DATA / "versions" / f"v{version}"
    directory.mkdir(parents=True, exist_ok=True)
    tables = _tables(version)
    manifest = {
        "schema": 1,
        "version": {
            "id": f"v{version}",
            "dataset": dataset,
            "created": created,
            "note": note,
            "previous": f"v{version - 1}" if version > 1 else None,
        },
        "tables": {},
    }
    for name, rows in tables.items():
        manifest["tables"][name] = _write_table(directory / f"{name}.jsonl", rows)
    (directory / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return manifest


def build_registry():
    versions = [
        {"id": "v1", "manifest": "versions/v1/manifest.json",
         "created": "2025-01-06", "previous": None,
         "note": "基线：跨河仅北桥，集镇与河湾共用人员"},
        {"id": "v2", "manifest": "versions/v2/manifest.json",
         "created": "2025-03-03", "previous": "v1",
         "note": "道路更新：河湾人行桥"},
        {"id": "v3", "manifest": "versions/v3/manifest.json",
         "created": "2025-04-07", "previous": "v2",
         "note": "能力更新：西岸与共用窗口增员"},
    ]
    (DATA / "versions.json").write_text(
        json.dumps({"schema": 1, "versions": versions}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def main():
    DATA.mkdir(parents=True, exist_ok=True)
    build_version(1, "2025-01-06", "基线：跨河仅北桥，集镇与河湾共用人员")
    build_version(2, "2025-03-03", "道路更新：河湾人行桥")
    build_version(3, "2025-04-07", "能力更新：西岸与共用窗口增员")
    build_registry()
    print("generated", DATA)


if __name__ == "__main__":
    main()
