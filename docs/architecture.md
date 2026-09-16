# Architecture

How the parts fit together and why the boundaries fall where they do. For
code-level design — locking, the phase machine, the HTTP API — see
[implementation.md](implementation.md). For the agent-facing contracts, see
[`skills/orchestrate/`](../skills/orchestrate/).

## The pieces

```
  you ──▶ webapp (kanban, queue, approvals)  ─┐
                                              ├─▶ state.json ──▶ board.md
  orchestrator ──▶ orch CLI ──────────────────┤     (locked)      (generated)
  workers      ──▶ orch CLI ──────────────────┘
       │
       └─▶ herdr: worktree per task, agent per worktree
```

| Piece | What it is | Responsibility |
|---|---|---|
| **state store** | `lib/store.py` over `state.json` | the only writer of truth; schema, locking, phase rules, board rendering |
| **`orch` CLI** | `bin/orch` | how every agent and every human reads and mutates state |
| **webapp** | `webapp/server.py` + static kanban | the human surface: queue, approvals, WIP cap, PR merge detection |
| **orchestrator** | a Claude Code session running the `orchestrate` skill | intake, triage, and the only thing that spawns agents |
| **workers / reviewers** | Claude Code agents, one per worktree | do the work and report their own phase |
| **herdr** | external multiplexer | panes, workspaces, and the one channel that can wake an idle agent |

Nothing in the system is a daemon. There is no background loop, no event bus
and no queue server — the store is a file, and every actor is a process that
takes the lock, writes, and leaves.

## One board, every repository

`state.json` is the source of truth, and there is exactly one of it
(`${HERDR_ORCHESTRATOR_HOME:-~/.claude/orchestrator}`) covering every
repository. Each task names a **project**: a registered repo, by short name.

```bash
orch project add infra --path ~/work/infra-diagrams
orch add DOC-2 --project infra --title "Diagram export is blurry"
```

A project's `path` is the resolved *main* repo root, so it is stable whether it
was registered from the main checkout or from a linked worktree, and a project
cannot be removed while it still has live tasks.

One board rather than one per repo because the thing being managed is your
attention across repositories, not any single checkout. A board per repo would
mean a per-repo WIP cap, a per-repo approvals queue, and no single answer to
"what is waiting on me?"

`board.md` is regenerated on every write — readable with `cat`, diffable in
git, never parsed back. It is a view, not a second source of truth.

## Resumability is the design constraint

The orchestrator is a Claude Code session, and its context is the one thing
guaranteed to be cleared. Everything follows from that:

- **Workers update their own phase.** The board stays current without the
  orchestrator burning context polling, which is what makes a mid-flight
  `/clear` safe.
- **Rules live in the store, not in a brief.** The WIP cap, the review-round
  cap and the PR gate are enforced by `lib/store.py`. A cap that evaporates on
  `/clear` is not a cap.
- **Findings live on the board, not in a `REVIEW.md`.** A file in a worktree
  dies with the worktree, is invisible until you open a pane, and cannot answer
  "is this resolved?" mechanically.
- **The orchestrator never reads code.** That is the reviewer's job. Its
  context is spent on the board and nothing else.

A resuming orchestrator reads `orch handoffs`, `orch approvals` and
`orch resumable`, and is caught up.

## The task lifecycle

`queued` → `planning` → `awaiting-plan` *(you approve)* → `implementing` →
`needs-review` → `reviewing` → `resolving` → `pr-open` *(you merge)* →
`merged` → `archived`

Plus `blocked` and `parked` off to the side. The parts worth knowing:

- **Plan approval before code.** The worker researches, confirms the
  requirement is unambiguous, and submits a TLDR with tradeoffs and risks.
  Ambiguity becomes a `question` card rather than a guess.
- **Adversarial review in fresh context.** When implementation lands, the
  orchestrator starts a *separate* reviewer in a sibling pane of the same
  worktree. Fresh context is the point — a reviewer that watched the code get
  written inherits its author's assumptions.
