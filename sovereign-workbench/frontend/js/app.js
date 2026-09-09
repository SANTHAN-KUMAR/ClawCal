/* Sovereign Workbench UI.
   Framework-free JavaScript. Every asset the page needs — script, stylesheet,
   typeface — ships inside the appliance, because a workbench whose own UI
   phoned out for a file would contradict the claim it exists to make.

   This file changes presentation only. Every endpoint, request body and data
   flow is exactly as the control plane defines it. */
'use strict';

const $  = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));
const esc = s => String(s ?? '').replace(/[&<>"']/g,
  c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const fmtBytes = n => n > 1e6 ? (n / 1e6).toFixed(1) + ' MB'
  : n > 1e3 ? (n / 1e3).toFixed(0) + ' kB' : (n || 0) + ' B';
const fmtSec = s => s == null ? '—' : s < 60 ? s.toFixed(1) + 's'
  : Math.floor(s / 60) + 'm ' + Math.round(s % 60) + 's';
const ago = ts => {
  if (!ts) return '—';
  const d = Date.now() / 1000 - ts;
  return d < 60 ? Math.round(d) + 's ago' : d < 3600 ? Math.round(d / 60) + 'm ago'
    : Math.round(d / 3600) + 'h ago';
};
const pill = (v, cls) => `<span class="pill ${esc(cls ?? v)}">${esc(v)}</span>`;
/* Claim values often already carry their unit ("4.0 bar"), and appending
   `unit` on top of that rendered "4.0 bar bar" in the evidence table. */
const withUnit = (v, u) => {
  const val = String(v ?? '');
  if (!u) return val;
  return val.toLowerCase().trim().endsWith(String(u).toLowerCase().trim())
    ? val : val + ' ' + u;
};
/* Mono suits an identifier or a measurement; it makes a sentence harder to read. */
const monoish = v => !/\s/.test(String(v ?? ''));

/* Repaint only on real change. The runtime polls every three seconds; blindly
   reassigning innerHTML on each tick threw away the operator's hover, text
   selection and any half-read row. */
function paint(el, html) {
  if (!el || el.__html === html) return;
  el.__html = html; el.innerHTML = html;
  scheduleResize();
}
const emptyRow = (cols, msg) =>
  `<tr><td colspan="${cols}" class="empty row">${esc(msg)}</td></tr>`;
const skeleton = () => '<div class="skel"><i></i><i></i><i></i></div>';

async function api(path, opts) {
  const r = await fetch(path, opts);
  if (!r.ok) throw new Error((await r.text()).slice(0, 300));
  return r.json();
}
const post = (p, body) => api(p, {
  method: 'POST', headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify(body || {})
});

/* Surface a failed view instead of leaving last minute's data on screen
   looking current. A stale control plane reading is worse than a visible gap. */
function fail(el, e, cols) {
  const msg = 'Could not load this view — ' + (e && e.message ? e.message : e);
  paint(el, cols ? emptyRow(cols, msg) : `<div class="empty">${esc(msg)}</div>`);
}

function setMsg(el, text, kind) {
  el.className = 'msg' + (kind ? ' ' + kind : '');
  el.textContent = text || '';
}

const state = { view: 'work', task: null, system: null, doc: null, drawing: null,
                lastUpload: null };

/* ------------------------------------------------------- smooth scrolling */
/* Lenis drives the single scroll surface. Vendored, not fetched. */
let lenis = null, resizePending = false;
function scheduleResize() {
  if (!lenis || resizePending) return;
  resizePending = true;
  requestAnimationFrame(() => { resizePending = false; lenis.resize(); });
}
function initScroll() {
  const reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  if (reduced || typeof window.Lenis !== 'function') return;
  lenis = new Lenis({
    wrapper: $('#scroll'),
    content: $('#scrollInner'),
    duration: 1.0,
    easing: t => Math.min(1, 1.001 - Math.pow(2, -10 * t)),
    smoothWheel: true,
    wheelMultiplier: 1,
    touchMultiplier: 1.6,
  });
  const raf = t => { lenis.raf(t); requestAnimationFrame(raf); };
  requestAnimationFrame(raf);
}
function scrollTop() {
  if (lenis) lenis.scrollTo(0, { immediate: true });
  else $('#scroll').scrollTop = 0;
}

/* --------------------------------------------------------------- sections */
const META = {
  work: ['Workbench',
    'Submit work to the agent and follow every step it takes.'],
  tasks: ['Tasks',
    'Every task the control plane has admitted, with the model it was routed to and why.'],
  runtime: ['Resource runtime',
    'Admission, queueing and VRAM residency on this appliance.'],
  models: ['Models',
    'The local registry, what each model costs to hold resident, and how the router scores it.'],
  docs: ['Documents',
    'Everything indexed locally. Agents reach these by tool, never by host path.'],
  drawings: ['Drawings',
    'Symbols, tags and a connectivity graph, each edge carrying its own confidence.'],
  evidence: ['Evidence',
    'Every claim classified, and every derived number traced to a reproducible calculation.'],
  deliverables: ['Deliverables',
    'Generated files, each recorded with the hash it was written under.'],
  approvals: ['Approvals',
    'Actions the policy engine will not take without a human decision.'],
  sovereignty: ['Sovereignty',
    'The five enforcement layers, and the log of every connection that was refused.'],
  audit: ['Audit',
    'A hash-chained record. Any edit to history breaks verification.'],
};

$$('#tabs button').forEach(b => b.onclick = () => {
  state.view = b.dataset.v;
  $$('#tabs button').forEach(x => {
    x.classList.toggle('on', x === b);
    x.setAttribute('aria-current', x === b ? 'page' : 'false');
  });
  $$('main .view').forEach(v => v.classList.toggle('on', v.id === 'v-' + state.view));
  const [t, d] = META[state.view] || ['', ''];
  $('#viewTitle').textContent = t;
  $('#viewDesc').textContent = d;
  scrollTop();
  refreshView();
});

/* ----------------------------------------------------------------- header */
function paintGauges(live) {
  const g = live?.gpu || {}, m = live?.memory || {};
  const set = (id, txtId, used, total, label) => {
    const pct = total ? Math.min(100, 100 * used / total) : 0;
    const el = $(id);
    el.classList.toggle('warn', pct > 75);
    el.classList.toggle('bad', pct > 92);
    el.querySelector('.bar > i').style.width = pct + '%';
    $(txtId).textContent = label;
  };
  set('#mVram', '#vramTxt', g.used_mb, g.total_mb,
    g.total_mb ? `${(g.used_mb / 1024).toFixed(1)} / ${(g.total_mb / 1024).toFixed(1)} GB` : '—');
  set('#mRam', '#ramTxt', m.used_mb, m.total_mb,
    m.total_mb ? `${(m.used_mb / 1024).toFixed(1)} / ${(m.total_mb / 1024).toFixed(1)} GB` : '—');
  /* The GPU meter reads GPU utilisation. It previously filled from the CPU
     figure while printing the GPU one, so the bar and its own label disagreed. */
  set('#mGpu', '#gpuTxt', g.util_pct, 100, `${Math.round(g.util_pct || 0)}%`);
}

async function loadSystem() {
  const s = await api('/api/system'); state.system = s;
  $('#orgName').textContent = s.org.name;
  $('#orgUnit').textContent = s.org.unit + ' · sovereign on-premise workbench';
  $('#policyBadge').innerHTML = 'Policy <b>' + esc(s.policy_mode) + '</b>';

  const nft = s.sovereignty.nftables || {};
  const eg = $('#egressBadge');
  eg.textContent = 'Egress default-deny' + (nft.loaded ? ' · nftables' : '');
  eg.className = 'chip ' + (s.sovereignty.app_guard ? 'ok' : 'bad');
  eg.title = s.sovereignty.app_guard
    ? 'In-process application guard installed'
    : 'Application guard is NOT installed';

  setCount('#cDocs', s.counts.documents);
  setCount('#cArt', s.counts.artifacts);
  setCount('#cNet', s.counts.network_events);

  const wf = $('#workflow');
  if (!wf.options.length) {
    wf.innerHTML = s.workflows.map(w =>
      `<option value="${esc(w)}">${esc(w.replace(/_/g, ' '))}</option>`).join('');
    wf.value = 'general';
  }
  paintGauges(s.live);
}
function setCount(sel, n, alert) {
  const el = $(sel); if (!el) return;
  el.textContent = n ? String(n) : '';
  el.classList.toggle('alert', !!(alert && n));
}

/* -------------------------------------------------------------- workbench */
function describeFile(f) {
  const d = $('#drop'), t = $('#dropTxt');
  d.classList.toggle('has', !!f);
  t.innerHTML = f
    ? `<b>${esc(f.name)}</b><span>${fmtBytes(f.size)} · indexed and attached on submit</span>`
    : '<b>Choose a file or drop it here</b><span>Indexed automatically when you submit</span>';
  const old = d.querySelector('.clear');
  if (old) old.remove();
  if (f) {
    const btn = document.createElement('button');
    btn.className = 'clear'; btn.type = 'button';
    btn.setAttribute('aria-label', 'Remove attachment');
    btn.textContent = '×';
    btn.onclick = ev => {
      ev.preventDefault(); ev.stopPropagation();
      $('#file').value = ''; state.lastUpload = null;
      describeFile(null); setMsg($('#submitMsg'), '');
    };
    d.appendChild(btn);
  }
}

$('#file').onchange = () => {
  state.lastUpload = null;
  describeFile($('#file').files[0] || null);
  setMsg($('#submitMsg'), '');
};

/* Dropping a file is the gesture operators reach for first. */
(() => {
  const d = $('#drop');
  ['dragenter', 'dragover'].forEach(ev => d.addEventListener(ev, e => {
    e.preventDefault(); d.classList.add('over');
  }));
  ['dragleave', 'drop'].forEach(ev => d.addEventListener(ev, e => {
    e.preventDefault(); d.classList.remove('over');
  }));
  d.addEventListener('drop', e => {
    const f = e.dataTransfer?.files?.[0];
    if (!f) return;
    const dt = new DataTransfer(); dt.items.add(f);
    $('#file').files = dt.files;
    state.lastUpload = null;
    describeFile(f);
    setMsg($('#submitMsg'), '');
  });
})();

async function uploadChosenFile() {
  /* Index the file in the picker and return an attachment descriptor.
     Returns null when no file is selected. */
  const f = $('#file').files[0];
  if (!f) return null;
  setMsg($('#submitMsg'), `Indexing ${f.name} …`);
  const fd = new FormData(); fd.append('file', f);
  const r = await api('/api/upload', { method: 'POST', body: fd });
  setMsg($('#submitMsg'),
    `Indexed ${r.title} — ${r.pages} pages, ${r.chunks ?? 0} chunks, class ${r.doc_class}`
    + (r.failed_pages?.length ? `. Pages ${r.failed_pages} could not be read` : '')
    + (r.reused ? ' (already indexed)' : ''), 'good');
  loadSystem();
  return {
    doc_id: r.doc_id, title: r.title, pages: r.pages,
    kind: (r.doc_class === 'drawing' ? 'drawing' : 'document'),
  };
}

$('#submitBtn').onclick = async () => {
  const prompt = $('#prompt').value.trim();
  if (!prompt) {
    setMsg($('#submitMsg'), 'Describe the task first.', 'err');
    $('#prompt').focus();
    return;
  }
  const btn = $('#submitBtn');
  btn.disabled = true; btn.classList.add('busy');
  try {
    /* A file sitting in the picker is one the operator plainly intends to use.
       Requiring a separate "Index only" click before Submit meant the file was
       silently dropped and the agent was asked about a document it had never
       been given. Submit indexes it first. */
    if (!state.lastUpload && $('#file').files[0]) {
      state.lastUpload = await uploadChosenFile();
    }
    const body = { prompt, workflow: $('#workflow').value };
    if ($('#priority').value) body.priority = $('#priority').value;
    if (state.lastUpload) body.attachments = [state.lastUpload];

    const r = await post('/api/tasks', body);
    state.task = r.task_id;
    setMsg($('#submitMsg'),
      (state.lastUpload ? `Attached ${state.lastUpload.title}. ` : '')
      + `Submitted ${r.task_id}.`
      + (r.injection_scan?.detected
        ? ' Note: the prompt itself contains instruction-like text, which has been recorded.'
        : ''), 'good');
    $('#prompt').value = '';
    $('#file').value = '';
    state.lastUpload = null;
    describeFile(null);
    loadTask(); loadRecent();
  } catch (e) {
    setMsg($('#submitMsg'), 'Error: ' + e.message, 'err');
  }
  btn.disabled = false; btn.classList.remove('busy');
};

$('#uploadBtn').onclick = async () => {
  if (!$('#file').files[0]) {
    setMsg($('#submitMsg'), 'Choose a file first.', 'err');
    return;
  }
  const btn = $('#uploadBtn');
  btn.disabled = true; btn.classList.add('busy');
  try {
    state.lastUpload = await uploadChosenFile();
  } catch (e) {
    setMsg($('#submitMsg'), 'Upload failed: ' + e.message, 'err');
  }
  btn.disabled = false; btn.classList.remove('busy');
};

async function loadRecent() {
  try {
    const { tasks } = await api('/api/tasks?limit=12');
    setCount('#cTasks', tasks.length);
    /* Open on the most recent task rather than on an empty right-hand pane. */
    if (!state.task && tasks.length) { state.task = tasks[0].id; loadTask(); }
    paint($('#recentTasks'), tasks.length ? tasks.map(t => `
      <li class="sel ${t.id === state.task ? 'on' : ''}" data-id="${esc(t.id)}"
          tabindex="0" role="button">
        <b>${esc(t.title)}</b>
        <span class="meta">${pill(t.state)} ${pill(t.priority)}
          <span>${esc(t.task_type || '')}</span>
          <span>${esc(t.selected_model || 'unrouted')}</span>
          <span>${ago(t.created_at)}</span></span></li>`).join('')
      : '<li class="empty">No tasks yet. Submit one to begin.</li>');
    $$('#recentTasks .sel').forEach(li => {
      const open = () => { state.task = li.dataset.id; loadTask(); loadRecent(); };
      li.onclick = open;
      li.onkeydown = e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); open(); } };
    });
  } catch (e) { fail($('#recentTasks'), e); }
}

