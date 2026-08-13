# A股市场情绪观察面板 V1

这个项目会自动拉取 A 股、题材和权益 ETF 的历史数据，然后生成一个可直接查看的市场情绪观察面板以及异动选股模块

## 安装

```bash
pip install -r requirements.txt
```

## 启动

```bash
streamlit run app.py
```

## 第一次运行会发生什么

- 如果本地还没有数据库，程序会自动在 `data/` 目录下创建。
- 面板启动时会自动检查历史数据是否落后，缺多少补多少。
- 最近 40 个交易日的指标结果会写到 `data/daily_metrics_last40.csv`。
- 为了兼容旧脚本，程序也会在项目根目录额外写出一份 `daily_matrics_last40.csv`。

## 核心文件

- `app.py`：统一启动入口。
- `app_panel_auto_update_v44.py`：主面板逻辑。
- `backfill_baostock_hsA_60d_v2.py`：A 股和指数历史数据。
- `backfill_adata_ths_concept_index_kline_60d.py`：同花顺题材历史数据。
- `backfill_etf_equity_60d_v2.py`：权益 ETF 份额数据。
- `filtered_concept.csv`：题材过滤名单。

## 建议上传到 GitHub 的文件

- 以上代码文件
- `requirements.txt`
- `README.md`
- `.gitignore`
- `tests/`

## 不建议上传的内容

- `*.db`
- `data/`
- `output/`
- `csv_out/`
- `debug_probe/`
- `V1/`
- `old version/`
