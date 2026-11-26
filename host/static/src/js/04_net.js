// SPDX-License-Identifier: AGPL-3.0-or-later

function bgRemindOn(){ try{ return typeof vsCfg==='function'&&vsCfg().bgn!==false; }
  catch(e){ return false; } }
/* 壳读不到 WebView 的 cookie（2026-11-19 S24 mini：getCookie 只回一个 cb_product），
   黑屏时页面又被冻结、没人能给壳换 10 分钟的 dt。所以页面用登录态向中继要一把
   90 天的设备凭据（/api/bg_ticket），交给壳存起来，壳自己带 X-CB-Bgd 头去拉
   /api/bg_push 和播报音频。票按「哪台开发机」分开存：切台后旧票不能交差。 */
function bgTokDev(){
  try{ var m=document.cookie.match(/(?:^|;\s*)cb_dev=([^;]+)/);
       return m?decodeURIComponent(m[1]):''; }catch(e){ return ''; }
}
function bgTokSync(){
  if(!(window.CB&&CB.bgToken)) return;
  var dev=bgTokDev(), j=null;
  try{ j=JSON.parse(localStorage.getItem('cb_bgtok')||''); }catch(e){}
  var now=Date.now()/1000;
  if(j&&j.tok&&j.dev===dev&&j.exp-now>7*86400){ try{ CB.bgToken(j.tok); }catch(e){} return; }
  xj('/api/bg_ticket').then(function(r){
    var d=r&&r.json;
    if(!r||!r.ok||!d||!d.ok||!d.tok) return;      // 没登录/设备不在：壳继续用旧票
    try{ localStorage.setItem('cb_bgtok',JSON.stringify({dev:dev,tok:d.tok,exp:d.exp})); }catch(e){}
    try{ CB.bgToken(d.tok); }catch(e){}
  });
}
setTimeout(function(){ try{ bgTokSync(); }catch(e){} },2500);
setInterval(function(){ try{ bgTokSync(); }catch(e){} },6*3600*1000);
/* 最近一次「页面重新可见」的时刻：息屏/切走期间来的提醒不归页面响 */
var VIS_AT=Date.now()/1000, HID_AT=0;
document.addEventListener('visibilitychange',function(){
  if(document.visibilityState==='visible') VIS_AT=Date.now()/1000;
  else {
    HID_AT=Date.now()/1000;
    /* 切走/黑屏：自动听写立刻收掉（壳的 onPause 也会兜一层） */
    try{ if(window.__cbAutoEnd) window.__cbAutoEnd(); }catch(e){}
  }
});
function onNotify(d){
  if(!d||!d.sid) return;
  /* 本机智能提醒关着：开发机可能因为别的手机开着做了智能判断（完成→待确认），
     这台手机按规则结果提示——它要的是基础提醒，不是别人那份智能。 */
  if(!smartOn()&&(d.kind_rule||d.sound_rule)){
    d=Object.assign({},d);
    if(d.kind_rule) d.kind=d.kind_rule;
    if(d.sound_rule) d.sound=d.sound_rule;
  }
  /* 会话页浮条：不管这条要不要出声，先记下「这一轮在等你」 */
  try{ pendNote(d); }catch(e){}
  var nkey=(d.id||0)+'|'+(d.ts||0);
  if(d.id&&NOTIF_SEEN[nkey]) return;
  var w0=nwLoad();
  /* 「这一轮之前就提醒过了」：id 和时刻都旧才算。开发机重启后 id 从 1 重来，
     只比 id 会把新提醒全当旧的吃掉（见 nwBump）。 */
  var seenBefore=!!(d.id&&w0.last&&d.id<=w0.last&&(!d.ts||d.ts<=w0.at));
  if(d.id){
    NOTIF_SEEN[nkey]=1;
    if(d.id>NOTIF_MAXID) NOTIF_MAXID=d.id;
    nwBump(d.id,d.ts);
    if(Object.keys(NOTIF_SEEN).length>400) NOTIF_SEEN={};   /* 兜底：清一次最多补响一条 */
  }
  /* 有壳、且是新壳（CB.announces）时，出声这件事**全归壳**：它不看前台/后台，
     收到就响一遍并记自己的水位。页面这时只负责角标和 toast，一句都不念——
     以前两边各管一段，壳把「前台」让给页面、页面又被冻着，就成了「熄屏没人响、
     亮屏才补念」，补念时音频过期还会换成别的嗓音。 */
  var shellAnn=!!(window.CB&&typeof CB.announces==='function'&&CB.announces());
  /* 告诉壳「这条页面已经看到了」：后台服务重连时据此跳过补发，别响两遍。
     壳负责出声时不要报——报了壳会以为这条已经处理过，反而把它跳过（谁都不响）。 */
  try{ if(d.id&&window.CB&&CB.seenNotify&&!shellAnn) CB.seenNotify(d.id); }catch(e){}
  const nc=notifyCfg();
  /* 免打扰时段：角标照挂、通知照发（走静音渠道），但不响提示音、不念语音。
     壳在黑屏时由 BgService 按同一份配置守；这里管页面/浏览器自己出声的那些路径。 */
  const dnd=dndActive();
  /* 静音（长按音量下键）：和免打扰同一套降级——角标、通知照常，只是不出声、不念。
     壳在后台出声的那些路径由 BgService 读 cfg.mute 守着，两边口径一致。 */
  const qm=muteOn();
  if(dnd||qm){ nc.sound=false; nc.voice=false; }
  /* 壳报的前后台比 document.hidden 准：安卓黑屏时 document.hidden 常常还是 false */
  const vis=!hiddenNow();
  /* 正在看这个会话：不挂角标、不发系统通知（页面自己会显示状态） */
  const viewing=(d.sid===SID&&pageIdx===1&&vis);
  if(nc.text&&!viewing){
    NOTIFY[d.sid]={kind:d.kind,ts:d.ts||0,text:d.text||''};
    if(lastExt.length||lastWeb.length) renderList();
  }
  /* 诊断：「有没有播报」由哪一环决定，全在这一行里（≤60 字，nDbg 会截断）。
     sb=页面判定已响过 wl=页面水位 sq=本次 hello 序号 b=壳记的已响过水位
     nv=页面语音开关 dv=这条提醒要不要念 dn=免打扰 s=say 音频地址长度 sh=有没有壳 */
  try{ if(d.sid===SID&&window.__cbDbg){
    var _bg=-9; try{ if(window.CB&&CB.bgAnnounced) _bg=CB.bgAnnounced(); }catch(e){}
    window.__cbDbg('N'+d.id+String(d.kind||'')[0]+' sb'+(seenBefore?1:0)+' wl'+w0.last
      +' sq'+PUSH.seq+' b'+_bg+' nv'+(nc.voice?1:0)+' dv'+(d.voice?1:0)
      +' dn'+(dnd?1:0)+' qm'+(qm?1:0)+' s'+String(d.say||'').length+' sh'+(shellAnn?1:0));
  } }catch(e){}
  if(seenBefore) return;   /* 已经提醒过：角标补上就够了，不再出声 */
  /* 自动听写：这一轮干完了/要确认 → 布防等用户说话（只当前会话、亮屏前台） */
  try{ if(viewing&&window.__cbAutoArm) window.__cbAutoArm(d,nc,shellAnn); }catch(e){}
  if(shellAnn){
    /* 壳说「出声归我」时页面一句都不念。3 秒后再问一次壳：它到底认没认这条
       （a=壳记的已响过水位，b=此刻是否在出声）。没播报时靠这行定位是
       「壳没收到/没收下」还是「收下了但没放出来」。 */
    try{ setTimeout(function(){
      try{
        var a=-9, b=0;
        try{ if(CB.bgAnnounced) a=CB.bgAnnounced(); }catch(e){}
        try{ if(CB.voiceBusy) b=CB.voiceBusy()?1:0; }catch(e){}
        if(window.__cbDbg) window.__cbDbg('Nsh'+d.id+' a'+a+' b'+b);
      }catch(e){}
    },3000); }catch(e){}
    /* 壳说在管，可它的后台通道可能是断的（/api/bg_push 401、黑屏时被冻）：
       那样它一条都收不到，页面再让着它就是谁都不响——2026-11-19 小手机
       「语音播报、提醒音、通知都没有」，现场就是它每 60 秒一次 HTTP 401。
       4 秒后核一次壳的水位：还没认领这条 → 页面自己补上（先报 seenNotify，
       壳晚点重连补发时据此跳过，不会响两遍）。 */
    try{ setTimeout(function(){
      var a=-1;
      try{ a=Number(CB.bgAnnounced&&CB.bgAnnounced()); }catch(e){ a=-1; }
      if(isFinite(a)&&d.id&&a>=d.id) return;         /* 壳收下了，正常 */
      try{ if(d.id&&CB.seenNotify) CB.seenNotify(d.id); }catch(e){}
      try{ pageAnnounce(d,nc,vis,viewing); }catch(e){}
    },4000); }catch(e){}
    /* 壳在念：页面只剩界面反馈（前台时才弹 toast，别挡着用户看终端）。
       文字提醒关着时壳不会发系统通知，也就没有那声提示音——前台补一声，
       否则「提示音开、文字提醒关」这个组合在前台是哑的。 */
    if(vis&&nc.sound&&!nc.text)
      beep(d.sound||({done:'done',perm:'alert',input:'ping',ask:'ping',limit:'alert',stuck:'ping'})[d.kind]||'done');
    if(vis&&nc.text&&!viewing)
      toast('🔔 '+nbLabel(d.kind)+' · '+(((lastExt.find(function(x){return x.sid===d.sid;})||{}).title)||d.title||''));
    return;
  }
  pageAnnounce(d,nc,vis,viewing);
}
/* 页面自己把这条提醒响一遍：提示音、语音、系统通知。壳不收（没壳/老壳）时走这里，
   壳说在管却没收下（4 秒后水位没动，见上面）也回来走这里。 */
