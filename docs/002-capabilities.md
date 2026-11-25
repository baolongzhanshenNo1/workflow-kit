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

| 断言正文质量（恒真/空体 lint，approve + merge 双拦） | **enforced** | `A1: true` / `A1: node` 形状的装饰断言红；merge 侧重跑 approve 同款校验，approve 后降级改卡到此为止 |
| `not_covered` 盲区声明 | **enforced** | 本卡故意不验什么——approve 展示、merge/finish 拦空值与「（…）」占位 |
| g2 块阈值基线证据对 | **enforced** | `min*/max*` 阈值（含 per-shot 后缀 `minFocusColors@1`）必配 `baseline.<键>` + `baseline_source`；机器化走 `wfk calibrate`（`C:` CALIB 证据行，合并闸复核基线测在分支基树） |
| `{doc}` 自解析 | **enforced** | 步骤命令缺 `--doc` 时自动用账本卡自身；g2 层卡无 ```g2 块 = 明确红，不是 skip |
| `[[tests.aux]]` 辅助文件冒烟 | **enforced** | aux 命中文件不触发全量回退；改动全为 aux 且配了 tests 只跑冒烟；未配仍全量（安全默认） |

## g2 卡块语法（规范，harness 作者照此实现）

g2 块是卡正文里的围栏（两种形态都认：` ```g2 ` 语言围栏，或裸围栏 + 首行 `g2`）。步骤命令用 `{doc}` 拿到卡路径，harness 解析块内 `key = value`：

```g2
url=/?qa=world&seed=7
warm=2.4
shots=1            # 要断言的机位（shot 索引，逗号分隔）
minColors=52000    # 全局阈值：作用于所有 shots
minEdge=0.026
minLuma=0.10
maxLuma=0.85
focus=0.135,0.615,0.215,0.685@1   # per-shot 绑定：@N 后缀 = 只对 shot N 断言
minFocusColors@1=3000             # 阈值键同样支持 @N
baseline.minFocusColors@1=1610    # 基线读数（改动前实测，lint 必查）
baseline_source=双仓探针 imageStats，改动前树实测
```

- **全局键**：对每个 shot 都断言；
- **`@N` 后缀键**：只对 shot 索引 N 断言——同一判别区在不同机位可能根本不在画面里，把阈值套到所有机位是伪断言（实测翻过车：无窗机位 colors=751 被 ≥3000 拦）；
- **基线对**：块内出现任何 `min*`/`max*` 阈值时，必须有同键 `baseline.<键> = <数值>` 与非空 `baseline_source`（approve 与 merge 两处 lint）。基线读数怎么来见 `wfk calibrate`；
- harness 侧只认自己声明的键，未知键忽略——规范向前兼容。
