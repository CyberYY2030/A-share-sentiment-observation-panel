# Screening v2.5 运营闭环收口任务卡（OPS-1 / OPS-2 / OPS-3）

- 日期：2026-08-11；最新裁决：2026-08-12
- 上游线索：`output/screening-v2-work/independent-review/REVIEW.md`（独立只读复审；其中 07-13、rc=999 和当前日口径已被本文二次复审更正，不能直接当真值）
- 原始冻结基线 commit：`534d546`（chore(runtime): freeze the actually-running version），分支 `codex/screening-v2`
- 当前已提交基线 commit：`7fd57fc`（fix: restore provisional screening activation）
- 状态：**PROTO-1、PROTO-1.1、PROTO-1.2 与 PROTO-2 的 2026-08-10 原型/canary 验收已完成，历史报告状态 `prototype_accepted_degraded_optional_data` 保留；但截至 2026-08-12，生产库没有 2026-08-11 stock/index 行及正式 v2.5/close_final batch。当前阶段状态修正为 `prototype_canary_accepted_ops_not_closed`，最新已完成交易日的自动运营闭环重新打开为 P0。**
- 当前测试基线：`python -m unittest` = 324 tests, OK (skipped=2)
- 续跑规则：PROTO-1/2 禁止重做，旧 OPS-1/2/3 章节只作根因背景，禁止原样续跑。下一唯一入口为本节 `OPS-HARDEN-1A`（源码/测试/沙箱，禁止生产写入）；通过独立复核后才可另行授权 `OPS-HARDEN-1B`（受控补齐 2026-08-11 并生成单日正式批次）。

---

## 用户已裁定的前提（按最新裁决覆盖旧裁决）

| # | 裁定 | 对卡的约束 |
|---|---|---|
| 1 | **不弃用 2026-08-07** | 08-07 现已达到 `usable_with_quarantine`，不再重复全量补抓；原型先收口 08-10，08-07 的历史正式批次转入 `OPS-HARDEN`，不得删除其数据 |
| 2 | **保持"打开才更新"**，不注册计划任务；但必须查清 08-07 为何没更新 | OPS-2 不引入常驻调度器；触发时机问题必须在面板侧解决 |
| 3 | **单次修复时间预算 = 1 小时** | 超时参数以 3600s 为准；超过 30 分钟的执行走 HEAVY-BATCH 协议（L-15） |
| 4 | **缺口必须可见** | 个股、概念的 have/expect/missing 和原因必须出现在日志、health、面板；不得伪绿 |
| 5 | **先完成可用原型**（2026-08-12 新裁决） | 股票 `usable_ratio` 原型门槛改为 `0.90`；概念为可选域，可 unavailable/空置；稳定指标面板和“只在可用股票范围内选股”优先于追补尾部缺口 |

---

## GPT 二次复审裁决（2026-08-11）

### 已确认是真问题

- `known_bad_session` 缺少“按当前数据重新判定后解除”的生产路径；08-07 补齐后若不重判，仍会被历史标记短路。
- 08-07 股票数据残缺、概念门槛过松、概念列表零行仍可能 rc=0、GB18030 排除文件被按 UTF-8 读取、概念补数无目标级续跑，均有代码或真实数据证据。
- `--dry-run` 会覆盖正式 health 证据；health 把观察日期、可用日期、writer、覆盖率、正式批次和 reader 混在一起；面板会接纳只有部分股票的日期。
- `mining_coverage_for_date` 未限定 v2.5 complete batch；不同 asof 的后台 job 可以绕过去重；180 秒只证明当前子进程被墙钟截断。

### 已推翻或必须降级的断言

1. **07-13 不是“完整但被误杀的 clean 日”。** 本轮绕过历史闩锁只读重算：5199 行中 5145 行为 `provider_halt_placeholder`、54 行 `invalid_price`、`valid_trade_rows=0`，原始状态仍为 `known_bad_session/no_valid_trade`。它应保持隔离，除非价格内容被真正修复。
2. **08-11 在 05:52–07:45 零行不是缺口。** 当时尚未收盘；任何运行都必须先计算中国时区“最近已完成且行情应就绪的交易日”，盘前/盘中不得把当日加入 close 修复计划。
3. **`rc=999` 的近因是 wrapper 墙钟超时，底层原因仍可能包含 provider 慢、限流或工作量过大。** 可确认的是进程在超时前有部分进展，不能据此宣称 provider 无问题。
4. **概念 320/390 不是正确的完成分母。** 当前 `concept_master=395`，排除文件有 44 个代码，08-07 的 320 行中有 38 个属于排除集；最终 `expect` 必须来自完成过滤且通过 A.2 retention/overlap 完整性门禁的 eligible universe，不能用约 390、近几日行数或任意一次非空 provider 响应替代。
5. **`saved_rows=0` 不能无条件判失败。** 若目标代码在执行前已完整，幂等续跑应允许零新增；只有“目标仍缺失且零新增”或“概念列表为空”才失败。
6. **`mining/history_bootstrap.py` 是 shadow-only dry-run。** 它只能做隔离预演，生产正式批次必须调用 `run_daily.py` 正式路径。

### 新增全局执行协议

- 每卡先完成代码、定向测试、全量测试和隔离副本验收，再提交代码 commit；真实生产写入只能由该冻结 commit 执行，运行期间禁止继续改代码。
- WAL 库不得用裸文件复制充当备份；对将被写入的 SQLite 库使用 online backup，并校验表清单、关键行数和 `quick_check`。四库都记录前后 SHA-256，未授权库必须不变。
- 当前卡只收口收盘运营链路；现有盘中 provisional snapshot/价格展示行为冻结且不得回退。股票池继续排除北交所、覆盖创业板与科创板。

---

## 2026-08-12 原型优先裁决与阶段收口（优先级最高）

> 本节覆盖后文旧的 OPS-1B、OPS-2、OPS-3 执行前提。后文仍保留作历史根因与设计参考；PROTO-1/2 已完成，任何 agent 均不得从旧卡续跑，也不得继续用 `0.98` 或“concept 必须 ready”阻断核心选股。

### 阶段结论（08-10 原型证据保留，最新日闭环重新打开）

- `PROTO-1` 系列最终 commit 为 `7fd57fc`；源码未在 PROTO-2 再次修改。前序独立验收为定向 97 项、全量 324 项（2 skipped）通过。
- PROTO-2 报告为 `output/screening-v2-work/proto-2-report.md/json`。生产 2026-08-10 已经原子重判为 `usable_with_quarantine`：5,018 条有效、164 条隔离，四指数齐全；concept 保持 optional unavailable。
- 正式执行只使用 `python run_daily.py --base-dir . --date 2026-08-10 --formal-only`。batch id=2、六个 formal runs、候选总数 470；同命令复跑复用 fingerprint，batch/run/candidate/state 均零增长。
- 独立复核确认这 470 条候选与 PROTO-1.2 沙箱按稳定身份字段逐条一致；生产 `a_share_mvp.db` 与 `mining_mvp.db` 的逻辑增量仅包含原子重判和该正式批次，online backup 均通过 `quick_check`。
- 页面已证明指标覆盖、08-10 正式选股结果和 concept 空态可同时稳定显示。该结论是“核心原型可用、可选域降级”，不等于 concept 已恢复，也不等于策略收益有效性已证明。
- 上述结论只证明冻结的 2026-08-10 数据面、formal parity 与降级显示可用，**不证明普通启动能自动推进到最近已完成交易日**。2026-08-12 启动后仍显示 08-10，说明运维控制面尚未收口，不得把 `prototype_accepted_degraded_optional_data` 扩写成“最新数据已就绪”或“阶段完全结束”。

### 阶段收尾后的启动 smoke 新发现（2026-08-12，未修复）

- 若页面来自先前验收端口，其进程使用 `SCREENING_BASE_DIR=output/screening-v2-work/proto-1-2-sandbox-20260812-revalidated` 与 `SCREENING_ACCEPTANCE_NOW_CN=2026-08-10T18:00:00+08:00`，这是故意冻结在 08-10 的隔离验收环境，不能用于判断生产最新日。即使切回普通生产模式，生产库当前也没有 08-11 stock/index 行或 08-11 formal batch，页面按 fail-closed 日期交集正确回退到 08-10。
- 普通生产模式打开面板时，只要最近 10 日存在 stock/index 缺口，`app_panel.py` 会在“自动追平”复选框未勾选时仍自动启动 `offline_daily_update.py`。这符合旧“打开才更新”设计，但该后台链路仍调用 `run_daily.py --range`，尚未切换到新的单日 `--formal-only` 合同。
- 本次 11:03 的真实启动证据为 `output/backfill_jobs/offline_daily_update_2026-08-11_20260812_110308.log`：面板传入总预算 180 秒并只加了 `--skip-concept`；ETF 与 mining 仍被纳入后续域。`offline_daily_update.py` 在 stock 子进程前按 `120 × 2` 预留 240 秒，导致 `_remaining_budget_seconds(...)=0`，以 rc=998、`invocation deadline exhausted before child launch` 结束。**08-11 股票修复子进程和外部行情端点均未启动，本次失败不能归因于 provider 不稳定、限流或请求频率。**
- stock 未启动后，旧编排仍让 ETF 写入 07-22..08-06，并让 mining 对 07-29..08-07 执行旧 `--range`，在 116 秒超时前留下 batch id=3（2026-07-29、v2.5/close_final、431 条 formal 候选），同时新增 unbatched legacy candidates、124 条 outcomes 和 638 条 watchlist outcomes。
- 生产只读证据：08-10 有 stock 5,182 行、四指数与 complete batch id=2；08-11 stock/index 为 0、formal batch 为 0。08-11 diagnostics 被写成 `bounded_repair_failed_for_observed_bad_session`，但实际上没有启动 repair child；这是调度失败被误记成数据质量失败，必须修正并允许数据到达后通过原子 revalidation 自愈，禁止手工删闩锁。
- 侧栏“接口最新收盘”目前来自交易日历期望值，而非真实 provider 探针。为避免把“应该有 08-11”误读成“接口已返回 08-11”，应改名为“目标/应有收盘日”；除非以后确实增加独立 provider freshness probe。
- 08-10 batch id=2、fingerprint 和 470 条候选没有被改写，四库 `quick_check` 仍为 `ok`；新增副作用不推翻 PROTO-2 的 A–E parity，但证明“普通启动的自动运营链路”尚未达到 formal-only/原子/零意外副作用标准。
- 不得直接删除 batch id=3 或恢复 PROTO-2 backup。下一卡 `OPS-HARDEN-1` 必须先在生产 online backup 副本上复现和裁定这批历史增量，再修复触发/日期集合/命令合同；任何生产清理由用户另行授权。

### 下一唯一入口：OPS-HARDEN-1A（源码/沙箱，P0，禁止生产写入）

**目标**：保留“打开才更新”，把启动链路改成“最近已完成交易日、核心域优先、单日 formal-only、一个总预算、可恢复且无历史副作用”。本卡只修源码并在 fixture/online-backup 副本验收；不得调用真实生产写入，不得清理 batch id=3。

**允许修改**：`app_panel.py`、`offline_daily_update.py`、`backfill_orchestrator.py`、`mining/data_quality.py`（仅状态/原子重判语义）、`run_daily.py`（仅补足已有 `--date --formal-only` 调用合同，禁止改 scanner/策略）、`mining/streamlit_tabs/tab_scanner.py`（仅日期/状态展示）、相关现有 tests、本任务卡及 `output/screening-v2-work/ops-harden-1a-*` 报告。禁止修改 scanner 公式/阈值、v2.5 DTO、盘中快照合同、数据库 schema、四个生产库。

**必须实现**：

