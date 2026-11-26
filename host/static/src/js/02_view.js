// SPDX-License-Identifier: AGPL-3.0-or-later

function setView(v){
  if(v==='raw'&&!fullColor){ toast('该会话未接入全彩镜像',true); v='live'; }
  /* 这台手机的 xterm 起不来（老内核）：留在原样只会黑屏。退实时，并忘掉这个选择 */
  if(v==='raw'&&typeof Terminal==='undefined'){
    toast('这台手机不支持原样视图，已切回实时',true);
    try{ localStorage.removeItem('pty_view'); }catch(e){}
    wantView=''; v='live';
  }
  /* 只有真的变了才记，带上切到哪个视图：光看「切视图」不知道切的是重排还是原样 */
  if(v!==view) track('view_switch', v);
  view=v;
  /* 视图选择落到开发机日志：黑屏时一眼看出停在哪个视图、是不是被老内核挡回来 */
  try{ window.__cbDbg&&window.__cbDbg('Vw '+v+(fullColor?' fc':'')+(typeof Terminal==='undefined'?' noT':''),24); }catch(e){}
  // 进原样由 rawSnap→syncRawMode 定夺（终端比面板高时不锁外层滚动）；离开原样立刻解锁
  try{ if(v!=='raw') document.getElementById('pgCtrl').classList.remove('rawmode'); }catch(e){}
  document.getElementById('t_live').className='tab'+(v==='live'?' on':'');
  document.getElementById('t_raw').className='tab'+(v==='raw'?' on':'');
  /* 对话视图（Codex 桌面版：没有窗口画面）没有重排/原样可选，别把死路的按钮摆在那 */
  document.getElementById('t_live').style.display=(v==='chat')?'none':'';
  document.getElementById('t_raw').style.display=(v==='chat')?'none':'';
  document.getElementById('fsm').style.display=(v==='live')?'':'none';
  document.getElementById('fsp').style.display=(v==='live')?'':'none';
  /* 引用终端只对全彩实时视图有意义（原样视图有原生选字，聊天视图没有终端） */
  document.getElementById('tabQuote').style.display=(v==='live'&&fullColor)?'':'none';
  /* #feed 在 CSS 里是 display:none，这里必须写 block，置空会退回到隐藏 */
  document.getElementById('feed').style.display=(v==='chat')?'block':'none';
  document.getElementById('live').style.display='none';
  document.getElementById('livehist').style.display='none';
  document.getElementById('liveflow').style.display='none';
  document.getElementById('live2').style.display='none';
  if(v==='live'){
    if(fullColor){
      document.getElementById('livehist').style.display='block';
      document.getElementById('liveflow').style.display='block';
      toBottom();
      liveflowPoll();
    }else{
      document.getElementById('live').style.display='block';
      screenPoll();
    }
  }else if(v==='raw'){
    document.getElementById('live2').style.display='block';
    rawSnap();
  }
  /* 文档浮条跟会话页走，不挑视图：离开会话页由 docFloatRender 自己按 pageIdx 收，
     切到实时/原样也得重判（Claude 会话默认就在实时视图，收了就永远看不到） */
  try{ docFloatRender(); }catch(e){}
}
/* 会话页内容缓存（只存「手头这一屏」：滚动历史 + 当前屏）：
   App 更新/进程被杀后页面重载，先把上次的内容画出来，推送/轮询回来再原地替换——
   不至于开页先空一段、只看到最后一点，等一个来回才补齐。按 sid 存、24 小时有效。 */
var LIVE_CK='codeboss_live_cache_v1', __liveCkAt=0, LIVE_CK_N=3;
/* 缓存结构：{m:{sid:{flow,hist,at}}}，最多留 3 个会话。只留一份时，来回切会话
   每次都从「加载中…」开始——157KB 历史要过一趟隧道才出现，看着就是「重新加载、卡一下」。 */
function _liveCkMap(){
  try{
    const d=JSON.parse(localStorage.getItem(LIVE_CK)||'null');
    if(d&&d.m&&typeof d.m==='object') return d;
  }catch(e){}
  return {m:{}};
}
function liveCacheSave(force){
  if(!SID||!fullColor||(!lastFlowHtml&&!lastHistHtml)) return;
  var now=Date.now(); if(!force&&now-__liveCkAt<4000) return; __liveCkAt=now;
  try{
    const d=_liveCkMap();
    d.m[SID]={flow:lastFlowHtml,hist:lastHistHtml,at:now};
    const ks=Object.keys(d.m).sort(function(a,b){ return (d.m[a].at||0)-(d.m[b].at||0); });
    while(ks.length>LIVE_CK_N) delete d.m[ks.shift()];
    localStorage.setItem(LIVE_CK,JSON.stringify(d));
  }catch(e){}
}
function liveCacheSeed(){
  try{
    /* 先看内存预取（比 localStorage 新，也不会因为只留 3 份被挤掉）：
       来回滑会话时直接画出来，不用每滑一页都等一趟隧道（2026-11-13）。 */
    const p=PF[SID];
    if(p&&Date.now()-p.at<120000&&(p.html||p.hist)){
      fullColor=true;
      /* 换会话的那一下只画「最后一屏 + 历史尾巴」：整段历史一次灌进去要几十毫秒的
         排版，正好卡在滑动动画中间，看着就是一顿（2026-11-13 实测 6 倍降速下 40ms+）。
         整段历史等动画落定后再补，滚动位置由浏览器锚定，看不出替换。 */
      if(p.hist){ const t=histTail(p.hist); document.getElementById('livehist').innerHTML=t; lastHistHtml=t; }
      if(p.html){ document.getElementById('liveflow').innerHTML=p.html; lastFlowHtml=p.html; }
      setView('live');
      toBottom();
      if(p.hist) seedFullHist(SID,p.hist,380);
      return true;
    }
    const e=_liveCkMap().m[SID];
    if(!e||(!e.flow&&!e.hist)) return false;
    if(Date.now()-Number(e.at||0)>24*3600*1000) return false;
    fullColor=true;                 // 缓存只存全彩会话；真值随后由 msg 事件纠正
    if(e.hist){ document.getElementById('livehist').innerHTML=e.hist; lastHistHtml=e.hist; }
    if(e.flow){ document.getElementById('liveflow').innerHTML=e.flow; lastFlowHtml=e.flow; }
    setView('live');                // 会顺带 liveflowPoll() 拉一次真内容替换掉缓存
    toBottom();
    return true;
  }catch(e){ return false; }
}
/* 聊天墙本地缓存：跟上面的实时屏缓存同一套思路——进会话先把上次看到的内容画
   出来（缓存优先），host 真值回来逐条比对、一样不重画；没缓存才干等 host。
   关闭再召回、断网、隧道慢，墙都不再是空白（2026-11-25 用户）。
   按 sid 存最近 4 个会话各 300 条、24 小时有效。 */
var MSG_CK='codeboss_msg_cache_v1', MSG_CK_N=4, MSG_CK_AT={}, MSG_CK_ARM={};
function msgCacheMap(){
  try{
    const d=JSON.parse(localStorage.getItem(MSG_CK)||'null');
    if(d&&typeof d==='object') return d;
  }catch(e){}
  return {};
}
function msgCacheSave(sid,msgs,force){
  if(!sid||!msgs||!msgs.length) return;
  const now=Date.now();
  if(!force&&now-(MSG_CK_AT[sid]||0)<15000) return;   // 流式期间增量很密：15s 落一次盘够
  MSG_CK_AT[sid]=now;
  try{
    const d=msgCacheMap();
    d[sid]={at:now,m:msgs.slice(-300)};
    const ks=Object.keys(d).sort((a,b)=>(d[a].at||0)-(d[b].at||0));
    while(ks.length>MSG_CK_N) delete d[ks.shift()];
    try{ localStorage.setItem(MSG_CK,JSON.stringify(d)); }
    catch(q){ delete d[ks[0]]; localStorage.setItem(MSG_CK,JSON.stringify(d)); }  // 配额满丢最旧再试一次
  }catch(e){}
}
function msgCachePrime(sid,delay){
  const run=function(){
    try{
      if(SID!==sid||feedMsgs.length) return;   // 滑走了 / 真数据先到：缓存让位
      const e=msgCacheMap()[sid];
      if(!e||!e.m||!e.m.length) return;
      if(Date.now()-Number(e.at||0)>24*3600*1000) return;
      feedMsgs=e.m.slice();
      renderFeed();
      MSG_CK_ARM[sid]=1;   // 现在显示的是缓存：这一轮网真断了要吱一声，别装没事
    }catch(e){}
  };
  if(delay) setTimeout(run,delay); else run();
}
function msgCacheDisarm(sid){ try{ delete MSG_CK_ARM[sid]; }catch(e){} }
function msgCacheOfflineNote(sid){
  try{
    if(MSG_CK_ARM[sid]){ delete MSG_CK_ARM[sid]; toast('网络不通，先显示上次的内容',true); }
  }catch(e){}
}
/* 相邻会话预取：滑到下一/上一页时直接有内容可画，不用每滑一页都重新加载。
   只在内存里留 6 份、2 分钟内有效；/api/ext_live/ 不记使用分钟。 */
var PF={}, PF_INF={}, SESS_DRAG=0;
/* 历史的尾部（够铺几屏）：切会话时先画这一段，整段等滑动结束再补 */
function histTail(h,n){
  n=n||9000;
  if(!h||h.length<=n) return h||'';
  const i=h.slice(-n).indexOf('<div');
  return i>0?h.slice(-n+i):'';
}
function seedFullHist(sid,h,delay){
  setTimeout(function(){
    try{
      if(SID!==sid||h===lastHistHtml) return;
      const bottom=atBottom();
      document.getElementById('livehist').innerHTML=h; lastHistHtml=h;
      if(bottom) toBottom(); else scrollRestoreTry();
      liveCacheSave();
    }catch(e){}
  },delay||380);
}
function pfSess(sid){
  if(!sid||sid===SID||PF_INF[sid]) return;
  if(PF[sid]&&Date.now()-PF[sid].at<60000) return;   // 一分钟内取过就够新
  PF_INF[sid]=1;
  xj('/api/ext_live/'+sid).then(function(r){
    if(r&&r.ok&&r.json) PF[sid]={html:r.json.html||'',hist:r.json.hist||'',at:Date.now()};
    const ks=Object.keys(PF).sort(function(a,b){ return (PF[a].at||0)-(PF[b].at||0); });
    while(ks.length>6) delete PF[ks.shift()];
    /* 正文到手后趁空闲把这页排好版（几十毫秒的排版放在这会儿，别留到滑动时） */
    const idle=(window.requestIdleCallback||function(f){ setTimeout(f,180); });
    try{ idle(function(){ if(SESS_DRAG) return; pvDrop(sid); pvBuild(sid); },{timeout:2500}); }catch(e){}
  }).catch(function(){}).then(function(){ delete PF_INF[sid]; });
}
/* ── 预排页：相邻会话的正文先在屏幕外排好版 ──
   滑过去时它和当前页一起平移，两页都是 transform（合成线程），中间不排版、不换 DOM；
   等内容真正换到会话页上时，它已经盖满屏幕，卡的那一下看不见（2026-11-13 实测：
   换内容那一下在 6 倍降速下要 40~70ms，正好卡在滑动中间 = 一顿）。 */
var PV={};
function pvBuild(sid){
  const p=PF[sid]||{}; if(!p.html&&!p.hist) return null;
  if(PV[sid]) return PV[sid].el;
  try{
    const el=document.createElement('div');
    el.className='pvpage';
    const h=document.createElement('div'); h.className='livehist'; h.innerHTML=histTail(p.hist);
    const f=document.createElement('div'); f.className='liveflow'; f.innerHTML=p.html||'';
    el.appendChild(h); el.appendChild(f);
    document.body.appendChild(el);
    el.style.display='block';
    el.style.transform='translateX(-400%)';
    el.offsetHeight;                 // 排版（此刻没有动画，卡也看不见）
    el.scrollTop=el.scrollHeight;    // 和会话页一样贴着底
    PV[sid]={el:el,at:Date.now()};
    pvTrim();
    return el;
  }catch(e){ return null; }
}
function pvTrim(){
  const ks=Object.keys(PV).filter(function(k){ return k!==SID; });
  while(ks.length>2) pvDrop(ks.shift());
}
function pvDrop(sid){
  const v=PV[sid]; delete PV[sid];
  try{ if(v&&v.el&&v.el.parentNode) v.el.parentNode.removeChild(v.el); }catch(e){}
}
function pvTake(sid){
  const v=PV[sid]; if(!v) return null;
  if(Date.now()-v.at>180000){ pvDrop(sid); return null; }
  return v.el;
}
function pvShow(el,x){
  try{ clearTimeout(el.__pvHideT); }catch(e){}   // 上一次「延迟收掉」还没到点就又用了它
  el.style.display='block';
  el.style.transition='none';
  el.style.transform='translateX('+x+'px)';
  el.offsetHeight;                   // 先落位再动，别让第一帧从 0 开始
}
function pvHide(el){
  try{ el.style.display='none'; el.style.transition='none'; el.style.transform=''; }catch(e){}
}
/* 当前这一页自己也要留一份预排页：滑回来（上一个会话）时才不用走老路。
   内容直接取屏幕上正在看的 DOM，不必再走一趟隧道。 */
function pvBuildCur(){
  if(SESS_DRAG) return;                       // 正在滑：别在这时候强制排版
  const h=document.getElementById('livehist'), f=document.getElementById('liveflow');
  if(!SID||!h||!f||(!h.innerHTML&&!f.innerHTML)) return;
  PF[SID]={html:f.innerHTML,hist:h.innerHTML,at:Date.now()};
  pvDrop(SID); pvBuild(SID);
}
function pfNeighbors(){
  const i=curOpenIdx(); if(i<0) return;
  setTimeout(function(){
    if(pageIdx!==1) return;
    if(OPEN[i+1]) pfSess(OPEN[i+1]);
    if(OPEN[i-1]) pfSess(OPEN[i-1]);
    /* 当前页的预排页：没有或放太久了就趁空闲重排一份（60 秒内不重复做） */
    if(!PV[SID]||Date.now()-PV[SID].at>60000){
      const idle=(window.requestIdleCallback||function(fn){ setTimeout(fn,180); });
      try{ idle(function(){ if(!PV[SID]||Date.now()-PV[SID].at>60000) pvBuildCur(); },{timeout:3000}); }catch(e){}
    }
  },700);
}
/* 会话正文里正选着字（长按复制）：这一轮先别重画，否则选区被 innerHTML 冲掉 */
function liveSelOn(){
  try{
    const s=document.getSelection();
    if(!s||s.isCollapsed||!String(s).trim()) return false;
    const inLive=n=>{ const e=n&&(n.nodeType===1?n:n.parentElement);
      return !!(e&&e.closest&&e.closest('#live,#livehist,#liveflow,#live2')); };
    return inLive(s.anchorNode)||inLive(s.focusNode);
  }catch(e){ return false; }
}
function renderLive(d,force){
  if(pageIdx!==1||view!=='live'||!fullColor||SESS_DRAG) return;
  const wasBottom=atBottom();
  /* 弹层刚冒出来 = 电脑上要你选：不管当时滚到哪儿都拉回底部，不然选项在屏幕外，
     手机上看着像「什么都没发生」。 */
  const hasDlg=(d.html||'').indexOf('class="dblk"')>=0;
  const hadDlg=lastFlowHtml.indexOf('class="dblk"')>=0;
  /* 屏幕上有可点的选项 → 会话页不用再浮一条按钮（在下面 pendRender 里用） */
  const hasOpt=(d.html||'').indexOf('data-opt="')>=0;
  if(hasOpt!==DLG_OPT_ON){ DLG_OPT_ON=hasOpt; try{ pendRender(); }catch(e){} }
  /* 用户正往上翻历史：先别换 DOM。整块 innerHTML 替换 = 阅读位置被顶走/页面闪一下
     （2026-11-09 实测：输出时页面自己往上顶，得重新滑）。先记着，回到底部后的
     下一轮轮询再补上；「电脑上要你选」的弹层例外——那个必须立刻看到。
     force=刚进页面/回前台的一次性重同步：DOM 还是要重画，不受这条限制。 */
  if(!force&&!wasBottom&&SCROLL_WANT===null&&!(hasDlg&&!hadDlg)) return;
  /* 正在选字复制：内容一换 innerHTML，选区就没了。等选区清掉再补画 */
  if(!force&&liveSelOn()) return;
  let changed=false;
  if(d.html!==lastFlowHtml){
    /* 画一次多久：手机卡的时候要靠这行分清「主机慢」还是「这一屏重画太贵」 */
    const t0=performance.now();
    document.getElementById('liveflow').innerHTML=d.html; lastFlowHtml=d.html; changed=true;
    const dt=performance.now()-t0; if(dt>200) nPendDbg('Rdt F'+Math.round(dt)+'ms '+(d.html.length>>10)+'KB');
  }
  if(changed&&(wasBottom||(hasDlg&&!hadDlg)||force)) toBottom();
  scrollRestoreTry();   // 重载前读到哪儿：内容够了就把位置放回去
  if(changed) liveCacheSave();
}
function renderHist(d,force){
  if(pageIdx!==1||view!=='live'||!fullColor||SESS_DRAG) return;
  const hh=d.hist||'';
  if(hh===lastHistHtml) return;
  /* 同上：翻历史时不重灌历史块（重灌会把滚动位置顶掉），回到底部再补 */
  if(!force&&!atBottom()&&SCROLL_WANT===null) return;
  if(!force&&liveSelOn()) return;
  const t0=performance.now();
  document.getElementById('livehist').innerHTML=hh; lastHistHtml=hh;
  const dt=performance.now()-t0; if(dt>200) nPendDbg('Rdt H'+Math.round(dt)+'ms '+(hh.length>>10)+'KB');
  toBottom();
  scrollRestoreTry();
  liveCacheSave();
}
var LIVE_INF=null;   /* 上一发还没回来就别再发：服务端冷启动回放要 ~1s，
                        定时器不认「还在路上」，慢一次就叠出好几发，越慢越堵 */
async function liveflowPoll(force){
  if((PUSH.st==='on'&&!force)||hiddenNow()) return;
  if(pageIdx!==1||view!=='live'||!fullColor) return;
  if(LIVE_INF&&LIVE_INF===SID) return;
  const lme=SID; LIVE_INF=lme;
  try{
    const r=await xj('/api/ext_live/'+SID);
    if(!r.ok) return;
    const d=r.json;
    if(typeof d.seq==='number') window.__cbHistSeq={sid:SID,seq:d.seq};   /* 断口游标：重连时判 host 是否重启过 */
    if(pageIdx!==1||view!=='live') return;
    renderLive(d,force); renderHist(d,force);
    pfNeighbors();   // 当前这一屏画完了再去预取左右邻居，别抢这一屏的首发
  }catch(e){}
  finally{ if(LIVE_INF===lme) LIVE_INF=null; }
}
/* 断链重连后的接续校验（窗口核心 Phase 1）：hello 前真掉过线才被叫（50s 自续不算）。
   host 重启过（seq 倒退）→ 作废这个会话的预取页和本地缓存、整窗 force 重画——
   上一代旧快照硬接在断口上，就是「注入消息终端隐形」那类静默坑的根源。
   seq 前进 → 正常增量接续，不打扰滚动位置。 */
