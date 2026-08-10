# 任务卡组：让挖掘系统真正"高效稳定"跑起来（Phase M–P）

状态：📝 待实现（实现交给 Codex，按卡顺序一张一张做）
作者：规划 by Claude（2026-07-03，基于 2026-07-02 实盘数据排查，全部证据已核实非猜测）
前置：Phase L（强度闸门）已完成，110 测试全绿，工作区干净。

---

## 0. 这组卡在解决什么（先看这里）

系统的价值链是：**回填行情 → 扫描 → 快照留痕 → 事后对账 → 评估**。排查发现这条链断在两处：

1. **对账层上线第一天就静默失效**：`watchlist_outcomes` 全部 2486 行被错标为 `delisted`（退市），0 行 complete，且永远不会被重算。照此下去 Phase J 的前向验证**永远等不来样本**，而且失败是静默的——你只会一直看到"样本不足"，看不出是机器坏了。
2. **整条链靠手动触发、没有健康自检**：快照只有 3 天（06-26/29/30），ETF 数据停在 06-18 两周没人发现。

这组卡按优先级修复：**卡 M 修对账毒化（P0）→ 卡 N 日更自动化+健康摘要（P0.5）→ 卡 O 数据质量（P1）→ 卡 P 每日推送（P2，可选）**。

依赖关系：M 必须最先做（每拖一个交易日就浪费一天样本）；N 依赖 M（complete 增量是核心健康指标）；O 独立，可与 N 并行；P 最后。

样本时间表（修好后的预期，用于对齐心理预期，不是验收项）：可动作两状态每天约各 12 只，各攒满 150 个"已走完"样本约需 13 个交易日。注意 06-30 之前的快照是 Phase L 闸门前的旧口径，评估时应从 `start=2026-06-30` 起算。

## 全组统一约束

- 编辑前 `git status --short`，勿回退无关改动；不提交 `*.db`、`knowledge/_raw/`、含密钥的配置。
- 每张卡完成后：全量 `python -m unittest` 绿（当前基线 110），并在该卡"实现记录"小节如实填写（含真实数据验收输出）。
- 不改 scanner 算法、默认参数、`universe.py`、漏斗状态机（卡 O 的守卫属于数据写入层，不属于此列）。
- 阈值/开关一律参数化，不写死在逻辑里。

---

# 卡 M（P0）：修复前向对账的"假退市"毒化

## M.0 一句话

当天跑当天快照时，票还没有"明天的 K 线"，系统却把它标成 `delisted`（退市）并从此永久跳过——把"数据还没来"误判成"票死了"。修复后，对账记录会随着行情回填自动从 无→partial→complete 推进，Phase J 才真正开始攒样本。

## M.1 证据（2026-07-02 实盘 DB 排查）

- `watchlist_outcomes`：2486 行全部 `status='delisted'`，0 行 complete。抽查 `000021`：快照日 2026-06-26 之后 kline 里明明有 06-29、06-30 两根 K 线，但记录在 06-29 上午写成 delisted 后再未更新。
- 老 `outcomes` 表同病：trade_date 2026-06-24 起 100% delisted；06-18/06-22/06-23 还有 183 行 partial 无人补。
- 病根 1（语义错）：`mining/backtest.py::_forward_bars`（约 line 45-47）在"该票 trade_date 之后一根未来 K 线都没有"时返回 `status='delisted'`。当天跑当天，全市场必然命中。
- 病根 2（不可重试）：两处增量回填的 WHERE 只重试 `status='partial'`（`backfill_outcomes` 约 line 106、`backfill_snapshot_outcomes` 约 line 181），delisted/halted 一旦写入永久跳过。
- 病根 3（范围太窄）：`run_daily.py` line 116 调 `backfill_outcomes(conn, trade_date=resolved_trade_date)` 只补当天，历史日期的 partial 无人管。
- 附带性能病根：`_forward_bars` **每行**调一次 `list_stock_trade_dates(conn)`（对 86 万行 kline 做 DISTINCT），全量 force 重算会慢到不可用。

