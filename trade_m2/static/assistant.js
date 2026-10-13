"use strict";
const $ = (id) => document.getElementById(id);
let positions = [];
let history = [];
let marketBusy = false;
let chatBusy = false;
let sessionPositions = [];
let lastImagePositions = [];

async function api(url, options = {}) {
  const response = await fetch(url, {cache: "no-store", ...options});
  let payload;
  try { payload = await response.json(); }
  catch { throw new Error("Unexpected server response."); }
  if (!response.ok) {
    const detail = payload.detail;
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail || "Request failed"));
  }
  return payload;
}
const asMoney = (num) => Number.isFinite(Number(num)) ? Number(num).toLocaleString("en-IN", {maximumFractionDigits: 2, minimumFractionDigits: 2}) : "—";
const setNote = (id, text) => {$(id).textContent = text;};
const make = (tag, cls, text) => {const e = document.createElement(tag);if(cls)e.className=cls;if(text!==undefined)e.textContent=text;return e;};

async function loadMarket() {
  if (marketBusy) return;
  marketBusy = true;
  try {
    const result = await api("/api/assistant/indices");
    const cards = $("marketGrid"); cards.replaceChildren();
    for (const name of ["NIFTY 50", "BANK NIFTY", "SENSEX"]) {
      const q = (result.indices || []).find(i => i.name === name);
      const card = make("article", "status-card");
      card.append(make("span", "", name));
      const value = make("strong", "", q ? asMoney(q.ltp) : "—");
      card.append(value);
      if (q) {
        const change = make("small", q.change >= 0 ? "up" : "down", (q.change >= 0 ? "+" : "") + asMoney(q.change) + " (" + (q.change_percent >= 0 ? "+" : "") + q.change_percent.toFixed(2) + "%)");
        card.append(change);
        if (q.session_open !== undefined) {
          card.append(make("small", "", "Today O/H/L: " + asMoney(q.session_open) + " / " + asMoney(q.session_high) + " / " + asMoney(q.session_low)));
          card.append(make("small", q.from_open_percent >= 0 ? "up" : "down", "From open: " + (q.from_open_percent >= 0 ? "+" : "") + q.from_open_percent.toFixed(2) + "%"));
        }
        const stamp = make("small", "", (q.live ? "LIVE" : "LAST QUOTE") + (q.quote_time ? " · " + new Date(q.quote_time).toLocaleTimeString("en-IN", {timeZone: "Asia/Kolkata"}) + " IST" : " · Timestamp unavailable"));
        card.append(stamp);
      } else card.append(make("small", "", result.connected ? "Quote unavailable" : "Upstox not connected"));
      cards.append(card);
    }
    setNote("marketNote", result.notice + " Snapshot requested " + new Date(result.as_of).toLocaleTimeString("en-IN", {timeZone: "Asia/Kolkata"}) + " IST.");
  } catch (error) {setNote("marketNote", error.message);}
  finally {marketBusy = false;}
}

function inputCell(tr, index, field, value, type = "text") {
  const cell = document.createElement("td");
  const el = document.createElement(field === "side" ? "select" : "input");
  if (field === "side") {
    for (const optionValue of ["BUY", "SELL"]) {
      const option = make("option", "", optionValue); option.value = optionValue; el.append(option);
    }
    el.value = value || "BUY";
  } else {
    el.type = type; el.value = value ?? "";
    if (type === "number") {el.step = field === "quantity" ? "1" : "0.01"; el.min = field === "quantity" ? "1" : "0.01";}
  }
  el.setAttribute("aria-label", field + " of position " + (index+1));
  el.addEventListener("input", () => { positions[index][field] = el.value; });
  el.addEventListener("change", () => { positions[index][field] = el.value; });
  cell.append(el); tr.append(cell);
}
function drawPositions() {
  const body = $("positionRows"); body.replaceChildren();
  positions.forEach((p, index) => {
    const tr = document.createElement("tr");
    inputCell(tr, index, "symbol", p.symbol);
    inputCell(tr, index, "contract", p.contract);
    inputCell(tr, index, "side", p.side);
    inputCell(tr, index, "quantity", p.quantity, "number");
    inputCell(tr, index, "average_price", p.average_price, "number");
    inputCell(tr, index, "ltp", p.ltp, "number");
    inputCell(tr, index, "expiry", p.expiry);
    const cell = document.createElement("td");
    const remove = make("button", "button secondary small", "Remove");remove.type="button";
    remove.addEventListener("click", () => {positions.splice(index, 1);drawPositions();});
    cell.append(remove);tr.append(cell);body.append(tr);
  });
  if (!positions.length) {
    const tr = document.createElement("tr");const td=make("td","muted","No imported positions yet.");td.colSpan=8;tr.append(td);body.append(tr);
  }
}
async function loadPositions() {
  try {const result = await api("/api/assistant/positions");positions = result.positions || [];drawPositions();refreshPositionQuotes();}
  catch (error) {setNote("positionsNote", error.message);}
}

