# 2026-07-19 面板可见性任务卡（Q → P 串行）

背景：M.1/M.2/N/O 与 EXIT 知识卡全部完成后，重启面板看不到任何变化。第一分歧点审计结论：**代码大多没有坏，而是所有"新变化"的渲染面当前全部为空或根本没接进面板**。本文件含两张卡，串行执行 Q → P，一卡一 commit，P 以 Q 的 commit 为 parent。

审计证据快照（2026-07-19 实测，mining_mvp.db）：

- `strategy_runs` 最新：launch_burst 于 2026-07-17 已跑（universe=5175, n_candidates=0, status='empty'）；历史上仅 2026-06-18 出过 4 条候选。→ 主升启动区显示"当天没有主升启动信号"是**真实空信号**，不是没接线。
- 2026-07-17 watchlist 快照：回踩中 151 / 延伸中 17 / 破位失效 465。**回踩到位最后出现 2026-07-10，再启动最后出现 2026-07-09**。→ 面板仅有的两个 playbook 渲染列（回踩到位·预备 / 再启动·触发）连续 9 天为空，知识卡因此一张都不显示。
- `compute_market_regime` / `get_forward_summary`（mining/reports.py:114, :224）的消费者只有 markdown 报告（reports.py:244, :324-325），面板零消费者。→ M.2 的市场灯在面板上不可见是**接线缺口**。
- 盘中扫描（卡 N/O）按设计只有 CLI，无面板 UI。→ 非缺陷，预期内。

共同禁改清单（另一工作流的未提交改动，两卡都不得触碰、不得混入 commit）：
`run_daily.py, offline_daily_update.py, runtime_paths.py, mining/backtest.py, daily_job.ps1, mining/notify.py, tests/test_notify.py, tests/test_backfill_rules.py, tests/test_mining_pipeline.py, .gitignore, AGENTS.md, CLAUDE.md, backfill_etf_equity_60d_v2.py, repair_market_day_akshare.py`

---

## 卡 Q：watchlist 状态常量化 + 知识卡挂钩健全

### 真值卡

- **业务定义**：watchlist 状态机的合法状态集是且仅是 mining/watchlist.py 状态机实际返回的五个字面量：`回踩中 / 破位失效 / 再启动 / 回踩到位 / 延伸中`。知识卡 frontmatter 的 `maps_to` 键只允许三类值：合法状态、已注册扫描器 strategy_id、显式豁免名单里的保留键。`knowledge/cards/` 下所有子目录的卡都应被加载（当前 indicators/ 目录永远不被读取）。
- **恒等式/硬约束**：`load_playbooks()` 索引键集合 ⊆ (WATCHLIST_STATES ∪ 注册扫描器 id ∪ 豁免名单)；状态字面量在仓库内只允许有一个定义点（watchlist.py 的常量），其余位置引用常量。
- **证据源**：
  - 状态字面量散落：mining/watchlist.py:190-208（状态机 return）、:530-531（`split_actionable_watchlist` 过滤）。
  - 目录缺口：mining/playbook.py:93-97 `_card_paths` 在 setups/ 存在时**只**读 `setups/*.md`，indicators/ 等子目录永不加载（合同无消费者）。
  - 死键实证：EXIT 卡曾写 `延伸`/`破位`（文档旧名）而 DB 实际是 `延伸中`/`破位失效`，已由主会话修正（本卡不需再改这两个文件）；索引中另有 `反包警示`、`relay_new_king` 两个疑似保留键，需按下述规则裁决。
- **未知项**：`反包警示`、`relay_new_king` 是否该进豁免名单——执行时用 `load_builtin_scanners()` 实际返回的 id 判定：不在其中且不是状态的键，若对应卡是"为将来保留的知识"（如连板族）则进豁免名单并附一行理由注释，否则修卡 frontmatter。标 `未实证` 直到测试落地。
- **完成闸门**：`python -m unittest tests.test_playbook_keys tests.test_mining_ui tests.test_mining_ui2 2>$null` 中实际存在的模块全部通过（至少 `tests.test_playbook_keys tests.test_mining_ui`）；最低标签 `locally-verified`。
- **Lessons 投递**：
  - L-1 [checklist] 改共享定义/常量必须全仓搜旧字面量本身；注释、测试断言、**知识卡 maps_to 键**都是消费者。sink → `scripts/check_stale_literal.py`。本卡本质就是给该教训建机器 sink。
  - L-2 [checklist] 台账条目已抄入卡文本（即本节）。

### 问题

