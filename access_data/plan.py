"""候选调整方案：把“延长开放时间 / 共享办理能力 / 增设服务点 / 修路”
表达为对资料版本的显式修改，并强制每个方案整体重算。

组合方案不能把各自的改善量相加：共享人员池、门槛与路径会相互作用。
``evaluate_plans`` 始终对每个方案（单独或组合）重新求解，并输出
“谁得到帮助、谁被挤占”，以及公平提升相对总量代价的交换关系。
"""
from __future__ import annotations

from dataclasses import dataclass, replace

from .access import Allocator, AnalysisResult
from .model import Pool, Site, Version, parse_hours

KINDS = ("open_hours", "shared_capacity", "new_site", "new_road", "new_pool_capacity")
KIND_LABELS = {
    "open_hours": "延长开放时间",
    "shared_capacity": "共享办理能力",
    "new_site": "增设服务点",
    "new_road": "增加道路/桥梁",
    "new_pool_capacity": "增加人员能力",
}


def _clone(version: Version, **changes):
    return replace(version, **changes)


@dataclass(frozen=True)
class Adjustment:
    """一个可实施调整。``kind`` 决定其余字段的含义。"""

    id: str
    kind: str
    description: str = ""

    # open_hours
    site_id: str = ""
    open_hours: tuple = ()          # 追加的开放时段 [{"day","start","end"}]

    # shared_capacity / new_pool_capacity
    pool_id: str = ""
    extra_minutes: float = 0.0
    merge_pools: tuple = ()          # 共享能力：把这些池并入 pool_id
    site_ids: tuple = ()             # 或指定网点改挂到 pool_id

    # new_site / new_road 的登记 id；缺省沿用调整 id
    target_id: str = ""
    node: str = ""
    service: str = ""
    minutes_per_unit: float = 0.0
    weekly_minutes: float | None = None
    area: str = ""

    # new_road
    source: str = ""
    target: str = ""
    mode: str = "walk"
    minutes: float = 0.0
    one_way: bool = False
    name: str = ""

    def apply(self, version: Version) -> Version:
        if self.kind == "open_hours":
            return self._apply_hours(version)
        if self.kind == "shared_capacity":
            return self._apply_shared(version)
        if self.kind == "new_pool_capacity":
            return self._apply_pool_capacity(version)
        if self.kind == "new_site":
            return self._apply_new_site(version)
        if self.kind == "new_road":
            return self._apply_new_road(version)
        raise ValueError(f"未知调整类型：{self.kind}")

    def _apply_hours(self, version):
        if self.site_id not in version.sites:
            raise ValueError(f"调整{self.id}引用不存在的网点{self.site_id}")
        site = version.sites[self.site_id]
        extra = parse_hours(list(self.open_hours), f"调整{self.id}")
        sites = dict(version.sites)
        sites[self.site_id] = replace(site, open_hours=site.open_hours | extra)
        return _clone(version, sites=sites)

    def _apply_shared(self, version):
        """共享办理能力：把指定网点改挂到同一人员池，或合并若干池。

        合并按“只取一次”原则：合并后的容量是各池容量之和——它们本来就是
        各自的人员预算；关键是让多个网点共用同一池，从而不会再出现
        “每个窗口都号称有余量、实际人员只有一组”的重复计数。
        """
        pools = dict(version.pools)
        sites = dict(version.sites)
        if self.site_ids:
            if self.pool_id not in pools:
                raise ValueError(f"调整{self.id}引用不存在的池{self.pool_id}")
            for site_id in self.site_ids:
                if site_id not in sites:
                    raise ValueError(f"调整{self.id}引用不存在的网点{site_id}")
                sites[site_id] = replace(sites[site_id], pool=self.pool_id)
        if self.merge_pools:
            total = 0.0
            names = []
            for pool_id in self.merge_pools:
                if pool_id not in pools:
                    raise ValueError(f"调整{self.id}引用不存在的池{pool_id}")
                total += pools[pool_id].weekly_minutes
                names.append(pools[pool_id].name)
                del pools[pool_id]
            target = self.pool_id
            for site_id, site in sites.items():
                if site.pool in self.merge_pools:
                    sites[site_id] = replace(site, pool=target)
            if target in pools:
                total += pools[target].weekly_minutes
            pools[target] = Pool(target, total, "、".join(n for n in names if n))
        if self.extra_minutes:
            if self.pool_id not in pools:
                raise ValueError(f"调整{self.id}引用不存在的池{self.pool_id}")
            pool = pools[self.pool_id]
            pools[self.pool_id] = replace(
                pool, weekly_minutes=pool.weekly_minutes + self.extra_minutes
            )
        return _clone(version, sites=sites, pools=pools)

    def _apply_pool_capacity(self, version):
        pools = dict(version.pools)
        if self.pool_id not in pools:
            raise ValueError(f"调整{self.id}引用不存在的池{self.pool_id}")
        pools[self.pool_id] = replace(
            pools[self.pool_id],
            weekly_minutes=pools[self.pool_id].weekly_minutes + self.extra_minutes,
        )
        return _clone(version, pools=pools)

    def _apply_new_site(self, version):
        if self.node not in version.nodes:
            raise ValueError(f"调整{self.id}的服务点节点{self.node}不在路网中")
        if self.pool_id not in version.pools:
            raise ValueError(f"调整{self.id}引用不存在的池{self.pool_id}")
        if not self.service:
            raise ValueError(f"调整{self.id}缺少事项")
        site_id = self.target_id or self.id
        if site_id in version.sites:
            raise ValueError(f"调整{self.id}要新增的网点{site_id}已存在")
        sites = dict(version.sites)
        sites[site_id] = Site(
            id=site_id, node=self.node, services=(self.service,),
            pool=self.pool_id, minutes_per_unit=self.minutes_per_unit,
            open_hours=parse_hours(list(self.open_hours), f"调整{self.id}")
            if self.open_hours else frozenset(range(7 * 24)),
            weekly_minutes=self.weekly_minutes, area=self.area,
            name=self.name or site_id,
        )
        return _clone(version, sites=sites)

    def _apply_new_road(self, version):
        from .model import Road
        road_id = self.target_id or self.id
        if road_id in version.roads:
            raise ValueError(f"调整{self.id}要新增的道路{road_id}已存在")
        roads = dict(version.roads)
        roads[road_id] = Road(
            id=road_id, source=self.source, target=self.target, mode=self.mode,
            minutes=self.minutes, one_way=self.one_way, name=self.name or road_id,
        )
        return _clone(version, roads=roads)


