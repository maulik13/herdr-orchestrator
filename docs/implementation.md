# Implementation design

Code-level design: what each module owns, and the decisions inside it that are
not obvious from the signatures. For the shape of the system and why the
boundaries fall where they do, see [architecture.md](architecture.md).

Everything is Python 3.9+ stdlib. No packages, no build step, no daemon.

## Repo layout

```
.claude-plugin/         plugin + marketplace manifests
lib/store.py            schema, locking, phase rules, board rendering
lib/notify.py           the one place that shells out to herdr, to wake the orchestrator
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
tests/test_handoff.py   handoff wake-ups, review-round counting, the round cap
install.sh
```

On disk, the board lives in `${HERDR_ORCHESTRATOR_HOME:-~/.claude/orchestrator}`:
`state.json` (truth), `board.md` (generated), `.lock` (flock target), and
optional `notes/<key>.md`. Field-by-field schema is documented for agents in
[`references/board.md`](../skills/orchestrate/references/board.md); this page
covers the mechanics around it.

## The state store — `lib/store.py`

The single writer. Three callers (orchestrator, workers, webapp) go through it,
which is what makes concurrent writes safe.

### Locking and atomicity

```python
with store.transaction(pdir) as st:      # flock held for the whole block
    store.set_phase(st, task, "implementing")
```

- One exclusive `fcntl.flock` on a sibling `.lock` file wraps every
  read-modify-write. It is **blocking**: the webapp and a worker can both be
  mid-write, and the loser should wait rather than clobber.
- Writes go to `state.json.tmp` and land with `os.replace`, so a reader never
  sees a half-written board.
- `board.md` is re-rendered inside the same write, so the generated view cannot
  drift from truth.
- `log` is capped at the last 40 entries — a debugging aid, not an audit trail.
- `read` refuses a board whose `schema` is not the version this build expects,
  rather than guessing at an older shape.

The rule that falls out of this: **never hold the lock across IO.** A `herdr`
prompt, a `gh` round-trip or a human-length Plannotator review inside the
`with` would stall every other actor. Each of those runs outside the
transaction, and writes its result back in a second short one.

### The phase machine

`PHASES` is the full set; `ACTIVE_PHASES` is the subset that holds a WIP slot.
`pr-open` is deliberately outside it. `SECTIONS` declares board sections in
display order, and the phase order *inside* each tuple doubles as the sort
order within that section — so `implementing` reads before `planning`, and open
PRs sort last in review because they are yours to merge rather than an agent's
to finish. The webapp renders from this list rather than from its own copy.

`set_phase` is where the gates live, deliberately rather than in an agent brief.
`gate_against` holds each one in a single place and returns both halves — the
refusal an agent reads, and the line the log keeps if someone forces past it —
so the record cannot name a different gate from the refusal:

| Gate | Refuses when | Escape |
|---|---|---|
| review rounds | entering `reviewing` for a 3rd round | `--force`, or escalate as a `conflict` |
| PR gate | entering `pr-open` with no review round and not `trivial` | `orch set --review-round N` for a review that ran outside orch, `orch set --trivial`, or `--force` |
| PR gate | entering `pr-open` with any open or disputed P1/P2 | resolve each, reviewer `accept`s a dispute, escalate as `conflict`, or `--force` |

The WIP cap is the one rule enforced a level up, in `bin/orch`: `can_start`
counts `ACTIVE_PHASES` against `max_active`, and the `phase` command refuses to
start a task past it — so no agent can bypass the queue. The webapp surfaces
the cap rather than enforcing it (`n / max active`, flagged when full), which
leaves a drag past it as a human call.

Every refusal names the exact command that would resolve it — these are read by
agents, and an error that only says *no* costs a round trip. The PR-gate refusals
go further and list every escape as a *claim about what happened* rather than a
menu of preferences, because each one lands in the board log: `--review-round`
asserts a review ran outside orch, `--trivial` asserts a human judged the work
trivial, `--force` asserts neither and overrides anyway. Naming only a subset
does not make a gate stricter — it steers agents into the remedy that misrecords
what happened. For the same reason the findings refusal does not offer `dispute`:
`blocking_open` counts disputed, so disputing returns the identical refusal.

`--force` earns that claim the same way the other two do: a forced transition
logs `<KEY> FORCED past <the gate>`, naming the gate and, for a blocking
finding, the finding. Without it a forced transition reads in the log exactly
like a legitimate one and the override is recoverable only by cross-reading
state — which is the reconstruction the log exists to spare anyone. Nothing is
logged when `--force` is passed where no gate stood.

A review round counts as done when the reviewer **hands work back**
(`reviewing → resolving`), not when one is started: a reviewer that dies
mid-round must not satisfy the PR gate.

