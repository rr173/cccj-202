# 离线选择性披露：检查点、区间页与收件稽核

本文档是**格式与语义规范**：只凭本文即可独立实现检查点重建、页导出、
验页与收件稽核，不需要参考本仓库源码。所有量的编码都在文中给出；
任何实现间的差异都必须能从本文判定对错。

适用场景：外部稽核方要核对某段区间内的事件编号是否报齐。发送方
（仓管方）先发布一份**检查点**（对某仓某已发布接收批次的离线承诺），
经**单独的可信渠道**交给验证方；之后把同一次稽核所需的数据分成若干
**凭证页**陆续发出。验证方只凭预先确认的检查点、自己的请求和收到的
页完成核验，不接触事件仓。

## 1. 信任边界（先读）

* 检查点由**单独的可信渠道**预先交付验证方。验证方**不接受**从披露页、
  或任何未获信任的来源自取的根/检查点。页里出现的检查点标识只用于
  匹配本地已信任的检查点，不构成信任来源。
* 本功能**不为真实世界事件背书**：它只证明"这份清册在该检查点里"，
  不证明事件本身真实、合法或完整覆盖了现实世界。
* 本功能**不做密钥分发**：检查点是内容寻址的散列承诺，不签名；其
  真实性完全由交付渠道保证。
* **不从未获信任的检查点推导结论**。若检查点不可信，则一切验页结论
  均无效。
* 验证方信任检查点所承诺的清册是**按键排序**的（见 §4）；在此前提下，
  邻接键 + 连续段证明 ⇒ 区间无遗漏（§8 给出完整论证）。若仓管方在
  构建检查点时就恶意作假，本机制无法发现——这正是检查点必须经可信
  渠道确认的原因。
* 记录**不带编号**（无 `eventID`）时不进入清册。空页只证明该范围在
  **这份清册**内无条目，不代表原始来件里没有未被识别的记录。
* 清册的多变体计数是披露层视图，**不改变**仓库原有的冲突判定
  （`review` 的 conflicts 语义不变）。

## 2. 键空间与区间

**清册条目（变体）**：批次 `B` 时已可见（`first_batch ≤ B`）且
`event_id` 非空的每条事件记录，按 `(event_id, content_digest)` 去重
后各为一个变体。完全重传不增加变体；同编号不同摘要的变体全部保留，
不以任何"可用版本"盖过无效或不支持的版本。

**键**：`(event_id, digest)`。排序规则：

* `event_id` 按其 **UTF-8 原始字节**字典序比较：不改大小写、不做
  Unicode 归一化、不做任何等价折叠。
* 同编号内，`digest`（内容摘要，32 字节）按**解码后的字节**字典序
  比较（其小写十六进制串的 ASCII 序与之等价）。

**边界**：区间用半开边界表示。一个边界是下列之一：

| 形式 | 含义 |
|---|---|
| 开边界（JSON `null`） | 下界为 −∞（一切之前）/ 上界为 +∞（一切之后） |
| `{"event_id": E}` | 在编号 E 的**所有变体之前** |
| `{"event_id": E, "digest": D}` | 在编号 E 的摘要 D **处**（从该摘要起包含） |

边界**不要求**对应真实存在的行。边界的全序：−∞ < `(E,)` <
`(E, D)`（D 按字节序）< +∞；不同编号按编号字节序。键 `k` 与边界
`b` 的比较把 `b` 视作占位（`(E,)` 小于 E 的任何变体）。

**区间** `[lo, hi)`：下界包含、上界不含。请求与页都要求**非零宽**
（`lo < hi`）。页区间必须整个落在请求区间内。命中为零的非零宽页
是有效页。

## 3. 稳定编码原语

* **canonical JSON**：`json.dumps(obj, sort_keys=True,
  separators=(",", ":"), ensure_ascii=False)` 的 UTF-8 编码。即键按
  码点排序、无空白、非 ASCII 字符不转义。
