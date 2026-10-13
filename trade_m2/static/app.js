const state = { rules: [], events: [], latestId: 0, source: null, jobTimer: null, telegramTesting: false };
const $ = (id) => document.getElementById(id);

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>'"]/g, (char) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;"
  })[char]);
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { "Content-Type": "application/json", ...(options.headers || {}) }
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body.detail || `Request failed (${response.status})`);
  return body;
}

function showMessage(text, error = false) {
  const element = $("message");
  element.textContent = text;
  element.classList.toggle("error", error);
  element.classList.remove("hidden");
  window.clearTimeout(showMessage.timer);
  showMessage.timer = window.setTimeout(() => element.classList.add("hidden"), 9000);
}

function showQueryMessage() {
  const query = new URLSearchParams(location.search);
  if (query.has("error")) showMessage(query.get("error"), true);
  if (query.has("login")) showMessage("Upstox connected. Live monitoring is ready.");
  if (query.size) history.replaceState({}, "", location.pathname);
}

async function refreshStatus() {
  try {
    const data = await api("/api/status");
    $("authStatus").textContent = data.authenticated ? "Connected" : (data.configured ? "Login needed" : "Not configured");
    $("userName").textContent = data.authenticated ? data.user_name : "OAuth required daily";
    $("loginButton").textContent = data.authenticated ? "Reconnect Upstox" : "Connect Upstox";
    const feed = data.monitor;
    $("feedStatus").textContent = feed.stale ? "Stale" : (feed.connected ? "Streaming" : (feed.running ? "Reconnecting" : "Stopped"));
    $("feedDetail").textContent = `${feed.active_subscriptions} live · ${feed.recovering_subscriptions} recovering`;
    $("ruleCount").textContent = `${data.rule_count} / 200`;
    $("activeCount").textContent = `${data.active_rule_count} active · ${data.market_open ? "market open" : "market closed"}`;
    if (feed.last_error) $("feedDetail").title = feed.last_error;
    if (data.load_job?.status === "RUNNING" && !state.jobTimer) watchJob(data.load_job.id);
    renderTelegram(data.telegram);
    refreshSectors();
    if (data.authenticated) refreshMovement();
  } catch (error) {
    $("feedStatus").textContent = "Unavailable";
  }
}

function renderTelegram(data) {
  if (!data) return;
  $("telegramStatus").textContent = data.error || (data.running
    ? `Ready · ${data.recipients.length} recipient(s) · delivery continues with the browser closed`
    : "Telegram delivery is stopped. Restart Trade M3.");
  $("telegramTestButton").disabled = !data.configured || !data.running || state.telegramTesting;
  $("telegramTestButton").textContent = state.telegramTesting ? "Testing…"
    : (data.recipients.length === 2 ? "Send test to both chats" : "Send Telegram test");
  $("telegramRecipients").innerHTML = data.recipients.map((recipient, index) => {
    const counts = recipient.counts;
    return `<div class="telegram-recipient">
      <strong>Recipient ${index + 1} · ${escapeHtml(recipient.chat)}</strong>
      <p class="muted">${counts.sent || 0} sent · ${counts.pending || 0} pending · ${counts.failed || 0} failed · ${counts.expired || 0} expired</p>
      ${recipient.last_error ? `<p class="telegram-error">${escapeHtml(recipient.last_error)}</p>` : ""}
    </div>`;
  }).join("");
}

async function testTelegram() {
  state.telegramTesting = true;
  $("telegramTestButton").disabled = true;
  $("telegramTestButton").textContent = "Testing…";
  $("telegramTestResult").textContent = "Queuing a test for each configured chat…";
  try {
    const test = await api("/api/telegram/test", { method: "POST", body: "{}" });
    const deadline = Date.now() + 60000;
    while (Date.now() < deadline) {
      const result = await api(`/api/telegram/tests/${encodeURIComponent(test.test_id)}`);
      $("telegramTestResult").textContent = result.recipients.map((recipient, index) =>
        `Recipient ${index + 1} (${recipient.chat}): ${recipient.last_status}${recipient.last_error ? ` — ${recipient.last_error}` : ""}`
      ).join(" | ");
      if (result.recipients.every((recipient) => !["pending", "waiting"].includes(recipient.last_status))) return;
      await new Promise((resolve) => window.setTimeout(resolve, 1000));
    }
    $("telegramTestResult").textContent += " | Still queued; retries continue in the background. Check delivery status above.";
  } catch (error) {
    $("telegramTestResult").textContent = error.message;
  } finally {
    state.telegramTesting = false;
    refreshStatus();
  }
}

