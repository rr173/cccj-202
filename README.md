# EPCIS 2.0 离线事件加工仓

对一批封闭的 EPCIS 2.0 JSON-LD 事件和一张声明单位表做离线加工，输出可按
**业务时间（eventTime）** 与 **接收批次（receiving batch）** 两个维度复查的追溯关系；
并在同一事件仓上提供**离线交接结账**：把发、收双方各自填报的交接单对照指定批次
里已收到的扫描逐条核实，交付可入账的流水、未入账的单据和结账后的余额；以及
**离线选择性披露**：把某批次的编号清册承诺进一份可单独交付的检查点，按编号区间
分页出具凭证，稽核方不接触事件仓即可验页、查缺口、交付逐编号清册。

明确不做的事（profile 边界）：不从包装关系推断物料消耗；不做原料配方、比例分摊、
召回优化或数量预测；交接结账不把包装成员或生产转换当作交接；选择性披露不为真实
世界事件背书、不做密钥分发。

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
python3 -m epcis_warehouse.cli --root WH settle --batch 2 \          # 离线交接结账（只读事件仓）
    --opening opening.jsonl --sender sender.jsonl --receiver receiver.jsonl --out settle-out/
python3 -m epcis_warehouse.cli --root WH coldchain --batch 2 \       # 离线冷链暴露台账（只读事件仓）
    --window-start 2026-01-01T23:50:00+00:00 --window-end 2026-01-02T00:10:00+00:00 \
    --opening opening.jsonl --settle-summary settle-out/summary.json \
    --ledger settle-out/ledger.jsonl --items items.jsonl \
    --installations installations.jsonl --samples samples.jsonl --out cold-out/
# 离线选择性披露（仓管方）
python3 -m epcis_warehouse.cli --root WH checkpoint build --warehouse-id 仓标识 --batch 1 --out cp.json
python3 -m epcis_warehouse.cli --root WH page export --checkpoint 标识 \
    [--lo-id E [--lo-digest D]] [--hi-id E [--hi-digest D]] --out page.jsonl
