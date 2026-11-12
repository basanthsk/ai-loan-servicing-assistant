// Loan Servicing Assistant: web client. Plain JavaScript, no build step.
"use strict";

const $ = (s) => document.querySelector(s);
const state = { token: null, ws: null, bubble: null, raw: "", name: "", turnOpen: false };
window.app = state;

// ---------- helpers ----------
function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function render(text) {
  // Minimal formatting: bullet lists, **bold**, clause chips like [LT-6.1].
  text = text.replace(/【[\s\u200b]*】/g, "").replace(/【([^】]+)】/g, "[$1]");  // model citation style
  const lines = esc(text.trim()).split(/\n+/);
  let html = "", inList = false;
  for (const line of lines) {
    const item = line.match(/^\s*[-•*]\s+(.*)/);
    if (item && !inList) { html += "<ul>"; inList = true; }
    if (!item && inList) { html += "</ul>"; inList = false; }
    html += item ? `<li>${item[1]}</li>` : `<p style="margin:0 0 6px">${line}</p>`;
  }
  if (inList) html += "</ul>";
  return html
    .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
    .replace(/\[([A-Z]{2,4}(?:[-‑–][A-Z])?)[-‑–](\d+(?:\.\d+)?)\]/g,
      (_, a, b) => `<span class="clause" title="Policy clause">${a.replace(/[‑–]/g, '-')}-${b}</span>`);
}
const inr = (n) => (n == null ? "–" : "₹" + Math.round(n).toLocaleString("en-IN"));
const nice = (iso) => (iso ? new Date(iso + "T00:00:00").toLocaleDateString("en-IN", { day: "numeric", month: "short", year: "numeric" }) : "–");
async function api(path, opts = {}) {
  const r = await fetch(path, { ...opts, headers: { "Content-Type": "application/json", ...(state.token ? { Authorization: "Bearer " + state.token } : {}), ...(opts.headers || {}) } });
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
  return r.json();
}
function scrollDown() { const m = $("#messages"); m.scrollTop = m.scrollHeight; }
function addMsg(cls, html) {
  const div = document.createElement("div");
  div.className = "msg " + cls;
  div.innerHTML = html;
  $("#messages").appendChild(div);
  scrollDown();
  return div;
}
function setStatus(text) {
  $("#status").hidden = !text;
  $("#status-text").textContent = text || "";
}

// ---------- login ----------
async function loadCustomers() {
  try {
    const list = await api("/api/customers");
    $("#customer-list").innerHTML = "";
    const notes = { C1001: "Floating-rate loan with a recent rate reset, failed payments, a card", C1002: "Floating-rate loan, clean history", C1003: "Fixed-rate loan past its lock-in" };
    for (const c of list) {
      const b = document.createElement("button");
      b.className = "customer";
      b.innerHTML = `<strong>${esc(c.name)}</strong><span class="sub">${esc(c.customer_id)} · ${esc(c.city)}${notes[c.customer_id] ? " · " + esc(notes[c.customer_id]) : ""}</span>`;
      b.onclick = () => login(c.customer_id);
      $("#customer-list").appendChild(b);
    }
  } catch (e) {
    $("#customer-list").innerHTML = `<p class="error">Can't reach the systems: ${esc(e.message)}. Is <code>poetry run poe up</code> running?</p>`;
  }
}
async function login(customerId) {
  try {
    const r = await api("/api/login", { method: "POST", body: JSON.stringify({ customer_id: customerId }) });
    state.token = r.token; state.name = r.name;
    try { sessionStorage.setItem("token", r.token); sessionStorage.setItem("name", r.name); } catch {}
    start();
  } catch (e) {
    $("#login-error").hidden = false;
    $("#login-error").textContent = e.message;
  }
}
function start() {
  $("#login").hidden = true;
  $("#app").hidden = false;
  $("#who-name").textContent = state.name;
  connect();
  refreshAccount();
  refreshEngine();
  showSuggestions(["What's my next EMI?", "Why did my EMI go up this month?", "What if I prepay ₹50,000?",
    "Can I foreclose without a penalty?", "Which payments failed in the last 6 months?", "What are my options on my loan?"]);
}
$("#logout").onclick = () => {
  try { sessionStorage.clear(); } catch {}
  location.reload();
};