1. 启动计划只选择 `latest_completed_trade_day`，本次固定验收日为 2026-08-11；不得因近 10 日历史 mining/ETF 缺口扩大本次写入范围。最近日闭环稳定后，历史回填另卡处理。
2. 为 updater 增加单一明确的域白名单（推荐 `--domains stock index` 或等价 `--core-only`）。`--skip-concept` 不足以表达核心模式；核心采集 invocation 必须排除 concept、ETF、mining，日志必须记录实际域集合。
3. 单次启动工作流总墙钟预算为 **3600 秒**。预算对象和保留量必须在启动前校验：任何 reserve 总和不得大于/等于总预算；核心 child 必须获得正 allowance。非法配置须在任何写入及任何 bad-session 记录前以 `configuration_error` 失败，禁止再用 rc=998 冒充 provider/数据质量失败。禁止简单把每个 child 都设成 3600 秒而突破总预算。
4. 只有 repair child 实际启动且 postcondition 仍失败时，才能写 `bounded_repair_failed_for_observed_bad_session`。child 未启动时使用 `not_attempted_budget` / `acquisition_blocked` 等结构化调度状态，仅进入 job/health，不得创建或强化 `known_bad_session`。对当前 08-11 误诊记录，数据到达后必须走现有原子 `revalidate_known_bad_session` 自愈，禁止手工 DELETE。
5. stock 达到 `clean|usable_with_quarantine` 且四指数齐全后，工作流才可调用且只能调用：`python run_daily.py --base-dir . --date 2026-08-11 --formal-only`。禁止 `--range`，禁止 legacy、outcome、watchlist、report；失败不得留下 partial formal batch，同 fingerprint 重跑零增长。
6. 对外区分 `market_close_ready = stock + 4 indices` 与 `screening_ready = market_close_ready + complete v2.5/close_final batch`。面板的收盘有效日期必须取指标数据日与正式选股批次日的交集；08-11 formal 未完成期间继续显示 08-10，并明确显示“目标 08-11 正在更新/等待正式批次”，禁止指标 08-11 与选股 08-10 混日。
7. 将侧栏“接口最新收盘”改为“目标/应有收盘日”。只有真实访问 provider freshness endpoint 并校验返回值后，才允许使用“接口最新”字样。
8. 保留全局后台互斥：同一 base-dir 不得因不同 asof/job name 并发运行两个 updater；页面 rerun 不重复启动。页面渲染线程不得等待长任务，必须持续显示 PID、目标日、阶段、elapsed、remaining budget、最后结构化结果。

**防作弊测试与验收**：

- 离线预算边界：总预算 180、后续保留 240 时，在零 DB 写入、零 provider/child 调用、零 bad-session 记录前返回 `configuration_error`；总预算 3600 的核心模式必须给 stock child 正 allowance。
- 离线调用捕获：目标 08-11 时只出现 stock/index repair 与一次 `run_daily.py --date 2026-08-11 --formal-only`；调用列表中不得出现 concept、ETF、`--range`、07-29..08-07、legacy/outcome/watchlist/report。
- 状态机：child 未启动、child 启动后失败、stock/index 成功但 formal 失败、全成功四种状态各有确定结果；只在全成功后 `screening_ready=true`，任一失败不伪绿。
- 日期一致性：08-11 无数据时稳定回退 08-10；仅 stock/index 08-11 ready 时仍回退 08-10；08-11 complete formal batch 后指标/选股/页面选中日同时推进到 08-11。
- 原子/幂等：任一步失败不留下 partial batch；同 fingerprint 重跑 batch/run/candidate/state 零增长；不同 target 不能消费历史缺口。
- 后台互斥：两个 asof、页面连续 rerun、过期 active.json、活 PID 四类用例；全局最多一个 updater writer。
- 运行相关定向测试、`python -m unittest`、`py_compile`；用四库 online backup 副本做 08-11 dry harness，记录前后 `quick_check`、表/关键行数、SHA-256。生产库前后哈希必须完全一致。

**交付与停止条件**：独立 commit；报告分 `passed / failed / environment-blocked`，列出精确命令、返回码、child/provider 调用次数、数据库差分和 UI 三态证据。任一生产库变化、仍出现 `--range`、no-launch 仍写 bad-session、或 08-11 未形成单日 complete batch 的沙箱证据时，状态只能为 `blocked`，禁止进入 1B。

### OPS-HARDEN-1B（1A 复核通过后另行授权；受控生产推进 08-11）

1. 冻结 1A commit，完成进程门禁；对将写入的 `a_share_mvp.db`、`mining_mvp.db` 做 SQLite online backup，对四库记录 SHA-256、`quick_check`、表与关键行数。不得把 WAL 裸复制当备份。
2. 只运行 2026-08-11 核心采集，最大总预算 3600 秒、provider 最多 3 次质量感知尝试；concept/ETF 缺失允许保持 optional unavailable，不得阻断核心。
3. stock/四指数通过后只运行 `run_daily.py --date 2026-08-11 --formal-only`；复跑一次证明 fingerprint 复用和零增长。若核心数据未达 `0.90` 原型日门槛或四指数不齐，立即停止，不运行 mining。
4. 生产验收必须证明：08-11 stock/index 质量、complete v2.5/close_final batch、指标/选股/页面日期全部为 08-11；concept 可空置且原因可见；浏览器 console error=0；启动再次刷新不重复写入。
5. batch id=3 及启动 smoke 留下的 legacy/outcome/watchlist 增量只做副本审计，不在 1B 删除。只有确认污染正式 reader parity 时才另开清理卡并请求用户授权；否则保留证据，避免为“整洁”引入恢复风险。

### 一、OPS-1B 失败裁决

- 这次失败**不是新的请求频率或 provider 超时问题**。2026-08-10 已有 5,182 行股票，其中 5,018 行完整有效；剩余 164 行 OHLCV 有效，但 `amount/pre_close/turnover_ratio` 为空，另有 20 个 eligible code 缺席。`usable_ratio=5018/5182=0.9683519876`，`coverage_ratio=5182/5199=0.9967301404`。旧 `0.98` 上限因此把可隔离尾部缺口判成整日不可用。
- 164 行必须继续标为 quarantine，并从正式 close-final universe 排除；现有 universe 的 `volume > 0 AND amount > 0` 与 adjusted-bar `row_status=valid_trade` 门禁必须保留。20 个缺席代码自然不进入 universe。**放宽的是“整日是否可用”，不是“缺字段的行是否合格”。**
- concept candidate=85 对同日 263 和历史 349 reference 未通过 `0.95` retention/overlap，A.2 正确拒绝。不得降低 A.2、不得把 expectation 改为 85、不得覆盖现有 349 行。原型把 concept 标为 optional/unavailable 并留空即可。
- 四指数齐全；ETF 1,496 个代码完整。股票核心数据已经足以进入“降级可用”路径。继续反复请求 184 个股票尾项或 concept provider 不能改变当前原型的核心价值，立即停止这种重试。

### 二、原型业务合同

1. **核心域**：`stock + 4 indices + v2.5 close_final batch`。它们决定 `screening_ready`。
2. **可选域**：`concept + ETF`。它们分别决定自己的 panel 是否 ready；concept 失败不得令 `screening_ready=false`，但总状态必须是 `degraded`，不得显示全绿。
3. **股票日门禁**：保留 `usable_ratio < 0.80` 或 `coverage_ratio < 0.80` 的 `known_bad_session`；保留 `coverage_ratio < 0.95` 的 `partial_missing`；把进入 `usable_with_quarantine` 的 usable 上限从 `0.98` 改为 **`0.90`**。阈值必须集中定义，禁止在 planner/UI 复制第二套数字。
4. **逐行门禁**：价格、成交量、成交额或 required adjusted-bar 不合格的股票不得进入正式选股；不能填造 `amount`、不能把 unknown 当 0、不能因为整日达标而放行 quarantine 行。
5. **面板口径**：指标只使用该指标字段有效的行作分母，同时显示 `valid/expected/excluded`；字段未知不得按平盘、零成交或下跌处理。没有可用 concept 时显示空态、目标日期和结构化失败原因，不显示旧数据为当日数据。
6. **日期口径**：盘后/次日读取最近 `screening_ready` 收盘日；若最新 close-ready 日尚不可用，回退到最近可用日并明确显示 `as_of` 与 stale 天数。盘中 provisional snapshot 行为保持冻结。

### 三、阶段顺序

| 阶段 | 内容 | 生产写入 | 完成后动作 |
|---|---|---:|---|
| `PROTO-1` | 统一原型门禁、可选域降级、health/UI/日期合同；测试和生产副本验收 | 禁止 | **已完成**，最终 commit `7fd57fc` |
| `PROTO-2` | 用冻结 commit 做 08-10 原子 revalidation、正式 v2.5 batch、只读浏览器验收 | 仅 a_share/mining | **08-10 canary 已完成**；历史报告状态保留，不能外推为最新日闭环 |
| `OPS-HARDEN-1A` | 修复核心域选择、180/240 预算死锁、no-launch 误诊、旧 `--range`、日期交集与全局互斥 | 禁止，先副本 | **历史：OPS-HARDEN-1A.1 已完成并停在 `ready_for_ops_harden_1b_review`；2026-08-12 更正：OPS-HARDEN-1A.2 补齐 worker 生命周期和 08-11 副本闭环后同样为 `ready_for_ops_harden_1b_review`。OPS-HARDEN-1B 仍须另行授权。** |
| `OPS-HARDEN-1B` | 用冻结 1A commit 受控补齐 08-11，并生成/复验单日 formal batch | 另行授权，仅 a_share/mining | 08-11 指标与选股同日后，最新日闭环才可收口 |
| `OPS-HARDEN` | 历史缺口、可选域恢复、batch id=3 清理评估与长期完整性 | 另行规划 | 不阻断核心原型；不得混入 1A/1B |

### 四、PROTO-1（已完成，禁止重做）

**目标**：在不访问外部行情、不写四个生产库的前提下，让当前 08-10 数据副本被判为 `usable_with_quarantine`，正式 universe 只包含有效股票；concept=85 仍被 A.2 拒绝，但只造成 optional degraded；指标面板和选股页在这种状态下可稳定读出。

**起点与白名单**：起点 HEAD 必须为 `a2461a9`；先记录 `git status --short`，不得覆盖用户未提交文件。只允许修改 `mining/data_quality.py`、确有必要时的 `mining/universe.py` / `mining/selection_context.py`、`offline_daily_update.py`、`backfill_orchestrator.py`、`app_panel.py`、`mining/streamlit_tabs/tab_scanner.py`、相关现有 tests、本任务报告与 `PROGRESS.md/BLOCKED.md`。`run_daily.py` 仅在现有正式路径无法消费统一 readiness 时才可改，并须在报告解释原因。禁止改策略公式、scanner 阈值、v2.5 DTO、数据库 schema、盘中快照逻辑。

**必须实现**：

1. 在 `mining/data_quality.py` 集中定义三条原型线 `0.80/0.95/0.90`，让所有 stock readiness 消费同一个判定；reason 改成与 0.90 一致的结构化值。不得只在 caller 传一个未被 classifier 使用的参数。
2. 保留逐行 quarantine 与 universe 活跃度过滤。health 至少输出 `stock.valid=5018`、`present=5182`、`expected=5199`、`quarantined=164`、`absent=17`（按质量 baseline）以及 selection eligible 实际数；若 stock-info eligible denominator 为 5,202，则另列 `universe_absent=20`，禁止混用两个分母。数字由运行时计算，不得硬编码。
3. 将 updater 结果拆为 `screening_ready`、`overall_status`、`domains.stock/index/concept/etf/mining`。concept A.2 失败时保留结构化 `eligible_universe_regression`，但在 prototype policy 下结果为 `screening_ready=true, overall_status=degraded, concept.ready=false`。新增明确的 `--skip-concept`（或等价单一 policy 参数）；被跳过时不得调用 provider，状态为 `optional_skipped/unavailable`，不得伪装 complete。
4. panel 启动任务在 prototype policy 下不因 concept 缺失重复启动同一 provider；核心 stock/index 已 ready 时允许生成/读取正式选股。concept panel 空置并显示原因。指标计算排除字段 unknown，并显示覆盖分母。
5. `known_bad_session` 不手工 DELETE。生产副本上通过已有原子 `revalidate_known_bad_session` 路径验证：新 raw policy 达标才清闩，失败事务回滚。`--dry-run` 仍必须零写入、零 health 覆盖。

**防作弊测试（全部离线临时 SQLite/生产 online-backup 副本）**：

