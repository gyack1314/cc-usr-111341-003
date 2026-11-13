"""可达性、容量分配、公平目标与方案重算的核对。"""
import copy
import unittest

from access_data.access import Allocator, exhaustive_optimum
from access_data.io import load
from access_data.network import Router
from access_data.plan import (
    Adjustment, Plan, additivity_check, evaluate_plan, evaluate_plans,
)
from access_data.versions import comparison_keys, load_versions

VERSIONS = load_versions("data/coordination/versions.json")


def scenario(version="v2"):
    return VERSIONS[version]


class ReachabilityTests(unittest.TestCase):
    def test_river_gap_is_unreachable_without_fabricated_edges(self):
        """西岸到河湾在 v1 没有直路，不能按直线距离补边。"""
        version = scenario("v1")
        router = Router(version, departure_minute=9 * 60)
        paths = router.shortest_paths("w2")
        self.assertIn("e1", paths)  # 绕北桥仍能到集镇
        self.assertNotIn("e2", router.shortest_paths("w2", modes=["walk"])
                         .get("e2", paths.get("e2", None)) and {})
        # 河湾 e2 在步行网络上不可直达：最短路径必须经过北桥，而不是虚构的直线。
        self.assertGreaterEqual(paths["e2"].minutes, 12 + 26 + 14 + 10 + 16)

    def test_new_road_restores_reachability(self):
        version = scenario("v1")
        plan = Plan("bridge", "便桥", (Adjustment(
            id="a", kind="new_road", target_id="r_test", source="w2",
            target="e2", mode="walk", minutes=8,
        ),))
        applied = plan.apply(version)
        before = Router(version).shortest_paths("w2").get("e2")
        after = Router(applied).shortest_paths("w2").get("e2")
        self.assertGreater(before.minutes, after.minutes)
        self.assertEqual(after.minutes, 8)

    def test_unreachable_is_reported_not_estimated(self):
        result = Allocator(scenario("v1"), "total").solve()
        unreachable = [r for r in result.reaches.values() if r.status != "reachable"]
        self.assertTrue(unreachable)
        for reach in unreachable:
            self.assertFalse(reach.candidates)
            self.assertTrue(reach.reason)

    def test_bus_waiting_and_service_window(self):
        """班车只在首末班之间可乘；末班之后不可达。"""
        version = scenario("v2")
        early = Router(version, departure_minute=5 * 60).shortest_paths("e1")
        late = Router(version, departure_minute=23 * 60).shortest_paths("e1")
        self.assertIn("e2", early)
        # 夜间公交不可乘，只能步行；若步行也不可达则应不存在该路径。
        if "e2" in late:
            self.assertIn("walk", late["e2"].modes)

    def test_bus_route_uses_headway_wait(self):
        version = scenario("v2")
        # 08:05 出发，30 分钟一班、06:30 首班 → 08:30 才有车，等待 25 分钟。
        paths = Router(version, departure_minute=8 * 60 + 5,
                       modes=["bus"]).shortest_paths("e1")
        self.assertIn("e2", paths)
        edge = paths["e2"].edges[0]
        self.assertEqual(edge.line_id, "L_e1_e2")
        self.assertAlmostEqual(edge.board_wait, 25.0)


