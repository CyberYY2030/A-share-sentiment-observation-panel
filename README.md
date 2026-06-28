# A股市场情绪观察面板 V1

这个项目会自动拉取 A 股、题材和权益 ETF 的历史数据，维护本地 SQLite 数据库，并生成一个可直接查看的市场情绪观察面板。面板还包含机会挖掘模块，用本地收盘数据和盘中快照展示当日机会与历史候选跟踪。

## 安装

```bash
pip install -r requirements.txt
```

## 启动面板

```bash
streamlit run app.py
```

启动入口是 `app.py`，实际逻辑在 `app_panel.py`。面板启动时会检查本地数据库是否落后；如果发现最近交易日存在缺口，会通过 `backfill_orchestrator.py` 在后台启动 `offline_daily_update.py`，避免在 Streamlit 渲染主线程里长时间卡住。

## 离线日更与修复

先只检查缺口，不写数据库：

```bash
python offline_daily_update.py --base-dir . --asof 2026-04-22 --days 10 --dry-run
```

检查缺口并按缺失域自动补齐：

```bash
python offline_daily_update.py --base-dir . --asof 2026-04-22 --days 10
```

只重跑机会挖掘和复盘：

```bash
python run_daily.py --base-dir . --range 2026-04-21 2026-04-22
```

当 BaoStock 不能补齐某个市场日时，可以用 AkShare 多接口修复单日 A 股和指数行情：

```bash
python repair_market_day_akshare.py --db a_share_mvp.db --date 2026-04-22 --workers 8 --attempts-per-source 3
```

数据抓取和修复脚本默认按接口限制重试次数。相同接口失败 3 次后应切换备用源或返回错误，不应无限等待或重复请求。

## 数据与输出

- `a_share_mvp.db` / `ashare_mvp.db`：A 股和指数日线。
- `ths_concept.db`：同花顺概念日线和概念名。
- `etf_mvp.db`：权益 ETF 份额和日线相关数据。
- `mining_mvp.db`：机会挖掘的策略运行、候选标的、复盘结果和基础信息。
- `daily_metrics_last40.csv`：市场情绪指标输出。
- `daily_matrics_last40.csv`：兼容旧脚本的拼写错误版本。
- `output/`：回填日志、日报、Excel 报表和 Streamlit 运行输出。

## 接手文档

- `AGENTS.md`：Codex 和通用 agent 的项目规则、路径边界和验证命令。
- `CLAUDE.md`：Claude Code 的项目阅读入口。
- `docs/architecture.md`：运行链路、数据存储和主要架构风险。
- `docs/runbook.md`：本地启动、日更修复、验证和故障处理。

## 核心文件

- `app.py`：统一启动入口。
- `app_panel.py`：主面板逻辑、数据新鲜度检查、日期选择、指标渲染和机会挖掘入口。
- `runtime_paths.py`：统一解析根目录和 `data/` 下的数据库、CSV、输出目录。
- `backfill_orchestrator.py`：后台启动长任务并写入 `output/backfill_jobs/`。
- `offline_daily_update.py`：检查最近交易日缺少哪些域的数据，并按 stock/index/concept/etf/mining 补齐。
- `repair_market_day_akshare.py`：使用 AkShare 多接口修复单个市场日的 A 股和指数日线。
- `backfill_baostock_hsA_60d_v2.py`：A 股和指数历史数据主回填脚本。
- `backfill_adata_ths_concept_index_kline_60d.py`：同花顺题材历史数据。
- `backfill_etf_equity_60d_v2.py`：权益 ETF 份额数据。
- `run_daily.py`：机会挖掘日更、复盘和报表 CLI。
- `mining/`：机会挖掘、scanner、复盘、报表和 Streamlit 子模块。
- `filtered_concept.csv`：题材过滤名单。

## 建议上传到 GitHub 的文件

- 代码文件和 `mining/`
- `requirements.txt`
- `README.md`
- `CLAUDE.md`
- `AGENTS.md`
- `.gitignore`
- `tests/`
- `docs/`

## 不建议上传的内容

- `*.db`
- `*.db-shm`
- `*.db-wal`
- `data/`
- `output/`
- `csv_out/`
- `debug_probe/`
- `V1/`
- `old version/`
- `__tmp_*/`
