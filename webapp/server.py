#!/usr/bin/env python3
"""Local kanban for the herdr orchestrator.

Reads and writes the same state.json the `orch` CLI does, through the same
locking, so the orchestrator and this app can both be live without clobbering
each other. Stdlib only — no install step, no build step.

  python3 webapp/server.py [--slug SLUG | --repo PATH] [--port 8787]
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "lib"))
import notify  # noqa: E402
import store  # noqa: E402

STATIC = os.path.join(os.path.dirname(os.path.realpath(__file__)), "static")
PDIR = None          # the single board
HAVE_PLANNOTATOR = False

TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
         ".css": "text/css; charset=utf-8"}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass  # keep the terminal usable

    def _send(self, code, body, ctype="application/json"):
        raw = body if isinstance(body, bytes) else body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj), "application/json")

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or "{}")

    def do_GET(self):
        path = urlparse(self.path).path

        if path == "/api/state":
            st = store.read(PDIR)
            st["_sections"] = store.SECTIONS
            st["_collapsed_default"] = store.COLLAPSED_BY_DEFAULT
            st["_yours_phases"] = store.YOURS_PHASES
            st["_active"] = len(store.active_tasks(st))
            for t in st["tasks"]:
                t["_deletable"] = store.deletable(t)
                t["_blocking"] = len(store.blocking_open(st, t["id"]))
            st["_plannotator"] = HAVE_PLANNOTATOR
            return self._json(st)

        rel = "index.html" if path == "/" else path.lstrip("/")
        full = os.path.normpath(os.path.join(STATIC, rel))
        # Refuse anything that escapes the static dir.
        if not full.startswith(STATIC) or not os.path.isfile(full):
            return self._send(404, "not found", "text/plain")
        with open(full, "rb") as fh:
            self._send(200, fh.read(), TYPES.get(os.path.splitext(full)[1], "application/octet-stream"))

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            body = self._body()
            handoff = None
            with store.transaction(PDIR) as st:
                if path == "/api/task":
                    store.add_task(st, body["key"], body["title"],
                                   body.get("source", "manual"), body.get("url"),
                                   body.get("done_when"), body.get("kind", "claude"),
                                   project=body.get("project"))
                elif path == "/api/plan/review":
                    aid = body["approval"]
                    a = next((x for x in st.get("approvals", []) if x["id"] == aid), None)
                    if not a or a["status"] != "pending":
                        raise ValueError("no pending approval %r" % aid)
                    if a.get("review_started"):
                        raise ValueError("a review is already open for %s" % a["key"])
                    plan = a.get("plan_path")
                    if not plan or not os.path.isfile(plan):
                        raise ValueError("plan file is missing: %s" % plan)
                    a["review_started"] = store.now()
                    threading.Thread(target=run_plannotator_gate,
                                     args=(aid, plan), daemon=True).start()
                elif path == "/api/suggestion/promote":
                    store.promote_suggestion(st, body["suggestion"], body["key"],
                                             body.get("project"))
                elif path == "/api/suggestion/dismiss":
                    store.dismiss_suggestion(st, body["suggestion"], body.get("reason"))
                elif path == "/api/task/delete":
                    store.delete_task(st, body["task"], force=body.get("force", False))
                elif path == "/api/reorder":
                    store.reorder(st, body["ids"])
                elif path == "/api/phase":
                    # Guard on the transition, not just on a pending handoff:
                    # dragging a card that already had one waiting must not
                    # re-prompt the orchestrator about it.
                    cur = store.find(st, body["task"])
                    old = cur["phase"] if cur else None
                    t = store.set_phase(st, body["task"], body["phase"], body.get("note"))
                    if store.handoff_reason(old, t["phase"]):
                        handoff = _handoff_for(st, t["id"])
                elif path == "/api/resolve":
                    # Answering a card is the handoff nothing else can record:
                    # the worker that raised it stopped and ended its turn, and
                    # no agent is present at the moment the button is clicked.
                    a = store.resolve_approval(st, body["approval"], body["decision"],
                                               body.get("note"))
                    handoff = _handoff_for(st, a["task"])
                elif path == "/api/project":
                    store.add_project(st, body["name"], body["path"])
                elif path == "/api/project/update":
                    store.update_project(st, body["name"], body.get("new_name"),
                                         body.get("path"))
                elif path == "/api/project/remove":
                    store.remove_project(st, body["name"])
                elif path == "/api/config":
                    st["max_active"] = int(body["max_active"])
                    store.log(st, "max_active set to %s" % st["max_active"])
                else:
                    return self._json({"error": "unknown endpoint"}, 404)
            # After the transaction, and on its own thread: `notify.wake` takes
            # the board lock to record the outcome, so calling it inside the
            # `with` would deadlock this process against itself, and a herdr
            # round-trip has no business delaying the HTTP response.
            _wake_later(handoff)
            return self._json({"ok": True})
        except Exception as e:
            return self._json({"error": str(e)}, 400)


def _handoff_for(st, tid):
    """A snapshot of the task's pending handoff, safe to use after the lock."""
    rows = store.pending_handoffs(st, tid)
    return dict(rows[0]) if rows else None


def _wake_later(handoff):
    """Send a handoff's wake-up prompt off the request/poll thread.

    Always on a thread, never inline: `notify.wake` reopens the board to record
    the outcome, and this process would block on its own flock.
    """
    if handoff:
        threading.Thread(target=notify.wake, args=(PDIR, handoff), daemon=True).start()