function pageAnnounce(d,nc,vis,viewing){
  /* 壳里的后台服务到底响过哪一条？老壳没有这个接口（hasBg=false），
     bg=-1 表示「这一代进程里还没响过」，不能当成接口不存在。 */
  let bg=-1, hasBg=false;
  try{ if(window.CB&&typeof CB.bgAnnounced==='function'){ hasBg=true; bg=Number(CB.bgAnnounced()); } }
  catch(e){ hasBg=false; bg=-1; }
  if(!isFinite(bg)) bg=-1;
  /* 只认 ≤ 本次 hello 的 seq：开发机重启后 id 归零，上一代进程的水位不算数 */
  const bgDid=!!(hasBg&&d.id&&bg>=0&&PUSH.seq>=0&&bg<=PUSH.seq&&d.id<=bg);
  /* 息屏/切走/刷新页面期间产生的提醒：壳的前台服务已经响过（刷新时是刚才那次
     已经响过），页面回来只补角标。以前这里是「回来把所有补发再响一遍」，
     用户听到的就是「亮屏两个提示音」「刷新又提示一次」。
     壳能确认「这条没响过」的，仍然让页面补响——别为了防双响把该响的也吞了。 */
  if(vis&&window.CB&&bgRemindOn()&&((d.ts||0)<VIS_AT-3)&&(!hasBg||bgDid)) return;
  /* 重连补发：短暂断网（≤3 分钟）里错过的提醒仍要出声/出通知——手机刚掉线、
     这一轮刚好在这时结束，人正等着这条。更旧的只补角标，避免一屏旧消息一起炸。
     （去重在上面：同一条不会因为 60 秒重连而反复响。） */
  const age=Date.now()/1000-(d.ts||0);
  /* 不在「刚发出」的窗口里 = 这条是补发的。壳里的后台服务已经把这条响过了
     （一声提示音 + 系统通知 + 念一遍），页面再响就是两遍。bg_announced_id
     只记「真响了」的，前台让给页面的不算。 */
  if(age>8&&bgDid) return;
  if(age>180) return;
  const tag=nbLabel(d.kind);
  if(nc.text) toast('🔔 '+tag+' · '+(((lastExt.find(x=>x.sid===d.sid)||{}).title)||d.title||''));
  const sound=d.sound||({done:'done',perm:'alert',input:'ping',ask:'ping',limit:'alert',stuck:'ping'})[d.kind]||'done';
  const voiceOn=nc.voice&&!!d.voice;
  /* 提醒音和语音播报不能同时开：提示音只有 0.2~0.5 秒，叠在一起念出来是糊的。
     先让提示音响完，再等一会儿念结论（1 秒；没开提示音就立刻念）。 */
  const speakAfter=function(){
    if(!voiceOn) return;
    setTimeout(function(){ speakKind(d.kind, d.about, d.say, d.saytext); }, nc.sound?1000:0);
  };
  /* 正在看这个会话：不弹系统通知（页面自己会显示状态），但声音照出——
     盯着屏幕等结果时没声，会以为提醒坏了 */
  if(viewing){
    if(nc.sound) beep(sound);
    speakAfter();
    return;
  }
  /* 壳里开着「息屏提醒」时，页面退到后台就别再自己响：那个前台服务会响。
     两边都响 = 用户说的「黑屏回来听到两个提示音」。没有壳（浏览器）或关了
     息屏提醒，还是页面自己负责。
     但壳切到后台后头 6 秒故意让给页面（它怕 JS 还没冻结会响两遍）——那几秒
     页面必须自己响，否则这两边都不响。留 7 秒，比壳的 6 秒多一点。 */
  const bgOwns=!vis&&!!window.CB&&bgRemindOn()&&(Date.now()/1000-(HID_AT||0))>7;
  /* 提醒音关了 → 系统通知也走静音渠道，否则关了还在响 */
  const notifSilent=vis||!nc.sound||bgOwns;
  if(nc.sound){
    try{ navigator.vibrate&&navigator.vibrate([20,50,20]); }catch(e){}
    if(vis) beep(sound);                     // 后台时交给系统通知的渠道音
  }
  if(vis) speakAfter();                      // 前台：页面自己播（先响音，再念结论）
  /* 系统通知交给壳；前台时页面已经出过声，通知走静音（别响两遍）。
     语音不交给通知（否则会念正文）：后台的念归 speakKind（有壳就交给壳）。 */
  if(nc.text&&window.CB&&(CB.notify2||CB.notify)){
    const title='🔔 '+tag+' · '+(d.title||'');
    const body=d.text||'';
    const url='/ext/'+d.sid;
    try{
      if(CB.notify2) CB.notify2(d.sid,title,body,url,sound,false,notifSilent);
      else CB.notify(d.sid,title,body,url);
    }catch(e){}
    if(voiceOn&&!vis&&!bgOwns) speakAfter();
  }else if(voiceOn&&!vis&&!bgOwns){
    speakAfter();                                 // 文字提醒关了、但后台要念
  }
}
function pushFeed(ev,data){
  let d=null; try{ d=JSON.parse(data); }catch(e){ return; }
  if(ev==='hello'){
    PUSH.gotHello=true; PUSH.fail=0; PUSH.why='';   /* 真连上了：旧掉线原因作废，设置页立刻显示已连接 */
    /* 掉线期结束（wasDown 只在 pushDown 里置位，50s 自续不算）：先对滚动游标，
       host 重启过（seq 倒退）就整窗重画作废本地缓存，别把旧快照硬接在断口上。 */
    var wasDown=PUSH.wasDown; PUSH.wasDown=false;
    if(wasDown){ try{ liveResync(); }catch(e){} }
    try{ vsPushRefresh(); }catch(e){}
    /* 开发机本次进程的提醒序号上限：用来判壳里的 bg_last_id 是不是上一代留下的 */
    PUSH.seq=(typeof d.seq==='number')?d.seq:-1;
    /* 开发机重启后序号归零：清去重表，否则新提醒会被当成旧 id 吃掉 */
    if(typeof d.seq==='number'&&d.seq<NOTIF_MAXID){ NOTIF_SEEN={}; NOTIF_MAXID=0; }
    if(typeof d.seq==='number'){
      var w=nwLoad();
      if(w.last>d.seq) nwSave({seq:d.seq,last:0,at:w.at});   /* 同上：存下来的水位也退回本代（保留时刻） */
    }
    return;
  }
  if(ev==='notify'){ onNotify(d); return; }
  if(ev==='hb'){ return; }   // 心跳：仅用于保活计数（esOpen 在监听器里自行刷 lastByte）
  if(ev==='sessions'){
    lastExt=d.sessions||lastExt;
    LIST_AT=Date.now(); listStaleAt=0;   /* 推送来了=在线，离线缓存横幅摘掉 */
    try{ sessSave(); }catch(e){}      // 推送就是「更新缓存」的主通道：来了就落盘
    curMeta=lastExt.find(x=>x.sid===SID)||curMeta;
    /* 输入框的读写状态也跟列表走：msg 事件只在转录变化时才来，host 那边探测
       一次抖了（桌面版被判成只读）之后页面就会一直关着输入框，直到重进会话。
       列表每轮都带 caps，caps 一变就会推一次，这里跟着重放一遍自愈。 */
    if(curMeta&&curMeta.caps&&SID===curMeta.sid) applyCaps(curMeta.caps,curMeta.why);
    refreshHeader(); renderList(); renderDetailLite();
    return;
  }
  if(ev==='msg'){ renderMsg(d); return; }
  if(ev==='live'){ renderLive(d); return; }
  if(ev==='hist'){ renderHist(d); return; }
  if(ev==='screen'){ renderScreen(d); return; }
  if(ev==='draft'){ setDraft(d); return; }
  if(ev==='state'){ setSt(d.state); if(d.gone) toast('会话已结束',true); return; }
  if(ev==='bye'){ pushReconnect(0); return; }   // 服务端 55min 主动退役：立即重连（bootstrap 即 catch-up）
  if(ev==='err'){ pushDown('err'); return; }
}
function esOpen(url,h){
  /* 原生 SSE（iOS Safari / 现代浏览器 / Chromium 6+ 全支持）：XHR 流式在 iOS 上
     readyState=3 不吐增量（实测看门狗连环掐），EventSource 是内核级实现没这问题。 */
  const es=new EventSource(url);
  let lastByte=Date.now(),dead=false;
  const NAMES=['hello','sessions','state','msg','draft','live','hist','screen','hb','bye','err','notify'];
  NAMES.forEach(function(ev){
    es.addEventListener(ev,function(e){ lastByte=Date.now(); if(h.onEvent)h.onEvent(ev,e.data||''); });
  });
  es.onopen=function(){ lastByte=Date.now(); };
  es.onerror=function(){                    // EventSource 自己会重连：必须关掉，重连权归 PUSH 状态机
    if(dead)return; dead=true;
    try{ es.close(); }catch(e){}
    if(h.onDead)h.onDead('es-error');
  };
  return { abort:function(){ if(dead)return; dead=true; try{es.close();}catch(e){} },
           lastByte:function(){ return lastByte; } };
}
function pushStart(){
  if(vsCfg().stream===false){ PUSH.st='off'; setSt(curState); return; }
  pushStop(true);
  /* 后台不建流（见 awayNow）。pushReconnect 的定时器可能在息屏后到点，
     在这里挡住；回前台 visibilitychange 会 pushReconnect(0) 重新拉起。 */
  if(awayNow()){ PUSH.st='off'; PUSH.fail=0; setSt(curState); return; }
  PUSH.st='on'; PUSH.gotHello=false; PUSH.helloAt=Date.now(); PUSH.sid=SID;
  const url=xb('/api/ext_push'+(SID?'/'+SID:''));
  const open=(directOn()&&DIRECT.mode==='dc')?dcSSE:(window.EventSource?esOpen:sseOpen);   // P2P 走 DC 帧；有原生 SSE 用原生，老内核才走 XHR 流式
  PUSH.tr=(directOn()&&DIRECT.mode==='dc')?'dc':(window.EventSource?'sse':'xhr');
  try{ if(window.CB&&CB.log) CB.log('push[on] tr='+PUSH.tr+' dir='+DIRECT.st+'/'+(DIRECT.mode||'-')+' fail='+PUSH.fail); }catch(e){}
  vsPushRefresh();
  const gz='';   /* 不再要 gz：aiohttp 压缩 write() 不 flush，SSE 会变哑流（推送总断的真凶） */
  PUSH.x=open(url+gz,{
    onEvent:pushFeed,
    onDead:function(){ pushDown('stream-dead'); }
  });
  // 50s 主动自续：蜂窝运营商代理 ~70s 静默截断长响应（iOS 实测 69.9s 被掐），
  // 赶在它前面自己重开；bootstrap 补齐+渲染幂等，无感。自续走 pushReconnect，不算失败。
  if(PUSH.recycle){ clearTimeout(PUSH.recycle); }
  PUSH.recycle=setTimeout(function(){ if(PUSH.st==='on') pushReconnect(0); },50000);
  setSt(curState);
}
function pushStop(clearTimer){
  if(PUSH.x){ try{ PUSH.x.abort(); }catch(e){} PUSH.x=null; }
  if(PUSH.recycle){ clearTimeout(PUSH.recycle); PUSH.recycle=null; }
  if(clearTimer&&PUSH.timer){ clearTimeout(PUSH.timer); PUSH.timer=null; }
}
function pushSleep(){   // 页面进后台：静默全停（推流+轮询都歇，省流量省电；不算失败）
  pushStop(true);
  PUSH.st='off'; PUSH.fail=0;
  setSt(curState);
}
function pushReconnect(delay){
  pushStop(true);
  PUSH.timer=setTimeout(pushStart,delay||0);
}
function pushDown(why){
  if(PUSH.st!=='on') return;
  PUSH.st='off'; PUSH.fail++; PUSH.why=why||''; PUSH.at=Date.now();
  PUSH.wasDown=true;   /* 给 hello 看：这次重连前真掉过线，要重对滚动游标 */
  /* 诊断：掉线的「哪条路 + 多久没字节 + 直连当时什么状态」一行写进 cbvol.log。
     iOS 上 P2P 掉回轮询一直只有一句「推送中断」，看不出是 DC 僵死、网络切换还是
     页面被系统挂起，这一行就是为了下次复现能直接定位。 */
  try{ if(window.CB&&CB.log) CB.log('push[down] '+PUSH.why+' tr='+PUSH.tr+' fail='+PUSH.fail
    +' hello='+(PUSH.gotHello?1:0)
    +' silent='+(PUSH.x?Math.round((Date.now()-PUSH.x.lastByte())/1000):-1)+'s'
    +' dir='+DIRECT.st+'/'+(DIRECT.mode||'-')+' vis='+(hiddenNow()?0:1)); }catch(e){}
  vsPushRefresh();
  /* P2P 通道连推流都自举不起来（连续两次没等到 hello）：别再在这条死通道上耗，
     直接判直连失效回中继——否则设置页就是「连接中 ↔ 轮询中」来回跳。 */
  if(PUSH.tr==='dc'&&!PUSH.gotHello&&PUSH.fail>=2){
    try{ directDown('dc-dead'); }catch(e){}
  }
  pushStop(true); setSt(curState);
  // 立即手动补一轮全量（渲染器全部幂等，断流期不留内容洞）
  poll(); liveflowPoll(); screenPoll();
  if(pageIdx!==1) loadListData();
  /* 前 2 次失败按瞬断处理（host 重启 / WiFi 抖动 / 蜂窝代理掐流）：
     静默秒级重连，不弹 toast 不退避——真断流（连不上 hello）才升级到
     提示 + 15s/45s/300s 长退避。fail 收到 hello 会清零（1152 行）。 */
  if(PUSH.fail<=2){
    if(vsCfg().stream!==false) pushReconnect(500+400*PUSH.fail);
    return;
  }
  if(PUSH.fail===3) toast('推送中断，已回轮询');
  /* ≥6 次说明这台机器长连接就是活不下来（ROM 掐、代理截）：别每 5 分钟白试，
     改成半小时一回。回前台 / 换网络会各自立刻再试，日常使用感觉不到这个间隔。 */
  const wait=(PUSH.fail>=6?1800000:(PUSH.fail===3?15000:45000))+Math.floor(Math.random()*10000);
  PUSH.timer=setTimeout(function(){ if(vsCfg().stream!==false) pushStart(); },wait);
}
/* 设置页已经没有「实时推送」开关了（能推就推、推不了自动回轮询）。这两个函数留着：
   某台机器长连接被 ROM 反复掐时，控制台敲 pushSet(false) 可强制只用轮询，不用发版。
   cb_cfg.stream=false 这个键照旧认，老配置里关过的手机不会自己打开。 */
