"""分析区域服务网点的地理可达、容量分配与候选调整。

用法::

    python -m access_data.cli data/coordination/versions.json analyse \
        --version v2 --objective total --objective fair \
        --plans data/coordination/plans.json --out out/report.md

    python -m access_data.cli data/coordination/versions.json compare \
        --from v1 --to v2

原有资料检查入口保持不变::

    python -m access_data.cli data/normal.json
"""
from __future__ import annotations

import argparse
import json
import sys

from .access import Allocator
from .io import DataError
from .plan import (
    Adjustment, Plan, additivity_check, evaluate_plans, pareto_frontier,
)
from .report import report, small_network_check
from .versions import attribution, compare_versions, load_registry, load_versions


def _load_target(path):
    """路径可以是单个清单（schema 1/2），也可以是版本登记。"""
    import json as _json
    from pathlib import Path
    from .io import load
    from .model import build_version

    raw = _json.loads(Path(path).read_text(encoding="utf-8"))
    if "versions" in raw:
        return load_versions(path)
    tables = load(path)
    meta = raw.get("version", {})
    name = meta.get("id", Path(path).stem)
    return {name: build_version(tables, name, meta.get("dataset", name),
                                meta.get("created", ""), meta)}


def _parse_args(argv=None):
    parser = argparse.ArgumentParser(description="检查资料并进行可达性与容量公平分析")
    parser.add_argument("target", help="资料清单或版本登记文件")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("inspect", help="列出表数量与身份检查（默认行为）")

    analyse = sub.add_parser("analyse", help="计算地理可达与容量分配")
    analyse.add_argument("--version")
    analyse.add_argument("--objective", action="append", default=None,
                         choices=["total", "fair", "worst"],
                         help="可重复；每个目标独立求解")
    analyse.add_argument("--plans", help="候选方案 JSON")
    analyse.add_argument("--out", help="Markdown 输出路径")
    analyse.add_argument("--json-out", help="JSON 输出路径")
    analyse.add_argument("--budget-nodes", type=int, default=200_000)
    analyse.add_argument("--time-budget", type=float, default=5.0)
    analyse.add_argument("--default-threshold", type=float, default=None,
                         help="为没有登记门槛的事项提供显式默认旅行分钟数")
    analyse.add_argument("--check-small", action="store_true",
                         help="用独立枚举核对小网最优值")

    compare = sub.add_parser("compare", help="比较两个版本的覆盖变化")
    compare.add_argument("--from", dest="from_version", required=True)
    compare.add_argument("--to", dest="to_version", required=True)
    compare.add_argument("--objective", default="total", choices=["total", "fair"])
    compare.add_argument("--json-out")

    return parser.parse_args(argv)


def _plan_from_dict(raw):
    adjustments = []
    for item in raw.get("adjustments", []):
        data = dict(item)
        if "open_hours" in data and data["open_hours"]:
            data["open_hours"] = tuple(data["open_hours"])
        if "site_ids" in data:
            data["site_ids"] = tuple(data["site_ids"])
        if "merge_pools" in data:
            data["merge_pools"] = tuple(data["merge_pools"])
        adjustments.append(Adjustment(**data))
    return Plan(
        id=raw["id"], label=raw.get("label", raw["id"]),
        adjustments=tuple(adjustments), note=raw.get("note", ""),
    )


def _load_plans(path):
    with open(path, encoding="utf-8") as handle:
        raw = json.load(handle)
    return [_plan_from_dict(item) for item in raw["plans"]]


def _select_version(versions, requested):
    if requested is None:
        if len(versions) == 1:
            return next(iter(versions.values()))
        raise DataError("多个版本时必须用 --version 指定")
    if requested not in versions:
        raise DataError(f"版本{requested}不在登记中")
    return versions[requested]


def _require_thresholds(version, default):
    from .model import with_default_standard
    missing = [d for d in version.demands.values()
               if version.standard_for(d) is None]
    if not missing:
        return version
    if default is None:
        services = sorted({d.service for d in missing})
        raise DataError(
            f"以下事项缺少服务门槛资料：{services}。"
            "请用 --default-threshold 显式给出，分析不会默认按无限时间处理。"
        )
    return with_default_standard(version, default)


