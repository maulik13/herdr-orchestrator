# Worker protocol

The lifecycle every implementation worker follows, and the briefs that carry it.

Two properties make this work. Workers update the board **themselves** via the
`orch` CLI, so phase changes are recorded at the moment they happen rather than
whenever the orchestrator next looks — the store's file lock makes concurrent
writes from workers, the orchestrator and the webapp safe. And **only the
orchestrator starts agents**. A worker that needs a reviewer asks for one by
moving to `needs-review`; it never spawns anything itself. That keeps one
spawning authority, keeps the board honest, and stops a confused worker from
spawning reviewers in a loop.

## Phases

| Phase | Who acts | Exit condition |
|---|---|---|
| `queued` | — | a slot frees and the orchestrator starts it |
| `planning` | worker | plan drafted, or a blocking question raised |
| `awaiting-plan` | **you** | plan approved or rejected |
| `implementing` | worker | code complete, tests run |
| `needs-review` | orchestrator | reviewer agent started |
| `reviewing` | reviewer | findings written |
| `resolving` | worker | P1/P2 findings addressed |
| `awaiting-decision` | **you** | breaking change or review conflict settled |
| `pr-open` | **you** | PR merged |
| `merged` | orchestrator | cleanup guards pass |
| `archived` | — | terminal |

Every phase change is one command, run by whoever owns the phase:

```bash
orch phase GH-412 implementing
herdr pane report-metadata "$HERDR_PANE_ID" --source orchestrator \
  --token phase=implementing --title "GH-412 · implementing"
```

The `orch` call is the board truth; the `report-metadata` call is what makes the
phase visible in the Herdr sidebar, so the human gets a live overview without
opening anything. Do both — they serve different readers.

## Implementation worker brief

Send this after `agent start`, with the issue fields filled in.

```
Issue <KEY>: <title>
<normalized issue body>

Done when: <done-condition>
Source: <url or "described by the user">

You are in an isolated git worktree on branch <branch>. Keep changes scoped to
this issue. `orch` is on your PATH and is how you report progress — the human
watches a board fed by it, so a phase you don't report is a phase nobody sees.

WORK IN THIS ORDER:

1. Orient. Read README and any docs that explain what this project is. Skim the
   areas of the codebase this issue touches. Do not start designing yet.

2. Check clarity. Decide whether the requirement and the done-condition are
   unambiguous. If anything material is unclear — behaviour, scope, which of two
   reasonable readings applies — stop and raise it:

     orch approve-request <KEY> --kind question --title "<the question in one line>" \
       --body "<what you need decided and why it changes the work>"
     orch phase <KEY> awaiting-plan

   Then stop and end your turn — the orchestrator will prompt you with the
   answer. Guessing on an ambiguous requirement wastes the whole cycle.

3. Plan. Once clear, work out how you will implement it.

4. Submit the plan for approval — a TLDR, not the whole plan. The human is
   reading several of these, so lead with what they'd want to argue with:

     orch approve-request <KEY> --kind plan --title "<one-line approach>" \
       --plan-path PLAN.md --body "<the same one-liner, for the card>"

   Write PLAN.md in your worktree root: the approach in 3-5 bullets, key
   tradeoffs, risks, and what you are deliberately not doing. Keep it under
   ~20 lines.

   `--plan-path` matters. The human opens that file in Plannotator and
   annotates it inline, so write it as a document to be marked up — short
   sections with headings, one idea per bullet — not as a wall of prose. Their
   annotations come back to you as the rejection note, quoting the parts they
   disagreed with.

     orch phase <KEY> awaiting-plan

   Then stop and end your turn. Do not poll and do not busy-wait. The
   orchestrator watches for the human's decision and will prompt you with it,
   including the reasoning if the plan was rejected.

5. Implement, once approved. If you discover the plan was wrong, say so and
   raise a new question rather than quietly changing course.

6. Hand off for review:

     orch phase <KEY> needs-review

   The orchestrator will start an independent reviewer against your worktree.
   Do not start one yourself.

7. Address findings. They are on the board, not in a file:

     orch finding list <KEY> --open

   Fix every P1 and P2, recording what you did on each one — that note is what
   the reviewer re-reads, so it has to say what changed, not that you agree:

     orch finding resolve <id> --note "redacted the env before logging"

   Disagree with one? Do not silently skip it and do not just resolve it.
   Dispute it with your reasoning; it keeps blocking until the reviewer accepts
   or reopens it:

     orch finding dispute <id> --note "the caller already redacts this"

     orch phase <KEY> resolving      # while fixing
     orch phase <KEY> needs-review   # when ready for re-check

8. Open the PR. You do not have to judge whether review is finished — the
   board does: `orch phase <KEY> pr-open` refuses while any P1 or P2 is open or
   disputed, and names them. Format below. Then:

     orch set <KEY> --pr-url <url> --pr-state open
     orch phase <KEY> pr-open

NEVER: push to the default branch, merge, or force-push. If a change breaks
existing behaviour — API shape, output format, config, database schema — stop
and raise it as --kind breaking-change before implementing it. The human decides
whether a break is acceptable; you do not.
```

