// A status-bar toggle for the orchestrator board.
//
// Click to show or hide the layout node that holds orch-board (`layout.toggle`).
// It lights up while the board is shown and counts the cards waiting on you,
// so a hidden board still says when it needs attention.
//
// Put it beside workspace-status in the status row:
//   { "id": "orch-toggle", "type": "block", "size": { "px": 190 },
//     "content": { "mod": "orch-toggle", "props": { "target": "board" } } }
// `target` is the id of the board's node; it defaults to "board".

import { View, div } from "gpui-kit";
import { h_flex } from "gpui-base";
import { exec, readTextFile, watchFolder, theme, run, layout, keys, window, props } from "herdr";

const FALLBACK_POLL_MS = 15000;

// The node's `hidden` in the shown layout, clicks included. Null when the
// layout has no such node, so the button can say so instead of doing nothing.
function nodeHidden(tree, id) {
  if (!tree || typeof tree !== "object") return null;
  if (tree.id === id) return Boolean(tree.hidden);
  for (const c of tree.children ?? []) {
    const h = nodeHidden(c, id);
    if (h !== null) return h;
  }
  return null;
}

// The key bound to toggling this node, drawn the way workspace-status draws its hint.
function keyHint(target) {
  const mac = window()?.platform === "macos";
  const mod = { cmd: mac ? "⌘" : "Ctrl+", ctrl: mac ? "⌃" : "Ctrl+", shift: mac ? "⇧" : "Shift+",
                alt: mac ? "⌥" : "Alt+", option: mac ? "⌥" : "Alt+" };
  const b = keys().find((k) => k.action === `layout.toggle:${target}`);
  if (!b) return null;
  return b.keys.split("-").map((p) => mod[p.toLowerCase()] ?? p.toUpperCase()).join("");
}

export default class OrchToggle extends View {
  init(_props, cx) {
    this.target = props()?.target ?? "board";
    this.dir = null;
    this.waiting = 0;   // pending approvals: the cards that need you
    this.active = 0;

    cx.spawn(async (cx) => {
      // Same default as `orch`, honouring the override if the login env sets it.
      const r = await exec("sh", ["-c", 'printf %s "${HERDR_ORCHESTRATOR_HOME:-$HOME/.claude/orchestrator}"']).catch(() => null);
      this.dir = r?.stdout?.trim() || null;
      if (!this.dir) return;
      await this.reload(cx);
      for (;;) {
        try { await watchFolder(this.dir); } catch { return; } // the timer below keeps it fresh
        await this.reload(cx);
      }
    });
    cx.timer.every(FALLBACK_POLL_MS, (cx) => { cx.spawn(async (cx) => this.reload(cx)); });
  }

  async reload(cx) {
    if (!this.dir) return;
    let st;
    try { st = JSON.parse(await readTextFile(`${this.dir}/state.json`)); } catch { return; }
    if (!st) return;
    const waiting = (st.approvals ?? []).filter((a) => a.status === "pending").length;
    const active = (st.tasks ?? []).filter((t) => !["queued", "archived", "parked", "blocked"].includes(t.phase)).length;
    if (waiting !== this.waiting || active !== this.active) {
      this.waiting = waiting;
      this.active = active;
      cx.notify();
    }
  }

  render() {
    const t = theme();
    const hidden = nodeHidden(layout(), this.target);
    const missing = hidden === null;
    const shown = hidden === false;
    const hint = keyHint(this.target);

    return h_flex().id("orch-toggle").size_full().px(12).gap(8).items_center().justify_end()
      .bg(t.chrome).border_t(1).border_color(t.border).text_size(11.8)
      .text_color(shown ? t.foreground : t.muted)
      .hover((s) => s.text_color(t.foreground))
      .on_click(() => { if (!missing) run("layout.toggle", { id: this.target }); })
      .child(div().size(7).rounded(4).bg(missing ? t.dim : shown ? t.accent : this.active ? t.muted : t.dim))
      .child(missing ? `no "${this.target}" node` : "Orchestrator")
      .when(!missing && this.waiting > 0, (el) => el.child(
        div().px(6).rounded(7).bg(t.blocked).text_color(t.background).child(String(this.waiting))))
      .when(!missing && Boolean(hint), (el) => el.child(div().text_color(t.dim).child(hint)));
  }
}