function pushToggle(){ var on=vsCfg().stream!==false; pushSet(on?false:true); }
function pushSet(v){
  track('push_set');
  const c=vsCfg(); c.stream=v; vsSave(c);
  if(v===false){
    pushStop(true); PUSH.st='off'; setSt(curState);
    poll(); liveflowPoll(); screenPoll();
    if(pageIdx!==1) loadListData();
  }else{
    PUSH.fail=0; pushStart();
  }
  renderVoice(c); toast('已保存');
}
setInterval(function(){          // 看门狗：bootstrap 卡死 / 静默断流
  if(setPageOpen()){ try{ vsPushRefresh(); vsDirectRefresh(); }catch(e){} }
  if(PUSH.st!=='on') return;
  if(!PUSH.gotHello&&Date.now()-PUSH.helloAt>22000){ pushDown('bootstrap-timeout'); return; }   // 冷启动 discover 子进程最坏 ~15s
  if(PUSH.x&&Date.now()-PUSH.x.lastByte()>30000) pushDown('stall');
},5000);
function onSleep(){   // 进后台：静默全停（推流+轮询都歇，省流量省电；不算失败）
  HID_AT=Date.now()/1000;   /* 记住睡着的时间：醒来的补发要判断是不是睡着期间响过的 */
  pushSleep();
}
/* 回前台：按流况分流 */
function onWake(){
  /* 先记下「人回来了」：这一轮补发里，睡之前产生的提醒壳已经响过，别再现声。
     安卓黑屏时 document.hidden 不一定翻转、visibilitychange 也不一定有，
     所以不能只靠那个事件（以前靠它，亮屏时就补响了一遍）。 */
  VIS_AT=Date.now()/1000;
  if(PUSH.st==='on'&&PUSH.x){   // 没睡过的（多窗/事件竞态）：流新鲜就留，陈旧判死
    if(Date.now()-PUSH.x.lastByte()<=15000){ poll(); return; }
    pushDown('wake-stall'); return;
  }
  /* 回前台要把显示设置重新下发一次：
     亮度是「App 设的全局背光 + 一层压暗遮罩」，而黑屏/锁屏后系统会把背光还原成
     自己的值，我们设的那份就丢了；遮罩也可能还挂着。以前只有进设置页才 applyBright
     → 表现就是「唤醒后聊天/列表/终端都偏暗，一切到设置页就正常」。
     dispStartup 是幂等的，且没存过亮度时它会跟随系统（不硬抢用户的背光）。 */
  try{ dispStartup(); }catch(e){}
  if(DIRECT.mode==='dc') dcKeepAuth();   // 睡了一觉票据可能过期：先续，别让第一帧就撞 403
  if(vsCfg().stream!==false) pushReconnect(0);   // 睡过的：立即重开（bootstrap 即 catch-up）
  directTry();   // 回前台顺带重探直连（内部有冷却栅栏，不重复打洞）
  poll(); liveflowPoll(); screenPoll();
  if(pageIdx!==1) loadListData(true);   // 补一轮全量，无洞（15 秒内刚拿过就不重复）
}
document.addEventListener('visibilitychange',function(){   // 后台全睡，回前台按流况分流
  if(hiddenNow()){ onSleep(); return; }
  onWake();
});
/* 壳报前后台：iOS 的 WKWebView 放进后台时 document.hidden 常常不变（visibilitychange
   不触发），于是页面以为还在前台 → 息屏一整夜也在不停轮询/重连推流 →
   按「有请求且屏幕亮着才算使用」的口径，这段时间本不该计费，却把时长烧掉了
   （实测一天能烧掉 900 分钟）。壳把真实的前后台状态告诉页面，这里统一收口。 */