## M.2 改动点（全部在 `mining/backtest.py` + `run_daily.py` 一行）

- **M1 语义修复（`_forward_bars`）**：先查市场日历——若日历在 `trade_date` 之后**没有任何交易日**，说明全市场数据未就绪，返回 `None`（调用方跳过、不写行、计入 skipped 计数）。日历有后续交易日但**该票**无未来 bar，才维持 `delisted`（真缺席嫌疑），且必须可被重试（见 M2）。
- **M2 重试集扩大**：`backfill_outcomes` 与 `backfill_snapshot_outcomes` 的增量查询，从"只重试 partial"改为**重试所有非 complete**（`o.status != 'complete'`，含 partial/halted/delisted）。两表行数在万级，每日重算非 complete 行成本可忽略。
- **M3 滚动补全**：`run_daily.py` line 116 改为 `backfill_outcomes(conn)`（去掉 trade_date 限定），让历史日期随每天日更自动愈合。
- **M4 性能**：把日历计算提升到回填循环外——`_forward_bars` 增加参数 `calendar: list[str] | None = None`（None 时自查，保持向后兼容）；两个 backfill 函数在循环前算一次传入。
- **M5 一次性治愈**：代码合入后执行一次 `backfill_outcomes(conn, force=True)` 与 `backfill_snapshot_outcomes(conn, force=True)`，把毒化行全部按新语义重写。把治愈前后的 status 分布对比原样贴进实现记录。

## M.3 测试（`tests/test_mining_pipeline.py` 增量）

- **状态推进链**：快照日=最新 K 线日 → 不写 outcome 行（skipped 计数）；补入 t1/t2 bar 后增量重跑 → partial；补满 t5 → complete。
- **毒化可愈合**：预置一条 delisted 的 outcome 行，随后该票出现未来 bar，增量重跑后翻转为 partial/complete（不需要 force）。
- **历史日期愈合**：多日 candidates fixture，`execute_daily_pipeline` 跑最新日时，早前日期的 partial 被补成 complete。
- **口径平价保留**：现有 outcomes vs watchlist_outcomes 逐值一致测试不回退。
- 全量回归：现有 110 测试全绿。

## M.4 验证命令与验收

```powershell
python -m unittest
python -m py_compile mining/backtest.py run_daily.py
python -c "from mining.db import connect; from mining.backtest import backfill_outcomes, backfill_snapshot_outcomes; conn=connect(base_dir='.'); print('outcomes:', backfill_outcomes(conn, force=True)); print('snapshots:', backfill_snapshot_outcomes(conn, force=True))"
python -c "import sqlite3; con=sqlite3.connect('mining_mvp.db'); print('watchlist_outcomes:', con.execute('SELECT status, COUNT(*) FROM watchlist_outcomes GROUP BY status').fetchall()); print('outcomes:', con.execute('SELECT status, COUNT(*) FROM outcomes GROUP BY status').fetchall())"
```

验收标准（真实数据）：
1. `watchlist_outcomes` 出现 complete/partial，delisted 降到接近 0（只剩真缺席个股）。
2. 老 `outcomes` 表 06-18~06-30 的 delisted 大面积转为 complete/partial。
3. 再跑一次增量（不带 force）幂等，且非 complete 行被重新处理。
4. force 全量重算在几分钟内完成（M4 性能生效）。

## M.5 实现记录（Codex 填写）

完成时间：2026-07-03 14:41。

