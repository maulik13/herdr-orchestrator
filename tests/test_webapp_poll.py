#!/usr/bin/env python3
"""Coverage for the webapp's PR poll loop (or-10).

`poll_prs` watches `pr-open` tasks and asks `gh` what happened to the PR. A
merge moves the task on and so leaves the watch; a close does NOT — `pr_state`
becomes `closed`, a question card goes up, and the phase stays `pr-open`. The
loop filtered its watch on phase and `pr_url` only, so the next tick found the
same task, asked `gh` again, got CLOSED again, and raised another identical
card: one a minute at the default interval, until a human moved the phase by
hand. The human answers one card of N and the rest sit on the board as noise.

THE FIX IS TRANSITION-KEYED, and the two rejected alternatives are why these
tests are shaped the way they are:

* **Skip tasks whose `pr_state` is already `closed`.** Cheapest, and wrong: it
  drops the task from the watch forever, while "reopen it" is the answer the
  card itself invites. The human reopens, the PR merges, and nothing ever sees
  MERGED. `test_a_reopened_pr_that_merges_is_still_seen` is that guard — it is
  the one case that passes today and would fail under that shape.

* **Derive "already carded" from a pending approval.** It re-raises the instant
  a human answers, because answering does not move the phase either — the same
  loop, one card per answer instead of one per tick.
  `test_answering_the_card_does_not_re_raise_it` is that guard.

So the key is `pr_state`: the board's record of what GitHub last said is also
the record of what this loop has already reacted to. That makes `pr_state`
load-bearing rather than cosmetic — `orch set --pr-state closed` by hand
suppresses the next card — which is noted where the field is declared.

Every case drives the real server subprocess with a `gh` shim ahead of the real
binary on PATH. Nothing here reaches GitHub, and the board is a temp dir, so a
developer's own board on 8787 is untouched by a test run.
"""
import json
import os
import time

from test_webapp_resolve import WebappCase

# Prints whatever state the test currently wants and records the call, so a
# case can both steer the answer and count completed ticks. Reaching the real
# `gh` would ask GitHub about a made-up PR url.
GH_SHIM = """#!/bin/sh
printf '%s\\n' "$*" >> "$FAKE_GH_LOG"
[ -n "$FAKE_GH_DELAY" ] && sleep "$FAKE_GH_DELAY"
cat "$FAKE_GH_STATE"
exit 0
"""

PR_URL = "https://github.com/example/repo/pull/1"


class PollCase(WebappCase):
    """A board parked on an open PR, with `gh` answering whatever we say."""

    def setUp(self):
        super().setUp()
        self.gh_log = os.path.join(self.tmp.name, "gh.log")
        self.gh_state = os.path.join(self.tmp.name, "gh-state.json")
        shim = os.path.join(self.tmp.name, "bin", "gh")
        with open(shim, "w") as fh:
            fh.write(GH_SHIM)
        os.chmod(shim, 0o755)
        self.env.update(FAKE_GH_LOG=self.gh_log, FAKE_GH_STATE=self.gh_state)
        self.says("OPEN")

    def says(self, state):
        """Set what `gh pr view` answers from the next call on.

        Written through a rename so a call in flight reads one whole file or
        the other, never a torn one.
        """
        body = json.dumps({"state": state,
                           "mergedAt": "2026-01-01T00:00:00Z" if state == "MERGED" else None})
        tmp = self.gh_state + ".tmp"
        with open(tmp, "w") as fh:
            fh.write(body + "\n")
        os.replace(tmp, self.gh_state)

    def park_on_an_open_pr(self, pr_state="open"):
        for phase in ("planning", "implementing", "needs-review", "reviewing",
                      "resolving", "pr-open"):
            self.orch("phase", "T-1", phase)
        self.orch("set", "T-1", "--pr-url", PR_URL, "--pr-state", pr_state)
        self.orch("ack", "T-1")
        open(self.shim_log, "w").close()

    def ticks(self):
        """Completed `gh` calls so far."""
        if not os.path.exists(self.gh_log):
            return 0
        with open(self.gh_log) as fh:
            return len([ln for ln in fh.read().splitlines() if ln.strip()])

    def wait_ticks(self, n, deadline=45):
        """Block until the poll loop has completed at least `n` `gh` calls."""
        end = time.time() + deadline
        while time.time() < end:
            if self.ticks() >= n:
                return
            time.sleep(0.05)
        self.fail("only %d poll ticks in %ds, wanted %d — zero ticks means the "
                  "task was dropped from the watch, not that carding was "
                  "suppressed" % (self.ticks(), deadline, n))

    def wait_for(self, predicate, what, deadline=45):
        """Block until `predicate()` holds.

        Needed wherever the expected outcome takes the task OUT of the watch:
        a merge moves the phase to `merged`, so the poll loop has nothing left
        to ask about and no further tick will ever land. Waiting on a tick
        count there hangs until the deadline on a working fix.
        """
        end = time.time() + deadline
        while time.time() < end:
            if predicate():
                return
            time.sleep(0.05)
        self.fail("timed out after %ds waiting for %s" % (deadline, what))

    def after_flip(self, extra=3):
        """Wait out enough ticks that at least one saw the new `gh` answer.

        The call in flight when `says` landed may still be reading the old
        file, so a single extra tick is not enough to be sure.
        """
        self.wait_ticks(self.ticks() + extra)

    def cards(self):
        return [a for a in self.state().get("approvals", [])
                if a["title"] == "PR closed without merging"]

    def task(self):
        import store
        return store.find(self.state(), "T-1")