- 100 行中 90 行有效、10 行缺 activity → `usable_with_quarantine`；89/100 → `partial_missing`；coverage=0.94 即使 usable=1.0 仍 partial；0 valid trade 仍 known_bad。
- 08-10 形状的副本产生 5,018 个可用行和 164 个 quarantine；正式 selection universe 不含 quarantine/absent，且每个入选基础行都满足 `volume>0 AND amount>0` 与 adjusted-bar 合法性。
- concept 85 对 263/349 仍 fail-closed、expectation 和 kline 不变；开启 prototype optional policy 后只降级 concept，`screening_ready` 仍为 true；`--skip-concept` 的 provider 调用计数为 0。
- stale/回退日期 fixture、指标 unknown 分母 fixture、concept 空态 fixture 均不得崩溃或把 unknown 当 0。
- 在生产库 online-backup 副本上运行正式 `run_daily.py --date 2026-08-10 --formal-only` 路径，得到 v2.5/close_final/complete batch；重复运行不新增同 fingerprint batch。不得手工 INSERT `selection_batches`。

**验收命令与证据**：先运行相关定向测试，再运行 `python -m unittest`、`py_compile`（所有改动 Python 文件）、`git diff --check`。全量测试不得少于当前基线；浏览器使用只读副本/禁自动修复模式，保存指标页、选股页、concept 空态三张截图及 console log。报告 `output/screening-v2-work/proto-1-report.md/json` 必须包含阈值边界、四库生产哈希前后相同、provider 调用计数 0、sandbox batch、页面日期/分母/空态、diff 清单与 `Lessons`。只提交白名单源码和测试，独立 commit；完成后停止，禁止同会话执行 `PROTO-2`。

### 五、PROTO-2（已完成，禁止重做）

1. 冻结并记录 PROTO-1 commit；对即将写入的 `a_share_mvp.db`、`mining_mvp.db` 做 SQLite online backup、`quick_check`、表/关键行数核对；concept/ETF 只读并记录 SHA-256。
2. 不访问任何 provider。对 2026-08-10 调用经测试的原子 revalidation API；预期 raw=`usable_with_quarantine`、闩锁清除、164 行继续 quarantine。任一预期不符立即回滚停止。
3. 仅通过 `run_daily.py --date 2026-08-10 --formal-only` 生成正式 v2.5/close_final batch；复跑验证 fingerprint 幂等。禁止手工写 batch。
4. 只读启动页面，验收：指标页稳定并显示数据覆盖；选股页显示 08-10 正式结果和 excluded 数；concept 显示 unavailable 空态；四指数齐全；无 traceback/console error。关闭只读实例，不终止用户进程。
5. 输出 `proto-2-report.md/json`、前后哈希、备份位置、revalidation before/raw/action/after、batch id/fingerprint/候选数、三态截图与 `Lessons`。成功状态为 `prototype_accepted_degraded_optional_data`；concept 未恢复不算失败。未经用户授权不得执行本节。

---

## 历史续跑入口（2026-08-11，已由 2026-08-12 原型裁决取代，禁止直接执行）

### 阶段状态

| 阶段 | 状态 | 下一动作 |
|---|---|---|
| OPS-1A.1 股票质量合同 | **完成**，commit `9805084` | 禁止重做、禁止改回行数成功条件 |
| OPS-1A.2 概念分母防缩小 | **历史记录：已完成**，commit `a2461a9` | 禁止重做 |
| OPS-1B 生产恢复 | **历史记录：两次失败，已终止** | 禁止原样重跑 |
| OPS-2 触发/health/正式批次 | **旧计划，已被 PROTO-1/2 取代** | 不可执行 |
| OPS-3 面板日期门禁 | **旧计划，已被 PROTO-1/2 取代** | 不可执行 |

### 已复核事实

- OPS-1A.1 报告：`output/screening-v2-work/ops-1a1-report.md`；定向 43/43、全量 306（2 skipped）、`py_compile` 和 `git diff --check` 均通过；四库哈希前后相同。
- 已修复：无效已有行可重试；provider 行入库前分类；Tencent 股票缺 `amount` 时拒绝；子进程 rc 与父进程共同使用 latch-independent raw postcondition；旧 `stock_count >= 2000` 伪成功条件已删除。
- 生产 2026-08-10 仍是 `partial_missing`：股票 5,182 行，其中 4,587 完整、595 条 Tencent 行缺 `amount`；按当前 5,202 股票池还有 20 个代码缺席，待处理总数 **615**。活动 `known_bad_session` 未清除。
- 2026-08-07 已为 `usable_with_quarantine`；不再重跑全市场，只在 OPS-2 补正式批次。
- 2026-08-10 指数已齐；ETF 为 **0/1,496**；生产 `selection_batches` 仍只有 2026-08-06 的一个 v2.5/close_final/complete 批次，08-07 和 08-10 均缺正式批次。
- 概念真实行数为 349，但同一日 provider eligible universe 曾由 349 缩为 263，`concept_coverage_expectations` 被无条件覆盖，导致 `263/263` 假绿。日志证据在 `output/screening-v2-work/ops-1-20260810-r3-readonly-status.json`。

### 已知事实与禁止重做

- `do_not_repeat`：不得重新全仓巡检 R0.1/R1/R2+/R3/R4/R4.4；不得再把请求频率、Chrome CDP、THS Updater 或 Python PID当作本轮默认根因；不得重跑 OPS-1A.1 红绿测试来替代 A.2 实现。
- 不改策略公式、v2.5 DTO、盘中快照、数据库 schema 或生产数据。
- Tencent 股票源保留为“可观察地拒绝”即可；本任务不为节省少量请求改 provider 顺序。

### 历史时点的唯一任务：OPS-1A.2 概念 eligible universe 防缩小（已完成，禁止执行）

**目标**：不完整 provider 列表不得缩小完成分母或返回成功；完成后可观察到 `349 → 263` 被 fail-closed，原可信 expectation 保持不变。

**写入白名单**：`backfill_adata_ths_concept_index_kline_60d.py`、确有传播结构化失败需要时的 `offline_daily_update.py`、`tests/test_backfill_rules.py`、`output/screening-v2-work/ops-1a2-*`、`output/screening-v2-work/PROGRESS.md`、`output/screening-v2-work/BLOCKED.md`。其余只读；四个生产库禁止写入。

**任务 0**：只读确认 HEAD=`9805084`、四库 SHA-256 与 OPS-1A.1 报告一致、08-07 expectation=349、08-10 expectation=263。任一事实变化则只把差异写入 `BLOCKED.md`，不猜测修复。随后在 `PROGRESS.md` 记录目标、顺序、风险和首个入口。

**实现合同**：

1. provider list 必须先完成规范化、88xxxx 过滤、去重和排除集处理，再形成 candidate set。
2. reference 分别取“同日已存 expectation”和“最近一个较早可信 expectation”（存在几个就校验几个）。集中定义 `MIN_CONCEPT_UNIVERSE_RETENTION=0.95`；candidate 对每个 reference 的 `count_ratio` 和 `overlap_ratio` 均须 `>=0.95`，不得只挑较小集合来通过。
3. 低于门槛：在任何 expectation/master/kline 写入前返回非零；保留旧 expectation；输出结构化 `eligible_universe_regression`、candidate/reference 数量、比例和缺失代码摘要。上层不得把它改写为成功。
4. 通过门槛：同日 expectation 只允许幂等或单调扩展为 `existing ∪ candidate`，绝不缩小。首次 bootstrap 必须显式记录 `universe_bootstrap=true`；生产已有 reference，不允许静默重建。
5. `--limit > 0` 只用于测试/探针，不得持久化正式 expectation，也不得声称目标日 complete。
6. 不硬编码 349/263，不降低既有完整性阈值，不修改或删除旧测试，不用 mock 替代被测数据库状态。

**必须先红后绿的 fixture**：`349→263` 拒绝且原值不变；`349→349` 幂等；`349→351` 单调扩展；新日期 candidate 低于最近可信 reference 时拒绝；`--limit` 不污染 expectation。全部使用临时 SQLite，不访问网络。

**验收**：

```powershell
Set-Location -LiteralPath 'D:\BaiduNetdiskDownload\cursor workflow\adata_sentiment_dashboard'
& 'C:\Users\TY_trader1\AppData\Local\Programs\Python\Python311\python.exe' -m unittest tests.test_backfill_rules
& 'C:\Users\TY_trader1\AppData\Local\Programs\Python\Python311\python.exe' -m unittest
& 'C:\Users\TY_trader1\AppData\Local\Programs\Python\Python311\python.exe' -m py_compile backfill_adata_ths_concept_index_kline_60d.py offline_daily_update.py
git diff --check
```

全量测试不得少于 306（2 skipped）；故意失败证据、绿色输出、沙箱 JSON、四库前后哈希和 diff 清单写入 `ops-1a2-report.md`。只提交白名单源码/测试，独立 commit；`output/` 证据不要求提交。完成后状态写 `ready_for_ops_1b_review` 并停止，禁止同会话启动生产修复。

---

## 真值卡

### 业务定义

- **目标收盘日**：按中国时区和交易日历计算的最近已完成交易日；交易日 17:30 前必须回退至前一交易日。显式 `--asof` 也不得绕过这个 close-ready 边界，除非使用仅测试可用的冻结时钟。
- **股票可用收盘日**：目标日必需指数齐全，且 `inspect_stock_session` 状态 ∈ {`clean`, `usable_with_quarantine`}。distinct code 是证据字段，不是单独真值。**不是** `MAX(trade_date)`。
- **域完整性**：stock/index 决定 Screening 正式批次是否可运行；concept/ETF 分别决定对应面板域是否 ready。health 必须同时报告 `screening_ready` 与各域状态，不能让 concept/ETF 失败伪装成选股失败，也不能把它们静默判绿。
- **正式批次**：`selection_batches` 中 `definition_version='v2.5' AND mode='close_final' AND status='complete'` 的一行，由 `run_daily.py` 正式路径产生。
- **每日运营闭环**：行情更新 → 质量门禁 → close_final 批次 → 面板与筛选页读出同一个可用收盘日。四段任一断裂即闭环未完成。
- **不做什么**：不新增或改写盘中选股/盘中批次，不做多机部署，不引入常驻调度进程；已有盘中快照显示路径保持原行为。

### 恒等式与硬约束

1. 原型质量分级阈值以最新裁决的 `0.80 / 0.95 / 0.90` 为准：`0.80` 是系统性坏日线、`0.95` 是覆盖率线、`0.90` 是 present rows 的 usable 线。旧 `0.98` 仅为历史生产强化目标，不得继续阻断原型；阈值必须集中定义并有边界测试。
2. `stock_coverage_for_date` 的 fail-closed 语义不得反转：status 必须 ∈ {clean, usable_with_quarantine} 才算覆盖（`offline_daily_update.py:152-153`）。
3. `known_bad_session` 只能由一个经过测试的原子 revalidation helper 清除：先忽略闩锁计算 raw quality，只有 raw status 达标才在同一事务中调用清除 API。禁止业务代码手写 DELETE，禁止“先清除、再检查”的瞬时 fail-open 顺序。
4. 面板与筛选页必须消费同一个"可用收盘日"函数，不得各算各的。
5. 正式批次只能由 `run_daily.py` 产生；禁止手工 INSERT 或改写 `selection_batches`。
6. 任何生产库写入前，先冻结 commit/源码清单；对实际写入库做 SQLite online backup，对四库记录写入前后 SHA-256。
7. 跨模块状态用结构化字段传递，消费者不得用正则从 `reason` 文案反推状态（L-33）。
8. `mining/history_bootstrap.py` 只能写影子库；生产 `selection_batches` 的当前单日运维入口是 `run_daily.py --date <day> --formal-only`。旧 `--range` 是完整日更/历史流程，不得用于单独补正式 A–E batch。

### 历史证据源（OPS-1A.1 前；当前实现以 `9805084` 为准）

> 下表解释原始缺陷为什么成立，其中“无生产调用者”“旧成功条件”等描述已被后续提交修复，不得作为当前源码结论。

