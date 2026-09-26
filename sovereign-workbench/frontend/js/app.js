/* ClawCal workbench UI.
   Framework-free JavaScript. Every asset the page needs — script, stylesheet,
   typeface, icon — ships inside the appliance, because a workbench whose own UI
   phoned out for a file would contradict the claim it exists to make.

   This file changes presentation only. Every endpoint, request body and data
   flow is exactly as the control plane defines it. What it adds is language: the
   control plane speaks in tasks, modes and evidence classes; the operator sees
   chats, "Ask first" and "Sourced". Internal names survive in tooltips and in
   the Admin area, where they are the point. */
'use strict';

/* ================================================================ helpers */
const $  = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));
const esc = s => String(s ?? '').replace(/[&<>"']/g,
  c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const enc = encodeURIComponent;
const fmtBytes = n => n > 1e6 ? (n / 1e6).toFixed(1) + ' MB'
  : n > 1e3 ? (n / 1e3).toFixed(0) + ' kB' : (n || 0) + ' B';
const fmtSec = s => s == null ? '—' : s < 1 ? '<1 s' : s < 60 ? Math.round(s) + ' s'
  : Math.floor(s / 60) + ' min ' + Math.round(s % 60) + ' s';
const ago = ts => {
  if (!ts) return '—';
  const d = Date.now() / 1000 - ts;
  return d < 45 ? 'just now' : d < 3600 ? Math.round(d / 60) + ' min ago'
    : d < 86400 ? Math.round(d / 3600) + ' h ago' : Math.round(d / 86400) + ' d ago';
};
const icon = (id, cls) => `<svg${cls ? ` class="${cls}"` : ''}><use href="#${id}"/></svg>`;
const LOGO = '<svg class="brand-mark"><use href="#logo"/></svg>';
const CHEV = icon('i-chev', 'chev');
const cap = s => { s = String(s ?? ''); return s.charAt(0).toUpperCase() + s.slice(1); };
const human = s => cap(String(s ?? '').replace(/_/g, ' ').toLowerCase());
/* Backend prose that reads like a stack trace or a wire error is for engineers:
   it goes under "Technical details", never in the headline. */
const technical = s => /traceback|exception|error:|errno|pool\(|port=\d|https?:|[{}<>]|\w+\.\w+\(|^[A-Z][a-zA-Z]+Error\b/i.test(String(s || '')) || String(s || '').length > 220;

/* A deliberately small markdown renderer for model output.

   Everything is HTML-escaped FIRST and only then given structure, so nothing
   the model emits can introduce markup -- which matters here, because that
   text may have come from an untrusted document the agent just read. Fenced
   code is stashed before the inline rules run so it is never reformatted.
   No links: an air-gapped workbench has nowhere to send the operator. */
function mdLite(src) {
  const code = [];
  let s = esc(String(src == null ? '' : src)).replace(/\r\n?/g, '\n');

  s = s.replace(/```[^\n]*\n([\s\S]*?)```/g, (_, body) =>
    '@@C' + (code.push(body.replace(/\n+$/, '')) - 1) + '@@');
  s = s.replace(/`([^`\n]+)`/g, '<code>$1</code>');
  s = s.replace(/\*\*([^\n]+?)\*\*/g, '<strong>$1</strong>');
  s = s.replace(/(^|[\s(])\*([^*\n]+?)\*(?=[\s.,;:)]|$)/g, '$1<em>$2</em>');

  const out = [];
  let list = null, para = [];
  const closePara = () => {
    if (para.length) { out.push('<p>' + para.join('<br>') + '</p>'); para = []; }
  };
  const closeList = () => { if (list) { out.push('</' + list + '>'); list = null; } };

  for (const raw of s.split('\n')) {
    const line = raw.trimEnd();
    const ph = line.trim().match(/^@@C(\d+)@@$/);
    if (ph) {
      closePara(); closeList();
      out.push('<pre><code>' + code[+ph[1]] + '</code></pre>');
      continue;
    }
    if (!line.trim()) { closePara(); closeList(); continue; }
    if (/^\s*(---+|___+|\*\*\*+)\s*$/.test(line)) {
      closePara(); closeList(); out.push('<hr>'); continue;
    }
    const h = line.match(/^(#{1,6})\s+(.*)$/);
    if (h) {
      closePara(); closeList();
      const n = Math.min(h[1].length + 1, 4);
      out.push('<h' + n + '>' + h[2] + '</h' + n + '>');
      continue;
    }
    const b = line.match(/^\s*[-*•]\s+(.*)$/);
    const n = line.match(/^\s*\d+[.)]\s+(.*)$/);
    if (b || n) {
      closePara();
      const want = b ? 'ul' : 'ol';
      if (list !== want) { closeList(); out.push('<' + want + '>'); list = want; }
      out.push('<li>' + (b || n)[1] + '</li>');
      continue;
    }
    closeList();
    para.push(line.trim());
  }
  closePara(); closeList();
  return out.join('') || '<p></p>';
}

/* Claim values often already carry their unit ("4.0 bar"), and appending
   `unit` on top of that rendered "4.0 bar bar" in the evidence table. */
const withUnit = (v, u) => {
  const val = String(v ?? '');
  if (!u) return val;
  return val.toLowerCase().trim().endsWith(String(u).toLowerCase().trim()) ? val : val + ' ' + u;
};
const monoish = v => !/\s/.test(String(v ?? ''));

/* Repaint only on real change. The runtime polls every few seconds; blindly
   reassigning innerHTML on each tick threw away the operator's hover, text
   selection and any half-read row. */
function paint(el, html) {
  if (typeof el === 'string') el = $(el);
  if (!el || el.__html === html) return false;
  el.__html = html; el.innerHTML = html;
  return true;
}
const skeleton = () => '<div class="skel"><i></i><i></i><i></i></div>';
const emptyState = (ico, title, text, extra = '') =>
  `<div class="empty"><span class="ico">${icon(ico)}</span><b>${esc(title)}</b>${
    text ? `<p>${esc(text)}</p>` : ''}${extra}</div>`;
const emptyRow = (cols, ico, title, text) =>
  `<tr><td colspan="${cols}" class="empty-cell">${emptyState(ico, title, text)}</td></tr>`;

/* ============================================================ vocabulary */
const STATE = {
  QUEUED: ['Waiting its turn', 'wait'], ADMITTED: ['Starting', 'run'],
  RUNNING: ['Working', 'run'], PAUSED: ['Paused', 'wait'], COMPLETED: ['Done', 'ok'],
  FAILED: ["Didn't finish", 'bad'], TERMINATED: ['Stopped', ''], REJECTED: ['Not started', 'bad'],
  PENDING: ['Waiting for you', 'wait'], APPROVED: ['Allowed', 'ok'], DENIED: ["Not allowed", 'bad'],
  REJECTED_APPROVAL: ["Not allowed", 'bad'], EXPIRED: ['Expired', ''], TIMEOUT: ['Timed out', ''],
};
const statePill = (s, title) => {
  const [label, cls] = STATE[s] || [human(s), ''];
  return `<span class="pill ${cls}"${title ? ` title="${esc(title)}"` : ''}><i class="px"></i>${esc(label)}</span>`;
};
const TERMINAL = new Set(['COMPLETED', 'FAILED', 'TERMINATED', 'REJECTED']);
const LIVE = new Set(['QUEUED', 'ADMITTED', 'RUNNING', 'PAUSED']);

const PRIORITY = { CRITICAL: 'Urgent', HIGH: 'High', MEDIUM: 'Normal', LOW: 'Low', BATCH: 'When free' };
const prio = p => PRIORITY[p] || human(p || '—');

const MODES = {
  review:  { label: 'Ask first', icon: 'i-hand', note: 'ClawCal will check with you before creating files or running code.' },
  trusted: { label: 'Auto-run',  icon: 'i-bolt', note: 'ClawCal will go ahead without asking, and log everything it does.' },
  locked:  { label: 'Read only', icon: 'i-lock', note: 'ClawCal can read and answer, but will not write anything.' },
};

const OUTCOME = {
  ESTABLISHED: ['Confirmed', 'i-check'], INTERPRETED: ['Interpreted', 'i-eye'],
  CANNOT_DETERMINE: ["Couldn't determine", 'i-x'], DEGRADED: ['Limited mode', 'i-info'],
};
const outcomeBadge = (o, why) => o && OUTCOME[o]
  ? `<span class="outcome ${esc(o)}" title="${esc(why || o.replace('_', ' '))}">${icon(OUTCOME[o][1])}${OUTCOME[o][0]}</span>` : '';

const EV = {
  A: ['Sourced', 'i-docs', 'Found in a document. Opens the page with the region highlighted.'],
  B: ['Calculated', 'i-calc', 'Worked out with a formula you can re-run.'],
  C: ['Judgement', 'i-eye', "ClawCal's reading of the evidence. Check it."],
  D: ['Not supported', 'i-x', 'No source supports this, so it was refused rather than guessed.'],
};
const evTag = (cls, attrs = '') => {
  const e = EV[cls] || [cls, 'i-info'];
  return `<button type="button" class="ev ${esc(cls)}" ${attrs}>${icon(e[1])}${esc(e[0])}</button>`;
};

const TOOL = {
  search_knowledge: ['Searched your library', 'i-search', 'search your library'],
  list_documents: ['Listed documents', 'i-docs', 'list documents'],
  read_document_page: ['Read a page', 'i-docs', 'read a document page'],
  extract_document_values: ['Pulled values from a document', 'i-docs', 'pull values from a document'],
  analyse_drawing: ['Read the drawing', 'i-draw', 'analyse a drawing'],
  trace_drawing_connection: ['Traced a line on the drawing', 'i-draw', 'trace a connection'],
  calculator: ['Calculated', 'i-calc', 'run a calculation'],
  spreadsheet_read: ['Read the spreadsheet', 'i-sheet', 'read a spreadsheet'],
  spreadsheet_edit: ['Edited a copy of the spreadsheet', 'i-sheet', 'edit a copy of the spreadsheet'],
  generate_report: ['Drafted a report', 'i-out', 'create a Word report'],
  generate_spreadsheet: ['Built a spreadsheet', 'i-sheet', 'create a spreadsheet'],
  generate_presentation: ['Built a presentation', 'i-out', 'create a presentation'],
  generate_approval_note: ['Drafted an approval note', 'i-out', 'create an approval note'],
  request_human_approval: ['Asked you to decide', 'i-hand', 'ask you a question'],
  human_decision: ['Asked you to decide', 'i-hand', 'ask you a question'],
  run_code: ['Ran code in the sandbox', 'i-chip', 'run code in the sealed sandbox'],
  read_file: ['Read a file', 'i-docs', 'read a file'],
  write_file: ['Wrote a file', 'i-out', 'write a file'],
  list_files: ['Listed files', 'i-list', 'list files'],
  sovereignty_selftest: ['Tested the data seal', 'i-shield', 'test the data seal'],
};
const toolOf = name => TOOL[name] || [human(name || 'Tool'), 'i-spark', human(name || 'use a tool').toLowerCase()];

/* file-type chip */
const extOf = s => { const m = String(s || '').toLowerCase().match(/\.([a-z0-9]{2,5})$/); return m ? m[1] : ''; };
const ftChip = (name, kind) => {
  let e = extOf(name) || String(kind || 'doc').toLowerCase().slice(0, 4);
  if (e === 'spreadsheet' || e === 'spre') e = 'xlsx';
  if (e === 'draw' || e === 'drawing') e = 'dwg';
  return `<span class="ft ${esc(e)}">${esc(e.slice(0, 4))}</span>`;
};

/* ======================================================= identity & API
   On a workstation bound to loopback the OS login is the authentication and
   no token is needed. On a server (SOVEREIGN_AUTH=token) every /api call
   carries a bearer token; it lives in this browser's storage only. */
const AUTH = { token: '' };
try { AUTH.token = localStorage.getItem('clawcal.token') || ''; } catch (_) { }

const nativeFetch = window.fetch.bind(window);
let signInShown = false;
async function authed(input, init) {
  const url = typeof input === 'string' ? input : (input && input.url) || '';
  let opts = init || {};
  if (AUTH.token && url.startsWith('/api/')) {
    const h = new Headers(opts.headers || {});
    h.set('Authorization', 'Bearer ' + AUTH.token);
    opts = Object.assign({}, opts, { headers: h });
  }
  const r = await nativeFetch(input, opts);
  if (r.status === 401 && !signInShown) { signInShown = true; signIn(); }
  return r;
}

function signIn(msg) {
  const d = $('#signIn');
  if (!d || d.open) return;
  $('#signInMsg').textContent = msg || '';
  d.showModal();
  $('#tokenInput').focus();
}
$('#signInForm').addEventListener('submit', e => {
  const t = $('#tokenInput').value.trim();
  if (!t) { e.preventDefault(); $('#signInMsg').textContent = 'Enter your access key to continue.'; return; }
  try { localStorage.setItem('clawcal.token', t); } catch (_) { }
  location.reload();
});

/* An <a href> download cannot carry a bearer header. With a token set, fetch
   the file and hand the browser a blob instead. */
document.addEventListener('click', async e => {
  const a = e.target.closest && e.target.closest('a[href^="/api/"]');
  if (!a || !AUTH.token) return;
  e.preventDefault();
  try {
    const r = await authed(a.getAttribute('href'));
    if (!r.ok) throw Object.assign(new Error('download failed'), { status: r.status });
    const cd = r.headers.get('Content-Disposition') || '';
    const m = cd.match(/filename\*?=(?:UTF-8'')?"?([^";]+)"?/i);
    const url = URL.createObjectURL(await r.blob());
    const tmp = Object.assign(document.createElement('a'),
      { href: url, download: m ? decodeURIComponent(m[1]) : 'download' });
    document.body.appendChild(tmp); tmp.click(); tmp.remove();
    setTimeout(() => URL.revokeObjectURL(url), 5000);
  } catch (err) { toastError(err, 'download'); }
});

async function api(path, opts) {
  let r;
  try { r = await authed(path, opts); }
  catch (e) { const err = new Error(e.message || 'network'); err.status = 0; err.network = true; throw err; }
  if (!r.ok) {
    const body = await r.text();
    let detail = body;
    try { detail = JSON.parse(body).detail || body; } catch (_) { }
    if (typeof detail !== 'string') detail = JSON.stringify(detail);
    const err = new Error(String(detail).slice(0, 600));
    err.status = r.status; err.path = path;
    throw err;
  }
  return r.json();
}
const post = (p, body) => api(p, {
  method: 'POST', headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify(body || {})
});

/* ================================================================ errors
   The control plane's error text is precise and meant for engineers. Every
   failure the operator sees is translated into what happened and what to do,
   with the original folded underneath -- never discarded, never the headline. */
function explain(e, doing) {
  const st = e && e.status, raw = String((e && e.message) || e || '');
  const low = raw.toLowerCase();
  const d = { title: 'Something went wrong', text: 'Try again. If it keeps happening, an admin can check the activity log.', kind: 'err', raw };
  if (e && e.network || st === 0 || /failed to fetch|networkerror|load failed/.test(low)) {
    return { ...d, kind: 'warn', title: "Can't reach the workbench", text: 'It may be restarting. ClawCal reconnects by itself; nothing you typed is lost.' };
  }
  if (st === 401) return { ...d, kind: 'warn', title: 'Your sign-in has expired', text: 'Sign in again to carry on.', action: ['Sign in', () => signIn()] };
  if (st === 403 && !(e.path && /drawings\/analyse/.test(e.path))) {
    if (/approv/.test(low)) return { ...d, title: "You can't approve this", text: 'Only people with approval rights can allow this. Ask an admin, or switch the chat to Read only.' };
    if (/locked|read.?only/.test(low)) return { ...d, title: 'Blocked by "Read only"', text: 'This chat can read but not write. Switch to Ask first and ClawCal will check with you before writing.' };
    return { ...d, title: "You don't have access to that", text: 'Your role on this workbench does not allow it. An admin can change your role.' };
  }
  if (st === 409) return { ...d, kind: 'info', title: 'Already decided', text: 'Someone answered this before you did. The chat shows what was decided.' };
  if (st === 403 && e.path && /drawings\/analyse/.test(e.path)) return { ...d, title: "ClawCal can't open files there", text: 'Drawings can only be analysed from the corpus folder or the workbench data folder.' };
  if (st === 404) return { ...d, kind: 'warn', title: 'That is no longer here', text: 'It may have been removed, or it belongs to someone else.' };
  if (st === 413) return { ...d, title: 'That file is too large', text: 'Try a smaller file, or split it into parts.' };
  if (st === 415 || /unsupported (file|type|format)/.test(low)) return { ...d, title: "ClawCal can't read that kind of file", text: 'Try a PDF, Word, Excel, image or CAD file.' };
  if (st === 429 || /queue.?full/.test(low)) return { ...d, kind: 'warn', title: 'The workbench is busy', text: 'Too much work is already waiting. Try again in about 30 seconds.' };
  if (/policy_denied|not permitted in mode|mode locked/.test(low)) return { ...d, title: 'Blocked by "Read only"', text: 'Switch the chat to Ask first to let ClawCal make changes.' };
  if (/11434|ollama|backend.*(down|unreachable)|connection refused|max retries/.test(low)) {
    return { ...d, kind: 'warn', title: "The AI engine isn't responding", text: 'If it was only just started, give it a minute. Your chat is saved.' };
  }
  if (/memory|vram|oom|out of ram/.test(low)) return { ...d, kind: 'warn', title: 'Not enough memory right now', text: "The machine didn't have enough free memory to run the model. Close other heavy programs or wait for current work to finish, then try again." };
  if (/prompt is required/.test(low)) return { ...d, kind: 'warn', title: 'Type a message first', text: '' };
  if (st >= 500) return { ...d, title: 'The workbench hit a problem', text: `It couldn't ${doing || 'finish that'}. Try again in a moment.` };
  if (st === 400 && raw && raw.length < 140 && !/[{}<>]|traceback|error:/i.test(raw)) {
    return { ...d, kind: 'warn', title: `Couldn't ${doing || 'do that'}`, text: cap(raw.replace(/\.$/, '')) + '.' };
  }
  return { ...d, text: `ClawCal couldn't ${doing || 'finish that'}. Try again.` };
}

/* A failed view: say so plainly instead of leaving stale data looking current. */
function failView(el, e, retry, cols) {
  const x = explain(e, 'load this');
  const html = `<div class="empty err"><span class="ico">${icon('i-warn')}</span><b>${esc(x.title)}</b>
    ${x.text ? `<p>${esc(x.text)}</p>` : ''}
    ${retry ? '<button type="button" class="btn sm secondary" data-retry>' + icon('i-refresh') + 'Try again</button>' : ''}
    ${x.raw ? `<details class="hint"><summary>Technical details</summary><code>${esc(x.raw)}</code></details>` : ''}</div>`;
  const target = typeof el === 'string' ? $(el) : el;
  if (!target) return;
  paint(target, cols ? `<tr><td colspan="${cols}" class="empty-cell">${html}</td></tr>` : html);
  const b = target.querySelector('[data-retry]');
  if (b && retry) b.onclick = () => { paint(target, cols ? '' : skeleton()); retry(); };
}

/* ================================================================ toasts */
function toast({ kind = 'info', title, text = '', raw = '', action, ttl }) {
  const ic = { err: 'i-x', warn: 'i-warn', ok: 'i-check', info: 'i-info' }[kind] || 'i-info';
  const el = document.createElement('div');
  el.className = 'toast ' + kind;
  el.setAttribute('role', kind === 'err' ? 'alert' : 'status');
  el.innerHTML = `<span class="ic">${icon(ic)}</span>
    <div><b>${esc(title)}</b>${text ? `<p>${esc(text)}</p>` : ''}<div class="acts"></div></div>
    <button type="button" class="x" aria-label="Dismiss">${icon('i-x')}</button>
    ${raw ? `<details><summary>Technical details</summary><pre>${esc(raw)}</pre></details>` : ''}`;
  if (action) {
    const b = document.createElement('button');
    b.type = 'button'; b.className = 'btn sm ' + (kind === 'err' ? 'secondary' : 'primary');
    b.textContent = action[0];
    b.onclick = () => { close(); action[1](); };
    el.querySelector('.acts').appendChild(b);
  }
  const close = () => { if (!el.isConnected) return; el.classList.add('out'); setTimeout(() => el.remove(), 220); };
  el.querySelector('.x').onclick = close;
  const box = $('#toasts');
  box.appendChild(el);
  while (box.children.length > 4) box.firstElementChild.remove();
  const life = ttl ?? (kind === 'err' ? 12000 : kind === 'warn' ? 9000 : 4500);
  let t = setTimeout(close, life);
  el.onmouseenter = () => clearTimeout(t);
  el.onmouseleave = () => { t = setTimeout(close, 3000); };
  return el;
}
function toastError(e, doing, action) {
  const x = explain(e, doing);
  return toast({ kind: x.kind, title: x.title, text: x.text, raw: x.raw, action: action || x.action });
}

/* ================================================================= state */
const state = {
  view: 'chat', adminTab: 'system', libTab: 'docs',
  session: null, sessionInfo: null, cache: {}, fold: {}, seen: new Set(),
  system: null, role: 'engineer', mode: 'review', modeTouched: false,
  doc: null, drawing: null, pendingFile: null, lastUpload: null,
  tps: {}, pending: new Set(), canDecide: true, apprTask: {},
  sessions: [], taskBySession: {}, lastDenials: null, sov: null, runtime: null,
};
const isAdmin = () => state.role === 'admin';
const canWrite = () => state.role !== 'viewer';

/* Measured decode rates, keyed by model. Real measurements taken on this
   machine, not a vendor figure -- so they are worth surfacing. */
async function loadRates() {
  try {
    const m = await api('/api/models');
    m.models.forEach(c => { if (c.profile && c.profile.decode_tps) state.tps[c.name] = c.profile.decode_tps; });
    const sel = $('#model');
    if (sel.options.length <= 1) {
      m.models.filter(c => c.enabled !== false).forEach(c => {
        const o = document.createElement('option');
        o.value = c.name;
        o.textContent = c.name + (c.role ? ' — ' + c.role : '');
        sel.appendChild(o);
      });
    }
  } catch (e) { /* rates are a nicety; the workbench works without them */ }
}
const rateOf = m => state.tps[m] ? state.tps[m].toFixed(1) + ' tok/s' : null;

/* ================================================================= theme */
function applyThemeIcon() {
  const t = document.documentElement.dataset.theme;
  const dark = t ? t === 'dark' : matchMedia('(prefers-color-scheme: dark)').matches;
  $('#themeBtn').innerHTML = icon(dark ? 'i-sun' : 'i-moon');
  $('#themeBtn').title = dark ? 'Switch to light' : 'Switch to dark';
}
$('#themeBtn').onclick = () => {
  const t = document.documentElement.dataset.theme;
  const dark = t ? t === 'dark' : matchMedia('(prefers-color-scheme: dark)').matches;
  const next = dark ? 'light' : 'dark';
  document.documentElement.dataset.theme = next;
  try { localStorage.setItem('clawcal.theme', next); } catch (_) { }
  applyThemeIcon();
};
applyThemeIcon();

/* A hidden tab runs no animation: nothing to look at, nothing to spend. */
document.addEventListener('visibilitychange', () =>
  document.body.classList.toggle('tab-hidden', document.hidden));

/* ============================================================ navigation */
const META = {
  chat: ['New chat', ''],
  library: ['Library', 'Everything you have given ClawCal. It reads these here, on this machine.'],
  outputs: ['Outputs', 'Files ClawCal has made for you, each recorded with a fingerprint.'],
  approvals: ['Approvals', 'Actions ClawCal will not take without a yes from a person.'],
  admin: ['Admin', 'How the workbench is running, and the proof that data stays here.'],
};

function go(view, opts = {}) {
  if (view === 'admin' && !isAdmin()) view = 'chat';
  state.view = view;
  $$('#places button').forEach(b => {
    const on = b.dataset.v === view;
    b.classList.toggle('on', on);
    b.setAttribute('aria-current', on ? 'page' : 'false');
  });
  $$('#views > .view').forEach(v => v.classList.toggle('on', v.id === 'v-' + view));
  $('#composerWrap').classList.toggle('hide', view !== 'chat');
  $('#scroll').classList.toggle('chat-bg', view === 'chat');
  if (view !== 'chat') {
    const [t, d] = META[view];
    $('#viewTitle').textContent = t; $('#viewDesc').textContent = d;
    paintChatList();
  } else paintChatTitle();
  if (opts.tab && view === 'admin') setAdminTab(opts.tab);
  closeDrawer();
  $('#scroll').scrollTop = 0;
  refreshView();
  syncUrl();
}
$$('#places button').forEach(b => b.onclick = () => go(b.dataset.v));

function syncUrl() {
  const q = new URLSearchParams();
  if (state.view === 'chat' && state.session) q.set('chat', state.session);
  else if (state.view !== 'chat') q.set('view', state.view === 'admin' ? 'admin:' + state.adminTab : state.view);
  const s = q.toString();
  history.replaceState(null, '', s ? '?' + s : location.pathname);
}

/* drawer on narrow screens */
const openDrawer = () => { $('#app').classList.add('sb-open'); $('#scrim').hidden = false; };
function closeDrawer() { $('#app').classList.remove('sb-open'); $('#scrim').hidden = true; }
$('#sbOpen').onclick = openDrawer;
$('#sbClose').onclick = closeDrawer;
$('#scrim').onclick = closeDrawer;

/* popovers: one open at a time, closed by Escape or a click elsewhere */
function togglePop(btn, pop, force) {
  const open = force ?? pop.hidden;
  $$('.pop').forEach(p => { if (p !== pop) p.hidden = true; });
  $$('[aria-expanded="true"]').forEach(b => { if (b !== btn) b.setAttribute('aria-expanded', 'false'); });
  pop.hidden = !open;
  btn.setAttribute('aria-expanded', String(open));
  if (open) { const f = pop.querySelector('[aria-checked="true"], button, select'); f && f.focus({ preventScroll: true }); }
}
document.addEventListener('click', e => {
  if (e.target.closest('.pop') || e.target.closest('[aria-haspopup], #sealBtn')) return;
  $$('.pop').forEach(p => p.hidden = true);
  $$('[aria-expanded="true"]').forEach(b => b.setAttribute('aria-expanded', 'false'));
});
document.addEventListener('keydown', e => {
  if (e.key === 'Escape') {
    const open = $$('.pop').find(p => !p.hidden);
    if (open) { open.hidden = true; $$('[aria-expanded="true"]').forEach(b => { b.setAttribute('aria-expanded', 'false'); b.focus(); }); }
    closeDrawer();
  }
  const mod = e.ctrlKey || e.metaKey;
  if (mod && e.key.toLowerCase() === 'k') { e.preventDefault(); openDrawer(); $('#chatSearch').focus(); }
  if (mod && e.shiftKey && e.key.toLowerCase() === 'o') { e.preventDefault(); newChat(); }
});

/* ================================================================ system */
async function loadSystem() {
  const s = await api('/api/system'); state.system = s;
  $('#orgName').textContent = s.org?.name || '';
  $('#orgUnit').textContent = s.org?.unit || '';
  const p = s.principal || {};
  state.role = p.role || 'engineer';
  const name = p.display_name || p.name || '';
  $('#meName').textContent = name;
  $('#meRole').textContent = cap(state.role) + (p.department ? ' · ' + p.department : '');
  $('#meAv').textContent = (name.match(/\b\w/g) || ['·']).slice(0, 2).join('');
  $('#adminNav').hidden = !isAdmin();
  $$('[data-admin]').forEach(x => x.hidden = !isAdmin());

  if (!canWrite()) {
    $('#prompt').disabled = true;
    $('#prompt').placeholder = 'Your role can read chats but not start them. Ask an admin for engineer access.';
  }

  /* A new chat starts in the appliance default until the operator picks. */
  if (!state.session && !state.modeTouched) setMode(s.policy_mode || 'review', false);

  setCount('#cDocs', s.counts?.documents);
  setCount('#cDocs2', s.counts?.documents);
  setCount('#cArt', s.counts?.artifacts);

  const wf = $('#workflow');
  if (!wf.options.length && s.workflows) {
    wf.innerHTML = s.workflows.map(w => `<option value="${esc(w)}">${esc(human(w))}</option>`).join('');
    wf.value = s.workflows.includes('general') ? 'general' : s.workflows[0];
  }
  paintEngine();
}
function setCount(sel, n) { const el = $(sel); if (el) el.textContent = n ? String(n) : ''; }

/* engine card: one line on what the machine is doing right now */
function paintEngine(live) {
  const r = state.runtime, s = state.system;
  const g = (live || s?.live || {}).gpu || {};
  const el = $('#engine'), dot = $('#engineDot');
  let txt = 'Ready', sub = '', cls = 'ok';
  if (r) {
    const run = r.running?.length || 0, q = r.queued?.length || 0;
    const res = (r.residency?.resident || []).map(x => x.model);
    if (run) { txt = run === 1 ? 'Working on 1 chat' : `Working on ${run} chats`; cls = 'busy'; }
    if (q) {
      if (run) txt += ` · ${q} waiting`;
      else {
        const mem = (r.queued || []).some(t => resourceWait(t.state_reason)?.[0] === 'Waiting for free memory');
        txt = mem ? `${q} waiting for free memory` : `${q} waiting for capacity`; cls = 'warn';
      }
    }
    sub = res.length ? res.join(', ') + ' loaded' : 'No model loaded';
    const rate = res[0] && rateOf(res[0]);
    if (rate) sub += ' · ' + rate;
  }
  const back = state.sov?.backends || {};
  if (Object.keys(back).length && !Object.values(back).some(v => v === 'up')) { txt = 'AI engine offline'; cls = 'bad'; }
  dot.className = 'px ' + cls;
  $('#engineTxt').textContent = txt;
  $('#engineSub').textContent = sub;
  const pct = g.total_mb ? Math.min(100, 100 * g.used_mb / g.total_mb) : 0;
  $('#engineBar').style.width = pct + '%';
  el.classList.toggle('warn', pct > 80); el.classList.toggle('bad', pct > 94);
  el.title = g.total_mb ? `GPU memory ${(g.used_mb / 1024).toFixed(1)} of ${(g.total_mb / 1024).toFixed(1)} GB in use` : 'Workbench status';
}
$('#engine').onclick = () => isAdmin() ? go('admin', { tab: 'system' }) : null;

/* ================================================================== seal */
async function paintSeal() {
  let v;
  try { v = await api('/api/sovereignty'); } catch (_) { return; }
  state.sov = v;
  const btn = $('#sealBtn');
  const label = { green: 'Sealed', amber: 'Sealed · partly verified', red: 'Not sealed' }[v.state] || 'Checking…';
  btn.className = 'seal ' + (v.state || '');
  $('#sealTxt').textContent = v.state === 'amber' ? 'Sealed' : label;
  btn.title = label;
  if (state.lastDenials != null && v.denials > state.lastDenials) {
    btn.classList.remove('flash'); void btn.offsetWidth; btn.classList.add('flash');
  }
  state.lastDenials = v.denials;

  const head = {
    green: ['Nothing leaves this machine', 'Every protection layer is in force and verified.'],
    amber: ['Nothing leaves this machine', 'The app-level seal is on. The host firewall could not be checked from here, so one layer is unverified.'],
    red: ['The seal is not in force', 'At least one protection layer is off. Ask an admin before working with confidential files.'],
  }[v.state] || ['Checking protection…', ''];
  paint('#sealHead', `<b>${esc(head[0])}</b><p>${esc(head[1])}</p>`);
  const host = { applied: ['ok', 'Host firewall blocks outside connections'],
    not_applied: ['bad', 'Host firewall is not applied'],
    unreadable: ['warn', 'Host firewall could not be checked without admin rights'] }[v.host_policy] || ['warn', 'Host firewall: ' + v.host_policy];
  const items = [
    [v.app_guard ? 'ok' : 'bad', v.app_guard ? 'App seal is on — every connection attempt is checked' : 'App seal is off'],
    host,
    ['ok', `<b>${Number(v.denials || 0).toLocaleString()}</b> outside connection attempts blocked`],
    [v.audit?.ok ? 'ok' : 'bad', v.audit?.ok ? `Activity log intact · ${Number(v.audit.entries || 0).toLocaleString()} entries` : 'Activity log failed its integrity check'],
  ];
  if (v.last_selftest) items.push([/PASS/.test(v.last_selftest.outcome) ? 'ok' : 'bad',
    `Last self-test ${/PASS/.test(v.last_selftest.outcome) ? 'passed' : 'failed'} · ${ago(v.last_selftest.ts)}`]);
  paint('#sealList', items.map(([c, t]) =>
    `<li class="${c}">${icon(c === 'ok' ? 'i-check' : c === 'bad' ? 'i-x' : 'i-warn')}<span>${t.startsWith('<b>') ? t : esc(t)}</span></li>`).join(''));
  paintEngine();
}
$('#sealBtn').onclick = e => { e.stopPropagation(); togglePop($('#sealBtn'), $('#sealPop')); };
$('#sealMore').onclick = () => { togglePop($('#sealBtn'), $('#sealPop'), false); go('admin', { tab: 'security' }); };
$('#sealTest').onclick = () => runSelftest($('#sealTest'));

async function runSelftest(btn) {
  btn.disabled = true; btn.classList.add('busy');
  try {
    const r = await post('/api/sovereignty/selftest');
    togglePop($('#sealBtn'), $('#sealPop'), false);
    toast({ kind: 'info', title: 'Self-test started', text: 'ClawCal is deliberately trying to reach outside. Every attempt should be blocked.' });
    openChat(r.session_id, r.task_id);
  } catch (e) { toastError(e, 'start the self-test'); }
  finally { setTimeout(() => { btn.disabled = false; btn.classList.remove('busy'); }, 3000); }
}

/* ============================================================== composer */
function autoGrow() {
  const t = $('#prompt');
  t.style.height = 'auto';
  t.style.height = Math.min(t.scrollHeight, 220) + 'px';
  $('#submitBtn').disabled = !t.value.trim() || !canWrite();
}

function setMode(m, touched = true) {
  if (!MODES[m]) m = 'review';
  state.mode = m;
  if (touched) state.modeTouched = true;
  const d = MODES[m];
  $('#modeLbl').textContent = d.label;
  $('#modeIcon').innerHTML = `<use href="#${d.icon}"/>`;
  $('#modeBtn').className = 'dd ' + m;
  $('#modeBtn').title = d.note;
  $$('#modeMenu [data-mode]').forEach(b => b.setAttribute('aria-checked', String(b.dataset.mode === m)));
}
$('#modeBtn').onclick = e => { e.stopPropagation(); togglePop($('#modeBtn'), $('#modeMenu')); };
$$('#modeMenu [data-mode]').forEach(b => b.onclick = async () => {
  const m = b.dataset.mode, prev = state.mode;
  togglePop($('#modeBtn'), $('#modeMenu'), false);
  if (m === prev) return;
  setMode(m);
  if (!state.session) return;            /* applies to the next new chat */
  try {
    await post(`/api/sessions/${enc(state.session)}/mode`, { mode: m });
    toast({ kind: 'ok', title: `This chat is now "${MODES[m].label}"`, text: MODES[m].note + ' A run in progress switches at its next step.' });
  } catch (e) { setMode(prev); toastError(e, 'change the setting'); }
});

function paintOpts() {
  const bits = [];
  const m = $('#model').value, p = $('#priority').value, w = $('#workflow').value;
  if (m) bits.push(m);
  if (p) bits.push(prio(p));
  if (w && w !== 'general') bits.push(human(w));
  $('#optsLbl').textContent = bits.length ? bits.join(' · ') : 'Auto';
}
$('#optsBtn').onclick = e => { e.stopPropagation(); togglePop($('#optsBtn'), $('#optsMenu')); };
['#model', '#priority', '#workflow'].forEach(s => $(s).addEventListener('change', paintOpts));

/* attachment */
function describeFile(f, status) {
  const chip = $('#attachChip');
  chip.hidden = !f;
  chip.className = 'attach' + (status === 'ok' ? ' ok' : '');
  if (!f) { chip.innerHTML = ''; return; }
  chip.innerHTML = `${ftChip(f.name)}<div><b>${esc(f.name)}</b>
    <small>${status === 'busy' ? 'Reading it in, on this machine…' : status === 'ok' ? esc(state.lastUpload?.note || 'Ready') : fmtBytes(f.size)}</small>
    ${status === 'busy' ? '<div class="prog"><i></i></div>' : ''}</div>
    ${status === 'busy' ? '' : `<button type="button" class="icon-btn" aria-label="Remove attachment">${icon('i-x')}</button>`}`;
  const x = chip.querySelector('button');
  if (x) x.onclick = () => { $('#file').value = ''; state.pendingFile = null; state.lastUpload = null; describeFile(null); };
}
function chooseFile(f) {
  state.pendingFile = f || null; state.lastUpload = null;
  describeFile(state.pendingFile);
  $('#prompt').focus();
}
$('#attachBtn').onclick = () => $('#file').click();
$('#file').onchange = () => chooseFile($('#file').files[0]);

$('#prompt').addEventListener('input', autoGrow);
/* Enter sends, Shift+Enter breaks the line -- what anyone who has used a chat
   interface will try first. */
$('#prompt').addEventListener('keydown', e => {
  if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); $('#composer').requestSubmit(); }
});

(() => {
  const c = $('#composer');
  let depth = 0;
  c.addEventListener('dragenter', e => { e.preventDefault(); depth++; c.classList.add('over'); });
  c.addEventListener('dragover', e => e.preventDefault());
  c.addEventListener('dragleave', e => { e.preventDefault(); if (--depth <= 0) { depth = 0; c.classList.remove('over'); } });
  c.addEventListener('drop', e => {
    e.preventDefault(); depth = 0; c.classList.remove('over');
    const f = e.dataTransfer?.files?.[0];
    if (f) chooseFile(f);
  });
})();

async function uploadFile(f) {
  const fd = new FormData(); fd.append('file', f);
  const r = await api('/api/upload', { method: 'POST', body: fd });
  const bits = [`${r.pages} page${r.pages === 1 ? '' : 's'}`];
  if (r.failed_pages?.length) bits.push(`pages ${r.failed_pages} unreadable`);
  if (r.reused) bits.push('already in your library');
  loadSystem().catch(() => { });
  return { doc_id: r.doc_id, title: r.title, pages: r.pages,
           kind: (r.doc_class === 'drawing' ? 'drawing' : 'document'), note: bits.join(' · '), raw: r };
}

$('#composer').addEventListener('submit', async e => {
  e.preventDefault();
  const prompt = $('#prompt').value.trim();
  if (!prompt) { $('#prompt').focus(); return; }
  if (!canWrite()) return;
  const btn = $('#submitBtn');
  btn.disabled = true; btn.classList.add('busy');
  try {
    /* A file sitting in the picker is one the operator plainly intends to use.
       Requiring a separate index step first meant it was silently dropped and
       the agent was asked about a document it had never been given. */
    if (!state.lastUpload && state.pendingFile) {
      describeFile(state.pendingFile, 'busy');
      try { state.lastUpload = await uploadFile(state.pendingFile); }
      catch (err) { describeFile(state.pendingFile); throw Object.assign(err, { doing: 'read that file' }); }
      describeFile(state.pendingFile, 'ok');
    }
    const body = { prompt, workflow: $('#workflow').value || 'general', mode: state.mode };
    if ($('#priority').value) body.priority = $('#priority').value;
    if ($('#model').value) body.model = $('#model').value;
    /* Follow-ups continue the open chat: the control plane replays prior turns
       and carries the working set of attached documents forward. */
    if (state.session) body.session_id = state.session;
    if (state.lastUpload) {
      const { raw, note, ...a } = state.lastUpload;
      body.attachments = [a];
    }
    const r = await post('/api/tasks', body);
    if (r.injection_scan?.detected) {
      toast({ kind: 'warn', title: 'Instruction-like text noted', text: 'Your message contains text that looks like instructions to the AI. It has been recorded, and ClawCal will treat it with care.' });
    }
    $('#prompt').value = ''; $('#file').value = '';
    state.pendingFile = null; state.lastUpload = null; describeFile(null); autoGrow();
    if (r.permission_mode) setMode(r.permission_mode, false);
    openChat(r.session_id || state.session, r.task_id);
    loadChats();
  } catch (err) {
    toastError(err, err.doing || 'send that');
  }
  btn.classList.remove('busy');
  autoGrow();
});

function newChat() {
  state.session = null; state.sessionInfo = null; state.cache = {}; state.fold = {};
  state.modeTouched = false;
  if (state.system) setMode(state.system.policy_mode || 'review', false);
  $('#ctxPill').hidden = true;
  if (state.view !== 'chat') go('chat'); else { syncUrl(); paintChatTitle(); }
  renderHello();
  paintChatList();
  closeDrawer();
  $('#prompt').focus();
}
$('#newChatBtn').onclick = newChat;

/* ============================================================= chat list */
async function loadChats() {
  try {
    const [{ sessions }, { tasks }] = await Promise.all([api('/api/sessions'), api('/api/tasks?limit=80')]);
    state.sessions = sessions || [];
    /* The session list has no run state; the newest run of each chat gives it. */
    const by = {};
    for (const t of tasks || []) {
      if (!t.conversation_id) continue;
      if (!by[t.conversation_id] || by[t.conversation_id].created_at < t.created_at) by[t.conversation_id] = t;
    }
    state.taskBySession = by;
    paintChatList();
  } catch (e) { /* the list can wait for the next tick */ }
}

function paintChatList() {
  const q = $('#chatSearch').value.trim().toLowerCase();
  const list = state.sessions.filter(s => !q || String(s.title || '').toLowerCase().includes(q));
  if (!list.length) {
    paint('#chatList', `<div class="none">${q ? 'No chats match.' : 'No chats yet. Start one above.'}</div>`);
    return;
  }
  const now = new Date(), day = 86400;
  const start = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime() / 1000;
  const group = ts => ts >= start ? 'Today' : ts >= start - day ? 'Yesterday' : ts >= start - 7 * day ? 'Previous 7 days' : 'Older';
  let last = null;
  const waiting = new Set(Object.values(state.apprTask));
  const html = list.slice(0, 60).map(s => {
    const g = group(s.updated_at || s.created_at);
    const t = state.taskBySession[s.id];
    const st = t && waiting.has(t.id) ? 'wait' : (t ? t.state : '');
    const head = g !== last ? `<div class="h">${g}</div>` : '';
    last = g;
    const on = state.view === 'chat' && s.id === state.session;
    return `${head}<button type="button" class="ci${on ? ' on' : ''}" data-id="${esc(s.id)}" title="${esc(s.title)}">
      <span class="t">${esc(s.title || 'Untitled chat')}</span><i class="st ${esc(st)}"></i></button>`;
  }).join('');
  if (paint('#chatList', html)) {
    $$('#chatList .ci').forEach(b => b.onclick = () => openChat(b.dataset.id));
  } else {
    $$('#chatList .ci').forEach(b => b.classList.toggle('on', state.view === 'chat' && b.dataset.id === state.session));
  }
}
$('#chatSearch').addEventListener('input', paintChatList);

function paintChatTitle() {
  if (state.view !== 'chat') return;
  const s = state.sessionInfo;
  $('#viewTitle').textContent = s ? (s.title || 'Untitled chat') : 'New chat';
  const n = s?.tasks?.length || 0;
  const docs = s?.working_set?.documents?.length || 0;
  $('#viewDesc').textContent = s ? [n ? `${n} message${n === 1 ? '' : 's'}` : '', docs ? `${docs} file${docs === 1 ? '' : 's'} attached` : '']
    .filter(Boolean).join(' · ') : '';
  document.title = (s ? (s.title || 'Chat') + ' — ' : '') + 'ClawCal — Sovereign AI Workbench';
}

/* ============================================================ the thread */
function openChat(sessionId, taskId) {
  if (!sessionId && taskId) {
    /* An older link names a run, not a chat: find the chat it belongs to. */
    api('/api/tasks/' + enc(taskId)).then(d => openChat(d.task.conversation_id || null, null))
      .catch(e => toastError(e, 'open that chat'));
    return;
  }
  if (sessionId !== state.session) {
    state.session = sessionId; state.sessionInfo = null; state.cache = {}; state.fold = {};
    state.seen = new Set(); state.modeTouched = false;
    paint('#thread', `<div class="thread-load">${skeleton()}</div>`);
  }
  state.followTask = taskId || null;
  if (state.view !== 'chat') go('chat'); else syncUrl();
  paintChatList();
  loadChat(true);
  closeDrawer();
}

let chatLoading = false;
async function loadChat(scrollDown) {
  if (state.view !== 'chat') return;
  if (!state.session) { renderHello(); return; }
  if (chatLoading) return;
  chatLoading = true;
  const sid = state.session;
  try {
    let s;
    try { s = await api('/api/sessions/' + enc(sid)); }
    catch (e) { if (sid === state.session) failView('#thread', e, () => loadChat(true)); return; }
    if (sid !== state.session) return;
    state.sessionInfo = s;
    if (!state.modeTouched && s.permission_mode) setMode(s.permission_mode, false);
    const ids = (s.tasks || []).map(t => t.id).slice(-12);
    if (state.followTask && !ids.includes(state.followTask)) ids.push(state.followTask);
    await Promise.all(ids.map(async id => {
      const c = state.cache[id];
      if (c && TERMINAL.has(c.task.state)) return;   /* a finished run is immutable */
      try { state.cache[id] = await api('/api/tasks/' + enc(id)); } catch (_) { }
    }));
    if (sid !== state.session) return;
    const turns = ids.map(id => state.cache[id]).filter(Boolean);
    if (turns.some(d => !TERMINAL.has(d.task.state))) {
      try {
        const a = await api('/api/approvals');
        state.pending = new Set(a.pending.map(x => x.id));
        state.canDecide = a.can_decide;
      } catch (_) { }
    }
    const sc = $('#scroll');
    const atBottom = sc.scrollHeight - sc.scrollTop - sc.clientHeight < 160;
    let changed;
    try { changed = renderChat(turns); }
    catch (err) {
      /* A payload shape this client does not expect must not leave an endless
         skeleton: say so, keep the chat usable, and keep the detail. */
      console.error(err);
      paint('#thread', `<div class="empty err"><span class="ico">${icon('i-warn')}</span><b>This chat couldn't be shown</b>
        <p>Something in it is in a form this screen doesn't recognise. You can still send new messages.</p>
        <details class="hint"><summary>Technical details</summary><code>${esc(err && (err.stack || err.message) || err)}</code></details></div>`);
      return;
    }
    paintChatTitle();
    const last = turns[turns.length - 1];
    if (last) paintContext(last.task);
    if (scrollDown || (changed && atBottom)) scrollBottom(!scrollDown);
  } finally { chatLoading = false; }
}

function scrollBottom(smooth) {
  const sc = $('#scroll');
  requestAnimationFrame(() => sc.scrollTo({ top: sc.scrollHeight, behavior: smooth ? 'smooth' : 'instant' }));
}
$('#scroll').addEventListener('scroll', () => {
  const sc = $('#scroll');
  const far = sc.scrollHeight - sc.scrollTop - sc.clientHeight > 400;
  $('#toBottom').hidden = !(state.view === 'chat' && state.session && far);
}, { passive: true });
$('#toBottom').onclick = () => scrollBottom(true);

/* empty chat: a greeting, the mark drawing itself, and somewhere to start */
const STARTERS = [
  ['i-draw', 'Check a drawing', 'List the equipment and connections on a P&ID.',
    'List the equipment and instrument tags on the attached P&ID, and the connections between them.'],
  ['i-docs', 'Summarise a report', 'Key findings, with the page each came from.',
    'Summarise the attached report. Give the key findings and cite the page each one comes from.'],
  ['i-sheet', 'Pull numbers from a spreadsheet', 'Find a value and the cell it lives in.',
    'From the attached workbook, find the value I need and tell me which cell it is in.'],
  ['i-calc', 'Run a calculation', 'Every step traced and re-runnable.',
    'Calculate the pressure drop for the line in the attached datasheet, and show each step.'],
];
function renderHello() {
  if (state.view !== 'chat') return;
  const h = new Date().getHours();
  const greet = h < 12 ? 'Good morning' : h < 18 ? 'Good afternoon' : 'Good evening';
  const who = (state.system?.principal?.display_name || '').split(/[ ._]/)[0];
  paint('#thread', `<div class="hello">
      <svg class="hello-art" viewBox="0 0 60 60" aria-hidden="true">
        <path class="g" d="M6 10H54M6 18H54M6 26H54M6 34H54M6 42H54M6 50H54M10 6V54M18 6V54M26 6V54M34 6V54M42 6V54M50 6V54"/>
        <g transform="translate(12 12) scale(1.5)">
          <path class="solid" d="M2 8a6 6 0 0 1 6-6h14v7.5h-4v-3H7v11h11v-3h4V22H2z"/>
          <path class="body" d="M2 8a6 6 0 0 1 6-6h14v7.5h-4v-3H7v11h11v-3h4V22H2z"/>
          <rect class="held" x="11" y="10.5" width="3" height="3"/>
        </g>
        <path class="dim" d="M12 55V58M48 55V58M12 56.5H48"/>
        <text class="dimtxt" x="30" y="54" text-anchor="middle">36.0</text>
      </svg>
      <h2>${esc(greet)}${who ? ', ' + esc(cap(who)) : ''}. What are we working on?</h2>
      <p>Attach a drawing, report or spreadsheet and ask about it. ClawCal reads it here — nothing leaves this machine — and shows where every number came from.</p>
    </div>
    <div class="starters">${STARTERS.map((s, i) => `<button type="button" class="starter" data-i="${i}">
      <span class="si">${icon(s[0])}</span><span><b>${esc(s[1])}</b><span>${esc(s[2])}</span></span></button>`).join('')}</div>`);
  $$('#thread .starter').forEach(b => b.onclick = () => {
    const s = STARTERS[+b.dataset.i];
    $('#prompt').value = s[3]; autoGrow(); $('#prompt').focus();
    if (!state.pendingFile) { toast({ kind: 'info', title: 'Attach the file this is about', text: 'Use the paper clip, or drop a file on the message box.', ttl: 5000 }); }
  });
  paintChatTitle();
}

const payloadOf = e => {
  if (!e || e.payload == null) return {};
  if (typeof e.payload === 'object') return e.payload || {};
  try { return JSON.parse(e.payload) || {}; } catch (_) { return {}; }
};
const SETUP = new Set(['submitted', 'admitted', 'residency', 'plan', 'resumed']);
const QUIET = new Set(['provenance', 'completed', 'approval_granted', 'approval_denied', 'approval_decided', 'terminated']);
const BAD = new Set(['failed', 'policy_denied', 'provenance_refusal', 'injection_detected', 'loop_detected', 'model_error']);
const WARN = new Set(['paused', 'preempt_requested', 'sovereignty']);
const NOTICE = {
  failed: ['This run stopped with an error', 'i-x'],
  policy_denied: ['An action was blocked by this chat\'s setting', 'i-lock'],
  provenance_refusal: ['ClawCal refused to state something it could not support', 'i-shield'],
  injection_detected: ['A document contained instructions aimed at the AI', 'i-shield'],
  loop_detected: ['ClawCal was going round in circles, so it stopped', 'i-refresh'],
  model_error: ['The AI engine returned an error', 'i-warn'],
  paused: ['Paused', 'i-pause'],
  preempt_requested: ['Making way for more urgent work', 'i-pause'],
  sovereignty: ['Something tried to reach outside, and was blocked', 'i-shield'],
};

/* Every number in the answer carries its evidence class; the provenance
   markers " [A]" arrive in `annotated`, one per verdict, in order. */
function answerHtml(res) {
  const verdicts = ((res.provenance && res.provenance.verdicts) || []).slice().sort((a, b) => a.start - b.start);
  let k = 0;
  const raw = String(res.annotated || res.summary || '');
  const text = /^\s*CANNOT DETERMINE\s*[-–—:]/i.test(raw) ? cap(raw.replace(/^\s*CANNOT DETERMINE\s*[-–—:]\s*/i, '')) : raw;
  return mdLite(text)
    /* Unsupported numbers arrive already replaced by "[CANNOT DETERMINE - why]". */
    .replace(/\[CANNOT DETERMINE\s*[-–—:]?\s*([^\]]*)\]/g, (_, why) =>
      `<span class="ev d" title="${why}">${icon('i-x')}Couldn't determine</span>`)
    .replace(/ \[([ABCD])\]/g, (_, cls) => {
    const v = verdicts[k] || {}; const i = k++;
    return ' ' + evTag(cls, `data-v="${i}" title="${esc(EV[cls][2] + (v.rationale ? ' — ' + v.rationale : ''))}"`);
  });
}