async function refreshMovement() {
  try {
    const data = await api("/api/market-movement");
    $("movementValue").textContent = `${data.moving_percentage_display}`;
    $("movementDetail").textContent = `VIX ${data.vix_close} · ${data.reference_date}`;
    if (!$("percentage").value) $("percentage").placeholder = data.moving_percentage;
  } catch (_) { /* Status already explains authentication. */ }
}

function renderRules() {
  const filter = $("filter").value.trim().toUpperCase();
  const rules = state.rules.filter((rule) => !filter || rule.tradingsymbol.includes(filter));
  if (!rules.length) {
    $("rulesBody").innerHTML = `<tr><td colspan="7" class="empty-state">${state.rules.length ? "No matching stocks." : "Load the Nifty 200 to begin."}</td></tr>`;
    return;
  }
  $("rulesBody").innerHTML = rules.map((rule) => {
    const zone = rule.opening_zone ? escapeHtml(rule.opening_zone.replace("ON_", "AT ")) : "waiting";
    return `<tr>
      <td><span class="symbol">${escapeHtml(rule.tradingsymbol)}</span></td>
      <td>₹${escapeHtml(rule.reference_close_display)}<br><small>${escapeHtml(rule.reference_date)}</small></td>
      <td>${rule.opening_price ? `₹${escapeHtml(rule.opening_price)}` : "—"}<br><small>${zone}</small></td>
      <td class="upper">₹${escapeHtml(rule.upper_level_display)}${rule.upper_sent ? '<span class="sent">✓ sent</span>' : ""}</td>
      <td class="lower">₹${escapeHtml(rule.lower_level_display)}${rule.lower_sent ? '<span class="sent">✓ sent</span>' : ""}</td>
      <td><span class="zone ${rule.active ? "active" : ""}">${rule.active ? "active" : "paused"}</span></td>
      <td><button class="button small secondary rule-toggle" data-id="${rule.id}" data-active="${!rule.active}">${rule.active ? "Pause" : "Start"}</button></td>
    </tr>`;
  }).join("");
}

async function refreshRules() {
  try {
    state.rules = await api("/api/rules");
    renderRules();
  } catch (error) { showMessage(error.message, true); }
}