| 结论 | 证据 |
|---|---|
| known_bad 闩锁 | `mining/data_quality.py:348-388` 短路返回；`:126` `clear_known_bad_session` 全仓无生产调用者。**运行时实证**：08-08 日志该日 `coverage_baseline: 5199, coverage_ratio: 0.076, reasons: ['coverage_ratio_below_0_80']`（真实计算）；08-10 日志同一天变成 `coverage_baseline: None, coverage_ratio: None, reasons: ['bounded_repair_failed_for_observed_bad_session']`（已不再计算，直接读标记） |
| 闩锁缺少重判路径 | `mining/data_quality.py:348-388` 见诊断记录即短路返回；`:126` `clear_known_bad_session` 全仓无生产调用者。真实 08-07 一旦补齐，现有 reader 仍不会观察新数据；这是代码合同缺口，需用 fixture + 修复后的 08-07 双重证明 |
| 07-13 不是误杀反例 | 本轮直接调用 `_raw_session_summary` + `_classify_summary`：5199 行、5145 个 provider halt placeholder、54 个 invalid price、0 个 valid trade，raw status=`known_bad_session/no_valid_trade`。该日继续保持闩锁才是正确结果 |
| 状态语义混叠 | `STATUS_BAD == STATUS_KNOWN_BAD == 'known_bad_session'` 使消费者无法仅凭 status 判断“raw 本轮坏”还是“持久闩锁”。修复优先增加结构化 `latched/raw_status` 证据并收紧 mark/revalidate 流程，不要求破坏既有公开 status 字符串合同 |
| 触发时机 | `app_panel.py:2977-2999`，17:30 门槛（`CN_CLOSE_DATA_READY_TIME`）。2026-08-07 共 11 次触发，时间 07:06–14:24，**全部早于 17:30**，因此 asof 一律为 2026-08-06 |
| 180s 硬编码 | `app_panel.py:4274` |
| 单次调用无外层重试 | `offline_daily_update.py:1166-1192`，`for market_day in market_days:` 单趟，每日调用一次 `_run(..., timeout_sec=max(30, timeout_sec))` |
| rc=999 的近因 | `offline_daily_update.py:469-481` 把 `TimeoutExpired` 等 wrapper 异常统一映射为 999；本次日志明确是 `timed out after 180 seconds`。底层 provider 性能/限流仍未被排除 |
| concept fail-open | 08-08 日志：`concept list rows=0` → `DONE saved_rows=0 fail=0` → `concept rc=0 attempt=1/3`（零行仍判成功） |
| filtered_concept.csv 编码 | 08-08 日志 `'utf-8' codec can't decode byte 0xc4 in position 28` → `exclude_set empty`。本轮逐字节验证：UTF-8 失败，GB18030 正确解出中文；文件含 44 个 distinct 排除代码 |
| concept 无续跑 | `backfill_adata_ths_concept_index_kline_60d.py:415-464`，每次从索引 1 重走全部概念 |
| 面板接纳残缺日 | `app_panel.py:892-899`（有任意一行即算可用日）、`:923-944`、`:4512-4529`（按原始日期取截面）；只读复现结果见下 |
| 筛选页 fail-closed 正确 | `mining/streamlit_tabs/tab_scanner.py:647-695` |

### 历史数据实况（本机实测，2026-08-11 05:52–07:45；仅作根因背景）

> 本节早于 OPS-1 多轮恢复与 OPS-1A.1。当前执行不得使用本节的 993/零行/旧哈希作前置判断；以“当前续跑真值与唯一入口”为准。

- 根路径 `D:\BaiduNetdiskDownload\cursor workflow\adata_sentiment_dashboard`，分支 `codex/screening-v2`，基线 `534d546`。
- `a_share_mvp.db` 241,717,248 B，journal_mode=wal，quick_check=ok。**`selection_session_diagnostics` 表在这个库里，不在 `mining_mvp.db`。**
  - 2026-08-06：distinct code **5203**，coverage_ratio 1.0025，usable_ratio 1.0，status **`clean`**。
  - 2026-08-07：distinct code **993**，指数 4 个齐全，status `known_bad_session`（已闩锁）；隔离 4 只（000632/000635/000636/002299，`missing_required_field`）。
  - 2026-08-10（周一交易日）：**零行**，status `known_bad_session`（reason `missing_stock_rows`，**计算得出，未闩锁**）。
  - 2026-08-11 在本段测量时尚未收盘，零行是预期状态，不得列入 close 缺口；执行时是否纳入取决于冻结时钟是否已过 17:30。
  - 08-07 的 `coverage_baseline` 实测 **5199**；`stock_info` 5202 行。
- **`selection_session_diagnostics` 实测只有 2 行**：

  | trade_date | reason | source_errors 摘要 | updated_at |
  |---|---|---|---|
  | 2026-07-13 | `bounded_repair_failed_for_observed_bad_session` | `ModuleNotFoundError: No module named 'akshare'`（4 个指数全失败） | 2026-08-06T14:09:05 |
  | 2026-08-07 | `bounded_repair_failed_for_observed_bad_session` | `timed out after 180 seconds` | 2026-08-10T10:17:35 |

- **07-13 的纠正证据**：distinct code 数量看似完整，但价格内容不可用。raw 重算得到 `valid_trade_rows=0`、`provider_halt_placeholder=5145`、`invalid_price=54`。因此本卡不得把“行数完整”写成“数据已补齐”，也不得以 07-13 转绿作为解闩验收。
- **真正的闩锁缺陷**：持久标记会让 reader 永远不再 raw reclass。08-07 当前本来就应为坏；当它真正补齐后，若没有原子 revalidation 才会成为误杀。`STATUS_BAD` 同值主要造成来源不可观察和重复 mark 风险，不能单独证明 08-10/08-11 必然“受传染”。
- `ths_concept.db` 62,271,488 B，journal_mode=**delete**，残留 `ths_concept.db-journal` 480,680 B @2026-08-10T10:16:53（只读打开与 quick_check 均正常）。08-07 raw rows=320；其中 38 个在当前 44-code 排除集中。`concept_master=395`，按当前 master 粗估 eligible universe=351；该段估算已被后续 349→263 分母缩小证据更新，生产验收必须经过 A.2 完整性门禁。
- `etf_mvp.db` 26,595,328 B，最新日 2026-08-06，单日真实行数约 **1493**。
- `mining_mvp.db` 26,550,272 B：`selection_batches` **仅 1 行**（batch_id=1，2026-08-06，v2.5，close_final，complete，fingerprint `03ec0d541c02…`，created/completed 2026-08-10 19:43:04）；formal runs 2525–2530 候选 **110/3/191/0/0/85 = 389**，与 `candidates` 实际行数逐条相等；strategy_runs 2530；candidates 26828；outcomes 24937；pullback_state_history **0**；watchlist_snapshots 17170（max 2026-08-06）；strategies 20。
- **股票修复吞吐实测**：0 → 397（一个 180s 窗口，2.2 codes/s）；397 → 993（另一个 180s 窗口，3.3 codes/s）。该样本只支持“08-07 股票可能在约 21~32 分钟补完”，不能证明 stock+index+concept+ETF+mining 全链能在 1 小时内完成。
- **原缺口计划实测**（`asof=2026-08-11, days=10`，只读，四库 mtime 未变）：

  ```
  expected: 2026-07-29 … 2026-08-11（10 个交易日）
  ok: False
  stock:   ['2026-08-07', '2026-08-10', '2026-08-11']
  index:   ['2026-08-10', '2026-08-11']
  concept: ['2026-08-10', '2026-08-11']      ← 08-07 不在其中
  etf:     ['2026-08-07', '2026-08-10', '2026-08-11']
  mining:  ['2026-08-07', '2026-08-10', '2026-08-11']
  ```

  三条推论：
  - **concept 门槛 fail-open 已坐实**：08-07 raw 320 > 100，未进入缺口清单；但正确的结构化证据应是 eligible `have/expect/missing`，不是 320/390。
  - 该命令在早盘错误把 08-11 当成 close 缺口，暴露了 `resolve_expected_trade_days(asof=...)` 缺少 close-ready 边界；修复顺序和预算评估必须先排除此假缺口。
  - 对真实 eligible gaps，单日长任务仍可能饿死后续日期；需要总 deadline + 分片/进度调度，不得简单把每个子进程 timeout 都设成 3600。
- **`2026-07-13` 已滑出 10 天窗口，但它当前确实是坏数据。** 维持 known_bad 是安全行为；若未来要修价格内容，应另立历史数据修复卡，不能在本卡中只清标记。
- 四库 SHA-256（复审前后一致）：mining `9153F14D…215BBE`、a_share `663E6CCA…F7E52DD5`、ths_concept `4C053170…8062CC8E`、etf `4C2585BB…DA3E259D4`。

### 环境拓扑

- **唯一触发路径**：`app_panel.py:4263-4279` → `backfill_orchestrator.start_background_job` → `subprocess.Popen(creationflags=CREATE_NEW_PROCESS_GROUP, stdout=日志文件, stdin=DEVNULL)`。**无任何 Windows 计划任务引用 `daily_job.ps1`。**
- **解释器实况**：`.venv\Scripts\python.exe` 存在但**已损坏**（指向不存在的 `C:\Users\logan\AppData\Local\Programs\Python\Python311\python.exe`，实测 `--version` 退出码 **103**）。实际在跑的是 `C:\Users\TY_trader1\AppData\Local\Programs\Python\Python311\python.exe`（由 Streamlit 的 `sys.executable` 传下去）。
  - `daily_job.ps1:7-28` 的候选顺序是 `.venv` → `py -3` → `python`，但它**逐个实跑 `--version` 并检查 `$LASTEXITCODE`**，坏 venv 返回 103 会被跳过，落到 `py -3`（实测退出码 0）。**因此 `daily_job.ps1` 的解释器选择是健壮的，不需要修**。
  - 坏 `.venv` 的实际影响面仅限于：手工 `activate`、IDE 解释器配置、以及任何**不做探测就直接拼 `.venv\Scripts\python.exe`** 的新代码。属于环境卫生问题，不属于本轮三卡范围。
- **`--dry-run` 有副作用（陷阱）**：`offline_daily_update.run_offline_update` 在 `dry_run` 分支（`:1154-1158`）返回前仍会调用 `write_health_summary`，**覆盖 `output/health_latest.md`**，并调用 `_send_daily_push`。因此 CLAUDE.md 里那条"验证命令"会销毁当前的 health 证据快照。**执行任何 `--dry-run` 前必须先备份 `output/health_latest.md`。**
- **作业去重**：`backfill_orchestrator.py:49-66` 用 `<job_name>.active.json` + PID 存活判断去重；job name 含 asof，**不同 asof 会被视为不同作业**。
- **写入者**：`offline_daily_update.py`（调度）、`repair_market_day_akshare.py`（个股/指数行）、`backfill_adata_ths_concept_index_kline_60d.py`（概念）、`backfill_etf_equity_60d_v2.py`（ETF）、`run_daily.py`（正式批次）、`mining/data_quality.py`（`selection_session_diagnostics`，唯一写入者）。
- **消费者**：`app_panel.py`（情绪面板）、`mining/streamlit_tabs/tab_scanner.py`（筛选页）、`offline_daily_update.write_health_summary` → `output/health_latest.md`、`mining/notify.py`（Telegram，未配置）。
- **影子能力边界**：`mining/history_bootstrap.py` 文件头和实现均冻结为 shadow-only dry-run，只可验证影子库，不可“实跑”生产。修复日的生产正式批次复用 `run_daily.py --range <day> <day>`。

### 历史未知项（按所属后续阶段处理，OPS-1A.2 不得扩展重测）

| 未知 | 影响 | 谁来测 |
|---|---|---|
| 启动父进程退出后后台任务是否存活（Windows `CREATE_NEW_PROCESS_GROUP` 不等于完全 detach） | 若不存活，1 小时预算形同虚设 | **OPS-1 第 0 步用隔离 parent/child harness 实测；禁止关闭用户正在运行的 Streamlit** |
| 两个不同 asof 的作业能否并发跑、是否会并发打同一 provider | 并发会放大限流风险 | OPS-2 |
| 当次 eligible concept code set 在 1 小时内能否补完 | 决定 concept 是否需要跨 invocation 续跑 | OPS-1（先小样本测速率） |
| 当次 source list 过滤后的 concept eligible universe 与 ETF eligible universe | 决定真实 expect/missing，不能从旧日行数猜 | OPS-1 Task 0，小样本/只读探针；concept 可用旧可信集合做完整性 reference，但不得把它伪装成本次 provider 成功；源不可用则标 `unavailable` |
| 复权口径一致性 | 影响策略正确性 | 恢复日更后另立卡，本轮不做 |
| provider 实时快照 | 盘中路径 | 本轮不做，保持 `unproven` |

### 完成闸门