* **散列**：一律 SHA-256。十六进制一律小写、64 字符。
* **整数**：无特别说明时，二进制编码为大端。`u32` = 4 字节无符号；
  `i64` = 8 字节有符号（`struct.pack(">q")`）。
* **域分离标签**（单字节，ASCII）：`0x4C`('L') 叶、`0x4E`('N') 内部
  节点、`0x52`('R') 行数据散列、`0x50`('P') 填充叶。节点与数据类别
  由标签区分，不会跨类别碰撞。

**披露行**：`{event_id, digest, status, first_batch, event_us}`

* `status` ∈ `ok | unsupported | invalid`（仓中记录状态；
  `uninterpretable` 的记录没有 event_id，不会出现在清册中）。
* `first_batch`：首次接收批次号（正整数）。
* `event_us`：业务时间（eventTime）的 UTC 微秒值，或 `null`。

行数据散列（承诺行的非键部分）：

```
row_data_hash = SHA256( 0x52 || status_code || u32(first_batch) || enc_us )
status_code   = ok:0x01 | unsupported:0x02 | invalid:0x03
enc_us        = 0x00                      （event_us 为 null）
              | 0x01 || i64(event_us)     （否则）
```

## 4. 有序清册承诺（Merkle 树）

清册 = 按 §2 键序排列的 N 个披露行。树有 `N' = 2^L` 个叶位，
`L = 0`（N ≤ 1）或 `L = ceil(log2(N))`；叶 `[0, N)` 是真实行，
`[N, N')` 为填充。

```
leaf(i)  = SHA256( 0x4C || u32(len(eid)) || eid || digest(32B)
                   || row_data_hash )
PAD(0)   = SHA256( 0x50 )
PAD(h)   = SHA256( 0x4E || PAD(h-1) || PAD(h-1) )     （纯填充子树）
node(h,i)= SHA256( 0x4E || node(h-1,2i) || node(h-1,2i+1) )
           （子节点超出 N 时用对应的 PAD）
root     = N=0 时 PAD(0)；否则 node(L, 0)
```

## 5. 检查点

检查点 JSON（`epcis-disclosure/1`）：

```json
{
  "format": "epcis-disclosure/1",
  "checkpoint": "<id，64 小写 hex>",
  "warehouse": "<仓标识，非空串>",
  "batch": <已发布接收批次号>,
  "variants": <N，总变体数>,
  "root": "<有序清册根，64 小写 hex>"
}
```

**检查点标识**：

```
checkpoint = SHA256_hex( canonical_json({
    "batch": batch, "format": "epcis-disclosure/1", "root": root,
    "variants": variants, "warehouse": warehouse }) )
```

即标识承诺仓标识、批次、格式版本、总变体数与有序清册根。验证方
收到检查点文件后应自行重算并比对（自检一致性）；其**真实性**由
可信渠道保证（§1）。

性质：相同逻辑输入（同仓标识、同批次、同批可见变体集合）必得同一
标识——与仓内行号、插入顺序、重传出处无关；已发布批次的可见集
不再变化，故**已发布的检查点不会被后续批次覆盖**（重建同批次检查
点得到同一标识）。

## 6. 页格式（JSONL，`epcis-disclosure-page/1`）

页文件为 UTF-8 JSONL：第 1 行是页头，随后恰好 `row_count` 行披露
行，然后必须立即 EOF。截断（行数不足或末行残破）或尾部附有未消耗
数据都必须拒收。

页头：

```json
{
  "format": "epcis-disclosure-page/1",
  "checkpoint": "<所属检查点标识>",
  "lo": <边界，见 §2>, "hi": <边界>,
  "start": <首行在清册中的叶序号，从 0 计>,
  "row_count": <本页行数 k>,
  "left_edge":  null | {"event_id": E, "digest": D, "row_hash": H},
  "right_edge": null | {"event_id": E, "digest": D, "row_hash": H},
  "proof": ["<64 hex>", ...]
}
```

