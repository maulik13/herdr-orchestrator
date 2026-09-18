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

# Answers with whatever state the case currently wants — per PR when the case
# set one, otherwise the shared default. Reaching the real `gh` would ask
# GitHub about a made-up PR url.
#
# It records the call twice: once on entry, once on the way out WITH the
# answer it actually served. The distinction is not pedantic. The entry line
# is how a case knows a call is in flight; the exit line is the only thing
# that proves a given tick read a given state. Waiting on entry lines to
# decide the loop has reacted to a flipped answer is a race, and was one.
GH_SHIM = """#!/bin/sh
printf '%s\\n' "$*" >> "$FAKE_GH_STARTS"
# A case that needs the round-trip held open creates FAKE_GH_GATE and
# releases it by touching the file, so the window is opened and closed by
# the test rather than measured against the clock. Bounded so a bug in the
# case cannot wedge the run; overshooting it is reported, never silent.
if [ -n "$FAKE_GH_GATE" ]; then
  waited=0
  while [ ! -f "$FAKE_GH_GATE" ] && [ "$waited" -lt 600 ]; do
    sleep 0.1
    waited=$((waited + 1))
  done
fi
# Builtins only past this point — no basename, no cat, no $( ). Every fork
# here is one the poll loop waits on, once per watched PR per tick, and a
# developer box under real load (other agents, a virus scanner) is where a
# suite that forks four times a tick stops keeping up with a 1s interval.
# `gh pr view <url> --json ...`, so the url is $3 and ${3##*/} is its number.
src="$FAKE_GH_STATE"
[ -f "$FAKE_GH_DIR/${3##*/}.json" ] && src="$FAKE_GH_DIR/${3##*/}.json"
read -r body < "$src"
printf '%s\\n' "$body"
printf '%s\\n' "$body" >> "$FAKE_GH_LOG"
exit 0
"""

PR_URL = "https://github.com/example/repo/pull/1"

# How long a wait may take before it is called a failure. Deliberately far
# above what the work needs: at a 1s poll interval every wait here wants two
# or three ticks, so a few seconds. The headroom is for the machine, not the
# code — these ran green for a long time and then failed three different ways
# on a box at load average 41, each one starved of ticks rather than wrong.
# A real hang still fails, just later.
TICK_DEADLINE = 150