- 最低完成标签：OPS-1 `real-env-verified`（真实生产库写入并达标）；OPS-2 `real-env-verified`（真实交易日闭环）；OPS-3 `broadly-verified` + 人工开页面一次（L-11）。
- 每卡交付必须报告：passed / failed / environment-blocked 三段分列，禁止只写"完成"。
- 全局回归：`python -m unittest` 必须全绿（当前基线：`Ran 306 tests`，`OK (skipped=2)`；只可增加，不得减少）。

### Lessons 投递（global canonical `~/.claude/LESSONS.md`，触发匹配的活跃条目）

> 解析说明：`check_lessons_ledger.py` 在本机不存在，按全局 canonical 直接检索；本项目无 `.agents/LESSONS.md`，project scope 为空。

| 编号 | 与本卡的关系 |
|---|---|
| **L-1** | OPS-1/OPS-2 要消除重复的静态门槛默认值，并把 180 秒改为总 deadline/分片预算。改完必须全仓搜旧字面量；不能把动态 denominator 又复制成三套新默认值。**本仓库无 `scripts/check_stale_literal.py`，替代命令见 OPS-1 验收段** |
| **L-11** | OPS-3 是 UI 改动，交付前必须人工开一次页面 |
| **L-15** | OPS-1 的 08-07 全量修复实测 21~32 分钟，叠加 concept 后可能超 30 分钟 → 走 HEAVY-BATCH：冻结执行包 → 预检 → 脱离会话的后台任务 → 新会话只读紧凑证据验收。**计算期间模型轮询预算为 0** |
| **L-20** | 本卡的「数据实况 / 环境拓扑」两节即为该条要求的产物 |
| **L-32** | OPS-2 的 health 正交状态轴直接命中：空表或"0 异常"只能在真实 writer 于同窗口成功运行时判绿；必须把"有意停用"与"意外未运行"分开 |
| **L-33** | health 与日期证据必须用结构化字段传递，禁止从 `reason` 文案正则反推 |
| **L-34** | 本卡所有验收命令必须带 `cd <repo>` 并在目标默认工作目录逐字可跑 |
| **L-31** | 交付分支为 `codex/screening-v2`（用户在途分支），不得切到 `main` 提交 |

### Lessons 收录裁决（本轮候选）

| 候选 | 裁决 | target_scope |
|---|---|---|
| "写入 fail-closed 标记的模块必须同时具备原子 raw revalidation 路径；清除只能发生在重判达标之后" | **create** | 先落 project（`.agents/LESSONS.md`，需新建）；若后续在其它项目复现同型缺陷再 promote 到 global。理由：默认取最窄 scope |
| "外部脚本 rc=0 或 saved_rows=0 都不能单独证明成功；必须以目标终态覆盖率判定，已完整的幂等零新增允许成功" | **create** | project |
| "定时/触发类能力的验收必须包含'触发时机'本身，不只是'触发后行为'" | **update**（并入 L-32 的 writer/freshness/disabled 三字段思路） | global，待 OPS-2 完成后再定稿 |
| 其余 | **skip** | — |

---

## 排序与文件所有权

**三卡严格串行，禁止并行，禁止开重叠 worktree。** 文件域重叠如下：

| | OPS-1 | OPS-2 | OPS-3 |
|---|---|---|---|
| `offline_daily_update.py` | ✅ 主 | ✅ 改 health/coverage | — |
| `mining/data_quality.py` | ✅ 主 | — | — |
| `repair_market_day_akshare.py` | ✅ 主 | — | — |
| `backfill_adata_ths_concept_index_kline_60d.py` | ✅ 主 | — | — |
| `app_panel.py` | — | ✅ 触发/预算 | ✅ 日期门禁/提示 |
| `backfill_orchestrator.py` | — | ✅ 全域互斥 | — |
| `daily_job.ps1` | — | 只读（已确认无需修改） | — |
| `run_daily.py` | — | ✅ | — |

- OPS-1 ∩ OPS-2 = `offline_daily_update.py`；OPS-2 ∩ OPS-3 = `app_panel.py`。**任何两张卡都不能并行。**
- 一卡一 commit，可独立 revert；后卡以前卡 commit 为 parent。
- 交付后逐 commit 检查 diff 纯度，发现无关改动不并入。
- 每卡的真实数据动作必须发生在代码 commit 之后；产物报告可写 `output/`，不得为了补报告再改运行代码。
- 当前顺序已由 2026-08-12 裁决更新为：`PROTO-1 → 独立复核 → 用户授权 → PROTO-2 → OPS-HARDEN`。旧 `OPS-1A.2 → OPS-1B → OPS-2 → OPS-3` 顺序只保留为历史记录。

---

# OPS-1　历史设计：补齐缺口 + 解除闩锁 + 缺口可见（暂停，禁止原样续跑）

**前置**：OPS-1A.1 commit `9805084` 已完成；当前从 OPS-1A.2 开始
**完成标签目标**：`real-env-verified`

**写入白名单**：`offline_daily_update.py`、`mining/data_quality.py`、`repair_market_day_akshare.py`、`backfill_adata_ths_concept_index_kline_60d.py`、相关现有测试文件、`output/screening-v2-work/ops-1-*`。`filtered_concept.csv` 只读；确需规范化文件本身必须另报 blocker。OPS-1A 禁止生产库写入；OPS-1B 仅允许正常补数写 `a_share_mvp.db`、`ths_concept.db`、`etf_mvp.db` 及 diagnostics，禁止写 `mining_mvp.db`。

## 一、历史问题清单（解释来源，禁止按本节重做已完成工作）

> 闩锁原子重判、质量感知重试、provider 入库准入和父子 raw postcondition 已由当前基线实现。当前未完成项仅为概念分母防缩小、生产恢复和后续运营闭环。

1. **闩锁只进不出；08-07 真正补齐后仍无法被系统承认。**
   - 历史上 `2026-08-07` 只有 993/5199 只；当前已经达到 `usable_with_quarantine`，此数字不得再作为重跑理由。
   - 这里的“误杀”是**补齐后的未来状态**，当前 08-07 本来就应为坏；代码缺陷是补齐后仍不会 raw reclass。
   - `2026-07-13` 不是 clean 对照组，其 raw 数据为 0 valid trades，必须继续隔离。
   - `STATUS_BAD` 与 `STATUS_KNOWN_BAD` 同字符串造成 raw/latched 语义不可观察和重复 mark 风险；修复应增加结构化来源字段，优先保持公开 status 合同稳定。
2. **2026-08-10 是真实 close 缺口；2026-08-11 是否为缺口取决于执行时是否已过 17:30。** 当前 planner 接受任意 `asof` 并会在早盘错误纳入当日，这是比“升序饿死”更前置的问题。
3. **概念备份路径 fail-open**：概念列表取到 0 个、`saved_rows=0`，脚本仍返回 rc=0，上游判为成功。
4. **`filtered_concept.csv` 是 GBK 文件**，UTF-8 解码失败 → 排除集静默为空 → 排除规则等于没有。
5. **概念补数无断点续跑**，每次从第 1 个概念重走 390 个；3 次重试只换 `--asof` 的日期格式，是同源无效重试。
6. **门槛 100 让残缺日判为完整**（概念 raw rows=320、ETF 历史约1493），且用户看不到 eligible `have/expect/missing`。08-07 概念未进入缺口清单是真问题；“320/390”只是 raw 行数对比，不能作为最终质量合同。

## 二、历史根因（已解决与未解决边界见当前续跑入口）

| 问题 | 根因指针 |
|---|---|
| 1 | `mining/data_quality.py:348-388` 见诊断记录即短路，`:126` clear API 无生产消费者。根因是缺少“忽略闩锁 raw reclass → 达标才原子清除/否则保留”的状态转换，不是 07-13 行数本身 |
| 2 | `resolve_expected_trade_days` 不理解 17:30 close-ready 边界；`offline_daily_update.py:1162-1192` 又按日期单趟调用，每个子进程各自吃满 timeout，没有整轮 deadline、分片公平性或当前日保护 |
| 3 | `backfill_adata_ths_concept_index_kline_60d.py` 在 `concept list rows=0` 时仍走到 `DONE saved_rows=0` 并正常退出；`offline_daily_update.py:1244` 只看 `rc == 0` |
| 4 | `filtered_concept.csv` 逐字节验证为 GB18030 可正确解码（UTF-8 失败），44 个 distinct code；读取处只用 UTF-8 且异常后静默返回空集合 |
| 5 | `backfill_adata_ths_concept_index_kline_60d.py:415-464` 无 skip-existing；`offline_daily_update.py:1227-1245` 三次 asof 格式重试 |
| 6 | 门槛值在 **3 个位置各抄了一遍**。stock 的 2000 又与 `inspect_stock_session` 动态 baseline 重复；concept/ETF 只有静态 row threshold，没有 eligible code-set denominator，导致“行数够但缺关键代码”不可见 |

## 三、历史实施设计（当前执行顺序由 OPS-1A.2/OPS-1B 新合同覆盖）

### 第 0 步（阻塞性预检，必须先做）

实测启动父进程退出后后台任务是否存活。使用临时目录和隔离 parent/child harness 调用 `start_background_job`，让 parent 自行退出并观察 5 分钟日志；**不得关闭、杀死或复用用户正在运行的 Streamlit**。

- **存活** → 按 1 小时预算设计，走后续步骤。
- **不存活** → **停手上报**，因为"打开才更新 + 1 小时预算"这个组合不成立，需要用户重新裁定（改为前台等待、或改用 `daily_job.ps1` 手动执行）。

### 第 1 步　解闩（这一步是本卡的核心）

新增两个职责清晰的 API：只读 `reinspect_stock_session(conn, day)` 忽略闩锁计算 raw quality；写入 `revalidate_known_bad_session(conn, day)` 在单一事务中记录 before/raw/action/after，只有 raw status ∈ {clean, usable_with_quarantine} 才调用 clear API。raw 仍坏时保留/更新闩锁。**禁止 clear → inspect 的顺序。**

- `revalidate_known_bad_session` 使用 caller-owned `BEGIN IMMEDIATE`；现有 `ensure/mark/clear` 的内部 `commit()` 必须改为可由调用者控制，避免“名义原子、实际中途提交”。表结构初始化在事务前完成。
- 每次生产 revalidation 把 before/raw/action/after 写入 `output/screening-v2-work/ops-1-revalidation.json`；诊断表可以继续表示“当前活动闩锁”，审计证据不得只存在终端滚屏中。

- **天然红对照组是 07-13**：raw `no_valid_trade`，revalidation 后必须继续 known_bad，不能因 5199 行而转绿。
- **天然先红后绿组是 08-07**：补齐前 raw coverage 约 0.191，闩锁保留；真实补齐并达标后才清除。
- 历史约束曾禁止修改 `_classify_summary` 的 0.80/0.95/0.98；**该句已被 2026-08-12 用户裁决取代**。当前必须按 PROTO-1 集中改为 0.80/0.95/0.90，并保留 `latched`、`raw_status`、`revalidated_at` 等结构化证据。
- 禁止业务代码手写 DELETE。clear API 可以内部删除活动闩锁，但调用必须发生在 raw 达标判定之后，并把 before/raw/after 写入结构化运行日志。
- `_mark_unrecoverable_bad_stock_sessions` 必须依据“本轮真实尝试过 + raw 仍坏 + 当前未被同一证据重复标记”，不能依据含糊的 status 字符串反复 upsert。

### 第 2 步　预算与顺序

- `--timeout-sec=3600` 定义为**一次 offline update invocation 的总 deadline**，不是每个子进程都独立拥有 3600 秒。每次 `_run` 只能取得剩余预算，并为后续域预留可观察的调度机会。
- 先由 close-ready 函数剔除盘前/盘中的当日；显式历史修复只接受已经收盘的交易日。
- 08-07 已达到数据质量门禁，不再全量补抓。OPS-1B 的下一场生产恢复只针对 **08-10**；常规自动模式再采用“最新 eligible gap 优先 + round-robin 分片”，避免一个旧日或一个 domain 吞完预算。
- 同一交易日在总 deadline 内允许多轮调用 `repair_market_day_akshare.py`；每轮利用已存在 code 续跑，并输出 before/after 增量。
- 单日连续 3 轮 distinct code 增量为 0 → 该日本轮放弃，记录原因，让出预算给下一日。

### 第 3 步　概念侧

