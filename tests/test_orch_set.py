#!/usr/bin/env python3
"""Coverage for `orch set` — run with `python3 tests/test_orch_set.py`.

Stdlib only, and every case runs against a throwaway board in a temp dir, so
this never touches the real state.json. The CLI is driven as a subprocess on
purpose: the bug this covers (OR-1) was in the argument parser, above anything
an in-process call to store would exercise.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ORCH = os.path.join(ROOT, "bin", "orch")
sys.path.insert(0, os.path.join(ROOT, "lib"))
import store  # noqa: E402


class BoardCase(unittest.TestCase):
    """A throwaway board with one registered project and one queued task."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.board = os.path.join(self.tmp.name, "board")
        self.repo = os.path.join(self.tmp.name, "repo")
        os.makedirs(self.repo)
        subprocess.run(["git", "-C", self.repo, "init", "-q"], check=True)
        self.env = dict(os.environ, HERDR_ORCHESTRATOR_HOME=self.board)
        self.orch("init")
        self.orch("project", "add", "tw", "--path", self.repo)
        self.orch("add", "T-1", "--project", "tw", "--title", "first")
        self.orch("add", "T-2", "--project", "tw", "--title", "second")
        self.addCleanup(self.tmp.cleanup)

    def orch(self, *argv, expect=0):
        r = subprocess.run([sys.executable, ORCH] + list(argv), env=self.env,
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, expect,
                         "orch %s -> %s\n%s%s" % (" ".join(argv), r.returncode,
                                                  r.stdout, r.stderr))
        return r

    def task(self, key="T-1"):
        return json.loads(self.orch("show", key, "--json").stdout)


class TestIntakeFields(BoardCase):
    def test_done_when_alongside_other_flags(self):
        """OR-1: --done-when used to abort the whole call and write nothing."""
        self.orch("set", "T-1", "--branch", "b1", "--done-when", "suite passes",
                  "--note", "n1")
        t = self.task()
        self.assertEqual(t["done_when"], "suite passes")
        self.assertEqual(t["branch"], "b1")   # the collateral damage in PL-59
        self.assertEqual(t["note"], "n1")

    def test_url_and_source(self):
        """ap-1 had to be rm'd and re-added to attach a Jira URL."""
        self.orch("set", "T-1", "--url", "https://j/ap-1", "--source", "jira")
        t = self.task()
        self.assertEqual(t["url"], "https://j/ap-1")
        self.assertEqual(t["source"], "jira")

    def test_title(self):
        self.orch("set", "T-1", "--title", "rescoped")
        self.assertEqual(self.task()["title"], "rescoped")

    def test_identity_and_queue_slot_survive(self):
        """The whole point: correcting a field must not re-create the task."""
        before = self.task()
        self.orch("set", "T-1", "--done-when", "d", "--url", "u", "--title", "t")
        after = self.task()
        for field in ("id", "key", "order", "worker", "project", "created", "phase"):
            self.assertEqual(before[field], after[field], field)

    def test_leaves_siblings_alone(self):
        self.orch("set", "T-1", "--title", "changed")
        self.assertEqual(self.task("T-2")["title"], "second")


class TestClearing(BoardCase):
    def test_empty_string_clears(self):
        self.orch("set", "T-1", "--url", "https://j/ap-1")
        self.orch("set", "T-1", "--url", "")
        self.assertIsNone(self.task()["url"])

    def test_whitespace_only_clears(self):
        self.orch("set", "T-1", "--done-when", "   ")
        self.assertIsNone(self.task()["done_when"])

    def test_values_are_stripped(self):
        self.orch("set", "T-1", "--done-when", "  suite passes  ")
        self.assertEqual(self.task()["done_when"], "suite passes")

    def test_title_cannot_be_cleared(self):
        r = self.orch("set", "T-1", "--title", "", expect=1)
        self.assertIn("title must be a non-empty string", r.stderr)
        self.assertEqual(self.task()["title"], "first")

    def test_refusal_writes_nothing_else(self):
        """A rejected field must not let its companions through."""
        self.orch("set", "T-1", "--branch", "b1", "--title", "", expect=1)
        self.assertIsNone(self.task()["branch"])


