"use strict";
const $ = (id) => document.getElementById(id);
let positions = [];
let history = [];
let marketBusy = false;
let chatBusy = false;

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
  try {const result = await api("/api/assistant/positions");positions = result.positions || [];drawPositions();}
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
    positions = r.positions || [];drawPositions();setNote("positionsNote", positions.length + " positions saved in local SQLite. These are NOT broker orders.");
  } catch(error){setNote("positionsNote", error.message);}
});
$("clearPositions").addEventListener("click", async () => {
  if (!confirm("Clear your imported position notebook? This does not affect broker or paper positions.")) return;
  try {
    await api("/api/assistant/positions", {method:"DELETE",headers:{"X-M3-Action":"local"}});
    positions=[];drawPositions();setNote("positionsNote","Imported positions cleared locally.");
  }catch(error){setNote("positionsNote",error.message);}
});

function bubble(role, text) {
  const log = $("chatLog");if(!history.length && log.querySelector(".muted"))log.replaceChildren();
  log.append(make("div", "chat-bubble " + role, text));log.scrollTop = log.scrollHeight;
}
async function sendQuestion(question) {
  if (chatBusy || !question.trim()) return;
  chatBusy = true;$("sendQuestion").disabled = true;
  const previous = history.slice(-10);
  bubble("user", question);history.push({role:"user",content:question});
  setNote("chatStatus", "Analysing stored positions and current Upstox index snapshot…");
  try {
    const result = await api("/api/assistant/chat", {method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({message:question,history:previous})});
    bubble("assistant", result.answer);history.push({role:"assistant",content:result.answer});
    setNote("chatStatus", "Market snapshot: " + new Date(result.market_as_of).toLocaleString("en-IN", {timeZone:"Asia/Kolkata"}) + " IST · Cross-check all trading decisions.");
    loadMarket();
  } catch (error) {bubble("assistant", "Unable to answer: " + error.message);setNote("chatStatus",error.message);}
  finally {chatBusy=false;$("sendQuestion").disabled=false;}
}
$("chatForm").addEventListener("submit", e => {e.preventDefault();const question=$("question").value.trim();if(!question)return;$("question").value="";sendQuestion(question);});
document.querySelectorAll("[data-question]").forEach(button => button.addEventListener("click", () => sendQuestion(button.dataset.question)));
$("refreshMarket").addEventListener("click",loadMarket);
drawPositions();loadPositions();loadMarket();setInterval(loadMarket,15000);
