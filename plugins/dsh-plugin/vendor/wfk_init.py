# -*- coding: utf-8 -*-
"""wfk init —— 第二实例脚手架（Phase 2，0.7.0）。

铁律：**不加角色、不做编排**。这里只做机械铺设：把一个 git 仓铺成
wfk 最小可跑实例（workflow.toml / 账本模板 / 工人入口 / 串行资源表），
不动 wfk.py 的状态机。项目差异全在 workflow.toml；铺完后门禁是否够硬，
由实例自己的 issue 去演化（README 设计约束：别加功能，除非第二项目证明必要）。

用法（在目标仓根目录）：
    python wfk.py init                 # 自动侦测 project/分支/前缀
    python wfk.py init --force         # 已有 workflow.toml 时仍铺（默认拒绝）
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

WORKFLOW_TOML = """\
# {project} —— wfk 实例档案（`wfk init` 生成，之后只放"项目差异"）
# 协议：{issue_dir}/README.md　串行资源：docs/LOCKS.md　工人红线：AGENTS.md

project = "{project}"
id_prefix = "{PREFIX}"
issue_dir = "{issue_dir}"

[repos]
main = {{ path = ".", default_branch = "{branch}" }}

[worktree]
root = "{wtroot}"

# 按改动路径选测试：没命中就回退 default 全量（strict_map 默认开——
# 部分映射不可靠时宁可全量，别拿子集当覆盖）。
[tests]
default = "tests"

# 示例映射：源码改动 → 对应测试。按仓库实际触点改。
# [[tests.map]]
# scope = "src/**"
# tests = ["tests/**"]

# 门禁 G0：语法层（几分钟内必须有结论）。
[[gate.g0.steps]]
name = "compileall"
cmd = "{{py}} -m compileall -q ."
timeout = 120

# 门禁 G1：行为层（占位符自动替换成按改动路径选出的测试）。
[[gate.g1.steps]]
name = "pytest"
cmd = "{{py}} -m pytest -q {{tests}}"
timeout = 900

# 完成矩阵：每层都要有「当前 HEAD + 干净树」的 PASS 证据才算 ready。
[policy]
required_tiers = {{ default = ["g0", "g1"] }}
"""

ISSUE_TEMPLATE = """\
+++
id = "{PREFIX}-0000"
title = "模板（新建账本正文从这里长出来）"
status = "proposed"
kind = "process"
write_scope = []
budget_tokens = 60000
verification = []
+++

## 背景

（为什么要做这件事；现状与证据。）

## 验收断言

A1: {{py}} -m pytest -q tests @step:g1.pytest

## 触达路径

（改动会落在哪些文件/模块；跨仓协作写清双仓。）

## 验证方式

（按 kind 选门禁层：process→g0/g1；engine→补 g2/g2.5；measure→g3。）

## 决策记录

（开工时留空；关闭前必填——只写"对方还不知道的"。）

## 验证证据

（关闭前必填；断言必须绑定 @step:<层>.<步骤>。）
"""

ISSUE_README = """\
# 账本协议（docs/issues/）

- 每个 issue 一张卡：`<PREFIX>-<编号>-<slug>.md`，TOML front-matter + 正文（可 diff/可 grep）。
- 生命周期：`wfk new`（proposed）→ `approve`（断言逐条校验）→ `start`（建 worktree+分支，in_progress）
  → `gate g0/g1/...`（证据绑定 HEAD+树）→ `merge`（读分支卡关账段）→ `finish --merged --cleanup`。
- 关账段（决策记录/验证证据）**双仓都要写**；merge 读的是分支卡。
- 改 `workflow.toml` 必须在卡 front-matter 声明 `gate_config_change` + 合并时 `gate_config_approved_by`
  签字——尺子不能被测方悄悄改。
- 写作用域重叠的 issue 并行前必须串行或改 scope（`wfk new` 会自动提醒）。
"""

AGENTS_MD = """\
# AGENTS.md —— 工人红线（wfk 实例）

给在本仓工作的 agent / 人：命令面就这些，别绕开状态机。

1. **门禁在跑，两仓一字不动**——包括账本 commit：证据绑定 HEAD+树，任何并行改动
   直接把在跑的门禁作废（F02），整链重录。
2. **卡字段链 start 后、gate 前一次写齐**：`g2_5_cases` / `gate_config_change` /
   `allow_test_changes` / `write_scope` 后补一次 = HEAD 移动一次 = 证据全 stale 一次。
3. **worktree 里有未提交改动，绝不 `git checkout <branch> -- .`**——它会把未提交
   补丁整棵吞掉（未跟踪文件幸存，红得比你以为的晚）。
