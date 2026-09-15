// Kanban over state.json. Polls rather than using websockets: the orchestrator
// writes on its own schedule, and a 2s poll of a small local file is cheaper to
// reason about than keeping a socket in sync with an flock'd file.

let state = null;
let dragId = null;
let dirty = false;   // suppress polling while the user is mid-drag or typing
let lastSig = null;  // only repaint when the server actually wrote
let filterProj = new URLSearchParams(location.search).get('project') || '';
let editingProj = null;   // project name currently open for inline editing

// Which sections are shut. Seeded from the server's defaults on first visit,
// then remembered per viewer — a shut section still reports through its header
// summary, so folding one hides nothing.
let collapsed = null;

function readCollapsed(defaults) {
  if (collapsed) return collapsed;
  try {
    const raw = localStorage.getItem('collapsedSections');
    collapsed = raw ? new Set(JSON.parse(raw)) : new Set(defaults);
  } catch { collapsed = new Set(defaults); }
  return collapsed;
}
function persistCollapsed() {
  try { localStorage.setItem('collapsedSections', JSON.stringify([...collapsed])); } catch {}
}

const $ = (s) => document.querySelector(s);

async function api(path, body) {
  const r = await fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  const j = await r.json();
  if (j.error) alert(j.error);
  await load(true);
  return j;
}

async function load(force) {
  if (dirty) return;
  const r = await fetch('/api/state');
  const next = await r.json();
  // Re-check, because the guard above ran before the request went out. A caret
  // that landed in a textarea while this was in flight would otherwise be
  // repainted away: noteDrafts restores the text, but not the focus or the
  // cursor position, and the human is left typing into nothing.
  if (dirty) return;
  // Repainting on every tick would tear the DOM out from under a click or a
  // drag. `updated` changes on every server write, so it is a sufficient signal.
  const sig = next.updated;
  if (!force && sig === lastSig) return;
  lastSig = sig;
  state = next;
  render();
}