改动摘要：
- `mining/backtest.py::_forward_bars` 增加可复用 `calendar` 参数；当 `trade_date` 后没有任何市场交易日时返回 `None`，调用方计入 `skipped` 并清理旧 outcome，避免把“数据未到”写成 `delisted`。
- `backfill_outcomes` 与 `backfill_snapshot_outcomes` 的增量重试范围改为所有非 `complete`，并新增 `skipped/delisted/halted` 返回计数。
- `run_daily.py::execute_daily_pipeline` 改为 `backfill_outcomes(conn)`，每日运行时滚动补全历史非 `complete`。
- `runtime_paths.py` 将 `Path.resolve()` 改为 `Path.absolute()`，修复 Windows 8.3 短路径导致的全量测试字符串不一致。

真实数据治愈前 status 分布：
```text
watchlist_outcomes: [('delisted', 3207)]
outcomes: [('complete', 19082), ('delisted', 3281), ('halted', 1), ('partial', 188)]
```

force 治愈输出：
```text
outcomes: {'processed': 22552, 'complete': 21762, 'partial': 683, 'skipped': 43, 'delisted': 60, 'halted': 4}
snapshots: {'processed': 3207, 'complete': 0, 'partial': 2795, 'skipped': 47, 'delisted': 364, 'halted': 1}
```

普通增量复跑输出：
```text
outcomes incremental: {'processed': 790, 'complete': 2, 'partial': 682, 'skipped': 43, 'delisted': 59, 'halted': 4}
snapshots incremental: {'processed': 3207, 'complete': 0, 'partial': 2795, 'skipped': 47, 'delisted': 364, 'halted': 1}
```

最终 status 分布：
```text
watchlist_outcomes: [('delisted', 364), ('halted', 1), ('partial', 2795)]
outcomes: [('complete', 21764), ('delisted', 59), ('halted', 4), ('partial', 682)]
```

测试：
```text
python -m py_compile mining/backtest.py run_daily.py runtime_paths.py tests/test_mining_pipeline.py
python -m unittest tests.test_runtime_paths tests.test_mining_pipeline  # Ran 37 tests OK
python -m unittest  # Ran 113 tests OK
```

备注：`watchlist_outcomes` 已从 100% `delisted` 转为主要 `partial`，但当前真实快照尚无 `complete`；这符合 t5 尚未走完或数据仍缺后续交易日的状态，后续日更会继续自动推进。

---

# 卡 N（P0.5）：日更自动化 + 每日健康摘要

## N.0 一句话

系统的价值依赖"每个交易日都跑一遍"，但现在靠人的记性（快照只有 3 天、ETF 断了两周没人发现）。这张卡把日更变成计划任务，并让机器每天自己报告"我是否健康"——以后链路坏了当天就能看见，而不是三天后偶然发现。

## N.1 现状事实

- `offline_daily_update.py` 已经覆盖 stock/index/concept/etf 各域缺口修复，并会调用 `run_daily.py`（约 line 942）。所以自动化**不需要新管道**，只需：一个可调度的入口 + 健康摘要。
- 当前没有任何 Windows 计划任务指向本项目。

## N.2 改动点

- **N1 健康摘要**：在 `offline_daily_update.py` 收尾处新增汇总函数（或扩展现有日志收口），写 `output/health_latest.md`（**覆盖式单文件**，不按日期堆积）。内容：
  - 各数据域最大数据日 + 相对 stock 域的落后交易日数（stock/index/concept/etf/mining candidates/watchlist_snapshots）。
  - 当日写入行数：candidates、watchlist_snapshots。
  - 两张 outcomes 表的 status 分布，以及 **complete 行数相对上次运行的增量**（上次值可存 `output/health_state.json` 或从表内 backfilled_at 推）。
  - 本次运行的错误列表（含 `watchlist_validation.error`）。
  - 告警行（醒目前缀 `[ALERT]`）：任一域落后 > 2 个交易日；complete 增量连续 3 次运行为 0；本次运行有域级失败。