$("uploadForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const file = $("screenshot").files[0];if (!file) return;
  if (file.size > 5*1024*1024) {setNote("positionsNote", "Maximum screenshot size is 5 MB.");return;}
  const button = $("extractButton");button.disabled = true;
  setNote("positionsNote", "Reading screenshot through your configured AI provider…");
  try {
    const form = new FormData();form.append("file", file);
    const result = await api("/api/assistant/extract", {method: "POST", headers: {"X-M3-Action":"local"}, body: form});
    positions = result.positions || [];
    drawPositions();
    setNote("positionsNote", positions.length + " rows extracted; " + result.rejected + " skipped. Not saved yet. Verify every row and click Confirm & save.");
  } catch (error) {setNote("positionsNote", error.message);}
  finally {button.disabled = false;}
});
$("addPosition").addEventListener("click", () => {
  positions.push({symbol:"",contract:"",side:"BUY",quantity:1,average_price:"",ltp:"",expiry:""});
  drawPositions();
});
$("savePositions").addEventListener("click", async () => {
  try {
    const data = positions.map(p => ({
      symbol: String(p.symbol || "").trim(), contract: String(p.contract || ""),
      side: String(p.side || "BUY"), quantity: Number(p.quantity),
      average_price: Number(p.average_price),
      ltp: p.ltp === "" || p.ltp == null ? null : Number(p.ltp),
      expiry: String(p.expiry || "")
    }));
    if (data.some(p => !p.symbol || !Number.isInteger(p.quantity) || !Number.isFinite(p.average_price) || p.average_price <= 0)) throw new Error("Check symbol, total units and average premium for every row.");
    const r = await api("/api/assistant/positions", {method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({positions:data})});
    positions = r.positions || [];drawPositions();refreshPositionQuotes();setNote("positionsNote", positions.length + " positions saved in local SQLite. These are NOT broker orders.");
  } catch(error){setNote("positionsNote", error.message);}
});
$("clearPositions").addEventListener("click", async () => {
  if (!confirm("Clear your imported position notebook? This does not affect broker or paper positions.")) return;
  try {
    await api("/api/assistant/positions", {method:"DELETE",headers:{"X-M3-Action":"local"}});
    positions=[];drawPositions();refreshPositionQuotes();setNote("positionsNote","Imported positions cleared locally.");
  }catch(error){setNote("positionsNote",error.message);}
});

