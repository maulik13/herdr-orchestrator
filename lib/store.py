"""State store for the herdr orchestrator.

state.json is the source of truth. board.md is regenerated from it on every
write, so a resuming orchestrator can `cat` one file and a human can read the
same thing in git. Both the `orch` CLI and the webapp go through this module,
which is what keeps concurrent writes from the two of them safe: every
read-modify-write happens inside an flock on a sibling .lock file.
"""

import fcntl
import hashlib
import json
import os
import re
import subprocess
import time
from contextlib import contextmanager
from datetime import datetime, timezone

SCHEMA_VERSION = 2

# Phases an agent is actively holding a slot for. `pr-open` is deliberately
# excluded: a PR can sit for days waiting on human review, and blocking the
# queue on that would starve everything behind it.
ACTIVE_PHASES = [
    "planning",
    "awaiting-plan",
    "implementing",
    "needs-review",
    "reviewing",
    "resolving",
    "awaiting-decision",
]

PHASES = ["queued"] + ACTIVE_PHASES + [
    "pr-open",
    "merged",
    "archived",
    "blocked",
    "parked",
]

TERMINAL_PHASES = ["archived"]

# Board sections, in display order. The phase order INSIDE each tuple is also
# the sort order within that section — implementing reads before planning, and
# open PRs sort last in review because they are yours to merge rather than an
# agent's to finish.
SECTIONS = [
    ("in progress", ["implementing", "planning", "awaiting-plan"]),
    ("review",      ["needs-review", "reviewing", "resolving", "awaiting-decision",
                     "pr-open", "merged"]),
    ("queued",      ["queued"]),
    ("parked",      ["parked", "blocked"]),
    ("done",        ["archived"]),
]

# Sections that start shut. Their header summary is what keeps them useful
# closed, so nothing here is hidden — only folded.
COLLAPSED_BY_DEFAULT = ["parked", "done"]

# Phases that are the human's move rather than an agent's, surfaced in a
# section header so a shut section still says what it is waiting on.
YOURS_PHASES = ["awaiting-plan", "awaiting-decision", "pr-open"]

# Transitions where the agent that made the move is NOT the agent that has to
# act next, so somebody idle has to be woken.
#
# Herdr has no event bus: the only way to wake an idle agent is
# `herdr agent prompt`, which some already-running process must call. The
# orchestrator is a Claude Code session with no background loop — it runs only
# when prompted — so any handoff routed through it stalls until a human pokes
# it. Recording the handoff here, on the one command every agent already runs,
# is what makes the wake-up automatic rather than a step in a brief that can be
# forgotten.
#
# Keyed on the pair, not the destination, because the destination alone does not
# say whether the actor changed: `reviewing -> resolving` is the reviewer
# handing findings back to an idle worker, while `needs-review -> resolving` is
# that same worker reporting it has started reading them. Only the first needs
# anyone woken.
HANDOFF_TRANSITIONS = {
    ("reviewing", "resolving"):
        "review round finished — relay the findings to the worker",
    ("implementing", "needs-review"):
        "implementation ready — start a reviewer",
    ("resolving", "needs-review"):
        "fixes ready — wake the existing reviewer for a re-check",
    ("planning", "awaiting-plan"):
        "plan submitted — awaiting the human",
    ("implementing", "awaiting-plan"):
        "question raised mid-implementation — awaiting the human",
    ("pr-open", "merged"):
        "PR merged — run cleanup-check",
}

# Every transition into these phases needs the orchestrator regardless of where
# it came from: a decision request can be raised from any active phase.
HANDOFF_PHASES = ["awaiting-decision"]


def section_of(phase):
    for name, phases in SECTIONS:
        if phase in phases:
            return name
    return None


def sort_key(t):
    """Position within a section: declared phase order, then queue order."""
    for _, phases in SECTIONS:
        if t["phase"] in phases:
            return (phases.index(t["phase"]), t.get("order", 0))
    return (99, t.get("order", 0))


SEVERITIES = ["P1", "P2", "P3"]
BLOCKING = ["P1", "P2"]          # P3 is advisory and never blocks a PR

APPROVAL_KINDS = ["plan", "breaking-change", "conflict", "question"]

# Review rounds before a disagreement becomes the human's. Counted on the
# reviewer handing work back, so this is rounds *completed*.
MAX_REVIEW_ROUNDS = 2


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def orch_home():
    return os.environ.get(
        "HERDR_ORCHESTRATOR_HOME", os.path.join(os.path.expanduser("~"), ".claude", "orchestrator")
    )


