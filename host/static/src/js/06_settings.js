// SPDX-License-Identifier: AGPL-3.0-or-later

/* 列表按钮统一委托（#list 重建也不失效）：data-act + data-sid。
   内联 onclick 随 innerHTML 重建被换掉 → 用户按下到抬起之间元素被换，点击就丢了。 */
document.getElementById('list').addEventListener('click',function(e){
  /* 按钮（data-act）和整行点按（data-csid）都在这一个委托里分派：
     行上不再放内联 onclick——按钮在行里，内联行处理先于本委托触发，
     点 ⓘ/⌨ 这些小按钮时会先掉进行 onclick 直接进会话。 */
  const b=e.target.closest('[data-act]');
  if(b){
    e.preventDefault();
    const sid=b.getAttribute('data-sid');
    switch(b.getAttribute('data-act')){
      case 'detail': showDetail(sid); break;
      case 'hostterm': hostTermOpen(sid); break;
      case 'cmd': cmdSheet(sid); break;
      case 'migrate': migrateOne(b,sid); break;
      case 'resume': resumeExt(b,sid); break;
      case 'respawn': act('/respawn/'+sid); break;
      case 'remove': confirmRemove(sid); break;
      case 'kill': act('/kill/'+sid); break;
      case 'new': sessNew(b,b.getAttribute('data-cwd')); break;
      case 'fork': forkSess(b,sid,b.getAttribute('data-cwd')); break;
      case 'del': delSess(sid); break;
    }
    return;
  }
  const w=e.target.closest('[data-hostterm]');
  if(w){ hostTermOpen(w.getAttribute('data-hostterm')); return; }
  const rp=e.target.closest('[data-respawn]');
  if(rp){ respawnOpen(rp.getAttribute('data-respawn')); return; }
  const r=e.target.closest('[data-csid]');
  if(r) switchTo(r.getAttribute('data-csid'));
});
/* 收件箱的卡片不在 #list 里（页签下的独立面板）：必须挂 document，
   挂在 #list 上就算点到了也收不到事件（2026-11-14：待你决定点了没反应）。 */
document.addEventListener('click',function(e){
  const ibv=e.target.closest('[data-ibview]');
  if(ibv){ const v=ibv.getAttribute('data-ibview');
           IB_VW=(v==='hid'||v==='acc')?v:''; inboxPaint(); return; }
  if(e.target.closest('[data-ibnew]')){ taskNewDlg(); return; }
  /* 搜索（全文检索）：工作台上的输入框已经填了词就直接搜，空词进子页等输入；
     结果里点开直接进那个会话 */
  if(e.target.closest('[data-ibfind]')){
    const w=document.getElementById('ibQ'); if(w) IB_Q=(w.value||'').trim();
    FIND_Q=IB_Q; IB_VW='find'; inboxPaint();
    if(FIND_Q) findRun();
    else{ const q=document.getElementById('findQ'); if(q) q.focus(); }
    return;
  }
  if(e.target.closest('[data-findgo]')){ findRun(); return; }
  const fsid=e.target.closest('[data-findsid]');
  if(fsid){ IB_VW=''; switchTo(fsid.getAttribute('data-findsid')); return; }
  const tv=e.target.closest('[data-tv]');
  if(tv){ taskViewAct(tv.getAttribute('data-tv'), tv.getAttribute('data-sid')); return; }
  /* 台账行内产出物（B4）：先于「进会话」分支——否则点了只会切会话 */
  const ar=e.target.closest('[data-arts]');
  if(ar){ artsPeek(ar.getAttribute('data-arts')); return; }
  /* 收件箱工件卡：点了开预览——ibArtOpen 自己先切回所属会话再取文件 */
  const art=e.target.closest('[data-art]');
  if(art){ ibArtOpen(art.getAttribute('data-art'), art.getAttribute('data-art-name'), art.getAttribute('data-art-sid')); return; }
  /* 别的机器上的任务（B2.1）：本机没有这个会话，点了是「切到那台机器」，
     先问一句——换台会把当前整个页面换成另一台机器的。 */
  const rem=e.target.closest('[data-remote-dev]');
  if(rem){
    const dev=rem.getAttribute('data-remote-dev');
    const nm=rem.getAttribute('data-remote-name')||'另一台机器';
    askConfirm('换台', '这个在「'+escHtml(nm)+'」上。切过去看？', '切过去', false,
      function(){ devGoto(dev); });
    return;
  }
  const pd=e.target.closest('[data-pend],[data-ledger]');
  const act=e.target.closest('[data-act]');
  if(act){  /* 台账验收按钮:从最近的卡片取 sid,自己处理,不落进下面的进会话分支 */
    const card=act.closest('[data-ledger]');
    const s=card?card.getAttribute('data-ledger'):'';
    if(act.getAttribute('data-act')==='accept') ledgerTaskAccept(s);
    else ledgerTaskReject(s);
    return;
  }
  const tcr=e.target.closest('[data-tcrun]');
  if(tcr){ testRun(tcr.getAttribute('data-tcrun')); return; }
  if(pd){
    const s=pd.getAttribute('data-pend')||pd.getAttribute('data-ledger');
    /* 派单中/失败的任务还没有会话（sid="task:<tid>"）：没有可进的地方 */
    if(s&&s.indexOf('task:')!==0) switchTo(s);
    return;
  }
  const dn=e.target.closest('[data-done]');
  if(dn){ switchTo(dn.getAttribute('data-done')); return; }
});
/* 搜索的输入框：回车就搜（手机键盘上的「搜索」也是回车）。
   工作台上的那个（ibQ）回车直接进结果页，不用先点「搜索」按钮。 */
document.addEventListener('keydown',function(e){
  if(e.key!=='Enter'||!e.target) return;
  if(e.target.id==='findQ'){ e.preventDefault(); findRun(); }
  else if(e.target.id==='ibQ'){
    e.preventDefault(); IB_Q=(e.target.value||'').trim();
    if(!IB_Q) return;
    FIND_Q=IB_Q; IB_VW='find'; inboxPaint(); findRun();
  }
});
/* 工作台几秒重画一次：先把草稿留在 IB_Q 里，重画完再填回去（见 inboxPaint） */
document.addEventListener('input',function(e){
  if(e.target&&e.target.id==='ibQ') IB_Q=e.target.value||'';
});
/* ── 设置（第 3 页）：语音/网络/账号/设备。
      有壳写壳配置（CB.getCfg/setCfg），无壳落 localStorage(cb_cfg)；
      账号/设备走中继接口（XHR——老 WebView 的 fetch 不落 Set-Cookie，
      改密码会重签会话 cookie，必须 XHR）；LAN 直连没有 /r/* 接口，对应卡片自动隐藏。 ── */
function vsCfg(){
  let sh={};
  try{ if(window.CB && CB.getCfg) sh=JSON.parse(CB.getCfg()); }catch(e){}
  let ls={};
  try{ ls=JSON.parse(localStorage.getItem('cb_cfg')||localStorage.getItem('cbvs')||'{}'); }catch(e){}
  return Object.assign({}, ls, sh);   // 壳键为准；壳丢掉的新键（旧壳）由 localStorage 兜住
}
function vsSave(c){
  try{ localStorage.setItem('cb_cfg', JSON.stringify(c)); }catch(e){}
  try{ if(window.CB && CB.setCfg) CB.setCfg(JSON.stringify(c)); }catch(e){}
}
function seg(id,on){ const b=document.getElementById(id); if(!b) return; b.classList[on?'add':'remove']('on'); b.classList.add('gray'); }
function swSet(id,on){ const e=document.getElementById(id); if(e) e.className='sw'+(on?' on':''); }
/* 编码模式：默认跟产品线（开发机按线算，prefLoad 带回），这里只做显式改写。
   乐观翻：点了立刻变，失败回读真值（同 prefSend 的路子）。 */
function codingToggle(){
  const to=!codingOn();
  CODING_ON=to;
  swSet('codingSw',to);
  xj('/api/ext_pref',{method:'POST',body:{dev:devId(),coding_on:to}}).then(function(r){
    if(r.json&&r.json.coding) CODING_ON=!!r.json.coding.on;
    swSet('codingSw',codingOn());
    toast(to?'编码模式已开':'编码模式已关');
  }).catch(function(){ toast('设置没保存上（网络）',true); prefLoad(); });
}
function renderVoice(cfg){
  const c=cfg||vsCfg();
  /* 两个音量滑条先回填、先显示：它们以前只在 vsDispRefresh 里被放出来，
     那一串刷新里任何一步在某个壳/WebView 上抛错，滑条就永远停在 display:none
     （2026-11-09 安卓反馈「音量调节都不见了」）。现在谁出错都不影响它们。 */
  try{ vsVolSync(); }catch(e){ vsErr('vol',e); }
  seg('vsHold',c.mode!=='toggle'); seg('vsTog',c.mode==='toggle');
  seg('vsAutoOn',c.autosend===true); seg('vsAutoOff',c.autosend!==true);
  /* 自动听写要新壳（CB.autoListenStart）；老壳/浏览器整行藏掉，别给个按了没反应的开关。
     免费版同理：整行留着但置灰，换成「开通解锁」入口。 */
  autoGateSync();
  swSet('swNAuto',c.autolisten===true);
  nRender();
  prefLoad();     // 「语音里说明在做什么」存在开发机，打开设置时取一次真值

  try{ if(window.CB&&CB.setKeepOn) CB.setKeepOn(c.keepon===true); }catch(e){}
  try{ applyEyeClass(c.eye===true); }catch(e){ vsErr('eye',e); }
  /* 一卡一拍：某一块在壳里报错（老 WebView / 缺接口）不许连坐下面所有卡片。 */
  [vsNetRefresh,vsDirectRefresh,vsDispRefresh,vsRunRefresh,vsVerRefresh,hsShowCached,
   planRefresh,acctLoad].forEach(function(fn){
    try{ fn(); }catch(e){ vsErr(fn.name||'?',e); }
  });
  const ps=document.getElementById('pushSt');
  if(ps) ps.textContent=c.stream===false?'已关（始终轮询）':(PUSH.st==='on'?'推送中':'轮询');
}
/* 设置页某一块初始化失败：把哪一块、什么错落进开发机日志（SEx 一行）。
   不抛出去——一块报错不该让后面的卡片全不渲染（音量滑条就是这么消失的）。 */
