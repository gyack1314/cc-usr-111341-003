"""把分析结果渲染成便于讨论的 Markdown 与 JSON。

报告刻意把三件事放在最前面：分母（谁、什么事、多少件）、地理可达范围
（不考虑容量时能到哪里）、容量竞争后仍未服务的原因与约束资源。方案
对照单独成节，组合方案一律重算，不把单项改善量相加。
"""
from __future__ import annotations

from .access import AnalysisResult, exhaustive_optimum
from .plan import additivity_check


def _pct(value):
    if value is None:
        return "—"
    return f"{value * 100:.1f}%"


def _num(value):
    if value is None:
        return "—"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


REASON_LABELS = {
    "unreachable": "无路可达",
    "mode_not_allowed": "该出行方式不被门槛允许",
    "time_threshold": "超过时间门槛",
    "qualification": "无办理资质",
    "no_open_overlap": "开放时段与事项时段无交集",
    "arrival_after_closing": "到达后已关门",
    "inactive": "网点停用",
    "capacity": "能力池已用尽",
    "unit_too_large_for_capacity": "整单超过单点周上限",
    "atomic_no_fit": "整单放不进剩余容量",
}


def _reason(code):
    return REASON_LABELS.get(code, code)


def _esc(text):
    return str(text).replace("|", "\\|")


def _table(headers, rows):
    lines = ["| " + " | ".join(headers) + " |",
             "|" + "|".join("---" for _ in headers) + "|"]
    for row in rows:
        lines.append("| " + " | ".join(_esc(c) for c in row) + " |")
    return "\n".join(lines)


def geographic_section(result: AnalysisResult):
    summary = result.reach_totals()
    rows = []
    for demand_id in sorted(result.reaches):
        reach = result.reaches[demand_id]
        outcome = result.outcomes[demand_id]
        if reach.status == "reachable":
            best = reach.candidates[0]
            detail = f"{best.site_id}（{_num(round(best.minutes, 1))} 分钟，{best.mode or '步行'}）"
            reason = "—"
        else:
            detail = "无"
            reason = _reason(reach.reason)
        rows.append([
            outcome.person, f"{outcome.area}/{outcome.group}", outcome.service,
            _num(outcome.units), _num(reach.threshold), detail, reason,
        ])
    body = _table(
        ["居民", "地区/群体", "事项", "件数", "门槛(分钟)", "最近可达网点（不考虑容量）", "不可达原因"],
        rows,
    )
    reasons = "、".join(
        f"{_reason(k)} {v} 条" for k, v in summary["by_reason"].items()
    ) or "无"
    return (
        f"### 地理可达范围（不考虑容量）\n\n"
        f"事项 {summary['demands']} 条、{summary['demand_units']} 件；"
        f"有可达网点 {summary['reachable_demands']} 条（{summary['reachable_units']} 件），"
        f"不可达 {summary['unreachable_demands']} 条（{summary['unreachable_units']} 件）。"
        f"不可达原因：{reasons}。\n\n{body}\n"
    )