# 离线选择性披露（稽核方，不需要 --root；检查点经可信渠道获得）
python3 -m epcis_warehouse.cli audit init --workspace WS --checkpoint cp.json [--lo-id E ...] [--hi-id E ...]
python3 -m epcis_warehouse.cli audit receive --workspace WS --page page.jsonl
python3 -m epcis_warehouse.cli audit gaps --workspace WS
python3 -m epcis_warehouse.cli audit finalize --workspace WS --out audit-out/
```

查询结果中的 `events`（事件→内容摘要）与 `evidence`（摘要→原文/展开文件路径）即共享证据表示：
同一证据对象被多条结果引用，不为每条查询展开所有路径。

## 离线交接结账（settle）

仓库和承运方每天各交一份交接单；两边单号一致并不代表账能结。`settle` 在指定
**已发布接收批次**的扫描凭证范围内，把一份带期初时刻的库存、一份发方单据和一份
收方单据（均为逐行 JSON，格式见 [docs/settlement.md](docs/settlement.md)，示例见
[docs/examples/](docs/examples/)）离线核对：

* 两侧同一交接号须同路线、同 UTC 生效瞬间、起讫不同、晚于期初；完全重送只算
  一份，同侧同号异单绝不择一；缺一侧、对不拢、同号异单分别交代原因。
* 每张单只在两侧扫描均能核实时才有资格过账：扫描须是该批次已收到、无冲突的
  有效 ObjectEvent OBSERVE 且只声明与该单相符的发收地；货物从扫描实际内容汇总
  （具名 EPC 比集合，散货按 class+量纲精确合计），事件 ID 一次性使用，跨单复用
  或两侧共用即入异常清单。
* 核对通过的单据按生效时刻过账：同一时刻共享具名 EPC 或散货库存键（地点,
  class, 量纲）的单据连成组一起结账——具名货须时刻开始就归发地且组内不二次
  转手，散货借方总量不得超出时刻开始时的余额（同刻贷记不补借方）；任一检查
  失败整组不入账并标出阻塞依据，无关组照常结账。
* 输出 `summary.json` 与 `ledger/documents/groups/balances.jsonl`：可入账流水
  （每笔变动追到单据与双方扫描）、每张单的结果、组级阻塞依据、期末所属与数量
  余额。期初重复具名归属或无效行、批次无效、单据行识别不出交接号时，不交付
  任何流水（退出码 2）。重复结同一组输入或只改行序，结果逐字节一致。

### 交接结账演练

```bash
python3 drills/settle/gen_drill.py        # 建仓、生成输入、依次跑 4 次结账
python3 drills/settle/verify_drill.py     # 独立核验：手写对账表 vs 实际输出
```

演练剧情（2026-01-01，期初 08:00 甲有 10 kg X / 8 kg Y / 10 kg Z / 1 kg W，
丙有 10 kg W，具名 e1、e2 在甲）：

| 时刻 | 单据 | 结果 |
|---|---|---|
| 10:00 | H1 甲→乙 6 kg X（发扫 2+4 kg，收扫 6000 g）、H2 甲→丙 5 kg X+3 kg Y、H3 甲→丙 2 kg Y | 三单经甲地 X/Y 余额连成一组；X 需 11 仅有 10，整组不过账（Y 够用也不拆账） |
| 10:00 | H4 丙→乙 6 kg W（2 kg+4 kg 对 6000 g） | 不相干的单照过；6 kg 只记一次 |
| 11:00 | H5 甲→乙 6 kg X；H6 乙→丙 6 kg X（run1 同时刻 / run2-4 改 12:00） | 同时刻：乙不能用当刻才到的货，整组被拦；下一时刻：两笔均过 |
| 13:00 | H7/H8 共用同一扫描、H9 缺收方单、H10 收方扫描未收到、H11 甲→丙 1 kg W、H12 具名 e1 过户、H13/H14 同时刻争 e2 | 复用占用、缺证、未收到各自入异常；H11 在批次 1 过账，批次 2 收到冲突内容后改判证据不可用；e2 二次转手整组被拦 |

`drills/settle/expected.json` 是手工核写的对账表（每单结果、组阻塞依据、期末
余额）；run4 用打乱行序的同内容输入复结，四个结果文件与 run2 逐字节一致。

### 规模复现

```bash
python3 scripts/settle_scale.py                 # 25 万单据行：10 万行同刻连成一个大组
python3 scripts/settle_scale.py --lines 20000 --big 8000   # 小规模冒烟
```

脚本在临时目录生成约 25 万扫描事件建仓（不计入限额），再以独立进程结账并用
`wait4` 测峰值 RSS：结账阶段不超过 192 MiB（Linux）。同时校验大组 5 万成员
全部过账、期末余额抽查、重复结账输出逐字节一致。大数据与大结果只落在临时
目录，不进入版本库。

## 离线冷链暴露台账（coldchain）

质检员圈定一个已发布批次和一个带时区的半开巡检窗口 `[起,止)`，再给三份名单
（巡检 EPC 及各自温度上限/连续超温门槛、探头安装容器与区间、探头采样读数），
`coldchain` 结合同一次 `settle` 的期初库存、成功摘要和 `ledger.jsonl`，离线
交付三份可互相核账的结果（格式见 [docs/coldchain.md](docs/coldchain.md)）：

* **逐件温度覆盖明细** `coverage.jsonl`：整个窗口切成互不重叠、无空洞的半开段，
  给出起止、归属、温度或未知原因与可回查证据；只在实际采用的归属/判定/证据变化
  处和 UTC 午夜断开。
* **归属日账** `daily.jsonl`：按 EPC × UTC 日期 × 归属汇总正常、超温与六种未知
  原因的时长，未知归属单列；三类时间加总恒等于窗口（程序强制校验）。
* **连续超温告警** `alarms.jsonl`：连续超温的极大区间，达到该件门槛才交付，按
  归属（含未知归属）拆分超温时长；换探头、换归属、跨午夜不断开，任何正时长的
  正常/未知段才断开。

关键边界：归属只认期初与过账 `named_moves`（未过账单据、期末余额、包装持有者、
扫描时刻都不能补归属，期初未登记的货归属记未知）；读数只在当次安装内有效，重装
不沿用旧读数，恰在卸下时采的样不属于旧安装；近处探头缺读数/过期/冲突不退回远处
好读数；包装链歧义、成环或被冲突包装事件阻断时整条温度归属未知；同瞬间异值读数
显式保留冲突，绝不挑值。合同错误（格式错误、归属流水自相矛盾、批次不符、安装段
重叠等）只交付 `status: failed` 的摘要，退出码 2。

### 冷链演练（可手核的 20 分钟时间条）

```bash
python3 drills/coldchain/gen_drill.py        # 建仓、settle、两次台账构建与两个失败用例
python3 drills/coldchain/verify_drill.py     # 独立核验：手写逐段预期 vs 实际输出文件
```

演练严格复刻题面时间条（UTC 23:50 起 20 分钟，跨午夜）：货 E 内箱探头的尚无读数
（0–2 分）、过期（7–9 分）、采样冲突（12–13 分）都不能用托盘 4°C 填上；仅
2–7 分的连续超温达到 4 分钟门槛，甲负责 3 分钟、乙 2 分钟；9–12 分跨午夜的超温
只有 3 分钟不告警，乙在午夜前后分别计入 1、2 分钟。另有留在内箱的对照 EPC、窗口
前存量与仍有效读数、同刻换装与采样、断开后同探头重装、未过账仍归原地、单容器多
探头冲突、包装双父链未知等场景；`expected.json` 是逐段手写预期，核验脚本只读
输出文件、不调用任何判定函数。

### 冷链规模复现

```bash
python3 scripts/coldchain_scale.py                 # 5 万巡检 EPC、30 万探头样本
python3 scripts/coldchain_scale.py --epcs 2000 --samples 30000   # 冒烟
```

构建台账在独立进程运行并用 `wait4` 测峰值 RSS，限额 256 MiB（Linux，不含造数与
建仓）。样本流式进入临时 SQLite，即使一个时刻有大量样本也不把整批读入内存；只
展开影响巡检货的时间变化，逐货只扫其包装链上的探头，不构造 EPC 与全局采样时间的
笛卡尔积。脚本同时核验：逐 EPC 无空洞覆盖、选定 EPC 的覆盖与告警归属拆分、无关
探头更新不出现在任何明细里、重复构建逐字节一致。

## 离线选择性披露（disclosure / audit）

外部稽核方要核对某段事件编号是否报齐，又不接触事件仓。仓管方先对
**仓标识 + 一个已发布接收批次**构建**检查点**（对有序清册的 Merkle
承诺，同时生成可复用磁盘索引），经**单独的可信渠道**交给稽核方；再把
同一次稽核所需的数据按编号区间分成**凭证页**陆续导出。稽核方在本地
收件工作区里逐页验证、查看缺口，只有验过的页严丝合缝覆盖整个请求
区间才交付最终清册。格式与语义规范（可据以独立实现）见
[docs/disclosure.md](docs/disclosure.md)。

* **清册**：该批次可见、带非空 `eventID` 的记录，每个
  `(event_id, content_digest)` 一个变体；完全重传不加行，同编号不同
  变体都保留（含无效/不支持）。披露行只有编号、摘要、状态、首次接收
  批次、业务时间（UTC 微秒或空）——不含原文、展开体、文件名、地点。
  无编号记录不进清册；空页只证明清册内无条目，不代表来件没有未识别
  记录。清册的多变体计数不改变仓库原有冲突判定。
* **检查点标识**承诺仓标识、批次、格式版本、总变体数与有序清册根；
  与仓内行号、插入顺序、重传出处无关；已发布检查点不被后续批次覆盖。
* **区间分页**：编号按 UTF-8 原始字节排序（不改大小写、不归一化）；
  边界可以是某编号所有变体之前，或该编号的指定摘要处（按解码字节
  比较），不必对应真实行——同编号上万变体也能拆页。每页自带半开子
  区间，命中为零的非零宽页也是有效页。
* **验页**不仅验每行属于检查点，还验证区间无遗漏（首尾、内侧缺行、
  零命中、空检查点）；发送方给的计数或边界不作为证据。页只含区间内
  的行，外加至多两个邻接键（编号+摘要及其行数据散列）与 O(log N) 个
  树节点散列。
* **收件工作区**可关闭重开，绑定一个检查点和一个请求范围；页可乱序，
  同区间重收幂等，正宽重叠拒收，邻接允许；旧批次页、越界页、格式或
  凭证错误、截断、尾随数据一律拒收且不污染进度。完成判据是区间覆盖
  而不是记录数；交付 `inventory.jsonl`（逐变体）、`by_event.jsonl`
  （逐编号汇总，跨页同号只出一条）、`summary.json`（最后落盘）。
* **信任边界**：检查点必须经可信渠道预先交付，不接受从披露页自取的
  根；本功能不为真实世界事件背书、不做密钥分发、不从未获信任的检查
  点推导结论。

### 披露演练

```bash
python3 drills/disclosure/gen_drill.py        # 两批接收、检查点、分页、乱序收件、篡改用例
python3 drills/disclosure/verify_drill.py     # 独立核验：手写预期 + 从规范重写的验页器
```

第一批含 `m-07` 的完全重传、`m-08` 的一个有效变体和一个不同摘要的
非有效变体、边界事件 `m-20` 与一个无编号记录；第二批再收一个
`m-08` 变体。对第一批请求 `[m-07, m-20)`：`m-08` 两个变体拆到不同
页，`[m-10, m-20)` 留作无命中页；乱序收两张非空页并重收一张时仍
报告空区间未覆盖，补齐后清册为 `m-07` 一条、`m-08` 两条、不含
`m-20`。篡改用例（少交一条真实记录、页端扩大越过未披露变体、第二
批的页、末尾截断、重叠页、尾随数据、改摘要）全部拒收且不污染进度；
另有空检查点（仓内只有无编号记录）交付空清册的用例。
`verify_drill.py` 内含按 docs/disclosure.md 独立实现的验页器，并以
仓库既有 `event` 命令为对照锚定摘要，不把被测导出器的清单当预期。

### 披露规模复现

```bash
python3 scripts/disclosure_scale.py                 # 100 万变体，单号 20 万变体
python3 scripts/disclosure_scale.py --variants 40000 --big 8000   # 冒烟
```

运行时生成 100 万变体（一个事件号占 20 万），保留稠密段、内部空段与
只命中 3 条记录的窄页。检查点构建、单页导出、收页、完成稽核各自在
独立进程中以 `wait4` 测峰值 RSS，均不超过 192 MiB（Linux，不含造数
与建仓）。窄页导出用命令自带的 `index_reads` 统计证明只读了
O(命中数 + log N) 行（不扫整份清册），页凭证只有 O(log N) 个散列与
至多两个邻接键；50k 行大页与跨页单号汇总都流式处理；两种分页方案
加乱序收件交付逐字节一致的最终清册。大数据与稽核档案只落在临时
目录，不进入版本库。

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
python3 drills/settle/gen_drill.py && python3 drills/settle/verify_drill.py  # 交接结账演练
python3 drills/coldchain/gen_drill.py && python3 drills/coldchain/verify_drill.py  # 冷链台账演练
python3 drills/disclosure/gen_drill.py && python3 drills/disclosure/verify_drill.py  # 选择性披露演练
python3 tests/run_tests.py                                               # 14 组场景
python3 tests/run_settle_tests.py                                        # 13 组结账场景
python3 tests/run_coldchain_tests.py                                     # 冷链台账场景
python3 tests/run_disclosure_tests.py                                    # 选择性披露场景
bash scripts/check_all.sh                                                # 全部
```