function vsErr(k,e){
  try{ window.__cbDbg && window.__cbDbg('SEx '+k+' '+String((e&&e.message)||e).slice(0,30),40); }catch(_){}
}
/* 分组头：组内卡片全隐藏（浏览器模式没有壳接口）时，连标题一起收掉，不留空标题 */
function grpSync(){
  var g=[['grpConn','boxConn'],['grpRem','boxRem'],['grpLook','boxLook'],['grpAcct','boxAcct'],['grpAbout','boxAbout']];
  for(var i=0;i<g.length;i++){
    var h=document.getElementById(g[i][0]), box=document.getElementById(g[i][1]);
    if(!h||!box) continue;
    var cs=box.querySelectorAll('.card'), vis=false;
    for(var j=0;j<cs.length;j++) if(cs[j].style.display!=='none') vis=true;
    var want=vis?'':'none';
    if(h.style.display!==want) h.style.display=want;   // 先比对再写，防 MutationObserver 自激
  }
}
/* ── T3 式分组折叠（窗口核心 Phase 1）：组头点一下收/放，记忆在 cb_setgrp。
      默认全展开；只记显式折叠过的组。组头隐藏（组内全空）由 grpSync 管，互不干扰。 ── */
var SET_GROUPS=['Conn','Rem','Look','Acct','About'];   /* 键=组 id 的后半（grpConn/boxConn） */
function grpToggle(k){
  var h=document.getElementById('grp'+k), b=document.getElementById('box'+k);
  if(!h||!b) return;
  var closed=h.className.indexOf('closed')>=0;
  if(closed){ h.className=h.className.replace(/\s*closed\s*/g,' '); b.style.display=''; }
  else{ h.className+=' closed'; b.style.display='none'; }
  var st={}; try{ st=JSON.parse(localStorage.getItem('cb_setgrp')||'{}')||{}; }catch(e){}
  st[k.toLowerCase()]=!closed;              /* 存储键统一小写，不随 id 大小写变 */
  try{ localStorage.setItem('cb_setgrp',JSON.stringify(st)); }catch(e){}
}
function grpInit(){
  var st={}; try{ st=JSON.parse(localStorage.getItem('cb_setgrp')||'{}')||{}; }catch(e){}
  for(var i=0;i<SET_GROUPS.length;i++){
    var k=SET_GROUPS[i];
    if(!st[k.toLowerCase()]) continue;
    var h=document.getElementById('grp'+k), b=document.getElementById('box'+k);
    if(h&&b){ h.className+=' closed'; b.style.display='none'; }
  }
}
try{ grpInit(); }catch(e){}
try{ new MutationObserver(grpSync).observe(document.getElementById('pgVoice'),
  {subtree:true,attributes:true,attributeFilter:['style'],childList:true}); }catch(e){}
function vsSet(k,v){ const c=vsCfg(); c[k]=v; vsSave(c); renderVoice(c); show('已保存'); }
/* ── 屏幕显示：亮度/常亮/护眼（有壳才显示；亮度只管本 App 窗口） ──
   人眼感知非线性：s² 映射让低段更细腻；背光有硬件下限，滑条 <40 再叠加页面级
   黑色压暗层（护眼层下面那层），最低档 ≈ 背光 2% + 85% 黑 → 比系统最暗更暗 */
var BRIGHT_SENT=-1;      // 我们最后一次写给壳的背光(0..1)：用来分辨「屏幕亮度是被人从外面改的」
function applyBright(s){
  s=Math.max(1,Math.min(100,parseInt(s,10)||100));
  const b=Math.max(0.02,(s/100)*(s/100));
  BRIGHT_SENT=b;
  try{ if(window.CB&&CB.setBrightness) CB.setBrightness(b); }catch(e){}
  const o=s<40?(40-s)/39*0.85:0;   // <40% 叠黑罩，暗到背光下限以下
  try{ if(window.CB&&CB.setDim) CB.setDim(o); }catch(e){}
  /* 壳有悬浮窗权限时压暗交给壳（连键盘/状态栏一起罩），页面就别再罩一层——
     两层叠加会让屏幕比滑条标的值暗得多，而且一层两层随页面状态跳来跳去。 */
  const shellDim=(window.CB&&CB.dimCap&&CB.dimCap()==='1');
  const oo=shellDim?0:o;
  const dm=document.getElementById('dimmer');
  if(dm){ dm.style.opacity=oo.toFixed(2); dm.style.display=oo>0?'block':'none'; }
  const el=document.getElementById('vsBright'); if(el) el.value=s;
}
/* restore 只在「用户亲手切到自动」时为 true：那一刻要把我们抢过的背光还回去。
   启动/回前台/进设置页一律不传 —— 自动档的本意是「别碰背光」。
   iOS 的 UIScreen.brightness 是**全局**的，每次唤醒都写一遍「App 启动时的亮度」
   会把用户后来在控制中心调的亮度、以及系统的感光自动亮度顶回去，
   表现就是「自动亮度过一会儿自己跳一下 / 感光不灵」。 */
function applyBrightAuto(c,restore){
  const el=document.getElementById('vsBright');
  if(c.brightAuto===true){          // 自动=跟着环境光走
    if(window.CB&&CB.setAutoBright){                 // 安卓：App 自己读光线传感器，不跳系统页
      try{ CB.setAutoBright(true); }catch(e){}
    }else if(restore===true&&window.CB&&CB.setBrightness){
      try{ CB.setBrightness(0); }catch(e){}          // iPhone：把背光交还系统
    }
    try{ if(window.CB&&CB.setDim) CB.setDim(0); }catch(e){}
    const dm=document.getElementById('dimmer'); if(dm) dm.style.display='none';
    if(el) el.disabled=true;
    BRIGHT_SENT=-1;                 // 没在接管背光：屏幕上的值都是系统的
  }else{
    if(window.CB&&CB.setAutoBright){ try{ CB.setAutoBright(false); }catch(e){} }
    if(el) el.disabled=false;
    applyBright(c.bright||100);
  }
  autoBrightHint();
}
var ISIOSE=/(iPhone|iPad|iPod)/.test(navigator.userAgent||'');
/* 「自动」档和系统设置对齐：
   · 安卓：App 自己读光线传感器调亮度（不跳系统页）；系统那个开关能静默写就顺手写上；
   · iPhone：那个开关没有公开 API，只能给指路文案。 */
function autoBrightHint(){
  const h=document.getElementById('vsAutoHint'); if(!h) return;
  if(vsCfg().brightAuto!==true){ h.style.display='none'; return; }
  if(window.CB&&CB.getAutoBright){
    let st=''; try{ st=String(CB.getAutoBright()||''); }catch(e){}
    if(st==='1'){ h.style.display='none'; return; }
    h.style.display=''; h.style.color='#838383';
    h.textContent=(st==='0')
      ? '系统的「自动调节亮度」没开，App 会自己感应环境光来调（点这里同步到系统）'
      : '读不到系统的「自动调节亮度」设置，App 会自己感应环境光来调';
    return;
  }
  if(ISIOSE){
    h.style.display=''; h.style.color='#838383';
    h.textContent='自动亮度由系统控制。若屏幕不随环境光变化，请到 iPhone「设置 → 辅助功能 → 显示与文字大小」里打开「自动亮度」。';
    return;
  }
  h.style.display='none';
}
/* 点提示 = 把系统那个开关也同步开（仅当已授权「修改系统设置」，全程不跳页）。
   没授权也没关系：屏幕已经由 App 自己感应调节，只是系统设置页里那个开关还是关的。 */
function autoBrightFix(){
  if(!(window.CB&&CB.syncSystemAutoBright)) return;
  let r=''; try{ r=String(CB.syncSystemAutoBright()||''); }catch(e){}
  toast(r==='1'?'已同步打开系统自动亮度':'系统开关需要授权才能改，App 已在自行调节');
  setTimeout(autoBrightHint,600);
}
/* 亮度双向同步：App 里改 → 系统跟着变（本来就是全局的）；系统/别处改 → App 里跟着变。
   以前只有单向，用户在控制中心调完亮度回到 App，滑条还显示旧值，看起来两边各说各话。
   自动档下我们不写背光，滑条灰掉但仍实时显示系统的值 —— 顺带成了「感光有没有在工作」的指示。 */
function brightSync(){
  if(!(window.CB&&CB.getBrightness)) return;
  if(SHELLBG||document.hidden) return;          // 后台不读
  let b; try{ b=parseFloat(CB.getBrightness()); }catch(e){ return; }
  if(!(b>=0)) return;                           // 读不到（部分机型跟随系统时报 -1）
  if(b>1) b=1;
  const s=Math.max(1,Math.min(100,Math.round(Math.sqrt(b)*100)||1));
  const el=document.getElementById('vsBright');
  if(vsCfg().brightAuto===true){ if(el) el.value=s; autoBrightHint(); return; }
  if(BRIGHT_SENT<0) return;                     // 还没接管过：以壳当前值为准，别乱写
  if(Math.abs(b-BRIGHT_SENT)<=0.03) return;     // 就是我们设的那个值
  const c=vsCfg(); c.bright=s; vsSave(c);       // 外面改过了：以系统为准，不顶回去
  applyBright(s);
}
setInterval(function(){ try{ brightSync(); }catch(e){} },2000);
function vsBrightAuto(){
  const c=vsCfg(); c.brightAuto=(c.brightAuto!==true);
  vsSave(c); applyBrightAuto(c,true);
  seg('vsBrightAuto',c.brightAuto===true); toast('已保存');
}
function vsDispRefresh(){
  const card=document.getElementById('vsDispCard');
  vsVolSync();          // 音量在「提醒音」卡里，跟亮度接口有没有无关
  if(!window.CB||!CB.setBrightness){ card.style.display='none'; return; }
  card.style.display='';
  const c=vsCfg();
  seg('vsBrightAuto',c.brightAuto===true);
  if(c.brightAuto===true) applyBrightAuto(c);           // 存过档的设置启动即回放，不等开设置页
  else if(c.bright) applyBright(c.bright);
  else{
    let b=100;   // 没存过：读壳当前值反推滑条（窗口亮度是 s² 映射过的）
    try{ b=Math.round(Math.sqrt(Math.max(0,parseFloat(CB.getBrightness())))*100); }catch(e){}
    if(!(b>0)||b>100) b=100;
    document.getElementById('vsBright').value=b;
  }
  swSet('vsKeepOn',c.keepon===true); swSet('vsEyeBtn',c.eye===true);
  keepHintSync();
}
/* 不限时长看额度（use_min_day=0），不看档位名：内部套餐/以后加的档也能对上 */
function planUnlimited(){
  var l=PLAND&&PLAND.ok&&PLAND.limits;
  return !!l&&!Number(l.use_min_day||0);
}
/* 常亮旁的说明暂时隐藏（用户要求先不提示）：节点留着，改回来只要恢复下面这行。
   事实是常亮不再额外计时——没人碰的轮询带 a=0，中继和开发机都不记分钟。 */
