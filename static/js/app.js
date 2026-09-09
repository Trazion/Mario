  // ── State ──────────────────────────────────────────────────────────────────
  let playlist=[], scannedFiles=[], currentTab='video';
  let selectedAudioTrack=null, globalMuted=false;
  let currentLogTab='app', appLogs=[];
  let ffmpegLogInterval=null, uptimeInterval=null, statsInterval=null;
  let streamStartEpoch=null, totalPlaylistSecs=0;
  let fpsHistory=new Array(60).fill(0), maxFpsSeen=30;
  let overlayPos='tl';
  const DAY_NAMES=['Mon','Tue','Wed','Thu','Fri','Sat','Sun'];

  // ── Init ───────────────────────────────────────────────────────────────────
  async function init() {
    log('Ready to stream...','info');
    restoreTheme();
    await checkStatus();
    // Restore playlist from backend (survives page refresh)
    try {
      const d=await(await fetch('/api/playlist')).json();
      if(d.playlist && d.playlist.length){
        playlist=d.playlist; renderPlaylist();
        log(`Restored playlist: ${playlist.length} video${playlist.length>1?'s':''}`, 'info');
      }
    } catch(e){}
    loadSavedPlaylists(); loadProfiles(); renderScheduleJobs(); renderHistory();
    renderStatsIdle(); drawSparkline(); updateFilter();
    setInterval(checkStatus,5000);
    setInterval(renderScheduleJobs,30000);
  }

  // ── Status ─────────────────────────────────────────────────────────────────
  async function checkStatus() {
    try {
      const d=await(await fetch('/api/status')).json();
      document.getElementById('dotFFmpeg').className='status-dot '+(d.ffmpeg_available?'ok':'error');
      document.getElementById('dotV4l2').className  ='status-dot '+(d.v4l2_available ?'ok':'error');
      if(d.streaming){
        window._isStreaming = true;
        document.getElementById('dotStream').className='status-dot live';
        document.getElementById('streamStatus').textContent='Live';
        document.getElementById('livePanel').classList.add('show');
        document.getElementById('progressRow').style.display='';
        _setStartButtonsState(true, '▶ Live');
        document.getElementById('stopBtn').disabled=false;
        document.getElementById('skipBtn').disabled=false;
        if(!uptimeInterval) startUptimeTimer(d.uptime);
      } else {
        window._isStreaming = false;
        document.getElementById('dotStream').className='status-dot';
        document.getElementById('streamStatus').textContent='Idle';
        document.getElementById('livePanel').classList.remove('show');
        document.getElementById('progressRow').style.display='none';
        const inFlight = (typeof _startInFlight!=='undefined' && _startInFlight);
        if(!inFlight){
          _setStartButtonsState(false, '▶ Start Stream');
        }
        document.getElementById('stopBtn').disabled=true;
        document.getElementById('skipBtn').disabled=true;
        stopUptimeTimer(); stopStatsPolling(); stopFFmpegLogPoll();
      }

      if(d.virtual_devices&&d.virtual_devices.length)
        document.getElementById('deviceSelect').innerHTML=d.virtual_devices.map(v=>`<option value="${esc(v)}">${esc(v)}</option>`).join('');
      window._isStreaming = !!d.streaming;
      try{ updateTopbarPill(!!d.streaming); }catch(_){}
      updateSidebarFooter(!!d.streaming);
    }catch(e){
      try{ updateTopbarPill(false, e && e.message ? e.message : 'status error'); }catch(_){}
      updateSidebarFooter(false, true);
    }
  }

  // Sidebar footer (auth + stream state). Auth comes from /api/app/info, cached.
  let _authCached=null;
  async function _loadAuthCache(){
    if(_authCached!==null) return _authCached;
    try{
      const i=await(await fetch('/api/app/info')).json();
      _authCached={auth:!!i.auth_enabled, csrf:!!i.csrf_enabled};
    }catch(e){ _authCached={auth:false, csrf:false}; }
    return _authCached;
  }
  async function updateSidebarFooter(streamingNow, statusErr){
    const a=await _loadAuthCache();
    const fa=document.getElementById('sbFootAuth');
    const fs=document.getElementById('sbFootStream');
    if(fa){
      fa.classList.remove('ok','bad');
      fa.classList.add(a.auth?'ok':'bad');
      const lbl=fa.querySelector('.sb-foot-text');
      if(lbl) lbl.textContent = a.auth?'Auth enabled':'Auth DISABLED';
    }
    if(fs){
      fs.classList.remove('ok','bad');
      if(statusErr) fs.classList.add('bad');
      else if(streamingNow) fs.classList.add('ok');
      const lbl=fs.querySelector('.sb-foot-text');
      if(lbl) lbl.textContent = statusErr?'Status error':(streamingNow?'Live':'Idle');
    }
  }
  window.updateSidebarFooter = updateSidebarFooter;

  // ── Uptime ─────────────────────────────────────────────────────────────────
  function startUptimeTimer(serverUptime){
    streamStartEpoch=Date.now()-serverUptime*1000;
    uptimeInterval=setInterval(()=>{
      const s=Math.floor((Date.now()-streamStartEpoch)/1000);
      document.getElementById('uptimeBadge').textContent=fmtDur(s);
      document.getElementById('progressTime').textContent=fmtDur(s);
      const pct=totalPlaylistSecs>0?Math.min(100,(s%totalPlaylistSecs)/totalPlaylistSecs*100):(s%60)/60*100;
      document.getElementById('progressFill').style.width=pct+'%';
    },1000);
  }
  function stopUptimeTimer(){
    if(uptimeInterval){clearInterval(uptimeInterval);uptimeInterval=null;}
    document.getElementById('uptimeBadge').textContent='00:00';
    document.getElementById('progressFill').style.width='0%';
  }

  // ── Log tabs ───────────────────────────────────────────────────────────────
  function switchLogTab(tab){
    currentLogTab=tab;
    document.getElementById('ltApp').className='log-tab'+(tab==='app'?' active':'');
    document.getElementById('ltFFmpeg').className='log-tab'+(tab==='ffmpeg'?' active':'');
    tab==='ffmpeg'?fetchFFmpegLogs():renderAppLogs();
  }
  function renderAppLogs(){
    if(currentLogTab!=='app') return;
    const a=document.getElementById('logArea');
    a.innerHTML=appLogs.map(l=>`<div class="log-line ${esc(l.t)}">${esc(l.m)}</div>`).join('');
    a.scrollTop=a.scrollHeight;
  }
  async function fetchFFmpegLogs(){
    if(currentLogTab!=='ffmpeg') return;
    try{
      // Prefer redacted /api/logs/recent (added v3.9.0); fall back to legacy.
      let lines=[];
      try{
        const r=await fetch('/api/logs/recent?n=100');
        if(r.ok){ const j=await r.json(); lines=j.lines||[]; }
      }catch(_){}
      if(!lines.length){
        try{ const d=await(await fetch('/api/stream/logs?n=60')).json(); lines=d.logs||[]; }catch(_){}
      }
      const a=document.getElementById('logArea');
      a.innerHTML=lines.map(l=>`<div class="log-line ${/[Ee]rror/.test(l)?'error':/warning/.test(l)?'warn':''}">${esc(l)}</div>`).join('')||'<div class="log-line">No output yet</div>';
      a.scrollTop=a.scrollHeight;
    }catch(e){}
  }
  // SSE-backed: log updates arrive on the shared event stream (see openSse).
  // We keep these two as no-ops so legacy callers stay valid.
  function startFFmpegLogPoll(){ openSse(); }
  function stopFFmpegLogPoll(){ /* SSE auto-closes when stream stops */ }
  // v3.7: harden against XSS — escape HTML (incl. quotes) and provide a
  // JSON encoder for safe injection of strings into inline JS attributes.
  function esc(s){return String(s==null?'':s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;');}
  function escJs(s){return JSON.stringify(String(s==null?'':s));}

  // ── Theme ──────────────────────────────────────────────────────────────────
  function _themeBtn(){ return document.getElementById('themeToggleBtn'); }
  // v3.9.2: light theme disabled (white-on-white bug). Lock to dark.
  function toggleTheme(){
    try{ toast('Light theme temporarily disabled in v3.9.2','warn'); }catch(_){}
  }
  function restoreTheme(){
    try{
      document.documentElement.setAttribute('data-theme','dark');
      localStorage.setItem('mario_theme','dark');
      const b=_themeBtn(); if(b){ b.textContent='☾'; b.title='Dark theme locked (v3.9.2)'; }
    }catch(_){}
  }

  // ── Keyboard shortcuts (safer: no destructive action without confirm) ──────
  // Space=preview · N=skip (if streaming) · S=start (confirm) · X=stop (confirm)
  // F=preview · R=restart (confirm if running) · T=theme
  document.addEventListener('keydown', e => {
    const tag=(e.target && e.target.tagName)||'';
    if(['INPUT','SELECT','TEXTAREA'].includes(tag)) return;
    if(e.target && e.target.isContentEditable) return;
    if(e.code==='Space'){ e.preventDefault(); togglePreview(); }
    else if(e.key==='n' || e.key==='N'){ if(window._isStreaming) skipClip(); }
    else if(e.key==='s' || e.key==='S'){
      if(window._isStreaming) return;
      if(confirm('Start stream now?')) startStream();
    }
    else if(e.key==='x' || e.key==='X'){
      if(!window._isStreaming) return;
      if(confirm('Stop the stream?')) stopStream();
    }
    else if(e.key==='f' || e.key==='F'){ togglePreview(); }
    else if(e.key==='r' || e.key==='R'){
      if(document.getElementById('startBtn').disabled) return;
      if(confirm('Restart stream?')) restartStream();
    }
    else if(e.key==='t' || e.key==='T'){ toggleTheme(); }
  });
  async function restartStream(){ await stopStream(); setTimeout(startStream,800); }

  // ── File browser ───────────────────────────────────────────────────────────
  function switchTab(tab){
    currentTab=tab;
    document.getElementById('tabVideo').className='tab'+(tab==='video'?' active':'');
    document.getElementById('tabAudio').className='tab'+(tab==='audio'?' active':'');
    document.getElementById('addAllBtn').style.display='none';
    scannedFiles=[];
    document.getElementById('fileList').innerHTML=`<div class="empty-state"><div class="icon">${tab==='audio'?'🎵':'🎬'}</div><p>Enter a folder path and click Scan</p></div>`;
  }

  async function scanFolder(){
    const folder=document.getElementById('folderInput').value.trim();
    const recursive=document.getElementById('recursiveCheck').checked;
    if(!folder) return;
    document.getElementById('fileList').innerHTML='<div class="empty-state"><div class="icon">⏳</div><p>Scanning...</p></div>';
    document.getElementById('addAllBtn').style.display='none';
    try{
      const d=await(await apiFetch('/api/scan',{method:'POST',body:{folder,type:currentTab,recursive}})).json();
      if(d.error){document.getElementById('fileList').innerHTML=`<div class="empty-state"><div class="icon">❌</div><p>${esc(d.error)}</p></div>`;return;}
      scannedFiles=d.files;
      if(!d.count){document.getElementById('fileList').innerHTML=`<div class="empty-state"><div class="icon">🔍</div><p>No ${esc(currentTab)} files found</p></div>`;return;}
      renderFileList(scannedFiles);
      if(currentTab==='video') document.getElementById('addAllBtn').style.display='';
      log(`Found ${d.count} file(s)`,'success');
    }catch(e){log('Scan error: '+e.message,'error');}
  }

  function renderFileList(files){
    const l=document.getElementById('fileList');
    if(currentTab==='audio'){
      l.innerHTML=files.map((f,i)=>`
        <div class="file-item audio-item" onclick="selectAudioTrack(${i})">
          <div class="file-thumb audio-bg">🎵</div>
          <div class="file-info"><div class="file-name">${esc(f.name)}</div><div class="file-meta">${esc(f.info.size_str)}</div></div>
          <div class="add-btn audio-add">♪</div>
        </div>`).join('');
    } else {
      l.innerHTML=files.map((f,i)=>`
        <div class="file-item" onclick="addToPlaylist(${i})">
          <img class="file-thumb" id="fthumb-${i}" src="" alt="" style="display:none" onerror="this.style.display='none';this.nextSibling.style.display='flex'">
          <div class="file-thumb" id="fthumb-icon-${i}" style="display:flex">🎬</div>
          <div class="file-info"><div class="file-name">${esc(f.name)}</div><div class="file-meta">${esc(f.info.duration_str)} · ${esc(f.info.size_str)} · ${esc(f.info.width)}×${esc(f.info.height)}</div></div>
          <span class="${f.info.has_audio?'has-audio-badge':'no-audio-badge'}">${f.info.has_audio?'🔊':'🔇'}</span>
          <button class="add-btn" title="Preview" style="background:rgba(124,58,237,.12);border-color:rgba(124,58,237,.25);color:var(--accent2);font-size:12px"
            onclick="event.stopPropagation();openPreview(${i})">▶</button>
          <div class="add-btn" onclick="event.stopPropagation();addToPlaylist(${i})" title="Add to playlist">+</div>
        </div>`).join('');
      // Lazy-load thumbnails
      files.forEach((f,i)=>loadThumb(f.path, i, 'fthumb-', 'fthumb-icon-'));
    }
  }

  async function loadThumb(path, i, imgPrefix, iconPrefix){
    try{
      const d=await(await apiFetch('/api/thumb',{method:'POST',body:{path}})).json();
      if(d.thumb){
        const img=document.getElementById(imgPrefix+i);
        const ico=document.getElementById(iconPrefix+i);
        if(img){img.src=d.thumb;img.style.display='block';if(ico)ico.style.display='none';}
      }
    }catch(e){}
  }

  async function addAllToPlaylist(){
    if(!scannedFiles.length||currentTab!=='video') return;
    const n=scannedFiles.length;
    scannedFiles.forEach(f=>playlist.push({...f,volume:1.0,muted:false,start_offset_seconds:0}));
    renderPlaylist();
    const ok=await syncPlaylistToBackend();
    log(ok?`Added all ${n} video(s) ✓`:`Added ${n} locally — backend sync failed`, ok?'success':'warn');
  }

  function selectAudioTrack(i){
    const f=scannedFiles[i]; selectedAudioTrack=f.path;
    const b=document.getElementById('audioTrackBadge');
    b.textContent='🎵 '+f.name; b.className='audio-track-badge set';
    log(`Audio track: ${f.name}`,'success');
  }
  function clearAudioTrack(){
    selectedAudioTrack=null;
    const b=document.getElementById('audioTrackBadge');
    b.textContent='No external audio'; b.className='audio-track-badge';
  }

  // ── Playlist ───────────────────────────────────────────────────────────────
  // Playlist sync — fire-and-forget, but returns a Promise so callers may await.
  // Backend keeps its own copy so /api/stream/start has the latest list.
  async function syncPlaylistToBackend(){
    try{
      const r = await apiFetch('/api/playlist/reorder',{method:'POST',body:{playlist}});
      if(!r.ok){
        let detail='';
        try{ const j=await r.json(); detail=j.error||j.detail||''; }catch(_){}
        const msg = (r.status>=500 || /database/i.test(detail))
          ? 'Cannot save playlist: database is not writable.'
          : `Playlist sync failed (HTTP ${r.status}) ${detail}`;
        try{ toast(msg,'error'); }catch(_){}
        log(msg,'error');
        return false;
      }
      return true;
    }catch(e){
      try{ toast('Cannot save playlist: '+e.message,'error'); }catch(_){}
      log('Playlist sync error: '+e.message,'error'); return false;
    }
  }
  async function addToPlaylist(i){
    playlist.push({...scannedFiles[i],volume:1.0,muted:false,start_offset_seconds:0});
    renderPlaylist();
    log(`Added: ${scannedFiles[i].name}`,'success');
    await syncPlaylistToBackend();
  }
  async function removeFromPlaylist(i){playlist.splice(i,1);renderPlaylist();await syncPlaylistToBackend();}
  async function clearPlaylist(){playlist=[];renderPlaylist();await syncPlaylistToBackend();log('Playlist cleared','info');}
  function toggleItemMute(i){playlist[i].muted=!playlist[i].muted;renderPlaylist();}
  function setItemVolume(i,v){playlist[i].volume=parseFloat(v);const el=document.getElementById(`vl-${i}`);if(el)el.textContent=Math.round(v*100)+'%';}

  // ── v3.9.16: per-clip start offset (Windows Media Player style trim) ─────
  function _fmtHMS(secs){
    let s=Math.max(0, Math.round(Number(secs)||0));
    const h=Math.floor(s/3600); s-=h*3600;
    const m=Math.floor(s/60);   s-=m*60;
    const pad=n=>String(n).padStart(2,'0');
    return h>0 ? `${pad(h)}:${pad(m)}:${pad(s)}` : `${pad(m)}:${pad(s)}`;
  }
  function _parseHMS(str){
    if(!str) return 0;
    const parts=String(str).trim().split(':').map(Number);
    if(parts.some(n=>Number.isNaN(n))) return NaN;
    if(parts.length===1) return parts[0];
    if(parts.length===2) return parts[0]*60+parts[1];
    if(parts.length===3) return parts[0]*3600+parts[1]*60+parts[2];
    return NaN;
  }
  let _offsetSyncTimer=null;
  function _scheduleOffsetSync(){
    if(_offsetSyncTimer) clearTimeout(_offsetSyncTimer);
    _offsetSyncTimer=setTimeout(()=>{ _offsetSyncTimer=null; syncPlaylistToBackend(); }, 600);
  }
  function _clampOffset(i, secs){
    const v=playlist[i]; if(!v) return 0;
    const dur=Number((v.info && v.info.duration)||0);
    let s=Math.max(0, Number(secs)||0);
    if(dur>0 && s > dur-1) s = Math.max(0, dur-1);
    return Math.round(s*1000)/1000;
  }
  function _updateOffsetUI(i){
    const v=playlist[i]; if(!v) return;
    const sofs=Number(v.start_offset_seconds||0);
    const dur=Number((v.info && v.info.duration)||0);
    const sl=document.getElementById(`ofs-slider-${i}`); if(sl){ sl.value=String(sofs); if(dur>0) sl.max=String(Math.max(1,dur)); }
    const tx=document.getElementById(`ofs-text-${i}`);   if(tx) tx.value=_fmtHMS(sofs);
    const lb=document.getElementById(`ofs-label-${i}`);
    if(lb){
      if(sofs<=0) lb.textContent='Clip starts at: Beginning';
      else        lb.textContent=`Clip starts at: ${_fmtHMS(sofs)}`;
      lb.classList.remove('warn');
      if(dur>0 && sofs >= dur*0.9){ lb.textContent+=' · near end'; lb.classList.add('warn'); }
    }
  }
  function setClipOffset(i, secs){
    if(!playlist[i]) return;
    playlist[i].start_offset_seconds = _clampOffset(i, secs);
    _updateOffsetUI(i);
    _scheduleOffsetSync();
  }
  function setClipOffsetText(i, str){
    const v=_parseHMS(str);
    if(Number.isNaN(v) || v<0){
      try{ toast('Invalid time — reset to 00:00','warn'); }catch(_){}
      setClipOffset(i, 0); return;
    }
    setClipOffset(i, v);
  }
  function resetClipOffset(i){ setClipOffset(i, 0); }
  function previewClipFromOffset(i){
    const v=playlist[i]; if(!v) return;
    const url='/api/preview/'+encodeURIComponent(v.path)+'?ts='+Date.now();
    const w=window.open('','_blank','width=720,height=480');
    if(!w) return;
    const sofs=Number(v.start_offset_seconds||0);
    w.document.write(`<title>${(v.name||'preview').replace(/[<>]/g,'')}</title><body style="margin:0;background:#000"><video src="${url}" controls autoplay style="width:100%;height:100vh;object-fit:contain"></video><script>document.querySelector('video').addEventListener('loadedmetadata',function(){try{this.currentTime=${sofs};}catch(_){}});</script>`);
  }
  window.setClipOffset = setClipOffset;
  window.setClipOffsetText = setClipOffsetText;
  window.resetClipOffset = resetClipOffset;
  window.previewClipFromOffset = previewClipFromOffset;

  async function shufflePlaylist(){
    for(let i=playlist.length-1;i>0;i--){const j=Math.floor(Math.random()*(i+1));[playlist[i],playlist[j]]=[playlist[j],playlist[i]];}
    renderPlaylist(); log('Playlist shuffled 🔀','info');
    await syncPlaylistToBackend();
  }

  function renderPlaylist(){
    const el=document.getElementById('playlistEl');
    if(!playlist.length){
      el.innerHTML='<div class="empty-state"><div class="icon">➕</div><p>Add videos from the browser on the left</p></div>';
      document.getElementById('totalDuration').textContent='00:00';
      totalPlaylistSecs=0; return;
    }
    const cnt={};
    playlist.forEach(v=>cnt[v.path]=(cnt[v.path]||0)+1);
    // current_index from status is updated async; use a local approximation
    const nowIdx=window._nowPlayingIdx||0;

    el.innerHTML=playlist.map((v,i)=>{
      const dur=Number((v.info && v.info.duration)||0);
      const sofs=Number(v.start_offset_seconds||0);
      const durMax=Math.max(1, dur||600);
      const offsetLabel = sofs<=0 ? 'Clip starts at: Beginning' : `Clip starts at: ${_fmtHMS(sofs)}`;
      return `
      <div class="playlist-item${cnt[v.path]>1?' duplicate':''}${i===nowIdx&&window._isStreaming?' now-playing':''}" draggable="true"
           ondragstart="dragStart(${i})" ondragover="dragOver(event,${i})" ondrop="drop(event,${i})">
        <div class="pl-row">
          <span class="drag-handle">⠿</span>
          <span class="playlist-num">${i+1}</span>
          <img class="pl-thumb" id="plthumb-${i}" src="" alt="" style="display:none" onerror="this.style.display='none';this.nextSibling.style.display='flex'">
          <div class="pl-thumb" id="plthumb-icon-${i}" style="display:flex">🎬</div>
          <div class="file-info">
            <div class="file-name">${esc(v.name)}</div>
            <div class="file-meta">${esc(v.info.duration_str)} · ${esc(v.info.width)}×${esc(v.info.height)}</div>
          </div>
          ${i===nowIdx&&window._isStreaming?'<span class="now-badge">▶ NOW</span>':''}
          ${cnt[v.path]>1?'<span class="dup-badge">DUP</span>':''}
          <button class="mute-btn" onclick="silentCheck(${escJs(v.path)})" title="Silent-gap check">🔈</button>
          <div class="remove-btn" onclick="removeFromPlaylist(${i})">×</div>
        </div>
        <div class="clip-trim" title="Start the clip from this time">
          <div class="ct-line">
            <span class="ct-from">Start from</span>
            <input type="range" id="ofs-slider-${i}" class="ct-slider"
                   min="0" max="${durMax}" step="1" value="${sofs}"
                   oninput="setClipOffset(${i}, this.value)">
            <input type="text" id="ofs-text-${i}" class="ct-text"
                   value="${_fmtHMS(sofs)}" placeholder="00:00"
                   onchange="setClipOffsetText(${i}, this.value)">
            <span class="ct-dur">/ ${esc(v.info.duration_str||_fmtHMS(dur))}</span>
            <button class="btn btn-ghost ct-btn" onclick="resetClipOffset(${i})" title="Reset to 00:00">↺</button>
            <button class="btn btn-ghost ct-btn" onclick="previewClipFromOffset(${i})" title="Preview from this time">▶</button>
          </div>
          <div id="ofs-label-${i}" class="ct-label">${offsetLabel}</div>
        </div>
      </div>`;
    }).join('');

    // Lazy-load playlist thumbnails
    playlist.forEach((v,i)=>loadThumb(v.path,i,'plthumb-','plthumb-icon-'));

    const total=playlist.reduce((s,v)=>s+(v.info.duration||0),0);
    totalPlaylistSecs=total;
    document.getElementById('totalDuration').textContent=fmtDur(total);
  }

  // ── Saved Playlists ────────────────────────────────────────────────────────
  async function loadSavedPlaylists(){
    try{
      const d=await(await fetch('/api/playlists')).json();
      document.getElementById('plSelect').innerHTML='<option value="">— saved playlists —</option>'+d.playlists.map(n=>`<option value="${esc(n)}">${esc(n)}</option>`).join('');
    }catch(e){}
  }
  async function savePlaylist(){
    const name=document.getElementById('plNameInput').value.trim();
    if(!name){log('Enter a playlist name','error');return;}
    if(!playlist.length){log('Playlist is empty — nothing to save','error');return;}
    // Send the frontend playlist directly so backend doesn't need to be in sync
    const d=await(await apiFetch('/api/playlists/save',{method:'POST',body:{name, items: playlist}})).json();
    if(d.error){log('Save error: '+d.error,'error');return;}
    log(`Saved: "${name}" (${playlist.length} video${playlist.length>1?'s':''})`,'success'); loadSavedPlaylists();
  }
  async function loadPlaylist(){
    const name=document.getElementById('plSelect').value; if(!name) return;
    const d=await(await apiFetch('/api/playlists/load',{method:'POST',body:{name}})).json();
    if(d.error){log('Load error: '+d.error,'error');return;}
    playlist=d.playlist;
    // Sync loaded playlist to backend so stream/start can use it
    await apiFetch('/api/playlist/reorder',{method:'POST',body:{playlist}});
    renderPlaylist(); log(`Loaded: "${name}" (${playlist.length} video${playlist.length>1?'s':''})`,'success');
  }
  async function deletePlaylist(){
    const name=document.getElementById('plSelect').value; if(!name) return;
    await apiFetch('/api/playlists/delete',{method:'POST',body:{name}});
    log(`Deleted: "${name}"`,'warn'); loadSavedPlaylists();
  }

  // ── Preset Profiles ────────────────────────────────────────────────────────
  function getStreamSettings(){
    return {
      device:       document.getElementById('deviceSelect').value,
      resolution:   document.getElementById('resolutionSelect').value,
      fps:          document.getElementById('fpsSelect').value,
      preset:       document.getElementById('presetSelect').value,
      buffer_size:  document.getElementById('bufferSelect').value,
      loop_mode:    document.getElementById('loopModeSelect')?.value || 'all',
      loop:         (document.getElementById('loopModeSelect')?.value || 'all') !== 'once',
      auto_restart: document.getElementById('autoRestartToggle').classList.contains('on'),
      shuffle:      document.getElementById('shuffleToggle').classList.contains('on') ||
                    document.getElementById('loopModeSelect')?.value === 'shuffle',
      auto_quality: document.getElementById('autoQualityToggle')?.classList.contains('on') || false,
      // Phase 3B: convert UI percent (0-200) to FFmpeg multiplier (0-2). Server clamps to 0-4.
      global_volume:Math.max(0, Math.min(4, (Number(document.getElementById('globalVolSlider').value||100))/100)),
      mute_audio:   globalMuted,
      // Phase 3A: external audio track (selected from audio tab) — was missing entirely.
      extra_audio:  selectedAudioTrack || null,
      rtmp_url:     document.getElementById('rtmpInput').value.trim(),
      rtmp_url_2:   (document.getElementById('rtmpInput2')?.value || '').trim(),
      text_overlay: document.getElementById('overlayText').value.trim(),
      overlay_pos:  overlayPos,
      watermark:        (document.getElementById('watermarkPath')?.value || '').trim() || null,
      watermark_pos:    document.getElementById('watermarkPos')?.value || 'br',
      watermark_opacity:parseFloat(document.getElementById('watermarkOpacity')?.value || 0.7),
      watermark_scale:  parseFloat(document.getElementById('watermarkScale')?.value || 0.15),
      record:           document.getElementById('recordToggle')?.classList.contains('on') || false,
      smart_shuffle:    document.getElementById('smartShuffleToggle')?.classList.contains('on') || false,
      fit_mode:         document.getElementById('fitModeSelect')?.value || 'fit',
      normalize_before_live: document.getElementById('normalizeBeforeLiveToggle')?.classList.contains('on') !== false,
      ...getFilterSettings(),
    };
  }
  function applyStreamSettings(s){
    if(s.device)       document.getElementById('deviceSelect').value=s.device;
    if(s.resolution)   {document.getElementById('resolutionSelect').value=s.resolution;updateResBadge();}
    if(s.fps)          document.getElementById('fpsSelect').value=s.fps;
    if(s.preset)       document.getElementById('presetSelect').value=s.preset;
    if(s.buffer_size!==undefined) document.getElementById('bufferSelect').value=s.buffer_size;
    if(s.loop_mode)               { const el=document.getElementById('loopModeSelect'); if(el) el.value=s.loop_mode; }
    if(s.fit_mode){ const el=document.getElementById('fitModeSelect'); if(el){ el.value=s.fit_mode; if(window.updateFitBadge) updateFitBadge(); } }
    if(s.normalize_before_live!==undefined){ setToggle('normalizeBeforeLiveToggle', s.normalize_before_live); if(window.updateNormalizeBadge) updateNormalizeBadge(); }
    if(s.auto_restart!==undefined){setToggle('autoRestartToggle',s.auto_restart);updateRestartBadge();}
    if(s.shuffle!==undefined)     setToggle('shuffleToggle',s.shuffle);
    if(s.auto_quality!==undefined) setToggle('autoQualityToggle', s.auto_quality);
    if(s.global_volume!==undefined){
      // Phase 4: profile stores multiplier (0..4, 1.0 = 100%). Legacy profiles
      // stored raw slider percent (0..200). Heuristic: <=4 → multiplier.
      const gv = Number(s.global_volume);
      const pct = (isFinite(gv) && gv <= 4) ? Math.round(gv*100) : Math.max(0, Math.min(200, gv|0));
      document.getElementById('globalVolSlider').value = pct;
      updateGlobalVol();
    }
    if(s.rtmp_url!==undefined)    document.getElementById('rtmpInput').value=s.rtmp_url;
    if(s.rtmp_url_2!==undefined){ const el=document.getElementById('rtmpInput2'); if(el) el.value=s.rtmp_url_2||''; }
    if(s.text_overlay!==undefined)document.getElementById('overlayText').value=s.text_overlay;
    if(s.overlay_pos)             setPos(s.overlay_pos);
    // Watermark
    if(s.watermark!==undefined){ const el=document.getElementById('watermarkPath'); if(el) el.value=s.watermark||''; }
    if(s.watermark_pos!==undefined){ const el=document.getElementById('watermarkPos'); if(el) el.value=s.watermark_pos; }
    if(s.watermark_opacity!==undefined){ const el=document.getElementById('watermarkOpacity'); if(el) el.value=s.watermark_opacity; }
    if(s.watermark_scale!==undefined){ const el=document.getElementById('watermarkScale'); if(el) el.value=s.watermark_scale; }
    // Recording + smart shuffle toggles
    if(s.record!==undefined && document.getElementById('recordToggle')) setToggle('recordToggle', s.record);
    if(s.smart_shuffle!==undefined && document.getElementById('smartShuffleToggle')) setToggle('smartShuffleToggle', s.smart_shuffle);
    // Global mute / external audio
    if(s.mute_audio!==undefined){
      globalMuted = !!s.mute_audio;
      const b=document.getElementById('globalMuteBtn');
      if(b){ b.className='global-mute-btn'+(globalMuted?' muted':''); b.textContent=globalMuted?'🔊 Unmute':'🔇 Mute'; }
    }
    if(s.extra_audio!==undefined){
      // Only restore if the path is still in the scanned list, otherwise just remember it.
      selectedAudioTrack = s.extra_audio || null;
      const b=document.getElementById('audioTrackBadge');
      if(b){
        if(selectedAudioTrack){
          const nm = String(selectedAudioTrack).split('/').pop();
          b.textContent='🎵 '+nm; b.className='audio-track-badge set';
        } else {
          b.textContent='No external audio'; b.className='audio-track-badge';
        }
      }
    }
    // Video filters
    if(s.vf_brightness!==undefined) document.getElementById('fBrightness').value=s.vf_brightness;
    if(s.vf_contrast!==undefined)   document.getElementById('fContrast').value=s.vf_contrast;
    if(s.vf_saturation!==undefined) document.getElementById('fSaturation').value=s.vf_saturation;
    if(s.vf_grayscale!==undefined)  setToggle('grayscaleToggle', s.vf_grayscale);
    updateFilter();
    updateResBadge(); updateRestartBadge();
  }
  function setToggle(id,on){
    const el=document.getElementById(id);
    if(on) el.classList.add('on'); else el.classList.remove('on');
  }

  async function loadProfiles(){
    try{
      const d=await(await fetch('/api/profiles')).json();
      const grid=document.getElementById('profileGrid');
      const sel=document.getElementById('profileSelect');
      // Always reset via DOM API — never innerHTML profile names, which
      // come from on-disk JSON and could contain HTML/quotes from an
      // imported backup. Even with escJs() the attribute context is fragile.
      grid.replaceChildren();
      sel.replaceChildren();
      const placeholder=document.createElement('option');
      placeholder.value=''; placeholder.textContent='— load profile —';
      sel.appendChild(placeholder);
      if(!d.profiles||!d.profiles.length){
        const empty=document.createElement('div');
        empty.className='empty-state';
        empty.style.cssText='padding:20px;grid-column:1/-1';
        empty.innerHTML='<div class="icon" style="font-size:28px">⚡</div><p>No profiles saved yet</p>';
        grid.appendChild(empty);
        return;
      }
      for(const p of d.profiles){
        const card=document.createElement('div');
        card.className='profile-card';
        card.dataset.name=p.name;
        card.addEventListener('click',()=>applyProfile(card.dataset.name));

        const del=document.createElement('div');
        del.className='profile-del';
        del.textContent='×';
        del.addEventListener('click',(ev)=>{
          ev.stopPropagation();
          deleteProfile(card.dataset.name);
        });

        const nameEl=document.createElement('div');
        nameEl.className='profile-name';
        nameEl.textContent=p.name;

        const meta=document.createElement('div');
        meta.className='profile-meta';
        const s=p.settings||{};
        const line1=document.createTextNode(
          `${s.resolution||'auto'} · ${s.fps||30}fps`);
        const line2=document.createTextNode(s.preset||'ultrafast');
        const created=document.createElement('span');
        created.style.color='var(--text-dim)';
        created.textContent=p.created||'';
        meta.appendChild(line1);
        meta.appendChild(document.createElement('br'));
        meta.appendChild(line2);
        meta.appendChild(document.createElement('br'));
        meta.appendChild(created);

        card.appendChild(del);
        card.appendChild(nameEl);
        card.appendChild(meta);
        grid.appendChild(card);

        const opt=document.createElement('option');
        opt.value=p.name; opt.textContent=p.name;
        sel.appendChild(opt);
      }
    }catch(e){}
  }
  async function saveProfile(){
    const name=document.getElementById('profileNameInput').value.trim();
    if(!name){log('Enter a profile name','error');return;}
    const d=await(await apiFetch('/api/profiles/save',{method:'POST',body:{name,settings:getStreamSettings()}})).json();
    if(d.error){log('Profile error: '+d.error,'error');return;}
    log(`Profile saved: "${name}"`,'success'); loadProfiles();
    document.getElementById('profileNameInput').value='';
  }
  async function applyProfile(name){
    try{
      const d=await(await fetch('/api/profiles')).json();
      const p=d.profiles.find(x=>x.name===name);
      if(p){applyStreamSettings(p.settings);log(`Profile applied: "${name}"`,'success');}
    }catch(e){}
  }
  async function loadProfile(){
    const name=document.getElementById('profileSelect').value; if(!name) return;
    applyProfile(name);
  }
  async function deleteProfile(name){
    await apiFetch('/api/profiles/delete',{method:'POST',body:{name}});
    log(`Profile deleted: "${name}"`,'warn'); loadProfiles();
  }

  // ── Stream History ─────────────────────────────────────────────────────────
  async function renderHistory(){
    try{
      const d=await(await fetch('/api/history')).json();
      const el=document.getElementById('historyList');
      if(!d.history.length){
        el.innerHTML='<div class="empty-state" style="padding:20px"><div class="icon" style="font-size:28px">📋</div><p>No sessions recorded yet</p></div>'; return;
      }
      el.innerHTML=d.history.map(h=>`
        <div class="history-item">
          <div class="history-dot"></div>
          <div class="history-info">
            <div class="history-date">${esc(h.started)}</div>
            <div class="history-meta">${esc(h.resolution)} · ${esc(h.device)} · ${esc(h.playlist_count)} video(s) · ${esc(h.peak_fps)} fps peak${h.restarts?` · ${esc(h.restarts)} restarts`:''}${h.rtmp?' · RTMP':''}</div>
          </div>
          <div class="history-dur">${esc(h.duration_str)}</div>
        </div>`).join('');
    }catch(e){}
  }
  async function clearHistory(){
    await apiFetch('/api/history/clear',{method:'POST'});
    renderHistory(); log('History cleared','info');
  }

  // ── Live Preview (MJPEG from virtual cam) ────────────────────────────────
  function togglePreview(){
    const box=document.getElementById('previewBox');
    const idle=document.getElementById('previewIdle');
    const img=document.getElementById('previewImg');
    const btn=document.getElementById('previewToggleBtn');
    if(box.style.display==='none'){
      const dev=document.getElementById('deviceSelect')?.value || '/dev/video10';
      img.src='/api/preview/mjpeg?device='+encodeURIComponent(dev)+'&_='+Date.now();
      box.style.display='block'; idle.style.display='none';
      btn.textContent='Hide';
    } else {
      img.src=''; box.style.display='none'; idle.style.display='block';
      btn.textContent='Show';
    }
  }

  // ── Stats Dashboard ────────────────────────────────────────────────────────
  async function fetchStats(){
    try{
      const d=await(await fetch('/api/stream/stats')).json();
      if(!d.streaming){renderStatsIdle();return;}
      const fps=Math.round(d.fps);
      sv('svFps',fps||'—'); sv('svBitrate',d.bitrate?d.bitrate.replace('kbits/s','k'):'—');
      sv('svSpeed',d.speed||'—'); sv('svFrames',d.frames?d.frames.toLocaleString():'—');
      sv('svDropped',d.dropped||0); sv('svSize',d.size_kb?fmtKB(d.size_kb):'—');
      document.getElementById('stDropped').className='stat-tile'+(d.dropped>0?' danger':'');
      maxFpsSeen=Math.max(maxFpsSeen,fps,1);
      sb('sbFps',fps/maxFpsSeen*100); sb('sbBitrate',Math.min((parseFloat(d.bitrate)||0)/5000*100,100));
      sb('sbSpeed',Math.min((parseFloat(d.speed)||0)/1.5*100,100));
      const badge=document.getElementById('statsIdleBadge');
      badge.textContent=`⏱ ${fmtDur(d.uptime)}`; badge.className='stats-idle-badge live';
      // Update "now playing" in live panel
      if(d.current_name) document.getElementById('liveNowPlaying').textContent='▶ '+d.current_name;
      window._nowPlayingIdx=d.current_index; window._isStreaming=true;
      fpsHistory.push(fps); fpsHistory.shift(); drawSparkline();
    }catch(e){renderStatsIdle();}
  }
  function renderStatsIdle(){
    ['svFps','svBitrate','svSpeed','svFrames','svSize'].forEach(id=>sv(id,'—'));
    sv('svDropped',0); ['sbFps','sbBitrate','sbSpeed','sbFrames'].forEach(id=>sb(id,0));
    const b=document.getElementById('statsIdleBadge'); b.textContent='Stream idle'; b.className='stats-idle-badge';
    window._isStreaming=false;
  }
  function sv(id,val){const el=document.getElementById(id);if(el)el.textContent=val;}
  function sb(id,pct){const el=document.getElementById(id);if(el)el.style.width=Math.max(0,Math.min(100,pct))+'%';}
  function fmtKB(kb){return kb<1024?kb+' KB':(kb/1024).toFixed(1)+' MB';}
  function drawSparkline(){
    const canvas=document.getElementById('fpsChart'); if(!canvas) return;
    const ctx=canvas.getContext('2d'), W=canvas.offsetWidth||400, H=canvas.height;
    canvas.width=W; ctx.clearRect(0,0,W,H);
    const max=Math.max(...fpsHistory,1), step=W/(fpsHistory.length-1);
    const grad=ctx.createLinearGradient(0,0,0,H);
    grad.addColorStop(0,'rgba(0,212,255,.35)'); grad.addColorStop(1,'rgba(0,212,255,.02)');
    ctx.beginPath();
    fpsHistory.forEach((v,i)=>{const x=i*step,y=H-(v/max)*(H-4)-2;i===0?ctx.moveTo(x,y):ctx.lineTo(x,y);});
    ctx.lineTo(W,H);ctx.lineTo(0,H);ctx.closePath();ctx.fillStyle=grad;ctx.fill();
    ctx.beginPath();
    fpsHistory.forEach((v,i)=>{const x=i*step,y=H-(v/max)*(H-4)-2;i===0?ctx.moveTo(x,y):ctx.lineTo(x,y);});
    ctx.strokeStyle='rgba(0,212,255,.9)';ctx.lineWidth=1.5;ctx.stroke();
  }
  function startStatsPolling(){ openSse(); fetchStats(); }
  function stopStatsPolling(){
    closeSse();
    renderStatsIdle();fpsHistory=new Array(60).fill(0);drawSparkline();
  }

  // ── Text overlay position ──────────────────────────────────────────────────
  function setPos(p){
    overlayPos=p;
    document.querySelectorAll('.pos-btn').forEach(b=>b.classList.toggle('active',b.dataset.pos===p));
  }

  // ── Controls ───────────────────────────────────────────────────────────────
  function updateGlobalVol(){document.getElementById('globalVolLabel').textContent=document.getElementById('globalVolSlider').value+'%';}
  function toggleGlobalMute(){
    globalMuted=!globalMuted;
    const b=document.getElementById('globalMuteBtn');
    b.className='global-mute-btn'+(globalMuted?' muted':'');
    b.textContent=globalMuted?'🔊 Unmute':'🔇 Mute';
  }
  function updateResBadge(){document.getElementById('autoResBadge').style.display=document.getElementById('resolutionSelect').value==='auto'?'':'none';}
  function updateRestartBadge(){
    const on=document.getElementById('autoRestartToggle').classList.contains('on');
    document.getElementById('restartBadge').style.display=on?'':'none';
  }

  // ── Drag & drop ────────────────────────────────────────────────────────────
  let dragIdx=null;
  function dragStart(i){dragIdx=i;}
  function dragOver(e,i){e.preventDefault();}
  async function drop(e,i){
    e.preventDefault();
    if(dragIdx===null||dragIdx===i){ dragIdx=null; return; }
    playlist.splice(i,0,playlist.splice(dragIdx,1)[0]);
    dragIdx=null; renderPlaylist();
    const ok=await syncPlaylistToBackend();
    if(!ok) log('Reorder not persisted — backend sync failed','warn');
  }
  window.drop = drop;

  // ── Server-Sent Events (push-based stats + logs) ────────────────────────
  let _sse=null, _ffLogBuf=[];
  function openSse(){
    if(_sse) return;
    try{
      _sse = new EventSource('/api/stream/events');
      _sse.addEventListener('tick', e => {
        const d = JSON.parse(e.data);
        // feed stats UI directly
        applyStats(d.stats || {}, d);
      });
      _sse.addEventListener('logs', e => {
        const lines = JSON.parse(e.data);
        _ffLogBuf = _ffLogBuf.concat(lines).slice(-200);
        if(currentLogTab==='ffmpeg'){
          const a=document.getElementById('logArea');
          a.innerHTML=_ffLogBuf.map(l=>`<div class="log-line">${l.replace(/</g,'&lt;')}</div>`).join('');
          a.scrollTop=a.scrollHeight;
        }
      });
      _sse.onerror = ()=>{ /* auto-reconnect handled by browser */ };
    }catch(e){ console.warn('SSE failed, falling back to polling', e); }
  }
  function closeSse(){ if(_sse){_sse.close(); _sse=null;} }
  function applyStats(s, meta){
    if(!meta.streaming){ renderStatsIdle(); return; }
    const fps=Math.round(s.fps||0);
    sv('svFps',fps||'—'); sv('svBitrate',s.bitrate?s.bitrate.replace('kbits/s','k'):'—');
    sv('svSpeed',s.speed||'—'); sv('svFrames',s.frames?s.frames.toLocaleString():'—');
    sv('svDropped',s.dropped||0); sv('svSize',s.size_kb?fmtKB(s.size_kb):'—');
    document.getElementById('stDropped').className='stat-tile'+(s.dropped>0?' danger':'');
    fpsHistory.push(fps); if(fpsHistory.length>60)fpsHistory.shift(); drawSparkline();
    document.getElementById('statsIdleBadge').style.display='none';
  }

  // (Hotkeys live in the main keyboard-shortcuts handler above)

  async function skipClip(){
    try{
      const r = await apiFetch('/api/stream/skip',{method:'POST'});
      const d = await r.json();
      if(d.error) log('Skip: '+d.error,'error');
      else log(`Skipped → clip #${(d.index||0)+1}`,'info');
    }catch(e){ log('Skip error: '+e.message,'error'); }
  }

  // ── Drop a folder path onto the scan box ───────────────────────────────
  window.addEventListener('DOMContentLoaded', () => {
    const f = document.getElementById('folderInput');
    if(!f) return;
    f.addEventListener('dragover', e => { e.preventDefault(); f.style.borderColor='var(--accent)'; });
    f.addEventListener('dragleave', () => { f.style.borderColor=''; });
    f.addEventListener('drop', e => {
      e.preventDefault(); f.style.borderColor='';
      const txt = e.dataTransfer.getData('text/plain') || e.dataTransfer.getData('text/uri-list');
      if(txt){ f.value = txt.replace(/^file:\/\//,'').trim(); log('Folder path dropped','info'); }
    });
  });

  let _startInFlight = false;
  function _allStartButtons(){
    try{
      const set = new Set();
      document.querySelectorAll('[data-action="start-stream"], .js-start-stream, #startBtn').forEach(b=>set.add(b));
      return Array.from(set);
    }catch(_){ return []; }
  }
  function _setStartButtonsState(disabled, label){
    _allStartButtons().forEach(b=>{
      try{
        b.disabled = !!disabled;
        if(label) b.textContent = label;
      }catch(_){}
    });
  }
  function disableAllStartButtons(){ _setStartButtonsState(true); }
  function showStartingState(){ _setStartButtonsState(true, '⏳ Starting…'); }
  function _forceLiveUI(isLive){
    try{
      const stopB =document.getElementById('stopBtn');
      const skipB =document.getElementById('skipBtn');
      if(isLive){
        _setStartButtonsState(true, '▶ Live');
      } else if(!_startInFlight){
        _setStartButtonsState(false, '▶ Start Stream');
      } else {
        _setStartButtonsState(true, '⏳ Starting…');
      }
      if(stopB)  stopB.disabled  = !isLive;
      if(skipB)  skipB.disabled  = !isLive;
      const ss=document.getElementById('streamStatus'); if(ss) ss.textContent = isLive?'Live':'Idle';
      const dot=document.getElementById('dotStream'); if(dot) dot.className='status-dot '+(isLive?'live':'');
      const lp=document.getElementById('livePanel'); if(lp) lp.classList.toggle('show', !!isLive);
      const pr=document.getElementById('progressRow'); if(pr) pr.style.display = isLive?'':'none';
      try{ updateTopbarPill(!!isLive); }catch(_){}
      const dm=document.getElementById('dmStatus'); if(dm) dm.textContent = isLive?'LIVE':'Offline';
      const dmCard=document.getElementById('dmStatusCard'); if(dmCard) dmCard.className='metric ' + (isLive?'good':'');
      window._isStreaming = !!isLive;
    }catch(_){}
  }
  function _refreshAll(){
    try{ checkStatus(); }catch(_){}
    try{ if(window.refreshDashboard) window.refreshDashboard(); }catch(_){}
    try{ if(window.refreshHealthPage) window.refreshHealthPage(); }catch(_){}
    try{ if(window.refreshStreamStats) window.refreshStreamStats(); }catch(_){}
  }
  window.refreshStatus = checkStatus;

  // ── v3.9.15: Start-Stream progress panel + poller ──────────────────────
  let _progressPollTimer = null;
  function _spEl(id){ return document.getElementById(id); }
  function _showStartProgress(initialMsg){
    const p = _spEl('startProgressPanel'); if(!p) return;
    p.classList.remove('is-error','is-done');
    p.style.display = '';
    _renderProgress({active:true, phase:'starting', percent:0,
      message: initialMsg || 'Preparing stream…',
      current_clip:null, current_index:0, total:0, error:null});
  }
  function _hideStartProgressSoon(delay){
    setTimeout(()=>{ const p=_spEl('startProgressPanel'); if(p) p.style.display='none'; }, delay||3000);
  }
  function _renderProgress(pr){
    if(!pr) return;
    const p = _spEl('startProgressPanel'); if(!p) return;
    const pct = Math.max(0, Math.min(100, Number(pr.percent||0)));
    const fill = _spEl('spFill');   if(fill) fill.style.width = pct+'%';
    const pctEl= _spEl('spPct');    if(pctEl) pctEl.textContent = pct+'%';
    const step = _spEl('spStep');   if(step) step.textContent = _phaseLabel(pr.phase);
    const clip = _spEl('spClip');   if(clip) clip.textContent = pr.current_clip ? ('Clip: '+pr.current_clip) : '';
    const idx  = _spEl('spIdx');    if(idx)  idx.textContent  = (pr.total>0 && pr.current_index>0) ? (`Progress: ${pr.current_index} / ${pr.total} clips`) : '';
    const st   = _spEl('spStatus'); if(st)   st.textContent   = pr.message || '';
    const err  = _spEl('spError');
    if(pr.error){ if(err){ err.style.display=''; err.textContent = 'Error: '+pr.error; } p.classList.add('is-error'); }
    else if(err){ err.style.display='none'; err.textContent=''; }
    if(pr.phase==='live' || pct>=100) p.classList.add('is-done');
  }
  function _phaseLabel(ph){
    switch(ph){
      case 'validating': return 'Validating playlist';
      case 'syncing':    return 'Syncing playlist';
      case 'preparing':  return 'Checking cache';
      case 'normalizing':return 'Normalizing videos';
      case 'normalized': return 'Normalization complete';
      case 'building':   return 'Building FFmpeg command';
      case 'launching':  return 'Launching FFmpeg';
      case 'live':       return 'Stream started';
      case 'error':      return 'Failed';
      default:           return 'Preparing…';
    }
  }
  function _startProgressPolling(){
    _stopProgressPolling();
    const tick = async ()=>{
      try{
        const r = await fetch('/api/stream/start-progress', {cache:'no-store'});
        if(!r.ok) return;
        const d = await r.json().catch(()=>null);
        if(d && d.progress) _renderProgress(d.progress);
      }catch(_){}
    };
    tick();
    _progressPollTimer = setInterval(tick, 500);
  }
  function _stopProgressPolling(){
    if(_progressPollTimer){ clearInterval(_progressPollTimer); _progressPollTimer = null; }
  }

  let _stopInFlight = false;
  async function startStream(){
    console.log('[START] user requested start', Date.now());
    if(_startInFlight){
      console.log('[START] ignored because startInProgress');
      try{ toast('Already starting…','info'); }catch(_){}
      return;
    }
    if(window._isStreaming){
      console.log('[START] ignored because already live');
      try{ toast('Stream already running','info'); }catch(_){}
      _forceLiveUI(true);
      return;
    }
    // LOCK FIRST — before any await/log/sync, so rapid clicks cannot enter twice.
    _startInFlight = true;
    disableAllStartButtons();
    showStartingState();
    _showStartProgress('Preparing stream…');
    _startProgressPolling();
    try{
      if(!playlist.length){
        log('Playlist is empty!','error');
        try{ toast('Playlist is empty','error'); }catch(_){}
        _renderProgress({active:false,phase:'error',percent:0,message:'Playlist is empty',error:'Playlist is empty'});
        return;
      }
      // Re-check status with backend before any heavy work to avoid duplicate Starts.
      try{
        const sr = await fetch('/api/status').then(r=>r.json()).catch(()=>null);
        if(sr && sr.streaming){
          console.log('[START] backend reports already streaming');
          try{ toast('Stream already running','info'); }catch(_){}
          _forceLiveUI(true);
          _renderProgress({active:false,phase:'live',percent:100,message:'Stream already running'});
          _hideStartProgressSoon();
          return;
        }
      }catch(_){}
      const synced = await syncPlaylistToBackend();
      if(!synced){
        log('Aborting start — backend playlist not in sync','error');
        _renderProgress({active:false,phase:'error',percent:0,message:'Playlist sync failed',error:'Playlist sync failed'});
        return;
      }
      const settings=getStreamSettings();
      if(settings.resolution==='auto') log(`Auto resolution: ${playlist[0].info.width}×${playlist[0].info.height}`,'info');
      if(settings.normalize_before_live){
        log(`Preparing videos… Normalizing ${playlist.length} clip(s) (first run may take a while; cache is reused next time).`,'info');
        log('Video Stability: ON · Mode: '+(settings.fit_mode||'fit')+' · Canvas locked','info');
      } else {
        log('Normalize Before Live OFF — using original sources (zoom artifact may return).','warn');
      }
      try{ toast('Starting stream…','info'); }catch(_){}
      log('Starting stream...','info');
      console.log('[START] POST /api/stream/start');
      const r=await apiFetch('/api/stream/start',{method:'POST',
        body:{...settings,fps:parseInt(settings.fps),buffer_size:parseInt(settings.buffer_size),playlist}});
      let d={}; try{ d=await r.json(); }catch(_){}
      if(r.ok && d && d.already_running){
        try{ toast('Stream already running','info'); }catch(_){}
        log('Stream already running','info');
        _forceLiveUI(true);
        _renderProgress({active:false,phase:'live',percent:100,message:'Stream already running'});
        _hideStartProgressSoon();
        startFFmpegLogPoll(); startStatsPolling();
        _refreshAll();
        return;
      }
      if(!r.ok || d.error || d.success===false){
        const msg = (d && d.error) ? String(d.error) : ('HTTP '+r.status);
        if(r.status===400 && /already/i.test(msg)){
          // Legacy backend fallback — surface as info, not error.
          try{ toast('Stream already running','info'); }catch(_){}
          log('Stream already running','info');
          _forceLiveUI(true);
          _renderProgress({active:false,phase:'live',percent:100,message:'Stream already running'});
          _hideStartProgressSoon();
          startFFmpegLogPoll(); startStatsPolling();
          _refreshAll();
          return;
        }
        log('Error: '+msg,'error');
        try{ toast('Start failed: '+msg,'error'); }catch(_){}
        if(/normaliz/i.test(msg)) log('Normalization failed: '+msg,'error');
        _renderProgress({active:false,phase:'error',percent:0,message:'Start failed',error:msg});
        return;
      }
      try{ document.getElementById('liveRes').textContent=d.resolution||''; }catch(_){}
      log(`Stream started ✓  ${d.resolution||''} · PID ${d.pid||'?'}`+(d.shuffled?' · Shuffled 🔀':''),'success');
      try{ toast('Stream started','success'); }catch(_){}
      if(selectedAudioTrack) log('External audio active','warn');
      if(settings.rtmp_url) log('RTMP output active 🔴','warn');
      if(settings.text_overlay) log(`Overlay: "${settings.text_overlay}" (${settings.overlay_pos.toUpperCase()})`,'info');
      if(settings.vf_grayscale) log('Filter: Grayscale ON','info');
      const fActive = settings.vf_brightness!==0 || settings.vf_contrast!==1 || settings.vf_saturation!==1;
      if(fActive) log(`Filters: brightness=${settings.vf_brightness} contrast=${settings.vf_contrast} saturation=${settings.vf_saturation}`,'info');
      _forceLiveUI(true);
      _renderProgress({active:false,phase:'live',percent:100,message:'Stream started'});
      _hideStartProgressSoon();
      startFFmpegLogPoll(); startStatsPolling();
      _refreshAll();
    }catch(e){
      log('Connection error: '+e.message,'error');
      try{ toast('Connection error: '+e.message,'error'); }catch(_){}
      _renderProgress({active:false,phase:'error',percent:0,message:'Connection error',error:e.message});
    }
    finally{
      _startInFlight = false;
      _stopProgressPolling();
      // /api/status is the source of truth; let checkStatus repaint buttons.
      if(!window._isStreaming){ _setStartButtonsState(false, '▶ Start Stream'); }
      setTimeout(()=>{ try{ checkStatus(); }catch(_){} }, 400);
    }
  }


  async function stopStream(){
    if(_stopInFlight){ try{ toast('Already stopping…','info'); }catch(_){} return; }
    if(!window._isStreaming){ try{ toast('Not streaming','info'); }catch(_){} return; }
    _stopInFlight = true;
    const sb=document.getElementById('stopBtn');
    const origTxt = sb ? sb.textContent : '';
    if(sb){ sb.disabled=true; sb.textContent='⏳ Stopping…'; }
    log('Stopping...','info');
    try{
      await apiFetch('/api/stream/stop',{method:'POST'});
      log('Stream stopped','success');
      stopFFmpegLogPoll(); stopUptimeTimer(); stopStatsPolling();
      window._isStreaming=false; renderPlaylist();
      await checkStatus(); renderHistory();
    }catch(e){log('Error: '+e.message,'error');}
    finally{
      _stopInFlight = false;
      if(sb){ sb.textContent = origTxt || '⏹ Stop'; }
    }
  }


  // ── Scheduler ─────────────────────────────────────────────────────────────
  document.querySelectorAll('.day-btn').forEach(b=>b.addEventListener('click',()=>b.classList.toggle('active')));
  function getSelectedDays(){return[...document.querySelectorAll('.day-btn.active')].map(b=>parseInt(b.dataset.d));}

  async function addScheduleJob(){
    const name=document.getElementById('schedName').value.trim();
    const time=document.getElementById('schedTime').value;
    const days=getSelectedDays();
    if(!days.length){log('Select at least one day','error');return;}
    const d=await(await apiFetch('/api/scheduler/add',{method:'POST',
      body:{name,time,days,params:{...getStreamSettings(),fps:parseInt(document.getElementById('fpsSelect').value),buffer_size:parseInt(document.getElementById('bufferSelect').value),playlist}}})).json();
    if(d.error){log('Scheduler error: '+d.error,'error');return;}
    log(`Scheduled: "${d.job.name}" at ${d.job.time}`,'success');
    document.getElementById('schedName').value=''; renderScheduleJobs();
  }
  async function renderScheduleJobs(){
    try{
      const d=await(await fetch('/api/scheduler')).json();
      const el=document.getElementById('schedJobList');
      if(!d.jobs.length){el.innerHTML='<div class="empty-state" style="padding:20px"><div class="icon" style="font-size:28px">⏰</div><p>No scheduled jobs yet</p></div>';document.getElementById('schedNextLabel').textContent='';return;}
      el.innerHTML=d.jobs.map(j=>`
        <div class="sched-job ${j.active?'':'inactive'}">
          <div class="sched-job-time">${esc(j.time)}</div>
          <div class="sched-job-info"><div class="sched-job-name">${esc(j.name)}</div><div class="sched-job-days">${j.days.map(x=>esc(DAY_NAMES[x]||'')).join(' · ')}</div></div>
          <div class="sched-toggle ${j.active?'on':''}" onclick="toggleJob(${j.id})"></div>
          <button class="icon-btn red" onclick="deleteJob(${j.id})">🗑</button>
        </div>`).join('');
      const next=nextJobTime(d.jobs.filter(j=>j.active));
      document.getElementById('schedNextLabel').textContent=next?`Next: ${next}`:'';
    }catch(e){}
  }
  function nextJobTime(jobs){
    if(!jobs.length) return '';
    const now=new Date(), nowM=now.getHours()*60+now.getMinutes(), nowD=(now.getDay()+6)%7;
    let best=null;
    for(const j of jobs){
      const[hh,mm]=j.time.split(':').map(Number), jM=hh*60+mm;
      for(let delta=0;delta<7;delta++){
        const d=(nowD+delta)%7; if(!j.days.includes(d)) continue;
        if(delta===0&&jM<=nowM) continue;
        const diff=delta*1440+jM-nowM;
        if(best===null||diff<best.diff) best={diff,label:`${DAY_NAMES[d]} ${j.time}`}; break;
      }
    }
    return best?best.label:'';
  }
  async function toggleJob(id){await apiFetch('/api/scheduler/toggle',{method:'POST',body:{id}});renderScheduleJobs();}
  async function deleteJob(id){await apiFetch('/api/scheduler/delete',{method:'POST',body:{id}});renderScheduleJobs();}

  // ── Utils ──────────────────────────────────────────────────────────────────
  function log(msg,t=''){
    const time=new Date().toLocaleTimeString('en-US');
    appLogs.push({m:`[${time}] ${msg}`,t}); if(appLogs.length>200) appLogs.shift();
    if(currentLogTab==='app') renderAppLogs();
  }
  function fmtDur(s){s=Math.floor(s);const h=Math.floor(s/3600),m=Math.floor((s%3600)/60),sec=s%60;return h?`${p(h)}:${p(m)}:${p(sec)}`:`${p(m)}:${p(sec)}`;}
  function p(n){return String(n).padStart(2,'0');}

  // ── Video Preview Modal ───────────────────────────────────────────────────
  let previewIdx = null;

  function openPreview(i) {
    const f = scannedFiles[i];
    previewIdx = i;
    document.getElementById('previewTitle').textContent = f.name;
    // Phase 6D: file:// is browser-blocked + unsafe; serve via gated HTTP endpoint.
    document.getElementById('previewVideo').src = '/api/media/preview?path=' + encodeURIComponent(f.path);
    document.getElementById('previewMeta').innerHTML = [
      `<span>⏱ ${esc(f.info.duration_str)}</span>`,
      `<span>📐 ${esc(f.info.width)}×${esc(f.info.height)}</span>`,
      `<span>💾 ${esc(f.info.size_str)}</span>`,
      `<span>${f.info.has_audio ? '🔊 Has audio' : '🔇 No audio'}</span>`,
    ].join('');
    // Update add button text to reflect if already in playlist
    const already = playlist.filter(v => v.path === f.path).length;
    document.getElementById('previewAddBtn').textContent =
      already ? `+ Add again (${already} in playlist)` : '+ Add to Playlist';
    document.getElementById('previewModal').classList.add('show');
    document.body.style.overflow = 'hidden';
  }

  function closePreviewModal() {
    const modal = document.getElementById('previewModal');
    modal.classList.remove('show');
    const vid = document.getElementById('previewVideo');
    vid.pause(); vid.src = '';
    document.body.style.overflow = '';
    previewIdx = null;
  }

  function closePreview(e) {
    if (e.target === document.getElementById('previewModal')) closePreviewModal();
  }

  function addFromPreview() {
    if (previewIdx !== null) {
      addToPlaylist(previewIdx);
      // Update button text
      const already = playlist.filter(v => v.path === scannedFiles[previewIdx].path).length;
      document.getElementById('previewAddBtn').textContent = `+ Add again (${already} in playlist)`;
    }
  }

  // Close preview with Escape key
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape' && document.getElementById('previewModal').classList.contains('show')) {
      closePreviewModal();
    }
  }, true);  // capture phase so it fires before the global keydown handler

  // ── Video Filters ─────────────────────────────────────────────────────────
  function updateFilter() {
    const b  = parseFloat(document.getElementById('fBrightness').value);
    const c  = parseFloat(document.getElementById('fContrast').value);
    const s  = parseFloat(document.getElementById('fSaturation').value);
    const gs = document.getElementById('grayscaleToggle').classList.contains('on');

    document.getElementById('fvBrightness').textContent = b.toFixed(2);
    document.getElementById('fvContrast').textContent   = c.toFixed(2);
    document.getElementById('fvSaturation').textContent = s.toFixed(2);

    // Show ACTIVE badge when any filter differs from default
    const active = b !== 0 || c !== 1 || s !== 1 || gs;
    document.getElementById('filterActiveBadge').classList.toggle('active', active);

    // Live-tint the sliders' track to give visual feedback
    const bPct = (b + 1) / 2 * 100;
    const cPct = c / 2 * 100;
    const sPct = s / 3 * 100;
    setSliderTrack('fBrightness', bPct, '#ffcc00');
    setSliderTrack('fContrast',   cPct, '#00d4ff');
    setSliderTrack('fSaturation', sPct, '#f472b6');
  }

  function setSliderTrack(id, pct, color) {
    const el = document.getElementById(id);
    if (!el) return;
    el.style.background = `linear-gradient(to right, ${color} ${pct}%, var(--border) ${pct}%)`;
  }

  function resetFilters() {
    document.getElementById('fBrightness').value = 0;
    document.getElementById('fContrast').value   = 1;
    document.getElementById('fSaturation').value = 1;
    document.getElementById('grayscaleToggle').classList.remove('on');
    updateFilter();
    log('Video filters reset', 'info');
  }

  function getFilterSettings() {
    return {
      vf_brightness: parseFloat(document.getElementById('fBrightness').value),
      vf_contrast:   parseFloat(document.getElementById('fContrast').value),
      vf_saturation: parseFloat(document.getElementById('fSaturation').value),
      vf_grayscale:  document.getElementById('grayscaleToggle').classList.contains('on'),
    };
  }

  // ── (17) Backup / Restore ──────────────────────────────────────────────────
  function exportBackup(){
    log('Exporting backup…','info');
    window.location.href = '/api/backup/export';
  }
  async function importBackup(input){
    const f = input.files[0]; input.value=''; if(!f) return;
    if(!confirm(`Restore from "${f.name}"? Current state will be overwritten.`)) return;
    const fd = new FormData(); fd.append('file', f);
    try{
      // FormData: do NOT set Content-Type — apiFetch leaves it to the browser.
      const r = await apiFetch('/api/backup/import', {method:'POST', body: fd});
      const d = await r.json();
      if(d.success){ log(`Restored ${d.count} files ✓`, 'success'); setTimeout(()=>location.reload(), 800); }
      else log('Restore failed: '+(d.error||'?'), 'warn');
    }catch(e){ log('Restore error: '+e,'warn'); }
  }

  // ── (19) Version / update checker ──────────────────────────────────────────
  async function checkVersion(){
    try{
      const r = await fetch('/api/version/check'); if(!r.ok) return;
      const d = await r.json();
      if(d.update_available){
        const b = document.getElementById('updateBadge');
        b.textContent = `⬆ v${d.latest}`; b.href = d.url || '#'; b.style.display='inline-flex';
        log(`Update available: v${d.current} → v${d.latest}`,'info');
      }
    }catch(e){}
  }

  // ── (26) Stream snapshot ───────────────────────────────────────────────────
  function openSnapshot(){
    const dev = document.getElementById('deviceSelect')?.value || '/dev/video10';
    const url = `/api/stream/snapshot.jpg?device=${encodeURIComponent(dev)}&t=${Date.now()}`;
    const w = window.open('', '_blank', 'width=520,height=320');
    if(!w){ window.location.href = url; return; }
    w.document.write(`<title>Snapshot</title><body style="margin:0;background:#000;display:flex;align-items:center;justify-content:center"><img src="${url}" style="max-width:100%;max-height:100vh"></body>`);
  }

  // ── (27) Silent-gap detection ──────────────────────────────────────────────
  async function silentCheck(path){
    log(`Scanning silence in ${path.split('/').pop()}…`,'info');
    try{
      const r = await apiFetch('/api/clip/silent-check', {method:'POST',
        body: {path, noise:'-30dB', min:1.0}});
      const d = await r.json();
      if(d.error){ log('Silent-check failed: '+d.error,'warn'); return; }
      const pct = Math.round((d.silent_ratio||0)*100);
      const msg = `🔈 ${path.split('/').pop()} — silent ${d.silent_total||0}s (${pct}%) · ${d.gaps.length} gaps`;
      log(msg, d.mostly_silent ? 'warn' : 'success');
      if(d.mostly_silent) alert('Warning: clip is >50% silent.\n'+msg);
    }catch(e){ log('Silent-check error: '+e,'warn'); }
  }

  checkVersion();
  init();

  // ─────────────────────────────────────────────────────────────
  // v3.9.0 — Dashboard shell: sidebar nav, topbar, toast, health
  // Lives in the same global scope as the legacy handlers.
  // ─────────────────────────────────────────────────────────────
  const DASH_PAGES = ['dashboard','live-studio','media-library','playlist','profiles',
                      'scheduler','recordings','backups','health','settings','logs'];
  const DASH_TITLES = {
    'dashboard':'Dashboard','live-studio':'Live Studio','media-library':'Media Library',
    'playlist':'Playlist Builder','profiles':'Profiles / Presets','scheduler':'Scheduler',
    'recordings':'Recordings','backups':'Backups','health':'System Health',
    'settings':'Settings','logs':'Logs'
  };
  function showPage(name){
    if(!DASH_PAGES.includes(name)) name='dashboard';
    document.querySelectorAll('.page').forEach(p=>p.classList.toggle('active', p.dataset.page===name));
    document.querySelectorAll('.sb-item').forEach(i=>i.classList.toggle('active', i.dataset.page===name));
    const t=document.getElementById('tbTitle'); if(t) t.textContent=DASH_TITLES[name]||name;
    try{ history.replaceState(null,'','#'+name); }catch(e){}
    if(name==='dashboard'){ refreshDashboard(); refreshHealthPage(); }
    if(name==='health')    refreshHealthPage();
    if(name==='logs')      fetchFFmpegLogs();
    if(window.innerWidth<=900){
      document.querySelector('.sidebar')?.classList.remove('open');
      document.getElementById('sbBackdrop')?.classList.remove('show');
    }
  }
  window.showPage = showPage;
  function toggleSidebar(){
    const sb=document.querySelector('.sidebar');
    if(window.innerWidth<=900){
      sb.classList.toggle('open');
      document.getElementById('sbBackdrop').classList.toggle('show', sb.classList.contains('open'));
    }else{
      sb.classList.toggle('collapsed');
      try{ localStorage.setItem('mario_sb_collapsed', sb.classList.contains('collapsed')?'1':'0'); }catch(e){}
    }
  }
  window.toggleSidebar = toggleSidebar;

  // v3.9.4: Video fit mode status badge
  function updateFitBadge(){
    const sel = document.getElementById('fitModeSelect');
    const badge = document.getElementById('fitModeBadge');
    if(!sel || !badge) return;
    const v = sel.value;
    if(v === 'fill'){
      badge.textContent = 'Fill / Crop (may crop)';
      badge.style.color = 'var(--warn,#f59e0b)';
    } else if(v === 'stretch'){
      badge.textContent = 'Stretch (distorts)';
      badge.style.color = 'var(--err,#ef4444)';
    } else {
      badge.textContent = 'Fit / No Zoom';
      badge.style.color = 'var(--accent,#22d3ee)';
    }
  }
  window.updateFitBadge = updateFitBadge;
  document.addEventListener('DOMContentLoaded', updateFitBadge);

  // v3.9.8: Normalize-before-live badge — reflects toggle state in UI.
  function updateNormalizeBadge(){
    const tog   = document.getElementById('normalizeBeforeLiveToggle');
    const badge = document.getElementById('normalizeBadge');
    if(!tog || !badge) return;
    const on = tog.classList.contains('on');
    badge.textContent = on ? 'ON' : 'OFF';
    badge.style.color = on ? '#22c55e' : 'var(--text-dim)';
  }
  window.updateNormalizeBadge = updateNormalizeBadge;
  document.addEventListener('DOMContentLoaded', updateNormalizeBadge);

  // Toast (replaces blocking alerts where called explicitly)
  function toast(msg, kind){
    const stack=document.getElementById('toastStack'); if(!stack) return;
    const t=document.createElement('div'); t.className='toast '+(kind||'info'); t.textContent=msg;
    stack.appendChild(t);
    setTimeout(()=>{ t.style.opacity='0'; t.style.transition='opacity .2s'; setTimeout(()=>t.remove(),250); }, 3200);
  }
  window.toast = toast;

  // Topbar stream status pill — driven by checkStatus()
  function updateTopbarPill(streamingNow, errMsg){
    const p=document.getElementById('tbStreamPill'); if(!p) return;
    p.classList.remove('live','err','starting');
    if(errMsg){ p.classList.add('err'); p.lastChild.nodeValue=' Error'; return; }
    if(streamingNow){ p.classList.add('live'); p.lastChild.nodeValue=' Live'; }
    else            { p.lastChild.nodeValue=' Offline'; }
  }
  window.updateTopbarPill = updateTopbarPill;

  // Dashboard summary
  async function refreshDashboard(){
    try{
      const [st, pl, info] = await Promise.all([
        fetch('/api/status').then(r=>r.json()).catch(()=>({})),
        fetch('/api/playlist').then(r=>r.json()).catch(()=>({playlist:[]})),
        fetch('/api/app/info').then(r=>r.json()).catch(()=>({})),
      ]);
      const set=(id,v)=>{const e=document.getElementById(id); if(e) e.textContent=v;};
      set('dmStatus', st.streaming?'LIVE':'Offline');
      const dmCard=document.getElementById('dmStatusCard');
      if(dmCard){ dmCard.className='metric ' + (st.streaming?'good':''); }
      set('dmPlaylist', (pl.playlist||[]).length);
      set('dmDevice', (st.device||'—'));
      set('dmCurrent', st.current_file ? st.current_file.split('/').pop() : '—');
      set('dmRtmp', st.rtmp_outputs!=null ? st.rtmp_outputs : (st.rtmp?'1':'0'));
      set('dmRec', st.recording ? 'ON' : 'OFF');
      set('dmVer', info.version || '—');
      set('dmDisk', info.disk_free_mb!=null ? (info.disk_free_mb+' MB') : '—');
    }catch(e){ /* silent */ }
  }
  window.refreshDashboard = refreshDashboard;

  // Dashboard health checklist + System Health page
  async function refreshHealthPage(){
    try{
      const [h, info, st] = await Promise.all([
        fetch('/api/health').then(r=>r.json()).catch(()=>({})),
        fetch('/api/app/info').then(r=>r.json()).catch(()=>({})),
        fetch('/api/status').then(r=>r.json()).catch(()=>({})),
      ]);
      h._streaming = !!st.streaming;
      renderHealthList('dhHealth', h);
      renderHealthList('shHealth', h);
      const setT=(id,v)=>{const e=document.getElementById(id); if(e) e.textContent=v;};
      setT('shVersion', info.version || '—');
      setT('shPython',  info.python  || '—');
      setT('shFfmpeg',  info.ffmpeg  || '—');
      setT('shUser',    info.user    || '—');
      setT('shDbPath',  info.db_path || '—');
      setT('shRecPath', info.recordings_path || '—');
      setT('shScanRoots', (info.scan_roots||[]).join(', ') || '—');
      setT('shAuth',    info.auth_enabled?'enabled':'DISABLED');
      setT('shCsrf',    info.csrf_enabled?'enabled':'disabled');
      setT('shDisk',    h.disk_free_mb!=null ? (h.disk_free_mb+' MB free') : '—');
      // Deployment / autostart / restart-controls
      const dep = info.deployment || h.deployment || {};
      setT('shDeployMode', (dep.deployment_mode||'unknown').toUpperCase());
      setT('shSystemdUnit', dep.systemd_unit || (dep.in_docker?'docker compose':'—'));
      const ae = dep.autostart_enabled;
      setT('shAutostart', ae===true?'Yes':(ae===false?'No':'Unknown'));
      const card = document.getElementById('shAutostartCard');
      if(card){ card.className = 'metric ' + (ae===true?'good':(ae===false?'warn':'')); }
      setT('shRestartCtrl', dep.restart_app_supported?'Available':'Unavailable');
      setT('shRebootEnabled', dep.reboot_allowed?'Yes':'No');
      const btnA=document.getElementById('btnRestartApp');
      const btnR=document.getElementById('btnRebootServer');
      const hint=document.getElementById('shCtrlHint');
      if(btnA){
        btnA.disabled = !dep.restart_app_supported;
        btnA.title = dep.restart_app_supported ? 'Restart Mario service' : (dep.restart_app_reason||'Unavailable');
      }
      if(btnR){
        btnR.disabled = !dep.reboot_allowed;
        btnR.title = dep.reboot_allowed ? 'Reboot the host server' : 'Set MARIO_ALLOW_SERVER_REBOOT=1 to enable';
      }
      if(hint){
        const parts = [];
        if(!dep.restart_app_supported && dep.restart_app_reason) parts.push(dep.restart_app_reason);
        if(!dep.reboot_allowed) parts.push('Reboot disabled (MARIO_ALLOW_SERVER_REBOOT≠1).');
        hint.textContent = parts.join(' · ');
      }
    }catch(e){}
  }
  window.refreshHealthPage = refreshHealthPage;

  // ── Server Controls ────────────────────────────────────────────────────────
  async function restartApp(){
    const btn=document.getElementById('btnRestartApp');
    if(btn && btn.disabled) return;
    if(!confirm('Restart the Mario Stream app service?\n\nThis will interrupt the stream.')) return;
    try{
      const r=await apiFetch('/api/system/restart-app',{method:'POST',body:{}});
      const d=await r.json().catch(()=>({}));
      if(d.success){ toast('Restarting app… page may briefly disconnect.','warn'); log('Restart App scheduled','warn'); }
      else        { toast('Restart failed: '+(d.error||r.status),'error'); log('Restart failed: '+(d.error||r.status),'error'); }
    }catch(e){ toast('Restart error: '+e.message,'error'); }
  }
  window.restartApp = restartApp;
  async function rebootServer(){
    const btn=document.getElementById('btnRebootServer');
    if(btn && btn.disabled){ toast('Reboot disabled. Set MARIO_ALLOW_SERVER_REBOOT=1.','warn'); return; }
    const typed = prompt('DANGER: This will reboot the VPS and interrupt the stream.\n\nType REBOOT (uppercase) to confirm:');
    if(typed !== 'REBOOT'){ toast('Reboot cancelled.','info'); return; }
    try{
      const r=await apiFetch('/api/system/reboot-server',{method:'POST',body:{confirm:'REBOOT'}});
      const d=await r.json().catch(()=>({}));
      if(d.success){ toast('Server rebooting in ~2s…','warn'); log('Reboot scheduled','warn'); }
      else        { toast('Reboot failed: '+(d.error||r.status),'error'); log('Reboot failed: '+(d.error||r.status),'error'); }
    }catch(e){ toast('Reboot error: '+e.message,'error'); }
  }
  window.rebootServer = rebootServer;

  function renderHealthList(targetId, h){
    const wrap=document.getElementById(targetId); if(!wrap) return;
    const checks = (h && h.checks) || {};
    const ffmpegOk = checks.ffmpeg ? !!checks.ffmpeg.ok : !!h.ffmpeg;
    const v4l2Ok   = checks.v4l2loopback ? !!checks.v4l2loopback.ok : !!h.v4l2_ok;
    const diskOk   = checks.disk ? !!checks.disk.ok : !h.disk_warning;
    const items=[
      {k:'FFmpeg installed',          ok:ffmpegOk,
       text: checks.ffmpeg && checks.ffmpeg.detail ? checks.ffmpeg.detail : ''},
      {k:'v4l2 device available',     ok:v4l2Ok,
       text: checks.v4l2loopback && checks.v4l2loopback.detail ? checks.v4l2loopback.detail : ''},
      {k:'Auth enabled',              ok:!!h.auth_enabled, warn:!h.auth_enabled},
      {k:'CSRF enabled',              ok:!!h.csrf_enabled, warn:!h.csrf_enabled},
      {k:'Disk space',                ok:diskOk,  warn:!diskOk,
       text:(h.disk_free_mb!=null?h.disk_free_mb+' MB free':(checks.disk&&checks.disk.detail)||'unknown')},
      // Stream daemon / Stream Engine: neutral Idle when not streaming,
      // green Live when streaming, red only if backend explicitly reports failure.
      (function(){
        const isLive = !!h._streaming;
        const daemonFailed = (h.ok===false) || (checks.stream && checks.stream.ok===false);
        return {
          k:'Stream Engine',
          ok:  isLive && !daemonFailed,
          warn: !isLive && !daemonFailed,   // Idle = neutral/warn (yellow dot), not red
          text: daemonFailed ? 'failed' : (isLive ? 'Live' : 'Idle')
        };
      })(),
    ];
    wrap.innerHTML='';
    items.forEach(it=>{
      const d=document.createElement('div');
      d.className='health-item '+(it.ok?'ok':(it.warn?'warn':'bad'));
      const dot=document.createElement('div'); dot.className='h-dot'; d.appendChild(dot);
      const lbl=document.createElement('div'); lbl.style.flex='1';
      lbl.textContent=it.k + (it.text?(' — '+it.text):'');
      d.appendChild(lbl);
      wrap.appendChild(d);
    });
  }

  // Init dashboard chrome — runs after legacy init()
  function initDash(){
    // sidebar collapsed state
    try{ if(localStorage.getItem('mario_sb_collapsed')==='1')
         document.querySelector('.sidebar')?.classList.add('collapsed'); }catch(e){}
    // sidebar item clicks
    document.querySelectorAll('.sb-item').forEach(el=>{
      el.addEventListener('click', ()=>showPage(el.dataset.page));
    });
    // backdrop closes mobile drawer
    const bd=document.getElementById('sbBackdrop');
    if(bd) bd.addEventListener('click', ()=>{ document.querySelector('.sidebar')?.classList.remove('open'); bd.classList.remove('show'); });
    // initial page from hash or dashboard
    const initial=(location.hash||'').replace('#','') || 'dashboard';
    showPage(initial);
    // dashboard refresh tick
    setInterval(()=>{
      if(document.querySelector('.page.active')?.dataset.page==='dashboard') refreshDashboard();
    }, 4000);
  }
  // Defer until legacy init has wired status polling
  if(document.readyState==='loading'){
    document.addEventListener('DOMContentLoaded', initDash);
  }else{ initDash(); }
