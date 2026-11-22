# 002 · Capabilities

| 能力 | 档位 | 说明 |
|---|---|---|
| issue 状态机 / worktree 生命周期 | **enforced** | 工具实际执行 |
| gate 前置（干净树 / 状态 / 配置漂移 / 改动面审计） | **enforced** | 任一不过 exit 2，不写 PASS |
| 完成矩阵（`required_tiers`，skip ≠ pass，空层 = 配置错误） | **enforced** | `in_review` 由矩阵算出来 |
| 合并闸（按分支 HEAD 重算 + 配置变更签字 + 关闭检查前移） | **enforced** | 见 `wfk merge` |
| `write_scope` / `[forbidden]` / `tests/**` 审计 | **enforced** | segment 级路径匹配 |
| `blocked_by` 依赖 | **enforced** | `start` 会拦；`unblock` 校验阻塞项状态 |
| 证据五件套（commit/tree/clean/cfg/**wfk 版本**） | **enforced** | 老格式、旧尺子版本的证据不再被认 |
| 账本原子写入（tmp + fsync + 先解析校验再替换 + 加载指纹比对） | **enforced** | 写一半不留截断账本；并发写拒绝覆盖 |
| 只读命令零副作用 / 恢复安全闸 | **enforced** | `list/scan/stats/gate status` 不触发恢复；自动恢复须变异进程已死且内容==记录哈希 |
| 合并候选树验证 | **enforced** | `[gate.merge_check]` 在冻结 base 的临时 worktree 上跑合并结果，通过且主干未动才真合并 |
| 证据有效性 tree 绑定 | **enforced** | 同一源码树上的证据永远有效——分支纯账本提交不再烧掉整套证据 |
| 日志 run-id + 轮转 | **enforced** | 重跑不覆盖旧日志（`log=` 指针永远可回放）；merge 校验指针真实存在 |
| merge 回执 / 盲评时效 | **enforced** | `merge_receipt` 落账；盲评只对绑定提交有效，分支动了必须重评或用户豁免 |
| `[metrics]` 指标白名单 | **enforced** | `moves_metric` 必须是声明过的指标 |
| 锁 | **enforced** | OS 互斥原语：同资源双持在原语层不可能，进程死自动释放 |
| 端口 / 跨仓 / POSIX | **未实现** | 端口只打印提示；跨仓拆串行 issue；POSIX 无测试基质 |
