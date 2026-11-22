"""wfk 自测（随包分发，`wfk selftest` 一条命令跑完）。

这些用例对应 2026-11-17 外部评审 + 复现发现的 P0 绕过：
    E-01 脏工作区伪造证据        → test_dirty_tree_blocks_gate
    E-02 工人自带尺子(删步骤)    → test_config_drift_requires_declaration / test_empty_tier_is_error
    E-03 g1 证据过期仍能合并     → test_stale_evidence_blocks_merge
    E-04 空矩阵/skip/单层通过    → test_empty_tier_is_error / test_skip_not_counted / test_single_tier_not_enough
    E-05 scope/forbidden/tests   → test_scope_segments / test_audit_forbidden_and_tests
    E-06 scan 漏 proposed        → test_scan_includes_proposed
    N-1 缺模板抛 Traceback       → test_new_without_template_is_clean_error
    N-2 review 理由阈值文案      → test_review_note_threshold
    N-3 空列表关不掉视觉闸       → test_empty_list_disables_visual_gate

0.6.0（审计链耐久）:
    E-09 日志覆盖+指针404       → test_log_runid_not_overwritten / test_merge_requires_log_pointer
    E-10g 盲评时效（消费侧）    → test_merge_review_timing
    F-07 锁非原子/误删他人锁    → test_lock_atomic
    F-12 手工 merge 洗白        → test_merge_receipt_blocks_laundered_merge / test_merge_receipt_from_wfk_merge
    方案§A1 指标白名单          → test_metrics_whitelist
    成本 0 假报                 → test_stats_hides_zero_cost

每个用例在系统临时目录建一个真 git 仓库，用真 CLI 走完流程；不碰任何真实项目。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path

WFK = Path(__file__).resolve().parents[1] / "wfk.py"
NL = chr(10)   # 0.6.2 回归用：写文件时换行的显式构造

CFG = """project = "st"
id_prefix = "T"
issue_dir = "docs/issues"

[repos]
main = {{ path = ".", default_branch = "main" }}

[worktree]
root = "{wt}"

[policy]
visual_required_for = ["engine"]
required_tiers = {{ default = ["g1"], engine = ["g1", "g2"] }}

[[gate.g1.steps]]
name = "check"
cmd = "{{py}} check.py"
timeout = 120

[forbidden]
paths = ["tests/**", "credentials/**"]

[[gate.g2.steps]]
name = "render"
cmd = "{{py}} render.py {{doc}}"
timeout = 120
"""

CHECK = 'import pathlib, sys\nsys.exit(1 if "broken" in pathlib.Path("src/x.py").read_text() else 0)\n'
RENDER = 'import sys\nsys.exit(0 if len(sys.argv) > 1 else 1)\n'

TEMPLATE = """+++
id = "T-0000"
title = "模板"
status = "proposed"
+++
## 背景

（待填）

## 验收断言

（待填）

## 决策记录

（开工时留空；关闭前必填。）

## 验证证据

