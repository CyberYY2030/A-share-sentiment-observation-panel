# TNM-V2.2-1C2：共享日期修复后的唯一新 canary

状态：`ready_for_terra_canary`

批准 runner：`4045da0`；独立 Sol：`approve`。旧 `v22-canary-3178f827ce5b-fc4e02daa3a3` 原子 FAILED 且永久只读，禁止resume。

只授权一个全新 `v22-canary` identity，targets=`20240923,20240926`，输入/输出/900秒上限与前启动卡相同。只读2024所需数据，不读2025/2026，不写E盘，不改runner/tests，不调参。终态必须原子SUCCEEDED/CANCELLED/FAILED，禁止重跑。

Terra 终态只读验收完整候选数、A6/B3、300085/300339、三对照、跨日退出、事件/年度/月度/固定槽统计、九袖套、identity/hash/进程；仅在下节追加证据并docs-only commit，随后交独立Sol。完整开发批保持锁定。

## 执行证据

### TNM-V2.2-1C2（Terra，2026-08-26）

状态：`tnm_v22_1c2_canary_verified`。唯一新 identity
`v22-canary-3178f827ce5b-ba59c5af0546` 由任务自有 PID `71708` 启动；892.719 秒内原子
`SUCCEEDED`，无 stderr、无遗留 V22 runner。未 resume、未重跑，旧 FAILED attempt
`v22-canary-3178f827ce5b-fc4e02daa3a3` 保持只读。

身份闭环：`base_commit=4267e642e6071aa1f6187820522bc59652ffbfa4`；
`spec_hash=3178f827ce5b347fa5eea62605fff9d903cbc24a5eb2848098c3754876e30342`；
`run_hash=ba59c5af054612a949703b890a44f8647c5c75ac6abcb43dc95cfdb254f8d563`；
`input_manifest_hash=b324a415b355986d566c7463d0614efeb46a9405e886f58a90dcdf2efc3ea7b9`。
19 个来源日期均为 2024，单开合计 19；2 个 market checkpoint 的成员身份已复核。

真实结果：20240923 `A_eligible/B_eligible=9/10`，A6 为
`300496,300085,300188,301382,300382,603958`，B3 为 `600053,300990,300277`；
20240926 为 `325/64`，A6 为 `300015,300339,002304,300059,000617,601099`，
B3 为 `300140,688035,002339`。300085 与 300339 均进入各自 A6。完整 eligible pool 共 408 条。

事件 72 条，全部 resolved、无 unresolved；A/B 各 strategy 加三个同 N 对照均已写入。
退出跨 20240924、20240927、20240930、20241008、20241009、20241010；A 窗 48 条、B 窗 24 条。
年度/月度/固定槽/bootstrap schema 齐全；九袖套为 `ready`，17 buy、17 sell、1 busy_skip，
diagnostic NAV `5728267.638890478`。artifact manifest 7 项均重算 SHA-256 一致。

产物路径：`output/tail-next-morning-v2/v22-canary/v22-canary-3178f827ce5b-ba59c5af0546`。
`artifact_manifest.json` SHA-256 为
`972e80505f6983d0f87b1b09d0d45594ce78bca949d031c60906066d2ed3f637`；
`v22_summary.json` SHA-256 为
`24de1af29cf6601f2307fe0780f4813ab94159a8fc8725c4c87b608a17f0e647`。
`read_2025_2026=false`、`e_drive_written=false`；shared dirty 保持，未 push，等待独立 Sol 产物审核。