- **rc 判定加终态后置条件**：`concept list rows == 0` 一律失败；`saved_rows == 0` 只有在目标 eligible code set 已经完整时可作为幂等成功，否则失败。
- **去掉 3 次 asof 格式重试**，改为单次调用；确需重试时必须换源或换参数，不换日期格式。
- **加目标级续跑**：由 updater 传入 target-day set；脚本只请求至少一个目标日仍缺失的 eligible concept code，并输出每个目标日的 have/expect/missing。
- **修 `filtered_concept.csv` 读取**：按 `utf-8-sig` → `gb18030` 的明确顺序尝试，每次都验证表头与 6 位代码；失败必须中止，不得再尝试 Big5/Latin-1 这类可解码但会产生乱码的编码。
- 生产恢复显式使用 `--purge_excluded 0`。当前默认 purge 会删除全历史排除概念，属于另一项破坏性迁移，不得夹带进缺口修复。

### 第 4 步　门槛与缺口可见（用户裁定 4）

- stock 直接复用 data-quality baseline/status，并输出 eligible universe、valid/halt/quarantine/missing；移除静态 2000 作为第二套真值。
- concept `expect` = 当次 source list 规范化、88xxxx 过滤、去重、应用排除集后，且通过 A.2 retention/overlap 门禁并与同日旧 expectation 单调合并的 code set；`have` 只数该集合与当日 DB 的交集，额外/已排除代码不得虚增覆盖率。
- ETF `expect` 必须来自明确的 eligible ETF universe 或可证明的 source snapshot；近 N 日中位数只能作为 `unavailable` 时的告警参考，不能伪装成精确 code coverage。
- **缺口数量必须出现在三处**：作业日志、结构化 health、面板提示（面板部分归 OPS-3 实现，本卡负责字段）。示例 `{"domain":"concept","trade_date":"2026-08-07","have":282,"expect":351,"missing":69,"expect_source":"provider_eligible_universe"}`；数值仅是当前库+排除集估算，运行时必须由当次 code set 计算。

**执行顺序依赖**：门槛必须**先**提高，08-07 的概念缺口才会进入计划；否则第 5 步补不到它。

### 第 5 步　执行真实补数（HEAVY-BATCH，L-15）

分为三个不可颠倒的 gate：

1. **OPS-1A.1（已完成）**：commit `9805084`，禁止重做。
2. **OPS-1A.2（历史任务，已完成）**：原要求按历史“唯一任务”完成概念分母防缩小；当前禁止重复执行。
3. **OPS-1B（后续生产恢复）**：必须以复核通过的 A.2 commit 为冻结源码，使用新的 online backup 和进程门禁；本次只处理 08-10，不再全量补抓 08-07。

### OPS-1B 当前生产合同（供 A.2 之后的独立执行会话使用）

- Task 0 只读确认：08-10 股票待处理集合仍为“无效已有行 ∪ 当前 eligible 缺席代码”；当前参考值 595+20=615，实际运行时动态计算，禁止硬编码。四指数必须齐全。
- 概念先取一次完整 candidate universe。只有 A.2 的 retention/overlap 门禁通过才可把 263 单调修正到可信集合；门禁失败则该域记 `unavailable` 并停止，不得沿用 263 报完整。
- 股票只抓待处理集合，完整的 4,587 行不得重抓；Tencent 缺 `amount` 的股票行只能被拒绝，禁止造 `amount`。真实 EM/Sina 均失败时保留缺口与闩锁，报告 `environment-blocked`。
- ETF 目标为当次可信 eligible universe；当前参考缺口 0/1,496。source snapshot 不可用时报告 `unavailable`，不得用历史中位数伪造精确分母。
- 写前对 `a_share_mvp.db`、`ths_concept.db`、`etf_mvp.db` 做 SQLite online backup、`quick_check`、表清单/关键行数核对并记录四库 SHA-256；`mining_mvp.db` 全程只读。
- 股票 raw postcondition 通过后才原子 revalidate/清除 08-10 闩锁；任一 child rc 与 parent raw quality 不一致都以失败为准。
- 一次 invocation 总 deadline 3600 秒；同一 provider 同一错误最多 3 次，随后换源或停止。不得降频重试确定性的缺字段错误。
- 交付 `ops-1b-report.md/json`：passed/failed/environment-blocked、before/after have/expect/missing、provider rejection、闩锁 before/raw/action/after、备份路径和四库哈希。OPS-1B 不生成选股批次。

- 写库前对将写入的 a_share/concept/ETF 库做 SQLite online backup；四库记录 SHA-256。OPS-1 不写 mining 库。
- 用脱离会话的后台任务执行，**计算期间模型轮询预算为 0**；结束后由新会话只读读取紧凑证据验收。

## 四、验收命令（目标机 Windows，默认工作目录即仓库根）

> 注意：`.venv` 已损坏，以下一律使用实际在用的解释器。

```powershell
Set-Location -LiteralPath 'D:\BaiduNetdiskDownload\cursor workflow\adata_sentiment_dashboard'
& 'C:\Users\TY_trader1\AppData\Local\Programs\Python\Python311\python.exe' -m unittest
```

质量状态复核（**必须设 `row_factory=sqlite3.Row`，否则 `classify_stock_rows` 会抛 `TypeError: tuple indices must be integers`**）：

```powershell
Set-Location -LiteralPath 'D:\BaiduNetdiskDownload\cursor workflow\adata_sentiment_dashboard'
& 'C:\Users\TY_trader1\AppData\Local\Programs\Python\Python311\python.exe' -c "import sqlite3,json;from mining.data_quality import inspect_stock_session,reinspect_stock_session;c=sqlite3.connect('file:a_share_mvp.db?mode=ro',uri=True);c.row_factory=sqlite3.Row;[(print(d,'latched',json.dumps(inspect_stock_session(c,d),ensure_ascii=False,default=str)),print(d,'raw',json.dumps(reinspect_stock_session(c,d),ensure_ascii=False,default=str))) for d in ('2026-07-13','2026-08-07','2026-08-10')]"
```

闩锁名单（改动前后各跑一次，逐行对比）：

```powershell
Set-Location -LiteralPath 'D:\BaiduNetdiskDownload\cursor workflow\adata_sentiment_dashboard'
& 'C:\Users\TY_trader1\AppData\Local\Programs\Python\Python311\python.exe' -c "import sqlite3;c=sqlite3.connect('file:a_share_mvp.db?mode=ro',uri=True);[print(r[0],r[1],r[2],r[4]) for r in c.execute('SELECT * FROM selection_session_diagnostics ORDER BY trade_date')]"
```

缺口计划（`asof` 是必需的 keyword-only 参数，漏掉会 `TypeError`；本命令实测只读，四库 mtime 不变）：

```powershell
Set-Location -LiteralPath 'D:\BaiduNetdiskDownload\cursor workflow\adata_sentiment_dashboard'
& 'C:\Users\TY_trader1\AppData\Local\Programs\Python\Python311\python.exe' -c "import offline_daily_update as o;d=o.resolve_expected_trade_days('a_share_mvp.db',asof='2026-08-10',days=10);print('expected:',d);p=o.build_missing_update_plan('.',d);print('ok:',p.get('ok'));print('missing_by_domain:',p.get('missing_by_domain'))"
```

**陈旧字面量闸门（L-1）**：本仓库**没有** `scripts/check_stale_literal.py`（全局与项目内均已确认不存在），因此用下面这条等价的可运行扫描代替。它直接搜旧字面量本身，覆盖源码与测试。

```powershell
Set-Location -LiteralPath 'D:\BaiduNetdiskDownload\cursor workflow\adata_sentiment_dashboard'
& 'C:\Users\TY_trader1\AppData\Local\Programs\Python\Python311\python.exe' -c "import re,pathlib;pat=re.compile(r'concept_min_rows\s*[:=][^,)]*100|etf_min_rows\s*[:=][^,)]*100|stock_min_rows\s*[:=][^,)]*2000|^\s*.180.,?\s*\$|timeout_sec\s*=\s*180');skip={'.venv','__pycache__','output','data','V1','old version','.git','mvp_store','csv_out','docs','.idea'};hits=[f'{p.as_posix()}:{i}: {l.strip()[:100]}' for p in pathlib.Path('.').rglob('*') if p.suffix in {'.py','.ps1'} and not any(part in skip or part.startswith('__tmp_') for part in p.parts) for i,l in enumerate(p.read_text(encoding='utf-8',errors='replace').splitlines(),1) if pat.search(l)];print(chr(10).join(hits));print('HITS',len(hits))"
```

**基线（commit `534d546` 实测，HITS=10）**：

```
app_panel.py:4274            "180",                        ← OPS-2 改
offline_daily_update.py:108  stock_min_rows: int = 2000     ← 函数签名默认值
offline_daily_update.py:169  concept_min_rows: int = 100    ← 函数签名默认值
offline_daily_update.py:204  etf_min_rows: int = 100        ← 函数签名默认值
offline_daily_update.py:289-291   三个默认值再抄一遍         ← 转发层复制
offline_daily_update.py:329-331   三个默认值又抄一遍         ← 转发层复制
```

**注意**：同一组门槛在 3 个位置各写了一遍（签名 + 两层转发）。改一处而漏两处，就会出现"函数默认严、调用方仍松"的静默不一致——这正是 L-1 要防的。**验收要求：OPS-1 与 OPS-2 完成后该命令 HITS = 0。**

## 五、绿证据（缺一不可）

1. **`2026-07-13` 继续保持 known_bad**；raw evidence 明确为 `no_valid_trade`，不能仅因 distinct code=5199 清除诊断行。
2. `2026-08-07`：当前已满足 `usable_with_quarantine`，保持可用且不得被 A.2/B 倒退；正式选股批次归 OPS-2。
3. `2026-08-10` 以及**执行时已 close-ready**的后续交易日分别达标（指数 4 个必需代码齐全）；盘前/盘中的当日不会进入计划或 diagnostics。
4. `build_missing_update_plan` 的 `missing_by_domain` 中 stock/index 不再包含所有已完成恢复的 eligible 目标日；未收盘当日从一开始就不应出现在 expected dates。
5. 概念：每个 eligible 目标日按通过 A.2 完整性门禁且不得缩小的 code set 达标；08-07 的 extra excluded codes 不计入 have，且不执行历史 purge。
6. 概念脚本在 list=0 时失败；在目标仍缺且 saved_rows=0 时失败；目标已完整且 saved_rows=0 时幂等成功。三种情况均有测试与结构化日志。
7. `filtered_concept.csv` 能被正确读取，排除集非空（或明确报错中止）。
8. `python -m unittest` 全绿；至少覆盖：原子重判的红→红/红→绿、禁止 clear-before-check、盘前当日不入计划、总 deadline、零列表失败、零新增的失败/幂等成功、eligible code-set 分母、GB18030 解析。
9. online backup 通过表清单/关键行数/`quick_check` 等价检查；四库前后 SHA-256 均已记录，未写入库哈希不变。
10. 诊断表最终只剩确实仍坏的日子；07-13 应保留，08-07 达标后应清除。

## 六、红证据（出现任一条立即停手上报）

- 修复达标后状态仍为 `known_bad_session`（解闩未生效）。
- **`2026-08-07` 在补齐前就被解闩放行**（解闩过头，fail-closed 被削弱）。这与上一条是一对：解闩必须"该绿的绿、该红的红"。
- `2026-07-13` 未修价格内容却转为 clean，或盘前/盘中把当日写入 diagnostics。
- 业务编排直接执行 `DELETE FROM selection_session_diagnostics`，或在 raw 达标前调用 clear API。
- （历史红证据，已 superseded）无授权放宽 0.80/0.95/0.98 曾是红线；当前仅授权按 PROTO-1 把 usable 上限改为 0.90，0.80、coverage 0.95 与逐行 quarantine 仍不得放宽。
- 任一库 `quick_check` 非 ok，或库体积异常缩水。
- 股票 required code 覆盖倒退，或 concept 恢复夹带 `purge_excluded=1` 删除历史数据。
- 第 0 步隔离 harness 实测 parent 退出后后台任务不存活（此时“打开即更新 + 长任务”前提不成立）。

## 七、停止条件

- 同一 provider 同一确定性错误连续 3 次 → 换源；两源都失败 → 停手报原始错误。
- 单日总预算 1 小时耗尽仍未达标 → 记录剩余缺口，停手上报，不自动延长。
- 出现任一红证据。