def capacity_section(result: AnalysisResult):
    rows = []
    for group in result.group_totals():
        reasons = "、".join(
            f"{_reason(k)} {_num(v)}件" for k, v in group["unserved_reasons"].items()
        ) or "—"
        sites = "、".join(group["sites"]) or "—"
        rows.append([
            f"{group['area']}/{group['group']}",
            _num(group["people"]),
            _num(group["demand_units"]),
            _num(group["served_units"]),
            _pct(group["coverage"]),
            _pct(group["reachable_coverage"]),
            sites,
            _num(group["cross_boundary_units"]),
            reasons,
        ])
    body = _table(
        ["地区/群体", "人口", "事项件数(分母)", "已服务", "件数覆盖", "地理可达覆盖", "去向网点", "跨界件数", "未服务原因"],
        rows,
    )
    pools = []
    for pool in result.ledger.snapshot()["pools"]:
        pools.append([
            pool["pool"], pool["name"] or "—", _num(pool["weekly_minutes"]),
            _num(pool["used_minutes"]), _num(pool["remaining_minutes"]),
            _pct(pool["utilization"]), "、".join(pool["sites"]),
        ])
    pool_body = _table(
        ["能力池", "名称", "周分钟", "已用", "剩余", "利用率", "共享网点"], pools
    )
    unserved = [d for d in result.as_dict()["demands"] if d["unserved_reason"]]
    detail_rows = []
    for demand in unserved:
        resources = "；".join(
            f"{r.get('resource')}（{r.get('kind')}，剩余 {_num(r.get('remaining_minutes'))} 分钟"
            f"，需要 {_num(r.get('needed_minutes'))} 分钟）"
            if r.get("remaining_minutes") is not None else
            f"地理：{_reason(r.get('reason'))}（不是容量问题）"
            if r.get("kind") == "geography"
            else f"{r.get('resource')}（{r.get('kind')}）"
            for r in demand["constraint_resources"]
        ) or "—"
        detail_rows.append([
            demand["demand"], demand["person"], demand["service"],
            _num(demand["units"]), _num(demand["served_units"]),
            _reason(demand["unserved_reason"]), resources,
        ])
    detail_body = _table(
        ["事项", "居民", "事项类型", "件数", "已服务", "原因", "约束资源"], detail_rows
    )
    return (
        "### 容量竞争后的服务缺口\n\n"
        f"总分母 {result.totals()['demand_units']} 件，已服务 {result.totals()['served_units']} 件；"
        f"人口覆盖（全部事项办成才计入）{_pct(result.totals()['people_coverage'])}。\n\n"
        f"{body}\n\n**能力池使用（共享池只扣一次）**\n\n{pool_body}\n\n"
        f"**未服务事项与约束资源**\n\n{detail_body}\n"
    )


def people_section(result: AnalysisResult):
    rows = []
    for entry in result.people_entries().values():
        rows.append([
            entry["person"], f"{entry['area']}/{entry['group']}", entry["age"] or "—",
            _num(entry["people"]), _num(entry["demand_units"]),
            _num(entry["served_units"]),
            "是" if entry["fully_served"] else "否",
        ])
    return (
        "### 人口覆盖（与事项件数分开）\n\n"
        "同一居民有多个事项时，只有全部事项都办成才计入人口覆盖；"
        "事项覆盖按件计，二者分母不同，不能相互替代。\n\n"
        + _table(["居民(人)", "地区/群体", "年龄", "人数", "事项件数", "已服务件数", "全部事项办成"], rows)
        + "\n"
    )


def objective_section(results):
    """并排展示同一方案在两个冻结目标下的结果，明确两者都非唯一最优。"""
    rows = []
    for objective, result in results.items():
        totals = result.totals()
        fair = result.fairness()
        rows.append([
            objective,
            _num(totals["served_units"]),
            _num(totals["unserved_units"]),
            _pct(totals["worst_group_coverage"]),
            f"{fair['worst_group']['area']}/{fair['worst_group']['group']}"
            if fair["worst_group"] else "—",
            _num(totals.get("total_units_upper_bound")),
            _status_label(result),
        ])
    return (
        "### 冻结的公平目标对照\n\n"
        "两个目标都由用户预先冻结，各自独立求解；报告不把任何一个称为唯一最优。\n\n"
        "`total` 先最大化总服务件数，同分时按 leximin 改善覆盖；"
        "`fair` 先按 leximin 依次改善最差、次差群体覆盖率，同分时最大化总服务量。\n\n"
        + _table(["目标", "已服务件数", "未服务件数", "最差群体覆盖", "最差群体", "总量上界", "求解状态"], rows)
        + "\n"
    )


def _status_label(result):
    labels = {
        "exact": "已证最优",
        "budget_exhausted": "预算耗尽（有上界）",
        "exact_enumeration": "独立枚举",
    }
    return labels.get(result.status, result.status)


