# Screening v2.6 独立审核修订任务卡

执行会话从候选 `944ac514ebbcc8e5e26aaa1720698eccde5c1a23` 继续，只修独立审核确认的问题；完成后独立提交并停止。不得生产激活。状态不是“微调阈值”，而是 `changes_required`：盘中路径不可达、C 门槛漏实现、B 证据非幂等、C 提前截断、全量与真实回放未达标。

## 冻结裁决与边界

- 用户目标不变：A–E 按能力合并、`sec_code` 去重后最多 Top20；不足不补位。
- 允许写：上一任务白名单、`mining/trading_calendar.py`、`offline_daily_update.py`、本卡、对应测试、`output/screening-v2-work/v26/` 小型证据。禁止改 `app_panel.py`、依赖、生产 DB、provider 行情抓取逻辑和 migration。
- 实现/测试不得联网；日历 provider 只由既有离线更新流程在正常运行时调用。面板渲染绝不联网取日历。
- 保留 `v2.5` fixture/历史；不得修改冻结数据来迁就 v2.6，不得删/skip/放宽有效断言。

## Task 0：复现后再改

先复现并写入 `PROGRESS.md`：盘中 `expected_prior_trade_date` 无生产写入；`shrink_ratio=1.28` 的 C 仍入选；B 双 subtype 二次 shortlist 后只剩一个；C scanner 在统一 shortlist 前 `.head(20)`；全量一次为 `364 / 4 failures / 1 error / 2 skipped`。禁止重跑旧 provider/生产调查或整仓探索。

## Task 1：本地权威交易日真值链

新增一个最小模块管理忽略的 `data/a_share_trade_calendar.json`，字段至少为 `schema_version/source/retrieved_at/coverage_end/open_dates`。`offline_daily_update.py` 正常运行时以 AkShare 交易日历优先、BaoStock 备用，原子更新；两者失败保留旧文件并记录状态，严禁写入 weekday fallback。面板只读本地文件：文件来源合法、时间可解析、`coverage_end >= current_date-1天`、`retrieved_at` 不超过14天时，取 `< current_date` 的最大开市日作为 T-1；否则返回明确 unavailable。盘中有效 snapshot 证明今日在交易，不由本地工作日近似推断。

`_formal_capability_view` 显式接收解析后的 T-1，不再读从未写入的 diagnostics 字段；若 T-1 不在 clean dates 才返回 `stale_history`。增加节假日、陈旧文件、损坏文件、缺 T-1、有效 T-1 五类测试。close_final 不依赖该文件。

## Task 2：修正能力语义与幂等

- C 资格强制 `shrink_ratio<=0.75`；“站回 MA10/MA20”必须证明前一日低于对应均线且当日重新站上，另一触发仅为真实突破前3日高。补 `0.75/0.7501`、前日已在线上、近期创新低正反边界。
- shortlist 只在能力聚合后执行一次，或保证重复调用严格幂等；再次调用必须合并已有 `subtype_evidence`，B 重码证据不得丢失。
- C evaluator 返回全部 eligible，删除 `second_launch.py` 的提前 Top20；最终统一 helper 再排名截断。用至少25个乱序 C 候选证明最终代码集合与全量统一排序一致。
- 保留 A 盘中 `reference_price` 修复；补正式“评估→聚合→持久化→读取”与盘中 parity 测试。

## Task 3：测试迁移与证据

保留 `v2.5` fixture，新增 v2.6 fixture/版本明确的种子；R4 旧 fixture 不再假装验证当前 evaluator。文本 fixture 哈希按规范化 LF 或 Git blob 计算，Windows CRLF 必须同值。补齐 A–D 各硬门槛正/反/边界及“当日下跌 prior pulse=0”。

先全新 sandbox 对 `2026-07-20 --formal-only` 做一次有界 smoke；通过后按日期逐个子进程回放最近20个 usable sessions，每日独立日志、每日期限120秒、完成即 checkpoint。单日失败即停止并保留精确日期/错误，禁止无日志后台长跑。输出非空漏斗、A–E 数量、B 去重、代表性增删和源库前后哈希；生产库只可 online backup 读取。

## 完成条件

定向含 R4/history 不少于99且全绿；全量不少于364且仅原2项 skip；编译、`git diff --check` 通过；五类 P1 复现转绿；20/20 回放非空且每能力每日≤20、B无重码、生产哈希不变。未同时满足则提交可审查候选但状态必须 `changes_incomplete`，禁止激活。清理任务 PID、临时 DB/cache，独立提交一个 commit，报告 `Lessons`。
