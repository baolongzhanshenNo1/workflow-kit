# 003 · Commands

| 命令 | 作用 |
|---|---|
| `wfk new "标题" --scope src/** --verify "A1 ..."` | 建 issue（`proposed`），顺带做写作用域冲突检查 |
| `wfk list [--status in_progress] [--json]` | 列账本；`--json` 给机器 |
| `wfk scan [--json]` | 巡检：待决策 / 在跑 / 卡住 / 写冲突 / 僵尸锁。`--json` 输出**不含时钟**，专供 cron monitor |
| `wfk start <ID> [--owner dev-a]` | 建 `feat/<ID>` 分支 + worktree + 跑 bootstrap，状态转 `in_progress` |
| `wfk gate <ID> g0\|g1\|g1.5\|g2\|g2.5 [--doc x.docx] [--template t.docx]` | 跑门禁；g1 按改动路径选测试子集；g2 自动取串行锁 |
| `wfk gate <ID> status` | **完成矩阵**：每层证据（含 tree/clean）、缺失原因、`ready_for_merge` |
| `wfk merge <ID>` | 合并到默认分支；**自动解账本 md 冲突**（front-matter 取主仓保证据，正文取分支保决策记录）；代码冲突不自动解，直接拒绝 |
| `wfk finish <ID>` | 关闭前检查（决策记录 / 验证证据 / 状态），不通过拒绝 |
| `wfk finish <ID> --merged --cleanup` | `wfk merge` 之后收尾：置 `merged` + 移除 worktree + 回填 `merged_sha`；先验 `git merge-base --is-ancestor`，代码没进默认分支就拒绝 |
| `wfk finish <ID> --merged --manual-merge` | 人工 git merge 的显式例外：**要求账本里有 `manual_merged_by` 用户签字**（签字 = 本单跳过矩阵重算与候选树验证） |
| `wfk block <ID> --by <其他卡> [--reason ...]` | 阻塞；`blocked_by` 被 `start`/`unblock` 真正检查 |
| `wfk unblock <ID> [--force]` | 解除阻塞；阻塞项未闭环时需 `--force`（留痕） |
| `wfk drop <ID> --reason "..."` | 丢弃 issue（必须写理由） |
| `wfk verify [--only corpus,full]` | **全局体检**：按 `[[verify.steps]]` 跑，日志落 `.wf/verify/`，打印可直接粘进账本的摘要；不绑 issue、不翻状态；`ok_exit` 可配置 |
| `wfk review <ID> --verdict PASS\|BLOCK --by <谁> --note "<理由>"` | 记盲评结论，不翻状态；BLOCK 必带 ≥10 字理由 |
| `wfk stats [--since D] [--json]` | kind 占比、缺视觉合并、回工率、中位墙钟、成本、盲评率。只读 |
| `wfk selftest` | 跑 kit 自带测试（`tests/`，不依赖任何项目配置） |
| `wfk init [--prefix XXX] [--force]` | 给任意 git 仓铺脚手架：`workflow.toml` + 账本 + `AGENTS.md` + `LOCKS.md`（gitignore 自动补） |
