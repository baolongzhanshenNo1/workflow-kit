# 005 · Versions

`0.x` 阶段；**改行为就要升版本**（`wfk.py` 顶部 `__version__` 一改，历史证据自动失效——尺子变了，结论不能还算数）。发布 = 打 tag（`git tag -a vX.Y.Z`），`wfk --version` 自证。

- **0.4** 可信度加固：证据 / 完成矩阵逐步骤 / 改动面审计 / 尺子体检。
- **0.5** 证据绑工具版本（`wfk=<ver>`）。
- **0.6** 审计链耐久：日志 run-id + 轮转 / 盲评时效进合并闸 / merge 回执 / 原子锁 / `[metrics]` 白名单。
- **0.6.1** 证据层名归一：CLI 点号 `g2.5` → 证据/矩阵一律 TOML 键形 `g2_5`。
- **0.6.2** 正确性修复：**外部评审 19 反例全数转拒绝**——查询命令只读化、恢复走 PID 活性 + 变异哈希安全闸、gate 执行前后树指纹（运行中被改 = 无效运行）、merge 前候选合并树验证、同提交最新盲评定夺（BLOCK 翻案）、账本先校验后替换 + 乐观并发 + 换行转义、步骤 env 与 verify 同构造、证据有效性改 tree 绑定（**纯账本提交不再烧证据**）、默认 engine 矩阵补 g2。
- **0.6.3** 并发与语义包：锁换 OS 互斥原语（同资源双持在原语层不可能，进程死自动释放，心跳/墓碑协议退役）；scope 交集保守证明（`a*.py`×`*b.py` 误放行修复）；when_scope 按实际改动文件计算且 merge 侧重算「不涉及」SKIP；pick_tests 未映射兜底全量；证据消费 cfg 身份 + `log=` 必填 + 主干配置演进拒并（R18）；验收断言 `@step:` 绑定（merge 硬拦）；merge 回执拓扑四验；发号独占创建。
- **0.7.0** Phase 2：`wfk init` 脚手架 + 自举第二实例（干净 clone 全链零改动冒烟通过）。
- **0.7.1** 自举实例 g1 换 `unittest discover`（无 venv 机可用）；开源发布（LICENSE-MIT）。
- **0.7.2** 开源面重切：README 按研究原型风格重写（薄 README + `docs/000-005` 编号文档）；内部账本卡与实例配置出公开仓（白名单切割，`scripts/build_public_cut.py` 可复跑）。
- **0.7.3** 断言质量包（FPS 试验场六连修，GitHub issues #1-#6）：merge 侧复查断言正文（恒真/空体 lint——approve 后降级改卡到此为止）；`not_covered` 盲区声明必填（approve 展示、merge/finish 拦占位）；g2 块阈值必配基线证据对（`baseline.<键>` + `baseline_source`，per-shot `@N` 后缀同规）+ `wfk calibrate`（`C:` CALIB 证据行，合并闸复核基线测在分支基树）；`{doc}` 省缺自动解析账本卡、无 ```g2 块明确红非 skip；`[[tests.aux]]` 辅助文件冒烟通道（未配仍全量）；g2 卡块语法规范入 docs/002（全局键 vs `@N` per-shot 绑定）。