def run_plannotator_gate(approval_id, plan_path):
    """Open a Plannotator review gate and settle the approval from its verdict.

    Plannotator blocks until the reviewer decides and publishes the same JSON
    to --result-file atomically, so this runs on its own thread and watches the
    file rather than holding the board lock across a human-length review.

      approved  -> approval approved, any feedback kept as the note
      annotated -> approval rejected, feedback IS the note; that text is what
                   tells the worker what to change, so it must survive verbatim
      dismissed -> left pending; the reviewer closed without deciding
    """
    out = os.path.join(tempfile.mkdtemp(prefix="orch-gate-"), "decision.json")
    try:
        proc = subprocess.run(
            ["plannotator", "annotate", plan_path, "--gate", "--json", "--result-file", out],
            capture_output=True, text=True, timeout=60 * 60 * 4)
        raw = ""
        if os.path.exists(out):
            with open(out) as fh:
                raw = fh.read()
        raw = raw or proc.stdout
        verdict = json.loads(raw.strip().splitlines()[-1]) if raw.strip() else {}
    except Exception as e:
        verdict = {"decision": "error", "feedback": str(e)}

    decision = verdict.get("decision")
    feedback = (verdict.get("feedback") or "").strip() or None

    handoff = None
    with store.transaction(PDIR) as st:
        a = next((x for x in st.get("approvals", []) if x["id"] == approval_id), None)
        if not a:
            return
        a["review_started"] = None
        if a["status"] != "pending":
            return          # settled elsewhere while the gate was open
        if decision in ("approved", "annotated"):
            store.resolve_approval(st, approval_id,
                                   "approved" if decision == "approved" else "rejected",
                                   feedback)
            handoff = _handoff_for(st, a["task"])
        else:
            # dismissed, or the gate failed to start: leave it for the human.
            store.log(st, "%s plan review closed without a decision (%s)"
                      % (a["key"], decision or "no result"))
    # A Plannotator verdict is the human deciding without touching the board,
    # so without this the annotations land in state.json and the worker that
    # needs them never hears.
    _wake_later(handoff)


def poll_prs(interval):
    """Watch `pr-open` tasks and react when GitHub says the PR closed.

    The webapp only *detects* the merge and moves the task to `merged`. It
    deliberately does not clean up: removing worktrees and deleting branches is
    the orchestrator's job, guarded by `orch cleanup-check`. A web request
    thread has no business tearing down a git worktree.
    """
    if not shutil.which("gh"):
        print("gh not found — PR merge polling disabled", flush=True)
        return
    print("polling PR state every %ds" % interval, flush=True)

    while True:
        time.sleep(interval)
        try:
            st = store.read(PDIR)
        except Exception:
            continue

        watch = [(t["id"], t["pr_url"]) for t in st.get("tasks", [])
                 if t.get("phase") == "pr-open" and t.get("pr_url")]
        if not watch:
            continue

        # Every network call happens outside the lock. Holding flock across a
        # `gh` round-trip would stall the orchestrator and every worker.
        found = {}
        for tid, url in watch:
            try:
                r = subprocess.run(["gh", "pr", "view", url, "--json", "state,mergedAt"],
                                   capture_output=True, text=True, timeout=20)
                if r.returncode != 0:
                    continue
                state = (json.loads(r.stdout).get("state") or "").upper()
                if state in ("MERGED", "CLOSED"):
                    found[tid] = state
            except Exception:
                continue  # transient network/auth trouble; try again next tick

        if not found:
            continue

        handoffs = []
        with store.transaction(PDIR) as st2:
            for tid, state in found.items():
                t = store.find(st2, tid)
                if not t or t["phase"] != "pr-open":
                    continue  # something moved it while we were off doing IO
                if state == "MERGED":
                    t["pr_state"] = "merged"
                    store.set_phase(st2, tid, "merged", "PR merged; awaiting cleanup")
                    handoffs.append(_handoff_for(st2, tid))
                else:
                    t["pr_state"] = "closed"
                    store.add_approval(
                        st2, tid, "question", "PR closed without merging",
                        "%s was closed but never merged. Decide whether to reopen it, "
                        "or what should happen to the branch and worktree." % t.get("pr_url"))
        for h in handoffs:
            _wake_later(h)


def main():
    global PDIR
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=8787)
    p.add_argument("--poll-seconds", type=int, default=60,
                   help="how often to check PR state via gh; 0 disables")
    args = p.parse_args()

    global HAVE_PLANNOTATOR
    PDIR = store.board_dir()
    os.makedirs(PDIR, exist_ok=True)
    HAVE_PLANNOTATOR = bool(shutil.which("plannotator"))

    # Bind loopback only. This exposes local repo state and drives real agents;
    # it has no auth and must not be reachable from the network.
    if args.poll_seconds > 0:
        threading.Thread(target=poll_prs, args=(args.poll_seconds,), daemon=True).start()

    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print("orchestrator board: http://127.0.0.1:%d" % args.port, flush=True)
    print("state: %s" % os.path.join(PDIR, "state.json"), flush=True)
    projs = store.read(PDIR).get("projects", [])
    print("plannotator: %s" % ("available" if HAVE_PLANNOTATOR
                               else "not found — plan review falls back to the inline buttons"),
          flush=True)
    print("projects: %s" % (", ".join(p["name"] for p in projs) or "none registered yet"), flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")


if __name__ == "__main__":
    main()