async function loadTask() {
  if (!state.task) {
    paint($('#trace'), '<li class="empty">Select a task, or submit one, to see its trace.</li>');
    paint($('#traceMeta'), '');
    $('#traceTitle').textContent = '';
    $('#answerCard').hidden = true;
    return;
  }
  let d;
  try { d = await api('/api/tasks/' + state.task); }
  catch (e) { fail($('#trace'), e); return; }
  const t = d.task;

  $('#traceTitle').textContent = t.title;
  paint($('#traceMeta'),
    `<div class="actions" style="margin-bottom:14px">
       ${pill(t.state)} ${pill(t.priority)}
       <span class="chip">Model <b>${esc(t.selected_model || '—')}</b></span>
       <span class="chip">Steps <b>${t.steps_used || 0}</b></span>
       <span class="chip">Queued <b>${fmtSec(t.queue_wait_s)}</b></span>
       <span class="chip">Ran <b>${fmtSec(t.runtime_s)}</b></span>
       ${t.state === 'RUNNING' ? '<button class="btn sm" data-a="pause">Pause</button>' : ''}
       ${t.state === 'PAUSED' ? '<button class="btn sm primary" data-a="resume">Resume</button>' : ''}
       ${['RUNNING', 'QUEUED', 'PAUSED'].includes(t.state)
        ? '<button class="btn sm danger" data-a="cancel">Terminate</button>' : ''}
     </div>`
    + (t.routing_reason ? `<p class="note" style="margin:0 0 6px"><b>Routing.</b>
        ${esc(t.routing_reason)}</p>` : '')
    + (t.state_reason ? `<p class="note" style="margin:0 0 6px"><b>State.</b>
        ${esc(t.state_reason)}</p>` : ''));
  $$('#traceMeta button').forEach(b => b.onclick = async () => {
    b.disabled = true;
    try { await post(`/api/tasks/${state.task}/${b.dataset.a}`, {}); } finally { loadTask(); }
  });

  paint($('#trace'), d.events.length ? d.events.map(e => {
    const det = String(e.detail ?? '');
    const long = det.length > 320;
    return `<li class="${esc(e.kind)}">
      <div class="k">${esc(e.kind.replace(/_/g, ' '))}</div>
      <div class="lab">${esc(e.label || '')}</div>
      ${det ? `<div class="det${long ? ' clip' : ''}">${esc(det)}</div>
        ${long ? '<button class="more" type="button">Show more</button>' : ''}` : ''}
    </li>`;
  }).join('') : '<li class="empty">No trace recorded yet.</li>');
  $$('#trace .more').forEach(b => b.onclick = () => {
    const det = b.previousElementSibling;
    const open = det.classList.toggle('clip') === false;
    b.textContent = open ? 'Show less' : 'Show more';
    scheduleResize();
  });

  const res = t.result || {};
  if (res.summary) {
    $('#answerCard').hidden = false;
    $('#answer').textContent = res.summary;
    const c = res.provenance?.counts || {};
    paint($('#provSummary'), ['A', 'B', 'C', 'D'].map(k =>
      `<span class="chip"><span class="cls ${k}">${k}</span> <b>${c[k] || 0}</b></span>`).join('')
      + (c.D ? ` <span class="pill DENIED">${c.D} unsupported value(s) refused</span>` : '')
      + (res.artifacts?.length
        ? ' ' + res.artifacts.map(a =>
          `<a class="btn sm" href="/api/artifacts/${esc(a.id)}/download">↓ ${esc(a.name)}</a>`).join(' ')
        : ''));
  } else { $('#answerCard').hidden = true; }
}