def _analyse(args, versions):
    version = _require_thresholds(
        _select_version(versions, args.version), args.default_threshold
    )
    objectives = args.objective or ["total"]
    results = {}
    for objective in objectives:
        allocator = Allocator(
            version, objective, budget_nodes=args.budget_nodes,
            time_budget_s=args.time_budget,
        )
        results[objective] = allocator.solve()

    payload = {
        "versions": {k: {"created": v.created, "dataset": v.dataset,
                         "residents": len(v.residents), "demands": len(v.demands),
                         "sites": len(v.sites), "roads": len(v.roads)}
                     for k, v in versions.items()},
        "version": version.version_id,
        "analyses": {objective: result.as_dict()
                     for objective, result in results.items()},
    }

    plan_outcomes = {}
    extra_notes = []
    if args.plans:
        plans = _load_plans(args.plans)
        for objective in objectives:
            baseline, outcomes = evaluate_plans(
                version, plans, objective, args.budget_nodes
            )
            plan_outcomes[objective] = outcomes
            payload.setdefault("baselines", {})[objective] = baseline.totals()
            payload.setdefault("plans", {})
            payload["plans"][objective] = {
                "plans": [outcome.as_dict() for outcome in outcomes.values()],
                "frontier": pareto_frontier(outcomes),
                "additivity": additivity_check(version, plans, objective),
            }
            if "combined" in outcomes:
                payload["plans"][objective]["combined"] = outcomes["combined"].as_dict()

    if args.check_small:
        checks = []
        for objective in objectives:
            try:
                check = small_network_check(version, objective)
                allocator = Allocator(version, objective,
                                      budget_nodes=args.budget_nodes,
                                      time_budget_s=args.time_budget)
                solved = allocator.solve()
                check["solver_value"] = list(allocator._best[0])
                check["match"] = check["solver_value"] == check["enumerated_value"]
                checks.append(check)
            except ValueError as exc:
                checks.append({"objective": objective,
                               "status": "skipped", "reason": str(exc)})
        payload["small_network_check"] = checks

    attributions = []
    if len(versions) > 1:
        ordered = [versions[e.id] for e in load_registry(args.target)[0] if e.id in versions]
        for before, after in zip(ordered, ordered[1:]):
            result_before = Allocator(before, objectives[0],
                                      budget_nodes=args.budget_nodes).solve()
            result_after = Allocator(after, objectives[0],
                                     budget_nodes=args.budget_nodes).solve()
            attributions.append(attribution(before, after, result_before.as_dict(),
                                            result_after.as_dict()))
        payload["version_attribution"] = attributions

    text = report(version, results, plan_outcomes, attributions, extra_notes,
                  payload.get("small_network_check"))
    if args.out:
        from pathlib import Path
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(text, encoding="utf-8")
    else:
        print(text)
    if args.json_out:
        from pathlib import Path
        Path(args.json_out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json_out).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n",
            encoding="utf-8",
        )
    return 0


def _compare(args, versions):
    if args.from_version not in versions or args.to_version not in versions:
        raise DataError("比较的版本不在登记中")
    before = versions[args.from_version]
    after = versions[args.to_version]
    result_before = Allocator(before, args.objective).solve()
    result_after = Allocator(after, args.objective).solve()
    data = attribution(before, after, result_before.as_dict(), result_after.as_dict())
    data["diff"] = compare_versions(before, after)
    text = json.dumps(data, ensure_ascii=False, indent=2, default=str)
    if args.json_out:
        from pathlib import Path
        Path(args.json_out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json_out).write_text(text + "\n", encoding="utf-8")
    else:
        print(text)
    return 0


def main(argv=None):
    args = _parse_args(argv)
    try:
        versions = _load_target(args.target)
    except (DataError, ValueError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    command = args.command or "inspect"
    try:
        if command == "inspect":
            from .domain import inspect_tables
            from .io import load
            from pathlib import Path
            import json as _json
            raw = _json.loads(Path(args.target).read_text(encoding="utf-8"))
            if "versions" in raw:
                entries, digest = load_registry(args.target)
                print(json.dumps({
                    "registry_sha256": digest,
                    "versions": [entry.__dict__ for entry in entries],
                }, ensure_ascii=False, indent=2))
            else:
                print(json.dumps(inspect_tables(load(args.target)),
                                 ensure_ascii=False, indent=2))
            return 0
        if command == "analyse":
            return _analyse(args, versions)
        if command == "compare":
            return _compare(args, versions)
        print(f"未知命令：{command}", file=sys.stderr)
        return 2
    except (DataError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