window.__cbVisible=function(v){
  var b=!!v;
  /* DBG：壳报前后台＝「自动听写 2 秒被取消」的头号嫌疑（MainActivity.onPause 会收麦
     并发 cancel）。留一行进开发机日志，跟 Nend 对时间就知道是不是它。查完删。 */
  try{ window.__cbDbg && window.__cbDbg('Nvis '+(b?1:0)+' '+(window.__cbAutoInfo?window.__cbAutoInfo():'-')); }catch(e){}
  SHELLBG=!b;
  if(b===CBVIS) return;
  CBVIS=b;
  try{ if(window.CB&&CB.log) CB.log('可见性='+(b?'前台':'后台')); }catch(e){}
  if(!b){ onSleep(); return; }
  onWake();
};
/* 壳心跳：壳每 2.5~3s 推一次（window.__cbAlive）。
   后台通知不一定送得进来（iOS 把 App 挂起时 evaluateJavaScript 常被丢），
   所以再上一道兜底：心跳一停超过 20s，就当自己进了后台 → 页面自己睡。
   心跳恢复 = 回前台（但壳明确说了后台时，心跳说了不算）。 */
window.__cbAlive=function(){
  var now=Date.now(), gap=now-ALIVE;
  ALIVE=now;
  if(!HASBEAT){ HASBEAT=true; return; }        /* 第一拍只认账，不当「恢复」 */
  if(SHELLBG||CBVIS||document.hidden) return;  /* 壳说了后台 / 页面已醒着：不用管 */
  if(gap<20000) return;                        /* 刚断过又来一拍：抖动，不醒 */
  CBVIS=true; onWake();
};
setInterval(function(){
  if(!HASBEAT) return;                         /* 不在壳里（普通浏览器）：没有心跳这回事 */
  if(Date.now()-ALIVE<=20000) return;          /* 心跳还在 */
  if(SHELLBG||document.hidden||!CBVIS) return;
  CBVIS=false; onSleep();                      /* 心跳断了 = 当后台，睡 */
},5000);
/* ── 断网时别让人对着一堆点不动的按钮 ──────────────────────────────
   壳里有自带的「网络」页（安卓/iOS 的 CB.offline）：连续的请求都发不出去
   （status 0）或壳报断网超过 6 秒，就请壳切过去，而不是让页面留在这里
   要么静默卡死、要么撞上系统那张「网页无法打开」。
   页面自己绝不跳，只发请求；打字中/说话中/后台/正在更新开发机时不切。 */
var NETDEAD_N=0, NETDEAD_T=0, NETDEAD_ASK=0, NETOFF_TIMER=0;
function netOffCancel(){
  if(NETOFF_TIMER){ clearTimeout(NETOFF_TIMER); NETOFF_TIMER=0; }
}
function hostUpdating(){
  try{ return !!localStorage.getItem('cb_hs_pending'); }catch(e){ return false; }
}
function netAskOffline(why){
  if(!(window.CB&&typeof CB.offline==='function')) return;
  if(hiddenNow()||SHELLBG) return;
  if(voiceBusyNow()) return;      /* 正在说话：抽走页面等于把人嘴堵上 */
  if(hasDraft()) return;          /* 输入框里还有没发的字：草稿会留，但没必要白白打断 */
  if(hostUpdating()) return;      /* 更新开发机中途本来就会失败几次，等它自己收尾 */
  var now=Date.now();
  if(NETDEAD_ASK&&now-NETDEAD_ASK<30000) return;   /* 别和壳的重试形成互相抽打 */
  NETDEAD_ASK=now;
  try{ if(window.CB&&CB.log) CB.log('请壳切网络页：'+why); }catch(e){}
  try{ CB.offline(); }catch(e){}
}
function netFailTick(status,direct){
  netOffCancel();                       /* 有一个请求完整回来（不管几几几）：网络通了 */
  if(direct){                           /* 直连路径：发不出去=直连的债，不是没网 */
    if(status===0){ try{ directDown('netdead',true); }catch(e){} }   /* 静默拆直连回中继 */
    return;                             /* 绝不计入 NETDEAD_N：中继还活着就不该锁进离线页 */
  }
  if(status>0){ NETDEAD_N=0; return; }
  if(!(window.CB&&typeof CB.offline==='function')) return;
  var now=Date.now();
  if(now-NETDEAD_T>20000){ NETDEAD_N=0; NETDEAD_T=now; }
  NETDEAD_N++;
  if(NETDEAD_N>=6) netAskOffline('连续 '+NETDEAD_N+' 次请求发不出去');
}
function netOffSoon(){                  /* 壳说断网：给它 6 秒（切 Wi-Fi 的瞬断不算） */
  netOffCancel();
  NETOFF_TIMER=setTimeout(function(){
    NETOFF_TIMER=0;
    if(NETTR!=='off') return;
    netAskOffline('壳报断网，6 秒没有恢复');
  },6000);
}
/* 换网络（WiFi↔流量、飞行模式回来）：链路换了就是一次新机会，失败计数清零后立刻再试。
   「永远不放弃」的重试收到半小时一次之后，靠这个和回前台补上快速恢复的路。 */
window.addEventListener('online',function(){
  netOffCancel();                    /* 浏览器层面回网了：别再请壳切网络页 */
  if(PUSH.st==='on'||vsCfg().stream===false) return;
  PUSH.fail=0; pushReconnect(2000);
});
/* ── 壳的网络广播（换网/断网时 pushNetState 推过来）──
   以前页面不接这个事件：换了网络后旧直连（LAN IP/DT 票据/DataChannel）全部作废，
   但 DIRECT.st 还是 'on'，推流继续往死路上开 → hello 收不到 → 看门狗掐掉 →
   「推送中断，已回轮询」+ 15s/45s/300s 退避，期间还有 60s 直连冷却挡着重探，
   表现就是换网后推送/轮询反复横跳好几分钟。现在收到广播：静默拆旧直连（不弹
   「直连中断」）、失败计数清零、推流立刻在新网上重开。gen=拓扑代数（wifi↔流量、
   断网恢复、换热点都变），tr=制式（兜底给不带 gen 的老壳）。 */
