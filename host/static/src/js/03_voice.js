// SPDX-License-Identifier: AGPL-3.0-or-later

function dndActive(now){
  var d=dndCfg(); if(!d.on) return false;
  var f=_dndHM(d.f), t=_dndHM(d.t);
  if(f<0||t<0||f===t) return false;        // 起止相同 = 不生效，免得整天静音
  var x=now?new Date(now):new Date();
  var m=x.getHours()*60+x.getMinutes();
  return f<t?(m>=f&&m<t):(m>=f||m<t);      // 跨午夜：22:00–08:00
}
function dndToggle(){
  var c=vsCfg(), wasOn=c.dnd===true;
  if(wasOn) track('dnd_off'); else track('dnd_on');
  vsSave(Object.assign(c,{dnd:!wasOn}));
  sysAudioApply();
  /* 显式退出免打扰（且静音也没开）：绕过壳的边沿记账直接按滑条刻度写回——
     记账脱节时边沿机会一直「以为不用动」，用户关了免打扰还是没声音 */
  try{ if(wasOn&&!dndActive()&&c.mute!==true&&window.CB&&CB.restoreSysAudio) CB.restoreSysAudio(); }catch(e){}
  nRender();
}
/* 静音/免打扰就是整机的静音键（用户 2026-11-15）：开了连系统铃声/通知一起静，
   App 退出也不恢复——铃声模式是系统设置，天然持久。改完立刻切（新壳
   CB.applySysAudio），后台服务每 30 秒也会自己跟一遍（黑屏、时段进出）。 */
function sysAudioApply(){
  try{ if(window.CB&&CB.applySysAudio) CB.applySysAudio(); }catch(e){}
}
/* 静音（快捷开关）：长按音量下键 0.8 秒切换，设置页也有一座开关。
   和免打扰同一个落点（提醒音、语音播报、系统铃声通知全静，角标和系统通知照挂），
   区别是它不看时段、开着就一直静音，直到再长按一次或在这里关掉。
   存在 cfg.mute 里——壳的 BgService 读同一份 cfg_raw，黑屏时也认这个键。 */
function muteOn(){ return vsCfg().mute===true; }
function muteToggle(){
  var c=vsCfg(), on=c.mute!==true, o={};
  if(on) track('mute_on'); else track('mute_off');
  o.mute=on;
  vsSave(Object.assign(c,o));
  sysAudioApply();
  if(on) toast('🔇 已静音（整机静音，长按音量下键恢复）');
  else if(dndActive()) toast('🌙 免打扰时段内保持整机静音（时段：'+dndCfg().f+'–'+dndCfg().t+'，设置页可改）');
  else{
    /* 显式关静音：不走壳的边沿记账，直接按滑条刻度写系统，再读回真值报给用户 */
    var st={};
    try{ if(window.CB&&CB.restoreSysAudio){ CB.restoreSysAudio(); st=JSON.parse(CB.sysAudioState()||'{}'); } }catch(e){}
    try{ beep('ping'); }catch(e){}           /* 写完再响：恢复前响等于没响（媒体还是 0） */
    toast('🔊 已恢复：提醒'+Math.round(alertVol())+'%·媒体'+Math.round(speechVol())+'%'
      +(st.ringMx>0?('（系统读回 铃'+st.ring+'/'+st.ringMx+'·媒'+st.music+'/'+st.musicMx+'）'):'（已写入系统音量）'));
  }
  nRender();
  return on;
}
/* 壳（音量下键长按）调这个：页面不在前台也能改，返回值给壳做震动反馈 */
try{ window.__cbMuteToggle=muteToggle; }catch(e){}
function dndSet(k,v){
  var c=vsCfg(), o={};
  o[k==='f'?'dndF':'dndT']=(_dndHM(v)>=0?v:(k==='f'?'22:00':'08:00'));
  vsSave(Object.assign(c,o)); sysAudioApply(); nRender();
}
/* 起止时间：原生 <input type="time"> 在 iOS / 安卓 WebView 里经常只画一个空框，
   值要点了弹出选择器才看得见。所以值放在 <b> 里常显，原生输入透明铺满整格——
   点哪儿都落在原生输入上，照样弹系统选择器，只是不再「看不见时间」。 */
/* 「语音里说明在做什么」：存在开发机（prefs.json），不存手机——
   概括是开发机算的（要决定花不花小模型的调用），只能跟着那台机器走。 */
var HPREF={on:true,llm:true,max:12,replyOn:true,replyMax:40,sayStyle:'plain',sayStyles:[],smartOn:true,naOn:true};
function hpReply(d){ if(d&&d.on!==undefined){ HPREF.replyOn=d.on!==false; HPREF.replyMax=d.max||40; } }
function hpNa(d){ if(d&&d.on!==undefined) HPREF.naOn=d.on!==false; }
/* ── 智能提醒是两层开关 ──
   本机层（这个函数）：存手机配置 cb_cfg / 壳 cfg_raw 的 smart 键，每台手机各管各的。
   开发机层：只要还有一台手机开着，就照常生成智能内容（模型概括/语音合成）；
   这台关着的手机收到提醒也只当基础提醒用——不念、不显示智能那部分。 */
function devId(){
  try{
    var d=localStorage.getItem('cb_dev');
    if(!d){
      d='d'+Math.random().toString(36).slice(2,10)+Date.now().toString(36).slice(-4);
      localStorage.setItem('cb_dev',d);
    }
    return d;
  }catch(e){ return ''; }
}
function smartOn(){ return vsCfg().smart!==false; }
function smartSet(on){
  track(on?'smart_on':'smart_off');
  var c=vsCfg(); c.smart=!!on; vsSave(c);
  HPREF.smartOn=!!on; nRender();
  prefReport();
}
/* 把本机开关报给开发机（开发机据此判断「是不是还有一台要智能提醒 / 要听语音」）。
   开机/进设置页各报一次，顺便刷新这台手机的上报时间（30 天不报就当不在了）。
   voice_on 报的是**实际生效值**：智能提醒关掉时这台手机不会念，按关算。 */