/* -------------------------------------------------------------- all tasks */
async function viewTasks() {
  try {
    const { tasks } = await api('/api/tasks?limit=200');
    paint($('#taskRows'), tasks.length ? tasks.map(t => `<tr>
      <td><a href="#" data-id="${esc(t.id)}"><b>${esc(t.title)}</b></a>
        <span class="sub mono">${esc(t.id)}</span></td>
      <td>${esc(t.task_type || '—')}</td>
      <td>${pill(t.priority)}</td>
      <td>${pill(t.state)}${t.state_reason
        ? `<span class="sub">${esc(t.state_reason.slice(0, 90))}</span>` : ''}</td>
      <td class="mono">${esc(t.selected_model || '—')}</td>
      <td class="num">${t.steps_used || 0}</td>
      <td class="num">${fmtSec(t.queue_wait_s)}</td>
      <td class="num">${fmtSec(t.runtime_s)}</td>
      <td><button class="btn sm" data-id="${esc(t.id)}">Open</button></td></tr>`).join('')
      : emptyRow(9, 'No tasks recorded yet.'));
    $$('#taskRows [data-id]').forEach(a => a.onclick = ev => {
      ev.preventDefault(); state.task = a.dataset.id;
      $$('#tabs button')[0].click(); loadTask(); loadRecent();
    });
  } catch (e) { fail($('#taskRows'), e, 9); }
}

