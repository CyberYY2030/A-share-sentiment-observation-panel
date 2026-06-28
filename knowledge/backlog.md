# 量化落地清单（卡片 → 扫描器）

把 `cards/setups/` 里的模式逻辑转成 `mining/` 扫描器/特征/过滤规则。
当前主攻**策略/模式/机会挖掘**；指标/情绪周期暂缓（指标面板已基本覆盖）。
状态：🟢 现有日线/概念数据可做　🟡 候选可量化、入场需盘中　⚪ 仅人工/逻辑量化

## 落地路线（当前主攻：初期异动 + 阶段最强势，不做连板）

实现规格见 `docs/superpowers/specs/2026-06-26-early-anomaly-true-leader-mining.md`（交 Codex）。

| 顺序 | 产物 | 来源 | 缺口 | 状态 |
|---|---|---|---|---|
| **0** | `mining/features.py` 补 `is_limit_up`（单根，非连板）、`rolling_new_high` | D0034/D0037 | 无 | ✅ 已实现 |
| **1** | `true_leader` 扫描器（阶段最强/真龙）：阶段强度 + 指数下跌日逆势分离 + 成交额带(20-80亿) | D0037 + SET-sector-resonance | 分离度量牛市饱和，待 v1.1 升级 | ✅ v1.0 已实现 |
| **2** | `trend_embryo` 扫描器（初期异动胚子）：5日涨>13%且涨停≤1 + 小阳堆积 | D0034 + SET-trend-strong-leg | 真实数据表现健康 | ✅ 已实现 |

### 已完成：分离度量升级 + 有效性回测 + 30日校准

实现规格见 `docs/superpowers/specs/2026-06-26-scanner-validation-and-separation-upgrade.md`。65测试通过。

| 顺序 | 产物 | 状态 |
|---|---|---|
| **A** | `true_leader` v1.1：连续超额收益分离度量 + 下跌日回退/降权 + 过度延展上界 | ✅ 已实现 |
| **B** | `mining/evaluate.py`：历史复跑+outcomes聚合，胜率/前向收益/相对基线/score分桶 | ✅ 已实现 |
| **C** | 30日校准：分离已恢复连续分布，给出建议值待复核 | ✅ 30日完成 |

### 已完成：评估可信化 + trend_embryo 信号修正 + 全区间评估

实现规格见 `docs/superpowers/specs/2026-06-26-eval-trust-and-signal-fix.md`。68测试通过。

| 顺序 | 产物 | 状态 |
|---|---|---|
| **D** | `evaluate.py` 可信化：向量化baseline跑完全区间(138日/92s) + 中位数/胜率相对基线 + top-k桶 + skip-existing | ✅ 已实现 |
| **E** | `trend_embryo` 诊断：各单因子对t+5无稳定方向 → v1.1退化为候选池(score=0/candidate_pool) | ✅ 已实现 |
| **F** | 全区间评估：Codex正确未擅自改参；详见下方结论 | ✅ 评估完成、参数冻结 |

**全区间(138日)关键结论**：两个新扫描器**无 t+5 edge**。`true_leader` 30日头部+3.89%在全区间翻成-0.98%(样本运气)；中位数相对基线全为负；唯一正top-k是旧`momentum_breakout`(+0.60%)。

### 已完成：测量公平化 + edge 终判

实现规格见 `docs/superpowers/specs/2026-06-27-measurement-fairness-and-edge-verdict.md`。71测试通过。

| 顺序 | 产物 | 状态 |
|---|---|---|
| **G1** | 同票池基线 `compute_baseline_universe`：只统计`build_universe`合格票池 | ✅ 已实现 |
| **G2** | 发现指标：mfe/mae/best_close + discovery_hit(5日内≥+5%命中率) | ✅ 已实现 |
| **G3** | edge终判（门槛预先写死） | ✅ 已实现 |

**终判结论**：`trend_embryo`/`true_leader` 均 **`discovery_only`**——机械持有无t+5 edge(top-k中位数 vs同票池基线 −2.5~−3.8%)，但发现命中率显著高于基线(+19~+24pct)；发现价值与高波动绑定(候选MAE中位数−7~−8.5%，约基线2倍)，**需人工择时兑现**。印证"选股只解决发现、买卖点人工"。

### 已完成：候选池沉淀 + 二次启动扫描器

实现规格见 `docs/superpowers/specs/2026-06-27-watchlist-sedimentation-and-second-launch.md`。78测试通过。

