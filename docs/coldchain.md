# 冷链暴露台账（coldchain）：输入格式与语义

`coldchain` 命令在一个**已发布接收批次**和一个带时区的半开巡检窗口
`[window_start, window_end)` 上，结合同一次 `settle` 的三份产物，离线构建逐件
货的温度暴露台账。命令对事件仓**只读**；运行中建立的索引放在临时 SQLite
库（`ATTACH` 为 `cold`），结束后删除。不改造 EPCIS 接收，也不引入任何传感器
协议——探头、安装与读数都来自质检员给的三份名单。

```bash
python3 -m epcis_warehouse.cli --root WH coldchain \
    --batch 1 \
    --window-start 2026-01-01T23:50:00+00:00 \
    --window-end   2026-01-02T00:10:00+00:00 \
    --opening opening.jsonl \
    --settle-summary settle-out/summary.json \
    --ledger settle-out/ledger.jsonl \
    --items items.jsonl \
    --installations installations.jsonl \
    --samples samples.jsonl \
    --out cold-out/
```

退出码：`0` = 台账建成（存在未知段、未达门槛的超温是正常业务结果）；
`2` = 输入合同错误，**只**写一份 `status: failed` 的 `summary.json`，不交付
任何 coverage/daily/alarm 文件。

所有时刻必须带 UTC 偏移，内部一律归一到 UTC 微秒；`+08:00` 与 `Z` 的等价写法
不改变业务结果。温度是整数毫摄氏度，有效期与告警门槛是正整数秒。

## 1. 归属来源（只用一次 settle 的具名流水）

归属完全来自同一次 `settle` 的三份产物，且**只**用 `ledger.jsonl` 里成功过账单
据的 `named_moves`：

* 期初归属取 `opening.jsonl` 的具名 EPC 行；
* 在每条过账的 `effective` 生效点切换归属，证据回指该交接号；
* 期初未登记的货允许出现在巡检名单里，归属记 `null`（未知），其覆盖段不带任何
  归属证据；
* 未过账单据、期末余额、包装持有者、扫描时刻一律不能用来补归属。

校验：窗口起点不得早于期初；`summary.json` 必须 `status: settled`，其
`inputs.batch` 必须等于本次 `--batch`，`inputs.opening_as_of` 必须与期初文件
一致；ledger 流水必须自洽（每条 named move 的 `from` 等于该 EPC 当前归属，
期初不存在的 EPC 不得被转手），否则按 `ownership_flow_conflict` 整体失败。

## 2. 三份输入名单（逐行 JSON）

### items.jsonl —— 巡检 EPC

```jsonl
{"epc": "urn:example:epc:E", "limit_mc": 8000, "alarm_seconds": 240}
```

* `limit_mc`：温度上限（毫摄氏度，整数）。读数**严格大于**上限才算超温，等于
  上限是正常。
* `alarm_seconds`：连续超温告警门槛（正整数秒）。同一 EPC 重复出现是致命错误。

### installations.jsonl —— 探头安装

```jsonl
{"probe": "pB", "container": "urn:example:pkg:B",
 "from": "2026-01-01T23:50:00+00:00", "to": "2026-01-02T00:07:00+00:00",
 "valid_for_seconds": 300}
```

* 安装区间是左闭右开 `[from, to)`；`to <= from` 直接失败。
* 同一探头**完全相同**的安装记载只算一次；不同安装段允许首尾相接，但**不可
  重叠**——即使装在同一容器也不行，否则无法确认该用哪次安装的有效期，本次
  输入失败（`installation_overlap`）。安装 ID 由内容哈希派生，输入行序不影响
  结果。
* 一个容器同时装多个探头即安装冲突：在该容器有在装探头的时段，温度判为
  `install_conflict`，直到只剩一个或全部卸下。

### samples.jsonl —— 探头读数

```jsonl
{"probe": "pB", "at": "2026-01-01T23:52:00+00:00", "value_mc": 9000}
```

* 同探头、同瞬间、同值的重复读数合并为一次测量；同瞬间不同值是**采样冲突**，
  在该次采样本应覆盖的时段显式保留为 `sample_conflict`，绝不挑值、不回退上一条。
* 读数只在**当次安装内**使用：必须采样于安装期间（半开，恰在卸下时刻采的样不
  属于旧安装），从采样点起有效，截止于「下个采样点 / 有效期满 / 卸下时刻」三者
  最早者。重装到同一容器不能沿用旧安装的读数；未安装期间采的样永久记为
  `outside_install`，装好后也不会变为有效。

## 3. 一件货在某刻看哪个探头

取该刻已生效的**完整包装祖先链**（含查询 EPC 本身，由近及远），找第一个有在装
探头的对象：

* 找的是在装关系，不按谁有温度挑选；近处探头缺读数、已过期或冲突时，**不得退回**
  远处的好读数。