/* ---------------------------------------------------------------- runtime */
async function viewRuntime() {
  let r;
  try { r = await api('/api/runtime'); }
  catch (e) { fail($('#runRows'), e, 4); return; }

  paint($('#runRows'), r.running.length ? r.running.map(t => `<tr>
    <td><b>${esc(t.title)}</b></td><td>${pill(t.priority)}</td>
    <td class="mono">${esc(t.selected_model || '—')}</td>
    <td class="num">${t.steps_used || 0}</td></tr>`).join('')
    : emptyRow(4, 'No agent is running.'));

  paint($('#queueRows'), r.queued.length ? r.queued.map(t => `<tr>
    <td><b>${esc(t.title)}</b></td><td>${pill(t.priority)}</td>
    <td class="mono num">${(t.effective_priority ?? 0).toFixed(2)}</td>
    <td>${esc(t.state_reason || '')}</td></tr>`).join('')
    : emptyRow(4, 'The queue is empty.'));

  const res = r.residency;
  paint($('#residency'), res.resident.length ? `<div class="tw"><table><thead><tr>
    <th>Model</th><th>VRAM</th><th>On GPU</th><th>Dwell</th><th>Served</th>
    <th>Evictable</th></tr></thead><tbody>` + res.resident.map(m => `<tr>
      <td class="mono"><b>${esc(m.model)}</b></td>
      <td class="num">${Math.round(m.vram_mb)} MB</td>
      <td class="num">${Math.round(m.gpu_fraction * 100)}%</td>
      <td class="num">${Math.round(m.dwell_s)}s</td>
      <td class="num">${m.tasks_served}</td>
      <td>${m.evictable ? pill('yes', 'COMPLETED') : pill('dwell floor', 'PAUSED')}</td>
    </tr>`).join('') + '</tbody></table></div>'
    : '<div class="empty">No model is currently resident.</div>');
  $('#residencyNote').textContent =
    `${Math.round(res.free_vram_mb)} MB VRAM free of ${Math.round(res.total_vram_mb)} MB ` +
    `(${Math.round(res.usable_vram_mb)} MB allocatable). A resident model may not be ` +
    `evicted before ${res.min_dwell_s}s of dwell, which is what stops the scheduler oscillating.`;

  paint($('#limits'), Object.entries(r.limits).map(([k, v]) =>
    `<dt>${esc(k.replace(/_/g, ' '))}</dt>
     <dd class="${monoish(v) ? 'mono' : ''}">${esc(v)}</dd>`).join(''));

  paint($('#admissionRows'), r.admissions.length ? r.admissions.slice().reverse().map(a => `<tr>
    <td>${pill(a.outcome, a.outcome === 'ADMIT' ? 'COMPLETED'
      : a.outcome === 'REJECT' ? 'FAILED' : 'PAUSED')}</td>
    <td><b>${esc(a.title || '')}</b></td><td>${pill(a.priority)}</td>
    <td class="mono">${esc(a.model || '—')}</td>
    <td>${esc(a.reason || '')}</td></tr>`).join('')
    : emptyRow(5, 'No admission decisions yet.'));
}

