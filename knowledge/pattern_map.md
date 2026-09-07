# 模式谱系总图（strategy / pattern map）

把语料里**强势股模式 + 启动标准**（去重后 210 篇，占全部 525 篇可用语料的 40%，是最大主题）归成 7 个模式族。
这是"策略/模式/机会挖掘"这条线的总地图：先有全局，再逐族蒸馏成 `cards/setups/`，最后统一落地到 `mining/` 扫描器。

> 落地性图例（按现有数据能做到哪一步）：
> 🟢 盘后用现有日线/概念数据可出候选池　🟡 候选可量化、入场需盘中分时（人工确认）　🔴 多数需分时/竞价数据，暂只编目

| 模式族 | 内核 | 代表原文 | 落地性 | 对接扫描器 | 卡片 |
|---|---|---|---|---|---|
| **连板接力** | 空间板/连板高度博弈，接最强连板的次日 | 新王登基次日接力(D0096)、超级龙头之后接力(D0010)、核心龙头挂了之后(D0280) | 🟡 | ⚠️ 需新增（连板引擎） | ✅ SET-relay-new-king |
| **反包** | 连板后调整收阴，次日是否反包 | 关于反包股(D0277)、双头反包五个买点(D0163)、龙头高位反包低吸(D0171) | 🟡 | ⚠️ 需新增（多为**警示/过滤**，非进攻） | ✅ SET-fanbao-low-odds |
| **大长腿低吸** | 转折日在主线人气股上借分时急杀承接低吸 | 总框架(D0101/D0286)、人气股大长腿(D0103)、连板后低开6%(D0104) | 🟡 | ⚠️ 需新增（盘后选池） | ✅ SET-long-leg-dip |
| **启动初期** | 只做有破局价值的三类启动 | 启动破局(D0619)、启动涨停潮次日(D0107)、启动加强(D0130) | 🟢 | ✅ momentum_breakout 部分覆盖 | ✅ SET-launch-breakthrough |
| **趋势突破** | 狙击有逻辑的日线主升浪，右侧持有 | 狙击强趋势股(D0116)、强趋势回避巨亏(D0033)、趋势票首阴战法(D0176) | 🟢 | ✅ momentum_breakout / rps_stock，可扩展 | ✅ SET-trend-strong-leg |
| **板块轮动/共振** | 用板块vs指数相对强弱筛主线与接力题材 | 共振板块(D0042)、龙头切换(D0047)、主流板块补涨(D0070)、300龙头套利(D0121) | 🟢 | ✅ rps_concept，可补抗跌维度 | ✅ SET-sector-resonance |
| **次日/分时低吸买点** | 二三连板分时低点、恐慌低点、龙回头 | 各类分时低吸(D0173)、恐慌低点(D0241)、龙回头(D0165) | 🔴 | 留人工，仅编目 | — |

## 现有扫描器与缺口

`mining/scanners/` 当前注册了 legacy/discovery 的 `momentum_breakout`、`rps_stock`、`rps_concept`、`trend_embryo`、`true_leader`、`launch_burst`，以及正式 v2.6 A–E 所用的 `strong_trend`、`second_launch`、`base_breakout`、`counter_trend_rs`。`mining.capabilities.CAPABILITY_REGISTRY` 是正式 A–E strategy id 的唯一清单；`cup_handle.py` 是未注册的旧模块。

**关键缺口**：`mining/features.py` 已有单日涨停和滚动新高判断；连续涨停高度、断板、空间板和反包仍没有统一的序列特征层。
连板接力、反包、大长腿三族都卡在同一块基建——**涨停连板引擎**。它是这三族扫描器的共同地基，纯用现有日线（change_pct 判涨停、连板计数）即可建原型。

## 落地顺序（统一落地阶段执行）

1. **涨停连板序列层**（在既有 `is_limit_up` 上增加连板高度 / 断板 / "首次成为空间板" / 连板后调整收阴 的识别）——一块基建解锁连板接力、反包、大长腿三族。
2. **连板接力候选池扫描器**（SET-relay-new-king）——验证"卡片→扫描器"链路。
3. **趋势/共振扫描器补强**（SET-trend-strong-leg → momentum_breakout 参数；SET-sector-resonance → rps_concept 抗跌维度）——改动小、复用现有。
4. 反包**过滤标记**（SET-fanbao-low-odds）——给连板后调整票打 LOW-ODDS，防止把低胜率反包当机会。

详见 [backlog.md](backlog.md)。