function keepHintSync(){
  const h=document.getElementById('vsKeepHint'); if(!h) return;
  h.style.display='none';
}
/* 启动即回放显示设置：以前只有进设置页才 applyBright，壳的压暗悬浮层却
   一启动就按 prefs 挂着 → 页面层和壳层两道黑罩时有时无，屏幕忽明忽暗。 */
function dispStartup(){
  const c=vsCfg();
  try{ if(window.CB&&CB.setKeepOn) CB.setKeepOn(c.keepon===true); }catch(e){}
  applyEyeClass(c.eye===true);
  if(c.brightAuto===true) applyBrightAuto(c);
  else if(c.bright) applyBright(c.bright);
  else{  // 没存过：跟随系统（背光不动），但把壳里可能残留的压暗层摘掉，保持一致
    try{ if(window.CB&&CB.setDim) CB.setDim(0); }catch(e){}
    const dm=document.getElementById('dimmer'); if(dm){ dm.style.opacity='0'; dm.style.display='none'; }
  }
}
function vsBrightSet(v){
  if(vsCfg().brightAuto===true) return;   // 自动档下滑条停用
  applyBright(v);
  const c=vsCfg(); c.bright=parseInt(v,10)||100; vsSave(c);
}
/* 两个音量滑条 = 系统音量的遥控器（用户 2026-11-15：App 里改了就直接是系统音量，
   不是在系统音量之上再缩放，App 退出也不恢复）。提醒音→铃声/通知流，语音→媒体流；
   播放端不再自己缩放（以前滑条×系统音量双重衰减，系统 50% 时拉满也只有 50%）。
   只在用户真的拖了滑条时才写系统，启动时不主动改。 */
function vsVolSetA(v){
  const c=vsCfg(); c.alertVol=Math.max(0,Math.min(100,parseInt(v,10)||0)); vsSave(c);
}
/* 系统已经整机静音、又不是我们自己要求的（另一款 App 的静音/免打扰）：
   这时拖滑条不许写系统——0 值往铃声流一写会顺手解除铃声静音，等于替对面
   把静音打开了（2026-11-21 两家 App 打架）。滑条值照存，等系统恢复后再生效。 */
function sysMutedNotByUs(){
  try{
    if(muteOn()||dndActive()) return false;           // 我们自己要的静音
    if(!(window.CB&&CB.sysAudioState)) return false;
    var st=JSON.parse(CB.sysAudioState()||'{}');
    return st.ringMx>0 && st.ring===0 && st.music===0;
  }catch(e){ return false; }
}
/* 提醒音→系统铃声+通知（锁屏那声响就是这条音路放的，滑条拖完跟着变） */
function vsVolApplyRing(){
  if(sysMutedNotByUs()) return;                       // 另一家静的音：别替它打开
  if(!(window.CB&&CB.setVol&&CB.getMaxVol)) return;   // iPhone / 浏览器没有通知音路
  try{
    const mx=parseInt(CB.getMaxVol('ring'),10);
    if(!(mx>0)) return;                               // 这条音路读不出来：别瞎设
    CB.setVol('ring', Math.round(alertVol()/100*mx));
  }catch(e){}
}
function vsVolSetP(v){
  const c=vsCfg(); c.speechVol=Math.max(0,Math.min(100,parseInt(v,10)||0)); vsSave(c);
}
/* 语音播报→系统媒体音量（壳的播放器、老壳的 Audio 都走这条流，全跟着变） */
function vsVolApplyMedia(){
  if(sysMutedNotByUs()) return;                       // 另一家静的音：别替它打开
  if(!(window.CB&&CB.setVol&&CB.getMaxVol)) return;
  try{
    const mx=parseInt(CB.getMaxVol('media'),10);
    if(!(mx>0)) return;
    CB.setVol('media', Math.round(speechVol()/100*mx));
  }catch(e){}
}
/* 两条滑条写的是整机系统音量，CodeBoss / AgentBoss 同装时是同一套：
   另一家改过，这边的 cfg 就成了旧值（看着 60，系统实际 30，拖一下才跳回来）。
   所以打开设置以系统真值为准回填，只在不是我们自己在静音时采纳。
   两条流都 0 不采纳——那是另一家开着整机静音，0 不是用户的滑条。 */
function vsVolAdopt(){
  if(muteOn()||dndActive()) return;
  try{
    if(!(window.CB&&CB.sysAudioState)) return;
    var st=JSON.parse(CB.sysAudioState()||'{}');
    var rp=st.ringMx>0?Math.round(st.ring/st.ringMx*100):0;
    var mp=st.musicMx>0?Math.round(st.music/st.musicMx*100):0;
    if(rp<=0&&mp<=0) return;
    var c=vsCfg(), ch=false;
    if(rp>0&&Math.abs(rp-alertVol())>3){ c.alertVol=rp; ch=true; }
    if(mp>0&&Math.abs(mp-speechVol())>3){ c.speechVol=mp; ch=true; }
    if(ch) vsSave(c);
  }catch(e){ vsErr('volA',e); }
}
/* 音量：提醒音在「基础提醒」，语音播报在「智能提醒」，各自回填存过的值。 */
function vsVolSync(){
  vsVolAdopt();                               // 先以系统为准，再回填滑条
  var aBox=document.getElementById('vsVolAlertBox');
  var sBox=document.getElementById('vsVolSpeechBox');
  if(aBox) aBox.style.display='';
  if(sBox) sBox.style.display='';
  const a=document.getElementById('vsVolAlert');
  const p=document.getElementById('vsVolSpeech');
  if(a) a.value=alertVol();
  if(p) p.value=speechVol();
}
function vsVolSetS(name,v,beep){
  try{ if(window.CB&&CB.setVol) CB.setVol(name,parseInt(v,10)||0); }catch(e){}
  if(beep){ try{ if(window.CB&&CB.beepVol) CB.beepVol(name); }catch(e){} }   // 松手放一声，听得到变化
}
function vsKeep(){
  const c=vsCfg(); c.keepon=(c.keepon===true)?false:true; vsSave(c);
  try{ if(window.CB&&CB.setKeepOn) CB.setKeepOn(c.keepon===true); }catch(e){}
  swSet('vsKeepOn',c.keepon===true); keepHintSync();
  toast('已保存');
}
/* 护眼类：有壳且悬浮层已接管（eyeSys）→ 页面暖色层让位，全屏（含键盘/状态栏）由壳罩；
   浏览器/无权限 → 页面自己罩 */
function applyEyeClass(on){
  const sys=(window.CB&&CB.eyeSys&&CB.eyeSys()==='1');
  document.body.classList[(on&&sys)?'add':'remove']('eyeSys');
  document.body.classList[(on===true)?'add':'remove']('eye');
}
function vsEye(){
  const c=vsCfg(); const on=(c.eye!==true);
  let st='';
  try{ if(window.CB&&CB.setEye) st=String(CB.setEye(on)); }catch(e){}
  c.eye=on; vsSave(c);
  applyEyeClass(on); swSet('vsEyeBtn',on);
  if(on&&st==='page') toast('已开启（授予悬浮窗权限后键盘也护眼）');
  else toast('已保存');
}
/* 壳里下载安装包的实时进度（Java 每 0.7s 推一次）：直接写在「更新 App」按钮上。
   不写的话，从点按钮到安装界面之间是一段安静的等待（手机上几百 KB 也要十几秒），
   用户只会觉得「点了没反应」。参数：pct=百分比（-1 总大小未知 / -2 失败 / -3 卡住重试） */
var __dlTick=0, __dlHold=0;   /* __dlHold：壳已经接管到安装阶段，页面兜底复位别再抢文案 */
window.__cbDlProg=function(pct,done,total){
  var b=document.getElementById('vsUpdGo');
  if(!b) return;
  __dlTick=Date.now();
  if(__dlHold) return;   /* 壳已经接管到安装阶段：下载线程再推 100% 也不能把「请在系统界面点「安装」」
                            盖回「正在打开安装界面…」——以前就是这么盖回去的，看着像一直卡在打开安装界面 */
  if(pct>=100){ b.dataset.busy='1'; b.textContent='正在打开安装界面…'; b.disabled=true; return; }
  if(pct===-2){ delete b.dataset.busy; b.textContent='更新 App'; b.disabled=false; toast('下载失败，请稍后再试',true); return; }
  if(pct===-3){ b.dataset.busy='1'; b.textContent='下载卡住，重试中…'; return; }
  b.dataset.busy='1';
  b.disabled=true;
  if(pct<0){ b.textContent = done>0 ? ('下载中 '+Math.round(done/1024)+' KB') : '下载中…'; }
  else { b.textContent='下载中 '+pct+'%'; }
};
/* 壳推来的安装状态（PackageInstaller 会话）：
   -1 确认框已弹 / 0 装成功 / >0 失败或取消（系统状态码）/ -2 超时收尾。
   失败必须把按钮复位——以前停在「正在打开安装界面…」且点不动，看着就是卡死。 */
window.__cbInstallSt=function(st){
  var b=document.getElementById('vsUpdGo'); if(!b) return;
  st=Number(st)||0;
  if(st===-1){ __dlHold=1; b.dataset.busy='1'; b.disabled=true; b.textContent='请在系统界面点「安装」'; return; }
  if(st===-3){ __dlHold=1; b.dataset.busy='1'; b.disabled=true; b.textContent='改用系统下载…'; return; }
  if(st===-4){ __dlHold=1; b.dataset.busy='1'; b.disabled=true; b.textContent='等系统安装界面…'; return; }
  if(st===0){ __dlHold=1; b.dataset.busy='1'; b.disabled=true; b.textContent='安装中，完成后自动打开'; return; }
  __dlHold=0;
  var needPerm=!!(window.CB&&CB.installPerm&&CB.installPerm()==='0');
  delete b.dataset.busy;
  b.disabled=false; b.textContent=needPerm?'授权并更新':'更新 App';
  if(st>0) toast('安装未完成，可以再点一次',true);
};