/* ----------------------------------------------------------------- models */
async function viewModels() {
  let m;
  try { m = await api('/api/models'); }
  catch (e) { fail($('#modelRows'), e, 9); return; }

  const health = Object.fromEntries(m.health.map(h => [h.model, h]));
  const resident = new Set(m.residency.resident.map(x => x.model));
  paint($('#modelRows'), m.models.map(c => `<tr>
    <td class="wrap-prose"><b>${esc(c.name)}</b>${resident.has(c.name)
      ? ' ' + pill('resident', 'COMPLETED') : ''}
      <span class="sub">${esc(c.notes || '')}</span>
      <span class="sub mono">adapter ${esc(c.prompt_adapter)}</span></td>
    <td>${esc(c.role)}</td><td>${esc(c.backend)}</td>
    <td class="num">${Math.round(c.residency_mb)} MB</td>
    <td class="num">${c.kv_mb_per_1k ? c.kv_mb_per_1k + ' MB' : '—'}</td>
    <td class="num">${c.cold_load_s}s</td>
    <td class="num">${c.profile?.decode_tps ? c.profile.decode_tps.toFixed(1) + ' t/s' : '—'}</td>
    <td>${c.enabled ? pill(health[c.name]?.status || 'unknown',
      health[c.name]?.status === 'healthy' ? 'COMPLETED' : 'QUEUED')
      : pill('not served', 'FAILED')}</td>
    <td><div class="actions">
      <button class="btn sm" data-m="${esc(c.name)}" data-a="pin">Pin</button>
      <button class="btn sm" data-m="${esc(c.name)}" data-a="evict">Evict</button>
    </div></td></tr>`).join('') || emptyRow(9, 'No models registered.'));
  $$('#modelRows [data-m]').forEach(b => b.onclick = async () => {
    b.disabled = true; b.classList.add('busy');
    try { await post(`/api/models/${b.dataset.m}/residency`, { action: b.dataset.a }); }
    finally { b.disabled = false; b.classList.remove('busy'); viewModels(); }
  });

  const axes = ['text', 'vision', 'reasoning', 'coding', 'extraction',
    'long_context', 'tool_use', 'speed', 'structured'];
  paint($('#capHead'), '<th>Model</th>' + axes.map(a =>
    `<th>${esc(a.replace('_', ' '))}</th>`).join(''));
  paint($('#capRows'), m.models.map(c => `<tr><td class="mono"><b>${esc(c.name)}</b></td>`
    + axes.map(a => `<td class="num">${(c.caps?.[a] ?? 0).toFixed(2)}</td>`).join('')
    + '</tr>').join(''));

  const hw = state.system?.hardware || {};
  paint($('#hwProfile'), [
    ['GPU', `${hw.gpu?.name || '—'} · ${Math.round(hw.gpu?.total_mb || 0)} MB VRAM`],
    ['Allocatable VRAM', `${Math.round(hw.usable_vram_mb || 0)} MB`],
    ['CPU', `${hw.cpu?.model || '—'} (${hw.cpu?.logical || 0} threads)`],
    ['System RAM', `${((hw.memory?.total_mb || 0) / 1024).toFixed(1)} GB`],
    ['PCIe', `gen ${hw.gpu?.pcie_gen_max || '?'} x${hw.gpu?.pcie_width_max || '?'} · about ${hw.pcie_est_gbs || '?'} GB/s`],
    ['Data filesystem', `${hw.data_fs || '—'} · ${hw.disk_free_gb || 0} GB free`],
    ['Sandbox engines', (hw.sandbox_engines || []).join(', ') || 'none'],
    ['OCR', `${hw.ocr?.version || 'none'} · ${(hw.ocr?.langs || []).join(', ')}`],
    ['Backends', Object.entries(hw.backends || {}).map(([k, v]) =>
      `${k}: ${v.status}`).join(' · ')],
  ].map(([k, v]) => `<dt>${esc(k)}</dt><dd class="mono">${esc(v)}</dd>`).join(''));
}

/* -------------------------------------------------------------- documents */
async function viewDocs() {
  let documents;
  try { ({ documents } = await api('/api/documents')); }
  catch (e) { fail($('#docList'), e); return; }

  if (!state.doc && documents.length) state.doc = documents[0].id;
  paint($('#docList'), documents.length ? documents.map(d => `
    <li class="sel ${d.id === state.doc ? 'on' : ''}" data-id="${esc(d.id)}"
        tabindex="0" role="button">
      <b>${esc(d.title)}</b>
      <span class="meta">${pill(d.doc_class || 'other')}
        ${pill(d.status, d.status === 'READY' ? 'COMPLETED' : 'FAILED')}
        <span>${d.pages} pages</span>
        <span>${esc((d.meta?.extractors || []).join(', '))}</span></span>
      ${d.status_detail ? `<span class="sub">${esc(d.status_detail)}</span>` : ''}</li>`).join('')
    : '<li class="empty">Nothing indexed yet.</li>');

  if (!state.doc) {
    paint($('#docDetail'), '<div class="empty">Select a document to read its pages.</div>');
  }
  $$('#docList .sel').forEach(li => {
    const open = () => { state.doc = li.dataset.id; viewDocs(); showDoc(li.dataset.id); };
    li.onclick = open;
    li.onkeydown = e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); open(); } };
  });
  if (state.doc) showDoc(state.doc);
}

async function showDoc(id) {
  try {
    const p = await api(`/api/documents/${id}/pages`);
    paint($('#docDetail'), p.pages.length ? p.pages.map(pg => `
      <div class="section">
        <div class="section-head"><h2>Page ${pg.page_no}</h2>
          <span class="aside">${esc(pg.extractor)} · confidence ${Math.round(pg.ocr_conf || 0)}</span></div>
        ${pg.has_image ? `<img src="/api/documents/${esc(id)}/page/${pg.page_no}/image"
          alt="Page ${pg.page_no}" loading="lazy"
          style="max-width:100%;border-radius:6px;border:1px solid var(--line);margin-bottom:12px">` : ''}
        <pre class="out">${esc(pg.text || '(no text extracted)')}</pre></div>`).join('')
      : '<div class="empty">This document has no readable pages.</div>');
  } catch (e) { fail($('#docDetail'), e); }
}

/* --------------------------------------------------------------- drawings */
async function viewDrawings() {
  let drawings;
  try { ({ drawings } = await api('/api/drawings')); }
  catch (e) { fail($('#dwgList'), e); return; }

  setCount('#cDwg', drawings.length);
  if (!state.drawing && drawings.length) state.drawing = drawings[0].id;
  paint($('#dwgList'), drawings.length ? drawings.map(d => `
    <li class="sel ${d.id === state.drawing ? 'on' : ''}" data-id="${esc(d.id)}"
        tabindex="0" role="button">
      <b>${esc(d.title)}</b>
      <span class="meta">${pill(d.source_kind)}
        <span>${d.summary?.symbols || 0} symbols</span>
        <span>${d.summary?.confirmed_connectivity || 0} confirmed connections</span></span></li>`).join('')
    : '<li class="empty">No drawings analysed yet.</li>');

  if (!state.drawing) {
    paint($('#dwgDetail'), '<div class="empty">Select a drawing to inspect it.</div>');
  }
  $$('#dwgList .sel').forEach(li => {
    const open = () => { state.drawing = li.dataset.id; viewDrawings(); showDrawing(li.dataset.id); };
    li.onclick = open;
    li.onkeydown = e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); open(); } };
  });
  if (state.drawing) showDrawing(state.drawing);
}

