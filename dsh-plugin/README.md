# dsh-wfk

dsh（DeepSeek Harness）插件：把 [workflow-kit](https://github.com/baolongzhanshenNo1/workflow-kit) 的 issue 工作流纪律注入编码 agent。

装上后，agent 的 system prompt 会多出一段协议：

- 每个改动一张 issue 卡，改动范围与验收断言开工前写死；
- 改动只发生在 `wfk start` 切出的 worktree 分支，禁直改默认分支；
- 过五层门禁（g0 语法 / g1 按路径选测试 / g1.5 摆回即红 / g2 真机视觉 / g2.5 语料对照）才许 merge；
- 说完成不算完成——完成矩阵说了算，矩阵由证据算出；
- 账本即真相：`docs/issues/` 先读，任何会话 `wfk list` 接手。

仓库没有铺过协议（没有 `docs/issues/README.md`）时，agent 会提议一次 `wfk init`，不强行铺。

工具本体随包 vendor（`vendor/wfk.py`，纯 Python 3.11+ 标准库、零依赖），agent 直接 `python "<插件目录>/vendor/wfk.py" <命令>`。

## 安装（桌面版 dsh）

```bash
cd ~/.dsh/profiles/desktop
pnpm add --config.minimum-release-age=0 file:<本仓>/dsh-plugin
# 再把 "dsh-wfk" 追加进该 profile package.json 的 dsh.profile.bundles，重启 app
```

License：MIT。
