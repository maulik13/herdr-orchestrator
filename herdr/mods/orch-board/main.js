// The orchestrator board as a Herdr pane.
//
// Reads state.json directly and redraws when the board folder changes. It never
// writes state.json: answering a card runs `orch resolve`, starting the PR
// poller runs `orch poll`, so every write goes through `orch`'s file lock like
// the workers', the orchestrator's and the webapp's do.
//
// Open it beside the orchestrator:
//   herdr-app do pane.split --direction right --mod orch-board

import { View, div } from "gpui-kit";
import { v_flex, h_flex, TextareaState, Textarea } from "gpui-base";
import { exec, hc, home, readTextFile, watchFolder, theme, panes, terminals, run, notify, props } from "herdr";

// Mirrors lib/store.py. The webapp sends these alongside the state; reading
// state.json directly, the mod has to carry its own copy.
const ACTIVE = ["planning", "awaiting-plan", "implementing", "needs-review", "reviewing", "resolving", "awaiting-decision"];
const SECTIONS = [
  ["in progress", ["implementing", "planning", "awaiting-plan"]],
  ["review", ["needs-review", "reviewing", "resolving", "awaiting-decision", "pr-open", "merged"]],
  ["queued", ["queued"]],
  ["parked", ["parked", "blocked"]],
  ["done", ["archived"]],
];
const SHUT_BY_DEFAULT = ["parked", "done"];
const YOURS = new Set(["awaiting-plan", "awaiting-decision", "pr-open"]);
const BLOCKING = new Set(["P1", "P2"]);
const DONE_LIMIT = 15;
const POLLER_NAME = "orch-poll";   // unique among live terminals, so a second start is refused by hc
const POLLER_LABEL = "orch";       // orch=poll marks it for the pane to find
const AGENT_POLL_MS = 10000;
const FALLBACK_POLL_MS = 15000;

const plural = (n, w) => `${n} ${w}${n === 1 ? "" : "s"}`;
const firstLine = (s) => String(s ?? "").split("\n")[0];

function load(key, fallback) {
  try { const v = localStorage.getItem(key); return v == null ? fallback : JSON.parse(v); } catch { return fallback; }
}
function save(key, value) {
  try { localStorage.setItem(key, JSON.stringify(value)); } catch {}
}

// `herdr agent list` output, tolerant of shape: the CLI's JSON envelope has
// changed between releases, and a status we cannot read is better shown as
// nothing than as a wrong word.
function agentMap(j) {
  const list = j?.result?.agents ?? j?.agents ?? j?.result ?? j;
  const map = new Map();
  if (!Array.isArray(list)) return map;
  for (const a of list) {
    const name = a?.name ?? a?.agent;
    const status = a?.status ?? a?.state;
    if (name && status) map.set(String(name), String(status));
  }
  return map;
}

export default class OrchBoard extends View {
  init(_props, cx) {
    this.dir = props()?.home ?? null;
    this.state = null;
    this.updated = null;
    this.error = null;
    this.herdr = null;          // Map worker name -> status, when `herdr agent list` answered
    this.herdrError = null;
    this.seenApprovals = null;  // null until the first load, so opening the pane doesn't toast the backlog
    this.seenFailed = new Set();
    this.shut = new Set(load("shut", SHUT_BY_DEFAULT));
    this.filter = load("filter", null);
    this.open = new Set();      // approval ids with their body shown
    // Per pending approval: its feedback box and the state of a decision being sent.
    // Kept outside render: a board write from any agent redraws the pane, and a
    // half-written rejection has to survive that.
    this.notes = new Map();
    // Notes whose card was settled elsewhere (the CLI, the web board, Plannotator)
    // while they were being written. Never sent, so shown to copy rather than dropped.
    this.orphans = new Map();
    this.pollerStarting = false;

    cx.spawn(async (cx) => {
      if (!this.dir) {
        // Same default as `orch`, honouring the override if the login env sets it.
        const r = await exec("sh", ["-c", 'printf %s "${HERDR_ORCHESTRATOR_HOME:-$HOME/.claude/orchestrator}"']).catch(() => null);
        this.dir = r?.stdout?.trim() || null;
      }
      if (!this.dir) { this.error = "Couldn't work out where the board lives."; cx.notify(); return; }
      await this.reload(cx);
      await this.pollAgents(cx);
      for (;;) {
        try { await watchFolder(this.dir); } catch { return; } // the timer below keeps it fresh
        await this.reload(cx);
      }
    });
    cx.timer.every(FALLBACK_POLL_MS, (cx) => { cx.spawn(async (cx) => this.reload(cx)); });
    cx.timer.every(AGENT_POLL_MS, (cx) => { cx.spawn(async (cx) => this.pollAgents(cx)); });
  }

