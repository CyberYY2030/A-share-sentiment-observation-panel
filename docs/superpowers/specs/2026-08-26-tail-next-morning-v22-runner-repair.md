# TNM-V2.2-1F：runner 生产链与证据治理修复卡

状态：`ready_for_terra_code_repair`

规划基线：`b47956f45cbccd320e81427fe296e7a96a407fea`

完整经济定义：`docs/superpowers/specs/2026-08-26-tail-next-morning-v22-task-cards.md` 第1～10节

## 1. 审核结论与唯一目标

独立 Sol 对 `b47956f` 的结论为 `changes_required`：P0=0、P1=3、P2=2。impulse 同日绑定、A/B gate 与 score、A6/B3、10:29/10:59 决策、3%边界及30bps纯函数未发现经济偏差。

本卡只修生产执行链、测试和证据治理，不改变经济公式、阈值、排名、槽位、买卖时间、成本、对照或统计定义。旧 attempt `output/tail-next-morning-v2/v22-canary/v22-canary-3178f827ce5b-e4e9b6ccc022` 永久只读，禁止 resume/改写。

## 2. P1：未来退出必须按日期批量读取

禁止逐股票打开未来日期容器。生产顺序固定为：

1. D 日完成 A6/B3及三组同池对照成员冻结；
2. 建立全部待退出事件状态，键为 `signal_date|channel|strategy_or_control|sec_code`；
3. 按交易日递增扫描未来日期；每个日期先收集当日仍未退出的全部 code，一次打开该日容器并选择性解析这些 code；
4. 同一日期的所有事件共用该次解析结果，逐事件应用 A/B 退出状态机；
5. 每个容器 `container_open_count<=1`；多个事件/股票共享同一天不得报 `v22_container_reopen_forbidden`；
6. 当全部事件已退出立即停止读取更晚日期；阶段末未退出保留 unresolved。

新增生产 runner 合成集成测试：至少2只股票在同一未来日期退出，断言只开一次且两者均结算；再覆盖一只延期、一只当日退出的分叉。

## 3. P1：第8～9节必须进入生产消费链

canary 和未来 development 必须调用同一组生产函数，而不是写占位字段：

- 每个日期/通道生成 strategy、`random_same_n`、`ret1450_same_n`、`activity_same_n`，保存成员与相同通道退出结果；
- 事件表必须含 selected/bought/resolved/unresolved、holding sessions、gross、net30、退出原因；
- 生产聚合必须生成通道/年份/月份/合并的事件数、coverage、胜率、均值/中位数/分位数、`sum_event_net_return`、固定6/3槽 `daily_slot_net`、月度表和固定月 block bootstrap；
- 九袖套必须由真实 A1～A6/B1～B3 信号和真实退出驱动，输出 buy/skip/hold/sell ledger、日末盯市 NAV、资金利用率、busy skip、总收益与最大回撤；上午退出资金可用于同日下午买入；
- canary 只含两个信号日也必须产出相同 schema，样本不足只标记 `insufficient_sample`，不得省略生产路径；
- development CLI/函数可冻结但本卡禁止真实开发批。

新增生产消费者测试：非并列 A6/B3、不足额现金槽、三对照成员数、30bps、同日退出再买、busy skip、NAV变化、阶段末未决阻断账户规则总收益。不得只测未被 runner 调用的 helper。

## 4. P1：输入身份、checkpoint、resume和原子终态

V22 入口必须沿用已审核 R1 内容治理：

- `run_manifest` 绑定 spec、module/test blob、base commit、允许读取的分钟容器 identity、日 K identity及规范化 input manifest hash；
- 每个 checkpoint 保存其实际消费分钟成员 SHA-256/缺失 sentinel、日 K workbook SHA-256和 `unit_consumed_input_identity`；
- resume 和 SUCCEEDED fast path 在任何产物写入前重算内容 identity；不一致 fail closed；
- 同 identity 显式 resume 只接受相同 spec/code/input，不能以“目录存在”代替验证；
- summary 的 consumed identity 只能由已验证 checkpoint 规范合成；
- 正常成功原子写 `completion=SUCCEEDED`；受控停止/KeyboardInterrupt 必须原子写 `CANCELLED` 和原因；异常写 `FAILED`，不得留下新的无终态 attempt；
- fast path 合法复用不得修改任何经济 artifact、hash或mtime。

新增集成测试：内容不变 resume、分钟成员等长改写、日 K等长改写、缺失成员后来出现、合法 SUCCEEDED fast path bytes/hash/mtime不变、KeyboardInterrupt/CANCELLED。

## 5. P2：进度和性能裁决

`progress.json` 至少在以下边界原子更新：输入冻结完成、每个依赖日期解析完成、每个市场 target 选股完成、退出扫描 frontier 前进、聚合完成。字段至少含：

- `stage`；
- `completed_units/total_units`；
- `source_frontier`；
- `completed_source_dates`；
- `open_exit_events`；
- `updated_at`。

三分钟规则限制“重复检查/无新信号的诊断方式”，不是固定计算进程的硬超时。CPU、frontier、checkpoint或mtime任一前进均是新信号。V22 canary 外部硬上限固定15分钟；达到15分钟才允许在验证PID/命令后受控停止并要求原子 `CANCELLED`。不得在3分38秒因0 checkpoint宣称 performance blocked。

## 6. 串行授权

### V22-1F：Terra 代码修复（当前唯一授权）

tracked 白名单：

- `mining/tail_next_morning_v2.py`；
- `tests/test_tail_next_morning_v2.py`；
- 本卡第7节执行证据。

Terra 只实现、测试并提交；不得启动任何真实 E盘 canary/development identity。允许临时合成数据，结束前清理。固定验证：

```powershell
python -m unittest tests.test_tail_next_morning_v2
python -m unittest tests.test_tail_next_morning
python -m py_compile mining\tail_next_morning_v2.py tests\test_tail_next_morning_v2.py
git diff --check
```

一个白名单本地 commit，不 push。完成后停止。

### V22-1FR：Sol 静态与合成生产复审

独立只读复算第2～5节，特别核验生产消费者而非孤立 helper。只能 `approve|changes_required|blocked_evidence`。只有 `approve` 才解锁下一卡。

### V22-1C：Terra 唯一新 identity canary

由规划会话另行派发。只允许一个全新 identity；只读 `20240923/20240926`及形成历史/退出所需的2024数据，不读2025/2026。外部15分钟上限，低频读 progress；成功后 Terra 终态验收并 docs-only commit。

### V22-1CR：Sol 产物审核

只读核验两日 candidate counts、A6/B3、300085/300339、三对照、跨日退出、事件汇总、九袖套、identity/hash/终态、dirty边界。`approve` 后才允许冻结完整 detached development 启动卡。

## 7. 执行证据

尚未执行。Terra 只能追加本节。

Lessons：`skip`，这是既有项目执行合同的定点修复。