function prefReport(){
  xj('/api/ext_pref',{method:'POST',body:{dev:devId(),smart_on:smartOn(),
    /* 自建中继没有 TTS（caps.tts=false）：按关报——host 停合成/概括，不白调 404 的合成 */
    voice_on:smartOn()&&vsCfg().nvoice!==false&&capsOk('tts')}}).catch(function(){});
}
function prefLoad(){
  /* 带 dev=：每设备的编码模式（coding.dev_on）只有带了本机 dev 才回得来 */
  xj('/api/ext_pref?dev='+encodeURIComponent(devId()),{}).then(function(r){
    if(r.json&&r.json.ok&&r.json.about){ HPREF=Object.assign(HPREF,r.json.about); }
    if(r.json&&r.json.reply) hpReply(r.json.reply);
    if(r.json&&r.json.say){ HPREF.sayStyle=r.json.say.style||'plain'; HPREF.sayStyles=r.json.say.styles||[]; }
    if(r.json&&r.json.na) hpNa(r.json.na);
    if(r.json&&r.json.coding){ CODING_ON=r.json.coding.on!==false; swSet('codingSw',CODING_ON); }
    if(r.json&&r.json.smart){
      /* 升级到「每台手机一个开关」后第一次进来：沿用开发机原来的共享值，
         别让所有人被默认值悄悄打开/关掉。之后本机设置只认本地这份。 */
      var c=vsCfg();
      if(c.smart===undefined){
        var sd=(r.json.smart.dev_on!==undefined&&r.json.smart.dev_on!==null)
               ? r.json.smart.dev_on : (r.json.smart.on!==false);
        c.smart=sd!==false; vsSave(c);
      }
      HPREF.smartOn=smartOn();
      prefReport();
    }
    nRender();
  }).catch(function(){});
}
function prefSend(o){
  /* 先乐观改本地（点了立刻有反馈），失败再回读真值 */
  if(o&&o.na_on!==undefined) HPREF.naOn=!!o.na_on;
  if(o&&o.on!==undefined) HPREF.on=!!o.on;
  if(o&&o.llm!==undefined) HPREF.llm=!!o.llm;
  if(o&&o.reply!==undefined) HPREF.replyOn=!!o.reply;
  nRender();
  xj('/api/ext_pref',{method:'POST',body:o}).then(function(r){
    if(r.json&&r.json.ok&&r.json.about) HPREF=Object.assign(HPREF,r.json.about);
    if(r.json&&r.json.reply) hpReply(r.json.reply);
    if(r.json&&r.json.say){ HPREF.sayStyle=r.json.say.style||HPREF.sayStyle; HPREF.sayStyles=r.json.say.styles||HPREF.sayStyles; }
    /* smart 不在这里回写：开发机回的是「所有手机的聚合值」，本机开关只认自己的配置 */
    if(r.json&&r.json.na) hpNa(r.json.na);
    nRender();
    if(r.status===0){ toast('设置没保存上（网络）',true); prefLoad(); }
  }).catch(function(){ toast('设置没保存上（网络）',true); prefLoad(); });
}
/* 播报风格：说法和音色一起换（开发机合成，手机只负责放）。
   点一下就试听——不试听光看名字，分不出「商务」和「夸夸」是什么味儿。 */
function sayPick(id){
  if(!remindPlanOK()||!smartOn()){ toast('智能提醒是 Pro / Max 套餐的增值功能',true); return; }
  if(vsCfg().nvoice===false){ toast('先打开「语音播报」再选风格'); return; }
  if(!id||id===HPREF.sayStyle) { sayDemo(); return; }
  HPREF.sayStyle=id; renderSayStyles();
  xj('/api/ext_pref',{method:'POST',body:{say_style:id}}).then(function(r){
    if(r.json&&r.json.say){ HPREF.sayStyle=r.json.say.style||id; HPREF.sayStyles=r.json.say.styles||HPREF.sayStyles; }
    renderSayStyles(); sayDemo();
  }).catch(function(){ renderSayStyles(); sayDemo(); });
}
/* 试听当前风格：开发机把「完成了」按这个风格念好，手机直接放（没有 TTS 引擎也能听） */
function sayDemo(){
  if(!remindPlanOK()||!smartOn()){ toast('智能提醒是 Pro / Max 套餐的增值功能',true); return; }
  if(vsCfg().nvoice===false){ toast('先打开「语音播报」再试听'); return; }
  toast('播放语音…');
  /* 放不出来就退回预录真人音，**不换嗓音**（系统朗读念出来就不是「播报风格」选的那个了） */
  var fb=function(){
    if(window.CB&&CB.cue){ try{ CB.cue('done'); return; }catch(e){} }
    /* 浏览器不出声：没有壳就没有那句合成好的音频 */
  };
  xj('/api/say_demo',{}).then(function(r){
    if(r.json&&r.json.ok&&r.json.say){
      /* 试听每次都绕开手机 HTTP 缓存：开发机上重新合成过、或换了音色时，
         URL 可能还是同一个（内容按音色+文本命名），旧缓存会让试听听不出变化。 */
      var u=r.json.say+(r.json.say.indexOf('?')<0?'?':'&')+'v='+Date.now();
      /* 第三个参数才是「放不出来」的兜底：playSay 播完也会回调 done，
         以前把 fb 当 done 传，试听念完紧跟着又响一遍壳里的预录音（两个嗓音）。 */
      if(!playSay(u, null, fb)) fb();
      return;
    }
    fb();
  }).catch(function(){ fb(); });
}
function renderSayStyles(){
  var box=document.getElementById('sayStyleBtns'); if(!box) return;
  var cur=HPREF.sayStyle||'plain', ss=HPREF.sayStyles||[];
  if(!ss.length) ss=[{id:'plain',name:'简洁',hint:''}];
  var h='';
  for(var i=0;i<ss.length;i++){
    var s=ss[i];
    h+='<button class="gray mini'+(s.id===cur?' on':'')+'" onclick="sayPick(\''+s.id+'\')" title="'+esc(s.hint||'')+'">'+esc(s.name)+'</button>';
  }
  box.innerHTML=h;
  var hint=document.getElementById('sayStyleHint'), cur0=null;
  for(var j=0;j<ss.length;j++) if(ss[j].id===cur) cur0=ss[j];
  if(hint) hint.textContent=cur0?(cur0.hint||'')+(cur0.vname?' · '+cur0.vname:''):'';
}
function nPrefDip(id,on){          /* 置灰 + 停点击：语音播报关着时，这几项点了也不会被念 */
  var el=document.getElementById(id); if(!el) return;
  el.style.opacity=on?'1':'.45';
  el.style.pointerEvents=on?'':'none';
}
function nPrefToggle(k){
  if(!remindPlanOK()) return;          /* 免费版：整区已置灰，这里再兜一层 */
  if(k==='smart'){ smartSet(!smartOn()); return; }
  track('npref');
  /* 「是否在等你」也走语音区：语音播报关掉后判断了也没人听，跟着一起停 */
  if(k==='na'){ if(!smartOn()||vsCfg().nvoice===false) return; prefSend({na_on:HPREF.naOn===false}); return; }
  if(!smartOn()) return;               /* 本机关掉智能提醒：子项不生效 */
  if(vsCfg().nvoice===false) return;   /* 语音播报关着：这几项不生效，先别让人改出「以为生效了」 */
  if(k==='on') prefSend({on:!HPREF.on});
  else if(k==='llm') prefSend({llm:!HPREF.llm});
  else if(k==='reply') prefSend({reply:HPREF.replyOn===false});
}
function nRender(){
  const c=vsCfg();
  swSet('swNBg',c.bgn!==false);
  swSet('swNText',c.ntext!==false); swSet('swNSound',c.nsound!==false);
  swSet('swNVoice',c.nvoice!==false);
  /* 免打扰时段：开关 + 起止时间（时间行只在开关打开时出现，省得设置页太长） */
  var dnc=dndCfg();
  swSet('swDnd',dnc.on);
  var dF=document.getElementById('dndF'), dT=document.getElementById('dndT');
  if(dF) dF.value=dnc.f;
  if(dT) dT.value=dnc.t;
  var fTx=document.getElementById('dndFTxt'), tTx=document.getElementById('dndTTxt');
  if(fTx) fTx.textContent=dnc.f;      // 常显的那份文字（原生输入在部分壳里看不见值）
  if(tTx) tTx.textContent=dnc.t;
  var dRow=document.getElementById('trDnd'), dHint=document.getElementById('trDndHint');
  if(dRow) dRow.style.display=dnc.on?'':'none';
  if(dHint) dHint.style.display=dnc.on?'':'none';
  /* 静音：长按音量下键或点这一行切换（开着时整行高亮，别忘了自己开过） */
  swSet('swMute',muteOn());
  /* 基础提醒免费；智能提醒是 Pro/Max：免费档把智能区置灰，只留一条去开通的提示
     （开关留在原处不动，升级后回来还是用户原来设的那套）。 */
  swSet('swNSmart',smartOn());
  swSet('swNNa',HPREF.naOn!==false);
  smartGateSync();
  swSet('swNAbout',HPREF.on!==false);
  swSet('swNAboutLlm',HPREF.llm!==false);
  swSet('swNReply',HPREF.replyOn!==false);
  renderSayStyles();
  /* 智能提醒关掉/语音播报关掉 → 下面这些都不会被念到：整区置灰、点不动。
     重新打开就按原来保存的值恢复（不改用户之前的设置）。 */
  var smart=smartOn()&&remindPlanOK();
  var nv=smart&&(c.nvoice!==false);
  nPrefDip('trNAbout',nv);
  nPrefDip('trNReply',nv);
  nPrefDip('trNAboutLlm',nv&&HPREF.on!==false);
  nPrefDip('trNNa',nv);              /* 「语音播报关」→ 在等你的判断也一起置灰 */
  nPrefDip('trSayStyle',nv);
  nPrefDip('trSayHint',nv);
  const cap=document.getElementById('nCap');
  if(cap){
    const ac=!!(window.AudioContext||window.webkitAudioContext);
    /* 语音一律是开发机合成的（播报风格选的那个音色），手机不装朗读引擎也能听 */
    const bg=(window.CB&&CB.notify2)?'后台通知 ✓':(window.CB?'App 较旧，后台无声音':'浏览器模式（只提示音）');
    /* 把「此刻为什么不念」直接写在卡片底部：开关关着 / 音量为 0 / 免打扰开着
       这三件事都不显眼，用户只会得出「语音播报坏了」这个结论。 */
    var mute=[];
    if(!remindPlanOK()) mute.push('智能提醒是套餐功能');
    else if(!smartOn()) mute.push('本机智能提醒关');
    if(!nv) mute.push('语音播报关');
    if(c.nsound===false) mute.push('提醒音关');
    if(speechVol()<=0) mute.push('语音音量 0');
    if(dnc.on) mute.push('免打扰开');
    if(muteOn()) mute.push('静音开');
    cap.textContent='本机：'+(ac?'音频 ✓':'音频 ✗')+' · 语音：开发机合成 · '+bg
      +(mute.length?(' · ⚠️ '+mute.join('、')+'，现在不会念'):'');
  }
}
/* 试听：确认是设备静音/引擎缺失，还是提醒本身没触发 */
function notifyTest(k){
  if(k==='snd'){
    const sc=volScale();
    if(sc<=0){ toast('提醒音已静音（把滑条往右拉）'); return; }
    beep('done'); setTimeout(function(){ beep('alert'); }, 350);   // 和真实提醒同一套音
    toast('已播放（提醒音 '+Math.round(sc*100)+'%）');
  }
  else {
    /* 试听要和真实提醒同一种声音：开发机按当前风格+音色念好一句，手机直接放。 */
    if(speechVol()<=0){ toast('语音播报音量是 0（把滑条往右拉）'); return; }
    sayDemo();
  }
}
/* 提醒音：三种音型（完成/待确认/待回复）。WebAudio 合成，不带资源文件；
   移动端首次触摸时解锁 AudioContext（自动播放策略）。 */
