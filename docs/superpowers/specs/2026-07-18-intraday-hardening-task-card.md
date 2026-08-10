# 盘中扫描加固任务卡（Card O）

日期：2026-07-18。来源：卡 N（盘中主升启动扫描，commit 6fc100c）二轮审计的三条裁决。
给交易者的一句话：这张卡让盘中扫描"宁可不出清单，也绝不出错清单"——单位错、节假日冻结数据、本地历史缺天这三种静默出错的情况，全部改为当场报错说人话。

## 全局约束

- 一卡一 commit，可独立 revert。
- 文件所有权（只许改这两个文件）：`mining/intraday.py`、`tests/test_intraday.py`。
- **禁止触碰另一工作流的未提交改动文件**：`run_daily.py`、`offline_daily_update.py`、`runtime_paths.py`、`mining/backtest.py`、`daily_job.ps1`、`mining/notify.py`、`tests/test_notify.py`、`tests/test_backfill_rules.py`、`tests/test_mining_pipeline.py`、`.gitignore`、`AGENTS.md`、`CLAUDE.md`、`backfill_etf_equity_60d_v2.py`、`repair_market_day_akshare.py`。动手前先 `git status --short` 核对。
- 不改扫描窗口时间、launch_burst 条件与参数；不写任何 DB 表；不新增网络接口（不引入交易日历 API）。

## 真值卡

- **业务定义**：盘中扫描输入 = 实时快照（em/sina）+ 本地日线历史；输出 = `output/intraday/` 下的盘中候选 markdown；不写 DB。本卡新增三重数据守卫：①成交量单位自检与换算 ②冻结快照检测（节假日/数据源停更）③本地历史新鲜度检查。不做：不改选股条件，不做交易日历，不做外推估算。
- **恒等式/硬约束**：同一行情帧内 `amount / close` 是"按收盘价折算的隐含成交量"，与 `volume` 同单位时全帧中位比值 ≈ 1（容差带 [0.5, 3]）；若 volume 单位是"手"则比值 ≈ 100（容差带 [50, 300]）。两带之外 = 数据异常，必须 fail-closed 抛错，禁止猜测继续。三个守卫的失败行为一律是抛 RuntimeError 终止本次扫描，绝不降级出清单。
- **证据源**：
  - `mining/intraday.py:58` 直接映射"成交量"列，`:73-78` 仅做 to_numeric，无单位换算；对照 `:38-43` 成交额有 ×10,000 启发式而成交量什么都没有。
  - akshare 文档：`stock_zh_a_spot_em` 成交量单位为"手"；本地 DB `ash.kline_daily.volume` 与 sina 源为"股"（2026-07-17 实测 sina 5193 行，600000/600519/000001 与 DB 07-16 行量比 1.05–1.34，同单位）。
  - `mining/intraday.py:226`：交易窗口只查 `weekday() >= 5`，节假日不排除。
  - `mining/intraday.py:176-208`：`build_synthetic_history` 对 history 最新日期零检查。实证：本地 DB 当前停在 2026-07-16、缺 07-17（周五），若周一盘中扫描将静默以 07-16 为 T-1，σ 与量比基线整体错一天。
- **未知项**：em 端点真实返回单位未实证（直连探针 3 次 RemoteDisconnected，预算用尽）——O.1 的自检机制使实现**不依赖**该未知，任何单位都能被判定或拒绝。真实交易日 14:00–15:00 全链路运行 `environment-blocked`，下一交易日窗口补验。
- **完成闸门**：`python -m unittest tests.test_intraday` 新旧测试全过；`python -m unittest` 全量无新增失败。最低完成标签 `locally-verified`；真实盘中窗口验证保留 `environment-blocked` 注记，不得声称 real-env-verified。
- **Lessons 投递**：活跃条目无触发匹配（none）。提醒一条审计纪律：守卫全部 fail-closed，静默空清单是本卡要消灭的失败形态，不许再引入。

## O.1 成交量单位自检（重点）