function sectorSummary(event) {
  try { const context = typeof event.sector_context === "string" ? JSON.parse(event.sector_context) : event.sector_context;
    return context?.summary || "Sector: unavailable";
  } catch (_) { return "Sector: unavailable"; }
}
async function refreshSectors() {
  try {
    const data = await api("/api/sectors");
    $("sectorStatus").textContent = `${data.mapping_source} · ${data.unmapped} unmapped · ${new Date(data.as_of).toLocaleTimeString()}`;
    $("sectorRows").innerHTML = data.sectors.map(row => `<tr><td>${escapeHtml(row.sector)}</td><td>${row.mean_percent === null ? "—" : `${row.mean_percent.toFixed(2)}%`}</td><td>${row.rising}/${row.fresh}</td><td>${row.fresh}/${row.total}</td><td>${escapeHtml(row.bias)}</td></tr>`).join("");
  } catch (_) { $("sectorStatus").textContent = "Sector data unavailable"; $("sectorRows").innerHTML = ""; }
}
function optionText(event) {
  const raw = event.option_context;
  if (!raw) return "Option selection pending…";
  try {
    const d = typeof raw === "string" ? JSON.parse(raw) : raw;
    if (d.status !== "READY") return `Option unavailable: ${d.reason}`;
    const c = d.candidate;
    return `Candidate BUY ${c.strike} ${c.kind} · expiry ${c.expiry} · lot ${c.lot_size} · bid/ask ₹${c.bid}/₹${c.ask} · delta ${c.delta} · ${c.reason}`;
  } catch (_) { return "Option selection unavailable"; }
}
async function refreshEnrichment() {
  try {
    const today = new Date().toLocaleDateString('en-CA', { timeZone: 'Asia/Kolkata' });
    const [history, current] = await Promise.all([
      api(`/api/review/alerts?trading_date=${today}`),
      api('/api/events?limit=500')
    ]);
    const byId = new Map(current.map(e => [Number(e.id), e]));
    state.events = history.alerts.map(row => {
      const live = byId.get(Number(row.id));
      const event = live ? { ...row, ...live } : { ...row };
      event.created_at = event.created_at || null;
      event.option_context = event.option_context || row.option_context;
      event.entry_price_display = event.entry_price_display || Number(event.entry_price).toLocaleString('en-IN', {minimumFractionDigits:2,maximumFractionDigits:2});
      event.stop_loss_display = event.stop_loss_display || Number(event.stop_loss).toLocaleString('en-IN', {minimumFractionDigits:2,maximumFractionDigits:2});
      event.risk_percent_display = event.risk_percent_display || `${Number(event.risk_percent).toFixed(2)}%`;
      return event;
    });
    state.latestId = Math.max(state.latestId, ...current.map(e => Number(e.id)), 0);
    renderEvents();
  } catch (_) { /* Existing stream remains active; next poll retries the audit log. */ }
}
function eventMarkup(event) {
  const when = event.created_at ? new Date(event.created_at).toLocaleTimeString("en-IN", { timeZone: "Asia/Kolkata", hour: "2-digit", minute: "2-digit", second: "2-digit" }) : "time unavailable";
  const candle = new Date(event.event_time).toLocaleTimeString("en-IN", { timeZone: "Asia/Kolkata", hour: "2-digit", minute: "2-digit", second: "2-digit" });
  const sideClass = event.trade_side === "SHORT" ? "short" : "";
  return `<article class="alert-row ${sideClass}">
    <span class="alert-side">${escapeHtml(event.trade_side)}</span>
    <span><span class="alert-symbol">${escapeHtml(event.tradingsymbol)}</span><br><span class="alert-meta">${escapeHtml(event.boundary)} · open ${escapeHtml(event.opening_zone)}</span></span>
    <span class="alert-price">Entry ₹${escapeHtml(event.entry_price_display)}</span>
    <span class="alert-price">SL ₹${escapeHtml(event.stop_loss_display)} (${escapeHtml(event.risk_percent_display)})</span>
    <span class="alert-meta sector-context">${escapeHtml(sectorSummary(event))}</span>
    <span class="alert-meta sector-context">${escapeHtml(optionText(event))} <button class="button small secondary" type="button" data-paper-alert="${Number(event.id)}">Paper trade</button></span>
    <span class="alert-meta">Alert ${when} IST · ${escapeHtml(event.source)}${event.source === "RECOVERY" ? ` · market candle ${candle} IST` : ""}</span>
  </article>`;
}

function renderEvents() {
  $("alertCount").textContent = `${state.events.length} alert${state.events.length === 1 ? "" : "s"}`;
  $("alerts").innerHTML = state.events.length
    ? [...state.events].sort((a, b) => new Date(b.created_at || 0) - new Date(a.created_at || 0) || Number(b.history_id || b.id) - Number(a.history_id || a.id)).map(eventMarkup).join("")
    : '<div class="empty-state">No Case 2 crossings yet.</div>';
}

function notifyEvent(event) {
  const detected = new Date(event.created_at || event.event_time).getTime();
  const signal = new Date(event.event_time).getTime();
  if (!["LIVE", "RECOVERY"].includes(event.source) || Date.now() - detected > 60000 || detected - signal > 240000) return;
  if (!("Notification" in window) || Notification.permission !== "granted") return;
  const title = `${event.trade_side} ${event.tradingsymbol} · ${event.boundary}${event.source === "RECOVERY" ? " · recently recovered" : ""}`;
  const body = `Entry ₹${event.entry_price_display} · SL ₹${event.stop_loss_display} (${event.risk_percent_display}) · ${sectorSummary(event)} · ${optionText(event)}`;
  const notification = new Notification(title, { body, tag: `trade-m2-${event.id}`, requireInteraction: true });
  notification.onclick = () => { window.focus(); notification.close(); };
}