class TestClosedWithoutMerging(PollCase):
    def test_a_closed_pr_cards_once_across_many_ticks(self):
        """The bug, straight out of the issue: at the default 60s interval this
        was a new identical card every minute until a human intervened."""
        self.park_on_an_open_pr(pr_state="closed")   # already carded once before
        self.says("CLOSED")
        self.serve(poll=1)
        self.wait_ticks(3)
        self.assertEqual(self.cards(), [],
                         "a state already reacted to must not card again")

    def test_open_to_closed_raises_exactly_one_card(self):
        """The transition still cards — and only the transition does. Reverting
        the fix turns this into one card per tick."""
        self.park_on_an_open_pr()
        self.serve(poll=1)
        self.wait_ticks(2)                       # ticks that see it still OPEN
        self.assertEqual(self.cards(), [], "an open PR is not a question")

        self.says("CLOSED")
        self.after_flip()
        self.after_flip()                        # and several ticks beyond the close
        cards = self.cards()
        self.assertEqual(len(cards), 1,
                         "one close is one decision, got %d cards" % len(cards))
        self.assertEqual(cards[0]["status"], "pending")
        self.assertIn(PR_URL, cards[0]["body"])
        t = self.task()
        self.assertEqual(t["pr_state"], "closed")
        self.assertEqual(t["phase"], "pr-open", "closing does not move the phase")

    def test_answering_the_card_does_not_re_raise_it(self):
        """The scenario the issue is really about, and the guard on deriving
        "already carded" from `pr_state` rather than from an open card.

        The human answers, the phase stays `pr-open` because deciding what to
        do with the branch is the orchestrator's next move — so a card-derived
        check would raise a fresh one on the very next tick.
        """
        self.park_on_an_open_pr()
        self.says("CLOSED")
        self.serve(poll=1)
        self.after_flip()
        aid = self.cards()[0]["id"]

        self.assertEqual(self.post("/api/resolve",
                                   {"approval": aid, "decision": "approved",
                                    "note": "abandon the branch"}), {"ok": True})
        self.after_flip()
        self.after_flip()
        cards = self.cards()
        self.assertEqual(len(cards), 1,
                         "an answered card must not come back: %s"
                         % [(c["id"], c["status"]) for c in cards])
        self.assertEqual(cards[0]["status"], "approved")
        self.assertEqual(self.task()["phase"], "pr-open")