/* ── 客户端版本：检查更新只做版本检查（自动/手动都一样）——发现有新版就提示，
      并露出单独的「更新 App」按钮，真正的下载安装只在这个按钮上发生 ── */
var vsUpdV=0, vsUpdNamed='', vsUpdHinted=0, vsUpdUrl='', vsTfUrl='';   /* 新版 code / 新版号 / 已提示过的 code / 安装页地址 / iOS 公开内测(TestFlight)链接 */
/* 版本号旁边得写清楚是哪个 App（2026-11-19 用户）：产品线跟 vsVerApi 同一套来源
   （query → 壳种的 cb_product cookie），标签说哪条线就是正在比对的哪个更新槽。 */
function vsAppName(){
  return lineName(cbLine()).app;
}
function vsVerLocal(){
  let inst=0, ver='';
  try{
    const raw = CB.appVer ? CB.appVer() : CB.apkVer();
    const v=JSON.parse(raw); inst=v.code; ver='v'+v.ver;
  }catch(e){}
  return {code:inst, txt:vsAppName()+' '+(ver||'…')};
}
function vsUpdShow(){   /* 有新版才露出升级按钮；没开安装权限时改文案，点了先带路去授权 */
  const b=document.getElementById('vsUpdGo'); if(!b) return;
  const needPerm=!!(window.CB&&CB.installPerm&&CB.installPerm()==='0');
  b.style.display='';
  /* 正在下载/安装（busy）时别抢文案、也别把按钮复活；空闲时才复位成可点 */
  if(!b.dataset.busy){ b.textContent=needPerm?'授权并更新':'更新 App'; b.disabled=false; }
  b.dataset.perm=needPerm?'1':'';
}
function vsUpdHide(){
  vsUpdV=0; vsUpdNamed='';
  const b=document.getElementById('vsUpdGo'); if(b) b.style.display='none';
}
var PLAND=null;
/* 免费版不含命令终端：终端页/新开按钮/新建终端卡都收起；真正的闸门在中继。 */
function termAllowed(){
  return !(PLAND&&PLAND.ok&&PLAND.features&&PLAND.features.terminal===false);
}
/* 自动听写（自动等你说话）也是增值功能：与智能提醒同一个套餐位 features.remind。
   取不到套餐时先放行——别让一次网络抖动把 Pro 用户的开关锁死。 */
function autoPlanOK(){
  return remindPlanOK();
}
/* 智能提醒（模型概括/状态识别/语音播报）是增值功能：免费版把智能区置灰。
   真正的闸门在开发机（不生成语音、不调模型）和中继（TTS/提醒用途 LLM），这里只负责说清楚去哪开通。 */
function remindPlanOK(){
  return !(PLAND&&PLAND.ok&&PLAND.features&&PLAND.features.remind===false);
}
function smartGateSync(){
  var ok=remindPlanOK();
  var lk=document.getElementById('vsSmartLock');
  if(lk) lk.style.display=ok?'none':'';
  var bs=document.getElementById('boxSmart');
  if(bs){ bs.style.opacity=ok?'':'0.45'; bs.style.pointerEvents=ok?'':'none'; }
  /* 总开关关掉：子项一起置灰（总开关自己留在可点区域） */
  var sub=document.getElementById('boxSmartSub');
  if(sub){
    var on=ok&&smartOn();
    sub.style.opacity=on?'':'0.45';
    sub.style.pointerEvents=on?'':'none';
  }
}
function autoGateSync(){
  var sup=false;
  try{ sup=!!(window.CB&&typeof CB.autoListenStart==='function'); }catch(e){}
  var ok=autoPlanOK();
  var r=document.getElementById('trNAuto'), h=document.getElementById('trNAutoHint');
  var lk=document.getElementById('trAutoLock');
  if(r){ r.style.display=sup?'':'none'; r.style.opacity=(sup&&!ok)?'0.45':''; }
  if(h) h.style.display=(sup&&ok)?'':'none';
  if(lk) lk.style.display=(sup&&!ok)?'':'none';
}
function autoToggle(){
  if(!autoPlanOK()){ toast('自动听写是 Pro / Max 套餐的增值功能',true); return; }
  vsSet('autolisten',vsCfg().autolisten!==true);
}
function applyPlanUI(){
  autoGateSync();
  smartGateSync();
  var ok=termAllowed();
  /* 套餐不含的功能不藏起来：入口留着、置灰 + 明说「免费版不具备此功能」，
     用户才知道升级能解锁什么。真正的闸门在中继（这里只是展示层）。 */
  var c=document.getElementById('newTermCard');
  if(c) c.style.opacity=ok?'':'0.5';
  ['termCreateBtn','termMacBtn'].forEach(function(id){
    var e=document.getElementById(id); if(e) e.style.opacity=ok?'':'0.45';
  });
  var lk=document.getElementById('termLock'); if(lk) lk.style.display=ok?'none':'';
  var lk2=document.getElementById('termLock2'); if(lk2) lk2.style.display=ok?'none':'';
  /* 「识别命令」也是终端位功能：入口留着但灰掉，点一下会说去哪儿开通。 */
  var ts=document.getElementById('tabScan');
  if(ts){ ts.style.opacity=ok?'':'0.45'; }
  var b=document.getElementById('termNewBtn');
  if(b){ b.style.display=''; b.style.opacity=ok?'':'0.45'; }
  /* 一台开发机都没有：模型行/新建终端/终端入口都无从谈起（显示的还是上次那台的数据），
     收起来换成「绑定开发机」入口。有设备时再按套餐恢复。 */
  var none=DEV.init&&!DEV.na&&!(DEV.list||[]).length;
  var mc=document.getElementById('modelCard'); if(mc) mc.style.display=none?'none':'';
  var ns=document.getElementById('newSessCard');
  if(ns){ if(none) ns.style.display='none'; }
  var bc=document.getElementById('bindCard'); if(bc) bc.style.display=none?'':'none';
  var ec=document.getElementById('termEntryCard'); if(ec) ec.style.display=none?'none':'';
  if(none){
    if(c) c.style.display='none';
    if(b) b.style.display='none';
    closeTermPage();
  }else{
    if(c) c.style.display='';
    if(b) b.style.display='';
  }
  try{ termDotSync(); }catch(e){}
}
/* 顶部「今日远程时长」小胶囊：数字都来自服务端（设置页同源），30 秒对一次账。
   读秒的道理（和中继的记账口径严格对齐）：
   - 中继按「计费刻度窗口」记账：一个窗口（默认 3 秒，后台可配）里出现过一次真正的
     使用动作（发消息/按键/开会话/看列表…）就记一格（unit 秒）；超出套餐后每格从
     时长包扣 unit 秒。消息/画面/推流这些长轮询是豁免计费的——盯着输出看一整天也不记。
   - 所以倒计时不是墙上时钟，而是「一直用下去，到哪一分钟会记满」：
       left = (有效上限 - 已用) × 60 - 当前整分钟已过的秒数
     锚点用服务器时钟（srv_ts / 响应 Date 头，不看手机时钟）。tk=1 表示锚点这一
     窗口确实记了账（LASTBILL 落在这一格里），此时倒计时连续递减、跨格不跳；
     tk=0（只是挂着看、这一格没做过任何实际动作）就冻住，不虚构时间。
   - 前台 + 最近一分钟碰过 + 最近一次真实请求还新鲜，三者同时满足才算「在用」。
   - 停手、息屏、断网都冻结；重新碰屏幕 / 第一个记账请求 / 恢复连接时马上对账。 */
var USEST={ok:false,plan:'',used:0,cap:0,daily:0,credit:0,
  phase:0,tk:false,at:0,el:0,fb:false,hold:-1,totSec:0,unit:3,
  live:false,froze:0};   /* live=上一秒在连续读秒；froze=刚停手/回前台，锚点过期等对账 */
/* 所有时长字段一律是**秒**：daily=今天这一池，credit=包余额，used/cap=已用/套餐上限；
   unit=计费刻度（秒，默认 3）；hold=当前显示的秒数（-1=未初始化）；
   tk=锚点这一格记账了没有；phase=锚点时刻服务器在刻度窗口里的第几秒 */
/* 浮窗用的紧凑时长：23分 / 1时20分 */
function useDurC(m){ m=Math.max(0,Math.round(Number(m)||0));
  if(m<60) return m+'分';
  var h=Math.floor(m/60), r=m%60;
  return h+'时'+(r?r+'分':''); }
/* 胶囊里的余额：整分钟说（包余额是「账」，不是倒计时）。不足 1 分按 1 分算。 */
function useDurM(s){ s=Math.max(0,Number(s)||0);
  var m=Math.ceil(s/60);
  if(m<60) return m+'分';
  var h=Math.floor(m/60), r=m%60;
  return h+'时'+(r?r+'分':''); }
/* 读秒：59:59 / 1:02:34（数字等宽，54px 胶囊放得下） */
function useDurSec(s){
  s=Math.max(0,Math.floor(Number(s)||0));
  var h=Math.floor(s/3600), m=Math.floor(s%3600/60), q=s%60;
  function two(n){ return (n<10?'0':'')+n; }
  if(h) return h+':'+two(m)+':'+two(q);
  return m+':'+two(q);
}
/* 刻度级时长：不足 1 分钟按秒说（刻度 3 秒时显示「0 分」会让人以为没扣时间） */
function useDurTick(s){ s=Math.max(0,Math.floor(Number(s)||0)); return s<60?(s+'秒'):useDurM(s); }
/* 时长包还剩多少秒：中继的余额现在就是秒；老中继只有分钟（use_credit_min→credit×60
   已在 planRefresh 里换算过），这里统一按秒存 */
