# TNM-V2-R1：尾盘涨停剔除与两日 Top10 诊断任务卡

状态：`ready_for_terra`

规划所有者：根会话

实现所有者：Terra 会话 `01a03137-df48-7281-b7d7-b48f7e41f209`

独立审核：Sol 会话 `01a03141-39e9-7dd1-a359-037346adf01b`

## 1. 唯一目标与停止边界

只对 `20240923`、`20240926` 做全市场诊断，按本文新 gate 与 score 输出每天独立的 A Top10、B Top10及完整合格池，供用户人工检查。A/B 是不同经济形态，分数不得跨通道混排。

本卡不授权完整 2023—2024 回测，不授权读取或运行 2025/2026，不冻结正式 Top N，不得根据本次结果在同一执行回合调参。Terra 完成一个新 identity canary、提交白名单 commit 后停止；只有原 Sol 审核通过，规划会话才可向用户交付诊断排名。

## 2. 权威输入、时间和既有不变量

- 分钟数据：`E:\分钟数据`，只读；
- 日 K：`E:\日K线全部至202606`，只读且只读实际消费代码的上市日期证据；
- 特征截止：D 日 `[09:30,14:50)`；尾窗固定 `[14:20,14:50)`；
- D−1 成交额严格 `> 200_000_000` 元；
- 至少 20 个 D 前交易日；分钟质量、上市资格、未来数据隔离、A/B 不重叠、percentile、经济并列和 outcome 定义沿用已审核 V2；
- 修改 D 日 14:50 后或 D+1 任意数据不得改变 D 的 gate、score、rank 或成员；
- 代码只能在成员确定后稳定序列化，不得决定经济排名或 Top10 成员。

## 3. 新共同硬门槛

### 3.1 尾窗涨停触及剔除

`tail_limit_touch=true` 当且仅当 D 日 `[14:20,14:50)` 任一有效分钟 bar 的 high 以“分”为整数比较后达到或超过标准涨停价。触及即从 A、B 两通道剔除。

标准涨停价用 D−1 最后一根分钟 close 计算并按人民币分四舍五入：

- `300/301/688/689`：20%；
- 当前 runner 支持的其他 A 股代码：10%。

上市至少 20 个交易日已排除新股前五日无涨跌幅限制。历史 ST 状态不可得，因此 5% ST 涨停识别明确标记 `st_limit_filter_status=unproven_standard_board_rule_applied`，不得声称完整 ST 过滤。当前 runner 不支持的北交所代码不扩入本卡。

输出必须保存 `limit_rate`、`limit_up_price`、`tail_high`、`tail_limit_touch` 和明确失败原因。

### 3.2 尾窗防崩

两通道共同要求 `tail_return >= -0.03`。该门只剔除明显尾盘崩落，允许 `300085/20240923` 的约 −1.79% 强势整理；`tail_location` 只输出诊断，不参与 gate 或 score。

## 4. A 通道：上涨启动/加速延续

以下条件全部满足才有 `a_shape_pass=true`：

1. `ret1450 >= 0.04`；
2. `close1450 > open_D`；
3. `position1450 >= 0.55`；
4. `vwap_dist >= 0.01`；
5. `activity >= 1.50`；
6. `range_expansion >= 1.50`；
7. `close1450 > prev_ma10`；
8. `tail_return >= -0.03`；
9. `tail_limit_touch=false`；
10. 趋势结构二选一：
   - `breakout_pass = close1450 >= prior10_high`；或
   - `trend_pass = prev_ma5 > prev_ma10 && close1450 > prev_ma5`。

其中 `prev_ma5=mean(D-5...D-1 session close)`，`prior10_high=max(high[D-10...D-1])`。除新增字段外，其余量定义沿用 V2。

A 排名只在完整 A 合格池内计算：

```text
rank_price = percentile(ret1450, higher_is_better)
rank_activity = percentile(activity, higher_is_better)
rank_amount = percentile(amount1450, higher_is_better)
rank_volatility = percentile(range_expansion, higher_is_better)
volume_score = mean(rank_activity, rank_amount)
score_A = mean(rank_price, volume_score, rank_volatility)
```

`position1450`、`vwap_dist` 只作为 gate，不再参与 score。按 `score_A`、`ret1450`、`volume_score`、`range_expansion`、`amount1450` 依次降序；名义输出诊断 Top10，完全经济并列保留并标记。

## 5. B 通道：上涨趋势中的缩量回调止跌

保留既有 B 条件：

- `prior10_runup >= 0.15`；
- `-0.15 <= drawdown <= -0.03`；
- `close1450 > prev_ma10`；
- `pullback_volume_ratio <= 0.80`；
- `close1450 > max(open_D, prev_close)`；
- `position1450 >= 0.55`。

新增：

- `body1450 >= 0.01`；
- `range_expansion >= 1.00`；
- `tail_return >= -0.03`；
- `tail_limit_touch=false`。

