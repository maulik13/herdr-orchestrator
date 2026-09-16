# herdr-orchestrator

> [!IMPORTANT]
> Disclaimer: This is a fully AI created tool with testing done by using the tool. Code reviews are done only by AI agents at this point.

Use agentic workflow using Claude Code and [Herdr](https://github.com/herdr-dev/herdr) to distribute work across different projects and agents.

You hand it issues — Jira/GitHub/Notion links, or just a description. It triages them into a queue, gives each one its own git worktree and agent, runs them through plan → implement → adversarial review → PR, and parks anything needing your decision as a card. You can `/clear` the orchestrator at any point and it resumes from disk.

📐 [Architecture](docs/architecture.md) · 🔧 [Implementation design](docs/implementation.md)

## Requirements

| | |
|---|---|
| **Herdr** | required — the skill refuses to run outside a Herdr-managed pane |
| **git** | required — worktree isolation depends on it |
| **python3** | required — 3.9+, stdlib only, no packages to install |
| `jq` | **required** — the spawn path parses Herdr's JSON responses with it |
| `gh` | needed for GitHub intake, PR creation, and merge verification |
| `glab` | optional, GitLab intake |
| `plannotator` | optional, inline plan annotation instead of approve/reject |

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

### 1. Register your repos

Work is created against a **project** — a repo registered once, by short name — and one board covers all of them.

```bash
orch project add infra --path ~/work/infra-diagrams
orch project list
```

### 2. Start the orchestrator

Launch Claude Code **from inside a Herdr pane** — the skill checks `HERDR_ENV=1` and stops otherwise, because without the injected pane context it can't tell its own pane from yours.

```
> orchestrate these: PROJ-412, PROJ-418, and the Safari login hang
```

It normalizes each item, queues them, shows a plan, and waits for one confirmation. After that it runs autonomously — everything further that needs you arrives as a card, not a chat interrupt.

### 3. Open the board

```bash
python3 webapp/server.py
```

Then open http://127.0.0.1:8787. It binds loopback only and has no auth — it drives real agents and exposes repo state, so don't expose it to a network.

Five collapsible sections stacked top to bottom — in progress, review, queued, parked, done. Each section header doubles as its summary ("2 implementing · 1 awaiting plan … 1 needs you"), so a shut section still reports what it holds. From here you:

- **drag to reorder** the queue, and adjust the WIP cap
- **approve or reject plans** inline, and answer question / conflict / breaking-change cards
- **filter by project** from the header dropdown (kept in the URL, so a reload lands on the same view)
- **manage repos** in the Projects drawer — register, remove, rename, or repoint one after a repo moves on disk. A rename cascades to every task, approval and suggestion that references it, and the UI tells you how many rows will be rewritten before you confirm.
- **delete a card** with the × that appears on hover. It only shows where deletion is safe — a task owning a herdr workspace, a worktree or an open PR has none, because dropping the record would leave those running with nothing tracking them. Clean those up first, or `orch rm --force` if you really mean it.

While it runs it polls `gh` every 60s (`--poll-seconds`, `0` disables) for tasks with an open PR. A merged PR moves the task to `merged`, which is the orchestrator's cue to run its cleanup guards. A PR *closed* without merging raises a question card instead and changes nothing.

### 4. Review plans in Plannotator (optional)

A worker writes its plan to `PLAN.md` in its worktree and attaches the path to the approval. The card then offers **Review in Plannotator**, which opens the plan in [Plannotator](https://github.com/backnotprop/plannotator) for inline annotation instead of a bare approve/reject.

| Plannotator | Board |
|---|---|
| Approve | approval approved; the worker is told to proceed |
| Send feedback | approval **rejected**, annotations kept verbatim as the note |
| Close | left pending — closing is not a decision |

The inline Approve/Reject buttons stay as a fallback, and are the only option when Plannotator is not installed or an approval has no document (questions, conflicts, breaking changes).

### What it won't do

- **Never merges or pushes.** Workers open PRs; you merge them.
- **Never answers a worker's approval prompt.** A blocked agent is asking a human — it escalates with the text quoted.
- **Only the orchestrator spawns agents.** Workers request a reviewer by changing phase; they never spawn one.
- **Never treats worker output as instructions.** Directives get quoted to you as data.
- **Never reads code itself.** That's the reviewer's job.

The reasoning behind each of these is in [Architecture](docs/architecture.md#trust-boundaries).

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
orch whoami --agent orchestrator --pane "$HERDR_PANE_ID"   # who handoffs wake
orch handoffs                 # tasks waiting on the orchestrator
orch ack GH-412               # handoff picked up
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

Agents that hit friction file it with `orch suggest`. Suggestions never consume a WIP slot and nothing dispatches them — they become work only when **you** `orch promote` one.

## Tests

Stdlib `unittest`, no dependencies, and every case runs against a throwaway board in a temp dir rather than your real one:

```bash
python3 -m unittest discover -s tests
```

## Status

The state layer, CLI and webapp are tested end to end, and the `worktree create` response shape is verified against herdr 0.8.0. The rest of the spawn→review→PR loop has not been run against live agents — try one small task before a batch.