function esc(s) {
  return String(s ?? '').replace(/[&<>"]/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
}

// Half-typed feedback, keyed by approval id. The poll rewrites #approvals
// wholesale on every board write — ours or another agent's — so a draft has to
// live outside the DOM, or a stray repaint eats what the human was writing.
const noteDrafts = new Map();

// Decisions we have posted but not yet heard back on. Until the server answers
// we cannot tell a delivered note from a lost one, so the prune leaves these be.
const inFlight = new Set();

// Notes rescued from a card that vanished mid-composition — settled by the
// Plannotator gate, by `orch resolve`, or by another agent. The approval is
// gone and the text was never sent, so it is shown for the human to copy
// rather than dropped. A carefully argued rejection is expensive to retype.
const orphanNotes = new Map();

function growNote(ta) {
  ta.style.height = 'auto';           // shrink first, or it only ever grows
  ta.style.height = `${ta.scrollHeight}px`;
}

// Approving with caveats is the point of this box, so the note is optional on
// that path. A rejection without one is not: that text is the only thing the
// worker gets telling it what to change. Enforced here only — the CLI and the
// Plannotator gate may both legitimately resolve without a note.
function decide(aid, decision) {
  // The box itself is the source of truth; the map is only the repaint backup.
  // Anything that fills a field without firing `input` still gets read here.
  const ta = $(`[data-note="${aid}"]`);
  const note = ((ta ? ta.value : noteDrafts.get(aid)) || '').trim();
  if (decision === 'rejected' && !note) {
    const hint = $(`[data-hint="${aid}"]`);
    if (hint) hint.hidden = false;
    if (ta) ta.focus();
    return;
  }
  dirty = false;                      // load() bails while dirty, even forced
  const body = { approval: aid, decision };
  if (note) body.note = note;         // omit rather than send '', so decision_note stays null

  // Keep the draft until the server confirms. The common failure — the approval
  // was settled elsewhere while this was being written — arrives as an error
  // here, and that is exactly when the text must not be thrown away.
  inFlight.add(aid);
  const rescue = () => { inFlight.delete(aid); render(); };  // prune turns it into an orphan
  api('/api/resolve', body).then(
    (j) => {
      if (j && j.error) return rescue();
      inFlight.delete(aid);
      noteDrafts.delete(aid);         // delivered
    },
    rescue,                           // network or parse failure: rescue it too
  );
}

function renderApprovals() {
  const pend = (state.approvals || []).filter((a) => a.status === 'pending');

  // A draft whose approval is no longer pending is either delivered or lost.
  // In flight, we do not know yet — leave it. Otherwise the card went away
  // under the human, so surface the text instead of deleting it.
  const live = new Set(pend.map((a) => a.id));
  for (const id of [...noteDrafts.keys()]) {
    if (live.has(id) || inFlight.has(id)) continue;
    const text = (noteDrafts.get(id) || '').trim();
    noteDrafts.delete(id);
    if (!text) continue;
    const a = (state.approvals || []).find((x) => x.id === id);
    orphanNotes.set(id, { text, key: a ? a.key : '?', title: a ? a.title : '',
                          status: a ? a.status : 'removed' });
  }

  $('#approvals-wrap').hidden = pend.length === 0 && orphanNotes.size === 0;

  const rescued = [...orphanNotes].map(([id, o]) => `
    <div class="approval orphan">
      <div><span class="k">${esc(o.key)}</span> — ${esc(o.title)}</div>
      <div class="orphanwhy">Settled <b>${esc(o.status)}</b> elsewhere while you were writing, so this note was never sent. Copy it before dismissing.</div>
      <textarea class="note ro" data-orphan="${id}" readonly aria-label="Unsent note">${esc(o.text)}</textarea>
      <div class="btns"><button data-drop="${id}">Dismiss</button></div>
    </div>`).join('');

  $('#approvals').innerHTML = rescued + pend.map((a) => `
    <div class="approval">
      <div>${a.project ? `<span class="tag proj">${esc(a.project)}</span> ` : ''}<span class="k">${esc(a.key)}</span> · ${esc(a.kind)} — ${esc(a.title)}</div>
      ${a.body ? `<pre>${esc(a.body)}</pre>` : ''}
      ${a.plan_path ? `<div class="planpath">${esc(a.plan_path)}</div>` : ''}
      <textarea class="note" data-note="${a.id}" rows="3"
        aria-label="Feedback for the worker"
        placeholder="Feedback for the worker — optional to approve, required to reject"></textarea>
      <div class="notehint" data-hint="${a.id}" hidden>A rejection needs a note — it is the only thing telling the worker what to change.</div>
      <div class="btns">
        ${a.plan_path && state._plannotator ? (
          a.review_started
            ? `<span class="reviewing">Plannotator review open — decide there</span>`
            : `<button class="primary" data-review="${a.id}">Review in Plannotator</button>`
        ) : ''}
        <button class="${a.plan_path && state._plannotator ? '' : 'primary'}" data-ok="${a.id}">Approve</button>
        <button data-no="${a.id}">Reject</button>
      </div>
    </div>`).join('');

  $('#approvals').querySelectorAll('[data-orphan]').forEach((ta) => growNote(ta));
  $('#approvals').querySelectorAll('[data-drop]').forEach((b) =>
    b.onclick = () => { orphanNotes.delete(b.dataset.drop); render(); });

  $('#approvals').querySelectorAll('[data-note]').forEach((ta) => {
    // Restore by value, never by markup: what the human typed is text, not HTML.
    ta.value = noteDrafts.get(ta.dataset.note) || '';
    growNote(ta);
    ta.addEventListener('input', () => {
      noteDrafts.set(ta.dataset.note, ta.value);
      growNote(ta);
      const hint = $(`[data-hint="${ta.dataset.note}"]`);
      if (hint) hint.hidden = true;
    });
    // Repainting under a caret would drop the cursor mid-sentence.
    ta.addEventListener('focus', () => { dirty = true; });
    ta.addEventListener('blur', () => { dirty = false; });
  });

  $('#approvals').querySelectorAll('[data-review]').forEach((b) =>
    b.onclick = () => {
      b.disabled = true;
      b.textContent = 'Opening…';
      // Plannotator opens its own browser window; the verdict comes back
      // through the board, so there is nothing more to do here.
      api('/api/plan/review', { approval: b.dataset.review });
    });

  $('#approvals').querySelectorAll('[data-ok]').forEach((b) =>
    b.onclick = () => decide(b.dataset.ok, 'approved'));
  $('#approvals').querySelectorAll('[data-no]').forEach((b) =>
    b.onclick = () => decide(b.dataset.no, 'rejected'));
}

function renderSuggestions() {
  const open = (state.suggestions || [])
    .filter((x) => x.status === 'open')
    .filter((x) => !filterProj || x.project === filterProj)
    .sort((a, b) => (b.seconded || 0) - (a.seconded || 0));

  $('#sugwrap').hidden = open.length === 0;
  $('#sugcount').textContent = open.length ? `${open.length} open` : '';

  $('#suggestions').innerHTML = open.map((x) => `
    <div class="sug">
      <div class="hd">
        <span class="tag proj">${esc(x.project || 'tooling')}</span>
        <span class="t">${esc(x.title)}</span>
        ${x.seconded ? `<span class="hits">hit ${x.seconded + 1}\u00d7</span>` : ''}
      </div>
      ${x.body ? `<div class="dim">${esc(x.body)}</div>` : ''}
      <ul>${(x.evidence || []).map((e) => `<li>${esc(e)}</li>`).join('')}</ul>
      <div class="btns">
        <button class="primary" data-promote="${x.id}">Promote to task</button>
        <button data-dismiss="${x.id}">Dismiss</button>
      </div>
    </div>`).join('');

  $('#suggestions').querySelectorAll('[data-promote]').forEach((b) =>
    b.onclick = () => {
      const x = state.suggestions.find((s) => s.id === b.dataset.promote);
      const key = prompt(`Task key for "${x.title}"`, '');
      if (!key) return;
      let project = x.project;
      if (!project) {
        const names = (state.projects || []).map((p) => p.name);
        project = prompt(`Which project? (${names.join(', ')})`, names[0] || '');
        if (!project) return;
      }
      api('/api/suggestion/promote', { suggestion: x.id, key, project });
    });
  $('#suggestions').querySelectorAll('[data-dismiss]').forEach((b) =>
    b.onclick = () => {
      const reason = prompt('Why dismiss? (optional)');
      if (reason === null) return;
      api('/api/suggestion/dismiss', { suggestion: b.dataset.dismiss, reason });
    });
}

const plural = (n, word) => `${n} ${word}${n === 1 ? '' : 's'}`;

function ageOf(t) {
  const ms = Date.now() - Date.parse(t.updated || t.created || '');
  if (!isFinite(ms) || ms < 0) return null;
  const m = Math.floor(ms / 60000);
  return m < 60 ? `${m}m` : `${Math.floor(m / 60)}h`;
}

/* Left half says what is inside; right half says what you would act on. That
   split is what lets a shut section stay informative. */
function summarise(name, rows, state) {
  const by = (ph) => rows.filter((t) => t.phase === ph).length;
  const pending = (state.approvals || []).filter((a) => a.status === 'pending');
  const mine = pending.filter((a) => rows.some((t) => t.id === a.task)).length;
  const left = [], right = [];

  if (name === 'in progress') {
    if (by('implementing')) left.push(`${by('implementing')} implementing`);
    if (by('planning')) left.push(`${by('planning')} planning`);
    if (by('awaiting-plan')) left.push(`${by('awaiting-plan')} awaiting plan`);
    const ages = rows.map(ageOf).filter(Boolean);
    if (ages.length) right.push({ text: `oldest ${ages[ages.length - 1]}` });
    if (mine) right.push({ text: `${mine} needs you`, tone: 'warn' });
  } else if (name === 'review') {
    const agent = rows.filter((t) => !['pr-open', 'merged'].includes(t.phase)).length;
    const prs = rows.filter((t) => t.phase === 'pr-open').length;
    if (agent) left.push(`${agent} with reviewer`);
    if (prs) left.push(plural(prs, 'open PR'));
    if (by('merged')) left.push(`${by('merged')} merged`);
    if (prs) right.push({ text: `${prs} yours to merge`, tone: 'ok' });
    if (mine) right.push({ text: `${mine} needs you`, tone: 'warn' });
  } else if (name === 'queued') {
    const next = rows[0];
    if (next) left.push(`next up: ${next.key}`);
    const free = Math.max(0, (state.max_active ?? 3) - state._active);
    right.push({ text: free ? plural(free, 'slot') + ' free' : 'no slots free',
                 tone: free ? 'ok' : null });
  } else if (name === 'parked') {
    left.push(rows.slice(0, 3).map((t) => t.key).join(' · '));
  } else if (name === 'done') {
    left.push(plural(rows.length, 'task') + ' archived');
    const last = rows[rows.length - 1];
    if (last) left.push(`last ${last.key}`);
  }
  return { left: left.join(' · '), right };
}

function card(t, canonical, yours) {
  const pr = t.pr_url
    ? `<a href="${esc(t.pr_url)}" target="_blank" rel="noopener">PR ${esc(t.pr_state || 'open')}</a>` : '';
  const agent = t.agent_state ? `<span class="tag agent-${esc(t.agent_state)}">${esc(t.agent_state)}</span>` : '';
  // Only queued cards reorder — dragging an in-flight task would imply
  // re-prioritising work an agent is already doing, which the queue can't honour.
  const drag = t.phase === 'queued';
  const mine = yours && yours.has(t.phase);
  return `<div class="card${mine ? ' yours' : ''}" ${drag ? 'draggable="true"' : ''} data-id="${t.id}">
      <div class="cardtop">
        <span class="key">${esc(t.key)}</span>
        ${t._deletable ? `<button class="del" data-del="${t.id}" title="delete this task">&times;</button>` : ''}
      </div>
      <div class="title">${esc(t.title)}</div>
      <div class="row">
        ${!filterProj && t.project ? `<span class="tag proj">${esc(t.project)}</span>` : ''}
        ${t.phase !== canonical ? `<span class="tag phase">${esc(t.phase)}</span>` : ''}
        ${agent}
        ${t.worker ? `<span class="tag">${esc(t.worker)}</span>` : ''}
        ${t.review_round ? `<span class="tag">rev ${t.review_round}</span>` : ''}
        ${t._blocking ? `<span class="tag blocking">${t._blocking} blocking</span>` : ''}
        ${t.trivial ? '<span class="tag trivial">trivial — review waived</span>' : ''}
        ${pr}
      </div>
    </div>`;
}

function syncProjectPickers() {
  const projs = (state.projects || []).slice().sort((a, b) => a.name.localeCompare(b.name));

  const f = $('#filter');
  if (document.activeElement !== f) {
    f.innerHTML = '<option value="">all projects</option>' +
      projs.map((p) => `<option value="${esc(p.name)}">${esc(p.name)}</option>`).join('');
    f.value = filterProj;
  }

  // Adding a task needs a project, so surface that rather than failing on submit.
  const ap = $('#addproject');
  if (document.activeElement !== ap) {
    const keep = ap.value;
    ap.innerHTML = projs.length
      ? projs.map((p) => `<option value="${esc(p.name)}">${esc(p.name)}</option>`).join('')
      : '<option value="">register a project first</option>';
    if (keep && projs.some((p) => p.name === keep)) ap.value = keep;
    else if (filterProj && projs.some((p) => p.name === filterProj)) ap.value = filterProj;
  }

  $('#projlist').innerHTML = projs.length ? projs.map((p) => {
    const n = state.tasks.filter((t) => t.project === p.name && t.phase !== 'archived').length;
    if (editingProj === p.name) {
      return `<div class="prow editing">
          <input class="enm" value="${esc(p.name)}" pattern="[a-z0-9][a-z0-9_-]*">
          <input class="epath" value="${esc(p.path)}">
          <button class="primary" data-save="${esc(p.name)}">Save</button>
          <button data-cancel="1">Cancel</button>
        </div>`;
    }
    return `<div class="prow">
        <span class="nm">${esc(p.name)}</span>
        <span class="pth">${esc(p.path)}</span>
        <span class="dim">${n} open</span>
        <button data-edit="${esc(p.name)}">edit</button>
        <button data-rm="${esc(p.name)}" ${n ? 'disabled title="has live tasks"' : ''}>remove</button>
      </div>`;
  }).join('') : '<div class="dim">No projects yet. Register one below to start adding work.</div>';

  $('#projlist').querySelectorAll('[data-edit]').forEach((b) =>
    b.onclick = () => { editingProj = b.dataset.edit; dirty = true; render(); });
  $('#projlist').querySelectorAll('[data-cancel]').forEach((b) =>
    b.onclick = () => { editingProj = null; dirty = false; load(true); });
  $('#projlist').querySelectorAll('[data-save]').forEach((b) => {
    b.onclick = () => {
      const row = b.closest('.prow');
      const name = b.dataset.save;
      const new_name = row.querySelector('.enm').value.trim();
      const path = row.querySelector('.epath').value.trim();
      const body = { name };
      if (new_name && new_name !== name) body.new_name = new_name;
      const cur = (state.projects.find((x) => x.name === name) || {}).path;
      if (path && path !== cur) body.path = path;
      if (!body.new_name && !body.path) { editingProj = null; dirty = false; load(true); return; }
      // Renaming rewrites every task's project, so make that consequence visible.
      const refs = state.tasks.filter((t) => t.project === name).length;
      if (body.new_name && refs &&
          !confirm(`Rename ${name} to ${body.new_name}? ${refs} task(s) will be updated.`)) return;
      editingProj = null; dirty = false;
      api('/api/project/update', body);
    };
  });

  $('#projlist').querySelectorAll('[data-rm]').forEach((b) =>
    b.onclick = () => api('/api/project/remove', { name: b.dataset.rm }));
}

function render() {
  syncProjectPickers();
  $('#updated').textContent = state.updated ? `updated ${state.updated.slice(11, 16)}` : '';
  const max = state.max_active ?? 3;
  const wip = $('#wip');
  wip.textContent = `${state._active} / ${max} active`;
  wip.classList.toggle('full', state._active >= max);
  if (document.activeElement !== $('#max')) $('#max').value = max;

  renderApprovals();
  renderSuggestions();

  const shut = readCollapsed(state._collapsed_default || []);
  const yours = new Set(state._yours_phases || []);

  $('#board').innerHTML = (state._sections || []).map(([name, phases]) => {
    const rows = state.tasks
      .filter((t) => phases.includes(t.phase))
      .filter((t) => !filterProj || t.project === filterProj)
      .sort((a, b) => (phases.indexOf(a.phase) - phases.indexOf(b.phase))
                   || ((a.order ?? 0) - (b.order ?? 0)));

    // An empty optional section is noise; the active ones stay so the board
    // keeps its shape when work drains out of them.
    if (!rows.length && ['parked', 'done', 'queued'].includes(name)) return '';

    const isShut = shut.has(name);
    const sum = summarise(name, rows, state);
    const cards = isShut ? '' :
      `<div class="cards">${rows.map((t) => card(t, phases[0], yours)).join('')}</div>`;

    return `<section class="sec ${isShut ? 'shut' : ''}" data-sec="${esc(name)}">
        <div class="sechead" data-toggle="${esc(name)}" role="button" tabindex="0"
             aria-expanded="${!isShut}">
          <span class="chev">${isShut ? '▸' : '▾'}</span>
          <span class="sectitle">${esc(name)} <span>${rows.length}</span></span>
          ${sum.left ? `<span class="sum">${esc(sum.left)}</span>` : ''}
          <span class="sumright">${sum.right.map((r) =>
            `<span class="${r.tone ? 'tone-' + r.tone : ''}">${esc(r.text)}</span>`).join('')}</span>
        </div>
        ${cards}
      </section>`;
  }).join('');

  $('#board').querySelectorAll('[data-toggle]').forEach((el) => {
    const flip = () => {
      const n = el.dataset.toggle;
      shut.has(n) ? shut.delete(n) : shut.add(n);
      persistCollapsed();
      render();
    };
    el.onclick = flip;
    el.onkeydown = (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); flip(); } };
  });

  wireDelete();
  wireDrag();
}

