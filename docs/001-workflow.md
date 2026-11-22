# 001 · Workflow

## 一张卡的一生

```
wfk new "标题" --kind engine --scope src/x.py --verify "A1: {py} -m pytest -q @step:g1"
   ↓ proposed
wfk approve <ID>          ← 逐条校验断言可执行（命令白名单 / MANUAL by=how=）
   ↓ approved
wfk start <ID>            ← 切 feat/<ID> 分支 + 建 worktree
   ↓ in_progress
wfk gate <ID> g0 → g1 → g1.5 → [g2] → [g2.5]     ← 按层门禁，证据落卡
   ↓ in_review（完成矩阵算出，不由人嘴说）
wfk merge <ID>            ← 合并闸：按分支 HEAD 重算矩阵 + 配置漂移签字
   ↓ merged
wfk finish <ID> --merged --cleanup              ← 关闭检查 + 清理 + cost 回填
```

## 门禁分层

默认 `g0/g1/g1.5`，engine 类加 `g2`——都在 `workflow.toml` 的 `required_tiers`：

| 层 | 查什么 | 拦住什么 |
|---|---|---|
| **g0** | 语法 / 类型全编译 | 最低级的坏提交 |
| **g1** | 按改动路径选出的测试子集（`[tests].map` 显式映射，无关键词兜底） | 改了 A 测了 B 的假绿 |
| **g1.5** | **摆回即红**：revert 本卡源码改动 → 选中测试必须变红；仍全绿 = 守卫无效，拒 | 测试存在但没测到本卡改动 |
| **g2** | 真机视觉（持串行锁，如 Word COM） | 结构正确但视觉不对 |
| **g2.5** | 语料对照（可判项 PASS / FAIL / N-A） | 单案过了整体没过 |

## 证据格式

每层一条，落卡上：

```
G:<tier>:<step> PASS <dur>s clean=yes @<短 sha> tree=<树 sha> cfg=<配置 blob> wfk=<版本> log=<相对路径>
```

五件套各证一件事：`@sha` 是哪个提交；`tree=` 实际被测的树（脏树 preflight 拒跑）；`clean=yes` 执行前后指纹一致（中途变脏 = FAIL）；`cfg=` 哪一份尺子；`wfk=` 哪一版工具（改了工具没重跑 = 旧证据自动失效）。

## 占位符

`{py}` 主仓 venv python、`{tests}` 选中的测试、`{doc}` 文档、`{template}` 模板、`{id}`、`{wt}`、`{root}`、`{issue}`。缺 `--doc`/`--template` 时对应步骤 skip（不假绿也不报错）。

## 运转细节

- **串行锁**：`<repo>/.wf/locks/<resource>.lock`，OS 互斥原语，进程死自动释放；取不到锁不许等。
- **worktree 运行时数据**：`[worktree] links = ["output"]` 给每棵 worktree 建 junction 指回主仓的 gitignored 目录（几千个样张不复制）。
- **全量在哪跑**：默认 worktree；`[gate] full_suite_in = "main"` 只在分支已合入后生效，否则等于拿主仓旧代码跑绿。