## How agents hand work to each other

Herdr has no message bus. `events.*` carries lifecycle only, pane metadata
tokens are capped at 16 short keys, and the sole content channel between agents
is `herdr agent prompt` — text into the other agent's input. So the question is
not "which Herdr primitive carries a review?" but "where does a review live?"

**Findings are board state, not files and not prompts.** A `REVIEW.md` dies with
the worktree that held it, is invisible until someone opens a pane, and cannot
answer "are P1 and P2 resolved?" except by an agent re-reading its own prose. A
prompt is worse: it lives only in the receiving agent's context, so a `/clear`
loses it.

The split that works:

| | carries |
|---|---|
| `orch finding` | the findings themselves — durable, queryable, visible on the board |
| `herdr agent prompt` | the nudge: "findings are up, go read them" |
| `herdr pane report-metadata` | live phase for the Herdr sidebar |

Never paste findings into a prompt. Point at the board and let the other agent
read them, so one copy exists and the human can see it too.

## Reviewer brief

Started by the orchestrator in a sibling pane of the *same* worktree, as a fresh
agent with no memory of the implementation. That independence is the point —
a reviewer that watched the code get written inherits its author's assumptions.

Keep the reviewer alive between rounds. "The same reviewer confirms the fixes"
only works if it still remembers what it asked for.

```
You are reviewing an implementation on branch <branch>, in this worktree.
It is not yours. Review it adversarially — assume it is subtly wrong and try
to prove it.

Issue <KEY>: <title>
Done when: <done-condition>

Read the diff against <base>:  git diff <base>...HEAD

Look hardest at:
- Security: injection, authz gaps, secrets in code or logs, unsafe deserialization,
  path traversal, dependency risk.
- Correctness: off-by-one, null/empty cases, error paths, concurrency, anything
  that fails the done-condition.
- Regressions: existing behaviour or callers this breaks. Search for callers
  rather than assuming there are none.
- Tests: does a test actually cover the changed behaviour, and would it fail if
  the change were reverted?

Record every finding on the board — do NOT write REVIEW.md or any other file:

  orch finding add <KEY> --severity P1 --title "<one line>" \
    --where "<file>:<line>" --detail "<why it matters>" --source "$AGENT"

Grade each one:
  P1  security vulnerability, data loss, breaks existing behaviour, or fails the
      done-condition
  P2  real bug in an edge case, missing coverage for changed behaviour, likely
      regression
  P3  style, naming, cleanup — advisory, never blocking

Only P1 and P2 block a PR, and the board enforces that: `orch phase <KEY>
pr-open` refuses while any is unresolved. So do not pad the list — a P3 dressed
up as a P2 costs a round-trip and trains everyone to ignore you.

Found nothing blocking? File no findings and say so. Then:
  orch phase <KEY> resolving

RE-REVIEW (later rounds): read what the implementer did with each one —
`orch finding list <KEY>` shows every response and anything disputed. Re-check
only P1 and P2. Resolve what is genuinely fixed and reopen what is not:

  orch finding accept <id> --note "fix confirmed"
  orch finding reopen <id> --note "<what is still wrong>"
```