class TestGuards(BoardCase):
    def test_unknown_task(self):
        r = self.orch("set", "NOPE", "--title", "x", expect=1)
        self.assertIn("no task", r.stderr)

    def test_nothing_to_set(self):
        self.orch("set", "T-1", expect=1)

    def test_unknown_flag_still_rejected(self):
        self.orch("set", "T-1", "--project", "tw", expect=2)

    def test_output_reports_what_landed(self):
        out = json.loads(self.orch("set", "T-1", "--url", "").stdout)
        self.assertEqual(out, {"url": None})

    def test_trivial_toggles(self):
        self.orch("set", "T-1", "--trivial")
        self.assertTrue(self.task()["trivial"])
        self.orch("set", "T-1", "--no-trivial")
        self.assertFalse(self.task()["trivial"])


class TestReviewRoundGate(BoardCase):
    """The `pr-open` review gate, and the string that used to defeat it."""

    def test_zero_rounds_still_blocks_pr_open(self):
        """`--review-round 0` stored "0", which is truthy, and opened the gate."""
        self.orch("set", "T-1", "--review-round", "0")
        r = self.orch("phase", "T-1", "pr-open", expect=1)
        self.assertIn("has had no review round", r.stderr)
        self.assertEqual(self.task()["phase"], "queued")

    def test_stored_as_int_not_string(self):
        self.orch("set", "T-1", "--review-round", "2")
        self.assertIsInstance(self.task()["review_round"], int)

    def test_a_real_round_opens_the_gate(self):
        self.orch("set", "T-1", "--review-round", "2")
        self.orch("phase", "T-1", "pr-open")
        self.assertEqual(self.task()["phase"], "pr-open")

    def test_bad_values_refused_at_the_parser(self):
        for bad in ("-1", "abc", "3.7", "", "1e3", "None"):
            self.orch("set", "T-1", "--review-round", bad, expect=2)
        self.assertEqual(self.task()["review_round"], 0)

    def test_refusal_does_not_write_companions(self):
        self.orch("set", "T-1", "--branch", "CLOBBER", "--review-round", "-1", expect=2)
        self.assertIsNone(self.task()["branch"])

    def test_whitespace_is_stripped_like_every_other_field(self):
        self.orch("set", "T-1", "--review-round", "  4  ")
        self.assertEqual(self.task()["review_round"], 4)

    def test_force_and_trivial_escapes_still_work(self):
        self.orch("phase", "T-1", "pr-open", "--force")
        self.assertEqual(self.task()["phase"], "pr-open")
        self.orch("set", "T-2", "--trivial")
        self.orch("phase", "T-2", "pr-open")
        self.assertEqual(self.task("T-2")["phase"], "pr-open")