function wireDelete() {
  document.querySelectorAll('[data-del]').forEach((b) => {
    b.onclick = (e) => {
      e.stopPropagation();          // a delete click is not a card drag
      const t = state.tasks.find((x) => x.id === b.dataset.del);
      if (!t) return;
      if (confirm(`Delete ${t.project}/${t.key} — "${t.title}"?`)) {
        api('/api/task/delete', { task: t.id });
      }
    };
    // Dragging from the button would start a card drag the user did not mean.
    b.draggable = false;
    b.addEventListener('dragstart', (e) => { e.preventDefault(); e.stopPropagation(); });
  });
}

function wireDrag() {
  document.querySelectorAll('.card[draggable="true"]').forEach((el) => {
    el.addEventListener('dragstart', () => {
      dragId = el.dataset.id; dirty = true; el.classList.add('dragging');
    });
    el.addEventListener('dragend', () => {
      el.classList.remove('dragging'); dirty = false;
    });
  });

  // Only the queued section accepts drops; every other section is agent-driven.
  const zone = document.querySelector('.sec[data-sec="queued"] .cards');
  if (!zone) return;

  zone.addEventListener('dragover', (e) => {
    e.preventDefault();
    zone.classList.add('over');
    const after = [...zone.querySelectorAll('.card:not(.dragging)')].find((c) => {
      const r = c.getBoundingClientRect();
      return e.clientY < r.top + r.height / 2;
    });
    const dragging = zone.querySelector('.dragging') || document.querySelector('.dragging');
    if (!dragging) return;
    after ? zone.insertBefore(dragging, after) : zone.appendChild(dragging);
  });
  zone.addEventListener('dragleave', () => zone.classList.remove('over'));
  zone.addEventListener('drop', (e) => {
    e.preventDefault();
    zone.classList.remove('over');
    dirty = false;
    const ids = [...zone.querySelectorAll('.card')].map((c) => c.dataset.id);
    api('/api/reorder', { ids });
  });
}