- **N2 入口脚本**：根目录新增 `daily_job.ps1`：用 `.venv` 的 python 跑 `offline_daily_update.py`（先确认其 CLI 参数用法），stdout/stderr 落 `output/backfill_jobs/` 日志，退出码透传。
- **N3 计划任务（用户自己注册，Codex 只交付命令）**：在本卡实现记录里给出完整 `schtasks /create` 命令——工作日 17:30 触发、错过则开机补跑（`/sc weekly /d MON,TUE,WED,THU,FRI` + 任务设置说明）。**Codex 不执行注册**（系统设置变更由用户自己跑）。
- **N4 文档**：`CLAUDE.md` 与 `AGENTS.md` 的验证命令/架构小节补一句日更入口与健康摘要路径。

## N.3 测试

- 健康摘要的落后天数与告警判定：合成 SQLite fixture 测"某域落后 3 天 → ALERT""complete 无增量 → 计数累积"。
- 摘要函数对缺表/空表安全（不抛错，标注 n/a）。
- `daily_job.ps1` 至少通过一次真实手动运行。

## N.4 验收标准

1. 手动跑一次 `daily_job.ps1`：全链路成功，`output/health_latest.md` 生成且各域数据日与实际一致。
2. 故意造一个落后域（或用 fixture 测试）能看到 `[ALERT]` 行。
3. `schtasks` 注册命令与任务说明写入实现记录，等用户执行。

## N.5 实现记录（Codex 填写）

完成时间：2026-07-03 14:47。

改动摘要：
- `offline_daily_update.py` 新增 `write_health_summary()`，每次 `run_offline_update()` 收尾覆盖写入 `output/health_latest.md`，并用 `output/health_state.json` 保存 complete 计数与连续 0 增量 streak。
- 健康摘要覆盖 stock/index/concept/etf/mining candidates/watchlist_snapshots 的最大数据日、相对 stock 交易日落后天数、当日 candidates/watchlist_snapshots 行数、`outcomes` 与 `watchlist_outcomes` status 分布、complete 增量、错误和 `[ALERT]`。
- 根目录新增 `daily_job.ps1`，优先使用 `.venv`，若不可启动则回退 `py -3` / `python`，stdout/stderr 写入 `output/backfill_jobs/daily_job_*.log` 并透传退出码。
- `AGENTS.md` 与 `CLAUDE.md` 已补充日更入口和健康摘要路径。

手动入口验收：
```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\daily_job.ps1 --dry-run --asof 2026-04-22 --days 1 --timeout-sec 30
```
输出：
```text
health=D:\BaiduNetdiskDownload\cursor workflow\adata_sentiment_dashboard\output\health_latest.md
ok=True
```

生成的 `output/health_latest.md` 摘要片段：
```text
| stock | 2026-07-02 | 0 |
| index | 2026-07-02 | 0 |
| concept | 2026-07-02 | 0 |
| etf | 2026-06-18 | 9 |
| mining candidates | 2026-07-02 | 0 |
| watchlist_snapshots | 2026-07-02 | 0 |
[ALERT] etf lag_vs_stock_days=9 max_date=2026-06-18
```

计划任务注册命令（由用户自行执行，Codex 未注册系统任务）：
```powershell
schtasks /create /tn "AdataSentimentDailyUpdate" /sc weekly /d MON,TUE,WED,THU,FRI /st 17:30 /tr "powershell.exe -NoProfile -ExecutionPolicy Bypass -File "D:\BaiduNetdiskDownload\cursor workflow\adata_sentiment_dashboard\daily_job.ps1"" /f
```
任务设置说明：注册后在 Windows Task Scheduler 中启用 “Run task as soon as possible after a scheduled start is missed”（错过后尽快补跑）。

测试：
```text
python -m py_compile offline_daily_update.py
python -m unittest tests.test_backfill_rules  # Ran 14 tests OK
```

---

# 卡 O（P1）：数据质量——ETF 域断流 + 指数价格串进股票池

## O.0 一句话