function stepHtml(key, ico, title, detail, body, cls = '') {
  const open = state.fold[key];
  return `<li class="step ${cls}"><span class="si">${icon(ico)}</span><div class="step-main">
    <div class="l"><b>${esc(title)}</b>${detail ? `<span class="d">${esc(detail)}</span>` : ''}</div>
    ${body ? `<details data-k="${esc(key)}"${open ? ' open' : ''}><summary>${CHEV}Details</summary>${body}</details>` : ''}
  </div></li>`;
}

/* Build one exchange: the operator's message, then ClawCal's turn. */
function turnHtml(d, isLast, keys) {
  const t = d.task, ev = d.events || [];
  const k = t.id;
  const mark = key => { keys.push(key); return state.seen.has(key) ? '' : ' enter'; };
  const out = [];

  /* 1. the operator's message */
  const files = (t.attachments || []).map(a =>
    `<span class="file-chip" title="${esc(a.title || a.doc_id)}">${ftChip(a.title, a.kind)}<span class="t">${esc(a.title || a.doc_id)}</span>${a.pages ? `<span class="hint">${a.pages} p</span>` : ''}</span>`).join('');
  out.push(`<div class="turn-u${mark(k + ':u')}"><div class="bubble">${esc(t.prompt || t.title)}</div>
    ${files ? `<div class="att-row">${files}</div>` : ''}</div>`);

  /* 2. the agent's turn */
  const live = LIVE.has(t.state);
  const body = [];

  /* meta: status, model, timing -- present, not shouting */
  const model = t.selected_model || (t.routing_reason === 'no model required' ? 'No model needed' : '');
  const meta = [statePill(t.state, t.state_reason)];
  if (model) meta.push(`<span title="${esc(t.routing_reason || '')}">${esc(model)}${rateOf(t.selected_model) ? ' · ' + esc(rateOf(t.selected_model)) : ''}</span>`);
  if (t.priority && t.priority !== 'MEDIUM') meta.push(`<span class="dot">${esc(prio(t.priority))} priority</span>`);
  if (t.runtime_s != null) meta.push(`<span class="dot">${fmtSec(t.runtime_s)}</span>`);
  if (t.policy_mode && MODES[t.policy_mode]) meta.push(`<span class="dot" title="Safety setting for this run">${esc(MODES[t.policy_mode].label)}</span>`);
  const ctl = ['RUNNING', 'QUEUED', 'PAUSED', 'ADMITTED'].includes(t.state) && canWrite()
    ? `<span class="ctl">${t.state === 'RUNNING' ? `<button class="btn sm ghost" data-a="pause" data-t="${esc(t.id)}">${icon('i-pause')}Pause</button>` : ''}
       ${t.state === 'PAUSED' ? `<button class="btn sm secondary" data-a="resume" data-t="${esc(t.id)}">${icon('i-play')}Resume</button>` : ''}
       <button class="btn sm danger" data-a="cancel" data-t="${esc(t.id)}">${icon('i-stop')}Stop</button></span>` : '';
  body.push(`<div class="meta-row">${meta.join('')}${ctl}</div>`);

  /* steps: every tool call, folded; setup folded inside that */
  const steps = [], notices = [], approvals = [], toolIcons = [];
  const decided = {};
  ev.forEach(e => {
    const p = payloadOf(e);
    if (['approval_granted', 'approval_denied', 'approval_decided'].includes(e.kind) && p.approval_id) {
      decided[p.approval_id] = e.kind === 'approval_granted' ? 'APPROVED'
        : e.kind === 'approval_denied' ? 'DENIED' : (p.state || 'DECIDED');
    }
  });
  const setup = ev.filter(e => SETUP.has(e.kind));
  if (setup.length) {
    steps.push(stepHtml(k + ':setup', 'i-gear', 'Got ready', `${setup.length} checks`,
      `<pre class="io">${esc(setup.map(e => e.label + (e.detail ? '\n  ' + e.detail : '')).join('\n'))}</pre>`));
  }
  const rest = ev.filter(e => !SETUP.has(e.kind));
  for (let i = 0; i < rest.length; i++) {
    const e = rest[i];
    if (QUIET.has(e.kind)) continue;
    if (e.kind === 'reasoning') {
      const n = (e.label || '').match(/\d+/);
      steps.push(stepHtml(k + ':r' + e.seq, 'i-spark', n ? `Thought it through` : (e.label || 'Thinking'), n ? `step ${n[0]}` : '',
        e.detail ? `<div class="think">${esc(e.detail)}</div>` : ''));
    } else if (e.kind === 'tool_call' || e.kind === 'observation') {
      let call = e, obs = null;
      if (e.kind === 'tool_call' && rest[i + 1] && rest[i + 1].kind === 'observation') { obs = rest[i + 1]; i++; }
      else if (e.kind === 'observation') { call = { label: e.label, seq: e.seq }; obs = e; }
      const name = payloadOf(obs).tool || (call.label || '').replace(/^Calling\s+/, '').replace(/\s*->.*$/, '').trim();
      const [label, ico] = toolOf(name);
      if (!toolIcons.includes(ico)) toolIcons.push(ico);
      const failed = obs && /->\s*(error|failed|denied)/i.test(obs.label || '');
      const p = payloadOf(obs);
      const badge = p.outcome && p.outcome !== 'ESTABLISHED' ? outcomeBadge(p.outcome, p.outcome_reason) : '';
      const bits = [];
      if (p.outcome && p.outcome !== 'ESTABLISHED' && p.outcome_reason) bits.push(`<div class="lab">Why</div><p class="hint">${esc(p.outcome_reason)}</p>`);
      if (call.detail) bits.push(`<div class="lab">Asked for</div><pre class="io">${esc(call.detail)}</pre>`);
      if (obs && obs.detail) bits.push(`<div class="lab">Got back</div><pre class="io">${esc(obs.detail)}</pre>`);
      steps.push(stepHtml(k + ':t' + (call.seq ?? i), ico, label,
        !obs ? 'running…' : failed ? "didn't work" : '', (badge ? `<p style="margin-top:6px">${badge}</p>` : '') + bits.join(''),
        !obs ? 'run' : failed ? 'bad' : ''));
    } else if (e.kind === 'approval_pending') {
      approvals.push(e);
    } else if (BAD.has(e.kind) || WARN.has(e.kind)) {
      notices.push(e);
    } else {
      /* retrieval, extraction, calculation, spreadsheet, deliverable,
         compacted, batch_item, tool_call_recovered … */
      const LABEL = { tool_call_recovered: 'Ran a tool call the model wrote as text',
        compacted: 'Summarised earlier steps to save memory' };
      const ico = { retrieval: 'i-search', extraction: 'i-docs', calculation: 'i-calc', spreadsheet: 'i-sheet',
        deliverable: 'i-out', compacted: 'i-list', batch_item: 'i-list', tool_call_recovered: 'i-refresh' }[e.kind] || 'i-spark';
      steps.push(stepHtml(k + ':e' + e.seq, ico, LABEL[e.kind] || e.label || human(e.kind), LABEL[e.kind] ? e.label : '',
        e.detail ? `<pre class="io">${esc(e.detail)}</pre>` : ''));
    }
  }
  if (steps.length) {
    const n = steps.length - (setup.length ? 1 : 0);
    const fk = k + ':steps';
    const open = state.fold[fk] ?? (live && isLast);
    const so = `${n} step${n === 1 ? '' : 's'}`;
    const head = t.state === 'RUNNING' || t.state === 'ADMITTED'
      ? `<span class="pixels"><i></i><i></i><i></i></span><b>Working</b> · ${so} so far`
      : t.state === 'PAUSED' ? `<b>Paused</b> · ${so} so far`
      : t.state === 'QUEUED' ? `<b>Waiting to start</b>`
      : n === 0 ? `<b>Setup</b> · ${setup.length} check${setup.length === 1 ? '' : 's'}`
      : `<b>${t.runtime_s != null ? 'Worked for ' + fmtSec(t.runtime_s) : 'Steps'}</b> · ${so}`;
    body.push(`<details class="steps${mark(fk)}" data-k="${esc(fk)}"${open ? ' open' : ''}>
      <summary>${CHEV}${head}<span class="icons">${toolIcons.slice(0, 4).map(i => `<span>${icon(i)}</span>`).join('')}</span></summary>
      <ol>${steps.join('')}</ol></details>`);
  }

  /* notices */
  notices.forEach(e => {
    const p = payloadOf(e);
    let [title, ico] = NOTICE[e.kind] || [e.label || human(e.kind), 'i-info'];
    if (e.kind === 'loop_detected' && p.tool) title = `ClawCal kept repeating "${toolOf(p.tool)[0].toLowerCase()}", so it stopped`;
    const bad = BAD.has(e.kind);
    const why = e.detail || '';
    const plain = why && !technical(why) ? cap(why) : bad ? explain({ status: 500, message: why }, 'finish this').text : '';
    const raw = [technical(why) ? why : '', p.summary].filter(Boolean).join('\n\n');
    body.push(`<div class="notice ${bad ? 'bad' : 'warn'}${mark(k + ':n' + e.seq)}"><span class="ni">${icon(ico)}</span><div>
      <b>${esc(title)}</b> ${outcomeBadge(p.outcome)}
      ${plain ? `<p>${esc(plain)}</p>` : ''}
      ${raw ? `<details><summary>Technical details</summary><pre class="io">${esc(raw)}</pre></details>` : ''}
    </div></div>`);
  });

  /* approvals: the one amber object */
  approvals.forEach(e => {
    const p = payloadOf(e);
    const aid = p.approval_id;
    const name = (e.label || '').replace(/^Approval required:\s*/, '').trim();
    const isQuestion = /human decision/i.test(e.label || '');
    const livePending = aid && state.pending.has(aid) && !decided[aid];
    const outcome = decided[aid] || (!livePending && TERMINAL.has(t.state) ? 'CLOSED' : null);
    const verb = isQuestion ? 'needs your decision' : `wants to ${toolOf(name)[2]}`;
    const key = k + ':a' + (aid || e.seq);
    const fresh = livePending && !state.seen.has(key);
    keys.push(key);
    const doneTxt = outcome === 'APPROVED' ? 'You allowed this' : outcome === 'DENIED' || outcome === 'REJECTED' ? 'Not allowed' : outcome ? 'Closed' : 'Waiting for a decision';
    body.push(`<div class="approve${livePending ? (fresh ? ' fresh' : '') : ' decided'}">
      <div class="top"><span class="ic">${icon(outcome === 'APPROVED' ? 'i-check' : outcome ? 'i-x' : 'i-hand')}</span><div>
        <b>${livePending ? `ClawCal ${esc(verb)}` : esc(doneTxt)}</b>
        <p>${esc(livePending ? (e.detail || 'This chat is set to Ask first.') : `ClawCal asked to ${toolOf(name)[2]}.`)}</p></div></div>
      ${p.summary ? `<div class="what"><pre class="io">${esc(p.summary)}</pre></div>` : ''}
      ${livePending ? `<div class="acts">${state.canDecide !== false
        ? `<button class="btn sm primary" data-appr="${esc(aid)}" data-ok="1">${icon('i-check')}Allow</button>
           <button class="btn sm secondary" data-appr="${esc(aid)}" data-ok="0">Don't allow</button>`
        : '<span class="who-can">Waiting for someone with approval rights.</span>'}</div>` : '<div style="height:12px"></div>'}
    </div>`);
  });

  /* the answer */
  const res = t.result || {};
  if (res.summary) {
    const c = (res.provenance && res.provenance.counts) || {};
    const o = res.outcome || {};
    const arts = (res.artifacts || d.artifacts || []).map(a =>
      `<a class="outfile" href="/api/artifacts/${esc(a.id)}/download">${ftChip(a.name, a.kind)}<span>${esc(a.name)}<small>${a.bytes ? fmtBytes(a.bytes) + ' · ' : ''}saved to Outputs</small></span>${icon('i-down', 'dl')}</a>`).join('');
    const counts = ['A', 'B', 'C', 'D'].filter(x => c[x]).map(x => `${c[x]} ${EV[x][0].toLowerCase()}`).join(', ');
    body.push(`<div class="answer${mark(k + ':ans')}" data-task="${esc(t.id)}">
      ${o.headline || (o.degraded || []).length ? `<div class="answer-head">${outcomeBadge(o.headline, o.label)}${
        String(res.summary || '').match(/^\s*CANNOT DETERMINE/i) && o.headline !== 'CANNOT_DETERMINE' ? outcomeBadge('CANNOT_DETERMINE') : ''}</div>` : ''}
      ${(o.degraded || []).length ? `<div class="degraded">${icon('i-info')}<div><b>Produced in a limited mode.</b> ${
        o.degraded.filter(x => !technical(x)).map(esc).join(' ')}${o.degraded.some(technical)
        ? `<details><summary>Why</summary><pre class="io">${esc(o.degraded.filter(technical).join('\n\n'))}</pre></details>` : ''}</div></div>` : ''}
      <div class="md">${answerHtml(res)}</div>
      ${arts ? `<div class="att-row" style="justify-content:flex-start;margin-top:14px">${arts}</div>` : ''}
      <div class="answer-foot">${counts ? `<span>${esc(counts)}</span>` : ''}<span class="sp"></span>
        ${t.workflow === 'sovereignty_proof' && t.state === 'COMPLETED'
          ? `<button type="button" class="btn sm secondary" data-report="${esc(t.id)}">${icon('i-shield')}Get signed report</button>` : ''}
        <button type="button" class="copy-btn" data-copy="${esc(t.id)}">${icon('i-docs')}Copy</button></div>
    </div>`);
  } else if (live && approvals.some(e => state.pending.has(payloadOf(e).approval_id) && !decided[payloadOf(e).approval_id])) {
    body.push(`<div class="working queued"><span class="pixels"><i></i><i></i><i></i></span><span>Waiting for your OK above</span></div>`);
  } else if (live) {
    const reason = String(t.state_reason || '');
    let line, sub = '';
    if (t.state === 'PAUSED') {
      line = 'Paused';
      sub = /restart/i.test(reason) ? 'The workbench restarted during this run. Resume carries on from where it stopped.'
        : reason && !technical(reason) ? cap(reason) : 'Resume to carry on.';
    } else if (t.state === 'QUEUED' && resourceWait(reason)) {
      /* Held back by the machine, not by the queue: say what it waits for,
         in plain words, and never pair it with a "starts immediately" guess. */
      [line, sub] = resourceWait(reason);
    } else if (t.state === 'QUEUED') {
      line = 'Waiting its turn';
      /* The wait estimate states its own basis ("measured, 9 samples"); show it as-is. */
      sub = [reason && !technical(reason) && !/^queued$/i.test(reason) ? cap(reason) : '', t.wait && t.wait.text ? t.wait.text : ''].filter(Boolean).join(' · ');
    } else {
      line = t.state === 'ADMITTED' ? 'Starting up' : 'Working on it';
      sub = reason && !/agent executing/.test(reason) && !technical(reason) ? cap(reason) : '';
    }
    body.push(`<div class="working${t.state === 'RUNNING' || t.state === 'ADMITTED' ? '' : ' queued'}"><span class="pixels"><i></i><i></i><i></i></span>
      <span>${esc(line)}${sub ? `<span class="sub"> · ${esc(sub)}</span>` : ''}</span></div>`);
  } else if (t.state !== 'COMPLETED' && !(t.state === 'FAILED' && notices.some(e => e.kind === 'failed'))) {
    /* A run that already explained its failure in a notice is not told twice. */
    const why = t.error || t.state_reason || '';
    const plain = why && !technical(why);
    const x = explain({ status: 500, message: why }, 'finish this');
    body.push(`<div class="notice ${t.state === 'TERMINATED' ? 'warn' : 'bad'}${mark(k + ':end')}"><span class="ni">${icon(t.state === 'TERMINATED' ? 'i-stop' : 'i-warn')}</span><div>
      <b>${t.state === 'TERMINATED' ? 'This run was stopped' : t.state === 'REJECTED' ? "This run couldn't start" : "This run didn't finish"}</b>
      <p>${esc(plain ? cap(why) + '.' : x.text)} ${t.state !== 'TERMINATED' && canWrite() ? 'You can send the message again.' : ''}</p>
      ${why && !plain ? `<details><summary>Technical details</summary><pre class="io">${esc(why)}</pre></details>` : ''}
    </div></div>`);
  }

  out.push(`<div class="turn-a${live ? ' live' : ''}${mark(k + ':a')}"><span class="who">${LOGO}</span><div class="a-body">${body.join('')}</div></div>`);
  return out.join('');
}

