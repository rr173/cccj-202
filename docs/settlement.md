# 交接结账（settle）：输入格式与语义

`settle` 命令把发方与承运（收）方各自填报的一日交接单，对照指定接收批次里
已收到的扫描事件做离线核对，产出库存系统可入账的流水、未能入账的单据清单和
结账后的余额。命令对事件仓**只读**；运行中建立的索引放在临时 SQLite 库，
结束后删除。既有查询行为不变；包装成员与生产转换不参与交接结账。

```bash
python3 -m epcis_warehouse.cli --root WH settle \
    --batch 2 \                 # 已发布的接收批次号，或 latest
    --opening opening.jsonl \   # 期初库存（逐行 JSON）
    --sender sender.jsonl \     # 发方单据（逐行 JSON）
    --receiver receiver.jsonl \ # 收方单据（逐行 JSON）
    --out settle-out/           # 结果目录
```

退出码：`0` = 完成结账（部分单据未入账是正常业务结果）；`2` = 输入存在致命
错误，未交付任何流水。

## 1. 期初库存 `opening.jsonl`

逐行 JSON，每行一个对象。有且仅有一行期初头，声明期初时刻；其余每行声明一个
具名 EPC 的唯一所属地点，或某地点对一个 class 的一笔数量。

```jsonl
{"type": "opening", "as_of": "2026-01-01T08:00:00+00:00"}
{"type": "epc", "epc": "urn:example:epc:e1", "location": "urn:example:loc:A"}
{"type": "quantity", "location": "urn:example:loc:A", "class": "urn:example:class:X", "quantity": "10", "uom": "KGM"}
{"type": "quantity", "location": "urn:example:loc:A", "class": "urn:example:class:X", "quantity": "500", "uom": "GRM"}
```

* `as_of` 必须带 UTC 偏移；数量库存按 **(地点, class, 量纲)** 归并——上面两行
  X 归并为 `(A, X, mass) = 10.5 kg`（用单位表精确换算，与事件仓同一套
  `fractions.Fraction` 运算，无浮点容差）。
* `quantity` 接受整数、小数字符串或 JSON 数值，必须非负；`uom` 必须在单位表中。
* 同一 EPC 的归属行出现两次（**即使地点相同**）、任何无效行、缺少期初头或
  期初头重复，都是致命错误：本次不交付任何流水。

## 2. 单据文件 `sender.jsonl` / `receiver.jsonl`

两个文件格式相同，每行一张单据；所属侧别由文件决定。

```jsonl
{"handover": "H-2026-0101", "from": "urn:example:loc:A", "to": "urn:example:loc:B", "effective": "2026-01-01T10:00:00+00:00", "scans": ["urn:uuid:9f1...", "urn:uuid:7a2..."]}
```

* `handover`：交接号，非空字符串。**无法识别交接号的行是致命错误**（不是
  JSON、不是对象、`handover` 缺失或不是非空字符串）。
* `from` / `to`：发地、收地，与事件里的地点标识**直接字符串比较**；起讫必须不同。
* `effective`：双方约定的生效时刻，必须带 UTC 偏移，且**晚于期初时刻**。
  比较时按 UTC 瞬间归一，`+08:00` 与 `Z` 等写法差异不影响判断。
* `scans`：该侧使用的扫描事件 ID 列表，非空、无重复元素。
* 只认以上五个字段；出现其他字段记为单据级错误（`invalid_document`）。

**去重与冲突**：内容完全一致（扫描 ID 列表次序、时刻偏移写法不计）的重送
只算一份，也不重复产生扫描占用声明。同一侧同一交接号出现内容不同的版本，
记 `conflicting_documents`——绝不择一使用。

**配对**：两侧同一交接号必须同路线（`from`/`to` 相同）、同 UTC 生效瞬间。
缺一侧记 `missing_sender` / `missing_receiver`；对不拢记 `mismatch`；
同号异单记 `conflicting_documents`——三种原因分别交代。

## 3. 扫描核实

每张单只有在两侧扫描均能核实时才有资格过账。对单据引用的每个事件 ID：