@dataclass
class Plan:
    id: str
    label: str
    adjustments: tuple[Adjustment, ...] = ()
    note: str = ""

    def apply(self, version: Version) -> Version:
        result = version
        for adjustment in self.adjustments:
            result = adjustment.apply(result)
        return _clone(result, version_id=f"{version.version_id}+{self.id}",
                      meta={**version.meta, "plan": self.id,
                            "plan_label": self.label,
                            "base_version": version.version_id})

    def as_dict(self):
        return {
            "plan": self.id,
            "label": self.label,
            "note": self.note,
            "adjustments": [
                {"id": a.id, "kind": a.kind,
                 "kind_label": KIND_LABELS.get(a.kind, a.kind),
                 "description": a.description}
                for a in self.adjustments
            ],
        }


@dataclass
class PlanOutcome:
    plan: Plan
    version: Version
    objective: str
    result: AnalysisResult
    baseline: AnalysisResult | None = None

    def deltas(self):
        if self.baseline is None:
            return {}
        base = self.baseline
        after = self.result
        base_totals = base.totals()
        after_totals = after.totals()
        people_before = base.people_entries()
        people_after = after.people_entries()
        helped = []
        displaced = []
        for person in sorted(set(people_before) | set(people_after)):
            before = people_before.get(person)
            after_entry = people_after.get(person)
            before_served = before["served_units"] if before else 0
            after_served = after_entry["served_units"] if after_entry else 0
            change = after_served - before_served
            if change > 0:
                helped.append({
                    "person": person, "area": after_entry["area"],
                    "group": after_entry["group"],
                    "served_units_before": before_served,
                    "served_units_after": after_served,
                    "delta": change,
                })
            elif change < 0:
                displaced.append({
                    "person": person, "area": (after_entry or before)["area"],
                    "group": (after_entry or before)["group"],
                    "served_units_before": before_served,
                    "served_units_after": after_served,
                    "delta": change,
                })
        group_changes = []
        base_groups = {(g["area"], g["group"]): g for g in base.group_totals()}
        after_groups = {(g["area"], g["group"]): g for g in after.group_totals()}
        for key in sorted(set(base_groups) | set(after_groups)):
            before = base_groups.get(key, {"served_units": 0, "demand_units": 0, "coverage": 0.0})
            after_entry = after_groups.get(key, {"served_units": 0, "demand_units": 0, "coverage": 0.0})
            group_changes.append({
                "area": key[0], "group": key[1],
                "served_before": before["served_units"],
                "served_after": after_entry["served_units"],
                "coverage_before": before["coverage"],
                "coverage_after": after_entry["coverage"],
                "delta_units": after_entry["served_units"] - before["served_units"],
                "delta_coverage": round((after_entry["coverage"] or 0) - (before["coverage"] or 0), 6),
            })
        return {
            "served_units": after_totals["served_units"] - base_totals["served_units"],
            "people_coverage": round(
                (after_totals["people_coverage"] or 0) - (base_totals["people_coverage"] or 0), 6
            ),
            "worst_group_coverage": round(
                (after_totals["worst_group_coverage"] or 0)
                - (base_totals["worst_group_coverage"] or 0), 6
            ),
            "helped": helped,
            "displaced": displaced,
            "groups": group_changes,
            "constraint_resources": [
                r for d in after.as_dict()["demands"]
                for r in d["constraint_resources"]
            ],
        }

    def as_dict(self):
        data = {
            "plan": self.plan.as_dict(),
            "version": self.version.version_id,
            "objective": self.objective,
            "status": self.result.status,
            "totals": self.result.totals(),
            "geographic_reach": self.result.reach_totals(),
            "groups": self.result.group_totals(),
            "resources": self.result.ledger.snapshot(),
        }
        if self.baseline is not None:
            data["delta"] = self.deltas()
        return data