var NETTR='', NETGEN=-1, NETTT=null, NETPEND='';
window.__cbNetState=function(s){
  let d=null; try{ d=(typeof s==='string')?JSON.parse(s):s; }catch(e){ return; }
  if(!d||typeof d!=='object') return;
  /* online 但认不出接口（VPN / NECP / USB 网卡）也是「有网」：以前归成 off，
     页面当成断网 → 停推流、六秒后请壳切网络页，开了代理就整页「手机没网了」。 */
  const tr=d.online===false?'off':(d.wifi?'wifi':(d.mobileConn?'mobile':'other'));
  const gen=(typeof d.gen==='number')?d.gen:-1;
  if(tr===NETTR&&gen===NETGEN) return;
  const first=(NETTR===''&&NETGEN===-1);
  /* 换网要留痕：直连被拆掉却不记一行，事后完全看不出通道为什么重建。 */
  try{ if(window.CB&&CB.log) CB.log('换网 tr='+NETTR+'→'+tr+' gen='+NETGEN+'→'+gen
    +(first?'（首次，只记录）':'')); }catch(e){}
  try{ if(window.__cbDbg) window.__cbDbg('Nnet '+NETTR+'>'+tr+' g'+NETGEN+'>'+gen); }catch(e){}
  /* ⚠️ 只按「上网方式」决策，不按 gen：gen 变了而 wifi/蜂窝没变（同一张卡、
     同一个 WiFi 上 NWPath 的路径细节抖一下，或者代理/VPN 让接口类型在
     cell↔other 之间来回报），不代表直连作废。以前 gen 一变就拆直连 + 重建推流，
     于是刚打通的 P2P 被周期性拆掉、8.5 秒后又重打——用户看到的就是
     「一会儿中继转发、一会儿 P2P 直连」不停切，每切一次推流重建一次，页面发卡。 */
  const genOnly=(tr===NETTR);
  if(gen>=0) NETGEN=gen;
  if(first){ NETTR=tr; if(tr==='off') netOffSoon(); return; }   // 首次只记录
  if(genOnly) return;
  NETPEND=tr;
  if(NETTT){ clearTimeout(NETTT); NETTT=null; }
  if(tr==='off'){ netApply(tr); return; }        // 断网不用等（壳自己还有 6 秒缓冲）
  /* 换网瞬间系统会连报好几拍（cell→other→cell），每一拍都拆一遍直连、重建推流
     就是白折腾。等 2.5 秒稳定下来，仍然跟当前不同才真处理。 */
  NETTT=setTimeout(function(){ NETTT=null; if(NETPEND) netApply(NETPEND); },2500);
};
function netApply(tr){
  NETPEND='';
  if(NETTT){ clearTimeout(NETTT); NETTT=null; }
  if(!tr||tr===NETTR) return;
  NETTR=tr;
  if(tr==='off'){     // 断网：静默停推（不算失败）；回网由本回调或 online 事件拉起
    pushStop(true); PUSH.st='off'; PUSH.fail=0; setSt(curState);
    netOffSoon();     // 6 秒还不回来就请壳切到「网络」页
    return;
  }
  netOffCancel();
  if(DIRECT.st==='on'){   // 静默拆旧直连：不弹「直连中断」；冷却只给 8s（换网了要尽快重探）
    try{ if(window.__cbDbg) window.__cbDbg('Nnet-tear '+tr); }catch(e){}
    DIRECT.st='off'; DIRECT.mode=''; DIRECT.dt=''; DIRECT.onAt=0;
    DIRECT.cool=Date.now()+8000;
    if(DIRECT.authT){ clearInterval(DIRECT.authT); DIRECT.authT=null; }
    try{ if(DIRECT.dc){DIRECT.dc.close();} }catch(e){}
    try{ if(DIRECT.pc){DIRECT.pc.close();} }catch(e){}
    DIRECT.pc=null; DIRECT.dc=null;
    vsDirectRefresh();
    setTimeout(directTry,8500);   // 新网稳定后重探直连（directTry 自带冷却/占用栅栏）
  }
  PUSH.fail=0;   // 换网不是页面的错：清掉退避计数，推流立刻在新网上重开
  if(vsCfg().stream!==false) pushReconnect(0);
  poll(); liveflowPoll(); screenPoll();
}
async function sendMsg(){
  try{ if(window.__cbAutoEnd) window.__cbAutoEnd(); }catch(e){}   /* 手动发送 → 自动听写收摊 */
  const ta=document.getElementById('txt'), btn=document.getElementById('send');
  const text=ta.value.trim(); if(!text) return;
  /* 没有当前会话（上次的会话已不在/页面还没选中）：以前照发，host 只回一条 404，
     用户看到的是「发出去了但什么都没有」。说清楚，字留在框里。 */
  if(!SID){ toast('当前没有会话，先从列表选一个',true); return; }
  track('send_msg');
  const asrSnap=window.__cbAsrSnap; window.__cbAsrSnap=null;
  btn.disabled=true;
  try{
    /* away:true = 人亲手点的发送，不因页面误判后台而被吞（发消息是最基本的功能）
       cid：直连超时后回落中继会再送一份，host 认出同一个 cid 只发一次——不重复上屏 */
    const r=await xj('/api/ext_send',{method:'POST',away:true,body:{sid:SID,text:text,
      cid:'s-'+Date.now().toString(36)+'-'+Math.random().toString(36).slice(2,8)}});
    const d=r.json;
    if(d&&d.ok){
      /* note：桌面版会话被 App 占着写时，host 会先退出电脑上的 Codex 再写原会话——
         人得知道电脑上的 App 被关了，不然会以为是自己弄没的 */
      /* 先把自己这条画上去：桌面版走队列时，App 要过一会才把它写进 rollout，
         手机端等推送就慢半拍（电脑上早就看见了）。本地先垫一条，真消息到了
         推送会把这段整体替换掉（inc 截断/全量重画），不会重复。 */
      feedMsgs.push({role:'user', text:text, pend:1});
      if(view==='chat'){ const wasB=atBottom(); renderFeed(); if(wasB) toBottom(); }
      ta.value=''; fitTxt(); toast(d.note||'已送入电脑终端', false, d.note?5000:1600);
      try{ SEND_AT[SID]=Date.now(); sendAtSave(); }catch(e){}   /* 记下发言时刻：随后产生的问句不弹浮条 */
      try{ nhMarkSent(); }catch(e){}   /* 从提醒历史「确认」过来的：标已确认 */
      try{ pendSent(); }catch(e){}     /* 会话页浮条：你回话了就算处理过 */
      try{ draftSave(true); }catch(e){}   // 发出去了：这份草稿作废
      /* 真送到电脑了才响：听声音就知道发没发出去。静音/免打扰时不响（静音 =
         App 里所有自动声音都停，设置页的「试听」除外——那是用户主动点的）。 */
      try{ if(!muteOn()&&!dndActive()) beep('sent'); }catch(e){}
      learnFix(asrSnap,text); setTimeout(poll,600);
    }
    else toast('失败：'+(d&&d.error||JSON.stringify(r).slice(0,80)),true);
  }catch(e){ toast('网络错误:'+(e&&e.message||e),true); }
  btn.disabled=false;
}
/* ── 语音识别「越用越懂项目」的取样 ──
   语音结果填进输入框后，用户往往会手工改掉几个词再发。发送时把「识别原文」
   和「真正发出去的文本」做词级对比，只把「一段换成另一段」的差异报给开发机，
   由它转给中继学习。纯润色/整段重写在中继侧还会再筛，这里不追求精确。 */
function diffTerms(a,b){
  function words(s){ return String(s||'').split(/\s+/).filter(function(x){return x;}); }
  var A=words(a), B=words(b), i=0, j=0;
  while(i<A.length&&i<B.length&&A[i]===B[i]) i++;
  while(j<A.length-i&&j<B.length-i&&A[A.length-1-j]===B[B.length-1-j]) j++;
  var ax=A.slice(i,A.length-j), bx=B.slice(i,B.length-j);
  if(!ax.length||!bx.length) return [];                 /* 只追加/只删除：不当改错 */
  if(ax.length===1&&bx.length===1) return [[ax[0],bx[0]]];
  if(ax.length===bx.length){
    var out=[];
    for(var k=0;k<ax.length;k++) if(ax[k]!==bx[k]) out.push([ax[k],bx[k]]);
    return out;
  }
  var sa=ax.join(' '), sb=bx.join(' ');
  if(sa.length<=60&&sb.length<=60) return [[sa,sb]];
  return [];
}
function learnFix(snap,finalText){
  if(!snap||!finalText) return;
  if(Date.now()-(snap.at||0)>600000) return;            /* 十分钟前的识别结果不管 */
  var pairs=diffTerms(snap.full,finalText);
  /* 临时探针：把「有没有改稿、改出几对、报给哪个项目」落进开发机日志。
     查完「错误纠正为什么没数据」就删。 */
  try{ if(window.__cbDbg) window.__cbDbg('Nlearn '+pairs.length+' '+
        (snap.proj||'-')+' '+String(snap.cwd||'').slice(-16)); }catch(e){}
  if(!pairs.length) return;
  try{
    xj('/api/ptt_fix',{method:'POST',body:{cwd:snap.cwd||'',proj:snap.proj||'',pairs:pairs}})
      .catch(function(e){ try{ if(window.__cbDbg) window.__cbDbg('Nlerr '+(e&&e.message||e)); }catch(x){} });
  }catch(e){}
}
/* 快捷键条：**按下只亮、抬手才发**。以前按 touchstart 立刻发键，可是往上拉手/往回拉
   收起手势都是从这排键上起步的 —— 手指在 ↑/↓ 上一蹭就把方向键发进终端（Claude Code 里
   就是挪光标、翻选项），正是「往回拉的时候误触了上下箭头」。
   抬手发键仍然绕开「document 有触摸监听 → 点按等 300ms 判定」的老 WebView 延迟。

   判定的口径（2026-11-09 改）：以前只比竖向位移、阈值 8px，大拇指自然点一下就会漂
   十几像素 → 大批点按被当成滑动悄悄丢掉，表现就是「按了快捷按键经常没反应」。
   现在两个轴都看，点按窗口放到 26px；只有明确的大幅竖滑（>44px 且明显竖着走）才
   让给收起/展开手势。中间地带两边都不认，宁可不动作也不误发。 */
