# TNM-V2-R1R2：证据链恢复任务卡

状态：`ready_for_fresh_terra`

## 1. 现场与目标

基线 HEAD 为 `b295fed963755d389629009fe50d141aab1f7f6b`。前一 Terra 留下未提交的 `mining/tail_next_morning_v2.py`、`tests/test_tail_next_morning_v2.py` 修改；不得回退，先审查后接管。

已保留证据：

- `diagnostic-2ad1049c829b-d84af76c7a3d`：原子 FAILED，`consumed_member_evidence_missing:20240905`；
- `diagnostic-2ad1049c829b-548ecfbb7e17`：违规重复启动后由规划会话按 PID 身份停止；永久只读，不得 resume/改写。

唯一目标：修正上述第一处分歧，完成 R1R 的内容校验和逐 gate 计数，生成一个新 identity。禁止改变经济公式、gate、score、排名、Top10、fixture 或 outcome。

## 2. 冻结修复

选择性读取中“请求但不存在或未成功解析的成员”必须进入内容证据，值固定为 `missing:<code>`；它不是运行错误。若以后该成员出现或可解析，sentinel 变为真实 SHA-256，checkpoint 验证必须拒绝旧结果。

必须新增回归：

1. selective source 中请求集合包含缺失成员时，checkpoint 可写、同输入可验证；
2. 缺失成员后来出现时，resume 拒绝；
3. 已存在成员等长改写并恢复 mtime 时，resume 拒绝；
4. 日 K 等长改写并恢复 mtime 时，resume 拒绝；
5. gate counts 的 predicate 与 first-failure 两种口径满足 R1R 卡。

## 3. 执行边界

tracked 白名单仅：

- `mining/tail_next_morning_v2.py`；
- `tests/test_tail_next_morning_v2.py`；
- 本卡第4节执行证据。

先运行 V2、V1、py_compile、diff check。只有全部通过才允许一个新真实 diagnostic identity；不得 resume 旧两个 identity。新结果必须与 `diagnostic-900178221317-bc70a0b260c5` 的四池排名、135条 eligible、两日 `5/6` 与 `104/20`、202条涨停剔除、300085/300339 A第2完全一致，否则原样停止。

一个白名单本地 commit，不 push。完成后只交全新独立 Sol 只读复审。完整开发期、2025、2026继续锁定。

## 4. 执行证据

尚未执行。