const dlg = $('#taskdlg');

function openTaskDialog() {
  if (!(state.projects || []).length) {
    alert('Register a project first — open the Projects panel at the bottom.');
    return;
  }
  syncProjectPickers();
  dirty = true;              // don't let the poll repaint under a half-typed form
  dlg.showModal();
}

function closeTaskDialog() {
  dlg.close();
  dirty = false;
}

$('#newtask').addEventListener('click', openTaskDialog);
$('#canceltask').addEventListener('click', closeTaskDialog);
// Native dialog closes on Esc without firing our handler, so clear dirty here.
dlg.addEventListener('close', () => { dirty = false; });
// Click on the backdrop (outside the form) dismisses.
dlg.addEventListener('click', (e) => { if (e.target === dlg) closeTaskDialog(); });

$('#add').addEventListener('submit', (e) => {
  e.preventDefault();
  const f = new FormData(e.target);
  const el = e.target;
  dirty = false;
  api('/api/task', Object.fromEntries(f)).then(() => { el.reset(); dlg.close(); });
});



$('#max').addEventListener('change', (e) =>
  api('/api/config', { max_active: e.target.value }));

$('#filter').addEventListener('change', (e) => {
  filterProj = e.target.value;
  // Keep the filter in the URL so a reload lands on the same view.
  history.replaceState(null, '', filterProj ? `?project=${encodeURIComponent(filterProj)}` : location.pathname);
  render();
});

const projdlg = $('#projdlg');
$('#openproj').addEventListener('click', () => { dirty = true; syncProjectPickers(); projdlg.showModal(); });
$('#closeproj').addEventListener('click', () => projdlg.close());
projdlg.addEventListener('close', () => { dirty = false; editingProj = null; });
// Clicking the backdrop beside the drawer dismisses it.
projdlg.addEventListener('click', (e) => { if (e.target === projdlg) projdlg.close(); });

$('#addproj').addEventListener('submit', (e) => {
  e.preventDefault();
  const f = Object.fromEntries(new FormData(e.target));
  api('/api/project', f).then(() => e.target.reset());
});
$('#addproj').addEventListener('focusin', () => { dirty = true; });
$('#addproj').addEventListener('focusout', () => { dirty = false; });

load(true);
setInterval(load, 2000);