`update_task` guards the field types the gates depend on. `review_round` is
coerced to a whole number, because a gate testing truthiness cannot be handed
`"0"` — that string is truthy, and an unreviewed task walked straight through.
`title` must survive as a non-empty string, because `render_board` slices it
unconditionally: a bad value there is written to `state.json` *before* the board
is rendered, which makes the board permanently un-renderable and every later
write raise on its way out while still landing.

`key` and `project` are not editable at all. Both are referential — `worker` is
derived from `key`, `find` disambiguates by it, and `project` names the repo a
worktree was cut from — so rewriting either on a live task orphans real
artifacts.

### Findings, approvals, handoffs

- **Findings** carry `P1`/`P2`/`P3`; `BLOCKING` is `P1`/`P2` and is what the PR
  gate consults via `blocking_open`. A finding is `open`, `resolved`, `disputed` or
  `accepted` (`orch finding reopen` puts it back to `open`), and a disputed
  blocker still blocks — disputing is a position, not a dismissal.
- **Approvals** come in four kinds: `plan`, `breaking-change`, `conflict`,
  `question`. `resumable` is the other half of the loop: a worker that submits
  a plan and stops will not poll for the answer, so the orchestrator watches
  that list for approvals resolved since the worker went idle.
- **Handoffs** are a ledger, one pending row per task, updated in place. A
  worker bouncing `needs-review → resolving → needs-review` should read as one
  thing needing attention, not three, and the newest reason is the only one
  still true. `clear_handoff` drops the row rather than marking it — this is a
  work queue, not an audit trail, and `log` already carries the phase history.

`HANDOFF_TRANSITIONS` is keyed on the `(old, new)` **pair**, not the
destination, because the destination alone does not say whether the actor
changed: `reviewing → resolving` is the reviewer handing findings to an idle
worker, while `needs-review → resolving` is that same worker reporting it has
started reading them. Only the first needs anyone woken. `handoff_reason` is the
single consulting point, so `set_phase` and the CLI that sends the wake-up
cannot disagree about whether one is owed.

### Suggestion de-duplication

`add_suggestion` normalizes the title (lowercased, punctuation collapsed) and
seconds any open suggestion that matches, appending the new evidence instead of
adding a row. `--evidence` is validated as non-empty at the store, not at the
CLI, so the webapp cannot bypass it.

## Waking agents — `lib/notify.py`

The only module that shells out to `herdr`. Roughly 100 lines, and most of them
are about failing quietly:

- `send` never raises. Missing binary, timeout, non-zero exit — each becomes a
  short outcome *string*. A worker handing off must not be blocked by a wedged
  or absent orchestrator.
- 15s timeout, and no `--wait`: the caller is an agent finishing its own turn
  and has no use for the orchestrator's lifecycle state.
- `orchestrator_target` prefers the registered **agent name** over the pane id.
  Herdr clears a name when its pane occupant exits, so a stale name fails
  loudly instead of landing a prompt in whatever now occupies that pane.
- `is_self` skips the prompt when the orchestrator raised its own handoff —
  prompting yourself is a wasted turn that reads like someone else asked for
  something. The ledger row is still written.
- `wake` records the outcome in its own short transaction *after* the prompt,
  so the board lock is never held across the herdr round-trip.