let NAC=null;
function nac(){
  try{
    NAC=NAC||new (window.AudioContext||window.webkitAudioContext)();
    if(NAC.state==='suspended') NAC.resume();
    return NAC;
  }catch(e){ return null; }
}
/* App 内音量：提醒音和语音播报各一个 0~100 的滑条，存在 cfg 里（壳也读同一份）。
   ⚠️ 以前 beep() 的增益写死 0.22，滑条只去改系统媒体音量 ——
   而 WebAudio 不跟着系统音量走（实测滑到底和滑到顶听感一样），用户看到
   「设了跟没设一样」。现在合成时按滑条值缩放，0 就是真静音。 */
function alertVol(){
  const c=vsCfg();
  return (typeof c.alertVol==='number')?Math.max(0,Math.min(100,c.alertVol)):60;
}
function speechVol(){
  const c=vsCfg();
  return (typeof c.speechVol==='number')?Math.max(0,Math.min(100,c.speechVol)):90;
}
function volScale(){ return alertVol()/100; }
function beep(kind){
  const sc=volScale(); if(sc<=0) return false;      // 滑到 0：静音，别响
  const a=nac(); if(!a) return false;
  /* WebAudio 被系统挂起时（长时间没触摸/页面刚回来），resume 是异步的：
     这时排的音会在恢复后的某个时刻响——自动听写正好开着麦，就录成「叮」。
     这种时候别排音（调用方会据此缩短开麦等待）。 */
  if(a.state && a.state !== 'running') return false;
  const t0=a.currentTime;
  /* 三个自动听写的提示音要一听就分得开（用户要求）：
       ping = 口述阶段开麦（单声低「叮」）
       ask  = 确认阶段开麦（上行两声，比 ping 高）
       sent = 发送完成（上行三连，C-E-G「完成」音） */
  const P={done:[[880,0,.12],[1320,.16,.16]],
           alert:[[740,0,.1],[740,.15,.1],[740,.3,.18]],
           ping:[[640,0,.16]],
           ask:[[820,0,.11],[1100,.14,.14]],
           sent:[[1046,0,.09],[1318,.12,.09],[1568,.24,.16]]};
  const seq=P[kind]||P.done;
  seq.forEach(function(x){
    const f=x[0],at=x[1],d=x[2];
    const o=a.createOscillator(), g=a.createGain();
    o.type='sine'; o.frequency.value=f;
    g.gain.setValueAtTime(0.0001,t0+at);
    g.gain.exponentialRampToValueAtTime(Math.max(0.0002,0.22*sc),t0+at+0.02);
    g.gain.exponentialRampToValueAtTime(0.0001,t0+at+d);
    o.connect(g); g.connect(a.destination);
    o.start(t0+at); o.stop(t0+at+d+0.02);
  });
  return true;
}
/* 念结论。**嗓音只有一处**：开发机按「播报风格」合成好的那句（/api/say/…）。
   以前这里会掉到系统朗读 / 预录音，用户听到的就是「怎么是别人的声音」——那条路
   已经删掉了（浏览器 Web Speech 也在内：没有壳就不出声，只留提示音和通知）。
   走到这里只有两种情况：① 息屏提醒关着、App 在前台；② 这代壳没有后台播报。
   · CB.sayUrl（新壳）→ 把地址交给壳，由壳下载播放
   · 只有 CB.cue（老壳）→ 页面自己放同一条音频（音色不变）；放不出来就静音
   · 浏览器 → 静音 */
