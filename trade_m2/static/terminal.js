const $ = id => document.getElementById(id);
const esc = value => String(value ?? '').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const money = value => value === null || value === undefined ? 'Unavailable' : Number(value).toLocaleString('en-IN',{style:'currency',currency:'INR',maximumFractionDigits:2});
const at = value => value ? new Date(value).toLocaleString('en-IN',{timeZone:'Asia/Kolkata',month:'short',day:'numeric',hour:'2-digit',minute:'2-digit',second:'2-digit'}) : '—';
const primary = {initial_cash:['Starting cash ₹',1000,100000000,1000],budget:['Budget / trade ₹',1,10000000,100],lots:['Lots (0 = fit budget)',0,100,1],target_percent:['Target %',.01,100,.1],stop_percent:['Stop %',.01,99,.1],hold_minutes:['Max hold · minutes',1,360,1]};
const advanced = {quote_ttl:['Fresh quote TTL · seconds',1,15,1],min_expiry_days:['Minimum expiry · days',0,30,1]};
let data, tab='open', initialized=false, ticketId=null, busy=false;
async function api(path,method='GET',body){const r=await fetch(path,{method,headers:{'Content-Type':'application/json'},...(body===undefined?{}:{body:JSON.stringify(body)})});const v=await r.json();if(!r.ok)throw new Error(typeof v.detail==='string'?v.detail:'Invalid settings or request. Check input ranges.');return v;}
function fields(defs,prefix){return Object.entries(defs).map(([key,v])=>`<label for="${prefix+key}">${esc(v[0])}<input required type="number" id="${prefix+key}" min="${v[1]}" max="${v[2]}" step="${v[3]}"></label>`).join('');}
$('primarySettings').innerHTML=fields(primary,'s_');$('advancedSettings').innerHTML=fields(advanced,'s_');
function loadSettings(s){for(const key of Object.keys({...primary,...advanced}))$('s_'+key).value=s[key];$('auto').checked=s.auto;}
function notice(message,error=false){$('notice').textContent=message;$('notice').classList.toggle('error',error);}
async function refresh(){
  try {
    data=await api('/api/paper');
    if(!initialized){
      loadSettings(data.settings);initialized=true;
      const id=Number(new URLSearchParams(location.search).get('alert'));
      if(id)openTicket(id);
    }
    $('cash').textContent=money(data.account.available);
    $('reserved').textContent=`Reserved ${money(data.account.reserved)} · Cash ${money(data.account.cash)}`;
    for(const [id,key] of [['realised','realised'],['unrealised','unrealised'],['total','total_pnl']]){
      const el=$(id),v=data.account[key];
      el.textContent=money(v);
      el.className=v===null?'uncertain':Number(v)<0?'negative':'positive';
    }
    $('quality').textContent=`${data.account.clean_wins}/${data.account.clean_closed} clean wins · ${data.account.uncertain_count} uncertain`;
    $('mode').textContent=data.settings.auto?'AUTO PAPER':'MANUAL PAPER';
    const feed=data.feed||{};
    const disconnected=!data.market_data_connected;
    $('retryFeed').hidden=!data.authenticated||!disconnected;
    const feedText=!data.authenticated?'Connect Upstox from Stock alerts. '
      :disconnected?`Market feed disconnected (${feed.running?'reconnecting':'stopped'}; ${feed.active_subscriptions||0} live, ${feed.recovering_subscriptions||0} recovering). ${feed.last_error||'No connection error reported yet.'} Retry the feed or reconnect Upstox from Stock alerts. `
      :feed.stale?`Market feed stale; check Upstox connection and live subscriptions. `
      :!feed.active_subscriptions?`No live stock subscriptions. Load Nifty 200 from Stock alerts. `:'';
    notice(`${feedText}${data.error||''}${data.running?'Paper engine running':'Paper engine stopped'} · ${data.settings.auto?'Auto paper enabled for new eligible live alerts.':'Enable auto and save defaults to trade future alerts.'} Daily P&L ${money(data.account.daily_pnl)} · ${at(data.as_of)} IST`,!!data.error||disconnected||!!feed.stale);
    renderSignals();renderBook();
  }catch(e){notice(e.message,true);}
}
$('retryFeed').onclick=async()=>{
  $('retryFeed').disabled=true;
  try{await api('/api/feed/reconnect','POST',{});notice('Reconnecting market feed with the current Upstox login…');await refresh();}
  catch(e){notice(e.message,true);}
  finally{$('retryFeed').disabled=false;}
};
function renderSignals(){const today=new Date().toLocaleDateString('en-CA',{timeZone:'Asia/Kolkata'});const current=data.decisions.filter(d=>new Date(d.detected_at||d.event_time).toLocaleDateString('en-CA',{timeZone:'Asia/Kolkata'})===today&&d.source!=='RECOVERY');$('signals').innerHTML=current.slice(0,12).map(d=>{const c=d.candidate;const trade=data.trades.find(t=>t.event_id===d.event_id);const fresh=d.status==='READY'&&data.authenticated&&data.entries_open;const reason=d.reason||'No option quote or contract available.';const label=trade?esc(trade.state):d.status!=='READY'?'Option unavailable':!data.entries_open?'Entry window closed':fresh?'Paper trade':'Connect Upstox';return `<article class="signal-card"><h3>${esc(d.symbol)} · ${esc(d.trade_side)} <span class="muted">#${d.event_id}</span></h3><p>${c?`BUY ${esc(c.strike)} ${esc(c.kind)} · ${esc(c.expiry)} · ${c.lot_size} / lot<br>Next-minute entry pending · sizing LTP ${money(c.ltp)} · Bid ${money(c.bid)} / Ask ${money(c.ask)} · Δ ${esc(c.delta)}<br>Chain quote ${at(d.quote_at)} IST<br>${esc(c.reason)}`:esc(reason)}<br>${esc(d.sector_context?.summary||'Sector unavailable')}<br>Alert ${at(d.detected_at||d.event_time)} IST${d.auto_result?`<br>Auto: ${esc(d.auto_result)}`:''}</p><button class="button secondary" data-ticket="${d.event_id}" ${!fresh||trade?'disabled':''}>${label}</button></article>`;}).join('')||'<p class="muted">Waiting for a new live alert. Connect Upstox on Stock alerts. Older alerts remain in Alerts history and are not replayed.</p>';}
function renderBook(){let heads,rows;if(tab==='orders'){heads=['Order','Contract','Type','Status','Filled / qty','Price / level','Created IST'];rows=data.orders.map(o=>[o.id,esc(o.symbol),esc(o.kind),esc(o.state),`${o.filled_quantity}/${o.quantity}`,money(o.price),at(o.created_at)]);}else if(tab==='fills'){heads=['Fill','Trade','Side','Quantity','Fill price','Estimated fee','Time IST'];rows=data.fills.map(f=>[f.id,f.trade_id,f.side,f.quantity,money(f.price),money(f.fee),at(f.at)]);}else{heads=['Trade / contract','State','Qty / remaining','Entry / mark','Target / stop','P&L & costs','Actions'];const ts=data.trades.filter(t=>tab==='pending'?t.state==='PENDING':tab==='open'?t.remaining>0:t.state!=='PENDING'&&!t.remaining);rows=ts.map(t=>{const stale=!t.last_quote_at||Date.now()-new Date(t.last_quote_at).getTime()>t.settings.quote_ttl*1000;const detail=[t.state==='PENDING'?`Virtual entry scheduled ${at(t.entry_at)} IST`:t.exit_reason,t.reason,t.unresolved_quantity?`Unresolved quantity: ${t.unresolved_quantity}`:''].filter(Boolean).map(esc).join('<br>');return [`#${t.id} · ${esc(t.candidate.symbol)}<br><small>${at(t.created_at)} IST · ${t.mode}</small>`,`${esc(t.state)}${t.uncertain?'<br><span class="uncertain">UNCERTAIN</span>':''}${detail?`<br><small>${detail}</small>`:''}`,`${t.quantity} / ${t.remaining}`,`${t.state==='PENDING'?'Awaiting next-minute open':money(t.entry)}<br><small>Mark ${stale?'unavailable':money(t.mark)}</small>`,`${money(t.target)} / ${money(t.stop)}<br><small>+${t.settings.target_percent}% / −${t.settings.stop_percent}%</small>`,`${money(t.realised)} realised<br><small>Costs ${money(t.fees)}</small>`,t.state==='PENDING'?`<button class="button small secondary" data-action="cancel" data-id="${t.id}">Cancel</button>`:t.remaining?`<button class="button small secondary" data-action="close" data-id="${t.id}">Close paper</button>`:'—'];});}$('bookHead').innerHTML=`<tr>${heads.map(x=>`<th>${x}</th>`).join('')}</tr>`;$('bookBody').innerHTML=rows.length?rows.map(r=>`<tr>${r.map(x=>`<td>${x}</td>`).join('')}</tr>`).join(''):`<tr><td colspan="${heads.length}" class="empty-state">No ${tab==='open'?'open positions':tab} yet.</td></tr>`;}
async function openTicket(id){
  ticketId=id;
  $('ticketTitle').textContent=`Paper option for alert #${id}`;
  $('ticketDetail').textContent='Fetching a current option quote…';
  const defs={budget:primary.budget,lots:primary.lots,target_percent:primary.target_percent,stop_percent:primary.stop_percent,hold_minutes:primary.hold_minutes};
  $('ticketFields').innerHTML=fields(defs,'t_');
  for(const key of Object.keys(defs))$('t_'+key).value=data.settings[key];
  $('ticketError').textContent='';
  $('ticketEstimate').textContent='';
  $('ticketForm').querySelector('[type=submit]').disabled=true;
  if(!$('ticketDialog').open)$('ticketDialog').showModal();
  await previewTicket();
}
async function previewTicket(){
  if(ticketId===null)return;
  try{
    const d=await api(`/api/paper/alerts/${ticketId}/preview`,'POST',{budget:Number($('t_budget').value),lots:Number($('t_lots').value)});
    data.decisions=[d,...data.decisions.filter(x=>x.event_id!==ticketId)];
    const c=d.candidate;
    $('ticketTitle').textContent=`BUY ${c.symbol}`;
    $('ticketDetail').textContent=`${c.lot_size} units / lot · Next-minute open entry (chain LTP ${money(c.ltp)} is sizing estimate) · Bid ${money(c.bid)} / Ask ${money(c.ask)} · Δ ${c.delta} · ${c.reason}`;
    $('ticketError').textContent='';
    $('ticketForm').querySelector('[type=submit]').disabled=false;
    estimate();
  }catch(e){
    $('ticketDetail').textContent='No current executable option preview.';
    $('ticketError').textContent=e.message;
    $('ticketForm').querySelector('[type=submit]').disabled=true;
  }
}
function estimate(){
  if(ticketId===null)return;
  const c=data.decisions.find(d=>d.event_id===ticketId)?.candidate;
  if(!c)return;
  const b=Number($('t_budget').value),fixed=Number($('t_lots').value);
  const unit=Number(c.ltp)*c.lot_size;
  const lots=fixed||Math.max(0,Math.floor(b/unit));
  const detected=data.decisions.find(d=>d.event_id===ticketId)?.detected_at;
  const next=detected?new Date(Math.floor(new Date(detected).getTime()/60000)*60000+60000):null;
  $('ticketEstimate').textContent=`Estimated ${lots} lot(s), ${lots*c.lot_size} units using current LTP. Entry waits for the first option trade at or after ${next?at(next.toISOString()):'the next minute'}; actual price can change and must fit budget. No broker order is sent.`;
}
$('ticketFields').addEventListener('input',estimate);
$('ticketCancel').onclick=()=>$('ticketDialog').close();
$('retryPreview').onclick=previewTicket;
$('ticketForm').onsubmit=async e=>{
  e.preventDefault();if(busy)return;busy=true;
  const submit=$('ticketForm').querySelector('[type=submit]');submit.disabled=true;
  try{
    const body={};for(const k of ['budget','lots','target_percent','stop_percent','hold_minutes'])body[k]=Number($('t_'+k).value);
    await previewTicket();
    if(submit.disabled)throw new Error($('ticketError').textContent);
    await api(`/api/paper/alerts/${ticketId}`,'POST',body);
    $('ticketDialog').close();await refresh();
  }catch(e){$('ticketError').textContent=e.message;}
  finally{busy=false;submit.disabled=false;}
};
$('settingsForm').onsubmit=async e=>{e.preventDefault();try{const s={...data.settings};for(const k of Object.keys({...primary,...advanced}))s[k]=Number($('s_'+k).value);Object.assign(s,{auto:$('auto').checked,aligned_only:false,latency_ms:0,slippage_percent:0,fee_fixed:0,fee_percent:0});await api('/api/paper/settings','PUT',s);$('settingsStatus').textContent='Saved. Applies to future orders; existing trades keep their settings.';await refresh();}catch(e){$('settingsStatus').textContent=e.message;}};
$('pauseAuto').onclick=async()=>{try{await api('/api/paper/settings','PUT',{...data.settings,auto:false});$('auto').checked=false;await refresh();}catch(e){notice(e.message,true);}};
document.addEventListener('click',async e=>{const ticket=e.target.closest('[data-ticket]');if(ticket)openTicket(Number(ticket.dataset.ticket));const action=e.target.closest('[data-action]');if(action){action.disabled=true;try{await api(`/api/paper/trades/${action.dataset.id}/${action.dataset.action}`,'POST',{});await refresh();}catch(e){notice(e.message,true);action.disabled=false;}}const b=e.target.closest('[data-tab]');if(b){tab=b.dataset.tab;document.querySelectorAll('[data-tab]').forEach(x=>x.classList.toggle('selected',x===b));renderBook();}});
refresh();setInterval(refresh,2000);