async function refreshPositionQuotes() {
  try {
    const symbols = ($("extraSymbols")?.value || "").trim();
    const data = await api("/api/assistant/quotes" + (symbols ? "?symbols=" + encodeURIComponent(symbols) : ""));
    const target = $("positionQuotes"); target.replaceChildren();
    if (!data.connected) {
      target.append(make("p", "muted", "Upstox disconnected. Connect on Stock alerts."));
      return;
    }
    const quotes = (data.prices || []).filter(q => q.instrument_kind !== "UNDERLYING");
    if (!quotes.length && !data.unavailable?.length) {
      target.append(make("p", "muted", "No saved positions. Add one or enter an NSE symbol."));
    }
    for (const q of quotes) {
      const line = make("div", "quote-line");
      const title = (q.trading_symbol || q.symbol) + " · ₹" + asMoney(q.ltp);
      const details = q.fresh ? "Recent last trade" : q.quote_label;
      line.append(make("strong", "", title));
      line.append(make("small", q.fresh ? "up" : "muted",
        details + (q.change_percent == null ? "" : " · " +
        (q.change_percent >= 0 ? "+" : "") + q.change_percent + "% vs prev close")));
      if (q.instrument_kind === "OPTION") {
        line.append(make("small", "muted", "Matched " + q.instrument_key +
          (q.indicative_pnl == null ? "" : " · LTP P&L approx ₹" + asMoney(q.indicative_pnl))));
      }
      target.append(line);
    }
    for (const q of (data.unavailable || [])) {
      const line = make("div", "quote-line unavailable");
      line.append(make("strong", "", q.symbol + " · " + (q.contract || "Equity")));
      line.append(make("small", "muted", q.reason || "Price unavailable"));
      target.append(line);
    }
    setNote("chatStatus", (data.notice || "") + " Retrieved " +
      new Date(data.as_of).toLocaleTimeString("en-IN", {timeZone:"Asia/Kolkata"}) + " IST.");
  } catch (error) {
    setNote("chatStatus", "Upstox quote lookup: " + error.message);
  }
}
function bubble(role, content) {
  const log = $("chatLog");
  if (!history.length && log.querySelector(".muted")) log.replaceChildren();
  const text = make("div", "chat-bubble " + role, content);
  log.append(text);log.scrollTop = log.scrollHeight;
}
function pendingSymbols() {
  return ($("extraSymbols").value || "").split(",")
    .map(item => item.trim().toUpperCase().replace(/^@/,""))
    .filter(item => /^[A-Z][A-Z0-9&.\-]{0,28}$/.test(item)).slice(0,8);
}
async function sendQuestion(question) {
  const image = $("chatImage").files[0];
  if (chatBusy || (!question.trim() && !image)) return;
  if (image && image.size > 5*1024*1024) {
    setNote("chatStatus","Image size must not exceed 5 MB.");return;
  }
  chatBusy = true;$("sendQuestion").disabled = true;
  const previous = history.slice(-10);
  const message = question.trim() || "Analyse this attached image.";
  bubble("user", message + (image ? "\n📎 " + image.name : ""));
  setNote("chatStatus", image ? "Reading attached screenshot and Upstox quotes…" :
    "Checking live broker quotes and saved positions…");
  try {
    let result;
    if (image) {
      const form = new FormData();
      form.append("file", image);
      form.append("message", message);
      form.append("history", JSON.stringify(previous));
      form.append("symbols", JSON.stringify(pendingSymbols()));
      form.append("session_positions", JSON.stringify(sessionPositions));
      result = await api("/api/assistant/chat-image", {
        method: "POST",headers: {"X-M3-Action":"local"},body:form
      });
      if (result.recognized_positions?.length) {
        lastImagePositions = result.recognized_positions;
        sessionPositions = result.recognized_positions;
        $("imagePositionsReview").hidden = false;
        setNote("imagePositionsNote", result.recognized_positions.length +
          " positions were read from the image for this chat. Not saved; review before importing.");
      } else {
        $("imagePositionsReview").hidden = true;
      }
    } else {
      result = await api("/api/assistant/chat", {
        method:"POST",headers:{"Content-Type":"application/json"},
        body:JSON.stringify({message,history:previous,symbols:pendingSymbols(),
          session_positions:sessionPositions})
      });
    }
    history.push({role:"user",content: message + (image ? " [image attached]" : "")});
    bubble("assistant", result.answer);
    history.push({role:"assistant",content:result.answer});
    setNote("chatStatus", "Market checked " +
      new Date(result.market_as_of).toLocaleString("en-IN",{timeZone:"Asia/Kolkata"}) +
      " IST. Check contract matching and last-trade timestamps before trading.");
    $("question").value = "";
    $("chatImage").value = "";
    $("chatImagePreview").textContent = "No image attached.";
    $("removeChatImage").hidden = true;
    refreshPositionQuotes();
  } catch(error) {
    bubble("assistant", "Unable to answer: " + error.message);
    setNote("chatStatus", error.message);
  } finally {chatBusy=false;$("sendQuestion").disabled=false;}
}
$("chatForm").addEventListener("submit", e => {
  e.preventDefault();sendQuestion($("question").value);
});
$("chatImage").addEventListener("change", () => {
  const file = $("chatImage").files[0];
  setNote("chatImagePreview", file ?
    "Attached: " + file.name + " · " + (file.size / 1024).toFixed(0) + " KB" :
    "No image attached.");
  $("removeChatImage").hidden = !file;
});
$("removeChatImage").addEventListener("click", () => {
  $("chatImage").value = "";
  $("removeChatImage").hidden = true;
  setNote("chatImagePreview","No image attached.");
});
$("useChatPositions").addEventListener("click",() => {
  positions = lastImagePositions.map(p => ({...p}));
  drawPositions();
  setNote("positionsNote", "Image positions added to editor. Verify all rows, then save explicitly.");
  $("positionRows").scrollIntoView({behavior:"smooth",block:"center"});
});
document.querySelectorAll("[data-question]").forEach(button =>
  button.addEventListener("click", () => sendQuestion(button.dataset.question)));
$("refreshMarket").addEventListener("click",loadMarket);
$("refreshPositionsQuotes").addEventListener("click",refreshPositionQuotes);
$("extraSymbols").addEventListener("change",refreshPositionQuotes);
drawPositions();loadPositions();loadMarket();setInterval(loadMarket,15000);