* 必须是指定批次**已收到**（`evidence_missing`：尚未收到）、内容无冲突的
  有效 `ObjectEvent OBSERVE`。事件非 ok（invalid/unsupported）、同 ID 内容
  冲突、不是 OBSERVE、声明的发收地与单据不符、扫描时刻不晚于期初或晚于单据
  生效时刻，都记 `evidence_unusable` 并附具体原因。
* 同 ID 原事件重传不放大扫描量（按 ID 核实一次）。
* 最后一次发扫不得晚于第一次收扫（`scan_time_order`）。
* 每侧可以有多次扫描；货物**从扫描的实际内容汇总**，不相信表单数量：
  * 具名 EPC：两侧集合必须一致（`goods_mismatch`）。同侧跨扫描重复出现的
    EPC 不是两件货——记 `epc_scanned_twice` 并拦账。
  * 散货：两侧按 (class, 量纲) 以基准单位精确合计，逐键一致才算核对通过。

**扫描占用**：事件 ID 是一次性证据。同一个 ID 被不同的 (交接号, 侧别) 声称
使用（包括同一交接号的两侧共用、跨交接号复用），记 `scan_reused`，所有有关
交接号都进入异常清单——即使其中一单只有一侧或另有错误。完全重送的同一单据
不新增一次声明。

## 4. 过账

核对通过的单据按生效时刻过账，同一时刻**不按单号抢库存**：

* 共享同一具名 EPC，或在任一借方（发地减账）、贷方（收地增账）共用散货
  库存键 (地点, class, 量纲) 的单据连成**组**；共享关系可以经另一张多货种
  单传递。组内各张单一起结账。
* 具名货须在该时刻开始时就归发地，且组内不能有两次转手同一 EPC。
* 散货对每个键看该组的借记总量是否超出**时刻开始时**的余额（未列余额当
  零）；不能先用同刻贷记补足借方。
* 任一检查失败，整组一笔都不入账，并在 `groups.jsonl` 标出直接阻塞的 EPC
  或余额键；没有共享键的其他组照常结账。成功组同时生效，下一时刻可动用新
  余额。

这样，文件排序不同不会把货分配给不同单据，也不会给一张实际不可拆开的交接
单只入半张账。

## 5. 输出（`--out` 目录）

每次运行先清理目录内旧的同名结果文件，然后写出：

| 文件 | 内容 |
|---|---|
| `summary.json` | 运行状态（`settled`/`failed`）、输入定位、计数 |
| `ledger.jsonl` | 每笔过账：交接号、生效时刻、路线、具名与散货变动、双方扫描 ID、所属组 |
| `documents.jsonl` | 每张单的结果：`posted` 或 `failed` + 原因数组 + 组号 |
| `groups.jsonl` | 每组：时刻、状态、成员数；被拦组附共同阻塞依据（不必给每张单复制整组详情） |
| `balances.jsonl` | 期末：每个具名 EPC 的所属地点；每个 (地点, class, 量纲) 的精确余额 |

异常原因码：`invalid_document`、`conflicting_documents`、`missing_sender`、
`missing_receiver`、`mismatch`、`scan_reused`、`evidence_missing`、
`evidence_unusable`、`scan_time_order`、`epc_scanned_twice`、`goods_mismatch`、
`group_blocked`。异常条目都带交接号、侧别/输入定位与相关扫描 ID。

**致命错误**（`summary.json` 中 `status: "failed"`，退出码 2，不写出四个
结果文件）：批次号不是已发布批次（`invalid_batch`）；期初重复具名归属
（`opening_duplicate_epc`）、期初无效行（`opening_invalid_line`）、期初头
缺失或重复；单据行坏到不能识别交接号
（`document_unidentifiable_handover`）；输入文件不可读（`input_unreadable`）。

**确定性**：重复结同一组输入、或只改输入文件内的行序，四个结果文件逐字节
一致（异常里的行号是输入定位，不属于业务结果）。

## 6. 示例

`drills/settle/` 是完整可核账的演练（见 README「交接结账演练」）；
`docs/examples/` 留有一份最小输入示例：

* `opening.jsonl`：甲地 10 kg X、具名 e1 归甲。
* `sender.jsonl` / `receiver.jsonl`：一张 H-1 甲→乙 6 kg X 的单
  （发方两次扫描 2 kg + 4 kg，收方一次 6000 g）。