class TestLegacyReviewRound(unittest.TestCase):
    """Values already on disk. There is no migration, so the gate reads
    defensively and fails closed."""

    def gate(self, raw):
        t = {"id": "t1", "key": "K", "phase": "needs-review", "title": "x",
             "trivial": False, "review_round": raw}
        try:
            store.set_phase({"tasks": [t], "log": []}, "t1", "pr-open")
            return "open"
        except ValueError:
            return "blocked"

    def test_legacy_string_count_still_opens(self):
        """OBJSTORE-1 holds the string "3"; three rounds did happen."""
        self.assertEqual(store.review_rounds({"review_round": "3"}), 3)
        self.assertEqual(self.gate("3"), "open")

    def test_legacy_string_zero_now_blocks(self):
        self.assertEqual(self.gate("0"), "blocked")

    def test_unparseable_fails_closed(self):
        for raw in ("abc", "", None, -1, "-1", True, [], {}):
            self.assertEqual(store.review_rounds({"review_round": raw}), 0, raw)
            self.assertEqual(self.gate(raw), "blocked", raw)

    def test_as_count_accepts_both_types(self):
        self.assertEqual(store.as_count("f", "3"), 3)
        self.assertEqual(store.as_count("f", 3), 3)
        self.assertEqual(store.as_count("f", " 0 "), 0)

    def test_as_count_rejects(self):
        for bad in ("-1", "abc", "3.7", "", None, True, 1.5, []):
            with self.assertRaises(ValueError, msg=repr(bad)):
                store.as_count("f", bad)

    def test_update_task_coerces_a_string_count(self):
        st = {"tasks": [{"id": "t1", "key": "K", "review_round": 0, "updated": ""}],
              "log": []}
        store.update_task(st, "t1", {"review_round": "3"})
        self.assertIs(type(st["tasks"][0]["review_round"]), int)

    def test_update_task_rejects_a_bad_count(self):
        st = {"tasks": [{"id": "t1", "key": "K", "review_round": 5, "updated": ""}],
              "log": []}
        with self.assertRaises(ValueError):
            store.update_task(st, "t1", {"review_round": "-2"})
        self.assertEqual(st["tasks"][0]["review_round"], 5)


class TestStoreLayer(unittest.TestCase):
    """update_task's rules, independent of the CLI."""

    def state(self):
        return {"tasks": [{"id": "t1", "key": "T-1", "title": "first",
                           "url": None, "updated": ""}], "log": []}

    def test_rejects_unlisted_field(self):
        with self.assertRaises(ValueError) as cm:
            store.update_task(self.state(), "t1", {"key": "OTHER"})
        self.assertIn("not an editable field", str(cm.exception))

    def test_key_and_project_are_not_editable(self):
        for field in ("key", "project", "id"):
            self.assertNotIn(field, store.EDITABLE_FIELDS)

    def test_unknown_task_raises_keyerror(self):
        with self.assertRaises(KeyError):
            store.update_task(self.state(), "nope", {"title": "x"})

    def test_logs_the_field_names(self):
        st = self.state()
        store.update_task(st, "t1", {"url": "u", "title": "t"})
        self.assertIn("T-1 set title, url", st["log"][-1])

    def test_bool_values_pass_through(self):
        """--no-trivial sends False; that must not read as "clear it"."""
        st = self.state()
        store.update_task(st, "t1", {"trivial": False})
        self.assertIs(st["tasks"][0]["trivial"], False)

    def test_none_clears_an_optional_field(self):
        st = self.state()
        store.update_task(st, "t1", {"url": None})
        self.assertIsNone(st["tasks"][0]["url"])

    def test_none_title_is_refused(self):
        """A None title is written before render_board runs, so it would
        durably brick the board for every other worker on the machine."""
        st = self.state()
        with self.assertRaises(ValueError) as cm:
            store.update_task(st, "t1", {"title": None})
        self.assertIn("must be a non-empty string", str(cm.exception))
        self.assertEqual(st["tasks"][0]["title"], "first")

    def test_non_string_title_is_refused(self):
        """render_board slices title, so any non-string is the same hazard."""
        for bad in (None, 0, 123, [], {}, False):
            st = self.state()
            with self.assertRaises(ValueError):
                store.update_task(st, "t1", {"title": bad})
            self.assertEqual(st["tasks"][0]["title"], "first")

    def test_blank_title_never_reaches_state(self):
        """The guard has to hold the invariant it claims: whatever is refused
        must also survive render_board if it ever did get through."""
        st = self.state()
        for bad in ("", "   ", None):
            with self.assertRaises(ValueError):
                store.update_task(st, "t1", {"branch": "b", "title": bad})
        self.assertIsNone(st["tasks"][0].get("branch"))   # companion not written


if __name__ == "__main__":
    unittest.main(verbosity=2)
