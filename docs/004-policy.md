# 004 · Policy & contract

## 账本政策（`[policy]`）

| 键 | 默认 | 作用 |
|---|---|---|
| `require_executable_assertions` | true | `approve` 逐条校验 `verification`：`A<n>: <命令>`（首词在白名单 `CMD_TOKENS`）或 `MANUAL: by= how=` |
| `require_kind` | true | `kind=unclassified` 不许 approve（engine / measure / process / hygiene） |
| `require_metric_for_engine` | true | engine 类必须有 `moves_metric` |
| `visual_required_for` | `["engine"]` | 合并前必须有绑分支 HEAD 的 `G:g2:` 证据，否则要用户 `visual_skipped_by` 签字 |
| `required_tiers` | `default=["g0","g1","g1.5"]`，engine 加 `g2` | 完成矩阵：每层都要有当前 HEAD + 当前树 + 干净 的 PASS 才算 ready |

> 空列表语义：键**不存在**才用默认值；显式写 `[]` = 关掉。

## 门禁前置

任一不过 → exit 2，不写 PASS：

- 状态必须 `in_progress`；工作区必须干净（放行 `docs/issues/` 与 `.wf/`）；
- `workflow.toml` 相对基线有改动必须声明 `gate_config_change`；
- 改动面必须落在 `write_scope`、不命中 `[forbidden]`；
- 改 `tests/**` 必须有 `allow_test_changes` + `test_change_reason`。

## 契约

- **先提交协议再开工**：`workflow.toml`、`docs/issues/`、`AGENTS.md` 必须先进默认分支——worktree 从默认分支切出，没提交就等于新 worktree 里没有门禁配置。
- **账本即真相**：状态写在 markdown 的 TOML front-matter（`+++` 包裹），不建数据库。任何会话 `wfk list` 就能接手。
- **编号按文件名保号**：发号看 `docs/issues/` 文件名而非解析成功的内容；账本解析失败时 `wfk new` 直接拒绝发号（防同号双卡——实测事故入档）。
- **测试选择不做关键词兜底**：只认 `[tests].map` 显式映射，没命中就是 `[tests].default`。关键词兜底会挑出与改动无关的测试，制造「绿了但没测到」的假象。
- **`finish --cleanup` 只点名清理自己的 worktree 路径，禁通配**（实测事故：通配清到在用的 worktree）。
