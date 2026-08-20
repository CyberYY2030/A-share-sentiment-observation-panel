# Screening v2.6 少而准改造任务书

执行者把本文当完整交接包。新会话先读 `output/screening-v2-work/v26/PROGRESS.md`（不存在则创建），拿不准写 `BLOCKED.md` 并继续不受影响部分。目标是让 A–E 只输出结构成立的高质量机会，每个能力合并去重后最多 Top 20。用户体验优先级：数据/日期真实 > 硬门槛语义 > 排名 > 数量。

## 我替领导拍的板

- 用户已裁决：按能力合并取 Top 20；不足 20 不补位，0 条允许。
- 版本升为 `v2.6`，保留 `v2.5` 历史，禁止原地改写旧批次。
- C“回调支撑”改用点时价格路径证明前期主升；历史 A 只作加分/证据，不再是硬资格。
- B 的 `compression_launch` 与 `momentum_anomaly` 合并排序、同代码去重；其余能力也统一能力级排名。
- 首版阈值按下文冻结；不得为凑数量调松。真实样本显示明显系统偏差时停在报告，交独立审核裁决。

## 界限

允许写：`mining/capabilities.py`、`mining/candidate_persistence.py`、`mining/scanners/{strong_trend,momentum_breakout,launch_burst,second_launch,base_breakout,counter_trend_rs}.py`、`mining/watchlist.py`、`mining/streamlit_tabs/tab_scanner.py`、对应 `tests/test_*.py`、本文、`docs/superpowers/specs/current-opportunity-selection-strategy.md`、`output/screening-v2-work/v26/`。其余只读。禁止写生产 `*.db`、停止 PID、调用 provider、运行生产 migration、改依赖/全局环境、顺手重构 `app_panel.py`。生产库只可在线 backup 到 v26 sandbox 后读取；前后 SHA-256 必须一致。

## 已知事实、禁止重做与任务 0

- 冻结基线：branch `codex/screening-v2`，commit `971c39977e8aa9847fabed8ab20b81a33940e9b7`，2026-08-20 工作树 clean；定向 91/91、全量 361/361（2 skipped）。
- 已核验：08-10～08-17正式 A 每日115～183；盘中 A 在 `tab_scanner.py:782` 错把 `reference_price` 当 `close` 读取。B 六日1,208条，558条当日下跌、795条涨幅≤5%。C 三个候选仅来自07-29 A；存在 `main_rise_base==peak_close`、`made_new_low_recent=True` 仍 retrigger。D 近期9条中5条箱体>25%、4条 contraction>0.8。
- 数据面：正式批次止于08-17；08-18仅4,603行，08-19缺失；预存 updater PID 49020/零字节日志属于生产运维问题，本任务不得处理。
- 已读：当前策略合同、五个 scanner、`watchlist.py:669-1037`、`candidate_persistence.py`、`tab_scanner.py:442-984`及相关测试。`do_not_repeat`：禁止全仓巡检、重复真实库统计、重新调查旧 provider 事故。
- 任务0仅确认新 worktree 能读取上述源码、Python 3.11和原库只读路径；不符则记录后继续合成测试部分。将目标、风险和首入口压缩到 PROGRESS ≤10行。

## 任务 1：真值链与能力级 Top 20

修正盘中 A 字段合同；正式/盘中走同一 shortlist helper。每能力先合并全部子策略、按 `sec_code` 去重（B 重合保留高分并保留 subtype 证据）、统一 `capability_rank=1..N`，截取 `N<=20` 后再持久化/展示；A/C/D/E同样适用。若上一预期交易日日线缺失，盘中返回 `stale_history` 并展示最近正式日，禁止跨缺口计算。增加红灯测试证明旧 `reference_column="close"`、第21名和缺日前序会失败，再恢复全绿。

## 任务 2：冻结 v2.6 市场语义

- A：`close>MA20>MA60`，MA20/MA60斜率非负，RPS20≥0.90；主板/双创距60日高点≤10%/15%，`close/MA20≤1.15/1.25`；fresh breakout 另需收盘突破、close position和活动确认。
- B momentum：只认当日需求冲击；删除 `prior_pulse`、纯 `shadow` 正向资格。主板涨5%～10.2%、双创7%～20.2%，`close/high≥0.97`、activity percentile≥0.80、5日涨幅5%～25%。compression 保留收缩结构，并把 activity≥0.80、amount≥1亿元变硬门。
- C：过去5～30日主升幅度主板≥15%、双创≥20%；回撤主板5%～15%、双创5%～20%，持续2～15日，缩量≤0.75，`close≥MA20≥MA60`。近期创新低直接否决；触发须站回MA10/MA20或突破3日高、阳线且活动≥前5日均值1.2倍。
- D：识别15～40日真实平台；箱体主板≤15%、双创≤20%，ATR10/ATR40≤0.75，阻力至少两次触及。当日突破0.5%～8%/12%，`close/high≥0.97`、activity≥0.80、amount≥1亿元；过度延伸仅诊断。
- E：保留现有资格语义，只参加能力级 Top 20。

每项必须有正例、反例、边界例；特别固定“当日下跌 prior pulse”“近期创新低 C”“25%宽箱体 D”为负例。禁止只 `.head(20)` 掩盖资格错误。

## 任务 3：隔离验收与交付

对生产库做只读 online backup，在 sandbox 回放最近20个可用日，输出 `v26-evidence.json/md`：每日 universe→结构→触发→eligible→Top20 漏斗、A–E数量、B subtype占比/去重、代表性增删各≥5、数据缺口状态、源库前后哈希。只提交源码、测试、规格和小型证据，不提交 DB/cache/log。独立提交一个 commit，报告 commit、diff、测试和未决项；不得生产激活。

## 规矩与完成条件

禁止 skip/todo、删/放宽旧断言、mock 被测 evaluator、改验收口径、`|| true`。同一失败3次换策略，最多2轮阈值实现；仍不可信则停止。完成必须同时满足：定向测试不少于91且全绿；全量不少于361且仅原2项可skip；同输入正式/盘中候选代码与 capability rank一致；每能力每天≤20、B无重复代码；三类固定负例为0命中；源生产库哈希不变。命令：

```powershell
python -m unittest tests.test_strong_trend tests.test_momentum_breakout tests.test_second_launch tests.test_watchlist_state_machine tests.test_base_breakout tests.test_mining_ui tests.test_candidate_persistence tests.test_selection_runtime
python -m unittest
python -m py_compile mining/capabilities.py mining/candidate_persistence.py mining/scanners/*.py mining/watchlist.py mining/streamlit_tabs/tab_scanner.py
git diff --check
```

`BLOCKED.md` 随交付提交，空则写“无”。
