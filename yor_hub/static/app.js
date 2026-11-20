"use strict";
const $ = (s, el = document) => el.querySelector(s);
const h = (tag, attrs = {}, ...kids) => {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") el.className = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (v !== undefined && v !== null && v !== false) el.setAttribute(k, v === true ? "" : v);
  }
  for (const k of kids.flat()) if (k != null) el.append(k.nodeType ? k : document.createTextNode(k));
  return el;
};

const GROUP_TITLES = { system: "System", robot: "Robot", teleop: "Teleop", collection: "Data collection",
                       deploy: "Policy deploy" };
const GROUP_ORDER = ["system", "robot", "collection", "teleop", "deploy"];
const STATE_TXT = { stopped: "stopped", running: "running", ready: "ready", exited: "done",
                    failed: "failed", stopping: "stopping…" };
let last = null;            // /api/state/all
let multi = true;           // false when talking to an old single-host hub
const hostEls = {};         // host -> {sec, refs}
const cards = {};           // "host/id" -> {el, refs, host, id}

function toast(msg, ok) {
  const t = $("#toast"); t.textContent = msg; t.classList.remove("hidden"); t.classList.toggle("ok", !!ok);
  clearTimeout(toast._t); toast._t = setTimeout(() => t.classList.add("hidden"), ok ? 2500 : 6000);
}