function speakKind(k, about, sayUrl, sayText){
  var p=String(sayUrl||'').trim();
  if(p){
    var abs=/^https?:\/\//i.test(p)?p:(location.origin+p);
    if(window.CB&&CB.sayUrl){
      /* 壳的 Java 端读不到 WebView 的 cookie（2026-11-19 S24 mini：getCookie 只回
         一个 cb_product）→ 身份只能放 URL 上：短命直连票据 dt（中继签给这台设备，
         10 分钟有效；中继 /api/say/<id> 认它）。在家 Wi-Fi、在外面流量都一样。 */
      var go=function(dt){
        var u=abs;
        if(dt) u+=(u.indexOf('?')>=0?'&':'?')+'dt='+encodeURIComponent(dt);
        try{ CB.sayUrl(u); }catch(e){}
      };
      if(DIRECT.dt&&DIRECT.exp-Date.now()/1000>60) return go(DIRECT.dt);
      /* directTicket 自己吞异常、用 true/false 说话；换不到就交裸地址（cookie 还能救） */
      directTicket().then(function(ok){ go(ok?DIRECT.dt:''); });
      return;
    }
    if(window.CB){ playSay(abs); return; }    // 老壳：页面自己放这条
    return;                                   // 浏览器：提醒不出声
  }
  /* 老开发机不带 say：只有内置真人音（固定几句），历史兜底，不参与风格 */
  if(window.CB&&CB.cue){ try{ CB.cue(k); return; }catch(e){} }
}
var SAYAUD=null;
/* 正在念的时候又来了新的一句：排在这，念完接着念。不许 pause 掉正在念的那句——
   那就是「播报会突然截断」（2026-11-20 用户）。只留最新一条：旧提醒已被新的覆盖。 */
var SAYPEND=null;
/* 页面自己放播报时（老壳没有 CB.sayUrl）也要让自动听写知道「还在念」：
   否则页面只能靠固定秒数猜，念得久一点就会把播报录进去。 */
var SAYBUSY=false;
function playSay(url,done,onfail){
  try{
    if(SAYAUD){ SAYPEND={u:url,d:done,f:onfail}; return true; }
    var a=new Audio(url);
    /* 音量全归系统媒体音量：「语音播报」滑条直接写系统（vsVolApplyMedia），不再双重缩放 */
    var failed=false;
    var next=function(){           /* 统一收尾：排队里有就接着念，没有才回调 */
      var p=SAYPEND; SAYPEND=null;
      if(p&&playSay(p.u,p.d,p.f)) return true;
      return false;
    };
    /* 播失败才叫 onfail（没传就退回 done）；正常播完只叫 done，别再多响一段 */
    var fail=function(){ if(failed) return; failed=true; SAYBUSY=false; SAYAUD=null;
      if(next()) return;
      var cb=onfail||done; try{ cb&&cb(); }catch(e){} };
    a.onended=function(){ SAYBUSY=false; SAYAUD=null;
      if(next()) return;
      try{ done&&done(); }catch(e){} };
    a.onerror=fail;
    SAYAUD=a; SAYBUSY=true;
    var p=a.play();
    if(p&&p.catch) p.catch(fail);
    return true;
  }catch(e){ return false; }   /* 老 WebView 连 Audio 都放不了：当场回落 */
}
document.addEventListener('touchstart',function(){ nac(); },{passive:true});
/* 安卓壳默认吞掉长按（防长按链接弹出带完整 URL 的菜单）。输入框里要能长按出
   系统「粘贴/全选」：手指落下时告诉壳这一下在不在可编辑控件里，壳据此放开/收回。 */
document.addEventListener('touchstart',function(e){ setLP(e.target); },{capture:true,passive:true});
/* 光标已经在输入框里时，长按前不一定再有别的动作：焦点进出也同步一次 */
document.addEventListener('focusin',function(e){ setLP(e.target); });
document.addEventListener('focusout',function(){ setLP(null); });
function setLP(t){
  if(!(window.CB&&CB.setLongPress)) return;
  var allow=false;
  try{
    allow=!!(t&&(t.isContentEditable||/^(INPUT|TEXTAREA)$/.test(t.tagName||'')
      ||(t.closest&&t.closest('[contenteditable=""],[contenteditable="true"]'))
      /* 会话正文：长按选中复制（用户 2026-11-14）。那里没有可点链接，不涉及 URL 泄露 */
      ||(t.closest&&t.closest('#live,#livehist,#liveflow,#live2'))));
  }catch(_){}
  try{ CB.setLongPress(allow); }catch(_){}
}
document.addEventListener('click',function(){ nac(); },{passive:true});
/* 补发去重：开发机的推送队列会在每次（每 60 秒）重连时重放 10 分钟内的提醒。
   同一条提醒（id+ts）只处理一次，否则把「新鲜窗口」放宽后，每次重连都会重响一遍。
   开发机重启后 id 从 0 重来 —— hello 里 seq 变小就清表（见 pushFeed）。 */
var NOTIF_SEEN={}, NOTIF_MAXID=0;
/* 「提醒到哪一条了」存在 localStorage：刷新页面、息屏回来重连，都不会把
   十分钟内的旧提醒再响一遍（以前只在内存里记，一刷新就忘了）。
   开发机重启后 id 归零：hello 里 seq 比它小就清表（见 pushFeed）。 */
function nwLoad(){ try{ var o=JSON.parse(localStorage.getItem('cb_nw')||'{}');
  return {seq:(o.seq|0),last:(o.last|0),at:(o.at||0)}; }catch(e){ return {seq:0,last:0,at:0}; } }
function nwSave(o){ try{ localStorage.setItem('cb_nw',JSON.stringify(o)); }catch(e){} }
/* 水位同时记 id 和「响过的最后时刻」：开发机重启后 id 从 1 重来，只看 id 的话
   新提醒会被上一代的水位整批吃掉（实测：重启后再也没出声）。ts 是开发机的
   epoch，跨重启单调，新提醒一定比水位新。 */
function nwBump(id,ts){ if(!id) return; var w=nwLoad();
  var ch=false;
  if(id>w.last){ w.last=id; ch=true; }
  if(ts&&ts>w.at){ w.at=ts; ch=true; }
  if(ch) nwSave(w); }
/* ── 提醒历史：开发机把每条提醒（含智能概括）持久化在 codeboss.db。
   页面按「天 → 会话」自动分组画时间线：不用手工整理，翻一眼就知道每个会话走到哪了。
   ?sid= 只看一个会话（会话详情里进来）。 */
var NH={items:[],sid:'',filter:'all',loading:false};
function nhOpen(sid){
  NH.sid=sid||'';
  var t=document.getElementById('nhTitle');
  if(t) t.textContent=NH.sid?'提醒历史 · 本会话':'提醒历史';
  var p=document.getElementById('nhPage'); if(p) p.classList.add('on');
  track('nh_open');
  nhLoad();
}
function nhClose(){ var p=document.getElementById('nhPage'); if(p) p.classList.remove('on'); }
function nhFilter(f){
  NH.filter=f;
  var a=document.getElementById('nhCAll'), s=document.getElementById('nhCSum');
  if(a) a.className=(f==='all')?'on':'';
  if(s) s.className=(f==='sum')?'on':'';
  nhRender();
}
/* 一次取数：直连（P2P/局域网）拿不到 items 就改走中继再来一次。
   iOS 上大响应整帧丢失（Safari 单条 DataChannel 消息有上限）就是「200 但空体」的样子；
   中继是普通 HTTP，不受这个限制。（host 侧现在也分帧了，这里是双保险。）
   ⚠️ 直连那条路**抛异常**（WebKit 里 DataChannel/解码出问题）时以前直接进 catch，
   历史就永远空着——现在任何直连失败都再走一次中继，别把用户挡在空页面上。 */