场景覆盖：class 重复数量 ADD、单位精确换算、超量 DELETE（unknown/不截零/不容差）、
同时双父、包装环、转换前向缺证与 lineage 链接（linked/pending/invalid/cycle）、
eventID 冲突暂停、未知 context、空成员 DELETE 等 unsupported、批次拒收、
完全重传多处出处、recordTime 不作业务时间、文件次序/分块不变、重开与复制重开、
转换分量纲统计且包装不入总量。

## 目录结构

```
epcis_warehouse/     主包（jsonld_offline / parse / store / state / query / ingest / warehouse / settle / coldchain / disclosure / audit / cli）
  contexts/          打包的固定 context + 白名单
vendor/              PyLD 及依赖（sha256 校验的 wheel 解包）
docs/                settlement.md、coldchain.md、disclosure.md（披露格式规范）+ examples/（最小输入示例）
drills/main/         主演练：gen_drill.py、expected.json（手写预期）、verify_drill.py（独立核验）
drills/settle/       交接结账演练：gen_drill.py、expected.json（手写对账表）、verify_drill.py
drills/coldchain/    冷链台账演练：gen_drill.py、expected.json（手写逐段预期）、verify_drill.py
drills/disclosure/   选择性披露演练：gen_drill.py、expected.json（手写预期）、verify_drill.py（含从规范重写的验页器）
tests/               run_tests.py、run_settle_tests.py、run_coldchain_tests.py、run_disclosure_tests.py
scripts/             memory_check.py、settle_scale.py、coldchain_scale.py、disclosure_scale.py（规模复现）、check_all.sh
```
