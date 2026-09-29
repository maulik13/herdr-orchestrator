#!/usr/bin/env python3
"""Coverage for the webapp's long-lived threads failing loudly (or-11).

`poll_prs` guarded `store.read` and the `gh` call but not its own transaction,
so one exception there ended the daemon thread: the webapp kept serving, the
board looked healthy, and no PR was checked again until someone restarted the
process. `run_plannotator_gate` had the same gap, with a worse aftermath — the
verdict was lost and `review_started` stayed set, so every later "Review in
Plannotator" was refused as already open.

Two halves, both tested here:

* **Survive.** A failed transaction is reported and the loop carries on. The
  failed transaction wrote nothing, so the next tick retries the same
  transition without any bookkeeping.
* **Be visible.** `/api/state` carries `_poll`: the thread's liveness, its last
  tick and its last error. Staleness is then read off the board, not inferred.

The survival case drives the real server with the board's lock file made
unopenable, which fails every transaction while `store.read` (lock-free) and
`gh` still succeed — exactly the shape that used to kill the thread. The
reporting and gate cases run in-process, because they need a transaction that
fails once and then works, and nothing on disk can be that selective.
"""
import contextlib
import importlib.util
import io
import json
import os
import stat
import time
import unittest
import urllib.request
from unittest import mock

from test_webapp_poll import PollCase, TICK_DEADLINE
from test_webapp_resolve import SERVER, WebappCase
import store