function nhFetch(ep){
  return Promise.resolve().then(function(){ return xj(ep); }).catch(function(e){
    try{ window.__cbDbg&&window.__cbDbg('NHdc '+(String((e&&e.stack)||(e&&e.message)||e)).slice(0,200),200); }catch(_){}
    return null;
  }).then(function(r){
    var d=(r&&r.json)||{};
    if(d.ok&&d.items&&d.items.length) return r;
    var pr=xjRelay(ep).catch(function(){ return null; });
    return Promise.resolve(pr).then(function(r2){
      var d2=(r2&&r2.json)||{};
      return (d2.ok&&d2.items) ? r2 : (r||r2||{status:0,json:null});
    });
  });
}
function nhLoad(){
  if(NH.loading) return;
  NH.loading=true; NH.err='';
  /* `_=` 防缓存：WebKit 可能把早先那次空结果的 GET 缓存住，之后一直「还没有记录」 */
  var cb='&_='+Date.now();
  var epAll='/api/notify_hist?limit=500'+cb;
  var epSid=NH.sid?(epAll+'&sid='+encodeURIComponent(NH.sid)):epAll;
  var via='D';
  /* 请求要是永远不回来（iOS 上见过：直连刚断、XHR 悬着），以前 loading 一直是 true，
     点多少次 ↻ 都没反应，界面永远停在「加载中…」。加个看门狗，12 秒没回就当失败。 */
  var wd=setTimeout(function(){ if(!NH.loading) return; NH.loading=false; NH.err='timeout'; nhRender(); },12000);
  var drop=function(r){
    clearTimeout(wd);
    var d=(r&&r.json)||{};
    NH.loading=false;
    if(d.ok&&Array.isArray(d.items)) NH.items=d.items;  /* 断网/后台失败：保留旧内容，别清空 */
    else NH.err=(r&&r.away)?'away':('HTTP '+((r&&r.status)||'0'));
    /* 诊断行：到底是没记录、请求失败、还是页面在后台被 `a=0` 挡了 */
    try{ window.__cbDbg&&window.__cbDbg('NH'+(NH.sid?'S':'')+'/'+via+' st'+((r&&r.status)||0)
      +' ok'+(d.ok?1:0)+' n'+((d.items||[]).length)+' it'+((NH.items||[]).length)
      +' aw'+((r&&r.away)?1:0),30); }catch(e){}
    nhRender();
  };
  nhFetch(epSid).then(function(r){
    var d=(r&&r.json)||{};
    /* 本会话一条都没有：可能只是 sid 口径对不上（会话详情传进来的 id 与提醒记录里存的
       不是同一个），重拉全量在页面里自己筛，别让用户看到一张空页。 */
    if(NH.sid&&d.ok&&d.items&&!d.items.length){
      via='S';
      return nhFetch(epAll).then(function(r2){
        var d2=(r2&&r2.json)||{};
        if(!(d2.ok&&Array.isArray(d2.items))) return drop(r);
        var want=NH.sid;
        var same=function(a,b){ return !!a&&!!b&&(a===b||a.indexOf(b)===0||b.indexOf(a)===0); };
        var hit=d2.items.filter(function(x){ return same(x.sid,want); });
        if(!hit.length) return drop(r);
        via='SF';
        drop({status:200,json:{ok:true,items:hit}});
      });
    }
    drop(r);
  }).catch(function(e){
    clearTimeout(wd);
    NH.err='net'; NH.loading=false;
    /* 打完整堆栈：只打 message 时 iOS 只给一句「undefined is not an object」，
       看不出是哪一行（2026-11-09 提醒历史又空，现场就靠这行）。 */
    try{ window.__cbDbg&&window.__cbDbg('NHe '+String((e&&e.stack)||(e&&e.message)||e).slice(0,260),220); }catch(_){}
    nhRender();
  });
}
function nhDayKey(ts){ var d=new Date(ts*1000);
  return d.getFullYear()+'-'+(d.getMonth()+1)+'-'+d.getDate(); }
function nhDayLabel(ts){
  var d=new Date(ts*1000), n=new Date();
  var z=function(x){ return new Date(x.getFullYear(),x.getMonth(),x.getDate()).getTime(); };
  var diff=Math.round((z(n)-z(d))/86400000);
  var w='日一二三四五六'[d.getDay()];
  if(diff===0) return '今天 · 周'+w;
  if(diff===1) return '昨天 · 周'+w;
  return (d.getMonth()+1)+'月'+d.getDate()+'日 · 周'+w+
         (d.getFullYear()!==n.getFullYear()?(' · '+d.getFullYear()):'');
}
function nhHHMM(ts){ var d=new Date(ts*1000);
  return ('0'+d.getHours()).slice(-2)+':'+('0'+d.getMinutes()).slice(-2); }
/* 一条提醒是不是还在等你处理（和卡片上「确认/回复」按钮同一套判定） */
function nhPending(e){
  if(e.state) return false;
  return e.kind==='perm'||e.kind==='ask'||e.kind==='input'
         ||(e.kind==='done'&&(e.act||'').trim()!=='');
}
/* 提醒健康：从每条提醒自带的 trace（host 判定时顺手记的）里算账。
   要回答的是「这套提醒还值不值得信」：多少条真在等你、多少条是模型改判的、
   多少条待确认问句因为「不是原话」或「已经做完还问」被丢掉、模型有没有答不上来。 */