## 八、不改什么

- 不改策略定义、不改 v2.5 口径、不改 `selection_batches` 结构。
- 不动 `mining/streamlit_tabs/tab_scanner.py`（属 OPS-3）。
- 不动 `app_panel.py`（属 OPS-2/OPS-3）。
- 不删除任何 journal、日志或旧证据文件（含 `ths_concept.db-journal`）。
- 不注册计划任务。

---

# OPS-2　历史设计：触发与闭环层（由 PROTO-1/PROTO-2 取代，暂停）

**前置**：OPS-1 全部绿证据
**当前状态**：未开始；不得在 OPS-1A.2 或 OPS-1B 未复核时提前修改
**完成标签目标**：`real-env-verified`

**写入白名单**：`offline_daily_update.py`、`backfill_orchestrator.py`、`app_panel.py`、`run_daily.py`（只有实际合同缺口才改）、相关现有测试文件、`output/health_latest.md`、`output/health_state.json`、`output/screening-v2-work/ops-2-*`。`daily_job.ps1` 只读。生产写入仅限正式日更路径产生的行情和 v2.5 batch。

## 一、仍存在的问题

1. **当天没有 17:30 后再次打开，导致 08-07 当天从未成为目标；次日补抓又被 180 秒截断。** “打开才更新”允许次日补齐，但必须把延迟和当前目标显式告诉用户。
2. **`--dry-run` 会覆盖 `output/health_latest.md`**——用来"安全检查"的命令本身销毁上一次运行的健康证据，且这条命令就写在 `CLAUDE.md` 的验证命令清单里。
3. 面板触发的超时**硬编码 180 秒**，与 1 小时裁定不符。
4. health 把 writer、覆盖率、正式批次和 reader 压成一个结果；这些状态可以同时发生，不能设计成互斥的“单一四态”。
5. `mining_coverage_for_date` 只看 `candidates>0 or runs>0`，**legacy 老数据能让"选股已完成"为真**，v2.5 正式批次缺失完全不可见。
6. 不同 asof 的作业互为独立 job name，**可能并发**打同一 provider。

## 二、根因

| 问题 | 根因指针 |
|---|---|
| 1 | `app_panel.py:2977-2999` 17:30 前回退本身正确；问题是 08-07 没有收盘后触发、次日触发才补，以及 updater 的显式 asof 没有统一 close-ready clamp。两次后续运行均被 180s wall clock 截断，但底层 provider 性能原因仍未完全定案 |
| 2 | `offline_daily_update.py:1154-1158`：`if dry_run or plan.get("ok"):` 分支在 `return` 前无条件执行 `write_health_summary(...)` 与 `_send_daily_push(...)`。`dry_run` 只跳过了子进程，没跳过产物写入 |
| 3 | `app_panel.py:4274` |
| 4 | `offline_daily_update.py:812-916` `write_health_summary` 用无质量过滤的 `DISTINCT`/`MAX` 日期（因此报 stock max_date=2026-08-07，而真实可用收盘日是 08-06）；`:713-732` 同源 |
| 5 | `offline_daily_update.py:244-281`，无 `definition_version` / `mode` / `batch_id` 过滤 |
| 6 | `backfill_orchestrator.py:49-66`，`active.json` 按 job name 去重，job name 含 asof |

## 三、改法

### 第 1 步　触发时机（用户裁定 2 的核心）

保持"打开才更新"；盘中打开只能推进**已完成交易日**，不得抓取当日 close：

- 面板打开时统一调用 close-ready helper：17:30 后且今天为交易日才把今天纳入目标；此前只补昨日及更早缺口。
- 未到 17:30 打开时，明确告知用户："今日收盘数据将在 17:30 后首次打开面板时抓取"——**这句提示是产品承诺，不是装饰**。
- **接受 1 天延迟**：若用户当天 17:30 后不再打开，次日盘中打开会以"昨日"身份补上。这是"打开才更新"模式的固有代价，必须让用户看得见，而不是静默落后。
- 不引入常驻调度进程。

> **这一段是最需要 GPT 复审的地方**：在"只有人打开才更新"的前提下，任何自动化都无法弥补"人不打开"。卡内的选择是把这个缺口显式化（面板提示 + health 标注），而不是偷偷用别的机制补。若复审认为该缺口不可接受，应回到用户裁定 2 重新讨论，而不是在本卡里私自加调度器。

### 第 2 步　预算与并发

- `app_panel.py:4274` 的 `180` 改为传递 `--timeout-sec 3600` 作为**整轮 deadline**；offline updater 负责把 remaining budget 分配给子进程。改完按 L-1 全仓搜旧字面量。
- 将整个 `offline_daily_update` 设为稳定的全局互斥 job（锁名不含 asof）；同一时刻只允许一个行情更新 invocation。新请求只复用/展示已有 job，不再启动第二个 writer。
- **修 `--dry-run` 的副作用**：`dry_run` 为真时不得写 `output/health_latest.md`、不得推送。要么完全跳过，要么写到 `output/health_dryrun.md`。理由：一条被文档推荐的"安全检查"命令，不应该覆盖唯一的运行证据。
- `daily_job.ps1` 的解释器探测**不需要改**（已实测健壮，见环境拓扑）。若执行者顺手"修"了它，属于超范围改动，按 diff 纯度要求剔除。

### 第 3 步　health 正交状态轴（L-32）

`output/health_state.json` 作为结构化真值，`output/health_latest.md` 只负责渲染。至少分开：

| 轴 | 允许值/证据 |
|---|---|
| `writer_status`（每域） | `not_run / running / succeeded / failed / timed_out / partial_progress / disabled`，带 returncode、before/after rows、started/completed_at |
| `coverage_status`（每域） | `complete / incomplete / unavailable`，带 eligible have/expect/missing、expect_source、max_date、latest_usable_date |
| `selection_status` | `complete / not_run / failed / blocked_by_market_data`，带 batch_id/version/mode/fingerprint |
| `reader_status` | `ok / failed / not_checked`，只有显式调用真实 reader 才能写 ok/failed |

例如“子进程超时但新增596行”必须同时是 `writer_status=partial_progress` 与 `coverage_status=incomplete`，不能被四选一状态吞掉。

同时：

- freshness 表必须**同时**给出 `max_date`（有任意数据的最新日）与 `latest_usable_date`（通过质量门禁的最新日）。**禁止用 `MAX(trade_date)` 单独代表最新可用交易日。**
- `mining_coverage_for_date` 增加 `definition_version='v2.5' AND mode='close_final' AND status='complete'` 判定。
- 输出 `screening_ready`、`dashboard_domains_ready` 和 `overall_status`；concept/ETF 不得阻止已完整 stock/index 生成正式选股批次，但对应面板域必须显示 degraded/incomplete。
- 按 L-32：空表或"0 异常"只有在真实 writer 于同一观察窗口内成功运行时才能判绿；"有意停用"（如 Telegram 未配置）与"意外未运行"必须分开呈现。

### 第 4 步　为已修复日补正式批次

每个已修复且 close-ready 的交易日分别运行 `run_daily.py --base-dir . --range <day> <day>`；这是已有正式路径并具有 batch 原子性/幂等性。`mining/history_bootstrap.py` 只允许在影子库预演，不得用于生产，也不做 60 日历史回填。

## 四、验收命令

```powershell
Set-Location -LiteralPath 'D:\BaiduNetdiskDownload\cursor workflow\adata_sentiment_dashboard'
& 'C:\Users\TY_trader1\AppData\Local\Programs\Python\Python311\python.exe' -m unittest
```

修复后 dry-run 必须对正式 health 完全无副作用；以下命令比较两个正式产物的哈希：

```powershell
Set-Location -LiteralPath 'D:\BaiduNetdiskDownload\cursor workflow\adata_sentiment_dashboard'
$before = Get-FileHash -Algorithm SHA256 -LiteralPath 'output\health_latest.md','output\health_state.json'
& powershell.exe -NoProfile -ExecutionPolicy Bypass -File '.\daily_job.ps1' --dry-run --asof 2026-08-10 --days 1 --timeout-sec 30
if ($LASTEXITCODE -ne 0) { throw "dry-run failed: $LASTEXITCODE" }
$after = Get-FileHash -Algorithm SHA256 -LiteralPath 'output\health_latest.md','output\health_state.json'
if (($before.Hash -join ',') -ne ($after.Hash -join ',')) { throw 'official health artifacts changed during dry-run' }
```

```powershell
Set-Location -LiteralPath 'D:\BaiduNetdiskDownload\cursor workflow\adata_sentiment_dashboard'
& 'C:\Users\TY_trader1\AppData\Local\Programs\Python\Python311\python.exe' -c "import sqlite3;c=sqlite3.connect('file:mining_mvp.db?mode=ro',uri=True);[print(r) for r in c.execute(\"SELECT batch_id,trade_date,definition_version,mode,status,substr(input_fingerprint,1,12) FROM selection_batches ORDER BY trade_date,batch_id\")]"
```

## 五、绿证据

1. 每个已恢复 eligible day 恰有一个可用的 v2.5/close_final/complete 批次；同 fingerprint 复跑只复用，不增长。不同日期 fingerprint 不同。
2. 每个批次六条 run 的 `n_candidates` 与 `candidates` 实际行数逐条相等。
3. 全新进程对每个目标日调用 `latest_complete_batch` 返回相应 complete batch。
4. `health_state.json` 与 Markdown 同源：target_close_date/latest_usable_date 正确，`screening_ready` 与各 dashboard domain 状态可独立解释；不再依赖单一 `MAX(date)` 或单一 update_ok。
5. 在临时副本构造 writer失败无增长、writer超时但有增长、coverage不完整、selection未跑、reader未检查/失败等组合，证明正交轴不会互相覆盖。
6. `--dry-run` 跑完后 `output/health_latest.md` 的 `generated_at` **未被改写**（或产物落到独立的 dry-run 文件）。
7. 在"打开面板"这一唯一触发方式下完成一次真实交易日闭环，并记录实际触发时刻、asof 与耗时。
8. 陈旧字面量扫描（见 OPS-1 验收段）HITS = 0。

## 六、红证据

- 批次 status 非 complete 却被 health 报成成功。
- 出现同日同 fingerprint 的重复批次（幂等复用失效）。
- 旧批次（batch 1 / 2026-08-06）被覆盖或删除。
- `known_bad_session` 被绕过而不是被正当清除。
- health 出现"全绿但 writer 本窗口未运行"（L-32 反例）。
- `reader_status=ok` 却没有同一 run_id 的 reader probe，或 17:30 前把当日列为 close target。
- 使用 shadow-only history bootstrap 直接写生产 mining 库。

## 七、停止条件

- 目标日修复失败且已达 OPS-1 的预算上限。
- 出现任一红证据。
- 第 1 步的触发时机方案若与用户裁定 2 冲突 → 停手回到用户，不私自加调度器。

## 八、不改什么

- 不注册 Windows 计划任务。
- 不改策略定义与 v2.5 口径。
- 不改 `mining/streamlit_tabs/tab_scanner.py`。
- 不手工构造正式批次。
- 不把 CDP / 网络 / provider / 数据规范化 / DB 写入合并成一个状态位。

---

# OPS-3　历史设计：显示层（由 PROTO-1/PROTO-2 取代，暂停）

**前置**：OPS-2 全部绿证据
**当前状态**：未开始；策略模块与生产库冻结只读
**完成标签目标**：`broadly-verified` + 人工开页面一次（L-11）

**写入白名单**：`app_panel.py`、相关 UI/日期测试、`output/screening-v2-work/ops-3-*` 与隔离验收副本。`mining/streamlit_tabs/tab_scanner.py`、四个生产库及策略模块冻结只读。

## 一、仍存在的问题

1. **情绪面板会静默采用残缺交易日**。当前 08-10 已有股票行且四指数齐全，但 raw status 仍为 `partial_missing`；现有 `resolve_available_panel_close_date` 只检查日期集合交集，会把 08-10 当作可用日，页面没有质量告警。
2. **面板与筛选页对"可用收盘日"的定义不一致**：筛选页 fail-closed 正确（`date_status=date_mismatch` → 清空候选），面板完全没有质量门禁。用户会看到"面板有数、筛选页空表"。
3. **缺口不可见**：用户看不到 eligible have/expect/missing；“4206只/70个”只能作为旧快照估算，不能硬编码为当前文案。

