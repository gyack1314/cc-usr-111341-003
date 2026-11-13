"""地理可达与容量分配。

分析分两层，输出顺序也按这两层：

1. **地理可达（不考虑容量）**：对每个居民事项，按门槛、出行方式求最短
   路径；缺边、缺方向、班次窗口之外都视为不可达，不以直线距离补缺。
   可服务网点还需资质兼容、开放时段与事项时段有交集、到点后能办完。
2. **容量分配**：按事项“件”分配，不按人口。共享能力池按周分钟数只用
   一次；网点也可有自己的每周上限。可分事项允许部分办理，整单事项要么
   全办要么不办。

分配器有三种明确状态：

* ``exact``：完整搜索了整数分配空间，结果可证明最优；
* ``budget_exhausted``：搜索到预算上限，返回目前最好可行解与松弛上界；
* ``exact_enumeration``：小网独立枚举器的结果，仅用于核对。

容量松弛上界会忽略其他需求对同一池的占用，因此永远不低于真实最优。
未搜完时报告必须同时给出可行解、上界与求解状态，不能把近似解写成最优。
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field

from .model import (
    BUS, WALK, DAY_MINUTES, Version, Demand,
    site_open_minutes_for_demand,
)
from .network import Path, Router

INF = float("inf")
EPS = 1e-9


# ---------------------------------------------------------------------------
# 地理可达
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Candidate:
    site_id: str
    path: Path
    mode: str = ""

    @property
    def minutes(self):
        return self.path.minutes

    def as_dict(self):
        return {"site": self.site_id, "mode": self.mode,
                "travel_minutes": round(self.minutes, 6),
                "path": self.path.as_dict()}


@dataclass(frozen=True)
class Option:
    """容量分配的一个候选网点（可与其他同池同速率的网点互换）。"""

    site_id: str
    pool_id: str
    rate: float
    travel_minutes: float
    path: Path | None = None
    site_capacity_minutes: float = INF

    @property
    def capacity_units(self):
        if self.rate <= 0:
            return INF
        return self.site_capacity_minutes / self.rate


@dataclass
class Reach:
    resident_id: str
    demand_id: str
    threshold: float | None
    allowed_modes: tuple[str, ...]
    service: str
    units: int
    candidates: list[Candidate] = field(default_factory=list)
    excluded: dict[str, list] = field(default_factory=dict)
    status: str = "unreachable"
    reason: str = ""

    def as_dict(self):
        return {
            "resident": self.resident_id,
            "demand": self.demand_id,
            "service": self.service,
            "units": self.units,
            "threshold_minutes": self.threshold,
            "allowed_modes": list(self.allowed_modes),
            "status": self.status,
            "reason": self.reason,
            "candidates": [c.as_dict() for c in self.candidates],
            "excluded": self.excluded,
        }


def _minute_blocks(hours, day):
    """把某一天的小时集合拆成连续区间 [(start_minute, end_minute)]。"""
    selected = sorted(h - day * 24 for h in hours if h // 24 == day)
    if not selected:
        return []
    blocks = []
    start = prev = selected[0]
    for hour in selected[1:]:
        if hour != prev + 1:
            blocks.append((start * 60, (prev + 1) * 60))
            start = hour
        prev = hour
    blocks.append((start * 60, (prev + 1) * 60))
    return blocks


def _first_departure(version, demand, default_clock):
    """路线按事项允许的第一个办理日、最早时刻计算；未登记则用默认时刻。"""
    day = 0
    if demand.days:
        from .model import DAY_INDEX
        day = min(DAY_INDEX[d] for d in demand.days)
    clock = demand.earliest_minute if demand.earliest_minute is not None else default_clock
    return day * DAY_MINUTES + clock


class Geographic:
    """按门槛、出行方式、资质和开放时段筛选可达网点。"""

    def __init__(self, version: Version, departure_minute=8 * 60):
        self.version = version
        self.departure_minute = departure_minute
        self._paths = {}
        self._routers = {}

    def _router(self, modes, departure):
        key = (tuple(sorted(modes)), departure)
        if key not in self._routers:
            self._routers[key] = Router(
                self.version, departure_minute=departure, modes=modes
            )
        return self._routers[key]

    def _paths_for(self, resident_id, modes, departure):
        key = (resident_id, tuple(sorted(modes)), departure)
        if key not in self._paths:
            router = self._router(modes, departure)
            self._paths[key] = router.shortest_paths(
                self.version.residents[resident_id].node
            )
        return self._paths[key]

    def evaluate(self, demand: Demand):
        version = self.version
        resident = version.residents[demand.resident]
        standard = version.standard_for(demand)
        threshold = standard.max_travel_minutes if standard else None
        allowed = tuple(standard.modes if standard else (WALK, BUS))
        reach = Reach(
            resident_id=demand.resident, demand_id=demand.id,
            threshold=threshold, allowed_modes=allowed,
            service=demand.service, units=demand.units,
        )
        departure = _first_departure(version, demand, self.departure_minute)
        all_paths = self._paths_for(demand.resident, (WALK, BUS), departure)
        allowed_paths = self._paths_for(demand.resident, allowed, departure)
        qualified = []

        for site in sorted(version.sites.values(), key=lambda s: s.id):
            if demand.service not in site.services:
                reach.excluded.setdefault("qualification", []).append({"site": site.id})
                continue
            if not site.site_active:
                reach.excluded.setdefault("inactive", []).append({"site": site.id})
                continue
            qualified.append(site)
            if not site_open_minutes_for_demand(site, demand):
                reach.excluded.setdefault("no_open_overlap", []).append({"site": site.id})
                continue
            # 逐种允许的出行方式取最短路径；各方式可能有不同门槛，
            # 因此取“满足该方式门槛且总时间最短”的那一条。
            best_path = None
            best_mode = None
            mode_reasons = []
            for mode in allowed:
                mode_paths = self._paths_for(demand.resident, (mode,), departure)
                path = mode_paths.get(site.node)
                if path is None:
                    if site.node in all_paths:
                        mode_reasons.append("mode_not_allowed")
                    else:
                        mode_reasons.append("unreachable")
                    continue
                limit = standard.limit_for(mode) if standard else None
                if limit is not None and path.minutes > limit + EPS:
                    mode_reasons.append("time_threshold")
                    continue
                if best_path is None or path.minutes < best_path.minutes:
                    best_path, best_mode = path, mode
            if best_path is None:
                # 有路但超门槛优先记为门槛问题；完全无路才是不可达。
                if site.node in all_paths:
                    reason = ("time_threshold" if "time_threshold" in mode_reasons
                              else "mode_not_allowed")
                else:
                    reason = "unreachable"
                if reason == "mode_not_allowed":
                    reach.excluded.setdefault("mode_not_allowed", []).append(
                        {"site": site.id, "allowed_modes": list(allowed)}
                    )
                elif reason == "time_threshold":
                    best_other = min(
                        (self._paths_for(demand.resident, (m,), departure)[site.node].minutes
                         for m in allowed
                         if site.node in self._paths_for(demand.resident, (m,), departure)),
                        default=None,
                    )
                    reach.excluded.setdefault("time_threshold", []).append(
                        {"site": site.id, "minutes": round(best_other, 6)
                         if best_other is not None else None,
                         "allowed_modes": list(allowed)}
                    )
                else:
                    reach.excluded.setdefault("unreachable", []).append({"site": site.id})
                continue
            path = best_path
            rate = site.rate_for(demand.service)
            needed = rate * (demand.units if demand.atomic else 1)
            if not self._arrival_fits(site, demand, path.minutes, needed):
                reach.excluded.setdefault("arrival_after_closing", []).append(
                    {"site": site.id, "minutes": round(path.minutes, 6),
                     "service_minutes": round(needed, 6)}
                )
                continue
            if site.weekly_minutes is not None and needed > site.weekly_minutes + EPS:
                # 单次整单都放不进网点自己的上限。
                pass
            reach.candidates.append(Candidate(site.id, path, best_mode))

        reach.candidates.sort(key=lambda c: (c.minutes, c.site_id))
        if reach.candidates:
            reach.status = "reachable"
        else:
            reach.status = "unreachable"
            if not qualified:
                reach.reason = "qualification"
            else:
                for reason in ("time_threshold", "mode_not_allowed", "unreachable",
                               "no_open_overlap", "arrival_after_closing", "inactive"):
                    if reason in reach.excluded:
                        reach.reason = reason
                        break
                else:
                    reach.reason = "no_open_overlap"
            reach.excluded.setdefault(reach.reason, [])
        return reach

    def _arrival_fits(self, site, demand, travel, service_minutes):
        overlap = site_open_minutes_for_demand(site, demand)
        clock = demand.earliest_minute if demand.earliest_minute is not None else 0
        for day in range(7):
            for start, end in _minute_blocks(overlap, day):
                day_start = day * 24 * 60
                depart = day_start + max(start, clock)
                if depart + travel + service_minutes <= day_start + end:
                    return True
        return False

    def all(self):
        return {d.id: self.evaluate(d) for d in self.version.demands.values()}

    def summary(self, reaches=None):
        reaches = reaches or self.all()
        by_reason = {}
        for reach in reaches.values():
            if reach.status != "reachable":
                by_reason[reach.reason] = by_reason.get(reach.reason, 0) + 1
        return {
            "demands": len(reaches),
            "demand_units": sum(r.units for r in reaches.values()),
            "reachable_demands": sum(1 for r in reaches.values() if r.status == "reachable"),
            "reachable_units": sum(r.units for r in reaches.values() if r.status == "reachable"),
            "unreachable_demands": sum(1 for r in reaches.values() if r.status != "reachable"),
            "unreachable_units": sum(r.units for r in reaches.values() if r.status != "reachable"),
            "by_reason": dict(sorted(by_reason.items())),
        }


# ---------------------------------------------------------------------------
# 分配台账
# ---------------------------------------------------------------------------

@dataclass
class Allocation:
    demand_id: str
    site_id: str
    units: int
    minutes: float
    travel_minutes: float
    path: Path | None = None
    cross_boundary: bool = False

    def as_dict(self):
        return {
            "demand": self.demand_id,
            "site": self.site_id,
            "units": self.units,
            "pool_minutes": round(self.minutes, 6),
            "travel_minutes": round(self.travel_minutes, 6),
            "cross_boundary": self.cross_boundary,
        }


class Ledger:
    """记录能力池与网点自己的每周分钟消耗；共享池只扣一次。"""

    def __init__(self, version: Version, options: dict[str, list[Option]]):
        self.version = version
        self.pool_cap = {p.id: p.weekly_minutes for p in version.pools.values()}
        self.pool_used = {p.id: 0.0 for p in version.pools.values()}
        self.site_cap = {}
        self.site_used = {}
        for site in version.sites.values():
            self.site_cap[site.id] = (
                site.weekly_minutes if site.weekly_minutes is not None else INF
            )
            self.site_used[site.id] = 0.0
        for option in (o for options_list in options.values() for o in options_list):
            if option.site_id not in self.site_cap:
                self.site_cap[option.site_id] = INF
                self.site_used[option.site_id] = 0.0

    def residual_minutes(self, option: Option):
        return min(
            self.pool_cap[option.pool_id] - self.pool_used[option.pool_id],
            self.site_cap[option.site_id] - self.site_used[option.site_id],
        )

    def residual_units(self, option: Option):
        """剩余分钟能支持的整件数；宁可少算一件，不可多算。"""
        if option.rate <= 0:
            return INF
        return max(0.0, math.floor(self.residual_minutes(option) / option.rate + EPS))

    def fits(self, option: Option, units: int):
        return units * option.rate <= self.residual_minutes(option) + EPS

    def spend(self, option: Option, units: int):
        minutes = units * option.rate
        self.pool_used[option.pool_id] += minutes
        self.site_used[option.site_id] += minutes

    def unspend(self, option: Option, units: int):
        minutes = units * option.rate
        self.pool_used[option.pool_id] -= minutes
        self.site_used[option.site_id] -= minutes

    def blocking_resources(self, options: list[Option], demand: Demand):
        """指出卡住该需求的资源；用于“约束资源”栏。"""
        resources = []
        if not options:
            return resources
        needed_units = demand.units
        pool_ids = sorted({o.pool_id for o in options})
        for pool_id in pool_ids:
            remaining = self.pool_cap[pool_id] - self.pool_used[pool_id]
            rates = [o.rate for o in options if o.pool_id == pool_id and o.rate > 0]
            if rates and remaining / min(rates) < needed_units - EPS:
                resources.append({
                    "resource": f"pool:{pool_id}", "kind": "pool",
                    "weekly_minutes": self.pool_cap[pool_id],
                    "used_minutes": round(self.pool_used[pool_id], 6),
                    "remaining_minutes": round(remaining, 6),
                    "needed_minutes": round(needed_units * min(rates), 6),
                })
        for site_id in sorted({o.site_id for o in options}):
            option = next(o for o in options if o.site_id == site_id)
            remaining = self.site_cap[site_id] - self.site_used[site_id]
            if remaining != INF and needed_units * option.rate > remaining + EPS:
                resources.append({
                    "resource": f"site:{site_id}", "kind": "site",
                    "weekly_minutes": self.site_cap[site_id],
                    "used_minutes": round(self.site_used[site_id], 6),
                    "remaining_minutes": round(remaining, 6),
                    "needed_minutes": round(needed_units * option.rate, 6),
                })
        return resources

    def snapshot(self):
        pools = []
        for pool in sorted(self.version.pools.values(), key=lambda p: p.id):
            pools.append({
                "pool": pool.id,
                "name": pool.name,
                "weekly_minutes": self.pool_cap[pool.id],
                "used_minutes": round(self.pool_used[pool.id], 6),
                "remaining_minutes": round(self.pool_cap[pool.id] - self.pool_used[pool.id], 6),
                "utilization": _ratio(self.pool_used[pool.id], self.pool_cap[pool.id]),
                "sites": sorted(s.id for s in self.version.sites.values() if s.pool == pool.id),
            })
        sites = []
        for site in sorted(self.version.sites.values(), key=lambda s: s.id):
            cap = self.site_cap[site.id]
            sites.append({
                "site": site.id,
                "pool": site.pool,
                "services": list(site.services),
                "weekly_minutes": None if cap == INF else cap,
                "used_minutes": None if cap == INF else round(self.site_used[site.id], 6),
                "remaining_minutes": None if cap == INF else round(cap - self.site_used[site.id], 6),
            })
        return {"pools": pools, "sites": sites}


# ---------------------------------------------------------------------------
# 结果对象
# ---------------------------------------------------------------------------

@dataclass
class DemandOutcome:
    demand_id: str
    resident_id: str
    person: str
    area: str
    group: str
    service: str
    units: int
    atomic: bool
    served: int = 0
    allocations: list[Allocation] = field(default_factory=list)
    reach_status: str = ""
    reach_reason: str = ""
    candidates: list[Candidate] = field(default_factory=list)
    reason: str = ""
    constraint_resources: list = field(default_factory=list)
    options: list[Option] = field(default_factory=list)

    @property
    def unserved(self):
        return self.units - self.served

    @property
    def fully_served(self):
        return self.served >= self.units

    def as_dict(self):
        return {
            "demand": self.demand_id,
            "resident": self.resident_id,
            "person": self.person,
            "area": self.area,
            "group": self.group,
            "service": self.service,
            "units": self.units,
            "atomic": self.atomic,
            "served_units": self.served,
            "unserved_units": self.unserved,
            "unserved_reason": None if self.fully_served else self.reason,
            "reach_status": self.reach_status,
            "reach_reason": None if self.reach_status == "reachable" else self.reach_reason,
            "allocations": [a.as_dict() for a in self.allocations],
            "reachable_sites": [
                {"site": c.site_id, "travel_minutes": round(c.minutes, 6)}
                for c in self.candidates
            ],
            "constraint_resources": (
                [] if self.fully_served else self.constraint_resources
            ),
        }


def _ratio(numerator, denominator):
    if denominator in (0, None):
        return None
    return round(numerator / denominator, 6)


def coverage_vector(groups_info, served_g):
    """按覆盖率升序排列的群体覆盖率向量；分母是事项件数。

    “优先改善最差覆盖群体”用 leximin 实现：逐项比较排序后的覆盖率，
    先改善最差的一组，再改善次差的一组，依此类推。这样在多个群体都
    为零覆盖时仍能区分“先救谁”，而不是被并列的最差组卡死。
    """
    values = sorted(
        served_g.get(g, 0) / info["demand_units"]
        for g, info in groups_info.items() if info["demand_units"] > 0
    )
    return tuple(values)


# ---------------------------------------------------------------------------
# 分配器
# ---------------------------------------------------------------------------

class Allocator:
    """在冻结的公平目标下分配容量。

    ``total``：最大化总服务件数，同分时最大化最差群体覆盖率。
    ``worst``：最大化最差群体覆盖率，同分时最大化总服务件数。

    分配单位是事项的“件”。整单事项不可拆分；可分事项按下述规则分配。
    搜索对不同 (池, 速率) 的候选去重，避免同质网点造成无意义的排列。
    """

    OBJECTIVES = ("total", "fair")
    ALIASES = {"worst": "fair", "leximin": "fair", "max_total": "total"}

    def __init__(self, version: Version, objective="total", budget_nodes=200_000,
                 time_budget_s=5.0, departure_minute=8 * 60):
        objective = self.ALIASES.get(objective, objective)
        if objective not in self.OBJECTIVES:
            raise ValueError(f"未知公平目标：{objective}")
        self.version = version
        self.objective = objective
        self.budget_nodes = int(budget_nodes)
        self.time_budget_s = float(time_budget_s)
        self.geo = Geographic(version, departure_minute)

    # -- 建模 -----------------------------------------------------------
    def build(self, reaches=None):
        reaches = reaches or self.geo.all()
        outcomes = {}
        for demand in self.version.demands.values():
            resident = self.version.residents[demand.resident]
            reach = reaches[demand.id]
            outcomes[demand.id] = DemandOutcome(
                demand_id=demand.id, resident_id=demand.resident,
                person=resident.person, area=resident.area, group=resident.group,
                service=demand.service, units=demand.units, atomic=demand.atomic,
                reach_status=reach.status, reach_reason=reach.reason,
                candidates=reach.candidates,
                options=self.options_for(demand, reach),
            )
        groups = {}
        for outcome in outcomes.values():
            groups.setdefault((outcome.area, outcome.group), {"demand_units": 0})
            groups[(outcome.area, outcome.group)]["demand_units"] += outcome.units
        return reaches, outcomes, groups

    def options_for(self, demand, reach):
        """候选网点去重：同池、同速率、同网点上限的网点可互换。"""
        seen = {}
        options = []
        for cand in reach.candidates:
            site = self.version.sites[cand.site_id]
            cap = site.weekly_minutes if site.weekly_minutes is not None else INF
            key = (site.pool, site.rate_for(demand.service), cap)
            option = Option(
                site_id=site.id, pool_id=site.pool,
                rate=site.rate_for(demand.service),
                travel_minutes=cand.minutes, path=cand.path,
                site_capacity_minutes=cap,
            )
            if key in seen:
                continue
            seen[key] = option
            options.append(option)
        options.sort(key=lambda o: (o.travel_minutes, o.site_id))
        return options

    # -- 求解入口 -------------------------------------------------------
    def solve(self, reaches=None):
        reaches, outcomes, groups = self.build(reaches)
        self._reaches = reaches
        self._outcomes = outcomes
        self._groups = groups
        self._order = self._search_order()
        self._ledger = Ledger(self.version, {k: o.options for k, o in outcomes.items()})
        self._served_by_group = {g: 0 for g in groups}
        self._total_served = 0
        self._nodes = 0
        self._start = time.monotonic()
        self._aborted = False
        self._best = None
        self.deadline = self._start + self.time_budget_s
        self.upper_bound = self._upper_bound()

        # 构造下界：两种贪心各自从空解出发并记录，取较好者。
        self._greedy_total()
        self._record()
        self._reset_state()
        self._greedy_worst()
        self._record()
        self._reset_state()

        self._dfs(0)
        status = "exact" if not self._aborted else "budget_exhausted"

        self._materialise(outcomes)
        return AnalysisResult(
            version=self.version, objective=self.objective, reaches=reaches,
            outcomes=outcomes, ledger=self._ledger, geo=self.geo,
            status=status, nodes=self._nodes, budget_nodes=self.budget_nodes,
            upper_bound=self.upper_bound, time_budget_s=self.time_budget_s,
        )

    # -- 目标与上界 -----------------------------------------------------
    def _search_order(self):
        demands = []
        for demand in self.version.demands.values():
            outcome = self._outcomes[demand.id]
            if not outcome.options:
                continue
            if self.objective == "total":
                key = (len(outcome.options), -demand.units,
                       min(o.travel_minutes for o in outcome.options), demand.id)
            else:
                group_units = self._groups[(outcome.area, outcome.group)]["demand_units"]
                key = (group_units, len(outcome.options), -demand.units, demand.id)
            demands.append((key, demand.id))
        demands.sort()
        return [demand_id for _, demand_id in demands]

    def _upper_bound(self):
        """容量松弛上界；返回目标序的值向量（忽略同池竞争）。"""
        vector, total, cover = self._relaxed(index=0)
        return {
            "total_units": total,
            "worst_group_coverage": cover[0] if cover else 1.0,
            "coverage_upper_vector": list(cover),
            "value_vector": list(vector),
            "method": "capacity_relaxation",
            "note": "忽略其他需求对同一能力池的占用，因此不低于真实最优。",
        }

    def _relaxed(self, index):
        """返回 (目标序值向量, 总件数上界, 覆盖率上界向量)。"""
        served = self._total_served
        relax_a = served
        remaining_minutes = 0.0
        rates = []
        per_group = {}
        for demand_id in self._order[index:]:
            demand = self.version.demands[demand_id]
            outcome = self._outcomes[demand_id]
            if demand.atomic:
                best = max((demand.units for o in outcome.options
                            if self._ledger.fits(o, demand.units)), default=0)
            else:
                best = min(demand.units, int(max(
                    (self._ledger.residual_units(o) for o in outcome.options),
                    default=0,
                )))
            relax_a += best
            group = (outcome.area, outcome.group)
            per_group[group] = per_group.get(group, 0) + best
        for pool_id in self._ledger.pool_cap:
            remaining_minutes += max(
                0.0, self._ledger.pool_cap[pool_id] - self._ledger.pool_used[pool_id]
            )
        for demand_id in self._order[index:]:
            for option in self._outcomes[demand_id].options:
                if option.rate > 0:
                    rates.append(option.rate)
        relax_b = served + (
            int(math.floor(remaining_minutes / min(rates) + EPS)) if rates else 0
        )
        upper_total = min(self._total_demand_units(), relax_a, max(relax_b, served))
        covers = []
        for group, info in self._groups.items():
            if info["demand_units"] <= 0:
                continue
            served_g = self._served_by_group.get(group, 0)
            reachable = min(info["demand_units"], served_g + per_group.get(group, 0))
            covers.append(reachable / info["demand_units"])
        covers.sort()
        if not covers:
            covers = [1.0]
        vector = ((float(upper_total),) + tuple(covers)
                  if self.objective == "total"
                  else tuple(covers) + (float(upper_total),))
        return vector, upper_total, covers

    def _total_demand_units(self):
        return sum(d.units for d in self.version.demands.values())

    def _bounds(self, index):
        vector, total, covers = self._relaxed(index)
        return {"total": total, "worst": covers[0], "vector": vector}

    # -- 贪心下界 -------------------------------------------------------
    def _greedy_total(self):
        for demand_id in self._order:
            self._allocate_greedy(demand_id, spill=True)

    def _greedy_worst(self):
        """轮流向覆盖率最低的群体分配，得到较好的公平下界。"""
        pending = set(self._order)
        guard = 0
        while pending and guard < 10_000:
            guard += 1
            group_cov = {}
            for group, info in self._groups.items():
                if info["demand_units"]:
                    group_cov[group] = self._served_by_group[group] / info["demand_units"]
            progress = False
            for group in sorted(group_cov, key=lambda g: (group_cov[g], g)):
                candidates = [d for d in sorted(pending)
                              if (self._outcomes[d].area, self._outcomes[d].group) == group]
                for demand_id in candidates:
                    if self._allocate_greedy(demand_id, spill=False, one_unit=True):
                        progress = True
                        if self._outcomes[demand_id].fully_served:
                            pending.discard(demand_id)
                        break
                if progress:
                    break
            if not progress:
                break

    def _allocate_greedy(self, demand_id, spill=True, one_unit=False):
        demand = self.version.demands[demand_id]
        outcome = self._outcomes[demand_id]
        options = sorted(outcome.options, key=lambda o: (o.rate, o.travel_minutes, o.site_id))
        if demand.atomic:
            for option in options:
                if self._ledger.fits(option, demand.units):
                    self._commit(demand_id, option, demand.units)
                    return True
            return False
        remaining = demand.units
        for option in options:
            if remaining <= 0:
                break
            capacity = self._ledger.residual_units(option)
            if capacity <= 0:
                continue
            take = min(remaining, int(capacity))
            if one_unit:
                take = min(take, 1)
            if take <= 0:
                continue
            self._commit(demand_id, option, take)
            remaining -= take
            if not spill:
                return True
        return remaining < demand.units

    def _commit(self, demand_id, option, units):
        outcome = self._outcomes[demand_id]
        self._ledger.spend(option, units)
        outcome.served += units
        self._total_served += units
        group = (outcome.area, outcome.group)
        self._served_by_group[group] += units
        outcome.allocations.append(Allocation(
            demand_id=demand_id, site_id=option.site_id, units=units,
            minutes=units * option.rate, travel_minutes=option.travel_minutes,
            path=option.path,
            cross_boundary=self._cross_boundary(option.site_id, outcome.area),
        ))

    def _cross_boundary(self, site_id, resident_area):
        site_area = self.version.sites[site_id].area
        return bool(site_area) and bool(resident_area) and site_area != resident_area

    def _snapshot(self):
        return {did: [(a.site_id, a.units) for a in o.allocations]
                for did, o in self._outcomes.items()}

    def _value(self):
        total = float(self._total_served)
        vector = coverage_vector(self._groups, self._served_by_group)
        return (total,) + vector if self.objective == "total" else vector + (total,)

    def _record(self):
        value = self._value()
        if self._best is None or _better(value, self._best[0]):
            self._best = (value, self._snapshot())
            return True
        return False

    def _reset_state(self):
        for outcome in self._outcomes.values():
            for alloc in outcome.allocations:
                option = next(o for o in outcome.options if o.site_id == alloc.site_id)
                self._ledger.unspend(option, alloc.units)
            outcome.served = 0
            outcome.allocations = []
        self._total_served = 0
        self._served_by_group = {g: 0 for g in self._groups}

    # -- 精确搜索 -------------------------------------------------------
    def _dfs(self, index):
        self._nodes += 1
        if self._nodes > self.budget_nodes or time.monotonic() > self.deadline:
            self._aborted = True
            return
        if index >= len(self._order):
            self._record()
            return
        bounds = self._bounds(index)
        if not self._can_improve(bounds):
            return
        demand_id = self._order[index]
        demand = self.version.demands[demand_id]
        outcome = self._outcomes[demand_id]

        # 不服务
        self._dfs(index + 1)
        if self._aborted:
            return

        if demand.atomic:
            for option in outcome.options:
                if not self._ledger.fits(option, demand.units):
                    continue
                self._commit(demand_id, option, demand.units)
                self._dfs(index + 1)
                self._uncommit(demand_id, option, demand.units)
                if self._aborted:
                    return
            return

        # 可分：把 units 分成若干份给候选网点；按枚举顺序避免重复排列。
        self._split(index, demand_id, 0, demand.units)

    def _split(self, index, demand_id, option_index, remaining):
        outcome = self._outcomes[demand_id]
        options = outcome.options
        if option_index >= len(options):
            self._dfs(index + 1)
            return
        option = options[option_index]
        capacity = self._ledger.residual_units(option)
        max_take = remaining if capacity == INF else min(remaining, int(capacity))
        for units in range(max_take, -1, -1):
            if units:
                self._commit(demand_id, option, units)
            self._split(index, demand_id, option_index + 1, remaining - units)
            if units:
                self._uncommit(demand_id, option, units)
            if self._aborted:
                return

    def _uncommit(self, demand_id, option, units):
        outcome = self._outcomes[demand_id]
        self._ledger.unspend(option, units)
        outcome.served -= units
        self._total_served -= units
        self._served_by_group[(outcome.area, outcome.group)] -= units
        outcome.allocations.pop()

    def _can_improve(self, bounds):
        if self._best is None:
            return True
        return _lex_better(bounds["vector"], self._best[0])

    # -- 结果整理 -------------------------------------------------------
    def _materialise(self, outcomes):
        if self._best is None:
            # 空解永远可行，用它作为最后兜底。
            self._best = (self._value(), {})
        _, allocations = self._best
        self._reset_state()
        ledger = Ledger(self.version, {k: o.options for k, o in outcomes.items()})
        for demand_id, site_units in allocations.items():
            outcome = outcomes[demand_id]
            demand = self.version.demands[demand_id]
            for site_id, units in site_units:
                option = next(o for o in outcome.options if o.site_id == site_id)
                ledger.spend(option, units)
                outcome.served += units
                outcome.allocations.append(Allocation(
                    demand_id=demand_id, site_id=site_id, units=units,
                    minutes=units * option.rate,
                    travel_minutes=option.travel_minutes, path=option.path,
                    cross_boundary=self._cross_boundary(site_id, outcome.area),
                ))
        self._ledger = ledger
        for demand_id, outcome in outcomes.items():
            demand = self.version.demands[demand_id]
            if not outcome.fully_served:
                outcome.reason = self._unserved_reason(demand, outcome)
                outcome.constraint_resources = self._constraints(demand, outcome)

    def _unserved_reason(self, demand, outcome):
        """未服务原因按可核查的顺序判定。

        以事项为粒度：同一事项的部分件未服务，若仍有候选网点且其能力
        已被占满，就是容量竞争；若所有候选的整周上限都装不下这一单，
        才是单点能力不足。地理原因（无路、超门槛、无资质、无时段交集）
        已在可达阶段判定，不在容量阶段重复归因。
        """
        if outcome.reach_status != "reachable":
            return outcome.reach_reason or "unreachable"
        if not outcome.options:
            return "qualification"
        need = demand.units if demand.atomic else 1
        best_fit = max(
            (min(self._ledger.pool_cap[o.pool_id], self._ledger.site_cap[o.site_id])
             / o.rate if o.rate > 0 else INF)
            for o in outcome.options
        )
        if best_fit + EPS < need:
            return "unit_too_large_for_capacity"
        return "capacity"

    def _constraints(self, demand, outcome):
        resources = self._ledger.blocking_resources(outcome.options, demand)
        if not resources:
            if outcome.reach_status != "reachable":
                resources.append({"resource": "geography", "kind": "geography",
                                  "reason": outcome.reach_reason})
            else:
                resources.append({"resource": "none", "kind": "model",
                                  "note": "容量尚有余量但仍被更优解放弃"})
        return _dedupe(resources)


def _dedupe(items):
    seen = set()
    result = []
    for item in items:
        key = tuple(sorted((k, str(v)) for k, v in item.items()))
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def _lex_better(new, old):
    """字典序严格更优：逐项比较，首个不同的项决定胜负。"""
    for a, b in zip(new, old):
        if a > b + EPS:
            return True
        if a < b - EPS:
            return False
    return len(new) > len(old)


def _better(new, old):
    return _lex_better(new, old)


# ---------------------------------------------------------------------------
# 结果与报告
# ---------------------------------------------------------------------------

class AnalysisResult:
    def __init__(self, version, objective, reaches, outcomes, ledger, geo=None,
                 status="exact", nodes=0, budget_nodes=0, upper_bound=None,
                 time_budget_s=None):
        self._groups_snapshot = {
            (o.area, o.group): {"demand_units": 0} for o in outcomes.values()
        }
        for outcome in outcomes.values():
            self._groups_snapshot[(outcome.area, outcome.group)]["demand_units"] += outcome.units
        self.version = version
        self.objective = objective
        self.reaches = reaches
        self.outcomes = outcomes
        self.ledger = ledger
        self.geo = geo
        self.status = status
        self.nodes = nodes
        self.budget_nodes = budget_nodes
        self.upper_bound = upper_bound or {}
        self.time_budget_s = time_budget_s

    @property
    def proven_optimal(self):
        return self.status == "exact"

    # -- 分组与指标 -----------------------------------------------------
    def group_totals(self):
        groups = {}
        for outcome in self.outcomes.values():
            key = (outcome.area, outcome.group)
            item = groups.setdefault(key, {
                "area": outcome.area, "group": outcome.group,
                "demand_units": 0, "served_units": 0,
                "reachable_units": 0,
                "people": 0, "covered_people": 0,
                "residents": set(), "services": {},
                "sites": set(), "cross_boundary_units": 0,
                "unserved_reasons": {},
            })
            item["demand_units"] += outcome.units
            item["served_units"] += outcome.served
            if outcome.reach_status == "reachable":
                item["reachable_units"] += outcome.units
            item["residents"].add(outcome.resident_id)
            item["sites"].update(a.site_id for a in outcome.allocations)
            item["cross_boundary_units"] += sum(
                a.units for a in outcome.allocations if a.cross_boundary
            )
            service = item["services"].setdefault(
                outcome.service,
                {"demand_units": 0, "served_units": 0, "demands": 0,
                 "served_demands": 0},
            )
            service["demand_units"] += outcome.units
            service["served_units"] += outcome.served
            service["demands"] += 1
            service["served_demands"] += 1 if outcome.fully_served else 0
            if not outcome.fully_served:
                reason = outcome.reason or outcome.reach_reason or "unknown"
                item["unserved_reasons"][reason] = (
                    item["unserved_reasons"].get(reason, 0) + outcome.unserved
                )
        people = self.people_entries()
        for entry in people.values():
            key = (entry["area"], entry["group"])
            if key in groups:
                groups[key]["people"] += entry["people"]
                groups[key]["covered_people"] += entry["people"] if entry["fully_served"] else 0
        result = []
        for key in sorted(groups):
            item = groups[key]
            item["residents"] = sorted(item["residents"])
            item["sites"] = sorted(item["sites"])
            item["coverage"] = _ratio(item["served_units"], item["demand_units"])
            item["reachable_coverage"] = _ratio(item["reachable_units"], item["demand_units"])
            item["people_coverage"] = _ratio(item["covered_people"], item["people"])
            for service in item["services"].values():
                service["coverage"] = _ratio(service["served_units"], service["demand_units"])
            item["unserved_reasons"] = dict(sorted(
                item["unserved_reasons"].items(), key=lambda kv: (-kv[1], kv[0])
            ))
            result.append(item)
        return result

    def people_entries(self):
        """按人汇总；只有全部事项都办成才算人口覆盖。"""
        entries = {}
        for resident_id, resident in self.version.residents.items():
            demands = self.version.demands_of(resident_id)
            if not demands:
                continue
            served_units = sum(self.outcomes[d.id].served for d in demands)
            full = all(self.outcomes[d.id].fully_served for d in demands)
            entry = entries.setdefault(resident.person, {
                "person": resident.person,
                "residents": [], "areas": set(), "groups": set(),
                "age": resident.age, "people": 0,
                "fully_served": True,
                "demand_units": 0, "served_units": 0,
            })
            entry["residents"].append(resident_id)
            entry["areas"].add(resident.area)
            entry["groups"].add(resident.group)
            entry["people"] += resident.people
            entry["fully_served"] = entry["fully_served"] and full
            entry["demand_units"] += sum(d.units for d in demands)
            entry["served_units"] += served_units
        result = []
        for entry in entries.values():
            entry["residents"] = sorted(entry["residents"])
            entry["areas"] = sorted(entry["areas"])
            entry["groups"] = sorted(entry["groups"])
            entry["area"] = entry["areas"][0] if len(entry["areas"]) == 1 else "、".join(entry["areas"])
            entry["group"] = entry["groups"][0] if len(entry["groups"]) == 1 else "、".join(entry["groups"])
            entry["coverage"] = _ratio(entry["served_units"], entry["demand_units"])
            result.append(entry)
        return {entry["person"]: entry for entry in result}

    def totals(self):
        demand_units = sum(o.units for o in self.outcomes.values())
        served = sum(o.served for o in self.outcomes.values())
        fully = sum(1 for o in self.outcomes.values() if o.fully_served)
        people = self.people_entries()
        total_people = sum(p["people"] for p in people.values())
        covered_people = sum(p["people"] for p in people.values() if p["fully_served"])
        groups = self.group_totals()
        worst = min((g["coverage"] for g in groups), default=None)
        totals = {
            "demand_records": len(self.outcomes),
            "demand_units": demand_units,
            "served_units": served,
            "unserved_units": demand_units - served,
            "unit_coverage": _ratio(served, demand_units),
            "fully_served_demands": fully,
            "people": total_people,
            "covered_people": covered_people,
            "people_coverage": _ratio(covered_people, total_people),
            "worst_group_coverage": worst,
        }
        if self.upper_bound:
            upper = self.upper_bound.get("total_units")
            totals["total_units_upper_bound"] = upper
            totals["optimality_gap_units"] = (
                None if upper is None else round(upper - served, 6)
            )
        return totals

    def reach_totals(self):
        if self.geo is not None:
            return self.geo.summary(self.reaches)
        return Geographic(self.version).summary(self.reaches)

    def fairness(self):
        groups = self.group_totals()
        if not groups:
            return {"objective": self.objective, "worst_group": None, "groups": {}}
        worst = min(groups, key=lambda g: (g["coverage"], g["area"], g["group"]))
        return {
            "objective": self.objective,
            "worst_group": {"area": worst["area"], "group": worst["group"],
                            "coverage": worst["coverage"]},
            "groups": {f"{g['area']}/{g['group']}": g["coverage"] for g in groups},
        }

    def as_dict(self):
        return {
            "version": self.version.version_id,
            "version_created": self.version.created,
            "objective": self.objective,
            "solution_status": self.status,
            "proven_optimal": self.proven_optimal,
            "search": {
                "nodes": self.nodes,
                "budget_nodes": self.budget_nodes,
                "time_budget_seconds": self.time_budget_s,
                "upper_bound": self.upper_bound,
            },
            "geographic_reach": self.reach_totals(),
            "totals": self.totals(),
            "fairness": self.fairness(),
            "groups": self.group_totals(),
            "people": sorted(self.people_entries().values(),
                             key=lambda p: (p["area"], p["group"], p["person"])),
            "resources": self.ledger.snapshot(),
            "demands": [self.outcomes[k].as_dict() for k in sorted(self.outcomes)],
            "notes": list(self.version.notes) + _model_notes(self.version),
        }


def _model_notes(version):
    from .model import validate_model
    return validate_model(version)


# ---------------------------------------------------------------------------
# 独立枚举核对（小网）
# ---------------------------------------------------------------------------

def _has_options(version, demand):
    """粗判该事项是否可能有候选网点，避免枚举器把不可达事项计入规模。"""
    return any(demand.service in site.services and site.site_active
               for site in version.sites.values())


def exhaustive_optimum(version: Version, objective="total", departure_minute=480,
                       max_demands=24, max_options=8):
    """不做任何界剪枝的独立枚举，用于验证小网的“最优”声明。

    可分需求逐件枚举，整单需求整单枚举；总状态数受 max_demands 与
    max_options 限制。返回 (best_value, AnalysisResult)。
    """
    if len([d for d in version.demands.values() if _has_options(version, d)]) > max_demands:
        raise ValueError(f"独立枚举仅用于小网：需分配事项数超过{max_demands}")
    geo = Geographic(version, departure_minute)
    reaches = geo.all()
    allocator = Allocator(version, objective, budget_nodes=10**9,
                          time_budget_s=10**9, departure_minute=departure_minute)
    reaches, outcomes, groups = allocator.build(reaches)
    for outcome in outcomes.values():
        if len(outcome.options) > max_options:
            raise ValueError(f"独立枚举仅用于小网：候选网点数超过{max_options}")
    allocator._reaches = reaches
    allocator._outcomes = outcomes
    allocator._groups = groups
    allocator._order = allocator._search_order()
    allocator._ledger = Ledger(version, {k: o.options for k, o in outcomes.items()})
    allocator._served_by_group = {g: 0 for g in groups}
    allocator._total_served = 0
    best = {"value": None, "alloc": None}

    def record():
        value = allocator._value()
        if best["value"] is None or _better(value, best["value"]):
            best["value"] = value
            best["alloc"] = allocator._snapshot()

    def dfs(index):
        if index >= len(allocator._order):
            record()
            return
        demand_id = allocator._order[index]
        demand = version.demands[demand_id]
        outcome = outcomes[demand_id]
        dfs(index + 1)  # 不服务
        if demand.atomic:
            for option in outcome.options:
                if allocator._ledger.fits(option, demand.units):
                    allocator._commit(demand_id, option, demand.units)
                    dfs(index + 1)
                    allocator._uncommit(demand_id, option, demand.units)
        else:
            def split(option_index, remaining):
                if option_index >= len(outcome.options):
                    dfs(index + 1)
                    return
                option = outcome.options[option_index]
                capacity = allocator._ledger.residual_units(option)
                max_take = remaining if capacity == INF else min(
                    remaining, int(math.floor(capacity + EPS))
                )
                for units in range(max_take, -1, -1):
                    if units:
                        allocator._commit(demand_id, option, units)
                    split(option_index + 1, remaining - units)
                    if units:
                        allocator._uncommit(demand_id, option, units)
            split(0, demand.units)

    dfs(0)
    allocator._best = (best["value"], best["alloc"])
    allocator._materialise(outcomes)
    result = AnalysisResult(
        version=version, objective=objective, reaches=reaches,
        outcomes=outcomes, ledger=allocator._ledger, geo=geo,
        status="exact_enumeration", nodes=0, budget_nodes=0,
    )
    return best["value"], result