(function(){
  const ks=document.getElementById('keys');
  const TAP=26, SWIPE=44;
  let kb=null, kx=0, ky=0, kt=0;
  function kbEnd(){
    const b=kb, x=kx, y=ky, t=kt; kb=null; kx=ky=kt=0;
    if(b) setTimeout(function(){ b.classList.remove('on'); },120);
    return [b,x,y,t];
  }
  ks.addEventListener('touchstart',function(e){
    const b=e.target.closest('button'); if(!b||e.touches.length!==1) return;
    /* 只接管带 data-k 的按键。文件/图片/测试/发布是 onclick 普通按钮：
       以前无条件 preventDefault，触屏上连合成 click 一起吃掉 → 点了毫无反应
       （桌面没有 touch 事件，所以一直看不出问题）。*/
    if(!b.hasAttribute('data-k')) return;
    e.preventDefault();                     // 压掉后续 click：触屏不会双发
    kb=b; kx=e.touches[0].clientX; ky=e.touches[0].clientY; kt=Date.now();
    b.classList.add('on');                  // 按下即时反馈（发不发键要等抬手）
  },{passive:false});
  ks.addEventListener('touchcancel',function(){ kbEnd(); },{passive:true});
  ks.addEventListener('touchend',function(e){
    const r=kbEnd(); const b=r[0], x=r[1], y=r[2], t0=r[3];
    if(!b||!e.changedTouches.length) return;
    if(!b.hasAttribute('data-k')) return;   // 普通按钮：别拦它的 click
    e.preventDefault();
    const dx=Math.abs(e.changedTouches[0].clientX-x);
    const dy=Math.abs(e.changedTouches[0].clientY-y);
    if(dy>TAP||dx>TAP) return;              // 手指真的移开了才算手势，别当按键
    /* 按得久一点也该发：以前超过 1.5 秒就丢掉，犹豫一下/指头没抬干净就是
       「按了没反应」。位置没怎么动（TAP 以内）就是一次点按——拖动展开/收起
       那条手势要移动 ≥44px，不会跟它撞。只在按了 6 秒以上才当误触。 */
    if(t0&&Date.now()-t0>6000) return;
    const k=b.getAttribute('data-k'); if(k) key(k);
  },{passive:false});
  /* 上沿拉手：整条带子就是按钮——上拉展开、下滑收起，原地按一下切换。
     手指落下整条泛蓝（.on），松手才动作；带子很薄且离 ↑/↓ 有 16px 空档。 */
  const g=document.getElementById('kgrab');
  let gy0=null;
  function gOn(){ g.classList.add('on'); }
  function gOff(){ g.classList.remove('on'); }
  g.addEventListener('touchstart',function(e){
    kbDismiss(); gy0=e.touches[0].clientY; gOn();
  },{passive:true});
  g.addEventListener('touchcancel',function(){ gy0=null; gOff(); },{passive:true});
  g.addEventListener('touchend',function(e){
    gOff();
    if(gy0===null||!e.changedTouches.length){ gy0=null; return; }
    const dy=e.changedTouches[0].clientY-gy0; gy0=null;
    e.preventDefault();
    window.__kGrabTouch=Date.now();   // 触摸刚处理过，别让随后的 click 又切一次
    if(dy<-12) kMoreToggle(true);
    else if(dy>12) kMoreToggle(false);
    else kMoreToggle();
  },{passive:false});
  /* 按键区上做纵向手势同样可展开/收起（纯点按不受影响）。
     手指从某个按键上起手时门槛更高（SWIPE）：否则点一下的几像素漂移就把整条
     快捷键条收起来/展开，看着就是「按键没反应、条子还乱动」。 */
  let y0=null, x0=0, onBtn=0;
  ks.addEventListener('touchstart',function(e){
    if(e.touches.length!==1||e.target.closest('#kgrab')){ y0=null; return; }
    y0=e.touches[0].clientY; x0=e.touches[0].clientX; onBtn=e.target.closest('button')?1:0;
  },{passive:true});
  ks.addEventListener('touchend',function(e){
    if(y0===null||!e.changedTouches.length){ y0=null; return; }
    const dy=e.changedTouches[0].clientY-y0, dx=Math.abs(e.changedTouches[0].clientX-x0);
    y0=null; const fromBtn=onBtn; onBtn=0;
    const min=fromBtn?SWIPE:18;
    if(Math.abs(dy)<min||Math.abs(dy)<dx*1.2) return;
    if(dy<0) kMoreToggle(true);
    else kMoreToggle(false);
  },{passive:true});
})();
/* ── 命令终端页的终端卡片：手开的 zsh 和 ▶ 识别命令跑出来的运行终端统一成卡片列表，
   点卡片进终端（iframe /s/<sid>，xterm + 输入条），✕ 关掉（杀进程 + 移出列表）。 ── */
var TERM={sid:'',view:false,terms:[]},TERM_SIG='';
/* 终端页（命令终端）只在真有终端时才存在：没有终端在跑时，整页连同导航点一起收起，
   点/滑都进不去；有一个终端才出现。TERM.terms 每 4-12 秒对一次，远程新开也会跟上。 */
function termPageOK(){ return termAllowed() && TERM.terms.length>0; }
function termDotSync(){
  /* 列表页「终端」入口：一直显示（点进去是新开/列表），数量跟着终端走 */
  var n=document.getElementById('termEntryN');
  if(n) n.textContent=TERM.terms.length?('共 '+TERM.terms.length+' 个'):'暂无';
  try{ fitHeader(); }catch(e){}
}
function termPreviewHtml(t){
  return (t.tail||[]).map(escHtml).join('<br>');
}
function termLoad(){
  var box=document.getElementById('termList');
  if(!termAllowed()){
    TERM_SIG=''; TERM.terms=[]; termDotSync();
    if(box&&!TERM.view) box.innerHTML='<div class="card" style="border-style:dashed;color:#787878;font-size:12.5px">命令终端 · 新建会话和终端是 Pro / Max 功能。'
      +(PLANS_OFF?'':'<br><button class="gray mini" style="margin-top:8px" onclick="vsPay()">查看套餐</button>')+'</div>';
    return;
  }
  xj('/api/terms').then(function(r){
    var d=r.json||{}; if(!r.ok||!d.ok) return;
    var arr=d.terms||[];
    /* 网页终端（🌐：电脑上开着的 shell 会话，主列表不再列）也归这一页。
       统一成终端卡片的字段；服务重启后留下的占位条目标「已失效」，点按可重新拉起。 */
    (lastWeb||[]).forEach(function(t){
      if(t.kind!=='web') return;
      arr.push({sid:t.sid,name:t.name,cwd:t.cwd,alive:!!t.alive&&!t.ghost,
                ghost:!!t.ghost,started:t.started||0,tail:null});
    });
    arr.sort(function(a,b){return (b.started||0)-(a.started||0);});
    if(TERM.view){
      /* 正在看终端：它已经不在服务端列表里（host 重启/被关掉）就退出浮层回列表，
         否则就是「终端页上既看不到窗口、又看不到列表」的死状态。
         电脑托管的会话本来就不在 /api/terms：pin 住时按会话列表认。 */
      var still=arr.some(function(t){return t.sid===TERM.sid;});
      if(!still&&TERM.pin) still=(lastWeb||[]).some(function(t){return t.sid===TERM.sid;})||hostSessAlive(TERM.sid);
      if(!still){
        termBack(); toast('终端已结束',true);
        return;
      }
      if(arr.some(function(t){return t.sid===TERM.sid;})) TERM.terms=arr;
      return;
    }
    TERM.terms=arr;
    termDotSync();
    var box=document.getElementById('termList');
    document.getElementById('termHint').textContent=TERM.terms.length?('共 '+TERM.terms.length+' 个'):'';
    if(!TERM.terms.length){
      /* 没有终端在跑就什么都不显示：终端页本身也收起（导航点已隐藏），
         空页面上那块「还没有终端…」既占地方又和上面的「＋ 新开」重复。 */
      TERM_SIG=''; box.innerHTML='';
      return;
    }
    /* 终端集合/状态没变就只刷新预览文本：每次全量重画会吃掉正好落下的点击 */
    var sig=TERM.terms.map(function(t){return t.sid+'\u0001'+(t.name||'')+'\u0001'+(t.ghost?'g':(t.alive?1:0));}).join('\u0002');
    if(sig!==TERM_SIG){
      TERM_SIG=sig;
      box.innerHTML=TERM.terms.map(function(t){
        return '<div class="card tcard" data-sid="'+t.sid+'" onclick="termTap(\''+t.sid+'\')">'
          +'<i class="dot'+(t.alive?'':' off')+'"></i>'
          +'<div class="grow">'
          +'<div class="tname">'+escHtml(t.name||t.sid.slice(0,6))+'</div>'
          +'<div class="tpath">'+escHtml(t.cwd||'')+' · '+(t.ghost?'已失效 · 点按重新拉起':(t.alive?'运行中':'已退出'))+'</div>'
          +'<div class="tprev">'+termPreviewHtml(t)+'</div>'
          +'</div>'
          +'<button class="gray mini" onclick="event.stopPropagation();termClose(\''+t.sid+'\')">✕</button>'
          +'</div>';
      }).join('');
    }else{
      TERM.terms.forEach(function(t){
        var el=box.querySelector('[data-sid="'+t.sid+'"] .tprev');
        if(el){ var h=termPreviewHtml(t); if(el.innerHTML!==h) el.innerHTML=h; }
      });
    }
  }).catch(function(){});
}
/* 停在终端页时轻量轮询，卡片里的内容跟着终端走；
   正在看终端时只确认它还在——host 重启后命令终端是内存态，浮层留着就是一块死白屏 */