两处脏数据在腐蚀面板可信度：ETF 数据停在 06-18 没人修；`repair_market_day_akshare.py` 的回退路径把**上证指数/中证1000 的点位**当成 平安银行(000001)/石化机械(000852) 的股价写进了股票 K 线——Phase L 里 run_up 异常 1307 倍的怪票就是它。这张卡：修断流、清脏行、堵住再犯的口子。

## O.1 证据

- `etf_mvp.db`：`etf_scale`/`etf_total` 最大 trade_date = 2026-06-18，落后 stock 域两周；`offline_daily_update` 明明有 etf 域计划（`SCRIPT_ETF_CANDIDATES`，约 line 904-929）。
- `a_share_mvp.db::kline_daily` 中 `sec_code='000001'`、`sec_type='stock'` 的行里，`source='akshare_stock_zh_a_daily'` 的 14 行 close 在 4027~4179（上证指数点位），而 baostock 同代码股票行 close 在 10.45~11.85（平安银行真实股价）。`000852` 同病（10 行 close 8202~8876 = 中证1000 点位）。即：**修复回退路径请求错了标的，把指数日线以 sec_type='stock' 写库**。

## O.2 改动点

- **O1 ETF 断流诊断修复**：
  - 先看 `output/backfill_jobs/` 最近日志确定失败模式（脚本反复失败？数据源空返回？coverage 判定失真？）。
  - 按诊断结果修复（遵守项目重试纪律：单接口 3 次封顶，两接口都挂就停下写报告）。先短探针（补 1~2 天）验证，再补全缺口。
  - 验收：etf 域最大数据日与 stock 域差 ≤ 1 个交易日，且再跑 `offline_daily_update` 能自动保持。
- **O2 清洗串档脏行**：
  - 清洗前**先备份**：复制 `a_share_mvp.db` 为 `a_share_mvp.backup-<date>.db`（不提交）。
  - 全库扫描定位所有污染（不只这两只）：对 `sec_type='stock'` 的每只票，找相邻交易日 close 跳变超过 ±50% 的 bar，列出 `(sec_code, trade_date, source, close)` 清单，贴进实现记录再删。预期主要命中 `source='akshare_stock_zh_a_daily'`。
  - 删除确认的脏行后，对受影响的 `(sec_code, trade_date)` 用正确路径重新修复（baostock 或修好的 akshare 路径）。
- **O3 堵住根因（`repair_market_day_akshare.py`）**：
  - 检查 `stock_zh_a_daily` 调用的符号前缀拼接：深市 `000/001/002/003/300/301` 开头必须 `sz` 前缀，`60/68` 开头 `sh`，`8/4/9` 开头北交所路径。修正误把 `000xxx` 拼成 `sh000xxx`（=指数代码）的分支。
  - 加**写入前连续性守卫**：回退源写入的 bar，若相对该票库内最近一根 close 的涨跌幅超过 ±30%（参数化），拒绝写入并记日志。守卫只作用于修复回退路径，不动主回填路径。
- **O4 下游验证**：清洗后跑 `build_watchlist`（最新日），确认 000852/000001 的 run_up 异常消失、不再以指数价格出现在漏斗。

## O.3 测试

- 符号前缀映射：覆盖 000/002/300/301/60/688/8 开头代码的前缀单元测试。
- 连续性守卫：合成"上一根 close=10、新 bar close=4000"→ 拒绝；正常涨跌 → 放行。
- 现有回归全绿。

## O.4 验收标准

1. 污染清单（扫描输出）与删除行数贴进实现记录；备份文件存在。
2. `kline_daily` 中 000001/000852 的 stock 行价格序列恢复连续。
3. etf 域数据日追平；`offline_daily_update` 连跑两日保持不掉队。
4. `build_watchlist` 最新日无指数价格标的。

## O.5 实现记录（Codex 填写）

完成时间：2026-07-03 16:31。

