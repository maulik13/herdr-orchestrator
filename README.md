# herdr-orchestrator

> [!IMPORTANT]
> Disclaimer: This is a fully AI created tool with testing done by using the tool. Code reviews are done only by AI agents at this point.

Use agentic workflow using Claude Code and herdr [Herdr](https://github.com/herdr-dev/herdr) to distribute work across different projects and agents.

You hand it issues — Jira/GitHub/Notion links, or just a description. It triages them into a queue, gives each one its own git worktree and agent, runs them through plan → implement → adversarial review → PR, and parks anything needing your decision as a card. You can `/clear` the orchestrator at any point and it resumes from disk.

## How the pieces fit

```
  you ──▶ webapp (kanban, queue, approvals)  ─┐
                                              ├─▶ state.json ──▶ board.md
  orchestrator ──▶ orch CLI ──────────────────┤     (locked)      (generated)
  workers      ──▶ orch CLI ──────────────────┘
       │
       └─▶ herdr: worktree per task, agent per worktree
```

`state.json` is the source of truth — **one board covering every repository**. Each task names a *project*: a registered repo, by short name. Everything writes through `lib/store.py`, which holds a file lock, so the orchestrator, the workers and the webapp can all be live at once. `board.md` is regenerated on every write — readable with `cat`, diffable in git, never parsed back.

Register a repo once and work can be created against it from anywhere:

```bash
orch project add infra --path ~/work/infra-diagrams
orch add DOC-2 --project infra --title "Diagram export is blurry"
```

Workers update their own phase as they go. That means the board is current without the orchestrator burning context polling, and it's what makes a mid-flight `/clear` safe.

## Requirements

| | |
|---|---|
| **Herdr** | required — the skill refuses to run outside a Herdr-managed pane |
| **git** | required — worktree isolation depends on it |
| **python3** | required — 3.9+, stdlib only, no packages to install |
| `jq` | **required** — the spawn path parses Herdr's JSON responses with it |
| `gh` | needed for GitHub intake, PR creation, and merge verification |
| `glab` | optional, GitLab intake |

Jira and Notion work through a connected MCP tool, falling back to pasted text.

## Install

```bash
git clone https://github.com/maulik13/herdr-orchestrator.git
cd herdr-orchestrator && ./install.sh
```

That symlinks the skill into `${CLAUDE_CONFIG_DIR:-~/.claude}/skills/` and `orch` into `~/.local/bin` (override with `HERDR_ORCH_BIN`). Because both are symlinks, `git pull` updates everything with no reinstall.

`./install.sh --check` reports readiness without changing anything. `--remove` unlinks.

As a plugin instead:

```bash
/plugin marketplace add maulik13/herdr-orchestrator
/plugin install herdr-orchestrator@herdr-tools
```

## Use

Launch Claude Code **from inside a Herdr pane** — the skill checks `HERDR_ENV=1` and stops otherwise, because without the injected pane context it can't tell its own pane from yours.

```
> orchestrate these: PROJ-412, PROJ-418, and the Safari login hang
```

It normalizes each item, queues them, shows a plan, and waits for one confirmation. After that it runs autonomously — everything further that needs you arrives as a card, not a chat interrupt.

Start the board:

```bash
python3 webapp/server.py
```

**Projects** in the header opens a drawer. It edits a registration in place — rename it, or repoint it after a repo moves on disk. A rename cascades to every task, approval and suggestion that references the project, in one transaction, and the UI tells you how many will be rewritten before you confirm.

Hovering a card reveals a × to delete it. It only appears where deletion is safe — a task that owns a herdr workspace, a worktree or an open PR has no × , because dropping the record would leave those running with nothing tracking them. Clean those up first, or `orch rm --force` if you really mean it.

Terminal-styled: monospace throughout, near-square corners, lowercase lane headings with `[n]` counts, and shell punctuation for prompts and paths. Colours are [Rosé Pine](https://rosepinetheme.com), taken from the canonical palette repo — Dawn in light mode, Moon in dark, following the system preference. Three surface levels carry the depth, and the ordering flips between variants: in Moon a raised panel is lighter than the page, in Dawn the lane is a faintly darker tray holding cards that are the whitest thing on screen. Section titles are iris, except in progress in rose. The lane colour rides on the cards themselves — rose fills in progress, foam fills review, queued and terminal sections stay neutral — so cards are the only bordered, filled objects on the page and hold the attention without a frame around every section. That column carries two swimlanes — implementation on top, planning below — since they are the same commitment but want different attention. Every colour is a CSS variable in the two `:root` blocks of `webapp/static/style.css`.

One board, every project. The header dropdown filters by project (kept in the URL, so a reload lands on the same view), cards are tagged with the repo they belong to, and the Projects drawer registers or removes repos without touching the CLI.

Then open http://127.0.0.1:8787. Five collapsible sections stacked top to bottom — in progress, review, queued, parked, done — so vertical position carries priority and the queue stops competing with active work for horizontal space. Each section header doubles as its summary ("2 implementing · 1 awaiting plan … 1 needs you"), which is what makes collapsing safe: a shut section still reports what it holds. Parked and done start shut; the state is remembered per browser. PR work lives inside review, sorted last and edged in pine, because it is review work that is yours rather than an agent's. Drag to reorder the queue, approve or reject plans inline, adjust the WIP cap. It binds loopback only and has no auth — it drives real agents and exposes repo state, so don't expose it to a network.

While it runs it also polls `gh` every 60s (`--poll-seconds`, `0` disables) for any task in `pr-open`. A merged PR moves the task to `merged`, which is the orchestrator's cue to run its cleanup guards. A PR *closed* without merging raises a question card instead and changes nothing, since that needs your decision. Detection only — the webapp never removes a worktree or deletes a branch; that stays with the orchestrator behind `orch cleanup-check`.

## Plan review with Plannotator

A worker writes its plan to `PLAN.md` in its worktree and attaches the path to
the approval. The card then offers **Review in Plannotator**, which opens the
plan in [Plannotator](https://github.com/backnotprop/plannotator) for inline
annotation instead of a bare approve/reject.

The verdict settles the approval on its own:

| Plannotator | Board |
|---|---|
| Approve | approval approved; the worker is told to proceed |
| Send feedback | approval **rejected**, annotations kept verbatim as the note |
| Close | left pending — closing is not a decision |

The annotations are what the worker reads to know what to change, so they pass
through unedited. The inline Approve/Reject buttons stay as a fallback, and are
the only option when Plannotator is not installed or an approval has no
document (questions, conflicts, breaking changes).

## The lifecycle

Each task moves through phases, mostly driven by the worker itself:

`queued` → `planning` → `awaiting-plan` *(you approve)* → `implementing` → `needs-review` → `reviewing` → `resolving` → `pr-open` *(you merge)* → `merged` → `archived`

The parts worth knowing:

- **Plan approval before code.** The worker researches, checks the requirement is unambiguous, and submits a TLDR with tradeoffs and risks. Ambiguity becomes a `question` card rather than a guess.
- **Adversarial review in fresh context.** When implementation lands, the orchestrator starts a *separate* reviewer in a sibling pane of the same worktree. Fresh context is the point — a reviewer that watched the code get written inherits its author's assumptions.
- **P1/P2 block, P3 advises** — and the board enforces it. Reviewers file findings with `orch finding add`; `orch phase <task> pr-open` refuses while any P1 or P2 is open or disputed, and names them. Findings live on the board rather than in a `REVIEW.md`, because a file in a worktree dies with the worktree, is invisible until you open a pane, and cannot answer "is this resolved?" mechanically.
- **Two rounds, then you.** The same reviewer re-checks its own findings, but disagreement is capped — after round two it becomes a `conflict` card with both positions.
- **Breaking changes need explicit approval** before implementation, not after.
- **Trivial work can skip both gates, but only on purpose.** `orch add --trivial` waives the plan approval and the reviewer for a typo or a version bump. Entering `pr-open` is refused for anything neither trivial nor reviewed, so the difference between an agreed shortcut and a skipped review stays visible.
- **Merge detection is automatic, cleanup is not.** The webapp notices the merge; the orchestrator then runs `orch cleanup-check`, which confirms the PR is genuinely `MERGED`, the tree is clean, and nothing is unpushed before any worktree is removed. `git branch -d` (not `-D`) is a second independent check.

Full detail in [`references/worker-protocol.md`](skills/orchestrate/references/worker-protocol.md).

## orch CLI

The orchestrator and workers both drive state through this; you can too.

```bash
orch project add infra --path ~/work/infra   # register a repo
orch project list
orch project edit infra --name infra-diagrams --path ~/work/repos/infra-diagrams
orch list                     # every project; --project X to narrow
orch add GH-412 --project infra --title "..." --source github
orch show infra/GH-412        # qualify when a key exists in two repos
orch phase GH-412 implementing
orch set GH-412 --done-when "..."  # correct intake in place, keeping id and queue slot
orch approvals                # what's waiting on you
orch resolve a1234 --decision approved
orch finding add GH-412 --severity P1 --title "..." --where f.py:42
orch finding list GH-412 --open              # what still blocks the PR
orch finding resolve f123 --note "what changed"
orch suggest --title "..." --evidence "..."   # file an improvement
orch suggestions              # open ones, most-hit first
orch promote s123 --key TOOL-4 --project infra
orch rm infra/DOC-2           # delete a task (refused if it owns a worktree or PR)
orch board                    # print the generated board.md
orch cleanup-check GH-412     # safe to archive?
```

`orch phase` refuses to exceed `max_active` (default 3), so the WIP cap holds even if something tries to bypass the queue.

## Self-improvement

Agents that hit friction can file it:

```bash
orch suggest --title "orch show should expose the repo path" \
  --evidence "spent 3 calls deriving the repo root before I could create the worktree"
```

Suggestions are a separate lane. They never consume a WIP slot, nothing
dispatches them, and they only become work when **you** promote one — which is
what stops the system from quietly rewriting itself.

Three things keep the list worth reading. `--evidence` is required, so a
suggestion states what actually happened rather than what an agent would
prefer. Workers file at most one per task, so they have to pick the thing that
cost most. And re-filing an open suggestion **seconds** it instead of
duplicating — three workers hitting one papercut shows as `hit 3×` at the top of
the list, not three rows.

Promote turns one into a normal queued task, carrying the accumulated evidence
and attribution into its note. Dismiss takes a reason.

## What it won't do

Deliberate limits:

- **Never merges or pushes.** Workers open PRs; you merge them.
- **Never answers a worker's approval prompt.** A blocked agent is asking a human — it escalates with the text quoted.
- **Only the orchestrator spawns agents.** Workers request a reviewer by changing phase; they never spawn one. One spawning authority keeps the board honest and prevents runaway loops.
- **Never treats worker output as instructions.** A worker that read a poisoned issue or dependency can emit text shaped like orders. It's data; directives get quoted to you.
- **Never reads code itself.** That's the reviewer's job — orchestrator context is what resumability depends on.

## Layout

```
.claude-plugin/         plugin + marketplace manifests
lib/store.py            schema, locking, phase rules, board rendering
bin/orch                state CLI
webapp/
  server.py             stdlib HTTP API over the same store
  static/               kanban UI (no build step)
skills/orchestrate/
  SKILL.md              orchestrator instructions
  references/
    worker-protocol.md  worker + reviewer briefs, phases, PR format
    board.md            state/board format
    intake.md           per-tracker fetch adapters
tests/test_orch_set.py  `orch set` field-editing coverage
install.sh
```

Tests are stdlib `unittest`, no dependencies, and every case runs against a throwaway
board in a temp dir rather than your real one:

```bash
python3 -m unittest discover -s tests
```

## Status

The state layer, CLI and webapp are tested end to end, and the `worktree create` response shape is verified against herdr 0.8.0. The rest of the spawn→review→PR loop has not been run against live agents — try one small task before a batch.