async function showDrawing(id) {
  let d;
  try { d = await api('/api/drawings/' + id); }
  catch (e) { fail($('#dwgDetail'), e); return; }

  const colour = s => s === 'CONFIRMED' ? '#15803d'
    : s === 'PROBABLE' ? '#b45309' : '#be123c';
  const svg = `<svg viewBox="0 0 ${d.width} ${d.height}" preserveAspectRatio="none" aria-hidden="true">
    ${d.edges.map(e => `<polyline points="${e.polyline.map(p => p.join(',')).join(' ')}"
        fill="none" stroke="${colour(e.status)}" stroke-width="2.2"
        stroke-dasharray="${e.line_type === 'instrument_signal' ? '5,4' : '0'}"
        opacity=".85"/>`).join('')}
    ${d.symbols.map(s => `<rect x="${s.bbox[0]}" y="${s.bbox[1]}"
        width="${s.bbox[2] - s.bbox[0]}" height="${s.bbox[3] - s.bbox[1]}"
        fill="none" stroke="#1d4ed8" stroke-width="1.4" opacity=".9"/>
      ${s.tag ? `<text x="${s.bbox[0]}" y="${s.bbox[1] - 3}" fill="#1d4ed8"
        font-size="9" font-family="monospace">${esc(s.tag)}</text>` : ''}`).join('')}
  </svg>`;

  const byStatus = g => d.edges.filter(e => e.status === g);
  const nameOf = i => (d.symbols.find(s => s.id.endsWith(i))?.tag)
    || (d.symbols.find(s => s.id.endsWith(i))?.label) || i;

  paint($('#dwgDetail'), `
    <div class="section">
      <div class="section-head"><h2>${esc(d.title)}</h2>
        <span class="aside">${esc(d.source_kind)} source</span></div>
      <div class="dwg"><img src="/api/drawings/${esc(id)}/image" alt="${esc(d.title)}">${svg}</div>
      <div class="dwg-key">
        <span style="color:#15803d"><i>━</i>Confirmed</span>
        <span style="color:#b45309"><i>━</i>Probable</span>
        <span style="color:#be123c"><i>━</i>Unresolved</span>
        <span style="color:#1d4ed8"><i>▭</i>Detected symbol</span></div>
    </div>

    <div class="section">
      <div class="section-head"><h2>Equipment and instruments</h2>
        <span class="aside">${d.symbols.length} detected</span></div>
      <div class="tw"><table><thead><tr><th>Tag</th><th>Class</th><th>Confidence</th>
        <th>How it was identified</th></tr></thead><tbody>
        ${d.symbols.map(s => `<tr><td class="mono"><b>${esc(s.tag || '—')}</b></td>
          <td>${esc(s.sym_class)}</td><td class="num">${(s.confidence || 0).toFixed(2)}</td>
          <td>${esc(s.method)}</td></tr>`).join('') || emptyRow(4, 'No symbols detected.')}
      </tbody></table></div></div>

    <div class="section">
      <div class="section-head"><h2>Connectivity</h2></div>
      ${['CONFIRMED', 'PROBABLE', 'UNRESOLVED'].map(g => {
        const rows = byStatus(g);
        if (!rows.length) return '';
        return `<div style="margin-bottom:24px">
          <div class="actions" style="margin-bottom:10px">${pill(g)}
            <span style="font-family:var(--alt);font-size:12px;color:var(--muted)">${
              g === 'CONFIRMED' ? 'established by geometry — may be stated as fact'
              : g === 'PROBABLE' ? 'interpretation — must be labelled as such'
              : 'the drawing does not establish this — CANNOT DETERMINE'}</span></div>
          <div class="tw"><table><tbody>${rows.map(e => `<tr>
            <td class="mono"><b>${esc(nameOf(e.src))}</b> → <b>${esc(nameOf(e.dst))}</b></td>
            <td>${esc(e.line_type)}</td>
            <td>${esc(e.rationale || '')}</td></tr>`).join('')}
          </tbody></table></div></div>`;
      }).join('')}</div>

    ${(d.summary?.warnings || []).length ? `<div class="section">
      <div class="section-head"><h2>What this reading does not establish</h2></div>
      <div class="panel inset"><ul style="margin:0;padding-left:20px;font-size:13px;color:var(--ink-2)">
        ${d.summary.warnings.map(w => `<li style="margin-bottom:6px">${esc(w)}</li>`).join('')}
      </ul></div></div>` : ''}`);
}

$('#dwgBtn').onclick = async () => {
  const b = $('#dwgBtn');
  b.disabled = true; b.classList.add('busy');
  setMsg($('#dwgMsg'), 'Analysing …');
  try {
    const r = await post('/api/drawings/analyse',
      { path: $('#dwgPath').value, use_vlm: false });
    state.drawing = r.drawing_id;
    setMsg($('#dwgMsg'),
      `${r.summary.symbols} symbols, ${r.summary.confirmed_connectivity} confirmed connections.`,
      'good');
    viewDrawings();
  } catch (e) { setMsg($('#dwgMsg'), 'Failed: ' + e.message, 'err'); }
  b.disabled = false; b.classList.remove('busy');
};