setInterval(function(){
  if(!termPageOpen()||hiddenNow()) return;
  if(!TERM.view){ termLoad(); return; }
  if(TERM.pin) return;   // 电脑会话不在 /api/terms：存活由 pin 的浮层自己按会话列表判断
  xj('/api/terms').then(function(r){
    var d=r.json||{}; if(!r.ok||!d.ok) return;
    var arr=d.terms||[];
    if(!arr.some(function(t){return t.sid===TERM.sid;})){
      termBack(); toast('终端已结束',true);
    }
  }).catch(function(){});
},4000);
/* 不在终端页时也隔一阵对一次：终端可能在电脑上被新开/关掉，导航点要跟着出现/消失 */
setInterval(function(){
  if(!termPageOpen()&&!TERM.view&&!hiddenNow()) termLoad();
},12000);
/* 终端面板最大高度 = 顶栏以下的整屏；高度按可用高度的比例记住 */
function termFocus(){
  try{ var w=document.getElementById('termFrame').contentWindow; if(w&&w.cbFocus) w.cbFocus(); }catch(e){}
}
/* 键盘占位：壳整页上推时用 --ime；浏览器/部分 ROM 只缩 visualViewport，用两者较大值。
   浮层底边只跟 visualViewport 的差异（壳已上推时视口底边就是键盘上沿，再加会悬空）。 */
function termShellIme(){
  try{ return parseFloat(getComputedStyle(document.documentElement).getPropertyValue('--ime'))||0; }catch(e){ return 0; }
}
function termVvInset(){
  var vv=window.visualViewport; if(!vv) return 0;
  return Math.max(0,(window.innerHeight||0)-(vv.height+(vv.offsetTop||0)));
}
function termInset(){
  var v=termVvInset(), s=termShellIme();
  return s>0?Math.max(v,s):v;
}
function termSyncIme(){
  var s=termShellIme(), v=termVvInset();
  try{ document.documentElement.style.setProperty('--kb',(s>0?Math.max(0,v-s):v)+'px'); }catch(e){}
  if(TERM.view) termApplyH(TERM.pref);
}
function termMaxH(){
  var hd=document.querySelector('header');
  var hh=hd?hd.getBoundingClientRect().height:49;
  return Math.max(200,(window.innerHeight||600)-hh-8-termInset());
}
/* TERM.pref = 用户拖出来的偏好高度；键盘弹起时按可用高度压缩，收起后自动恢复 */
function termApplyH(h){
  var box=document.getElementById('termViewBox'); if(!box) return;
  if(h) TERM.pref=h;
  var maxH=termMaxH(), minH=Math.min(maxH,220);
  var v=Math.max(minH,Math.min(maxH,TERM.pref||0));
  TERM.h=v; box.style.height=v+'px'; termFitFrame();
}
function termView(sid,pin){
  TERM.sid=sid; TERM.view=true; TERM.pin=!!pin;
  if(!termPageOpen()) subOn('termPage',true);   // 浮层盖在终端子页上，返回时露出列表
  /* 浮层从顶栏下沿开始：顶栏高度量出来写进 --hdh（各机型/安全区不一样） */
  try{
    var h=document.querySelector('header').getBoundingClientRect().height;
    document.documentElement.style.setProperty('--hdh', (h>0?h:49)+'px');
  }catch(e){}
  var ratio=0.78;
  try{ ratio=parseFloat(localStorage.getItem('term_h_ratio')||'')||0.78; }catch(e){}
  termApplyH(termMaxH()*ratio);
  termSyncIme();
  var t=TERM.terms.find(function(x){return x.sid===sid;})||
        (TERM.pin?findSession(sid):null);
  document.getElementById('termTitle').textContent=(t&&(t.name||t.title))||sid.slice(0,8);
  document.getElementById('termFrame').src='/s/'+sid+'?bare=1';   /* bare：终端页自己的头隐藏，用浮层顶栏 */
  /* 浮层盖在终端子页上（z 6 > 5）：打开即显示 */
  try{ termSyncOverlay(); }catch(e){}
}
/* 电脑托管的网页会话（Windows 的 ConPTY、网页里手开的终端）没有 ext 聊天数据。
   以前「打开」是 location='/s/<sid>' 把整个 WebView 导航走：页面一慢/一报错就停在
   白屏，App 没有浏览器的返回键，等于把 App 弄死（2026-11-11 开发机 Windows）。
   统一在我们自己的终端浮层里开：浮层有返回/关闭，永远回得到列表。 */
function hostTermOpen(sid){
  termView(sid,true);
  termSyncOverlay();
}
/* 电脑会话里由 host 自管 ConPTY 的那些（Windows）：不在 /api/terms，也不在 /api/sessions，
   只在 ext 列表里带 host:1。浮层靠它判断「还活着」。 */
function hostSessAlive(sid){
  return (lastExt||[]).some(function(x){return x.sid===sid&&x.host;});
}
/* iframe 高度用行内样式直接算出来写死：老 WebView 上 flex / 百分比高度都可能不生效，
   高度一塌终端就量不到尺寸（整屏黑）。窗口变化、键盘上推、拖高度时都重算。 */
function termFitFrame(){
  if(!TERM.view) return;
  var b=document.getElementById('termViewBox'), f=document.getElementById('termFrame');
  if(!b||!f) return;
  var bh=b.clientHeight||0;
  var h=bh-56; if(h<80) h=80;
  f.style.cssText='position:absolute;left:0;right:0;top:56px;width:100%;height:'+h+'px;border:0;display:block;background:#0a0a0a';
}
/* iOS 原生键盘不经过壳：系统会把整个页面往上滚去找输入框，而 html/body 又禁止滚动，
   结果顶栏和标题被顶出屏幕、手指也滑不回来。输入条/快捷键条已经按 --kb 抬到键盘
   上沿（见 #bar/#keys/#deck 的 CSS），这里再把系统那次滚动复位。 */
/* ── 键盘弹起后「看得见的那一条」──
   安卓壳（沉浸全屏下 adjustResize 不生效）把整页上推 --ime；iOS 缩 visualViewport
   或把页面滚走 offsetTop。先按两者算出可见带（页面坐标，跟 getBoundingClientRect
   同一坐标系），再把焦点输入框滚回去——搜索框这种靠上的框，以前整页一推就没了
   （2026-11-21 用户）。 */
function kbBandCalc(ime, vvTop, vvH, H){
  var top = Math.max(0, ime || 0), bot = H || 0;
  if (vvH > 50 && vvH < (H || 0) - 80){
    top = Math.max(top, vvTop || 0);
    bot = Math.min(bot, (vvTop || 0) + vvH);
  }
  return {top: top, bot: bot};
}
/* 要滚多少：正值 = 内容往上滚（被键盘盖住），负值 = 往下滚（顶出屏幕了）。
   留白是「已经够贴边就别动」的门槛，不是目标位置：会话页输入条本来就贴着
   可见带下沿，按「滚到 bot-pad」算的话每次都有一笔正位移，滚一下、kbUnscroll
   再复位一下，自己把自己抖起来（2026-11-21 iOS 会话页持续抖）。 */
function kbScrollDelta(r, band, pad){
  if (!r) return 0;
  pad = (pad === undefined) ? 12 : pad;
  if (r.top < band.top - pad) return r.top - (band.top + pad);
  if (r.bottom > band.bot + pad) return r.bottom - (band.bot - pad);
  return 0;
}
function kbBand(){
  var vv = window.visualViewport || {};
  return kbBandCalc(termShellIme(), vv.offsetTop || 0, vv.height || 0, window.innerHeight || 0);
}
function kbReveal(){
  var el = document.activeElement;
  if (!el) return;
  var tag = String(el.tagName || '').toUpperCase();
  if (tag !== 'INPUT' && tag !== 'TEXTAREA' && !el.isContentEditable) return;
  var band = kbBand();
  var H = window.innerHeight || 0;
  if (band.top <= 0 && band.bot >= H - 1) return;          /* 键盘没开：别动 */
  var d = kbScrollDelta(el.getBoundingClientRect(), band, 12);
  if (!d) return;
  /* 内容都在各自的滚动容器里（html/body 不滚）：找最近一个真能滚的祖先 */
  var s = el.parentNode;
  while (s && s.nodeType === 1 && s !== document.body){
    var oy = '';
    try{ oy = getComputedStyle(s).overflowY; }catch(e){}
    if ((oy === 'auto' || oy === 'scroll') && s.scrollHeight > s.clientHeight + 2) break;
    s = s.parentNode;
  }
  /* 没有能滚的祖先 = 固定定位的输入条（会话页那根）：整页本来就不许滚
     （html/body overflow:hidden），自己 scrollBy 只会跟 kbUnscroll 打架。不动。 */
  if (!s || s.nodeType !== 1 || s === document.body) return;
  try{ s.scrollTop = Math.max(0, s.scrollTop + d); }catch(e){}
}
/* 键盘动画期间位置要变好几拍：一次开合里隔一会儿再摆两遍（幂等，看不见就不动）。
   一串 resize/scroll 事件只排一次，别每帧都 measure。 */
var kbRevT = 0, kbRevN = 0;
function revealSoon(){
  if (kbRevT) return;
  kbRevN = 0;
  var tick = function(){
    kbRevT = 0;
    kbReveal();
    if (++kbRevN < 3) kbRevT = setTimeout(tick, kbRevN === 1 ? 180 : 340);
  };
  kbRevT = setTimeout(tick, 60);
}
/* 键盘开合/页面切换时按当前是哪个页重排：
   会话页整页上推（输入条贴键盘，顶栏用 --hime 反向补偿）；
   其它页把 body 收成键盘以上那一条（--kbtop），顶栏就不用补了。 */