/* Why a queued run is held back by the machine itself, in plain words. */
function resourceWait(reason) {
  const r = String(reason || '');
  if (/host memory|OOM|memory stalls|swap is/i.test(r))
    return ['Waiting for free memory',
      'loading the AI model right now could crash this computer, so the run starts by itself as soon as enough memory is free. Closing other apps makes that sooner.'];
  if (/VRAM|graphics|GPU/i.test(r))
    return ['Waiting for the graphics card', 'another model is using it; this run starts when it is free.'];
  if (/no enabled local model|no model/i.test(r))
    return ['No AI model available', 'an administrator needs to add or enable a model under Admin → Models.'];
  return null;
}

function renderChat(turns) {
  const el = $('#thread');
  if (!turns.length) { paint(el, emptyState('i-chat', 'This chat is empty', 'Send a message to start.')); return false; }
  const keys = [];
  const html = turns.map((d, i) => turnHtml(d, i === turns.length - 1, keys)).join('');
  const changed = paint(el, html);
  keys.forEach(k => state.seen.add(k));
  if (!changed) return false;
  bindThread(turns);
  return true;
}

/* Fold state survives repaints, so nothing the operator opened slams shut on
   the next poll. `toggle` does not bubble, so listen in the capture phase. */
$('#thread').addEventListener('toggle', e => {
  const k = e.target.dataset && e.target.dataset.k;
  if (k) state.fold[k] = e.target.open;
}, true);

