# 规格：让漏斗只跟"真·强势股回踩" —— 给观察名单装上强度闸门（Phase L）

状态：📝 待 Codex 实现（方向已由用户确认：中等·双门槛）
作者：规划 by Claude
前置：Phase I（漏斗双清单）、Phase K（复盘笔记提示）已完成。本轮**不新增扫描器、不要新数据**，只把已有的强度信号接进漏斗。

---

## 0. 一句话：这次解决什么

> **现在**：观察名单收的是"近40日被任意扫描器标记过"的票，被最松的 `trend_embryo` 主导。结果一堆**前期没涨过、只是回撤形状像**的弱票混进"回踩到位/再启动"。你一眼就看出来——这些不是强势股。
>
> **改完后**：名单里**每一只都必须先当过强势股**（被 RPS / 龙头 / 突破扫描器认证过，且前期有过明显主升），然后才看它现在回踩到哪一步。漏斗从"挑回撤形状"变成"**先是龙头，再回踩**"——这正是你卡片 `SET-long-leg-dip` 的原话："强势标的先完成一段明显上攻，随后缩量回踩"。

---

## 1. 为什么是这个方向（对着你的目标和笔记）

- 你的目标：每天收敛出**真正的强势股**短名单，含"前期强势股的回踩"；买卖点你定。
- 你卡片里的标准：强势回踩的**第一前提是"明显上攻"**。现在的算法把这个前提整个跳过了——`classify_state` 判"回踩到位/再启动"只看回撤形态，`run_up_pct`（前期强度）从不参与判定。
- 强度信号其实早就在库里：`rps_stock_top20`(相对强度龙头)、`true_leader`(中军龙头)、`momentum_breakout`(突破)；涨停/连板也能用现成 `change_pct` 推。**缺的不是数据，是把强度当成入场门槛。**

---

## 2. 三层缺陷（本轮要修的）

1. **候选池太松**：收"被任意扫描器标记"的票，`trend_embryo`/`second_launch`（形态/接力探测器）单独就能让一只票进池，池子根本不是强势股池。
2. **强度度量错锚**：`run_up_pct = 峰值/标记日收盘 - 1`，锚在标记日。标记晚→run_up≈0；且 run_up=0 本意是"标记后只跌没涨"=弱票。
3. **判定无强度闸门**：`回踩到位`/`再启动` 只看回撤形态，前期涨 0% 的票照样进行动名单。

---

## 3. 要装的闸门（中等·双门槛）

**(A) 强度合格池 —— 替换"被任意标记"**
一只票本轮窗口内要进观察名单，必须**同时**满足：
- **强度来源**：被 `rps_stock_top20` 或 `true_leader` 或 `momentum_breakout` 标记过 ≥1 次；
- **明显上攻**：修正后的前期主升 `run_up_pct ≥ 0.30`。

`trend_embryo` / `second_launch` 的标记**不单独构成强度**（可作共同标记，但不能让一只票仅凭它们入池）。

**(B) 修正 run_up 锚点 —— 量"真主升浪"**
`run_up_pct = 峰值收盘 / 起涨基准 - 1`，其中**起涨基准 = 峰值日前 N 根 K 线内的最低收盘**（N≈20），反映冲峰前最近一段真实涨幅，而非标记日漂移。

**(C) 涨停印记加分 —— 贴"看懂龙头股"**
窗口内涨停天数（主板 `change_pct ≥ 9.8%`；创业板/科创板/北交所 `≥ 19.5%`）计入 `_triage` 排序加权，**只加分、不做硬门槛**（中等档保留弹性）。

**(D) 全池都是强势股**
入池后，五个状态（延伸中/回踩中/回踩到位/再启动/破位失效）描述的是"**这些前期强势股现在回踩到哪一步**"。破位失效 = 这些昔日龙头已破坏结构，提示放弃。整个面板因此名副其实地是"强势股回踩与再启动跟踪"。

---

## 4. 诚实边界（防马后炮）