function nhHealth(){
  var all=(NH.items||[]), now=Date.now()/1000, w=now-7*86400;
  var h={n:0,n7:0,pend:0,re:0,dropped:0,model:0,down:0,oldest:0};
  for(var i=0;i<all.length;i++){
    var e=all[i], tr=e.trace||{};
    h.n++;
    if((e.ts||0)>=w) h.n7++;
    if(nhPending(e)){ h.pend++;
      var age=now-(e.ts||now); if(age>h.oldest) h.oldest=age; }
    if((e.ts||0)<w) continue;          /* 下面几项跟着「近 7 天」这句话走；在等你的是「现在」 */
    if(tr.k==='perm') h.re++;
    if(tr.act==='not-grounded'||tr.act==='conflict') h.dropped++;
    if(tr.na==='llm-yes'||tr.na==='llm-no') h.model++;
    if(tr.na==='llm-down') h.down++;
  }
  return h;
}
function nhAge(s){
  if(s<3600) return Math.max(1,Math.round(s/60))+' 分钟';
  if(s<86400) return Math.round(s/3600)+' 小时';
  return Math.round(s/86400)+' 天';
}
function nhRender(){
  var box=document.getElementById('nhBody'); if(!box) return;
  var items=(NH.items||[]).slice();
  if(NH.filter==='sum') items=items.filter(function(x){ return !!(x.summary||x.about); });
  if(!items.length){
    box.innerHTML='<div class="nhEmpty">'+(NH.items.length
      ? '没有带概括的提醒。<br>智能提醒开着时，每条提醒会带上「起因」「在做什么」和「AI 回复概括」。'
      : (NH.err
          ? '没拿到提醒记录（'+esc(NH.err)+'）。<br>点右上角 ↻ 重试；一直失败就是这台手机到开发机的请求没通。'
          : '还没有提醒记录。<br>会话跑完、需要确认时会自动记在这里。'))+'</div>';
    return;
  }
  /* 自动分组：先按天（时间线），天内再按会话归堆（同一天的同一会话只出一张卡） */
  var days=[], di={};
  for(var i=0;i<items.length;i++){
    var it=items[i], dk=nhDayKey(it.ts), sid=it.sid||'';
    var g=di[dk];
    if(!g){ g={ts:it.ts,sids:[],m:{}}; di[dk]=g; days.push(g); }
    if(!g.m[sid]){ g.m[sid]={sid:sid,items:[]}; g.sids.push(sid); }
    g.m[sid].items.push(it);
  }
  var h='';
  var hp=nhHealth();
  if(hp.n){
    h+='<div class="nhHp"><div>近 7 天 <b>'+hp.n7+'</b> 条'+
       (hp.pend?' · <span class="warn"><b>'+hp.pend+'</b> 条在等你</span>':'')+
       (hp.re?' · <b>'+hp.re+'</b> 条改报待确认':'')+
       (hp.dropped?' · <span class="bad"><b>'+hp.dropped+'</b> 个问句已丢</span>':'')+
       '</div>';
    var why=[];
    if(hp.model) why.push(hp.model+' 条是模型判的');
    if(hp.down) why.push(hp.down+' 条模型没答上，退回规则判');
    if(hp.pend&&hp.oldest>86400) why.push('最久一条等了 '+nhAge(hp.oldest));
    if(why.length) h+='<div class="nhHpWhy">'+esc(why.join('；'))+'</div>';
    h+='</div>';
  }
  for(var a=0;a<days.length;a++){
    var day=days[a];
    h+='<div class="nhDay">'+esc(nhDayLabel(day.ts))+'</div>';
    for(var b=0;b<day.sids.length;b++){
      var grp=day.m[day.sids[b]];
      h+='<div class="nhCard"><div class="nhSess">'+
         '<span class="t">'+esc(grp.items[0].title||('会话 '+String(grp.sid).slice(0,8)))+'</span>'+
         '<span class="c">'+grp.items.length+' 条</span></div>';
      for(var k=0;k<grp.items.length;k++){
        var e=grp.items[k];
        var sum=e.summary||e.about||'';
        /* 判定来路：模型判的还是规则判的、待确认问句是原话摘的，还是因为编的/做完还问被丢掉。
           上面健康页的账就是这些小标签一条条数出来的，每条都要能对上。 */
        var tr=e.trace||{}, tch='';
        if(tr.act==='ok') tch+='<span class="nhTr">原话</span>';
        else if(tr.act==='not-grounded'||tr.act==='conflict')
          tch+='<span class="nhTr bad">问句已丢</span>';
        if(tr.na==='llm-yes'||tr.na==='llm-no') tch+='<span class="nhTr">模型判</span>';
        else if(tr.na==='llm-down') tch+='<span class="nhTr bad">模型没答上</span>';
        h+='<div class="nhEv"><span class="nhTime">'+nhHHMM(e.ts)+'</span>'+
           '<i class="nhDot '+esc(e.kind||'')+'"></i><div class="nhMain">'+
           '<div class="nhTop"><span class="nhKind">'+esc(nbLabel(e.kind))+'</span>'+tch+'</div>';
        if(e.cause) h+='<div class="nhCause"><span>起因</span>'+esc(e.cause)+'</div>';
        /* 概括放主位：一眼看完「这轮结果如何、要不要你动手」。下面是提醒正文——中继按
           max_text 截的原文尾巴，有概括时折成一行，点一下展开（2026-11-09 用户要求：
           这段截断原文不能比概括还抢眼）。 */
        var raw=(e.text||''), fold=!!(sum&&sum!==raw);
        /* 「完成」但模型写出了待确认事项，也算待确认：na 那个 YES/NO 判断有时候会漏
           （2026-11-09 真实漏报：回复末尾问「要不要发 0.7.39」，na 判 NO）。 */
        var confirmKind=(e.kind==='perm'||e.kind==='ask'||e.kind==='input'
                         ||(e.kind==='done'&&(e.act||'').trim()!==''));
        if(sum&&sum!==raw) h+='<div class="nhSum">'+esc(sum)+'</div>';
        /* 这一轮真正要你拍板的那件事（模型另起一行给的问句，带版本号/方案名）。
           它和「这轮结果如何」是两回事：浮条和带入输入框都优先用它。 */
        var need=(e.act||'').trim();
        if(need&&need!==sum) h+='<div class="nhNeed"><span>待确认</span>'+esc(need)+'</div>';
        /* 待确认/待回复的卡片：按钮旁边一律不显示中继截的原文尾巴（2026-11-09 用户要求
           「按钮旁不要带原文」）。只留模型给的概括和「待确认」那一句；两者都没有时
           这条就是空内容，宁可不显示，也不拿断句的原文凑数。 */
        if(raw&&!confirmKind) h+='<div class="nhRaw"><span class="nhTag">原文'+(fold?' ▾':'')+'</span>'+
          '<div class="nhTxt'+(fold?' fold':'')+'"'+(fold?' onclick="nhFold(this)"':'')+'>'+
          esc(raw)+'</div></div>';
        if(e.rep>0) h+='<div class="nhRep">重复提醒 '+e.rep+' 次（一直没处理）</div>';
        /* 待确认/待回复且还没处理：给一个按钮，把前因后果和待确认动作带进输入框。
           点过一次（state=sent）就只显示等结果；会话再报完成时后端标 done。 */
        var st=e.state||'';
        if(confirmKind&&st==='done') h+='<div class="nhAct"><span class="nhDone">✓ 已完成</span></div>';
        /* st==='sent'（点过确认）：按钮和「等结果」都收起来，卡片只留内容本身。 */
        else if(confirmKind&&!st) h+='<div class="nhAct"><button class="nhBtn" onclick="nhConfirm('+
                  NH.items.indexOf(e)+')">'+(e.kind==='ask'||e.kind==='input'?'回复':'确认')+'</button></div>';
        h+='</div></div>';
      }
      h+='</div>';
    }
  }
  box.innerHTML=h;
}
/* 原文一行 ↔ 全文。标签跟着变，不然点开了也看不出还能收回去 */
function nhFold(el){
  var on=el.classList.toggle('fold');
  var t=el.parentNode&&el.parentNode.querySelector('.nhTag');
  if(t) t.textContent=on?'原文 ▾':'原文 ▴';
}
/* 「确认/回复」：把这一条的起因、上一轮结果和待确认的动作带进该会话输入框。
   用户自己改完点发送；发送成功才标「已确认 · 等结果」（/api/notify_act）。
   之后会话报「完成」时，开发机会把同一会话未处理的 perm/ask/input 自动标 done。 */