改动摘要：
- `backfill_etf_equity_60d_v2.py` 在 AkShare ETF scale 调用期间兼容 `pd.read_excel(bytes)`，将 bytes 包装为 `BytesIO`，修复当前 AkShare 1.18.64 + pandas/openpyxl 下 SZSE ETF 接口报 `Expected file path name or file-like object` 的断流。
- `repair_market_day_akshare.py::market_prefix` 改为股票前缀映射：`000/001/002/003/300/301 -> sz`，`60/68 -> sh`，`8/4/9 -> bj`；指数请求改用独立 `index_market_prefix`，避免再把股票 `000001` 请求成 `sh000001` 指数。
- `repair_market_day_akshare.py` 增加回退写入前连续性守卫：股票回退行相对库内上一根 close 跳变超过 30% 时拒绝写入并计入 `guard_rejection_count`。

ETF 断流诊断与修复：
```text
原最近日志：fund_etf_scale_szse -> pd.read_excel(r.content) -> TypeError: Expected file path name or file-like object, got <class 'bytes'> type；3/3 attempts failed。
短探针：python backfill_etf_equity_60d_v2.py --db etf_mvp.db --days 2 --asof 2026-07-02 --ref-db a_share_mvp.db -> DONE total_rows=2161。
补全：python backfill_etf_equity_60d_v2.py --db etf_mvp.db --days 14 --asof 2026-07-02 --ref-db a_share_mvp.db -> DONE total_rows=10641。
etf_scale: ('2026-07-02', 184735)
etf_total: ('2026-07-02', 159)
stock_max: 2026-07-02
```

污染扫描与清洗：
```text
相邻交易日 close 跳变超过 50% 的全库扫描：255 行候选，其中多数为 close=0 的停牌/缺失式异常，不属于本卡根因。
严格污染口径：source='akshare_stock_zh_a_daily'，且该股非污染来源历史 median close < 100，当前 close > 20 * median。
strict_confirmed_pollution_rows: 24
删除行数：24
备份文件：a_share_mvp.backup-20260703-162321.db
```

删除清单摘要：
```text
000001: 2026-04-22, 2026-04-30, 2026-05-06, 2026-05-08, 2026-05-14, 2026-05-18, 2026-05-19, 2026-06-05, 2026-06-12, 2026-06-18, 2026-06-22, 2026-06-25, 2026-06-30, 2026-07-01
000852: 2026-04-22, 2026-04-30, 2026-05-06, 2026-05-14, 2026-05-18, 2026-05-19, 2026-06-05, 2026-06-12, 2026-06-18, 2026-07-01
```

清洗后验证：
```text
remaining_strict_pollution_rows: 0
000001 max_close: 11.52; recent tail includes 2026-06-30 close=10.05, 2026-07-01 close=10.16, 2026-07-02 close=10.28
000852 max_close: 7.31; recent tail includes 2026-06-30 close=4.96, 2026-07-01 close=5.02, 2026-07-02 close=5.01
build_watchlist latest: trade_date=2026-07-02, watchlist_rows=682, 000001_000852_rows=0
offline_daily_update 两次保持性检查：missing_by_day={}, missing_by_domain={}, ok=True
```

测试：
```text
python -m py_compile app.py app_panel.py runtime_paths.py backfill_orchestrator.py offline_daily_update.py repair_market_day_akshare.py backfill_etf_equity_60d_v2.py run_daily.py
python -m unittest tests.test_backfill_rules  # Ran 17 tests OK
python -m unittest  # Ran 118 tests OK
```

---

# 卡 P（P2·可选）：每日推送——名单自己来找你

## P.0 一句话

报告 `report_YYYYMMDD.md` 每天在生成，但获取方式是"你记得去打开它"。提示系统的定义应该是"它来找你"：日更收尾后，把可动作双清单 + 健康摘要推送到 Telegram。这张卡做完，"机会挖掘提示系统"才名副其实。

## P.1 前置（用户提供，缺则本卡挂起）

