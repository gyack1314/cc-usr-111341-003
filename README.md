# 共享服务网点的容量可达性与公平改进

本项目分析基层政务服务点的**实际**可达性：地图上很近不等于真方便。
它分两层计算——

1. **地理可达**：按出行方式、时间门槛、资质和开放时段筛选可达网点；
   缺路径就是不可达，不用直线距离补边。
2. **容量分配**：把事项需求按“件”分配给有人员能力的网点。共用人员池
   只扣一次容量；整单事项不拆分；可分事项允许部分办理。

输出每个地区群体的分母、可达路径、分配去向、未服务原因和约束资源；
候选调整（延长开放、共享能力、增设网点、修桥）一律整体重算，并说明
一项改动帮助了谁、挤占了谁。

## 快速开始

```sh
python -m unittest discover -s tests -v
python -m compileall -q access_data tests

# 原有资料检查入口（保持不变）
python -m access_data.cli data/normal.json

# 三版本区域协调示例
python -m access_data.cli data/coordination/versions.json analyse \
  --version v2 --objective total --objective fair --check-small \
  --plans data/coordination/plans.json \
  --out out/report.md --json-out out/report.json
```

上面的命令会本地生成 `out/report.md`（v2）与 `out/report-v1.md`
（v1，含跨河无路与两个目标的取舍）；`out/` 按 `.gitignore` 不纳入版本库，
报告由代码与受摘要绑定的资料可重复生成。

## 资料

| 路径 | 内容 |
|---|---|
| `data/normal.json`、`data/boundary.json` | 原有 schema 1 资料与边界情形 |
| `data/coordination/versions.json` | 三个版本的登记（v1 基线 / v2 修桥 / v3 增员） |
| `data/coordination/plans.json` | 四类候选调整 |
| `data/broken-digest.json` | 摘要不一致的拒绝样例 |

示例资料为合成数据，仅用于方法核对。版本更新只追加，不覆盖旧版本。

## 口径要点

- **分母**：群体分母是事项件数；人口覆盖另算，且只有全部事项办成才计入。
- **同一人多事项**：人口只计一次，事项各自分配，不能把事项相加当人口。
- **跨界办理**：允许，并单独记录跨地区件数；重复登记同一人会被拒绝。
- **公平目标**：`total`（总量优先）与 `fair`（leximin，优先最差群体）
  由用户冻结，报告并排展示，不把任何一个称为唯一最优。
- **求解状态**：`exact` 已证最优；`budget_exhausted` 给出可行解与上界；
  小网另用独立枚举核对，大网明确说明未枚举。

详细模型、算法与边界见 [docs/method.md](docs/method.md)；schema 2 字段见
[docs/schema2.md](docs/schema2.md)；原资料约定见
[docs/data-contract.md](docs/data-contract.md)。

## 环境

Python 3.11+，仅用标准库。

```sh
docker build -t access-data .
docker run --rm access-data
```
