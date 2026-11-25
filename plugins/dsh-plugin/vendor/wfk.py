#!/usr/bin/env python3
"""wfk — issue 驱动工作流的机械层（worktree / gate / 账本）。

设计约束：
- 只用标准库（tomllib），任何装了 Python 3.11+ 的机器都能直接跑。
- 账本 = docs/issues/*.md 的 TOML front-matter（+++ ... +++），文件即真相。
- 项目差异全部来自 workflow.toml；本文件不出现任何项目专有路径。
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from datetime import date
from pathlib import Path

import tomllib

__version__ = "0.7.3"

STATUSES = ["proposed", "approved", "in_progress", "in_review", "blocked", "merged", "dropped"]
ACTIVE = {"approved", "in_progress", "in_review", "blocked"}
FM = "+++"
LOCK_TTL = 180  # 秒；超过这个心跳年龄视为僵尸锁

# 账本分类（2026-11-17）：产品的进步、尺子的进步、流程的进步必须能分开数。
KINDS = ["engine", "measure", "process", "hygiene", "unclassified"]

# 断言可执行化（2026-11-17）：`verification` 每条必须是 `A<n>: <命令>`，或
# `A<n>: MANUAL: by=<谁> how=<怎么判>`。命令按首个 token 判定；占位符见 run_step。
CMD_TOKENS = {
    "{py}", "{kit}", "python", "python3", "pytest", "node", "npm", "pnpm", "yarn",
    "npx", "tsc", "cargo", "go", "dotnet", "pwsh", "powershell", "bash", "sh",
    "grep", "rg", "git", "make", "just", "uv", "curl",
}
ASSERT_RE = re.compile(r"^\s*A(\d+)\s*[:：]\s*(.+)$", re.S)

# 0.7.3（GitHub issue #1）：恒真/空体断言的正文禁词——命令 token 合法但内容只剩这些，
# 断言就是装饰。approve 与 merge 两处都按它红。
VACUOUS_WORDS = {"true", "false", "ok", "pass", "passed", "fail", "skip",
                 "yes", "no", "none", "nil", "null", "ok.", "绿", "通过", "通过。"}

# 0.7.3（issue #4/#3）：g2 卡块围栏（```g2 语言围栏，或裸围栏 + 首行 g2）。
G2_FENCE_RE = re.compile(r"```g2\s*\n(.*?)```", re.S)
G2_BARE_RE = re.compile(r"```[^\n`]*\n\s*g2\s*\n(.*?)```", re.S)

# 可信度加固（2026-11-18）：证据必须绑定「被测试的树 + 用过的尺子」。
# 背景：外部评审 + 本人复现的三条 P0 绕过——脏工作区伪造 PASS、工人自带尺子、
# 旧证据过期仍被合并闸接受。细节见 docs/改造方向-2026-11-17.md。
CFG_FILE = "workflow.toml"


def policy_cfg(cfg: dict) -> dict:
    return cfg.get("policy", {}) or {}


def policy_list(cfg: dict, key: str, default: list[str]) -> list[str]:
    """取 policy 里的列表：**键不存在**才用默认值，显式写 `[]` 表示关掉。

    旧实现 `pol.get(k) or ["engine"]` 把空列表当"没配"，导致 `visual_required_for = []`
    关不掉视觉闸（交接 N-3 / F-05，2026-11-17 复现）。
    """
    pol = policy_cfg(cfg)
    if key not in pol:
        return list(default)
    v = pol[key]
    if v is None:
        return []
    if isinstance(v, str):
        return [v]
    return [str(x) for x in v]


def required_tiers(cfg: dict, kind: str) -> list[str]:
    """完成矩阵：默认 g0/g1/g1.5，engine 再加 g2。空层不是 skip，是配置错误。"""
    rt = policy_cfg(cfg).get("required_tiers")
    if not isinstance(rt, dict):
        rt = {}
    out = [str(t) for t in (rt.get("default") or ["g0", "g1", "g1.5"])]
    for t in (rt.get(kind or "unclassified") or []):
        if str(t) not in out:
            out.append(str(t))
    # 0.6.2（外部评审 R01/F07）：默认档兑现 docstring 承诺——engine 未显式配置时补
    # g2（visual_required_for 默认含 engine，却从不把 g2 加进矩阵，承诺落空）。
    # 显式写了 required_tiers 的项目不受影响。
    if kind == "engine" and not (rt.get("engine") or []):
        for t in ("g2",):
            if t not in out:
                out.append(t)
    return out


def _steps_signature(cfg: dict, tier: str) -> str:
    '''层步骤定义签名（0.6.3/R18）：命令/条件/env 任何一项演进都算配置变了——
    分支证据在旧配置下跑的，合并后配置不同则证据不能跟着进主干。'''
    keys = ('name', 'cmd', 'env', 'when_scope', 'cwd', 'serial', 'ok_exit')
    return json.dumps([{k: s.get(k) for k in keys} for s in tier_steps(cfg, tier)],
                      ensure_ascii=False, sort_keys=True)


def tier_steps(cfg: dict, tier: str) -> list[dict]:
    """TOML 里 g1.5 只能写成 [[gate.g1_5.steps]]。"""
    return ((cfg.get("gate") or {}).get(tier.replace(".", "_")) or {}).get("steps") or []


def _g(repo: Path, *a: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "core.quotepath=false", *a],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        stdin=subprocess.DEVNULL, check=False,
    )


def commit_tree(repo: Path, rev: str = "HEAD", *, exclude: str = "") -> str:
    """被测试的**源码树**指纹（0.5.2 起排除账本目录）。

    为什么排除账本：证据要写进账本，而账本一提交 HEAD 就变、整棵树 hash 也变，
    于是"填完证据就过期"——自己把自己锁死（2026-11-18 `EZI-0045` 实测，重跑三轮）。
    账本是记录，不是被测对象；代码进库才算。
    """
    if not exclude:
        r = _g(repo, "rev-parse", "--short=7", f"{rev}^{{tree}}")
        return (r.stdout or "").strip() if r.returncode == 0 else "unknown"
    r = _g(repo, "ls-tree", "-r", rev)
    if r.returncode != 0:
        return "unknown"
    keep = []
    for line in (r.stdout or "").splitlines():
        parts = line.split("\t", 1)
        if len(parts) != 2:
            continue
        path = parts[1].strip()
        if path == exclude.rstrip("/") or path.startswith(exclude.rstrip("/") + "/"):
            continue
        keep.append(line)
    blob = "\n".join(keep).encode("utf-8")
    return hashlib.sha1(blob).hexdigest()[:7]


def cfg_blob(repo: Path) -> str:
    """实际被使用的 workflow.toml 内容 hash（含未提交改动）。"""
    if not (repo / CFG_FILE).is_file():
        return "none"
    r = _g(repo, "hash-object", CFG_FILE)
    return (r.stdout or "").strip()[:7] if r.returncode == 0 else "?"


def validate_verification(items) -> list[str]:
    """逐条检查验收断言是否可执行；返回问题列表（空=合格）。"""
    problems: list[str] = []
    for raw in items or []:
        m = ASSERT_RE.match(str(raw))
        if not m:
            problems.append(f"缺 A<编号>: 前缀 → {str(raw)[:60]}")
            continue
        body = m.group(2).strip()
        if body.upper().startswith("MANUAL:"):
            low = body.lower()
            _by = re.search(r"by\s*=\s*([^\s]+)", body)
            _how = re.search(r"how\s*=\s*(.+\S)", body)
            if not _by or not _how:
                problems.append(f"A{m.group(1)} 的 MANUAL 必须同时给非空 by= 与 how= → {body[:60]}")
            continue
        # 0.6.3/F12：可执行断言**建议**带 @step:<层>.<步骤> 绑定（merge 时硬性要求，见
        # assertion_coverage——有门禁步骤跑过的断言必须绑定执行证据，纯手跑的写 MANUAL）。
        if not re.search(r"@step:\s*[A-Za-z0-9_]+\.[A-Za-z0-9_-]+", body):
            print(f"  [next] A{m.group(1)} 可执行断言记得绑 @step:<层>.<步骤>（merge 时硬性要求）")
        words = body.split()
        token = words[0].strip("`") if words else ""
        if token not in CMD_TOKENS:
            problems.append(f"A{m.group(1)} 绑定不了可跑命令（首词 '{token}'）→ {body[:60]}")
            continue
        # 0.7.3（issue #1）：恒真/空体 lint——首词是合法命令但去掉 @step 绑定后没有
        # 实际内容（零参数，或参数全是恒真词）的断言是装饰，approve/merge 都红。
        _content = re.sub(r"@step:\s*[A-Za-z0-9_]+\.[A-Za-z0-9_-]+", "", body)
        _content = re.sub(r"\s+", " ", _content).strip().strip("`")
        _words = [w for w in _content.split() if w]
        if len(_words) < 2 or all(w.lower() in VACUOUS_WORDS for w in _words[1:]):
            problems.append(f"A{m.group(1)} 断言正文没有实际内容（恒真/空体）→ {body[:60]}")
    return problems


def g2_blocks_of(body: str) -> list[str]:
    """取卡正文里的全部 g2 块（语言围栏与裸围栏两种形态都认）。"""
    out = []
    for m in G2_FENCE_RE.finditer(body or ""):
        out.append(m.group(1))
    for m in G2_BARE_RE.finditer(body or ""):
        out.append(m.group(1))
    return out


def lint_g2_baseline(body: str) -> list[str]:
    """0.7.3（issue #3 phase 1）：g2 块里的 min*/max* 阈值必须带基线证据对。

    阈值没有基线读数就是先验拍脑袋——不可证伪。规则：
      * 块内出现 `min*`/`max*` 阈值键（含 per-shot 后缀 `minFocusColors@1`，issue #4）
        时，必须有同键 `baseline.<key> = <数值>`，且有一条非空 `baseline_source`；
      * 没有 g2 块或块里没有阈值键的卡不受影响。
    基线读数的机器化产出走 `wfk calibrate`（C: CALIB 证据行，合并闸复核其树）。
    """
    problems: list[str] = []
    for block in g2_blocks_of(body):
        kv: dict[str, str] = {}
        for line in block.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            kv[k.strip()] = v.strip()
        thresholds = [k for k in kv
                      if (k.startswith("min") or k.startswith("max")) and not k.startswith("baseline.")]
        if not thresholds:
            continue
        if not (kv.get("baseline_source") or "").strip():
            problems.append("g2 块有阈值但没有 baseline_source（基线怎么测出来的必须写一行）")
        for k in thresholds:
            bv = (kv.get(f"baseline.{k}") or "").strip()
            if not bv or not re.match(r"^-?\d+(\.\d+)?$", bv):
                problems.append(f"g2 阈值 {k} 缺基线读数 → 补 baseline.{k} = <改动前实测值>")
    return problems


def short_sha(repo: Path) -> str:
    r = _git(repo, "rev-parse", "--short=7", "HEAD")
    return (r.stdout or "").strip() if r.returncode == 0 else "unknown"


def branch_sha(repo: Path, branch: str) -> str:
    """分支尖端的短 sha。合并前的视觉证据要绑它，而不是主仓 HEAD。"""
    r = _git(repo, "rev-parse", "--short=7", branch)
    if r.returncode != 0:
        die(f"取不到分支 {branch} 的提交号（分支不存在？先 wfk start）")
    return (r.stdout or "").strip()


def gate_evidence(iss: "Issue", tier: str = "", sha: str = "") -> list[str]:
    """取账本里的门禁证据；可按层（g0/g1/g1.5/g2）与提交号过滤。"""
    out = []
    for e in iss.meta.get("evidence") or []:
        s = str(e)
        if not s.startswith("G:"):
            continue
        if tier and not s.startswith(f"G:{tier}:"):
            continue
        if sha and f"@{sha}" not in s:
            continue
        out.append(s)
    return out


# ────────────────────────── 项目发现与配置 ──────────────────────────


def find_root(start: Path) -> Path:
    cur = start.resolve()
    for p in [cur, *cur.parents]:
        if (p / "workflow.toml").is_file():
            return main_worktree(p)
    die("找不到 workflow.toml —— 请在项目仓库内执行（worktree 内调用会自动解析回主工作区）。")


def main_worktree(p: Path) -> Path:
    """在 linked worktree 里发现配置时，回退到主工作区：账本、venv、锁都必须只有一份。"""
    r = subprocess.run(
        ["git", "-C", str(p), "rev-parse", "--git-common-dir"], capture_output=True, text=True
    )
    if r.returncode != 0:
        return p
    common = Path(r.stdout.strip())
    if not common.is_absolute():
        common = (p / common).resolve()
    main = common.parent
    if (main / "workflow.toml").is_file() and main != p:
        return main
    return p


def load_cfg(root: Path) -> dict:
    p = root / CFG_FILE
    try:
        with open(p, "rb") as f:
            cfg = tomllib.load(f)
    except FileNotFoundError:
        die(f"{p} 不存在 —— 请在项目根写 workflow.toml")
    except tomllib.TOMLDecodeError as exc:
        die(f"{p} 解析失败：{exc}\n"
            f"  常见原因：Windows 路径的反斜杠没转义 —— 写正斜杠（E:/wt/x）或双反斜杠。")
    for key in ("project", "issue_dir"):
        if key not in cfg:
            die(f"workflow.toml 缺少必填字段 {key}")
    cfg.setdefault("id_prefix", "ISS")
    cfg.setdefault("repos", {"main": {"path": ".", "default_branch": "main"}})
    cfg.setdefault("worktree", {})
    cfg["worktree"].setdefault("root", str(root.parent / "_wt"))
    return cfg


def repo_path(root: Path, cfg: dict, key: str) -> Path:
    repos = cfg["repos"]
    if key not in repos:
        die(f"未知 repo '{key}'；workflow.toml 里只有：{', '.join(repos)}")
    p = Path(repos[key]["path"])
    return (root / p).resolve() if not p.is_absolute() else p


def default_branch(cfg: dict, key: str) -> str:
    return cfg["repos"][key].get("default_branch", "main")


def venv_python(root: Path) -> str:
    for cand in (root / ".venv/Scripts/python.exe", root / ".venv/bin/python"):
        if cand.is_file():
            return str(cand)
    return sys.executable


# ────────────────────────── issue 读写 ──────────────────────────


def toml_escape(s: str) -> str:
    # 0.6.2（外部评审 R06）：换行/回车/制表符必须转义——旧实现只处理反斜杀与引号，
    # 标题带换行会生成非法 TOML，而且旧流程是「先替换再解析」，账本直接被写坏。
    out = (s.replace("\\", "\\\\").replace('"', '\\"')
           .replace("\r\n", "\\n").replace("\r", "\\n").replace("\n", "\\n")
           .replace("\t", "\\t"))
    return "".join(ch if ch >= " " else f"\\u{ord(ch):04x}" for ch in out)


def toml_value(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, list):
        return "[" + ", ".join(toml_value(x) for x in v) + "]"
    return '"' + toml_escape(str(v)) + '"'


def dump_front_matter(meta: dict) -> str:
    lines = [FM]
    for k, v in meta.items():
        lines.append(f"{k} = {toml_value(v)}")
    lines.append(FM)
    return "\n".join(lines)


def parse_issue(path: Path) -> tuple[dict, str]:
    text = path.read_text(encoding="utf-8")
    if not text.startswith(FM):
        die(f"{path.name} 缺少 {FM} front-matter")
    _, fm, body = text.split(FM, 2)
    meta = tomllib.loads(fm)
    return meta, body


def _atomic_write(path: Path, text: str) -> None:
    """原子落盘 + **写前**解析自检（0.6.2）。

    `Issue.save` 原来直接 write_text：进程在中途被杀会留下截断账本，
    而账本是唯一真相。写临时文件 → fsync → os.replace；**解析在替换之前**——
    0.6.2 前是先替换再解析（外部评审 R06），非法序列化会把合法账本覆盖成废文件。
    保留原文件的换行风格（账本有的是 CRLF），避免每次保存都整个文件翻掉。
    """
    old = path.read_text(encoding="utf-8") if path.is_file() else ""
    nl = "\r\n" if "\r\n" in old else "\n"
    data = text.replace("\r\n", "\n")
    if nl != "\n":
        data = data.replace("\n", nl)
    # 写前自检（0.6.2/R06）：新内容自己必须能解析，否则原文件一字节不动。
    if data.startswith(FM):
        try:
            _head, _fm, _rest = data.split(FM, 2)
            tomllib.loads(_fm)
        except Exception as exc:  # noqa: BLE001
            die(f"账本新内容解析失败（{path.name}），原文件未改动：{exc}")
    tmp = path.with_name(path.name + f".tmp{os.getpid()}")
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    try:
        parse_issue(path)
    except Exception as exc:  # noqa: BLE001
        die(f"账本写入后解析失败（{path.name}）：{exc}")


class Issue:
    def __init__(self, path: Path):
        self.path = path
        self.meta, self.body = parse_issue(path)
        # 0.6.2（外部评审 R07）：读时记磁盘指纹。save 时磁盘若已被其他会话改过，
        # 拒绝覆盖并要求重读——两个旧快照互相保存 = 静默吞掉对方证据的日子到头了。
        self._disk = _file_sha256(path) or ""

    @property
    def id(self) -> str:
        return self.meta.get("id", self.path.stem.split("-")[0])

    def save(self, meta=None, body=None):
        if meta is not None:
            self.meta = meta
        if body is not None:
            self.body = body
        # 乐观并发（0.6.2/R07）：本对象加载之后磁盘变了（另一会话保存过）→ 拒绝覆盖。
        cur = _file_sha256(self.path) or ""
        if cur != self._disk:
            die(f"{self.path.name} 在本次读取后被其他会话/进程修改过（磁盘指纹不一致)。"
                f"本次改动未写入——请重新加载账本（重跑当前命令）后再保存，"
                f"否则会静默吞掉对方写入（外部评审 R07）。")
        self.meta["updated"] = date.today().isoformat()
        _atomic_write(self.path, dump_front_matter(self.meta) + "\n" + self.body.lstrip("\n"))
        self._disk = _file_sha256(self.path) or ""

    def section(self, title: str) -> str:
        """取正文中 '## <title>' 到下一个 '## ' 之间的内容。"""
        m = re.search(rf"^##\s+{re.escape(title)}\s*$(.*?)(?=^##\s+|\Z)", self.body, re.M | re.S)
        return (m.group(1) if m else "").strip()


def load_issues(root: Path, cfg: dict) -> list[Issue]:
    d = root / cfg["issue_dir"]
    if not d.is_dir():
        return []
    out = []
    for p in sorted(d.glob("*.md")):
        if p.name in ("README.md", "_template.md") or p.name.startswith("_"):
            continue
        try:
            out.append(Issue(p))
        except Exception as e:  # 坏账本要吵，不能静默跳过
            print(f"[warn] 解析失败 {p.name}: {e}", file=sys.stderr)
    return out


def bad_issues(root: Path, cfg: dict) -> list[str]:
    """返回无法解析的账本文件名列表（供 new/approve 等前置检查用）。"""
    d = root / cfg["issue_dir"]
    bad: list[str] = []
    if not d.is_dir():
        return bad
    for p in sorted(d.glob("*.md")):
        if p.name in ("README.md", "_template.md") or p.name.startswith("_"):
            continue
        try:
            Issue(p)
        except Exception as e:  # noqa: BLE001
            bad.append(f"{p.name}（{e}）")
    return bad


def slugify(title: str) -> str:
    s = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff._-]+", "-", title.strip())
    s = re.sub(r"-{2,}", "-", s).strip("-")
    return s[:48] or "issue"


def next_id(root: Path, cfg: dict) -> str:
    """下一个编号。**按文件名保号**：解析失败的账本也要占住自己的编号。

    2026-11-17 实测事故：`EZI-0037` 账本 front-matter 里 `worktree` 路径反斜杠没转义
    → `load_issues` 只 warn 不中止 → `next_id` 看不到 0037 → `wfk new` 又发了一个
    `EZI-0037`，仓库里出现两条同号账本。编号来自文件名，坏账本同样占用。
    """
    prefix = cfg["id_prefix"]
    d = root / cfg["issue_dir"]
    mx = 0
    if d.is_dir():
        pat = re.compile(rf"{re.escape(prefix)}-(\d+)")
        for p in d.glob("*.md"):
            m = pat.match(p.name)
            if m:
                mx = max(mx, int(m.group(1)))
    for iss in load_issues(root, cfg):  # 兜底：文件名不规范但 id 字段正确
        m = re.fullmatch(rf"{re.escape(prefix)}-(\d+)", iss.id)
        if m:
            mx = max(mx, int(m.group(1)))
    return f"{prefix}-{mx + 1:04d}"


# ────────────────────────── 锁 ──────────────────────────


def lock_dir(root: Path) -> Path:
    d = root / ".wf" / "locks"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _pid_alive(pid: int) -> bool:
    '''跨平台 PID 活性检查（0.6.3 锁负载判用，同 recover_inflight 的活性闸语义）。'''
    if not pid or pid < 1:
        return False
    try:
        if os.name == 'nt':
            import ctypes
            SYNCHRONIZE = 0x00100000
            h = ctypes.windll.kernel32.OpenProcess(SYNCHRONIZE, 0, int(pid))
            if not h:
                return False
            ctypes.windll.kernel32.CloseHandle(h)
            return True
        os.kill(int(pid), 0)
        return True
    except OSError:
        return False


class Lock:
    """串行资源锁（0.6.3：双层——OS 互斥 + 生态兼容负载协议）。

    外部评审 R02/R03 的竞争（同 issue 双持、墓碑 rename 删新 owner 的锁）根在
    token/心跳协议对 wfk 自身实例之间**证不出互斥**。0.6.3 第一版把负载文件整个换成
    OS 字节区锁——单机互斥成立了，但 earlyz 侧脚本（visual_gate 等）的 WordComLock 是
    **同 issue 过继**语义：作为 gate 步骤的子进程，它们重写同一负载文件表达
    「接管不夺锁」；字节区锁把过继写全拒（EZI-0062 g2 梯次实撞：
    visual_gate 过继写 [Errno 13] Permission denied，独立手跑全过）。

    正解是双层，各管各的：
    * ``<res>.mutex`` —— OS 字节区锁（Windows ``msvcrt.locking`` / POSIX ``fcntl.flock``）：
      wfk 实例之间**真互斥**（同进程双句柄也拒）。R02/R03 在原语层不可能；
      进程死亡（含 kill -9）内核关句柄自动释放。
    * ``<res>.lock`` —— 0.6.0 起的 token/心跳/墓碑负载协议**原样保留**：earlyz 脚本生态
      依赖它（standalone 运行按心跳新鲜度拒让；wfk gate 子进程按同 issue 过继重写）。
      wfk 持锁期间心跳线程照常喂它，standalone 脚本由此看得见「锁活着」。
    """

    def __init__(self, root: Path, resource: str, issue: str):
        self.path = lock_dir(root) / f"{resource}.lock"
        self.mutex_path = lock_dir(root) / f"{resource}.mutex"
        self.resource, self.issue = resource, issue
        self._stop = threading.Event()
        self.held = False
        self._acquired = 0.0
        self.token = f"{os.getpid():x}{os.urandom(4).hex()}"
        self._mutex_fh = None

    def _payload(self) -> dict:
        return {
            "resource": self.resource,
            "issue": self.issue,
            "pid": os.getpid(),
            "host": os.environ.get("COMPUTERNAME") or os.environ.get("HOSTNAME") or "local",
            "token": self.token,
            "acquired_at": self._acquired,
            "heartbeat_at": time.time(),
        }

    def _read(self) -> dict:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _reclaim(self, why: str) -> None:
        """原子回收：rename 进唯一墓碑名抢独占删除权再删（直接 unlink 有 TOCTOU）。

        wfk 实例之间的回收竞争已被 mutex 串行化；这里防的是与 standalone 脚本
        （不持 mutex）交错的窗口。
        """
        print(f"[warn] 回收僵尸锁 {self.resource}（{why}）", file=sys.stderr)
        tomb = self.path.with_name(self.path.name + f".dead-{os.urandom(4).hex()}")
        try:
            os.rename(self.path, tomb)
        except OSError:
            return
        try:
            tomb.unlink()
        except OSError:
            pass

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.mutex_path.parent.mkdir(parents=True, exist_ok=True)
        # 1) OS 互斥（wfk 实例之间——R02/R03 的根）
        fh = open(self.mutex_path, "a+b")
        try:
            fh.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            fh.close()
            held = self._read()
            die(f"资源 '{self.resource}' 正被 wfk 持有（issue={held.get('issue', '?')} "
                f"pid={held.get('pid', '?')}）——不要等，记 blocked 后换串行。{e}")
        self._mutex_fh = fh
        # 2) 负载协议（earlyz 脚本生态同协议）
        for _ in range(3):
            try:
                self._acquired = time.time()
                fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                with os.fdopen(fd, "w", encoding="utf-8", closefd=True) as f:
                    json.dump(self._payload(), f, ensure_ascii=False)
                self.held = True
                threading.Thread(target=self._beat, daemon=True).start()
                return
            except FileExistsError:
                cur = self._read()
                age = time.time() - float(cur.get("heartbeat_at") or 0)
                held_other = (age < LOCK_TTL and cur.get("issue") != self.issue)
                if held_other and _pid_alive(int(cur.get("pid") or 0)):
                    self._release_mutex()
                    die(
                        f"资源 '{self.resource}' 被 {cur.get('issue', '?')} 持有 "
                        f"（心跳 {int(age)}s 前，pid {cur.get('pid')}）。不要等，记 blocked。"
                    )
                # 僵尸锁（心跳超时/持有进程已死）或同 issue 残留：原子回收后重试。
                # 同 issue 残留在这里是安全的——mutex 已证明没有另一个 wfk 实例活着。
                why = (f"心跳 {int(age)}s 未更新" if age >= LOCK_TTL
                       else "持有进程已死" if held_other else "同 issue 残留")
                self._reclaim(why)
                time.sleep(0.05)
        self._release_mutex()
        die(f"锁 {self.resource} 竞争失败（重试 3 次）——不要等，记 blocked。")

    def _beat(self) -> None:
        while not self._stop.wait(30):
            try:
                if self._read().get("token") != self.token:
                    return    # 锁已被回收/易主，别把心跳写回别人的锁上
                self.path.write_text(json.dumps(self._payload(), ensure_ascii=False), encoding="utf-8")
            except Exception:
                return

    def _release_mutex(self) -> None:
        if self._mutex_fh is not None:
            try:
                self._mutex_fh.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(self._mutex_fh.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self._mutex_fh.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
            try:
                self._mutex_fh.close()
            except OSError:
                pass
            self._mutex_fh = None

    def release(self) -> None:
        self._stop.set()
        if self.held:
            if self._read().get("token") == self.token:
                try:
                    self.path.unlink()
                except OSError:
                    pass
            # token 不匹配 = 锁已被别人回收重建；删掉会误伤新持有者（F-07 场景），不删
        self._release_mutex()
        self.held = False


def live_locks(root: Path) -> list[dict]:
    '''锁文件元数据（仅供 scan 展示）。活性由 OS 锁保证：文件存在 ≠ 被持有，
    持有者退出后文件保留、下个申请者直接复用。'''
    out = []
    for p in sorted(lock_dir(root).glob('*.lock')):
        try:
            d = json.loads(p.read_text(encoding='utf-8'))
        except Exception:  # noqa: BLE001
            d = {'resource': p.stem, 'issue': '?'}
        d.setdefault('resource', p.stem)
        out.append(d)
    return out
# ────────────────────────── worktree / 改动文件 ──────────────────────────


def wt_path(root: Path, cfg: dict, repo: str, issue_id: str) -> Path:
    return Path(cfg["worktree"]["root"]).resolve() / f"{repo}-{issue_id}"


def changed_files(wt: Path, base: str) -> list[str]:
    def run(args):
        r = subprocess.run(
            ["git", "-C", str(wt), "-c", "core.quotepath=false", *args],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        return r.stdout if r.returncode == 0 else ""

    files = set()
    # 起点必须是 merge-base，且**优先本地 base 分支**：这个仓库从不 push 主干，
    # origin/app-mode 是几个月前的陈旧引用——用它算 merge-base 会把整段历史都算进改动面
    # （实测拿到 200 个文件，测试选择直接跑偏）。
    mb = ""
    for ref in (base, f"origin/{base}"):
        mb = run(["merge-base", ref, "HEAD"]).strip()
        if mb:
            break
    if mb:
        for line in run(["diff", "--name-only", mb, "HEAD"]).splitlines():
            if line.strip():
                files.add(line.strip())
    for line in run(["status", "--porcelain"]).splitlines():
        if not line.strip():
            continue
        p = line[3:].strip()
        if " -> " in p:
            p = p.split(" -> ", 1)[1]
        files.add(p.strip('"'))
    return sorted(files)


def worktree_dirty(wt: Path, ignore_prefixes: tuple[str, ...] = ()) -> list[str]:
    """工作区里不干净的文件（忽略账本目录与 .wf）。"""
    st = _g(wt, "status", "--porcelain")
    out = []
    for line in st.stdout.splitlines():
        if len(line) < 4:
            continue
        p = line[3:].strip()
        if " -> " in p:
            p = p.split(" -> ", 1)[1]
        p = p.strip('"')
        if any(p == pre.rstrip("/") or p.startswith(pre) for pre in ignore_prefixes):
            continue
        out.append(p)
    return sorted(set(out))


def config_drift(wt: Path, base: str) -> bool:
    """当前分支（合并基点到 HEAD）是否改过 workflow.toml。"""
    if not (wt / CFG_FILE).is_file():
        return False
    r = _g(wt, "diff", "--quiet", f"{base}...HEAD", "--", CFG_FILE)
    return r.returncode == 1


def _parts(s: str) -> list[str]:
    return [p for p in s.replace("\\", "/").strip().strip("/").split("/") if p]


def _match_parts(parts: list[str], pats: list[str]) -> bool:
    if not pats:
        return not parts
    if pats[0] == "**":
        return any(_match_parts(parts[i:], pats[1:]) for i in range(len(parts) + 1))
    if not parts:
        return False
    return fnmatch.fnmatchcase(parts[0], pats[0]) and _match_parts(parts[1:], pats[1:])


def path_matches(path: str, pattern: str) -> bool:
    """路径 segment 级匹配。

    2026-11-18 重写：旧实现 `f.startswith(s.rstrip("*/"))` 让 `src/**` 吃到 `src2/a.py`
    （假阳性，Codex F-13 实测），而 `src/*/a.py` 与 `src/x/*.py` 的真重叠漏报。
    现在 `**` 跨目录、`*` 只吃单段，与使用者直觉一致。
    """
    return _match_parts(_parts(path), _parts(pattern))


def match_any(path: str, patterns: list[str]) -> bool:
    return any(path_matches(path, p) for p in patterns or [])


def scope_match(files: list[str], scopes: list[str]) -> list[str]:
    return [f for f in files if match_any(f, scopes)]


def _seg_is_literal(seg: str) -> bool:
    return bool(seg) and not any(c in seg for c in '*?[')


def _scopes_provably_disjoint(a: str, b: str) -> bool:
    '''能否**证明**两个 glob 永不命中同一文件（0.6.3/R05）。

    旧实现给每个 glob 造一条"见证路径"互测——只能找到部分交集，证不出不相交：
    ``src/a*.py`` 与 ``src/*b.py`` 都能命中 ``src/ab.py``，旧算法却报"无重叠"，
    批准阶段放行了两个会写同一文件的任务。正确姿势是保守证明：
    逐段比较，**两段都字面且不同 → 永不交汇**；任何一段含通配/``**``/一侧提前
    结束但另一侧还有内容 → 无法证明不相交（按可能重叠处理）。Windows 大小写
    不敏感，比较前统一小写。
    '''
    pa = _parts(a)
    pb = _parts(b)
    if not pa or not pb:
        return False
    for i in range(max(len(pa), len(pb))):
        sa = pa[i] if i < len(pa) else None
        sb = pb[i] if i < len(pb) else None
        if sa == '**' or sb == '**':
            return False       # ** 覆盖任意后续段，证明不了
        if sa is None or sb is None:
            return True        # 一侧走完且此前全等：路径层级不同，字面不交汇
        if sa.lower() == sb.lower():
            continue
        if _seg_is_literal(sa) and _seg_is_literal(sb):
            return True        # 两段字面且不同 → 永不交汇
        return False           # 有通配 → 保守按重叠
    return False               # 全等 = 完全重叠
def audit_changes(iss: "Issue", files: list[str], cfg: dict) -> list[str]:
    """改动面审计：write_scope 越界 / 禁写路径 / tests 未授权。

    协议里写了很多"不许"，但工具以前一条都不读（Codex F-03）。现在 gate 与 merge
    各审计一次——这是把"README 说不许"变成"工具会拒绝"的最小落点。
    """
    if not files:
        return []
    problems: list[str] = []
    # 账本必须能在 worktree 里改（决策记录 / 验证证据），它不是代码改动。
    # 2026-11-18 实测：不剔除会让"按协议写证据"的 issue 被判越界，等于尺子逼人违规。
    idir = str(cfg.get("issue_dir", "docs/issues")).rstrip("/")
    files = [f for f in files if not (f == idir or f.startswith(idir + "/"))]
    if not files:
        return []
    tdir = (cfg.get("tests") or {}).get("default", "tests")
    cfg_declared = bool((iss.meta.get("gate_config_change") or "").strip())
    allow = str(iss.meta.get("allow_test_changes", "")).strip().lower() in ("true", "1", "yes")

    def _excused(f: str) -> bool:
        """已被其它显式机制放行的文件，不再按通用禁写规则拦。"""
        if cfg_declared and f == CFG_FILE:
            return True      # 门禁配置：走「声明 + 合并签字」
        if allow and path_matches(f, f"{tdir}/**"):
            return True      # 测试：走 allow_test_changes 授权（tests 常在 [forbidden] 里）
        return False

    scope = list(iss.meta.get("write_scope") or [])
    if scope:
        out = [f for f in files if not match_any(f, scope) and not _excused(f)]
        if out:
            problems.append("越界改动（不在 write_scope）：" + ", ".join(out[:8]))
    forb = list((cfg.get("forbidden") or {}).get("paths") or [])
    hits = [f for f in files if match_any(f, forb) and not _excused(f)]
    if hits:
        problems.append("命中禁写路径：" + ", ".join(hits[:8]))
    tfiles = [f for f in files if path_matches(f, f"{tdir}/**")]
    if tfiles and not allow:
        problems.append("改动了测试但 issue 未授权（补 allow_test_changes = true 与 "
                        "test_change_reason = \"<为什么必须改测试>\"）：" + ", ".join(tfiles[:8]))
    return problems


def expand(root: Path, wt: Path, pattern: str) -> list[str]:
    """把 "tests/test_latex*.py" 展开成真实存在的相对路径（相对 worktree）。"""
    return [str(p.relative_to(wt)).replace("\\", "/") for p in sorted(wt.glob(pattern))]


def pick_tests(root: Path, cfg: dict, wt: Path, files: list[str]) -> str:
    tcfg = cfg.get("tests", {})
    default = tcfg.get("default", "tests")
    picked: list[str] = []
    for entry in tcfg.get("map", []):
        if scope_match(files, [entry["scope"]]):
            for pat in entry.get("tests", []):
                picked.extend(expand(root, wt, pat))
    # 0.6.3/R04（F09）：**任何**改动文件不在映射覆盖内（且不是测试文件本身）→ 回退默认全量。
    # 旧实现在「部分命中」时只跑命中的子集，未映射的改动静默逃过测试。
    # 0.6.3 补丁：账本目录与 workflow.toml 不算「未映射源码」；cfg 改动走签字路径。
    # 0063 实撞扩口径：docs/**（报告/快照/语料数据）是数据面，不是测试输入。
    files = [f for f in files
             if not f.startswith('docs/') and f != 'workflow.toml']
    strict = bool((cfg.get('tests') or {}).get('strict_map', True))
    tdir = ((cfg.get('tests') or {}).get('dir') or 'tests').strip('/')
    # 0.7.3（issue #6）：辅助文件白名单——.gitignore/LICENSE/依赖垫片这类改动不该触发
    # 全量回退噪音。[[tests.aux]] 声明 scope（glob，可多条）与可选 tests（冒烟测试）：
    #   * aux 命中的文件不计入"未映射 → 回退全量"的触发名单（安全语义对其它路径不变）；
    #   * 改动**全部**是 aux 且有声明的冒烟 → 只跑冒烟；没声明 → 仍全量（安全默认）。
    aux_entries = [e for e in (tcfg.get("aux") or []) if e.get("scope")]
    aux_pats = [e["scope"] for e in aux_entries]
    if strict and picked:
        mapped_scopes = [e['scope'] for e in tcfg.get('map', [])]
        unmapped = [f for f in files
                    if not match_any(f, mapped_scopes) and not match_any(f, [f'{tdir}/**'])
                    and not (aux_pats and match_any(f, aux_pats))]
        if unmapped:
            print(f"[tests] {len(unmapped)} 个改动文件不在 tests.map 覆盖内（如 {unmapped[:3]}）")
            print("[tests] 部分映射不可靠 → 回退默认全量。workflow.toml [tests].strict_map=false 可豁免（不推荐）")
            return str(default)
    # 改动本身就是测试文件 → 把它加进选择（新守卫必须跟着跑，而不是躲在全量后面）
    for tf in [f for f in files if match_any(f, [f'{tdir}/**'])]:
        picked.extend(expand(root, wt, tf))
    if picked:
        return " ".join(sorted(set(picked)))
    # 0.7.3（issue #6）：路径映射未命中时，若改动全是 [[tests.aux]] 声明的辅助文件
    # 且有配冒烟测试 → 只跑冒烟；没配 → 仍全量（安全默认，提示怎么降噪）。
    if aux_pats and files and all(match_any(f, aux_pats) for f in files):
        for e in aux_entries:
            if any(match_any(f, [e["scope"]]) for f in files):
                for pat in e.get("tests") or []:
                    picked.extend(expand(root, wt, pat))
        if picked:
            print(f"[tests] 改动均为辅助文件 → 只跑 [[tests.aux]] 声明的冒烟（{len(set(picked))} 项）")
            return " ".join(sorted(set(picked)))
        print("[tests] 改动均为辅助文件但未配 aux tests → 仍跑全量（在 [[tests.aux]] 配 tests 可降噪）")
    # 映射没命中就是全量。**不要**再加关键词兜底：那会挑出一批与改动无关的测试。
    # 实测过一次：改的是 cli.py / api/agent_loop.py / pipeline/*，兜底选出 18 个 equation 测试
    # 跑出 115 passed 的假绿，真正的改动面一条都没测。
    print(f"[tests] 路径映射未命中（{len(files)} 个改动文件）→ 跑全部测试", file=sys.stderr)
    return default


# ────────────────────────── 子命令 ──────────────────────────


def cmd_new(args, root, cfg):
    bad = bad_issues(root, cfg)
    if bad:
        die("账本里有无法解析的文件，先修好再发号（否则会重号）：\n  - "
            + "\n  - ".join(bad))
    tid = next_id(root, cfg)
    meta = {
        "id": tid,
        "title": args.title,
        "status": "proposed",
        "kind": getattr(args, "kind", None) or "unclassified",
        "repo": args.repo,
        "branch": "",
        "worktree": "",
        "write_scope": args.scope or [],
        "blocked_by": args.blocked_by or [],
        "budget_tokens": args.budget,
        "cost_tokens": 0,
        "cost_wall_min": 0,
        "rework": 0,
        "moves_metric": getattr(args, "moves_metric", "") or "",
        "expected_delta": "",
        "not_covered": "",  # 0.7.3（issue #2）：本卡故意不验什么——approve 展示、merge 必填非空
        "visual_skipped_by": "",
        "gate_config_change": "",
        "gate_config_approved_by": "",
        "allow_test_changes": "",
        "test_change_reason": "",
        "review": "",
        "owner": "",
        "created": date.today().isoformat(),
        "updated": date.today().isoformat(),
        "verification": args.verify or [],
        "evidence": [],
    }
    tpath = root / cfg["issue_dir"] / "_template.md"
    if not tpath.is_file():
        die(f"缺少 {tpath.relative_to(root).as_posix()} —— 这是新建账本的正文模板，"
            f"从已有项目复制一份（含「背景/验收断言/触达路径/验证方式」五段）再用 wfk new。")
    tmpl = tpath.read_text(encoding="utf-8")
    _, _, body = tmpl.split(FM, 2)
    body = body.replace("EZI-0000", tid).replace("EZI-XXXX", tid)
    # 0.6.3/R07：发号-落盘**独占创建**（'x' 模式）——两个并发 wfk new 扫到同一个
    # 最大号就会发重号；旧写法直接 write_text 覆盖，输家整张卡静默消失。
    for _attempt in range(5):
        out = root / cfg["issue_dir"] / f"{tid}-{slugify(args.title)}.md"
        try:
            with open(out, "x", encoding="utf-8", newline="") as fh:
                fh.write(dump_front_matter(meta) + "\n" + body.lstrip("\n"))
            break
        except FileExistsError:
            new_tid = next_id(root, cfg)
            body = body.replace(tid, new_tid)
            tid = new_tid
            meta["id"] = tid
            continue
    else:
        die("发号竞争 5 连撞——稍后重试 wfk new")
    print(f"created {out.relative_to(root)}  kind={meta['kind']}")
    if meta["kind"] == "unclassified":
        print("  [next] 在 front-matter 补 kind = engine|measure|process|hygiene —— approve 会拦 unclassified")
    if meta["write_scope"]:
        conflicts = find_conflicts(load_issues(root, cfg), include_proposed=True)
        for a, b, sc in conflicts:
            if tid in (a, b):
                print(f"[conflict] {a} × {b} 写作用域重叠：{sc} —— 并行前必须串行或改 scope")
    return 0


def scope_overlap(a_scopes: list[str], b_scopes: list[str]) -> str | None:
    '''两个 write_scope 是否可能覆盖同一文件（保守：能**证明**不相交才算安全）。'''
    for sa in a_scopes or []:
        for sb in b_scopes or []:
            if not _scopes_provably_disjoint(sa, sb):
                return f"{sa} ↔ {sb}"
    return None
def find_conflicts(issues: list[Issue], include_proposed: bool = False) -> list[tuple[str, str, str]]:
    out = []
    pool = ACTIVE | ({"proposed"} if include_proposed else set())
    act = [i for i in issues if i.meta.get("status") in pool]
    for i in range(len(act)):
        for j in range(i + 1, len(act)):
            a, b = act[i], act[j]
            ov = scope_overlap(a.meta.get("write_scope", []), b.meta.get("write_scope", []))
            if ov:
                out.append((a.id, b.id, ov))
    return out


def cmd_list(args, root, cfg):
    issues = load_issues(root, cfg)
    if args.status:
        issues = [i for i in issues if i.meta.get("status") in args.status]
    if args.json:
        rows = [
            {
                "id": i.id,
                "status": i.meta.get("status"),
                "repo": i.meta.get("repo"),
                "owner": i.meta.get("owner") or "",
                "blocked_by": i.meta.get("blocked_by") or [],
                "write_scope": i.meta.get("write_scope") or [],
                "worktree": bool(i.meta.get("worktree")) and Path(i.meta["worktree"]).exists(),
                "title": i.meta.get("title", ""),
            }
            for i in sorted(issues, key=lambda x: x.id)
        ]
        print(json.dumps({"project": cfg["project"], "issues": rows}, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    if not issues:
        print("（账本为空）")
        return 0
    w = max(len(i.id) for i in issues)
    for i in sorted(issues, key=lambda x: x.id):
        m = i.meta
        owned = f" @{m['owner']}" if m.get("owner") else ""
        print(f"{i.id:<{w}}  {m.get('status','?'):<11} {m.get('repo','?'):<5} {m.get('title','')}{owned}")
    return 0


def cmd_scan(args, root, cfg):
    issues = load_issues(root, cfg)
    conflicts = find_conflicts(issues)
    locks = live_locks(root)
    stale = [
        i.id
        for i in issues
        if i.meta.get("status") == "in_progress"
        and (not i.meta.get("worktree") or not Path(i.meta["worktree"]).exists())
    ]
    # 待决策 = 待批准(proposed) + 待验收(in_review)。以前漏了 proposed，
    # 而巡检器最重要的职责就是提醒"有一条等你点头"（Codex F-16 / E-06）。
    awaiting_approval = [i.id for i in issues if i.meta.get("status") == "proposed"]
    awaiting_review = [i.id for i in issues if i.meta.get("status") == "in_review"]
    awaiting = awaiting_approval + awaiting_review
    orphan_locks = [l for l in locks if l.get("age_s", 0) > LOCK_TTL]
    kinds: dict[str, int] = {}
    for i in issues:
        k = i.meta.get("kind") or "unclassified"
        kinds[k] = kinds.get(k, 0) + 1
    merged_no_visual = sorted(
        i.id for i in issues
        if i.meta.get("status") == "merged" and (i.meta.get("kind") or "unclassified") in
        set(policy_list(cfg, "visual_required_for", ["engine"]))
        and not gate_evidence(i, tier="g2")
    )
    payload = {
        "project": cfg["project"],
        "awaiting_human": sorted(awaiting),
        "awaiting_approval": sorted(awaiting_approval),
        "awaiting_review": sorted(awaiting_review),
        "stale": sorted(stale),
        "conflicts": sorted([f"{a}×{b}:{s}" for a, b, s in conflicts]),
        "locks": sorted([f"{l['resource']}@{l.get('issue')}" for l in locks if l.get("age_s", 0) <= LOCK_TTL]),
        "orphan_locks": sorted([l["resource"] for l in orphan_locks]),
        "in_flight": sorted([i.id for i in issues if i.meta.get("status") == "in_progress"]),
        "blocked": sorted([i.id for i in issues if i.meta.get("status") == "blocked"]),
        "kinds": dict(sorted(kinds.items())),
        "merged_engine_without_visual": merged_no_visual,
    }
    if args.json:  # 必须确定性：不含时钟，monitor 才能靠"输出变了"唤醒
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    print(f"待你决策 {len(payload['awaiting_human'])}: " + (", ".join(payload["awaiting_human"]) or "-"))
    print(f"在跑     {len(payload['in_flight'])}: " + (", ".join(payload["in_flight"]) or "-"))
    print(f"卡住     {len(payload['blocked']) + len(payload['stale'])}: "
          + (", ".join(payload["blocked"] + payload["stale"]) or "-"))
    if conflicts:
        print("写作用域冲突:")
        for a, b, s in conflicts:
            print(f"  {a} × {b}: {s}")
    return 0


def cmd_approve(args, root, cfg):
    iss = load_issue_by_id(root, cfg, args.id)
    m = iss.meta
    if m.get("status") not in ("proposed", "blocked"):
        die(f"{iss.id} 状态是 {m.get('status')}，只有 proposed/blocked 可以批准")
    if not iss.meta.get("write_scope"):
        die(f"{iss.id} 没有 write_scope —— 批准前必须先把写作用域写清楚")
    if not iss.meta.get("verification"):
        die(f"{iss.id} 没有可执行验收断言（verification）—— 批准前必须补")
    pol = policy_cfg(cfg)
    if pol.get("require_executable_assertions", True):
        bad = validate_verification(iss.meta.get("verification"))
        if bad:
            die(f"{iss.id} 的验收断言不合格（写不出命令 = 需求没想清楚）：\n  - "
                + "\n  - ".join(bad)
                + "\n  合法形状：`A1: {py} -m pytest -q tests/test_x.py` 或 `A2: MANUAL: by=我 how=看渲染图标题是否居中`")
    kind = (m.get("kind") or "unclassified").strip()
    if kind not in KINDS:
        die(f"{iss.id} 的 kind='{kind}' 不合法，只能是：{', '.join(KINDS)}")
    if pol.get("require_kind", True) and kind == "unclassified":
        die(f"{iss.id} 没有分类（kind）—— 批准前在 front-matter 补一行："
            f"kind = \"engine|measure|process|hygiene\"（engine=改产品，measure=修尺子）")
    if kind == "engine" and pol.get("require_metric_for_engine", True) and not (m.get("moves_metric") or "").strip():
        die(f"{iss.id} 是 engine 类却没有 moves_metric —— 这条改动想让哪个指标动？"
            f"补 `moves_metric = \"<指标名>\"`（真的不动就写 kind=\"hygiene\"）")
    # 0.7.3（issue #3 phase 1）：g2 块的阈值必须带基线证据对——approve 时就拦，
    # 不要等到 merge 才发现阈值是拍脑袋的。
    g2lint = lint_g2_baseline(iss.body)
    if g2lint:
        die(f"{iss.id} 的 g2 块阈值缺基线证据对：\n  - "
            + "\n  - ".join(g2lint)
            + "\n  在 g2 块里补 baseline.<键> = <改动前实测值> 与 baseline_source；机器化走 wfk calibrate")
    # 0.7.3（issue #2）：盲区显式化——approve 的人必须看见"本卡不验什么"。
    nc = str(m.get("not_covered") or "").strip()
    if nc:
        print(f"  not_covered: {nc[:100]}")
    else:
        print("  not_covered: （空——开工前补一行：本卡故意不验什么；merge 时必填非空）")
    # 北星指标白名单（0.6.0）：moves_metric 必须是 [metrics] 声明过的真实指标。
    # 自由文本的后果：每条 issue 各造一个指标名，月度归因表对不上账（方案 §A1）。
    # 声明 `"xxx.*" = "…"` 表示接受该前缀（corpus.fail_cluster.<判定项> 这类派生指标）。
    metrics = [str(k) for k in (cfg.get("metrics") or {}).keys()]
    mm = (m.get("moves_metric") or "").strip()
    if mm and metrics:
        ok_metric = mm in metrics or any(
            k.endswith(".*") and mm.startswith(k[:-1]) for k in metrics)
        if not ok_metric:
            die(f"{iss.id} 的 moves_metric='{mm}' 不在 [metrics] 白名单里：\n"
                f"  已声明：{', '.join(metrics)}\n"
                f"  改用已声明指标，或先在 workflow.toml [metrics] 里声明它——"
                f"对不上账的指标名等于没写。")
    # 批准 = 第二个 issue 真正进入并行的时刻，这里必须硬拦写作用域重叠。
    # 但**显式声明了串行的**（任一方 blocked_by 另一方）必须放行——否则协议里
    # "加 blocked_by 串行"这句话根本执行不了（2026-11-18 实测：三条同改 new_actions.py
    # 的 issue 互相锁死，只能靠手改工具）。
    mine = list(m.get("blocked_by") or [])
    for o in load_issues(root, cfg):
        if o.id == iss.id or o.meta.get("status") not in ACTIVE:
            continue
        theirs = list(o.meta.get("blocked_by") or [])
        if o.id in mine or iss.id in theirs:
            continue                      # 已声明串行，不算并行冲突
        ov = scope_overlap(iss.meta.get("write_scope", []), o.meta.get("write_scope", []))
        if ov:
            die(f"{iss.id} 与在飞的 {o.id} 写作用域重叠（{ov}）—— 串行：给 {iss.id} 加 blocked_by，或改 scope")
    m["status"] = "approved"
    iss.save(meta=m)
    print(f"{iss.id} → approved（可以 wfk start）")
    return 0


def check_blockers(iss: "Issue", root: Path, cfg: dict) -> None:
    """开工前检查 blocked_by：依赖没做完就不许 start。"""
    ids = list(iss.meta.get("blocked_by") or [])
    if not ids:
        return
    st = {i.id: i.meta.get("status") for i in load_issues(root, cfg)}
    undone = [x for x in ids if st.get(x) not in ("merged", "dropped", None)]
    missing = [x for x in ids if x not in st]
    if missing:
        die(f"{iss.id} 的 blocked_by 里有不存在的 issue：" + ", ".join(missing))
    if undone:
        die(f"{iss.id} 还有未完成的阻塞项：" + ", ".join(f"{x}={st.get(x)}" for x in undone)
            + f"\n  依赖完成后再 start；或用 wfk unblock {iss.id} 显式解开。")


def cmd_start(args, root, cfg):
    iss = load_issue_by_id(root, cfg, args.id)
    m = iss.meta
    if m.get("status") not in ("approved", "in_progress", "blocked"):
        die(f"{iss.id} 状态是 {m.get('status')}，只有 approved 才能开工（你确认过再改状态）")
    check_blockers(iss, root, cfg)
    repo = m.get("repo", "main")
    rp = repo_path(root, cfg, repo)
    branch = m.get("branch") or f"feat/{iss.id}"
    wt = wt_path(root, cfg, repo, iss.id)
    retry = False
    if wt.exists():
        # 上次 bootstrap 失败留下的 worktree：允许原地重试，不许无声复用（那会掩盖半装好的环境）
        if m.get("status") == "blocked" and (wt / ".git").exists():
            retry = True
            print(f"[retry] worktree 已存在（上次 bootstrap 失败），重跑 bootstrap：{wt}")
        else:
            die(f"worktree 已存在：{wt}（要重建先 wfk finish 或手工 git worktree remove）")
    base = default_branch(cfg, repo)
    if not retry:
        r = subprocess.run(
            ["git", "-C", str(rp), "worktree", "add", "-b", branch, str(wt), base],
            capture_output=True,
            text=True,
        )
        if r.returncode != 0:
            die(f"git worktree add 失败：{r.stderr.strip()}")
    if m.get("status") in ("in_progress", "blocked"):
        # 回工自增（2026-11-18）：以前 rework 永远 0，"回工率"是假的。
        # 口径：非首次开工（含 bootstrap 失败后的原地重试）= 一次回工。
        m["rework"] = int(m.get("rework") or 0) + 1
        print(f"[rework] {iss.id} 第 {m['rework']} 次回工")
    m.update({"status": "in_progress", "branch": branch, "worktree": str(wt),
              "owner": args.owner or os.environ.get("WF_OWNER", "")})
    iss.save(meta=m)
    (wt / ".wf").mkdir(exist_ok=True)
    (wt / ".wf" / "issue.json").write_text(json.dumps({"id": iss.id, "path": str(iss.path)}, ensure_ascii=False), encoding="utf-8")
    # gitignored 的运行时数据不会跟过来（output/ 里几千个样张），缺了它们全量会假红。
    # 用 junction 指回主仓：worktree 里能读，写也落在同一处（与既有测试的绝对路径行为一致）。
    for link in cfg.get("worktree", {}).get("links", []):
        target = (root / link).resolve()
        dst = wt / Path(link).name
        if dst.exists() or not target.exists():
            continue
        r = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(dst), str(target)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        print(f"[link] {dst.name} → {target}" + ("" if r.returncode == 0 else "（失败，可手动建）"))
    for step in cfg.get("bootstrap", {}).get("steps", []):
        # 0.6.3/R08：bootstrap 阶段还没有实际 diff，退回保守 scope 交集——能证明不相交才跳过。
        # 旧写法拿声明 scope 当路径互测，声明 src/** + 条件 src/api/** 会被判"不涉及"。
        if step.get("when_scope") and (m.get("write_scope") or []):
            ws_pairs = [(s, w) for s in (m.get("write_scope") or []) for w in step["when_scope"]]
            if all(_scopes_provably_disjoint(s, w) for s, w in ws_pairs):
                continue
        # bootstrap 失败必须中止（以前不检查返回值，start 照样返回 0，
        # 工人进到一个依赖没装好的 worktree，之后的失败全算到代码头上——Codex F-02）
        if run_step(root, cfg, wt, iss, step) == "fail":
            m["status"] = "blocked"
            m["blocked_reason"] = f"bootstrap 失败：{step['name']}"
            iss.save(meta=m)
            die(f"bootstrap 步骤 {step['name']} 失败，worktree 已建但不可用：{wt}\n"
                f"  日志：.wf/gate/ 下最新一份 {iss.id}-{step['name']}-<时间戳>.log\n"
                f"  修好后重跑：wfk start {iss.id} --owner <你>（会原地重试 bootstrap）")
    ports = cfg.get("runtime", {}).get("ports", [])
    if ports:
        print(f"[ports] 本 worktree 独占端口：{ports[0]}（本地服务）")
    print(f"worktree {wt}\nbranch   {branch}")
    print(f"下一步：cd {wt}  → wfk gate {iss.id} g0")
    return 0


def load_issue_by_id(root, cfg, issue_id: str) -> Issue:
    for i in load_issues(root, cfg):
        if i.id.lower() == issue_id.lower():
            return i
    die(f"账本里没有 {issue_id}")


def tree_of(cfg: dict, repo: Path, rev: str = "HEAD") -> str:
    """证据里记录的 `tree=` 必须全走这里：排除账本目录的源码树。"""
    return commit_tree(repo, rev, exclude=str(cfg.get("issue_dir", "docs/issues")))


def run_step(root: Path, cfg: dict, wt: Path, iss: Issue, step: dict, doc: str = "",
             template: str = "", tier: str = "") -> str:
    """跑一步门禁。返回 "pass" / "fail" / "skip"。

    2026-11-18：**skip 不再等于 pass**。以前缺 --doc 时直接 return True，
    等于"没验也算过"（E-04 的一条）。现在跳过会写一条 SKIP 证据，且不计入完成矩阵。
    """
    sha = short_sha(wt)
    # 证据层名一律用 TOML 键形（下划线）：CLI 收 g2.5（TIER_RE 只认点号），但
    # tier_steps 查表按 g2_5、compute_readiness 的 required_tiers 也是 g2_5——
    # 写证据不归一就会「步骤真跑过、矩阵认不到」（0.6.1：EZI-0053 首飞 g2.5 实撞）。
    tier = (tier or "gate").replace(".", "_")
    tree = tree_of(cfg, wt)
    cfgid = cfg_blob(wt)

    def _skip(why: str) -> str:
        print(f"  skip {step['name']}（{why}）")
        ev = iss.meta.setdefault("evidence", [])
        line = f"G:{tier or 'gate'}:{step['name']} SKIP reason={why} @{sha} cfg={cfgid}"
        if line not in ev:
            ev.append(line)
            iss.save(meta=iss.meta)
        return "skip"

    scope = iss.meta.get("write_scope", [])
    if step.get("when_scope"):
        # 0.6.3/R08：条件按**实际改动文件**计算——声明 scope 之间互测会把真实相关改动
        # 误判成"不涉及"（声明 src/**、实际改 src/api/new.py、步骤条件 src/api/**）。
        _files_now = changed_files(wt, default_branch(cfg, iss.meta.get("repo", "main")))
        _involved = any(match_any(f, step["when_scope"]) for f in _files_now)
        if not _involved:
            return _skip(f"不涉及 {step['when_scope']}")
    cmd = step["cmd"]
    if "{doc}" in cmd and not doc:
        # 0.7.3（issue #5）：卡就在账本目录里、issue 对象已在手——路径不用人喂。
        # 旧行为是 skip + 命令链 exit 0，`wfk gate <id> g0 && … && g2` 会一路绿过去。
        doc = str(iss.path)
        if tier.startswith("g2") and not g2_blocks_of(iss.body or ""):
            # 无 g2 块的卡在这层必然跑不出断言——明确红（配置/卡错误），不是 skip。
            print(f"  FAIL {step['name']}：卡里没有 ```g2 块，{tier} 层无从断言（这是卡错误，不是 skip）")
            ev = iss.meta.setdefault("evidence", [])
            line = f"G:{tier or 'gate'}:{step['name']} FAIL reason=no-g2-block @{sha} cfg={cfgid}"
            if line not in ev:
                ev.append(line)
                iss.save(meta=iss.meta)
            return "fail"
    if "{template}" in cmd and not template:
        return _skip("需要 --template")
    files = changed_files(wt, default_branch(cfg, iss.meta.get("repo", "main"))) if wt.exists() else []
    tests_sel = pick_tests(root, cfg, wt, files) if "{tests}" in cmd else ""
    # 全量在 worktree 里会假红（缺 gitignored 样张、缺按 ROOT.parent 解析的兄弟仓库），
    # 所以当某步展开成「全部测试」时改在主仓跑——但仅当分支已合入默认分支，
    # 否则等于拿主仓的旧代码跑出门禁绿（比不跑更危险）。
    base_dir = wt
    if tests_sel and tests_sel == cfg.get("tests", {}).get("default", "tests") \
            and cfg.get("gate", {}).get("full_suite_in") == "main":
        repo_key = iss.meta.get("repo", "main")
        rp = repo_path(root, cfg, repo_key)
        branch = iss.meta.get("branch") or f"feat/{iss.id}"
        merged = subprocess.run(
            ["git", "-C", str(rp), "-c", "core.quotepath=false",
             "merge-base", "--is-ancestor", branch, default_branch(cfg, repo_key)],
            capture_output=True,
        ).returncode == 0
        if merged:
            base_dir = rp
            print("  [cwd] 该步骤展开为全部测试，且分支已合入默认分支 → 改在主仓跑")
        else:
            print("  [warn] 该步骤是全部测试，但分支未合入默认分支：worktree 里跑全量会因缺样张/兄弟仓库假红")
    # 含空格的路径（D:\Microsoft VS Code Projects\...）在 shell=True 下必须带引号
    cmd = (
        cmd.replace("{py}", f'"{venv_python(root)}"')
        .replace("{tests}", tests_sel)
        .replace("{doc}", f'"{doc}"')
        .replace("{template}", f'"{template}"')
        .replace("{issue}", f'"{iss.path}"')
        .replace("{id}", iss.id)
        .replace("{wt}", f'"{wt}"')
        .replace("{root}", f'"{root}"')
    )
    cwd = base_dir / step["cwd"] if step.get("cwd") else base_dir
    lock = None
    if step.get("serial"):
        lock = Lock(root, step["serial"], iss.id)
        lock.acquire()
    started = time.time()
    print(f"  run  {step['name']}: {cmd}")
    # 0.6.2（外部评审 R14）：步骤 env 与 verify 同一构造——空值=从环境里删除该变量
    # （PYTHONPATH="" 等文档写法从此在 gate 真正生效），非空=覆盖。
    env = dict(os.environ)
    for k, v in (step.get("env") or {}).items():
        if v == "":
            env.pop(k, None)
        else:
            env[k] = str(v)
    env.setdefault("PYTHONIOENCODING", "utf-8")
    # 0.6.2（外部评审 R09/F02）：受测输入的指纹在执行前后必须一致——命令退出 0
    # 但中途改了工作区 = 这次运行**无效**（证据不写 clean=yes），合并闸按 FAIL 对待。
    # 修测试的「修复器」、代码生成器、另一会话误写，都属于这一类。
    # 指纹排除账本目录与 .wf（与门禁前置同口径）：并行会话写账本/日志不算动受测输入。
    _fp_ignore = (str(cfg.get("issue_dir", "docs/issues")).rstrip("/") + "/", ".wf/")
    def _wt_fingerprint(bd: Path) -> tuple[str, str]:
        head_fp = _git(bd, "rev-parse", "HEAD").stdout.strip()
        st_fp = "|".join(worktree_dirty(bd, _fp_ignore))
        return head_fp, st_fp
    pre_head, pre_status = _wt_fingerprint(base_dir)
    try:
        r = subprocess.run(
            cmd,
            cwd=str(cwd),
            shell=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            timeout=step.get("timeout") or None,
        )
        out = (r.stdout or "") + (r.stderr or "")
        ok = r.returncode == 0
    except subprocess.TimeoutExpired as e:
        out = (e.stdout or "") + (e.stderr or "") if isinstance(e.stdout, str) else ""
        ok = False
    finally:
        if lock:
            lock.release()
    post_head, post_status = _wt_fingerprint(base_dir)
    tree_moved = (post_head, post_status) != (pre_head, pre_status)
    if tree_moved:
        ok = False
        out += ("\n[wfk] 执行前后工作区指纹不一致"
                f"（HEAD {pre_head[:8]}→{post_head[:8]}，status {pre_status}→{post_status}）："
                "受测输入被本步骤或并行会话改动，本次运行无效（外部评审 F02）。\n")
    dur = int(time.time() - started)
    # 成本自记账（2026-11-18）：以前 cost_wall_min 永远 0，stats 的中位墙钟是摆设。
    # 门禁本来就测得到每步耗时，这里按秒累加、展示按分钟取整。
    iss.meta["cost_wall_min"] = int(iss.meta.get("cost_wall_min") or 0) + max(1, round(dur / 60))
    logdir = root / ".wf" / "gate"
    logdir.mkdir(parents=True, exist_ok=True)
    # run-id 日志（0.6.0）：固定名会被重跑覆盖，账本里的 log= 指针指向的可能是
    # 另一次运行的内容——审计链在时间维度断裂（外部评审 E-09 残留）。
    # 文件名按时间戳排序即按新旧排序，旧证据永远指向旧文件，不被覆盖。
    # pid 后缀防同秒碰撞（脚本化重跑）。
    runid = f"{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}"
    log = logdir / f"{iss.id}-{step['name']}-{runid}.log"
    header = (f"# tier={tier or '-'} step={step['name']} commit={sha} tree={tree} "
              f"cfg={cfgid} exit={0 if ok else 1} dur={dur}s\n")
    log.write_text(header + f"$ {cmd}\n\n{out}", encoding="utf-8")
    # 轮转：只清同一步骤的旧日志，保最近 N 份（[gate].log_keep，默认 20）；
    # 刚写的这份永远在保留名单里。keep=0 = 不留历史。
    try:
        keep = int((cfg.get("gate") or {}).get("log_keep") or 20)
    except (TypeError, ValueError):
        keep = 20
    if keep >= 0:
        olds = sorted(p for p in logdir.glob(f"{iss.id}-{step['name']}-*.log") if p != log)
        for p in (olds if keep == 0 else olds[:-keep]):
            try:
                p.unlink()
            except OSError:
                pass
    tail = "\n".join(out.strip().splitlines()[-12:])
    print(f"  {'PASS' if ok else 'FAIL'} ({dur}s) → {log.relative_to(root)}")
    if tail:
        print("    " + tail.replace("\n", "\n    "))
    ev = iss.meta.setdefault("evidence", [])
    # 证据格式（2026-11-18）：commit + tree + clean + cfg 四件套，合并闸按它复核。
    # 老格式（只有 @sha）不再被认作有效证据——补提交/脏树都会被它掩盖。
    common = (f"@{sha} tree={tree} cfg={cfgid} wfk={__version__} "
              f"log={log.relative_to(root).as_posix()}")
    if ok:
        line = f"G:{tier or 'gate'}:{step['name']} PASS {dur}s clean=yes {common}"
    else:
        line = f"G:{tier or 'gate'}:{step['name']} FAIL {dur}s {common}"
    # 同层同步骤同提交重跑 = 覆盖旧行，不是追加（0.6.0：日志带 run-id 后整行必然
    # 不同，靠「整行相等」去重失效，会堆出一串只有 log= 不同的重复证据）。
    # 顺带清掉该步的 SKIP 行：真跑过就取代「缺 --doc 跳过」的占位。
    prefix = f"G:{tier or 'gate'}:{step['name']} "
    ev[:] = [e for e in ev if not (str(e).startswith(prefix) and f"@{sha}" in str(e))]
    ev.append(line)
    iss.save(meta=iss.meta)
    return "pass" if ok else "fail"


def evidence_valid(line: str, head: str, tree: str) -> bool:
    """一条证据是否对「这个提交的这棵树 + 这一版尺子」有效。

    `wfk=` 是 0.5.0 加的：工具改过之后，旧证据必须失效——否则"尺子变了、
    结论还算数"，这正是我们在 0037 事故里踩过的坑。
    """
    return (f"@{head}" in line and " PASS " in line
            and "clean=yes" in line and f"tree={tree}" in line
            and f"wfk={__version__}" in line)


EVIDENCE_RE = re.compile(r"^G:([^:]+):(.+?) (PASS|FAIL|SKIP)\b")


def tier_state(iss: "Issue", cfg: dict, tier: str, head: str, tree: str,
              cfgid: str = "", changed: list[str] | None = None) -> tuple[str, list[str]]:

    """**逐步骤**判定一层是否完成。返回 (ok|incomplete, problems)。

    2026-11-18 修：以前一层里只要有**任意一条** PASS 就算过，于是
    「compileall PASS + tests-map FAIL」会显示 g0 OK —— 一层里的失败步骤被通过步骤掩盖
    （EZI-0043 工人实测撞到）。现在要求该层配置的**每个步骤**都有当前树上的 PASS。
    """
    steps = tier_steps(cfg, tier)
    if not steps:
        return "incomplete", [f"{tier}：配置里没有步骤（空层不能当绿灯）"]
    # 读侧同归一（0.6.1）：required_tiers 里 g1.5（点号）与 g2_5（下划线）两种记法
    # 并存，证据一律按 TOML 键形（下划线）写——读必须同形，否则点号层认不到新证据。
    recs: dict[str, list[tuple[str, str]]] = {}
    raw: dict[str, list[str]] = {}
    # 0.6.2（外部评审 R15/F15）：证据有效性核心改 **tree 绑定**——同一源码树上的
    # PASS 永远有效，sha 只做溯源。旧实现按分支 HEAD sha 硬过滤：工人补一条纯账本
    # 提交（源码树一字未动）就把整套有效证据烧成过期，被迫整梯次重跑（EZI-0053
    # 三轮重跑的痛）。账本目录本就排除在 tree 指纹外，账本提交不再烧证据；
    # 改源码 → tree 变 → 证据照旧失效。
    for line in gate_evidence(iss, tier=tier.replace(".", "_"), sha=""):
        m = EVIDENCE_RE.match(line)
        if not m:
            continue
        name, kind = m.group(2), m.group(3)
        raw.setdefault(name, []).append(line)
        if kind != "SKIP" and not (f"tree={tree}" in line and "clean=yes" in line
                                   and (f"cfg={cfgid}" in line if cfgid else True)
                                   and " log=" in line
                                   and f"wfk={__version__}" in line):
            # 失效，评审 R12/R18）；log= 必填（PASS 无日志指针 = 不可回放，R13）。
            continue          # 旧格式 / 别的树 / 别的尺子版本的证据都不算
        recs.setdefault(name, []).append((kind, line))
    problems: list[str] = []
    for s in steps:
        name = s.get("name") or "?"
        got = recs.get(name) or []
        if any(k == "PASS" for k, _ in got):
            continue
        # 0.6.3/R08："不涉及" SKIP 按**当前实际改动文件**重算——当初跳过的步骤，
        # 后来改动蔓延进了它的 when_scope 就必须补跑（声明 scope 互测的老口径已废）。
        _ws = s.get("when_scope")
        if _ws:
            _involved = bool(changed) and any(path_matches(f, w)
                                              for f in (changed or []) for w in _ws)
            if not _involved and any(k == "SKIP" and "不涉及" in line
                                     for k, line in got):
                continue
        elif any(k == "SKIP" and "不涉及" in line for k, line in got):
            continue          # 无 when_scope 的历史遗留 SKIP，宽容放行
        if any(k == "FAIL" for k, _ in got):
            problems.append(f"{tier}/{name}：失败（FAIL @{head}）")
        elif any(k == "SKIP" for k, _ in got):
            problems.append(f"{tier}/{name}：被跳过（缺 --doc/--template），不计入完成")
        else:
            vers = sorted({m.group(1) for line in (raw.get(name) or [])
                           for m in [re.search(r"wfk=([\w.]+)", line)] if m})
            if vers and any(v != __version__ for v in vers):
                problems.append(f"{tier}/{name}：证据来自 wfk {'/'.join(vers)}"
                                f"（当前 {__version__}），尺子变了必须重跑该层")
            else:
                problems.append(f"{tier}/{name}：缺 @{head} 的有效证据（未跑 / 已过期 / 来自脏树）")
    return ("ok" if not problems else "incomplete"), problems


def assertion_coverage(iss: "Issue", cfg: dict, head: str, tree: str,
                       cfgid: str = "") -> list[str]:
    '''验收断言覆盖（0.6.3/F12，评审 R19）：merge 时每条可执行断言必须绑定一个
    有当前树有效证据的门禁步骤（``@step:<层>.<步骤>``），或写成 MANUAL 回执。
    旧实现 merge 只看 gate 层覆盖率，验收断言本身是不是真跑过没人管。
    0.7.3（issue #1）：**先复查断言正文质量再查绑定**——正文校验原来只在 approve 跑，
    approve 之后卡是纯文本随手可改，`A1: true @step:g1.unit`（步骤真实存在）能一路绿；
    现在把 validate_verification 在 merge 侧重跑，approve 后的降级改卡到此为止。
    '''
    problems: list[str] = []
    problems.extend(validate_verification(iss.meta.get("verification")))
    for raw in iss.meta.get("verification") or []:
        text = str(raw)
        m = ASSERT_RE.match(text)
        if not m:
            problems.append(f"断言缺 A<编号>: 前缀 → {text[:60]}")
            continue
        body = m.group(2).strip()
        if body.upper().startswith("MANUAL:"):
            _by = re.search(r"by\s*=\s*([^\s]+)", body)
            _how = re.search(r"how\s*=\s*(.+\S)", body)
            if not _by or not _how:
                problems.append(f"A{m.group(1)} MANUAL 回执 by=/how= 不完整 → {body[:60]}")
            continue
        mb = re.search(r"@step:\s*([A-Za-z0-9_]+)\.([A-Za-z0-9_-]+)", body)
        if not mb:
            problems.append(f"A{m.group(1)} 可执行断言未绑 @step:<层>.<步骤> → {body[:60]}")
            continue
        tier, step = mb.group(1), mb.group(2)
        tier_toml = tier.replace(".", "_")
        names = {s.get("name") for s in tier_steps(cfg, tier_toml)}
        if names and step not in names:
            problems.append(f"A{m.group(1)} 绑定的步骤 {tier}.{step} 不在 workflow.toml 配置里")
            continue
        valid = False
        for line in gate_evidence(iss, tier=tier_toml, sha=""):
            em = EVIDENCE_RE.match(line)
            if not em or em.group(2) != step:
                continue
            if em.group(3) != "PASS":
                continue
            if f"tree={tree}" not in line or "clean=yes" not in line:
                continue
            if cfgid and f"cfg={cfgid}" not in line:
                continue
            if " log=" not in line:
                continue
            valid = True
            break
        if not valid:
            problems.append(f"A{m.group(1)} 绑定的 {tier}.{step} 在当前树上没有有效 PASS 证据")
    return problems


def compute_readiness(iss: "Issue", cfg: dict, head: str, tree: str,
                  cfgid: str = "", changed: list[str] | None = None):
    """完成矩阵：**每一层的每个步骤**都要有「当前 HEAD + 当前树 + 干净」的 PASS。


    返回 (ok, missing[说明], waived[说明])。merge 与 gate 共用这一处判断。
    """
    kind = iss.meta.get("kind") or "unclassified"
    need = required_tiers(cfg, kind)
    visual_kinds = policy_list(cfg, "visual_required_for", ["engine"])
    missing: list[str] = []
    waived: list[str] = []
    for tier in need:
        state, problems = tier_state(iss, cfg, tier, head, tree, cfgid, changed)
        if state == "ok":
            continue
        if tier == "g2" and kind in visual_kinds and (iss.meta.get("visual_skipped_by") or "").strip():
            waived.append(f"{iss.id} 无 @{head} 的完整 G2 证据，{iss.meta['visual_skipped_by']} 已签字跳过——汇报里必须写明")
            continue
        missing.extend(problems)
    return (not missing), missing, waived


def _section_of(body: str, title: str) -> str:
    m = re.search(rf"^##\s+{re.escape(title)}\s*$(.*?)(?=^##\s+|\Z)", body, re.M | re.S)
    return (m.group(1) if m else "").strip()


def validate_close_text(body: str) -> list[str]:
    """按正文文本判「能不能关」。"""
    problems: list[str] = []
    decision = _section_of(body, "决策记录")
    evidence = _section_of(body, "验证证据")
    if not decision or len(decision) < 20 or decision.startswith("（"):
        problems.append("「决策记录」为空或过短（<20 字）")
    if not evidence or evidence.startswith("（"):
        problems.append("「验证证据」为空")
    return problems


def validate_close(iss: "Issue") -> list[str]:
    """关闭前必须齐的正文（合并前就要查，避免"代码进了主干、账本关不掉"）。"""
    problems = validate_close_text(iss.body)
    # 0.7.3（issue #2）：盲区显式化——not_covered 必须真写"本卡故意不验什么"。
    # 占位约定与本仓一致：以「（」开头的未填模板、空串、敷衍词都不算数。
    nc = str(iss.meta.get("not_covered") or "").strip()
    bare = nc.lstrip("（(").strip().rstrip("）。)").strip()
    if (not nc or nc.startswith("（") or nc.startswith("(") or not bare
            or bare.lower() in {"无", "none", "n/a", "-", "待填", "略"}):
        problems.append("not_covered 为空或敷衍——本卡故意不验什么，一行写出来（approve 的人要看）")
    # 0.7.3（issue #3 phase 1）：g2 阈值必须带基线证据对。
    problems.extend(lint_g2_baseline(iss.body))
    return problems


def gate_preflight(cfg: dict, iss: "Issue", wt: Path) -> None:
    """门禁前置：状态 / 干净树 / 配置漂移 / 改动面审计。任何一条不过都不许跑。"""
    m = iss.meta
    if m.get("status") not in ("in_progress", "in_review"):
        # in_review 也允许：补提交后旧证据失效，必须能重跑刷新（否则卡死）。
        die(f"{iss.id} 状态是 {m.get('status')}，门禁只能在 in_progress / in_review 上跑（先 wfk start）")
    if not wt.exists():
        die(f"{iss.id} 的 worktree 不存在：{wt}")
    base = default_branch(cfg, m.get("repo", "main"))
    ignore = (str(cfg.get("issue_dir", "docs/issues")).rstrip("/") + "/", ".wf/")
    dirty = worktree_dirty(wt, ignore)
    if dirty:
        die("工作区不干净，门禁拒绝执行（证据必须对应一棵确定的树）：\n  - "
            + "\n  - ".join(dirty[:10])
            + "\n  处理：git -C <worktree> commit / stash；探针写到 D:/Hermes/cache/scratch 而不是仓库里。")
    if config_drift(wt, base):
        why = (m.get("gate_config_change") or "").strip()
        if not why:
            die(f"本分支改了 {CFG_FILE} 但没有声明。\n"
                f"  这是刻意的摩擦：门禁配置就是尺子，尺子不能被测方悄悄改。\n"
                f"  要改就写进账本 front-matter：gate_config_change = \"<为什么改>\"，"
                f"合并时还要 gate_config_approved_by 签字。")
        print(f"[cfg] 分支改了 {CFG_FILE}（已声明：{why[:40]}）")
    problems = audit_changes(iss, changed_files(wt, base), cfg)
    if problems:
        die("改动面审计不通过：\n  - " + "\n  - ".join(problems))


def show_status(iss: "Issue", cfg: dict, wt: Path) -> int:
    head, tree = short_sha(wt), tree_of(cfg, wt)
    kind = iss.meta.get("kind") or "unclassified"
    need = required_tiers(cfg, kind)
    print(f"{iss.id}  status={iss.meta.get('status')}  kind={kind}  HEAD=@{head} tree={tree}")
    ok, missing, waived = compute_readiness(iss, cfg, head, tree, cfgid=cfg_blob(wt), changed=changed_files(wt, default_branch(cfg, iss.meta.get("repo", "main"))) if wt.exists() else [])
    for tier in need:
        state, problems = tier_state(iss, cfg, tier, head, tree, cfg_blob(wt), changed_files(wt, default_branch(cfg, iss.meta.get("repo", "main"))) if wt.exists() else [])
        steps = [s.get("name", "?") for s in tier_steps(cfg, tier)]
        mark = "OK  " if state == "ok" else "--  "
        print(f"  {mark}{tier:<5} 步骤={len(steps)}（{', '.join(steps)}）")
        for p in problems:
            print(f"         · {p}")
    for w in waived:
        print(f"  WAIVE {w}")
    print(f"ready_for_merge = {ok}")
    return 0 if ok else 2


TIER_RE = re.compile(r"^g\d+(\.\d+)?$")


def cmd_gate(args, root, cfg):
    if args.tier and args.tier != "status" and not TIER_RE.match(args.tier):
        die(f"未知门禁层 '{args.tier}'：只接受 g0/g1/g1.5/g2/g3… 或 status")
    iss = load_issue_by_id(root, cfg, args.id)
    # 门禁用「被测分支自己的」workflow.toml：改配置的 issue 才能验证自己。
    # 但配置漂移会被 preflight 拦下并要求声明 + 签字（见 gate_preflight）。
    wt = Path(iss.meta.get("worktree") or root)
    gate_cfg = load_cfg(wt) if (wt / CFG_FILE).is_file() else cfg
    if gate_cfg is not cfg:
        print(f"[cfg] 使用 worktree 的 workflow.toml（{wt.name}）")
    if args.tier == "status":
        return show_status(iss, gate_cfg, wt)
    gate_preflight(gate_cfg, iss, wt)
    kind = iss.meta.get("kind") or "unclassified"
    tiers = [args.tier] if args.tier else required_tiers(gate_cfg, kind)
    ok_all = True
    for tier in tiers:
        steps = tier_steps(gate_cfg, tier)
        if not steps:
            die(f"[{tier}] 没有配置任何步骤 —— 空门禁不能当绿灯（这是配置错误，不是 skip）")
        print(f"[{tier}] {iss.id}")
        for step in steps:
            res = run_step(root, gate_cfg, wt, iss, step, args.doc or "",
                           getattr(args, "template", "") or "", tier=tier)
            if res == "fail":
                ok_all = False
                break
        if not ok_all:
            break
    if not ok_all:
        print(f"{iss.id} gate 未通过；issue 状态不变")
        return 1
    head, tree = short_sha(wt), tree_of(cfg, wt)
    ok, missing, waived = compute_readiness(iss, gate_cfg, head, tree, cfgid=cfg_blob(wt), changed=changed_files(wt, default_branch(cfg, iss.meta.get("repo", "main"))) if wt.exists() else [])
    for w in waived:
        print(f"[warn] {w}")
    if not ok:
        print(f"{iss.id} 步骤都过了，但完成矩阵还缺（不翻状态）：")
        for x in missing:
            print(f"  - {x}")
        print(f"  看矩阵：wfk gate {iss.id} status")
        return 0
    if iss.meta.get("status") == "in_progress":
        iss.meta["status"] = "in_review"
        iss.save(meta=iss.meta)
        print(f"{iss.id} → in_review（矩阵齐：{' / '.join(tiers)}）")
    else:
        print(f"{iss.id} 矩阵齐（状态已是 {iss.meta.get('status')}）")
    return 0


def cmd_verify(args, root, cfg):
    """跑「全量验证」步骤（如 12 对语料 + 全量 pytest），日志落盘并给出可粘贴摘要。

    与 gate 的区别：gate 属于单个 issue 的准入门槛（会翻状态）；verify 是合并后/定时跑的
    全局健康检查，不绑定 issue、不翻任何状态，只回答"现在到底几分"。
    """
    steps = list(cfg.get("verify", {}).get("steps", []))
    if args.only:
        want = {s.strip() for s in args.only.split(",") if s.strip()}
        steps = [s for s in steps if s.get("name") in want]
    if not steps:
        print("[verify] workflow.toml 里没有 [[verify.steps]]（或 --only 没命中）")
        return 2
    ts = time.strftime("%Y%m%d-%H%M%S")
    outdir = root / ".wf" / "verify"
    outdir.mkdir(parents=True, exist_ok=True)
    results = []
    for step in steps:
        name = step.get("name") or "step"
        cmd = (
            step["cmd"]
            .replace("{py}", f'"{venv_python(root)}"')
            .replace("{root}", f'"{root}"')
            .replace("{ts}", ts)
        )
        env = dict(os.environ)
        for k, v in (step.get("env") or {}).items():
            if v == "":
                env.pop(k, None)
            else:
                env[k] = str(v)
        env.setdefault("PYTHONIOENCODING", "utf-8")
        log = outdir / f"{ts}-{name}.log"
        ok_exit = step.get("ok_exit", [0])
        print(f"[verify] {name}: {cmd}")
        t0 = time.time()
        with open(log, "w", encoding="utf-8", errors="replace") as fh:
            rc = subprocess.run(
                cmd, shell=True, cwd=str(root), stdin=subprocess.DEVNULL,
                stdout=fh, stderr=subprocess.STDOUT, env=env,
            ).returncode
        dur = time.time() - t0
        passed = rc in ok_exit
        results.append({"name": name, "rc": rc, "ok": passed, "dur": dur, "log": log})
        print(f"[verify] {name}: {'PASS' if passed else 'FAIL'} rc={rc} {dur:.1f}s → {log}")

    ok_all = all(r["ok"] for r in results)
    print("\n===== 摘要（可直接粘进账本/STATE）=====")
    for r in results:
        tail = []
        try:
            for line in Path(r["log"]).read_text(encoding="utf-8", errors="replace").splitlines():
                if line.strip():
                    tail.append(line.rstrip())
        except OSError:
            pass
        keep = tail[-3:]
        print(f"- {r['name']}: {'PASS' if r['ok'] else 'FAIL'}（{r['dur']:.1f}s）")
        for line in keep:
            print(f"    {line}")
    print(f"\n日志目录：{outdir}")
    return 0 if ok_all else 1


def cmd_calibrate(args, root, cfg):
    """0.7.3（issue #3 phase 2）：在当前树上跑测量步骤，落 CALIB 证据行。

    与 gate 的区别：**不判过/不过**（校准就是"把当前读数量下来"），不要求干净树
    （量基线常发生在开工前/半途——门禁的 clean 指纹在这里没有意义）。步骤命令带
    WFK_CALIBRATE=1，项目侧 harness 可据此切换测量输出模式。
    证据行前缀 ``C:``——gate_evidence/完成矩阵只认 ``G:``，天然不混流。
    合并闸只复核一件事：CALIB 行的 tree= 必须等于分支基树（基线必须是"改动前"读数）。
    """
    iss = load_issue_by_id(root, cfg, args.id)
    wt = Path(iss.meta.get("worktree") or root)
    gate_cfg = load_cfg(wt) if (wt / CFG_FILE).is_file() else cfg
    tier = (args.tier or "g2").replace(".", "_")
    steps = tier_steps(gate_cfg, tier)
    if not steps:
        die(f"[{tier}] 没有配置任何步骤——calibrate 测的就是这些步骤")
    sha = short_sha(wt)
    tree = tree_of(cfg, wt)
    cfgid = cfg_blob(wt)
    logdir = root / ".wf" / "gate"
    logdir.mkdir(parents=True, exist_ok=True)
    base = default_branch(cfg, iss.meta.get("repo", "main"))
    files = changed_files(wt, base) if wt.exists() else []
    for step in steps:
        cmd = step["cmd"]
        if "{tests}" in cmd:
            cmd = cmd.replace("{tests}", pick_tests(root, gate_cfg, wt, files))
        cmd = (cmd.replace("{py}", f'"{venv_python(root)}"')
               .replace("{doc}", f'"{args.doc or iss.path}"')
               .replace("{template}", f'"{getattr(args, "template", "") or ""}"')
               .replace("{issue}", f'"{iss.path}"')
               .replace("{id}", iss.id)
               .replace("{wt}", f'"{wt}"')
               .replace("{root}", f'"{root}"'))
        runid = f"{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}"
        log = logdir / f"{iss.id}-{step['name']}-calibrate-{runid}.log"
        env = dict(os.environ, WFK_CALIBRATE="1", PYTHONIOENCODING="utf-8")
        for k, v in (step.get("env") or {}).items():
            if v == "":
                env.pop(k, None)
            else:
                env[k] = str(v)
        print(f"[calibrate:{tier}] {step['name']}: {cmd}")
        with open(log, "w", encoding="utf-8", errors="replace") as fh:
            try:
                rc = subprocess.run(cmd, shell=True, cwd=str(wt), stdin=subprocess.DEVNULL,
                                    stdout=fh, stderr=subprocess.STDOUT, env=env,
                                    timeout=step.get("timeout") or None).returncode
            except subprocess.TimeoutExpired:
                fh.write("\n[wfk] calibrate 超时——按步骤 timeout 放宽或拆分测量\n")
                rc = -1
        print(f"[calibrate:{tier}] {step['name']} rc={rc} → {log.relative_to(root)}")
        ev = iss.meta.setdefault("evidence", [])
        line = (f"C:{tier}:{step['name']} CALIB @{sha} tree={tree} cfg={cfgid} "
                f"log={log.relative_to(root).as_posix()}")
        ev[:] = [e for e in ev if not str(e).startswith(f"C:{tier}:{step['name']} ")]
        ev.append(line)
        iss.save(meta=iss.meta)
    print(f"[calibrate] 完成。把测量读数写进卡：baseline.<键> = <实测值> + baseline_source = <怎么测的>")
    print(f"[calibrate] 合并闸将复核 CALIB 行的 tree= 是否为分支基树（基线必须是改动前读数）")
    return 0


def _git(rp: Path, *args: str):
    """跑 git；stdin=DEVNULL 防止任何交互式提问把进程挂住。

    core.quotepath=false 必须带：否则中文文件名会被转义成 \\345\\210\\240 形式，
    路径比较全部失效（实测踩过）。
    """
    return subprocess.run(
        ["git", "-C", str(rp), "-c", "core.quotepath=false", *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        stdin=subprocess.DEVNULL, check=False,
    )


def cmd_recover(args, root, cfg):
    """显式处置摆回即红残留标记：默认=安全尝试（v2 哈希预检自动还原），--force=强制。"""
    if args.force:
        recover_inflight_force(root)
        return 0
    recover_inflight_mutation(root, read_only=False)
    return 0


def _resolve_ledger_conflict(rp: Path, rel_issue: str, target: Path, out_scope: list | None = None) -> None:
    '''账本-only 冲突解决：front-matter 取 ours（主/base 侧）、正文取 theirs（分支侧）。

    从 index 阶段（:2:/:3:）取版本——工作区那份带冲突标记，tomllib 直接炸（实测踩过）。
    write_scope 取并集（EZI-0006：分支里补过的写作用域不能被 base 侧覆盖）。
    0.6.2 起同时供 cmd_merge 与合并候选树验证（F03）复用——两处语义必须一处定义。
    '''
    def _stage(n: int) -> str:
        return _git(rp, 'show', f':{n}:{rel_issue}').stdout

    ours, theirs = _stage(2), _stage(3)
    if not ours.startswith(FM) or not theirs.startswith(FM):
        die('取不到 index 阶段的账本版本（:2:/:3:）')
    _a, fm_ours, _body_ours = ours.split(FM, 2)
    _b, _fm_theirs, body_theirs = theirs.split(FM, 2)
    try:
        meta_ours = tomllib.loads(fm_ours)
        meta_theirs = tomllib.loads(_fm_theirs)
    except Exception as e:  # noqa: BLE001
        die(f'账本 front-matter 解析失败：{e}')
    scope = list(meta_ours.get('write_scope') or [])
    for s in (meta_theirs.get('write_scope') or []):
        if s not in scope:
            scope.append(s)
    if scope:
        meta_ours['write_scope'] = scope
    target.write_text(
        dump_front_matter(meta_ours) + chr(10) + body_theirs.lstrip(chr(10)),
        encoding='utf-8')
    _git(rp, 'add', rel_issue)
    r = _git(rp, 'commit', '-q', '--no-edit')
    if r.returncode != 0:
        die('提交合并失败：' + (r.stderr or r.stdout))


def card_at(repo: Path, rev: str, rel: str):
    """读某提交里的账本卡（不落盘）；读不到返回 None。"""
    r = _git(repo, "show", f"{rev}:{rel}")
    if r.returncode != 0 or not r.stdout.startswith(FM):
        return None
    try:
        _, fm, body = r.stdout.split(FM, 2)
        return tomllib.loads(fm), body
    except Exception:  # noqa: BLE001
        return None


def cmd_merge(args, root, cfg):
    """合并 issue 分支，并自动解决账本文件的合并冲突。

    背景：`wfk start` / `wfk gate` 会改写**主仓**账本（状态、owner、evidence），
    而工人在自己分支里也改同一份 md（决策记录、验证证据），直接 git merge 必冲突。
    本命令固定口径：front-matter 取主仓（保住门禁证据），正文取分支（保住工人的决策与自检）。
    """
    iss = load_issue_by_id(root, cfg, args.id)
    m = iss.meta
    if m.get("status") != "in_review":
        # 顺手把缺哪层打出来：只报"状态不对"会让人以为跑一次 gate 就行
        _rp0 = repo_path(root, cfg, m.get("repo", "main"))
        _br0 = m.get("branch") or f"feat/{iss.id}"
        try:
            _h = branch_sha(_rp0, _br0)
            _ok, _miss, _ = compute_readiness(iss, cfg, _h, tree_of(cfg, _rp0, _h), cfgid=cfg_blob(_rp0), changed=changed_files(_rp0, _br0, default_branch(cfg, m.get("repo", "main"))))
        except SystemExit:
            _ok, _miss = False, []
        tip = ("\n  完成矩阵还缺：\n  - " + "\n  - ".join(_miss)) if _miss else ""
        die(f"{iss.id} 状态是 {m.get('status')}，不能合并（应在 in_review）。{tip}"
            f"\n  看矩阵：wfk gate {iss.id} status")
    branch = m.get("branch") or f"feat/{iss.id}"
    repo_key = m.get("repo", "main")
    rp = repo_path(root, cfg, repo_key)
    base = default_branch(cfg, repo_key)

    rel_issue = None
    try:
        rel_issue = iss.path.relative_to(rp).as_posix()
    except ValueError:
        pass

    # base 在此冻结；期间主干被推进 = 候选结论作废。
    # -- 合并候选树验证（0.6.2/F03 + 0.6.3/R18）：验证「合并后的组合」。
    # 0.6.3 起候选树**无条件**建立：除跑 [gate.merge_check] 步骤外，还对比分支证据
    # 所用配置与合并后配置的必需层步骤定义——主干演进改过步骤命令时（R18），
    # 分支旧证据不能跟着进主干，必须先并主干重跑门禁。
    merge_steps = ((cfg.get('gate') or {}).get('merge_check') or {}).get('steps') or []
    base_sha = _git(rp, 'rev-parse', 'HEAD').stdout.strip()
    cand = root / '.wf' / ('merge-check-' + iss.id + '-' + time.strftime('%Y%m%d-%H%M%S'))
    r_add = subprocess.run(
        ['git', '-C', str(rp), '-c', 'core.quotepath=false',
         'worktree', 'add', '--detach', str(cand), base_sha],
        capture_output=True, text=True)
    if r_add.returncode != 0:
        die('候选树 worktree 建立失败：' + r_add.stderr.strip()[:200])
    try:
        r_merge = _git(cand, 'merge', '--no-ff', branch, '-m', f'merge-check: {iss.id}')
        if r_merge.returncode != 0:
            conflicted = [c.strip().strip(chr(34)) for c in
                          _git(cand, 'diff', '--name-only', '--diff-filter=U').stdout.splitlines()
                          if c.strip()]
            if rel_issue and conflicted == [rel_issue]:
                _resolve_ledger_conflict(cand, rel_issue, cand / rel_issue)
            else:
                die(f'{iss.id} 候选合并冲突（不是账本-only）：{conflicted[:5]}——合并前必须人工处理')
        # R18：合并后配置 vs 分支证据配置——必需层步骤定义必须一致
        _kind = iss.meta.get('kind') or 'unclassified'
        _wt_path = Path(m.get('worktree') or '')
        if _wt_path.is_dir() and (_wt_path / CFG_FILE).is_file():
            _cfg_branch = load_cfg(_wt_path)
            _cfg_cand = load_cfg(cand)
            for _tier in required_tiers(_cfg_branch, _kind):
                _sig_branch = _steps_signature(_cfg_branch, _tier)
                _sig_cand = _steps_signature(_cfg_cand, _tier)
                if _sig_branch != _sig_cand:
                    die(f'{iss.id} 合并后的门禁配置变了（{_tier} 步骤定义与分支证据所用配置不一致）——'
                        f'主干演进改过 workflow.toml，分支证据不能跟着进主干：'
                        f'先把 {base} 合进 {branch} 重跑门禁，或回退主干配置改动。')
        else:
            print('[merge-check] worktree 已不存在，跳过合并后配置一致性对比')
        # 候选配置身份（try 内计算——finally 会移除 worktree）
        _cand_cfg = load_cfg(cand)
        _cand_cfgid = cfg_blob(cand)
        if merge_steps:
            print(f'[merge-check] 候选树 {cand.name}（base @{base_sha[:8]} + {branch}）跑 {len(merge_steps)} 个步骤')
            for st in merge_steps:
                if run_step(root, cfg, cand, iss, st, tier='merge_check') == 'fail':
                    die(f'{iss.id} 合并候选树验证失败（步骤 ' + str(st.get('name')) + '）——'
                        f'两个单独合法的改动组合后不合法（外部评审 R10），先修组合问题再合并。')
        else:
            print('[merge-check] 未配置 [gate.merge_check] 步骤——候选组合语义未经验证（在 workflow.toml 配置后启用）')
    finally:
        subprocess.run(['git', '-C', str(rp), 'worktree', 'remove', '--force', str(cand)],
                       capture_output=True, text=True)
        subprocess.run(['git', '-C', str(rp), 'worktree', 'prune'], capture_output=True)
    if _git(rp, 'rev-parse', 'HEAD').stdout.strip() != base_sha:
        die(f'{iss.id} 合并检查期间主干被推进——候选结论作废，重新执行 wfk merge。')

    # ── 合并闸（2026-11-18）：按**分支 HEAD** 重算完成矩阵，不信状态字段 ──
    # 以前只查 engine 的 G2，于是「g1 在旧提交上通过 → 补提交 → 直接合并」无人拦（E-03）。
    head = branch_sha(rp, branch)
    tree = tree_of(cfg, rp, head)
    # 0.6.3/R08：merge 侧按 base...branch 的真实 diff 重算"不涉及"SKIP（工作树在 base 上，
    # changed_files(wt, base) 在这里只会得到空集——必须显式 diff 分支）。
    _changed = [l.strip() for l in _git(rp, "diff", "--name-only", f"{base}...{branch}").stdout.splitlines() if l.strip()]
    ok, missing, waived = compute_readiness(iss, _cand_cfg, head, tree, cfgid=_cand_cfgid, changed=_changed)
    if not ok:
        die(f"{iss.id} 合并前复核不通过（分支 HEAD @{head} tree={tree}）：\n  - "
            + "\n  - ".join(missing)
            + f"\n  补齐：wfk gate {iss.id} <层>；看矩阵：wfk gate {iss.id} status")
    # 0.6.3/F12（R19）：验收断言覆盖——每条可执行断言绑定当前树上有有效 PASS 的门禁步骤，
    # MANUAL 回执必须 by=/how= 齐全。断言层覆盖率此前无人验证。
    _acov = assertion_coverage(iss, _cand_cfg, head, tree, cfgid=_cand_cfgid)
    if _acov:
        die(f"{iss.id} 验收断言覆盖不通过：" + chr(10) + "  - "
            + chr(10) + "  - ".join(_acov)
            + chr(10) + "  可执行断言补 @step:<层>.<步骤> 绑定并跑 wfk gate，或改写 MANUAL: by= how= 回执")

    # 0.7.3（issue #2/#3）：关卡预检上移到 merge——validate_close 原来只在 finish 跑，
    # 代码已进主干才发现"账本关不掉"为时已晚。not_covered 与 g2 基线对在这里就拦。
    # 正文取**分支版**卡（两套协作惯例都覆盖：正文改主仓时分支版与主仓一致）。
    _close_view = None
    if rel_issue:
        _at = card_at(rp, branch, rel_issue)
        if _at:
            class _CardView:  # noqa: D401 - 轻量只读视图
                pass
            _close_view = _CardView()
            _close_view.meta, _close_view.body = _at
    _close_problems = validate_close(_close_view if _close_view is not None else iss)
    if _close_problems:
        die(f"{iss.id} 关卡预检不通过（合并前就要齐，避免代码进主干、账本关不掉）：\n  - "
            + "\n  - ".join(_close_problems)
            + "\n  补齐：决策记录/验证证据正文 + not_covered（本卡不验什么）+ g2 基线对")

    # 0.7.3（issue #3 phase 2）：CALIB 基线证据必须测在分支基树上——基线的语义就是
    # "改动前读数"；在分支尖上 calibrate 等于把阈值对齐到改动后的自己，基线失去意义。
    _calib = [str(e) for e in (iss.meta.get("evidence") or []) if str(e).startswith("C:")]
    if _calib:
        _base_tree = tree_of(cfg, rp, base)
        if not any(f"tree={_base_tree}" in c for c in _calib):
            die(f"{iss.id} 的 CALIB 基线证据不在分支基树（期望 tree={_base_tree}）——"
                f"基线必须是改动前读数：在基树上 wfk calibrate {iss.id} 重测。")
    for w in waived:
        print(f"[warn] {w}")

    # 证据指针可落地（0.6.0）：PASS 证据指向的日志必须真实存在，否则"可复核"是空话。
    # 0.6.2/F15：tree 绑定后有效证据可能在旧 sha 上——检查范围 = 当前树的有效行
    for line in gate_evidence(iss, tier="", sha=""):
        if " PASS " not in line:
            continue
        if not (f"tree={tree}" in line and "clean=yes" in line
                and f"wfk={__version__}" in line):
            continue          # 无效/过期行不检查（它们本来就不算数）
        mlog = re.search(r"log=(\S+)", line)
        if mlog and not (root / mlog.group(1)).is_file():
            die(f"{iss.id} 的 PASS 证据指向不存在的日志：{mlog.group(1)}\n"
                f"  日志被清理或未入盘——重跑该层拿回可复核证据：wfk gate {iss.id} <层>\n"
                f"  {line[:120]}")

    # 盲评时效（0.6.0）：评的是哪个提交，就只对那个提交有效。旧实现只存不校验：
    # A 提交上 PASS、分支推进到 B，也能带着旧结论合并（评审 E-10g 后半）。
    reviews = list(m.get("reviews") or [])
    if reviews and not (m.get('review_waived_by') or '').strip():
        # 0.6.2（外部评审 R11/F06）：同提交的**最新一条**评审定夺——旧实现只要
        # 历史上存在任意 PASS 就放行，后来的 BLOCK 被无视。
        at_head = [str(e) for e in reviews if f'@{head}' in str(e)]
        latest = at_head[-1] if at_head else ''
        if ':BLOCK' in latest:
            die(f'{iss.id} 当前提交 @{head} 的最新盲评是 BLOCK：{latest[:90]}' + chr(10) + '解除需要重新评审 PASS，或由用户在 front-matter 写 review_waived_by 显式豁免。')
        if not at_head:
            die(f'{iss.id} 有盲评记录，但没有绑定当前提交 @{head} 的任何结论——' + chr(10) + f'  重评：wfk review {iss.id} --verdict PASS --by <谁> --note <理由>' + chr(10) + '  或由用户在 front-matter 写 review_waived_by = <名字> 显式豁免（合并汇报里写明）。')
    else:
        # 0.6.2/F06：没有盲评记录 ≠ 盲评 PASS——如实显示未评审；要求盲评的种类由
        # [policy].review_required_kinds 控制（默认空 = 沿用签字链，不强制）。
        req_kinds = [str(k) for k in (policy_cfg(cfg).get('review_required_kinds') or [])]
        if (m.get('kind') or '') in req_kinds:
            die(f'{iss.id} 的 kind=' + str(m.get('kind')) + ' 要求盲评，但 reviews 为空——先 wfk review。')
        print('[review] 未评审（无盲评记录；本单正确性由门禁矩阵+签字链兜底）')
    wt = Path(m.get("worktree") or root)
    if config_drift(wt, base):
        stat = _git(rp, "diff", "--stat", f"{base}...{branch}", "--", CFG_FILE).stdout.strip()
        print(f"[cfg-drift] 本分支修改了 {CFG_FILE}：\n{stat}")
        why = (m.get("gate_config_change") or "").strip()
        approver = (m.get("gate_config_approved_by") or "").strip()
        if not why:
            die(f"{iss.id} 改了门禁配置却没有 gate_config_change 说明")
        print(f"[cfg-drift] 声明：{why[:60]}")
        if not approver:
            die(f"{iss.id} 改了门禁配置，但没有 gate_config_approved_by —— "
                f"需要用户签字才能合并（尺子不能被测方自己改）")
        print(f"[cfg-drift] 已由 {approver} 签字")

    # 关闭检查前移：避免"代码已进主干、账本却关不掉"。
    # 注意判定对象是**分支上的账本正文**：工人写的是他分支里那份，
    # 主仓这份要等 merge 才被"正文取分支"覆盖（2026-11-18 实测踩过：
    # 拿主仓占位符判定 → 合法交付被判"决策记录为空"）。
    rel = None
    try:
        rel = iss.path.relative_to(rp).as_posix()
    except ValueError:
        pass
    close_body = iss.body
    if rel:
        r_show = _git(rp, "show", f"{branch}:{rel}")
        if r_show.returncode == 0 and r_show.stdout.startswith(FM):
            try:
                close_body = r_show.stdout.split(FM, 2)[2]
            except IndexError:
                pass
    close_problems = validate_close_text(close_body)
    if close_problems:
        die(f"{iss.id} 关闭检查未过（先补齐再合并）：\n  - " + "\n  - ".join(close_problems))

    # 1) 主仓只允许账本文件脏（start/gate 会写它）；别的脏东西一律拒绝。
    #    未跟踪文件（?? 开头的构建产物、build-delta/ 之类）不拦合并——它们不影响 merge 结果。
    st = _git(rp, "status", "--porcelain")
    dirty = [
        line[3:].strip()
        for line in st.stdout.splitlines()
        if line.strip() and not line.startswith("??")
    ]
    # 账本目录下的脏文件都算"账本类"：finish 写 merged_sha、gate 写证据都会让它变脏，
    # 挡在合并前面只会逼人手工 commit（2026-11-18 实测：合并 0043 时被 0042 的账本挡住）。
    idir = str(cfg.get("issue_dir", "docs/issues")).rstrip("/")
    # 账本可能是"未跟踪"（仓库第一条 issue、或新账本还没进过版本库）；
    # `dirty` 为了放过构建垃圾已经滤掉了 ??，这里单独扫一遍，别漏掉未跟踪的账本。
    ledger_dirty = []
    for line in st.stdout.splitlines():
        if len(line) < 4:
            continue
        p = line[3:].strip().strip('"')
        if " -> " in p:
            p = p.split(" -> ", 1)[1].strip().strip('"')
        if p == rel_issue or p.startswith(idir + "/"):
            ledger_dirty.append(p)
    stray = [d for d in dirty if d not in ledger_dirty]
    if stray:
        die("主仓有未提交改动，先处理：" + ", ".join(stray[:6]))
    # 账本必须在合并前入账：它既可能是"改动过的已跟踪文件"，也可能是
    # "全新未跟踪文件"（仓库第一条 issue）。两种都要 git add，否则 merge 会因为
    # "untracked working tree files would be overwritten" 直接失败（2026-11-18 自测撞到）。
    if ledger_dirty:
        for f in ledger_dirty:
            _git(rp, "add", "--", f)
        staged = _git(rp, "diff", "--cached", "--name-only", "--", idir).stdout.strip()
        if staged:
            r = _git(rp, "commit", "-q", "-m", f"chore(ledger): {iss.id} 账本入账（状态/证据/merged_sha）")
            if r.returncode != 0:
                die("提交账本失败：" + (r.stderr or r.stdout))
            print(f"[ledger] {len(ledger_dirty)} 个账本文件已入账")

    # 2) 必须在默认分支上合
    cur = _git(rp, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    if cur != base:
        die(f"主仓当前在 {cur}，不是 {base}；切过去再合")

    res = _git(rp, "merge", "--no-ff", branch, "-m", f"merge: {iss.id} {m.get('title', '')}".strip())
    if res.returncode == 0:
        print(f"{iss.id} 已合并（{branch} → {base}）")
        # merge 回执（0.6.0）：finish 只认 wfk 留下的回执或人工合并签字，
        # 否则"手工 git merge + finish --merged"能把绕过合并闸的合并洗白（F-12 残留）。
        # 重读账本再写：合并刚把分支正文带进主仓，拿旧的 iss.body 存档会把工人正文打回占位符。
        fresh = Issue(iss.path)
        fresh.meta["merged_by"] = "wfk-merge"
        fresh.meta["merge_receipt"] = f"wfk@{short_sha(rp)}"
        fresh.save()
        print(f"下一步：wfk finish {iss.id} --merged --cleanup")
        return 0

    # 3) 自动解决：冲突只发生在账本文件时
    conflicted = [
        c.strip().strip('"')
        for c in _git(rp, "diff", "--name-only", "--diff-filter=U").stdout.splitlines()
        if c.strip()
    ]
    if not rel_issue or conflicted != [rel_issue]:
        print(res.stdout or "", res.stderr or "")
        die(f"合并失败，冲突文件：{conflicted or '(未知)'}；代码冲突必须人工处理，本命令只解账本冲突")

    # 必须从 index 阶段取版本——工作区那份带 <<<<<<< 标记，tomllib 直接炸（实测踩过）
    # 0.6.2：逻辑提取为 _resolve_ledger_conflict（与候选树验证共用一份语义）。
    _resolve_ledger_conflict(rp, rel_issue, iss.path)
    # merge 回执（同干净路径）：重读账本，别拿旧正文覆盖刚解完冲突的合并结果
    fresh = Issue(iss.path)
    fresh.meta["merged_by"] = "wfk-merge"
    fresh.meta["merge_receipt"] = f"wfk@{short_sha(rp)}"
    fresh.save()
    print(f"下一步：wfk finish {iss.id} --merged --cleanup")
    return 0


def cmd_finish(args, root, cfg):
    iss = load_issue_by_id(root, cfg, args.id)
    m = iss.meta
    problems = validate_close(iss)
    if m.get("status") not in ("in_review", "merged"):
        problems.append(f"状态是 {m.get('status')}，应当是 in_review（gate 通过后）")
    if problems:
        print(f"{iss.id} 拒绝关闭：")
        for p in problems:
            print(f"  - {p}")
        return 1
    if not args.merged:
        print(f"{iss.id} 检查通过。合并走：wfk merge {iss.id}（合并闸会重算矩阵），然后 wfk finish {iss.id} --merged")
        return 0
    # 关门之前必须确认代码真的在默认分支上：曾经出现过 `merge` 因主仓脏而失败、
    # 紧接着 `finish --merged` 照样把状态改成 merged 并删掉 worktree 的事故——
    # 账本说合并了，代码却没进去，分支还留在那里。状态是真相，就得先验真相。
    _rp = repo_path(root, cfg, m.get("repo", "main"))
    _branch = m.get("branch") or ""
    _base = default_branch(cfg, m.get("repo", "main"))
    if not _branch:
        print(f"{iss.id} 拒绝关闭：账本没有 branch 字段，无法确认代码已合并")
        return 1
    _anc = subprocess.run(
        ["git", "-C", str(_rp), "merge-base", "--is-ancestor", _branch, _base],
        capture_output=True, text=True, stdin=subprocess.DEVNULL,
    )
    if _anc.returncode != 0:
        print(f"{iss.id} 拒绝关闭：{_branch} 的提交不在 {_base} 上——合并没成功（或被回退了）")
        print(f"  先手动合并：git -C {_rp} merge --no-ff {_branch}")
        print(f"  确认 git merge-base --is-ancestor {_branch} {_base} 返回 0 后，再执行 wfk finish {iss.id} --merged")
        return 1
    # merge 回执（0.6.0）：finish 只认两种合法合并——wfk merge 的回执，
    # 或人工合并 + 用户签字（manual_merged_by）。没有回执的 finish --merged
    # 会给绕过合并闸的手工 git merge 盖章（F-12）。
    receipt = str(m.get("merge_receipt") or "")
    # 0.6.3/F13 后半：回执**拓扑验证**——wfk@<sha> 必须是真的合并提交、
    # 第二父提交是本 issue 的分支、且已在主干历史里。旧检查只认前缀，
    # 回执里写死字符串照样盖章。
    if receipt.startswith("wfk@"):
        _rsha = receipt[4:].strip()
        _rt = _git(_rp, 'cat-file', '-t', _rsha)
        if _rt.returncode != 0 or _rt.stdout.strip() != 'commit':
            print(f"{iss.id} 拒绝关闭：回执 {_rsha} 不是本仓库的提交")
            return 1
        _par = _git(_rp, 'rev-list', '--parents', '-n', '1', _rsha).stdout.split()
        if len(_par) != 3:
            print(f"{iss.id} 拒绝关闭：回执 {_rsha[:8]} 不是合并提交（单父提交）")
            return 1
        _anc = _git(_rp, 'merge-base', '--is-ancestor', _branch, _rsha)
        if _anc.returncode != 0:
            print(f"{iss.id} 拒绝关闭：回执 {_rsha[:8]} 的父提交不是 {_branch}——回执与分支对不上")
            return 1
        _inbase = _git(_rp, 'merge-base', '--is-ancestor', _rsha, _base)
        if _inbase.returncode != 0:
            print(f"{iss.id} 拒绝关闭：回执 {_rsha[:8]} 不在 {_base} 历史里（合并后被回滚？）")
            return 1
    if not receipt.startswith("wfk@"):
        manual_by = str(m.get("manual_merged_by") or "").strip()
        if not (getattr(args, "manual_merge", False) and manual_by):
            print(f"{iss.id} 拒绝关闭：没有 wfk merge 的回执（merge_receipt）。")
            print(f"  正路：wfk merge {iss.id}（合并闸重算矩阵 + 写回执），再 finish --merged")
            print(f"  确是人工合并：先在 front-matter 写 manual_merged_by = \"<用户名>\"，"
                  f"再 wfk finish {iss.id} --merged --manual-merge（合并汇报里必须写明）")
            return 1
    m["status"] = "merged"
    m["merged_sha"] = short_sha(_rp)
    iss.save(meta=m)
    print(f"{iss.id} → merged")
    if args.cleanup and m.get("worktree"):
        rp = repo_path(root, cfg, m.get("repo", "main"))
        wtp = Path(m["worktree"])
        # 先卸 junctions：git 的递归删除啃不动目录联接，会报 Permission denied 并留下半空的树
        for link in cfg.get("worktree", {}).get("links", []):
            jp = wtp / Path(link).name
            if jp.exists():
                subprocess.run(["cmd", "/c", "rmdir", str(jp)],
                               capture_output=True, text=True, stdin=subprocess.DEVNULL)
        # stdin=DEVNULL：目录删不掉时 git 会问 "Should I try again?"，不接管就会挂住
        subprocess.run(
            ["git", "-C", str(rp), "worktree", "remove", "--force", "--force", str(wtp)],
            check=False, stdin=subprocess.DEVNULL, capture_output=True, text=True,
        )
        subprocess.run(["git", "-C", str(rp), "worktree", "prune"], check=False, capture_output=True)
        if wtp.exists():
            print(f"[warn] worktree 已从 git 注销，但目录没删掉：{wtp}")
            print("       常见原因：有进程的 CWD 在那棵树里（例如在 worktree 内跑 pytest）。")
            print("       结束该进程后 rm -rf 即可；不要相信“已移除”这句话，要 ls 一眼。")
        else:
            print(f"worktree 已移除：{wtp}")
    return 0


# ────────────────────────── 入口 ──────────────────────────


def cmd_stats(args, root, cfg):
    """产品 / 尺子 / 流程分开数：这套流程值不值，只能靠这张表回答（只读，允许时钟）。

    与 `scan --json` 的分工：scan 给 cron 做变化检测（clock-free），stats 给人读。
    """
    issues = load_issues(root, cfg)
    since = (args.since or "").strip()
    if since:
        issues = [i for i in issues if (i.meta.get("created") or "") >= since]
    total = len(issues)
    kinds = {k: 0 for k in KINDS}
    for i in issues:
        k = i.meta.get("kind") or "unclassified"
        kinds[k] = kinds.get(k, 0) + 1
    merged = [i for i in issues if i.meta.get("status") == "merged"]
    need = set(policy_list(cfg, "visual_required_for", ["engine"]))
    merged_need = [i for i in merged if (i.meta.get("kind") or "unclassified") in need]
    no_visual = [i.id for i in merged_need if not gate_evidence(i, tier="g2")]
    merged_unclassified = [i.id for i in merged if (i.meta.get("kind") or "unclassified") == "unclassified"]
    reworked = [i for i in issues if int(i.meta.get("rework") or 0) > 0]
    walls = sorted(int(i.meta.get("cost_wall_min") or 0) for i in issues
                   if int(i.meta.get("cost_wall_min") or 0) > 0)
    median_wall = walls[len(walls) // 2] if walls else 0
    tokens = sum(int(i.meta.get("cost_tokens") or 0) for i in issues)
    reviewed = [i for i in issues if (i.meta.get("review") or "").strip()]

    def pct(n, d):
        return round(100.0 * n / d, 1) if d else 0.0

    payload = {
        "project": cfg["project"],
        "since": since or "(all)",
        "total": total,
        "kinds": dict(sorted(kinds.items())),
        "engine_share_pct": pct(kinds.get("engine", 0), total),
        "measure_share_pct": pct(kinds.get("measure", 0), total),
        "merged": len(merged),
        "merged_need_visual": len(merged_need),
        "merged_without_visual": no_visual,
        "merged_unclassified": merged_unclassified,
        "rework_count": len(reworked),
        "rework_rate_pct": pct(len(reworked), total),
        "median_wall_min": median_wall,
        "cost_tokens": tokens,
        "reviewed": len(reviewed),
        "review_rate_pct": pct(len(reviewed), total),
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    print(f"# 统计 {cfg['project']}（since={payload['since']}，共 {total} 条）")
    print("分类      " + "  ".join(f"{k} {v}" for k, v in payload["kinds"].items()))
    print(f"产品占比  engine {payload['engine_share_pct']}% / measure {payload['measure_share_pct']}%"
          f"   （高价值信号：engine 占比越大，说明在往前推而不是在磨尺子）")
    print(f"已合并    {len(merged)}   其中需视觉 {len(merged_need)}   缺视觉 {len(no_visual)}"
          + (f"：{', '.join(no_visual[:12])}" if no_visual else ""))
    if merged_unclassified:
        print(f"未分类合并 {len(merged_unclassified)}（历史账本没有 kind，缺视觉统计对它们不适用——别把 0 当达标）")
    wall_note = "" if walls else "（没有账本记录墙钟：先跑一次 gate）"
    print(f"回工      {len(reworked)}/{total} = {payload['rework_rate_pct']}%"
          f"   中位墙钟 {median_wall}min{wall_note}")
    # 成本不假报（0.6.0）：cost_tokens 靠人回填，没人填时显示"未记录"。
    # 报 0 会被当成"零成本"——0 比没有更危险。
    if tokens:
        print(f"成本      {tokens} tokens")
    else:
        print("成本      未记录（cost_tokens 无写入方——0 不是零成本，不假报）")
    print(f"盲评      {len(reviewed)}/{total} = {payload['review_rate_pct']}%"
          + ("   （盲评会绑提交 @sha）" if reviewed else ""))
    return 0


def cmd_review(args, root, cfg):
    """记录一次盲评结果（只改 review 字段，不翻状态、不影响门禁）。"""
    iss = load_issue_by_id(root, cfg, args.id)
    verdict = args.verdict.upper().strip()
    if verdict not in ("PASS", "BLOCK"):
        die("--verdict 只能是 PASS 或 BLOCK")
    note = (args.note or "").strip().replace("\n", " ")
    if verdict == "BLOCK" and len(note) < 10:
        die("BLOCK 必须写明 >=10 字理由——盲评的价值在理由，不在结论")
    by = (args.by or "unknown").strip()
    m = iss.meta
    # 盲评必须绑提交（2026-11-18）：否则"在 A 提交上评过"会被用到 B 提交上。
    sha = (getattr(args, "sha", "") or "").strip()
    if not sha:
        try:
            sha = branch_sha(repo_path(root, cfg, m.get("repo", "main")),
                             m.get("branch") or f"feat/{iss.id}")
        except SystemExit:
            sha = "unknown"
    entry = (f"blind@{by}:{verdict}({date.today().isoformat()}) @{sha}"
             + (f" {note}" if note else ""))
    hist = list(m.get("reviews") or [])
    hist.append(entry)
    m["reviews"] = hist
    m["review"] = entry            # 兼容：stats / 旧账本仍读单值
    iss.save(meta=m)
    print(f"{iss.id} review = {entry}")
    return 0


def cmd_block(args, root, cfg):
    """阻塞一条 issue（blocked_by 真正被读——以前只是备注字段）。"""
    iss = load_issue_by_id(root, cfg, args.id)
    ids = [x.strip() for x in (args.by or []) if x.strip()]
    if not ids:
        die("block 至少要给 --by <id>")
    known = {i.id for i in load_issues(root, cfg)}
    unknown = [x for x in ids if x not in known]
    if unknown:
        die("blocked_by 里有不存在的 issue：" + ", ".join(unknown))
    cur = list(iss.meta.get("blocked_by") or [])
    for x in ids:
        if x not in cur:
            cur.append(x)
    iss.meta["blocked_by"] = cur
    if iss.meta.get("status") not in ("merged", "dropped"):
        iss.meta["status"] = "blocked"
    if (args.reason or "").strip():
        iss.meta["blocked_reason"] = args.reason.strip()
    iss.save(meta=iss.meta)
    print(f"{iss.id} → {iss.meta['status']}  blocked_by={cur}")
    if (args.reason or "").strip():
        print(f"  理由：{args.reason.strip()[:80]}")
    return 0


def cmd_unblock(args, root, cfg):
    """解除阻塞：阻塞项必须已 merged/dropped，否则要 --force（并留痕）。"""
    iss = load_issue_by_id(root, cfg, args.id)
    ids = list(iss.meta.get("blocked_by") or [])
    if not ids:
        die(f"{iss.id} 没有 blocked_by，无需 unblock")
    st = {i.id: i.meta.get("status") for i in load_issues(root, cfg)}
    undone = [x for x in ids if st.get(x) not in ("merged", "dropped")]
    if undone and not args.force:
        die(f"{iss.id} 的阻塞项还没完成："
            + ", ".join(f"{x}={st.get(x)}" for x in undone)
            + "\n  确实要解开加 --force（会写进账本，交接时看得见）")
    iss.meta["blocked_by"] = []
    if iss.meta.get("status") == "blocked":
        iss.meta["status"] = "approved"
    if undone:
        iss.meta["unblocked_forced"] = ", ".join(undone)
    iss.save(meta=iss.meta)
    print(f"{iss.id} → {iss.meta['status']}（阻塞已清）")
    return 0


def cmd_drop(args, root, cfg):
    """丢弃一条 issue（必须写理由——丢弃也是决策）。"""
    iss = load_issue_by_id(root, cfg, args.id)
    if not (args.reason or "").strip():
        die("drop 必须写 --reason（丢弃也要留为什么）")
    iss.meta["status"] = "dropped"
    iss.meta["dropped_reason"] = args.reason.strip()
    iss.save(meta=iss.meta)
    print(f"{iss.id} → dropped（{args.reason.strip()[:60]}）")
    return 0


def cmd_selftest(args, root=None, cfg=None) -> int:
    """跑 kit 自带测试（不依赖任何项目配置）。"""
    tdir = Path(__file__).resolve().parent / "tests"
    if not tdir.is_dir():
        die(f"找不到自测目录：{tdir}")
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    r = subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-s", str(tdir), "-p", "test_*.py", "-v"],
        env=env, stdin=subprocess.DEVNULL,
    )
    return 0 if r.returncode == 0 else 1


def die(msg: str):
    print(f"wfk: {msg}", file=sys.stderr)
    raise SystemExit(2)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="wfk", description="issue 驱动工作流：账本 / worktree / 门禁")
    ap.add_argument("--version", action="version", version=f"wfk {__version__}")
    ap.add_argument("--root", help="项目根（默认从当前目录向上找 workflow.toml）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("new", help="新建 issue（proposed）")
    p.add_argument("title")
    p.add_argument("--repo", default="main")
    p.add_argument("--scope", action="append", help="写作用域，可多次")
    p.add_argument("--blocked-by", action="append")
    p.add_argument("--verify", action="append", help="验收断言，可多次（必须 A<n>: <命令> 或 MANUAL: by= how=）")
    p.add_argument("--kind", choices=KINDS, help="engine=改产品 / measure=修尺子 / process=改流程 / hygiene=清理")
    p.add_argument("--moves-metric", help="engine 类必填：这条改动想让哪个指标动")
    p.add_argument("--budget", type=int, default=300000)
    p.set_defaults(fn=cmd_new)

    p = sub.add_parser("list", help="列账本")
    p.add_argument("--status", action="append", choices=STATUSES)
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_list)

    p = sub.add_parser("scan", help="巡检：待决策/在跑/卡住/冲突/僵尸锁")
    p.add_argument("--json", action="store_true", help="确定性输出，供 cron monitor 用")
    p.set_defaults(fn=cmd_scan)

    p = sub.add_parser("approve", help="批准 issue（proposed/blocked → approved）")
    p.add_argument("id")
    p.set_defaults(fn=cmd_approve)

    p = sub.add_parser("start", help="建 worktree + 分支，状态转 in_progress")
    p.add_argument("id")
    p.add_argument("--owner", help="工人标识")
    p.set_defaults(fn=cmd_start)

    p = sub.add_parser("gate", help="跑门禁")
    p.add_argument("id")
    p.add_argument("tier", nargs="?",
                   help="g0/g1/g1.5/g2/g3…（跑指定层）或 status（打印完成矩阵）；"
                        "不给则按 required_tiers 跑必需层")
    p.add_argument("--doc", help="G2 文档路径；省缺=账本卡自身（工具已持有 issue 对象）")
    p.add_argument("--template", help="G2 需要的模板路径；步骤里用 {template} 占位")
    p.set_defaults(fn=cmd_gate)

    p = sub.add_parser("calibrate", help="在当前树跑测量步骤落 CALIB 基线证据行（不判过/不过）")
    p.add_argument("id")
    p.add_argument("tier", nargs="?", default="g2",
                   help="要测量的层（默认 g2）")
    p.add_argument("--doc", help="测量文档路径；省缺=账本卡自身")
    p.add_argument("--template", help="模板路径；步骤里用 {template} 占位")
    p.set_defaults(fn=cmd_calibrate)

    p = sub.add_parser("verify", help="跑全局验证（语料 + 全量），不绑 issue")
    p.add_argument("--only", help="只跑指定步骤（逗号分隔，如 corpus,full）")
    p.set_defaults(fn=cmd_verify)

    p = sub.add_parser("stats", help="产品/尺子/流程分开数 + 回工率 + 成本（只读）")
    p.add_argument("--since", help="只看该日期（YYYY-MM-DD）之后创建的")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_stats)

    p = sub.add_parser("recover", help="摆回即红残留标记：预览（默认）/ --force 强制还原")
    p.add_argument("--force", action="store_true",
                   help="按标记路径 git checkout HEAD（丢弃当前改动，用户已确认）")
    p.set_defaults(fn=cmd_recover)

    p = sub.add_parser("review", help="记录一次盲评结果（不翻状态）")
    p.add_argument("id")
    p.add_argument("--verdict", required=True, help="PASS 或 BLOCK")
    p.add_argument("--by", required=True, help="评审者标识（模型名或人名）")
    p.add_argument("--note", help="理由（BLOCK 必填，>=10 字）")
    p.add_argument("--sha", help="评的是哪个提交（留空取分支 HEAD）")
    p.set_defaults(fn=cmd_review)

    p = sub.add_parser("block", help="阻塞 issue（blocked_by 会被 unblock/start 检查）")
    p.add_argument("id")
    p.add_argument("--by", action="append", help="阻塞它的 issue id，可多次")
    p.add_argument("--reason")
    p.set_defaults(fn=cmd_block)

    p = sub.add_parser("unblock", help="解除阻塞（阻塞项应已 merged/dropped）")
    p.add_argument("id")
    p.add_argument("--force", action="store_true", help="阻塞项未完成也强解（留痕）")
    p.set_defaults(fn=cmd_unblock)

    p = sub.add_parser("drop", help="丢弃 issue（必须写理由）")
    p.add_argument("id")
    p.add_argument("--reason", required=True)
    p.set_defaults(fn=cmd_drop)

    p = sub.add_parser("selftest", help="跑 kit 自带测试（不依赖项目配置）")
    p.set_defaults(fn=cmd_selftest)

    p = sub.add_parser("merge", help="合并 issue 分支（自动解账本冲突）")
    p.add_argument("id")
    p.set_defaults(fn=cmd_merge)

    p = sub.add_parser("finish", help="关闭前检查 / 置 merged")
    p.add_argument("id")
    p.add_argument("--merged", action="store_true", help="人已执行合并之后再加")
    p.add_argument("--manual-merge", action="store_true",
                   help="人工 git merge 的收尾：要求账本里有 manual_merged_by 用户签字")
    p.add_argument("--cleanup", action="store_true", help="移除 worktree")
    p.set_defaults(fn=cmd_finish)

    # Phase 2（0.7.0）：第二实例脚手架——机械件全在 wfk_init.py，本文件只接线。
    p = sub.add_parser("init", help="把当前 git 仓铺成 wfk 实例（workflow.toml+账本+AGENTS+LOCKS）")
    p.add_argument("--force", action="store_true", help="已有 workflow.toml 时仍重铺（只重写 cfg，账本不动）")
    p.add_argument("--prefix", help="覆盖自动推导的 id 前缀（如 WFK）")
    p.add_argument("--branch", help="覆盖自动侦测的默认分支")
    p.set_defaults(fn=lambda *a, **k: 0)

    args = ap.parse_args(argv)
    if args.cmd == "selftest":          # 自测不需要项目配置
        return cmd_selftest(args)
    if args.cmd == "init":              # init 就是建实例：不需要 find_root/load_cfg
        from wfk_init import scaffold
        return scaffold(Path.cwd(), force=args.force,
                        branch=getattr(args, "branch", None),
                        prefix=getattr(args, "prefix", None))
    root = Path(args.root).resolve() if args.root else find_root(Path.cwd())
    cfg = load_cfg(root)
    if args.cmd == "recover":
        # 显式恢复命令自带安全闸（--force 才动文件），不走自动恢复。
        return cmd_recover(args, root, cfg)
    # 0.6.2（外部评审 R16）：只读命令绝不触发恢复——查询不能 checkout 掉工作区。
    read_only = args.cmd in ("list", "scan", "stats") or (
        args.cmd == "gate" and str(getattr(args, "tier", "") or "").lower() == "status")
    recover_inflight_mutation(root, read_only=read_only)
    return args.fn(args, root, cfg)


def recover_inflight_mutation(root: Path, read_only: bool = False) -> None:
    """救回「摆回即红」被强杀后残留的变异态（0.6.2 安全化）。

    `scripts/revert_check.py` 在变异前写 `.wf/gate/revert-inflight.json`，正常结束会删。
    进程被 kill -9 / 前台超时杀掉时 finally 跑不到，源码会**留在摆回态**。

    0.6.2 前的问题（外部评审 R16）：恢复无条件跑在**每个**子命令前，包括
    list/scan/stats 这类只读命令——查询也能 `git checkout` 掉工作区里未提交的新
    内容。现在：

    * 只读命令（list/scan/stats/gate status）**绝不还原**，只提示标记存在；
    * 变异进程（marker 里的 pid）还活着 → 不还原（它自己会收尾）；
    * 标记带 `files:[{path, mutated_sha256}]`（revert_check v2 起）→ 当前内容
      == 记录的变异哈希才 checkout；不符 = 有人写了新内容，**拒绝覆盖**；
    * 旧格式标记（只有 paths）不自动 checkout——哈希未知时自动恢复等于赌，
      改走显式 `wfk recover --force`；
    * 逐路径报结果，部分拒绝/失败不删标记、不谎报全部恢复。
    """
    marker = root / ".wf" / "gate" / "revert-inflight.json"
    if not marker.is_file():
        return
    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] {marker} 解析失败（{exc}），请手工检查工作区", file=sys.stderr)
        return
    if read_only:
        print(f"[recover-skip] 只读命令不自动还原（发现标记 pid={data.get('pid')}，"
              f"用 wfk recover 查看处置）")
        return
    pid = data.get("pid")
    if pid and _pid_alive(int(pid)):
        print(f"[recover-skip] 变异进程 pid={pid} 仍在运行，不动工作区（它自己会收尾）")
        return
    entries = data.get("files")
    if entries:
        # v2 标记：带 mutated_sha256 的逐文件哈希预检
        done, refused, failed = [], [], []
        for e in entries:
            p = e.get("path")
            want = e.get("mutated_sha256")
            if not isinstance(p, str) or not isinstance(want, str):
                refused.append((p, "标记条目缺 path/mutated_sha256"))
                continue
            cur = _file_sha256(root / p)
            if cur is None:
                if _git(root, "status", "--porcelain", "--", p).stdout.strip():
                    failed.append((p, "文件已不存在但有 git 状态残留"))
                else:
                    done.append(p)  # 路径本来就干净/没 checkout 出来过
                continue
            if cur == want:
                r = subprocess.run(
                    ["git", "-C", str(root), "-c", "core.quotepath=false",
                     "checkout", "HEAD", "--", p], capture_output=True, text=True)
                (done if r.returncode == 0 else failed).append(
                    p if r.returncode == 0 else (p, r.stderr.strip()[:120]))
            else:
                refused.append((p, "当前内容与标记记录的变异不一致（可能有新写入的工作）"))
        for p in done:
            print(f"  OK   还原 {p}")
        for p, why in refused:
            print(f"  STOP {p} —— {why}")
        for p, why in failed:
            print(f"  FAIL {p} —— {why}")
        if refused or failed:
            print(f"[recover] {len(done)} 个已还原；{len(refused) + len(failed)} 个未动——"
                  f"标记保留（{marker.relative_to(root)}）。确信要覆盖：wfk recover --force")
            return
        marker.unlink(missing_ok=True)
        print(f"[recover] 已还原 {len(done)} 个文件并清除标记。")
        return
    # 旧格式（只有 paths，无哈希）：自动恢复等于赌当前脏内容就是那次变异。
    paths = [p for p in (data.get("paths") or []) if isinstance(p, str)]
    if not paths:
        marker.unlink(missing_ok=True)
        return
    print(f"[recover] 标记是旧格式（无 mutated 哈希，tag={data.get('tag')} pid={pid}），"
          f"涉及 {len(paths)} 个文件：")
    for p in paths:
        dirty = bool(_git(root, "status", "--porcelain", "--", p).stdout.strip())
        print(f"  {'脏' if dirty else '净'} {p}")
    print("不自动覆盖——确认后用 `wfk recover --force`（会按标记路径 git checkout HEAD）。")
    return


def recover_inflight_force(root: Path) -> None:
    """显式强制恢复（用户确认过当前脏内容可丢弃）。"""
    marker = root / ".wf" / "gate" / "revert-inflight.json"
    if not marker.is_file():
        print("[recover] 没有残留标记。")
        return
    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        die(f"{marker} 解析失败（{exc}），请手工检查工作区")
    pid = data.get("pid")
    if pid and _pid_alive(int(pid)):
        die(f"变异进程 pid={pid} 还活着——先等它结束（或人工处理），--force 也不覆盖正在跑的变异")
    paths = [p for p in (data.get("paths") or []) if isinstance(p, str)] or \
        [e.get("path") for e in (data.get("files") or []) if isinstance(e, dict) and isinstance(e.get("path"), str)]
    if not paths:
        marker.unlink(missing_ok=True)
        print("[recover] 标记里没有路径，已清除。")
        return
    print(f"[recover] 强制还原 {len(paths)} 个文件（用户已确认可丢弃当前改动）：")
    bad = []
    for p in paths:
        r = subprocess.run(
            ["git", "-C", str(root), "-c", "core.quotepath=false",
             "checkout", "HEAD", "--", p], capture_output=True, text=True)
        print(f"  {'OK ' if r.returncode == 0 else 'FAIL'} {p}"
              + ("" if r.returncode == 0 else f"  {r.stderr.strip()[:120]}"))
        if r.returncode != 0:
            bad.append(p)
    if bad:
        print(f"[recover] {len(bad)} 个路径还原失败，标记保留。")
        return
    marker.unlink(missing_ok=True)
    print("[recover] 已还原并清除标记。")


def _pid_alive(pid: int) -> bool:
    """跨平台进程活性（stdlib/ctypes，不引第三方）。"""
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        h = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            return False
        try:
            code = ctypes.c_ulong()
            if ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code)):
                if code.value != STILL_ACTIVE:
                    return False
            return True
        finally:
            ctypes.windll.kernel32.CloseHandle(h)
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except OSError:
        return False


def _file_sha256(path: Path) -> str | None:
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
    except OSError:
        return None
    return h.hexdigest()



if __name__ == "__main__":
    raise SystemExit(main())