  async reload(cx) {
    if (!this.dir) return;
    let text;
    try { text = await readTextFile(`${this.dir}/state.json`); } catch (e) { text = null; }
    if (text == null) {
      const msg = `No board at ${this.dir}/state.json`;
      if (this.error !== msg) { this.error = msg; cx.notify(); }
      return;
    }
    let st;
    try { st = JSON.parse(text); } catch { return; } // keep the last good board; the next change retries
    if (this.error) { this.error = null; }
    if (st.updated === this.updated && this.state) { cx.notify(); return; }
    this.updated = st.updated;
    this.state = st;
    this.syncNotes(st);
    this.announce(st);
    cx.notify();
  }

  // Runs in a task (reload), never in render: TextareaState.new needs a live host call.
  syncNotes(st) {
    const byId = new Map((st.approvals ?? []).map((a) => [a.id, a]));
    for (const [id, n] of this.notes) {
      if (byId.get(id)?.status === "pending" || n.busy) continue;
      const text = n.input.value().trim();
      const a = byId.get(id);
      if (text && !n.sent) this.orphans.set(id, { key: a?.key ?? "?", title: a?.title ?? "", status: a?.status ?? "removed", input: TextareaState.new({ value: text, rows: 3 }) });
      n.input.release();
      this.notes.delete(id);
    }
    for (const a of st.approvals ?? []) {
      if (a.status !== "pending" || this.notes.has(a.id)) continue;
      const input = TextareaState.new({ placeholder: "Feedback for the worker — optional to approve, required to reject" });
      input.set_auto_grow(2, 8);
      const n = { input, busy: false, error: null, hint: false, sent: false };
      input.on("change", (_e, cx) => { if (n.hint || n.error) { n.hint = false; n.error = null; cx.notify(); } });
      this.notes.set(a.id, n);
    }
  }

  // `orch resolve`, exactly as a person would type it. A rejection without a
  // note is refused here: that text is the only thing telling the worker what
  // to change. The note stays in the box until orch confirms.
  decide(a, decision, cx) {
    const n = this.notes.get(a.id);
    if (!n || n.busy) return;
    const note = n.input.value().trim();
    if (decision === "rejected" && !note) { n.hint = true; n.input.focus(); cx.notify(); return; }
    n.busy = true;
    n.error = null;
    cx.notify();
    cx.spawn(async (cx) => {
      const args = ["resolve", a.id, "--decision", decision];
      if (note) args.push("--note", note);
      let r;
      try { r = await exec("orch", args, { timeout: 30000 }); }
      catch (e) { r = { code: 1, stdout: "", stderr: String(e) }; }
      n.busy = false;
      if (r.code === 0) n.sent = true;
      else n.error = firstLine((r.stderr || r.stdout || "orch resolve failed").trim());
      await this.reload(cx);
      cx.notify();
    });
  }

  poller() {
    return terminals().find((x) => x?.labels?.[POLLER_LABEL] === "poll" && x.state === "live") ?? null;
  }

