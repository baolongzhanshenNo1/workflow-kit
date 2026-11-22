# workflow-kit

> **内部工具的开源版，供参考，不是产品。** 这套流程来自我们与 AI 编程 agent 的日常开发——EarlyZ 排版引擎的大部分代码由 coding agents 在它管的工作流里完成。当作研究代码对待：可能有 bug，风格与工程实践不一致，用前自行审阅调整。
>
> 为发布做过裁剪：仓里是工具本体与通用文档；内部开发账本卡与机器配置不出仓。

wfk.py 是一个单文件命令行工具（Python 标准库，零依赖），给「agent 写码」的 git 仓库装一套 issue 卡驱动的开发流程。每个改动立一张卡：改哪里（`write_scope`）、怎么验收（`verify` 命令）写死在卡上；改动在独立 worktree 里完成，过完按层门禁——g0 语法、g1 按改动路径选测试、g1.5 摆回即红（把本卡改动 revert 掉，选中测试必须变红，否则判守卫无效）、g2 真机视觉、g2.5 语料对照——每层 PASS 绑定提交指纹与工具版本，过合并闸才进主干。说完成不算完成：完成矩阵说了算，矩阵由证据算出。

它也是一种与 agent 协作的方式：**占人尽可能少的注意力，给尽可能多的可信改动。** agent 写得快，也能把一个仓库写乱；所以人只在 approve 与 merge 两处出现，其余判断交给可机械执行的断言。设计取舍见 [docs/000-philosophy.md](docs/000-philosophy.md)。

## 接入

```bash
git clone https://github.com/baolongzhanshenNo1/workflow-kit
python wfk.py selftest          # 125 条自测
cd <你的仓库>
python <path-to>/wfk.py init    # 铺 workflow.toml + 账本 + AGENTS.md + LOCKS.md
```

只用 Python 3.11+，零依赖。Windows 上命令叫 `wfk` 不叫 `wf`：`PATHEXT` 含 `.MSC`，`wf` 会命中系统自带的防火墙控制台（WF.msc）；`wfk.cmd` 自己解析解释器（`WF_PYTHON` → uv 缓存 3.11 → `uv run` 兜底）。

## Documentation

按序号读，后篇只建在前篇上：

- [000 Philosophy](docs/000-philosophy.md)：为什么存在，它怎么被验证
- [001 Workflow](docs/001-workflow.md)：卡的一生、门禁分层、证据格式
- [002 Capabilities](docs/002-capabilities.md)：能力矩阵（enforced / 未实现）
- [003 Commands](docs/003-commands.md)：命令速查
- [004 Policy & contract](docs/004-policy.md)：账本政策、证据五件套、契约
- [005 Versions](docs/005-versions.md)：版本史

## Development

`python wfk.py selftest`（tests/ 随包，不依赖任何项目配置）。

## License

[MIT](LICENSE)