（关闭前必填。）
"""


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="wfkst-"))
        self.root = self.tmp / "repo"
        (self.root / "docs/issues").mkdir(parents=True)
        (self.root / "src").mkdir()
        (self.root / "src/x.py").write_text("# good\n", encoding="utf-8")
        (self.root / "check.py").write_text(CHECK, encoding="utf-8")
        (self.root / "render.py").write_text(RENDER, encoding="utf-8")
        (self.root / ".gitignore").write_text(".wf/\n", encoding="utf-8")
        (self.root / "docs/issues/_template.md").write_text(TEMPLATE, encoding="utf-8")
        self._write_cfg()
        self.git("init", "-q", "-b", "main")
        # 0.6.2：显式关行尾转换——平台/git 版本的 autocrlf 差异曾让账本 3-way 合并
        # 在外部环境产生冲突（同测试在本机绿、在 git 2.55/Py3.13 下红）。
        self.git("config", "core.autocrlf", "false")
        self.git("config", "core.eol", "lf")
        self.git("-c", "user.email=t@t", "-c", "user.name=t", "add", "-A")
        self.git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "init")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ── 工具 ──
    def _write_cfg(self, extra: str = ""):
        (self.root / "workflow.toml").write_text(
            CFG.format(wt=(self.tmp / "wt").as_posix()) + extra, encoding="utf-8")

    def git(self, *args, cwd=None) -> str:
        # 0.6.2：外部评审发现夹具吞 returncode——git 失败时测试拿冲突标记当正常输入，
        # 报出与真因无关的断言差（他们的环境 32/33 的根因）。失败必须立刻炸出真 stderr。
        r = subprocess.run(["git", "-C", str(cwd or self.root), *args],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace")
        if r.returncode != 0:
            self.fail(f"git {' '.join(str(a) for a in args[:4])} 失败(rc={r.returncode})："
                      f"{(r.stderr or r.stdout).strip()[:300]}")
        return r.stdout.strip()

    def commit_all(self, msg: str, cwd=None):
        self.git("-c", "user.email=t@t", "-c", "user.name=t", "add", "-A", cwd=cwd)
        self.git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", msg, cwd=cwd)

    def wfk(self, *args, cwd=None) -> tuple[int, str]:
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        env.pop("PYTHONPATH", None)
        r = subprocess.run([sys.executable, str(WFK), *args],
                           cwd=str(cwd or self.root), capture_output=True, text=True,
                           encoding="utf-8", errors="replace", env=env)
        return r.returncode, (r.stdout or "") + (r.stderr or "")

    def issue_meta(self, iid: str) -> dict:
        for p in (self.root / "docs/issues").glob(f"{iid}-*.md"):
            text = p.read_text(encoding="utf-8")
            _, fm, body = text.split("+++", 2)
            meta = tomllib.loads(fm)
            meta["_body"] = body
            return meta
        raise AssertionError(f"找不到账本 {iid}")

    def new_issue(self, title: str, kind: str = "hygiene", scope: str = "src/**") -> str:
        """新建一条 issue（不 start），返回 id——id 从账本文件名取，别解析 stdout。"""
        code, out = self.wfk("new", title, "--scope", scope, "--kind", kind,
                             "--verify", "A1: MANUAL: by=st how=人工核查 check.py 输出")
        self.assertEqual(code, 0, out)
        ids = [p.name.split("-", 2)[0] + "-" + p.name.split("-", 2)[1]
               for p in (self.root / "docs/issues").glob("*.md") if p.name.startswith("T-")]
        return sorted(ids, key=lambda x: int(x.split("-")[1]))[-1]

    def new_started(self, kind="hygiene", scope="src/**") -> str:
        code, out = self.wfk("new", "用例", "--scope", scope, "--kind", kind,
                             "--verify", "A1: MANUAL: by=st how=人工核查 check.py 输出")
        self.assertEqual(code, 0, out)
        iid = re.search(r"created\s+\S*?(T-\d+)-", out).group(1)
        if kind == "engine":
            self.patch_meta(iid, moves_metric='"clean43.pass_rate"')
        code, out = self.wfk("approve", iid)
        self.assertEqual(code, 0, out)
        code, out = self.wfk("start", iid, "--owner", "st")
        self.assertEqual(code, 0, out)
        return iid

    def patch_meta(self, iid: str, **kv):
        for p in (self.root / "docs/issues").glob(f"{iid}-*.md"):
            text = p.read_text(encoding="utf-8")
            for k, v in kv.items():
                new = f"{k} = {v}"
                if re.search(rf"^{k} = .*$", text, flags=re.M):
                    text = re.sub(rf"^{k} = .*$", new, text, count=1, flags=re.M)
                else:
                    a, b, c = text.split("+++", 2)   # 只插在开头，别碰结尾那个 +
                    text = a + "+++" + "\n" + new + b + "+++" + c
            p.write_text(text, encoding="utf-8")

    def wt(self, iid: str) -> Path:
        return self.tmp / "wt" / f"main-{iid}"

    def fill_branch_body(self, iid: str) -> None:
        """把主仓账本正文补进 worktree 并提交（合并前的关闭检查读的是分支正文）。"""
        wt = self.wt(iid)
        src = next((self.root / "docs/issues").glob(f"{iid}-*.md"))
        (wt / "docs/issues").mkdir(parents=True, exist_ok=True)
        dst = wt / "docs/issues" / src.name
        body = src.read_text(encoding="utf-8")
        body = body.replace("（开工时留空；关闭前必填。）",
                            "在扉页首段插入 pageBreakBefore，让封面与扉页各占一页，不动节结构。")
        body = body.replace("（关闭前必填。）",
                            "prod-p2 从 9.9KB 空页变成 67KB；渲染前 3 页无空白页。")
        dst.write_text(body, encoding="utf-8")
        self.commit_all("补账本正文", cwd=wt)

    # ── 用例 ──
    def test_dirty_tree_blocks_gate(self):
        """E-01：脏工作区不许跑门禁（否则 PASS 会被记到一个没修好的提交上）。"""
        iid = self.new_started()
        wt = self.wt(iid)
        (wt / "src/x.py").write_text("# broken\n", encoding="utf-8")
        self.commit_all("broken", cwd=wt)
        (wt / "src/x.py").write_text("# good\n", encoding="utf-8")   # 不提交
        code, out = self.wfk("gate", iid, "g1", cwd=wt)
        self.assertEqual(code, 2, out)
        self.assertIn("工作区不干净", out)
        self.assertEqual(self.issue_meta(iid).get("status"), "in_progress")

    def test_empty_tier_is_error(self):
        """E-04 / E-02：必需层没步骤 = 配置错误，不是 skip（删步骤不能换来绿灯）。"""
        iid = self.new_started()
        wt = self.wt(iid)
        cfg = (wt / "workflow.toml").read_text(encoding="utf-8")
        (wt / "workflow.toml").write_text(
            re.sub(r"\[\[gate\.g1\.steps\]\].*?(?=\n\[|\Z)", "", cfg, flags=re.S), encoding="utf-8")
        self.commit_all("删掉 g1", cwd=wt)      # 提交，走"配置漂移"分支
        code, out = self.wfk("gate", iid, "g1", cwd=wt)
        self.assertEqual(code, 2, out)
        self.assertIn("配置", out)
        self.assertEqual(self.issue_meta(iid).get("status"), "in_progress")

    def test_config_drift_requires_declaration(self):
        """E-02：改了 workflow.toml 却没声明 → 拒绝；声明了才放行。"""
        iid = self.new_started()
        wt = self.wt(iid)
        cfg = (wt / "workflow.toml").read_text(encoding="utf-8")
        (wt / "workflow.toml").write_text(cfg.replace('"check"', '"check2"'), encoding="utf-8")
        self.commit_all("改配置", cwd=wt)
        code, out = self.wfk("gate", iid, "g1", cwd=wt)
        self.assertEqual(code, 2, out)
        self.assertIn("gate_config_change", out)
        self.patch_meta(iid, gate_config_change='"测试：换步骤名"')
        code, out = self.wfk("gate", iid, "g1", cwd=wt)
        self.assertEqual(code, 0, out)

    def test_stale_evidence_blocks_merge(self):
        """E-03：g1 在旧提交通过，补提交后 merge 必须拒绝。"""
        iid = self.new_started()
        wt = self.wt(iid)
        code, out = self.wfk("gate", iid, "g1", cwd=wt)
        self.assertEqual(code, 0, out)
        (wt / "src/x.py").write_text("# good\n# more\n", encoding="utf-8")
        self.commit_all("再提交", cwd=wt)
        code, out = self.wfk("merge", iid)
        self.assertEqual(code, 2, out)
        self.assertIn("g1", out)

    def test_single_tier_not_enough(self):
        """E-04：engine 类只跑 g2，不得进 in_review。"""
        iid = self.new_started(kind="engine", scope="src/x.py")
        wt = self.wt(iid)
        (wt / "src/x.py").write_text("# good\n# a\n", encoding="utf-8")
        self.commit_all("改一行", cwd=wt)
        code, out = self.wfk("gate", iid, "g2", "--doc", str(self.root / "x.docx"), cwd=wt)
        self.assertEqual(code, 0, out)
        self.assertEqual(self.issue_meta(iid).get("status"), "in_progress")

    def test_full_matrix_flips_to_in_review(self):
        """正例：必需层齐 + 证据绑当前树 → in_review。"""
        iid = self.new_started()
        wt = self.wt(iid)
        code, out = self.wfk("gate", iid, "g1", cwd=wt)
        self.assertEqual(code, 0, out)
        self.assertEqual(self.issue_meta(iid).get("status"), "in_review")
        code, out = self.wfk("gate", iid, "status", cwd=wt)
        self.assertEqual(code, 0, out)
        self.assertIn("ready_for_merge = True", out)

    def test_skip_not_counted(self):
        """E-04：g2 缺 --doc 被 skip，不算 PASS（engine 类不得 ready）。"""
        iid = self.new_started(kind="engine", scope="src/x.py")
        wt = self.wt(iid)
        self.wfk("gate", iid, "g1", cwd=wt)
        code, out = self.wfk("gate", iid, "g2", cwd=wt)     # 不给 --doc
        self.assertEqual(code, 0, out)
        code, out = self.wfk("gate", iid, "status", cwd=wt)
        self.assertEqual(code, 2, out)
        self.assertIn("g2", out)

    def test_empty_list_disables_visual_gate(self):
        """N-3：`visual_required_for = []` 是"关掉"，不是"回落到 engine"。"""
        sys.path.insert(0, str(WFK.parent))
        import wfk  # noqa: PLC0415

        self.assertEqual(wfk.policy_list({"policy": {"visual_required_for": []}},
                                         "visual_required_for", ["engine"]), [])
        self.assertEqual(wfk.policy_list({"policy": {}},
                                         "visual_required_for", ["engine"]), ["engine"])
        # end-to-end：engine 类但矩阵里不要 g2 → 只跑 g1 也能 ready
        cfg = (self.root / "workflow.toml").read_text(encoding="utf-8")
        (self.root / "workflow.toml").write_text(
            cfg.replace('required_tiers = { default = ["g1"], engine = ["g1", "g2"] }',
                        'required_tiers = { default = ["g1"], engine = ["g1"] }')
               .replace('visual_required_for = ["engine"]', "visual_required_for = []"),
            encoding="utf-8")
        self.commit_all("关视觉闸")
        iid = self.new_started(kind="engine", scope="src/x.py")
        wt = self.wt(iid)
        code, out = self.wfk("gate", iid, "g1", cwd=wt)
        self.assertEqual(code, 0, out)
        self.assertEqual(self.issue_meta(iid).get("status"), "in_review")

    def test_scope_segments(self):
        """E-05：`src/**` 不吃 `src2/`；`*` 不跨目录。"""
        sys.path.insert(0, str(WFK.parent))
        import wfk  # noqa: PLC0415

        self.assertTrue(wfk.path_matches("src/a.py", "src/**"))
        self.assertFalse(wfk.path_matches("src2/a.py", "src/**"))
        self.assertTrue(wfk.path_matches("tests/test_a.py", "tests/test_*.py"))
        self.assertFalse(wfk.path_matches("tests/helper/a.py", "tests/test_*.py"))
        self.assertIsNotNone(wfk.scope_overlap(["src/*/a.py"], ["src/x/*.py"]))
        self.assertIsNone(wfk.scope_overlap(["src/foo/**"], ["src/foobar/**"]))

    def test_audit_forbidden_and_tests(self):
        """E-05：禁写路径与未授权改测试都必须红。"""
        iid = self.new_started()
        wt = self.wt(iid)
        (wt / "src/x.py").write_text("# good\n# b\n", encoding="utf-8")
        (wt / "tests").mkdir(exist_ok=True)
        (wt / "tests/test_new.py").write_text("def test_x():\n    assert True\n", encoding="utf-8")
        self.commit_all("改测试", cwd=wt)
        code, out = self.wfk("gate", iid, "g1", cwd=wt)
        self.assertEqual(code, 2, out)
        self.assertIn("allow_test_changes", out)

    def test_authorized_tests_pass_even_if_forbidden(self):
        """尺子自身回归：tests/** 在 [forbidden] 里，但 issue 已授权 → 不许再拦。

        2026-11-18 实测（EZI-0042 工人撞到）：`[forbidden]` 分支不读 allow_test_changes，
        于是"合法改测试的 issue"永远过不了审计——尺子逼人违规。
        """
        iid = self.new_started()
        self.patch_meta(iid, allow_test_changes='"true"',
                        test_change_reason='"新增回归用例，缺陷必须有守卫"')
        wt = self.wt(iid)
        (wt / "src/x.py").write_text("# good\n# c\n", encoding="utf-8")
        (wt / "tests").mkdir(exist_ok=True)
        (wt / "tests/test_new.py").write_text("def test_y():\n    assert True\n", encoding="utf-8")
        self.commit_all("授权改测试", cwd=wt)
        code, out = self.wfk("gate", iid, "g1", cwd=wt)
        self.assertEqual(code, 0, out)

    def test_ledger_edits_are_not_out_of_scope(self):
        """账本改动（决策记录 / 验证证据）不该被判越界。

        2026-11-18 实测：`changed_files()` 会把 worktree 里的账本算进改动面，
        而 write_scope 从不包含 docs/issues/**，于是"按协议写证据"变成违规。
        """
        iid = self.new_started()
        wt = self.wt(iid)
        (wt / "docs/issues").mkdir(parents=True, exist_ok=True)
        (wt / "docs/issues" / f"{iid}-x.md").write_text("+++\nid = \"" + iid + "\"\n+++\n",
                                                        encoding="utf-8")
        self.commit_all("补账本", cwd=wt)
        code, out = self.wfk("gate", iid, "g1", cwd=wt)
        self.assertEqual(code, 0, out)

    def test_blocked_by_blocks_start(self):
        """F-04：blocked_by 真的被读——依赖没完成不许开工。"""
        code, out = self.wfk("new", "前置", "--scope", "src/a/**", "--kind", "hygiene",
                             "--verify", "A1: MANUAL: by=st how=人工核查 check.py 输出")
        first = re.search(r"created\s+\S*?(T-\d+)-", out).group(1)
        code, out = self.wfk("new", "后继", "--scope", "src/b/**", "--kind", "hygiene",
                             "--verify", "A1: MANUAL: by=st how=人工核查 check.py 输出")
        second = re.search(r"created\s+\S*?(T-\d+)-", out).group(1)
        self.wfk("block", second, "--by", first, "--reason", "等前置")
        code, out = self.wfk("start", second, "--owner", "st")
        self.assertEqual(code, 2, out)
        self.assertIn("阻塞", out)

    def test_drop_requires_reason(self):
        code, out = self.wfk("new", "要丢的", "--scope", "src/c/**", "--kind", "hygiene",
                             "--verify", "A1: MANUAL: by=st how=人工核查 check.py 输出")
        iid = re.search(r"created\s+\S*?(T-\d+)-", out).group(1)
        code, out = self.wfk("drop", iid)
        self.assertNotEqual(code, 0)
        code, out = self.wfk("drop", iid, "--reason", "重复，已被别的 issue 覆盖")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.issue_meta(iid).get("status"), "dropped")

    def test_scan_includes_proposed(self):
        """E-06：巡检必须提醒"有待批准的"。"""
        self.wfk("new", "待批准", "--scope", "src/d/**", "--kind", "hygiene",
                 "--verify", "A1: MANUAL: by=st how=人工核查 check.py 输出")
        code, out = self.wfk("scan", "--json")
        data = json.loads(out[out.index("{"):])
        self.assertTrue(data["awaiting_approval"], out)
        self.assertIn("T-0001", data["awaiting_human"])

    def test_new_without_template_is_clean_error(self):
        """N-1：缺 _template.md 给一行可读错误，不抛 Traceback。"""
        (self.root / "docs/issues/_template.md").unlink()
        code, out = self.wfk("new", "无模板", "--scope", "src/e/**")
        self.assertNotEqual(code, 0)
        self.assertNotIn("Traceback", out)
        self.assertIn("_template.md", out)

    def test_review_note_threshold(self):
        """N-2：BLOCK 理由阈值与 help 文案一致（>=10 字）。"""
        code, out = self.wfk("new", "盲评", "--scope", "src/f/**", "--kind", "hygiene",
                             "--verify", "A1: MANUAL: by=st how=人工核查 check.py 输出")
        iid = re.search(r"created\s+\S*?(T-\d+)-", out).group(1)
        code, out = self.wfk("review", iid, "--verdict", "BLOCK", "--by", "x", "--note", "太短")
        self.assertEqual(code, 2, out)
        code, out = self.wfk("review", iid, "--verdict", "BLOCK", "--by", "x",
                             "--note", "这个改动没有证明产物变好，需要补视觉证据")
        self.assertEqual(code, 0, out)


    def test_close_check_reads_branch_body(self):
        """合并前的关闭检查必须看**分支上的**账本正文（主仓那份还是占位符）。"""
        iid = self.new_started()
        wt = self.wt(iid)
        self.wfk("gate", iid, "g1", cwd=wt)
        self.assertEqual(self.issue_meta(iid).get("status"), "in_review")
        src = next((self.root / "docs/issues").glob(f"{iid}-*.md"))
        (wt / "docs/issues").mkdir(parents=True, exist_ok=True)
        dst = wt / "docs/issues" / src.name
        body = src.read_text(encoding="utf-8")
        body = body.replace("（开工时留空；关闭前必填。）",
                            "在扉页首段插入 pageBreakBefore，让封面与扉页各占一页，不动节结构。")
        body = body.replace("（关闭前必填。）",
                            "prod-p2 从 9.9KB 空页变成 67KB；渲染前 3 页无空白页。")
        dst.write_text(body, encoding="utf-8")
        self.commit_all("补账本正文", cwd=wt)
        code, out = self.wfk("gate", iid, "g1", cwd=wt)   # 新提交 → 证据要重跑
        self.assertEqual(code, 0, out)
        code, out = self.wfk("merge", iid)
        self.assertEqual(code, 0, out)


    def test_stats_fields_are_actually_fed(self):
        """统计不能是空转：墙钟由 gate 记，回工由重开工记，盲评绑提交。"""
        iid = self.new_started()
        wt = self.wt(iid)
        self.wfk("gate", iid, "g1", cwd=wt)
        meta = self.issue_meta(iid)
        self.assertGreaterEqual(int(meta.get("cost_wall_min") or 0), 1, "gate 没记墙钟")
        self.wfk("review", iid, "--verdict", "PASS", "--by", "blind-x",
                 "--note", "证据支持断言，可合并")
        meta = self.issue_meta(iid)
        self.assertIn("@", meta.get("review", ""), "review 没绑提交")
        self.assertEqual(len(meta.get("reviews") or []), 1)
        # 回工：把状态退回 blocked 再开工一次（走 worktree 原地重试）
        self.patch_meta(iid, status='"blocked"', blocked_reason='"模拟回工"')
        code, out = self.wfk("start", iid, "--owner", "st2")
        self.assertEqual(code, 0, out)
        self.assertEqual(int(self.issue_meta(iid).get("rework") or 0), 1)


    def test_tier_needs_every_step(self):
        """一层里有一个步骤失败，整层不能算 OK。

        2026-11-18 实测（EZI-0043 工人）：g0 = compileall PASS + tests-map FAIL，
        矩阵却显示 g0 OK —— 通过步骤把失败步骤盖住了。
        """
        cfg_path = self.root / "workflow.toml"
        cfg_path.write_text(
            cfg_path.read_text(encoding="utf-8")
            + '\n[[gate.g1.steps]]\nname = "always-fail"\n'
              'cmd = "{py} -c \\"raise SystemExit(1)\\""\n',
            encoding="utf-8")
        self.commit_all("加一个必败步骤")
        iid = self.new_started()
        wt = self.wt(iid)
        code, out = self.wfk("gate", iid, "g1", cwd=wt)
        self.assertEqual(code, 1, out)          # 层里有失败 → 非零
        self.assertEqual(self.issue_meta(iid).get("status"), "in_progress")
        code, out = self.wfk("gate", iid, "status", cwd=wt)
        self.assertEqual(code, 2, out)
        self.assertIn("always-fail", out)

    def test_evidence_from_other_wfk_version_is_rejected(self):
        """换了尺子版本，旧证据必须失效（否则工具改了、结论还算数）。"""
        iid = self.new_started()
        wt = self.wt(iid)
        self.wfk("gate", iid, "g1", cwd=wt)
        self.assertEqual(self.wfk("gate", iid, "status", cwd=wt)[0], 0)
        # 篡改：把证据里的版本号改成别的版本
        ledger = next((self.root / "docs/issues").glob(f"{iid}-*.md"))
        text = ledger.read_text(encoding="utf-8")
        new = re.sub(r"wfk=[0-9.]+", "wfk=0.0.1", text)
        self.assertNotEqual(new, text, "证据里没有 wfk= 版本号")
        ledger.write_text(new, encoding="utf-8")
        self.commit_all("伪造成别的版本的证据")
        code, out = self.wfk("gate", iid, "status", cwd=wt)
        self.assertEqual(code, 2, out)
        self.assertIn("wfk", out.lower())


    def test_dotted_tier_evidence_normalized(self):
        """CLI 层名 g2.5（点号）→ 证据与矩阵一律按 TOML 键形 g2_5（0.6.1）。

        0.6.0 实撞（EZI-0053 首飞 g2.5）：tier_steps 查表归一化了（g2.5→g2_5），
        证据写入却用原始串 → 证据行是 G:g2.5:…，compute_readiness 找 G:g2_5:…
        → 步骤真跑过、矩阵永远认不到、issue 永远不 ready。
        """
        cfg_path = self.root / "workflow.toml"
        cfg_path.write_text(
            cfg_path.read_text(encoding="utf-8").replace(
                'engine = ["g1", "g2"]', 'engine = ["g2_5"]')
            + '\n[[gate.g2_5.steps]]\nname = "noop"\ncmd = "{py} -c pass"\n',
            encoding="utf-8")
        self.commit_all("engine 层改 g2_5 + noop 步骤")
        iid = self.new_started(kind="engine", scope="src/x.py")
        wt = self.wt(iid)
        code, out = self.wfk("gate", iid, "g2.5", cwd=wt)   # 点号 CLI
        self.assertEqual(code, 0, out)
        ledger = next((self.root / "docs/issues").glob(f"{iid}-*.md"))
        text = ledger.read_text(encoding="utf-8")
        self.assertIn("G:g2_5:noop PASS", text)             # 证据按 TOML 键形
        self.assertNotIn("G:g2.5:noop", text)
        code, out = self.wfk("gate", iid, "status", cwd=wt)  # 矩阵必须认到这层
        self.assertEqual(code, 2, out)                       # g1 仍未跑 → 未 ready（正确）
        self.assertIn("OK  g2_5", out)                      # g2_5 被认到（0.6.0 永远缺）

    def test_dotted_required_tier_reads_normalized_evidence(self):
        """required_tiers 的 g1.5（点号）必须认到下划线证据（0.6.1 读侧归一）。

        首飞反例：只归一写入不归一读取 → g2_5 修好了、g1.5 反而认不到新证据
        （required_tiers 用点号、证据按下划线写）——两种记法并存于同一配置，
        读写必须同归一到 TOML 键形。
        """
        cfg_path = self.root / "workflow.toml"
        cfg_path.write_text(
            cfg_path.read_text(encoding="utf-8").replace(
                'default = ["g1"]', 'default = ["g1.5"]')
            + '\n[[gate.g1_5.steps]]\nname = "noop15"\ncmd = "{py} -c pass"\n',
            encoding="utf-8")
        self.commit_all("default 层改 g1.5 + noop 步骤")
        iid = self.new_started()
        wt = self.wt(iid)
        code, out = self.wfk("gate", iid, "g1.5", cwd=wt)
        self.assertEqual(code, 0, out)
        ledger = next((self.root / "docs/issues").glob(f"{iid}-*.md"))
        text = ledger.read_text(encoding="utf-8")
        self.assertIn("G:g1_5:noop15 PASS", text)
        # 完成矩阵按 required_tiers 的 g1.5 判 → 认到下划线证据 → ready
        self.assertEqual(self.issue_meta(iid).get("status"), "in_review")

    def test_blocked_by_excuses_scope_overlap(self):
        """显式串行（blocked_by）必须能通过批准，否则"加 blocked_by"这句协议是空话。"""
        a = self.new_started(kind="hygiene", scope="src/**")
        b = self.new_issue("B 改同一文件", scope="src/**")
        self.patch_meta(b, blocked_by='["' + a + '"]')
        code, out = self.wfk("approve", b)
        self.assertEqual(code, 0, out)

    def test_ledger_commit_does_not_invalidate_evidence(self):
        """写账本不能让自己的证据过期——否则"填完证据就失效"是死循环。

        2026-11-18 实测：EZI-0045 门禁全绿 → 工人补「决策记录/验证证据」并提交
        → HEAD 变了 → g0/g1/g1.5/g2 全部作废，重跑再写又作废。
        """
        iid = self.new_started()
        wt = self.wt(iid)
        self.assertEqual(self.wfk("gate", iid, "g1", cwd=wt)[0], 0)
        self.assertEqual(self.wfk("gate", iid, "status", cwd=wt)[0], 0)
        ledger = next((self.root / "docs/issues").glob(f"{iid}-*.md"))
        ledger.write_text(ledger.read_text(encoding="utf-8") + "\n## 决策记录\n\n只改账本，不动代码。\n",
                          encoding="utf-8")
        self.commit_all("补账本正文")
        code, out = self.wfk("gate", iid, "status", cwd=wt)
        self.assertEqual(code, 0, out)


    # ── 0.6.0：审计链耐久 ──

    def test_metrics_whitelist(self):
        """moves_metric 必须是 [metrics] 声明过的指标（对不上账的指标名等于没写）。"""
        self._write_cfg('\n[metrics]\nclean_pass = "清洗集"\n"corpus.fail_cluster.*" = "靶向立项"\n')
        self.commit_all("声明指标")
        iid = self.new_issue("指标白名单", kind="engine", scope="src/x.py")
        self.patch_meta(iid, moves_metric='"not_a_metric"')
        code, out = self.wfk("approve", iid)
        self.assertEqual(code, 2, out)
        self.assertIn("[metrics]", out)
        # 前缀通配：corpus.fail_cluster.<判定项> 被 "corpus.fail_cluster.*" 接受
        self.patch_meta(iid, moves_metric='"corpus.fail_cluster.title_size"')
        code, out = self.wfk("approve", iid)
        self.assertEqual(code, 0, out)

    def test_log_runid_not_overwritten(self):
        """0.6.0：重跑不覆盖旧日志（run-id）；同层同提交重跑覆盖证据行，不堆重复。"""
        iid = self.new_started()
        wt = self.wt(iid)
        self.wfk("gate", iid, "g1", cwd=wt)
        self.wfk("gate", iid, "g1", cwd=wt)      # 同一提交再跑一遍
        logs = sorted((self.root / ".wf" / "gate").glob(f"{iid}-check-*.log"))
        self.assertEqual(len(logs), 2, "两次运行应留下两份日志（旧的不再被覆盖）")
        meta = self.issue_meta(iid)
        evs = [e for e in meta.get("evidence") or [] if str(e).startswith("G:g1:check ")]
        self.assertEqual(len(evs), 1, "同提交重跑应覆盖证据行，而不是追加重复行")

    def test_merge_requires_log_pointer(self):
        """0.6.0：PASS 证据指向的日志不存在（被清掉）→ 拒绝合并。"""
        iid = self.new_started()
        wt = self.wt(iid)
        self.wfk("gate", iid, "g1", cwd=wt)
        self.assertEqual(self.issue_meta(iid).get("status"), "in_review")
        for p in (self.root / ".wf" / "gate").glob(f"{iid}-*.log"):
            p.unlink()
        code, out = self.wfk("merge", iid)
        self.assertEqual(code, 2, out)
        self.assertIn("不存在的日志", out)

    def test_merge_review_timing(self):
        """0.6.0：盲评绑在旧提交上不能跟着新提交合并；用户豁免（review_waived_by）才行。"""
        iid = self.new_started()
        wt = self.wt(iid)
        self.wfk("gate", iid, "g1", cwd=wt)
        self.wfk("review", iid, "--verdict", "PASS", "--by", "blind-x",
                 "--note", "证据支持断言")
        self.fill_branch_body(iid)                       # 新提交 → 分支 HEAD 前移
        self.wfk("gate", iid, "g1", cwd=wt)             # 证据刷新到新 HEAD
        code, out = self.wfk("merge", iid)
        self.assertEqual(code, 2, out)
        self.assertIn("review_waived_by", out)
        self.patch_meta(iid, review_waived_by='"用户本人"')
        code, out = self.wfk("merge", iid)
        self.assertEqual(code, 0, out)

    def test_merge_receipt_from_wfk_merge(self):
        """0.6.0：wfk merge 写回执（wfk@<合并提交>）；finish --merged 凭回执放行。"""
        iid = self.new_started()
        wt = self.wt(iid)
        self.wfk("gate", iid, "g1", cwd=wt)
        self.fill_branch_body(iid)
        self.wfk("gate", iid, "g1", cwd=wt)
        code, out = self.wfk("merge", iid)
        self.assertEqual(code, 0, out)
        receipt = str(self.issue_meta(iid).get("merge_receipt") or "")
        self.assertTrue(receipt.startswith("wfk@"), f"merge 没写回执：{receipt}")
        code, out = self.wfk("finish", iid, "--merged")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.issue_meta(iid).get("status"), "merged")

    def test_merge_receipt_blocks_laundered_merge(self):
        """0.6.0：手工 git merge 绕过合并闸后，finish --merged 拒绝洗白；
        用户签字（manual_merged_by）+ --manual-merge 才能收尾。"""
        iid = self.new_issue("手工合并", kind="hygiene", scope="src/x.py")
        self.wfk("approve", iid)
        self.commit_all("账本入账 base")     # 分叉点带上账本：手工 merge 才是内容级 3-way
        self.wfk("start", iid, "--owner", "st")
        wt = self.wt(iid)
        self.wfk("gate", iid, "g1", cwd=wt)                       # 证据写主仓账本
        self.commit_all("账本入账")                                # 主仓：front-matter 变化（evidence/status）
        # 0.6.2 夹具修正：账本只有 16 行，主仓 FM 变化与分支正文变化落在同一个合并
        # hunk——两侧各改一边必然冲突（外部环境 32/33 的真因，本机此前是「冲突后
        # 碰巧对上断言」绿）。真实流程是「账本双份同步」：工人把主仓账本（含 gate
        # 证据）同步进 worktree 再改正文——两侧 FM 变更一致，3-way 干净合并。
        src_wt = next((wt / "docs/issues").glob(f"{iid}-*.md"))
        main_ledger = next((self.root / "docs/issues").glob(f"{iid}-*.md"))
        body = main_ledger.read_text(encoding="utf-8")
        body = body.replace("（开工时留空；关闭前必填。）",
                            "在扉页首段插入 pageBreakBefore，让封面与扉页各占一页，不动节结构。")
        body = body.replace("（关闭前必填。）",
                            "prod-p2 从 9.9KB 空页变成 67KB；渲染前 3 页无空白页。")
        # Path.write_text 走 universal newline 转换（LF→CRLF）——副本行尾与主仓
        # 不同，git 视全文件改动，3-way 必冲突（0.6.2 找到的真因，autocrlf 管不住它）。
        # 凡写账本副本一律 newline='' 保真。
        with open(src_wt, "w", encoding="utf-8", newline="") as f:
            f.write(body)
        self.commit_all("补账本正文（同步主仓账本）", cwd=wt)
        self.git("merge", "--no-ff", f"feat/{iid}", "-m", "manual merge")
        code, out = self.wfk("finish", iid, "--merged")
        self.assertEqual(code, 1, out)
        self.assertIn("merge_receipt", out)
        self.patch_meta(iid, manual_merged_by='"用户本人"')
        code, out = self.wfk("finish", iid, "--merged")   # 签了字但没带 --manual-merge
        self.assertEqual(code, 1, out)
        code, out = self.wfk("finish", iid, "--merged", "--manual-merge")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.issue_meta(iid).get("status"), "merged")

    def test_lock_atomic(self):
        """0.6.3：锁换 OS 互斥原语（R02/R03）——同资源双持在原语层不可能；
        释放可重获；持有者退出后锁自动释放（无僵尸、无心跳、无墓碑）。"""
        sys.path.insert(0, str(WFK.parent))
        import wfk  # noqa: PLC0415

        a = wfk.Lock(self.root, "res-x", "T-0001")
        a.acquire()
        self.assertTrue(a.held)
        # 同进程双持必须拒（R02：issue id 不表示锁所有权）
        b = wfk.Lock(self.root, "res-x", "T-0002")
        with self.assertRaises(SystemExit):
            b.acquire()
        self.assertFalse(b.held)
        a.release()
        self.assertFalse(a.held)
        # 释放后重获；锁文件保留复用（活性由 OS 句柄保证，不靠文件存在性）
        c = wfk.Lock(self.root, "res-x", "T-0003")
        c.acquire()
        self.assertTrue(c.held)
        self.assertTrue(c.path.exists())
        c.release()
        d = wfk.Lock(self.root, "res-x", "T-0004")
        d.acquire()
        d.release()
        # 跨进程：子进程持有时父进程被拒；子进程退出后自动释放（kill -9 同理，
        # 内核关句柄）——旧心跳/墓碑协议的整个竞态面从根上不存在
        import subprocess as _sp
        import sys as _sys
        _child = "; ".join([
            "import sys, time",
            f"sys.path.insert(0, r'{WFK.parent}')",
            "from pathlib import Path",
            "from wfk import Lock",
            f"l = Lock(Path(r'{self.root}'), 'res-y', 'T-9')",
            "l.acquire()",
            "print('HELD', flush=True)",
            "time.sleep(8)",
        ])
        proc = _sp.Popen([_sys.executable, "-c", _child], stdout=_sp.PIPE, text=True)
        try:
            self.assertEqual(proc.stdout.readline().strip(), "HELD")
            e = wfk.Lock(self.root, "res-y", "T-0005")
            with self.assertRaises(SystemExit):
                e.acquire()                      # 被别的进程持有 → die
        finally:
            proc.stdout.close()
            proc.terminate()
            proc.wait(timeout=10)
        f = wfk.Lock(self.root, "res-y", "T-0006")
        f.acquire()                                   # 持有者死亡 → 立即可获
        f.release()

    def test_stats_hides_zero_cost(self):
        """0.6.0：cost_tokens 无写入方时显示"未记录"，不假报 0。"""
        code, out = self.wfk("stats")
        self.assertEqual(code, 0, out)
        self.assertIn("未记录", out)
        self.assertNotIn("0 tokens", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)



class Regression062(Base):
    '''0.6.2：外部评审（gpt6.2 astra）19 个反例的正式回归——修复后必须全部拒绝错误行为。'''

    def _marker_write(self, data: dict) -> None:
        d = self.root / '.wf' / 'gate'
        d.mkdir(parents=True, exist_ok=True)
        (d / 'revert-inflight.json').write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')

    def test_readonly_never_recovers(self):
        '''F01/R16：存活 PID 标记下只读命令 0 字节改动，标记保留。'''
        iid = self.new_issue('只读恢复', scope='src/**')
        (self.root / 'src/x.py').write_text('# uncommitted work', encoding='utf-8')
        before = (self.root / 'src/x.py').read_text(encoding='utf-8')
        self._marker_write({'tag': 't', 'pid': os.getpid(), 'paths': ['src/x.py']})
        code, out = self.wfk('list')
        self.assertEqual(code, 0, out)
        self.assertIn('recover-skip', out)
        self.assertEqual((self.root / 'src/x.py').read_text(encoding='utf-8'), before)
        self.assertTrue((self.root / '.wf/gate/revert-inflight.json').is_file())

    def test_recovery_live_pid_untouched(self):
        '''F01：变异进程还活着 → 状态命令也不动工作区（它自己会收尾）。'''
        iid = self.new_issue('活进程', scope='src/**')
        (self.root / 'src/x.py').write_text('# mutated-stale', encoding='utf-8')
        self._marker_write({'tag': 't', 'pid': os.getpid(), 'paths': ['src/x.py']})
        code, out = self.wfk('approve', iid)
        self.assertEqual(code, 0, out)
        self.assertIn('仍在运行', out)
        self.assertEqual((self.root / 'src/x.py').read_text(encoding='utf-8'), '# mutated-stale')
        self.assertTrue((self.root / '.wf/gate/revert-inflight.json').is_file())

    def test_recovery_hash_match_restores(self):
        '''F01：v2 标记 + 当前内容==记录变异哈希 → 自动还原到 HEAD、删标记。'''
        import hashlib
        iid = self.new_issue('哈希匹配', scope='src/**')
        mutated = '# mutated'
        (self.root / 'src/x.py').write_text(mutated, encoding='utf-8')
        self._marker_write({'tag': 't', 'pid': 0, 'files': [
            {'path': 'src/x.py', 'mutated_sha256': hashlib.sha256(mutated.encode()).hexdigest()}]})
        code, out = self.wfk('approve', iid)
        self.assertEqual(code, 0, out)
        self.assertEqual((self.root / 'src/x.py').read_text(encoding='utf-8'), '# good' + NL)
        self.assertFalse((self.root / '.wf/gate/revert-inflight.json').is_file())

    def test_recovery_new_content_refused(self):
        '''F01：当前内容 != 记录变异（有人写了新内容）→ 拒绝覆盖、标记保留。'''
        import hashlib
        iid = self.new_issue('新内容拒绝', scope='src/**')
        (self.root / 'src/x.py').write_text('# someone new work', encoding='utf-8')
        self._marker_write({'tag': 't', 'pid': 0, 'files': [
            {'path': 'src/x.py', 'mutated_sha256': hashlib.sha256(b'# mutated').hexdigest()}]})
        code, out = self.wfk('approve', iid)
        self.assertEqual(code, 0, out)
        self.assertIn('STOP', out)
        self.assertEqual((self.root / 'src/x.py').read_text(encoding='utf-8'), '# someone new work')
        self.assertTrue((self.root / '.wf/gate/revert-inflight.json').is_file())

    def test_step_mutating_worktree_is_invalid_run(self):
        '''F02/R09：步骤运行中改源码、退出 0 → 记 FAIL 不记 clean PASS。'''
        mut = ['import pathlib', chr(34) + 'pathlib.Path(' + chr(39) + 'src/x.py' + chr(39) + ').write_text(' + chr(39) + '# mutated' + chr(39) + ')' + chr(34)]
        (self.root / 'mutator.py').write_text('import pathlib' + NL + 'import ' + chr(34) + chr(34) + chr(34) + NL, encoding='utf-8')
        mut_src = NL.join([
            'import pathlib',
            'target = ' + chr(34) + 'src/x.py' + chr(34),
            'content = ' + chr(34) + '# mutated' + chr(34),
            'pathlib.Path(target).write_text(content)',
        ])
        (self.root / 'mutator.py').write_text(mut_src + NL, encoding='utf-8')
        self._write_cfg(extra=NL + '[[gate.g2.steps]]' + NL + 'name = ' + chr(34) + 'mutate' + chr(34) + NL
                        + 'cmd = ' + chr(34) + '{py} mutator.py' + chr(34) + NL + 'timeout = 60' + NL)
        self.commit_all('cfg+mutator 入提交树（worktree 检出自提交树）')
        iid = self.new_started()
        wt = self.wt(iid)
        code, out = self.wfk('gate', iid, 'g2', '--doc', str(self.root / 'x.docx'), cwd=wt)
        self.assertIn('FAIL', out)
        self.assertIn('指纹不一致', out)
        ev = NL.join(str(e) for e in self.issue_meta(iid).get('evidence') or [])
        self.assertIn('G:g2:mutate FAIL', ev)
        self.assertNotIn('G:g2:mutate PASS', ev)

    def test_step_env_applied_in_gate(self):
        '''F14/R14：步骤 env 空值=从子进程环境删除该变量（gate 与 verify 同语义）。'''
        probe_src = NL.join([
            'import os, sys',
            'sys.exit(1 if os.environ.get(' + chr(34) + 'WFK_T14_VAR' + chr(34) + ') is not None else 0)',
        ])
        (self.root / 'env_probe.py').write_text(probe_src + NL, encoding='utf-8')
        extra = (NL + '[[gate.g2.steps]]' + NL + 'name = ' + chr(34) + 'envprobe' + chr(34) + NL
                 + 'cmd = ' + chr(34) + '{py} env_probe.py' + chr(34) + NL
                 + 'env = { WFK_T14_VAR = ' + chr(34) + chr(34) + ' }' + NL
                 + 'timeout = 60' + NL)
        self._write_cfg(extra=extra)
        self.commit_all('cfg: 附加门禁步骤入提交树')
        os.environ['WFK_T14_VAR'] = 'X'
        try:
            iid = self.new_started()
            wt = self.wt(iid)
            self.wfk('gate', iid, 'g2', '--doc', str(self.root / 'x.docx'), cwd=wt)
            ev = NL.join(str(e) for e in self.issue_meta(iid).get('evidence') or [])
            self.assertIn('G:g2:envprobe PASS', ev)
        finally:
            os.environ.pop('WFK_T14_VAR', None)

    def test_latest_block_overrides_pass(self):
        '''F06/R11：同提交先 PASS 后 BLOCK → merge 必须拒绝（最新结论定夺）。'''
        iid = self.new_started()
        wt = self.wt(iid)
        self.wfk('gate', iid, 'g1', cwd=wt)
        self.fill_branch_body(iid)
        self.wfk('review', iid, '--verdict', 'PASS', '--by', 'a', '--note', 'first look')
        self.wfk('review', iid, '--verdict', 'BLOCK', '--by', 'a',
                 '--note', '组合问题：该修复与主干新改动语义冲突')
        code, out = self.wfk('merge', iid)
        self.assertEqual(code, 2, out)
        self.assertIn('最新盲评是 BLOCK', out)

    def test_combined_invalid_merge_blocked(self):
        '''F03/R10：两侧各自合法、组合非法 → 候选树验证拦下，主干不动。'''
        (self.root / 'src/y.py').write_text('Y = 1' + NL, encoding='utf-8')
        (self.root / 'src/x.py').write_text('X = 1' + NL, encoding='utf-8')
        self.commit_all('base: X=1 Y=1')
        combo_src = NL.join([
            'import pathlib, sys',
            'x = int(pathlib.Path(' + chr(34) + 'src/x.py' + chr(34) + ').read_text().split(' + chr(34) + '=' + chr(34) + ')[1])',
            'y = int(pathlib.Path(' + chr(34) + 'src/y.py' + chr(34) + ').read_text().split(' + chr(34) + '=' + chr(34) + ')[1])',
            'sys.exit(1 if x + y > 3 else 0)',
        ])
        (self.root / 'combo.py').write_text(combo_src + NL, encoding='utf-8')
        extra = (NL + '[[gate.merge_check.steps]]' + NL + 'name = ' + chr(34) + 'combo' + chr(34) + NL
                 + 'cmd = ' + chr(34) + '{py} combo.py' + chr(34) + NL + 'timeout = 60' + NL)
        self._write_cfg(extra=extra)
        iid = self.new_started()
        wt = self.wt(iid)
        (wt / 'src/x.py').write_text('X = 2' + NL, encoding='utf-8')
        self.commit_all('X 1->2', cwd=wt)
        self.wfk('gate', iid, 'g1', cwd=wt)
        self.fill_branch_body(iid)
        (self.root / 'src/y.py').write_text('Y = 2' + NL, encoding='utf-8')
        self.commit_all('Y 1->2')
        main_before = self.git('rev-parse', 'HEAD')
        code, out = self.wfk('merge', iid)
        self.assertEqual(code, 2, out)
        self.assertIn('合并候选树验证失败', out)
        self.assertEqual(self.git('rev-parse', 'HEAD'), main_before)

    def test_ledger_only_commit_keeps_evidence(self):
        '''F15/R15：分支纯账本提交（源码树不动）→ 证据仍有效直接可合；0.6.1 会烧证据。'''
        iid = self.new_started()
        wt = self.wt(iid)
        self.wfk('gate', iid, 'g1', cwd=wt)
        self.fill_branch_body(iid)   # 只提交账本正文，src/ 一字未动
        code, out = self.wfk('merge', iid)   # 不重跑 g1：0.6.1 必拒，0.6.2 放行
        self.assertEqual(code, 0, out)
        receipt = str(self.issue_meta(iid).get('merge_receipt') or '')
        self.assertTrue(receipt.startswith('wfk@'), receipt)

    def test_newline_title_roundtrip(self):
        '''F11/R06：标题带换行 → 账本仍合法 TOML 且标题往返保真（0.6.1 写废文件）。'''
        title = '标题带换行' + NL + '第二行'
        iid = self.new_issue(title, scope='src/**')
        self.assertEqual(self.issue_meta(iid).get('title'), title)

    def test_atomic_write_invalid_keeps_original(self):
        '''F11/R06：非法新内容 → 原文件逐字节不变（先校验后替换）。'''
        sys.path.insert(0, str(WFK.parent))
        import wfk
        f = self.root / 'docs/issues' / 'zz-atomic-probe.md'
        good = '+++' + NL + 'id = ' + chr(34) + 'zz' + chr(34) + NL + '+++' + NL + 'body'
        f.write_text(good, encoding='utf-8')
        bad = '+++' + NL + 'bad = [' + NL + '+++' + NL + 'body'
        try:
            wfk._atomic_write(f, bad)
            self.fail('非法内容必须 die')
        except SystemExit:
            pass
        self.assertEqual(f.read_text(encoding='utf-8'), good)

    def test_concurrent_issue_save_refused(self):
        '''F11/R07：两份旧快照互相保存 → 后者拒绝覆盖并要求重读（不静默吞证据）。'''
        sys.path.insert(0, str(WFK.parent))
        import wfk
        f = self.root / 'docs/issues' / 'zz-concurrent-probe.md'
        f.write_text('+++' + NL + 'id = ' + chr(34) + 'zz2' + chr(34) + NL + '+++' + NL + 'body', encoding='utf-8')
        a = wfk.Issue(f)
        b = wfk.Issue(f)
        a.meta['title'] = 'A'
        a.save()
        b.meta['title'] = 'B'
        try:
            b.save()
            self.fail('后写者必须拒绝')
        except SystemExit:
            pass
        self.assertEqual(wfk.Issue(f).meta.get('title'), 'A')

    def test_default_engine_matrix_includes_g2(self):
        '''F07/R01：默认 engine 完成矩阵含 g2（docstring 承诺兑现）；显式配置让位。'''
        sys.path.insert(0, str(WFK.parent))
        import wfk
        self.assertIn('g2', wfk.required_tiers({}, 'engine'))
        self.assertNotIn('g2', wfk.required_tiers({}, 'measure'))
        self.assertEqual(wfk.required_tiers({'policy': {'required_tiers':
            {'engine': ['g0']}}}, 'engine'), ['g0', 'g1', 'g1.5'])



class Regression063(Base):
    """0.6.3：外部评审第二包反例转正式回归——锁族/scope 族/cfg 消费/断言绑定/回执拓扑。

    对应修复：R02/R03（OS 锁）、R05（glob 保守交集）、R04（未映射兜底全量）、
    R08（when_scope 按实际文件）、R12/R13/R18（cfg/log 消费 + 配置演进）、
    R19（断言覆盖）、F13 后半（回执拓扑）、R07（独占发号）。
    """

    def test_lock_atomic(self):
        # 基类版已覆盖 OS 锁全语义（同进程双持拒/释放重获/跨进程自动释放）
        super().test_lock_atomic()

    def test_lock_payload_writable_by_adopted_child(self):
        """EZI-0062 g2 实撞回归：wfk 持锁期间，gate 子进程（earlyz WordComLock 同 issue
        过继语义）必须能重写负载文件——OS 区锁只放 mutex 文件，不锁负载。"""
        sys.path.insert(0, str(WFK.parent))
        import json
        import wfk  # noqa: PLC0415

        a = wfk.Lock(self.root, "res-eco", "T-0001")
        a.acquire()
        try:
            child = ("import json, sys, time, pathlib; "
                     "p = pathlib.Path(sys.argv[1]); "
                     "cur = json.loads(p.read_text(encoding='utf-8')); "
                     "assert cur.get('issue') == 'T-0001', cur; "
                     "cur['heartbeat_at'] = time.time(); "
                     "p.write_text(json.dumps(cur), encoding='utf-8'); "
                     "print('CHILD-ADOPT-OK')")
            r = subprocess.run([sys.executable, "-c", child, str(a.path)],
                               capture_output=True, text=True)
            self.assertIn("CHILD-ADOPT-OK", r.stdout, r.stderr[:400])
            # 互斥不受影响：同资源第二个 wfk 实例仍被拒
            with self.assertRaises(SystemExit):
                wfk.Lock(self.root, "res-eco", "T-0002").acquire()
        finally:
            a.release()

    def test_scope_overlap_conservative(self):
        """R05：src/a*.py × src/*b.py 都能命中 src/ab.py——旧见证法报"无重叠"放行。"""
        sys.path.insert(0, str(WFK.parent))
        import wfk  # noqa: PLC0415
        self.assertIsNotNone(wfk.scope_overlap(['src/a*.py'], ['src/*b.py']))
        self.assertIsNotNone(wfk.scope_overlap(['src/**'], ['SRC/a.py']))   # 大小写不敏感
        self.assertIsNone(wfk.scope_overlap(['tests/**'], ['src/**']))     # 字面顶层可证明不相交
        self.assertIsNone(wfk.scope_overlap(['src/x.py'], ['src/x/**']))    # 文件 vs 其下目录
        # 批准阶段冲突提醒也必须吃到保守交集
        self._write_cfg()
        code, out = self.wfk("new", "甲", "--scope", "src/a*.py", "--kind", "hygiene",
                             "--verify", "A1: MANUAL: by=st how=看输出")
        self.assertEqual(code, 0, out)
        code, out = self.wfk("new", "乙", "--scope", "src/*b.py", "--kind", "hygiene",
                             "--verify", "A1: MANUAL: by=st how=看输出")
        self.assertEqual(code, 0, out)
        self.assertIn("[conflict]", out)

    def test_when_scope_runs_on_actual_files(self):
        """R08：声明 src/** + 条件 src/api/** + 实际改 src/api/new.py → 必须跑（旧法判"不涉及"）。"""
        extra = (NL + '[[gate.g1.steps]]' + NL + 'name = "api-check"' + NL
                 + 'cmd = "{py} check.py"' + NL + 'timeout = 60' + NL
                 + 'when_scope = ["src/api/**"]' + NL)
        self._write_cfg(extra=extra)
        self.commit_all('cfg: 附加步骤入主干', cwd=self.root)
        iid = self.new_started(scope="src/**")
        wt = self.wt(iid)
        (wt / "src/api").mkdir()
        (wt / "src/api/new.py").write_text("# new" + NL, encoding="utf-8")
        self.commit_all("改 api", cwd=wt)
        code, out = self.wfk("gate", iid, "g1", cwd=wt)
        self.assertEqual(code, 0, out)
        self.assertIn("api-check", out)
        meta = self.issue_meta(iid)
        self.assertTrue(any("api-check" in e and "PASS" in e for e in meta.get("evidence", [])))

    def test_skip_recompute_after_scope_creep(self):
        """R08：当初合法"不涉及"SKIP，改动后来蔓延进 when_scope → SKIP 不再算数。"""
        extra = (NL + '[[gate.g1.steps]]' + NL + 'name = "api-check"' + NL
                 + 'cmd = "{py} check.py"' + NL + 'timeout = 60' + NL
                 + 'when_scope = ["src/api/**"]' + NL)
        self._write_cfg(extra=extra)
        self.commit_all('cfg: 附加步骤入主干', cwd=self.root)
        iid = self.new_started(scope="src/**")
        wt = self.wt(iid)
        (wt / "src/plain.py").write_text("# plain" + NL, encoding="utf-8")
        self.commit_all("改无关文件", cwd=wt)
        code, out = self.wfk("gate", iid, "g1", cwd=wt)
        self.assertEqual(code, 0, out)
        self.assertIn("不涉及", out)                       # 合理跳过
        code, out = self.wfk("gate", iid, "status", cwd=wt)
        self.assertEqual(code, 0, out)                     # SKIP 被接受
        # 改动蔓延进 api 范围 → 同一条 SKIP 必须翻脸
        (wt / "src/api").mkdir()
        (wt / "src/api/new.py").write_text("# new" + NL, encoding="utf-8")
        self.commit_all("蔓延进 api", cwd=wt)
        code, out = self.wfk("gate", iid, "status", cwd=wt)
        self.assertEqual(code, 2, out)
        self.assertIn("api-check", out)
        code, out = self.wfk("gate", iid, "g1", cwd=wt)
        self.assertEqual(code, 0, out)
        code, out = self.wfk("gate", iid, "status", cwd=wt)
        self.assertEqual(code, 0, out)

    def test_unmapped_change_falls_back_to_default(self):
        """R04：改动文件不在 tests.map 覆盖内 → 回退默认全量，不静默跑子集。"""
        extra = (NL + '[tests]' + NL + 'default = "check.py"' + NL
                 + '[[tests.map]]' + NL + 'scope = "src/a/**"' + NL
                 + 'tests = ["check.py"]' + NL
                 + '[[gate.g1.steps]]' + NL + 'name = "sel"' + NL
                 + 'cmd = "{py} {tests}"' + NL + 'timeout = 60' + NL)
        self._write_cfg(extra=extra)
        self.commit_all('cfg: 附加步骤入主干', cwd=self.root)
        iid = self.new_started(scope="src/**")
        wt = self.wt(iid)
        (wt / "src/a").mkdir()
        (wt / "src/b").mkdir()
        (wt / "src/a/one.py").write_text("# a" + NL, encoding="utf-8")
        (wt / "src/b/two.py").write_text("# b" + NL, encoding="utf-8")
        self.commit_all("a 命中映射 + b 未映射", cwd=wt)
        code, out = self.wfk("gate", iid, "g1", cwd=wt)
        self.assertEqual(code, 0, out)
        self.assertIn("回退默认全量", out)
        self.assertIn("src/b/two.py", out)

    def test_r18_main_cfg_advance_blocks_merge(self):
        """R18：主干演进改了步骤命令 → 分支旧证据不能跟着进主干，merge 拒绝。"""
        iid = self.new_started()
        wt = self.wt(iid)
        self.wfk("gate", iid, "g1", cwd=wt)
        self.fill_branch_body(iid)
        # 主干推进：g1 步骤命令改了
        cfg = (self.root / "workflow.toml").read_text(encoding="utf-8")
        (self.root / "workflow.toml").write_text(cfg.replace('cmd = "{py} check.py"',
                                                             'cmd = "{py} check.py -q"'),
                                                encoding="utf-8")
        self.commit_all("主干：g1 命令演进")
        code, out = self.wfk("merge", iid)
        self.assertEqual(code, 2, out)
        self.assertIn("门禁配置变了", out)

    def test_assertion_coverage_binding(self):
        """R19：可执行断言必须绑 @step 且当前树有 PASS；未绑/绑空 merge 硬拦。"""
        iid = self.new_started()
        wt = self.wt(iid)
        self.wfk("gate", iid, "g1", cwd=wt)
        self.fill_branch_body(iid)
        # 未绑定的可执行断言 → merge 拒
        self.patch_meta(iid, verification='["A1: {py} check.py"]')
        code, out = self.wfk("merge", iid)
        self.assertEqual(code, 2, out)
        self.assertIn("断言覆盖", out)
        self.assertIn("@step", out)
        # 绑定到有当前树 PASS 的步骤 → 放行
        self.patch_meta(iid, verification='["A1: {py} check.py @step:g1.check"]')
        code, out = self.wfk("merge", iid)
        self.assertEqual(code, 0, out)
        code, out = self.wfk("finish", iid, "--merged", "--cleanup")
        self.assertEqual(code, 0, out)

    def test_receipt_topology(self):
        """F13 后半：回执必须是真合并提交——死串/非合并提交/别的分支都拒。"""
        iid = self.new_started()
        wt = self.wt(iid)
        self.wfk("gate", iid, "g1", cwd=wt)
        self.fill_branch_body(iid)
        self.patch_meta(iid, verification='["A1: {py} check.py @step:g1.check"]')
        code, out = self.wfk("merge", iid)
        self.assertEqual(code, 0, out)
        # 死字符串回执
        self.patch_meta(iid, merge_receipt='"wfk@deadbeef"')
        code, out = self.wfk("finish", iid, "--merged")
        self.assertNotEqual(code, 0, out)
        self.assertIn("不是本仓库的提交", out)
        # 指向单亲提交（非合并提交）
        tip = self.git("rev-parse", "HEAD^1")   # 单亲提交（合并前主干尖）
        self.patch_meta(iid, merge_receipt=f'"wfk@{tip}"')
        code, out = self.wfk("finish", iid, "--merged")
        self.assertNotEqual(code, 0, out)
        self.assertIn("不是合并提交", out)
        # 真回执放行
        msha = None
        for line in self.git("log", "--merges", "-1", "--format=%H").splitlines():
            msha = line.strip()
        self.patch_meta(iid, merge_receipt=f'"wfk@{msha}"')
        code, out = self.wfk("finish", iid, "--merged")
        self.assertEqual(code, 0, out)

    def test_exclusive_numbering_no_clobber(self):
        """R07：同号文件已存在 → 发下一个号，绝不覆盖已有卡（旧 write_text 会静默吞卡）。"""
        existing = self.root / "docs/issues" / "T-0001-占位.md"
        existing.write_text("+++" + NL + 'id = "T-0001"' + NL + "+++" + NL + "既有内容",
                            encoding="utf-8")
        code, out = self.wfk("new", "后继", "--scope", "src/**", "--kind", "hygiene",
                             "--verify", "A1: MANUAL: by=st how=看输出")
        self.assertEqual(code, 0, out)
        self.assertIn("T-0002", out)
        self.assertIn("既有内容", existing.read_text(encoding="utf-8"))
