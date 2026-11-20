"use strict";
const $ = (s, el = document) => el.querySelector(s);
const h = (tag, attrs = {}, ...kids) => {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") el.className = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (v !== undefined && v !== null) el.setAttribute(k, v);
  }
  for (const k of kids.flat()) if (k != null) el.append(k.nodeType ? k : document.createTextNode(k));
  return el;
};

const GROUPS = [["system", "System"], ["robot", "Robot"], ["teleop", "Teleop & policy"]];
const STATE_TXT = { stopped: "stopped", running: "running", ready: "ready", exited: "done",
                    failed: "failed", stopping: "stopping…" };
let last = null;
const cards = {};   // id -> {el, refs}

function toast(msg) {
  const t = $("#toast"); t.textContent = msg; t.classList.remove("hidden");
  clearTimeout(toast._t); toast._t = setTimeout(() => t.classList.add("hidden"), 5000);
}

async function api(path, body) {
  const r = await fetch(path, body === undefined ? {} : {
    method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body) });
  if (r.status === 401) { location.reload(); throw new Error("unauthorized"); }
  const j = await r.json().catch(() => ({}));
  if (!r.ok) { const e = new Error(j.error || r.statusText); e.data = j; throw e; }
  return j;
}

function ago(t) {
  if (!t) return "";
  const s = Math.max(0, Date.now() / 1000 - t);
  if (s < 60) return `${s | 0}s`;
  if (s < 3600) return `${(s / 60) | 0}m`;
  return `${(s / 3600).toFixed(1)}h`;
}

// ---------- rendering ----------
function renderStats(st) {
  const x = st.extra || {};
  const parts = [
    ["CPU", st.cpu_temp_c != null ? `${st.cpu_temp_c}°C` : "–"],
    ["load", st.load ? st.load[0].toFixed(2) : "–"],
    ["mem", st.mem_used_pct != null ? `${st.mem_used_pct}%` : "–"],
    ["disk free", st.disk_free_gb != null ? `${st.disk_free_gb} GB` : "–"],
  ];
  if (x.throttled !== undefined)
    parts.push(["power", x.undervoltage_now ? "UNDERVOLT" : x.throttled_now ? "THROTTLED" : "ok"]);
  $("#stats").replaceChildren(...parts.map(([k, v]) => h("span", {}, `${k} `, h("b", {}, v))));
}

function renderChecks(checks) {
  $("#checks").replaceChildren(...Object.entries(checks).map(([name, c]) =>
    h("span", { class: "chip", title: name },
      h("span", { class: `dot ${c.level}` }), c.label || name, h("span", { class: "detail" }, c.detail))));
}

function renderPeers(peers) {
  $("#peers").replaceChildren(...(peers || []).map(p =>
    h("span", { class: "chip", title: p.url },
      h("span", { class: `dot ${p.online === true ? "ok" : p.online === false ? "error" : ""}` }),
      `${p.label || p.name} hub`, h("span", { class: "detail" }, p.detail || "checking…"))));
}

function makeCard(s) {
  const refs = {};
  const el = h("div", { class: "card" },
    h("div", { class: "top" },
      refs.dot = h("span", { class: "dot" }),
      refs.title = h("span", { class: "title" }),
      refs.state = h("span", { class: "state" })),
    refs.desc = h("div", { class: "desc" }),
    refs.args = h("input", { type: "text", placeholder: "args", spellcheck: "false" }),
    refs.blocked = h("div", { class: "blocked" }),
    h("div", { class: "row" },
      refs.start = h("button", { class: "primary", onclick: () => act(s.id, "start") }, "Start"),
      refs.stop = h("button", { class: "danger", onclick: () => act(s.id, "stop") }, "Stop"),
      refs.restart = h("button", { onclick: () => act(s.id, "restart") }, "Restart"),
      refs.term = h("button", { onclick: () => openTerm(s.id, s.label) }, "Terminal"),
      refs.clear = h("button", { onclick: () => act(s.id, "clear") }, "Clear")));
  refs.args.addEventListener("keydown", e => { if (e.key === "Enter") act(s.id, "start"); });
  if (s.kind === "agent") refs.args.style.display = "none";
  return { el, refs };
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
  r.args.oninput = () => (r.args.dataset.dirty = "1");
  r.args.disabled = alive;
  r.blocked.textContent = !alive && s.blocked_by.length ? `waiting on: ${s.blocked_by.join(", ")}` : "";
  r.blocked.style.display = r.blocked.textContent ? "" : "none";
  r.start.disabled = alive;
  r.start.textContent = !alive && s.blocked_by.length ? "Start anyway…" : "Start";
  r.stop.disabled = !alive || s.state === "stopping";
  r.restart.disabled = !alive;
  r.restart.style.display = s.kind === "agent" ? "none" : "";
  r.term.disabled = !s.window;
  r.clear.style.display = s.window && !alive ? "" : "none";
}

