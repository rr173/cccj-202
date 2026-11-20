# EPCIS 2.0 离线事件加工仓

对一批封闭的 EPCIS 2.0 JSON-LD 事件和一张声明单位表做离线加工，输出可按
**业务时间（eventTime）** 与 **接收批次（receiving batch）** 两个维度复查的追溯关系。

明确不做的事（profile 边界）：不从包装关系推断物料消耗；不做原料配方、比例分摊、
召回优化或数量预测。

## 运行环境

* Python ≥ 3.11，无需联网、无需安装依赖。
* JSON-LD 展开使用标准处理器 **PyLD**（vendored 于 `vendor/`，含 cachetools /
  frozendict / lxml，均为按 sha256 校验的 PyPI wheel 解包）。`epcis_warehouse/_vendor.py`
  在导入时自动把 `vendor/` 加入 `sys.path`；整个目录可复制后在别处直接运行。
* 展开时的 document loader 只服务**打包的固定 context**（`epcis_warehouse/contexts/epcis.jsonld`）
  与**显式白名单 IRI**（`contexts/whitelist.json`）。任何未知 context IRI 不会触发网络访问，
  该事件记为 `uninterpretable` 并保留原文。

## 快速开始（主演练）

```bash
python3 drills/main/gen_drill.py        # 生成数据、建仓、依次接收批次1、批次2
python3 drills/main/verify_drill.py     # 独立核验：手写预期 vs 实际 CLI 输出
```

演练时间线（2026-01-01，UTC）：

| 时刻 | 事件 | 内容 | 批次 |
|---|---|---|---|
| 09:00 | e0 | A 入箱 B0（**迟到事件**） | 2 |
| 10:00 | e1 | A、O1 入箱 B | 1 |
| 11:00 | e2 | A 从 B 拆出 | 1 |
| 12:00 | e3 | 转换 T：输入 A 与 5 kg class X，产出 C 与 4.8 kg class D | 1 |

`drills/main/expected.json` 是**手工编写**的预期（各截点父链、T 的输入输出、
`allocation=not_declared` 字段等）；`verify_drill.py` 只通过 CLI 取实际输出做子集比对，
不调用任何重建函数。要点：11:30 查 A 已不在 B；C 的来源列出 A 和 X 但不给两者到 C 的份额；
B 的另一成员 O1 不被牵连为 T 的输入；9 点的迟到事件只改变批次 2 对旧业务时点的认识，
批次 1 的查询结果不变。

## 数据与模型

### 输入

* **单位表** `units.json`：每个单位给出 `dimension` 与正有理数 `factor`
  （字符串形式，如 `"1/1000"`，拒绝 float）。兼容量纲按精确有理数换算到该量纲基准单位。
* **批次 manifest**：`{"batch": 名, "files": [{"name","sha256"}], "lineage_links": [...]}`。
  文件缺失或摘要不符 → 整批拒收（记录 rejected，不发布），旧批次仍可查。
* **事件文件**：单个事件、事件数组，或带 `eventList` 的 EPCISDocument（文档级
  `@context` 由事件继承）。文件次序与分块不影响任何业务结果。

### 事件 profile

每事件必须有：`eventID`、`eventTime`（带偏移）、`recordTime`、非空
`sourceList`/`destinationList`，以及按类型要求的 EPC 或 class 数量成员。

| 类型 | 支持 | 其他 |
|---|---|---|
| ObjectEvent | `OBSERVE` | 其他 action → `unsupported` |
| AggregationEvent | `ADD` / `DELETE`，声明成员非空 | 空成员（含全体 DELETE）→ `unsupported`，不解释为无操作 |
| TransformationEvent | 输入、输出列表均非空 | — |
| 其他类型 | — | 一律 `unsupported`，不当空事件 |

缺字段、未知 uom、无偏移时间 → `invalid`；未知 context → `uninterpretable`。
三类非 ok 事件都保留原文并出现在 `review` 中，均不进入业务状态。

### 接收与出处

* 事件以 `(eventID, 展开内容摘要)` 去重：**完全重传**只增加出处（批次+文件，多处保留）。
* **同 ID 不同展开内容** = conflict。涉及该事件的合格结果在该批次视图中**暂停**
  （`status: suspended`）；旧批次视图不受影响。
* `recordTime` 只作首次被报告的时间证据，绝不参与业务时间排序。

## 语义规则

### 包装状态（按 eventTime 求值）

* 同一 eventTime 的包装变化是**同时操作组**：先删除已声明量，再加入；与文件/接收顺序无关。
* 具名 EPC 同一时刻至多一个直接父；同一父子关系重复 ADD 按集合处理（幂等）；
  后续 ADD 到其他父视为移动；**同一时刻被加入两个不同父 = 歧义**（不按 ID 排序择一），
  该 child 的父链结果转为 `unknown`。
* class 成员按 `(parent, class, dimension)` 以基准单位**精确**增减（`fractions.Fraction`），
  不当具名集合去重；DELETE 超过已有量 = 冲突，该 `(parent,class,dimension)`
  自该时刻起 `unknown`（**不截到零**，无 float 容差），原因可回指事件。
* 包装环：环内及经过环的祖先结果为 `unknown`（`status: cycle`，列环节点与成因事件）。
* unknown 不被后续普通增删自动清除；只有**新接收批次**全量重算出无冲突历史才恢复
  （迟到事件可因此改变新批次对旧业务时点的认识）。
