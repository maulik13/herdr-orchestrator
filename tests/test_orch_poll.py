"""`orch poll`: the web board's PR poller, run on its own.

It is the same loop as `server.py`'s (the poll tests cover its behaviour in
depth); what these cases pin is that the headless entry point actually runs it
— reacts to a merge and wakes the orchestrator — and refuses to start in a way
that would poll nothing.
"""
import os
import subprocess
import sys
import unittest

from test_webapp_poll import PollCase
from test_webapp_resolve import ORCH


class TestOrchPoll(PollCase):
    def poll(self, *argv):
        self.server_log = os.path.join(self.tmp.name, "orch-poll.log")
        self.server_fh = open(self.server_log, "w")
        self.addCleanup(self.server_fh.close)
        self.proc = subprocess.Popen([sys.executable, ORCH, "poll"] + list(argv),
                                     env=self.env, stdout=self.server_fh,
                                     stderr=subprocess.STDOUT, text=True)
        self.addCleanup(self.stop)

    def test_a_merge_moves_the_phase_and_wakes_the_orchestrator(self):
        self.park_on_an_open_pr()
        self.says("MERGED")
        self.poll("--seconds", "1")
        self.wait_for(lambda: self.task()["phase"] == "merged", "the merge")
        self.assertEqual(self.task()["pr_state"], "merged")
        self.assertEqual(len(self.handoffs()), 1)
        self.wait_for(lambda: self.prompts(), "the wake-up prompt")
        self.assertIn("T-1", self.prompts()[0])
        self.assertIsNone(self.proc.poll(), "the poller should keep running after a merge")

    def test_an_open_pr_is_left_alone(self):
        self.park_on_an_open_pr()
        self.poll("--seconds", "1")
        self.wait_ticks(3)
        self.assertEqual(self.task()["phase"], "pr-open")
        self.assertEqual(self.cards(), [])

    def test_zero_seconds_is_refused(self):
        r = subprocess.run([sys.executable, ORCH, "poll", "--seconds", "0"],
                           env=self.env, capture_output=True, text=True, timeout=30)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("--seconds", r.stderr)


if __name__ == "__main__":
    unittest.main()