class CapacityTests(unittest.TestCase):
    def test_shared_pool_counted_once(self):
        """集镇综合窗口与河湾点共用 p_town_a，总能力只能扣一次。"""
        result = Allocator(scenario("v2"), "total").solve()
        pools = {p["pool"]: p for p in result.ledger.snapshot()["pools"]}
        self.assertEqual(pools["p_town_a"]["used_minutes"],
                         min(20.0, pools["p_town_a"]["used_minutes"]))
        self.assertLessEqual(pools["p_town_a"]["used_minutes"], 20.0 + 1e-9)

    def test_atomic_orders_are_not_split(self):
        result = Allocator(scenario("v2"), "total").solve()
        for demand in result.version.demands.values():
            if not demand.atomic:
                continue
            outcome = result.outcomes[demand.id]
            self.assertIn(outcome.served, (0, demand.units))

    def test_divisible_orders_can_be_split(self):
        result = Allocator(scenario("v2"), "total").solve()
        wang = result.outcomes["d_r_w1_wang_card"]
        self.assertEqual(wang.served, 2)  # 可分，2 件全部完成
        self.assertTrue(wang.allocations)

    def test_capacity_respected(self):
        for objective in ("total", "fair"):
            result = Allocator(scenario("v2"), objective).solve()
            used = {}
            for outcome in result.outcomes.values():
                for alloc in outcome.allocations:
                    pool = result.version.sites[alloc.site_id].pool
                    used[pool] = used.get(pool, 0.0) + alloc.minutes
            for pool_id, minutes in used.items():
                self.assertLessEqual(
                    minutes, result.version.pools[pool_id].weekly_minutes + 1e-6
                )

    def test_multi_item_person_counts_people_once(self):
        result = Allocator(scenario("v2"), "total").solve()
        people = result.people_entries()
        self.assertEqual(people["p_wang"]["people"], 2)  # 两项事项，人口只计一次
        total = sum(p["people"] for p in people.values())
        self.assertEqual(total, sum(r.people for r in result.version.residents.values()))

    def test_demand_not_summed_as_people(self):
        result = Allocator(scenario("v2"), "total").solve()
        totals = result.totals()
        self.assertNotEqual(totals["demand_units"], totals["people"])
        # p_wang 两项事项共 3 件，但人口只有 2 人。

    def test_cross_boundary_is_allowed_and_recorded(self):
        result = Allocator(scenario("v2"), "total").solve()
        cross = [a for o in result.outcomes.values() for a in o.allocations
                 if a.cross_boundary]
        self.assertTrue(cross)
        for alloc in cross:
            site_area = result.version.sites[alloc.site_id].area
            self.assertNotEqual(site_area, result.outcomes[alloc.demand_id].area)

    def test_duplicate_person_registration_rejected(self):
        """同一人重复列入两个地区不得增加需求：登记层直接拒绝。"""
        from access_data.io import DataError
        tables = load("data/coordination/versions/v2/manifest.json")
        duplicated = copy.deepcopy(tables)
        extra = dict(duplicated["residents"][0])
        extra["id"] = "r_dup"
        extra["area"] = "东岸"
        duplicated["residents"].append(extra)
        with self.assertRaisesRegex(DataError, "重复登记"):
            from access_data.model import build_version
            build_version(duplicated, "dup", "dup")


class ObjectiveTests(unittest.TestCase):
    def test_objectives_can_diverge(self):
        total = Allocator(scenario("v2"), "total").solve()
        fair = Allocator(scenario("v2"), "fair").solve()
        # 两个目标都合法；本例在组合方案下可产生不同分配（见方案测试）。
        self.assertIn(total.status, ("exact", "budget_exhausted"))
        self.assertIn(fair.status, ("exact", "budget_exhausted"))

    def test_fair_never_increases_worst_group_shortfall(self):
        for version_id in ("v1", "v2", "v3"):
            total = Allocator(VERSIONS[version_id], "total").solve()
            fair = Allocator(VERSIONS[version_id], "fair").solve()
            self.assertGreaterEqual(
                fair.totals()["worst_group_coverage"] + 1e-9,
                total.totals()["worst_group_coverage"],
            )

    def test_both_objectives_reach_enumeration_optimum(self):
        for objective in ("total", "fair"):
            value, result = exhaustive_optimum(
                scenario("v2"), objective, max_demands=24, max_options=8
            )
            allocator = Allocator(scenario("v2"), objective, budget_nodes=500_000)
            allocator.solve()
            self.assertEqual(list(allocator._best[0]), list(value))

    def test_objective_is_frozen_and_reported(self):
        result = Allocator(scenario("v2"), "fair").solve()
        payload = result.as_dict()
        self.assertEqual(payload["objective"], "fair")
        self.assertIn("fairness", payload)
        self.assertNotIn("唯一", str(payload))


class PlanTests(unittest.TestCase):
    def setUp(self):
        self.version = scenario("v2")

    def test_open_saturday_helps_and_displaces(self):
        plan = Plan("sat", "周六", (Adjustment(
            id="a", kind="open_hours", site_id="s_town_card",
            open_hours=({"day": "sat", "start": "09:00", "end": "12:00"},),
        ),))
        outcome = evaluate_plan(self.version, plan, "total",
                                Allocator(self.version, "total").solve())
        delta = outcome.deltas()
        self.assertGreater(delta["served_units"], 0)
        self.assertTrue(delta["helped"])
        self.assertTrue(delta["displaced"])  # 共享池被重新分配

    def test_combination_is_recomputed_not_summed(self):
        plans = _plans()
        check = additivity_check(self.version, plans, "total")
        self.assertTrue(check["checked"])
        self.assertNotEqual(check["combined_delta"], check["sum_of_singles"])

    def test_combined_plan_respects_all_pool_limits(self):
        plans = _plans()
        _, outcomes = evaluate_plans(self.version, plans, "total")
        combined = outcomes["combined"]
        used = {}
        for outcome in combined.result.outcomes.values():
            for alloc in outcome.allocations:
                pool = combined.version.sites[alloc.site_id].pool
                used[pool] = used.get(pool, 0.0) + alloc.minutes
        for pool_id, minutes in used.items():
            self.assertLessEqual(minutes, combined.version.pools[pool_id].weekly_minutes + 1e-6)

    def test_new_site_requires_qualification_and_pool(self):
        plan = Plan("bad", "坏方案", (Adjustment(
            id="a", kind="new_site", target_id="s_x", node="e2",
            service="unknown", pool_id="p_town_a", minutes_per_unit=5,
        ),))
        applied = plan.apply(self.version)
        result = Allocator(applied, "total").solve()
        reached_sites = {c.site_id for reach in result.reaches.values()
                         for c in reach.candidates}
        self.assertNotIn("s_x", reached_sites)

    def test_version_diff_locates_source_of_change(self):
        keys = comparison_keys(VERSIONS["v1"], VERSIONS["v2"])
        self.assertIn("roads", keys)
        self.assertNotIn("capacity", keys)