async function liveResync(){
  if(pageIdx!==1||view!=='live'||!SID) return;
  try{
    const r=await xj('/api/ext_live/'+SID);
    if(!r.ok||!r.json) return;
    const d=r.json;
    const prev=(window.__cbHistSeq&&window.__cbHistSeq.sid===SID)?window.__cbHistSeq.seq:-1;
    const force=(typeof d.seq==='number'&&prev>=0&&d.seq<prev);
    if(force){
      delete PF[SID];
      try{
        const c=JSON.parse(localStorage.getItem(LIVE_CK)||'null');
        if(c&&c.m&&c.m[SID]){ delete c.m[SID]; localStorage.setItem(LIVE_CK,JSON.stringify(c)); }
      }catch(e){}
    }
    renderLive(d,force); renderHist(d,force);
    if(typeof d.seq==='number') window.__cbHistSeq={sid:SID,seq:d.seq};
  }catch(e){}
}
var SNAP_INF=false;
async function rawSnap(){
  // 原样整帧快照：电脑列数+手机叠加行数；full=1 附带更早历史进 xterm 回滚区
  if(SNAP_INF) return false;   // 一整帧在路上：再来一发只会排队
  SNAP_INF=true;
  try{
    const d=rawDims();
    const r=await xj('/api/ext_snap/'+SID+'?rows='+d.rows+'&full=1',{bin:true});
    if(!r.ok) return false;
    const hw=(r.hdr('X-Host-Winsize')||'').split(',');
    if(hw.length===2) hostWin=[parseInt(hw[0],10)||80,parseInt(hw[1],10)||24];
    const ws=(r.hdr('X-Winsize')||d.cols+','+d.rows).split(',');
    const cols=parseInt(ws[0],10)||d.cols, rows=parseInt(ws[1],10)||d.rows;
    if(!xt) initXT(cols,rows,d.fs);
    else if(curWin!==cols+'x'+rows) resetXT(cols,rows,d.fs);
    rawRowsMin=Math.max(rawRowsMin,rows);
    lastFrameHash=null;
    const buf=new Uint8Array(r.buf||[]);
    if(xt){ xt.reset(); xt.write(buf); }
    syncRawMode();
    return true;
  }catch(e){
    /* 终端没建起来（老内核里 xterm 抛错）：别把页面留在黑屏，退回实时视图 */
    if(!xt){ try{ setView('live'); }catch(_){} }
    return false;
  }
  finally{ SNAP_INF=false; }
}
/* 原样视图只留 xterm 一个滚动容器（见 CSS #pgCtrl.rawmode）。万一终端比面板高
   （横屏 + 快捷键条这类极端尺寸），退回外层滚动，别把底部截掉。 */
function syncRawMode(){
  try{
    const pg=document.getElementById('pgCtrl'), l=document.getElementById('live2');
    pg.classList.toggle('rawmode', view==='raw' && l.offsetHeight<=pg.clientHeight+2);
  }catch(e){}
}
function renderScreen(d){
  if(pageIdx!==1||view!=='live'||fullColor) return;
  const lv=document.getElementById('live');
  const wasBottom=atBottom();
  /* 往上翻着看时别整块换字：内容一变高度就变，阅读位置跟着漂。回到底部再继续刷新 */
  if(!wasBottom&&SCROLL_WANT===null) return;
  /* 没全彩镜像也没窗口（Warp 里手敲 claude 起的会话）：host 只会回「未找到终端窗口」。
     光这一行字用户不知道下一步干什么——直接告诉他点列表里的 🔄。 */
  const hint=d.ok?'':((curMeta&&curMeta.desktop)
    ?'\n\nCodex 桌面版没有窗口画面，正在显示对话转录。'
    :'\n\n这个会话不是在 App 里拉起的：回到列表，点这个会话那一行的 🔄 转全彩（会话会重开到原终端），之后就能读写。');
  lv.textContent=(d.screen||'（空）').replace(/[\s　]+$/,'')+hint;   // 裁掉快照尾部空行，末行贴住快捷键条
  toBottom();
  scrollRestoreTry();
}
var SCR_INF=null;
async function screenPoll(force){
  if(!SID) return;   // 同上：没会话就别发 /api/ext_screen/
  if((PUSH.st==='on'&&!force)||hiddenNow()) return;
  if(pageIdx!==1||view!=='live'||fullColor) return;
  if(SCR_INF&&SCR_INF===SID) return;
  const sme=SID; SCR_INF=sme;
  try{
    const r=await xj('/api/ext_screen/'+SID);
    const d=r.json;
    if(pageIdx!==1||view!=='live') return;
    renderScreen(d);
  }catch(e){}
  finally{ if(SCR_INF===sme) SCR_INF=null; }
}
/* ── 状态轮询（推送模式下 interval 全部闸掉，这里只做降级兜底） ── */
let feedMsgs=[];   // 聊天墙本地副本：推送增量按 inc_from splice 后整墙重渲
let DRAFT='';      // 桌面版这一轮的流式草稿（host 连 Codex 守护进程逐字推过来）
let DRAFT_WAIT=false;  // 这一轮交给了桌面版 App：它只落成品，先挂「正在处理」占位
let DRAFT_ST='';       // 这一轮在干什么：正在思考/正在运行命令/正在改文件…
/* 「用时」用 host 报的秒数 + 本机走表：手机时钟不准也不会跑飞 */
let DRAFT_EL=0, DRAFT_EL_AT=0;
let DONE_EL=0;         // 刚答完那一轮的用时（秒）：写在最后一条回答下面，下一轮开始时清掉
/* 流式时贴着底：安卓 WebView 里内联重排后同步写 scrollTop 常常落空（帧还没排完），
   所以贴底要重试几次，并用 scroll 事件维持「贴底/用户自己翻上去了」的状态。 */
let DRAFT_STICK=true;
function stickBottom(){
  const b=ctrlBox(), go=()=>{ b.scrollTop=b.scrollHeight; };
  go(); requestAnimationFrame(go); setTimeout(go,80);
}
(function(){
  const b=ctrlBox();
  if(!b) return;
  b.addEventListener('scroll',function(){
    DRAFT_STICK=b.scrollHeight-b.clientHeight-b.scrollTop<140;
  },{passive:true});
})();
/* ── 极简 markdown → HTML（聊天墙用） ──
   Codex 桌面版的回答就是 md 原文，以前直接 esc() 显示，满屏 ** / # / ```。
   先整体转义再按行排版，行内代码最后还原——任何输入都进不了标签。 */