function acceptEvent(event, notify = true) {
  const id = Number(event.id);
  if (state.events.some((existing) => Number(existing.id) === id)) return;
  state.events.push(event);
  state.events = state.events.slice(-500);
  state.latestId = Math.max(state.latestId, id);
  renderEvents();
  if (notify) notifyEvent(event);
  refreshRules();
}

async function startEvents() {
  try {
    const snapshot = await api("/api/events/snapshot");
    state.events = snapshot.events;
    state.latestId = snapshot.latest_id;
    renderEvents();
    refreshEnrichment();
  } catch (error) { showMessage(error.message, true); }
  connectEventSource();
}

function connectEventSource() {
  state.source?.close();
  const source = new EventSource(`/api/events/stream?after_id=${state.latestId}`);
  state.source = source;
  source.addEventListener("alert", (message) => acceptEvent(JSON.parse(message.data), true));
  source.onerror = async () => {
    try {
      const missed = await api(`/api/events?after_id=${state.latestId}&limit=500`);
      missed.forEach((event) => acceptEvent(event, true));
    } catch (_) { /* EventSource retries on its own. */ }
  };
}

async function loadNifty200() {
  const button = $("loadButton");
  const value = $("percentage").value.trim();
  button.disabled = true;
  try {
    const body = value ? { percentage: value } : { percentage: null };
    const job = await api("/api/nifty200/load", { method: "POST", body: JSON.stringify(body) });
    $("jobArea").classList.remove("hidden");
    watchJob(job.id);
  } catch (error) {
    showMessage(error.message, true);
    button.disabled = false;
  }
}

function watchJob(id) {
  window.clearInterval(state.jobTimer);
  const update = async () => {
    try {
      const job = await api(`/api/jobs/${id}`);
      const total = job.total || 200;
      const percent = Math.round((job.processed / total) * 100);
      $("jobArea").classList.remove("hidden");
      $("progressBar").style.width = `${percent}%`;
      $("jobText").textContent = job.status === "RUNNING"
        ? `${job.processed}/${total} processed · ${job.created} ready · ${job.failed} failed · ${job.current_symbol || "starting"}`
        : `${job.status}: ${job.created} ready, ${job.failed} failed. Source: ${job.source || "—"}`;
      if (job.status !== "RUNNING") {
        window.clearInterval(state.jobTimer);
        state.jobTimer = null;
        $("loadButton").disabled = false;
        await Promise.all([refreshStatus(), refreshRules()]);
        await startEvents();
        if (job.status === "FAILED") showMessage(job.fatal_error || "Nifty 200 load failed.", true);
        else if (job.failed) showMessage(`${job.failed} symbols failed. Check the job API for details.`, true);
      }
    } catch (error) {
      window.clearInterval(state.jobTimer);
      state.jobTimer = null;
      $("loadButton").disabled = false;
      showMessage(error.message, true);
    }
  };
  update();
  state.jobTimer = window.setInterval(update, 1200);
}

async function setBulk(active) {
  try {
    await api("/api/rules/bulk-status", { method: "POST", body: JSON.stringify({ active }) });
    await Promise.all([refreshStatus(), refreshRules()]);
  } catch (error) { showMessage(error.message, true); }
}

$("notifyButton").addEventListener("click", async () => {
  if (!("Notification" in window)) return showMessage("This browser does not support desktop notifications.", true);
  const permission = await Notification.requestPermission();
  $("notifyButton").textContent = permission === "granted" ? "Browser alerts enabled" : "Enable browser alerts";
  if (permission !== "granted") showMessage("Browser notifications were not enabled.", true);
});
$("loadButton").addEventListener("click", loadNifty200);
$("telegramTestButton").addEventListener("click", testTelegram);
$("filter").addEventListener("input", renderRules);
$("startAll").addEventListener("click", () => setBulk(true));
$("pauseAll").addEventListener("click", () => setBulk(false));
$("rulesBody").addEventListener("click", async (event) => {
  const button = event.target.closest(".rule-toggle");
  if (!button) return;
  button.disabled = true;
  try {
    await api(`/api/rules/${button.dataset.id}`, {
      method: "PATCH", body: JSON.stringify({ active: button.dataset.active === "true" })
    });
    await refreshRules();
  } catch (error) { showMessage(error.message, true); button.disabled = false; }
});