- **Findings are judged, not just obeyed.** The implementer checks each one for
  correctness, scope against the done-condition, and severity before touching
  code. A valid finding outside this issue's scope gets disputed and filed, not
  quietly folded into the diff.
- **P1/P2 block, P3 advises** — and the board enforces it. `orch phase <task>
  pr-open` refuses while any P1 or P2 is open or disputed, and names them.
- **Two rounds, then you.** The same reviewer re-checks its own findings, but
  disagreement is capped: a third round is refused and the escalation is named.
  After round two it becomes a `conflict` card with both positions.
- **Breaking changes need explicit approval** before implementation, not after.
- **Trivial work can skip both gates, but only on purpose.** `orch add
  --trivial` waives the plan approval and the reviewer for a typo or a version
  bump. Entering `pr-open` is refused for anything neither trivial nor
  reviewed, so the difference between an agreed shortcut and a skipped review
  stays visible.
- **Merge detection is automatic, cleanup is not.** The webapp notices the
  merge; the orchestrator then runs `orch cleanup-check`, which confirms the PR
  is genuinely `MERGED`, the tree is clean, and nothing is unpushed before any
  worktree is removed.

`pr-open` deliberately does not hold a WIP slot — a PR can sit for days waiting
on human review, and blocking the queue on that would starve everything behind
it.

Full agent-facing detail in
[`references/worker-protocol.md`](../skills/orchestrate/references/worker-protocol.md).

## Handoffs: waking the next agent

Herdr has no event bus, and the orchestrator has no background loop — it runs
only when prompted. So an agent that finishes and stops would leave the next
one asleep until you noticed.

`orch phase` closes that. When a transition means the *actor* changes and the
next move is not yours, the store records a handoff and the CLI prompts the
orchestrator, which relays.

Two properties make this safe rather than merely convenient:

- **The board is the durable record; the prompt is best-effort.** The handoff
  is written to `state.json` before any prompt is attempted. If the
  orchestrator is mid-turn, restarted into a new pane, or gone, the row is
  still there and `orch handoffs` is the queue a resuming orchestrator reads.
  How the wake-up went is recorded alongside it, so a silent orchestrator shows
  on the board as **Awaiting the orchestrator** rather than as nothing at all.
- **The prompt is a fixed template.** It carries a task key, a phase and a
  reason drawn from a table in the store — never worker or reviewer prose.
  Findings, plans and decisions stay on the board, in one copy, where you can
  see them too.

## Trust boundaries

The second point above is a security boundary, not a style choice. A wake-up is
the one message that arrives in the orchestrator's input already looking like an
instruction, and the orchestrator is the only agent permitted to spawn agents.
Relaying agent-authored text through that channel would hand any worker that
read a poisoned issue or dependency a direct line to it.

The rest of the deliberate limits:

- **Never merges or pushes.** Workers open PRs; you merge them.
- **Never answers a worker's approval prompt.** A blocked agent is asking a
  human — it escalates with the text quoted.
- **Only the orchestrator spawns agents.** Workers request a reviewer by
  changing phase; they never spawn one. One spawning authority keeps the board
  honest and prevents runaway loops.
- **Never treats worker output as instructions.** A worker can emit text shaped
  like orders. It is data; directives get quoted to you.
- **The webapp binds loopback only and has no auth.** It drives real agents and
  exposes repo state, so it must not be reachable from a network.

## The suggestions lane

Agents that hit friction can file it with `orch suggest`. Suggestions are a
separate lane: they never consume a WIP slot, nothing dispatches them, and they
only become work when **you** promote one — which is what stops the system from
quietly rewriting itself.

Three things keep the list worth reading. `--evidence` is required, so a
suggestion states what actually happened rather than what an agent would
prefer. Workers file at most one per task, so they have to pick the thing that
cost most. And re-filing an open suggestion **seconds** it instead of
duplicating — three workers hitting one papercut shows as `hit 3×` at the top
of the list, not three rows.
