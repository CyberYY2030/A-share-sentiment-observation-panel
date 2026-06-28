# 选股系统下一步规格：沉淀清单 → 可动作漏斗（状态机 + 双清单）

状态：待实现（实现交给 Codex）
作者：规划 by Claude
前置：`2026-06-27-watchlist-sedimentation-and-second-launch.md`（Phase H）已完成，78 测试通过。H1 产出 `build_watchlist`（真实 2026-06-25 → 908 行）；H3 终判 `second_launch=no_edge`。
触发：用户选定方向 B「把现有内容打磨好用」，要求从原理出发、做高效实用的选股漏斗，不要马后炮辞藻。

---

## 0. 第一性原理：这个系统的真实工作

**选股系统 = 漏斗，不是存档。** 每个交易日把"所有近期强势票"收敛成**当天可动手、短到能逐只判断**的清单。人工一天能认真复核 ~30 只 → 漏斗出口必须 ≤30 且按交易含义排序。

**已证明的硬约束（决定设计方向）**：机械打分排不出 t+5 超额（`second_launch` 即"回踩到位"机械版，判 `no_edge`）。故**不再堆指标预测收益**。定位：**人是 alpha，系统是漏斗**——系统负责收敛到状态正确的几十只 + 给齐人工判断的上下文；买卖由人定。

**H1 现状病根（真实数据）**：908 行无状态大表——315 只一次性标记、~25% 几乎没回踩（仍在新高附近）、最深 −59% 已破位；排序键"最近标记日"与决策无关。它把本质是**序列**的「强势→回踩到位→再启动」拍平了。

**本轮目标**：把 `build_watchlist` 升级成**生命周期状态机**，默认只出两张可动作短清单（预备 + 触发），列改成决策上下文。

**用户已定取舍**：低吸风格 = **两栏都要**——「回踩到位·预备」（左侧提前盯）+「再启动·触发今日」（右侧可动手）。

**纪律护栏**：本层是**分诊/决策支持，不声称预测收益**；UI 不出 edge 字样；`evaluate.py` 真值闸门保留，未来若要主张"分诊顺序有价值"再同口径量，不预设。

**边界**：不改 `trend_embryo`/`true_leader`/`momentum`/`rps`/`second_launch`/`universe.py` 算法与默认参数。纯打磨层：扩 `watchlist.py` + 改 `tab_scanner.py` 视图 + 抽一个共享缩量助手。

---

## 1. Phase I1：状态机 + 决策字段（扩 `mining/watchlist.py`，不 fork）

`build_watchlist` 在现有字段基础上**新增**每只票字段（向量化，禁止逐票 SQL；新增一次 volume pivot 加载、补 ma60）：

**原始强度（透明价格口径，不依赖拟合）**
- `run_up_pct = peak_close_since_flag / first_flag_close − 1`（首次标记到峰值的实涨幅）。
- 沿用 `flag_count`（被标记天数）。
- 可选：若 `candidates.features_json` 含 `rps_N`/`ret_5d`，取标记日最大值填 `flag_strength_raw`（缺则留空，不强求）。

**回踩健康度（复用 second_launch 量能逻辑——抽公共助手 `mining/features.py::volume_shrink_ratio(...)` 或 `mining/watchlist.py` 内部，两处共用，不复制）**
- `shrink_ratio = 近 vol_recent_n(5) 日均量 / 标记日附近 vol_run_n(5) 日均量`。
- `pullback_red_days`：峰值日→今日，单日 `change_pct < red_day_thr`（默认 −5%）的天数（回踩是否有序）。
- `max_down_day`：同段最大单日跌幅。
- `made_new_low_recent`：近 `stop_n(3)` 日是否创新低（止跌判据）。

**支撑/触发**
- 沿用 `ma_proximity`（贴 MA10/MA20）；新增 `ma60`、`below_ma60`（破位判据）。
- `reclaim_ma10 = close_today >= ma10`（再启动触发）。
- `vol_expand_up = 今日为阳线 且 今日量 > 近5日均量`（再启动放量确认）。