B 排名只在不属于 A 的完整 B 合格池内计算：

```text
score_B = mean(
  percentile(prior10_runup),
  percentile(-pullback_volume_ratio),
  percentile(body1450),
  percentile(range_expansion)
)
```

`position1450` 只作为 gate。按 `score_B`、`prior10_runup` 降序、`pullback_volume_ratio` 升序、`body1450`、`range_expansion`、`amount1450` 降序；名义输出诊断 Top10，完全经济并列保留并标记。

## 6. 真实样本断言

- `300085/20240923`：A 全部新 gate 通过且进入 A Top10；
- `300339/20240926`：A 全部新 gate 通过且进入 A Top10；
- 两日旧 A Top3：`600619,002405,600203,002583,300100,600208` 必须因 `tail_limit_touch=true` 从 A/B 剔除；
- `300328/20240813`：新 B gate 通过且 D−1 流动性通过；单股 fixture 不声称全市场排名；
- `300972/20240826`：应至少因 `body1450<0.01`、`range_expansion<1.00` 与 D−1 流动性失败而不入选；
- `300972/20240827`：继续不入选，保留原位置失败等原因。

若真实数据与上述断言不符，原样输出完整证据并标记 `diagnostic_changes_required`；Terra 不得改阈值、权重或样本断言后重跑。

## 7. Sol 已确认的工程 P1 同卡修复

1. outcome 独立降级：D+1 `[09:30,10:00)` 诊断无效时，若固定卖出窗有效，仍必须保存 `sell_vwap/gross_fixed_exit/net30`；反向亦然。
2. 输入冻结：input manifest 必须绑定实际消费的分钟容器/成员内容摘要及实际读取的日 K 工作簿摘要；内容变化必须改变 identity 或拒绝 resume，不得只记录父目录 mtime。
3. manifest 将 `base_commit` 与实际 `source_blob_identity` 分开，不得把父提交写成执行提交。

## 8. 产物与可观察性

新 identity 至少输出：

- 原子 `run_manifest.json`、`progress.json`、checkpoints、`completion.json`；
- `diagnostic_eligible_pool.csv.gz`：两日所有 A/B 合格股票，不限 Top10，含全部原始指标、gate、失败原因、分项 percentile、score、pool_rank、selected_top10、outcome；
- `diagnostic_daily_top10.json`：两日分通道 Top10及经济边界并列；
- `diagnostic_fixture_rows.json`：五个指定样本的全部值与逐 gate 结论；
- `diagnostic_excluded_limit_touch.csv.gz`：两日因尾窗涨停触及被剔除的代码与证据；
- `artifact_manifest.json`：文件大小和 SHA-256；
- summary 显式写候选池大小、涨停剔除数、各 gate 淘汰数、ST/公司行为降级、2025/2026 未读、E: 未写。

## 9. 测试与验收

最少新增并通过：

1. 主板10%、创业/科创20%的分价取整、尾窗触及/未触及、14:50 后触及不影响选择；
2. `tail_return=-3%` 边界通过，下一最小变动失败；
3. A 新 gate 每项正/反/边界，breakout/trend OR 结构；
4. A score 三维等权，position/vwap 改变但仍过 gate 时不改变 score；
5. B body/range 新 gate及新 score；
6. 完整池输出、Top10、并列、无代码经济 tie-break；
7. outcome 窗口独立降级；
8. 实际消费文件内容变更导致 input identity 变化；
9. 预筛与朴素完整 gate/rank/成员等价，容器单开，未来数据隔离；
10. V1 全部定向测试继续通过。

固定命令：

```powershell
python -m unittest tests.test_tail_next_morning_v2
python -m unittest tests.test_tail_next_morning
python -m py_compile mining\tail_next_morning_v2.py tests\test_tail_next_morning_v2.py
git diff --check
```

## 10. 写入白名单、提交与后续串行流程

Terra 唯一 tracked 写入：

- `mining/tail_next_morning_v2.py`；
- `tests/test_tail_next_morning_v2.py`；
- 本任务卡仅追加执行证据。

ignored 输出仅限 `output/tail-next-morning-v2/diagnostic/` 新 identity。不得触碰既有 shared dirty、V1、旧 canary 或其他策略文件；不得 `git add -A`，不得 push。

完成标签只能为 `tnm_v2_r1_diagnostic_ready|diagnostic_changes_required|tnm_v2_r1_blocked`。Terra 创建一个白名单本地 commit 后停止。随后只交原 Sol 做独立只读审核；Sol 未 approve 前，规划会话不得把排名作为正式经济结论，完整开发期继续锁定。

Lessons 决策：当前 `skip`。本卡是项目专属经济规则修订，尚无跨项目复用证据。

## 11. 执行证据

尚未执行。Terra 只能在本节追加紧凑证据，不得改写第 1—10 节。