随后每行一个披露行（字段恰好为 `event_id, digest, status,
first_batch, event_us`；`digest` 为 64 小写 hex）。

**边叶（edge）**：被证明段向两侧各扩一格（存在时）。`left_edge`
是序号 `start-1` 的叶，`right_edge` 是序号 `start+row_count` 的
叶。边叶只公开**键**（编号+摘要）和该行非键部分的散列
`row_hash = row_data_hash`（§3）——不公开其状态、批次、业务时间。
验页方以 `leaf = SHA256(0x4C || u32(len) || eid || digest ||
row_hash)` 复算边叶散列。除至多两个邻接键及其行数据散列外，页
不含请求范围外任何记录的内容。

**被证明段** `[a, b)`：`a = start - (left_edge?1:0)`，
`b = start + row_count + (right_edge?1:0)`。约束：无左边叶时
`start == 0`；无右边叶时 `start + row_count == N`；`0 ≤ a ≤ b ≤ N`。

## 7. 区间证明的生成与验证

记树高 `L`（§4）。生成方对段 `[a,b)` 自根向下递归（初始
`rec(0, L)`，节点 `(lo, h)` 覆盖 `[lo, lo+2^h)`）：

```
rec(lo, h):
  若 [lo, lo+2^h) 与 [a,b) 不相交:   proof.append(node(h, lo>>h))
  若 [lo, lo+2^h) 完全含于 [a,b):    （不输出，由验证方自算）
  否则:                              rec(lo, h-1); rec(lo+2^(h-1), h-1)
```

验证方持 `[a,b)` 的叶散列流（边叶、逐行、边叶，按序）与 `proof`
散列流，镜像递归重算根：

```
rec(lo, h):
  不相交:        return proof 的下一个散列
  完全含于:      return 从叶流取 2^h 个散列自底向上合并的子树根
  否则:          return SHA256(0x4E || rec(左) || rec(右))
```

叶流合并（`size = 2^h` 个叶）：按序入栈，栈顶两棵同高子树即合并，
最终剩唯一高度 h 的根。两流必须**恰好同时耗尽**；重算根必须等于
检查点的 `root`。叶的位置由证明结构绑定：段位置或树形不同则重算
根不同（抗原像攻击下不可伪造）。

## 8. 验页全流程（验证方清单）

输入：本地已信任的检查点 `(warehouse, batch, N, root)`、请求
`[req_lo, req_hi)`、一页。全部通过才接受，任一失败即拒收：

1. **格式**：JSONL 结构、字段、类型、hex 形态全部符合 §6；行数与
   `row_count` 一致；EOF 紧随末行。
2. **归属**：页头 `checkpoint` 等于本地检查点标识。
3. **区间**：`req_lo ≤ lo < hi ≤ req_hi`（非零宽且在请求内）。
4. **行内序与范围**：逐行键严格递增，且每行键 ∈ `[lo, hi)`。
5. **邻接完备性**：有左边叶则 `key(left_edge) < lo`；有右边叶则
   `key(right_edge) ≥ hi`。
6. **位置一致性**：§6 的 `a, b` 约束（无左边叶 ⇒ `start=0`；无右
   边叶 ⇒ 段终于 `N`；`0≤a≤b≤N`）。
7. **凭证**：§7 重算根 == 检查点 `root`，两流恰好耗尽。

**为何无遗漏**：清册按键排序（§1 的信任前提）。被证明段是清册中
**连续**的叶序列（位置由证明绑定）。若 `[lo, hi)` 内存在未交付的
键 k：k 不可能在段内（段内叶逐行交付且与范围核对过）；k 在段左则
`k ≤ key(left_edge) < lo`，矛盾；k 在段右则 `k ≥ key(right_edge)
≥ hi`，矛盾。零命中页（`row_count=0`）同理：两边叶相邻，夹逼出
区间内无键。空检查点（N=0）下任何页都无行无边叶，证明即
`[PAD(0)]`，根即 `PAD(0)`——空页只证明清册内无条目（§1）。