1. 五个状态字面量在 watchlist.py 内至少两处硬编码，知识卡、面板列又各自持有副本，已实际发生一次漂移（卡里写 `延伸`/`破位`，索引成死键，卡永不显示）。
2. `_card_paths` 只读 setups/，indicators/ 下的卡是死文件。
3. 没有任何机器检查保证 maps_to 键有真实消费者。

### 根因

- mining/playbook.py:93-97（setups-only glob）。
- mining/watchlist.py:190-208、:530-531（字面量无单一定义点）。

### 改法

1. mining/watchlist.py 顶部新增模块常量（示例名）：`STATE_PULLBACK="回踩中"`, `STATE_BROKEN="破位失效"`, `STATE_RETRIGGER="再启动"`, `STATE_READY="回踩到位"`, `STATE_EXTEND="延伸中"`，以及 `WATCHLIST_STATES = frozenset({...五个...})`；状态机 return 与 `split_actionable_watchlist` 改用常量。**字符串值一个都不变**（快照 DB 里存的就是这些值，改值=毁历史数据）。
2. mining/playbook.py `_card_paths` 改为递归加载 `cards_dir` 下所有 `*.md`（`sorted(cards_dir.glob("**/*.md"))`），保留坏卡计入 `errors` 的现有容错行为。
3. 新增 `tests/test_playbook_keys.py`：加载真实 `knowledge/cards/`，断言 ①`errors == 0`；②每个索引键 ∈ `WATCHLIST_STATES` ∪ `{s.strategy_id for s in load_builtin_scanners()}` ∪ 豁免名单（豁免名单写在测试文件顶部，每项带一行理由注释）；③indicators/ 目录存在卡时索引里能查到其键（防目录缺口回归）。
4. 边界：不改 `split_actionable_watchlist` 的筛选语义（仍只取 回踩到位/再启动）；不改任何状态机阈值；不改 knowledge/cards 下 setups 两张 EXIT 卡（主会话已修）。

### 文件所有权（互斥）

`mining/watchlist.py`, `mining/playbook.py`, `tests/test_playbook_keys.py`（新建）。

### 验收命令

```powershell
python -m unittest tests.test_playbook_keys tests.test_mining_ui
python scripts/check_stale_literal.py --old "回踩到位" --exclude docs/superpowers/specs/2026-07-19-panel-visibility-task-cards.md
```

说明：check_stale_literal 此处用于确认常量化后残留的裸字面量都是"允许保留"的位置（DB 数据、测试 fixture、UI 展示文案）；发现源代码逻辑位仍用裸字面量则改为引用常量。exit 0 或逐条给出保留理由后才可标 `locally-verified`。

---

## 卡 P：面板可见性接线（市场灯 + 运行状态 + 空态可读 + 延伸中持有面）

前置：以卡 Q 的 commit 为 parent，状态字面量一律 `from mining.watchlist import ...` 引用常量，不得新写裸字面量。

### 真值卡

- **业务定义**：扫描器页需回答用户三个问题：①今天市场什么状态（M.2 灯，仅展示不闸门）；②扫描器今天到底跑没跑、结果是空还是失败；③两个行动列为空时，空得是否合理（最近一次出现是哪天）。另为"延伸中"（持有管理场景）补一个带知识卡的渲染面——EXIT 卖点卡的主战场是持有中的票，当前它们没有任何显示位置。
- **恒等式/硬约束**：市场灯只展示、不过滤不排序任何候选（RED 日出过凯莱英的反证在案，硬闸被否）；新增区块全部只读 DB，不写任何表；面板在 mining DB 缺表/空表时必须照常渲染（fail-open 到"无数据提示"，不是崩溃）。
- **证据源**：
  - 接线缺口：`compute_market_regime`（mining/reports.py:114）与 `get_forward_summary`（:224）消费者仅 reports.py:244、:324-325；tab_scanner.py 零引用。
  - 空态不可读：tab_scanner.py:990 `st.info("当天没有主升启动信号。")` 无法区分"没跑"与"跑了为空"；实测 launch_burst 07-17 已跑 status='empty'。
  - 渲染面缺口：playbook expanders 只挂在 launch 区与 ready/trigger 两列（tab_scanner.py:1010-1027），:1029 全量沉淀名单 expander 无 playbook；回踩到位/再启动分别自 07-10/07-09 起为空 → 知识卡整体不可见。延伸中当日 17 行，无任何专属渲染面。
  - lookup 键机制：tab_scanner.py:834 `_playbook_lookup_keys`（state / strategy_id / flag_strategies）。
