# -*- coding: utf-8 -*-
"""wfk init 脚手架自测（Phase 2 / 0.7.0）。

覆盖三件事：
1. 裸 git 仓 → init → 最小实例可跑（new/approve/start/gate g0 全链冒烟）；
2. 已有 workflow.toml 拒绝覆盖，--force 只重写 cfg（账本/AGENTS 不动）；
3. 非项目目录（无 .git）给一行可读错误，不抛 Traceback。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

WFK = Path(__file__).resolve().parent.parent / "wfk.py"


class InitBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="wfkinit-"))
        self.root = self.tmp / "repo"
        (self.root / "src").mkdir(parents=True)
        (self.root / "src" / "x.py").write_text("# good\nprint('x')\n", encoding="utf-8")
        self.git("init", "-q", "-b", "main")
        self.git("config", "core.autocrlf", "false")
        self.git("-c", "user.email=t@t", "-c", "user.name=t", "add", "-A")
        self.git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "init")

    def tearDown(self):
        # worktree 可能建在 root.parent 下（[worktree] root = <parent>/_wt）
        shutil.rmtree(self.tmp, ignore_errors=True)

    def git(self, *args, cwd=None) -> str:
        r = subprocess.run(["git", "-C", str(cwd or self.root), *args],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace")
        if r.returncode != 0:
            self.fail(f"git {' '.join(args[:4])} 失败：{(r.stderr or r.stdout).strip()[:200]}")
        return r.stdout.strip()

    def wfk(self, *args, cwd=None) -> tuple[int, str]:
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        env.pop("PYTHONPATH", None)
        r = subprocess.run([sys.executable, str(WFK), *args],
                           cwd=str(cwd or self.root), capture_output=True, text=True,
                           encoding="utf-8", errors="replace", env=env)
        return r.returncode, (r.stdout or "") + (r.stderr or "")

    def commit_all(self, msg: str, cwd=None):
        self.git("-c", "user.email=t@t", "-c", "user.name=t", "add", "-A", cwd=cwd)
        self.git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", msg, cwd=cwd)


class TestInitScaffold(InitBase):
    def test_init_scaffolds_minimal_instance_then_smoke(self):
        """裸仓 init → 协议五件套落盘 → new/approve/start/gate g0 全链冒烟。"""
        rc, out = self.wfk("init")
        self.assertEqual(rc, 0, out)
        for rel in ("workflow.toml", "docs/issues/_template.md",
                    "docs/issues/README.md", "AGENTS.md", "docs/LOCKS.md"):
            self.assertTrue((self.root / rel).is_file(), f"缺 {rel}")
        self.assertIn(".wf/", (self.root / ".gitignore").read_text(encoding="utf-8"))

        self.commit_all("chore: wfk init")
        rc, out = self.wfk("new", "试运行", "--kind", "process",
                           "--scope", "src/**",
                           "--verify", "A1: {py} -m compileall -q . @step:g0.compileall")
        self.assertEqual(rc, 0, out)
        iid = self._first_id(out)
        rc, out = self.wfk("approve", iid)
        self.assertEqual(rc, 0, out)
        rc, out = self.wfk("start", iid, "--owner", "t")
        self.assertEqual(rc, 0, out)
        rc, out = self.wfk("gate", iid, "g0")
        self.assertEqual(rc, 0, out)

    def _first_id(self, new_out: str) -> str:
        import re as _re
        m = _re.search(r"created docs[/\\\\]issues[/\\\\](.+?-\d+)-", new_out)
        self.assertIsNotNone(m, new_out)
        return m.group(1)

    def test_init_refuses_existing_cfg(self):
        rc, _ = self.wfk("init")
        self.assertEqual(rc, 0)
        rc, out = self.wfk("init")
        self.assertNotEqual(rc, 0)
        self.assertIn("已存在", out)
        self.assertNotIn("Traceback", out)

    def test_init_force_rewrites_cfg_only(self):
        """--force 只重写 workflow.toml；手工改过的 AGENTS/账本不被吞。"""
        self.wfk("init")
        (self.root / "AGENTS.md").write_text("# 自定义红线\n", encoding="utf-8")
        (self.root / "docs/issues/_template.md").write_text(
            "+++\n+++\n自定义正文模板\n", encoding="utf-8")
        rc, out = self.wfk("init", "--force")
        self.assertEqual(rc, 0, out)
        self.assertEqual((self.root / "AGENTS.md").read_text(encoding="utf-8"),
                         "# 自定义红线\n")
        self.assertIn("自定义正文模板",
                      (self.root / "docs/issues/_template.md").read_text(encoding="utf-8"))

    def test_init_on_non_git_dir_is_clean_error(self):
        bare = self.tmp / "not-repo"
        bare.mkdir()
        rc, out = self.wfk("init", cwd=bare)
        self.assertNotEqual(rc, 0)
        self.assertIn("不是 git 仓", out)
        self.assertNotIn("Traceback", out)


if __name__ == "__main__":
    unittest.main()