  // `orch poll` in a background core terminal: it keeps running when Herdr
  // closes, and hc refuses a second one by name. PATH is the one exec sees (the
  // login environment), so `orch`, `gh` and `herdr` resolve as in your shell.
  startPoller(cx) {
    if (this.pollerStarting) return;
    this.pollerStarting = true;
    cx.notify();
    cx.spawn(async (cx) => {
      try {
        const env = await exec("sh", ["-c", 'printf "%s\\n%s" "$PATH" "$(command -v orch)"']);
        const [path, orch] = env.stdout.split("\n");
        if (!orch) throw new Error("orch isn't on PATH. Run install.sh in herdr-orchestrator.");
        const r = await hc(["run", "--bg", "--no-parent", "--json", "--name", POLLER_NAME, "--label", `${POLLER_LABEL}=poll`,
          "--cwd", home(), "--", "env", `PATH=${path}`, orch, "poll"]);
        if (r.code !== 0) throw new Error(firstLine((r.stderr || r.stdout).trim()) || "hc run failed");
      } catch (e) {
        notify({ title: "Couldn't start the PR poller", body: String(e?.message ?? e), level: "error" });
      } finally {
        this.pollerStarting = false;
        cx.notify();
      }
    });
  }

  // Only the things a person must act on: a new approval, or a handoff that
  // never reached the orchestrator (the pipeline is stalled until someone looks).
  announce(st) {
    const pending = (st.approvals ?? []).filter((a) => a.status === "pending");
    const first = this.seenApprovals == null;
    if (first) this.seenApprovals = new Set();
    for (const a of pending) {
      if (this.seenApprovals.has(a.id)) continue;
      this.seenApprovals.add(a.id);
      if (!first) notify({ title: `${a.key} needs you`, body: `${a.kind}: ${firstLine(a.title)}`, level: "warning", timeout: 10000 });
    }
    for (const h of st.handoffs ?? []) {
      if (h.status !== "pending" || !String(h.notified ?? "").startsWith("failed")) continue;
      const k = `${h.id}:${h.notified}`;
      if (this.seenFailed.has(k)) continue;
      this.seenFailed.add(k);
      if (!first) notify({ title: `${h.key}: handoff didn't reach the orchestrator`, body: firstLine(h.reason), level: "error", timeout: 10000 });
    }
  }

  async pollAgents(cx) {
    const live = (this.state?.tasks ?? []).some((t) => t.worker && (ACTIVE.includes(t.phase) || t.phase === "pr-open"));
    if (!live) return;
    let r;
    try { r = await exec("herdr", ["agent", "list"], { timeout: 5000 }); }
    catch (e) { r = { code: 1, stdout: "", stderr: String(e) }; }
    let j = null;
    try { j = JSON.parse(r.stdout || r.stderr); } catch {}
    const err = j?.error?.message ?? (r.code !== 0 ? (r.stderr || r.stdout || "herdr agent list failed") : null);
    const next = err ? null : agentMap(j);
    const nextErr = err ? firstLine(err) : null;
    const same = nextErr === this.herdrError && JSON.stringify([...(next ?? [])]) === JSON.stringify([...(this.herdr ?? [])]);
    this.herdr = next;
    this.herdrError = nextErr;
    if (!same) cx.notify();
  }

  // A Herdr-app pane standing in the task's worktree, if there is one: that is
  // what lets a card jump to its worker. Workers started by the standalone
  // `herdr` server have no pane here, and their cards simply don't link.
  paneFor(t, all) {
    if (!t.worktree) return null;
    return all.find((p) => p.terminal && p.cwd && (p.cwd === t.worktree || p.cwd.startsWith(`${t.worktree}/`))) ?? null;
  }

  workerStatus(t, pane) {
    if (pane?.status) return pane.status;
    if (!t.worker) return null;
    const s = this.herdr?.get(t.worker);
    if (s) return s;
    // Absent from a list that answered: the agent exited or was replaced. Never read as success.
    if (this.herdr && ACTIVE.includes(t.phase)) return "gone";
    return null;
  }