function usePackSec(){
  return Math.max(0,Math.round(Number(USEST.credit)||0));
}
/* 现在该不该读秒：前台（亮屏）+ 最近一次真实请求还新鲜。
   「碰过屏幕」不算条件：音量键说话 → 下键发送这条路上，页面一次触摸事件都收不到，
   可人明明在用（麦克风亮着、转写请求也在跑）。以前拿 LASTACT 卡着，说完一句
   倒计时还冻在原地（2026-11 实测）。判断依据就是「亮屏 + 有请求」。
   请求新鲜这条不能少：中继和开发机都不通时账也不会走，倒计时不能自己往下编。
   /api/plan 本身不算新鲜（见 xjX/dcReq）：中继活着但开发机掉线时它照样回，
   可那会儿一个能记账的请求都没有，倒计时该停。
   这一分钟到底记没记账仍由 tk 卡着（见 usePaint）：亮屏但整分钟没有实际动作，
   照样冻住，不会凭空少时长。 */
function useActive(){
  var now=Date.now();
  return !hiddenNow()&&(now-LASTOK<75*1000);
}
/* 重新碰屏幕 / 恢复连接：马上对一次账，别干等 30 秒轮询（锚点要立刻收回偏差） */
function useAnchorSoon(){
  var now=Date.now();
  if(now-(useAnchorSoon.t||0)<3000) return;
  useAnchorSoon.t=now;
  planRefresh();
}
/* 记了一次账：从屏幕现在这个数起读一格（unit 秒），读完整好落在服务器记的账上。
   同一格里再记一次账（中继按格幂等，不会重复扣）不重新起读，免得读快一倍。 */
function useTickStart(){
  var now=Date.now();
  var un=Number(USEST.unit)||useUnit(PLAND||{});   /* 以最近一次对账给的刻度为准 */
  if(now<(USEST.tickEnd||0)) return;            /* 这一格已经在读 */
  var from=USEST.hold;
  if(from==null||from<0){
    if(!USEST.ok) return;                       /* 还不知道余额：等第一次对账 */
    from=USEST.totSec||0;
  }
  USEST.tickFrom=from; USEST.tickStart=now; USEST.tickEnd=now+un*1000;
  USEST.live=true;
}
/* 语音也算「在用」：账上记了，屏幕上也该走。
   中继在「识别成功」时已经记了一格刻度（relay h_asr → use_take，失败不扣），
   但安卓壳的原生录音是壳自己发请求（HttpURLConnection），页面全程没参与 →
   LASTBILL 不会更新 → 读秒锚点以为「这一分钟没记账」→ 倒计时冻在原地，
   看着像说话根本不计时（2026-11 实测：按住音量键说话，倒计时一动不动）。
   这里把「这一分钟记了账」补上，并用中继回的 quota 校正「今天已用」；
   具体数字仍以服务器为准（立刻 planRefresh 对一次账）。 */
function useBillPTT(json){
  try{
    var d=null, plain='';
    try{ d=JSON.parse(json); }catch(e){ d=(json&&typeof json==='object')?json:null; }
    /* 两条回调口径不一样：安卓壳原生录音回的是整段 JSON，页面自己的上传（没有壳、
       或者壳没有原生录音）回的是**纯文本**，而且只在识别成功时才回调到这里
       （失败走 fail()，不回 __pttResult / __autoResult）。以前只认 JSON，
       纯文本一律 return —— 真机上就成了「说话扣了时长，浮标一动不动」。 */
    if(!d && typeof json==='string' && json) plain=json;
    if(!d && !plain) return;
    if(d && d.ok===false) return;        /* 失败不扣：中继只在识别成功时记账 */
    var at=Date.now();
    LASTOK=at;                           /* 刚有请求 → useActive 为真（亮屏 + 请求新鲜） */
    LASTBILL=Math.max(LASTBILL||0,at);   /* 这一分钟记了账 → 锚点连续读秒 */
    useTickStart();                      /* 从现在的数读一格（10 秒），读完停在服务器记的账上 */
    USEST.bt=at+3000;                    /* 浮标第二行「记账 ✓」亮三秒：证明这条路真跑到了 */
    var q=(d&&d.quota)||null;            /* 纯文本那条路 d 是 null，不能直接取字段 */
    if(q&&q.used_s!=null){
      var u=Number(q.used_s||0);
      if(u>(USEST.used||0)) USEST.used=u;   /* 已用只增不减 */
    }
    planRefresh();                       /* 立刻对账：服务器说了算 */
    usePaint();
  }catch(e){}
}
function usePaint(){
  var f=document.getElementById('useFloat'); if(!f) return;
  /* 只挂在会话页（麦克风只在会话页显示）；设置页有完整明细 */
  if(!USEST.ok||pageIdx!==1){ f.style.display='none'; return; }
  /* 必须写 block：基础样式的 display:none 是 CSS 规则，'' 清不掉 */
  f.style.display='block';
  var act=useActive();
  /* 记一次账 = 读一格（unit 秒）。中继按 10 秒一格记账，页面这头就从「记账那一刻
     屏幕上的数」往下走一格，正好落到服务器记的账上；走完就冻住，没在用不倒数。
     以前读秒开关是对账时拍的快照（USEST.tk），拍完要等下一次对账（最多 30 秒）才重算，
     于是读秒长短不一（10~30 秒），跟 10 秒一格对不上——用户直接问了「读秒到多长时间」。
     现在只认时间锚点：now 过了 tickEnd 就停。 */
  var now0=Date.now();
  var ticking=act&&now0<(USEST.tickEnd||0);
  if(ticking){
    USEST.el=Math.max(0,(now0-(USEST.tickStart||now0))/1000);
    USEST.hold=Math.max(0,(USEST.tickFrom||0)-USEST.el);
    USEST.live=true;
  }else{
    USEST.live=false;
    /* 没在读秒：以服务器的数为准，只往下修（买包/升级这类上调走 planRefresh） */
    if(USEST.ok&&USEST.totSec>=0){
      USEST.hold=(USEST.hold<0)?USEST.totSec:Math.min(USEST.hold,USEST.totSec);
    }
  }
  if(ticking&&USEST.hold<0){ USEST.hold=0; }
  var val=Math.max(0,USEST.hold<0?0:USEST.hold);
  var km=usePackSec();                             /* 时长包余额（秒） */
  var v=document.getElementById('ufV'), pv=document.getElementById('ufP');
  /* 2026-11-17 起全部套餐按「次数+秒数」计（免费同款口径）：能发几条消息、
     还能说多少秒。数字来自 /api/plan（30 秒刷新，读/看不扣），不在这里自己数。 */
  if(PLAND&&PLAND.ok){
    var _lm=PLAND.limits||{}, _um=PLAND.usage||{};
    var _sc=Number(_lm.sends_day||0), _su=Number(_um.sends_day||0);
    var _ac=Number(_lm.asr_sec_day||0), _au=Number(_um.asr_day||0);
    if(_sc>0||_ac>0){
      f.className=act?'':'pause';
      var _sl=Math.max(0,_sc-_su), _al=Math.max(0,_ac-_au);
      if(v) v.textContent=_sc>0?('消息 '+_sl+' 条'):('语音 '+_al+' 秒');
      if(pv){
        if(_sc>0&&_ac>0){ pv.style.display=''; pv.textContent='语音 '+_al+' 秒'; }
        else pv.style.display='none';
      }
      f.title='今日额度：消息 '+_su+'/'+_sc+' 条 · 语音 '+_au+'/'+_ac+' 秒（时长不限）';
      return;
    }
  }
  if(!USEST.cap){                 /* 全档次数都不设上限（如 Max）：没有可倒数的额度 */
    f.className=act?'':'pause'; if(v) v.textContent='不限';
    /* Max 用户也能买包（套餐到期后才开始用）：钱花了就得看得见余额 */
    if(pv&&USEST.bt&&Date.now()<USEST.bt){ pv.style.display=''; pv.textContent='记账 ✓'; }
    else if(pv){ if(km>0){ pv.style.display=''; pv.textContent='包 '+useDurM(km); }
            else pv.style.display='none'; }
    var _lc=PLAND&&PLAND.ok?Number((PLAND.limits||{}).llm_day||0):0;
    f.title='今日远程时长：不限（点开设置）'+
      (_lc?(' · 模型 '+_lc+' 次/天'):'')+
      (km>0?(' · 时长包余 '+useDurC(Math.ceil(km/60))+'（套餐到期后使用）'):'');
    return;
  }
  var p=Math.min(100,USEST.used*100/USEST.cap);
  /* 今天的额度用完、正在扣包：黄色提醒；都没了才红；没在记账再叠一层淡 */
  var cls=val<=0?'full':((USEST.used>=USEST.cap||p>=80)?'warn':'');
  if(!ticking) cls+=(cls?' ':'')+'pause';
  f.className=cls;
  if(v) v.textContent=useDurSec(val);
  if(pv&&USEST.bt&&Date.now()<USEST.bt){
    pv.style.display=''; pv.textContent='记账 ✓';   /* 语音转写完的高亮，三秒后自动归位 */
  }else if(pv){
    if(km>0&&USEST.used<USEST.cap){ pv.style.display=''; pv.textContent='包 '+useDurM(km); }
    else if(km>0){ pv.style.display=''; pv.textContent='今日 0分'; }
    else pv.style.display='none';                 /* 包余额 0：不显示第二行 */
  }
  f.title='今日远程时长：已用 '+useDurC(Math.ceil(USEST.used/60))+' · 剩余 '+useDurC(Math.ceil(val/60))+
    '（上限 '+useDurC(Math.ceil(USEST.cap/60))+'）'+
    (km>0?(' · 时长包余 '+useDurC(Math.ceil(km/60))):'')+
    (USEST.fb?' · 中继离线，开发机本地估算':'')+
    (ticking?'':' · 已暂停计时（没在记账）')+' · 点开设置';
}
function usePollStart(){
  if(usePollStart.t) return;
  usePollStart.t=setInterval(function(){
    if(hiddenNow()) return;   /* 后台/息屏不读秒、不轮询（onSleep 已经停了） */
    usePaint();               /* 每秒重画：只改文字，不发请求 */
    var now=Date.now();
    if(now-(usePollStart.k||0)<30000) return;
    usePollStart.k=now;
    planRefresh();            /* 30 秒对一次账；挂着看带 a=0，不记分钟 */
  },1000);
}
/* 标题栏套餐徽章：免费 / Pro / Max 用不同颜色和图标区分；
   内部套餐（plans.json 里自定的档）直接显示它的名字，别再冒充「免费版」 */
