# 知识卡片 Schema 规范

每张卡片 = 一个**可复用的交易逻辑单元**，由两部分组成：

1. **YAML front matter**（机器可读的量化规格）——能被脚本读取、转写成扫描器/指标/风控规则。
2. **Markdown 正文**（人读的原理与细节）——保留作者原意、案例、边界条件，避免量化时丢失语境。

设计原则：**能数据量化的进 `quant_spec`，只能逻辑量化的进正文并标 `data_quantifiable: false`**。宁可如实标注"需人工判断"，也不要把模糊经验硬编成假精确的阈值。

---

## 字段定义

```yaml
---
id: IND-adv-count-emotion-bottom      # 稳定唯一 id：<TYPE前缀>-<英文短名>
type: indicator                       # 卡片类型，见下表
title: 涨跌家数判断情绪冰点              # 中文标题
themes: [情绪周期, 实用指标]            # 所属高价值切片主题（可多）
source_docs: [D0071]                  # 溯源：corpus_index.csv 里的 doc_id（可多）
data_quantifiable: true               # 能否直接写成代码（数据可量化）
maturity: high                        # 作者验证成熟度：high/medium/low
maps_to:                              # 对接现有系统的位置
  module: app_panel                   # app_panel / mining_scanner / 新增模块
  target: 市场情绪 market_light
  status: 需新增                       # 已具备 / 部分已有 / 需新增 / 需补数据
quant_spec:                           # 机器可读规格（仅 data_quantifiable 时填实）
  inputs:                             # 计算所需输入
    - name: adv_count
      desc: 全市场上涨家数（通达信 880005）
  signals:                            # 触发条件 -> 含义 -> 动作
    - name: 极致冰点
      when: "adv_count <= 300"
      implies: "次日反弹概率≈100%"
      action: 低吸
    - name: 情绪高潮顶
      when: "adv_count >= 3500"
      implies: "次日大概率分歧"
      action: 减仓
  constraints:                        # 关键约束 / 失效条件（最易被量化漏掉的部分）
    - "必须由弱转更弱(800→300)才有效；由强转弱(2000→700)无效甚至是坑"
---
```

### type 取值与 id 前缀

| type | 前缀 | 含义 | 典型对接 |
|------|------|------|----------|
| `indicator` | `IND-` | 可计算的市场/个股指标 | dashboard 情绪指标 |
| `setup` | `SET-` | 交易模式/买卖点（入场—确认—止损—卖出） | mining 扫描器 |
| `cycle_stage` | `CYC-` | 情绪周期阶段定义与应对 | 情绪分层标注 |
| `risk_rule` | `RSK-` | 仓位/风报比/账户管理规则 | 仓位风控模块 |
| `discipline` | `DIS-` | 纪律/心法/常见错误 | 执行 checklist |

### setup 类的 `quant_spec` 额外结构

```yaml
quant_spec:
  applicable_stage: [启动, 分歧转折]      # 适用情绪阶段
  universe: 主线题材内人气股             # 选股范围/标的类型
  entry:                               # 入场条件（尽量量化）
    - "低开后小幅杀跌≤3%即企稳，盘口承接强"
  confirm:                             # 确认信号
    - "跌停瞬间无封单；二次探低不破前低"
  stop: "买入逻辑（题材/承接）消失即止损"
  exit: "次日冲高 / 弱转强失败"
  risk_reward: "一月不开张，开张吃一月（低胜率高赔率）"
  invalidation:                        # 失效条件
    - "由强转弱的转折无效"
```

---

## 正文结构（Markdown body）

固定小标题，便于人读与后续二次加工：

- **一句话**：这张卡解决什么问题。
- **原理**：为什么成立（市场行为逻辑）。
- **用法**：具体怎么用，含阈值/步骤。
- **边界与失效**：什么时候不要用、容易踩的坑。
- **案例**：作者给的实例（保留日期/标的，便于回溯原文与截图）。
- **量化落地**：如何接入本仓库系统，缺什么数据。

---

## 与系统的对接约定

- `maps_to.module` 必须是仓库里真实存在的模块或明确标注"新增"。
- `quant_spec.inputs` 用到的数据若现有 DB/指标不具备，`maps_to.status` 标 `需补数据`，并在 `backlog.md` 立项。
- 卡片只写**你自己综合后的逻辑**；原始课程全文留在 `_raw/`（不进 git），通过 `source_docs` 回溯。