class PollCase(WebappCase):
    """A board parked on an open PR, with `gh` answering whatever we say."""

    def setUp(self):
        super().setUp()
        self.gh_log = os.path.join(self.tmp.name, "gh.log")
        self.gh_starts = os.path.join(self.tmp.name, "gh-starts.log")
        self.gh_state = os.path.join(self.tmp.name, "gh-state.json")
        shim = os.path.join(self.tmp.name, "bin", "gh")
        with open(shim, "w") as fh:
            fh.write(GH_SHIM)
        os.chmod(shim, 0o755)
        self.gh_dir = os.path.join(self.tmp.name, "gh-per-pr")
        os.makedirs(self.gh_dir)
        self.env.update(FAKE_GH_LOG=self.gh_log, FAKE_GH_STARTS=self.gh_starts,
                        FAKE_GH_STATE=self.gh_state, FAKE_GH_DIR=self.gh_dir)
        self.says("OPEN")

    def says(self, state):
        """Set what `gh pr view` answers from the next call on.

        Written through a rename so a call in flight reads one whole file or
        the other, never a torn one. Remembers the state and where the served
        log had reached, which is what lets `after_flip` wait on ticks that
        actually saw this answer rather than on ticks that merely happened.
        """
        body = json.dumps({"state": state,
                           "mergedAt": "2026-01-01T00:00:00Z" if state == "MERGED" else None})
        tmp = self.gh_state + ".tmp"
        with open(tmp, "w") as fh:
            fh.write(body + "\n")
        os.replace(tmp, self.gh_state)
        self.said = state
        self.mark = len(self.served())

    def says_for(self, pr, state):
        """Set the answer for ONE pull request, overriding `says` for it."""
        body = json.dumps({"state": state,
                           "mergedAt": "2026-01-01T00:00:00Z" if state == "MERGED" else None})
        tmp = os.path.join(self.gh_dir, "%s.json.tmp" % pr)
        with open(tmp, "w") as fh:
            fh.write(body + "\n")
        os.replace(tmp, os.path.join(self.gh_dir, "%s.json" % pr))

    def park_on_an_open_pr(self, pr_state="open"):
        for phase in ("planning", "implementing", "needs-review", "reviewing",
                      "resolving", "pr-open"):
            self.orch("phase", "T-1", phase)
        self.orch("set", "T-1", "--pr-url", PR_URL, "--pr-state", pr_state)
        self.orch("ack", "T-1")
        open(self.shim_log, "w").close()

    def served(self):
        """The answers `gh` has FINISHED giving, oldest first."""
        if not os.path.exists(self.gh_log):
            return []
        with open(self.gh_log) as fh:
            return [json.loads(ln)["state"] for ln in fh.read().splitlines() if ln.strip()]

    def ticks(self):
        """Completed `gh` calls so far."""
        return len(self.served())

    def starts(self):
        """`gh` calls that have BEGUN, in flight ones included."""
        if not os.path.exists(self.gh_starts):
            return 0
        with open(self.gh_starts) as fh:
            return len([ln for ln in fh.read().splitlines() if ln.strip()])

    def wait_ticks(self, n, deadline=TICK_DEADLINE):
        """Block until the poll loop has completed at least `n` `gh` calls."""
        end = time.time() + deadline
        while time.time() < end:
            if self.ticks() >= n:
                return
            time.sleep(0.05)
        self.fail("only %d poll ticks in %ds, wanted %d. Zero ticks means the task "
                  "was dropped from the watch, not that carding was suppressed; a "
                  "few means the loop was starved (check machine load) or the poll "
                  "thread died — the server said:\n%s"
                  % (self.ticks(), deadline, n, self.server_output()))

    def wait_for(self, predicate, what, deadline=TICK_DEADLINE):
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
        self.fail("timed out after %ds waiting for %s (%d poll ticks meanwhile) — "
                  "the server said:\n%s"
                  % (deadline, what, self.ticks(), self.server_output()))

    def after_flip(self, n=2, deadline=TICK_DEADLINE):
        """Wait until the loop has acted on the answer `says` last set.

        Waits for `n` ticks that served that answer, not for `n` ticks. The
        poll loop is strictly sequential — read, act, sleep, read — so the
        SECOND tick to serve a state proves the first one's transaction has
        already been written. That makes this exact rather than a timing
        guess: the call in flight when `says` landed may still have been
        holding the old file, and waiting on tick counts alone let a case
        assert against a board the loop had not reached yet.
        """
        end = time.time() + deadline
        while time.time() < end:
            if self.served()[self.mark:].count(self.said) >= n:
                return
            time.sleep(0.05)
        self.fail("timed out after %ds waiting for %d ticks serving %s; served %s "
                  "since the flip. Few or no ticks means the loop was starved or "
                  "the poll thread died rather than that it ignored the state — "
                  "the server said:\n%s"
                  % (deadline, n, self.said, self.served()[self.mark:],
                     self.server_output()))

    def more_ticks(self, n=3):
        """Let `n` further ticks complete, for cases proving nothing happens."""
        self.wait_ticks(self.ticks() + n)

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
        self.more_ticks()                        # and several ticks beyond the close
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
        self.more_ticks(4)
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
        # Waits are on the OUTCOME, then `more_ticks` proves it settles there.
        # This case walks the board through four states, so asserting at a tick
        # boundary makes the whole thing rest on reasoning about which tick has
        # acted; waiting for the state and then proving it is stable does not.
        self.park_on_an_open_pr()
        self.says("CLOSED")
        self.serve(poll=1)
        self.wait_for(lambda: len(self.cards()) == 1, "the first card")
        self.orch("resolve", self.cards()[0]["id"], "--decision", "approved",
                  "--note", "reopen it")

        self.says("OPEN")
        self.wait_for(lambda: self.task()["pr_state"] == "open", "the reopen")
        self.assertEqual(len(self.cards()), 1, "reopening is not itself a question")

        self.says("CLOSED")
        self.wait_for(lambda: len(self.cards()) == 2, "the card for the second close")
        self.more_ticks()
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
        self.more_ticks()
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
        gate = os.path.join(self.tmp.name, "release-gh")
        self.park_on_an_open_pr()
        self.says("CLOSED")
        self.env["FAKE_GH_GATE"] = gate        # the call parks until released
        self.serve(poll=1)
        self.wait_for(lambda: self.starts() >= 1, "the gh call to begin")

        # The call is parked inside the shim, so the loop is holding a snapshot
        # that still says "open". Record the close while it is stuck there.
        # Held open by the gate rather than by a timed delay: `orch` is a fresh
        # interpreter, and on a loaded box interpreter startup is exactly what
        # overruns a fixed window. Missing it would make tick 1 card
        # legitimately and report a product bug that is not there.
        self.orch("set", "T-1", "--pr-state", "closed")
        self.assertEqual(self.ticks(), 0,
                         "the gh call must still be in flight; the premise of "
                         "this case is a write that lands during the round-trip")

        open(gate, "w").close()                # let it answer
        self.wait_for(lambda: self.ticks() >= 1, "the parked gh call to return")
        # A second call STARTING proves the first one's transaction is written:
        # the loop reads, acts, sleeps, reads.
        self.wait_for(lambda: self.starts() >= 2,
                      "its transaction to land")

        cards = self.cards()
        self.assertEqual(len(cards), 0,
                         "the close was already recorded; the stale snapshot "
                         "must not card again: %s" % [c["id"] for c in cards])
        self.assertEqual(self.task()["pr_state"], "closed")
        self.assertIn("CLOSED", self.served(),
                      "the parked call has to have actually answered — a gh "
                      "call that timed out would raise no card either, and "
                      "would pass this case for the wrong reason")