function tierPaint(){
  var b=document.getElementById('tierBadge'); if(!b) return;
  var p=(PLAND&&PLAND.plan)||'';
  if(!p){ b.style.display='none'; return; }
  b.style.display='';
  if(p==='max'){ b.className='plevel max'; b.textContent='max'; b.title='尊贵 VIP · Max 会员'; }
  else if(p==='pro'){ b.className='plevel pro'; b.textContent='pro'; b.title='Pro 会员'; }
  else if(p==='free'){ b.className='plevel free'; b.textContent='free'; b.title='免费版'; }
  else{ b.className='plevel pro'; b.textContent=(PLAND.name||p);
        b.title=(PLAND.name||p)+'（内部套餐）'; }
}
/* 现在这档叫什么：价目表里有这一档的名字就用它（「Max 年付」），没有才退回档位名（「Max」） */
function vsPlanName(d){
  var sk=String((d&&d.sku)||''), n=(((d&&d.skus)||{})[sk]||{}).name||'';
  if(!n) return (d&&(d.name||d.plan))||'—';
  return n.replace(/^Codeboss\s*/,'')||n;
}
/* 按钮写「开通 / 升级 / 续费」——判断的是「还有没有更高的档」：
   高低 = 等级（pro<max）× 大数 + 时长天数。价目表里还有比当前更高/更长的才算能升级；
   到顶了（Max 年付）就只剩续费。价目表本身来自中继，后台改了档位这里自动跟着走。 */
function vsPayLabel(d){
  if(!d||!d.ok) return '开通套餐';
  if((d.plan||'free')==='free') return '开通套餐';
  var skus=d.skus||{}, cur=String(d.sku||'');
  function rank(k){ return (k.indexOf('max_')===0?100000:0)+Number((skus[k]||{}).days||0); }
  var best=rank(cur);
  for(var k in skus){
    if(k.indexOf('pack')===0) continue;      /* 时长包不是套餐档位，不参与比高低 */
    if(rank(k)>best) return '升级套餐';
  }
  return '续费';
}
/* 计费刻度（秒）：中继 /api/plan 的 usage.use_unit；老中继没有这个字段，按 3 秒。 */
function useUnit(d){
  var u=Number((((d||{}).usage)||{}).use_unit||0);
  return (u>=1&&u<=60)?Math.floor(u):3;
}
/* 服务器时钟在「一个计费刻度窗口」里的第几秒：中继按窗口记账，读秒要跟它的拍子对齐。
   中继的 /api/plan 没有 ts 字段（老版本），退回响应的 Date 头；两条都没有就不扣秒。 */
function useSrvPhase(d,r){
  var ts=Number((d&&d.srv_ts)||0);
  if(!ts){ var h=(r&&r.hdr&&r.hdr('Date'))||''; ts=(Date.parse(h)||0)/1000; }
  if(!ts) return 0;
  var u=useUnit(d);
  return ((Math.floor(ts)%u)+u)%u;
}
function planRefresh(){            /* 套餐/额度：只展示，判断都在中继 */
  /* 定时查询额度是页面自己在轮询，不是用户操作：带 a=0，中继/开发机照样应答但不记分钟。
     以前刷新页面会给 60 秒「在用」宽限，挂着的手机每分钟白记一分钟。 */
  /* 记住「请求发出去的那一刻」给下面的 tk 判据用：跨中继一个来回要 1~3 秒，拿响应到达
     时刻去跟记账时刻比，phase 那 1 秒容差全被往返吃掉（0.7.53 中兴实测：记账后
     判据 tk=0，浮标一格不走）。 */
  var reqAt=Date.now();
  xj('/api/plan?a=0').then(function(r){
    if(!r||r.away) return;         /* 后台不发请求：什么都没发生，别把界面刷成「—」 */
    var d=r.json;
    var ok=!!(d&&d.ok), fb=!!(d&&(d.ok||d.fallback));
    if(ok) PLAND=d;                /* 取不到就保留上次结果，别把免费版的终端入口放出来 */
    tierPaint();
    keepHintSync();                /* 套餐到了再决定常亮提示显不显示（Max 不提示） */
    applyPlanUI();
    if(fb){                        /* 顶部胶囊只认服务端的数，本地不按墙钟估算 */
      var l0=d.limits||{}, us0=d.usage||{};
      /* 单位：新中继给秒（use_used_sec/use_credit_sec/use_cap_sec + use_unit）；
         老中继只有分钟（use_min_day/use_credit_min/use_cap_eff）——一律换成秒再算，
         下面的 USEST 字段全部是秒。 */
      var sec0=(us0.use_used_sec!=null||us0.use_cap_sec!=null);
      var used0=sec0?Number(us0.use_used_sec||0):Number(us0.use_min_day||0)*60;
      /* 今天的量和时长包的量分开算：先倒今天的，今天的用完再倒包的。
         以前上限只看套餐（买包后一直显示 0:00:00）；只看有效上限又分不出
         两段各剩多少，所以这里两个数都留着。 */
      var base0=Number(l0.use_min_day||0)*60;                  /* 套餐每日上限（秒），0=不限 */
      var cred0=sec0?Number(us0.use_credit_sec||0):Number(us0.use_credit_min||0)*60;  /* 包余额（秒） */
      var capE=sec0?Number(us0.use_cap_sec||0):Number(us0.use_cap_eff||0)*60;         /* 有效上限（秒） */
      if(!capE&&base0) capE=Math.max(base0,used0)+cred0;
      /* 额度浮层还顶着、账上却没满（capE=0 = 不限）→ 撤掉。浮层是一次性的，判定
         翻案后不会自己消失，误判一次这台手机就一直卡在「用完了」（2026-11 实测：
         中继账上 15/60，手机上却整屏挡着）。 */
      var wallEl=document.getElementById('qwall');
      if(wallEl&&(capE<=0||used0<capE)){
        wallEl.remove(); QUOTA_WALL=false; DIRECT.cool=0;
        try{ if(!directOn()) directTry(); }catch(e){}
      }
      /* 开发机估算（中继断链）：新 host 会把套餐和时长包分开给（split）；
         老 host 只缓存了「有效上限」，分不出两段，当一整池倒——数字是对的，
         只是不显示「包余」。 */
      var splitFb=!!(d.fallback&&d.split);
      if(d.fallback&&!splitFb&&capE) base0=capE;
      var dmSec=base0?Math.max(0,base0-used0):0;         /* 今天这一池（秒） */
      var kmSec=(d.fallback&&!splitFb)?0:cred0;          /* 时长包余额（秒） */
      var had=USEST.ok;
      var prevCap=had?USEST.cap:0, prevCred=had?USEST.credit:0;
      var prevTot=USEST.totSec||0;
      /* 刷新前是不是正在连续读秒（tk/at 下面会被覆盖，先记下来） */
      var wasTick=USEST.tk&&useActive();
      USEST.ok=true; USEST.plan=d.plan||'';
      USEST.used=used0; USEST.cap=base0; USEST.unit=useUnit(d);
      USEST.credit=(d.fallback&&!splitFb)?0:cred0;
      USEST.daily=dmSec;
      USEST.fb=!!d.fallback;
      USEST.totSec=dmSec+kmSec;
      USEST.phase=useSrvPhase(d,r);
      USEST.at=Date.now(); USEST.el=0;
      /* 这一分钟记账了没有：中继按整分记账，LASTBILL 落在当前整分钟里就是记了。
         记了 → 锚点扣掉已过秒数，读秒连续；没记 → 冻住，只在余额变少时往下修。 */
      /* 1 秒容差：phase 是把服务器「整秒」取模得来的，真实落在一格里的秒数比它多个小数，
         卡在格边界（phase=0）时严格比就成了 LASTBILL>=at，刚记完账也永远不成立，
         浮标冻住整整一格 —— 10 秒刻度时约十分之一的语音都撞这个（实测脚本可复现）。
         比的是请求发出的时刻（reqAt），不是响应回来的时刻：见函数开头。 */
      USEST.tk=(LASTBILL>=reqAt-USEST.phase*1000-1000);
      var ramping=Date.now()<(USEST.tickEnd||0);
      if(ramping){
        /* 正在读这一格：数值由 useTickStart 的锚点走，别拿对账结果打断——
           对账回来时往往已经慢了一两秒，直接采用会把刚读下去的秒数弹回来。
           余额真变多（买包/升级）另说：那是该立刻上调的。 */
        if(!USEST.live&&USEST.totSec>prevTot+1) USEST.hold=USEST.totSec;
        USEST.live=true; USEST.froze=0;
      }else if(USEST.tk){
        var nh=Math.max(0,USEST.totSec-(USEST.phase||0));
        /* 正在读秒时只允许往下修：直连/LAN 的分钟是开发机记的，中继要等最 5 秒的补报
           才看得到，这期间算出来的余额会比上次多出整整一分钟——直接采用就是
           「读一分钟又跳回去」（2026-11 实测 12:10:08 反弹了 58 秒）。
           余额真的变多（买包/升级）、本来冻着（停手后的恢复）才接受上调。 */
        USEST.hold=(!wasTick||USEST.hold<0||USEST.totSec>prevTot+1||USEST.froze)
          ? nh : Math.min(USEST.hold,nh);
        USEST.live=true; USEST.froze=0;   /* 锚点刚换新：接下来可以按它连续读秒 */
      }
      /* 超过 60 秒没记账：开发机补报（直连/LAN 最多晚 5 秒到中继）早就落库了，
         中继的数就是准的——直接采用，允许往上调。以前只许往下修，一次误判记账
         （如开屏的 rtc_offer）就让显示永久比账上少一格，再也不会自己回来。 */
      else if(USEST.hold<0||USEST.totSec>prevTot+1||Date.now()-(LASTBILL||0)>60000) USEST.hold=USEST.totSec;  /* 到账/升级/误判自愈：直接显示服务器余额 */
      else USEST.hold=Math.min(USEST.hold,USEST.totSec);
      if(!USEST.tk&&!ramping){ USEST.live=false; USEST.froze=0; }
      /* 时长包到账（余额变大）/ 升级套餐（上限变大）：当场说一声，
         并把已经盖上的额度墙撤掉——「买完没提示、回来还是零」就是这么来的。 */
      /* 第一次买包时 prevCred 是 0，以前 && 一挡就永远不提示；只看「比上次多」
         （第一次拉取 had=false 不提示，避免一进页面就弹）。只对中继真数做，
         fallback 会来回切，切一次就误报一次。 */
      if(ok&&had&&(cred0>prevCred||base0>prevCap)){
        var add=(cred0>prevCred)?(cred0-prevCred):(base0-prevCap);   /* 秒 */
        toast('已到账：使用时长 +'+useDurTick(add)+'，剩余 '+
          useDurC(Math.ceil(Math.max(0,USEST.daily)/60)+Math.ceil(cred0/60)));
        var qw=document.getElementById('qwall');
        if(qw&&qw.parentNode) qw.parentNode.removeChild(qw);
        QUOTA_WALL=false;
      }
      usePaint();
    }
    var v=document.getElementById('vsPlan'), u=document.getElementById('vsPlanUse');
    if(!v||!u) return;
    if(!ok){
      if(fb) return;               /* 中继离线：保留上次的套餐文字，别显示成「—」 */
      v.textContent=(d&&d.error)||'—'; u.textContent=''; return;
    }
    var l=d.limits||{}, us=d.usage||{};
    v.textContent=vsPlanName(d)+(d.plan!=='free'&&d.expires?('（至 '+new Date(d.expires*1000).toLocaleDateString()+'）'):'');
    /* 已付款、等现套餐到期才生效的换档：写明从哪天起，别让人以为钱白花了 */
    if(d.next&&d.next.sku){
      v.textContent+=' · 已预约 '+(((d.skus||{})[d.next.sku]||{}).name||d.next.sku).replace(/^Codeboss\s*|^CodeBoss\s*/i,'')+
        ' '+new Date(d.next.from*1000).toLocaleDateString()+' 起';
    }
    var go=document.getElementById('vsPlanGo');
    if(go) go.textContent=vsPayLabel(d);
    /* 2026-11-17 起全档统一「次数+秒数」口径（免费同款）：消息条数 / 语音秒数 /
       模型次数（0 = 不限）。时长计费已退役，不再显示远程时长行；
       老用户已购的时长包余额照旧展示（新模型下不扣，见计费文档 §6-5）。 */
    var _p0=function(n){ return Number(n||0)>0?String(n):'不限'; };
    var qTxt='消息 '+(_p0(l.sends_day)==='不限'?'不限':(Number(us.sends_day||0)+'/'+l.sends_day+' 条'))+
      ' · 语音 '+(_p0(l.asr_sec_day)==='不限'?'不限':(Number(us.asr_day||0)+'/'+l.asr_sec_day+' 秒'))+
      ' · 模型 '+(_p0(l.llm_day)==='不限'?'不限':(Number(us.llm_day||0)+'/'+l.llm_day+' 次'));
    if(Number(us.credit||0)>0) qTxt+=' · 时长包余 '+useDurM(Number(us.credit||0));
    u.textContent=qTxt+
      ' · 开发机 '+(us.hosts||0)+' 台/'+
      (l.hosts?l.hosts+' 台':'∞')+' · 客户端 '+(us.clients||0)+'/'+(l.clients||'∞')+
      '（24 小时没连接自动释放）';
  });
}
/* 套餐/升级入口总开关：先全隐藏（设置页卡片、锁提示、额度浮层、vsPay 都拦）。
   中继侧 /r/plans 也关着（relay.py 的 PLANS_OPEN），恢复时两边一起开。 */
