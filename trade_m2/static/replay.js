const $=id=>document.getElementById(id);
const esc=x=>String(x??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const money=x=>x==null?'—':Number(x).toLocaleString('en-IN',{style:'currency',currency:'INR'});
const local=x=>x?new Date(x).toLocaleString('en-IN',{timeZone:'Asia/Kolkata',day:'2-digit',month:'short',hour:'2-digit',minute:'2-digit',second:'2-digit'}):'—';
let result=null;
let pollingId=null;

function paint(data){
  result=data;
  $('results').hidden=false;
  const counted=Object.entries(data.counts).map(([k,v])=>`${v} ${k}`).join(' · ');
  $('summary').textContent=`${data.processed}/${data.total} alerts · ${counted} · realised scenario P&L ${money(data.total_pnl)}. Only completed exits count toward P&L.`;
  $('rows').innerHTML=data.rows.map(r=>`<tr><td>${esc(local(r.alert_time))}<small>#${esc(r.id)}</small></td><td>${esc(r.symbol)}<small>${esc(r.side)}</small></td><td>${r.contract?esc(r.contract):'—'}<small>${r.strike?`${esc(r.strike)} ${esc(r.kind)} · lot ${esc(r.lot_size)}${r.strike_rank>1?` · fallback #${esc(r.strike_rank)}`:''}`:'—'}</small></td><td>${esc(local(r.entry_time))}<small>${money(r.entry_price)}</small></td><td>${esc(r.lots??'—')}<small>${money(r.cost)}</small></td><td>${esc(local(r.exit_time))}<small>${money(r.exit_price)}</small></td><td>${esc(r.status)}${r.uncertain?'<small>UNCERTAIN</small>':''}</td><td>${money(r.pnl)}</td><td class="reason">${esc(r.reason)}</td></tr>`).join('');
  $('download').hidden=data.state!=='DONE';
}

async function poll(id){
  pollingId=id;
  $('run').disabled=true;
  for(;;){
    const response=await fetch('/api/replay/'+encodeURIComponent(id));
    if(!response.ok)throw new Error('Could not read replay progress.');
    const data=await response.json();
    paint(data);
    $('status').textContent=`${data.state}: ${data.processed}/${data.total} alerts processed.`;
    if(data.state!=='RUNNING'){
      pollingId=null;
      $('run').disabled=false;
      return data;
    }
    await new Promise(resolve=>setTimeout(resolve,1000));
  }
}

async function restoreCurrent(){
  $('run').disabled=true;
  try{
    const response=await fetch('/api/replay/current',{cache:'no-store'});
    if(response.status===404){
      $('run').disabled=false;
      return false;
    }
    if(!response.ok)throw new Error('Could not restore replay progress.');
    const data=await response.json();
    paint(data);
    if(data.state==='RUNNING'){
      $('status').textContent=`Restored running replay: ${data.processed}/${data.total} alerts processed.`;
      await poll(data.id);
    }else{
      $('status').textContent=`Restored last replay: ${data.state} · ${data.processed}/${data.total} alerts processed.`;
      $('run').disabled=false;
    }
    return true;
  }catch(err){
    $('status').textContent=err.message;
    $('run').disabled=false;
    return false;
  }
}

$('replayForm').addEventListener('submit',async e=>{
  e.preventDefault();
  if(pollingId)return;
  $('run').disabled=true;
  $('status').textContent='Reading CSV and starting replay…';
  try{
    const file=$('file').files[0];
    if(!file||file.size>2000000)throw new Error('Choose a CSV under 2 MB.');
    const csv_text=await file.text();
    const payload={
      csv_text,
      expiry:$('expiry').value,
      target_percent:$('target').value,
      stop_percent:$('stop').value,
      budget:$('budget').value
    };
    const response=await fetch('/api/replay',{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify(payload)
    });
    const body=await response.json();
    if(!response.ok){
      if(body.detail==='A replay is already running.'){
        const restored=await restoreCurrent();
        if(restored)return;
      }
      throw new Error(typeof body.detail==='string'?body.detail:JSON.stringify(body.detail));
    }
    await poll(body.id);
  }catch(err){
    $('status').textContent=err.message;
  }finally{
    if(!pollingId)$('run').disabled=false;
  }
});

$('download').addEventListener('click',()=>{
  if(!result)return;
  const keys=['row','id','symbol','side','alert_time','spot','contract','strike','kind','lot_size','strike_rank','contracts_tested','selection_reason','history_gaps','status','entry_time','entry_price','lots','quantity','cost','target_price','stop_price','exit_time','exit_price','pnl','uncertain','reason'];
  const cell=value=>{
    let s=String(value??'');
    if(/^[=+@\-\t\r]/.test(s)&&!/^\-?\d+(\.\d+)?$/.test(s))s="'"+s;
    return '"'+s.replaceAll('"','""')+'"';
  };
  const csv='\ufeff'+[keys.join(','),...result.rows.map(row=>keys.map(k=>cell(row[k])).join(','))].join('\r\n');
  const a=document.createElement('a');
  a.href=URL.createObjectURL(new Blob([csv],{type:'text/csv;charset=utf-8'}));
  a.download=`trade_m3_replay_${result.expiry}.csv`;
  a.click();
  setTimeout(()=>URL.revokeObjectURL(a.href),1000);
});

restoreCurrent();
