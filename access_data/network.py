"""按出行方式计算时间最短路径；缺路径即不可达，不以直线距离替代。

公交网络是时间相关的：在节点等待、按首末班与班距上车。这里用“最早到达
时刻”作为 Dijkstra 的键：到达函数对出发时刻单调（FIFO），因此按最早到达
时刻扩展是正确且有限的。总行程时间 = 到达时刻 - 出发时刻。
"""
from __future__ import annotations

import heapq
import math
from dataclasses import dataclass

from .model import BUS, DAY_MINUTES, WALK, DAYS, BusLine, Version

INF = float("inf")


@dataclass(frozen=True)
class EdgeUse:
    road_id: str
    mode: str
    minutes: float
    board_wait: float = 0.0
    line_id: str = ""

    def as_dict(self):
        data = {"road": self.road_id, "mode": self.mode, "minutes": self.minutes}
        if self.line_id:
            data["line"] = self.line_id
            data["board_wait"] = self.board_wait
        return data


@dataclass(frozen=True)
class Path:
    minutes: float
    edges: tuple[EdgeUse, ...]
    departure_minute: int | None = None
    arrivals: tuple[int, ...] = ()

    @property
    def modes(self):
        return tuple(sorted({e.mode for e in self.edges}))

    def as_dict(self):
        return {
            "minutes": self.minutes,
            "modes": list(self.modes),
            "departure_minute": self.departure_minute,
            "edges": [e.as_dict() for e in self.edges],
        }


def _next_departure(line: BusLine, clock):
    """返回 clock 之后（含）下一班车的周内时刻；超出班次窗口返回 None。

    等待约定：``half_headway``/``timetable`` 都按首班、班距与末班实际计算；
    ``none`` 表示资料只给预算，此时在首末班窗口内按半个班距估计。
    """
    clock = int(math.floor(clock))
    day_index = (clock // DAY_MINUTES) % 7
    day_start = (clock // DAY_MINUTES) * DAY_MINUTES
    # 允许等到之后的运行日，但最多等一周。
    for offset in range(8):
        index = (day_index + offset) % 7
        base = day_start + offset * DAY_MINUTES
        if DAYS[index] not in line.days:
            continue
        first = base + line.first_departure
        last = base + line.last_departure
        if clock > last:
            continue
        candidate = first if clock <= first else first + (
            (clock - first + line.headway_minutes - 1) // line.headway_minutes
        ) * line.headway_minutes
        if candidate > last:
            continue
        return candidate
    return None


class Router:
    """分出行方式的最短路。"""

    def __init__(self, version: Version, departure_minute=8 * 60, modes=None,
                 horizon_minutes=DAY_MINUTES):
        self.version = version
        self.departure_minute = departure_minute
        self.modes = tuple(modes or (WALK, BUS))
        self.horizon_minutes = horizon_minutes
        self._adj = {}
        self._lines_by_road = {}
        for line in version.bus_lines.values():
            self._lines_by_road.setdefault(line.road, []).append(line)
        for road in version.roads.values():
            for src, dst in road.directions():
                self._adj.setdefault((src, road.mode), []).append((dst, road))

    def _ride(self, road, clock):
        """返回 (等待分钟, 乘车分钟, 线路)；不可乘返回 None。"""
        if road.mode != BUS:
            return 0.0, road.minutes, ""
        lines = self._lines_by_road.get(road.id, [])
        best = None
        for line in sorted(lines, key=lambda l: l.id):
            departure = _next_departure(line, clock)
            if departure is None:
                continue
            wait = departure - clock
            if line.wait_rule == "none":
                wait = line.headway_minutes / 2.0
            if best is None or (wait, line.id) < (best[0], best[2]):
                best = (wait, road.minutes, line.id)
        return best

    def shortest_paths(self, origin, modes=None, departure_minute=None, limit=None):
        """origin 到各节点的最早到达路径，以目标节点为键。"""
        selected = tuple(modes or self.modes)
        start = self.departure_minute if departure_minute is None else departure_minute
        deadline = start + self.horizon_minutes
        paths = {}
        best = {origin: start}
        heap = [(start, origin, ())]
        while heap:
            clock, node, trail = heapq.heappop(heap)
            if clock > best.get(node, INF):
                continue
            elapsed = clock - start
            if node not in paths or elapsed < paths[node].minutes:
                paths[node] = Path(elapsed, trail, start, ())
            if limit is not None and elapsed > limit + 1e-9:
                continue
            for mode in selected:
                for dst, road in self._adj.get((node, mode), ()):
                    ride = self._ride(road, clock)
                    if ride is None:
                        continue
                    wait, minutes, line_id = ride
                    arrival = clock + wait + minutes
                    if arrival > deadline:
                        continue
                    if arrival + 1e-9 < best.get(dst, INF):
                        best[dst] = arrival
                        edge = EdgeUse(road.id, mode, minutes, wait, line_id)
                        heapq.heappush(heap, (arrival, dst, trail + (edge,)))
        return paths

    def reachable(self, origin, modes=None, max_minutes=INF, departure_minute=None):
        paths = self.shortest_paths(origin, modes, departure_minute, limit=max_minutes)
        return {node: p for node, p in paths.items()
                if p.minutes <= max_minutes + 1e-9}

    def describe(self):
        return {
            "modes": list(self.modes),
            "departure_minute": self.departure_minute,
            "roads": len(self.version.roads),
            "bus_lines": len(self.version.bus_lines),
        }