## 二、根因

| 问题 | 根因指针 |
|---|---|
| 1 | `app_panel.py:892-899` `_stock_trade_date_set`：当天有**任意一行**即算可用日；`:923-944` `resolve_available_panel_close_date` 仅做股票日期集 ∩ 指数日期集；`:4512-4529` 按原始日期取截面，无质量过滤 |
| 2 | 面板走上面这条路径，筛选页走 `mining/streamlit_tabs/tab_scanner.py:647-695`（`_date_alignment_evidence` + `_apply_date_evidence_gate`），两套定义 |
| 3 | 侧栏仅在**请求日完全缺失**时告警；08-07 存在行，因此静默通过 |

## 三、改法

1. `resolve_available_panel_close_date` 只接受**通过质量门禁**的交易日：以 `mining.data_quality.usable_stock_trade_dates/inspect_stock_session` 为权威，offline updater、面板和筛选页共同消费；**不要在面板里重写一份**。
2. 被拒绝的日期必须在侧栏可见地说明，例如：`2026-08-07 数据不完整（个股 have/expect/missing；概念 have/expect/missing），已回退至 2026-08-06`。数值来自 `health_state.json` 的结构化字段，不得写死，也不得反向从文案解析状态（L-33）。
3. 面板与筛选页的"可用收盘日"统一到同一函数，消除两页不一致。
4. 不改情绪计算公式本身，只改"用哪一天的数据"。

## 四、验收命令

```powershell
Set-Location -LiteralPath 'D:\BaiduNetdiskDownload\cursor workflow\adata_sentiment_dashboard'
& 'C:\Users\TY_trader1\AppData\Local\Programs\Python\Python311\python.exe' -m unittest tests.test_mining_ui tests.test_runtime_paths tests.test_mining_pipeline
```

浏览器验收必须先把 post-OPS-2 四库用 SQLite online backup 复制到隔离目录；以下只读模式会禁用自动修复：

```powershell
Set-Location -LiteralPath 'D:\BaiduNetdiskDownload\cursor workflow\adata_sentiment_dashboard'
$env:SCREENING_BASE_DIR = 'D:\BaiduNetdiskDownload\cursor workflow\adata_sentiment_dashboard\output\screening-v2-work\ops3-ui-sandbox'
$env:SCREENING_ACCEPTANCE_NOW_CN = '2026-08-11T18:00:00+08:00'
& 'C:\Users\TY_trader1\AppData\Local\Programs\Python\Python311\python.exe' -m streamlit run app.py
```

人工验收（L-11，必须真的开一次页面）：核对页面 close date、三日期对齐状态、筛选页条数、侧栏缺口提示。

## 五、绿证据

1. 页面 close date = 通过质量门禁的最新日；筛选页 `date_status=aligned`（selected/effective/formal 三者一致）。
2. 筛选页条数等于该日正式批次六条 run 的 `n_candidates` 之和；合法空结果也必须显示 complete/0，而不是被误报 unavailable。
3. 人为制造一个残缺日时，侧栏出现明确的缺口提示，且面板**不采用**该日。
4. 面板与筛选页显示同一个交易日。
5. 单测全绿 + 人工开页面截图或记录；隔离副本可有预期的 Streamlit cache，四个生产库哈希必须完全不变。

## 六、红证据

- 页面仍显示未通过质量门禁的日期。
- 面板与筛选页显示不同交易日。
- 验收过程中意外触发生产写入（四库 SHA-256 发生非预期变化）。
- 缺口提示靠解析文案而非结构化字段实现。

## 七、停止条件

- 浏览器自动化不可用时直接转人工验收（截图 + 记录）；人工验收完成则不把 CDP 标记成产品 blocker，未完成人工验收才记 `environment-blocked`。
- 出现任一红证据。

## 八、不改什么

- 不改情绪指标计算公式。
- 不改现有盘中 provisional snapshot、盘后 close_final 覆盖和跨日快照失效语义。
- 不改 `offline_daily_update.py`（属 OPS-1/OPS-2）。
- 不改策略定义与 v2.5 口径。
- 不在面板里重写一份质量判定逻辑。

---

## 附：本轮已澄清、不必再查的事项

1. **本次 `rc=999` 的近因已定案**：wrapper 墙钟超时；不要再把 Chrome/CDP 或任意 Python PID作为默认根因。底层 provider 吞吐/限流只在新证据出现时窄查，不能宣称已排除。
2. **08-07 未更新是组合原因**：当天所有触发都早于 17:30，因此当天未成为目标；次日/周一补抓又被 180s 截断。17:30 回退规则本身正确，缺的是收盘后触发可见性、次日补偿和足够的总 deadline。
3. **2026-08-07 那天出现的"反复修复已完整的 08-06 等 8 天"现象属于旧代码版本**，当前代码（08-10 运行）的计划已只列 08-07，不必按现存缺陷追。
4. **close_final 批次原子性、重启读取、v2.5 与 legacy 版本隔离**：已 proven，不必再复审。
5. **`second_launch = 0` 不是故障**，可能是未回补历史的冷启动。
6. **不要删除 `ths_concept.db-journal`**（已加入 `.gitignore`，作为超时证据保留）。
7. **不要重复执行 P1 生产 apply**（同 fingerprint 会复用）。
8. **不要用固定 fixture 的测试通过代替真实运营证据**；不要用 `MAX(trade_date)` 代表最新可用交易日。
9. **`daily_job.ps1` 的解释器探测已实测健壮**（坏 venv 退出 103 被跳过 → `py -3` 退出 0），不要再查、不要顺手改。
10. **本仓库没有 `scripts/check_stale_literal.py`**，全局也没有。不要按 skill 模板照抄那条命令，用 OPS-1 验收段给出的等价扫描。
11. **07-13 不是 clean 日**：raw price content 为 0 valid trades，保持 known_bad；不得再把 distinct count=5199 当成完整性证明。
12. **`mining/history_bootstrap.py` 只写影子库**；生产补批次只走 `run_daily.py` 单日正式路径。
13. **盘前/盘中的当前交易日不是 close 缺口**；验收日志必须记录 `now_cn`、`target_close_date` 和 cutoff decision。

---

## 附：写卡过程中对上一轮复审结论的更正

供 GPT 复审时对照 `REVIEW.md`，以本卡为准：

1. **`daily_job.ps1` 被坏 `.venv` 卡死 —— 该判断不成立，已撤销。** 实测 `.venv\Scripts\python.exe --version` 退出码 103，脚本第 14-28 行的探测循环会跳过它并选中 `py -3`（退出码 0）。坏 `.venv` 是环境卫生问题，不是产品缺陷，已移出 OPS-2 范围。
2. **新增一条上一轮未发现的缺陷**：`offline_daily_update.py:1154-1158` 的 `dry_run` 分支在 `return` 前仍执行 `write_health_summary` 与 `_send_daily_push`，即 `--dry-run` 会覆盖 `output/health_latest.md`。这条命令被 `CLAUDE.md` 列为标准验证命令，等于文档在推荐一个会销毁运行证据的动作。已作为 OPS-2 问题 2 收入。
3. **Opus 对 07-13 的“完整但被误杀”判断已被推翻。** 绕过闩锁 raw reclass 后仍为 `no_valid_trade`；5199 只是代码数量，价格内容是 5145 个 placeholder + 54 个 invalid。闩锁缺少重判路径仍是真缺陷，但验收对照必须改为“07-13 保持红、08-07 补齐后转绿”。
4. **`selection_session_diagnostics` 位于 `a_share_mvp.db`，不在 `mining_mvp.db`**。查表和备份时别找错库。
5. **Opus 把早盘 08-11 零行列为缺口不成立。** close-ready helper 必须先排除未收盘当日，避免未来自动写入错误闩锁。
6. **Opus 建议用 history bootstrap 实跑生产批次不成立。** 该模块代码明确为 shadow-only；已改为 `run_daily.py --range day day`。
7. **Opus 的单一 health 四态会丢信息。** 超时与部分增长、覆盖不完整可以同时发生，已改为 writer/coverage/selection/reader 四条正交状态轴。

## 2026-08-12 OPS-HARDEN-1A.3 provider circuit-breaker and low-frequency repair

- Status: `ready_for_ops_harden_1b_1_review`. This card completed source, tests, SQLite online-backup copies, and one bounded BaoStock read-only sample only; it did not run a production repair, formal batch, browser, or OPS-HARDEN-1B.
- Closure: stock repair is provider-stage and dynamic-gap based: BaoStock, then Sina, then Eastmoney only after its same-run probe succeeds. Complete existing rows are skipped. A timeout or error threshold opens that provider's circuit and transfers the remaining codes to the next source without the former daemon-thread semaphore saturation.
- Defaults: one worker and a 0.45-second request-start interval. The parent starts one repair child; up to three distinct provider passes live inside that child. `PROVIDER_SUMMARY` is compact and `remaining_budget_seconds` is actual remaining budget.
- Evidence: 5,202-code circuit regression, BaoStock complete-field/login-logout regression, four-db online-backup table/row parity, controlled 08-11 sandbox skip/reject/resume evidence, and one 20-code low-frequency BaoStock probe at 20/20 complete. Full test result: 352 passed, 2 skipped; all four production SHA-256 values match Task 0.
- Next boundary: production activation requires a newly authorized, separately reviewed `OPS-HARDEN-1B.1` task. It must not begin from this card or this session.

## 2026-08-12 OPS-HARDEN-1A.4 single-code timeout isolation

- Status: `ready_for_ops_harden_1b2_review`; source/tests/temporary SQLite evidence only. No provider call, production database write, offline update, formal batch, or Streamlit action ran.
- A provider worker now reports its active code. On a one-code timeout, the parent flushes accepted rows, terminates that worker, leaves only the timed-out code unresolved for fallback, and starts a new worker for later non-terminal codes. Complete, empty, invalid, guard-rejected, and previously checkpointed codes are not re-fetched by that provider.
- Timeout is a consecutive provider-error signal. A successful complete row resets it; only the third consecutive active-code timeout opens `timeout_threshold`. Summaries now expose `timeout_codes`, `worker_restarts`, `codes_in`, `unresolved`, and the maximum consecutive error count.
- Evidence: red Windows-spawn regression failed before the fix because a third-code hang ended the source after two valid rows; green regressions prove 1/2/4/5 continue, fallback sees only 3, separated timeouts do not trip a circuit, three consecutive timeouts preserve the exact tail, and 400 SQLite checkpoint rows survive a restart before `repair_day()` reopens only the true remaining code.
- Validation: P0/P1 five tests passed; `tests.test_backfill_rules` 76 passed; full suite 361 passed, 2 skipped; `py_compile repair_market_day_akshare.py` and `git diff --check` passed. Current four production SHA-256 values are unchanged during this card.
- Bound: a failing provider has at most three consecutive 12-second active-call waits (36 seconds of request timeout budget, plus finite worker terminate/spawn overhead) before fallback. It cannot spin indefinitely.

## 2026-08-12 OPS-HARDEN-1A.3 independent-review correction (P0/P1)

- Status remains `ready_for_ops_harden_1b_1_review`; this correction stops before any production action.
- P0 fixed: a successful Eastmoney one-code probe now removes only the actually persisted probe code. The subsequent Eastmoney full pass receives every remaining unresolved code; a failed probe preserves the complete dynamic gap. The regression proves exact five-code conservation across BaoStock/Sina circuits, probe, and full pass.
- P1 fixed: `repair_day()` now owns bounded SQLite checkpoints (default 200 accepted rows). Provider children return rows only; after every checkpoint, pass end, or circuit end the next source sees the true unresolved set. Guard-rejected rows are retained for later sources or a later invocation.
- Each provider pass/circuit now emits `PROVIDER_SUMMARY` immediately. Circuit accounting is explicitly consecutive provider-call errors: a complete row resets it, while empty and invalid rows are tracked separately and do not accumulate the error threshold.
- New forced-interruption regression checkpoints 400 rows of a 450-code pass before interruption, then proves the resumed invocation skips those 400 and requests only the remaining 50 codes.
- Correction validation: `tests.test_backfill_rules` 72 passed; five P0/P1-focused tests 5 passed; full `python -m unittest` 357 passed, 2 skipped; `py_compile` and `git diff --check` passed. Production SHA-256 values remained unchanged.