function bindThread(turns) {
  $$('#thread [data-appr]').forEach(b => b.onclick = async () => {
    const card = b.closest('.approve');
    card.querySelectorAll('button').forEach(x => x.disabled = true);
    b.classList.add('busy');
    const ok = b.dataset.ok === '1';
    try {
      await post('/api/approvals/' + enc(b.dataset.appr), { approve: ok });
      toast({ kind: ok ? 'ok' : 'info', title: ok ? 'Allowed' : 'Not allowed', text: ok ? 'ClawCal is carrying on.' : 'ClawCal will continue without doing that.' });
    } catch (e) { toastError(e, 'record your decision'); }
    finally { state.pending.delete(b.dataset.appr); refreshApprovals(); loadChat(); }
  });
  $$('#thread .meta-row [data-a]').forEach(b => b.onclick = async () => {
    b.disabled = true; b.classList.add('busy');
    const verb = { pause: 'pause', resume: 'resume', cancel: 'stop' }[b.dataset.a];
    try { await post(`/api/tasks/${enc(b.dataset.t)}/${b.dataset.a}`, {}); }
    catch (e) { toastError(e, verb + ' this run'); }
    finally { loadChat(); }
  });
  $$('#thread button.ev[data-v]').forEach(b => b.onclick = () => {
    const d = turns.find(x => x.task.id === b.closest('.answer')?.dataset.task);
    if (!d) return;
    const verdicts = (((d.task.result || {}).provenance || {}).verdicts || []).slice().sort((a, c) => a.start - c.start);
    showEvidence(verdicts[+b.dataset.v], d);
  });
  $$('#thread [data-report]').forEach(b => b.onclick = async () => {
    b.disabled = true; b.classList.add('busy');
    try {
      const r = await post('/api/sovereignty/report/' + enc(b.dataset.report));
      toast({ kind: r.all_blocked ? 'ok' : 'warn', title: r.all_blocked ? 'Signed report ready' : 'Report ready — not every attempt was blocked',
        text: `Fingerprint ${String(r.fingerprint || '').slice(0, 16)}…`, ttl: 15000,
        action: ['Download', () => { const a = document.createElement('a'); a.href = '/api/artifacts/' + enc(r.artifact_id) + '/download'; document.body.appendChild(a); a.click(); a.remove(); }] });
      loadSystem().catch(() => { });
    } catch (e) { toastError(e, 'make the report'); }
    b.disabled = false; b.classList.remove('busy');
  });
  $$('#thread [data-copy]').forEach(b => b.onclick = async () => {
    const d = turns.find(x => x.task.id === b.dataset.copy);
    const text = d?.task?.result?.summary || '';
    try { await navigator.clipboard.writeText(text); b.innerHTML = icon('i-check') + 'Copied'; }
    catch (_) { b.innerHTML = icon('i-x') + 'Copy blocked'; }
    setTimeout(() => { b.innerHTML = icon('i-docs') + 'Copy'; }, 1600);
  });
}