/* 带进输入框的正文：只放模型总结出来的「这一轮在做什么 / 要你确认什么」，
   不再塞提醒正文（那是中继按 max_text 截的原文尾巴，160 字，读起来像断句）。 */
/* 把模型的待确认问句改写成一句「肯定」的回复，直接能发：
   「要不要发布 0.7.39 版本？」→「要，发布 0.7.39 版本。」；«选 A 还是 B？»→「选 A。」
   用户点确认按钮，默认就是要干；不满意自己改两个字再发。 */
function nhAffirm(q){
  var t=String(q||'').replace(/[\s。.!！]+$/,'').replace(/[?？]+$/,'').trim();
  var m;
  if((m=t.match(/^(?:需不需要|要不要)(.*)$/))&&m[1].trim())
    return '要，'+m[1].replace(/^[，,、\s]+/,'')+'。';
  if((m=t.match(/^是否(?:要|需要)?(.*)$/))&&m[1].trim())
    return '是，'+m[1].replace(/^[，,、\s]+/,'')+'。';
  if((m=t.match(/^选(.+?)(?:还是|或)(.+)$/)))
    return '选'+m[1].trim()+'。';
  if(/^要不要$/.test(t)||/^是否要$/.test(t)) return '要。';
  if(/吗$/.test(t)) return t.replace(/吗$/,'')+'。';
  return '确认，'+t+'。';
}
function nhConfirmText(e){
  var cause=(e.cause||'').trim();
  var act=(e.act||'').trim();                 /* 要你拍板的那件事，优先 */
  var what=act||(e.summary||e.about||'').trim();
  var s;
  if(act){
    /* 待确认问句：默认填成肯定的回复（用户 2026-11-09：点确认就是要干，别让我再打字） */
    s=nhAffirm(act);
  }else if(e.kind==='perm'||e.kind==='done') s='我确认，继续执行'+(what?('：'+what):'。');
  else s='我来回复'+(what?('（'+what+'）'):'')+'：';
  /* 起因放前面：回复本身已经是完整一句，起因作为背景垫在上一行（2026-11-09 用户要求换序） */
  return cause?('起因：'+cause+'\n'+s):s;
}
function nhConfirm(i){
  var e=(NH.items||[])[i]; if(!e) return;
  if(!e.sid){ toast('这条提醒没有关联会话',true); return; }
  var msg=nhConfirmText(e);
  NH.pend={n:e.n,sid:e.sid,at:Date.now()};
  nhClose();
  Promise.resolve(switchTo(e.sid)).then(function(){
    return dlgFirst(e.sid).then(function(hit){
      /* 电脑屏幕上真有选项弹层：停在实时页让用户点它（dlgPick 会现读屏幕核对），
         别再往输入框里塞一句「我确认，继续执行」。 */
      if(hit) return;
      /* 点过就不留按钮：这条在页面上立刻按「已确认」处理（下次打开历史也不再出现），
         别让一个点过的确认按钮一直挂在那里（2026-11-09 用户要求）。 */
      e.state='sent'; markSent(e.n);
      var t=document.getElementById('txt');
      if(t){ t.value=msg; try{fitTxt();}catch(_){}; try{t.focus();}catch(_){} }
      toast('已带进输入框，确认后点发送');
    });
  }).catch(function(){ toast('打开会话失败',true); });
}
var SENT_AT={}, SENT_N=0;
/* 标「已确认 · 等结果」：同一行 5 秒内只报一次（历史按钮/会话页浮条/发送可能同时触发） */
function markSent(n){
  if(!n) return;
  var k=String(n);
  if(SENT_AT[k]&&Date.now()-SENT_AT[k]<5000) return;
  if(++SENT_N>200){ SENT_AT={}; SENT_N=0; }
  SENT_AT[k]=Date.now();
  try{ xj('/api/notify_act',{method:'POST',body:{n:n,act:'sent'}}).catch(function(){}); }catch(e){}
}
function nhMarkSent(){
  var pd=NH.pend; if(!pd||pd.sid!==SID) return;
  NH.pend=null;
  if(Date.now()-(pd.at||0)>600000) return;   /* 十分钟前点的：不算这次发送 */
  var it=(NH.items||[]).find(function(x){ return x.n===pd.n; });
  if(it) it.state='sent';
  markSent(pd.n);
  if(PEND[pd.sid]){ delete PEND[pd.sid]; pendRender(); }
}
/* ── 会话页底部浮条：这一轮在等你时，不用再去提醒历史翻 ──────────────────
   点「确认/回复」和提醒历史是同一个动作：把起因、上一轮结果和待确认的动作带进
   输入框，你自己看一眼、改完再发送。不替你按任何键——电脑上有选项弹层时仍然
   直接点那个选项（dlgPick），这里只解决「模型用文字问你要不要继续」这种情况。 */
/* 这条「等你」是不是用户刚发完言之后的直接后续（ts 秒；缺省用当前时刻） */
function pendQuiet(sid,ts){
  var sa=SEND_AT[sid]||0; if(!sa) return false;
  var t=ts?ts*1000:Date.now();
  return t>=sa&&t-sa<PEND_QUIET_MS;
}
function nPendDbg(m){
  try{ window.__cbDbg&&window.__cbDbg(m,24); }catch(_){}
}
function pendNote(d){
  var k=d.kind||'';
  /* 「完成」但带着待确认事项（act）也算在等你：na 判断会漏，act 是更具体的信号 */
  if(k==='done'&&(d.act||'').trim()) k='perm';
  if(k==='perm'||k==='ask'||k==='input'){
    /* 已经点过「确认」的那条（SENT_AT 里记过 n）：事件流重连会把它重放一遍，
       不放这一道就会把浮条又挂回来（2026-11-09 用户报「发完消息按钮还在」）。 */
    var nn=d.nh||0;
    if(nn&&SENT_AT[String(nn)]){
      if(PEND[d.sid]&&PEND[d.sid].n===nn){ delete PEND[d.sid]; pendRender(); }
      nPendDbg('PoldN '+String(d.sid||'').slice(0,8));
      return;
    }
    /* 事件比用户最后一次发言还早 = 那句话发出时就已经回过了：
       不管隔了多久重放过来，都不该再弹（pendQuiet 只管发言后 2 分钟）。 */
    var sa=SEND_AT[d.sid]||0;
    if(sa&&d.ts&&d.ts*1000<sa){
      if(PEND[d.sid]){ delete PEND[d.sid]; pendRender(); }
      nPendDbg('PoldT '+String(d.sid||'').slice(0,8)+' '+Math.round((sa-d.ts*1000)/1000)+'s');
      return;
    }
    /* 这条问句紧跟在用户自己发言之后：人就在消息流里，回话也是直接打，
       浮条弹出来只会一直挡着，不记（用户 2026-11-09）。 */
    if(pendQuiet(d.sid,d.ts)){
      delete PEND[d.sid]; pendRender();
      nPendDbg('Psup '+String(d.sid||'').slice(0,8)+' '+Math.round((Date.now()-SEND_AT[d.sid])/1000)+'s');
      return;
    }
    PEND[d.sid]={n:d.nh||0,kind:k,text:d.text||'',cause:d.cause||'',
                 about:d.about||'',summary:d.summary||'',act:d.act||'',ts:d.ts||0,
                 rid:d.rid||'',title:d.title||'',
                 ask:d.ask||null,risk:d.risk||null,why:d.why||'',
                 options:d.options||null,tool:d.tool||''};
  }else if(k==='done'){
    delete PEND[d.sid];        /* 这一轮结束了：之前那句「等你」自动作废 */
  }else{
    return;
  }
  pendRender();
}
/* 在选什么：AskUserQuestion 这类有具体选项（不是「批准/拒绝」）的是「选一项」，
   和普通权限的「确认」、跑完问你的「回话」是三件事，按钮和行文都要分开。 */
