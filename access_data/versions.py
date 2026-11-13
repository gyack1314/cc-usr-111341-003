"""接入并保留资料版本。

清单与 JSONL 的摘要绑定沿用 io.py；这里额外记录版本标识、生成时间与
上一版本，用于区分覆盖变化来自道路、需求还是能力配置。

版本清单位于 data/coordination/versions.json，例如::

    {"schema": 1,
     "versions": [
       {"id": "v1", "manifest": "versions/v1/manifest.json",
        "created": "2025-01-06", "previous": null,
        "note": "基线"},
       {"id": "v2", "manifest": "versions/v2/manifest.json",
        "created": "2025-03-01", "previous": "v1",
        "note": "新增跨河步行桥"}
     ]}

旧版本只追加、不覆盖；同名版本重复登记视为错误。清单本身也有摘要，
不能静默改写已发布的旧版本资料。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from .io import DataError, load
from .model import Version, build_version


@dataclass(frozen=True)
class VersionEntry:
    id: str
    manifest: str
    created: str
    previous: str | None
    note: str


def _read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise DataError(f"版本清单不可读取：{path}") from exc


def load_registry(path):
    path = Path(path).resolve()
    raw = _read_json(path)
    if raw.get("schema") != 1 or not isinstance(raw.get("versions"), list):
        raise DataError("版本清单版本或结构无效")
    entries = []
    seen = set()
    for row in raw["versions"]:
        for field in ("id", "manifest", "created"):
            if not isinstance(row.get(field), str) or not row[field]:
                raise DataError(f"版本登记缺少{field}")
        if row["id"] in seen:
            raise DataError(f"版本{row['id']}重复登记")
        seen.add(row["id"])
        entry = VersionEntry(
            id=row["id"], manifest=row["manifest"], created=row["created"],
            previous=row.get("previous"), note=row.get("note", ""),
        )
        entries.append(entry)
    return entries, hashlib.sha256(path.read_bytes()).hexdigest()


def load_versions(path):
    """读取全部版本并按登记顺序返回 {id: Version}。"""
    entries, _ = load_registry(path)
    root = Path(path).resolve().parent
    versions = {}
    for entry in entries:
        manifest = (root / entry.manifest).resolve()
        if not manifest.is_relative_to(root):
            raise DataError(f"版本{entry.id}的清单越过版本目录")
        tables = load(manifest)
        raw = _read_json(manifest)
        meta = raw.get("version", {})
        version = build_version(
            tables, entry.id, meta.get("dataset", entry.id), entry.created,
            {"previous": entry.previous, "note": entry.note, **meta},
        )
        versions[entry.id] = version
    return versions


def comparison_keys(before: Version, after: Version):
    """快速判断哪一类登记发生了变化（道路/需求/能力）。"""
    diff = compare_versions(before, after)
    changed = set()
    if any(diff["roads"][k] or diff["bus_lines"][k] for k in ("added", "removed", "changed")):
        changed.add("roads")
    if any(diff["residents"][k] or diff["demands"][k] for k in ("added", "removed", "changed")):
        changed.add("demand")
    if any(diff["sites"][k] for k in ("added", "removed", "changed")) or diff["pools"]["changed"]:
        changed.add("capacity")
    return changed


def compare_versions(before: Version, after: Version):
    """只比较登记内容，不推断因果关系；用于定位覆盖变化的资料来源。"""
    def ids(mapping):
        return set(mapping)

    def values(mapping, attr):
        return {k: getattr(v, attr) for k, v in mapping.items()}

    road_added = sorted(ids(after.roads) - ids(before.roads))
    road_removed = sorted(ids(before.roads) - ids(after.roads))
    road_changed = sorted(
        k for k in ids(before.roads) & ids(after.roads)
        if before.roads[k] != after.roads[k]
    )
    resident_added = sorted(ids(after.residents) - ids(before.residents))
    resident_removed = sorted(ids(before.residents) - ids(after.residents))
    resident_changed = sorted(
        k for k in ids(before.residents) & ids(after.residents)
        if before.residents[k] != after.residents[k]
    )
    demand_added = sorted(ids(after.demands) - ids(before.demands))
    demand_removed = sorted(ids(before.demands) - ids(after.demands))
    demand_changed = sorted(
        k for k in ids(before.demands) & ids(after.demands)
        if before.demands[k] != after.demands[k]
    )
    site_added = sorted(ids(after.sites) - ids(before.sites))
    site_removed = sorted(ids(before.sites) - ids(after.sites))
    site_changed = sorted(
        k for k in ids(before.sites) & ids(after.sites)
        if before.sites[k] != after.sites[k]
    )
    pool_changed = sorted(
        k for k in ids(before.pools) & ids(after.pools)
        if before.pools[k] != after.pools[k]
    )
    bus_added = sorted(ids(after.bus_lines) - ids(before.bus_lines))
    bus_removed = sorted(ids(before.bus_lines) - ids(after.bus_lines))
    bus_changed = sorted(
        k for k in ids(before.bus_lines) & ids(after.bus_lines)
        if before.bus_lines[k] != after.bus_lines[k]
    )
    standards_changed = sorted(
        k for k in ids(before.standards) | ids(after.standards)
        if before.standards.get(k) != after.standards.get(k)
    )
    return {
        "roads": {"added": road_added, "removed": road_removed, "changed": road_changed},
        "bus_lines": {"added": bus_added, "removed": bus_removed, "changed": bus_changed},
        "residents": {"added": resident_added, "removed": resident_removed,
                      "changed": resident_changed},
        "demands": {"added": demand_added, "removed": demand_removed,
                    "changed": demand_changed},
        "sites": {"added": site_added, "removed": site_removed, "changed": site_changed},
        "pools": {"changed": pool_changed},
        "standards": {"changed": standards_changed},
    }


def attribution(before: Version, after: Version, result_before, result_after):
    """把覆盖变化归因到“道路 / 需求 / 能力配置”三类登记差异。

    只说明哪类登记发生了变化，不声称因果强度；组合版本必须整体重算。
    """
    diff = compare_versions(before, after)
    road_delta = bool(diff["roads"]["added"] or diff["roads"]["removed"] or diff["roads"]["changed"]
                      or diff["bus_lines"]["added"] or diff["bus_lines"]["removed"] or diff["bus_lines"]["changed"])
    demand_delta = bool(diff["residents"]["added"] or diff["residents"]["removed"]
                        or diff["residents"]["changed"] or diff["demands"]["added"]
                        or diff["demands"]["removed"] or diff["demands"]["changed"])
    capacity_delta = bool(diff["sites"]["added"] or diff["sites"]["removed"] or diff["sites"]["changed"]
                          or diff["pools"]["changed"])
    return {
        "before": before.version_id,
        "after": after.version_id,
        "diff": diff,
        "changed": {
            "roads": road_delta,
            "demand": demand_delta,
            "capacity": capacity_delta,
        },
        "served_before": result_before["totals"]["served_units"],
        "served_after": result_after["totals"]["served_units"],
        "served_delta": result_after["totals"]["served_units"] - result_before["totals"]["served_units"],
    }