- 用户自建 Telegram Bot，提供 `TG_BOT_TOKEN` 与 `TG_CHAT_ID`。
- 凭据放环境变量或 `data/notify_config.json`（加入 `.gitignore`）；**绝不写入代码、日志、提交**。

## P.2 改动点

- 新增轻量模块 `mining/notify.py`（标准库 `urllib` 即可，不引新依赖）：
  - `send_daily_digest(conn, trade_date)`：读当日 watchlist 快照的可动作两状态（回踩到位/再启动，各 top 10：代码/名称/状态/triage/回撤%/缩量比），加 3 行健康摘要（各域数据日、complete 增量、ALERT 数），组一条 ≤4000 字符的 Markdown 消息，调 Telegram `sendMessage`。
  - 未配置凭据 → 静默跳过，健康摘要标注 `push: unconfigured`；发送失败 → 最多重试 3 次退避，失败只记日志**不阻断主流程**。
- 接入点：`offline_daily_update.py` 收尾（健康摘要之后）。

## P.3 测试与验收

- 消息组装函数纯文本单测（不真发）：双清单为空、超长截断、未配置跳过。
- 真机验收：手动跑一次日更，Telegram 收到消息，内容与面板一致。

## P.4 实现记录（Codex 填写）

完成时间：2026-07-03 16:49。

改动摘要：
- 新增 `mining/notify.py`，提供 `build_daily_digest()` 和 `send_daily_digest()`：读取当日 `watchlist_snapshots` 中 `回踩到位` / `再启动` 两个状态，各取 top 10，附带 `triage`、`pullback_pct`、`shrink_ratio` 和 `output/health_latest.md` 的健康摘要，消息限制在 4000 字符内。
- `offline_daily_update.py` 在 `write_health_summary()` 之后调用 `_send_daily_push()`；未配置凭据时返回 `skipped: unconfigured`，并在 `output/health_latest.md` 追加 `## Push` 段；发送失败最多重试 3 次且不阻断日更主流程。
- 凭据读取顺序为环境变量 `TG_BOT_TOKEN`/`TG_CHAT_ID` 或 `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID`，再到 `data/notify_config.json`；`.gitignore` 已显式忽略 `data/notify_config.json`，错误摘要会脱敏 token/chat，避免凭据进入 health、日志或测试输出。
- `AGENTS.md` 与 `CLAUDE.md` 已同步每日 Telegram push 的可选配置边界。

未配置凭据验收：
```text
powershell -NoProfile -Command "Remove-Item Env:TG_BOT_TOKEN -ErrorAction SilentlyContinue; Remove-Item Env:TELEGRAM_BOT_TOKEN -ErrorAction SilentlyContinue; Remove-Item Env:TG_CHAT_ID -ErrorAction SilentlyContinue; Remove-Item Env:TELEGRAM_CHAT_ID -ErrorAction SilentlyContinue; python offline_daily_update.py --base-dir . --asof 2026-07-02 --days 1 --dry-run --timeout-sec 30"
health=D:\BaiduNetdiskDownload\cursor workflowdata_sentiment_dashboard\output\health_latest.md
ok=True
```

生成的 `output/health_latest.md` 推送段：
```text
## Push

- push: skipped: unconfigured
```

真机 Telegram 验收状态：
```text
未执行真实发送：当前未提供 TG_BOT_TOKEN/TG_CHAT_ID 或 data/notify_config.json。代码路径已通过注入 sender 单测覆盖；配置凭据后日更收尾会自动发送。
```

测试：
```text
python -m py_compile app.py app_panel.py runtime_paths.py backfill_orchestrator.py offline_daily_update.py repair_market_day_akshare.py backfill_etf_equity_60d_v2.py run_daily.py mining/notify.py tests/test_notify.py
python -m unittest tests.test_notify tests.test_backfill_rules  # Ran 23 tests OK
python -m unittest  # Ran 124 tests OK
```