/* context budget: surfaced before the agent starts forgetting things */
function paintContext(t) {
  const pill = $('#ctxPill');
  const budget = Number(t.context_budget_tokens || 0), used = Number(t.context_used_tokens || 0);
  if (!budget || !used) { pill.hidden = true; return; }
  const left = Math.max(0, Math.min(100, Math.round(100 - (used / budget) * 100)));
  const compacted = Number(t.compacted_messages || 0);
  pill.hidden = false;
  pill.classList.toggle('low', left <= 20);
  pill.textContent = `${left}% memory left`;
  pill.title = `${used.toLocaleString()} of ${budget.toLocaleString()} tokens used by ${t.selected_model || 'the model'}.`
    + (compacted ? `\n${compacted} earlier message(s) were summarised to stay inside the budget.`
      : '\nOlder tool output is summarised automatically when this runs low.');
}

/* ============================================================== evidence */
async function showEvidence(v, d) {
  const dlg = $('#evidenceView'), bodyEl = $('#evidenceBody');
  if (!v) return;
  const cls = v['class'];
  const e = EV[cls] || [cls, 'i-info', ''];
  const head = `<div class="ev-meta">${evTag(cls)}
    <div class="v">${esc(v.value)}</div>
    <p class="rat">${esc(v.rationale || e[2])}</p>
    ${v.context ? `<blockquote>“${esc(v.context)}”</blockquote>` : ''}</div>`;
  bodyEl.innerHTML = head;
  dlg.showModal();

  if (cls === 'B' && v.calc_id) {
    const c = (d.calculations || []).find(x => x.id === v.calc_id);
    if (c) {
      let steps = {};
      try { steps = typeof c.steps === 'string' ? JSON.parse(c.steps) : (c.steps || {}); } catch (_) { }
      bodyEl.innerHTML = head + `<div class="lab">Calculation ${esc(c.id)}</div>
        <pre class="io">${esc(c.expression)} = ${esc(c.result)} ${esc(c.unit || '')}${steps.substituted ? '\n' + esc(steps.substituted) : ''}</pre>`;
    }
    return;
  }
  if (cls !== 'A') return;
  /* The source passage: the first evidence row for this run whose text holds
     the value as written. */
  const num = (String(v.value).match(/[\d.,]+/) || [''])[0];
  const row = (d.evidence || []).find(x => num && (x.snippet || '').includes(num)) || (d.evidence || [])[0];
  if (!row || !row.doc_id) return;
  let region = row.region;
  try { region = typeof region === 'string' ? JSON.parse(region) : region; } catch (_) { region = null; }
  bodyEl.innerHTML = head + skeleton();
  let page = null;
  try {
    const pages = (await api(`/api/documents/${enc(row.doc_id)}/pages`)).pages || [];
    page = pages.find(p => p.page_no === row.page_no);
  } catch (_) { }
  const where = `${esc(row.doc_id)} · page ${esc(row.page_no)}`;
  if (!page || !page.has_image) {
    bodyEl.innerHTML = head + `<p class="hint">From ${where}. No page image is available for this document, so the passage is shown as text.</p>
      <pre class="io" style="margin-top:8px">${esc(row.snippet || '')}</pre>`;
    return;
  }
  let box = '';
  if (region && region.x1 && page.width && page.height) {
    const pct = (a, b) => (100 * a / b).toFixed(2) + '%';
    box = `<div class="hl" style="left:${pct(region.x0, page.width)};top:${pct(region.y0, page.height)};
      width:${pct(region.x1 - region.x0, page.width)};height:${pct(region.y1 - region.y0, page.height)}"></div>`;
  }
  const src = `/api/documents/${enc(row.doc_id)}/page/${row.page_no}/image`;
  const img = AUTH.token ? URL.createObjectURL(await (await authed(src)).blob()) : src;
  bodyEl.innerHTML = head + `<p class="hint" style="margin-bottom:10px">From ${where}${region ? ' — highlighted below' :
    ' — the exact spot could not be located, so the whole page is shown'}</p>
    <div class="ev-page"><img alt="Source page" src="${esc(img)}">${box}</div>`;
}

/* =============================================================== library */
$$('#libTabs button').forEach(b => b.onclick = () => {
  state.libTab = b.dataset.t;
  $$('#libTabs button').forEach(x => { x.classList.toggle('on', x === b); x.setAttribute('aria-selected', String(x === b)); });
  $('#docPane').hidden = state.libTab !== 'docs';
  $('#dwgPane').hidden = state.libTab !== 'drawings';
  $('#libDetail').__html = null;
  viewLibrary();
});
$('#libUpload').onclick = () => $('#libFile').click();
$('#libFile').onchange = async () => {
  const f = $('#libFile').files[0];
  if (!f) return;
  const b = $('#libUpload');
  b.disabled = true; b.classList.add('busy');
  const t = toast({ kind: 'info', title: `Reading ${f.name}`, text: 'Indexing it on this machine…', ttl: 60000 });
  try {
    const r = await uploadFile(f);
    t.remove();
    toast({ kind: 'ok', title: `${r.title} is in your library`, text: r.note });
    state.doc = r.doc_id;
    viewLibrary();
  } catch (e) { t.remove(); toastError(e, 'add that file'); }
  b.disabled = false; b.classList.remove('busy'); $('#libFile').value = '';
};