function pendPick(it){
  var os=(it&&it.options)||[];
  if(!os.length||(os.length===2&&String(os[0].id||'')==='allow')) return [];
  var out=[];
  os.forEach(function(o){ var t=String((o&&o.label)||'').trim(); if(t) out.push(t); });
  return out;
}
function pendRender(){
  var b=document.getElementById('pendBar'); if(!b) return;
  var e=PEND[SID];
  /* 和收件箱「待你决定」卡同一套说法（2026-11-14）：
     有结构化输入 → 「动作：对象」+ ⚠ 风险 + 哪个会话/起因；
     没有（这轮跑完在问你）→ 用 act 那句，其次结果概括。
     不加「等你确认」这类前缀，整条留给要确认的内容本身；没内容不出（只剩按钮等于没说）。 */
  var a=(e&&e.ask)||{}, risk=(e&&e.risk&&e.risk[0])||null;
  /* 屏幕上真有可点的选项：在弹层里点就行，手机再浮一条按钮只会跟它打架。
     其他情况（权限确认、跑完在问你、选项已经不在屏幕上）照旧浮（2026-11-14 用户）。
     注意：判据是「屏幕上现在有没有选项」，不是「这条是不是选择题」——
     弹层已经过去/根本没显示时，还是得靠这条浮条提醒。 */
  var opt=pendPick(e);
  if(opt.length&&DLG_OPT_ON){ b.classList.remove('on'); return; }
  var what=(opt.length?(a.target||a.action||'')
    :((a.action||'')+(a.target?('：'+a.target):''))).replace(/\s+/g,' ').trim();
  if(!what) what=(e?(e.act||e.summary||e.about||''):'').replace(/\s+/g,' ').trim();
  var on=!!(e&&pageIdx===1&&what);
  b.classList.toggle('on',on);
  if(!on) return;
  b.style.borderColor=(risk&&risk.level==='high')?'rgba(248,81,73,.45)':'';
  var why=(e.why||e.cause||'').replace(/\s+/g,' ').trim().slice(0,40);
  var meta=(e.title||'')+(why?((e.title?' · ':'')+'起因：'+why):'');
  document.getElementById('pendTxt').innerHTML=
    '<div class="pWhat">'+esc(what)+'</div>'+
    (risk?('<div class="pRisk">⚠ '+esc(risk.text||'')+'</div>'):'')+
    (meta?('<div class="pMeta">'+esc(meta)+'</div>'):'');
  /* 选择题但选项不在屏幕上 → 去选（切回实时页看电脑画面）；权限确认 → 确认；在问你 → 回复 */
  document.getElementById('pendBtn').textContent=
    opt.length?'去选':(e.rid?'确认':'回复');
}
function pendGo(){
  var e=PEND[SID]; if(!e) return;
  /* 选择题：作答就是点屏幕上那个弹层，别往输入框塞一句「我确认」 */
  if(pendPick(e).length){
    try{ setView('live'); }catch(_){}
    try{ toBottom(); }catch(_){}
    toast('在电脑屏幕的弹层里点一下选项');
    return;
  }
  NH.pend={n:e.n,sid:SID,at:Date.now()};
  dlgFirst(SID).then(function(hit){
    /* 弹层在屏幕上：浮条留着，让用户点选项；dlgPick 成功后会 pendSent 收掉 */
    if(hit) return;
    var t=document.getElementById('txt');
    if(t){ t.value=nhConfirmText(e); try{fitTxt();}catch(_){}; try{t.focus();}catch(_){} }
    /* 点完浮条立刻收起来（用户 2026-11-09：按钮不要一直留在页面上）：
       内容已经在输入框里，浮条再挡着只会重复。这一条同时按「已确认」落库。 */
    delete PEND[SID]; pendRender();
    markSent(e.n);
    toast('已带进输入框，确认后点发送');
  });
}
/* 用户自己回话了（不是点浮条进来的也算）：这一条就算处理过，浮条收掉 */
function pendSent(){
  var e=PEND[SID]; if(!e) return;
  delete PEND[SID]; pendRender();
  var pd=NH.pend;
  if(!(pd&&pd.n===e.n)) markSent(e.n);
}
/* 重开页面/切会话：从最近的提醒历史里把「还在等你」那条找回来（6 小时内） */
async function pendLoad(){
  if(!SID) return;
  try{
    var r=await xj('/api/notify_hist?limit=5&sid='+encodeURIComponent(SID));
    var items=(r.json&&r.json.items)||[];
    var e=items[0]||null, now=Date.now()/1000;
    var pendKind=(e?(e.kind||''):'');
    if(pendKind==='done'&&(e.act||'').trim()) pendKind='perm';   /* 完成但有待确认事项 */
    if(e&&(pendKind==='perm'||pendKind==='ask'||pendKind==='input')
       &&!e.state&&now-(e.ts||0)<6*3600
       &&!((SEND_AT[SID]||0)&&e.ts&&e.ts*1000<SEND_AT[SID])){
      if(pendQuiet(SID,e.ts)){
        /* 这一问是用户刚才那句话的直接回应：重新进会话也不补弹浮条 */
        delete PEND[SID];
        nPendDbg('PsupL '+String(SID||'').slice(0,8));
      }else{
        PEND[SID]={n:e.n,kind:pendKind,text:e.text||'',cause:e.cause||'',
                   about:e.about||'',summary:e.summary||'',act:e.act||'',ts:e.ts||0,
                   rid:e.rid||'',title:e.title||''};
      }
    }else{
      delete PEND[SID];
    }
    pendRender();
    /* 结构化的「在确认什么」只在队列内存里（30 分钟），历史表没有：和收件箱
       用同一份 /api/ext_pending 补上，浮条才能像待你决定卡那样写动作 + 对象。 */
    if(PEND[SID]){
      try{
        var r2=await xj('/api/ext_pending',{away:true,timeout:4000,dcto:4000});
        var pi=((r2.json&&r2.json.items)||[]).find(function(x){return x.sid===SID;});
        if(pi&&PEND[SID]){
          PEND[SID].ask=pi.ask||null; PEND[SID].risk=pi.risk||null;
          PEND[SID].why=pi.why||''; PEND[SID].title=pi.title||PEND[SID].title;
          PEND[SID].options=pi.options||null; PEND[SID].tool=pi.tool||'';
          pendRender();
        }
      }catch(_){}
    }
  }catch(e){}
}
/* 壳的「息屏提醒」开关（页面设置里同一份配置）。读不到就当成没开，
   让页面自己响——宁可多响一声，也别该响的时候没声。 */