function kbLayout(){
  var ime = termShellIme();
  var pg1 = false;
  try{ pg1 = document.body.classList.contains('pg1'); }catch(e){}
  try{
    document.documentElement.style.setProperty('--kbtop', (pg1 ? 0 : ime) + 'px');
    document.documentElement.style.setProperty('--hime', (pg1 ? ime : 0) + 'px');
  }catch(e){}
  revealSoon();
}
function kbUnscroll(){
  try{
    if(window.scrollY || window.pageYOffset || document.documentElement.scrollTop || document.body.scrollTop){
      window.scrollTo(0,0);
      document.documentElement.scrollTop=0;
      document.body.scrollTop=0;
    }
  }catch(e){}
}
function kbSync(){
  var wasBot=false;
  try{ wasBot=atBottom(); }catch(e){}
  /* 可见区域 = visualViewport：键盘弹起时它缩、被系统滚走时 offsetTop 变大，
     两条都按它摆正，不依赖各系统版本怎么报 innerHeight。 */
  try{
    var vv=window.visualViewport;
    if(vv&&vv.height>50){
      document.documentElement.style.setProperty('--vvh',Math.round(vv.height)+'px');
      document.documentElement.style.setProperty('--vvt',Math.round(vv.offsetTop||0)+'px');
      /* 键盘弹起时底部安全区（Home 条那块）已经没了：不去掉的话输入框会离键盘空出
         一截 ~34px，看着像没贴住。收起键盘再还给 env()。 */
      /* 键盘开着没：不能用 innerHeight 算——iOS 在键盘动画中间会让它一会儿缩一会儿回来，
         按它判断会得到「没开」（手机实测就是这里翻车）。拿布局视口（clientHeight/outerHeight，
         键盘不会改它）和可见高度比。 */
      var full=Math.max(window.outerHeight||0, document.documentElement.clientHeight||0, window.innerHeight||0);
      var kbOn=(full-vv.height)>120;
      var sabNow=kbOn?'0px':'';
      if(sabNow!==lastSab){                 /* 只有真变了才重排：键盘动画期间这函数每帧都在跑 */
        lastSab=sabNow;
        document.documentElement.style.setProperty('--sab',sabNow);
        /* 输入条高度变了 34px，快捷键拉手是按 --barh 贴上去的，得重新量一遍，
           否则它会留在旧位置——「拉手离输入框变远、一打字又自己缩回来」就是这个。 */
        setTimeout(function(){ try{ syncBarH(); }catch(e){} },0);
      }
    }
  }catch(e){}
  termSyncIme(); kbUnscroll();
  revealSoon();          /* 键盘弹/收、系统滚走：焦点框摆回看得见的那一条 */
  if(wasBot) requestAnimationFrame(function(){ try{ toBottom(); }catch(e){} });   // 原来就贴底：缩完还是贴底
  var cs='';
  try{ cs=String(getComputedStyle(document.documentElement).getPropertyValue('--kb')||'').trim(); }catch(e){}
  if(cs!==kbLast){ kbLast=cs; setTimeout(function(){ kbDiag('kb='+cs); },80); }
  /* 可见高度每变一次都记一笔：展开快捷键时到底是谁把输入框顶歪的，看序列就知道 */
  var vhNow=Math.round((window.visualViewport&&window.visualViewport.height)||0);
  if(vhNow!==kbLastVh){ kbLastVh=vhNow; setTimeout(function(){ kbDiag('vh'); },120);
    /* 键盘起落 = 可见高度变了，输入框的上限跟着变：按新高度重新量一次（别每帧都量，拖到停） */
    clearTimeout(fitTxtT); fitTxtT=setTimeout(function(){ try{ fitTxt(); }catch(e){} },120); }
}
/* 手机视口数字打到 host 日志（[clidiag]）：iOS 各版本的 innerHeight / visualViewport
   报法不一样，「键盘弹起后谁被顶出去」只能让手机自己报，肉眼读代码猜不准。 */
var kbLast='', kbLastVh=0, lastSab=null, fitTxtT=0;
function kbDiag(tag){
  if(location.search.indexOf('kbd=1')<0) return;   /* 只在网址带 kbd=1 时打日志，别把正常使用的日志刷满 */
  try{
    var vv=window.visualViewport||{};
    function r(id){ var el=document.getElementById(id); if(!el) return '-';
      var x=el.getBoundingClientRect(); return Math.round(x.top)+'..'+Math.round(x.bottom); }
    var a=(document.activeElement&&(document.activeElement.id||document.activeElement.tagName))||'-';
    var de=document.documentElement, cs=getComputedStyle(de);
    var msg=tag+' ih='+window.innerHeight+' vh='+Math.round(vv.height||0)+' ot='+Math.round(vv.offsetTop||0)+
      ' pt='+Math.round(vv.pageTop||0)+' sy='+Math.round(window.scrollY||0)+
      ' vvh='+String(cs.getPropertyValue('--vvh')||'').trim()+' sab='+String(cs.getPropertyValue('--sab')||'').trim()+
      ' bar='+r('bar')+' keys='+r('keys')+' deck='+r('deck')+' act='+a+
      ' kopen='+((document.getElementById('keys')||{}).className||'').replace(' ','');
    /* 走页面自己的请求通道（cookie/票据都在里面），别自己拼 URL：ext.html 里没有 TOKEN 这个变量 */
    if(typeof xj==='function') xj('/api/ext_diag?m='+encodeURIComponent(msg));
  }catch(e){}
}
window.addEventListener('resize', kbSync);
if(window.visualViewport){
  window.visualViewport.addEventListener('resize', kbSync);
  window.visualViewport.addEventListener('scroll', kbSync);
}
/* 聚焦/失焦后的几拍里再复位几次：键盘动画期间 WebKit 会一边收边滚，只抹一次
   会留下一截顶不回去的位移。 */
document.addEventListener('focusin', function(){ [30,120,320,700].forEach(function(t){ setTimeout(kbUnscroll,t); }); revealSoon(); setTimeout(function(){ kbDiag('focus'); },500); }, true);
document.addEventListener('focusout', function(){ [60,260,600].forEach(function(t){ setTimeout(kbUnscroll,t); }); setTimeout(function(){ kbDiag('blur'); },700); }, true);
window.addEventListener('orientationchange', function(){ setTimeout(function(){ if(TERM.view) termApplyH(TERM.pref||termMaxH()*0.78); },320); });
/* 顶部把手：上下拖动改终端高度；松手记住比例 */
(function(){
  var g=document.getElementById('termGrab');
  if(!g) return;
  var y0=0,h0=0,on=false;
  function start(y){ if(!TERM.view) return; on=true; y0=y; h0=document.getElementById('termViewBox').clientHeight; }
  function move(y){ if(on) termApplyH(h0-(y-y0)); }
  function end(){ if(!on) return; on=false;
    try{ localStorage.setItem('term_h_ratio',String((TERM.h||0)/termMaxH())); }catch(e){} }
  g.addEventListener('touchstart',function(e){ start(e.touches[0].clientY); e.preventDefault(); },{passive:false});
  g.addEventListener('touchmove',function(e){ if(on){ move(e.touches[0].clientY); e.preventDefault(); } },{passive:false});
  g.addEventListener('touchend',end);
  g.addEventListener('touchcancel',end);
  g.addEventListener('mousedown',function(e){ start(e.clientY); e.preventDefault(); });
  document.addEventListener('mousemove',function(e){ move(e.clientY); });
  document.addEventListener('mouseup',end);
})();
/* 终端浮层里不再接管左右滑：换页/换会话都走顶栏和列表入口，横滑留给终端自己 */
/* 终端浮层：TERM.view 开着就盖在终端子页上面（z 6 > 子页 5），列表先藏起来。
   终端没了就收起浮层、露出列表，绝不留一块死白屏。
   以前浮层显不显示要看面板停在哪一格，现在终端页是子页、不占格，只看 TERM.view。 */
function termSyncOverlay(){
  var box=document.getElementById('termViewBox');
  var list=document.getElementById('termList');
  if(!box) return;
  var alive=!!TERM.view&&((TERM.terms||[]).some(function(t){return t.sid===TERM.sid;})||
    (TERM.pin&&((lastWeb||[]).some(function(t){return t.sid===TERM.sid;})||hostSessAlive(TERM.sid))));
  if(alive){
    if(list) list.style.display='none';
    if(box.style.display==='none'){ box.style.display=''; termApplyH(TERM.h||termMaxH()*0.78); }
  }else{
    if(box.style.display!=='none') box.style.display='none';
    if(list) list.style.display='';
  }
}
function termPageSync(){ termSyncOverlay(); }
function termBack(){
  TERM.view=false; TERM.sid=''; TERM.pin=false;
  try{ termSyncOverlay(); }catch(e){}
  document.getElementById('termFrame').src='about:blank';
  termLoad();
}