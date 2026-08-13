# 交易知识库（trading knowledge base）

把个人交易学习资料（复盘哥/「看懂龙头股」体系，约 1080 个文件）蒸馏成**可复用、可对接量化系统**的知识卡片。服务于本仓库的情绪 dashboard 与 mining 扫描器。

## 目录结构

```
knowledge/
├── README.md          本说明
├── SCHEMA.md          知识卡片的 YAML+Markdown 规范（先读）
├── pattern_map.md     模式谱系总图（策略/模式族 -> 落地性 -> 扫描器）
├── index.md           卡片导航（按主题/类型）
├── backlog.md         量化落地清单（卡片 -> 系统模块）
├── corpus_index.csv   语料溯源总账（doc_id / 标题 / 来源路径 / 去重 / 主题标签）
├── cards/             知识卡片（你自己综合的逻辑，进 git）
│   ├── indicators/    可计算指标
│   ├── setups/        交易模式/买卖点
│   ├── cycles/        情绪周期阶段
│   ├── risk/          仓位风控
│   └── discipline/    纪律心法
├── tools/
│   └── extract_corpus.py   抽取+去重管线
└── _raw/              抽取出的课程全文（版权原文，.gitignore 不进库）
```

## 语料概况（已盘点）

源目录：`D:\BaiduNetdiskDownload\personal\交易有关`（1080 文件 / 588MB）。

- 课程 PDF 384 + 文档 DOCX 235 = **619 篇可抽文字**（已验证非扫描，无需 OCR）。
- 去重后 **525 篇独立**（93 篇重复、1 篇旧 .doc 抽取失败）。
- 截图 423 张（K线案例，文件名自带标题日期）= 案例佐证，暂只编目不解析。
- 两本外文/论文（Trading & Exchanges、T+1）= 理论背景，低优先。

高价值切片（首轮聚焦，与现有系统直接对接）：情绪周期 / 实用指标 / 启动标准 / 强势股模式 / 仓位风控。

## 工作流

1. **抽取**：`python knowledge/tools/extract_corpus.py` → 重建 `_raw/` 与 `corpus_index.csv`（幂等，可重跑）。依赖 `pymupdf`（PDF），DOCX 用标准库。
2. **蒸馏**：按 `SCHEMA.md` 把 `_raw/` 原文逐篇/逐簇综合成 `cards/` 卡片，标注可量化程度与对接位置。
3. **落地**：在 `backlog.md` 把 `data_quantifiable: true` 且 `maps_to.status` 可实现的卡片立项，转成指标/扫描器。

## 当前进度

- [x] 盘点 + 可行性验证 + 抽取去重管线
- [x] 卡片 Schema + 样板卡
- [x] **主攻策略/模式/机会挖掘**：模式谱系总图 + 6 张 setup 卡（连板接力 / 反包 / 大长腿 / 启动 / 趋势突破 / 板块共振）
- [x] 基础特征：`mining/features.py` 已有单日 `is_limit_up` 与 `rolling_new_high`；`true_leader`、`trend_embryo` 和正式 v2.5 A–E 已分别落地。
- [ ] 落地：补齐连板高度、断板与反包等连续形态，再评估 relay/trend/concept 的后续扩展（见 backlog.md）。
- [ ] 继续补 setup 卡（超级龙头接力 / 双头反包 / 龙头切换 等）

> 指标/情绪周期切片暂缓——指标面板已基本覆盖相关数据。

## 版权与边界

个人学习资料，转化为私有量化规则自用。原始全文仅留本地 `_raw/`（不进 git、不外传），仓库只提交自己综合产出的卡片，经 `source_docs` 回溯。