let homeTicketId = null;
let homeCandidate = null;
let homeSubmitting = false;
const homeSubmit = () => $("homeTicketForm").querySelector('[type=submit]');
async function refreshHomePreview() {
  if (homeTicketId === null) return;
  homeSubmit().disabled = true;
  $("homeTicketError").textContent = '';
  $("homeTicketDetail").textContent = 'Fetching current option quote…';
  homeCandidate = null;
  try {
    const d = await api(`/api/paper/alerts/${homeTicketId}/preview`, { method: 'POST', body: JSON.stringify({budget: Number($("homeBudget").value), lots: Number($("homeLots").value)}) });
    homeCandidate = d.candidate;
    $("homeTicketDetail").textContent = `BUY ${homeCandidate.symbol} · ${homeCandidate.expiry} · ${homeCandidate.lot_size} per lot · bid/ask ₹${homeCandidate.bid}/₹${homeCandidate.ask} · ${homeCandidate.reason}`;
    homeSubmit().disabled = false;
    estimateHomeTicket();
  } catch (error) {
    $("homeTicketDetail").textContent = 'Current option preview unavailable.';
    $("homeTicketError").textContent = error.message;
  }
}
function estimateHomeTicket() {
  if (!homeCandidate) return;
  const unit = Number(homeCandidate.ask) * homeCandidate.lot_size;
  const budget = Number($("homeBudget").value);
  const lots = Number($("homeLots").value) || Math.max(0, Math.floor(budget / unit));
  $("homeTicketEstimate").textContent = `${lots} whole lot(s), ${lots * homeCandidate.lot_size} units · simulated entry ₹${(lots * unit).toFixed(2)}. Target and stop apply to the option premium.`;
}
async function openHomeTicket(id) {
  homeTicketId = id;
  homeCandidate = null;
  const event = state.events.find(e => Number(e.id) === id);
  $("homeTicketTitle").textContent = `${event?.tradingsymbol || 'Alert'} ${event?.trade_side || ''} · #${id}`;
  $("homeTicketEstimate").textContent = '';
  $("homeTicketError").textContent = '';
  homeSubmit().disabled = true;
  if (!$("homeTicket").open) $("homeTicket").showModal();
  try {
    const snapshot = await api('/api/paper');
    for (const [field,key] of [['homeBudget','budget'],['homeLots','lots'],['homeTarget','target_percent'],['homeStop','stop_percent'],['homeHold','hold_minutes']]) $(field).value = snapshot.settings[key];
    await refreshHomePreview();
  } catch (error) { $("homeTicketError").textContent = error.message; }
}
$("alerts").addEventListener('click', e => {
  const button = e.target.closest('[data-paper-alert]');
  if (button) openHomeTicket(Number(button.dataset.paperAlert));
});
$("homeTicketForm").addEventListener('input', estimateHomeTicket);
$("homePreviewRetry").onclick = refreshHomePreview;
$("homeTicketCancel").onclick = () => $("homeTicket").close();
$("homeTicketForm").onsubmit = async e => {
  e.preventDefault(); if (homeSubmitting) return; homeSubmitting = true;
  homeSubmit().disabled = true;
  try {
    await refreshHomePreview();
    if (!homeCandidate) throw new Error($("homeTicketError").textContent);
    const body = {
      budget: Number($("homeBudget").value), lots: Number($("homeLots").value),
      target_percent: Number($("homeTarget").value), stop_percent: Number($("homeStop").value),
      hold_minutes: Number($("homeHold").value)
    };
    const trade = await api(`/api/paper/alerts/${homeTicketId}`, { method: 'POST', body: JSON.stringify(body) });
    $("homeTicket").close();
    showMessage(`Paper option trade #${trade.id} recorded. See the Paper terminal for fills and P&L.`);
    refreshEnrichment();
  } catch (error) { $("homeTicketError").textContent = error.message; }
  finally { homeSubmitting = false; if (homeCandidate) homeSubmit().disabled = false; }
};

showQueryMessage();
refreshStatus();
refreshRules();
startEvents();
window.setInterval(refreshStatus, 5000);
window.setInterval(refreshRules, 8000);
window.setInterval(refreshEnrichment, 3000);
if ("Notification" in window && Notification.permission === "granted") $("notifyButton").textContent = "Browser alerts enabled";