**状态分类** `classify_state(row, params) -> str`（规则优先级从上到下，参数全部可配）：
1. `破位失效`：`pullback_pct < broken_pullback(−0.30)` 或（`below_ma60` 且 `made_new_low_recent`）。
2. `再启动`：处于回踩深度带 `[ready_hi(−0.25), ready_lo(−0.08)]` 且 `reclaim_ma10` 且 `vol_expand_up` 且 非 `made_new_low_recent`。
3. `回踩到位`：`pullback_pct ∈ [ready_hi, ready_lo]` 且 `ma_proximity <= ma_tol(0.03)` 且 `shrink_ratio <= shrink_max(0.7)` 且 非 `made_new_low_recent`。
4. `回踩中`：`pullback_pct < extend_max(−0.05)` 但未达"回踩到位/再启动"。
5. `延伸中`：`pullback_pct >= extend_max`（仍贴峰值）。

**分诊分（仅复核优先级排序，文档注明非收益预测）**
```
triage = w_run*norm(run_up_pct) + w_flag*norm(flag_count)
       + w_shrink*clip(1 − shrink_ratio) + w_support*clip(1 − ma_proximity/ma_tol)
       + w_trigger*(reclaim_ma10 & vol_expand_up) − w_dirty*pullback_red_days
```
默认权重均 ~1.0、`w_dirty` 0.5；**不对收益调参**。`build_watchlist` 返回新增 `state`、`triage`、上述字段，整体按 `triage desc` 排序。

`build_watchlist` 签名加可选 `params: dict | None`（带上述默认），保持向后兼容。

## 2. Phase I2：面板双清单视图（改 `mining/streamlit_tabs/tab_scanner.py`）

替换当前"908 行单表 dump"为：

- **「回踩到位·预备（N）」**：`state=='回踩到位'`，按 `triage desc` 取 ≤ `top_n(30)`。
- **「再启动·触发今日（M）」**：`state=='再启动'`，同排序。
- 两表并列（复用现有 `st.columns(2)` 风格）。
- **决策列**（一行=一个判断）：`代码 | 名称 | 状态 | 原始强度(run_up%) | 标记次数 | 峰值回撤% | 缩量比 | 距MA% | 今日站回MA10 | 回踩大阴线 | 距标记日 | 来源策略`。
- 全量沉淀（全部状态 908）收进 `st.expander("查看全部沉淀名单")`，默认折叠、可筛。
- `_prepare_watchlist_display` 改造为按"可动作子集 + 决策列"渲染；空集各自给 `st.info`。

## 3. Phase I3：诚实登记（不新建测量，仅留口）

- `state` 字段使 future evaluate 可把"回踩到位/再启动"当候选集复跑评估——**本轮不跑、不声称有 edge**，仅在 `build_watchlist` docstring 注明可评估口径。
- 不改 `evaluate.py`。

---

## 4. 交付顺序

```
I1 watchlist.py 状态机 + 决策字段 + 共享缩量助手 + 测试
I2 tab_scanner 双清单视图 + 决策列 + 全量折叠 + 测试
I3 docstring 评估口径注记（无新建）
```

---

## 5. 测试

- `classify_state`：构造覆盖 5 状态的行，断言各规则边界（深度带上下沿、ma_tol、破位、止跌、再启动触发）正确归类。
- 新字段：`run_up_pct/shrink_ratio/pullback_red_days/made_new_low_recent/reclaim_ma10/vol_expand_up` 数值正确；缺历史窗口边界返回 NaN/False、不抛错。
- 共享缩量助手与 `second_launch` 现有口径一致（无行为漂移）。
- 双清单过滤：只含目标状态、≤ top_n、空集安全。
- 现有 78 测试保持全绿。

## 6. 验证命令

```powershell
python -m unittest tests.test_mining_features tests.test_mining_pipeline tests.test_mining_ui tests.test_mining_evaluate
python -m py_compile mining/watchlist.py mining/features.py mining/streamlit_tabs/tab_scanner.py
```
真实数据：
```powershell
python -c "from mining.db import connect; from mining.watchlist import build_watchlist; wl=build_watchlist(connect(base_dir='.'),'2026-06-25'); print(wl['state'].value_counts()); print((wl['state']=='回踩到位').sum(),'预备 /',(wl['state']=='再启动').sum(),'触发')"
```
验收：
1. 测试全绿、py_compile 通过。
2. 真实数据上 908 行被分到 5 状态；「回踩到位」+「再启动」合计为可动作短清单（数量级几十，远小于 908）。
3. 面板默认显示双短清单 + 决策列，全量折叠在 expander。
4. 全程未改其他 scanner 算法/默认参数；UI 无 edge 声称。