- **未知项**：`strategy_runs` 表精确列名以 PRAGMA 为准（实测含 strategy_id / universe / n_candidates / status，另有运行时间列）；`get_forward_summary` 本卡不接（数据量少、面板表意不清，保持报告专属）——标 `未实证：面板是否需要前向摘要，由用户用过后再定`。
- **完成闸门**：`python -m unittest tests.test_mining_ui` 通过 + **L-A1 人工开一次页面**（`streamlit run app.py`，扫描器页四个新区块渲染无异常）。最低标签 `locally-verified`，页面人工确认后报 `real-env-verified(UI)`。
- **Lessons 投递**：
  - L-A1 [checklist] UI 改动交付前人工开一次页面。sink → Task Routing SHAPE 行。
  - L-1 [checklist] 状态字面量引用卡 Q 常量，不新增裸字面量副本。
  - L-2 [checklist] 已抄入（本节）。

### 问题

用户重启面板看不到 M.1/M.2/EXIT 卡的任何效果：M.2 没接线；M.1 只在 launch_burst 出信号日可见（自 06-18 起 0 信号）且空态无解释；知识卡的全部渲染面（launch 行、回踩到位列、再启动列）连续 9 天为空。

### 根因

- mining/reports.py:114/:224 无面板消费者（接线缺口）。
- tab_scanner.py:990 空态文案无运行状态（可观测性缺口）。
- tab_scanner.py:1010-1029 playbook 渲染面与当前实际存在的状态（延伸中 17 行）错配（渲染面缺口）。

### 改法

四个子项，全部在 `render_scanner_tab` 内：

1. **P.1 市场灯行**：页首调用 `compute_market_regime(conn, trade_date)`（复用 mining/reports.py:114，勿复制公式），展示 灯色徽记（🔴/🟡/🟢）+ 上涨家数占比 + 一行阈值说明（<40% RED，>60% GREEN）。conn 复用现有 mining DB 连接方式（需 attach ash）。计算失败时显示"市场灯不可用"并继续渲染其余区块。
2. **P.2 扫描器运行状态**：当日 `strategy_runs` 中每个 strategy_id 的最新一行（status / n_candidates / universe / 运行时间）渲染为 caption 级小表。当日无任何运行记录时显示"今日扫描未运行"。
3. **P.3 空态最近出现日**：ready/trigger 两列为空时，各自附一行 `最近一次出现：YYYY-MM-DD`（`SELECT MAX(snapshot_date) FROM watchlist_snapshots WHERE state=?`，状态用卡 Q 常量）。从无出现过则显示"历史上未出现过"。
4. **P.4 延伸中持有管理面**：在两行动列之后、全量沉淀名单 expander 之前，新增"持有管理·延伸中"区块：取当日快照 `state == STATE_EXTEND` 的行（沿用现有排序，top_n=30），复用现有表格渲染 + `_render_watchlist_playbooks`。空时一行提示即可。**只做延伸中**：回踩中(151 行)噪声大不做，破位失效(465 行)无行动价值不做。

测试（tests/test_mining_ui.py 追加）：合成 DB fixture 下 ①regime 行渲染/降级不抛异常；②strategy_runs 有/无记录两种路径；③延伸中行出现在新区块且 playbook 键按 state 命中；④ready/trigger 空态提示含最近日期。

### 边界与不改什么

- 不把市场灯做成过滤/排序/硬闸门；不改 launch_burst 及任何扫描器条件；不改 Phase L 强度闸门；不改 `split_actionable_watchlist` 语义；不接 `get_forward_summary`；不动盘中扫描（CLI 专属）；不碰共同禁改清单。

### 文件所有权（互斥）

`mining/streamlit_tabs/tab_scanner.py`, `tests/test_mining_ui.py`。

### 验收命令

```powershell
python -m unittest tests.test_mining_ui tests.test_playbook_keys
streamlit run app.py   # L-A1：人工打开扫描器页确认四区块
```

---

## 明确不开卡的两项（决策留给用户）

1. **回踩到位/再启动 9 天为空的根因**：状态机可产出这两个状态（07-10/07-09 还有），当前为空 = Phase L 强度闸门收紧（second_launch universe 908→129）×近期弱市（破位失效占 73%）的叠加。这是**策略阈值问题不是代码缺陷**，且 L-5 在案（Phase L 形态曾被用户看图否决）。要不要放宽闸门，等卡 P 落地、面板能看到"最近出现日"后由用户拍板；需要时可先跑一个只读诊断（列出无闸门时会入选的回踩中标的）供看图。
2. **前向摘要 (`get_forward_summary`) 进面板**：样本尚少，先留在 markdown 报告。
