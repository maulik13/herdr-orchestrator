"""Wake the orchestrator when a task needs it.

Herdr has no event bus. The only channel that reaches an idle agent is
`herdr agent prompt`, which some already-running process has to call — and the
orchestrator is a Claude Code session with no background loop, so it cannot
call it for itself. This module is what the agent making a handoff calls
instead, through `orch`, so the wake-up happens at the moment of the handoff
rather than the next time a human thinks to poke the orchestrator.

Two rules hold this together.

**The board is the durable record; the prompt is best-effort.** A prompt lives
only in the receiving agent's context and is lost on a `/clear`, and the
orchestrator may be mid-turn, restarted into a new pane, or gone. So nothing
here raises and nothing here blocks a worker: the handoff is already written to
state.json before this runs, `orch handoffs` is the queue a resuming
orchestrator reads, and the outcome recorded below is only there to make a
silent orchestrator visible on the board.

**The message is a fixed template.** It names the task, the phase and a reason
drawn from `store.HANDOFF_TRANSITIONS` — never worker or reviewer prose. Agent
output is data, and a wake-up is the one message that arrives in the
orchestrator's input already looking like an instruction; relaying content
through it would hand any agent that read a poisoned issue a direct line to the
one agent allowed to spawn agents. Findings stay on the board, in one copy,
where the human can see them too.
"""
import os
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import store  # noqa: E402

# Long enough for the socket round-trip, short enough that a wedged server
# cannot hold up the agent that just handed its work off.
TIMEOUT = 15


def message(handoff):
    """The wake-up text. Template only — see the module docstring."""
    return ("[handoff] %s is now %s: %s. Run `orch handoffs` for the queue, then "
            "`orch ack %s` once you have acted."
            % (handoff["key"], handoff["phase"], handoff["reason"], handoff["key"]))


def send(target, text):
    """Prompt an agent. Returns a short outcome string; never raises.

    No `--wait`: the caller is an agent finishing its own turn and has no use
    for the orchestrator's lifecycle state. Waiting here would also mean
    blocking on however long the orchestrator's current turn takes.
    """
    if not shutil.which("herdr"):
        return "failed: herdr not on PATH"
    try:
        r = subprocess.run(["herdr", "agent", "prompt", target, text],
                           capture_output=True, text=True, timeout=TIMEOUT)
    except subprocess.TimeoutExpired:
        return "failed: herdr agent prompt timed out"
    except Exception as e:                                  # noqa: BLE001
        return "failed: %s" % e
    if r.returncode != 0:
        # Most often the recorded target is stale: herdr clears an agent name
        # when its pane occupant exits, so the orchestrator was restarted and
        # has not re-registered.
        detail = (r.stderr or r.stdout).strip().splitlines()
        return "failed: %s" % (detail[0][:160] if detail else "exit %d" % r.returncode)
    return "prompted %s" % target


def is_self(state, as_agent=False):
    """Is the caller the orchestrator itself?

    It raises its own handoffs — escalating a conflict moves a task to
    `awaiting-decision` — and prompting yourself is a wasted turn that reads
    like someone else asked for something. The ledger row is still recorded;
    only the prompt is skipped.

    Only an agent taking a turn can be the orchestrator, so this answers False
    unless the caller says otherwise — see `wake` for why that is the default
    rather than the opt-out.
    """
    if not as_agent:
        return False
    pane = os.environ.get("HERDR_PANE_ID")
    rec = state.get("orchestrator") or {}
    return bool(pane and rec.get("pane") == pane)


def pending(state, tid):
    """A snapshot of a task's pending handoff, safe to use after the lock.

    One helper rather than the same three lines at every call site: the wake-up
    has to happen outside the transaction that recorded it, so every caller has
    to carry a copy across the lock boundary, and a caller that forgets sends
    nothing and reports nothing.
    """
    rows = store.pending_handoffs(state, tid)
    return dict(rows[0]) if rows else None


def wake(pdir, handoff, as_agent=False):
    """Send the wake-up for one recorded handoff and note how it went.

    The outcome is written back in its own short transaction, after the prompt,
    so the board lock is never held across the herdr round-trip — the
    orchestrator and every other worker would queue behind it.

    `as_agent=True` means the caller is an agent taking a turn, and so could BE
    the orchestrator: only then is the self-raise check worth making. `orch`
    passes it; nothing else should.

    It defaults to off, which is the whole guard. The original bug was the
    webapp inheriting the orchestrator's `HERDR_PANE_ID` from the pane it was
    launched in, so every wake-up it sent was dropped as self-directed while
    the board showed a healthy row. A caller that omits this argument is, by
    construction, one that never thought about panes — and defaulting it on
    would suppress that caller's wake-ups in exactly the same silence, because
    a forgotten keyword and a deliberate self-raise arrive here identical. The
    two failure directions are not equal: defaulting off can cost the
    orchestrator a wasted turn prompting itself, defaulting on costs a stalled
    pipeline nobody is told about.

    The residual risk is a non-agent caller that passes `as_agent=True` by
    copying `orch`'s call. That is a deliberate claim to be an agent rather
    than an omission, and nothing here can see through it.
    """
    if not handoff:
        return None
    state = store.read(pdir)
    target = store.orchestrator_target(state)
    if is_self(state, as_agent):
        outcome = "queued (raised by the orchestrator itself)"
    elif not target:
        outcome = ("failed: no orchestrator registered "
                   "(`orch whoami --agent <name>` in its pane)")
    else:
        outcome = send(target, message(handoff))
    try:
        with store.transaction(pdir) as st:
            store.record_notify(st, handoff["id"], outcome)
    except Exception:                                       # noqa: BLE001
        pass          # the handoff itself is already recorded; this is a note
    return outcome