class TestMergeIsUnconditional(PollCase):
    """A merge always acts, whatever the board last recorded.

    Putting a task back on `pr-open` after a merge is an ordinary recovery
    move — the merge was reverted, or more work turned out to be needed — and
    `set_phase` permits it with no `--force`. The task then carries
    `pr_state="merged"` while its PR is live again.

    That is the one input where filtering the `gh` answer against the recorded
    state is wrong. A missed close is loud: the card never goes up but the
    board still shows an open PR. A missed merge is silent, and worse, it never
    self-heals — the task is dropped before the lock, so nothing rewrites
    `pr_state`, and the loop stays blind to that PR for the life of the task
    while everything else about the board looks healthy.
    """

    def test_a_task_put_back_on_pr_open_after_a_merge_still_sees_a_merge(self):
        self.park_on_an_open_pr()
        self.says("MERGED")
        self.serve(poll=1)
        self.wait_for(lambda: self.task()["phase"] == "merged", "the first merge")
        self.assertEqual(self.task()["pr_state"], "merged")

        # The recovery move: merge reverted, more work needed, second PR.
        self.orch("phase", "T-1", "pr-open")
        self.orch("set", "T-1", "--pr-url", PR_URL.replace("/1", "/2"))
        self.assertEqual(self.task()["pr_state"], "merged",
                         "nothing resets pr_state on the way back to pr-open")

        # No tick-counting here: on a working fix the very next tick moves the
        # task to `merged` and so out of the watch, and no further tick lands.
        self.wait_for(lambda: self.task()["phase"] == "merged",
                      "the second merge, on a task whose pr_state is already merged")


class TestTheLogTellsTheTruth(PollCase):
    """The board log is what a human reads to reconstruct what became of a PR.

    The OPEN branch is reached whenever `gh` says OPEN and `pr_state` is
    anything but "open" — which includes two states that are not reopens at
    all: `pr_state` starts null and `orch set --pr-url` alone leaves it that
    way, and a task put back on `pr-open` after a merge arrives carrying
    "merged". Both have to be recorded, neither is an event.
    """

    def log_lines(self):
        return [e["msg"] if isinstance(e, dict) else str(e)
                for e in self.state().get("log", [])]

    def test_a_pr_that_was_only_ever_open_logs_no_reopen(self):
        for phase in ("planning", "implementing", "needs-review", "reviewing",
                      "resolving", "pr-open"):
            self.orch("phase", "T-1", phase)
        self.orch("set", "T-1", "--pr-url", PR_URL)   # no --pr-state; stays null
        self.assertIsNone(self.task()["pr_state"])
        self.serve(poll=1)
        self.wait_ticks(2)
        self.wait_for(lambda: self.task()["pr_state"] == "open",
                      "the null pr_state to be recorded")
        self.assertEqual([ln for ln in self.log_lines() if "reopened" in ln], [],
                         "this PR never closed, so it never reopened")

    def test_a_real_reopen_is_still_logged(self):
        """The other half: suppressing the false line must not lose the true
        one, which is the only record that the close was undone."""
        self.park_on_an_open_pr()
        self.says("CLOSED")
        self.serve(poll=1)
        self.after_flip()
        self.assertEqual(len(self.cards()), 1)

        self.says("OPEN")
        self.after_flip()
        self.assertEqual(self.task()["pr_state"], "open")
        self.assertTrue([ln for ln in self.log_lines() if "PR reopened" in ln],
                        "a genuine close -> open move is worth a log line")