class TestReopening(PollCase):
    def test_reopening_re_arms_and_a_second_close_cards_again(self):
        """closed -> answered -> reopened -> closed again is two decisions.

        Recording the reopen is what makes the second one visible: without it
        `pr_state` sits lying at `closed` while GitHub says open, and the
        re-close is swallowed as a state already reacted to.
        """
        self.park_on_an_open_pr()
        self.says("CLOSED")
        self.serve(poll=1)
        self.after_flip()
        self.assertEqual(len(self.cards()), 1)
        self.orch("resolve", self.cards()[0]["id"], "--decision", "approved",
                  "--note", "reopen it")

        self.says("OPEN")
        self.after_flip()
        self.assertEqual(self.task()["pr_state"], "open", "the reopen is recorded")
        self.assertEqual(len(self.cards()), 1, "reopening is not itself a question")

        self.says("CLOSED")
        self.after_flip()
        self.after_flip()
        cards = self.cards()
        self.assertEqual(len(cards), 2,
                         "a second close is a second decision, got %d" % len(cards))
        self.assertEqual(len([c for c in cards if c["status"] == "pending"]), 1)

    def test_a_reopened_pr_that_merges_is_still_seen(self):
        """The guard on the rejected shape.

        Dropping `pr_state == "closed"` tasks from the watch would stop asking
        `gh` about exactly the PR the card invited the human to reopen: it is
        reopened, it merges, and the board never notices. This case passes with
        the transition-keyed fix and fails with the skip-if-closed one.
        """
        self.park_on_an_open_pr(pr_state="closed")   # carded and answered already
        self.says("MERGED")
        self.serve(poll=1)
        self.wait_for(lambda: self.task()["phase"] == "merged",
                      "the merge of a PR the board last saw closed")
        self.assertEqual(self.task()["pr_state"], "merged")


class TestMergedIsUnchanged(PollCase):
    """Regression guard rather than a reproduction: the merge path is what the
    poll loop existed for, and it runs before the new transition test, so this
    fails if the fix broke it — not if the fix is reverted."""

    def test_a_merged_pr_moves_the_phase_and_wakes_the_orchestrator(self):
        self.park_on_an_open_pr()
        self.says("MERGED")
        self.serve(poll=1)
        self.wait_for(lambda: self.task()["phase"] == "merged", "the merge")
        self.assertEqual(self.task()["pr_state"], "merged")
        self.assertEqual(self.cards(), [], "a merge is not a question")
        rows = self.handoffs()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["phase"], "merged")
        sent = self.prompts()
        self.assertTrue(sent, "a merge has to reach the orchestrator")
        self.assertIn("T-1", sent[0])


class TestSteadyStateIsQuiet(PollCase):
    def test_an_open_pr_neither_cards_nor_touches_the_board(self):
        """An ordinary open PR is the steady state — both live tasks on the
        real board are in it. OPEN is now carried out of the `gh` loop so a
        reopen can be recorded, so it has to be filtered against the snapshot:
        rewriting `open` over `open` every tick would put the orchestrator and
        every worker behind this thread's flock for nothing.
        """
        self.park_on_an_open_pr()
        self.serve(poll=1)
        self.wait_ticks(1)
        before = os.stat(os.path.join(self.board, "state.json")).st_mtime_ns
        self.wait_ticks(self.ticks() + 3)
        self.assertEqual(os.stat(os.path.join(self.board, "state.json")).st_mtime_ns,
                         before, "an unchanged open PR must not rewrite the board")
        self.assertEqual(self.cards(), [])
        self.assertEqual(self.task()["phase"], "pr-open")


class TestTheRaceUnderTheLock(PollCase):
    """The snapshot `gh` was asked about can be stale by the time it answers.

    The pre-lock filter is what stops the common case, and on its own it is a
    check against a copy of `pr_state` read BEFORE a round-trip that can take
    20 seconds. The board is writable throughout: a human, `orch set`, or the
    orchestrator can move `pr_state` while the call is in flight. Re-testing
    under the lock is what keeps the duplicate card from coming back through
    that window, and the pre-lock filter hides this path from every other case
    here — so without this one it is an untested claim in a comment.
    """

    def test_a_concurrent_write_during_the_gh_call_still_cards_once(self):
        self.park_on_an_open_pr()
        self.says("CLOSED")
        self.env["FAKE_GH_DELAY"] = "3"        # hold the round-trip open
        self.serve(poll=1)
        self.wait_ticks(1)                     # the call is in flight, not done

        # Someone records the close by hand while `gh` is still answering, so
        # the snapshot the loop is holding ("open") is now stale.
        self.orch("set", "T-1", "--pr-state", "closed")
        self.wait_for(lambda: self.ticks() >= 2, "the in-flight call to finish")
        time.sleep(1)                          # and the transaction behind it

        cards = self.cards()
        self.assertEqual(len(cards), 0,
                         "the close was already recorded; the stale snapshot "
                         "must not card again: %s" % [c["id"] for c in cards])
        self.assertEqual(self.task()["pr_state"], "closed")