## Bounded rounds

Two review rounds, then stop. Implementer and reviewer can disagree
indefinitely, and each round costs real tokens with diminishing returns.

The board makes the disagreement legible rather than buried in prose: anything
still `disputed` after round two is the conflict, already carrying both sides —
the reviewer's `--detail` and the implementer's `--note`. So the escalation
writes itself:

```bash
orch finding list GH-412 --open --blocking --json > /tmp/conflict.json
orch approve-request GH-412 --kind conflict \
  --title "Reviewer and implementer disagree on the retry semantics" \
  --body-file /tmp/conflict.md
orch phase GH-412 awaiting-decision
```

When the human rules, record it on the finding itself so the history stays in
one place — `accept` if the implementer was right, `reopen` if the reviewer was.

## PR format

The TLDR exists so a human reviewer knows where to spend attention. Detail goes
underneath for whoever needs it.

```markdown
## TLDR
- <what changed, in one line>
- <the one decision worth arguing with>
- <what to look at first>

## Why
<problem, and the issue link>

## What changed
<walkthrough of the change, file by file where it helps>

## Risks and tradeoffs
<what might bite, what was deliberately not done>

## Testing
<commands run, results, what is not covered>
```

## Cleanup after merge

Never automatic on a timer. Check the guards first:

```bash
orch cleanup-check GH-412
```

That verifies the PR is genuinely `MERGED` via `gh`, the worktree is clean, and
nothing is unpushed. It exits non-zero and lists problems otherwise. Only on a
clean pass:

```bash
herdr worktree remove --workspace <ws-id>
git -C "$REPO_ROOT" branch -d <branch>
orch phase GH-412 archived
```

`branch -d` rather than `-D` on purpose: it refuses to delete anything not fully
merged, which is a second, independent check on the same question. If it
complains, something is wrong with the merge assumption — stop and ask.

## Filing an improvement

You will notice things while working: a command that should exist, a document
that misled you, a step every worker repeats by hand. Record those — but the
bar is deliberately high, because a queue full of speculative polish is worse
than no suggestions at all. The next agent pays for your noise.

File only when all three hold:

1. **It cost you something real, in this task.** You lost time, took a wrong
   turn, or produced a wrong result. Not "this could be cleaner" — something
   happened.
2. **It would happen again.** The next agent hits the same wall.
3. **It is one bounded change.** Nameable in a sentence, fixable in a sitting.

```bash
orch suggest --source "$WORKER" \
  --title "orch show should expose the repo path" \
  --evidence "spent 3 calls deriving the repo root before I could create the worktree" \
  --project infra        # omit --project when it is about the orchestrator tooling
```

`--evidence` is required and is the whole point: it says what happened, not
what you would prefer. A suggestion without it is a preference.

**At most one per task.** If you have three, file the one that cost the most.
The discipline is the feature — being forced to choose is what keeps the list
readable.

**Seconding is free and encouraged.** Filing something already open does not
create a duplicate; it increments the hit count and appends your evidence. Three
workers hitting one papercut should read as one urgent item, not three rows. So
never reword a suggestion to avoid a "duplicate" — matching is what gives the
human a priority signal.

Do not file: style and naming preferences, speculation about code you did not
touch, anything you can simply fix inside your current task, or complaints about
the issue itself (that is a `question` approval, which stops your work and asks
the human — a suggestion does not).

A suggestion never becomes work on its own. It sits in its own lane until a
human promotes it, so filing one costs nobody a WIP slot and changes nothing
about your task. Finish what you were doing.
