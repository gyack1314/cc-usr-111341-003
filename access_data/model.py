"""把登记表规范化为可达性与容量分析使用的领域模型。

两种资料版本都受支持：

* schema 1：现有五表资料（nodes/roads/demand/sites/pools）。人口按
  ``resident_group`` 去重，事项按行登记；没有分项资质、开放时段、班次和
  门槛资料，分析时必须另行提供门槛。
* schema 2：把居民、事项要求、网点资质、开放时段、能力池与班次拆开登记。

规范化不会静默改变原始登记：任何版本兼容假设都写入 ``notes``，报告会
一并输出。
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

from .io import DataError

WALK = "walk"
BUS = "bus"
MODES = (WALK, BUS)
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
DAY_INDEX = {d: i for i, d in enumerate(DAYS)}
DAY_MINUTES = 24 * 60
WEEK_HOURS = 7 * 24
NO_SCHEDULE = frozenset(range(WEEK_HOURS))
WAIT_RULES = ("timetable", "half_headway", "none")


def _fail(message):
    raise DataError(message)


def _number(value, label, allow_zero=True):
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
        _fail(f"{label}必须是非负数值")
    if not allow_zero and value == 0:
        _fail(f"{label}必须大于0")
    return float(value)


def _count(value, label):
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        _fail(f"{label}必须是非负整数（按件登记）")
    return value


@dataclass(frozen=True)
class Road:
    id: str
    source: str
    target: str
    mode: str
    minutes: float
    one_way: bool
    name: str = ""
    note: str = ""

    def directions(self):
        """返回可用方向；缺边的方向就是不可达，不以直线距离补缺。"""
        yield self.source, self.target
        if not self.one_way:
            yield self.target, self.source


@dataclass(frozen=True)
class BusLine:
    id: str
    road: str
    headway_minutes: float
    first_departure: int  # 一日内分钟
    last_departure: int
    days: tuple[str, ...] = DAYS
    wait_rule: str = "half_headway"
    name: str = ""


@dataclass(frozen=True)
class Resident:
    id: str
    person: str
    group: str
    node: str
    area: str
    people: int
    age: str = ""
    note: str = ""


@dataclass(frozen=True)
class Demand:
    id: str
    resident: str
    service: str
    units: int
    atomic: bool
    days: tuple[str, ...] = ()
    earliest_minute: int | None = None
    latest_minute: int | None = None
    note: str = ""


@dataclass(frozen=True)
class Site:
    id: str
    node: str
    services: tuple[str, ...]
    pool: str
    minutes_per_unit: float
    open_hours: frozenset[int] = NO_SCHEDULE
    site_active: bool = True
    weekly_minutes: float | None = None
    area: str = ""
    service_rates: tuple[tuple[str, float], ...] = ()
    name: str = ""
    note: str = ""

    @property
    def service(self):
        return self.services[0] if self.services else ""

    def rate_for(self, service):
        for name, rate in self.service_rates:
            if name == service:
                return rate
        return self.minutes_per_unit


@dataclass(frozen=True)
class Standard:
    id: str
    service: str
    group: str  # "*" 表示该事项的默认门槛
    max_travel_minutes: float
    modes: tuple[str, ...] = MODES
    mode_limits: tuple[tuple[str, float], ...] = ()
    note: str = ""

    def limit_for(self, mode):
        """按出行方式返回时间门槛；未单列则该方式沿用统一门槛。"""
        for name, limit in self.mode_limits:
            if name == mode:
                return limit
        return self.max_travel_minutes

    def allowed_modes(self):
        if self.mode_limits:
            return tuple(name for name, _ in self.mode_limits)
        return self.modes


@dataclass(frozen=True)
class Pool:
    id: str
    weekly_minutes: float
    name: str = ""


@dataclass(frozen=True)
class Version:
    """一个资料版本的可分析视图，保留来源清单与生成时间。"""

    version_id: str
    dataset: str
    created: str
    residents: dict[str, Resident]
    demands: dict[str, Demand]
    sites: dict[str, Site]
    pools: dict[str, Pool]
    roads: dict[str, Road]
    nodes: tuple[str, ...]
    bus_lines: dict[str, BusLine] = field(default_factory=dict)
    standards: dict[str, Standard] = field(default_factory=dict)
    notes: tuple[str, ...] = ()
    meta: dict[str, Any] = field(default_factory=dict)

    def services(self):
        return sorted({d.service for d in self.demands.values()})

    def demands_of(self, resident):
        return [d for d in self.demands.values() if d.resident == resident]

    def standard_for(self, demand):
        resident = self.residents[demand.resident]
        exact = [s for s in self.standards.values()
                 if s.service == demand.service and s.group == resident.group]
        if exact:
            return exact[0]
        default = [s for s in self.standards.values()
                   if s.service == demand.service and s.group == "*"]
        return default[0] if default else None

    def group_key(self, resident_id):
        resident = self.residents[resident_id]
        return (resident.area, resident.group)


# ---------------------------------------------------------------------------
# 时段与班次约定
# ---------------------------------------------------------------------------

def _parse_day(value, label):
    if value not in DAY_INDEX:
        _fail(f"{label}的星期{value!r}无效")
    return value


def _parse_clock(value, label):
    if not isinstance(value, str) or value.count(":") != 1:
        _fail(f"{label}的时间{value!r}无效")
    hh, mm = value.split(":")
    try:
        h, m = int(hh), int(mm)
    except ValueError:
        _fail(f"{label}的时间{value!r}无效")
    if not (0 <= h <= 24 and 0 <= m < 60) or (h == 24 and m):
        _fail(f"{label}的时间{value!r}超出一天")
    return h * 60 + m


def parse_hours(entries, label):
    """接受开放时段列表，返回一周内按小时展开的集合。"""
    if entries is None:
        return NO_SCHEDULE
    hours = set()
    for raw in entries:
        if not isinstance(raw, dict) or "day" not in raw or "start" not in raw or "end" not in raw:
            _fail(f"{label}的开放时段条目无效")
        day_index = DAY_INDEX[_parse_day(raw["day"], label)]
        start = _parse_clock(raw["start"], label)
        end = _parse_clock(raw["end"], label)
        if end <= start:
            _fail(f"{label}的结束时间必须晚于开始时间（跨夜请拆成两段）")
        for minute in range(start, end, 60):
            hours.add(day_index * 24 + minute // 60)
    if not hours:
        _fail(f"{label}的开放时段为空")
    return frozenset(hours)


def demand_hours(demand):
    """事项可办理的周内小时集合；未登记天/时段则视为全周全天。"""
    days = [DAY_INDEX[d] for d in demand.days] if demand.days else list(range(7))
    start = demand.earliest_minute if demand.earliest_minute is not None else 0
    end = demand.latest_minute if demand.latest_minute is not None else DAY_MINUTES
    hours = set()
    for day in days:
        for minute in range(start, end, 60):
            hours.add(day * 24 + minute // 60)
    return hours


def site_open_minutes_for_demand(site, demand):
    """网点开放与事项可办理时段的交集（按小时）；空集即无时间交集。"""
    return {h for h in demand_hours(demand) if h in site.open_hours}


# ---------------------------------------------------------------------------
# schema 1（现有五表）
# ---------------------------------------------------------------------------

def from_schema1(tables, version_id="normal", dataset="normal", created=""):
    notes = []
    nodes = tuple(sorted(row["id"] for row in tables["nodes"]))
    node_set = set(nodes)

    residents = {}
    demands = {}
    seen_groups = set()
    for row in tables["demand"]:
        group = row["resident_group"]
        if not isinstance(group, str) or not group:
            _fail("demand.resident_group不得为空")
        node = row["node"]
        area = row["area"]
        if node not in node_set:
            _fail(f"demand {row['id']}的node不在nodes中")
        people = _count(row["people"], f"demand {row['id']}的people")
        units = _count(row["units"], f"demand {row['id']}的units")
        if group in residents:
            resident = residents[group]
            if resident.node != node or resident.area != area or resident.people != people:
                _fail(f"居民组{group}的居住格、地区或人口登记不一致")
        else:
            residents[group] = Resident(
                id=group, person=group, group=group, node=node, area=area,
                people=people,
            )
        if (group, row["service"]) in seen_groups:
            _fail(f"居民{group}的事项{row['service']}以多行登记，无法区分需求")
        seen_groups.add((group, row["service"]))
        demands[row["id"]] = Demand(
            id=row["id"], resident=group, service=row["service"], units=units,
            atomic=True, note="schema1默认整单办理",
        )
    notes.append(
        "schema1：人口按resident_group去重；同一居民多事项分别计算；"
        "未登记可分性时按整单办理；未登记开放时段时视为全周开放；"
        "schema1没有分项资质、班次与门槛资料，分析时必须另行提供门槛。"
    )

    sites = {}
    pool_ids = {row["id"] for row in tables["pools"]}
    for row in tables["sites"]:
        if row["node"] not in node_set:
            _fail(f"site {row['id']}的node不在nodes中")
        if row["pool"] not in pool_ids:
            _fail(f"site {row['id']}引用不存在的pool")
        sites[row["id"]] = Site(
            id=row["id"], node=row["node"], services=(row["service"],),
            pool=row["pool"],
            minutes_per_unit=_number(row["minutes_per_unit"], f"site {row['id']}"),
        )
    pools = {
        row["id"]: Pool(row["id"], _number(row["weekly_minutes"], f"pool {row['id']}"))
        for row in tables["pools"]
    }
    roads = {}
    for row in tables["roads"]:
        if row["source"] not in node_set or row["target"] not in node_set:
            _fail(f"road {row['id']}的端点不在nodes中")
        if row["mode"] not in MODES:
            _fail(f"road {row['id']}的mode {row['mode']!r}未知")
        roads[row["id"]] = Road(
            id=row["id"], source=row["source"], target=row["target"],
            mode=row["mode"],
            minutes=_number(row["minutes"], f"road {row['id']}"),
            one_way=bool(row["one_way"]),
        )
    return Version(
        version_id=version_id, dataset=dataset, created=created,
        residents=residents, demands=demands, sites=sites, pools=pools,
        roads=roads, nodes=nodes, notes=tuple(notes),
    )


# ---------------------------------------------------------------------------
# schema 2（拆分登记）
# ---------------------------------------------------------------------------

def _require(tables, name):
    if name not in tables:
        _fail(f"缺少资料表{name}")
    return tables[name]


def _parse_service_rates(row, site_id, base_rate):
    raw = row.get("service_rates")
    if raw is None:
        return ()
    if not isinstance(raw, dict) or not raw:
        _fail(f"sites {site_id}的service_rates必须是非空映射")
    rates = []
    for service, rate in sorted(raw.items()):
        if not isinstance(service, str) or not service:
            _fail(f"sites {site_id}的service_rates事项名无效")
        rates.append((service, _number(rate, f"sites {site_id}的service_rates[{service}]", allow_zero=False)))
    return tuple(rates)


def from_schema2(tables, version_id=None, dataset="", created="", meta=None):
    notes = []
    nodes = tuple(sorted(row["id"] for row in _require(tables, "nodes")))
    node_set = set(nodes)

    residents = {}
    persons = {}
    for row in _require(tables, "residents"):
        rid = row["id"]
        if row["node"] not in node_set:
            _fail(f"residents {rid}的node不在nodes中")
        people = _count(row["people"], f"residents {rid}的people")
        person = row.get("person", rid)
        if not isinstance(person, str) or not person:
            _fail(f"residents {rid}的person不得为空")
        if person in persons:
            # 同一人重复列入两个地区不得增加需求；这里直接拒绝重复登记。
            _fail(f"person {person}在residents中重复登记（{persons[person]}与{rid}）")
        persons[person] = rid
        residents[rid] = Resident(
            id=rid, person=person, group=row.get("group", "all"),
            node=row["node"], area=row["area"], people=people,
            age=row.get("age", ""), note=row.get("note", ""),
        )

    demands = {}
    for row in _require(tables, "demands"):
        did = row["id"]
        resident = row.get("resident")
        if resident not in residents:
            _fail(f"demands {did}引用不存在的residents记录")
        atomic = row.get("atomic", True)
        if not isinstance(atomic, bool):
            _fail(f"demands {did}的atomic必须是布尔值")
        days = tuple(_parse_day(d, f"demands {did}") for d in row.get("days", ()))
        earliest = latest = None
        if row.get("earliest") is not None:
            earliest = _parse_clock(row["earliest"], f"demands {did}")
        if row.get("latest") is not None:
            latest = _parse_clock(row["latest"], f"demands {did}")
        if earliest is not None and latest is not None and latest <= earliest:
            _fail(f"demands {did}的最晚时间必须晚于最早时间")
        demands[did] = Demand(
            id=did, resident=resident, service=row["service"],
            units=_count(row["units"], f"demands {did}的units"),
            atomic=atomic, days=days, earliest_minute=earliest,
            latest_minute=latest, note=row.get("note", ""),
        )

    pools = {}
    for row in _require(tables, "pools"):
        pools[row["id"]] = Pool(
            row["id"], _number(row["weekly_minutes"], f"pools {row['id']}"),
            name=row.get("name", ""),
        )

    sites = {}
    for row in _require(tables, "sites"):
        sid = row["id"]
        if row["node"] not in node_set:
            _fail(f"sites {sid}的node不在nodes中")
        if row.get("pool") not in pools:
            _fail(f"sites {sid}引用不存在的pools记录")
        services = tuple(row["services"]) if "services" in row else (row["service"],)
        if not services or any(not isinstance(s, str) or not s for s in services):
            _fail(f"sites {sid}的事项资质为空")
        minutes_per_unit = _number(
            row.get("minutes_per_unit", 0), f"sites {sid}的minutes_per_unit"
        )
        rates = _parse_service_rates(row, sid, minutes_per_unit)
        if "minutes_per_unit" not in row and not rates:
            _fail(f"sites {sid}缺少办理速率")
        weekly = row.get("weekly_minutes")
        sites[sid] = Site(
            id=sid, node=row["node"], services=services, pool=row["pool"],
            minutes_per_unit=minutes_per_unit or (
                rates[0][1] if rates else 0.0
            ),
            open_hours=parse_hours(row.get("open_hours"), f"sites {sid}"),
            site_active=bool(row.get("active", True)),
            weekly_minutes=None if weekly is None else _number(weekly, f"sites {sid}的weekly_minutes"),
            area=row.get("area", ""), service_rates=rates,
            name=row.get("name", ""), note=row.get("note", ""),
        )

    roads = {}
    for row in _require(tables, "roads"):
        rid = row["id"]
        if row["source"] not in node_set or row["target"] not in node_set:
            _fail(f"roads {rid}的端点不在nodes中")
        if row["mode"] not in MODES:
            _fail(f"roads {rid}的mode未知")
        one_way = row.get("one_way", False)
        if not isinstance(one_way, bool):
            _fail(f"roads {rid}的one_way必须是布尔值")
        roads[rid] = Road(
            id=rid, source=row["source"], target=row["target"], mode=row["mode"],
            minutes=_number(row["minutes"], f"roads {rid}"), one_way=one_way,
            name=row.get("name", ""), note=row.get("note", ""),
        )

    bus_lines = {}
    for row in _require(tables, "bus_lines"):
        line = _parse_bus_line(row)
        roads.setdefault(line.road, None)
        bus_lines[line.id] = line
    for line in bus_lines.values():
        road = roads[line.road]
        if road is None or road.mode != BUS:
            _fail(f"bus_lines {line.id}必须引用mode=bus的roads记录")

    standards = {}
    for row in _require(tables, "standards"):
        modes = tuple(row.get("modes", MODES))
        if not modes or any(m not in MODES for m in modes):
            _fail(f"standards {row['id']}的modes无效")
        raw_limits = row.get("mode_limits") or {}
        if not isinstance(raw_limits, dict):
            _fail(f"standards {row['id']}的mode_limits必须是映射")
        mode_limits = tuple(sorted(
            (name, _number(limit, f"standards {row['id']}的mode_limits[{name}]"))
            for name, limit in raw_limits.items()
        ))
        if any(name not in MODES for name, _ in mode_limits):
            _fail(f"standards {row['id']}的mode_limits含未知出行方式")
        standards[row["id"]] = Standard(
            id=row["id"], service=row["service"], group=row.get("group", "*"),
            max_travel_minutes=_number(
                row["max_travel_minutes"], f"standards {row['id']}的max_travel_minutes"
            ),
            modes=modes, mode_limits=mode_limits, note=row.get("note", ""),
        )
    if not standards:
        _fail("standards不得为空")

    if "notes" in tables:
        notes.extend(str(n["text"]) if isinstance(n, dict) else str(n)
                     for n in tables["notes"])
    if version_id is None:
        version_id = dataset or "version"
    return Version(
        version_id=version_id, dataset=dataset, created=created,
        residents=residents, demands=demands, sites=sites, pools=pools, roads=roads,
        nodes=nodes, bus_lines=bus_lines, standards=standards,
        notes=tuple(notes), meta=dict(meta or {}),
    )


def _parse_bus_line(row):
    line = row["id"]
    headway = _number(row["headway_minutes"], f"bus_lines {line}的headway_minutes",
                      allow_zero=False)
    first = _parse_clock(row["first_departure"], f"bus_lines {line}")
    last = _parse_clock(row["last_departure"], f"bus_lines {line}")
    if last < first:
        _fail(f"bus_lines {line}的末班早于首班")
    days = tuple(_parse_day(d, f"bus_lines {line}") for d in row.get("days", DAYS))
    if not days:
        _fail(f"bus_lines {line}的运行日为空")
    wait_rule = row.get("wait_rule", "half_headway")
    if wait_rule not in WAIT_RULES:
        _fail(f"bus_lines {line}的wait_rule未知")
    return BusLine(
        id=line, road=row["road"], headway_minutes=headway,
        first_departure=first, last_departure=last, days=days,
        wait_rule=wait_rule, name=row.get("name", ""),
    )


# ---------------------------------------------------------------------------
# 载入入口
# ---------------------------------------------------------------------------

SCHEMA1_TABLES = {"nodes", "roads", "demand", "sites", "pools"}
SCHEMA2_TABLES = {"nodes", "residents", "demands", "sites", "pools", "roads",
                  "bus_lines", "standards"}


def build_version(tables, version_id, dataset, created="", meta=None):
    """按表集合选择 schema 1 或 schema 2 规范化。"""
    names = set(tables)
    if names == SCHEMA1_TABLES:
        return from_schema1(tables, version_id, dataset, created)
    if names == SCHEMA2_TABLES:
        return from_schema2(tables, version_id, dataset, created, meta)
    _fail(f"资料表集合不受支持：{sorted(names)}")


def version_from_manifest(manifest_path, version_id=None):
    """读取清单（可含 version 元数据）并规范化。"""
    import json
    from pathlib import Path

    from .io import load

    tables = load(manifest_path)
    raw = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    meta = raw.get("version", {})
    name = version_id or meta.get("id") or Path(manifest_path).stem
    return build_version(tables, name, meta.get("dataset", name),
                         meta.get("created", ""), meta)


# ---------------------------------------------------------------------------
# 结构性警告
# ---------------------------------------------------------------------------

def with_default_standard(version: Version, minutes, modes=MODES,
                          services=None):
    """为没有登记门槛的事项补一个显式默认门槛。

    schema 1 没有门槛字段；分析时若不提供默认值，不应悄悄按“无限时间”
    处理。此函数只补缺失项，已登记的门槛保持不变。
    """
    targets = services or version.services()
    standards = dict(version.standards)
    for service in targets:
        has = any(s.service == service for s in standards.values())
        if has:
            continue
        key = f"default_{service}"
        standards[key] = Standard(
            id=key, service=service, group="*",
            max_travel_minutes=float(minutes), modes=tuple(modes),
            note="由分析入口显式提供的默认门槛",
        )
    return replace(version, standards=standards)


def validate_model(version: Version):
    """返回结构性警告；不可修复的问题在规范化时已抛出 DataError。"""
    warnings = []
    demand_residents = {d.resident for d in version.demands.values()}
    residents_without_demand = sorted(set(version.residents) - demand_residents)
    if residents_without_demand:
        warnings.append(f"以下居民没有事项需求：{residents_without_demand}")
    for demand in version.demands.values():
        resident = version.residents[demand.resident]
        if resident.people == 0:
            warnings.append(f"居民{demand.resident}的人口为0，事项{demand.id}需求可能无法解释")
        if version.standard_for(demand) is None:
            warnings.append(f"事项{demand.id}缺少服务门槛资料")
    for site in version.sites.values():
        if site.site_active and not site.open_hours:
            warnings.append(f"网点{site.id}开放时段为空")
    if not any(s.site_active for s in version.sites.values()):
        warnings.append("没有启用的网点")
    return warnings