- 这是**收紧入池标准**，不是预测涨跌、不声称胜率。Phase J 的前向留痕照常记录，验证收紧后名单到底有没有变好。
- 阈值（0.30 主升、N=20 起涨窗、强度扫描器集合、涨停加权）全部参数化，集中在 `_params`，方便你回头调。
- `涨停` 用 `change_pct` 阈值近似，非交易所官方涨停标识；ST 股（5%/不在加分范围）暂按普通处理。

---

## 5. 给 Codex 的实现细节（与上面分开，按需读）

**L1 强度合格池（`mining/watchlist.py::build_watchlist`）**
- 在按 code 聚合 candidates 后，标注每只票窗口内是否被强度扫描器集合 `STRENGTH_SCANNERS = {"rps_stock_top20","true_leader","momentum_breakout"}` 标记过（来自 `flag_strategies`）。
- 入池硬条件：`has_strength_flag AND run_up_pct >= p["min_runup"]`。不满足者整只剔除，不进 watchlist（连"查看全部"也不显示——名单全程只含强势股）。
- 集合与阈值放入 `_params`：`min_runup=0.30`、`strength_scanners=STRENGTH_SCANNERS`、`runup_base_n=20`。

**L2 修正 run_up 锚点（同函数 run_up_pct 计算处，当前 line ~326）**
- `base_close = close_pivot 中 [peak_date 前 runup_base_n 根 .. peak_date] 区间的最小收盘`（用已加载的 `close_pivot`/`all_dates`，不额外查库）。
- `run_up_pct = peak_close / base_close - 1`（base 无效时回退到 `first_flag_close`，保持向后兼容）。
- 保留 `first_flag_close` 字段（其它展示仍用），仅改 `run_up_pct` 口径。

**L3 涨停加分（`_triage`）**
- 新增 `limit_up_count`：窗口内 `change_pct >= limit_up_thr(code)` 的天数；`limit_up_thr` 按代码前缀判主板/创业科创北交所。
- `_triage` 增一项 `w_limit * _norm(limit_up_count, 5.0)`；`w_limit` 入 `_params`（默认小权重，如 0.15），不改既有权重含义。
- `limit_up_count` 一并写入 row，供面板/排序展示。

**L4 状态机不动**
- `classify_state` 逻辑保持（回撤形态判定不变）；强度闸门在 L1 入池层已生效，行动名单自然只剩强势股。无需在 classify_state 里再加 run_up 判断（入池已保证）。

**测试（`tests/test_mining_pipeline.py` 增量）**
- 强度池：仅被 trend_embryo 标记的票被剔除；被 rps/true_leader/momentum 标记且 run_up≥0.30 的保留；run_up<0.30 的强度票也剔除。
- run_up 锚点：构造"标记日在峰值附近"的合成数据，验证修正后 run_up 反映前期真涨幅（旧口径≈0、新口径≥阈值）。
- 涨停加分：有涨停的票 triage 排序高于同形态无涨停票。
- 回归：现有 mining/UI/evaluate 全绿（当前 91）。

**验证命令**
```powershell
python -m unittest tests.test_mining_pipeline tests.test_mining_ui tests.test_mining_features tests.test_mining_evaluate
python -m py_compile mining/watchlist.py
python -c "from mining.streamlit_tabs.tab_scanner import load_watchlist_snapshot; df,e=load_watchlist_snapshot('.','2026-06-26'); print('total',len(df), (df['run_up_pct']>=0.30).all() if len(df) else 'empty')"
```
（最后一行应显示总数明显小于 908，且全部 run_up_pct≥0.30）

**约束**
- 不新增扫描器、不要新数据、不改 Phase J 留痕与 Phase K 提示层。
- 卡片仍是文本真相源；编辑前 `git status --short`，勿回退无关改动；不提交 `knowledge/_raw/`。

---

## 6. 实现记录（由 Claude 实现）

完成时间：2026-06-30

### 实现要点（全部落在 `mining/watchlist.py`，状态机未动）