/* --------------------------------------------------------------- evidence */
async function viewEvidence() {
  try {
    const tasks = (await api('/api/tasks?limit=25')).tasks;
    const head = tasks.slice(0, 8);
    if (!head.length) {
      paint($('#claimRows'), emptyRow(5, 'No claims recorded yet.'));
      paint($('#calcRows'), emptyRow(6, 'No calculations recorded.'));
      return;
    }
    /* Fetched together rather than one after another: eight sequential round
       trips left the table blank for noticeably long on a loaded appliance. */
    const details = await Promise.all(head.map(t =>
      api('/api/tasks/' + t.id).then(d => ({ d, t })).catch(() => null)));

    const claims = [], calcs = [];
    for (const r of details) {
      if (!r) continue;
      r.d.claims.forEach(c => claims.push({ ...c, task: r.t.title }));
      r.d.calculations.forEach(c => calcs.push({ ...c, task: r.t.title }));
    }

    paint($('#claimRows'), claims.length ? claims.slice(0, 250).map(c => `<tr>
      <td><span class="cls ${esc(c.ev_class)}">${esc(c.ev_class)}</span></td>
      <td class="mono nowrap"><b>${esc(withUnit(c.value, c.unit))}</b></td>
      <td>${esc((c.statement || '').slice(0, 200))}</td>
      <td>${esc((c.rationale || '').slice(0, 160))}</td>
      <td>${esc(c.task)}</td></tr>`).join('')
      : emptyRow(5, 'No claims recorded yet.'));

    paint($('#calcRows'), calcs.length ? calcs.map(c => {
      let steps = {}; try { steps = JSON.parse(c.steps || '{}'); } catch (e) { }
      const ver = steps.inputs_verified !== false;
      return `<tr><td class="mono">${esc(c.id)}</td><td>${esc(c.label || '')}</td>
        <td class="mono">${esc(c.expression)}</td>
        <td class="mono">${esc(steps.substituted || '')}</td>
        <td class="mono"><b>${esc(c.result ?? '—')}</b> ${esc(c.unit || '')}</td>
        <td>${ver ? pill('verified', 'COMPLETED')
          : pill('UNVERIFIED', 'FAILED') + `<span class="sub">${
            esc((steps.unverified_inputs || []).join(', '))}</span>`}</td></tr>`;
    }).join('') : emptyRow(6, 'No calculations recorded.'));
  } catch (e) { fail($('#claimRows'), e, 5); }
}

/* ----------------------------------------------------------- deliverables */
async function viewDeliverables() {
  try {
    const { artifacts } = await api('/api/artifacts');
    paint($('#artRows'), artifacts.length ? artifacts.map(a => `<tr>
      <td><b>${esc(a.name)}</b></td><td>${esc(a.kind)}</td>
      <td class="num">${fmtBytes(a.bytes)}</td>
      <td class="mono">${esc((a.sha256 || '').slice(0, 16))}…</td>
      <td class="mono">${esc(a.task_id || '')}</td>
      <td>${a.exists
        ? `<a class="btn sm" href="/api/artifacts/${esc(a.id)}/download">Download</a>`
        : pill('missing', 'FAILED')}</td></tr>`).join('')
      : emptyRow(6, 'Nothing generated yet.'));
  } catch (e) { fail($('#artRows'), e, 6); }
}

/* -------------------------------------------------------------- approvals */
async function viewApprovals() {
  let a;
  try { a = await api('/api/approvals'); }
  catch (e) { fail($('#pendingApprovals'), e); return; }

  setCount('#cAppr', a.pending.length, true);
  paint($('#pendingApprovals'), a.pending.length ? a.pending.map(p => `
    <div class="appr">
      <div class="appr-head">${pill('PENDING')}<b>${esc(p.tool)}</b></div>
      <div class="appr-body">
        <pre class="out tight">${esc(p.summary)}</pre>
        <div class="actions" style="margin-top:14px">
          <button class="btn ok" data-id="${esc(p.id)}" data-ok="1">Approve</button>
          <button class="btn danger" data-id="${esc(p.id)}" data-ok="0">Reject</button>
        </div></div></div>`).join('')
    : '<div class="empty">Nothing is awaiting a decision.</div>');
  $$('#pendingApprovals [data-id]').forEach(b => b.onclick = async () => {
    $$('#pendingApprovals [data-id]').forEach(x => x.disabled = true);
    b.classList.add('busy');
    try { await post('/api/approvals/' + b.dataset.id, { approve: b.dataset.ok === '1' }); }
    finally { viewApprovals(); }
  });

  paint($('#apprRows'), a.recent.length ? a.recent.map(r => `<tr>
    <td class="mono"><b>${esc(r.tool)}</b></td>
    <td>${esc((r.summary || '').slice(0, 170))}</td>
    <td>${pill(r.state)}</td><td>${esc(r.decided_by || '—')}</td></tr>`).join('')
    : emptyRow(4, 'No decisions recorded yet.'));
}