function renderServices(services) {
  const groups = $("#groups");
  const seen = new Set();
  const groupEls = {};
  for (const [g, title] of GROUPS) {
    let sec = groups.querySelector(`[data-g="${g}"]`);
    if (!sec) {
      sec = h("div", { "data-g": g }, h("h2", {}, title), h("div", { class: "cards" }));
      groups.append(sec);
    }
    groupEls[g] = $(".cards", sec);
  }
  groupEls.agents = $("#agents");
  for (const s of services) {
    seen.add(s.id);
    if (!cards[s.id]) cards[s.id] = makeCard(s);
    const parent = groupEls[s.group] || groupEls.robot;
    if (cards[s.id].el.parentNode !== parent) parent.append(cards[s.id].el);
    updateCard(cards[s.id], s);
  }
  for (const id of Object.keys(cards)) if (!seen.has(id)) { cards[id].el.remove(); delete cards[id]; }
}

function renderExtra(wins) {
  $("#extra").replaceChildren(...wins.map(w =>
    h("span", { class: "chip clickable", onclick: () => openTerm(w, w) }, "▸ ", w)));
}

async function act(id, action) {
  const c = cards[id];
  const body = {};
  if (c && action !== "stop" && action !== "clear") body.args = c.refs.args.value;
  const svc = last && last.services.find(s => s.id === id);
  if (action === "start" && svc && svc.blocked_by.length) {
    if (!confirm(`${svc.label} is waiting on: ${svc.blocked_by.join(", ")}.\nStart anyway?`)) return;
    body.force = true;
  }
  if (action === "stop" && id === "yor" && !confirm("Stop yor.py? Teleop/policy clients will lose the robot.")) return;
  try {
    await api(`/api/services/${id}/${action}`, body);
    if (c) c.refs.args.dataset.dirty = "";
  } catch (e) { toast(`${id} ${action}: ${e.message}`); }
  poll();
}

// ---------- terminals ----------
const terms = {};  // window -> {term, fit, ws, el, tab}
let activeTerm = null;

function openTerm(win, label) {
  $("#termpane").classList.remove("hidden");
  if (terms[win]) { focusTerm(win); return; }
  const el = h("div", {});
  $("#termbody").append(el);
  const term = new Terminal({ fontSize: 13, scrollback: 5000, cursorBlink: true,
    fontFamily: 'ui-monospace, Menlo, "DejaVu Sans Mono", monospace', theme: { background: "#0d1117" } });
  const fit = new FitAddon.FitAddon();
  term.loadAddon(fit);
  term.open(el);
  const tab = h("span", { class: "tab", onclick: () => focusTerm(win) }, label || win,
    h("span", { class: "x", onclick: e => { e.stopPropagation(); closeTerm(win); } }, "✕"));
  $("#termtabs").append(tab);
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws/term/${encodeURIComponent(win)}`);
  ws.binaryType = "arraybuffer";
  const sendSize = () => ws.readyState === 1 && ws.send(JSON.stringify({ t: "r", cols: term.cols, rows: term.rows }));
  ws.onopen = () => { fit.fit(); sendSize(); };
  ws.onmessage = e => term.write(typeof e.data === "string" ? e.data : new Uint8Array(e.data));
  ws.onclose = () => term.write("\r\n\x1b[90m[disconnected]\x1b[0m\r\n");
  term.onData(d => ws.readyState === 1 && ws.send(JSON.stringify({ t: "i", d })));
  term.onResize(sendSize);
  terms[win] = { term, fit, ws, el, tab };
  focusTerm(win);
}

function focusTerm(win) {
  activeTerm = win;
  for (const [w, t] of Object.entries(terms)) {
    t.el.classList.toggle("hidden", w !== win);
    t.tab.classList.toggle("active", w === win);
  }
  requestAnimationFrame(() => { terms[win].fit.fit(); terms[win].term.focus(); });
}

function closeTerm(win) {
  const t = terms[win]; if (!t) return;
  t.ws.close(); t.term.dispose(); t.el.remove(); t.tab.remove(); delete terms[win];
  const rest = Object.keys(terms);
  if (rest.length) focusTerm(rest[rest.length - 1]); else $("#termpane").classList.add("hidden");
}

$("#termclose").onclick = () => Object.keys(terms).forEach(closeTerm);
$("#termmax").onclick = () => {
  $("#termpane").classList.toggle("max");
  if (activeTerm) requestAnimationFrame(() => terms[activeTerm].fit.fit());
};
window.addEventListener("resize", () => activeTerm && terms[activeTerm] && terms[activeTerm].fit.fit());

$("#newagent").onclick = async () => {
  try { const s = await api("/api/agents", {}); await poll(); openTerm(s.id, s.label); }
  catch (e) { toast(`agent: ${e.message}`); }
};

// ---------- polling ----------
async function poll() {
  try {
    const st = await api("/api/state");
    last = st;
    $("#conn").className = "dot ok";
    $("#hostlabel").textContent = st.label;
    document.title = `YOR Hub · ${st.label}`;
    renderStats(st.stats || {});
    renderChecks(st.checks || {});
    renderPeers(st.peers);
    renderServices(st.services);
    renderExtra(st.extra_windows || []);
  } catch (e) {
    $("#conn").className = "dot error";
  }
}
poll();
setInterval(poll, 1000);