---

## 7. 约束

- 纯打磨层；不改 scanner 算法、默认参数、`universe.py`、`evaluate.py`。
- 向量化优先；新增 volume pivot 一次加载，禁止逐票 SQL。
- 缩量逻辑抽公共助手，`second_launch` 与 `watchlist` 共用，不复制粘贴。
- 状态阈值集中为可配 params，注释每条规则的交易含义。
- 编辑前 `git status --short`，勿回退无关改动；不提交 `knowledge/_raw/`。

---

## 8. 实现记录（Codex 填写）

### Phase I1：状态机 + 决策字段
- 已扩展 `mining/watchlist.py::build_watchlist`，保留原签名并新增可选 `params`，返回字段新增 `run_up_pct/first_flag_close/flag_strength_raw/shrink_ratio/pullback_red_days/max_down_day/made_new_low_recent/ma60/below_ma60/reclaim_ma10/vol_expand_up/state/triage`。
- 共享缩量助手已抽到 `mining/features.py::volume_shrink_ratio(recent_volume, run_volume)`，`watchlist.py` 与 `mining/scanners/second_launch.py` 共用；未修改 `second_launch` 默认参数。
- 状态规则集中在 `classify_state(row, params)`：`破位失效`、`再启动`、`回踩到位`、`回踩中`、`延伸中`，默认阈值为 `broken_pullback=-0.30`、ready 带 `[-0.25,-0.08]`、`extend_max=-0.05`、`ma_tol=0.03`、`shrink_max=0.7`、`stop_n=3`。
- `triage` 只用于人工复核优先级排序，不声称预测收益；成分为原始涨幅、标记次数、缩量、贴均线、今日触发，扣减回踩大阴线天数。
- 真实数据 `2026-06-25`：共 908 行，状态分布 `延伸中=319`、`破位失效=294`、`回踩中=249`、`再启动=27`、`回踩到位=19`。

### Phase I2：面板双清单
- 已改造 `mining/streamlit_tabs/tab_scanner.py`：默认展示双短清单，左侧 `回踩到位·预备`，右侧 `再启动·触发今日`，各自通过 `split_actionable_watchlist(..., top_n=30)` 按 `triage desc` 截断。
- `_prepare_watchlist_display` 改为决策列：`代码 | 名称 | 状态 | 原始强度% | 标记次数 | 峰值回撤% | 缩量比 | 距MA% | 今日站回MA10 | 回踩大阴线 | 距标记日 | 来源策略`。
- 全量沉淀名单放入 `st.expander("查看全部沉淀名单")`，默认折叠，并提供状态筛选；UI 文件确认无 `edge` 文案。

### Phase I3：评估口径注记
- 已在 `build_watchlist` docstring 注明：`state` 可作为 future evaluation cohort，但当前函数只是 triage layer，不声明 predictive edge。
- 本轮未修改 `evaluate.py`，未新增测量结论，未改任何 scanner 算法、默认参数或 `mining/universe.py`。

### 测试覆盖
- 新增/更新测试覆盖：`volume_shrink_ratio` 数值与无效分母；`classify_state` 五状态优先级与边界；`build_watchlist` 新字段 `run_up_pct/shrink_ratio/pullback_red_days/made_new_low_recent/reclaim_ma10/vol_expand_up/state/triage`；`split_actionable_watchlist` 状态过滤与 `top_n`；UI 决策列与无 edge 文案。
- 已通过：`python -m py_compile mining/watchlist.py mining/features.py mining/streamlit_tabs/tab_scanner.py`。
- 已通过：`python -m unittest tests.test_mining_features tests.test_mining_pipeline tests.test_mining_ui tests.test_mining_evaluate`，共 83 tests OK。
- 已通过真实数据命令：`build_watchlist(conn, "2026-06-25")` 输出 908 行，`回踩到位=19` 预备、`再启动=27` 触发。