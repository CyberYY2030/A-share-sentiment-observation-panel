# 规格：复盘笔记决策助手 —— 让每只候选票挂上「模式 + 案例 + 原文」（Phase K）

状态：✅ 已完成（Codex 实现，Claude 验收：85 tests OK、6 卡四段化 + maps_to、案例来源 D 号全部真实对题、面板无技术词）
作者：规划 by Claude
前置：Phase I（漏斗双清单）已完成。本轮不改漏斗算法，只在它上面加一层"复盘笔记提示"。

---

## 0. 一句话：这玩意儿每天帮你做什么

> **现在**：漏斗给你二三十只候选票，旁边只有代码、状态、缩量比这些冷数字。你还得自己回想"这种票我的复盘笔记里是怎么讲的"。
>
> **改完后**：点开任一只候选票，它直接告诉你三件事——
>
> 1. **【模式·怎么做】** 这是复盘笔记里的哪一类（如"二次启动低吸""强趋势中军"），这类票**在哪买、什么算确认启动、什么情况要放弃**。——这是把笔记里的**规则演绎**到今天这只票上。
> 2. **【案例·过往经验】** 复盘笔记里**同类情形的过往例子**和当时的结论（如"回踩10日线缩量止跌后放量站回 → 二次启动成立；跌破前低 → 放弃"）。——这是把笔记里的**案例归纳**成经验，遇到同类情形提醒你。
> 3. **【原文】** 一键点开来源笔记，随时回去复盘。

**效果**：你那 409 万字复盘笔记，从"躺在文件夹里"变成"**每天站在你旁边帮你认票的助手**"。遇到同类问题，它替你想起你自己积累过的经验和结构，而不用你硬记是哪篇。

**还有个复利**：以后每多整理一篇笔记成卡片，这个助手就更厚一点——给你继续整理笔记一个**马上能用上**的理由。

---

## 1. 为什么是这个方向（对着你的目标）

- 你的目标：每天收敛出短名单，且名单要"说复盘笔记的话"；买卖点你来定。
- 已验证的硬事实：① 复盘笔记是**方法论 + 案例经验**，不是能跑回测的数据；② 机械打分排不出超额，**人是 alpha，系统是漏斗**。
- 所以笔记最值钱的用法 = **在你判断的那一刻给你规则(演绎)和先例(归纳)**，而不是再做扫描器或回测。这一层正好落在"漏斗已选出候选"之后、"你按键下单"之前，是你每天真正用脑的环节。

---

## 2. 要搭的结构（这才是真正的活）

把"模式"和"案例"整理成**按情形可查的结构**，三块拼起来：

**(A) 模式卡（演绎规则）——已有 7 张，本轮补全为统一四段式**
每张 `knowledge/cards/setups/*.md` 卡片，正文统一成四段，便于机器读、便于你看：
- `模式定义`：一句话这是什么形态。
- `看法`：分 **买点 / 确认信号 / 放弃信号** 三行（演绎规则，取自笔记）。
- `案例`：2–3 条**归纳要点**，每条形如"〔情形〕→〔后续〕→〔结论〕"，并标注来源笔记 ID（如 `D0034`）。这就是把笔记里的案例沉成经验。
- `原文`：来源笔记 ID 列表（可点开 `knowledge/_raw/` 对应文）。

**(B) 模式 ↔ 漏斗的挂钩（让系统知道哪只票配哪张卡）**
卡片 frontmatter 增 `maps_to:` 字段，声明这张卡服务哪些**漏斗状态/扫描器**，例如：
- 二次启动低吸卡 → `maps_to: [回踩到位, 再启动, second_launch]`
- 强趋势中军卡 → `maps_to: [true_leader]`
- 趋势胚子卡 → `maps_to: [trend_embryo]`

**(C) 查询入口（候选票 → 它的模式+案例）**
一个小模块按候选票的状态/来源扫描器，回查命中的卡片，返回 A 的四段内容。**卡片是唯一真相源**，代码不复制文本、只解析卡片。

> 这套结构本身就是你要的"归纳和演绎的经验与结构"：模式是演绎骨架，案例是归纳血肉，挂钩让它在对的时机自动找上对的票。

---

## 3. 每天怎么用（面板表现）

在 Phase I 的双清单里，每只候选票可展开（`st.expander`），展开后渲染该票命中的卡片四段：模式定义 / 看法(买·确认·放弃) / 案例(带原文ID) / 原文链接。
- 命中多张卡 → 都列出。
- 没命中卡（如"延伸中""破位失效"暂无对应卡）→ 给一行温和提示"该状态暂无复盘笔记模式"，不报错。
- 全程大白话，无任何"edge/分位/基线"等技术词。

---

## 4. 诚实边界（防马后炮）

- 这里的"案例"是从复盘笔记**归纳出的经验提示，不是回测信号**，不声称胜率/超额。面板只做"提醒你笔记怎么说"，决策仍是你的。
- 不改漏斗算法、扫描器、默认参数。纯加"笔记提示"层。
- 案例若来自无本地行情的历史时段，只作经验展示，不参与任何收益统计。

---

## 5. 给 Codex 的实现细节（与上面分开，按需读）