  render() {
    const t = theme();
    const st = this.state;
    const root = v_flex().id("orch-board").size_full().min_h(0).bg(t.background).text_color(t.foreground).text_size(13);
    if (!st) {
      return root.child(div().p(16).text_color(this.error ? t.blocked : t.muted).child(this.error ?? "Loading the board…"));
    }

    const inFilter = (x) => !this.filter || x.project === this.filter;
    const tasks = (st.tasks ?? []).filter(inFilter);
    const allPanes = panes();
    const pending = (st.approvals ?? []).filter((a) => a.status === "pending" && inFilter(a));
    const handoffs = (st.handoffs ?? []).filter((h) => h.status === "pending" && inFilter(h));
    const active = (st.tasks ?? []).filter((x) => ACTIVE.includes(x.phase)).length;
    const blocking = new Map();
    for (const f of st.findings ?? []) {
      if ((f.status === "open" || f.status === "disputed") && BLOCKING.has(f.severity)) blocking.set(f.task, (blocking.get(f.task) ?? 0) + 1);
    }
    const ctx = { t, st, allPanes, blocking, pending };

    const body = v_flex().id("orch-body").flex_1().min_h(0).overflow_y_scrollbar().p(10).gap(14);
    if (this.error) body.child(div().text_color(t.blocked).child(this.error));
    if (this.herdrError) body.child(this.banner(t, `herdr: ${this.herdrError}`, "Worker status is unavailable until `herdr agent list` answers."));

    if (this.orphans.size) {
      body.child(v_flex().gap(6).children([...this.orphans].map(([id, o]) => this.orphan(id, o, t))));
    }
    if (pending.length) {
      body.child(this.heading(t, "Awaiting you", plural(pending.length, "card"), t.blocked));
      body.child(v_flex().gap(6).children(pending.map((a) => this.approval(a, ctx))));
    }
    if (handoffs.length) {
      body.child(this.heading(t, "Awaiting the orchestrator", plural(handoffs.length, "handoff"), t.done));
      body.child(v_flex().gap(4).children(handoffs.map((h) => this.handoff(h, ctx))));
    }
    for (const [name, phases] of SECTIONS) {
      const rows = tasks.filter((x) => phases.includes(x.phase))
        .sort((a, b) => phases.indexOf(a.phase) - phases.indexOf(b.phase) || (a.order ?? 0) - (b.order ?? 0));
      if (!rows.length && (name === "parked" || name === "done")) continue;
      body.child(this.section(name, phases, rows, ctx));
    }
    const suggestions = (st.suggestions ?? []).filter((s) => s.status === "open" && inFilter(s)).length;
    if (suggestions) body.child(div().text_color(t.dim).text_size(12).child(`${plural(suggestions, "open suggestion")} · orch suggestions`));

    return root.child(this.header(t, st, active, pending.length)).child(this.projects(t, st)).child(body);
  }

  header(t, st, active, pendingCount) {
    const max = st.max_active ?? 3;
    const d = st.updated ? new Date(st.updated) : null;
    const time = d && !isNaN(d) ? `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}` : "";
    return h_flex().id("orch-header").flex_none().h(36).px(12).gap(8).items_center().border_b_1().border_color(t.border).bg(t.chrome)
      .child(div().font_semibold().child("Orchestrator"))
      .child(this.pill(t, `${active}/${max} active`, active >= max ? t.done : t.muted))
      .when(pendingCount > 0, (el) => el.child(this.pill(t, `${pendingCount} need you`, t.blocked)))
      .child(div().flex_1())
      .child(this.pollerPill(t))
      .child(div().text_size(11).text_color(t.dim).child(time ? `updated ${time}` : ""));
  }