**不得**只验每条返回行的成员证明（那查不出中间少行）；**不得**把
发送方给的计数或边界当证据（一切计数、位置都要经证明绑定）。

## 9. 页泄露了什么（信息披露面）

一页向验证方公开：区间内全部披露行；至多两个**邻接键**（编号 +
摘要）及其行数据散列（不含其状态/批次/时间）；O(log N) 个树节点
散列；检查点标识与页区间。不公开：事件原文、展开体、源文件名、
地点及任何行内容字段（行内只有编号、摘要、状态、首次批次、业务
时间）。验证方据此无法还原范围外记录的内容。

## 10. 收件工作区语义

工作区绑定**一个**检查点和**一个**请求范围（`audit init`）。已存在
的工作区若绑定不同的检查点或请求，不得继续使用（拒绝 init）。

收页（`audit receive`）：

* 乱序到达合法。页先过 §8 全部验证，再与已收区间比对。
* **同一区间**重收：有效页内容唯一（由检查点与区间决定），验过即
  幂等空操作，记录与覆盖范围都不重复计数；对该区间的**坏重放**
  （任何字节变动）验证失败，必须拒收。
* **不同区间**有正宽重叠即拒收；邻接（`hi == lo'`）允许。
* 任何失败（旧批次页、越界、格式错、凭证错、截断、尾随数据、重
  叠）都不得污染已收进度或留下部分验过的行：验证与入库在同一
  事务中，失败整体回滚。

查看（`audit gaps`）：列出已覆盖区间与仍缺区间；不得因记录数看似
正确就关闭稽核——完成判据只看**区间覆盖**。

交付（`audit finalize`）：仅当已验页严丝合缝覆盖整个请求才交付，
否则退出码 2、不写任何结果文件。交付物：

* `inventory.jsonl`：逐变体一行（`event_id, digest, status,
  first_batch, event_us`），按 §2 键序；跨页同号自然归并。
* `by_event.jsonl`：逐编号一行 `{event_id, variants,
  multi_variant}`；同编号跨页只出一个汇总。
* `summary.json`：最后落盘（其出现即"完成"标记）：检查点标识、
  仓标识、批次、请求、变体总数、编号总数、多变体编号数。

整段确实为空时交付空清册（`inventory.jsonl` 为空），但仍须有覆盖
凭证（若干有效空页铺满请求）。重开、收件次序、合理改分页不改变
最终业务清册（输出由行集合与键序决定，与收件历史无关）。

## 11. 命令与磁盘索引

```bash
# 仓管方（需要 --root）
python3 -m epcis_warehouse.cli --root WH checkpoint build \
    --warehouse-id 仓标识 --batch 1 [--out checkpoint.json]
python3 -m epcis_warehouse.cli --root WH page export \
    --checkpoint 标识 [--lo-id E [--lo-digest D]] [--hi-id E [--hi-digest D]] \
    --out page.jsonl
# 稽核方（不需要 --root；检查点文件经可信渠道获得）
python3 -m epcis_warehouse.cli audit init --workspace WS \
    --checkpoint checkpoint.json [--lo-id E ...] [--hi-id E ...]
python3 -m epcis_warehouse.cli audit receive --workspace WS --page page.jsonl
python3 -m epcis_warehouse.cli audit gaps --workspace WS
python3 -m epcis_warehouse.cli audit finalize --workspace WS --out DIR
```

`checkpoint build` 同时生成可复用磁盘索引
`WH/disclosure/<checkpoint>/index.sqlite`（叶表 + 各层节点表 +
检查点元数据）；`page export` 只读该索引，不重读事件仓，并按
B 树定位区间端点——导出只命中少数记录的页时，叶/节点读取数为
O(命中数 + log N)（命令输出 `index_reads` 统计，可复核）。索引是
承诺内容的物化缓存，可随时由 §3–§5 重建校验，不是信任来源。