function viewLibrary() { return state.libTab === 'docs' ? viewDocs() : viewDrawings(); }

async function viewDocs() {
  let documents;
  try { ({ documents } = await api('/api/documents')); }
  catch (e) { failView('#docList', e, viewDocs); return; }
  setCount('#cDocs2', documents.length); setCount('#cDocs', documents.length);
  if (!state.doc && documents.length) state.doc = documents[0].id;
  if (paint('#docList', documents.length ? documents.map(d => `
    <li><button type="button" class="item ${d.id === state.doc ? 'on' : ''}" data-id="${esc(d.id)}">
      ${ftChip(d.title, d.kind || d.doc_class)}
      <span><b>${esc(d.title)}</b><span class="meta">
        <span>${d.pages} page${d.pages === 1 ? '' : 's'}</span>
        ${d.doc_class ? `<span>${esc(human(d.doc_class))}</span>` : ''}
        ${d.status !== 'READY' ? `<span class="bad">${esc(human(d.status))}</span>` : ''}
      </span></span></button></li>`).join('')
    : `<li>${emptyState('i-lib', 'Your library is empty', 'Add a document, or attach one to a chat.')}</li>`)) {
    $$('#docList .item').forEach(b => b.onclick = () => { state.doc = b.dataset.id; $$('#docList .item').forEach(x => x.classList.toggle('on', x === b)); showDoc(b.dataset.id, documents); });
  }
  if (state.doc) showDoc(state.doc, documents);
  else paint('#libDetail', '');
}

async function showDoc(id, documents) {
  const d = (documents || []).find(x => x.id === id) || {};
  try {
    const p = await api(`/api/documents/${enc(id)}/pages`);
    paint('#libDetail', `<div class="detail-h"><h2>${esc(d.title || id)}</h2>${d.status ? `<span class="pill ${d.status === 'READY' ? 'ok' : 'bad'}"><i class="px"></i>${d.status === 'READY' ? 'Ready' : esc(human(d.status))}</span>` : ''}</div>
      ${d.status_detail ? `<p class="note">${esc(d.status_detail)}</p>` : ''}
      ${p.pages.length ? p.pages.map(pg => `
      <div class="doc-page">
        <div class="doc-page-h"><b>Page ${pg.page_no}</b><span>read by ${esc(pg.extractor || '—')}${pg.ocr_conf ? ` · ${Math.round(pg.ocr_conf)}% confidence` : ''}</span></div>
        ${pg.has_image ? `<img src="/api/documents/${esc(id)}/page/${pg.page_no}/image" alt="Page ${pg.page_no}" loading="lazy">` : ''}
        <pre class="out">${esc(pg.text || '(no text could be read from this page)')}</pre></div>`).join('')
      : emptyState('i-docs', 'No readable pages', 'ClawCal could not read any text from this document.')}`);
  } catch (e) { failView('#libDetail', e, () => showDoc(id, documents)); }
}

async function viewDrawings() {
  let drawings;
  try { ({ drawings } = await api('/api/drawings')); }
  catch (e) { failView('#dwgList', e, viewDrawings); return; }
  setCount('#cDwg', drawings.length);
  if (!state.drawing && drawings.length) state.drawing = drawings[0].id;
  if (paint('#dwgList', drawings.length ? drawings.map(d => `
    <li><button type="button" class="item ${d.id === state.drawing ? 'on' : ''}" data-id="${esc(d.id)}">
      <span class="ft dwg">${esc((d.source_kind || 'dwg').slice(0, 4))}</span>
      <span><b>${esc(d.title)}</b><span class="meta">
        <span>${d.summary?.symbols || 0} symbols</span>
        <span>${d.summary?.confirmed_connectivity || 0} confirmed lines</span></span></span></button></li>`).join('')
    : `<li>${emptyState('i-draw', 'No drawings yet', 'Analyse one below, or ask about a drawing in a chat.')}</li>`)) {
    $$('#dwgList .item').forEach(b => b.onclick = () => { state.drawing = b.dataset.id; $$('#dwgList .item').forEach(x => x.classList.toggle('on', x === b)); showDrawing(b.dataset.id); });
  }
  if (state.drawing) showDrawing(state.drawing);
  else paint('#libDetail', '');
}

async function showDrawing(id) {
  let d;
  try { d = await api('/api/drawings/' + enc(id)); }
  catch (e) { failView('#libDetail', e, () => showDrawing(id)); return; }
  const cs = getComputedStyle(document.documentElement);
  const col = n => cs.getPropertyValue(n).trim();
  const colour = s => s === 'CONFIRMED' ? col('--ok') : s === 'PROBABLE' ? col('--signal') : col('--bad');
  const blue = col('--info');
  const svg = `<svg viewBox="0 0 ${d.width} ${d.height}" preserveAspectRatio="none" aria-hidden="true">
    ${d.edges.map(e => `<polyline points="${e.polyline.map(p => p.join(',')).join(' ')}"
        fill="none" stroke="${colour(e.status)}" stroke-width="2.4"
        ${e.line_type === 'instrument_signal' ? 'style="stroke-dasharray:5 4;animation:none;stroke-dashoffset:0"' : ''} opacity=".9"/>`).join('')}
    ${d.symbols.map(s => `<rect x="${s.bbox[0]}" y="${s.bbox[1]}" width="${s.bbox[2] - s.bbox[0]}" height="${s.bbox[3] - s.bbox[1]}"
        fill="none" stroke="${blue}" stroke-width="1.4" opacity=".9"/>
      ${s.tag ? `<text x="${s.bbox[0]}" y="${s.bbox[1] - 3}" fill="${blue}" font-size="9" font-family="monospace">${esc(s.tag)}</text>` : ''}`).join('')}
  </svg>`;
  const byStatus = g => d.edges.filter(e => e.status === g);
  const nameOf = i => (d.symbols.find(s => s.id.endsWith(i))?.tag) || (d.symbols.find(s => s.id.endsWith(i))?.label) || i;
  const sum = d.summary || {};
  paint('#libDetail', `
    <div class="detail-h"><h2>${esc(d.title)}</h2><span class="pill info">${esc(human(d.source_kind))} source</span></div>
    <div class="dwg"><img src="/api/drawings/${esc(id)}/image" alt="${esc(d.title)}">${svg}</div>
    <div class="dwg-key">
      <span><i style="background:var(--ok)"></i>Confirmed</span>
      <span><i style="background:var(--signal)"></i>Probable</span>
      <span><i style="background:var(--bad)"></i>Unresolved</span>
      <span><i style="background:var(--info)"></i>Detected symbol</span></div>
    <div class="stats">
      <div class="stat"><b>${d.symbols.length}</b><span>symbols found</span></div>
      <div class="stat"><b>${byStatus('CONFIRMED').length}</b><span>lines confirmed</span></div>
      <div class="stat"><b>${byStatus('PROBABLE').length}</b><span>lines probable</span></div>
      <div class="stat"><b>${byStatus('UNRESOLVED').length}</b><span>lines unresolved</span></div>
    </div>
    ${(sum.warnings || []).length ? `<h2 class="h-sec">What this reading does not establish</h2>
      <ul class="warnings">${sum.warnings.map(w => `<li>${esc(w)}</li>`).join('')}</ul>` : ''}
    <h2 class="h-sec">Equipment and instruments</h2>
    <div class="tw"><table><thead><tr><th>Tag</th><th>Kind</th><th class="r">Confidence</th><th>How it was found</th></tr></thead><tbody>
      ${d.symbols.map(s => `<tr><td class="mono"><b>${esc(s.tag || '—')}</b></td><td>${esc(human(s.sym_class))}</td>
        <td class="num">${Math.round((s.confidence || 0) * 100)}%<span class="bar-cell"><i style="width:${Math.round((s.confidence || 0) * 100)}%"></i></span></td>
        <td>${esc(s.method)}</td></tr>`).join('') || emptyRow(4, 'i-draw', 'No symbols detected', '')}
    </tbody></table></div>
    ${['CONFIRMED', 'PROBABLE', 'UNRESOLVED'].map(g => {
      const rows = byStatus(g);
      if (!rows.length) return '';
      const [label, pc, note] = {
        CONFIRMED: ['Confirmed connections', 'ok', 'established by the geometry — safe to state as fact'],
        PROBABLE: ['Probable connections', 'wait', 'an interpretation — labelled as such'],
        UNRESOLVED: ['Unresolved', 'bad', 'the drawing does not establish these'] }[g];
      return `<h2 class="h-sec"><span class="pill ${pc}"><i class="px"></i>${label}</span><span class="aside">${note}</span></h2>
        <div class="tw"><table><tbody>${rows.map(e => `<tr>
          <td class="mono nowrap"><b>${esc(nameOf(e.src))}</b> → <b>${esc(nameOf(e.dst))}</b></td>
          <td>${esc(human(e.line_type))}</td><td>${esc(e.rationale || '')}</td></tr>`).join('')}</tbody></table></div>`;
    }).join('')}`);
}

$('#dwgBtn').onclick = async () => {
  const b = $('#dwgBtn');
  b.disabled = true; b.classList.add('busy');
  try {
    const r = await post('/api/drawings/analyse', { path: $('#dwgPath').value, use_vlm: false });
    state.drawing = r.drawing_id;
    toast({ kind: 'ok', title: 'Drawing analysed', text: `${r.summary.symbols} symbols and ${r.summary.confirmed_connectivity} confirmed connections found.` });
    viewDrawings();
  } catch (e) { toastError(e, 'analyse that drawing'); }
  b.disabled = false; b.classList.remove('busy');
};

/* =============================================================== outputs */
async function viewOutputs() {
  try {
    const { artifacts } = await api('/api/artifacts');
    setCount('#cArt', artifacts.length);
    paint('#artGrid', artifacts.length ? artifacts.map((a, i) => `
      <div class="file-card" style="animation-delay:${Math.min(i, 12) * 30}ms">
        <div class="top">${ftChip(a.name, a.kind)}<div><b>${esc(a.name)}</b><small>${esc(human(a.kind))} · ${fmtBytes(a.bytes)}${a.created_at ? ' · ' + ago(a.created_at) : ''}</small></div></div>
        <div class="hash" title="SHA-256 ${esc(a.sha256 || '')}">SHA-256 ${esc((a.sha256 || '').slice(0, 24))}…</div>
        <div class="row">${a.exists
          ? `<a class="btn sm secondary" href="/api/artifacts/${esc(a.id)}/download">${icon('i-down')}Download</a>`
          : '<span class="pill bad"><i class="px"></i>File missing</span>'}</div>
      </div>`).join('')
      : `<div style="grid-column:1/-1">${emptyState('i-out', 'Nothing made yet', 'Ask ClawCal for a report, spreadsheet or presentation and it will appear here.')}</div>`);
  } catch (e) { failView('#artGrid', e, viewOutputs); }
}

/* ============================================================= approvals */
async function refreshApprovals() {
  try {
    const a = await api('/api/approvals');
    state.pending = new Set(a.pending.map(x => x.id));
    state.canDecide = a.can_decide;
    state.apprTask = Object.fromEntries(a.pending.map(x => [x.id, x.task_id]));
    setCount('#cAppr', a.pending.length);
    paintChatList();
    return a;
  } catch (_) { return null; }
}