class TestPrStateIsReadCaseInsensitively(PollCase):
    """`pr_state` is written by hand as well as by this loop.

    `gh` prints states uppercase, so "CLOSED" is what someone typing
    `orch set --pr-state` from a `gh` output reasonably writes, and
    `update_task` neither validates nor normalises it. The suppress/re-arm
    escape hatch the field's comment documents should not turn on
    capitalisation — the card it fails to suppress is exactly the duplicate
    this task exists to remove.
    """

    def test_an_uppercase_closed_suppresses_the_card_like_a_lowercase_one(self):
        self.park_on_an_open_pr(pr_state="CLOSED")
        self.says("CLOSED")
        self.serve(poll=1)
        self.wait_ticks(3)
        self.assertEqual(self.cards(), [],
                         "already recorded as closed, whatever the spelling")


class TestTasksDoNotBleedIntoEachOther(PollCase):
    """Several PRs are watched in one tick, and the state is now per task.

    The watch used to carry `(id, url)` and this change made it
    `(id, url, pr_state)`, so each task's answer is now filtered against its
    OWN recorded state and the results are dispatched from a dict keyed by
    task id. Every other case here drives a single task, which is exactly the
    shape that cannot catch a mix-up — and the real board watches two.
    """

    def park_both(self):
        self.orch("add", "T-2", "--project", "tw", "--title", "second")
        for key, pr in (("T-1", "1"), ("T-2", "2")):
            for phase in ("planning", "implementing", "needs-review", "reviewing",
                          "resolving", "pr-open"):
                self.orch("phase", key, phase)
            self.orch("set", key, "--pr-url", PR_URL.replace("/1", "/" + pr),
                      "--pr-state", "open")
            self.orch("ack", key)
        open(self.shim_log, "w").close()

    def task_for(self, key):
        import store
        return store.find(self.state(), key)

    def test_one_closing_while_the_other_merges(self):
        """Each task gets its own outcome, and neither answer is applied to
        the other."""
        self.park_both()
        self.says_for("1", "CLOSED")
        self.says_for("2", "MERGED")
        self.serve(poll=1)
        self.wait_for(lambda: self.task_for("T-2")["phase"] == "merged",
                      "T-2 to be seen merging")
        self.wait_for(lambda: self.task_for("T-1")["pr_state"] == "closed",
                      "T-1 to be seen closing")

        cards = [a for a in self.state()["approvals"]
                 if a["title"] == "PR closed without merging"]
        self.assertEqual(len(cards), 1, "only the closed PR is a question")
        self.assertEqual(cards[0]["key"], "T-1")
        self.assertEqual(self.task_for("T-1")["phase"], "pr-open")
        self.assertEqual(self.task_for("T-2")["pr_state"], "merged")

    def test_a_task_already_carded_does_not_suppress_a_sibling(self):
        """The filter is per task: T-1 sitting at `closed` with nothing to say
        must not stop T-2's close from being noticed on the same tick."""
        self.park_both()
        self.orch("set", "T-1", "--pr-state", "closed")
        self.says_for("1", "CLOSED")          # already recorded; nothing to do
        self.says_for("2", "CLOSED")          # a fresh close
        self.serve(poll=1)
        self.wait_for(lambda: self.task_for("T-2")["pr_state"] == "closed",
                      "T-2 to be seen closing")
        self.more_ticks()

        cards = [a for a in self.state()["approvals"]
                 if a["title"] == "PR closed without merging"]
        self.assertEqual([c["key"] for c in cards], ["T-2"],
                         "exactly one card, on the task that actually changed")