/* ------------------------------------------------------------ sovereignty */
async function viewSovereignty() {
  let n;
  try { n = await api('/api/network'); }
  catch (e) { fail($('#netRows'), e, 8); return; }

  const nft = n.nftables || {};
  /* Each layer states whether it is actually in force. Reading five lines of
     prose to discover that layer 1 is not loaded is the kind of thing an
     operator should see at a glance. */
  paint($('#sovLayers'), [
    ['Layer 1 — host firewall', nft.loaded,
      nft.loaded ? 'nftables default-deny table loaded'
        : (nft.available
          ? 'nft present but the sovereign table is NOT loaded — run ops/egress-policy.sh as root'
          : 'nft not available on this host')],
    ['Layer 2 — application guard', n.app_guard_installed,
      n.app_guard_installed
        ? 'Every outbound connection and DNS resolution is checked in-process and attributed to a task.'
        : 'Not installed.'],
    ['Layer 3 — tool policy', true, 'Agents are granted no network tool at all.'],
    ['Layer 4 — sandbox', true, 'Code executes in an empty network namespace, with no interfaces.'],
    ['Layer 5 — audit', true, 'Every refusal is written to the hash-chained audit log.'],
    ['Permitted destinations', null,
      n.allowed_hosts.join(', ') + ' on ports ' + n.allowed_ports.join(', ')
      + ' (local inference backends only)'],
    ['Recorded attempts', null, n.counts.map(c =>
      `${c.layer}: ${c.n} ${c.result}`).join(' · ') || 'none yet'],
  ].map(([k, state, v]) => `<dt>${esc(k)}</dt><dd>${
    state === null ? '' : state ? pill('ACTIVE', 'COMPLETED') : pill('NOT ENFORCED', 'PAUSED')
  }${esc(v)}</dd>`).join(''));

  paint($('#netRows'), n.recent.length ? n.recent.map(e => `<tr>
    <td>${pill(e.result, e.result === 'ALLOWED' ? 'FAILED' : 'COMPLETED')}</td>
    <td class="mono">${esc(e.destination)}</td><td class="num">${esc(e.port ?? '')}</td>
    <td class="nowrap">${esc(e.layer)}</td>
    <td class="mono nowrap">${esc(e.process || '')}</td>
    <td class="mono nowrap">${esc(e.task_id || '')}</td>
    <td>${esc((e.detail || '').slice(0, 140))}</td>
    <td class="num">${ago(e.ts)}</td></tr>`).join('')
    : emptyRow(8, 'No egress has been attempted yet. Run the self-test to produce a denial record.'));
}

$('#selftestBtn').onclick = async () => {
  const b = $('#selftestBtn');
  b.disabled = true; b.classList.add('busy');
  try { const r = await post('/api/sovereignty/selftest'); state.task = r.task_id; }
  finally {
    setTimeout(() => {
      b.disabled = false; b.classList.remove('busy');
      viewSovereignty();
    }, 6000);
  }
};

/* ------------------------------------------------------------------ audit */
async function viewAudit() {
  let a;
  try { a = await api('/api/audit?limit=400'); }
  catch (e) { fail($('#auditRows'), e, 7); return; }

  const v = a.verification;
  paint($('#auditVerify'), v.ok
    ? `<div class="actions">${pill('CHAIN VERIFIED', 'COMPLETED')}
        <span style="font-family:var(--alt);font-size:12.5px;color:var(--muted);max-width:74ch">
          ${v.entries} entries. Each row commits to the hash of the one before it, so any
          deletion or edit of history breaks verification.
          Head <span class="mono">${esc(v.head.slice(0, 24))}…</span></span></div>`
    : `<div class="actions">${pill('CHAIN BROKEN', 'FAILED')}
        <span style="font-family:var(--alt);font-size:12.5px;color:var(--bad)">
          At entry ${v.broken_at}: ${esc(v.reason)}</span></div>`);

  paint($('#auditRows'), a.entries.length ? a.entries.map(e => `<tr>
    <td class="mono num">${e.seq}</td><td class="num">${ago(e.ts)}</td>
    <td>${esc(e.category)}</td><td class="mono">${esc(e.action)}</td>
    <td>${pill(e.outcome || 'OK', ['BLOCKED', 'OK'].includes(e.outcome) ? 'COMPLETED'
      : ['DENIED', 'FAILED', 'LEAK'].includes(e.outcome) ? 'FAILED' : 'PAUSED')}</td>
    <td class="mono">${esc(e.task_id || '')}</td>
    <td>${esc((e.detail || '').slice(0, 190))}</td></tr>`).join('')
    : emptyRow(7, 'The audit log is empty.'));
}

/* ------------------------------------------------------------------ views */
const VIEWS = {
  work: () => { loadRecent(); loadTask(); },
  tasks: viewTasks, runtime: viewRuntime, models: viewModels, docs: viewDocs,
  drawings: viewDrawings, evidence: viewEvidence, deliverables: viewDeliverables,
  approvals: viewApprovals, sovereignty: viewSovereignty, audit: viewAudit,
};
function refreshView() { (VIEWS[state.view] || (() => { }))(); }

/* ------------------------------------------------------------------- live */
function connect() {
  const es = new EventSource('/api/stream');
  es.onopen = () => {
    $('#sseDot').className = 'dot on'; $('#sseTxt').textContent = 'Live';
  };
  es.onerror = () => {
    $('#sseDot').className = 'dot off'; $('#sseTxt').textContent = 'Reconnecting…';
  };
  es.onmessage = ev => {
    let m; try { m = JSON.parse(ev.data); } catch (e) { return; }
    if (m.type === 'task_event' && m.task_id === state.task) loadTask();
    if (m.type === 'task_state') { loadRecent(); if (m.task_id === state.task) loadTask(); }
    if (m.type === 'approval') viewApprovals();
    if (m.type === 'network_event' && state.view === 'sovereignty') viewSovereignty();
    if (m.type === 'admission' && state.view === 'runtime') viewRuntime();
  };
}

async function tick() {
  try {
    const t = await api('/api/telemetry');
    paintGauges(t.latest);
    const r = await api('/api/runtime');
    const res = r.residency.resident.map(x => x.model).join(', ');
    $('#modelBadge').innerHTML = 'Resident <b>' + esc(res || 'none') + '</b>';
    if (['runtime', 'sovereignty', 'approvals'].includes(state.view)) refreshView();
  } catch (e) { /* the control plane may be restarting */ }
}

(async function boot() {
  initScroll();
  const [t, d] = META.work;
  $('#viewTitle').textContent = t; $('#viewDesc').textContent = d;
  paint($('#trace'), skeleton());
  paint($('#recentTasks'), '<li>' + skeleton() + '</li>');
  try { await loadSystem(); }
  catch (e) { setMsg($('#submitMsg'), 'Control plane unreachable: ' + e.message, 'err'); }
  connect();
  refreshView();
  setInterval(tick, 3000);
  setInterval(loadSystem, 20000);
})();
