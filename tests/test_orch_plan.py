#!/usr/bin/env python3
"""Coverage for `orch approve-request --plan` — run with `python3 tests/test_orch_plan.py`.

Plans used to be written to PLAN.md in the worker's worktree, where the
untracked file failed `cleanup-check` and blocked `worktree remove`. `--plan`
has orch store the plan under the orchestrator home instead.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ORCH = os.path.join(ROOT, "bin", "orch")


class PlanCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.board = os.path.join(self.tmp.name, "board")
        self.repo = os.path.join(self.tmp.name, "repo")
        os.makedirs(self.repo)
        subprocess.run(["git", "-C", self.repo, "init", "-q"], check=True)
        self.env = dict(os.environ, HERDR_ORCHESTRATOR_HOME=self.board)
        self.orch("init")
        self.orch("project", "add", "tw", "--path", self.repo)
        self.orch("add", "T-1", "--project", "tw", "--title", "first")

    def orch(self, *argv, expect=0, stdin=None):
        r = subprocess.run([sys.executable, ORCH] + list(argv), env=self.env,
                           input=stdin, capture_output=True, text=True, cwd=self.repo)
        self.assertEqual(r.returncode, expect,
                         "orch %s -> %s\n%s%s" % (" ".join(argv), r.returncode,
                                                  r.stdout, r.stderr))
        return r

    def approvals(self):
        return json.loads(self.orch("approvals", "--json").stdout)

    def submit(self, text, expect=0):
        return self.orch("approve-request", "T-1", "--kind", "plan", "--title", "go",
                         "--plan", "-", stdin=text, expect=expect)

    def test_stdin_plan_stored_outside_worktree(self):
        self.submit("## Approach\n- do it")
        (a,) = self.approvals()
        want = os.path.join(self.board, "plans", "T-1.md")
        self.assertEqual(a["plan_path"], want)
        with open(want) as fh:
            self.assertEqual(fh.read(), "## Approach\n- do it\n")
        status = subprocess.run(["git", "-C", self.repo, "status", "--porcelain"],
                                capture_output=True, text=True, check=True).stdout
        self.assertEqual(status, "")

    def test_resubmission_replaces_plan(self):
        self.submit("v1")
        self.submit("v2")
        with open(os.path.join(self.board, "plans", "T-1.md")) as fh:
            self.assertEqual(fh.read(), "v2\n")

    def test_plan_from_file(self):
        src = os.path.join(self.tmp.name, "draft.md")
        with open(src, "w") as fh:
            fh.write("from file\n")
        self.orch("approve-request", "T-1", "--kind", "plan", "--title", "go", "--plan", src)
        (a,) = self.approvals()
        self.assertEqual(a["plan_path"], os.path.join(self.board, "plans", "T-1.md"))

    def test_empty_plan_refused(self):
        r = self.submit("  \n", expect=1)
        self.assertIn("empty", r.stderr)
        self.assertEqual(self.approvals(), [])

    def test_unknown_task_leaves_no_file(self):
        self.orch("approve-request", "NOPE", "--kind", "plan", "--title", "go",
                  "--plan", "-", stdin="x", expect=1)
        self.assertFalse(os.path.exists(os.path.join(self.board, "plans", "NOPE.md")))

    def test_plan_and_plan_path_exclusive(self):
        self.orch("approve-request", "T-1", "--kind", "plan", "--title", "go",
                  "--plan", "-", "--plan-path", "x.md", stdin="x", expect=2)

    def test_plan_path_still_used_in_place(self):
        src = os.path.join(self.tmp.name, "p.html")
        open(src, "w").close()
        self.orch("approve-request", "T-1", "--kind", "plan", "--title", "go",
                  "--plan-path", src)
        (a,) = self.approvals()
        self.assertEqual(a["plan_path"], src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