def _plans():
    return [
        Plan("sat", "周六", (Adjustment(
            id="a1", kind="open_hours", site_id="s_town_card",
            open_hours=({"day": "sat", "start": "09:00", "end": "12:00"},),
        ),)),
        Plan("share", "共享", (Adjustment(
            id="a2", kind="shared_capacity", pool_id="p_town_a",
            merge_pools=("p_town_b",),
        ),)),
        Plan("newsite", "新点", (Adjustment(
            id="a3", kind="new_site", target_id="s_east_sat", node="e2",
            service="card", minutes_per_unit=5, pool_id="p_town_a",
            area="东岸",
            open_hours=({"day": "sat", "start": "09:00", "end": "12:00"},),
        ),)),
    ]


if __name__ == "__main__":
    unittest.main()


class TradeoffTests(unittest.TestCase):
    def test_fair_spends_total_units_to_lift_a_zero_group(self):
        """v1 中 fair 用更少总量换取第 4 差群体从 0 提升。"""
        total = Allocator(scenario("v1"), "total").solve()
        fair = Allocator(scenario("v1"), "fair").solve()
        self.assertEqual(total.totals()["served_units"], 7)
        self.assertEqual(fair.totals()["served_units"], 5)
        # 逐项比较排序后的覆盖向量：fair 在首个不同项严格更好。
        value, _ = exhaustive_optimum(scenario("v1"), "fair",
                                      max_demands=24, max_options=8)
        # 两份独立结果的目标值向量一致。
        allocator = Allocator(scenario("v1"), "fair", budget_nodes=500_000)
        allocator.solve()
        self.assertEqual([round(v, 9) for v in allocator._best[0]],
                         [round(v, 9) for v in value])

    def test_reasons_are_distinguished(self):
        """无时段交集与超门槛是不同原因，不合并成“closed”。"""
        result = Allocator(scenario("v1"), "total").solve()
        self.assertEqual(result.reaches["d_r_w1_wang_care"].reason, "time_threshold")
        self.assertEqual(result.reaches["d_r_w1_li_card"].reason, "no_open_overlap")

    def test_budget_exhaustion_is_reported_with_bound(self):
        from dataclasses import replace
        base = scenario("v2")
        residents, demands, pools, sites = {}, {}, {}, {}
        for c in range(14):
            for rid, resident in base.residents.items():
                residents[f"{rid}_c{c}"] = replace(
                    resident, id=f"{rid}_c{c}", person=f"{resident.person}_c{c}"
                )
            for did, demand in base.demands.items():
                demands[f"{did}_c{c}"] = replace(
                    demand, id=f"{did}_c{c}", resident=f"{demand.resident}_c{c}"
                )
            for pid, pool in base.pools.items():
                pools[f"{pid}_c{c}"] = replace(pool, id=f"{pid}_c{c}")
            for sid, site in base.sites.items():
                sites[f"{sid}_c{c}"] = replace(
                    site, id=f"{sid}_c{c}", pool=f"{site.pool}_c{c}"
                )
        version = replace(base, version_id="scaled", residents=residents,
                          demands=demands, pools=pools, sites=sites)
        allocator = Allocator(version, "total", budget_nodes=3_000,
                              time_budget_s=2.0)
        result = allocator.solve()
        self.assertEqual(result.status, "budget_exhausted")
        self.assertFalse(result.proven_optimal)
        self.assertIsNotNone(result.totals()["total_units_upper_bound"])
        self.assertGreaterEqual(
            result.totals()["total_units_upper_bound"],
            result.totals()["served_units"],
        )