  // Projects with anything not archived, as a one-click filter.
  projects(t, st) {
    const live = new Set((st.tasks ?? []).filter((x) => x.phase !== "archived").map((x) => x.project));
    const names = [...live].filter(Boolean).sort();
    if (names.length < 2 && !this.filter) return div();
    const chip = (label, value) => {
      const on = this.filter === value;
      return div().id(`proj-${label}`).px(8).py(2).rounded(10).text_size(11).cursor_pointer()
        .bg(on ? t.selected : t.surface).text_color(on ? t.foreground : t.muted).hover((s) => s.bg(t.hover))
        .on_click((_e, cx) => { this.filter = on ? null : value; save("filter", this.filter); cx.notify(); })
        .child(label);
    };
    return h_flex().flex_none().flex_wrap().gap(4).px(10).pt(8).child(chip("all", null)).children(names.map((n) => chip(n, n)));
  }

  heading(t, title, right, color) {
    return h_flex().gap(8).items_center()
      .child(div().text_size(11).font_semibold().text_color(color ?? t.muted).child(title.toUpperCase()))
      .child(div().text_size(11).text_color(t.dim).child(right ?? ""));
  }

  section(name, phases, rows, ctx) {
    const { t } = ctx;
    const shut = this.shut.has(name);
    const head = h_flex().id(`sec-${name}`).gap(8).items_center().cursor_pointer()
      .on_click((_e, cx) => { shut ? this.shut.delete(name) : this.shut.add(name); save("shut", [...this.shut]); cx.notify(); })
      .child(div().w(10).text_color(t.dim).child(shut ? "▸" : "▾"))
      .child(div().text_size(11).font_semibold().text_color(t.muted).child(name.toUpperCase()))
      .child(div().text_size(11).text_color(t.dim).child(String(rows.length)))
      .child(div().flex_1().truncate().text_size(11).text_color(t.dim).child(this.summary(name, rows, ctx)));
    const out = v_flex().gap(6).child(head);
    if (shut) return out;
    if (!rows.length) return out.child(div().pl(18).text_size(12).text_color(t.dim).child(name === "queued" ? "Nothing queued." : "Nothing here."));
    const shown = name === "done" ? rows.slice(-DONE_LIMIT).reverse() : rows;
    out.child(v_flex().gap(6).children(shown.map((x) => this.card(x, phases, ctx))));
    if (shown.length < rows.length) out.child(div().pl(18).text_size(12).text_color(t.dim).child(`+${rows.length - shown.length} older · orch list --phase archived`));
    return out;
  }

  summary(name, rows, { st, pending }) {
    const by = (ph) => rows.filter((x) => x.phase === ph).length;
    const mine = pending.filter((a) => rows.some((x) => x.id === a.task)).length;
    const parts = [];
    if (name === "in progress") {
      for (const ph of ["implementing", "planning", "awaiting-plan"]) if (by(ph)) parts.push(`${by(ph)} ${ph.replace("-", " ")}`);
    } else if (name === "review") {
      const prs = by("pr-open");
      const withReviewer = rows.length - prs - by("merged");
      if (withReviewer) parts.push(`${withReviewer} with reviewer`);
      if (prs) parts.push(`${plural(prs, "open PR")} yours to merge`);
      if (by("merged")) parts.push(`${by("merged")} merged`);
    } else if (name === "queued") {
      if (rows[0]) parts.push(`next up: ${rows[0].key}`);
      const free = Math.max(0, (st.max_active ?? 3) - (st.tasks ?? []).filter((x) => ACTIVE.includes(x.phase)).length);
      parts.push(free ? `${plural(free, "slot")} free` : "no slots free");
    } else if (name === "parked") {
      parts.push(rows.slice(0, 3).map((x) => x.key).join(" · "));
    }
    if (mine) parts.push(`${mine} needs you`);
    return parts.join(" · ");
  }