def tradeoff_section(results):
    """公平提升付出了多少总量代价：逐组比较两个冻结目标。"""
    if len(results) < 2:
        return ""
    names = list(results)
    base_name = "total" if "total" in results else names[0]
    fair_name = "fair" if "fair" in results else names[-1]
    if base_name == fair_name:
        return ""
    base = results[base_name]
    fair = results[fair_name]
    base_totals = base.totals()
    fair_totals = fair.totals()
    rows = []
    base_groups = {(g["area"], g["group"]): g for g in base.group_totals()}
    fair_groups = {(g["area"], g["group"]): g for g in fair.group_totals()}
    for key in sorted(set(base_groups) | set(fair_groups)):
        b = base_groups.get(key, {"coverage": 0.0, "served_units": 0})
        f = fair_groups.get(key, {"coverage": 0.0, "served_units": 0})
        delta = (f["coverage"] or 0) - (b["coverage"] or 0)
        if abs(delta) < 1e-9:
            continue
        rows.append([
            f"{key[0]}/{key[1]}",
            _pct(b["coverage"]), _pct(f["coverage"]),
            f"{delta * 100:+.1f}pp",
            f"{f['served_units'] - b['served_units']:+}",
        ])
    cost = (fair_totals["served_units"] or 0) - (base_totals["served_units"] or 0)
    verdict = (
        f"`{fair_name}` 相对 `{base_name}` 的总量代价为 {cost:+} 件"
        if cost else "两个目标在本例的总服务量相同"
    )
    table = _table(["地区/群体", f"{base_name} 覆盖", f"{fair_name} 覆盖",
                    "覆盖变化", "件数变化"], rows) if rows else "本例两个目标的群体覆盖完全相同。"
    return (
        "### 公平提升的总量代价\n\n"
        f"{verdict}。下表只列出覆盖率发生变化的群体；"
        "正值为公平目标改善的群体，负值为其为公平让出的服务量。\n\n"
        + table
        + "\n"
    )


def plan_section(outcomes, objective):
    rows = []
    for outcome in outcomes.values():
        totals = outcome.result.totals()
        delta = outcome.deltas()
        rows.append([
            outcome.plan.label,
            _num(totals["served_units"]),
            f"{delta.get('served_units', 0):+}" if delta else "—",
            _pct(totals["worst_group_coverage"]),
            f"{(delta.get('worst_group_coverage', 0) or 0) * 100:+.1f}pp" if delta else "—",
            _pct(totals["people_coverage"]),
            _status_label(outcome.result),
            _num(totals.get("total_units_upper_bound")),
        ])
    return (
        f"### 候选调整对照（目标：{objective}）\n\n"
        "每个方案都从基线整体重算；组合方案单独重算，不能把单项改善量相加。\n\n"
        + _table(["方案", "已服务件数", "件数增量", "最差群体覆盖", "最差覆盖变化",
                  "人口覆盖", "求解状态", "总量上界"], rows)
        + "\n"
    )


def distribution_section(outcomes):
    parts = []
    for outcome in outcomes.values():
        delta = outcome.deltas()
        if not delta:
            continue
        helped = "、".join(
            f"{h['person']}(+{_num(h['delta'])})" for h in delta["helped"]
        ) or "无"
        displaced = "、".join(
            f"{h['person']}({_num(h['delta'])})" for h in delta["displaced"]
        ) or "无"
        parts.append(
            f"- **{outcome.plan.label}**：得到帮助 {helped}；被挤占 {displaced}。"
        )
    return (
        "### 谁得到帮助、谁被挤占\n\n"
        "“被挤占”指同一居民在方案实施后已服务件数下降；它不是需求消失，"
        "而是共享能力池被重新分配的结果。\n\n"
        + ("\n".join(parts) if parts else "没有可比较的分配变化。")
        + "\n"
    )


def additivity_section(version, plans, objective):
    check = additivity_check(version, plans, objective)
    if not check.get("checked"):
        return ""
    singles = "、".join(f"{k} {v:+}" for k, v in check["single_deltas"].items())
    return (
        "### 组合方案必须重算\n\n"
        f"基线已服务 {_num(check['baseline_served_units'])} 件；单项增量 {singles}；"
        f"单项相加 {_num(check['sum_of_singles'])} 件，组合实算 {_num(check['combined_delta'])} 件，"
        f"交互项 {_num(check['interaction'])} 件。"
        + ("两者相等，但这只是本例的巧合，方法上仍以组合重算为准。"
           if check.get("equal") else
           ("两者不等：组合创造了单项没有的新可达路径，收益高于相加。"
            if check["interaction"] > 0 else
            "两者不等：共享池竞争使单项收益无法叠加，组合低于相加。"))
        + "\n"
    )