def evaluate_plan(version, plan, objective="total", baseline=None,
                  budget_nodes=300000):
    applied = plan.apply(version)
    result = Allocator(applied, objective, budget_nodes=budget_nodes).solve()
    return PlanOutcome(plan=plan, version=applied, objective=objective,
                       result=result, baseline=baseline)


def evaluate_plans(version, plans, objective="total", budget_nodes=300000):
    """对每个方案整体重算，返回基线、各个方案与组合方案的对照。"""
    baseline = Allocator(version, objective, budget_nodes=budget_nodes).solve()
    outcomes = {}
    for plan in plans:
        outcomes[plan.id] = evaluate_plan(
            version, plan, objective, baseline, budget_nodes
        )
    combined = _combined_plan(plans)
    if combined is not None:
        outcomes[combined.id] = evaluate_plan(
            version, combined, objective, baseline, budget_nodes
        )
    return baseline, outcomes


def _combined_plan(plans):
    if len(plans) < 2:
        return None
    adjustments = [a for plan in plans for a in plan.adjustments]
    # 组合里若有两个共享能力调整指向同一个池并各自合并不同池，顺序会改变
    # 结果；这属于方案设计冲突，必须显式拒绝而不是悄悄按字典序取一个。
    merges = {}
    for adjustment in adjustments:
        if adjustment.kind != "shared_capacity" or not adjustment.merge_pools:
            continue
        key = adjustment.pool_id
        merges.setdefault(key, []).append(tuple(sorted(adjustment.merge_pools)))
    for target, values in merges.items():
        if len(values) > 1:
            raise ValueError(
                f"组合方案中池{target}被多个共享能力调整重复合并：{values}；"
                "请先合并为一个调整，避免顺序决定结果。"
            )
    return Plan(
        id="combined", label="全部候选调整同时实施",
        adjustments=tuple(adjustments),
        note="组合方案整体重算；其效果不等于各方案单项效果相加。",
    )


def pareto_frontier(outcomes):
    """按 (总服务量, 最差群体覆盖率) 标出非受支配方案。"""
    points = []
    for outcome in outcomes.values():
        totals = outcome.result.totals()
        points.append({
            "plan": outcome.plan.id,
            "label": outcome.plan.label,
            "objective": outcome.objective,
            "served_units": totals["served_units"],
            "worst_group_coverage": totals["worst_group_coverage"],
            "people_coverage": totals["people_coverage"],
        })
    frontier = []
    for point in points:
        dominated = any(
            other["served_units"] >= point["served_units"]
            and (other["worst_group_coverage"] or 0) >= (point["worst_group_coverage"] or 0)
            and (other["served_units"] > point["served_units"]
                 or (other["worst_group_coverage"] or 0) > (point["worst_group_coverage"] or 0))
            for other in points
        )
        if not dominated:
            frontier.append(point)
    return frontier


def additivity_check(version, plans, objective="total"):
    """验证“组合不等于单项相加”，并量化交互项。"""
    baseline, outcomes = evaluate_plans(version, plans, objective)
    if "combined" not in outcomes:
        return {"checked": False, "reason": "少于两个方案"}
    base_served = baseline.totals()["served_units"]
    singles = {}
    for plan in plans:
        outcome = outcomes[plan.id]
        singles[plan.id] = outcome.result.totals()["served_units"] - base_served
    combined_delta = outcomes["combined"].result.totals()["served_units"] - base_served
    return {
        "checked": True,
        "baseline_served_units": base_served,
        "single_deltas": singles,
        "sum_of_singles": sum(singles.values()),
        "combined_delta": combined_delta,
        "interaction": combined_delta - sum(singles.values()),
        "equal": abs(combined_delta - sum(singles.values())) < 1e-9,
    }


def search_candidates(version, candidate_plans, objective="total", budget_nodes=300000):
    """候选方案搜索：逐一评估并可回溯比较；返回按目标排序的结果。

    这里刻意不做“贪心叠加”：每个候选与组合都从基线整体求解，因此解
    始终满足全部能力池、事项兼容与出行预算约束。
    """
    baseline, outcomes = evaluate_plans(version, candidate_plans, objective, budget_nodes)
    ranked = sorted(
        outcomes.values(),
        key=lambda o: (
            -(o.result.totals()["served_units"] or 0),
            -((o.result.totals()["worst_group_coverage"] or 0)),
            o.plan.id,
        ),
    )
    return baseline, ranked, outcomes