var PLANS_OFF=1;
function vsPay(){
  if(PLANS_OFF){ toast('套餐升级暂未开放，先等等'); return; }
  track('pay');
  var go=function(b){
    b=String(b||'').replace(/\/+$/,'');
    if(!b){ toast('中继连不上，稍后再试',true); return; }
    sheetOpen(b+'/r/plans?emb=1','套餐与续费');
  };
  if(PLAND&&PLAND.relay_base){ go(PLAND.relay_base); return; }
  toast('正在连接中继…');          /* 进页面那次查询可能还没回来/超时，点按钮时现场再查一次 */
  xget('/api/plan',function(d){
    if(d&&d.relay_base){ go(d.relay_base); return; }
    toast((d&&d.error)||'中继连不上，稍后再试',true);
  });
}
function vsVerRefresh(){          /* 进设置页 / 回到前台自动查一次（只查不装） */
  const row=document.getElementById('vsVerRow'), tv=document.getElementById('vsVer');
  const btnRow=document.getElementById('vsUpdBtns');
  /* 自建中继不做 APK/TestFlight 分发（caps.apk=false）：检查/下载/内测入口整块收起，
     升级走官方渠道（docs/selfhost-relay.md） */
  const apkok=capsOk('apk');
  if(!window.CB){                 /* 浏览器：没壳也能下安卓 APK，按钮留着 */
    if(row) row.style.display='none';
    if(btnRow) btnRow.style.display=apkok?'':'none';
    return;
  }
  if(!CB.apkVer&&!CB.appVer){     /* 壳报不了版本：整块收起来 */
    if(row) row.style.display='none';
    if(btnRow) btnRow.style.display='none';
    return;
  }
  if(row) row.style.display=apkok?'':'none';
  const tfRow=document.getElementById('vsTfRow');   /* iOS：公开内测入口常驻（安卓没有这行） */
  if(tfRow) tfRow.style.display=(CB.appVer&&!CB.apkVer&&apkok)?'':'none';
  const loc=vsVerLocal(); const inst=loc.code;
  tv.textContent=loc.txt; tv.style.color='';
  if(btnRow) btnRow.style.display=apkok?'':'none';
  const gt=document.getElementById('grpVerT'); if(gt) gt.textContent='版本更新';
  if(vsUpdV>inst){   /* 本轮已查出的新版还没装：保持高亮和按钮 */
    tv.textContent=loc.txt+' → v'+vsUpdNamed; tv.style.color='#ffb900';
    vsUpdShow(); return;
  }
  xget(vsVerApi(),function(d,st){
    if(st!==200||!d||!d.code||!inst) return;
    if(d.tf_url) vsTfUrl=d.tf_url;   /* iOS：公开内测链接，存着给「打开 TestFlight」用 */
    if(d.code>inst){
      vsUpdV=d.code; vsUpdNamed=d.ver; vsUpdUrl=d.url||'';
      tv.textContent=loc.txt+' → v'+d.ver; tv.style.color='#ffb900';
      vsUpdShow();
      if(vsUpdHinted!==d.code){ vsUpdHinted=d.code; toast('发现新版本 v'+d.ver+'，可点「更新 App」'); }
    }
  });
}
/* 安卓查 APK 版本，iOS 查 .ipa 版本——两个通道都在中继上，接口一样。
   中继按 ?product= 分槽答题（缺省 codeboss），壳原生层的检查一直带产品线；
   页面来源不定（defaultUrl 带 query、扫码配对的 URL 可能不带），
   所以 query 优先、壳每次启动种的 cb_product cookie 兜底。 */
function vsVerApi(){
  if(window.CB && CB.appVer && !CB.apkVer) return '/api/ios_ver';
  var p='', mm;
  if((mm=(location.search||'').match(/[?&]product=([^&]*)/))) p=mm[1];
  if(!p && (mm=(document.cookie||'').match(/(?:^|;\s*)cb_product=([^;]*)/))) p=mm[1];
  p=(p||'').trim();
  return '/api/apk_ver'+(p?('?product='+encodeURIComponent(p)):'');
}
function vsCheckUpd(manual){
  const tv=document.getElementById('vsVer'), btn=document.getElementById('vsUpdBtn');
  const loc=vsVerLocal();
  if(btn){ btn.disabled=true; btn.textContent='检查中…'; }
  xget(vsVerApi(),function(d,st){
    if(btn){ btn.disabled=false; btn.textContent='检查客户端更新'; }
    if(st!==200||!d||!d.code){ toast('检查失败，稍后再试',true); return; }
    if(d.tf_url) vsTfUrl=d.tf_url;   /* iOS：公开内测链接 */
    if(d.code>loc.code){
      vsUpdV=d.code; vsUpdNamed=d.ver; vsUpdUrl=d.url||'';
      if(tv){ tv.textContent=loc.txt+' → v'+d.ver; tv.style.color='#ffb900'; }
      vsUpdShow();
      toast(manual?('发现新版本 v'+d.ver+'，可点「更新 App」'):('可更新到 v'+d.ver));
    }else{
      vsUpdHide();
      if(tv){ tv.textContent=loc.txt; tv.style.color=''; }
      if(manual) toast('已是最新版本');
    }
  });
}
/* 开发机服务（host）版本：只在用户点「检查更新」时才联网检查。
   打开设置页 / 切回前台只显示上次检查的结果，避免每次进页面都去打扰开发机。
   平时 host 自己会自动查并自动更新，这里只是手动兜底。 */