def version_section(attributions):
    if not attributions:
        return ""
    rows = []
    for attribution in attributions:
        changed = attribution["changed"]
        rows.append([
            f"{attribution['before']} → {attribution['after']}",
            "是" if changed["roads"] else "否",
            "是" if changed["demand"] else "否",
            "是" if changed["capacity"] else "否",
            _num(attribution["served_before"]),
            _num(attribution["served_after"]),
            f"{attribution['served_delta']:+}",
        ])
    return (
        "### 版本之间：覆盖变化来自哪里\n\n"
        "这里只列出登记差异与整体重算后的覆盖变化，不把差异直接当作因果；"
        "旧版本资料保留原样。\n\n"
        + _table(["版本", "道路/班次变化", "需求变化", "能力配置变化", "前已服务", "后已服务", "变化"], rows)
        + "\n"
    )


def check_section(checks):
    """小网独立枚举核对表；大网明确说明为何不做枚举。"""
    if not checks:
        return ""
    rows = []
    for check in checks:
        if check.get("status") == "skipped":
            rows.append([check["objective"], "—", "—", check["reason"]])
            continue
        rows.append([
            check["objective"],
            _num(check["enumerated_served_units"]),
            _num(check["enumerated_worst_group_coverage"] and
                 round(check["enumerated_worst_group_coverage"] * 100, 1)),
            "一致" if check.get("match") else "不一致",
        ])
    return (
        "### 小网核对（独立枚举）\n\n"
        "枚举不做任何界剪枝，只用于验证求解器的最优值；大网若跳过，会在此说明原因，"
        "报告改用容量松弛上界与求解状态。\n\n"
        + _table(["目标", "枚举已服务件数", "枚举最差覆盖(%)", "与求解器"], rows)
        + "\n"
    )


def report(version, results, plan_outcomes=None, attributions=None,
           extra_notes=None, checks=None):
    """生成完整 Markdown 报告。``results`` 是 {目标: AnalysisResult}。"""
    lines = [
        f"# 服务可达性与容量公平报告：{version.dataset} / {version.version_id}",
        "",
        f"- 版本：`{version.version_id}`，生成时间 {version.created or '未登记'}。",
        f"- 居民 {len(version.residents)} 人/记录，事项 {len(version.demands)} 条，"
        f"网点 {len(version.sites)} 个，能力池 {len(version.pools)} 个。",
        f"- 道路 {len(version.roads)} 条（步行/公交分开登记），班线 {len(version.bus_lines)} 条。",
        "",
        "> 资料为合成示例，不代表任何现实机构。",
        "",
    ]
    notes = list(version.notes)
    if extra_notes:
        notes.extend(extra_notes)
    if notes:
        lines += ["**资料约定与兼容说明**", ""]
        lines += [f"- {note}" for note in notes]
        lines.append("")
    for objective, result in results.items():
        lines += [
            f"## 目标 `{objective}`",
            "",
            geographic_section(result),
            capacity_section(result),
            people_section(result),
        ]
        warnings = result.as_dict()["notes"]
        model_warnings = [w for w in warnings if w not in version.notes]
        if model_warnings:
            lines += ["**结构警告**", ""]
            lines += [f"- {w}" for w in model_warnings]
            lines.append("")
    if len(results) > 1:
        lines += ["## 目标对照", "", objective_section(results),
                  tradeoff_section(results)]
    if plan_outcomes:
        lines += ["## 候选调整", ""]
        for objective, outcomes in plan_outcomes.items():
            lines += [plan_section(outcomes, objective)]
            lines += [distribution_section(outcomes)]
            singles = [o.plan for o in outcomes.values() if o.plan.id != "combined"]
            lines += [additivity_section(version, singles, objective)]
    lines += ["## 求解可信度", "", check_section(checks)]
    lines += ["## 版本记录", "", version_section(attributions)]
    return "\n".join(lines)


def small_network_check(version, objective="total", departure_minute=480):
    """小网独立枚举：返回与求解器一致的核对记录。"""
    value, result = exhaustive_optimum(version, objective, departure_minute)
    return {
        "objective": objective,
        "enumerated_value": list(value) if isinstance(value, tuple) else value,
        "enumerated_served_units": result.totals()["served_units"],
        "enumerated_worst_group_coverage": result.totals()["worst_group_coverage"],
    }