async function api(path, body, method, headers) {
  const opt = { method: method || (body === undefined ? "GET" : "POST"), headers: { ...(headers || {}) } };
  if (body !== undefined) { opt.headers["content-type"] = "application/json"; opt.body = JSON.stringify(body); }
  const r = await fetch(path, opt);
  if (r.status === 401) { location.reload(); throw new Error("unauthorized"); }
  const j = await r.json().catch(() => ({}));
  if (!r.ok) { const e = new Error(j.error || r.statusText); e.status = r.status; e.data = j; throw e; }
  return j;
}
const isSelf = host => !last || last.hosts[host]?.self;
const apiBase = host => (isSelf(host) ? "/api" : `/api/hosts/${encodeURIComponent(host)}`);
const shortLabel = host => (last?.hosts[host]?.label || host).split(/\s+[·(]/)[0];

function ago(t) {
  if (!t) return "";
  const s = Math.max(0, Date.now() / 1000 - t);
  if (s < 60) return `${s | 0}s`;
  if (s < 3600) return `${(s / 60) | 0}m`;
  return `${(s / 3600).toFixed(1)}h`;
}

// ---------- host sections ----------
function hostSection(host) {
  if (hostEls[host]) return hostEls[host];
  const refs = {};
  const sec = h("section", { class: "host", "data-host": host },
    h("div", { class: "hosthead" },
      refs.dot = h("span", { class: "dot" }),
      refs.title = h("span", { class: "hosttitle" }),
      refs.stats = h("span", { class: "stats" }),
      h("span", { class: "spacer" }),
      refs.cycles = h("button", { class: "small hidden", onclick: () => openCycles(host) }, "Cycles"),
      refs.shell = h("button", { class: "small", onclick: () => openTerm(host, "shell", "shell") }, "Shell"),
      h("button", { class: "small", onclick: () => newAgent(host) }, "+ Claude agent")),
    refs.offline = h("div", { class: "offline hidden" }),
    refs.checks = h("div", { class: "chips" }),
    refs.groups = h("div", {}),
    refs.agentsWrap = h("div", { class: "hidden" }, h("h3", {}, "Agents & other windows"),
      refs.agents = h("div", { class: "cards" }), refs.extra = h("div", { class: "chips" })));
  $("#hosts").append(sec);
  return (hostEls[host] = { sec, refs, groups: {} });
}

function renderStats(el, st) {
  const x = st.extra || {};
  const parts = [];
  if (st.cpu_temp_c != null) parts.push(["CPU", `${st.cpu_temp_c}°C`]);
  if (st.load) parts.push(["load", st.load[0].toFixed(2)]);
  if (st.mem_used_pct != null) parts.push(["mem", `${st.mem_used_pct}%`]);
  if (x.gpu_util_pct != null) parts.push(["GPU", `${Math.round(x.gpu_util_pct)}%`]);
  if (x.power_total_w != null) parts.push(["power", `${x.power_total_w} W`]);
  if (x.power_mode) parts.push(["mode", x.power_mode]);
  if (x.temps_c?.tj != null) parts.push(["Tj", `${x.temps_c.tj}°C`]);
  if (x.fan_rpm != null) parts.push(["fan", `${x.fan_rpm}`]);
  parts.push(["disk", st.disk_free_gb != null ? `${st.disk_free_gb} GB` : "–"]);
  if (x.throttled !== undefined)
    parts.push(["supply", x.undervoltage_now ? "UNDERVOLT" : x.throttled_now ? "THROTTLED" : "ok"]);
  el.replaceChildren(...parts.map(([k, v]) => h("span", {}, `${k} `, h("b", {}, v))));
}

function renderHost(host, st) {
  const H = hostSection(host), r = H.refs;
  r.title.textContent = st.label || host;
  r.dot.className = `dot ${st.online ? "ok" : "error"}`;
  r.offline.classList.toggle("hidden", !!st.online);
  r.offline.textContent = st.online ? "" : `${st.label || host} hub is offline: ${st.detail || "unreachable"}. Its processes keep running; this page reconnects on its own.`;
  r.shell.disabled = !st.online;
  if (!st.online) {
    r.stats.replaceChildren(); r.checks.replaceChildren();
    for (const c of Object.values(cards)) if (c.host === host) c.el.classList.add("stale");
    return;
  }
  renderStats(r.stats, st.stats || {});
  r.checks.replaceChildren(...Object.entries(st.checks || {}).map(([name, c]) =>
    h("span", { class: "chip", title: name },
      h("span", { class: `dot ${c.level}` }), c.label || name, h("span", { class: "detail" }, c.detail))));
  const hasCycles = (st.features || []).includes("cycles") ||
    (st.services || []).some(s => (s.params || []).some(p => p.options_from === "cycles"));
  r.cycles.classList.toggle("hidden", !hasCycles);

  // groups in a stable order
  const services = st.services || [];
  const groups = [...new Set(services.filter(s => s.kind !== "agent").map(s => s.group))]
    .sort((a, b) => (GROUP_ORDER.indexOf(a) + 1 || 99) - (GROUP_ORDER.indexOf(b) + 1 || 99));
  for (const g of groups) {
    if (!H.groups[g]) {
      const wrap = h("div", {}, h("h3", {}, GROUP_TITLES[g] || g), h("div", { class: "cards" }));
      H.groups[g] = $(".cards", wrap);
      H.groups[g]._wrap = wrap;
    }
  }
  r.groups.replaceChildren(...groups.map(g => H.groups[g]._wrap));
  const seen = new Set();
  for (const s of services) {
    const key = `${host}/${s.id}`;
    seen.add(key);
    if (!cards[key]) cards[key] = makeCard(host, s);
    const parent = s.kind === "agent" ? r.agents : H.groups[s.group];
    if (cards[key].el.parentNode !== parent) parent.append(cards[key].el);
    cards[key].el.classList.remove("stale");
    updateCard(cards[key], s);
  }
  for (const [k, c] of Object.entries(cards)) if (c.host === host && !seen.has(k)) { c.el.remove(); delete cards[k]; }
  const extra = (st.extra_windows || []).filter(w => w !== "shell");
  r.extra.replaceChildren(...extra.map(w =>
    h("span", { class: "chip clickable", onclick: () => openTerm(host, w, w) }, "▸ ", w)));
  r.agentsWrap.classList.toggle("hidden", !services.some(s => s.kind === "agent") && !extra.length);
}

// ---------- service cards ----------
function makeCard(host, s) {
  const refs = {};
  const el = h("div", { class: "card" },
    h("div", { class: "top" },
      refs.dot = h("span", { class: "dot" }),
      refs.title = h("span", { class: "title" }),
      refs.state = h("span", { class: "state" })),
    refs.desc = h("div", { class: "desc" }),
    refs.params = h("div", { class: "params" }),
    refs.args = h("input", { type: "text", placeholder: "args", spellcheck: "false" }),
    refs.blocked = h("div", { class: "blocked" }),
    h("div", { class: "row" },
      refs.start = h("button", { class: "primary", onclick: () => act(host, s.id, "start") }, "Start"),
      refs.stop = h("button", { class: "danger", onclick: () => act(host, s.id, "stop") }, "Stop"),
      refs.restart = h("button", { onclick: () => act(host, s.id, "restart") }, "Restart"),
      refs.term = h("button", { onclick: () => openTerm(host, s.id, s.label) }, "Terminal"),
      refs.clear = h("button", { onclick: () => act(host, s.id, "clear") }, "Clear")));
  refs.args.addEventListener("keydown", e => { if (e.key === "Enter") act(host, s.id, "start"); });
  refs.args.addEventListener("input", () => (refs.args.dataset.dirty = "1"));
  if (s.kind === "agent") refs.args.style.display = "none";
  refs.paramEls = {};
  return { el, refs, host, id: s.id };
}

function updateParams(c, s, alive) {
  const r = c.refs;
  const params = s.params || [];
  r.params.style.display = params.length ? "" : "none";
  for (const p of params) {
    let pe = r.paramEls[p.name];
    if (!pe) {
      const sel = h("select", { onchange: () => (sel.dataset.dirty = "1") });
      const row = h("label", { class: "param" }, h("span", {}, p.label), sel);
      if (p.options_from === "cycles")
        row.append(h("button", { class: "small", title: "Add / edit cycles",
          onclick: e => { e.preventDefault(); openCycles(c.host, sel.value || null); } }, "Edit"));
      r.params.append(row);
      pe = r.paramEls[p.name] = { sel, sig: "" };
    }
    const sig = JSON.stringify(p.options);
    const want = pe.sel.dataset.dirty === "1" ? pe.sel.value : (p.value ?? "");
    if (pe.sig !== sig) {
      pe.sel.replaceChildren(h("option", { value: "" }, p.none_label || "(none)"),
        ...p.options.map(o => h("option", { value: o }, o)));
      pe.sig = sig;
    }
    pe.sel.value = p.options.includes(want) ? want : "";
    pe.sel.disabled = alive;
  }
}

function updateCard(c, s) {
  const r = c.refs;
  const alive = ["running", "ready", "stopping"].includes(s.state);
  r.dot.className = `dot ${s.state}`;
  r.title.textContent = s.label;
  r.state.textContent = STATE_TXT[s.state] + (s.state === "failed" || s.state === "exited" ? ` (${s.exit_code})` : "")
    + (alive && s.started_at ? ` · ${ago(s.started_at)}` : "");
  r.desc.textContent = s.description || "";
  r.desc.style.display = s.description ? "" : "none";
  if (document.activeElement !== r.args && r.args.dataset.dirty !== "1") r.args.value = s.args || "";
  r.args.disabled = alive;
  updateParams(c, s, alive);
  const blocked = s.blocked_by || [];
  r.blocked.textContent = !alive && blocked.length ? `waiting on: ${blocked.join(", ")}` : "";
  r.blocked.style.display = r.blocked.textContent ? "" : "none";
  r.start.disabled = alive;
  r.start.textContent = !alive && blocked.length ? "Start anyway…" : "Start";
  r.stop.disabled = !alive || s.state === "stopping";
  r.restart.disabled = !alive;
  r.restart.style.display = s.kind === "agent" ? "none" : "";
  r.term.disabled = !s.window;
  r.clear.style.display = s.window && !alive ? "" : "none";
  c.svc = s;
}

async function act(host, id, action) {
  const c = cards[`${host}/${id}`];
  const svc = c?.svc;
  const body = {};
  if (c && (action === "start" || action === "restart")) {
    if (svc.kind !== "agent") body.args = c.refs.args.value;
    if (svc.params?.length) {
      body.params = {};
      for (const [n, pe] of Object.entries(c.refs.paramEls)) body.params[n] = pe.sel.value || null;
    }
  }
  if (action === "start" && svc?.blocked_by?.length) {
    if (!confirm(`${svc.label} (${shortLabel(host)}) is waiting on: ${svc.blocked_by.join(", ")}.\nStart anyway?`)) return;
    body.force = true;
  }
  if (action === "stop" && host === last?.self && id === "yor" &&
      !confirm("Stop yor.py? Teleop/policy clients will lose the robot.")) return;
  const btn = c?.refs[action];
  if (btn) btn.disabled = true;
  try {
    await api(`${apiBase(host)}/services/${encodeURIComponent(id)}/${action}`, body);
    if (c) { c.refs.args.dataset.dirty = ""; for (const pe of Object.values(c.refs.paramEls)) pe.sel.dataset.dirty = ""; }
  } catch (e) { toast(`${shortLabel(host)} ${id} ${action}: ${e.message}`); }
  poll();
}

async function newAgent(host) {
  try { const s = await api(`${apiBase(host)}/agents`, {}); await poll(); openTerm(host, s.id, s.label); }
  catch (e) { toast(`${shortLabel(host)} agent: ${e.message}`); }
}

// ---------- terminals ----------
const terms = {};  // "host/window" -> {term, fit, ws, el, tab}
let activeTerm = null;

function openTerm(host, win, label) {
  const key = `${host}/${win}`;
  $("#termpane").classList.remove("hidden");
  if (terms[key]) { focusTerm(key); return; }
  const el = h("div", {});
  $("#termbody").append(el);
  const term = new Terminal({ fontSize: 13, scrollback: 5000, cursorBlink: true,
    fontFamily: 'ui-monospace, Menlo, "DejaVu Sans Mono", monospace', theme: { background: "#0d1117" } });
  const fit = new FitAddon.FitAddon();
  term.loadAddon(fit);
  term.open(el);
  const tab = h("span", { class: "tab", onclick: () => focusTerm(key) },
    h("span", { class: "tabhost" }, shortLabel(host)), label || win,
    h("span", { class: "x", onclick: e => { e.stopPropagation(); closeTerm(key); } }, "✕"));
  $("#termtabs").append(tab);
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const path = multi ? `/ws/hosts/${encodeURIComponent(host)}/term/${encodeURIComponent(win)}`
                     : `/ws/term/${encodeURIComponent(win)}`;
  const ws = new WebSocket(`${proto}://${location.host}${path}`);
  ws.binaryType = "arraybuffer";
  const sendSize = () => ws.readyState === 1 && ws.send(JSON.stringify({ t: "r", cols: term.cols, rows: term.rows }));
  ws.onopen = () => { fit.fit(); sendSize(); };
  ws.onmessage = e => term.write(typeof e.data === "string" ? e.data : new Uint8Array(e.data));
  ws.onclose = () => term.write("\r\n\x1b[90m[disconnected]\x1b[0m\r\n");
  term.onData(d => ws.readyState === 1 && ws.send(JSON.stringify({ t: "i", d })));
  term.onResize(sendSize);
  terms[key] = { term, fit, ws, el, tab };
  focusTerm(key);
}

function focusTerm(key) {
  activeTerm = key;
  for (const [k, t] of Object.entries(terms)) {
    t.el.classList.toggle("hidden", k !== key);
    t.tab.classList.toggle("active", k === key);
  }
  requestAnimationFrame(() => { terms[key].fit.fit(); terms[key].term.focus(); });
}

function closeTerm(key) {
  const t = terms[key]; if (!t) return;
  t.ws.close(); t.term.dispose(); t.el.remove(); t.tab.remove(); delete terms[key];
  const rest = Object.keys(terms);
  if (rest.length) focusTerm(rest[rest.length - 1]); else $("#termpane").classList.add("hidden");
}

$("#termclose").onclick = () => Object.keys(terms).forEach(closeTerm);
$("#termmax").onclick = () => {
  $("#termpane").classList.toggle("max");
  if (activeTerm) requestAnimationFrame(() => terms[activeTerm].fit.fit());
};
window.addEventListener("resize", () => activeTerm && terms[activeTerm] && terms[activeTerm].fit.fit());

// ---------- cycle editor ----------
const cyc = { host: null, mtime: null, cycles: {}, sel: null, draft: null, dirty: false, filter: "" };

async function openCycles(host, select) {
  cyc.host = host; cyc.filter = "";
  $("#cyc-host").textContent = shortLabel(host);
  $("#cyc-filter").value = "";
  $("#cyclemodal").classList.remove("hidden");
  await loadCycles(select);
}

function closeCycles() {
  if (cyc.dirty && !confirm("Discard unsaved changes to this cycle?")) return;
  cyc.dirty = false;
  $("#cyclemodal").classList.add("hidden");
  poll();
}

async function loadCycles(select) {
  try {
    const r = await api(`${apiBase(cyc.host)}/cycles`);
    cyc.mtime = r.mtime; cyc.cycles = r.cycles || {};
    $("#cyc-file").textContent = r.file || "";
    const names = Object.keys(cyc.cycles);
    pickCycle(select !== undefined && select !== null && cyc.cycles[select] ? select : (cyc.sel && cyc.cycles[cyc.sel] ? cyc.sel : names[0] ?? null), true);
  } catch (e) { cycMsg(`load failed: ${e.message}`, true); }
}

function cycMsg(text, err) {
  const m = $("#cyc-msg"); m.textContent = text || ""; m.className = `cycmsg ${err ? "err" : "ok"}`;
}

function pickCycle(name, force) {
  if (!force && cyc.dirty && !confirm("Discard unsaved changes to this cycle?")) return;
  cyc.sel = name; cyc.dirty = false;
  const raw = name !== null ? cyc.cycles[name] : [];
  cyc.draft = name === null ? { name: "", subtasks: [{ task: "", raw: null }], isNew: true } :
    { name, subtasks: raw.map(s => ({ task: typeof s === "string" ? s : (s.task || ""), raw: typeof s === "string" ? null : s })), isNew: false };
  cycMsg("");
  renderCycleList(); renderCycleEditor();
}

function renderCycleList() {
  const f = cyc.filter.toLowerCase();
  const names = Object.keys(cyc.cycles).filter(n => !f || n.toLowerCase().includes(f) ||
    cyc.cycles[n].some(s => (typeof s === "string" ? s : s.task || "").toLowerCase().includes(f)));
  $("#cyc-list").replaceChildren(...names.map(n =>
    h("button", { class: `cycitem ${n === cyc.sel && !cyc.draft?.isNew ? "active" : ""}`, onclick: () => pickCycle(n) },
      h("span", {}, n), h("span", { class: "muted" }, `${cyc.cycles[n].length}`))));
}

function markDirty() { cyc.dirty = true; $("#cyc-save").disabled = false; }

function renderCycleEditor() {
  const d = cyc.draft;
  $("#cyc-name").value = d.name;
  $("#cyc-delete").style.display = d.isNew ? "none" : "";
  $("#cyc-save").disabled = !cyc.dirty;
  $("#cyc-title").textContent = d.isNew ? "New cycle" : "Edit cycle";
  const rows = d.subtasks.map((s, i) => {
    const inp = h("input", { type: "text", value: s.task, placeholder: "subtask prompt, e.g. open the fridge",
      oninput: () => { s.task = inp.value; markDirty(); },
      onkeydown: e => { if (e.key === "Enter") { d.subtasks.splice(i + 1, 0, { task: "", raw: null }); markDirty(); renderCycleEditor(); focusSub(i + 1); } } });
    return h("li", { class: "subrow" },
      h("span", { class: "num" }, `${i + 1}`), inp,
      h("button", { class: "small", title: "Move up", disabled: i === 0,
        onclick: () => { [d.subtasks[i - 1], d.subtasks[i]] = [d.subtasks[i], d.subtasks[i - 1]]; markDirty(); renderCycleEditor(); } }, "↑"),
      h("button", { class: "small", title: "Move down", disabled: i === d.subtasks.length - 1,
        onclick: () => { [d.subtasks[i + 1], d.subtasks[i]] = [d.subtasks[i], d.subtasks[i + 1]]; markDirty(); renderCycleEditor(); } }, "↓"),
      h("button", { class: "small danger", title: "Remove", disabled: d.subtasks.length === 1,
        onclick: () => { d.subtasks.splice(i, 1); markDirty(); renderCycleEditor(); } }, "✕"));
  });
  $("#cyc-subs").replaceChildren(...rows);
}
function focusSub(i) { requestAnimationFrame(() => $("#cyc-subs").children[i]?.querySelector("input")?.focus()); }

async function saveCycle() {
  const d = cyc.draft;
  const name = $("#cyc-name").value.trim();
  const subtasks = d.subtasks.filter(s => s.task.trim() || s.raw)
    .map(s => (s.raw ? { ...s.raw, task: s.task.trim() } : s.task.trim()));
  if (!name) return cycMsg("give the cycle a name", true);
  if (!subtasks.length) return cycMsg("a cycle needs at least one subtask", true);
  if (d.isNew && cyc.cycles[name]) return cycMsg(`a cycle named "${name}" already exists`, true);
  const target = d.isNew ? name : d.name;
  const body = { subtasks };
  if (!d.isNew && name !== d.name) body.rename_to = name;
  try {
    const r = await api(`${apiBase(cyc.host)}/cycles/${encodeURIComponent(target)}`, body, "PUT", { "If-Match": cyc.mtime });
    cyc.mtime = r.mtime ?? cyc.mtime; cyc.cycles = r.cycles || cyc.cycles; cyc.dirty = false;
    pickCycle(name, true);
    cycMsg(r.note ? `Saved. ${r.note}` : "Saved.");
    if (r.mtime === undefined) await loadCycles(name);
  } catch (e) {
    if (e.status === 409 && e.data?.cycles) {
      cyc.mtime = e.data.mtime; cyc.cycles = e.data.cycles; renderCycleList();
      cycMsg(`${e.message}. The list was reloaded; your edit is still in the form, save again to apply it.`, true);
    } else cycMsg(e.message, true);
  }
}

async function deleteCycle() {
  const d = cyc.draft;
  if (d.isNew || !confirm(`Delete cycle "${d.name}"? (a backup is kept on the ${shortLabel(cyc.host)})`)) return;
  try {
    const r = await api(`${apiBase(cyc.host)}/cycles/${encodeURIComponent(d.name)}`, undefined, "DELETE", { "If-Match": cyc.mtime });
    cyc.mtime = r.mtime ?? cyc.mtime; cyc.cycles = r.cycles || {}; cyc.dirty = false;
    pickCycle(Object.keys(cyc.cycles)[0] ?? null, true);
    cycMsg(r.note ? `Deleted. ${r.note}` : "Deleted.");
  } catch (e) {
    if (e.status === 409 && e.data?.cycles) { cyc.mtime = e.data.mtime; cyc.cycles = e.data.cycles; renderCycleList(); }
    cycMsg(e.message, true);
  }
}

$("#cyc-close").onclick = closeCycles;
$("#cyclemodal").addEventListener("click", e => { if (e.target.id === "cyclemodal") closeCycles(); });
$("#cyc-new").onclick = () => { pickCycle(null); $("#cyc-name").focus(); };
$("#cyc-add").onclick = () => { cyc.draft.subtasks.push({ task: "", raw: null }); markDirty(); renderCycleEditor(); focusSub(cyc.draft.subtasks.length - 1); };
$("#cyc-name").oninput = () => { cyc.draft.name = $("#cyc-name").value; markDirty(); };
$("#cyc-save").onclick = saveCycle;
$("#cyc-delete").onclick = deleteCycle;
$("#cyc-revert").onclick = () => pickCycle(cyc.draft.isNew ? (Object.keys(cyc.cycles)[0] ?? null) : cyc.draft.name, true);
$("#cyc-filter").oninput = () => { cyc.filter = $("#cyc-filter").value; renderCycleList(); };
document.addEventListener("keydown", e => {
  if (e.key === "Escape" && !$("#cyclemodal").classList.contains("hidden")) closeCycles();
  if ((e.ctrlKey || e.metaKey) && e.key === "s" && !$("#cyclemodal").classList.contains("hidden")) { e.preventDefault(); saveCycle(); }
});

// ---------- polling ----------
async function poll() {
  try {
    let st;
    if (multi) {
      try { st = await api("/api/state/all"); }
      catch (e) { if (e.status === 404) { multi = false; return poll(); } throw e; }
    } else {
      const one = await api("/api/state");
      st = { self: one.host, order: [one.host], hosts: { [one.host]: { ...one, online: true, self: true } } };
    }
    last = st;
    $("#conn").className = "dot ok";
    document.title = `YOR Hub · ${st.order.map(n => shortLabel(n)).join(" + ")}`;
    for (const host of st.order) renderHost(host, st.hosts[host]);
    // keep host sections in server order
    for (const host of st.order) $("#hosts").append(hostEls[host].sec);
  } catch (e) {
    $("#conn").className = "dot error";
  }
}
poll();
setInterval(poll, 1000);