* 链上没有任何在装探头是独立原因 `not_installed`。
* 包装链歧义（同时双父）、成环，或被当前批次的冲突包装事件阻断时，整条温度归属
  为 `chain_unknown`，即使已经走到的前半段有探头；无关包装的异常不连带影响其他货。
* 包装语义完全沿用事件仓：同一 eventTime 的变更加起来生效后再判断；不从转换或
  class 数量推断具名货所在容器。包装链只在真正影响某个巡检 EPC 的时刻重算。

## 4. 六种未知原因（未测到绝不报合格）

| 原因码 | 含义 |
|---|---|
| `not_installed` | 包装链上没有任何在装探头 |
| `not_sampled` | 有在装探头，但当次安装尚无有效读数 |
| `expired` | 最近一条读数的有效期已过（或冲突覆盖期已满） |
| `sample_conflict` | 同瞬间读数互相冲突，覆盖期内显式保留 |
| `install_conflict` | 选中容器上同时有多个在装探头 |
| `chain_unknown` | 包装链歧义、成环或被冲突包装事件阻断 |

另有 `normal` 与 `over_limit` 两个实测状态。不插值、不用平均抵消超温。

## 5. 三份可互相核账的结果（`--out` 目录）

| 文件 | 内容 |
|---|---|
| `coverage.jsonl` | 逐 EPC 把整个窗口切成互不重叠、无空洞的半开段：起止、归属、状态、温度（未知段为 `null` + `reason`）、证据引用 |
| `daily.jsonl` | 按 EPC × UTC 日期 × 归属汇总 `normal`、`over_limit` 与各未知原因的微秒时长；未知归属单列（`owner: null`） |
| `alarms.jsonl` | 达到该 EPC 门槛的连续超温极大区间，含按归属、按归属×日期的超温时长拆分 |
| `evidence.jsonl` | 共享证据：期初行/交接号、采用的包装链或阻断事件、安装、实际采样（冲突时含全部冲突值） |
| `summary.json` | 运行状态、批次与窗口、六个外部文件的 sha256 摘要、计数与各类时长合计 |

### 切段与午夜规则

只在**实际采用的归属、判定或证据变化**处断开，另在每个 UTC 午夜断开；无关探头
或无关包装的变化不产生碎片。每行带稳定的 EPC 内序号 `seg`。

### 日账核账

同一 EPC 同一 UTC 日期下，`normal + over_limit + 全部未知原因` 的时长必须等于
该 EPC 在这一天内的窗口切片；程序在写出前对每个 EPC 强制做这一恒等校验，不满足
即报 `internal_accounting_gap`，不交付结果。

### 告警规则

* 告警是单件货**连续超温的极大区间**，在窗口内持续达到该件自己的门槛才交付；
  换探头、换归属、午夜或新读数本身不必然断开；
* 任何有正时长的正常段或未知段都会断开一次连续超温——一次连续超温因此不会被误
  拆成两个不达门槛的短事件；
* 每条告警能经 `epc + 起止` 回到 coverage 明细，并在 `by_owner` /
  `by_owner_day` 里列出各归属（含未知归属）覆盖的超温时长。

## 6. 证据与可复查性

coverage 行只放**引用**（`opening:`、`handover:`、`chain:`、`installation:`、
`sample:`），原文集中在 `evidence.jsonl`，多件货共用一个包装/信号时共享同一条
证据，不在每行复制。采样冲突的证据保留全部冲突值；链证据给出节点序列、
`unknown`/`cycle` 状态或阻断事件 ID（可再用 `event`/`evidence` 命令回查事件仓）。

## 7. 输入合同错误（退出码 2，不交付台账）

`invalid_batch`、`invalid_window`、`window_before_opening`、`batch_mismatch`、
`opening_mismatch`、`summary_not_settled`、`ownership_flow_conflict`、
`invalid_ledger_line`、`invalid_item`、`duplicate_item`、`empty_items`、
`invalid_installation`、`invalid_installation_range`、`installation_overlap`、
`empty_installations`、`invalid_sample`、`empty_samples`、`invalid_jsonl`、
`input_unreadable`。

## 8. 确定性与规模

* 重复构建同一组输入、或只改名单内的行序/等价时间偏移写法，四个结果文件逐字节
  一致（输入文件行号属于输入定位，不进入业务结果）。
* 样本经流式扫描进入临时 SQLite，全程不把整批读数读入内存；逐 EPC 扫描只访问其
  包装链容器上的探头，不构造 EPC × 全局采样时间的笛卡尔积。
* 规模复现：`python3 scripts/coldchain_scale.py`（默认 5 万巡检 EPC、30 万样本，
  构建进程峰值 RSS 限额 256 MiB，独立进程用 wait4 测量；大数据只落临时目录）。
