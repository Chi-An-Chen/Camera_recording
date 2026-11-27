'use strict';
const $ = s => document.querySelector(s);
const by = (root, s) => root.querySelector(s);
const rows = new Map();
const previews = new Map();
const MAX_LIVE = 4;
const MS = window.MediaSource || window.ManagedMediaSource;
const useMse = !!(MS && window.ReadableStream && MS.isTypeSupported('video/mp4; codecs="avc1.4D0029"'));
let view, latest, received = 0, offline = true, setupTimer, expiryTimer, inFlight;
let busy = false, pendingConfirm, toastTimer;
let archiveVersion = 0, filesVersion = 0, inventory, files = [], selectedClip;
let managing = false, archiveReady = false;
const deleting = new Set();

const healthLabels = {
  writing:['寫入中','good'], confirming:['確認寫入中','neutral'],
  pending_write:['等待寫入更新','warn'], closing:['正在收尾','warn'],
  retrying:['連線異常・重試中','bad'], connecting:['連線中','neutral'],
  waiting:['等待排程','neutral'], paused:['已暫停','neutral'],
  stopped:['未啟用','neutral'], unknown:['狀態未確認','warn']
};
const reasons = {
  open:'仍在使用或收尾', incomplete:'片段尚未完整', unverified:'無法確認安全',
  changed:'檔案已變更', missing:'檔案已不存在', invalid:'檔案無效', io_error:'檔案讀寫失敗'
};