| 顺序 | 产物 | 状态 |
|---|---|---|
| **H1** | `mining/watchlist.py` 沉淀(纯读)：近40日强势名单+回踩状态。真实2026-06-25→908条 | ✅ 已实现 |
| **H2** | `second_launch` 扫描器：缩量回踩均线止跌低吸 + `moving_average`助手 | ✅ 已实现 |
| **H3** | 面板登记+沉淀清单视图；same-harness评估 | ✅ 已实现 |

**second_launch 终判：`no_edge`**(topk中位数−0.58%、发现命中率−2.15%、胜率−4.35%，三项全未过门槛)。机械低吸排序连发现命中率都低于基线——低吸票本就"安静"，5日MFE指标对其天生不利(度量与setup错配，未retro改门槛)。可用部分是H1沉淀清单(人工低吸的观察面)，非机械排序。

### 系统现状（四扫描器终判）
- `momentum_breakout`：top-k +0.60%，唯一机械top-k为正(弱)。
- `true_leader`/`trend_embryo`：`discovery_only`，发现强、需人工择时。
- `second_launch`：`no_edge`，仅H1清单可用。
- `rps_stock_top20`：弱对照。
**本arc最值钱的交付=可信评估闸门 + 滚动观察清单，非任何单一机械alpha信号。**

### 下一步：沉淀清单 → 可动作漏斗（方向B：打磨好用）

实现规格见 `docs/superpowers/specs/2026-06-28-watchlist-actionable-funnel.md`（交 Codex）。
缘由：H1输出908行无状态大表=放大版存档，不是漏斗。原理：人是alpha系统是漏斗，机械打分已证无t+5 edge故不再堆指标预测，改做状态机收敛+决策上下文。

| 顺序 | 产物 | 状态 |
|---|---|---|
| **I1** | `watchlist.py` 生命周期状态机(延伸/回踩中/回踩到位/再启动/破位)+决策字段(run_up/缩量比/大阴线/止跌/站回MA10)+triage分诊排序(非收益预测) | 🟢 |
| **I2** | 面板双清单:「回踩到位·预备」+「再启动·触发今日」(各≤30)+决策列，908全量折叠进expander | 🟢 |
| **I3** | docstring注记state可评估口径(本轮不跑不声称edge) | 🟢 |

用户低吸风格=两栏都要(预备+触发)。纪律：决策支持层不声称预测收益，UI无edge字样，evaluate闸门保留。

### 后续候选
| 产物 | 来源 | 状态 |
|---|---|---|
| 低吸setup专属度量(更长/条件horizon，非5日MFE) → 重判second_launch/分诊顺序 | H3+I发现 | ⚪ 度量变体，不retro改现门槛 |
| trend_embryo 概念独立性加分（齐涨=跟风/独立=优） | D0034 | 🟡 需先核对 ths 概念成员表 |

## 现有扫描器对照

`momentum_breakout`（当日异动，含 early_strength）、`rps_stock`（涨幅RPS top20）、`rps_concept`、`cup_handle`。
两侧各覆盖一半、口径偏粗：momentum 缺"涨停取反/分歧未加速"的早期过滤与形态加分；rps 缺真龙的价格带/成交额带/**分离逆势**。两个新扫描器各补一头，**不改 momentum/rps 口径**。

## 关键判断

- **分离逆势强度是性价比最高的新维度**：把"强"从'涨得多'升级为'指数跌时还逆势新高'（真龙本质判据），且指数日线本地已具备，现在就能算。同时落地了 SET-sector-resonance 的"抗跌度"。
- **新增专用扫描器、不动旧的**：momentum/rps 口径已固化且有测试，新建 `trend_embryo`/`true_leader` 更清晰、可独立回测、不破坏现有面板。
- **选股只解决"发现"**（D0034 原话）：扫描器出候选+确认/失败信号，逻辑定性与买卖点留人工。
- **机构介入（龙虎榜）不做**：本地无数据源。
- **连板族暂存**：SET-relay-new-king / SET-fanbao-low-odds 卡片保留为知识，但按用户方向暂不落地。

## 已识别、待蒸馏（继续补 setup 卡）

- 连板接力族：超级龙头之后接力(D0010)、核心龙头挂了之后(D0280)。
- 反包族：双头反包战法与龙头五个买点(D0163)。
- 板块轮动族：龙头切换(D0047)、主流板块补涨(D0070)、300龙头套利(D0121)。
- 弱转强系统介绍。