- **L1 强度合格池**：新增模块常量 `STRENGTH_SCANNERS = ("rps_stock_top20","true_leader","momentum_breakout")`，并放入 `_params`（`strength_scanners`）。按 code 聚合 candidates 后，先按 `flag_strategies` 是否含强度扫描器做第一道硬筛（不含的整只剔除，连"查看全部"都不出现）；定价拿到峰值后，再按 `run_up_pct >= min_runup(0.30)` 做第二道筛。
- **关键修复（前置依赖）**：把 `build_watchlist` 默认 `source_strategies` 从 `("trend_embryo","true_leader")` 扩成 5 个扫描器（加 `second_launch`/`rps_stock_top20`/`momentum_breakout`）。原默认根本没把 `rps_stock_top20`/`momentum_breakout` 载入候选池，`flag_strategies` 里检测不到这两个强度标记——不扩 source，L1 的"rps 或 momentum"分支永远是死代码。`second_launch` 扫描器自带显式 source，不受默认改动影响。
- **L2 修正 run_up 锚点**：`run_up_pct = peak_close / base_close - 1`，`base_close = close_pivot 中 [peak 前 runup_base_n(20) 根 .. peak] 的最小收盘`；base 无效时回退 `first_flag_close`（向后兼容）。`first_flag_close` 字段保留供展示。run_up gate 紧跟峰值计算放在循环前段，被剔除的票直接 `continue`，省掉后续 MA/缩量计算。
- **L3 涨停加分**：新增 `_limit_up_threshold(code)`，复用现成 `mining/features.py::board_kind`（300/301→创业、688/689→科创 取 19.5%，其余主板 9.8%，北交所前缀另判 19.5%）；`limit_up_count` 统计 lookback 窗口内 `change_pct >= 阈值` 天数，写入 row；`_triage` 加一项 `w_limit(0.15) * _norm(limit_up_count, 5.0)`，纯加分、不设硬门槛，既有权重含义不变。

### 名单收敛前后对比（真实数据，2026-06-26）

| 口径 | 数量 |
| --- | --- |
| 旧候选池（trend_embryo+true_leader，无闸门） | 909 |
| 新原始池（5 扫描器并集） | 1357 |
| 强度合格池（rps/true_leader/momentum 去重） | 851 |
| **闸门后漏斗总数**（强度 + run_up≥0.30） | **634**（全部 run_up≥0.30） |

- 状态分布：可动作短名单 = 回踩到位 12 + 再启动 12 = **24**；破位失效 150；延伸中/回踩中合计约 460。漏斗全程只含"曾被强度认证 + 明显主升"的票，弱回撤形态票（仅 trend_embryo、无强度）已整体剔除。
- `limit_up_count > 0` 的票 410/634，涨停印记进入排序加权。

### 诚实边界 / 数据质量观察

- 新锚点把**上游既有的脏数据**放大暴露：`000852`、`000001` 两只以"指数级价格"（8381、4112）混入股票池，导致 run_up 异常（1307、379）。这是行情库里指数代码/价格串入 `sec_type='stock'` 的历史问题，**不属本轮逻辑缺陷**；二者均被判为"破位失效"，**未进入可动作 24 名单**。run_up 仅用于"≥0.30 闸门"与上限封顶（`_norm(run_up,0.8)`）的 triage 加权，异常大值不扭曲排序。若要根治需在上游候选/universe 层过滤指数代码，已超出 Phase L 范围。

### 测试

- 新增 4 个 Phase L 测试（`tests/test_mining_pipeline.py`）：强度池三选一（trend_embryo 剔除 / true_leader+run_up0.5 保留 / rps+run_up0.2 剔除）、run_up 锚点（标记落在峰值时旧口径≈0、新口径=0.5）、涨停 triage 加分、板块阈值。
- 更新 canonical seed `_seed_second_launch_path`：`600001` 增 `true_leader` 强度标记并把前期基准压到 8.0（run_up=0.5），相应回归断言改 `flag_count=2`、`flag_strategies="trend_embryo,true_leader"`、`run_up_pct=0.5`。
- **全量 `python -m unittest` 110 通过**；spec 指定四套件 94 通过（含 4 个新增）；`python -m py_compile mining/watchlist.py` 通过。