4. 长跑门禁一律后台 + 大 timeout；前台 420s 超时腰斩会留 revert 残骸。
5. 合并闸签字不可自签：`gate_config_approved_by` 必须是用户落款（或用户明示的
   代合授权 + 当日指令）。
6. 验收断言必须可执行绑定 `@step:<层>.<步骤>`；MANUAL 断言 `by=`/`how=` 非空。
"""

LOCKS_MD = """\
# 串行资源表（docs/LOCKS.md）

真机/全局资源在此登记；`wfk gate` 的步骤声明 `serial = "<资源名>"` 时全局串行，
取不到锁**不许等**。示例（按需增删）：

- `word-com`：Word COM 真机（渲染/视觉门禁持有）。
- `<本仓专用资源>`：说明持有时机与时长。
"""

GITIGNORE_ADD = "__pycache__/\n*.pyc\n.wf/"


def _git(root: Path, *args: str) -> str:
    r = subprocess.run(["git", "-C", str(root), *args], capture_output=True,
                       text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        die(f"git {' '.join(args[:3])} 失败：{(r.stderr or '').strip()[:200]}")
    return r.stdout.strip()


def die(msg: str) -> None:
    print(f"wfk: {msg}", file=sys.stderr)
    raise SystemExit(1)


def derive_prefix(project: str) -> str:
    """workflow-kit → WFK；sgs-godot → SGS；取不出字母就用 WF。"""
    letters = re.split(r"[-_./ ]+", project)
    if len(letters) > 1:
        pfx = "".join(w[0] for w in letters if w)
    else:
        m = re.match(r"[a-zA-Z]+", project)
        pfx = m.group(0) if m else "WF"
    pfx = re.sub(r"[^A-Z]", "", pfx.upper()) or "WF"
    return pfx[:6]


def scaffold(root: Path, *, force: bool = False, branch: str | None = None,
             prefix: str | None = None) -> int:
    root = root.resolve()
    if not (root / ".git").exists():
        die("当前目录不是 git 仓——wfk 实例必须长在 git 仓上（先 git init + 首提交）。")
    if (root / "workflow.toml").is_file() and not force:
        die(f"{root / 'workflow.toml'} 已存在——wfk init 拒绝覆盖；确要重铺用 --force（会重写 workflow.toml，账本/AGENTS 不动）。")

    branch = branch or _git(root, "symbolic-ref", "--short", "HEAD") or "main"
    project = root.name
    PREFIX = prefix or derive_prefix(project)
    issue_dir = "docs/issues"
    wtroot = (root.parent / "_wt").as_posix()

    sub = {
        "project": project, "PREFIX": PREFIX, "branch": branch,
        "wtroot": wtroot, "issue_dir": issue_dir,
    }
    files: list[tuple[Path, str]] = [
        (root / "workflow.toml", WORKFLOW_TOML.format(**sub)),
        (root / issue_dir / "_template.md", ISSUE_TEMPLATE.format(PREFIX=PREFIX)),
        (root / issue_dir / "README.md", ISSUE_README.replace("<PREFIX>", PREFIX)),
        (root / "AGENTS.md", AGENTS_MD),
        (root / "docs" / "LOCKS.md", LOCKS_MD),
    ]
    wrote: list[str] = []
    for path, text in files:
        is_cfg = path.name == "workflow.toml"
        if path.exists() and (not is_cfg or not force):
            continue  # 账本/AGENTS/LOCKS 已有内容不覆盖；cfg 无 --force 也不覆盖（上方已 die）
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
        wrote.append(str(path.relative_to(root)).replace("\\", "/"))

    gi = root / ".gitignore"
    if gi.is_file():
        if GITIGNORE_ADD not in gi.read_text(encoding="utf-8", errors="replace"):
            with open(gi, "a", encoding="utf-8", newline="\n") as fh:
                fh.write(f"\n{GITIGNORE_ADD}\n")
            wrote.append(".gitignore(+.wf/)")
    else:
        gi.write_text(GITIGNORE_ADD + "\n", encoding="utf-8", newline="\n")
        wrote.append(".gitignore")

    print(f"[init] project={project}  id_prefix={PREFIX}  branch={branch}")
    for w in wrote:
        print(f"  铺设 {w}")
    print("[next] 把协议文件提交进默认分支（wfk start 从默认分支切 worktree）：")
    print("        git add workflow.toml docs AGENTS.md .gitignore && git commit -m 'chore: wfk init'")
    print("[next] 试发一张卡：python wfk.py new 试运行 --kind process "
          f"--verify 'A1: {{py}} -m pytest -q tests @step:g1.pytest'")
    return 0
