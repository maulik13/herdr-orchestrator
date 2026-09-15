# State format

`state.json` is the source of truth. `board.md` is regenerated from it on every
write and is never parsed back — it exists so a human (or a resuming
orchestrator) can read the situation with one `cat`.

Reach both only through the `orch` CLI. It holds an exclusive file lock around
every read-modify-write, which is what lets workers, the orchestrator and the
webapp all write without corrupting each other. Hand-editing `state.json` races
those writers; hand-editing `board.md` is simply lost on the next write.

## Layout on disk

```
$ORCH_HOME/                  # ${HERDR_ORCHESTRATOR_HOME:-$HOME/.claude/orchestrator}
├── state.json               # truth — projects + tasks + approvals, all repos
├── board.md                 # generated view
├── .lock                    # flock target
└── notes/<key>.md           # optional, only when a decision needs recording
```

One board, not one per repo, because work is managed across repositories in a
single place. A **project** is a registered repository:

```json
{ "name": "infra", "path": "/Users/me/work/infra-diagrams", "added": "..." }
```

`path` is the resolved main repo root (via `git rev-parse --git-common-dir`),
so it is stable whether it was registered from the main checkout or a linked
worktree. A project cannot be removed while it still has live tasks.

## Task fields

```json
{
  "id": "t52847411",
  "key": "GH-412",
  "project": "infra",
  "title": "Login hangs on Safari",
  "source": "github",
  "url": "https://github.com/o/r/issues/412",
  "done_when": "Safari 17 loads /login under 2s, suite passes",
  "kind": "claude",
  "phase": "implementing",
  "order": 1,
  "worker": "gh-412",
  "reviewer": "rev-gh-412",
  "workspace": "w4",
  "pane": "w4:p1",
  "worktree": "/Users/me/work/api-wt/gh-412",
  "branch": "fix/gh-412-login",
  "pr_url": null,
  "pr_state": null,
  "review_round": 0,
  "note": null
}
```

`id` is the stable handle the webapp uses; `key` is what humans say. `orch`
accepts `id`, `worker`, `key`, or `project/key` wherever a task is named.

Ids and worker names are globally unique; **keys are only unique within a
project**, since two repos may both have a `DOC-1`. A bare ambiguous key is
refused with the list of matching projects rather than resolved by guesswork.

`worker` is the join key between this board and `herdr agent list`. A herdr
agent name is cleared when its agent exits or is replaced, so a task in an
active phase whose worker is absent from `agent list` means the agent is **gone**
— never that it succeeded.

## Phases

`queued` `planning` `awaiting-plan` `implementing` `needs-review` `reviewing`
`resolving` `awaiting-decision` `pr-open` `merged` `archived` `blocked` `parked`

The webapp shows `implementing`, `planning` and `awaiting-plan` in one
**In progress** column split into two swimlanes, implementation above planning.
The phases themselves are unchanged — that grouping is presentation only, and
`board.md` orders the column the same way.

Everything from `planning` through `awaiting-decision` counts against
`max_active`. `pr-open` deliberately does not: a PR can wait days on human
review, and holding a slot for that would starve the queue. `orch phase`
enforces the cap on entry to an active phase.

## Approvals

Anything needing the human is an approval record, surfaced in the webapp's
"Awaiting you" section and by `orch approvals`.

| Kind | Raised when |
|---|---|
| `question` | requirement is ambiguous; worker stops rather than guessing |
| `plan` | implementation plan TLDR, before any code is written |
| `breaking-change` | change would break existing behaviour |
| `conflict` | reviewer and implementer disagree after two rounds |

```json
{ "id": "a52848085", "task": "t52847411", "key": "GH-412", "kind": "plan",
  "title": "Swap polling for an event listener", "body": "...markdown...",
  "status": "pending", "decision_note": null }
```

Keep `title` to one line — it is the whole card in a scan — and put reasoning in
`body` via `--body-file`. A rejection carries `decision_note`, which is what the
worker reads to know what to change.

## Notes and log

`log` keeps the last 40 entries, appended automatically on every mutation. It is
trimmed on write, because beyond that it becomes a context tax on every resume
rather than a useful history.

Write `notes/<key>.md` only when a decision, dead end or constraint would
otherwise be lost — cap it around 15 lines. Both the board and the notes get
read into a fresh context on every resume, so sprawl there is paid for
repeatedly.