1. **问题**：em 主源成交量单位按文档为"手"，代码零换算。若 em 成功返回，量能条件⑥（volx 量比）与⑧（liquid 流动性）看到缩小 100 倍的成交量，结果是**静默空清单**——不报错，只是永远没有候选。当前只是恰好 em 网络失败落到 sina（单位=股）才没出事。
2. **根因**：`mining/intraday.py:58`（列映射）与 `:73-78`（无换算），见证据源。
3. **改法**：在 `normalize_spot_frame` 中、成交额缩放（`:79`）**之后**追加帧级自检：
   - 取 close>0 且 volume>0 的行，计算 `ratio = median((amount / close) / volume)`。
   - `ratio ∈ [0.5, 3]` → 单位一致，不动；`ratio ∈ [50, 300]` → volume 整列 ×100；其余 → `RuntimeError`，报出观测中位比值。
   - 只做整帧判定不做逐行换算（逐行受 VWAP 与收盘价偏离噪声影响）；判定与数据源名称无关（源无关，未来换源自动适用）。
   - 注意：若成交额启发式（万元→元）误判，比值会落在两带之外 → 正确地 fail-closed，而不是错上加错。
   - 自检抛错发生在 `fetch_spot_frame` 的重试循环内，等同该端点该次尝试失败，自然轮转到下一端点——行为符合现有降级链，无需改 fetch 逻辑。
4. **测试**：①sina 风格帧（单位=股）通过且数值不变；②em 风格帧（volume 为手，即真实值 1/100）换算后 volume ×100、其余列不变；③构造比值 ≈10 的畸形帧抛 RuntimeError。

## O.2 冻结快照检测（节假日守卫）

1. **问题**：`_validate_scan_window`（`:226`）只排除周末。节假日逢工作日运行时，spot 接口返回上一交易日的冻结全天数据，被合成为"今天"的假日线 bar。
2. **根因**：无节假日历；引入日历 API 违反本卡"不新增网络接口"约束，且日历是代理指标，真正的危险是"数据是旧的"。
3. **改法**：在 `build_synthetic_history` 内（已同时持有 spot 与 history），取 history 最近一个交易日的 bar 与 spot 按 `sec_code` join：若 join 行中 close 完全相等**且** volume 相对差 <0.5% 的行占比 ≥90% → `RuntimeError`："快照与上一交易日收盘数据一致，疑为节假日或数据源停更"。
   - 误杀分析：真实交易日 14:00 时当日累计成交量必然远小于昨日全天，close 全场与昨收相等更不可能，误杀概率可忽略。
4. **测试**：①把昨日 bar 直接复制为 spot → 抛错；②正常盘中样式（volume≈昨日 60%、close 有变动）→ 通过。

## O.3 本地历史新鲜度守卫

1. **问题**：`build_synthetic_history`（`:176-208`）不检查 history 最新日期。实证场景见证据源（DB 停 07-16 缺 07-17）。
2. **根因**：合成历史默认"DB 是新鲜的"，该假设无守卫。
3. **改法**：计算期望上一交易日：today 为周一 → today−3 天，否则 → today−1 天。history 最大日期 < 期望日 → `RuntimeError`，提示先运行离线更新补数据。
   - 逃生口：环境变量 `INTRADAY_ALLOW_DB_LAG=1` 时降级为警告并继续（长假后周期规则会误报，此时用户自行确认后放行）。
   - 无论正常或放行，报告头部新增一行"基线截止 T-1=YYYY-MM-DD"，用户永远看得见基线日期（O.2/O.3 共用此行）。
   - 覆盖开关走环境变量而非 CLI 参数，因为 `run_daily.py` 属另一工作流 dirty 文件，本卡禁止触碰。
4. **测试**：①模拟周一 + history 停在上周四 → 抛错；②同场景 + 环境变量放行 → 通过且报告含 T-1 行；③周二 + history 停在周一（=昨日）→ 正常通过。

## 不改什么

扫描窗口与参数、launch_burst 八条件、`run_daily.py` 及全部他流 dirty 文件、DB 写入路径（依旧零写入）、网络端点清单。

## 验收命令

```powershell
python -m unittest tests.test_intraday
python -m unittest
```

完成标签：`locally-verified`（真实盘中窗口 `environment-blocked`，下一交易日 14:00–15:00 补验并在本卡追加记录）。