def load_server():
    spec = importlib.util.spec_from_file_location("orch_server_under_test", SERVER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class ApiStateMixin:
    def api_state(self):
        with urllib.request.urlopen(
                "http://127.0.0.1:%d/api/state" % self.port, timeout=10) as r:
            return json.loads(r.read())

    def wait_api(self, predicate, what, deadline=TICK_DEADLINE):
        end = time.time() + deadline
        while time.time() < end:
            if predicate(self.api_state()["_poll"]):
                return
            time.sleep(0.1)
        self.fail("timed out waiting for %s; _poll=%r — the server said:\n%s"
                  % (what, self.api_state()["_poll"], self.server_output()))


class TestATransactionFailureDoesNotEndPolling(ApiStateMixin, PollCase):
    def break_the_lock(self):
        lock = os.path.join(self.board, ".lock")
        open(lock, "a").close()
        os.chmod(lock, 0)
        self.addCleanup(os.chmod, lock, stat.S_IRUSR | stat.S_IWUSR)
        return lock

    def test_the_loop_survives_reports_and_recovers(self):
        if os.geteuid() == 0:
            self.skipTest("root opens a mode-0 file anyway")
        self.park_on_an_open_pr()
        self.serve(poll=1)
        self.wait_ticks(1)
        lock = self.break_the_lock()
        self.says("CLOSED")

        # Several failing ticks in a row: the first one used to be the last.
        self.after_flip(n=3)
        poll = self.api_state()["_poll"]
        self.assertTrue(poll["alive"], "the poll thread died on a failed transaction")
        self.assertIsNotNone(poll["last_error"], "the failure is not visible on the board")
        self.assertIn("applying PR state", poll["last_error"]["error"])
        self.assertEqual(self.cards(), [], "nothing can have been written yet")

        # The failed ticks wrote nothing, so the next good one simply acts.
        os.chmod(lock, stat.S_IRUSR | stat.S_IWUSR)
        self.wait_for(lambda: len(self.cards()) == 1, "the close to card once the lock works")
        self.wait_api(lambda p: p["last_error"] is None, "the error to clear")
        self.more_ticks()
        self.assertEqual(len(self.cards()), 1)
        self.assertEqual(self.task()["pr_state"], "closed")
        self.assertIn("PR poll: applying PR state", self.server_output())


class TestHeartbeatIsServed(ApiStateMixin, PollCase):
    def test_a_running_poller_reports_recent_ticks(self):
        self.serve(poll=1)
        self.wait_api(lambda p: p["last_tick"] is not None, "a first tick")
        p = self.api_state()["_poll"]
        self.assertTrue(p["enabled"])
        self.assertTrue(p["alive"])
        self.assertEqual(p["interval"], 1)
        self.assertIsNone(p["last_error"])
        self.assertIsNone(p["off_reason"])

    def test_polling_off_says_so(self):
        self.serve(poll=0)
        p = self.api_state()["_poll"]
        self.assertFalse(p["enabled"])
        self.assertFalse(p["alive"])
        self.assertEqual(p["off_reason"], "--poll-seconds 0")


def flaky(n):
    """A `store.transaction` whose first `n` uses raise, then the real one."""
    real = store.transaction
    calls = {"n": 0}

    @contextlib.contextmanager
    def tx(pdir):
        calls["n"] += 1
        if calls["n"] <= n:
            raise OSError("disk went away")
        with real(pdir) as st:
            yield st
    return tx


class ServerInProcess(WebappCase):
    def setUp(self):
        super().setUp()
        self.srv = load_server()
        self.srv.PDIR = self.board

    def log_lines(self, needle):
        return [ln for ln in self.state().get("log", []) if needle in ln]

    def quietly(self, fn, *a):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            fn(*a)
        return err.getvalue()


class TestPollFailureReporting(ServerInProcess):
    def test_a_repeated_failure_is_logged_once_and_recovery_once(self):
        for _ in range(5):
            self.quietly(self.srv._poll_failed, "applying PR state", ValueError("boom"))
        self.assertEqual(len(self.log_lines("PR poll failed")), 1,
                         "a persistent fault would flood the 40-line log")
        self.assertIn("boom", self.srv.POLL["last_error"]["error"])

        self.srv._poll_ok()
        self.srv._poll_ok()
        self.assertIsNone(self.srv.POLL["last_error"])
        self.assertEqual(len(self.log_lines("PR poll recovered")), 1)

    def test_a_different_failure_is_logged_again(self):
        self.quietly(self.srv._poll_failed, "applying PR state", ValueError("one"))
        self.quietly(self.srv._poll_failed, "applying PR state", ValueError("two"))
        self.assertEqual(len(self.log_lines("PR poll failed")), 2)

    def test_a_board_that_cannot_be_written_does_not_raise(self):
        with mock.patch.object(store, "transaction", flaky(1)):
            out = self.quietly(self.srv._poll_failed, "applying PR state", OSError("x"))
        self.assertIn("PR poll: applying PR state", out)
        self.assertEqual(self.log_lines("PR poll failed"), [])
        # Not marked as logged, so the next occurrence tries the board again.
        self.quietly(self.srv._poll_failed, "applying PR state", OSError("x"))
        self.assertEqual(len(self.log_lines("PR poll failed")), 1)


class TestGateTransactionFailure(ServerInProcess):
    def gate_with(self, verdict, failures):
        plan = os.path.join(self.repo, "PLAN.md")
        with open(plan, "w") as fh:
            fh.write("# plan\n")
        aid = self.park_on_a_plan(plan)
        with store.transaction(self.board) as st:
            st["approvals"][0]["review_started"] = store.now()
        proc = mock.Mock(stdout=json.dumps(verdict) + "\n", returncode=0)
        with mock.patch.object(self.srv.subprocess, "run", return_value=proc), \
                mock.patch.object(store, "transaction", flaky(failures)):
            return self.quietly(self.srv.run_plannotator_gate, aid, plan)

    def test_a_lost_verdict_re_arms_the_review_and_keeps_the_feedback(self):
        out = self.gate_with({"decision": "annotated", "feedback": "fix bullet two"}, 1)
        a = self.state()["approvals"][0]
        self.assertEqual(a["status"], "pending", "the failed settle must not half-apply")
        self.assertIsNone(a["review_started"],
                          "left set, every later review is refused as already open")
        self.assertTrue(self.log_lines("could not be recorded"))
        self.assertIn("fix bullet two", out, "the annotations must survive somewhere")

    def test_a_board_that_stays_broken_does_not_raise(self):
        out = self.gate_with({"decision": "approved", "feedback": "ship it"}, 2)
        self.assertIn("ship it", out)
        self.assertEqual(self.state()["approvals"][0]["status"], "pending")


if __name__ == "__main__":
    unittest.main()
