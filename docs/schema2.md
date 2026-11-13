# 分析层资料约定（schema 2）

`schema 1` 的读取与校验见 `docs/data-contract.md`，保持不变。`schema 2`
把分析所需的信息拆开登记：居民、事项要求、网点资质、开放时段、能力池、
班次和服务门槛各自成表。

清单结构仍是 `schema: 1` 的通用格式（文件名、字节数、SHA256、行数、字段
集合），额外允许 `version` 元数据。读取入口仍是 `access_data.io.load`。

## 表

### nodes
| 字段 | 类型 | 说明 |
|---|---|---|
| id | str | 路网节点 |

### residents
| 字段 | 类型 | 说明 |
|---|---|---|
| id | str | 居住记录 |
| person | str | 自然人标识；**同一 person 不得重复登记** |
| group | str | 群体（如 `老年`、`劳动年龄`） |
| area | str | 行政区/地区 |
| node | str | 居住格 |
| people | int | 对应人口；同一 person 只计一次 |
| age | str | 年龄组（可选） |

### demands
| 字段 | 类型 | 说明 |
|---|---|---|
| id | str | 事项记录 |
| resident | str | 指向 residents |
| service | str | 事项类型 |
| units | int | 件数 |
| atomic | bool | `true` 必须整单办理；`false` 可分 |
| days | list[str] | 可办理的星期；空表示不限 |
| earliest / latest | str `HH:MM` | 可办理时段 |
| note | str | 可读说明 |

### sites
| 字段 | 类型 | 说明 |
|---|---|---|
| id | str | 网点 |
| node | str | 所在节点 |
| area | str | 网点所在地区（用于识别跨界办理） |
| services | list[str] | 资质事项；多事项网点用列表 |
| pool | str | 人员池 |
| minutes_per_unit | number | 每件耗时；若给了 `service_rates` 可作默认 |
| service_rates | map[str, number] | 分事项每件耗时 |
| open_hours | list[{day,start,end}] | 开放时段；空表示全周 |
| weekly_minutes | number / null | 网点自身周上限（可选） |
| active | bool | 是否启用 |

### pools
| 字段 | 类型 | 说明 |
|---|---|---|
| id | str | 人员池 |
| weekly_minutes | number | 周分钟预算；共用该池的网点**共享同一预算** |
| name | str | 可读名称 |

### roads
| 字段 | 类型 | 说明 |
|---|---|---|
| id | str | 道路/线路 |
| source / target | str | 有向端点 |
| mode | `walk` / `bus` | 出行方式 |
| minutes | number | 基础时间 |
| one_way | bool | `true` 只允许 source→target |
| name | str | 可读名称 |

### bus_lines
| 字段 | 类型 | 说明 |
|---|---|---|
| id | str | 班线 |
| road | str | 必须指向 `mode=bus` 的道路 |
| headway_minutes | number | 班距 |
| first_departure / last_departure | str `HH:MM` | 首末班 |
| days | list[str] | 运行日 |
| wait_rule | `timetable` / `half_headway` / `none` | 候车口径 |
| name | str | 可读名称 |

### standards
| 字段 | 类型 | 说明 |
|---|---|---|
| id | str | 门槛 |
| service | str | 事项 |
| group | str | 群体；`*` 表示该事项默认 |
| max_travel_minutes | number | 统一时间门槛 |
| modes | list[str] | 允许的出行方式 |
| mode_limits | map[str, number] | 分方式门槛；优先于统一门槛 |
| note | str | 可读说明 |

## 关系约束

- `residents.node`、`demands.resident`、`sites.node`、`sites.pool`、
  `roads.source/target`、`bus_lines.road` 都要指向已登记记录。
- `person` 重复、事项件数非整数、时段结束不晚于开始、公交末班早于首班，
  都会在载入时拒绝。
- `standards` 不得为空；缺少某事项门槛时，分析入口要求显式提供默认值。

## 版本登记

`data/coordination/versions.json`：

```json
{
  "schema": 1,
  "versions": [
    {"id": "v1", "manifest": "versions/v1/manifest.json",
     "created": "2025-01-06", "previous": null, "note": "基线"}
  ]
}
```

版本清单只追加；同一 `id` 重复登记会被拒绝。旧版本目录原样保留，便于
对比“覆盖变化来自道路、需求还是能力配置”。
