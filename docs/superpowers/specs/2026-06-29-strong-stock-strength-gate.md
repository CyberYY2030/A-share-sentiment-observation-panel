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

## 6. 实现记录（Codex 填写）

完成时间：
（实现要点、实际名单收敛前后对比、测试数）