function element(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined) node.textContent = text;
  return node;
}
function button(text, cls, action) {
  const node = element('button',cls,text);
  node.type = 'button';
  node.onclick = action;
  return node;
}
function displaySize(n) {
  for (const [unit, divisor] of [['TB',1e12],['GB',1e9],['MB',1e6],['KB',1e3]]) {
    if (n >= divisor) return (n/divisor).toFixed(1)+' '+unit;
  }
  return n+' B';
}
function clock(stamp, date=false) {
  if (!stamp) return '—';
  const iso = new Date((stamp + (latest?.utc_offset || 0))*1000).toISOString();
  return date ? iso.slice(5,10)+' '+iso.slice(11,19) : iso.slice(11,19);
}
function clipTime(f) {
  return f.name.slice(9,15).replace(/(..)(..)(..)/,'$1:$2:$3');
}
function notify(message) {
  const node=$('#toast');
  node.textContent=message; node.hidden=false;
  clearTimeout(toastTimer); toastTimer=setTimeout(()=>node.hidden=true,5000);
}
async function request(path, body, timeout=body ? 60000 : 8000) {
  const controller=new AbortController();
  const timer=setTimeout(()=>controller.abort(),timeout);
  try {
    const options={signal:controller.signal,cache:'no-store'};
    if (body) Object.assign(options,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    const r=await fetch(path,options);
    const data=await r.json().catch(()=>{throw Error('HTTP '+r.status)});
    if (!r.ok || data.error) throw Error(data.error || 'HTTP '+r.status);
    return data;
  } finally {clearTimeout(timer)}
}
function dismiss() {
  const dialog=$('#confirm');
  if (dialog.open) dialog.close();
  pendingConfirm=null;
}
function confirmAction(title, description, label, action) {
  dismiss();
  if (typeof $('#confirm').showModal!=='function') {
    if (window.confirm(title+'\n'+description)) action();
    return;
  }
  $('#confirmTitle').textContent=title;
  $('#confirmDetail').textContent=description;
  $('#proceed').textContent=label;
  pendingConfirm=action;
  $('#confirm').showModal();
  $('#cancel').focus();
}
$('#cancel').onclick=dismiss;
$('#detailsBtn').onclick=()=>{
  const open=$('#metrics').classList.toggle('expanded');
  $('#detailsBtn').setAttribute('aria-expanded',String(open));
};
$('#confirm').addEventListener('cancel',()=>pendingConfirm=null);
$('#proceed').onclick=()=>{
  const action=pendingConfirm;
  dismiss();
  if (action) action();
};

function newRow(ip) {
  const row=element('article','camera-row');
  row.innerHTML='<div class="grid"><div><label class="pick"><input type="checkbox"><strong class="device-ip"></strong></label><span class="sub device-name"></span></div>'+
    '<div class="state-cell"><span class="state"></span><span class="sub intent"></span></div>'+
    '<div class="clock-cell"><span class="clock"></span><span class="sub evidence"></span><span class="sub filename"></span></div>'+
    '<div class="actions"><button class="watch">預覽</button><button class="record"></button></div></div>'+
    '<div class="preview" hidden></div>';
  by(row,'.device-ip').textContent=ip;
  const pick=by(row,'.pick input');
  pick.setAttribute('aria-label','選取 '+ip+' 加入錄影');
  pick.onchange=paintSetup;
  row.setAttribute('aria-label',ip);
  by(row,'.watch').setAttribute('aria-label','預覽 '+ip);
  by(row,'.watch').setAttribute('aria-expanded','false');
  by(row,'.watch').onclick=()=>previews.has(ip) ? stopPreview(ip) : startPreview(ip,row);
  by(row,'.record').onclick=()=>{
    if (row.camera.record) confirmAction('停止這台攝影機錄影？',ip+'\n當前片段會先收尾，既有錄影仍會保留。','停止錄影',()=>changeRecord({ip,record:false}));
    else changeRecord({ip,record:true});
  };
  rows.set(ip,row); $('#deviceList').appendChild(row);
  return row;
}
function paintSetup() {
  clearTimeout(expiryTimer);
  if (!latest || view !== 'setup' || document.hidden) return;
  const age=(performance.now()-received)/1000;
  const now=latest.now+age;
  const stale=offline || age>=15;
  const heartbeat=!stale && latest.rec.running && now-latest.rec.updated<15;
  const deadlines=[15-age];
  if (heartbeat) deadlines.push(15-(now-latest.rec.updated));
  const cameras=latest.cameras, selected=cameras.filter(c=>c.record).length;
  const found=new Set(cameras.map(c=>c.ip));
  for (const [ip,row] of rows) {
    if (!found.has(ip)) {stopPreview(ip); row.remove(); rows.delete(ip)}
  }
  let writing=0, picked=0;
  for (const c of cameras) {
    const row=rows.get(c.ip) || newRow(c.ip), h=c.health;
    row.camera=c;
    const pick=by(row,'.pick input');
    pick.hidden=c.record || !c.can_preview;
    pick.disabled=pick.hidden || busy || stale;
    if (pick.hidden) pick.checked=false;
    if (pick.checked) picked++;
    let state=h.state;
    if (stale || (h.source==='recorder' && !heartbeat)) state='unknown';
    if (state==='writing') {
      const left=15-(now-h.last_write);
      if (left<=0 || !h.last_write) state='pending_write';
      else {deadlines.push(left); if (c.record) writing++}
    }
    const [text,tone]=healthLabels[state] || healthLabels.unknown;
    by(row,'.device-name').textContent=c.name || ({ok:'已辨識串流',auth:'需要攝影機帳密',unknown:'尚未辨識串流',unseen:'尚無掃描結果'}[c.status] || '');
    const label=by(row,'.state');
    label.className='state '+tone; label.textContent=text;
    const intent=by(row,'.intent');
    intent.textContent=c.record ? (latest.paused ? '已選取・全機暫停' : '已啟用錄影') : '未選取錄影';
    intent.hidden=state==='stopped';
    by(row,'.clock').textContent=clock(h.last_write,true);
    const evidence=by(row,'.evidence');
    evidence.textContent=h.last_write ? (h.source==='filesystem' ?
      (heartbeat ? '檔案觀察' : '檔案觀察・未取得服務回報') : '錄影器回報') : '尚無寫入證據';
    const filename=by(row,'.filename');
    filename.hidden=!h.file;
    filename.textContent=h.file ? h.file+' · '+displaySize(h.size || 0) : '';
    const watch=by(row,'.watch');
    watch.disabled=!previews.has(c.ip) && !c.can_preview;
    const record=by(row,'.record');
    record.disabled=busy || stale || (!c.record && !c.can_preview);
    record.textContent=c.record ? '停止錄影' : '啟用錄影';
    record.setAttribute('aria-label',record.textContent+' '+c.ip);
    record.className='record '+(c.record?'':'primary');
  }
  $('#batchStart').textContent='啟用所選（'+picked+'）';
  $('#batchStart').disabled=!picked || busy || stale;
  $('#writeCount').textContent=stale ? '— / '+selected : writing+' / '+selected;
  $('#heartbeat').textContent=heartbeat ? '已收到' : '未取得';
  $('#heartbeat').className=heartbeat ? '' : 'warn';
  $('#heartbeatTime').textContent=latest.rec.updated ? '最後回報 '+clock(latest.rec.updated,true) : '尚無有效心跳';
  $('#space').textContent=displaySize(latest.disk.free);
  $('#spaceBar').value=100*latest.disk.free/(latest.disk.total || 1);
  $('#stale').hidden=!stale && heartbeat;
  $('#stale').textContent=stale ? '狀態資料已過期，正在重新取得。' :
    '未取得錄影服務近期回報，依 MP4 檔案變化確認寫入。';
  $('#deviceEmpty').hidden=!!cameras.length;
  $('#deviceEmpty').textContent='尚未發現攝影機，可重新掃描已連線的網段。';
  $('#scanMeta').textContent=latest.scanning ? '正在掃描已連線的網段…' : [
    (latest.networks || []).join('、'), latest.time ? '上次掃描 '+clock(latest.time,true) : '尚無掃描紀錄',
    latest.scan_error || ''
  ].filter(Boolean).join(' · ');
  $('#scanBtn').disabled=latest.scanning;
  const schedule=latest.rec.schedule;
  $('#schedule').textContent=heartbeat && Array.isArray(schedule) && schedule.length===2 ?
    (schedule[0]===schedule[1] ? '全天' : schedule.join(' – ')) : '尚無回報';
  $('#recAll').textContent=latest.paused ? '恢復全部錄影' : '暫停全部錄影';
  $('#recAll').disabled=busy || stale;
  const next=Math.min(...deadlines.filter(x=>x>0));
  if (Number.isFinite(next)) expiryTimer=setTimeout(paintSetup,next*1000+50);
}
async function syncSetup() {
  clearTimeout(setupTimer);
  if (document.hidden || view!=='setup') return;
  if (inFlight) return inFlight;
  inFlight=(async()=>{
    const started=performance.now();
    try {
      latest=await request('/api/live');
      received=started; offline=false;
    } catch (e) {
      offline=true;
      $('#stale').textContent='無法讀取主機狀態，稍後將自動重試。';
      $('#stale').hidden=false;
      if (!latest) $('#deviceEmpty').textContent='等待主機回應';
    } finally {
      inFlight=null;
      paintSetup();
      if (view==='setup' && !document.hidden) setupTimer=setTimeout(syncSetup,5000);
    }
  })();
  return inFlight;
}
async function changeRecord(body) {
  if (busy) return;
  busy=true; paintSetup(); $('#controls').open=false;
  try {
    const result=await request('/api/record',body);
    notify(body.ips ? '已啟用 '+body.ips.length+' 台。'+(result.config.paused ?
      '全機暫停中，恢復後依排程錄影。' : '依排程錄影，請查看各台寫入狀態。') : '錄影設定已儲存');
    if (inFlight) await inFlight;
    await syncSetup();
  } catch (e) {notify('設定失敗：'+e.message)}
  finally {busy=false; paintSetup()}
}
$('#batchStart').onclick=()=>{
  const ips=[];
  for (const [ip,row] of rows) {
    const pick=by(row,'.pick input');
    if (pick.checked && !pick.disabled) ips.push(ip);
  }
  if (ips.length) changeRecord({ips,record:true});
};
$('#recAll').onclick=()=>{
  if (!latest) return;
  if (latest.paused) changeRecord({paused:false});
  else confirmAction('暫停全部錄影？','將暫停 '+latest.cameras.filter(c=>c.record).length+' 台已啟用的攝影機。','全部暫停',()=>changeRecord({paused:true}));
};
$('#scanBtn').onclick=async()=>{
  $('#scanBtn').disabled=true;
  try {
    const r=await request('/api/scan',{});
    await syncSetup();
    notify(r.started ? '已開始掃描' : (latest?.scan_error || '已有掃描作業'));
  } catch(e) {notify('掃描失敗：'+e.message); $('#scanBtn').disabled=false}
};

// Each preview owns its request, buffers and retry timer. Opening the page creates none.
function stopPreview(ip) {
  const p=previews.get(ip);
  if (!p) return;
  previews.delete(ip);
  p.stop();
  const box=by(p.row,'.preview');
  box.hidden=true; box.textContent='';
  const watch=by(p.row,'.watch');
  watch.textContent='預覽'; watch.setAttribute('aria-expanded','false');
  watch.setAttribute('aria-label','預覽 '+ip);
}
function releasePreviews() {
  for (const ip of previews.keys()) stopPreview(ip);
}
function startPreview(ip,row) {
  if (previews.size>=MAX_LIVE) {
    const oldest=previews.keys().next().value;
    stopPreview(oldest); notify('已關閉 '+oldest+' 的預覽');
  }
  const box=by(row,'.preview'), watch=by(row,'.watch');
  box.hidden=false; watch.textContent='關閉預覽'; watch.setAttribute('aria-expanded','true');
  watch.setAttribute('aria-label','關閉預覽 '+ip);
  const media=element(useMse?'video':'img','media'), msg=element('p','','連線中…');
  media.hidden=true;
  if (useMse) {media.muted=true; media.autoplay=true; media.playsInline=true}
  else media.alt=ip+' 即時預覽';
  box.append(media,msg);
  const full=media.requestFullscreen || media.webkitEnterFullscreen;
  if (full) box.appendChild(button('全螢幕','',()=>full.call(media)));
  let abort, timer, retryTimer, url, generation=0;
  function stop() {
    generation++;
    clearTimeout(retryTimer); clearInterval(timer);
    if (abort) abort.abort();
    media.onload=media.onerror=media.onplaying=null;
    media.removeAttribute('src');
    if (useMse) media.load();
    if (url) {URL.revokeObjectURL(url);url=null}
  }
  function connect() {
    stop();
    const token=generation;
    media.hidden=true; msg.textContent='連線中…';
    function ready() {
      if (token!==generation) return;
      media.hidden=false; msg.textContent=useMse?'即時預覽':'即時預覽 · MJPEG';
    }
    function retry() {
      if (token!==generation) return;
      stop();
      media.hidden=true; msg.textContent='預覽連線中斷，重新連線中…';
      retryTimer=setTimeout(connect,useMse?3000:5000);
    }
    media.onerror=retry;
    if (!useMse) {
      media.onload=ready;
      media.src='/live/'+ip+'?t='+Date.now();
      return;
    }
    media.onplaying=ready;
    const ms=new MS();
    abort=new AbortController();
    const signal=abort.signal;
    let last=performance.now();
    timer=setInterval(()=>{
      if (performance.now()-last>8000) return retry();
      const buf=media.buffered;
      if (!buf.length) return;
      const end=buf.end(buf.length-1);
      if (end-media.currentTime>0.5) media.currentTime=end-0.05;
      if (media.paused) media.play().catch(()=>{});
    },500);
    ms.addEventListener('sourceopen',async()=>{
      if (token!==generation) return;
      try {
        const response=await fetch('/fmp4/'+ip+'?t='+Date.now(),{signal});
        if (!response.ok) throw Error(response.status);
        const reader=response.body.getReader();
        let sb, head=new Uint8Array(0), queue=[];
        function pump() {
          if (token!==generation || !sb || sb.updating || !queue.length) return;
          try {
            const b=media.buffered;
            if (b.length && media.currentTime-b.start(0)>10) {
              sb.remove(b.start(0),media.currentTime-3);
            } else sb.appendBuffer(queue.shift());
          } catch (e) {retry()}
        }
        while (token===generation) {
          const {done,value}=await reader.read();
          if (done || token!==generation) break;
          last=performance.now();
          if (!sb) {
            const combined=new Uint8Array(head.length+value.length);
            combined.set(head); combined.set(value,head.length); head=combined;
            let k=0;
            while (k+8<head.length && !(head[k]===97&&head[k+1]===118&&head[k+2]===99&&head[k+3]===67)) k++;
            if (k+8>=head.length) {
              if (head.length>1048576) throw Error('avcC');
              continue;
            }
            const codec=[5,6,7].map(n=>head[k+n].toString(16).padStart(2,'0')).join('');
            sb=ms.addSourceBuffer('video/mp4; codecs="avc1.'+codec+'"');
            sb.addEventListener('updateend',pump);
            queue.push(head); head=null;
          } else queue.push(value);
          pump();
        }
        retry();
      } catch(e) {retry()}
    },{once:true});
    media.disableRemotePlayback=true;
    url=URL.createObjectURL(ms); media.src=url;
  }
  previews.set(ip,{row,stop});
  connect();
}

function isCurrent(cam) {
  return view==='archive' && $('#camera').value===cam;
}
function closePlayback() {
  const player=$('#player');
  selectedClip=null;
  player.onended=player.onerror=null;
  if (player.hasAttribute('src')) {
    player.pause(); player.removeAttribute('src'); player.load();
  }
  $('#playback').hidden=true;
  $('#download').removeAttribute('href'); $('#delCur').disabled=true;
  const old=$('.file.selected');
  if (old) old.classList.remove('selected');
}
function changeManagement(on) {
  managing=on;
  $('#manage').setAttribute('aria-pressed',String(on));
  $('#manage').textContent=on ? '結束管理' : '管理檔案';
  $('#delCur').hidden=!on;
}
function clearFileList() {
  dismiss(); closePlayback(); changeManagement(false);
  files=[]; archiveReady=false;
  $('#days').replaceChildren(); $('#archiveError').hidden=true;
  $('#range').disabled=$('#manage').disabled=true;
  $('#archiveMeta').textContent='讀取中…';
}
function archiveError(message) {
  $('#archiveError').textContent=message;
  $('#archiveError').hidden=false; $('#archiveMeta').textContent='無法取得清單';
}
function showPath(cam) {
  $('#folder').value=cam && inventory ? inventory.root+'/'+cam : '';
  $('#pathbar').hidden=!cam || !inventory;
}
async function loadArchive() {
  const version=++archiveVersion, camera=$('#camera');
  const oldCam=camera.value, oldRange=$('#range').value;
  filesVersion++; clearFileList(); showPath('');
  camera.disabled=true; camera.replaceChildren();
  try {
    const data=await request('/api/cams'+($('#showAll').checked ? '?all=1' : ''));
    if (view!=='archive' || version!==archiveVersion) return;
    inventory=data;
    for (const c of data.cams) camera.add(new Option(c.ip+(c.record?'':'（已停錄）'),c.dir));
    if (data.cams.some(c=>c.dir===oldCam)) camera.value=oldCam;
    camera.disabled=!data.cams.length;
    await loadFiles(camera.value,oldRange);
  } catch(e) {
    if (view==='archive' && version===archiveVersion) archiveError('無法讀取攝影機清單：'+e.message);
  }
}
async function loadFiles(cam,range='recent') {
  const version=++filesVersion;
  clearFileList(); showPath(cam);
  $('#range').replaceChildren(new Option('最近三天','recent'),new Option('全部日期','all'));
  if (!cam) {
    $('#archiveMeta').textContent='尚無攝影機資料夾';
    return;
  }
  try {
    const data=await request('/api/files/'+encodeURIComponent(cam),null,30000);
    if (version!==filesVersion || !isCurrent(cam)) return;
    files=data; archiveReady=true;
    const days=new Set(files.map(f=>f.day));
    for (const day of days) $('#range').add(new Option(day,day));
    $('#range').value=range==='all' || days.has(range) ? range : 'recent';
    $('#range').disabled=!files.length;
    renderDays();
  } catch(e) {
    if (version===filesVersion && isCurrent(cam)) archiveError('無法讀取片段：'+e.message);
  }
}
function visibleFiles() {
  const range=$('#range').value;
  if (range==='all') return files;
  if (range!=='recent') return files.filter(f=>f.day===range);
  const start=new Date(inventory.today+'T00:00:00Z');
  start.setUTCDate(start.getUTCDate()-2);
  const first=start.toISOString().slice(0,10);
  return files.filter(f=>first<=f.day && f.day<=inventory.today);
}
function renderDays(expanded=new Set()) {
  const cam=$('#camera').value, panel=$('#days'), visible=visibleFiles();
  panel.replaceChildren();
  $('#manage').disabled=!visible.some(f=>f.closed);
  $('#archiveMeta').textContent='已完成 '+visible.filter(f=>f.closed).length+' 段';
  if (!visible.length) {
    const empty=element('div','empty',files.length ? '這個日期範圍沒有片段。' : '這台攝影機尚無片段。');
    if (files.length) empty.appendChild(button('查看全部日期','',()=>{
      $('#range').value='all'; changeRange();
    }));
    panel.appendChild(empty);
    return;
  }
  const days=new Map();
  for (const f of visible) {
    if (!days.has(f.day)) days.set(f.day,[]);
    days.get(f.day).push(f);
  }
  for (const [day,items] of days) {
    const group=element('details','day'), head=element('summary'), content=element('div','day-files');
    group.dataset.day=day;
    const closed=items.filter(f=>f.closed), count=closed.length;
    const total=closed.reduce((sum,f)=>sum+f.size,0);
    const times=count ? ' · 起錄 '+clipTime(closed[count-1])+(count>1 ? ' – '+clipTime(closed[0]) : '') : '';
    head.append(element('strong','',day),element('span','sub','已完成 '+count+' 段 · '+displaySize(total)+times+
      (items.length>count ? ' · '+(items.length-count)+' 段待確認' : '')));
    group.append(head,content);
    function fill() {
      if (!group.open || content.childElementCount) return;
      if (managing && count) {
        const actions=element('div','day-actions');
        actions.appendChild(button('刪除當天已完成片段','danger-text',()=>askDelete(cam,closed,day)));
        content.appendChild(actions);
      }
      for (const f of items) {
        const line=element('div','file'), play=button(clipTime(f),'file-time',()=>playClip(cam,f));
        line.dataset.name=f.name;
        if (selectedClip?.cam===cam && selectedClip.file.identity===f.identity) line.classList.add('selected');
        play.disabled=!f.closed; play.setAttribute('aria-label','播放 '+f.name);
        line.appendChild(play);
        if (!f.closed) line.appendChild(element('span','sub',reasons[f.reason] || '等待確認'));
        line.appendChild(element('span','file-size',displaySize(f.size)));
        if (managing && f.closed) line.appendChild(button('刪除','del danger-text',()=>askDelete(cam,[f])));
        content.appendChild(line);
      }
    }
    group.ontoggle=fill;
    group.open=expanded.has(day) || (days.size===1 && !expanded.size);
    panel.appendChild(group); fill();
  }
}
function changeRange() {
  dismiss(); closePlayback(); changeManagement(false); renderDays();
}
function playClip(cam,file,scroll=true) {
  if (!file.closed || !isCurrent(cam)) return;
  closePlayback();
  const target=Object.freeze({cam,file});
  selectedClip=target;
  const src='/video/'+encodeURIComponent(cam)+'/'+encodeURIComponent(file.name)+'?id='+encodeURIComponent(file.identity);
  const player=$('#player');
  $('#clipTitle').textContent='片段回放';
  $('#clipInfo').textContent=cam.replace(/_/g,'.')+' · '+file.day+' '+clipTime(file)+' · '+displaySize(file.size);
  $('#download').href=src+'&dl=1'; $('#delCur').disabled=false;
  $('#playback').hidden=false;
  for (const line of document.querySelectorAll('.file')) line.classList.toggle('selected',line.dataset.name===file.name);
  player.onerror=()=>{if (selectedClip===target) notify('無法播放這段影片，請重新整理清單。')};
  player.onended=()=>{
    if (selectedClip!==target || !isCurrent(cam) || document.hidden) return;
    const parts=visibleFiles().filter(f=>f.closed);
    const i=parts.indexOf(file);
    if (i>0) playClip(cam,parts[i-1],false);
  };
  player.src=src; player.play().catch(()=>{});
  if (scroll) $('#playback').scrollIntoView({block:'nearest'});
}
async function removeClips(cam,items) {
  if (!isCurrent(cam)) return;
  const expected={}, names=[];
  for (const f of items) {expected[f.name]=f.identity; names.push(f.name)}
  if (deleting.has(cam)) return;
  deleting.add(cam);
  const version=filesVersion;
  try {
    const r=await request('/api/delete',{cam,names,expected});
    const skipped=[...new Set(r.skipped.map(n=>reasons[r.reasons[n]] || '無法確認'))];
    notify(cam.replace(/_/g,'.')+' 已刪除 '+r.deleted.length+' 段'+
      (skipped.length ? '，保留 '+r.skipped.length+' 段（'+skipped.join('、')+'）' : ''));
    if (isCurrent(cam) && version===filesVersion) await loadFiles(cam,$('#range').value);
  } catch(e) {notify('未取得刪除結果，請重新整理清單：'+e.message)}
  finally {deleting.delete(cam)}
}
function askDelete(cam,items,day) {
  if (!managing || !isCurrent(cam) || deleting.has(cam) || !items.length) return;
  const targets=items.slice(), label=day || targets[0].name;
  const total=targets.reduce((sum,f)=>sum+f.size,0);
  confirmAction(day?'刪除當天已完成片段？':'刪除這段錄影？',
    cam.replace(/_/g,'.')+' · '+label+'\n共 '+targets.length+' 段，'+displaySize(total)+'。刪除後無法復原。',
    '確認刪除',()=>removeClips(cam,targets));
}
$('#showAll').onchange=$('#reload').onclick=loadArchive;
$('#camera').onchange=()=>loadFiles($('#camera').value);
$('#range').onchange=changeRange;
$('#manage').onclick=()=>{
  if (!archiveReady) return;
  const expanded=new Set(Array.from(document.querySelectorAll('.day[open]'),n=>n.dataset.day));
  changeManagement(!managing); renderDays(expanded);
};
$('#closePlayer').onclick=closePlayback;
$('#delCur').onclick=()=>{if (selectedClip) askDelete(selectedClip.cam,[selectedClip.file])};
$('#copy').onclick=async()=>{
  const input=$('#folder');
  try {
    if (navigator.clipboard && window.isSecureContext) await navigator.clipboard.writeText(input.value);
    else {input.select(); if (!document.execCommand('copy')) throw Error()}
    notify('已複製主機資料夾路徑');
  } catch(e) {input.focus(); input.select(); notify('已選取路徑，可使用系統的複製功能。')}
};
function switchView() {
  const next=location.hash==='#archive' ? 'archive' : 'setup';
  if (next===view) return;
  dismiss(); releasePreviews(); closePlayback(); $('#controls').open=false;
  clearTimeout(setupTimer); clearTimeout(expiryTimer);
  archiveVersion++; filesVersion++;
  view=next;
  for (const page of ['setup','archive']) {
    $('#'+page).hidden=page!==view;
    const nav=$('#nav-'+page);
    if (page===view) nav.setAttribute('aria-current','page'); else nav.removeAttribute('aria-current');
  }
  document.title=(view==='archive'?'錄影檔':'架設檢查')+' · AAEON 錄影主機';
  if (view==='archive') loadArchive();
  else {offline=true; paintSetup(); syncSetup()}
}
window.addEventListener('hashchange',switchView);
document.addEventListener('visibilitychange',()=>{
  if (document.hidden) {
    releasePreviews(); clearTimeout(setupTimer); clearTimeout(expiryTimer); $('#player').pause();
  } else if (view==='setup') {offline=true; paintSetup(); syncSetup()}
});
window.addEventListener('pagehide',releasePreviews);
switchView();