* 未受影响的成员与数量键始终可查。

### 转换与跨事件来源

* TransformationEvent 的输入/输出列表共同组成一个转换节点；每个输出可追溯到该节点
  全部输入，但**不断言**某输入到某输出的份额：`allocation = "not_declared"`，不做均匀混合。
* 统计按量纲给出输入总量、输出总量、差额（精确分数）；非零差额不非法（未承诺无损）；
  不同量纲不硬相加；包装 ADD/DELETE 不进入转换数量总计。
* 跨转换来源**只**由 manifest 的 `lineage_links` 声明（下游 eventID+输入项坐标 →
  上游 eventID+输出项坐标）。校验：同 EPC/class、量纲兼容、上游 eventTime 不晚于下游；
  链接本身不声明数量分配。仅编号相同不认定来源；无链接时返回参与事实 +
  `missing_evidence`。缺端点 `pending`；未来来源 `invalid`；同刻链接成环显式报告
  （`cycle`），不无限展开、不生成假确定路径。

### 两个时间维度

查询 = （业务时刻 t, 接收批次 b)：只用 `eventTime <= t` 且在批次 ≤ b 已完整收到的事件。
迟到事件改变新批次对旧业务时点的认识，但不改写旧批次查询。每个新批次全量重建业务状态
（流式重放，不把全量原文和全部路径放内存）。

## CLI

```bash
P=/workspace  # 仓库根
python3 -m epcis_warehouse.cli --root WH init --units units.json
python3 -m epcis_warehouse.cli --root WH ingest manifest.json
python3 -m epcis_warehouse.cli --root WH query ancestors --epc urn:... --t 2026-01-01T11:30:00+00:00 --batch 2
python3 -m epcis_warehouse.cli --root WH query children --parent urn:... --t ... --batch latest
python3 -m epcis_warehouse.cli --root WH query trace --item urn:...            # 或 class:urn:...
python3 -m epcis_warehouse.cli --root WH query transformation --event urn:uuid:... --t ...
python3 -m epcis_warehouse.cli --root WH query observations --item urn:... --t ...
python3 -m epcis_warehouse.cli --root WH event --id urn:uuid:...     # 逐事件声明的数量与成员、出处
python3 -m epcis_warehouse.cli --root WH review [--batch N]          # 批次/文件摘要、非 ok 事件、冲突
python3 -m epcis_warehouse.cli --root WH evidence --digest SHA256    # 原文 + 展开 IRI
```

查询结果中的 `events`（事件→内容摘要）与 `evidence`（摘要→原文/展开文件路径）即共享证据表示：
同一证据对象被多条结果引用，不为每条查询展开所有路径。

## 重开、复制与证据复查

```bash
# 重开（数据与 context 都在目录/包内，可复制后离线重开）
cp -r WH WH-copy
python3 -m epcis_warehouse.cli --root WH-copy query ancestors --epc urn:... --t ...

# 证据复查：先取查询结果中的 digest，再回指原文与展开 IRI
python3 -m epcis_warehouse.cli --root WH evidence --digest <sha256>
```

## 运行时大事件集内存命令

```bash
# 生成约 3 万事件（3000 层包装嵌套、1.5k 节点转换链、宽容器），分批接收并测量
python3 scripts/memory_check.py --events 30000
# 在 384MB 地址空间上限内完成全部查询
python3 scripts/memory_check.py --events 30000 --limit-mb 384
# 外部测量（若系统提供 /usr/bin/time；脚本自身已用 getrusage 报告峰值 RSS）
/usr/bin/time -v python3 scripts/memory_check.py --events 60000
```

实测（本机）：3 万事件 / 磁盘 60MB，接收峰值 RSS ≈ 39MB，查询峰值 ≈ 59MB；
6 万事件 / 磁盘 120MB，接收峰值 ≈ 63MB，查询峰值 ≈ 81MB —— 内存随**状态规模**增长，
与事件总量无关（重放流式读取 SQLite，路径只在查询时按需计算）。

## 测试

```bash
python3 drills/main/gen_drill.py && python3 drills/main/verify_drill.py   # 主演练 15 项
python3 tests/run_tests.py                                               # 14 组场景
bash scripts/check_all.sh                                                # 全部
```

场景覆盖：class 重复数量 ADD、单位精确换算、超量 DELETE（unknown/不截零/不容差）、
同时双父、包装环、转换前向缺证与 lineage 链接（linked/pending/invalid/cycle）、
eventID 冲突暂停、未知 context、空成员 DELETE 等 unsupported、批次拒收、
完全重传多处出处、recordTime 不作业务时间、文件次序/分块不变、重开与复制重开、
转换分量纲统计且包装不入总量。

## 目录结构

```
epcis_warehouse/     主包（jsonld_offline / parse / store / state / query / ingest / warehouse / cli）
  contexts/          打包的固定 context + 白名单
vendor/              PyLD 及依赖（sha256 校验的 wheel 解包）
drills/main/         主演练：gen_drill.py、expected.json（手写预期）、verify_drill.py（独立核验）
tests/run_tests.py   场景测试集
scripts/             memory_check.py、check_all.sh
```