/*MD_START*/
function mdEsc(s){
  return String(s).replace(/[&<>"]/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
}
/* 本机网页地址 → 同源 /pv/<端口>/：agent 常给出 http://localhost:3000/…
   （成品网页、效果图），手机上直接点会打到手机自己身上，必须由 host 反代。
   只认明确的回环地址，别的域名/局域网 IP 不动。 */
function pvLocalUrl(u){
  const m=/^(?:https?:\/\/)?(?:localhost|127\.0\.0\.1|0\.0\.0\.0|\[::1\]):(\d{1,5})(\/[^\s"'<>]*)?$/i.exec(String(u||'').trim());
  return m?('/pv/'+m[1]+(m[2]||'/')):'';
}
function mdInline(s){
  const codes=[];                       // 行内代码先抽出来，免得里面的 ** / * 被当成强调
  s=s.replace(/`([^`\n]+)`/g,(m,a)=>{codes.push(a);return '\u0000'+(codes.length-1)+'\u0000';});
  s=s.replace(/\*\*([^*\n]+)\*\*/g,'<b>$1</b>')
     .replace(/(^|[^*])\*([^*\n]+)\*/g,'$1<i>$2</i>');
  /* markdown 链接先抽出来：下面的裸地址规则否则会把 href 再包一层 */
  const links=[];
  s=s.replace(/\[([^\]\n]+)\]\((https?:[^)\s]+)\)/g,function(m,txt,url){
    const pv=pvLocalUrl(url);
    const h=pv?('<a href="'+pv+'" onclick="pvOpen(this.href);return false">'+txt+'</a>')
             :('<a href="'+url+'" onclick="return extOpen(this.href)">'+txt+'</a>');
    links.push(h); return '\u0001'+(links.length-1)+'\u0001';
  });
  /* 裸的外网地址（agent 常把 https://… 直接写进回复，点不开=2026-11-23 反馈）：
     也变成可点的链接。本机回环地址不在这吃——留给下一条规则走 /pv/ 反代；
     尾随的英文标点（句号逗号）留在链接外面。 */
  s=s.replace(/(^|[\s，：；（(【>])((?:https?:\/\/)(?!localhost|127\.0\.0\.1|0\.0\.0\.0|\[::1\])[^\s<>"'（）)】。，！？；、」』\u0000\u0001]+)/gi,
    function(m,pre,u){
      const tr=(u.match(/[.,;:]+$/)||[''])[0];
      const core=tr?u.slice(0,u.length-tr.length):u;
      links.push('<a href="'+core+'" onclick="return extOpen(this.href)">'+core+'</a>');
      return pre+'\u0001'+(links.length-1)+'\u0001'+tr;
    });
  /* 裸的本机网页地址（不带 markdown 链接语法也说得出处） */
  s=s.replace(/(^|[\s（(【>])((?:https?:\/\/)?(?:localhost|127\.0\.0\.1|0\.0\.0\.0|\[::1\]):\d{1,5}(?:\/[^\s<>"'）)】。，！？；、」』]*)?)/gi,
    function(m,pre,u){
      const pv=pvLocalUrl(u);
      return pv?(pre+'<a href="'+pv+'" onclick="pvOpen(this.href);return false">'+u+'</a>'):m;
    });
  /* 消息里的 path:line 可点（批次三）：点开直接到那一行。只认相对路径，
    服务端还会按会话 cwd 再拦一次越界——前端这层只是省事。 */
  s=s.replace(/\b([\w][\w.\/-]*\.(?:py|js|mjs|ts|tsx|jsx|go|rs|swift|java|kt|c|h|cpp|hpp|rb|php|sql|sh|md|json|ya?ml|html|css)):(\d{1,6})\b/g,
    function(m,pn,no,off,whole){
      const pre=whole.slice(Math.max(0,off-3),off);
      if(pre.indexOf('//')>=0||pre.indexOf('<')>=0) return m;   /* URL / 已有标签里不动 */
      return '<a href="#" onclick="openCodeAt(\''+pn+'\','+no+');return false">'+m+'</a>';
    });
  s=s.replace(/\u0001(\d+)\u0001/g,(m,i)=>links[+i]);
  return s.replace(/\u0000(\d+)\u0000/g,(m,i)=>'<code>'+codes[+i]+'</code>');
}
function mdToHtml(text){
  const lines=mdEsc(String(text||'')).replace(/\r\n?/g,'\n').split('\n');
  const out=[]; let i=0, list='';
  const close=()=>{ if(list){ out.push('</'+list+'>'); list=''; } };
  while(i<lines.length){
    const ln=lines[i], t=ln.trim();
    const f=ln.match(/^\s*(```|~~~)/);
    if(f){                                   // 代码块：没写收尾也照样收（流式输出中途）
      close(); i++; const body=[];
      while(i<lines.length && lines[i].trim().indexOf(f[1])!==0) body.push(lines[i++]);
      i++;
      while(body.length && !body[body.length-1].trim()) body.pop();
      out.push('<pre><code>'+body.join('\n')+'</code></pre>');
      continue;
    }
    const hd=ln.match(/^\s*(#{1,6})\s+(.*)$/);
    if(hd){ close(); const lv=Math.min(hd[1].length,4);
      out.push('<h'+lv+'>'+mdInline(hd[2])+'</h'+lv+'>'); i++; continue; }
    if(/^([-*_])(\s*\1){2,}$/.test(t)){ close(); out.push('<hr>'); i++; continue; }
    const ul=ln.match(/^\s*[-*+]\s+(.*)$/);
    if(ul){ if(list!=='ul'){ close(); out.push('<ul>'); list='ul'; }
      out.push('<li>'+mdInline(ul[1])+'</li>'); i++; continue; }
    const ol=ln.match(/^\s*\d+[.)]\s+(.*)$/);
    if(ol){ if(list!=='ol'){ close(); out.push('<ol>'); list='ol'; }
      out.push('<li>'+mdInline(ol[1])+'</li>'); i++; continue; }
    if(/^\s*(?:>|&gt;)/.test(ln)){          // 已转义成 &gt;，两种都认
      close(); const q=[];
      while(i<lines.length && /^\s*(?:>|&gt;)/.test(lines[i]))
        q.push(lines[i++].replace(/^\s*(?:>|&gt;)\s?/,''));
      out.push('<blockquote>'+mdInline(q.join('\n')).replace(/\n/g,'<br>')+'</blockquote>');
      continue;
    }
    if(!t){ close(); i++; continue; }
    close(); out.push('<p>'+mdInline(ln)+'</p>'); i++;
  }
  close();
  return out.join('');
}
/*MD_END*/
/* 消息里提到的电脑本地图片/视频：挂个预览卡（视频直接播，图片点开全屏）。
   路径本身留在正文里不动——用户要复制命令的时候还得看得到。 */
function mediaCards(t){
  if(!t) return '';
  const re=/((?:~|\/)[^\s"'`<>()（）\[\]]+\.(?:png|jpe?g|gif|webp|bmp|heic|mp4|m4v|mov|webm|m4a|mp3|wav))/gi;
  const paths=[]; let m;
  /* 别截断：一条消息里 7 个视频只出 3 个卡（用户：视频有 7 个，app 上只显示了 3 个）。
     视频是懒加载（滚到眼前才取）、图片 loading=lazy，多挂几个元素不费流量。 */
  while((m=re.exec(t))){
    const p=m[1].replace(/[.,;:]+$/,'');
    if(paths.indexOf(p)<0) paths.push(p);
  }
  if(!paths.length) return '';
  /* 元素先不挂 src：真正的取法在 mediaHydrate（要按通道决定走局域网直连还是
     走页面自己的请求通道；页面挂在中继域名下时 /api/* 是「相对路径」，会打到
     中继自己的接口上回 401 —— 2026-11-13 实锤：视频点了就报错误码 4、消失）。 */
  return '<div class="media">'+paths.map(p=>{
    const dp=esc(p), fail='onerror="mediaErr(this)"';
    if(/\.(mp4|m4v|mov|webm)$/i.test(p))
      /* preload=none：一条会话里挂着几个视频时，别一打开就自动下几十 MB（手机上就是流量）。
         全屏走原生的：安卓壳 0.11.18 起实现了 CustomView，控制条的全屏键会亮；
         iPhone 不写 playsinline（点播放直接进系统全屏播放器，不靠那颗灰键）。 */
      return `<video data-p="${dp}" controls${NO_PI?'':' playsinline'} preload="none" onclick="mediaKick(this)" ${fail}></video>`;
    if(/\.(m4a|mp3|wav)$/i.test(p))
      return `<audio data-p="${dp}" controls preload="none" onclick="mediaKick(this)" ${fail}></audio>`;
    return `<img data-p="${dp}" loading="lazy" ${fail} onclick="bigImg(this.src)">`;
  }).join('')+'</div>';
}
/* 消息里提到的文档（设计文档 / 方案 / 报告 / 原型页）：挂一张可点的卡。
   工头得把该看的东西递到手边：agent 说到哪份，就在那句话下面点开哪份，
   不用去翻「产出物」列表找。点开走预览层，底下有「✅ 确认 / 💬 提建议」。 */
function docPaths(t){
  if(!t) return [];
  const re=/((?:~|\/|\.{1,2}\/|[A-Za-z0-9_-]+\/)[^\s"'`<>()（）\[\]，。；：、]+\.(?:md|markdown|html?|pdf|docx|pptx|xlsx))/gi;
  const paths=[]; let m;
  while((m=re.exec(t))){
    if(t[m.index-1]===':') continue;          /* https://…/a.md 是网址，不是本机文件 */
    const p=m[1].replace(/[.,;:）)\]]+$/,'');
    if(paths.indexOf(p)<0) paths.push(p);
  }
  return paths;
}
function docCards(t){
  const paths=docPaths(t);
  if(!paths.length) return '';
  return '<div class="docs">'+paths.map(function(p){
    const e=String(p).split('.').pop().toLowerCase();
    const ic=e==='html'||e==='htm'?'🌐':(e==='pdf'?'📕':(e==='md'||e==='markdown'?'📝':'📄'));
    return '<div class="dcard" data-dp="'+esc(p)+'" onclick="docOpen(this)">'+
      '<span class="dic">'+ic+'</span>'+
      '<span class="dname">'+esc(p.split('/').pop())+'</span>'+
      '<span class="dact">查看</span></div>';
  }).join('')+'</div>';
}
function docOpen(el){
  const p=el&&el.getAttribute&&el.getAttribute('data-dp');
  if(!p) return;
  docMarkSeen(p);
  fvPath=p; fvName=p.split('/').pop();
  fviewShow(p, fvName);
}
/* ── 工头递文档：这一轮 agent 写了文档，会话里直接浮出来 ──
   2026-11-20 用户：只在「产出物」里能看是基础功能，我要的是会话里当场浮一条、
   点一下直接跳进查看页。看过/划走的记在本地，不再反复浮。
   ponytail: 只认「最新一条有正文的回答」里提到的文档；agent 写了却没说路径时
   不浮（要覆盖得等产出物轮询接进来）。 */
function docSeenAll(){
  try{ return JSON.parse(localStorage.getItem('docSeen')||'{}')||{}; }catch(e){ return {}; }
}
function docMarkSeen(p){
  if(!p) return;
  try{
    const m=docSeenAll(); m[p]=Date.now();
    const ks=Object.keys(m);
    if(ks.length>200){                       /* 只留最近看过的一批：别把 localStorage 撑爆 */
      ks.sort(function(a,b){ return m[a]-m[b]; });
      ks.slice(0,ks.length-200).forEach(function(k){ delete m[k]; });
    }
    localStorage.setItem('docSeen',JSON.stringify(m));
  }catch(e){}
}
/* 实时/原样视图不画对话墙（renderFeed 不跑），但那才是 Claude 会话的默认视图：
   浮条得照样判，所以 renderMsg 把消息顺手留在 DOC_MSGS 里，这里没有对话墙就退回看它。 */
var DOC_MSGS=[];
/* 消息里 agent 起好的本机网页服务（http://localhost:5173 …）：要「看到效果」只能
   进跑着的服务，把文件当网页贴出来只是个外壳（相对接口/构建产物都取不到）。
   记成 /pv/<端口>/<路径>，点开由 pvOpen 走 host 反代。 */
function docWebLinks(t){
  if(!t) return [];
  const re=/(?:https?:\/\/)?(?:localhost|127\.0\.0\.1|0\.0\.0\.0|\[::1\]):(\d{1,5})(\/[^\s"'<>()（）\[\]，。；：、]*)?/gi;
  const out=[]; let m;
  while((m=re.exec(t))){
    const u=pvLocalUrl(m[0].replace(/[.,;:）)\]]+$/,''));
    if(u&&out.indexOf(u)<0) out.push(u);
  }
  return out;
}
function docFloatPick(){
  const seen=docSeenAll();
  const src=feedMsgs.length?feedMsgs:DOC_MSGS;
  for(let i=src.length-1;i>=0;i--){
    const m=src[i];
    if(m.role!=='assistant'||!m.text) continue;
    /* 网页服务排前面：先看跑起来的样子，文件页面留着接着看 */
    const all=docWebLinks(m.text).concat(docPaths(m.text))
      .filter(function(p){ return !seen[p]; });
    return all.length?all:null;            /* 只看最后一条有正文的回答 */
  }
  return null;
}
function docFloatRender(){
  const b=document.getElementById('docBar'); if(!b) return;
  const ps=(typeof pageIdx!=='undefined'&&pageIdx===1)?docFloatPick():null;
  if(!ps){ b.classList.remove('on'); return; }
  DOC_FLOAT[SID]=ps;
  const p=ps[0], e=String(p).split('.').pop().toLowerCase();
  const svc=p.indexOf('/pv/')===0;                 /* 本机网页服务：点开进跑着的页面 */
  const ic=document.getElementById('docBarIc');
  if(ic) ic.textContent=svc?'🌐':(e==='html'||e==='htm')?'🌐':(e==='pdf'?'📕':(e==='md'||e==='markdown'?'📝':'📄'));
  document.getElementById('docBarName').textContent=svc?('网页服务 :'+p.split('/')[2]):p.split('/').pop();
  document.getElementById('docBarMeta').textContent=
    svc?'agent 起好的网页服务 · 点开直接看效果'
       :(ps.length>1?'这轮写了 '+ps.length+' 份文档，还有 '+(ps.length-1)+' 份 · 点开直接看'
                    :'这轮写好的文档 · 点开直接看');
  b.classList.add('on');
}
var DOC_FLOAT={};                    /* SID → 当前浮着的文档列表（[0] 就是「查看」打开那篇） */
function docFloatOpen(){
  const ps=DOC_FLOAT[SID]||[]; const p=ps[0];
  if(!p) return;
  docMarkSeen(p);
  DOC_FLOAT[SID]=ps.slice(1);
  docFloatRender();
  /* 网页服务：直接进跑着的页面（host 反代），别拿文件冒充 */
  if(p.indexOf('/pv/')===0){ try{ pvOpen(p); }catch(e){} return; }
  fvPath=p; fvName=p.split('/').pop();
  fviewShow(p, fvName);
}
function docFloatClose(){
  const ps=DOC_FLOAT[SID]||[]; const p=ps[0];
  if(!p) return;
  docMarkSeen(p);
  DOC_FLOAT[SID]=ps.slice(1);
  docFloatRender();
}
/* 文档/页面/模型看完直接表态：确认 = 一句话发回会话；建议 = 预填一段，接着长按
   麦克风说完就发。就地闭环，不用再切回输入框说「那个文档我看过了」。 */
function fvReviewKind(k){
  return k==='text'||k==='code'||k==='page'||k==='model'||k==='image';
}
function fvReviewOk(){
  const p=fvPath;
  fviewClose();
  if(!p) return;
  const ta=document.getElementById('txt');
  const msg='[文档: '+p+' 已确认] 按这个做。';
  if(ta.value.trim()){        /* 输入框里有没发出去的草稿：并进去，不许冲掉 */
    ta.value=msg+'\n'+ta.value;
    try{ ta.focus(); }catch(e){}
    try{ fitTxt(); }catch(e){}
    toast('确认已填好，和你的草稿一起发');
    return;
  }
  ta.value=msg;
  sendMsg();
}
function fvReviewAdvice(){
  const p=fvPath;
  fviewClose();
  if(!p) return;
  const ta=document.getElementById('txt');
  ta.value='[文档: '+p+'] 建议：'+(ta.value?'\n':'');
  try{ ta.focus(); }catch(e){}
  try{ fitTxt(); }catch(e){}
  toast('接着说你的建议，说完就发');
}
/* 图片小，直接取；视频/音频**滚到眼前才取**（中继出方向 ~100KB/s，一条消息里的
   6MB 视频要拉一分钟——一条会话里的全预取，点播放要干等半分钟）。播放器本身不加
   多余按钮：没取到时下面有一行「加载中 N%」，取完就是普通播放器。 */
var MEDIA_IO=null;
/* iPhone：playsinline 会把原生播放器的全屏键弄灰（WebKit 老规矩）。不写它，
   点播放直接全屏——用户要的就是全屏看。安卓/桌面保持 playsinline（卡片内播）。 */
var NO_PI=/\b(?:iPhone|iPod)\b/.test(navigator.userAgent);
function mediaHydrate(){
  try{
    const els=document.querySelectorAll('#feed .media [data-p]');
    if(MEDIA_IO) MEDIA_IO.disconnect();   /* 墙是整面重画的，旧观察对象都作废 */
    for(let i=0;i<els.length;i++){
      const el=els[i];
      if(el.__hy||el.getAttribute('src')) continue;
      if(el.tagName==='VIDEO'||el.tagName==='AUDIO'){
        if(!MEDIA_IO&&typeof IntersectionObserver!=='undefined'){
          MEDIA_IO=new IntersectionObserver(function(es){
            es.forEach(function(e){
              if(!e.isIntersecting) return;
              MEDIA_IO.unobserve(e.target);
              if(e.target.__hy) return;
              e.target.__hy=1; mediaLoad(e.target);
            });
          },{threshold:0.3});
        }
        if(MEDIA_IO){ MEDIA_IO.observe(el); continue; }
      }
      el.__hy=1;
      mediaLoad(el);
    }
  }catch(e){}
}
/* 播放器被点了但还没取到（人在等）：不等进屏判定，立刻开始取 */
function mediaKick(el){
  if(el.getAttribute('src')) return;
  el.__want=1;
  if(el.__hy) return;
  el.__hy=1; mediaLoad(el);
}
/* 取一个媒体：
   ① 局域网直连在线 → 直接给绝对地址（dt 票据在 URL 里，能 Range、能拖进度）；
   ② 否则走页面自己的请求通道（xj，cookie/票据都在里面）拿整段再转 blob。
      中继隧道本来就是整包缓冲，没有流式可丢；关键是能鉴权、能到 host。 */
var MEDIA_BLOB={};   /* 路径 → blob URL：renderFeed 每次重画整面墙，取过的别再下一遍 */
/* 中继的「整包隧道」是一条 res 帧把整个响应带回来，而中继 WS 的单帧上限是 4MiB：
   6MB 视频 base64 后 8.9MB 整帧发过去，连接直接被掐 → 页面只看到 502
   （host 日志里是 send_json 时 Connection lost）。所以按 2MiB 分段要 Range
   （host 的 /api/ext_file 支持 206），自己拼回整段；直连通道不认 Range 就整包一次拿完。 */
var MEDIA_CHUNK=2097152;
function mediaPull(ep,onP){
  return new Promise(function(res,rej){
    const parts=[]; let off=0,total=-1;
    (function step(){
      if(total>=0&&off>=total) return res(new Blob(parts));
      xj(ep,{bin:true,timeout:120000,headers:{Range:'bytes='+off+'-'+(off+MEDIA_CHUNK-1)}})
        .then(function(r){
          if(!r||!r.ok||!r.buf) return rej(r?r.status:0);
          const b=new Uint8Array(r.buf);
          parts.push(b); off+=b.length;
          const m=/bytes\s+\d+-\d+\/(\d+)/.exec((r.hdr&&r.hdr('Content-Range'))||'');
          if(m) total=parseInt(m[1],10);
          else if(r.status===200||!b.length) total=off;
          try{ if(onP) onP(off,total); }catch(e){}
          step();
        },function(){ rej(0); });
    })();
  });
}
var MEDIA_PULL={};   /* 路径 → 正在拉的 Promise：同一条消息里同一路径画了两个播放器，别下两遍 */
/* 加载进度：播放器下面一行小字（取完就撤），不是挡在播放器上的按钮 */
function mediaNote(el,off,total){
  try{
    let n=el.__mn;
    if(!n){
      n=el.__mn=document.createElement('div');
      n.className='mst';
      el.parentNode.insertBefore(n, el.nextSibling);
    }
    n.textContent=total>0?('加载中 '+Math.min(99,Math.floor(off*100/total))+'%'):'加载中…';
  }catch(e){}
}
function mediaNoteDone(el){
  try{ if(el.__mn){ el.__mn.remove(); el.__mn=null; } }catch(e){}
}
function mediaLoad(el){
  track('media_open');
  const p=el.getAttribute('data-p')||'';
  if(MEDIA_BLOB[p]){ el.src=MEDIA_BLOB[p]; mediaNoteDone(el); return; }
  const ep='/api/ext_file?p='+encodeURIComponent(p)+(SID?'&sid='+encodeURIComponent(SID):'');
  /* 局域网直连：给原生地址（浏览器自己 Range，能拖进度）。它是用 URL 里的 dt 票据
     鉴权的，票据过期/被拒时浏览器只会拿到一段 403 文本 → 播放器报错（2/4）。
     这种纠不回来（元素自己看不到状态码），mediaErr 里会改走中继分段取。 */
  try{ if(!el.__relay&&DIRECT.st==='on'&&DIRECT.mode==='lan'&&DIRECT.base){
    el.__lan=1;
    el.src=DIRECT.base+ep+'&dt='+encodeURIComponent(DIRECT.dt);
    mediaNoteDone(el);
    try{ if(el.__want) el.play(); }catch(e){}
    return; } }catch(e){}
  const ct=el.tagName==='VIDEO'?'video/mp4':(el.tagName==='AUDIO'?'audio/mpeg':'image/jpeg');
  var job=MEDIA_PULL[p];
  if(!job){
    job=MEDIA_PULL[p]=mediaPull(ep,function(off,total){ mediaNote(el,off,total); }).then(function(blob){
      const ks=Object.keys(MEDIA_BLOB);
      for(let i=0;i<ks.length-7;i++){          /* 最多留 8 段：手机上几百 MB 的 blob 顶不住 */
        const k=ks[i];
        let used=false;
        try{ used=!!document.querySelector('[src="'+MEDIA_BLOB[k]+'"]'); }catch(e){}
        if(!used){ try{ URL.revokeObjectURL(MEDIA_BLOB[k]); }catch(e){} delete MEDIA_BLOB[k]; }
      }
      delete MEDIA_PULL[p];
      return (MEDIA_BLOB[p]=URL.createObjectURL(new Blob([blob],{type:ct})));
    },function(st){ delete MEDIA_PULL[p]; throw st; });
  }
  job.then(function(u){
    el.src=u; mediaNoteDone(el);
    if(el.__want){ try{ el.play(); }catch(e){} }
  },function(st){ mediaErr(el, st); });
}
/* 媒体打不开（文件删了 / 不在允许目录 / 编码不支持 / 通道被拒）：不能静默消失——
   留一行可点的提示，并把错误码报回 host（[media-err]），手机上放不出来时有据可查。 */
function mediaErr(el, st){
  const c=st||(el.error&&el.error.code)||0;
  /* 局域网直连挂了（票据过期/换网/Wi-Fi 断了）：别弹提示，改走中继重取一遍。
     中继那条是页面自己的请求通道，鉴权/票据都在 xj 里，能拿到就是能拿到。 */
  if(el.__lan&&!el.__relay){
    el.__lan=0; el.__relay=1; el.__hy=1; el.__me=0; el.style.display='';
    mediaNoteDone(el);
    try{ URL.revokeObjectURL(el.src); }catch(e){}
    el.removeAttribute('src');
    mediaLoad(el);
    return;
  }
  el.style.display='none';
  mediaNoteDone(el);
  if(c===404) return;    /* 文件本来就不在（老消息里的路径多半早删了）：静默收起，别刷一排提示 */
  if(el.__me) return; el.__me=1;
  const n=document.createElement('div');
  n.className='mfail';
  n.textContent='打不开这个媒体（'+c+'）· 点我重试';
  n.onclick=function(){
    n.remove(); el.__me=0; el.__hy=0; el.__lan=0; el.__relay=0; el.__want=1; el.style.display='';
    const p=el.getAttribute('data-p')||'';
    try{ URL.revokeObjectURL(MEDIA_BLOB[p]); }catch(e){}
    delete MEDIA_BLOB[p];
    try{ URL.revokeObjectURL(el.src); }catch(e){}
    el.removeAttribute('src'); mediaLoad(el);
  };
  try{ el.parentNode.insertBefore(n, el.nextSibling); }catch(e){}
  try{ (new Image()).src='/api/ext_merr?b='+encodeURIComponent(PAGE_BUILD)+'&e='+c
      +'&m='+encodeURIComponent((el.__relay?'relay':'')+(el.__lan?'lan':''))
      +'&s='+encodeURIComponent((el.getAttribute('data-p')||'').slice(0,180))
      +'&u='+encodeURIComponent(location.href.replace(/t=[^&]+/,'t=***').slice(0,120)); }catch(e){}
}
/* 已经落在 DOM 里的媒体，切到/切回局域网时补一次 src（先到的那次可能是走中继取的） */
function mediaRefresh(){
  try{
    const els=document.querySelectorAll('#feed .media [data-p]');
    for(let i=0;i<els.length;i++){ const el=els[i]; if(el.getAttribute('src')) continue; el.__hy=0; }
    mediaHydrate();
  }catch(e){}
}
function bigImg(src){
  let d=document.getElementById('bigimg');
  if(!d){
    d=document.createElement('div'); d.id='bigimg';
    d.onclick=function(){ d.style.display='none'; };
    d.innerHTML='<img>'; document.body.appendChild(d);
  }
  d.querySelector('img').src=src;
  d.style.display='flex';
}
function renderFeed(){
  let h=feedMsgs.map(m=>{
    const body=(m.role==='assistant')?mdToHtml(m.text):esc(m.text);
    /* 只有工具调用、没有正文的回答不要画空气泡（用户看到的是「命令上面一条空白框」） */
    let x=`<div class="m ${m.role}">`+(body?`<div class="bub${m.role==='assistant'?' md':''}">${body}</div>`:'');
    if(m.role==='assistant') x+=mediaCards(m.text)+docCards(m.text);
    /* 命令默认收起（跟桌面版一样不铺满屏），点一下展开 */
    (m.tools||[]).forEach(t=>{ x+=`<div class="tool" onclick="this.classList.toggle('open')"><b>${esc(toolName(t.name))}</b><span class="tdet">${esc(t.detail)}</span></div>`; });
    /* 这一轮跑了多久：host 从 rollout 的 task_complete 拿的 duration_ms 挂在消息上，
       跟着消息走——页面重载、切走再回来都还在（跟桌面版「已处理 20秒」同源）。 */
    if(m.el&&m.role==='assistant') x+=`<div class="dur">用时 ${fmtDur(m.el)}</div>`;
    return x+'</div>';
  }).join('');
  /* 答完之后这一轮跑了多久：写在最后一条回答下面（桌面版「已处理 20秒」那个位置）。
     只挂在本轮刚答完时（DONE_EL 是 draft done 事件带下来的），重进会话没有。 */
  const _last=feedMsgs[feedMsgs.length-1];
  if(DONE_EL&&_last&&_last.role==='assistant'&&!_last.el) h+=`<div class="dur">用时 ${fmtDur(DONE_EL)}</div>`;
  /* 流式草稿：先按现在收到的部分画出来，整段消息到了就撤掉 */
  /* 只有状态、还没有正文（正在思考/正在运行命令）也要画：不然这一整段什么都不显示 */
  if(DRAFT||DRAFT_WAIT||DRAFT_ST){
    const _b=DRAFT?`<div class="bub md">${mdToHtml(DRAFT)}</div>`
                 :(DRAFT_WAIT?`<div class="bub md">${esc('（桌面版正在处理…）')}</div>`:'');
    h+=`<div class="m assistant">${_b}<div class="dstat" id="draftTool">${esc(draftToolText())}</div></div>`;
  }
  document.getElementById('feed').innerHTML=h;
  mediaHydrate();
  try{ docFloatRender(); }catch(e){}
}
/* 状态行：在干什么 + 这一轮跑了多久。host 只在状态变化时推，
   秒数由手机端自己走表（1 秒一刷，只改这一行文字，不重排整面墙）。 */
function fmtDur(s){
  s=Math.max(0,s|0);
  return s<60? s+'秒' : Math.floor(s/60)+'分'+(s%60)+'秒';
}
/* 工具调用改成人话标签（跟桌面版一样不铺命令）；点一下展开原文 */
function toolName(n){
  const s=String(n||'').toLowerCase();
  if(/exec|shell|command/.test(s)) return '运行命令';
  if(/patch|edit|write|apply/.test(s)) return '改文件';
  if(/search/.test(s)) return '搜索';
  if(/image/.test(s)) return '生成图片';
  if(/tool|mcp/.test(s)) return '工具';
  return n||'工具';
}
function draftToolText(){
  const st=DRAFT_ST||(DRAFT?'正在输出':'正在处理');
  let el='';
  if(DRAFT_EL_AT) el=' · '+fmtDur(DRAFT_EL+Math.round((Date.now()-DRAFT_EL_AT)/1000));
  return st+'…'+el;
}
let DRAFT_TICK=0;
function draftTickStart(){
  if(DRAFT_TICK) return;
  DRAFT_TICK=setInterval(function(){
    const e=document.getElementById('draftTool');
    if(!e){ if(!DRAFT&&!DRAFT_WAIT&&!DRAFT_ST){ clearInterval(DRAFT_TICK); DRAFT_TICK=0; } return; }
    const s=draftToolText();
    if(e.textContent!==s) e.textContent=s;
  },1000);
}
function setDraft(d){
  if(!d) return;
  const t=d.done?'':(d.text||'');
  const w=!d.done && !!d.waiting;
  const st=d.done?'':(d.st||'');
  if(!d.done && d.el!=null){ DRAFT_EL=d.el|0; DRAFT_EL_AT=Date.now(); }
  /* 答完那一帧带本轮用时；4 秒后草稿被撤还会再推一帧 done（el=0），别把用时清掉。
     新一轮开始（非 done）才清。 */
  const de=d.done?((d.el|0)||DONE_EL):0;
  if(t===DRAFT && w===DRAFT_WAIT && st===DRAFT_ST && de===DONE_EL) return;
  const stick=DRAFT_STICK||atBottom();   /* 用户往上翻在回看：别抢滚动 */
  DRAFT=t; DRAFT_WAIT=w; DRAFT_ST=st; DONE_EL=de;
  if(view==='chat'){ renderFeed(); DRAFT_STICK=stick; if(stick) stickBottom(); }
  if(DRAFT||DRAFT_WAIT||DRAFT_ST) draftTickStart();
}
function applyCaps(caps,why){
  /* 终端没有写入通道（Warp/VS Code 里手敲起的会话）：先把输入框关掉并说清楚下一步，
     别让用户打完字点发送才看到失败。caps 缺省（老 host）时不改动，保持原行为。
     why：host 给的具体原因（iTerm2 没开 / 窗口关了 / 这类终端没外部通道），有就显示。 */
  if(!caps) return;
  const ta=document.getElementById('txt'), btn=document.getElementById('send');
  const can=(Array.isArray(caps)&&caps.indexOf('input')>=0);
  if(can){ ta.disabled=false; btn.disabled=false;
    if(ta.dataset.ro){ delete ta.dataset.ro; ta.placeholder='说点什么…'; } }
  else{ ta.disabled=true; btn.disabled=true; ta.dataset.ro='1';
    ta.placeholder=why||'这个终端只读：点上方 🔄 接管后可输入'; }
}
function renderMsg(d){
  curState=d.state; curTotal=d.total;
  applyCaps(d.caps,d.why);
  setSt(d.state);
  try{ msgCacheDisarm(SID); }catch(e){}   // 真帧到达=网络通着，离线提示不用吱了
  /* 全量帧强制落缓存（增量不带全窗不落）：关掉再召回，墙先有上次的最后 300 条。
     会话已 gone 也照存——召回恰恰发生在关掉之后，这份就是召回时要画的历史。 */
  if(!d.inc&&d.messages&&d.messages.length){ try{ msgCacheSave(SID,d.messages,true); }catch(e){} }
  /* 文档浮条：实时/原样视图下面不会画对话墙，消息留一份在这里给 docFloatPick 判，
     浮条也要在「非聊天视图提前返回」之前重判（2026-11-20 用户：浮层还是没有出来） */
  try{
    const _ds=d.messages||[];
    if(_ds.length) DOC_MSGS=(d.inc?DOC_MSGS.concat(_ds):_ds.slice()).slice(-400);
    docFloatRender();
  }catch(e){}
  /* 转录最后一条是新来的用户消息 = 人已经回过了（手机上发、电脑上敲、语音，哪个入口
     都算）：这一轮「等你」的浮条当场收掉，不用等下一次提醒或重进会话（2026-11-18 用户）。
     只看增量帧：整页重载时的全量历史不算「刚回话」，否则会把还在等你的浮条误收。 */
  try{
    const _ms=d.messages||[], _lm=_ms[_ms.length-1];
    if(d.inc&&_lm&&_lm.role==='user'&&PEND[SID]){ delete PEND[SID]; pendRender(); }
  }catch(e){}
  /* Codex 桌面版没有窗口画面：点进去直接给转录（对话墙），别停在「加载中/无窗口」 */
  if(d.desktop&&view!=='chat'){ try{ setView('chat'); }catch(e){} }
  if(fullColor!==!!d.full_color){ fullColor=!!d.full_color;
    if(!wantView) viewChosen=false;   // 用户手选过视图：全彩检测结果变化也不改他的选择
    autoSwitched=false; renderDetailLite();
    /* 布局跟着全彩/纯文本走：本地缓存播种可能把 fullColor 猜成 true，
       真值回来是纯文本时必须切回 #live，否则两个全彩块占着屏、文字区被藏起来。 */
    if(view==='live') setView('live');
  }
  /* 接管按钮跟着 caps 走（claude 专属能力位），不再认 agent 名 */
  document.getElementById('migbtn').style.display=(!fullColor&&d.state!=='gone'&&curMeta&&!curMeta.host&&(curMeta.caps||d.caps||[]).indexOf('takeover')>=0)?'':'none';
  // 智能默认：全彩会话落到实时屏幕；手选过原样的就恢复原样（刷新后不跳回实时）
  if(!viewChosen&&!autoSwitched&&fullColor){
    autoSwitched=true;
    const want=wantView==='raw'?'raw':'live';
    /* 上面那行 setView('live') 刚切过就别再切一次：setView 会再拉一遍 179KB 的
       全彩镜像整块重画，滑一次会话白白多解析一遍（2026-11-13） */
    if(view!==want) setView(want);
    viewChosen=!!wantView;
  }
  if(d.state==='gone'){ toast('会话已结束',true); return; }
  if(view!=='chat'){ if(curTotal>0) renderDetailLite(); return; }  // 聊天页已隐藏：只负责状态行/全彩检测/转全彩按钮
  if(d.inc){
    const r=mergeInc(d.messages||[], d.inc_from);
    if(r==='noop') return;
    if(r==='gap'){ poll(true); return; }   // 对不上号：立刻拉一次全量，别让墙冻住
  }else{
    /* 全量：逐条比内容，不能只看 total，也不能只看首末两条的签名。
       转录超过 120 条后 host 永远给最后 120 条，total 卡在 120 不动、窗口却在往前滑；
       签名（条数+首条前 40 字+末条后 40 字）看不见中间的改动——一轮里全是工具调用
       时（正文空、工具行多）签名一直不变，这面墙从此不再更新
       （用户：桌面版里一大段，app 上后面的内容都没有）。 */
    const ms=d.messages||[];
    if(sameFeed(feedMsgs, ms)) return;
    feedMsgs=ms.slice();
  }
  const wasBottom=atBottom();
  lastTotal=d.total;
  renderFeed();
  try{ msgCacheSave(SID,feedMsgs); }catch(e){}   // 增量并进来也落缓存（15s 节流）
  if(wasBottom&&view==='chat') toBottom();
}
/* 把 host 的增量并进本地消息表。返回 noop=没变化、ok=并进去了、gap=对不上号。
   空尾巴必须原样忽略：自己发消息时本地先垫了一条（比 host 的计数多 1），
   而 host 的文件常常只长了工具输出/思考——解析出来的消息表没变，就发一条
   inc_from=host 计数的空尾巴。老代码拿 inc_from 去截断，正好把刚垫上去的用户
   消息截掉；之后 host 真带上这条消息的增量 inc_from 又比本地多 1，被当成
   「对不上」丢掉——手机端表现就是流式期间看不到自己发的那句话，整面墙卡死。 */
/* 两份消息表是不是同一份：逐条比角色/正文/工具行。
   比签名准：签名只看首末两条，一轮里中间的改动（工具行、正文追加）它看不见，
   墙会冻住不动——这正是「桌面版有好多内容、app 上后面的都没有」的原因。 */
function sameFeed(a,b){
  if(!a||!b||a.length!==b.length) return false;
  for(let i=0;i<a.length;i++){
    const x=a[i],y=b[i];
    if(x===y) continue;
    if(!x||!y) return false;
    if(x.role!==y.role||x.text!==y.text) return false;
    if((x.el||0)!==(y.el||0)) return false;
    const tx=x.tools||[],ty=y.tools||[];
    if(tx.length!==ty.length) return false;
    for(let k=0;k<tx.length;k++){
      if((tx[k].name||'')!==(ty[k].name||'')||(tx[k].detail||'')!==(ty[k].detail||'')) return false;
    }
  }
  return true;
}
function mergeInc(tail, from){
  if(!tail.length) return 'noop';
  if(from<=feedMsgs.length){
    feedMsgs.length=from;
    feedMsgs=feedMsgs.concat(tail);
    return 'ok';
  }
  return 'gap';
}
var MSG_INF=null;   /* 上一发没回来不再发（同一会话）；换会话不算占用——新会话得立刻拉 */
async function poll(force){
  if(!SID) return;   // 没选会话还轮询 = 每分钟一串 /api/ext_msg/ 404，日志里全是它
  if((PUSH.st==='on'&&!force)||hiddenNow()) return;
  if(MSG_INF&&MSG_INF===SID) return;
  const me=SID; MSG_INF=me;
  try{
    const r=await xj('/api/ext_msg/'+SID);
    if(r.ok&&r.json) renderMsg(r.json);
  }catch(e){ try{ msgCacheOfflineNote(me); }catch(_){}}   // 网断了但墙上有缓存：吱一声
  finally{ if(MSG_INF===me) MSG_INF=null; }
}
/* ── P2P 直连：中继只牵线，三级传输自动择优。
   ①局域网 HTTP 直达 host（最快最省）；②广域网 WebRTC DataChannel
   （DTLS 加密 + STUN 打洞，信令经中继；坚果 Chromium 62 也带 RTCPeerConnection）；
   ③都不行=中继隧道（现状兜底）。鉴权用中继签的短命票据 dt（query 传，免 CORS 预检），
   host token 永不落到页面。写操作（发消息/按键/语音上传）仍走中继——安全面最小。
   ⚙ 网络卡 cb_cfg.direct 总开关；任何一环失败自动回中继并冷却重试，零回归。 ── */
var DIRECT={st:'off',mode:'',base:'',dt:'',exp:0,lans:[],port:0,rtc:false,
  cool:0,dcid:0,pc:null,dc:null,trying:false,authT:null,fails:0,onAt:0,discT:null};
/* 打洞/直连反复横跳的退避：连着失败（或刚连上就断）就一轮比一轮等得久。
   蜂窝 CGNAT 下打洞成功率本来就低，每隔一两分钟硬试一次，用户看到的就是
   「一会儿中继转发、一会儿 P2P 直连」不停切，每次切都重建推流 → 页面发卡。 */
function directCoolMs(){
  var n=Math.max(0,(DIRECT.fails||0)-1);
  return Math.min(60000*Math.pow(2,n), 30*60000);
}
/* DC 票据续期：host 每帧校验 exp（中继断链后最长还能再用 ~10 分钟），页面每 4 分钟
   把中继刚签的 dt 重发一次。后台不发（后台本来就不该有请求），回前台立刻补一次。 */
function dcKeepAuth(){
  if(!DIRECT.dc||DIRECT.dc.readyState!=='open'||!DIRECT.dt) return;
  try{ DIRECT.dc.send(JSON.stringify({t:'auth',dt:DIRECT.dt})); }
  catch(e){ directDown('dc-auth'); }   /* 通道假在线：票据都发不出去，立刻回中继 */
}
var DCMAP={};   // DataChannel 请求 id → 处理器（dcReq/dcSSE 共用）
var DIRECT_GET=['/api/ext_push','/api/ext_msg/','/api/ext_live/','/api/ext_screen/',
  '/api/ext_snap/','/api/ext_sessions','/api/ext_stream/','/api/ext_scan/','/api/ext_cmdhist/','/api/ext_cmddel/','/api/ext_cmdrun/',
  '/api/ext_cmdruns','/api/ext_cmdout/','/api/ext_model','/api/ext_model_switch',
  '/api/ext_key','/api/ext_send','/api/plan','/api/notify_hist'];   /* 按键/发送走直连：外网绕中继一个来回太慢。
     语音（/api/ptt）故意不放进来：中继自己就有 ASR key，手机直传中继最短，
     而且上传体大，走 DataChannel 要整段 base64 过一遍，没有任何好处。 */
function directOn(){ return DIRECT.st==='on'; }
function directWant(ep){
  if(!directOn()) return false;
  for(var i=0;i<DIRECT_GET.length;i++) if(ep.indexOf(DIRECT_GET[i])===0) return true;
  return false;
}
function directCid(){
  try{ var m=document.cookie.match(/(?:^|;\s*)cb_cid=([^;]+)/);
       return m?decodeURIComponent(m[1]):''; }catch(e){ return ''; }
}
function xb(ep){
  if(!directWant(ep)) return ep;
  var cid=directCid();
  return DIRECT.base+ep+(ep.indexOf('?')>=0?'&':'?')+'dt='+encodeURIComponent(DIRECT.dt)+
    (cid?('&c='+encodeURIComponent(cid)):'');
}
function directDown(why,silent){
  if(DIRECT.st!=='on') return;
  /* 掉线原因必须留痕：以前这里静默，日志里只看到「又连上一次」，
     看不出中间为什么断了（dc-closed / ticket / quota 全是不同的病）。 */
  try{ if(window.CB&&CB.log) CB.log('direct[down] why='+(why||'-')+' silent='+!!silent
    +' mode='+DIRECT.mode+' chan='+(DIRECT.dc&&DIRECT.dc.readyState)+' pc='+(DIRECT.pc&&DIRECT.pc.connectionState)); }catch(e){}
  /* 活了多久：刚连上就断（<60s）= 这条网络就是打不通，记一次失败并翻倍退避；
     正常用了很久才断的（host 重启、换网）按第一次算，60s 后再探。 */
  var lived = DIRECT.onAt ? (Date.now()-DIRECT.onAt)/1000 : 9999;
  if(lived < 60) DIRECT.fails=(DIRECT.fails||0)+1;
  else DIRECT.fails=0;
  DIRECT.st='off'; DIRECT.mode=''; DIRECT.dt=''; DIRECT.onAt=0;
  DIRECT.cool=Date.now()+directCoolMs();   // 冷却：第一次 1 分钟，反复失败逐轮翻倍（最多 30 分钟）
  try{ if(window.CB&&CB.log) CB.log('direct[cool] fails='+DIRECT.fails+' lived='+Math.round(lived)+'s wait='+Math.round(directCoolMs()/1000)+'s'); }catch(e){}
  if(DIRECT.authT){ clearInterval(DIRECT.authT); DIRECT.authT=null; }
  if(DIRECT.discT){ clearTimeout(DIRECT.discT); DIRECT.discT=null; }
  try{ if(DIRECT.dc){DIRECT.dc.close();} }catch(e){}
  try{ if(DIRECT.pc){DIRECT.pc.close();} }catch(e){}
  DIRECT.pc=null; DIRECT.dc=null;
  vsDirectRefresh();
  if(!silent) toast('直连中断，已回中继');
  pushReconnect(0);   // SSE 立即切回中继通道（轮询闸自行恢复）
}
async function directTicket(){
  try{
    const r=await xj('/api/direct_ticket');
    const d=r.json;
    if(r.ok&&d&&d.ok){ DIRECT.dt=d.dt; DIRECT.exp=d.exp; return true; }
  }catch(e){}
  return false;
}
/* 探针结果用对象返回。
   ⚠️ 以前写的是 res(false,'原因') —— Promise 的 resolve 只认第一个参数！
   第二个参数被丢掉 → probeLans 只 resolve 出 null（或成功时只 resolve 出
   一个 IP 字符串），调用方再 pr[0] 就炸了（null[0] → TypeError）→
   整轮直连在「局域网探针失败」这一步就崩了，压根走不到打洞那一步 →
   表现就是「同一局域网也一直是中继转发」。教训：多值一律用对象。 */
function probePing(url,nonce){
  return new Promise(function(res){
    let done=false;
    const x=new XMLHttpRequest();
    const to=setTimeout(function(){ if(!done){done=true; try{x.abort();}catch(e){} res({ok:false,why:'超时'});} },1500);
    x.open('GET',url,true);
    x.onreadystatechange=function(){
      if(x.readyState!==4||done) return;
      done=true;clearTimeout(to);
      if(x.status!==200){ res({ok:false,why:'HTTP '+x.status}); return; }
      /* 200 ≠「是开发机」：代理/运营商劫持也答 200（2026-11-18 aliyun 事故根源，
         误建直连后整页半死）。必须验到身份：/api/ping 的 JSON（ok:1）+ 探针回声
         （pr==nonce，新 host 才回；老 host 缺 pr 字段时 ok:1 也认——随机门户页
         两样都给不出来）。 */
      let d=null; try{ d=JSON.parse(x.responseText); }catch(e){}
      if(d&&d.ok===1&&(d.pr===undefined||d.pr===nonce)) res({ok:true,why:''});
      else res({ok:false,why:'应答不是开发机（可能被代理劫持）'});
    };
    x.onerror=function(){ if(!done){done=true;clearTimeout(to);res({ok:false,why:'网络错误（多半是 https 页面发 http 被浏览器拦了）'});} };
    try{ x.send(null); }catch(e){ if(!done){done=true;clearTimeout(to);res({ok:false,why:String((e&&e.message)||e||'发送失败')});} }
  });
}
function probeLans(lans,port){   // 并行探针，第一个通的赢（串行最坏 6×1.5s 太拖）
  return new Promise(function(res){
    let pending=lans.length,done=false;
    const errs=[];
    if(!pending){ res({ip:null,err:''}); return; }
    const nonce=(Math.random().toString(16).slice(2)+Math.random().toString(16).slice(2)).slice(0,32);
    lans.forEach(function(ip){
      probePing('http://'+ip+':'+port+'/api/ping?pr='+nonce,nonce).then(function(r){
        var ok=r&&r.ok, why=(r&&r.why)||'';
        try{ if(window.CB&&CB.log) CB.log('probe '+ip+':'+port+' → '+(ok?'通':'不通')+' '+(why||'')); }catch(e){}
        if(done) return;
        if(ok){ done=true; res({ip:ip,err:''}); }
        else{
          errs.push(ip+':'+(why||'不通'));
          if(--pending===0&&!done){ done=true; res({ip:null,err:errs.join('；')}); }
        }
      });
    });
  });
}
function directRetry(){   // 设置卡「重测」：清冷却立刻再试一次
  if(DIRECT.trying) return;
  DIRECT.cool=0; DIRECT.diag=''; vsDirectRefresh(); directTry();
}
/* 直连状态快照：写进手机日志（CB.log → cbvol.log），排查「同一局域网还走中继」用。
   尤其看 trying 卡住（一次请求挂住 → 直连再也不会重试）和 mdns（浏览器把本机地址藏了）。
   step= 是本轮尝试走到哪一步（挂住时一眼看出卡在哪个请求上）。 */
function directDiag(tag){
  try{
    if (!(window.CB && CB.log)) return;
    CB.log('direct['+(tag||'')+'] st='+DIRECT.st+' mode='+DIRECT.mode+' trying='+DIRECT.trying
      +' cool='+Math.max(0,Math.round((DIRECT.cool-Date.now())/1000))
      +' cands='+(DIRECT.cands||0)+' mdns='+(DIRECT.mdns||0)
      +' step='+(DIRECT.step||'')
      +' diag='+(DIRECT.diag||'')+' lanErr='+(DIRECT.lanErr||''));
  }catch(e){}
}
async function directTry(){
  if(directOn()||DIRECT.trying||Date.now()<DIRECT.cool) return;
  DIRECT.trying=true; vsDirectRefresh();   // 打洞要试 ~30s，卡片先写「正在试直连…」
  try{
    /* 看门狗：整轮最多 90s。directTry0 里任何一个 promise 挂住（getUserMedia
       不回话、ajax 不回话），DIRECT.trying 就永远清不掉 → 之后再也不打洞，
       表现就是「一直在中继转发」。这里给它兜个底，超时就放行下一轮。 */
    await Promise.race([ directTry0(),
      new Promise(function(r){ setTimeout(r, 90000); }) ]);
  }catch(e){
    /* 别把异常吞成空气：directTry0 里任何一处抛错都会让整轮直连静默死掉
       （step 停在原地、lanErr 空、trying 提前清掉），看板上完全看不出出了啥。 */
    try{ if(window.CB&&CB.log) CB.log('direct[err] '+String((e&&e.stack)||e).slice(0,300)); }catch(x){}
  }
  finally{ DIRECT.trying=false; vsDirectRefresh(); }
}
async function directTry0(){
  let d=null;
  DIRECT.step='info';
  try{
    const r=await xj('/api/direct_info');
    d=r.ok&&r.json;
  }catch(e){ DIRECT.diag='直连信息请求失败：'+(e&&e.message||e); vsDirectRefresh(); }
  if(!d||!d.ok||!(d.lan||[]).length){
    DIRECT.diag='中继没给直连信息'+(d&&d.error?'：'+d.error:'（开发机未上报）'); vsDirectRefresh();
    DIRECT.step='';
    return;   // 老中继/未上报：永远走中继，无感
  }
  DIRECT.lans=d.lan; DIRECT.port=d.port; DIRECT.rtc=!!d.rtc;
  DIRECT.ice=d.ice||null;   // 中继下发的 STUN/TURN 列表（没配 TURN 时只有 STUN）
  try{ if(window.CB&&CB.log) CB.log('direct_info lan='+JSON.stringify(d.lan)+' port='+d.port
    +' rtc='+!!d.rtc+(d.rtc_why?' why='+d.rtc_why:'')+' ice='+((d.ice||[]).length)); }catch(e){}
  DIRECT.step='probe';
  const pr=await probeLans(DIRECT.lans,DIRECT.port);
  const ip=pr.ip, lanErr=pr.err||'';
  DIRECT.lanErr=lanErr;
  if(ip){   // ① 局域网直连
    DIRECT.step='lanticket';
    if(!await directTicket()){ DIRECT.diag='局域网通了但拿不到直连票据'; vsDirectRefresh(); DIRECT.step=''; return; }
    DIRECT.st='on'; DIRECT.mode='lan'; DIRECT.base='http://'+ip+':'+DIRECT.port;
    DIRECT.fails=0; DIRECT.onAt=Date.now();
    DIRECT.diag=''; DIRECT.step='lan-ok'; toast('已直连（局域网）'); vsDirectRefresh();
    pushReconnect(0);
    return;
  }
  if(!DIRECT.rtc){
    /* 把开发机给的失败原因带上：只写「aiortc 不可用」时，用户不知道该去那台
       机器上干什么（是没装、装坏了，还是 python 版本/架构不匹配）。 */
    DIRECT.diag='开发机没启用 P2P（aiortc 不可用'+(d.rtc_why?'：'+d.rtc_why:'')+'）'
      +(lanErr?'；局域网也没通：'+lanErr:'');
    vsDirectRefresh(); DIRECT.step=''; return;
  }
  if(!window.RTCPeerConnection){ DIRECT.diag='这个浏览器内核不支持 WebRTC'+(lanErr?'；局域网也没通：'+lanErr:''); vsDirectRefresh(); DIRECT.step=''; return; }
  await dcTry();   // ② 广域网打洞
}
function bytesOf(bin){
  const u=new Uint8Array(bin.length);
  for(let i=0;i<bin.length;i++) u[i]=bin.charCodeAt(i)&0xff;
  return u;
}
function b64of(buf){   // ArrayBuffer/Blob → base64（DC 通道只能带字符串）
  const u=new Uint8Array(buf); let s='';
  for(let i=0;i<u.length;i+=4096) s+=String.fromCharCode.apply(null,u.subarray(i,i+4096));
  return btoa(s);
}
function dcSend(o){   // 返回是否真的发出去了：通道僵死（host 重启/断网）必须立刻暴露，别让请求悬 150s
  if(!DIRECT.dc||DIRECT.dc.readyState!=='open') return false;
  try{ DIRECT.dc.send(JSON.stringify(o)); return true; }catch(e){ return false; }
}
function dcDispatch(e){
  let d=null; try{ d=JSON.parse(e.data); }catch(err){ return; }
  /* 这些帧没有 id，是通道级的控制帧 */
  if(d.t==='quota'){ quotaSheet(d); return; }             // host 侧额度墙
  if(d.t==='auth_expired'){ directDown('ticket'); return; }   // 票据到期没续上（中继断）
  if(d.t==='auth_bad'){ directDown('ticket'); return; }       // 续期被拒
  const h=DCMAP[d.id]; if(!h) return;
  if(d.t==='res') h.onRes(d);
  else if(d.t==='chunk') h.onChunk(d);
  else if(d.t==='res_end'){ delete DCMAP[d.id]; if(h.onEnd) h.onEnd(d); }
}
function dcReq(ep,opt){opt=opt||{};   // 语义对齐 xj：同款返回形状（ok/status/json/text/buf/hdr）
  return new Promise(function(res){
    const id='q'+(++DIRECT.dcid);
    const t0=Date.now();              // 发出时刻（直连/补报的分钟同样按整分归账）
    let parts=[],rhdr=null,done=false;
    const bad=function(){ if(done)return; done=true; delete DCMAP[id];
      res({ok:false,status:0,json:null,text:null,buf:null,hdr:function(){return null;}}); };
    /* 150s 超时 = P2P 通道僵尸时（host 重启后 dc 假在线，发送成功但对端收不到）
       请求悬 2.5 分钟毫无动静 →「点了没反应」。快接口 8s 就该回中继；
       慢接口（模型切换要重启会话 / 扫描读 LLM / 语音）放宽到 60s。 */
    const slow=ep.indexOf('ext_model_switch')>=0||ep.indexOf('ext_scan')>=0||ep.indexOf('/api/ptt')>=0;
    /* opt.dcto：调用方自己指定直连超时（只读的弹层快照 4s、按键 5s——通道僵死时
       早点回落中继，别让人干等 8 秒才看到结果）。重复送达由 host 的 cid 去重。 */
    const to=setTimeout(bad,opt.dcto||(slow?60000:8000));
    DCMAP[id]={
      /* ⚠️ host 侧非流式响应是把 body 直接放在 res 帧里的（base64），
         以前 onRes 只留了头，body 被整段丢掉 → 走直连时所有响应都是空体
         （会话列表空、快照空白）。流式（st=1）的 body 是空串，不受影响。 */
      onRes:function(d){ rhdr=d; if(d&&d.body){ try{ parts.push(atob(d.body)); }catch(e){} } },
      onChunk:function(d){ try{ parts.push(atob(d.b64)); }catch(e){} },
      onEnd:function(){ if(done)return; done=true; clearTimeout(to); delete DCMAP[id];
        const bin=parts.join(''); parts=null;
        let txt=null,js=null,buf=null;
        if(opt.bin){ buf=bytesOf(bin).buffer; }
        else{ try{ txt=new TextDecoder('utf-8').decode(bytesOf(bin)); js=JSON.parse(txt); }catch(e){} }
        const good=!!(rhdr&&((rhdr.code>=200&&rhdr.code<400)||rhdr.code===403));
        if(good){
          if(ep.indexOf('/api/plan')<0) LASTOK=Date.now();
          if(ep.indexOf('a=0')<0&&billWant(ep,opt)){
            LASTBILL=Math.max(LASTBILL,t0);
            useTickStart();          /* 记了账就读一格，读完整好落在服务器的数上 */
            if(!USEST.tk) useAnchorSoon();
          }
        }
        /* 直连的时长额度墙：以前 host 单发一帧 t:'quota'，现在改成普通的 403 res
           （读请求不再被拦、也不整条断通道）。这里把升级浮层补上，口径和 xj 一致。 */
        if(rhdr&&rhdr.code===403&&js&&js.quota){ quotaSheet(js); }
        res({ok:(rhdr&&rhdr.code>=200&&rhdr.code<300),status:(rhdr&&rhdr.code)||0,
          json:js,text:txt,buf:buf,
          hdr:function(n){ return (rhdr&&rhdr.headers&&rhdr.headers[n])||null; }});      }};
    var hdr={}; if(opt.ctype) hdr['Content-Type']=opt.ctype;
    var _cid=directCid(); if(_cid) hdr['X-CB-Cid']=_cid;   // 直连流量按客户端归类（中继统计用）
    var b=opt.body;
    /* ⚠️ 头字段的键名必须跟 host 侧一致：host 的 _dc_serve 读的是 d["headers"]
       （中继隧道帧也是这个键）。以前这里写的是 h: → host 收不到 Content-Type，
       POST 的 JSON 体（发送/语音）在直连上解析不出来。两个键都带上，兼容。 */
    var go=function(b64){ return dcSend({t:'req',id:id,m:opt.method||'GET',p:ep,h:hdr,headers:hdr,body:b64||''}); };
    if(b&&typeof Blob!=='undefined'&&b instanceof Blob){
      /* 语音 Blob：DC 通道只能带字符串 → FileReader 读成 base64（host 侧 b64decode 还原） */
      var fr=new FileReader();
      fr.onload=function(){ if(!go(String(fr.result||'').split(',')[1]||'')) bad(); };
      fr.onerror=function(){ bad(); };
      fr.readAsDataURL(b);
      return;
    }
    let payload='';
    if(!b){ payload=''; }
    else if(typeof b==='string'){ payload=btoa(unescape(encodeURIComponent(b))); }
    else if(typeof ArrayBuffer!=='undefined'&&b instanceof ArrayBuffer){ payload=b64of(b); }
    else{ payload=btoa(unescape(encodeURIComponent(JSON.stringify(b)))); }
    if(!go(payload)) bad();   // 通道没发出去：立即失败，xj 层会回退中继
  });
}
function dcSSE(path,h){   // 语义对齐 sseOpen：onEvent/onDead/lastByte/abort
  const id='s'+(++DIRECT.dcid);
  let dec=null,buf='',lastByte=Date.now(),dead=false,closing=false;
  try{ dec=new TextDecoder('utf-8'); }catch(e){}
  /* closing：我们自己关的（50 秒自续/切后台）：不能回调 onDead，否则每次自续都
     被记成一次掉线（见 sseOpen 里的同一处注释）。 */
  function die(why){ if(dead)return; dead=true; delete DCMAP[id];
    dcSend({t:'cancel',id:id}); if(!closing&&h.onDead)h.onDead(why); }
  DCMAP[id]={
    onRes:function(d){ if((d.code||0)!==200) die('http '+d.code); },
    onChunk:function(d){
      lastByte=Date.now();
      let t;
      try{
        const b=atob(d.b64);
        t=dec?dec.decode(bytesOf(b),{stream:true}):decodeURIComponent(escape(b));
      }catch(e){ die('decode'); return; }
      buf+=t;
      let i;
      while((i=buf.indexOf('\n\n'))>=0){
        const blk=buf.slice(0,i); buf=buf.slice(i+2);
        let ev='message',data='';
        const ls=blk.split('\n');
        for(let j=0;j<ls.length;j++){
          const ln=ls[j];
          if(ln.charAt(0)===':') continue;
          if(ln.indexOf('event:')===0) ev=ln.slice(6).replace(/^\s+|\s+$/g,'');
          else if(ln.indexOf('data:')===0){ const dd=ln.slice(5); data+=(data?'\n':'')+(dd.charAt(0)===' '?dd.slice(1):dd); }
        }
        if(h.onEvent) h.onEvent(ev,data);
      }
    },
    onEnd:function(){ die('eof'); }
  };
  if(!dcSend({t:'req',id:id,m:'GET',p:path,st:1})){
    /* 通道已经不在 open（host 重启/断网）：推送不能在这条死路上干等
       22 秒 bootstrap 超时，立刻判掉并让直连退回中继，pushStart 马上改走中继。 */
    die('dc-send'); try{ directDown('dc-send'); }catch(e){}
  }
  return { abort:function(){ closing=true; die('abort'); }, lastByte:function(){ return lastByte; } };
}
function waitIce(pc,ms){
  return new Promise(function(res){
    let done=false;
    const fin=function(){ if(!done){done=true;res();} };
    const to=setTimeout(fin,ms||3000);
    try{
      pc.onicegatheringstatechange=function(){
        if(pc.iceGatheringState==='complete'&&!done){ clearTimeout(to); fin(); }
      };
    }catch(e){ setTimeout(fin,0); }
  });
}
/* Safari/Chrome 在页面没有麦克风权限时，会把 WebRTC 的本机地址藏成
   mDNS（<uuid>.local）——开发机（aiortc）解析不了 .local，ICE 一个 pair
   都配不出来，打洞必挂。先拿一次媒体权限（拿到就立刻停掉轨道，不占麦），
   浏览器才会给真地址。壳那边的系统麦克风权限没要过的话，让壳去要。 */
let mediaTried = false, mediaOk = false, mediaT = 0;
function ensureMediaPerm(){
  return new Promise(function(res){
    if (mediaOk) return res(true);
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) return res(false);
    if (mediaTried) return res(false);          /* 只试一次，别反复弹窗 */
    mediaTried = true;
    /* ⚠️ 必须带超时：系统麦克风权限还没给时，iOS 的 getUserMedia 可能永远不
       settle（系统弹窗被挂起）。一挂，整条直连链路就卡死 —— dcTry 不返回 →
       directTry 的 finally 永远不跑 → DIRECT.trying 一直是 true → 之后
       再也不打洞，全走中继（这正是 22:31 之后不再有 rtc_offer 的原因）。 */
    var done=false;
    var fin=function(v){ if(!done){done=true; try{clearTimeout(mediaT);}catch(e){} res(v);} };
    mediaT=setTimeout(function(){
      if(done) return;
      try{ if (window.CB && CB.log) CB.log('media perm 超时（getUserMedia 没回话）'); }catch(e){}
      fin(false);
    },4000);
    navigator.mediaDevices.getUserMedia({audio:true}).then(function(s){
      try{ s.getTracks().forEach(function(t){ t.stop(); }); }catch(e){}
      mediaOk = true;
      try{ if (window.CB && CB.log) CB.log('media perm ok'); }catch(e){}
      fin(true);
    }).catch(function(e){
      try{ if (window.CB && CB.needMedia) CB.needMedia(); }catch(e2){}
      try{ if (window.CB && CB.log) CB.log('media perm 被拒：'+String(e&&e.name||e).slice(0,60)); }catch(e3){}
      /* 失败的，过一分钟允许再试一次（安卓壳可能一开始没给 RECORD_AUDIO） */
      setTimeout(function(){ mediaTried=false; }, 60000);
      fin(false);
    });
  });
}
/* 壳那边系统麦克风权限到手后，过几拍再重试一轮直连：
   刚才那轮 PC 建的时候还没权限，候选是 .local。 */
let mediaRetryT = null;
function scheduleDirectRetry(){
  if (mediaRetryT) return;
  var tries = 0;
  mediaRetryT = setInterval(function(){
    tries++;
    if (tries > 15){ clearInterval(mediaRetryT); mediaRetryT = null; return; }
    try{
      if (DIRECT.trying) return;          /* 上一轮还没结束，下一拍再试 */
      DIRECT.cool = 0;
      clearInterval(mediaRetryT); mediaRetryT = null;
      directTry();
    }catch(e){}
  }, 2000);
}
window.__cbMedia = function(ok){    /* 壳拿到系统麦克风权限后的回话 */
  if (ok){
    /* 壳给的是「系统」麦克风权限，不是「网页级」媒体授权。网页没拿到授权的话，
       浏览器照样把本机地址藏成 .local（mDNS）→ aiortc 配不上对 → 打洞必挂。
       所以这里不能当 mediaOk（那会让后面直接跳过 getUserMedia），
       只能把「试过了」的标记清掉，让下一轮真的再 getUserMedia 一次。 */
    mediaTried = false; mediaOk = false;
    try{ if (window.CB && CB.log) CB.log('media perm ok (shell) → 重试 getUserMedia'); }catch(e){}
    scheduleDirectRetry();
  }
};

async function dcTry(){
  DIRECT.step='dcticket';
  if(!await directTicket()){ DIRECT.step=''; return; }
  try{
    /* 网页级媒体授权必须在建 PC 之前拿到，否则浏览器把本机地址藏成 .local，
       开发机（aiortc）解析不了 → 一个 pair 都配不出来 → 打洞必挂。
       两个壳都要做（安卓壳的 onPermissionRequest 直接授音频采集）。
       正在录音就跳过：原生录音和网页采集抢麦克风会互相打架。 */
    var rec = !!(window.CB && CB.pttState && CB.pttState()==='rec');
    if (!rec){ DIRECT.step='media'; await ensureMediaPerm(); }
  }catch(e){}
  DIRECT.step='pc';
  /* ICE 由中继下发（配了 TURN 就有中继兜底）；拿不到就用内置 STUN（老中继/离线中继） */
  const ice=(DIRECT.ice&&DIRECT.ice.length)?DIRECT.ice:[
    {urls:'stun:stun.miwifi.com:3478'},
    {urls:'stun:stun.chat.bilibili.com:3478'},
    {urls:'stun:stun.l.google.com:19302'}];
  const pc=new RTCPeerConnection({iceServers:ice});
  const dc=pc.createDataChannel('cb',{});   // ordered+reliable 默认
  const fail=function(why){ try{dc.close();}catch(e){} try{pc.close();}catch(e){}
    DIRECT.fails=(DIRECT.fails||0)+1;      // 连着打不通就逐轮翻倍，别每两分钟硬顶一次
    DIRECT.cool=Date.now()+Math.max(120000,directCoolMs());
    if(why) DIRECT.diag=why+(DIRECT.lanErr?('；局域网也没通（'+DIRECT.lanErr+'）'):'');
    DIRECT.step='';
    vsDirectRefresh(); directDiag('fail'); };
  try{
    const linked=await new Promise(async function(res){
      let settled=false;
      const fin=function(v){ if(!settled){settled=true;res(v);} };
      const to=setTimeout(function(){ fin(false); },20000);
      try{
      pc.onconnectionstatechange=function(){
        /* Safari 早期没有 pc.connectionState，退回 iceConnectionState */
        const st=pc.connectionState||pc.iceConnectionState||'';
        if(!settled){
          if(st==='connected'){ clearTimeout(to); fin(true); }
          if(st==='failed'||st==='closed'){ clearTimeout(to); fin(false); }
          return;
        }
        /* 连上之后的掉线也必须处理：host 重启/换网时对端是直接消失的，DTLS 不会发
           close，dc.onclose 有时候一直不响——页面就一直显示「P2P 直连」，推送还在
           这条死通道上反复开流、每次等 22 秒 bootstrap 超时，表现就是设置页
           「连接中 ↔ 轮询中」来回跳（2026-11-09 实测）。ICE/PC 状态是唯一可靠的信号。 */
        if(DIRECT.mode!=='dc') return;
        try{ if(window.CB&&CB.log) CB.log('pc 状态 '+st+' chan='+(dc.readyState)); }catch(e){}
        if(st==='failed'||st==='closed'){ directDown('pc-'+st); return; }
        if(st==='connected'){
          if(DIRECT.discT){ clearTimeout(DIRECT.discT); DIRECT.discT=null; }
          return;
        }
        if(st==='disconnected'){
          /* 换网/信号抖动几秒常常自己回来：给 8 秒宽限，回来就取消 */
          if(DIRECT.discT) return;
          DIRECT.discT=setTimeout(function(){
            DIRECT.discT=null;
            const now=pc.connectionState||pc.iceConnectionState||'';
            if(DIRECT.mode==='dc'&&now!=='connected') directDown('pc-disconnected');
          },8000);
        }
      };
      /* 状态轨迹：ice / conn / 通道 readyState 三者一起打。
         典型故障：conn 已 connected（DTLS 通了）但通道还没 open（SCTP 还在握手），
         这时候 send() 在 Safari 上会抛 InvalidStateError。 */
      pc.oniceconnectionstatechange=function(){
        try{ if(window.CB&&CB.log) CB.log('dc 状态 ice='+pc.iceConnectionState+' conn='+pc.connectionState+' chan='+dc.readyState); }catch(e){}
      };
      const offer=await pc.createOffer();
      await pc.setLocalDescription(offer);
      /* 蜂窝网收 srflx 候选慢：2.5s 往往还没凑齐就把 offer 发出去了，对端只看到内网地址
         → 打洞必失败。给到 6s（offer 里候选不全，后面没有 trickle 通道）。 */
      DIRECT.step='ice';
      await waitIce(pc,6000);
      /* 诊断：本机候选长什么样。Safari/Chrome 在页面没有麦克风权限时会把
         主机地址藏成 <uuid>.local（mDNS）——开发机（aiortc）解析不了 .local，
         ICE 一个 pair 都配不出来，打洞必挂。 */
      try{
        const cl=(pc.localDescription&&pc.localDescription.sdp||'').match(/a=candidate:[^\r\n]*/g)||[];
        const md=cl.filter(x=>/\.local(\s|$)/i.test(x));
        DIRECT.cands=cl.length; DIRECT.mdns=md.length;
        try{ if(window.CB&&CB.log) CB.log('dc cands='+cl.length+' mdns='+md.length+' :: '+cl.slice(0,4).join(' | ').slice(0,600)); }catch(e){}
        if(md.length) DIRECT.diag='浏览器把本机地址藏成了 .local（mDNS）';
      }catch(e){}
      let r=null;
      DIRECT.step='offer';
      try{ r=await xj('/api/rtc_offer',{method:'POST',body:{sdp:pc.localDescription.sdp,type:'offer'}}); }catch(e){}
      const d=r&&r.json;
      if(!r||!r.ok||!d||!d.ok){ clearTimeout(to); DIRECT.diag='中继拒绝打洞：'+((d&&d.error)||('HTTP '+((r&&r.status)||0))); fin(false); return; }
      try{ if(window.CB&&CB.log) CB.log('dc offer ok cid='+d.cid+' st='+(pc.connectionState||pc.iceConnectionState||'?')); }catch(e){}
      DIRECT.step='answer';
      const a=await xj('/api/rtc_ans?cid='+encodeURIComponent(d.cid));   // 中继长轮询等 answer
      const ad=a&&a.json;
      if(a&&a.ok&&ad&&ad.ok&&pc.connectionState!=='closed'){
        await pc.setRemoteDescription({sdp:ad.sdp,type:ad.type||'answer'});
        try{ if(window.CB&&CB.log) CB.log('dc answer ok，握手中…'); }catch(e){}
      }else{ clearTimeout(to); DIRECT.diag='没收到开发机的应答'; fin(false); }
      }catch(e){
        /* ⚠️ 这里必须自己兜住：executor 是 async function，Promise 构造器不接管
           async executor 的 rejection —— 里面一抛错，外层 promise 永远不 settle，
           这一轮直连就挂到 90 秒看门狗才结束（表现为「一直在中继转发」）。 */
        try{ if(window.CB&&CB.log) CB.log('dc[exec] '+String((e&&e.message)||e)); }catch(x){}
        DIRECT.diag='打洞过程异常：'+String((e&&e.message)||e);
        clearTimeout(to); fin(false);
      }
    });
    try{ if(window.CB&&CB.log) CB.log('dc 握手结果 linked='+linked+' ice='+pc.iceConnectionState+' conn='+pc.connectionState+' chan='+dc.readyState); }catch(e){}
    if(!linked){ fail(DIRECT.diag||'打洞被运营商 NAT 挡住'); return; }
    /* ── 票据栅栏 ──
       ⚠️ Safari/WebKit：DataChannel 的 readyState 还不是 'open' 时 send() 直接抛
       InvalidStateError（Chrome 会把消息缓存住，Safari 不缓存）。而
       pc.connectionState 变成 'connected' 只说明 DTLS 通了，SCTP 关联
       （通道 readyState='open'）还要再等几十毫秒。直接在 connected 的瞬间 send
       → Safari 抛异常 → 整轮打洞炸掉（以前异常被外层 catch 吞成空原因，
       看板上看不出任何线索）。所以：等通道 open 再发。 */
    const auth=await new Promise(function(res){
      const to=setTimeout(function(){ res(false); },8000);
      let sent=false, iv=null;
      dc.onmessage=function(e){
        let m=null; try{ m=JSON.parse(e.data); }catch(err){ return; }
        if(m.t==='auth_ok'){ clearTimeout(to); res(true); }
        if(m.t==='auth_bad'){ clearTimeout(to); res(false); }
      };
      const sendAuth=function(){
        if(sent) return true;          /* 只发一次：onopen 和轮询可能都触发 */
        try{
          dc.send(JSON.stringify({t:'auth',dt:DIRECT.dt}));
          sent=true;
          if(iv){ clearInterval(iv); iv=null; }
          return true;
        }catch(e){
          try{ if(window.CB&&CB.log) CB.log('dc 发票据失败：'+String((e&&e.message)||e)); }catch(x){}
          DIRECT.diag='发票据时通道不可用：'+String((e&&e.message)||e);
          clearTimeout(to); res(false);
          return false;
        }
      };
      if(dc.readyState==='open'){ sendAuth(); return; }
      dc.onopen=function(){
        try{ if(window.CB&&CB.log) CB.log('dc 通道 open，发 auth'); }catch(e){}
        sendAuth();
      };
      iv=setInterval(function(){
        if(dc.readyState==='open') sendAuth();
      },100);
      setTimeout(function(){ if(iv){ clearInterval(iv); iv=null; } },7000);
    });
    if(!auth){ fail(DIRECT.diag||'连上了但票据校验失败'); return; }
    try{ if(window.CB&&CB.log) CB.log('dc 票据通过 chan='+dc.readyState); }catch(e){}
    DIRECT.fails=0; DIRECT.onAt=Date.now();   // 连上了：退避归零（下次断了从 1 分钟重新算）
    DIRECT.st='on'; DIRECT.mode='dc'; DIRECT.pc=pc; DIRECT.dc=dc;
    dc.onmessage=dcDispatch;
    dc.onclose=function(){ directDown('dc-closed'); };
    if(DIRECT.authT) clearInterval(DIRECT.authT);
    DIRECT.authT=setInterval(function(){
      if(DIRECT.mode!=='dc'||hiddenNow()) return;
      dcKeepAuth();
    },240000);
    toast('已直连（P2P 打洞）'); vsDirectRefresh();
    DIRECT.step='dc-ok';
    directDiag('ok');
    pushReconnect(0);
  }catch(e){
    /* 别把异常吞成空气：以前这里 fail() 不带原因 → 看板上的诊断永远是空的，
       根本看不出是哪一步炸的。 */
    const msg=String((e&&e.message)||e);
    try{ if(window.CB&&CB.log) CB.log('dc[err] '+msg+' :: '+String((e&&e.stack)||'').slice(0,260)); }catch(x){}
    fail('打洞异常：'+msg);
  }
}
/* 打开系统的 WiFi / 移动数据面板（浮层优先，没有就落系统设置页）；kiosk 下由壳让路 */
function netOpen(kind){
  try{
    if(!window.CB){ show('需要 CodeBoss App（安卓 / iOS）'); return; }
    if(kind==='wifi'){
      if(CB.openWifiSettings){ CB.openWifiSettings(); return; }
      if(CB.openNetPanel){ CB.openNetPanel(); return; }
    }else{
      if(CB.openMobileSettings){ CB.openMobileSettings(); return; }
      if(CB.toggleMobile){ CB.toggleMobile(); return; }
    }
    show('本机不支持');
  }catch(e){ show('打不开系统设置'); }
}
function vsDirectRefresh(){
  const el=document.getElementById('vsDirect');
  if(!el) return;
  if(DIRECT.st==='on'){
    /* 走到 P2P 时把「局域网为什么没连上」一并写出来（截短）：iOS WKWebView 里
       https 页面发 http 探针被内核按混合内容拦是**常态**（安卓壳放行混合内容，
       所以安卓真见过局域网通），对 iPhone 用户显示「局域网不通」是吓人——
       实际 P2P 一直好好的。改成中性说明；错误细节留在 title/日志里。 */
    const ios=/iPhone|iPad|iPod/.test(navigator.userAgent);
    let why='';
    if(DIRECT.mode!=='lan'&&DIRECT.lanErr){
      why=ios?'（iOS 内核拦局域网探测，已走 P2P）'
             :('（局域网不通：'+DIRECT.lanErr.slice(0,40)+'）');
    }
    el.textContent=(DIRECT.mode==='lan'?'局域网直连':'P2P 直连')+why;
    el.title=DIRECT.lanErr||'';
    return;
  }
  var cd=DIRECT.cool>Date.now()?Math.ceil((DIRECT.cool-Date.now())/1000)+'s 后重试':'';
  el.textContent='中继转发（'+(DIRECT.trying?'正在试直连…':DIRECT.diag||cd||'待探测')+'）';
}
/* 推送状态也放到设置页：一直「在轮询」时，用户能看到是哪一步掉的、
   以及点一下就能重连，不用等 30 分钟退避。 */
function pushWhyText(why){
  const m={'stream-dead':'流被对端关掉','es-error':'长连接出错','bootstrap-timeout':'22 秒没收到首帧',
    'stall':'30 秒没收到数据','err':'服务端报错','wake-stall':'回前台时流已陈旧'};
  return m[why]||why||'';
}
function vsPushRefresh(){
  const el=document.getElementById('vsPush'); if(!el) return;
  if(PUSH.st==='on'){
    const via=(PUSH.tr==='dc'?'P2P 通道':(PUSH.tr==='sse'?'推送流':'兼容流'));
    /* st=on 只是「已经发起」，要等到 hello 才算真连上：这会儿说「已连接」会让人以为
       流是好的，其实还在自举（冷启动 discover 最坏 15 秒）。 */
    el.textContent=(PUSH.gotHello?'已连接（'+via+'）':'连接中（'+via+'）');
    el.title=''; return;
  }
  const w=pushWhyText(PUSH.why);
  /* 排着重连定时器（PUSH.timer 未清）时别说「轮询中」：那 0.5～1.3 秒本来就要重开，
     说成轮询会让用户以为推流死了。真进入长退避（定时器也要等 15 秒以上）才这么写。 */
  const retry=!!PUSH.timer&&PUSH.fail<=2;
  el.textContent=(retry?'重连中':'轮询中')+(w?('（'+w+'）'):'')+(PUSH.fail>1?(' ×'+PUSH.fail):'');
  el.title=PUSH.why||'';
}
function pushRetry(){ PUSH.fail=0; PUSH.why=''; pushReconnect(0); toast('正在重连推送…'); }
/* ── 实时推送（SSE over XHR）：轮询的增量替代。
   老 WebView 上 EventSource 未验证、fetch 双向坏——XHR 流式读是唯一实证通道：
   readyState≥3 起 responseText 单调增长，按 \n\n 自分帧。断流/陈旧自动回轮询。 ── */
var PUSH={st:'off',x:null,timer:null,recycle:null,helloAt:0,gotHello:false,fail:0,sid:null,seq:-1,
  tr:'',why:'',at:0};   /* tr=走的哪条路 dc/sse/xhr；why/at=上次掉线原因和时刻（设置页显示） */
function sseOpen(url,h){
  const x=new XMLHttpRequest();
  let seen=0,buf='',lastByte=Date.now(),dead=false,opened=false,closing=false;
  /* closing=我们自己关的（50 秒自续/切后台/换流）：绝不能回调 onDead。
     以前 abort 也走 onDead → 每次自续都算一次「掉线」，fail 每 50 秒 +1，
     而 pushDown 里排的重连定时器又被外层覆盖成孤儿定时器，两个 pushStart 互相关掉
     对方的流 → 连锁到 3 次失败 → 弹「推送中断，已回轮询」。iOS 上 P2P 走 dcSSE，
     同一个坑，表现就是「明明连着 P2P，推送一会儿就退回轮询」。 */
  function die(why){ if(dead)return; dead=true; try{x.abort();}catch(e){}
    if(!closing&&h.onDead)h.onDead(why); }
  x.open('GET',url,true);
  try{ x.setRequestHeader('Accept','text/event-stream'); }catch(e){}
  x.onreadystatechange=function(){
    if(x.readyState===2){
      if(opened) return; opened=true;
      if(x.status!==200){ die('http '+x.status); return; }
    }
    if(x.readyState!==3&&x.readyState!==4) return;
    let t='';
    try{ t=x.responseText; }catch(e){ die('read'); return; }
    if(t.length>seen){
      lastByte=Date.now();
      buf+=t.slice(seen); seen=t.length;
      let i;
      while((i=buf.indexOf('\n\n'))>=0){
        const blk=buf.slice(0,i); buf=buf.slice(i+2);
        let ev='message',data='';
        const ls=blk.split('\n');
        for(let j=0;j<ls.length;j++){
          const ln=ls[j];
          if(ln.charAt(0)===':') continue;   // : hb 心跳/注释
          if(ln.indexOf('event:')===0) ev=ln.slice(6).replace(/^\s+|\s+$/g,'');
          else if(ln.indexOf('data:')===0){ const dd=ln.slice(5); data+=(data?'\n':'')+(dd.charAt(0)===' '?dd.slice(1):dd); }
        }
        if(h.onEvent) h.onEvent(ev,data);
      }
    }
    if(x.readyState===4) die('eof');
  };
  x.onerror=function(){ die('net'); };
  /* send 同步抛（坏 URL / 内核拒绝）以前会直接冒到 pushStart：PUSH.st 卡在 'on'、
     PUSH.x 没赋值，看门狗以为流还活着。改成按真掉线处理。 */
  try{ x.send(null); }catch(e){ die('send'); }
  return { abort:function(){ closing=true; die('abort'); }, lastByte:function(){ return lastByte; } };
}
/* 提醒角标：sid → {kind,ts,text}。来自 host 事件层（Claude Code hooks）。
   只为「该看手机了」：完成 / 待确认 / 等你回复。进会话即清。 */
const NOTIFY={};
/* 当前会话还欠着一次确认/回复：会话页底部浮条用（和提醒历史的「确认」同一个动作）。
   sid -> {n(历史行号),kind,text,cause,about,summary,ts}；本条会话报 done 或用户回话后清掉。 */
const PEND={};
/* sid -> 用户「自己在这个会话里发消息成功」的时刻（毫秒）。发言之后紧跟着产生的
   问句就压在消息流里，人正对着它回话，不需要浮条再挡一层（用户 2026-11-09：
   输入并发送完就让它消失，别一直显示）。 */
const SEND_AT={};
/* 发言时刻要跨刷新記住：事件流重连/重开页面时，旧的那条「等你」会被重放，
   只按「事件时间早于上次发言」判断才认得出来（2026-11-09 用户实测：发完消息
   确认按钮又挂回来了）。 */
try{
  var _sa=JSON.parse(localStorage.getItem('cb_send_at')||'{}')||{};
  for(var _k in _sa) if(_sa[_k]) SEND_AT[_k]=_sa[_k];
}catch(e){}
function sendAtSave(){
  try{
    var ks=Object.keys(SEND_AT);
    if(ks.length>40){ ks.sort(function(a,b){return SEND_AT[a]-SEND_AT[b];});
      ks.slice(0,ks.length-40).forEach(function(k){ delete SEND_AT[k]; }); }
    localStorage.setItem('cb_send_at', JSON.stringify(SEND_AT));
  }catch(e){}
}
/* 发言后这段时间内产生的「等你」不弹浮条（毫秒） */
const PEND_QUIET_MS=120000;
function nbLabel(k){return ({done:'完成',perm:'待确认',input:'待回复',ask:'输入',limit:'额度',stuck:'卡住'})[k]||'提醒';}
/* 语音只念「结论」，不念整段通知正文（正文常是最后一段回复，太长且难听） */
/* 提醒总开关（设置页可关）：文字 / 提醒音 / 语音播报。默认都开。
   语音还受本机「智能提醒」总开关管：关了这个开关，这台手机只留基础提醒，
   哪怕开发机因为别的手机开着、照样生成了语音。 */
function notifyCfg(){
  const c=vsCfg();
  return {text:c.ntext!==false, sound:c.nsound!==false,
          voice:smartOn()&&c.nvoice!==false};
}
function nToggle(k){
  const c=vsCfg(); const add={}; add[k]=c[k]===false;    // 取反
  vsSave(Object.assign(c,add)); nRender();
  /* 语音播报是「开发机要不要合成」的总闸之一：改了立刻按设备上报，
     否则关掉后开发机还在调模型/合成（页面以为是关的，钱照花）。 */
  if(k==='nvoice') prefReport();
}
/* 免打扰时段：只留角标和静音通知，不出声、不念。
   存手机配置（cb_cfg）；壳的 BgService 读同一份 cfg_raw（键名必须保持一致：
   dnd / dndF / dndT），所以黑屏时守的也是这套规则。 */
function _dndHM(s){ var m=/^(\d{1,2}):(\d{2})$/.exec(String(s||'')); if(!m) return -1;
  var h=parseInt(m[1],10), mi=parseInt(m[2],10);
  return (h>=0&&h<24&&mi>=0&&mi<60)?(h*60+mi):-1; }
function dndCfg(){
  var c=vsCfg();
  return {on:c.dnd===true,
          f:_dndHM(c.dndF)>=0?c.dndF:'22:00',
          t:_dndHM(c.dndT)>=0?c.dndT:'08:00'};
}let fsub='', fseg='arts';      // 段控：产出物（默认，老板先看这次出了啥）/ 全部文件
function filesOpen(){ fseg='arts'; subOn('filePage',true); filesLoad(''); }
function filesClose(){ subOn('filePage',false); }
function filesSeg(k){ fseg=k; filesLoad(''); }
function fsegRender(){
  const a=document.getElementById('fsegArts'), b=document.getElementById('fsegAll');
  if(a) a.className=(fseg==='arts'?'on':'');
  if(b) b.className=(fseg==='all'?'on':'');
}
function fmtTm(t){
  const d=new Date((t||0)*1000), p=function(n){ return (n<10?'0':'')+n; };
  return (d.getMonth()+1)+'-'+p(d.getDate())+' '+p(d.getHours())+':'+p(d.getMinutes());
}
/*ART_START*/
/* 产出物卡片：类型图标 / 文件名 / 大小 · 时间 · 所在子目录（同名图分得清） */
const ART_ICON={image:'🖼',video:'🎬',audio:'🎵',doc:'📄',model:'🧊',page:'🌐',other:'📦'};
function artDir(rel){ const i=String(rel||'').lastIndexOf('/'); return i>0?rel.slice(0,i):''; }
function fxArtHtml(d){
  const es=(d&&d.entries)||[];
  if(!es.length) return '<div class="nhEmpty">这个会话还没产出文件</div>';
  const grid='<div class="fgrid">'+es.map(function(e){
    const rel=e.rel||e.name, dir=artDir(rel);
    return '<div class="fcard" onclick="filesTap2(\''+encodeURIComponent(rel)+'\',\''
      +esc(e.name).replace(/'/g,'')+'\')">'
      +'<div class="fic">'+(ART_ICON[e.kind]||ART_ICON.other)+'</div>'
      +'<div class="fnm">'+esc(e.name)+'</div>'
      +'<div class="meta">'+fmtSz(e.size||0)+' · '+fmtTm(e.mtime||0)
      +(dir?' · '+esc(dir):'')+'</div></div>';
  }).join('')+'</div>';
  return grid+((d&&d.truncated)
    ?'<div class="meta" style="padding:8px 14px">只列最新 500 项，更早的去「全部文件」里找</div>':'');
}
/*ART_END*/
function fmtSz(n){ if(n>=1048576) return (n/1048576).toFixed(1)+'M'; if(n>=1024) return (n/1024).toFixed(0)+'K'; return n+'B'; }
/* ── 测试红绿卡（2026-11-19 计划 A1）──────────────────────────────────
   只显示会话里跑过的验证命令（服务端白名单：lint/test/build），最近 5 次，
   失败的带末几行输出。重跑只 POST 开跑，结果靠轮询卡片（跑测试可能要几分钟）。 */
let TEST_POLL=null;
async function testOpen(){ subOn('testPage',true); testLoad(); await cmdsProbe(SID); }
function testClose(){ subOn('testPage',false); if(TEST_POLL){clearTimeout(TEST_POLL);TEST_POLL=null;} }
function testBadge(st){ return st==='pass'?'🟢 通过':(st==='fail'?'🔴 失败':'⚪ 未跑'); }
function testCardHtml(it){
  const tail=it.tail?('<pre style="white-space:pre-wrap;word-break:break-all;font-size:11px;max-height:120px;overflow:auto;background:#111111;border-radius:6px;padding:6px;margin:6px 0 0">'+esc(it.tail)+'</pre>'):'';
  return '<div class="fcard" style="padding:10px 12px;margin-bottom:10px">'
    +'<div style="display:flex;align-items:center;gap:8px">'
    +'<span style="flex:none;font-size:12px">'+testBadge(it.state)+'</span>'
    +'<code style="flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:12px;color:#cccccc">'+esc(it.cmd)+'</code>'
    +(it.src==='agent'?'<span class="meta" style="flex:none;font-size:11px">自动</span>':'')
    +'<button class="lt" data-tcrun="'+esc(it.cmd)+'" style="flex:none">重跑</button></div>'
    +'<div class="meta">'+fmtTm(it.ts)+' · '+Math.round(it.dur||0)+'s · 退出码 '+it.rc+'</div>'+tail+'</div>';
}
async function testLoad(){
  const el=document.getElementById('testList'); if(!el) return;
  try{
    const r=await xj('/api/test_cards?sid='+encodeURIComponent(SID));
    const items=(r&&r.json&&r.json.items)||[];
    el.innerHTML=items.length?items.map(testCardHtml).join('')
      :'<div class="nhEmpty">还没有测试记录：在电脑上跑过 pytest / npm test 之后，这里会出现红绿卡</div>';
    return items;
  }catch(e){ el.innerHTML='<div class="nhEmpty">网络错误</div>'; return []; }
}
function testPoll(prevTs,left){
  TEST_POLL=setTimeout(async function(){
    const items=await testLoad();
    const newest=items.length?items[0].ts:0;
    if(newest>prevTs) return;              /* 新结果出来了：停 */
    if(left<=0){ toast('测试还在跑，稍后回来看',true); return; }
    testPoll(prevTs,left-1);
  },3000);
}
async function testRun(cmd){
  await tcStart('/api/test_run',{sid:SID,cmd:cmd},cmd);
}
/* 一键 lint/test/build（A2）：只传 kind，跑什么由服务端 products/<线>/product.json 配置定 */
async function cmdRun(kind){
  await tcStart('/api/cmd_run',{sid:SID,kind:kind},'一键 '+kind);
}
async function tcStart(path,body,label){
  const el=document.getElementById('testList');
  const items=await testLoad();
  const prevTs=items.length?items[0].ts:0;
  if(el) el.innerHTML='<div class="nhEmpty">正在跑：'+esc(label)+'…（结果出来自动刷新）</div>';
  let r;
  try{ r=await xj(path,{method:'POST',body:body}); }
  catch(e){ toast('网络错误',true); testLoad(); return; }
  const ok=r&&r.json&&r.json.ok;
  if(!ok){ toast('没跑起来'+((r&&r.json&&r.json.error)?'：'+r.json.error:''),true); testLoad(); return; }
  testPoll(prevTs,200);                  /* 3s × 200 ≈ 10 分钟上限 */
}
async function filesLoad(sub){
  fsub=sub||'';
  if(fsub) fseg='all';                  // 进了子目录就是文件浏览，别还在「产出物」口径
  const arts=(fseg==='arts' && !fsub);
  fsegRender();
  const fl=document.getElementById('fileList');
  fl.innerHTML='<div class="nhEmpty">加载中…</div>';
  document.getElementById('fcrumb').textContent=fsub?('目录：'+fsub)
    :(arts?'本次会话产出（新→旧）':'工作目录');
  try{
    const r=await xj('/api/ext_files?sid='+encodeURIComponent(SID)+'&sub='+encodeURIComponent(fsub)
      +(arts?'&artifacts=1':''));
    const d=r.json;
    if(!d||d.error){ fl.innerHTML='<div class="nhEmpty">'+esc((d&&d.error)||'加载失败')+'</div>'; return; }
    if(arts){ fl.innerHTML=fxArtHtml(d); return; }
    if(!d.entries.length){ fl.innerHTML='<div class="nhEmpty">空目录</div>'; return; }
    fl.innerHTML=d.entries.map(function(e){
      const p=fsub?(fsub+'/'+e.name):e.name;
      const meta=e.is_dir?'':('<span class="meta">'+fmtSz(e.size)+'</span>');
      return '<div class="frow" onclick="filesTap(\''+encodeURIComponent(p)+'\','+(e.is_dir?1:0)+',\''+esc(e.name).replace(/'/g,'')+'\')">'
        +(e.is_dir?'📂':'📄')+' '+esc(e.name)+' '+meta+'</div>';
    }).join('');
  }catch(e){ fl.innerHTML='<div class="nhEmpty">网络错误</div>'; }
}
let fvPath='', fvName='', fvLine=1, fvTotal=0;
/* 代码子页（批次三）：源码带行号 + 目标行高亮，上下翻页。
   根 = 会话 cwd（服务端 _ext_guard_path 管），这里只渲染。 */
const CODE_EXT=['py','js','mjs','cjs','ts','tsx','jsx','html','css','scss','json','md','sh','zsh','yml','yaml','toml','go','rs','swift','java','kt','c','h','cpp','hpp','rb','php','sql','xml','ini'];
function isCodeExt(ext){ return CODE_EXT.indexOf(String(ext||'').toLowerCase())>=0; }
async function fviewCode(path,line){
  fvLine=Math.max(1,parseInt(line||1,10)||1);
  CODE_ANCHOR=0;
  const body=document.getElementById('fvBody');
  const ob=document.getElementById('fvOpen');
  body.innerHTML='<pre style="white-space:pre;font-size:12px;color:#cccccc;overflow:auto">读取中…</pre>';
  ob.style.display=hasBridge()?'':'none';
  try{
    const r=await xj('/api/ext_code?sid='+encodeURIComponent(SID)
      +'&path='+encodeURIComponent(path)+'&line='+fvLine);
    const d=r.json||{};
    if(d.error){ body.innerHTML='<div class="meta">'+esc(d.error)+'</div>'; ob.style.display=''; return; }
    fvTotal=d.total||0;
    const ls=d.lines||[];
    const first=ls.length?ls[0].no:0, last=ls.length?ls[ls.length-1].no:0;
    const pad=String(Math.max(1,last)).length;
    let h='';
    if(/\.html?$/i.test(String(path)))   /* 源码在文件里，效果去预览里看 */
      h+='<div style="margin:0 0 6px"><button onclick="fvPreviewTap()">🌐 看效果（预览）</button></div>';
    h+=fvEditBtnHtml();
    h+='<div class="meta" style="margin:0 0 6px">'+esc(d.lang||'text')+' · 共 '+(d.total||0)
      +' 行 · 第 '+first+'-'+last+' 行</div>';
    if(d.total>ls.length){
      h+='<div style="display:flex;gap:8px;margin-bottom:6px">'
        +'<button onclick="fviewCodePage(-1)"'+(first<=1?' disabled':'')+'>↑ 上一页</button>'
        +'<button onclick="fviewCodePage(1)"'+(last>=d.total?' disabled':'')+'>↓ 下一页</button>'
        +'<button onclick="fviewCodeJump()">跳到行</button></div>';
    }
    h+='<pre style="white-space:pre;font-size:12px;line-height:1.5;color:#cccccc;overflow:auto;max-height:60vh">';
    CODE_PG={path:path,first:first,lines:ls.map(function(x){ return x.text; })};
    h+=ls.map(function(x){
      const hl=(x.no===fvLine)?'background:#1f1f1f;':'';
      /* 行号可点：挑一行/一段预填引用（手机上拖选字很费劲） */
      return '<span style="'+hl+'"><span data-cn="'+x.no+'" title="点行号引用"'
        +' style="color:#3d3d3d;user-select:none;cursor:pointer" onclick="codeQuoteTap('+x.no+')">'
        +('        '+x.no).slice(-pad)+' </span>'+esc(x.text)+'</span>';
    }).join('\n');
    h+='</pre>';
    body.innerHTML=h;
    codeQuoteMark();
  }catch(e){ body.innerHTML='<div class="meta">读取失败</div>'; }
}
/* 消息里点 path:line：直接开代码子页并高亮那一行 */
var CODE_PG={path:'',first:1,lines:[]}, CODE_ANCHOR=0;
/* 源码行引用：a/b 是全局行号，钳在本页范围；翻页后 first>1 也要对 */
function codeQuoteText(path,first,a,b,lines){
  const last=first+lines.length-1;
  a=Math.max(first,Math.min(parseInt(a,10)||first,last));
  b=Math.max(first,Math.min(parseInt(b,10)||first,last));
  if(a>b){ const t=a; a=b; b=t; }
  return '[引用 '+path+':'+a+'-'+b+']\n'+lines.slice(a-first,b-first+1).join('\n')+'\n';
}
function codeQuoteMark(){
  const g=document.querySelectorAll('#fvBody [data-cn]');
  for(let i=0;i<g.length;i++){
    g[i].style.background=(parseInt(g[i].getAttribute('data-cn'),10)===CODE_ANCHOR)?'#3b2f00':'';
  }
}
function codeQuoteTap(no){
  no=parseInt(no,10)||0;
  if(!CODE_ANCHOR){ CODE_ANCHOR=no; codeQuoteMark(); toast('再点一行定范围；点同一行只引这一行'); return; }
  const t=codeQuoteText(CODE_PG.path,CODE_PG.first,CODE_ANCHOR,no,CODE_PG.lines);
  CODE_ANCHOR=0; codeQuoteMark();
  fviewClose();
  const ta=document.getElementById('txt');
  ta.value=t+(ta.value||'');
  try{ ta.focus(); }catch(e){}
  try{ fitTxt(); }catch(e){}
  toast('已引用源码，接着说要改什么');
}
function openCodeAt(path,line){
  fvPath=path; fvName=String(path).split('/').pop();
  document.getElementById('fvTitle').textContent=fvName;
  document.getElementById('fview').classList.add('on');
  fviewCode(path,line||1);
}
/* 网页的「效果」不在文件浮层里铺（这里是看源码的地方）：交给预览——
   已经在跑的服务答得出这个文件就用它，否则在电脑上起一个静态服务（/api/ext_web）。 */
function fvPreviewTap(){
  if(!codingOn()){ toast('编码模式没开：设置 → 外观 → 编码模式',true); return; }
  fviewClose();
  sheetOpen((location.origin||'')+'/api/ext_web?open=1&sid='+encodeURIComponent(SID)
    +'&path='+encodeURIComponent(fvPath),'网页预览');
}
/* ── 产出物编辑（窗口核心 Phase 2）：✏️ → 拉全文改 → 保存落 diff → 短引用消息回流 ── */
var FV_EDIT=null;
/* 入口钮：编码模式开着才渲染（文本类代码视图都走这里；图片/视频不经过它） */
function fvEditBtnHtml(){
  if(!codingOn()) return '';
  return '<div style="margin:0 0 6px"><button onclick="fvEditOpen()">✏️ 编辑</button></div>';
}
async function fvEditOpen(){
  if(!codingOn()){ toast('编码模式没开：设置 → 外观 → 编码模式',true); return; }
  const body=document.getElementById('fvBody');
  body.innerHTML='<div class="meta">读取中…</div>';
  try{
    const r=await xj('/api/ext_art_text?sid='+encodeURIComponent(SID)
      +'&rel='+encodeURIComponent(fvPath));
    const d=r.json||{};
    if(!d.ok) throw new Error(d.error||('HTTP '+r.status));
    FV_EDIT={hash:d.hash, rel:fvPath};
    body.innerHTML='<textarea id="fvEditTa" spellcheck="false" '
      +'style="width:100%;min-height:50vh;background:#111111;color:#cccccc;border:1px solid #2a2a2a;'
      +'font-size:12px;line-height:1.5;padding:8px;white-space:pre;overflow:auto"></textarea>'
      +'<div style="display:flex;gap:8px;margin-top:8px">'
      +'<button id="fvEditSave" onclick="fvEditSave()">💾 保存并让 agent 接手</button>'
      +'<button class="gray" onclick="fviewShow(fvPath,fvName)">取消</button></div>';
    document.getElementById('fvEditTa').value=d.text;
  }catch(e){
    body.innerHTML='<div class="meta">读不了这个文件：'+escHtml(String((e&&e.message)||e))+'</div>';
  }
}
async function fvEditSave(){
  const ta=document.getElementById('fvEditTa');
  if(!ta||!FV_EDIT) return;
  const btn=document.getElementById('fvEditSave');
  if(btn){ btn.disabled=true; btn.textContent='保存中…'; }
  try{
    /* diff 走文件不走红字：ref_msg 是 ≤120 字的短引用（Review Focus：长消息进 pty 会碎） */
    const r=await xj('/api/ext_art_edit',{method:'POST',body:{sid:SID,rel:FV_EDIT.rel,
      content:ta.value,base_hash:FV_EDIT.hash}});
    const d=r.json||{};
    if(r.status===409||d.conflict){ toast('文件已变（agent 改过它）——重开再看最新内容再改',true); return; }
    if(!d.ok) throw new Error(d.error||('HTTP '+r.status));
    toast('已保存，diff 交给会话');
    const msg=String(d.ref_msg||'');
    fviewClose();
    if(msg){                       /* 预填后自动发：走 sendMsg 的既有通道（pty/桌面/ACP 都通） */
      const t2=document.getElementById('txt');
      t2.value=msg;
      try{ await sendMsg(); }catch(e){}
    }
  }catch(e){
    toast('保存失败：'+String((e&&e.message)||e),true);
    if(btn){ btn.disabled=false; btn.textContent='💾 保存并让 agent 接手'; }
  }
}
function fviewCodePage(dir){
  const to=fvLine+dir*400;
  fviewCode(fvPath, Math.max(1,to));
}
function fviewCodeJump(){
  const v=prompt('跳到第几行？',''+(fvLine));
  if(v) fviewCode(fvPath, v);
}
function filesTap2(p,name){
  fvPath=decodeURIComponent(p); fvName=name;
  fviewShow(fvPath,name);
}
function filesTap(p,isDir,name){
  if(isDir){ filesLoad(decodeURIComponent(p)); return; }
  fvPath=decodeURIComponent(p); fvName=name;
  fviewShow(fvPath,name);
}
/*FVT_START*/
/* 文本预览的缓存参数必须叫 `_`：以前叫 `t`，和 host 的 token 参数撞名——
   中继转发时会再补一个 &t=<token>，require_token 只读第一个 t（= 时间戳）→
   打开 JSON/MD 直接显示「bad token」（2026-11-19 反馈）。请求也改走 xj：
   老 WebView 的裸 fetch 连 cookie 都不带。 */
function fvUrl(){ return '/api/ext_artifact?sid='+encodeURIComponent(SID)+'&path='+encodeURIComponent(fvPath)+'&_='+Date.now(); }
async function fvText(){
  const r=await xj(fvUrl());
  if(!r.ok) throw new Error(String(r.status)+' '+String(r.text||'').slice(0,40));
  return String(r.text||'').slice(0,20000)||'(空)';
}
/*FVT_END*/
function hasBridge(){ try{ return !!(window.CB&&CB.openWith); }catch(e){ return false; } }
function fviewSysOpen(){ try{ CB.openWith(SID, fvPath); }catch(e){ toast('打开失败',true); } }
function fviewClose(){ document.getElementById('fview').classList.remove('on'); }
/*FVI_START*/
/* 扩展名 → 预览方式：图/视频分块取（大文件走 mediaPull），文档交系统应用，
   文本贴出来，源码进代码子页。 */
const FV_MIME={png:'image/png',jpg:'image/jpeg',jpeg:'image/jpeg',gif:'image/gif',webp:'image/webp',
  mp4:'video/mp4',webm:'video/webm',mov:'video/quicktime',
  m4a:'audio/mp4',mp3:'audio/mpeg',wav:'audio/wav'};
function fvKind(ext){
  ext=String(ext||'').toLowerCase();
  if(['png','jpg','jpeg','webp','gif'].indexOf(ext)>=0) return 'image';
  if(['mp4','webm','mov'].indexOf(ext)>=0) return 'video';
  if(['mp3','wav','m4a'].indexOf(ext)>=0) return 'audio';
  if(['pdf','docx','xlsx','pptx','doc','xls','ppt','zip'].indexOf(ext)>=0) return 'doc';
  if(ext==='stl') return 'model';           /* 3D 模型：页面里转视角看 */
  if(ext==='html'||ext==='htm') return 'page';   /* 网页先给效果（预览），起不来才退回源码 */
  if(['md','txt','csv','json','log'].indexOf(ext)>=0) return 'text';
  if(isCodeExt(ext)) return 'code';
  return 'other';
}
function fvMime(ext){ return FV_MIME[String(ext||'').toLowerCase()]||'application/octet-stream'; }
/*FVI_END*/
var FV_BLOB={};       /* 面板里取过的：同一个文件多看几次别重下（最多留 4 段） */
function fvMediaUrl(path,ext,onP){
  if(FV_BLOB[path]) return Promise.resolve(FV_BLOB[path]);
  const ep='/api/ext_file?p='+encodeURIComponent(path)+(SID?'&sid='+encodeURIComponent(SID):'');
  return mediaPull(ep,onP).then(function(blob){
    const ks=Object.keys(FV_BLOB);
    while(ks.length>=4){ var k=ks.shift(); try{ URL.revokeObjectURL(FV_BLOB[k]); }catch(e){} delete FV_BLOB[k]; }
    return (FV_BLOB[path]=URL.createObjectURL(new Blob([blob],{type:fvMime(ext)})));
  });
}
function fvNote(body,txt){
  let n=document.getElementById('fvNote');
  if(!n){ n=document.createElement('div'); n.id='fvNote'; n.className='meta'; n.style.marginTop='6px';
    body.parentNode.insertBefore(n, body.nextSibling); }
  n.textContent=txt;
}
function fvNoteDone(){ const n=document.getElementById('fvNote'); if(n) n.remove(); }
async function fviewShow(path,name){
  const ext=(name.split('.').pop()||'').toLowerCase();
  const kind=fvKind(ext);
  fvPath=path;
  document.getElementById('fvTitle').textContent=name;
  /* 看完就地表态：能渲染的都在底下给「确认 / 提建议」，PDF 之类交给系统应用的没有 */
  document.getElementById('fvReview').classList[fvReviewKind(kind)?'add':'remove']('on');
  const body=document.getElementById('fvBody');
  const ob=document.getElementById('fvOpen');
  fvNoteDone();
  if(kind==='image'||kind==='video'||kind==='audio'){
    ob.style.display='none';
    document.getElementById('fvCrop').style.display=(kind==='image')?'':'none';
    body.innerHTML='<div class="meta">加载中…</div>';
    document.getElementById('fview').classList.add('on');
    const tag=kind==='image'?'img':kind;
    const el=document.createElement(tag);
    if(kind!=='image') el.controls=true;
    el.style.maxWidth='100%'; el.style.maxHeight='60vh'; el.style.display='block'; el.style.margin='0 auto';
    try{
      el.src=await fvMediaUrl(path,ext,function(off,total){
        fvNote(body,total>0?('加载中 '+Math.floor(off*100/total)+'% · '
          +fmtSz(off)+'/'+fmtSz(total)):('加载中 '+fmtSz(off)));
      });
      fvNoteDone();
      body.innerHTML=''; body.appendChild(el);
    }catch(st){
      fvNoteDone();
      body.innerHTML='<div class="meta">打不开这个文件（'+(st||0)+'）。</div>';
      ob.style.display=hasBridge()?'':'none';
    }
    return;
  }
  if(kind==='page'){
    /* 网页：用户关心的是效果，优先进预览（2026-11-20）。
       已经在跑的服务答得出这个文件就用它，没有就在电脑上起一个静态服务；
       直接跳预览查看页，标题里带上访问地址。预览起不来才退回源码。 */
    document.getElementById('fvCrop').style.display='none';
    body.innerHTML='<div class="meta">正在准备预览…</div>';
    ob.style.display='none';
    document.getElementById('fview').classList.add('on');
    try{
      const r=await xj('/api/ext_web?sid='+encodeURIComponent(SID)
        +'&path='+encodeURIComponent(path));
      const d=r.json||{};
      if(!r.ok||d.error||!d.href) throw new Error(d.error||('HTTP '+r.status));
      fviewClose();
      sheetOpen((location.origin||'')+d.href,'网页预览 · '+(d.url||''));
    }catch(e){
      fviewCode(path,1);                /* 没有预览：这个文件看源码 */
      fvNote(body,'预览起不来（'+String((e&&e.message)||e)+'），先看源码');
    }
    return;
  }
  if(kind==='text'){
    document.getElementById('fvCrop').style.display='none';
    body.innerHTML='<pre style="white-space:pre-wrap;font-size:12px;color:#cccccc">读取中…</pre>';
    try{
      body.firstChild.textContent=await fvText();
      ob.style.display='none';
      body.insertAdjacentHTML('beforeend', fvEditBtnHtml());
    }catch(e){
      body.firstChild.textContent='读取失败（'+String((e&&e.message)||e)+'）';
      ob.style.display=hasBridge()?'':'none';
    }
  } else if(kind==='model'){
    document.getElementById('fvCrop').style.display='';   /* 转好视角就能圈 */
    body.innerHTML='<div class="meta">加载模型…</div>';
    ob.style.display='none';
    document.getElementById('fview').classList.add('on');
    try{
      const r=await xj('/api/ext_artifact?sid='+encodeURIComponent(SID)
                       +'&path='+encodeURIComponent(path),{bin:true,timeout:60000});
      if(!r.ok) throw new Error('加载失败（'+r.status+'）');
      const mesh=stlParse(r.buf);
      body.innerHTML='';
      stlView(body,mesh);
    }catch(e){
      document.getElementById('fvCrop').style.display='none';
      body.innerHTML='<div class="meta">打不开这个模型（'+esc(String((e&&e.message)||e))+'）。</div>';
      ob.style.display=hasBridge()?'':'none';
    }
    return;
  } else if(kind==='code'){
    document.getElementById('fvCrop').style.display='none';
    document.getElementById('fview').classList.add('on');   /* 先开浮层：fviewCode 只填内容 */
    fviewCode(path,1);                 /* 源码：带行号 + 上下翻页（批次三） */
    return;
  } else {
    document.getElementById('fvCrop').style.display='none';
    /* 文档/压缩包/未知类型：手机上的应用比页面会开（pdf/docx/xlsx/pptx）
       视频音频 codec 不认时也走这里兜底 */
    body.innerHTML='<div class="meta">此类型（.'+esc(ext)+'）用手机上的其他应用打开。</div>';
    ob.style.display=hasBridge()?'':'none';
  }
  document.getElementById('fview').classList.add('on');
}
/* ── 圈选发指令（批次三）──
   图片：预览里点「✂️ 圈选」→ 全屏拖框 → 长边 ≤1024 裁 → /api/ext_upload →
   输入框预填 [图: <路径> 选中区域]，用户接着长按麦克风说一句，一起发出。
   终端：全彩实时视图的浮层里点「⛶ 引用」→ 挑起止行 → 预填 [引用终端 a-b 行] + 原文。
   入口做成按钮而不是长按：#liveflow 的长按是浏览器原生选字复制，抢了就没法复制。 */
/*CROP_START*/
/* 圈选几何：手机显示坐标（相对图片绘制区）→ 原图坐标；再按长边 ≤1024 缩 */
function cropBox(pts, dispW, dispH, natW, natH){
  const kx=natW/dispW, ky=natH/dispH;
  let x=Math.min(pts.sx,pts.ex)*kx, y=Math.min(pts.sy,pts.ey)*ky;
  let w=Math.abs(pts.ex-pts.sx)*kx, h=Math.abs(pts.ey-pts.sy)*ky;
  x=Math.max(0,Math.min(x,natW)); y=Math.max(0,Math.min(y,natH));
  w=Math.max(0,Math.min(w,natW-x)); h=Math.max(0,Math.min(h,natH-y));
  return {x:Math.round(x),y:Math.round(y),w:Math.round(w),h:Math.round(h)};
}
function cropScale(w,h,max){
  max=max||1024;
  const k=Math.min(1,max/Math.max(w||1,h||1));
  return {w:Math.round(w*k),h:Math.round(h*k)};
}
/*CROP_END*/
/*QUOTE_START*/
/* 全彩屏 cells（颜色游程）→ 纯文本行；终端行定宽，行尾空白去掉 */
function cellsLines(cells){
  return (cells||[]).map(function(row){
    return (row||[]).map(function(run){ return run[6]||''; }).join('').replace(/\s+$/,'');
  });
}
function quoteLines(lines, a, b){
  const n=lines.length;
  a=Math.max(1,Math.min(parseInt(a,10)||1,n||1));
  b=Math.max(1,Math.min(parseInt(b,10)||1,n||1));
  if(a>b){ const t=a; a=b; b=t; }
  return '[引用终端 '+a+'-'+b+' 行]\n'+lines.slice(a-1,b).join('\n')+'\n';
}
/*QUOTE_END*/
var CROP=null;            /* {sx,sy,ex,ey}：相对图片显示区 */
function fvCropOpen(){
  const fb=document.getElementById('fvBody');
  const img=fb.querySelector('img');
  let src=(img&&img.src)||'';
  if(!src){
    /* 3D 模型没有 <img>：把当前视角这一帧截下来（WebGL 上下文开了 preserveDrawingBuffer，
       转完直接截就是屏幕上看到的样子） */
    const cv=fb.querySelector('canvas');
    if(cv){ try{ src=cv.toDataURL('image/png'); }catch(e){} }
  }
  if(!src){ toast('图片还没加载好，稍等再圈',true); return; }
  document.getElementById('cropImg').src=src;   /* 同一个 blob/data URL，不重复下载 */
  CROP=null; cropPaint();
  document.getElementById('cropOk').style.display='none';
  document.getElementById('cropPage').classList.add('on');
}
function cropClose(){ document.getElementById('cropPage').classList.remove('on'); CROP=null; }
function cropPaint(){
  const r=document.getElementById('cropRect');
  if(!CROP){ r.style.display='none'; return; }
  const ir=document.getElementById('cropImg').getBoundingClientRect();
  const br=document.getElementById('cropBox').getBoundingClientRect();
  r.style.display='block';
  r.style.left=(Math.min(CROP.sx,CROP.ex)+ir.left-br.left)+'px';
  r.style.top=(Math.min(CROP.sy,CROP.ey)+ir.top-br.top)+'px';
  r.style.width=Math.abs(CROP.ex-CROP.sx)+'px';
  r.style.height=Math.abs(CROP.ey-CROP.sy)+'px';
}
(function(){
  const box=document.getElementById('cropBox');
  if(!box) return;
  let drag=false;
  function pt(clientX,clientY){
    const ir=document.getElementById('cropImg').getBoundingClientRect();
    return {x:Math.max(0,Math.min(clientX-ir.left,ir.width)),
            y:Math.max(0,Math.min(clientY-ir.top,ir.height))};
  }
  function begin(x,y){
    const p=pt(x,y);
    CROP={sx:p.x,sy:p.y,ex:p.x,ey:p.y};
    drag=true; document.getElementById('cropOk').style.display='none'; cropPaint();
  }
  function moveTo(x,y){ if(!drag||!CROP) return; const p=pt(x,y); CROP.ex=p.x; CROP.ey=p.y; cropPaint(); }
  function end(){
    drag=false;
    if(CROP&&Math.abs(CROP.ex-CROP.sx)>8&&Math.abs(CROP.ey-CROP.sy)>8)
      document.getElementById('cropOk').style.display='';
  }
  box.addEventListener('touchstart',function(e){
    if(e.touches.length!==1) return; begin(e.touches[0].clientX,e.touches[0].clientY); e.preventDefault();
  },{passive:false});
  box.addEventListener('touchmove',function(e){
    if(!drag||e.touches.length!==1) return; moveTo(e.touches[0].clientX,e.touches[0].clientY); e.preventDefault();
  },{passive:false});
  box.addEventListener('touchend',end);
  box.addEventListener('mousedown',function(e){ begin(e.clientX,e.clientY); e.preventDefault(); });
  box.addEventListener('mousemove',function(e){ moveTo(e.clientX,e.clientY); });
  document.addEventListener('mouseup',end);
})();
function cropBlob(){
  return new Promise(function(res){
    const ci=document.getElementById('cropImg');
    if(!CROP||!ci.naturalWidth){ res(null); return; }
    const ir=ci.getBoundingClientRect();
    const r=cropBox(CROP,ir.width,ir.height,ci.naturalWidth,ci.naturalHeight);
    const sc=cropScale(r.w,r.h,1024);
    if(sc.w<4||sc.h<4){ res(null); return; }
    const c=document.createElement('canvas');
    c.width=sc.w; c.height=sc.h;
    c.getContext('2d').drawImage(ci,r.x,r.y,r.w,r.h,0,0,sc.w,sc.h);
    try{ c.toBlob(function(b){ res(b); },'image/png'); }catch(e){ res(null); }
  });
}
async function cropRun(){
  const b=await cropBlob();
  if(!b){ toast('框太小了，重新圈一下',true); return; }
  const fd=new FormData(); fd.append('file',b,'crop.png');
  try{
    const r=await xj('/api/ext_upload?sid='+encodeURIComponent(SID),
                     {method:'POST',body:fd,timeout:60000});
    const d=r.json||null;
    if(!(d&&d.ok)){ toast('上传失败：'+((d&&d.error)||r.status),true); return; }
    cropClose(); fviewClose();
    try{ subOn('filePage',false); }catch(e){}
    const ta=document.getElementById('txt');
    ta.value='[图: '+d.path+' 选中区域] '+(ta.value||'');
    try{ ta.focus(); }catch(e){}
    try{ fitTxt(); }catch(e){}
    toast('圈好了：长按麦克风说一句，一起发');
  }catch(e){ toast('网络错误：'+(e&&e.message||e),true); }
}
var QLINES=[], QSEL=null;      /* QSEL=[a,b] 1 基行号 */
function quoteFromTabs(){
  document.getElementById('tabs').classList.remove('on');
  if(!SID){ toast('先打开一个会话',true); return; }
  if(!fullColor){ toast('该会话未接入全彩镜像',true); return; }
  quoteOpen();
}
async function quoteOpen(){
  const box=document.getElementById('qLines');
  box.innerHTML='<div class="meta" style="padding:10px">读取中…</div>';
  QLINES=[]; QSEL=null; quoteRender();
  document.getElementById('qsheet').classList.add('on');
  try{
    const r=await xj('/api/ext_cells/'+encodeURIComponent(SID),{timeout:30000});
    const d=r.json||{};
    if(!d.ok){ box.innerHTML='<div class="meta" style="padding:10px">'+esc(d.error||'读不到终端')+'</div>'; return; }
    QLINES=cellsLines(d.cells);
    /* 只列最后 200 行（手机翻不到更早的），行号仍按全局算，和终端里看到的一致 */
    const from=Math.max(0,QLINES.length-200);
    box.innerHTML=QLINES.slice(from).map(function(t,i){
      const no=from+i+1;
      return '<div class="qrow" data-n="'+no+'" onclick="quoteTap('+no+')">'+no+' '+esc(t||' ')+'</div>';
    }).join('');
    box.scrollTop=box.scrollHeight;
  }catch(e){ box.innerHTML='<div class="meta" style="padding:10px">网络错误</div>'; }
}
function quoteRender(){
  const rs=document.querySelectorAll('#qLines .qrow');
  for(let i=0;i<rs.length;i++){
    const n=parseInt(rs[i].getAttribute('data-n'),10);
    rs[i].className='qrow'+((QSEL&&n>=QSEL[0]&&n<=QSEL[1])?' on':'');
  }
  document.getElementById('qRange').textContent=QSEL
    ?('第 '+QSEL[0]+'-'+QSEL[1]+' 行'):'点第一行，再点最后一行';
}
function quoteTap(n){
  if(!QSEL){ QSEL=[n,n]; }
  else if(QSEL[0]===QSEL[1]){ QSEL=[Math.min(QSEL[0],n),Math.max(QSEL[0],n)]; }
  else { QSEL=[n,n]; }            /* 已经选过一段：第三次点 = 重新开始 */
  quoteRender();
}
function quoteRun(){
  if(!QSEL||!QLINES.length) return;
  const t=quoteLines(QLINES,QSEL[0],QSEL[1]);
  const span=QSEL[1]-QSEL[0]+1;
  quoteClose();
  const ta=document.getElementById('txt');
  ta.value=t+(ta.value||'');
  try{ ta.focus(); }catch(e){}
  try{ fitTxt(); }catch(e){}
  toast('已引用 '+span+' 行，接着说要改什么');
}
function quoteClose(){ document.getElementById('qsheet').classList.remove('on'); }
async function photoPick(inp){
  const f=inp.files&&inp.files[0]; inp.value='';
  if(!f||!SID) return;
  toast('上传图片中…');
  try{
    const fd=new FormData(); fd.append('file',f,f.name||'photo.jpg');
    const r=await fetch('/api/ext_upload?sid='+encodeURIComponent(SID),{method:'POST',body:fd});
    const d=await r.json().catch(function(){return null;});
    if(d&&d.ok){
      const ta=document.getElementById('txt');
      ta.value=(ta.value?ta.value+'\n':'')+'[图片已附: '+d.path+' 请查看这个文件]';
      ta.focus(); fitTxt();
      toast('图片已附上，点「发送」让 agent 查看');
    } else toast('上传失败：'+((d&&d.error)||r.status),true);
  }catch(e){ toast('网络错误:'+(e&&e.message||e),true); }
}
