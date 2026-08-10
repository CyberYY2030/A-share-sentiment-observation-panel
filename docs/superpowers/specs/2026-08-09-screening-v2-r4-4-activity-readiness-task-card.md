# Screening v2.5 R4.4 B/D 真实 activity 可用性任务卡

本卡在 R4.3 复审通过后交给 Terra，禁止与 R4.3 并行。无需读取旧会话；先读本卡和 PROGRESS。目标：把少数股票的 activity 缺口局部隔离，使真实 A 股数据中的 B/D 能正常评估，而不是全市场一起 `activity_missing`。优先级：用户指令 > 本卡 > 旧报告。

## 1. 我替领导拍的板

- 不做历史大回填：现有 `amount` 覆盖足够，本轮修正全有或全无的计算口径。
- activity 资格按 `sec_code` 判断；每只股票优先使用完整正值 `amount`，只有该股 amount 不足时才允许使用完整正值 `turnover_ratio`。缺失只淘汰该股。
- 20 日 baseline 继续严格使用 T-20…T-1，禁止把 T 放进中位数；B compression 原 46 日窗口、所有策略阈值和排序冻结。
- diagnostics 中每只缺失股票只计一次，禁止 builder 与 scanner 双重累计。
- 生产数据库绝对只读；只在一次性 sandbox 副本做真实 smoke。

## 2. 界限

写入白名单：`mining/event_activity.py`；仅在消除重复计数需要时可改 `mining/scanners/{launch_burst.py,momentum_breakout.py,base_breakout.py}`；相关新/现有测试；当前策略文档；本地 ignored `output/screening-v2-work/r4_4-*`、PROGRESS、BLOCKED。本卡只读。

冻结：`selection_context.py`、universe/adjusted-price/data-quality、A/C/E、B/D gate 阈值与 rank、生产四库、依赖、测试发现配置、用户 dirty files。发现必须越界时写 BLOCKED，不自行扩大范围。

## 3. 已知事实、禁止重做与任务 0

- 2026-08-09 只读统计：target=`2026-08-06`，目标活跃股票 5,036；完整 21 日 `amount`=4,981（98.91%），完整 46 日=4,950（98.29%）；`turnover_ratio` 两窗口均为 0。55/86 只局部缺口不应使全市场失败。
- 根因在 `event_activity.py:48-67`：`history.notna().all().all()` 和 `current.notna().all()` 要求所有代码同时完整；任何一格缺失即返回全体 `activity_missing`。
- B、D real smoke 当前均为 0，报告将其描述为源数据不完整；更准确结论是现有口径无法容忍 A 股正常停牌、新股和局部缺行。
- 复审基线：R4.2 全量 `265 OK (skipped=2)`；R4.3 完成后的 N 作为本卡新基线。
- `do_not_repeat`：不重做行情源调查、不回填数据库、不改策略阈值、不重审 C/E/UI。

任务 0：确认 R4.3 已独立提交并复审通过；记录 HEAD、status、生产哈希和全量测试 N；在 sandbox 用现有代码保存一次 activity red evidence（全局 empty 与真实覆盖统计）。

## 4. 执行任务（严格顺序）

1. **先写混合完整度红灯。** 同一 context 至少 3 股：A 的 amount 21 日完整、B 的 amount 缺失但 turnover 21 日完整、C 两源均缺。旧实现必须错误地返回空；目标为 A/B 各一行、source 分别正确、C 唯一计入 `activity_missing`。
2. **实现逐股 activity。** 对每个 code 独立检查 current 与 T-1 baseline 的有限正数；生成 partial rows 和每股 source/ratio/pct/provisional。横截面 percentile 只在可用股票内计算。没有可用行才返回空，但缺失计数仍等于唯一缺失代码数。
3. **消除 diagnostics 双计数。** 明确 builder 或 scanner 单一拥有缺失计数；B compression 的 46 日 `activity_window_missing` 与 20 日 `activity_missing` 分开，不能把同一原因累计两次。新增 0、部分、全部缺失及停牌/新股反例。
4. **真实 sandbox smoke。** 复制四库一次，评估 2026-08-06：记录 universe、20/46 日 eligible/missing、B compression/momentum 与 D 的完整漏斗和候选数。硬门槛：B/D 不得再以 `activity_missing=5036` 或全市场 activity empty 结束；候选可因真实策略 gate 合法为 0。源库与 sandbox source 哈希不变，不写 outcomes。
5. **验证与提交。** 定向至少包含 event_activity、三 scanner、selection context/pipeline 回归；全量 `N>=R4.3 baseline, OK, skipped=2`；diff-check；独立 commit `fix(screening): isolate per-stock activity gaps`。代码 commit 不含 output；本地交付 `r4_4-report.md`、red/green JSON、PROGRESS、BLOCKED。

## 5. 规矩

禁止放宽 B/D 阈值、用 `fillna(0)`/前向填充伪造 activity、把 T 纳入 baseline、删除/skip 测试、硬编码真实命中数、写生产库、提交生成证据。相同失败最多 3 次/3 分钟；劣于基线即回滚并记录。

## 6. 完成条件

用户结果：正常具备历史 amount 的绝大多数股票可进入 B/D activity 计算，少数缺失股票独立 fail closed；真实 smoke 输出可信漏斗。约束结果：混合 fixture、T-1 边界、逐股 fallback、唯一计数、46 日窗口全部绿；全量不退化；生产四库/outcomes 零变化。R4.3、R4.4 都复审通过后，才允许编写生产 migration/canary 任务卡。