async function viewApprovals() {
  let a;
  try { a = await api('/api/approvals'); }
  catch (e) { failView('#pendingApprovals', e, viewApprovals); return; }
  state.pending = new Set(a.pending.map(x => x.id));
  state.apprTask = Object.fromEntries(a.pending.map(x => [x.id, x.task_id]));
  setCount('#cAppr', a.pending.length);
  if (paint('#pendingApprovals', a.pending.length ? a.pending.map(p => `
    <div class="approve">
      <div class="top"><span class="ic">${icon('i-hand')}</span><div>
        <b>ClawCal wants to ${esc(toolOf(p.tool)[2])}</b>
        <p>${esc(ago(p.created_at))}${p.owner ? ' · in ' + esc(p.owner) + "'s chat" : ''}</p></div></div>
      <div class="what"><pre class="io">${esc(p.summary)}</pre></div>
      <div class="acts">${a.can_decide ? `
        <button class="btn sm primary" data-id="${esc(p.id)}" data-ok="1">${icon('i-check')}Allow</button>
        <button class="btn sm secondary" data-id="${esc(p.id)}" data-ok="0">Don't allow</button>`
        : '<span class="who-can">Waiting for someone with approval rights.</span>'}
        <span class="grow"></span>
        ${p.session_id ? `<button class="btn sm ghost" data-open="${esc(p.session_id)}" data-task="${esc(p.task_id)}">Open chat</button>` : ''}</div>
    </div>`).join('')
    : emptyState('i-check', 'Nothing is waiting for you', 'When a chat is set to Ask first, ClawCal will ask here before it changes anything.'))) {
    $$('#pendingApprovals [data-id]').forEach(b => b.onclick = async () => {
      $$('#pendingApprovals [data-id]').forEach(x => x.disabled = true);
      b.classList.add('busy');
      const ok = b.dataset.ok === '1';
      try { await post('/api/approvals/' + enc(b.dataset.id), { approve: ok }); toast({ kind: ok ? 'ok' : 'info', title: ok ? 'Allowed' : 'Not allowed' }); }
      catch (e) { toastError(e, 'record your decision'); }
      finally { viewApprovals(); }
    });
    $$('#pendingApprovals [data-open]').forEach(b => b.onclick = () => openChat(b.dataset.open, b.dataset.task));
  }
  paint('#apprRows', a.recent.length ? a.recent.map(r => `<tr>
    <td><b>${esc(toolOf(r.tool)[0])}</b><span class="sub mono">${esc(r.tool)}</span></td>
    <td>${esc((r.summary || '').slice(0, 170))}</td>
    <td>${statePill(r.state)}</td><td>${esc(r.decided_by || '—')}</td><td class="nowrap">${ago(r.decided_at || r.created_at)}</td></tr>`).join('')
    : emptyRow(5, 'i-list', 'No decisions yet', ''));
}

/* ================================================================= admin */
$$('#adminTabs button').forEach(b => b.onclick = () => { setAdminTab(b.dataset.t); refreshView(); syncUrl(); });
function setAdminTab(t) {
  state.adminTab = t;
  $$('#adminTabs button').forEach(x => { x.classList.toggle('on', x.dataset.t === t); x.setAttribute('aria-selected', String(x.dataset.t === t)); });
  $$('#v-admin .apane').forEach(p => p.classList.toggle('on', p.id === 'a-' + t));
}

/* a ring gauge: one SVG, one transition, no loop */
function gauge(label, pct, big, sub) {
  const C = 2 * Math.PI * 26;
  const p = Math.max(0, Math.min(100, pct || 0));
  const cls = p > 92 ? 'bad' : p > 78 ? 'warn' : '';
  return `<div class="gauge ${cls}"><svg viewBox="0 0 64 64"><circle class="trk" cx="32" cy="32" r="26"/>
    <circle class="val" cx="32" cy="32" r="26" stroke-dasharray="${C.toFixed(1)}" stroke-dashoffset="${(C * (1 - p / 100)).toFixed(1)}"/></svg>
    <div><span class="lbl">${esc(label)}</span><b>${esc(big)}</b><span>${esc(sub || '')}</span></div></div>`;
}
function paintGauges(live) {
  const el = $('#gauges');
  if (!el || !live) return;
  const g = live.gpu || {}, m = live.memory || {}, c = live.cpu || {};
  const gb = mb => (mb / 1024).toFixed(1);
  const html = [
    gauge('GPU memory', g.total_mb ? 100 * g.used_mb / g.total_mb : 0, g.total_mb ? `${gb(g.used_mb)} GB` : '—', g.total_mb ? `of ${gb(g.total_mb)} GB` : 'no GPU'),
    gauge('GPU load', g.util_pct, `${Math.round(g.util_pct || 0)}%`, g.temp_c ? `${Math.round(g.temp_c)} °C` : ''),
    gauge('System memory', m.total_mb ? 100 * m.used_mb / m.total_mb : 0, m.total_mb ? `${gb(m.used_mb)} GB` : '—', m.total_mb ? `of ${gb(m.total_mb)} GB` : ''),
    gauge('CPU', c.util_pct, `${Math.round(c.util_pct || 0)}%`, c.logical ? `${c.logical} threads` : ''),
  ];
  /* Update rings in place so the stroke transitions instead of jumping. */
  if (el.children.length === html.length) {
    const tmp = document.createElement('div'); tmp.innerHTML = html.join('');
    Array.from(tmp.children).forEach((n, i) => {
      const cur = el.children[i];
      cur.className = n.className;
      cur.querySelector('.val').setAttribute('stroke-dashoffset', n.querySelector('.val').getAttribute('stroke-dashoffset'));
      cur.querySelector('div').innerHTML = n.querySelector('div').innerHTML;
    });
  } else el.innerHTML = html.join('');
}

async function viewRuntime(pre) {
  let r = pre;
  if (!r) {
    try { r = await api('/api/runtime'); }
    catch (e) { failView('#runRows', e, () => viewRuntime(), 4); return; }
  }
  paint('#runRows', r.running.length ? r.running.map(t => `<tr>
    <td><b>${esc(t.title)}</b></td><td>${esc(prio(t.priority))}</td>
    <td class="mono">${esc(t.selected_model || '—')}</td><td class="num">${t.steps_used || 0}</td></tr>`).join('')
    : emptyRow(4, 'i-pulse', 'Nothing running', 'The workbench is idle.'));
  paint('#queueRows', r.queued.length ? r.queued.map(t => `<tr>
    <td><b>${esc(t.title)}</b></td><td>${esc(prio(t.priority))}</td>
    <td class="num mono">${(t.effective_priority ?? 0).toFixed(2)}</td>
    <td>${esc(t.state_reason || '')}</td></tr>`).join('')
    : emptyRow(4, 'i-check', 'No queue', 'New work starts straight away.'));

  const res = r.residency;
  paint('#residency', res.resident.length ? `<div class="tw"><table><thead><tr>
    <th>Model</th><th class="r">GPU memory</th><th class="r">On GPU</th><th class="r">Loaded for</th><th class="r">Chats served</th>
    <th>Can unload</th></tr></thead><tbody>` + res.resident.map(m => `<tr>
      <td class="mono"><b>${esc(m.model)}</b></td>
      <td class="num">${Math.round(m.vram_mb).toLocaleString()} MB</td>
      <td class="num">${Math.round(m.gpu_fraction * 100)}%<span class="bar-cell"><i style="width:${Math.round(m.gpu_fraction * 100)}%"></i></span></td>
      <td class="num">${fmtSec(m.dwell_s)}</td>
      <td class="num">${m.tasks_served}</td>
      <td>${m.evictable ? '<span class="pill ok"><i class="px"></i>Yes</span>' : '<span class="pill wait" title="A model must stay loaded for a minimum time, so the scheduler does not thrash"><i class="px"></i>Not yet</span>'}</td>
    </tr>`).join('') + '</tbody></table></div>'
    : `<div class="card">${emptyState('i-chip', 'No model loaded', 'The next chat loads the model it needs.')}</div>`);
  $('#residencyNote').textContent =
    `${Math.round(res.free_vram_mb).toLocaleString()} MB free of ${Math.round(res.total_vram_mb).toLocaleString()} MB `
    + `(${Math.round(res.usable_vram_mb).toLocaleString()} MB usable). A loaded model stays for at least ${res.min_dwell_s} s, which stops the scheduler swapping models back and forth.`;

  paint('#limits', Object.entries(r.limits).map(([k, v]) =>
    `<dt>${esc(human(k))}</dt><dd class="${monoish(v) ? 'mono' : ''}">${esc(v)}</dd>`).join(''));

  paint('#admissionRows', r.admissions.length ? r.admissions.slice().reverse().slice(0, 30).map(a => `<tr>
    <td><span class="pill ${a.outcome === 'ADMIT' ? 'ok' : a.outcome === 'REJECT' ? 'bad' : 'wait'}"><i class="px"></i>${esc(human(a.outcome))}</span></td>
    <td><b title="${esc(a.title || '')}">${esc((a.title || '').slice(0, 42))}${(a.title || '').length > 42 ? '…' : ''}</b></td><td class="mono nowrap">${esc(a.model || '—')}</td>
    <td>${esc(a.reason || '')}</td></tr>`).join('')
    : emptyRow(4, 'i-list', 'No decisions yet', ''));
}

$('#offloadBtn').onclick = async () => {
  const btn = $('#offloadBtn');
  btn.disabled = true; btn.classList.add('busy');
  try {
    const d = await post('/api/models/evict-all');
    const freed = Math.round(d.residency?.free_vram_mb ?? 0);
    toast({ kind: 'ok', title: d.evicted?.length ? `Unloaded ${d.evicted.join(', ')}` : 'Nothing was loaded',
      text: `${freed.toLocaleString()} MB of GPU memory is free. The next chat is routed on merit alone.` });
    viewRuntime();
  } catch (e) { toastError(e, 'unload the models'); }
  btn.disabled = false; btn.classList.remove('busy');
};

/* A performance figure is shown with its basis, or not at all. A prior is a
   range, because a point value would claim a precision nobody measured. */
function withBasis(m, fmt) {
  if (!m) return '—';
  const n = m.samples ? ` · ${m.samples}` : '';
  if (m.basis === 'prior' && m.range) {
    return `<span title="${esc(m.why || 'size-derived prior')}">${fmt(m.range[0])}–${fmt(m.range[1])}<span class="sub">estimate</span></span>`;
  }
  return `${fmt(m.value)}<span class="sub">${esc(m.basis)}${n}</span>`;
}

async function viewModels() {
  let m;
  try { m = await api('/api/models'); }
  catch (e) { failView('#modelRows', e, viewModels, 8); return; }
  const health = Object.fromEntries(m.health.map(h => [h.model, h]));
  const resident = new Set(m.residency.resident.map(x => x.model));
  if (paint('#modelRows', m.models.map(c => {
    const h = health[c.name]?.status;
    return `<tr>
    <td><b>${esc(c.name)}</b>${resident.has(c.name) ? ' <span class="pill run"><i class="px"></i>Loaded</span>' : ''}
      ${c.notes ? `<span class="sub clamp" title="${esc(c.notes)}">${esc(c.notes)}</span>` : ''}
      <span class="sub mono">${esc(c.backend)} · adapter ${esc(c.prompt_adapter)}</span></td>
    <td>${esc(human(c.role))}</td>
    <td class="num">${withBasis(c.basis?.residency_mb, v => Math.round(v).toLocaleString() + ' MB')}</td>
    <td class="num">${c.kv_mb_per_1k ? c.kv_mb_per_1k + ' MB' : '—'}</td>
    <td class="num">${withBasis(c.basis?.cold_load_s, v => v.toFixed(1) + ' s')}</td>
    <td class="num">${withBasis(c.basis?.decode_tps, v => v.toFixed(1) + ' tok/s')}</td>
    <td>${c.enabled ? `<span class="pill ${h === 'healthy' ? 'ok' : 'wait'}"><i class="px"></i>${esc(h === 'healthy' ? 'Healthy' : human(h || 'unknown'))}</span>`
      : '<span class="pill bad"><i class="px"></i>Not served</span>'}</td>
    <td class="nowrap"><button class="btn sm ghost" data-m="${esc(c.name)}" data-a="pin" title="Keep this model loaded">Keep loaded</button>
      <button class="btn sm ghost" data-m="${esc(c.name)}" data-a="evict" title="Unload this model from GPU memory">Unload</button></td></tr>`;
  }).join('') || emptyRow(8, 'i-chip', 'No models registered', ''))) {
    $$('#modelRows [data-m]').forEach(b => b.onclick = async () => {
      b.disabled = true; b.classList.add('busy');
      try { await post(`/api/models/${enc(b.dataset.m)}/residency`, { action: b.dataset.a }); }
      catch (e) { toastError(e, b.dataset.a === 'pin' ? 'keep that model loaded' : 'unload that model'); }
      finally { b.disabled = false; b.classList.remove('busy'); $('#modelRows').__html = null; viewModels(); }
    });
  }

  const axes = ['text', 'vision', 'reasoning', 'coding', 'extraction', 'long_context', 'tool_use', 'speed', 'structured'];
  paint('#capHead', '<th>Model</th>' + axes.map(a => `<th class="r">${esc(human(a))}</th>`).join(''));
  paint('#capRows', m.models.map(c => `<tr><td class="mono"><b>${esc(c.name)}</b></td>`
    + axes.map(a => { const v = c.caps?.[a] ?? 0; return `<td class="num" style="background:color-mix(in srgb, var(--primary) ${Math.round(v * 22)}%, transparent)">${v.toFixed(2)}</td>`; }).join('')
    + '</tr>').join(''));

  const hw = state.system?.hardware || {};
  paint('#hwProfile', [
    ['GPU', `${hw.gpu?.name || '—'} · ${Math.round(hw.gpu?.total_mb || 0).toLocaleString()} MB`],
    ['Usable GPU memory', `${Math.round(hw.usable_vram_mb || 0).toLocaleString()} MB`],
    ['CPU', `${hw.cpu?.model || '—'} (${hw.cpu?.logical || 0} threads)`],
    ['System memory', `${((hw.memory?.total_mb || 0) / 1024).toFixed(1)} GB`],
    ['PCIe', `gen ${hw.gpu?.pcie_gen_max || '?'} x${hw.gpu?.pcie_width_max || '?'} · about ${hw.pcie_est_gbs || '?'} GB/s`],
    ['Data disk', `${hw.data_fs || '—'} · ${hw.disk_free_gb || 0} GB free`],
    ['Model files', `${hw.model_dir || '—'} (${hw.model_fs || '?'})${hw.model_fs_warning ? ' — ' + hw.model_fs_warning : ''}`],
    ['Sandbox', (hw.sandbox_engines || []).join(', ') || 'none'],
    ['Text recognition', `${hw.ocr?.version || 'none'} · ${(hw.ocr?.langs || []).join(', ')}`],
    ['AI engines', Object.entries(hw.backends || {}).map(([k, v]) => `${k}: ${v.status}`).join(' · ')],
  ].map(([k, v]) => `<dt>${esc(k)}</dt><dd class="mono">${esc(v)}</dd>`).join(''));
}

async function viewSecurity() {
  let n;
  try { n = await api('/api/network'); }
  catch (e) { failView('#netRows', e, viewSecurity, 8); return; }
  viewDevices();
  const v = state.sov || {};
  const st = v.state || 'green';
  const hero = {
    green: ['Sealed — nothing leaves this machine', 'Every protection layer is in force.'],
    amber: ['Sealed, with one layer unverified', 'The host firewall needs admin rights to read. Check it with: sudo ops/egress-policy.sh status.'],
    red: ['Not sealed', 'At least one protection layer is off. Treat confidential work with care until it is fixed.'],
  }[st] || ['Checking…', ''];
  if (paint('#secHero', `<div class="sec-hero ${esc(st)}"><span class="big">${icon('i-shield')}</span>
      <div><b>${esc(hero[0])}</b><p>${esc(hero[1])} ${v.denials != null ? `${Number(v.denials).toLocaleString()} outside connection attempts have been blocked.` : ''}</p></div>
      <button class="btn secondary" id="selftestBtn" type="button">${icon('i-shield')}Run self-test</button></div>`)) {
    $('#selftestBtn').onclick = () => runSelftest($('#selftestBtn'));
  }
  const nft = n.nftables || {};
  paint('#sovLayers', [
    ['Host firewall', !!nft.loaded, nft.loaded ? 'The operating system blocks all outside connections (nftables default-deny).'
      : nft.available ? 'Could not confirm the firewall table is loaded. An admin can apply it with ops/egress-policy.sh.' : 'nftables is not available on this host.', 'i-shield'],
    ['App seal', n.app_guard_installed, n.app_guard_installed ? 'Every outbound connection and name lookup is checked inside ClawCal and traced to a chat.' : 'Not installed.', 'i-lock'],
    ['Tool rules', true, 'The AI is given no tool that can reach a network.', 'i-sliders'],
    ['Sandbox', true, 'Code runs in a sandbox with no network at all.', 'i-chip'],
    ['Activity log', true, 'Every blocked attempt is written to a tamper-evident log.', 'i-ledger'],
    ['Allowed destinations', null, n.allowed_hosts.join(', ') + ' on ports ' + n.allowed_ports.join(', ') + ' — the local AI engines only.', 'i-info'],
    ['Attempts so far', null, n.counts.map(c => `${c.layer}: ${c.n} ${c.result.toLowerCase()}`).join(' · ') || 'None yet.', 'i-list'],
  ].map(([k, on, text, ico]) => `<div class="layer ${on === null ? 'plain' : on ? '' : 'off'}"><span class="li">${icon(ico)}</span>
    <div><b>${esc(k)}</b><p>${esc(text)}</p></div>
    ${on === null ? '' : on ? '<span class="pill ok"><i class="px"></i>On</span>' : '<span class="pill wait"><i class="px"></i>Unverified</span>'}</div>`).join(''));

  paint('#netRows', n.recent.length ? n.recent.map(e => `<tr>
    <td><span class="pill ${e.result === 'ALLOWED' ? 'wait' : 'ok'}"><i class="px"></i>${esc(e.result === 'ALLOWED' ? 'Allowed' : 'Blocked')}</span></td>
    <td class="mono">${esc(e.destination)}</td><td class="num">${esc(e.port ?? '')}</td>
    <td class="nowrap">${esc(e.layer)}</td><td class="mono nowrap">${esc(e.process || '')}</td>
    <td class="mono nowrap">${esc(e.task_id || '')}</td>
    <td>${esc((e.detail || '').slice(0, 140))}</td><td class="num">${ago(e.ts)}</td></tr>`).join('')
    : emptyRow(8, 'i-shield', 'No attempts yet', 'Run the self-test to produce a blocked-attempt record.'));
}

/* ------------------------------------------------------- member devices */
const GRADE_TEXT = {
  A: 'Managed and hardware-attested. May see all documents and work away from the node.',
  B: 'Managed, attested by the organisation’s device management. May see confidential documents.',
  C: 'Not managed by the organisation. Its network lock is self-reported, so it sees internal documents only.',
  D: 'Its network lock is off or unreported. It sees public documents only.',
};
const LEASE_TEXT = { ACTIVE: 'Active', IN_GRACE: 'Expired — grace period', EXPIRED: 'Expired',
  REVOKED: 'Revoked', SUPERSEDED: 'Replaced' };

const ATTEST_TEXT = { 'tpm-quote': 'TPM verified', mdm: 'MDM attested', 'self-report': 'Self-reported' };

async function viewDevices() {
  let d;
  try { d = await api('/api/devices'); }
  catch (e) { failView('#devRows', e, viewDevices, 7); return; }
  api('/api/trust/health').then(h => {
    const items = h.items || [];
    const cap = t => t.charAt(0).toUpperCase() + t.slice(1);
    const fix = items.filter(i => i.ok === false);
    const ready = items.filter(i => i.ok !== false).map(i => cap(i.check));
    paint('#trustHealth', fix.map(i =>
      `<div class="layer off"><span class="li">${icon('i-info')}</span>
        <div><b>${esc(cap(i.check))}</b><p>${esc(i.detail)}</p></div>
        <span class="pill wait"><i class="px"></i>Needs attention</span></div>`).join('')
      + (ready.length ? `<p class="health-ok">${icon('i-shield')}Ready: ${esc(ready.join(' · '))}</p>` : ''));
  }).catch(() => { });
  const rows = d.devices || [];
  const g = d.by_grade || {};
  const tamper = d.open_tamper_events || 0;
  paint('#devSummary', rows.length
    ? `<span>${rows.length} enrolled</span>${['A', 'B', 'C', 'D'].filter(k => g[k]).map(k =>
        `<span class="grade g${k}" title="${esc(GRADE_TEXT[k])}">${k}<b>${g[k]}</b></span>`).join('')}
       ${tamper ? `<span class="pill bad"><i class="px"></i>${tamper} tamper event${tamper > 1 ? 's need' : ' needs'} review</span>` : ''}`
    : '');
  paint('#devRows', rows.length ? rows.map(r => {
    const lease = r.lease || {};
    const ls = lease.status || '—';
    const open = r.tamper_events || [];
    const status = r.state === 'REVOKED' ? '<span class="pill bad"><i class="px"></i>Revoked</span>'
      : r.state === 'QUARANTINED' ? '<span class="pill bad"><i class="px"></i>Quarantined</span>'
      : r.state === 'PENDING' ? '<span class="pill wait"><i class="px"></i>Awaiting approval</span>'
      : '<span class="pill ok"><i class="px"></i>Attached</span>';
    const acts = !isAdmin() || r.state === 'REVOKED' ? '' :
      (open.length ? `<button class="btn ghost sm" data-clear="${esc(open[0].id)}">Review</button>` : '')
      + `<button class="btn ghost sm" data-revoke="${esc(r.id)}">Revoke</button>`;
    return `<tr data-dev="${esc(r.id)}">
      <td><b>${esc(r.name || r.id)}</b><div class="mono sub">${esc(r.id)} · ${esc(r.platform)}</div></td>
      <td>${esc(r.principal)}</td>
      <td><span class="grade g${esc(r.grade || 'D')}" title="${esc(r.grade_reason || '')}">${esc(r.grade || '?')}</span>
        <span class="sub-inline">${esc(r.mode === 'detached' ? 'Working away from the node' : 'Attached')} ·
          ${esc((r.attestation && r.attestation.verified ? ATTEST_TEXT[r.attestation.kind] : null) || 'Self-reported')}</span>
        <div class="sub">${esc(GRADE_TEXT[r.grade] || '')}</div></td>
      <td>${esc(LEASE_TEXT[ls] || ls)}${lease.expires_at && ls === 'ACTIVE' ? `<div class="sub">until ${new Date(lease.expires_at * 1000).toLocaleString()}</div>` : ''}</td>
      <td class="nowrap">${r.anchored_at ? ago(r.anchored_at) : 'never'}<div class="sub">entry ${esc(r.chain_seq || 0)}</div></td>
      <td>${status}${open.length ? `<div class="sub bad-t">${esc(open[0].kind.replace(/_/g, ' '))}: ${esc((open[0].detail || '').slice(0, 120))}</div>` : ''}</td>
      <td class="r nowrap">${acts}</td></tr>
      <tr class="dev-form" hidden data-form="${esc(r.id)}"><td colspan="7"></td></tr>`;
  }).join('') : emptyRow(7, 'i-chip', 'No devices yet',
    'A laptop joins with “clawcal device enrol”. Until then, only this machine uses the node.'));

  const form = (id, label, go) => {
    const tr = $(`tr[data-form="${CSS.escape(id)}"]`);
    tr.hidden = false;
    tr.firstElementChild.innerHTML = `<form class="dev-act"><label>${esc(label)}
      <input required minlength="4" placeholder="Reason, kept on the record"></label>
      <button class="btn sm" type="submit">Confirm</button>
      <button class="btn ghost sm" type="button">Cancel</button></form>`;
    const f = $('form', tr);
    $('input', f).focus();
    $('button[type=button]', f).onclick = () => { tr.hidden = true; };
    f.onsubmit = async ev => {
      ev.preventDefault();
      try { await go($('input', f).value.trim()); viewDevices(); }
      catch (e) { toastError(e, label.toLowerCase()); }
    };
  };
  $$('#devRows [data-revoke]').forEach(b => b.onclick = () => form(b.dataset.revoke,
    'Revoke this device — its lease stops now and it cannot renew',
    reason => post(`/api/devices/${enc(b.dataset.revoke)}/revoke`, { reason })));
  $$('#devRows [data-clear]').forEach(b => {
    const id = b.closest('tr').dataset.dev;
    b.onclick = () => form(id, 'Clear the tamper event and let the device continue its log from here',
      reason => post(`/api/tamper/${enc(b.dataset.clear)}/clear`, { reason, rebase: true }));
  });
}

async function viewEvidence() {
  try {
    const tasks = (await api('/api/tasks?limit=40')).tasks;
    /* Scan a deeper slice than the visible recent list: claims only come from
       runs that extracted values or computed something. */
    const head = tasks.slice(0, 20);
    if (!head.length) {
      paint('#claimRows', emptyRow(5, 'i-docs', 'No claims yet', ''));
      paint('#calcRows', emptyRow(6, 'i-calc', 'No calculations yet', ''));
      return;
    }
    const details = await Promise.all(head.map(t =>
      api('/api/tasks/' + enc(t.id)).then(d => ({ d, t })).catch(() => null)));
    const claims = [], calcs = [];
    for (const r of details) {
      if (!r) continue;
      r.d.claims.forEach(c => claims.push({ ...c, task: r.t.title }));
      r.d.calculations.forEach(c => calcs.push({ ...c, task: r.t.title }));
    }
    paint('#claimRows', claims.length ? claims.slice(0, 250).map(c => `<tr>
      <td>${evTag(c.ev_class, 'tabindex="-1"')}</td>
      <td class="mono nowrap"><b>${esc(withUnit(c.value, c.unit))}</b></td>
      <td>${esc((c.statement || '').slice(0, 200))}</td>
      <td>${esc((c.rationale || '').slice(0, 160))}</td>
      <td>${esc(c.task)}</td></tr>`).join('')
      : emptyRow(5, 'i-docs', `No claims in the last ${head.length} runs`, 'Claims are recorded when ClawCal pulls a value from a document or works one out, not for ordinary questions.'));
    paint('#calcRows', calcs.length ? calcs.map(c => {
      let steps = {}; try { steps = JSON.parse(c.steps || '{}'); } catch (e) { }
      const ver = steps.inputs_verified !== false;
      return `<tr><td class="mono">${esc(c.id)}</td><td>${esc(c.label || '')}</td>
        <td class="mono">${esc(c.expression)}</td><td class="mono">${esc(steps.substituted || '')}</td>
        <td class="mono nowrap"><b>${esc(c.result ?? '—')}</b> ${esc(c.unit || '')}</td>
        <td>${ver ? '<span class="pill ok"><i class="px"></i>Verified</span>'
          : `<span class="pill bad"><i class="px"></i>Unverified</span><span class="sub">${esc((steps.unverified_inputs || []).join(', '))}</span>`}</td></tr>`;
    }).join('') : emptyRow(6, 'i-calc', 'No calculations yet', ''));
  } catch (e) { failView('#claimRows', e, viewEvidence, 5); }
}

async function viewTasks() {
  try {
    const { tasks } = await api('/api/tasks?limit=200');
    if (paint('#taskRows', tasks.length ? tasks.map(t => `<tr class="clickable" data-s="${esc(t.conversation_id || '')}" data-id="${esc(t.id)}">
      <td><b>${esc(t.title)}</b><span class="sub mono">${esc(t.id)}</span></td>
      <td>${esc(human(t.task_type || '—'))}</td><td>${esc(prio(t.priority))}</td>
      <td>${statePill(t.state, t.state_reason)}</td>
      <td class="mono">${esc(t.selected_model || '—')}</td>
      <td class="num">${t.steps_used || 0}</td><td class="num">${fmtSec(t.queue_wait_s)}</td><td class="num">${fmtSec(t.runtime_s)}</td></tr>`).join('')
      : emptyRow(8, 'i-list', 'No runs yet', ''))) {
      $$('#taskRows tr[data-id]').forEach(tr => tr.onclick = () => openChat(tr.dataset.s || null, tr.dataset.id));
    }
  } catch (e) { failView('#taskRows', e, viewTasks, 8); }
}

async function viewActivity() {
  let a;
  try { a = await api('/api/audit?limit=400'); }
  catch (e) { failView('#auditRows', e, viewActivity, 7); return; }
  const v = a.verification;
  paint('#auditVerify', v.ok
    ? `<div class="verify"><span class="pill ok"><i class="px"></i>Intact</span>
        <span>${Number(v.entries).toLocaleString()} entries. Each one commits to the one before it, so any edit or deletion breaks the chain. Head <span class="mono">${esc(v.head.slice(0, 20))}…</span></span></div>`
    : `<div class="verify"><span class="pill bad"><i class="px"></i>Broken</span><span>At entry ${esc(v.broken_at)}: ${esc(v.reason)}</span></div>`);
  paint('#auditRows', a.entries.length ? a.entries.map(e => `<tr>
    <td class="num mono">${e.seq}</td><td class="nowrap">${ago(e.ts)}</td>
    <td>${esc(human(e.category))}</td><td class="mono">${esc(e.action)}</td>
    <td><span class="pill ${['BLOCKED', 'OK'].includes(e.outcome || 'OK') ? 'ok' : ['DENIED', 'FAILED', 'LEAK'].includes(e.outcome) ? 'bad' : 'wait'}"><i class="px"></i>${esc(human(e.outcome || 'OK'))}</span></td>
    <td class="mono">${esc(e.task_id || '')}</td>
    <td>${esc((e.detail || '').slice(0, 190))}</td></tr>`).join('')
    : emptyRow(7, 'i-ledger', 'The log is empty', ''));
}

/* ================================================================= views */
const ADMIN = { system: () => viewRuntime(), models: viewModels, security: viewSecurity,
  evidence: viewEvidence, tasks: viewTasks, activity: viewActivity };
const VIEWS = {
  chat: () => state.session ? loadChat() : renderHello(),
  library: viewLibrary, outputs: viewOutputs, approvals: viewApprovals,
  admin: () => { (ADMIN[state.adminTab] || ADMIN.system)(); if (state.adminTab === 'system') pollTelemetry(); },
};
function refreshView() { (VIEWS[state.view] || (() => { }))(); }

/* ================================================================== live */

/* Stream events arrive far faster than a view needs redrawing. Collapse a burst
   into one refresh; the trailing edge is what matters, so the final state is
   never the one that got dropped. */
const bounce = (fn, ms = 450) => { let t = null; return () => { clearTimeout(t); t = setTimeout(fn, ms); }; };
const bChat = bounce(() => loadChat());
const bChats = bounce(() => loadChats(), 700);
const bApprovals = bounce(() => { refreshApprovals(); if (state.view === 'approvals') viewApprovals(); if (state.view === 'chat') loadChat(); });
const bSecurity = bounce(() => viewSecurity());
const bRuntime = bounce(() => viewRuntime());
const bSeal = bounce(() => paintSeal(), 250);

let es = null, sseTimer = null, sseWait = 1000, downSince = 0, bannerTimer = null;
function setLinkUp(up) {
  clearTimeout(bannerTimer);
  if (up) {
    if (!$('#netBanner').hidden) toast({ kind: 'ok', title: 'Reconnected', ttl: 2500 });
    $('#netBanner').hidden = true; downSince = 0;
  } else if (!downSince) {
    downSince = Date.now();
    /* A blip is not news. Only say so if the link stays down. */
    bannerTimer = setTimeout(() => { if (downSince) $('#netBanner').hidden = false; }, 4000);
  }
}

function connect() {
  clearTimeout(sseTimer);
  if (es) { try { es.close(); } catch (_) { } }
  es = new EventSource('/api/stream' + (AUTH.token ? '?token=' + enc(AUTH.token) : ''));
  es.onopen = () => {
    sseWait = 1000; setLinkUp(true);
    /* Anything raised while the stream was down is gone for good — it is not
       replayed on reconnect. Re-read what is on screen. */
    loadSystem().catch(() => { });
    refreshView();
  };
  es.onerror = () => {
    setLinkUp(false);
    /* A failure at the HTTP level — what a control-plane restart looks like —
       closes EventSource permanently. Rebuild it, backing off. */
    if (es.readyState === EventSource.CLOSED) {
      sseTimer = setTimeout(connect, sseWait);
      sseWait = Math.min(sseWait * 2, 15000);
    }
  };
  es.onmessage = ev => {
    let m; try { m = JSON.parse(ev.data); } catch (e) { return; }
    const mine = m.task_id && state.cache[m.task_id];
    if (m.type === 'task_event' && mine && state.view === 'chat') bChat();
    if (m.type === 'task_state') { bChats(); if ((mine || m.task_id === state.followTask) && state.view === 'chat') bChat(); }
    /* Always re-read on an approval event: a stale badge means an operator never
       learns a decision is waiting on them. */
    if (m.type === 'approval') bApprovals();
    if (m.type === 'network_event') { bSeal(); if (state.view === 'admin' && state.adminTab === 'security') bSecurity(); }
    if (m.type === 'admission' && state.view === 'admin' && state.adminTab === 'system') bRuntime();
    /* A device's log failed its check: say so wherever the operator is. */
    if (m.type === 'tamper') {
      bSeal();
      if (state.view === 'admin' && state.adminTab === 'security') bSecurity();
      if (m.kind !== 'silent') toast({ kind: 'error', title: 'A device was quarantined',
        text: `${m.device_id}: its activity log failed a check (${String(m.kind).replace(/_/g, ' ')}). Review it under Admin → Security.` });
    }
  };
}

async function pollTelemetry() {
  try { const t = await api('/api/telemetry'); paintGauges(t.latest); paintEngine(t.latest); } catch (_) { }
}

let tickN = 0;
async function tick() {
  if (document.hidden) return;             /* nobody is looking: spend nothing */
  tickN++;
  try {
    const r = await api('/api/runtime');
    state.runtime = r;
    setLinkUp(true);
    paintEngine();
    if (state.view === 'admin' && state.adminTab === 'system') { viewRuntime(r); pollTelemetry(); }
    else if (tickN % 3 === 0) pollTelemetry();
    if (state.view === 'approvals' && tickN % 2 === 0) viewApprovals();
    /* The stream is an optimisation, not the only path to the truth: if it
       died, a run that finished would read "Working" forever. Poll the open
       chat while any of its runs can still change. */
    if (state.view === 'chat' && state.session) {
      const live = Object.values(state.cache).some(d => !TERMINAL.has(d.task.state));
      if (live || state.followTask && !state.cache[state.followTask]) loadChat();
    }
    if (tickN % 3 === 0) loadChats();
  } catch (e) { if (e.network || e.status >= 500) setLinkUp(false); }
}

(async function boot() {
  autoGrow();
  paint('#thread', skeleton());
  /* ?chat=<id> opens a chat; ?task=<id> (older links) opens the chat it is in;
     ?view=library|outputs|approvals|admin:<tab> opens a section. */
  const q = new URLSearchParams(location.search);
  try { await loadSystem(); }
  catch (e) {
    if (e.status === 401) signIn();
    else toastError(e, 'reach the workbench');
  }
  const qv = q.get('view');
  if (q.get('chat')) openChat(q.get('chat'));
  else if (q.get('task')) openChat(null, q.get('task'));
  else if (qv) { const [v, t] = qv.split(':'); go(v in META ? v : 'chat', { tab: t }); }
  else renderHello();
  connect();
  loadChats();
  loadRates();
  refreshApprovals();
  api('/api/drawings').then(r => setCount('#cDwg', r.drawings.length)).catch(() => { });
  paintSeal();
  tick();
  setInterval(tick, 3000);
  setInterval(() => { if (!document.hidden) paintSeal(); }, 15000);
  setInterval(() => { if (!document.hidden) loadSystem().catch(() => { }); }, 30000);
})();