let hsUpdVer='';
function hsShowCached(){
  const tv=document.getElementById('hsVer'), go=document.getElementById('hsGo');
  if(!tv) return;
  let pend=null; try{ pend=localStorage.getItem('cb_hs_pending'); }catch(e){}
  if(pend){   // 刚点过更新：host 正在重启，别把旧的「可更新」状态又摆出来
    tv.textContent='更新中，等待开发机重启…'; tv.style.color='#ffb900';
    if(go) go.style.display='none'; return;
  }
  let c=null;
  try{ c=JSON.parse(localStorage.getItem('cb_hs_ver')||'null'); }catch(e){}
  if(!c||!c.v){ tv.textContent='未检查'; tv.style.color=''; if(go) go.style.display='none'; return; }
  hsUpdVer=c.u||'';
  if(c.u){
    tv.textContent='v'+c.v+' → v'+c.u; tv.style.color='#ffb900';
    if(go) go.style.display='';
  }else{
    tv.textContent='v'+c.v+(c.t?('（'+new Date(c.t).toLocaleDateString()+' 检查）'):'');
    tv.style.color='';
    if(go) go.style.display='none';
  }
}
async function hsCheck(manual){
  const tv=document.getElementById('hsVer'), btn=document.getElementById('hsBtn');
  let got=null;
  if(btn){ btn.disabled=true; btn.textContent='检查中…'; }
  try{
    /* 静默复查（更新后等待 host 重启）给短超时：host 还没起来时别一次挂 15s */
    const r=await xj('/api/version'+(manual?'?force=1':''), manual?undefined:{timeout:8000});
    const d=r.json||{};
    if(!d.ok) throw 0;
    got=d;
    tv.textContent='v'+d.version; tv.style.color='';
    if(d.update){
      hsUpdVer=d.relay_ver||d.gh_tag||'新版本';
      tv.textContent='v'+d.version+' → v'+hsUpdVer; tv.style.color='#ffb900';
      document.getElementById('hsGo').style.display='';
      if(manual) toast('开发机服务可更新到 v'+hsUpdVer);
    }else{
      document.getElementById('hsGo').style.display='none';
      try{ localStorage.removeItem('cb_hs_pending'); }catch(e){}
      if(manual) toast(d.err?('检查失败：'+d.err):'开发机服务已是最新');
    }
    try{ localStorage.setItem('cb_hs_ver',JSON.stringify({v:d.version,t:Date.now(),u:d.update?hsUpdVer:''})); }catch(e){}
  }catch(e){ if(manual) toast('检查失败：网络错误',true); }
  if(btn){ btn.disabled=false; btn.textContent='检查服务更新'; }
  return got;
}
/* 更新后 host 会重启：复查到版本对上为止，然后明说一声「更新好了」。
   （以前这里全程静默，更新完页面只是默默变成新版本号，用户以为没反应。）
   页面若因 page_ver 自更新而重载，cb_hs_pending 还在，init() 会接着等，
   结果照样会提示一次；pending 清掉后就不再重复。 */
function vgeJs(a,b){
  if(!b||b==='1') return true;
  const p=s=>String(s).split('.').map(n=>parseInt(n,10)||0);
  const A=p(a),B=p(b);
  for(let i=0;i<Math.max(A.length,B.length);i++){
    if((A[i]||0)!==(B[i]||0)) return (A[i]||0)>(B[i]||0);
  }
  return true;
}
async function hsWait(){
  let want=''; try{ want=localStorage.getItem('cb_hs_pending')||''; }catch(e){}
  if(!want) return;
  let last=null, tries=0;
  const deadline=Date.now()+15*60000;   // 最多守 15 分钟，之后不再挂着「更新中」
  while(tries<12&&Date.now()<deadline){
    await new Promise(r=>setTimeout(r,tries?5000:4000));
    /* App 在后台时请求都发不出去（awayNow 直接返回空）：这时的等待不算次数，
       回前台接着等——用户切出去再切回来，照样能收到「更新好了」的提示 */
    if(hiddenNow()) continue;
    const d=await hsCheck(0);
    tries++;
    if(!d) continue;
    last=d;
    /* 版本对上就算成功——哪怕中继那边又报了更新的版本（装的确实是想要的那个） */
    if(vgeJs(d.version,want)){
      try{ localStorage.removeItem('cb_hs_pending'); }catch(e){}
      hsDone(d.version);
      return;
    }
  }
  /* 等到超时：别把「更新中」一直挂着，把真实结果说出来，用户想再试一次再点 */
  try{ localStorage.removeItem('cb_hs_pending'); }catch(e){}
  try{ hsShowCached(); }catch(e){}
  const b=document.getElementById('hsGo');
  if(b){ b.disabled=false; b.textContent='更新服务'; }
  if(hiddenNow()) return;
  if(last) toast('开发机服务还是 v'+last.version+'，稍后再点一次更新',true,4000);
  else toast('没连上开发机，等它恢复后在设置里点「检查服务更新」',true,4000);
}
function hsDone(ver){
  /* 更新完成的收尾：按钮复位、缓存状态刷成新版本、给一个看得见的提示 */
  const b=document.getElementById('hsGo');
  if(b){ b.disabled=false; b.textContent='更新服务'; }
  try{ hsShowCached(); }catch(e){}
  if(!hiddenNow()){ toast('开发机服务已更新到 v'+ver,false,4000); }
}
async function hsUpdate(){
  const btn=document.getElementById('hsGo');
  if(btn){ btn.disabled=true; btn.textContent='更新中…'; }
  try{
    const r=await xj('/api/update',{method:'POST'});
    const d=r.json||{};
    if(d.uptodate){   // 本来就没新版本：正常提示，别报红
      toast(d.why||'已是最新版本');
      if(btn){ btn.disabled=false; btn.textContent='更新服务'; }
      return;
    }
    if(d.ok){
      toast('开发机更新中，完成后会提示…',false,3000);
      try{ localStorage.setItem('cb_hs_pending', hsUpdVer||'1'); }catch(e){}
      hsWait();
    }else{
      toast(d.why||'更新失败',true);
      if(btn){ btn.disabled=false; btn.textContent='更新服务'; }
    }
  }catch(e){ toast('网络错误',true); if(btn){ btn.disabled=false; btn.textContent='更新服务'; } }
}
function vsIosManifestUrl(){   /* itms-services 清单地址（中继的 /ios.plist） */
  let base = (vsUpdUrl || (location.origin||'')).replace(/\/ios-install\/?$/,'');
  if(!/\/ios\.plist$/.test(base)) base += '/ios.plist';
  return base;
}
function vsUpdateApp(){   /* 唯一的下载/安装入口 */
  const b=document.getElementById('vsUpdGo');
  if(b&&b.dataset.perm==='1'&&window.CB&&CB.grantInstallPerm){
    try{ CB.grantInstallPerm(); show('打开「安装未知应用」开关后返回，会自动继续安装'); return; }catch(e){}
  }
  /* iOS：公开用户只能走 TestFlight（陌生设备装不了开发签名包）。
     有公开链接就先去 TestFlight 更新——那里点「接受」「更新」就行，配对和设置都保留。
     没有链接（老中继）才退回系统安装清单那条老路。 */
  if(window.CB && CB.appVer && !CB.apkVer){
    if(vsTfUrl){
      try{ CB.openExternal(vsTfUrl); show('已打开 TestFlight，点「接受」再点「安装」'); return; }catch(e){}
    }
    const u = vsUpdUrl || ((location.origin||'')+'/ios-install');
    /* 老路：开发签名包走系统安装清单（直接开清单 → 系统当场开始装）。 */
    try{
      if(CB.openExternal){
        const its='itms-services://?action=download-manifest&url='+encodeURIComponent(vsIosManifestUrl());
        CB.openExternal(its);
        show('系统正在下载安装，按提示输入锁屏密码即可');
        return;
      }
      CB.openExternal(u); show('已打开安装页，点「点这里安装」'); return;
    }catch(e){}
    location.href=u; return;
  }
  try{
    if(window.CB&&CB.checkUpdate){
      if(b){ b.textContent='准备下载…'; b.disabled=true; b.dataset.busy='1'; }
      __dlTick=Date.now();
      /* 25s 内一条进度都没来（多数是壳里版本检查就说「已是最新」）→ 按钮恢复可点。
         到点还得接着看：下载拖过 25s 时以前只查一次，之后按钮就永远复活不了；
         壳已经接管到安装阶段（__dlHold）就交给它，别去抢对话框的文案。 */
      __dlHold=0;
      (function still(){
        setTimeout(function(){
          if(__dlHold) return;
          var b2=document.getElementById('vsUpdGo');
          if(b2 && b2.disabled && __dlTick && Date.now()-__dlTick>=24000){
            b2.textContent='更新 App'; b2.disabled=false; delete b2.dataset.busy;
            return;
          }
          if(__dlTick) still();
        },25000);
      })();
      CB.checkUpdate(); show('下载中，装好会自动重启客户端'); return;
    }
  }catch(e){}
  location.href='/'+cbLine()+'.apk';   // 老壳没有桥：直接触发下载（按本线取包，别给老板塞工头的包）
}
function vsOpenTf(){   /* iOS 公开内测：开 Apple 的 TestFlight 公开链接（接受→安装/更新） */
  const u=vsTfUrl||((location.origin||'')+'/ios-install');
  try{ if(window.CB&&CB.openExternal){ CB.openExternal(u); return; } }catch(e){}
  location.href=u;
}
/* 从系统设置/安装器回来：版本和权限都可能变了，重画版本行 */
document.addEventListener('visibilitychange',function(){
  /* 剩余时长完全用中继的真数（后台那段本来就不会被记），回前台补拉一轮校正。 */
  if(!hiddenNow()) planRefresh();   /* 回前台先刷新顶部「今日时长」 */
  if(!hiddenNow()&&setPageOpen()){ vsVerRefresh(); hsShowCached(); vsNetRefresh(); }
  /* 回前台且正在看某个会话：它的通知算已读；画面/历史补拉一次（后台期间推送断开、
     回来若只是断点续传，可能只拿到尾巴——补齐靠这一次） */
  if(!hiddenNow()&&SID){
    try{ window.CB&&CB.setView&&CB.setView(SID); }catch(e){}
    seenMark(SID);
    /* 在底部才补拉：强拉会 toBottom，把人正在翻的历史顶走。往上翻着看时
       不打扰——推送恢复后下一次输出照样会补上（那时已经在底部）。 */
    if(pageIdx===1&&atBottom()&&SCROLL_WANT===null){
      try{ if(fullColor){ liveflowPoll(true); } else { screenPoll(true); } }catch(e){}
    }
  }
});
/* ── 运行模式：kiosk 切换要走二次确认（锁定不容易反悔）；壳回调刷新选中态 ── */
function vsRunRefresh(){
  const card=document.getElementById('vsRunCard');
  if(!window.CB||!CB.getRunMode){ card.style.display='none'; return; }
  card.style.display='';
  const m=CB.getRunMode();
  seg('vsModeApp',m!=='kiosk'); seg('vsModeKiosk',m==='kiosk');
}
function cbRunMode(btn,m){
  if(!window.CB||!CB.setRunMode){ toast('需要安卓客户端壳'); return; }
  if(m==='kiosk') armConfirm(btn,'再次点击确认：进入桌面接管（手机将锁定在 Codeboss）',function(){ CB.setRunMode('kiosk'); });
  else{ CB.setRunMode('app'); toast('已切回 App 模式'); }
}
window.__cbRunMode=function(m){ try{ vsRunRefresh(); }catch(e){} };