  card(x, phases, ctx) {
    const { t, allPanes, blocking } = ctx;
    const pane = this.paneFor(x, allPanes);
    const status = this.workerStatus(x, pane);
    const yours = YOURS.has(x.phase);
    const statusColor = { working: t.working, blocked: t.blocked, done: t.done, idle: t.idle, gone: t.blocked }[status] ?? t.unknown;
    const tags = [];
    if (phases.length > 1 && x.phase !== phases[0]) tags.push(this.tag(t, x.phase, yours ? t.foreground : t.muted));
    if (!this.filter && x.project) tags.push(this.tag(t, x.project));
    if (x.worker) tags.push(this.tag(t, x.worker));
    if (Number(x.review_round)) tags.push(this.tag(t, `rev ${x.review_round}`));
    const nBlocking = blocking.get(x.id) ?? 0;
    if (nBlocking) tags.push(this.tag(t, `${nBlocking} blocking`, t.blocked));
    if (x.trivial) tags.push(this.tag(t, "trivial"));
    if (x.pr_url) {
      tags.push(this.tag(t, `PR ${x.pr_state ?? "open"} ↗`, t.accent).id(`pr-${x.id}`).cursor_pointer()
        .on_mouse_down("left", (_e, cx) => cx.stop_propagation())
        .on_click((_e, cx) => { cx.stop_propagation(); exec("open", [x.pr_url]).catch(() => {}); }));
    }
    return v_flex().id(`card-${x.id}`).p(8).gap(4).rounded(6).bg(t.surface).border_1()
      .border_color(yours ? t.accent : t.border)
      .when(Boolean(pane), (el) => el.cursor_pointer().hover((s) => s.bg(t.hover))
        .on_click(() => run("pane.focus", { pane: pane.pane })))
      .child(h_flex().gap(6).items_center()
        .child(div().font_semibold().flex_none().child(x.key))
        .child(div().flex_1().min_w(0).truncate().text_color(t.muted).child(x.title ?? ""))
        .when(Boolean(status), (el) => el.child(h_flex().flex_none().gap(4).items_center()
          .child(div().w(7).h(7).rounded_full().bg(statusColor))
          .child(div().text_size(11).text_color(statusColor).child(status)))))
      .when(tags.length > 0, (el) => el.child(h_flex().flex_wrap().gap(4).children(tags)))
      .when(Boolean(x.note) && x.phase !== "archived", (el) => el.child(div().text_size(11).text_color(t.dim).truncate().child(firstLine(x.note))));
  }

  pollerPill(t) {
    const p = this.poller();
    if (p) {
      return this.pill(t, "PR poller on", t.working).id("poller").cursor_pointer().hover((s) => s.bg(t.hover))
        .on_click(() => run("terminal.peek", { terminal: p.address }));
    }
    return this.pill(t, this.pollerStarting ? "starting poller…" : "PR poller off · start", t.done).id("poller").cursor_pointer()
      .hover((s) => s.bg(t.hover)).on_click((_e, cx) => this.startPoller(cx));
  }