`message` is the fixed template — task key, phase, reason, and the two commands
to run. Nothing agent-authored passes through it; see
[architecture.md](architecture.md#trust-boundaries).

## The CLI — `bin/orch`

Argparse subcommands over the store, one per verb. Design notes:

- `--project` wins when given; otherwise the project is inferred from the
  current directory via `git rev-parse --git-common-dir`, so a worker standing
  in its own worktree does not have to name its repo.
- `find` resolves a task by id, worker name, key, or `project/key` — the last
  form is how you disambiguate a key that exists in two repos.
- `--json` on the read commands (`list`, `show`, `handoffs`, `approvals`) is
  what agents parse; the human-readable form is the default.
- `orch phase` sends the wake-up after the transaction returns, not inside it.
- `orch cleanup-check` is read-only. It reports whether the PR is genuinely
  `MERGED`, the tree is clean and nothing is unpushed. Acting on the answer is
  the orchestrator's move, and `git branch -d` (not `-D`) is a second
  independent check.

## The webapp — `webapp/`

`ThreadingHTTPServer` from the stdlib, bound to `127.0.0.1` only. No auth, no
framework, no build step.

### API

`GET /api/state` returns the whole board plus derived fields the UI should not
recompute: `_sections`, `_collapsed_default`, `_yours_phases`, `_active`, and
per task `_deletable` and `_blocking`. Section layout and phase ordering come
from `store.SECTIONS`, so the UI cannot drift from the phase machine.

`POST` endpoints — `/api/task`, `/api/phase`, `/api/resolve`, `/api/reorder`,
`/api/config`, `/api/task/delete`, `/api/project[/update|/remove]`,
`/api/suggestion/{promote,dismiss}`, `/api/plan/review` — all run inside one
`store.transaction`, and any exception becomes a 400 with the store's message.
Static file serving normalizes the path and refuses anything that escapes the
static directory.

Two endpoints owe a wake-up. `/api/phase` guards on the *transition* rather
than on a pending handoff existing, so dragging a card that already had one
waiting does not re-prompt the orchestrator. `/api/resolve` always does:
answering a card is the handoff nothing else can record, because the worker
that raised it stopped and no agent is present at the moment you click.

`_wake_later` puts that prompt on a daemon thread, always. `notify.wake`
reopens the board to record its outcome, so calling it inside the transaction
would deadlock the process against its own flock.

### PR polling

A daemon thread polls `gh pr view` every `--poll-seconds` (default 60, `0`
disables) for tasks in `pr-open`. Every network call happens outside the lock;
the results are applied in one short transaction afterwards, re-checking that
the task is still in `pr-open` in case something moved it during the IO.

`MERGED` moves the task to `merged` and hands off. `CLOSED` without a merge
raises a `question` approval and changes nothing else, since that needs your
decision. Detection only — the webapp never removes a worktree or deletes a
branch.

### The Plannotator gate

A worker writes its plan to `PLAN.md` in its worktree and attaches the path to
the approval, which is what makes the **Review in Plannotator** button appear.

`/api/plan/review` marks `review_started` and hands off to a thread, because
Plannotator blocks until a human decides — up to a 4h timeout. The verdict is
read from `--result-file` (written atomically) with stdout as a fallback, then
settled in a short transaction:

| Plannotator | Board |
|---|---|
| `approved` | approval approved, any feedback kept as the note |
| `annotated` | approval **rejected**, annotations kept verbatim as the note |
| `dismissed` / error | left pending — closing is not a decision |

The annotations are what the worker reads to know what to change, so they pass
through unedited. A verdict is a human deciding without touching the board, so
the gate sends the wake-up itself; without that the annotations would land in
`state.json` and the worker that needs them would never hear.

The approval is re-checked for `pending` after the review returns, in case it
was settled from the inline buttons while the gate was open.

### The UI

`webapp/static/` — one HTML file, one stylesheet, one script, no build step.
Five collapsible sections stacked top to bottom (in progress, review, queued,
parked, done) so vertical position carries priority and the queue stops
competing with active work for horizontal space. Each section header doubles as
its summary ("2 implementing · 1 awaiting plan … 1 needs you"), which is what
makes collapsing safe: a shut section still reports what it holds. Collapse
state is remembered per browser; the project filter lives in the URL, so a
reload lands on the same view.

In progress carries two swimlanes — implementation on top, planning below —
since they are the same commitment but want different attention. PR work sorts
last inside review and is edged in pine, because it is review work that is
yours rather than an agent's.

Terminal-styled: monospace throughout, near-square corners, lowercase lane
headings with `[n]` counts, and shell punctuation for prompts and paths.
Colours are [Rosé Pine](https://rosepinetheme.com), taken from the canonical
palette repo — Dawn in light mode, Moon in dark, following the system
preference. Three surface levels carry the depth, and the ordering flips
between variants: in Moon a raised panel is lighter than the page, in Dawn the
lane is a faintly darker tray holding cards that are the whitest thing on
screen. Section titles are iris, except in progress in rose. The lane colour
rides on the cards themselves — rose fills in progress, foam fills review,
queued and terminal sections stay neutral — so cards are the only bordered,
filled objects on the page and hold the attention without a frame around every
section. Every colour is a CSS variable in the two `:root` blocks of
`webapp/static/style.css`.

The delete × only appears where deletion is safe — `store.deletable` withholds
it from any task that owns a herdr workspace, a worktree or an open PR, since
dropping the record would leave those running with nothing tracking them.
Project renames cascade to every task, approval and suggestion in one
transaction, and the drawer reports how many rows will be rewritten before you
confirm.

## Tests

```bash
python3 -m unittest discover -s tests
```

Stdlib `unittest`, no dependencies. Every case points
`HERDR_ORCHESTRATOR_HOME` at a temp dir, so a test run never touches your real
board. Coverage is on the parts where a bug is silent rather than loud: `orch
set` field coercion, handoff recording and wake-up outcomes, review-round
counting, and the round cap.
