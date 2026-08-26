# TNM-V2.2-1F3：槽权重与 fixed-slot 汇总定点修复

状态：`ready_for_terra_code_repair`

规划基线：`1cfbc4c`。禁止改变经济 gate、score、排序、候选、退出、成本及其他已通过项。

## 1. 边界并列槽权重

名义 A6/B3 中，边界前的每个唯一名次权重固定为1；仅完全经济并列争夺边界槽的成员平分最后一个槽。

例：A 前5唯一，3只完全并列争第6槽，则权重必须为 `1,1,1,1,1,1/3,1/3,1/3`，不得把8只统一赋 `6/8`。无边界扩展时每只权重1。不足额时每只权重1、剩余为空槽现金0。

生产测试必须覆盖 A5+3并列、B2+2并列、无并列、不足额，并断言权重总和与每项值。

## 2. fixed-slot 汇总

对每个 `channel × strategy/control × {year,combined}` 单独输出：

- `mean, median, std, p10, p25, p75, p90, min, max`；
- `arithmetic_sum = sum(daily_slot_net)`；
- `compound_diagnostic = product(1+daily_slot_net)-1`；
- `max_drawdown`：按日期排序的 `product(1+daily_slot_net)` 曲线相对历史峰值的最小回撤。

bootstrap 必须同时具有年度和 combined 分组键；canary样本不足保留完整 schema并标记 insufficient。生产测试覆盖两年不同收益序列，断言年度与combined不混淆及三项公式。

## 3. 授权

Terra 白名单仅 `mining/tail_next_morning_v2.py`、`tests/test_tail_next_morning_v2.py`、本卡第4节。只跑V2/V1/py_compile/diff-check与合成测试；禁止E盘和真实identity。一个commit后交独立Sol仅复核第1～2节；通过才允许canary。

## 4. 执行证据

尚未执行。旧产物与shared dirty保持不动，不push。Lessons：`skip`。