  approval(a, { t }) {
    const open = this.open.has(a.id);
    const n = this.notes.get(a.id);
    const button = (id, text, primary, onClick) => div().id(id).px(10).py(4).rounded(5).text_size(12).font_medium()
      .bg(primary ? t.solid : t.raised).text_color(primary ? t.solid_foreground : t.foreground)
      .border_1().border_color(primary ? t.solid : t.input)
      .when(!n?.busy, (el) => el.cursor_pointer().hover((s) => s.opacity(0.88)).on_click(onClick))
      .when(Boolean(n?.busy), (el) => el.opacity(0.5))
      .child(text);
    return v_flex().id(`appr-${a.id}`).p(8).gap(6).rounded(6).bg(t.surface).border_1().border_color(t.blocked)
      .child(h_flex().id(`appr-head-${a.id}`).gap(6).items_center().cursor_pointer()
        .on_click((_e, cx) => { open ? this.open.delete(a.id) : this.open.add(a.id); cx.notify(); })
        .child(div().font_semibold().flex_none().child(a.key))
        .child(this.tag(t, a.kind, t.blocked))
        .when(Boolean(a.project) && !this.filter, (el) => el.child(this.tag(t, a.project)))
        .child(div().flex_1())
        .child(div().text_size(11).text_color(t.dim).child(open ? "hide details ▾" : a.body ? "details ▸" : "")))
      .child(div().child(a.title ?? ""))
      .when(open && Boolean(a.body), (el) => el.child(div().p(6).rounded(4).bg(t.background).text_size(12).font_family("monospace").text_color(t.muted).child(a.body)))
      .when(Boolean(a.plan_path), (el) => el.child(div().text_size(11).text_color(t.dim).truncate().child(`plan: ${a.plan_path}`)))
      .when(Boolean(n), (el) => el
        .child(Textarea.new(n.input).w_full().px(8).py(6).rounded(5).border_1().border_color(n.hint ? t.blocked : t.input).bg(t.background).text_size(12).text_color(t.foreground))
        .when(n.hint, (el) => el.child(div().text_size(11).text_color(t.blocked).child("A rejection needs a note: it's the only thing telling the worker what to change.")))
        .when(Boolean(n.error), (el) => el.child(div().text_size(11).text_color(t.blocked).child(n.error)))
        .child(h_flex().gap(6).items_center()
          .child(button(`ok-${a.id}`, "Approve", true, (_e, cx) => this.decide(a, "approved", cx)))
          .child(button(`no-${a.id}`, "Reject", false, (_e, cx) => this.decide(a, "rejected", cx)))
          .when(Boolean(n.busy), (el) => el.child(div().text_size(11).text_color(t.dim).child("sending…")))
          .when(Boolean(a.plan_path) && !n.busy, (el) => el.child(div().text_size(11).text_color(t.dim).child("Plannotator review: web board")))));
  }

  orphan(id, o, t) {
    return v_flex().p(8).gap(6).rounded(6).bg(t.surface).border_1().border_color(t.done)
      .child(div().child(`${o.key} — ${o.title}`))
      .child(div().text_size(11).text_color(t.done).child(`Settled ${o.status} elsewhere while you were writing, so this note was never sent. Copy it before dismissing.`))
      .child(Textarea.new(o.input).w_full().px(8).py(6).rounded(5).border_1().border_color(t.input).bg(t.background).text_size(12))
      .child(h_flex().child(div().id(`drop-${id}`).px(10).py(4).rounded(5).bg(t.raised).text_size(12).cursor_pointer()
        .on_click((_e, cx) => { o.input.release(); this.orphans.delete(id); cx.notify(); }).child("Dismiss")));
  }

  handoff(h, { t }) {
    const failed = String(h.notified ?? "").startsWith("failed");
    return v_flex().p(6).gap(2).rounded(6).bg(t.surface).border_1().border_color(failed ? t.blocked : t.border)
      .child(h_flex().gap(6).items_center()
        .child(div().font_semibold().child(h.key))
        .child(this.tag(t, h.phase))
        .child(div().flex_1().min_w(0).truncate().text_size(12).text_color(t.muted).child(firstLine(h.reason))))
      .when(Boolean(h.notified), (el) => el.child(div().text_size(11).text_color(failed ? t.blocked : t.dim).truncate().child(h.notified)));
  }

  banner(t, title, detail) {
    return v_flex().p(8).gap(2).rounded(6).border_1().border_color(t.border).bg(t.surface)
      .child(div().text_size(12).text_color(t.done).child(title))
      .child(div().text_size(11).text_color(t.dim).child(detail));
  }

  pill(t, text, color) {
    return div().px(8).py(1).rounded(10).bg(t.surface).text_size(11).text_color(color ?? t.muted).child(text);
  }

  tag(t, text, color) {
    return div().px(6).py(1).rounded(4).bg(t.raised).text_size(11).text_color(color ?? t.muted).child(String(text));
  }
}