// ---------- conversation ----------
function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws?token=${encodeURIComponent(state.token)}`);
  state.ws = ws;
  ws.onmessage = (m) => onEvent(JSON.parse(m.data));
  ws.onclose = (e) => {
    if (e.code === 4401) { try { sessionStorage.clear(); } catch {} location.reload(); return; }
    setStatus("Reconnecting…");
    setTimeout(connect, 1500);
  };
  ws.onopen = () => setStatus("");
}
function send(text) {
  if (!text.trim() || !state.ws || state.ws.readyState !== 1) return;
  state.ws.send(JSON.stringify({ type: "message", text }));
  $("#suggestions").innerHTML = "";
}
$("#composer").onsubmit = (e) => {
  e.preventDefault();
  send($("#input").value);
  $("#input").value = "";
};
function showSuggestions(items) {
  const box = $("#suggestions");
  box.innerHTML = "";
  for (const t of items) {
    const b = document.createElement("button");
    b.type = "button"; b.textContent = t;
    b.onclick = () => send(t);
    box.appendChild(b);
  }
}
function ensureBubble() {
  if (!state.bubble) {
    state.bubble = addMsg("bot", "");
    state.raw = "";
  }
  return state.bubble;
}

function onEvent(e) {
  window.dispatchEvent(new CustomEvent("assistant-event", { detail: e }));
  switch (e.type) {
    case "user":
      state.bubble = null;
      addMsg("user", esc(e.text) + (e.channel === "voice" ? '<span class="channel">🎙</span>' : ""));
      setStatus("Thinking…");
      break;
    case "status":
      setStatus(e.text + "…");
      break;
    case "token":
      ensureBubble();
      state.raw += e.text;
      state.bubble.innerHTML = render(state.raw);
      setStatus("");
      scrollDown();
      break;
    case "replace":
      ensureBubble();
      state.raw = e.text;
      state.bubble.innerHTML = render(state.raw) + '<div class="muted small">Answer corrected by the number check.</div>';
      break;
    case "action_required":
      actionCard(e.action);
      break;
    case "otp_required":
      otpCard(e);
      break;
    case "action_result":
      document.querySelectorAll(`[data-action="${e.action_id}"] button`).forEach((b) => (b.disabled = true));
      if (e.status === "executed") refreshAccount();
      break;
    case "done":
      setStatus("");
      if (state.bubble && e.explain) addExplain(state.bubble, e);
      if (state.cardThisTurn) { $("#messages").appendChild(state.cardThisTurn); state.cardThisTurn = null; scrollDown(); }
      state.bubble = null;
      if (e.trace) showTrace(e.trace);
      refreshEngine();
      if (e.pack || e.intent) refreshAccountSoon();
      break;
    case "voice_metric":
      $("#turn-total").textContent += ` · first audio ${e.first_audio_ms} ms after you stopped`;
      break;
    case "cancelled":
      setStatus("");
      if (state.bubble) state.bubble.insertAdjacentHTML("beforeend", '<div class="muted small">(interrupted)</div>');
      state.bubble = null;
      break;
    case "error":
      setStatus("");
      addMsg("system", esc(e.text));
      break;
  }
}

function addExplain(bubble, e) {
  const ex = e.explain;
  const tools = ex.tools.map((t) => `<li><code>${esc(t.name)}</code> ${t.ok ? "" : "(failed) "}<span class="num">${t.ms} ms</span></li>`).join("");
  const rules = ex.rules.filter((r) => r.rule).map((r) => `<li><code>${esc(r.rule)}</code> → ${esc(r.clause || "")}</li>`).join("");
  const clauses = ex.clauses.map((c) => `<li><span class="clause">${esc(c.id)}</span> ${esc(c.title)}${c.source ? ` <span class="small">(${esc(c.source)})</span>` : ""}</li>`).join("");
  const badge = ex.verified
    ? '<span class="badge ok">numbers verified</span>'
    : `<span class="badge warn">unverified: ${esc((e.unverified_numbers || []).join(", "))}</span>`;
  if (!tools && !rules && !clauses) return;
  bubble.insertAdjacentHTML("beforeend", `<details class="explain"><summary>How I got this ${badge}</summary>
    ${tools ? `<div>Data and calculations</div><ul>${tools}</ul>` : ""}
    ${rules ? `<div>Rules applied</div><ul>${rules}</ul>` : ""}
    ${clauses ? `<div>Policy cited</div><ul>${clauses}</ul>` : ""}</details>`);
}

function actionCard(a) {
  const div = document.createElement("div");
  div.className = "card-action";
  div.dataset.action = a.action_id;
  div.innerHTML = `<div class="title">Confirm ${esc(a.title)}</div><div>${esc(a.summary)}</div>
    <div class="muted small">${a.requires_otp ? "You'll be asked for a one-time code." : ""}</div>
    <div class="buttons"><button class="btn primary">Confirm</button><button class="btn">Cancel</button></div>`;
  const [ok, no] = div.querySelectorAll("button");
  const answer = (approve) => {
    ok.disabled = no.disabled = true;
    state.ws.send(JSON.stringify({ type: "confirm", approve }));
  };
  ok.onclick = () => answer(true);
  no.onclick = () => answer(false);
  $("#messages").appendChild(div);
  state.cardThisTurn = div;
  scrollDown();
}

function otpCard(e) {
  const div = document.createElement("div");
  div.className = "card-action";
  div.dataset.action = e.action_id;
  div.innerHTML = `<div class="title">One-time code</div>
    <div>Sent to your phone ending ${esc(e.phone_last4)}. <span class="muted small">(Demo: the code is ${esc(e.demo_code)}.)</span></div>
    <form class="otp"><input inputmode="numeric" maxlength="6" aria-label="One-time code"><button class="btn primary">Verify</button></form>`;
  const form = div.querySelector("form");
  form.onsubmit = (ev) => {
    ev.preventDefault();
    state.ws.send(JSON.stringify({ type: "otp", code: form.querySelector("input").value }));
  };
  $("#messages").appendChild(div);
  div.querySelector("input").focus();
  scrollDown();
}

// ---------- account panel ----------
let refreshTimer = null;
function refreshAccountSoon() { clearTimeout(refreshTimer); refreshTimer = setTimeout(refreshAccount, 400); }
async function refreshAccount() {
  let a;
  try { a = await api("/api/account"); } catch { return; }
  const loans = Array.isArray(a.loans) ? a.loans : [];
  $("#loans").innerHTML = loans.length ? loans.map((l) => `
    <div class="acct">
      <div class="head"><strong>${esc(l.product)}</strong><span class="chip ${l.rate_type}">${esc(l.rate_type)} ${l.current_rate_pct ?? ""}%</span></div>
      <div class="id">${esc(l.loan_id)}</div>
      <dl><dt>Next EMI</dt><dd>${inr(l.current_emi)} on ${nice(l.next_due_date)}</dd>
      <dt>Outstanding</dt><dd>${inr(l.outstanding_principal)}</dd>
      <dt>EMIs left</dt><dd>${l.remaining_installments}</dd></dl>
    </div>`).join("") : `<p class="muted small">${a.loans && a.loans.unavailable ? "Loans system unavailable right now." : "No loans."}</p>`;

  const cards = Array.isArray(a.cards) ? a.cards : [];
  $("#cards-section").hidden = !cards.length;
  $("#cards").innerHTML = cards.map((c) => `
    <div class="acct">
      <div class="head"><strong>${esc(c.product)}</strong><span class="chip">${esc(c.status)}</span></div>
      <div class="id">${esc(c.card_id)} · ending ${esc(c.last4)}</div>
      <dl><dt>Statement due</dt><dd>${inr(c.statement_balance)} by ${nice(c.due_date)}</dd>
      <dt>Minimum due</dt><dd>${inr(c.minimum_due)}</dd><dt>Limit</dt><dd>${inr(c.credit_limit)}</dd></dl>
    </div>`).join("");

  const pays = (a.payments && a.payments.payments) || [];
  $("#payments").innerHTML = pays.length ? pays.map((p) => `
    <li><span>${nice(p.attempt_date)} · ${esc(p.loan_id)}</span><span class="num">${inr(p.amount)} <span class="chip ${p.status}">${p.status}</span></span>
    ${p.status === "failed" ? `<span class="sub">${esc(p.failure_reason)}${p.charge ? ` · charge ${inr(p.charge)}` : ""}</span>` : ""}</li>`).join("")
    : `<li class="muted">${a.payments && a.payments.unavailable ? "Payments system unavailable right now." : "No payments."}</li>`;

  const reqs = Array.isArray(a.requests) ? a.requests : [];
  $("#requests").innerHTML = reqs.length ? reqs.map((r) => `
    <li><span><code>${esc(r.request_id)}</code> ${esc(r.type.replaceAll("_", " "))}</span><span class="chip">${esc(r.status)}</span>
    <span class="sub">Raised ${esc(r.created_at.slice(0, 10))} · due ${nice(r.sla_due)}</span></li>`).join("")
    : '<li class="muted">No requests yet.</li>';
}

// ---------- under the hood ----------
function showTrace(t) {
  const total = Math.max(t.total_ms, 1);
  const spans = t.spans.filter((s) => s.kind !== "turn").sort((a, b) => a.start_ms - b.start_ms);
  $("#turn-total").textContent = `${Math.round(t.total_ms)} ms · ${t.channel}`;
  $("#waterfall").innerHTML = spans.map((s) => `
    <div class="wf-row"><span class="wf-name" title="${esc(s.kind)}: ${esc(s.name)}">${esc(s.name)}${s.attrs && s.attrs.cancelled ? " (discarded)" : ""}${s.attrs && s.attrs.cached ? " (cache)" : ""}</span>
      <span class="wf-track"><span class="wf-bar k-${s.kind} ${s.attrs && s.attrs.cancelled ? "cancelled" : s.ok ? "" : "bad"}" style="left:${(100 * s.start_ms) / total}%;width:${(100 * s.duration_ms) / total}%"></span></span>
      <span class="wf-ms">${Math.round(s.duration_ms)}</span></div>`).join("") +
    `<div class="wf-marks small">first status ${t.marks.first_status ?? "–"} ms · first words ${t.marks.first_token ?? "–"} ms${t.marks.first_audio ? ` · first audio ${t.marks.first_audio} ms` : ""}</div>`;
}
async function refreshEngine() {
  try {
    const m = await api("/api/metrics");
    const order = (k) => (k.startsWith("turn") ? 0 : k.startsWith("decider") ? 1 : k.startsWith("llm") ? 2 : k.startsWith("tool") ? 3 : 4);
    const rows = Object.entries(m.latency).sort((a, b) => order(a[0]) - order(b[0]) || a[0].localeCompare(b[0]));
    $("#metrics tbody").innerHTML = rows.map(([k, v]) => `<tr><td class="k">${esc(k)}</td><td class="r">${v.p50} / ${v.p95}</td><td class="r muted">n=${v.n}</td></tr>`).join("");
    const f = await api("/api/faults");
    $("#faults tbody").innerHTML = Object.entries(f).map(([sys, v]) => v.unreachable
      ? `<tr><td class="k">${sys}</td><td colspan="3" class="muted">not running</td></tr>`
      : `<tr><td class="k">${sys}</td><td class="r">${Math.round(v.latency_ms)} ms</td>
         <td class="r">circuit ${esc(m.circuit_breakers[sys] || "closed")}</td>
         <td class="r"><select data-sys="${sys}" aria-label="${sys} behaviour">
           <option value="normal" ${!v.down && v.failure_rate < 0.5 ? "selected" : ""}>normal</option>
           <option value="flaky" ${!v.down && v.failure_rate >= 0.5 ? "selected" : ""}>50% failures</option>
           <option value="down" ${v.down ? "selected" : ""}>down</option></select></td></tr>`).join("");
    document.querySelectorAll("#faults select").forEach((s) => (s.onchange = async () => {
      const body = s.value === "down" ? { down: true } : s.value === "flaky" ? { down: false, failure_rate: 0.5 } : { down: false, failure_rate: 0.03 };
      await api(`/api/faults/${s.dataset.sys}`, { method: "PUT", body: JSON.stringify(body) });
      refreshEngine();
    }));
    const audit = await api("/api/audit").catch(() => []);
    $("#audit").innerHTML = audit.length ? audit.map((x) => `<li><span><code>${esc(x.action)}</code> ${esc(x.outcome)}</span><span class="muted">${esc(x.ts.slice(11, 19))}</span>
      <span class="sub">${esc(x.params.request_id || x.params.error || "")} · key ${esc((x.idempotency_key || "").slice(0, 8))}…</span></li>`).join("") : '<li class="muted">No actions yet.</li>';
  } catch {}
}
document.querySelectorAll(".tab").forEach((t) => (t.onclick = () => {
  document.querySelectorAll(".tab").forEach((x) => x.classList.toggle("active", x === t));
  $("#tab-account").hidden = t.dataset.tab !== "account";
  $("#tab-engine").hidden = t.dataset.tab !== "engine";
  if (t.dataset.tab === "engine") refreshEngine();
}));

// ---------- boot ----------
try {
  state.token = sessionStorage.getItem("token");
  state.name = sessionStorage.getItem("name") || "";
} catch {}
if (state.token) start(); else loadCustomers();