**K1 卡片四段式 + 挂钩字段**
- 统一 `knowledge/cards/setups/*.md` 正文为四段（模式定义/看法/案例/原文），frontmatter 增 `maps_to: [...]`。
- 先以 1 张卡（`SET-long-leg-dip` 二次启动低吸）为模板做满，其余卡补齐结构（案例内容由 Claude 后续补，Codex 先保证结构与解析可用，缺案例段时优雅留空）。

**K2 查询模块 `mining/playbook.py`**
- `load_playbooks(cards_dir="knowledge/cards") -> dict`：解析所有卡片 frontmatter(`maps_to`) + 四段正文，构建 `状态/扫描器 -> [卡片内容]` 索引。
- `lookup_playbook(state_or_scanner: str) -> list[dict]`：返回命中卡片的四段结构化内容（缺段返回空、不抛错）。
- 卡片为唯一真相源；解析失败的卡跳过并计数，不阻断。

**K3 面板接入 `mining/streamlit_tabs/tab_scanner.py`**
- 双清单每行加 `st.expander`，调 `lookup_playbook(该行 state 及 flag_strategies)` 渲染四段；多卡合并、空命中给提示。
- 复用现有渲染风格，不引入技术词。

**测试**
- `load_playbooks`：`maps_to` 索引正确；四段解析正确；缺段/坏卡安全跳过。
- `lookup_playbook`：按状态/扫描器命中正确；无命中返回空列表。
- 面板：展开渲染四段；空命中给提示不报错；现有 83 测试保持全绿。

**验证命令**
```powershell
python -m unittest tests.test_mining_features tests.test_mining_pipeline tests.test_mining_ui
python -m py_compile mining/playbook.py mining/streamlit_tabs/tab_scanner.py
python -c "from mining.playbook import lookup_playbook; print([c['模式定义'] for c in lookup_playbook('回踩到位')])"
```

**约束**
- 不改漏斗/扫描器算法与默认参数；卡片是唯一文本源，代码不复制。
- 编辑前 `git status --short`，勿回退无关改动；不提交 `knowledge/_raw/`。

---

## 6. Phase J（前向验证）往后放
J 是"验证漏斗准不准"的后台体检，不改变你每天看盘体验。等本层让系统每天真正帮到你，再回头用前向数据验证它靠不靠谱。**先解渴，后体检。**

---

## 7. 实现记录（Codex 填写）

完成时间：2026-06-28

### K1 卡片四段化与 maps_to
- 当前仓库实际存在 6 张 `knowledge/cards/setups/*.md` setup 卡，已全部统一为四段：`模式定义` / `看法` / `案例` / `原文`。规格中提到“7 张”，本次按实际文件库存全覆盖，没有新增占位卡。
- `SET-long-leg-dip.md` 作为完整模板，映射：`[回踩到位, 再启动, second_launch]`。
- `SET-trend-strong-leg.md` 映射：`[true_leader, trend_embryo]`。
- `SET-launch-breakthrough.md` 映射：`[momentum_breakout, trend_embryo]`。
- `SET-sector-resonance.md` 映射：`[rps_concept_top20, rps_stock_top20, true_leader]`。
- `SET-relay-new-king.md` 映射：`[momentum_breakout, relay_new_king]`。
- `SET-fanbao-low-odds.md` 映射：`[反包警示, momentum_breakout]`。
- 未修改 `knowledge/_raw/`。

### K2 playbook 模块
- 新增 `mining/playbook.py`。
- 暴露 `load_playbooks(cards_dir="knowledge/cards") -> dict`：解析 setup 卡 frontmatter 的 `maps_to` 与四段正文，返回 `cards`、`index`、`errors`。
- 暴露 `lookup_playbook(state_or_scanner: str, cards_dir="knowledge/cards") -> list[dict]`：按状态或扫描器 ID 返回命中的结构化卡片；无命中返回 `[]`；坏卡跳过并计入 `errors`，不阻断面板。
- 卡片为唯一文本源，代码只解析和索引，不复制卡片正文作为决策内容。

### K3 挖掘面板接入
- `mining/streamlit_tabs/tab_scanner.py` 接入 `lookup_playbook`。
- Phase I 双清单中每个候选保留原表格展示，并新增逐行 `st.expander` 复盘笔记展开。
- 每行按 `state` 与 `flag_strategies` 合并查卡，去重后渲染 `模式定义` / `看法` / `案例` / `原文`。
- 空命中显示：`该状态暂无复盘笔记模式`。
- UI 新增文案未使用 `edge`、`分位`、`基线`。

### 测试与冒烟
- `python -m py_compile mining/playbook.py mining/streamlit_tabs/tab_scanner.py`：通过。
- `python -m unittest tests.test_mining_features tests.test_mining_pipeline tests.test_mining_ui`：75 tests OK。
- `python -m unittest tests.test_mining_features tests.test_mining_pipeline tests.test_mining_ui tests.test_mining_evaluate`：85 tests OK。
- lookup 冒烟：`lookup_playbook('回踩到位')` 命中 1 张卡，返回 `SET-long-leg-dip` 的 `模式定义`。