def repo_root(cwd=None):
    """Main repo root, resolved so it is stable from inside a linked worktree."""
    cwd = cwd or os.getcwd()
    try:
        out = subprocess.run(
            ["git", "-C", cwd, "rev-parse", "--path-format=absolute", "--git-common-dir"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        return os.path.dirname(out)
    except Exception:
        return None


def slug_for(root):
    h = hashlib.sha1(root.encode()).hexdigest()[:6]
    return "%s-%s" % (os.path.basename(root), h)


def board_dir():
    """The single board. Work spans repos, so state is not scoped to one."""
    return orch_home()


def resolve_repo(path):
    """Absolute main-repo root for a path, or raise if it is not a git repo."""
    p = os.path.abspath(os.path.expanduser(path))
    r = subprocess.run(
        ["git", "-C", p, "rev-parse", "--path-format=absolute", "--git-common-dir"],
        capture_output=True, text=True)
    if r.returncode != 0:
        raise ValueError("%s is not a git repository" % p)
    return os.path.dirname(r.stdout.strip())


PROJECT_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")

# Herdr's own rule for a live agent name. Validated here so a bad name is
# refused when it is recorded rather than failing later, at the moment a
# worker is trying to hand work back.
AGENT_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")


def add_project(state, name, path):
    if not PROJECT_RE.match(name):
        raise ValueError("project name %r must match [a-z0-9][a-z0-9_-]{0,31}" % name)
    root = resolve_repo(path)
    for p in state.setdefault("projects", []):
        if p["name"] == name:
            raise ValueError("project %r already registered at %s" % (name, p["path"]))
        if p["path"] == root:
            raise ValueError("%s is already registered as %r" % (root, p["name"]))
    rec = {"name": name, "path": root, "added": now()}
    state["projects"].append(rec)
    log(state, "registered project %s -> %s" % (name, root))
    return rec


def update_project(state, name, new_name=None, new_path=None):
    """Rename a project and/or point it at a different repo.

    Tasks, approvals and suggestions reference a project by name, so a rename
    has to cascade to all of them in the same transaction — a half-applied
    rename would orphan every task in the project.
    """
    proj = find_project(state, name)
    if not proj:
        raise KeyError("no project %r" % name)

    changes = []

    if new_path:
        root = resolve_repo(new_path)
        clash = next((p for p in state["projects"]
                      if p["path"] == root and p["name"] != name), None)
        if clash:
            raise ValueError("%s is already registered as %r" % (root, clash["name"]))
        if root != proj["path"]:
            changes.append("path %s -> %s" % (proj["path"], root))
            proj["path"] = root

    if new_name and new_name != name:
        if not PROJECT_RE.match(new_name):
            raise ValueError("project name %r must match [a-z0-9][a-z0-9_-]{0,31}" % new_name)
        if find_project(state, new_name):
            raise ValueError("project %r already exists" % new_name)
        n = 0
        for coll in ("tasks", "approvals", "suggestions"):
            for row in state.get(coll, []):
                if row.get("project") == name:
                    row["project"] = new_name
                    n += 1
        proj["name"] = new_name
        changes.append("name %s -> %s (%d reference%s updated)"
                       % (name, new_name, n, "" if n == 1 else "s"))

    if not changes:
        raise ValueError("nothing to change")
    proj["updated"] = now()
    log(state, "project %s: %s" % (name, "; ".join(changes)))
    return proj


def remove_project(state, name):
    held = [t["key"] for t in state["tasks"]
            if t.get("project") == name and t["phase"] not in TERMINAL_PHASES]
    if held:
        raise ValueError("project %r still has live tasks: %s" % (name, ", ".join(held)))
    before = len(state.get("projects", []))
    state["projects"] = [p for p in state.get("projects", []) if p["name"] != name]
    if len(state["projects"]) == before:
        raise KeyError("no project %r" % name)
    log(state, "removed project %s" % name)


def find_project(state, name):
    for p in state.get("projects", []):
        if p["name"] == name:
            return p
    return None


def project_for_cwd(state, cwd=None):
    """Which registered project contains this directory, if any.

    Lets a worker or the orchestrator infer the project from where it is
    standing, while leaving the creator free to name one explicitly.
    """
    try:
        root = resolve_repo(cwd or os.getcwd())
    except Exception:
        return None
    for p in state.get("projects", []):
        if p["path"] == root:
            return p
    return None


def worker_name(key, taken=()):
    """Sanitise an issue key into a herdr agent name: [a-z][a-z0-9_-]{0,31}."""
    n = re.sub(r"[^a-z0-9]+", "-", key.lower()).strip("-")[:32]
    if not n or not n[0].isalpha():
        n = ("t-" + n)[:32]
    base, i = n, 2
    while n in taken:
        suffix = "-%d" % i
        n = base[: 32 - len(suffix)] + suffix
        i += 1
    return n


@contextmanager
def locked(pdir):
    os.makedirs(pdir, exist_ok=True)
    lock_path = os.path.join(pdir, ".lock")
    with open(lock_path, "w") as fh:
        # Blocking flock: the webapp and the orchestrator can both be mid-write,
        # and the loser should wait rather than clobber.
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _blank():
    return {
        "schema": SCHEMA_VERSION,
        "max_active": 3,
        "updated": now(),
        "projects": [],
        "tasks": [],
        "suggestions": [],
        "findings": [],
        "approvals": [],
        "handoffs": [],
        "orchestrator": None,
        "log": [],
    }


def _path(pdir):
    return os.path.join(pdir, "state.json")


def read(pdir):
    p = _path(pdir)
    if not os.path.exists(p):
        return _blank()
    with open(p) as fh:
        st = json.load(fh)
    if st.get("schema", 1) != SCHEMA_VERSION:
        raise ValueError("board at %s uses schema v%s; this build expects v%s"
                         % (p, st.get("schema", 1), SCHEMA_VERSION))
    return st


def write(pdir, state):
    state["updated"] = now()
    state["log"] = state.get("log", [])[-40:]
    os.makedirs(pdir, exist_ok=True)
    tmp = _path(pdir) + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(state, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, _path(pdir))
    render_board(pdir, state)
    return state


@contextmanager
def transaction(pdir):
    """Read-modify-write under lock. Yields the state dict; mutate it in place."""
    with locked(pdir):
        state = read(pdir)
        yield state
        write(pdir, state)


def log(state, msg):
    state.setdefault("log", []).append("%s %s" % (now(), msg))


def find(state, tid, project=None):
    """Resolve by id, worker name, key, or project/key.

    Ids and worker names are globally unique. Keys are not — two repos can both
    have a DOC-1 — so an ambiguous key raises instead of guessing which repo
    the caller meant.
    """
    if tid and "/" in tid:
        project, tid = tid.split("/", 1)
    for t in state["tasks"]:
        if t["id"] == tid or t.get("worker") == tid:
            return t
    hits = [t for t in state["tasks"]
            if t.get("key") == tid and (project is None or t.get("project") == project)]
    if len(hits) > 1:
        raise ValueError("%r is ambiguous across projects (%s); qualify it as <project>/%s"
                         % (tid, ", ".join(sorted(t.get("project") or "?" for t in hits)), tid))
    return hits[0] if hits else None


def active_tasks(state):
    return [t for t in state["tasks"] if t["phase"] in ACTIVE_PHASES]


def next_queued(state):
    q = sorted(
        [t for t in state["tasks"] if t["phase"] == "queued"],
        key=lambda t: t.get("order", 0),
    )
    return q[0] if q else None


def can_start(state):
    return len(active_tasks(state)) < int(state.get("max_active", 3))


def add_task(state, key, title, source="manual", url=None, done_when=None,
             kind="claude", project=None):
    if not find_project(state, project):
        known = ", ".join(p["name"] for p in state.get("projects", [])) or "none registered"
        raise ValueError("unknown project %r (known: %s). Register it with "
                         "`orch project add <name> --path <repo>`." % (project, known))
    taken = {t.get("worker") for t in state["tasks"] if t.get("worker")}
    orders = [t.get("order", 0) for t in state["tasks"]] or [0]
    task = {
        "id": "t%d" % (int(time.time() * 1000) % 100000000),
        "key": key,
        "project": project,
        "title": title,
        "source": source,
        "url": url,
        "done_when": done_when,
        "kind": kind,
        "phase": "queued",
        "order": max(orders) + 1,
        "worker": worker_name(key, taken),
        "reviewer": None,
        "workspace": None,
        "pane": None,
        "worktree": None,
        "branch": None,
        "pr_url": None,
        "pr_state": None,
        "review_round": 0,
        "trivial": False,
        "note": None,
        "created": now(),
        "updated": now(),
    }
    state["tasks"].append(task)
    log(state, "added %s/%s (%s)" % (project, key, task["worker"]))
    return task


# Fields an existing task may be edited through. The first row is intake
# metadata — what the issue is and where it came from. It is editable because
# intake is not always right the first time: a task adopted mid-flight starts
# with an empty done_when, and a mis-scoped one has to be correctable against
# its own definition of done. Without this the only route was delete-and-
# recreate, which costs the task its queue position and its id.
#
# `key` and `project` are deliberately absent. Both are referential: `worker`
# is derived from `key` when the task is created, `find` resolves and
# disambiguates by `key`, and `project` names the repo a worktree was cut from.
# Rewriting either on a live task orphans real artifacts.
EDITABLE_FIELDS = (
    "title", "url", "source", "done_when",
    "branch", "workspace", "pane", "worktree", "pr_url", "pr_state",
    "reviewer", "note", "review_round", "trivial",
)

# Fields that must survive as a non-empty string. `render_board` slices
# `title` unconditionally, so anything else there — None, a number, a list —
# is not a cosmetic problem: it is written to state.json before the board is
# rendered, so the board becomes permanently un-renderable and every later
# `orch` write raises on its way out while still landing the write.
REQUIRED_FIELDS = ("title",)

# Fields stored as a count. `review_round` is read by the `pr-open` gate, and a
# gate that tests truthiness cannot be handed a string: "0" is truthy, so an
# unreviewed task walked straight through. Coerced on the way in so the value
# on disk is the type the gate expects.
INT_FIELDS = ("review_round",)


def as_count(field, v):
    """Parse a whole number, zero or more. Raises ValueError on anything else.

    Deliberately strict: `int(3.7)` would silently truncate, and `int(True)` is
    1, neither of which anyone meant to write into a review count.
    """
    try:
        n = int(str(v).strip())
    except (TypeError, ValueError):
        raise ValueError("%s must be a whole number, got %r" % (field, v))
    if n < 0:
        raise ValueError("%s must be zero or more, got %r" % (field, v))
    return n


def review_rounds(t):
    """How many review rounds a task has recorded, as an int.

    Tolerant of boards written before `review_round` was coerced: values are
    already on disk as strings, there is no migration, and this is a safety
    gate, so it reads defensively and fails closed — anything unparseable
    counts as zero rounds, which blocks rather than opens.
    """
    try:
        return as_count("review_round", t.get("review_round") or 0)
    except ValueError:
        return 0


def update_task(state, tid, updates):
    """Apply field edits to an existing task.

    An empty string clears a field to None — that is the only way to unset one,
    and a cleared field reads exactly like one never supplied at intake.
    Fields in REQUIRED_FIELDS cannot be cleared, and are checked by type rather
    than by emptiness: the CLI can only hand this function strings, but the
    webapp is invited to call it too, and JSON has more ways to say "blank".
    """
    t = find(state, tid)
    if not t:
        raise KeyError("no task %r" % tid)
    if not updates:
        raise ValueError("nothing to set")

    clean = {}
    for k, v in updates.items():
        if k not in EDITABLE_FIELDS:
            raise ValueError("%r is not an editable field; expected one of %s"
                             % (k, ", ".join(sorted(EDITABLE_FIELDS))))
        if isinstance(v, str):
            v = v.strip() or None
        if k in REQUIRED_FIELDS and not isinstance(v, str):
            raise ValueError("%s must be a non-empty string, got %r — a task with no "
                             "readable %s is a blank row on the board" % (k, v, k))
        if k in INT_FIELDS and v is not None:
            v = as_count(k, v)
        clean[k] = v

    t.update(clean)
    t["updated"] = now()
    # Name the field, and for the counts a gate reads, the value too:
    # `--review-round 3` is a claim that three rounds happened, and a log line
    # saying only "set review_round" leaves that claim unauditable afterwards.
    written = ["%s=%s" % (k, clean[k]) if k in INT_FIELDS else k for k in sorted(clean)]
    log(state, "%s set %s" % (t["key"], ", ".join(written)))
    return t


def set_phase(state, tid, phase, note=None, force=False):
    if phase not in PHASES:
        raise ValueError("unknown phase %r; expected one of %s" % (phase, ", ".join(PHASES)))
    t = find(state, tid)
    if not t:
        raise KeyError("no task %r" % tid)

    # The two-round cap, enforced here rather than left to the orchestrator
    # remembering. Its memory is the one thing designed to be cleared, and a
    # cap that evaporates on /clear is not a cap. Implementer and reviewer can
    # disagree indefinitely, and each round costs real tokens for less return.
    if phase == "reviewing" and not force and review_rounds(t) >= MAX_REVIEW_ROUNDS:
        raise ValueError(
            "%s has already had %d review rounds. Anything still disputed is the "
            "conflict — escalate it with `orch finding list %s --open --blocking` and "
            "`orch approve-request %s --kind conflict`, or "
            "`orch phase %s reviewing --force` to buy another round."
            % (t["key"], review_rounds(t), t["key"], t["key"], t["key"]))

    # Opening a PR is the point of no return for unreviewed code, so the review
    # gate is enforced here rather than left to an agent remembering a brief.
    # Three sanctioned routes past it: the round is recorded (including one that
    # ran outside orch), the task was marked trivial at intake, or a human passed
    # --force. All three are recorded; silently skipping review is not.
    if phase == "pr-open" and not force and not t.get("trivial"):
        if not review_rounds(t):
            # Every line here is a claim about what happened, and the board keeps
            # it, so the menu is written as facts to pick between rather than
            # options to prefer. `--review-round` is the honest remedy when a
            # review ran somewhere this board never saw — omitting it pushed
            # agents towards `--trivial` (a human judgement nobody made) or
            # `--force` (an override of a gate that was right to block). It sits
            # under its condition and never first, because it is also the one
            # flag that could walk genuinely unreviewed work through.
            raise ValueError(
                "%s has no review round recorded and is not marked trivial.\n"
                "Each way past this gate is a claim about what happened, and each is "
                "recorded — pick the true one:\n"
                "  not reviewed yet         run the reviewer: `orch phase %s needs-review`\n"
                "  reviewed outside orch    `orch set %s --review-round N` — N = rounds "
                "that actually ran\n"
                "  does not merit a review  `orch set %s --trivial` — a judgement about "
                "the work, recorded as one\n"
                "  none of the above        `orch phase %s pr-open --force` — recorded as "
                "an override, not as a review"
                % (t["key"], t["key"], t["key"], t["key"], t["key"]))
        # A round having happened is not the same as its findings being dealt
        # with, which is what REVIEW.md could never answer mechanically.
        blockers = blocking_open(state, t["id"])
        if blockers:
            # `blocking_open` counts `disputed`, so telling anyone to dispute
            # their way past this returned the identical refusal and cost a
            # round trip. Disputing is a position, not a dismissal: what clears
            # a dispute is the reviewer accepting it, or the human settling it.
            raise ValueError(
                "%s has %d unresolved blocking finding(s): %s.\n"
                "Disputing does not clear them — a disputed blocker still blocks, by "
                "design. What does:\n"
                "  you fixed it        `orch finding resolve <id> --note \"what changed\"`\n"
                "  your dispute stands the reviewer runs `orch finding accept <id>` — "
                "the reviewer's call, not yours\n"
                "  you cannot agree    `orch approve-request %s --kind conflict "
                "--title \"...\"`\n"
                "  none of the above   `orch phase %s pr-open --force` — recorded as "
                "an override, not as a review"
                % (t["key"], len(blockers),
                   ", ".join("%s %s" % (f["id"], f["severity"]) for f in blockers),
                   t["key"], t["key"]))

    old = t["phase"]
    t["phase"] = phase
    t["updated"] = now()
    if note:
        t["note"] = note
    log(state, "%s %s -> %s%s" % (t["key"], old, phase, (": " + note) if note else ""))

    # A round counts as done when the reviewer hands it back, not when one is
    # started: a reviewer that dies mid-round must not satisfy the `pr-open`
    # gate. Nothing incremented this before, so that gate refused every
    # non-trivial task no matter how thoroughly it had been reviewed.
    if (old, phase) == ("reviewing", "resolving"):
        t["review_round"] = review_rounds(t) + 1

    reason = handoff_reason(old, phase)
    if reason:
        add_handoff(state, t["id"], phase, reason)
    return t


def handoff_reason(old, new):
    """Why this transition needs the orchestrator, or None if it does not.

    The one place the transition table is consulted, so `set_phase` and the
    `orch` command that sends the wake-up cannot disagree about whether one is
    owed.
    """
    if old == new:
        return None
    if new in HANDOFF_PHASES:
        return "decision requested — awaiting the human"
    return HANDOFF_TRANSITIONS.get((old, new))


def _norm(t):
    return re.sub(r"[^a-z0-9]+", " ", (t or "").lower()).strip()


def add_finding(state, tid, severity, title, detail="", where=None, source=None):
    """Record one review finding against a task.

    Findings are board state, not a file in the worktree. A REVIEW.md dies with
    the worktree it was written in, is invisible to the human until someone
    opens a pane, and cannot be checked mechanically — so "are P1 and P2
    resolved?" ends up being answered by an agent reading its own prose.
    """
    if severity not in SEVERITIES:
        raise ValueError("severity must be one of %s" % ", ".join(SEVERITIES))
    t = find(state, tid)
    if not t:
        raise KeyError("no task %r" % tid)
    f = {
        "id": "f%d" % (int(time.time() * 1000) % 100000000),
        "task": t["id"],
        "key": t["key"],
        "project": t.get("project"),
        # `review_round` counts rounds handed back, so the one being filed
        # against is the next one. Reading the field directly would label every
        # round-two finding as round one.
        "round": review_rounds(t) + 1,
        "severity": severity,
        "title": title,
        "detail": detail,
        "where": where,
        "status": "open",
        "source": source,
        "response": None,
        "created": now(),
        "updated": now(),
    }
    state.setdefault("findings", []).append(f)
    log(state, "%s finding %s %s: %s" % (t["key"], f["id"], severity, title))
    return f


def findings_for(state, tid, open_only=False, blocking_only=False):
    t = find(state, tid)
    if not t:
        raise KeyError("no task %r" % tid)
    out = [f for f in state.get("findings", []) if f["task"] == t["id"]]
    if open_only:
        out = [f for f in out if f["status"] in ("open", "disputed")]
    if blocking_only:
        out = [f for f in out if f["severity"] in BLOCKING]
    return sorted(out, key=lambda f: (SEVERITIES.index(f["severity"]), f["created"]))


def blocking_open(state, tid):
    """Open P1/P2 findings — what stands between a task and a PR."""
    return [f for f in findings_for(state, tid, open_only=True, blocking_only=True)]


def set_finding(state, fid, status, response=None):
    if status not in ("open", "resolved", "disputed", "accepted"):
        raise ValueError("unknown finding status %r" % status)
    for f in state.get("findings", []):
        if f["id"] == fid:
            f["status"] = status
            f["response"] = response or f.get("response")
            f["updated"] = now()
            log(state, "%s finding %s -> %s" % (f["key"], fid, status))
            return f
    raise KeyError("no finding %r" % fid)


def add_suggestion(state, title, evidence, body="", source=None, project=None):
    """File an improvement suggestion.

    Suggestions are deliberately NOT tasks. They never consume a WIP slot and
    are never dispatched; a human promotes one into real work or dismisses it.
    That gap is what stops the system from rewriting itself unsupervised.

    Re-filing something already open does not create a duplicate — it seconds
    the existing one and appends the new evidence. The same papercut hit by
    three workers should read as one high-priority item, not three rows.
    """
    if not evidence or not evidence.strip():
        raise ValueError("a suggestion needs --evidence: what actually happened, "
                         "in this task, that cost time or produced a wrong result")
    for x in state.setdefault("suggestions", []):
        if x["status"] == "open" and _norm(x["title"]) == _norm(title):
            x["seconded"] = x.get("seconded", 0) + 1
            x.setdefault("evidence", [])
            if evidence not in x["evidence"]:
                x["evidence"].append("%s (%s)" % (evidence, source or "?"))
            x["updated"] = now()
            log(state, "seconded suggestion %s (%dx): %s" % (x["id"], x["seconded"] + 1, x["title"]))
            return x

    sug = {
        "id": "s%d" % (int(time.time() * 1000) % 100000000),
        "title": title,
        "body": body,
        "evidence": ["%s (%s)" % (evidence, source or "?")],
        "project": project,          # None == about the orchestrator tooling itself
        "source": source,
        "status": "open",
        "seconded": 0,
        "promoted_to": None,
        "created": now(),
        "updated": now(),
    }
    state["suggestions"].append(sug)
    log(state, "suggested: %s" % title)
    return sug


def open_suggestions(state):
    return [x for x in state.get("suggestions", []) if x["status"] == "open"]


def find_suggestion(state, sid):
    for x in state.get("suggestions", []):
        if x["id"] == sid:
            return x
    raise KeyError("no suggestion %r" % sid)


def promote_suggestion(state, sid, key, project=None, trivial=False):
    """Turn a suggestion into a real queued task. Human-initiated only."""
    sug = find_suggestion(state, sid)
    if sug["status"] != "open":
        raise ValueError("suggestion %s is already %s" % (sid, sug["status"]))
    project = project or sug.get("project")
    body = sug.get("body") or ""
    ev = "\n".join("- %s" % e for e in sug.get("evidence", []))
    t = add_task(state, key, sug["title"], source="suggestion", project=project,
                 done_when=body or None)
    t["trivial"] = trivial
    t["note"] = ("promoted from %s\nevidence:\n%s" % (sid, ev)).strip()
    sug["status"] = "promoted"
    sug["promoted_to"] = t["id"]
    sug["updated"] = now()
    log(state, "promoted %s -> %s/%s" % (sid, project, key))
    return t


def dismiss_suggestion(state, sid, reason=None):
    sug = find_suggestion(state, sid)
    if sug["status"] != "open":
        raise ValueError("suggestion %s is already %s" % (sid, sug["status"]))
    sug["status"] = "dismissed"
    sug["reason"] = reason
    sug["updated"] = now()
    log(state, "dismissed %s%s" % (sid, (": " + reason) if reason else ""))
    return sug


def delete_task(state, tid, force=False):
    """Remove a task and its approval cards.

    Refused while the task still owns real artifacts — a herdr workspace, a
    worktree or an open PR — because deleting the record does not delete those,
    it just means nothing is tracking them any more. An archived task has
    already been through cleanup, so its record is safe to drop.
    """
    t = find(state, tid)
    if not t:
        raise KeyError("no task %r" % tid)
    if not force and t["phase"] not in TERMINAL_PHASES:
        held = [lbl for lbl, v in (("workspace", t.get("workspace")),
                                   ("worktree", t.get("worktree")),
                                   ("PR", t.get("pr_url"))) if v]
        if held:
            raise ValueError(
                "%s still owns %s — deleting the task would orphan %s. Clean up first "
                "(`orch cleanup-check %s`), or pass --force to drop the record anyway."
                % (t["key"], ", ".join(held), "them" if len(held) > 1 else "it", t["key"]))
    state["tasks"] = [x for x in state["tasks"] if x["id"] != t["id"]]
    state["approvals"] = [a for a in state.get("approvals", []) if a["task"] != t["id"]]
    log(state, "deleted %s/%s" % (t.get("project"), t["key"]))
    return t


def deletable(t):
    """Whether delete_task would accept this task without --force."""
    if t["phase"] in TERMINAL_PHASES:
        return True
    return not (t.get("workspace") or t.get("worktree") or t.get("pr_url"))


def reorder(state, ordered_ids):
    """Apply a new queue order. Ids not mentioned keep their relative position after."""
    pos = {tid: i for i, tid in enumerate(ordered_ids)}
    for t in state["tasks"]:
        if t["id"] in pos:
            t["order"] = pos[t["id"]]
    log(state, "queue reordered")


def add_approval(state, tid, kind, title, body="", plan_path=None):
    if kind not in APPROVAL_KINDS:
        raise ValueError("unknown approval kind %r" % kind)
    t = find(state, tid)
    if not t:
        raise KeyError("no task %r" % tid)
    a = {
        "id": "a%d" % (int(time.time() * 1000) % 100000000),
        "task": t["id"],
        "key": t["key"],
        "project": t.get("project"),
        "kind": kind,
        "title": title,
        "body": body,
        "status": "pending",
        "plan_path": plan_path,   # a document Plannotator can open for review
        "review_started": None,   # set while a Plannotator gate is open
        "created": now(),
        "resolved": None,
        "decision_note": None,
    }
    state.setdefault("approvals", []).append(a)
    log(state, "%s awaiting %s approval" % (t["key"], kind))
    return a


def resolve_approval(state, aid, decision, note=None):
    for a in state.get("approvals", []):
        if a["id"] == aid and a["status"] == "pending":
            a["status"] = decision
            a["resolved"] = now()
            a["decision_note"] = note
            log(state, "%s %s %s" % (a["key"], a["kind"], decision))
            # The worker that raised this stopped and ended its turn, and no
            # agent is present at the moment a human clicks a button — so this
            # is the one handoff nothing else can record. Skipping it is how a
            # board full of cleared cards sits behind an idle worker forever.
            t = find(state, a["task"])
            if t and t["phase"] in ("awaiting-plan", "awaiting-decision"):
                add_handoff(state, t["id"], t["phase"],
                            "%s %s — relay the decision to the worker"
                            % (a["kind"], decision))
            return a
    raise KeyError("no pending approval %r" % aid)


def pending_approvals(state, tid=None):
    out = [a for a in state.get("approvals", []) if a["status"] == "pending"]
    if tid:
        t = find(state, tid)
        out = [a for a in out if t and a["task"] == t["id"]]
    return out


def task_repo(state, t):
    """Main repo root for a task, from its registered project."""
    p = find_project(state, t.get("project"))
    return p["path"] if p else None


def set_orchestrator(state, name=None, pane=None):
    """Record who to wake when a task needs the orchestrator.

    Written at preflight and kept on the board rather than in the
    orchestrator's context, because the context is the thing expected to be
    cleared. A worker that finishes an hour after a `/clear` still has to be
    able to find out who to ping.
    """
    rec = dict(state.get("orchestrator") or {})
    if name:
        if not AGENT_RE.match(name):
            raise ValueError("agent name %r must match %s (herdr's own rule)"
                             % (name, AGENT_RE.pattern))
        rec["agent"] = name
    if pane:
        rec["pane"] = pane
    if not rec.get("agent") and not rec.get("pane"):
        raise ValueError("pass --agent, --pane, or both")
    rec["updated"] = now()
    state["orchestrator"] = rec
    log(state, "orchestrator is %s" % (rec.get("agent") or rec.get("pane")))
    return rec


def orchestrator_target(state):
    """The `herdr agent prompt` target for the orchestrator, or None.

    Prefers the agent name: herdr names follow the pane occupant and are
    cleared when it exits, so a stale name fails loudly rather than landing a
    prompt in whatever now occupies that pane. A pane id is the fallback for an
    orchestrator that never named itself.
    """
    rec = state.get("orchestrator") or {}
    return rec.get("agent") or rec.get("pane")


def add_handoff(state, tid, phase, reason):
    """Record that a task is waiting on the orchestrator.

    One row per task, updated in place. A worker that bounces
    needs-review -> resolving -> needs-review should read as one thing needing
    attention, not three, and the newest reason is the only one still true.

    This ledger is what makes the wake-up survive a failure. The prompt that
    goes with it is best-effort — the orchestrator may be mid-turn, restarted
    into a new pane, or simply gone — so the durable record is the board, and
    `orch handoffs` is what a resuming orchestrator reads to catch up.
    """
    t = find(state, tid)
    if not t:
        raise KeyError("no task %r" % tid)
    for h in state.setdefault("handoffs", []):
        if h["task"] == t["id"] and h["status"] == "pending":
            h.update(phase=phase, reason=reason, updated=now())
            h["notified"] = None
            return h
    h = {
        "id": "h%d" % (int(time.time() * 1000) % 100000000),
        "task": t["id"],
        "key": t["key"],
        "project": t.get("project"),
        "phase": phase,
        "reason": reason,
        "status": "pending",
        "notified": None,     # how the wake-up prompt went, for diagnosis
        "created": now(),
        "updated": now(),
    }
    state["handoffs"].append(h)
    return h


def pending_handoffs(state, tid=None):
    """What the orchestrator owes attention to, oldest first."""
    out = [h for h in state.get("handoffs", []) if h["status"] == "pending"]
    if tid:
        t = find(state, tid)
        out = [h for h in out if t and h["task"] == t["id"]]
    return sorted(out, key=lambda h: h["created"])


def clear_handoff(state, tid):
    """Mark a task's handoff as picked up. Silent when there is none."""
    t = find(state, tid)
    if not t:
        raise KeyError("no task %r" % tid)
    done = [h for h in state.get("handoffs", [])
            if h["task"] == t["id"] and h["status"] == "pending"]
    # Dropped rather than marked: the ledger is a work queue, not an audit
    # trail, and `log` already carries the phase history that produced it.
    state["handoffs"] = [h for h in state.get("handoffs", []) if h not in done]
    return done


def record_notify(state, hid, outcome):
    """Store how the wake-up prompt went, so a dead orchestrator is visible."""
    for h in state.get("handoffs", []):
        if h["id"] == hid:
            h["notified"] = outcome
            h["updated"] = now()
            return h
    return None


def resumable(state):
    """Tasks stopped on a human decision that has since been made.

    A worker that submits a plan and stops is idle: it will not spontaneously
    poll for the answer. Something has to wake it, so the orchestrator watches
    this list and prompts the worker with the decision.
    """
    out = []
    for t in state["tasks"]:
        if t["phase"] not in ("awaiting-plan", "awaiting-decision"):
            continue
        mine = [a for a in state.get("approvals", []) if a["task"] == t["id"]]
        if not mine or any(a["status"] == "pending" for a in mine):
            continue
        latest = sorted(mine, key=lambda a: a.get("resolved") or "")[-1]
        out.append((t, latest))
    return out


def render_board(pdir, state):
    """Regenerate board.md. Human/git-readable view; never parsed back."""
    L = []
    L.append("# Orchestrator board")
    L.append("Updated: %s" % state.get("updated", ""))
    L.append("Max active: %s  (active now: %d)" % (state.get("max_active", 3), len(active_tasks(state))))
    L.append("")
    L.append("_Generated from state.json — edit via the `orch` CLI or the webapp, not by hand._")
    L.append("")

    L.append("## Projects")
    if state.get("projects"):
        L.append("| Name | Path | Open tasks |")
        L.append("|---|---|---|")
        for p in sorted(state["projects"], key=lambda p: p["name"]):
            n = len([t for t in state["tasks"]
                     if t.get("project") == p["name"] and t["phase"] not in TERMINAL_PHASES])
            L.append("| %s | `%s` | %d |" % (p["name"], p["path"], n))
    else:
        L.append("_none registered — `orch project add <name> --path <repo>`_")
    L.append("")

    pend = pending_approvals(state)
    L.append("## Awaiting you")
    if pend:
        L.append("| Project | Key | Kind | What | Since |")
        L.append("|---|---|---|---|---|")
        for a in pend:
            L.append("| %s | %s | %s | %s | %s |" % (
                a.get("project") or "-", a["key"], a["kind"], a["title"], a["created"][11:16]))
    else:
        L.append("_nothing pending_")
    L.append("")

    # Deliberately after "Awaiting you" and before the task tables: an
    # unclaimed handoff whose prompt failed means the orchestrator is not
    # listening, and the pipeline is stopped in a way no phase column shows.
    hand = pending_handoffs(state)
    if hand:
        L.append("## Awaiting the orchestrator")
        L.append("| Project | Key | Phase | Why | Woken | Since |")
        L.append("|---|---|---|---|---|---|")
        for h in hand:
            L.append("| %s | %s | %s | %s | %s | %s |" % (
                h.get("project") or "-", h["key"], h["phase"], h["reason"],
                h.get("notified") or "not yet", h["created"][11:16]))
        L.append("")

    for col, phases in SECTIONS:
        rows = [t for t in state["tasks"] if t["phase"] in phases]
        rows.sort(key=sort_key)
        L.append("## %s" % col)
        if not rows:
            L.append("_empty_")
            L.append("")
            continue
        L.append("| Project | Key | Title | Phase | Worker | Branch | PR |")
        L.append("|---|---|---|---|---|---|---|")
        for t in rows:
            pr = "[%s](%s)" % (t.get("pr_state") or "open", t["pr_url"]) if t.get("pr_url") else "-"
            flag = " _(trivial)_" if t.get("trivial") else ""
            L.append("| %s | %s | %s%s | %s | %s | %s | %s |" % (
                t.get("project") or "-", t["key"], t["title"][:44], flag, t["phase"],
                t.get("worker") or "-", t.get("branch") or "-", pr))
        L.append("")

    sugs = sorted(open_suggestions(state), key=lambda x: -x.get("seconded", 0))
    L.append("## Suggestions")
    if sugs:
        L.append("| id | Hit | Project | Suggestion |")
        L.append("|---|---|---|---|")
        for x in sugs:
            L.append("| %s | %dx | %s | %s |" % (
                x["id"], x.get("seconded", 0) + 1, x.get("project") or "tooling", x["title"]))
    else:
        L.append("_none open_")
    L.append("")

    L.append("## Log")
    for line in state.get("log", [])[-40:]:
        L.append("- %s" % line)
    L.append("")

    with open(os.path.join(pdir, "board.md"), "w") as fh:
        fh.write("\n".join(L))
