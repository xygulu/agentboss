// SPDX-License-Identifier: AGPL-3.0-or-later

/* 壳键盘上推时把位移量同步过来：header 用 --ime 单独平移回屏幕顶（见 header CSS） */
window.__imeShift=function(px){ px=Number(px)||0;
  /* 壳偶尔会算出荒唐的位移（实测报过 968px，浮层 top 直接跑到屏幕外 → 一片黑）：不合理就当 0 */
  if (px<0 || px>((window.innerHeight||800)*0.75)) px=0;
  try{ document.documentElement.style.setProperty('--ime',px+'px'); }catch(e){}
  try{ kbLayout(); }catch(e){}      /* 会话页/其它页摆法不一样，顶栏和 body 重新算 */
  try{ termSyncIme(); }catch(e){}   /* 浮层底边贴键盘上沿、高度按剩余空间压缩，收起恢复 */
  /* 终端在 iframe 里，外壳的整页上推它收不到：透传进去，终端页自己补偿 */
  try{ var f=document.getElementById('termFrame'); if(f&&f.contentWindow&&f.contentWindow.__imeShift) f.contentWindow.__imeShift(px); }catch(e){}
};
window.__cbEyeSys=function(){ try{ applyEyeClass(vsCfg().eye===true); }catch(e){} };   /* 壳悬浮层状态变化时回叫（授权回来等） */
(function(){   // 启动即应用护眼/常亮，不等用户进设置页
  try{ const c=vsCfg(); document.body.classList[(c.eye===true)?'add':'remove']('eye');
       if(window.CB&&CB.setKeepOn) CB.setKeepOn(c.keepon===true); }catch(e){}
})();
/* ── 设置页请求层：一律 XHR（ES5 风格也照顾老 WebView） ── */
function xget(ep,cb){
  if(awayNow()) return;   // 后台不发请求（回前台各视图会自己补拉）
  /* charged：这一条请求有没有被算进使用时长（没带 a=0）。/api/plan 靠它判断
     返回的 used 里包不包含「当前这一分钟」，读秒的锚点必须用「已计费」的那次。 */
  const charged=Date.now()-LASTACT<ACT_MS;
  ep=actTag(ep);          // 挂着看：不参与使用时长
  const x=new XMLHttpRequest();
  x.open('GET',ep,true);
  x.onreadystatechange=function(){ if(x.readyState!==4)return;
    if((x.status>=200&&x.status<400)||x.status===403) LASTOK=Date.now();
    let d=null; try{d=JSON.parse(x.responseText)}catch(e){}
    cb(d,x.status,x.responseText,{charged:charged,hdr:function(n){
      try{return x.getResponseHeader(n)}catch(e){return null}}}); };
  x.send();
}
function xpost(ep,body,cb){
  if(awayNow()) return;   // 后台不发请求
  const x=new XMLHttpRequest();
  x.open('POST',ep,true);
  x.setRequestHeader('Content-Type','application/json');
  x.onreadystatechange=function(){ if(x.readyState!==4)return; let d={}; try{d=JSON.parse(x.responseText)}catch(e){} cb(d,x.status); };
  x.send(JSON.stringify(body));
}
/* ── 网络：状态一行说清；WiFi 开关原生切；系统面板只在有浮层的壳上出现 ── */
function vsNetRefresh(){
  let st=null;
  try{ if(window.CB && CB.netState) st=JSON.parse(CB.netState()); }catch(e){}
  const card=document.getElementById('vsNetCard');
  if(!st){ card.style.display='none'; return; }        // 浏览器模式：控制不了网络，整卡藏掉
  card.style.display='';
  document.getElementById('vsNet').textContent=
    (st.wifi?('WiFi'+(st.ssid?' · '+st.ssid:'')):(st.wifiOn?'WiFi 未连接':(st.mobile?'移动数据':'离线')));
  /* Android 10+ 读 SSID 要定位权限：没授权时给个「读取名称」按钮，而不是干显示 WiFi */
  const fix=document.getElementById('vsNetFix');
  if(fix) fix.style.display=(st.wifi&&!st.ssid&&st.locPerm!=='1')?'':'none';
}
function vsNetFixPerm(){
  try{ if(window.CB&&CB.requestLocation){ CB.requestLocation(); show('允许定位后返回，WiFi 名称就能显示'); return; } }catch(e){}
  show('当前版本不支持，请升级客户端');
}
function vsRefresh(){
  try{ pageRemember(pageIdx); }catch(e){}
  try{ if(window.CB && CB.refresh){CB.refresh();return;} }catch(e){}
  location.reload();
}
/* ── 账号：用户名/手机/邮箱从 /api/plan 的 account 取（中继自己维护，脱敏后的值）；
   老中继没有 account 字段时退回抓 /r/account 页面。表单内联完成，不跳页 ── */
var vsBcd=0, vsPcd=0, ACC={name:'',email:'',phone:''};
/* 账号操作走底部抽屉：设置页只留入口，不铺表单 */
function accOpen(n){
  var id = n===1?'pwSheet':(n===2?'beSheet':'phSheet');
  var el=document.getElementById(id);
  var er = document.getElementById(n===1?'pwErr':(n===2?'beErr':'phErr')); if(er) er.textContent='';
  if(n===2){
    var t=document.getElementById('beTitle');
    if(t) t.textContent=(ACC.email&&ACC.email!=='未绑定')?'换绑邮箱':'绑定邮箱';
  }
  if(n===3){
    var t2=document.getElementById('phTitle');
    if(t2) t2.textContent=(ACC.phone&&ACC.phone!=='未绑定')?'换绑手机':'绑定手机';
  }
  if(el) el.className='on';
}
function accClose(n){ var el=document.getElementById(n===1?'pwSheet':(n===2?'beSheet':'phSheet')); if(el) el.className=''; }
function accPaint(){
  var ph=(ACC.phone&&ACC.phone!=='未绑定')?ACC.phone:'未绑定';
  var em=(ACC.email&&ACC.email!=='未绑定')?ACC.email:'未绑定';
  document.getElementById('vsAcctName').textContent=ACC.name||'未登录';
  document.getElementById('vsAcctPhone').textContent=ph;
  document.getElementById('vsAcctEmail').textContent=em;
  /* 没绑定就直接给「绑定」按钮，不和「换绑」混在一个下拉里 */
  document.getElementById('vsAcctPhoneBtn').innerHTML=
    '<button class="gray mini" onclick="accOpen(3)">'+(ph==='未绑定'?'绑定':'换绑')+'</button>';
  document.getElementById('vsAcctEmailBtn').innerHTML=
    '<button class="gray mini" onclick="accOpen(2)">'+(em==='未绑定'?'绑定':'换绑')+'</button>';
}
function acctLoad(){
  const card=document.getElementById('vsAccCard');
  const out=document.getElementById('vsOutCard');
  xget('/api/plan',function(d,st){
    if(st===200&&d&&d.ok&&d.anon){
      /* 匿名设备（免注册免费版）：只显示本机名，绑定/退出都藏起来，
         开通入口走账号卡下面的套餐页 */
      ACC={name:String((d.account&&d.account.name)||(d.device&&d.device.name)||'本机'),
           email:'',phone:''};
      card.style.display=''; out.style.display='none';
      document.getElementById('vsAcctName').textContent=ACC.name+'（免费版）';
      document.getElementById('vsAcctPhone').textContent='—';
      document.getElementById('vsAcctEmail').textContent='—';
      document.getElementById('vsAcctPhoneBtn').innerHTML='';
      document.getElementById('vsAcctEmailBtn').innerHTML='';
      return;
    }
    if(st===200&&d&&d.ok&&d.account){
      ACC={name:String(d.account.name||''),email:String(d.account.email||''),
           phone:String(d.account.phone||'')};
      card.style.display=''; out.style.display='';
      accPaint();
      return;
    }
    /* 老中继没有 account 字段：退回抓账号页（HTML 原文） */
    xget('/r/account',function(d2,st2,raw){
      if(st2!==200){ card.style.display='none'; out.style.display='none'; return; } // LAN 直连：没有中继账号接口
      card.style.display=''; out.style.display='';
      const s=String(raw||'');
      const m=s.match(/账号：<b>([^<]*)<\/b>/)||s.match(/用户名：<b>([^<]*)<\/b>/);
      const e=s.match(/绑定邮箱：([^\s<]+)/);
      const p=s.match(/绑定手机：([^\s<]+)/);
      ACC={name:(m?m[1]:''),
           email:(e&&e[1]!=='未绑定'?e[1]:''),
           phone:(p&&p[1]!=='未绑定'?p[1]:'')};
      accPaint();
    });
  });
}
/* ── 已登录客户端：列表 + 踢下线（短信/邮箱二次验证）──
   走 host 代理 /r/account_* → 中继设备自证接口；限额页用的是同一套中继接口（用户会话版）。 */
let ACCT2={d:null,via:'',cid:''};
function acctSelfCid(){
  try{ const m=document.cookie.match(/(?:^|;\s*)cb_cid=([^;]+)/);
       return m?decodeURIComponent(m[1]):''; }catch(e){ return ''; }
}
function acctClose(){ document.getElementById('acctsheet').classList.remove('on'); }
async function acctOpen(){
  ACCT2={d:null,via:'',cid:''};
  const box=document.getElementById('acctBox2');
  box.innerHTML='<div class="meta">读取中…</div>';
  document.getElementById('acctsheet').classList.add('on');
  try{
    const r=await xj('/r/account_info',{method:'POST',body:{cid:acctSelfCid()}});
    const d=r.json||{};
    if(!d.ok){box.innerHTML='<div class="meta">'+escHtml(d.error||'读取失败')+'</div>';return;}
    ACCT2.d=d; acctRender();
  }catch(e){ box.innerHTML='<div class="meta">网络错误</div>'; }
}
function acctRender(){
  const d=ACCT2.d||{}; const box=document.getElementById('acctBox2'); if(!box) return;
  const list=d.clients||[];
  let h='<div class="meta" style="margin-bottom:6px">最近 24 小时用过 '+(d.used||0)+' / '+(d.cap||'—')+
        ' 个客户端；超过 24 小时没连的会自动释放</div>';
  if(!list.length) h+='<div class="meta">还没有登记过的客户端</div>';
  list.forEach(function(c,i){
    const ago=(Date.now()/1000-(c.last||0))/60;
    const when=ago<60?(Math.max(0,Math.round(ago))+' 分钟前'):
               (ago<1440?(Math.round(ago/60)+' 小时前'):(Math.round(ago/1440)+' 天前'));
    h+='<div class="trow"><div style="min-width:0;flex:1"><div style="font-weight:600">'+
       escHtml(c.label||(c.cid||'').slice(0,8))+'</div><div class="meta">'+
       (c.current?'本机 · ':'')+(c.counted?'占名额':'不占名额')+' · '+when+'</div></div>'+
       (c.current?'<span class="meta">当前</span>':
         '<button class="gray mini" data-kick="'+i+'">踢下线</button>')+'</div>';
  });
  h+='<div id="acctVerify2" style="display:none;margin-top:8px"></div>';
  box.innerHTML=h;
  box.querySelectorAll('[data-kick]').forEach(function(b){
    b.addEventListener('click',function(){ acctKick(list[+b.getAttribute('data-kick')]); });
  });
}
function acctKick(c){
  if(!c) return;
  ACCT2.cid=c.cid;
  const d=ACCT2.d||{};
  const v=document.getElementById('acctVerify2'); if(!v) return;
  v.style.display='block';
  v.innerHTML='<div class="meta" style="margin-bottom:6px">踢下线需要二次验证，验证码会发到账号绑定的手机号或邮箱</div>'+
    '<div class="row" style="gap:8px;flex-wrap:wrap">'+
    (d.has_phone?'<button class="gray mini" data-via="sms">发短信验证码</button>':'')+
    (d.has_email?'<button class="gray mini" data-via="email">发邮箱验证码</button>':'')+
    '</div><div class="row" style="gap:8px;margin-top:8px">'+
    '<input id="acctCode" inputmode="numeric" maxlength="6" placeholder="6 位验证码" style="flex:1">'+
    '<button data-do="1">确认踢下线</button></div>'+
    '<div class="meta" id="acctMsg" style="margin-top:6px"></div>';
  ACCT2.via=d.has_phone?'sms':(d.has_email?'email':'');
  v.querySelectorAll('[data-via]').forEach(function(b){
    b.addEventListener('click',function(){ acctSend(b.getAttribute('data-via')); });
  });
  v.querySelector('[data-do]').addEventListener('click',acctDo);
}
async function acctSend(via){
  ACCT2.via=via;
  const m=document.getElementById('acctMsg'); if(m) m.textContent='发送中…';
  try{
    const r=await xj('/r/account_send_code',{method:'POST',body:{cid:ACCT2.cid,via:via}});
    const d=r.json||{};
    if(m) m.textContent=d.ok?(d.msg||'验证码已发送'):('发送失败：'+(d.error||''));
  }catch(e){ if(m) m.textContent='网络错误'; }
}
async function acctDo(){
  const m=document.getElementById('acctMsg'); const el=document.getElementById('acctCode');
  const code=el?el.value.trim():'';
  if(!ACCT2.via){ if(m) m.textContent='先点一个「发验证码」'; return; }
  if(m) m.textContent='验证中…';
  try{
    const r=await xj('/r/account_kick',{method:'POST',body:{cid:ACCT2.cid,cur:acctSelfCid(),code:code,via:ACCT2.via}});
    const d=r.json||{};
    if(d.ok){
      toast('已踢下线，名额已释放');
      ACCT2.d=d; acctRender();
      try{ acctLoad(); }catch(e){}
    }else if(m) m.textContent='失败：'+(d.error||'');
  }catch(e){ if(m) m.textContent='网络错误'; }
}
function vsSendCode(){
  if(vsBcd>0)return;
  xpost('/r/send_code',{purpose:'bind',email:document.getElementById('vsBe').value.trim()},function(d){
    const err=document.getElementById('beErr');
    if(!d.ok){ err.textContent=d.error||'发送失败'; return; }
    err.textContent='';
    show(d.msg||'验证码已发送，去邮箱看');
    vsBcd=60;
    const b=document.getElementById('vsBcb');
    b.disabled=true; b.textContent='60s';
    const t=setInterval(function(){ vsBcd--; b.textContent=vsBcd>0?vsBcd+'s':'发验证码';
      if(vsBcd<=0){clearInterval(t);b.disabled=false;} },1000);
  });
}
function vsBind(){
  track('bind_email');
  xpost('/r/bind_email',{email:document.getElementById('vsBe').value.trim(),code:document.getElementById('vsBc').value.trim()},function(d){
    const err=document.getElementById('beErr');
    if(d.ok){ err.textContent=''; document.getElementById('vsBc').value=''; accClose(2); show('邮箱已绑定'); acctLoad(); }
    else err.textContent=d.error||'绑定失败';
  });
}
/* 绑定/换绑手机：先发短信验证码（purpose=bind），再带码换绑 */
function vsSendSms(){
  if(vsPcd>0) return;
  var phone=document.getElementById('vsPh').value.trim();
  var err=document.getElementById('phErr');
  if(!/^1[3-9]\d{9}$/.test(phone)){ err.textContent='手机号格式不正确'; return; }
  xpost('/r/send_sms',{purpose:'bind',phone:phone},function(d){
    if(!d.ok){ err.textContent=d.error||'发送失败'; return; }
    err.textContent='';
    show(d.msg||'验证码已发送，去手机短信看');
    vsPcd=60;
    const b=document.getElementById('vsPcb');
    b.disabled=true; b.textContent='60s';
    const t=setInterval(function(){ vsPcd--; b.textContent=vsPcd>0?vsPcd+'s':'发码';
      if(vsPcd<=0){clearInterval(t);b.disabled=false;} },1000);
  });
}
function vsBindPhone(){
  var phone=document.getElementById('vsPh').value.trim();
  var code=document.getElementById('vsPc').value.trim();
  var err=document.getElementById('phErr');
  if(!/^1[3-9]\d{9}$/.test(phone)){ err.textContent='手机号格式不正确'; return; }
  if(!code){ err.textContent='填一下短信验证码'; return; }
  track('bind_phone');
  xpost('/r/bind_phone',{phone:phone,code:code},function(d){
    if(d.ok){ err.textContent=''; document.getElementById('vsPc').value=''; accClose(3);
      show('手机已绑定'); acctLoad(); }
    else err.textContent=d.error||'绑定失败';
  });
}
function vsPass(){
  const err=document.getElementById('pwErr'),
        op=document.getElementById('vsOp'), np=document.getElementById('vsNp'),
        np2=document.getElementById('vsNp2');
  const nv=np.value||'';
  if(nv.length<6){ err.textContent='新密码至少 6 位'; return; }
  if(nv!==(np2.value||'')){ err.textContent='两次输入的新密码不一致'; np2.value=''; np2.focus(); return; }
  err.textContent='';
  xpost('/r/changepass',{old:op.value,new:nv},function(d){
    if(d.ok){ op.value=''; np.value=''; np2.value=''; accClose(1); show('密码已改，其他设备已下线'); }
    else err.textContent=d.error||'修改失败';
  });
}
/* 退出登录（原主页「退出登录」；两击确认防误触，WebView 弹不出 confirm） */
function vsLogout(btn){
  if(!btn){
    var b=event&&event.target; if(b&&b.tagName==='BUTTON') btn=b;
  }
  if(btn&&!btn.__arm){
    btn.__arm=1; btn.textContent='再点一次退出';
    setTimeout(function(){ if(btn){btn.__arm=0;btn.textContent='退出登录';} },4000);
    return;
  }
  location='/r/logout';
}
/* ── App 内页（套餐 / 条款 / 隐私）：盖一层 iframe，不离开当前页 ──────────
   以前是整页跳走再返回，设置页得重新加载、还落在别的页。现在「返回」只是收起
   这一层，设置页原样还在（滚动位置、开关状态都不动）。 */
function appBase(){
  try{ if(PLAND&&PLAND.relay_base) return String(PLAND.relay_base); }catch(e){}
  return location.origin||'';
}
function sheetOpen(url,title){
  var s=document.getElementById('cbSheet'), f=document.getElementById('cbSheetF');
  var same=false;
  try{ same=!!location.origin&&String(url).indexOf(location.origin+'/')===0; }catch(e){}
  /* 只有和中继同源（App 正常走中继）才盖层。LAN 直连跨站时 iframe 带不上中继登录
     cookie，退回原来整页打开；emb 也去掉，那一页照旧画自己的「返回」。 */
  if(!s||!f||!same){
    location.href=String(url).replace(/([?&])emb=1/,'').replace(/[?&]$/,'');
    return;
  }
  document.getElementById('cbSheetT').textContent=title||'';
  s.classList.add('on');
  try{ f.__pvAt=0; }catch(e){}
  f.src=url;
}
function sheetClose(){
  var s=document.getElementById('cbSheet'), f=document.getElementById('cbSheetF');
  if(!s||!f) return;
  /* 预览：点进某个端口后「返回」先回端口列表，别一下收起落回会话页。
     iframe 走的是本站 /pv/<端口>/…，同源，读得到它的当前路径。
     连点两下：第一下已经把 iframe 送去端口列表，可它还没加载完，再读一次路径
     还是 /pv/<端口>/，看着像退不出来——刚送过去（1.5 秒内）就直接收起。 */
  try{
    var p=(f.contentWindow&&f.contentWindow.location.pathname)||'';
    if(/^\/pv\/\d+(\/|$)/.test(p)){
      if(!(f.__pvAt&&Date.now()-f.__pvAt<1500)){
        f.__pvAt=Date.now();
        f.src='/pv';
        document.getElementById('cbSheetT').textContent='预览';
        return;
      }
    }
  }catch(e){}
  f.src='about:blank';                            /* 停掉里面的轮询/加载 */
  s.classList.remove('on');
}
/* 中继页面在 iframe 里跳 alipays:// 壳不一定接管：交给顶层页面跳（壳会拦下来唤起支付宝） */
window.addEventListener('message',function(e){
  var d=e&&e.data;
  if(typeof d!=='string') return;
  if(d==='cb-close'){ sheetClose(); return; }
  if(d.indexOf('cb-alipay:')===0){
    var u=d.slice(10);
    if(u) location.href=u;
  }
});
/* 隐私政策 / 服务条款：App 里盖一层打开（返回只收起这层，设置页原样还在），
   浏览器里退回整页打开。中继页 ?emb=1 会把自己那条「返回」藏掉。 */
function legalOpen(k){
  var t=(k==='privacy'?'隐私政策':'服务条款');
  sheetOpen((appBase()||'')+'/r/'+(k==='privacy'?'privacy':'terms')+'?emb=1',t);
}
/* ── 开发机：会话列表页顶部的「选台 + 接入」（原设置页搬到这） ── */
function devLoad(){
  var bar=document.getElementById('devBar');
  if(bar) bar.style.display='flex';   // 先亮出来：接口慢/失败时也不该整条消失
  xget('/r/devices',function(d,st){
    DEV.init=true;
    bar.style.display='';
    /* 本机还没绑定账号 = 账户下一台可用的开发机都没有：按零设备处理，
       收起终端/模型、露出「绑定开发机」。不能当成"拿不到设备表"（那会保留旧界面）。 */
    if(d && !d.ok && String(d.error||'').indexOf('未绑定账号')>=0){
      DEV.list=[]; DEV.base=d.relay_base||DEV.base; DEV.na=false;
      var s0=document.getElementById('devSel');
      if(s0) s0.innerHTML='<option value="">未绑定开发机</option>';
      var e0=document.getElementById('devEditOpenBtn'); if(e0) e0.disabled=true;
      applyPlanUI();
      return;
    }
    if(st!==200||!d||!d.ok){                       // 浏览器/局域网：没有设备接口，只留设置入口
      DEV.na=true;                                 // 拿不到设备表≠没有设备，别按"零设备"收起界面
      ['devSel','devAddBtn','devEditOpenBtn'].forEach(function(id){
        var e=document.getElementById(id); if(e) e.style.display='none';
      });
      applyPlanUI();
      return;
    }
    var cur=curDev(), list=d.devices||[], sel=document.getElementById('devSel');
    DEV.list=list;
    DEV.base=d.relay_base||'';   /* host 代理返回中继地址：切台跳中继 /d（本页是 host 源） */
    var h='';
    if(!list.length) h='<option value="">未绑定开发机</option>';
    else{
      if(!cur) h+='<option value="">选择开发机…</option>';
      list.forEach(function(v){
        var tag=(v.id===cur)?' · 当前':(v.online?' · 在线':' · 离线');
        /* 列表页不显示开发机版本号：选台要看的是「哪台、通不通」，
           版本号在设置页的「开发机服务」里看就够了 */
        h+='<option value="'+escHtml(v.id)+'"'+(v.id===cur?' selected':'')+'>'
          +escHtml(v.name||v.id.slice(0,8))+tag+(v.frozen?' ❄':'')+'</option>';
      });
    }
    sel.innerHTML=h;
    var e=document.getElementById('devEditOpenBtn');  // 没设备时不给编辑入口
    if(e) e.disabled=list.length===0;
    var q=document.getElementById('vsQuota'); if(q) q.textContent=d.quota||'';
    applyPlanUI();   /* 有/无设备决定模型行与新建终端露不露出 */
  });
}
var DEV={list:[],init:false,base:"",na:false};
/* 下拉切台＝换 cb_dev cookie 再进控制页（与 relay 的 enter 同语义） */
function devSwitch(sel){
  devGoto(sel.value);
}
/* 切台（下拉框和「别的机器上的任务」共用）：种 cb_dev 再进那台机器的列表页 */
function devGoto(id){
  if(!id||id===curDev()) return;
  track('dev_switch');       /* 下拉切台和远程行共用这条路：都算「切开发机」 */
  document.cookie='cb_dev='+id+';path=/;max-age=2592000;samesite=lax';
  /* 局域网直连（host 源）时 /d 不在本页源上，切台要去中继入口。
     cookie 在直连源是 IP 域的，跳到中继域名就读不到了 → query 带上设备，
     中继校验归属后种 cookie（2026-11-18：安卓家里 WiFi 切台后内容全是旧机器的）。 */
  var base=DEV.base||(PLAND&&PLAND.relay_base)||'';
  if(base){ location=base+'/d?dev='+encodeURIComponent(id); return; }
  /* base 为空（devLoad 还没回来/失败）时绝不能相对跳 /d——host 源上没有这个
     路由，405 原地打转，看起来就是「切台没反应」（2026-11-18 安卓实测）。
     先问一次设备表拿中继地址；拿不到就报错，宁可不动也不原地转圈。 */
  xget('/r/devices',function(d){
    var b=(d&&d.relay_base)||(PLAND&&PLAND.relay_base)||'';
    if(!b){ toast('切台失败：拿不到中继地址',true); return; }
    location=b+'/d?dev='+encodeURIComponent(id);
  });
}
/* 当前选中的开发机（cb_dev cookie） */
function curDev(){
  var m=/(?:^|;\s*)cb_dev=([^;]+)/.exec(document.cookie||'');
  return m?m[1]:'';
}
function devPairOpen(){
  track('pair');
  try{ vsPairStop(); }catch(e){}     /* 上一次的等待框/轮询别带进新的一次扫码 */
  document.getElementById('vsDevErr').textContent='';
  document.getElementById('vsPairCode').value='';
  document.getElementById('psheet').classList.add('on');
}
function devPairClose(){
  try{ vsPairStop(); }catch(e){}
  document.getElementById('psheet').classList.remove('on');
}
/* 扫码接入：页面是 http（非安全上下文），getUserMedia 用不了 → 交给壳里的原生扫码页 */
function devScan(){
  if(!window.CB||!CB.scanQr){ toast('当前客户端不支持扫码，装新版后再试',true); return; }
  try{ CB.scanQr(); }catch(e){ toast('相机打不开',true); }
}
/* 壳里扫码页回传结果：接受完整配对链接（…?p=CODE）或裸配对码 */
window.__cbQr=function(txt){
  try{
    var s=String(txt||'').trim(), m=/(?:[?&]p=)([A-Za-z0-9_\-]+)/.exec(s), code;
    if(m) code=m[1];
    else if(/^https?:/i.test(s)) code=s.replace(/[?#].*$/,'').replace(/\/+$/,'').split('/').pop();
    else code=s;
    if(!code){ toast('没读到配对码',true); return; }
    if(!document.getElementById('psheet').classList.contains('on')) devPairOpen();
    document.getElementById('vsPairCode').value=code;
    vsPair();
  }catch(e){}
};
window.__cbQrDenied=function(){ toast('没有相机权限：系统设置里允许相机后重试',true); };
function vsPair(){
  var code=document.getElementById('vsPairCode').value.trim(), err=document.getElementById('vsDevErr');
  if(!code){ err.textContent='配对码为空'; return; }
  vsPairStop();
  err.style.color=''; err.textContent='';
  xpost('/r/pair',{code:code},function(d){
    if(d.ok){ devPairClose(); show('已绑定'); devLoad(); }
    else if(d.need_confirm){ vsPairWait(d.req||'', d.device_name||''); }
    else err.textContent=d.error||'绑定失败';
  });
}
/* 机器已在别人账号下：不静默改归属。中继排队等原账号手机点「允许」，
   或直接在那台电脑上点「允许」；10 分钟没人表态就作废，重新扫码再来。 */
var PAIR_WAIT={id:'',t:0,lastPoll:0,iv:0};
function vsPairWait(rid,name){
  var err=document.getElementById('vsDevErr');
  err.textContent=''; err.style.color='';
  var box=document.getElementById('vsTakeover'); if(box) box.remove();
  box=document.createElement('div'); box.id='vsTakeover';
  box.style.cssText='margin-top:8px;font-size:14px;line-height:1.65;color:#ffb900';
  box.innerHTML='⏳ 这台电脑'+ (name?('「'+esc(name)+'」'):'') +
    '已绑定其他账号。<br>已发出接管请求：等原账号在手机 App 上点「允许」，' +
    '或直接在那台电脑上点「允许」。<br><span id="vsTkCd"></span>';
  err.parentNode.insertBefore(box, err.nextSibling);
  PAIR_WAIT={id:rid,t:Date.now(),lastPoll:0,iv:0};
  PAIR_WAIT.iv=setInterval(vsPairTick,1000);
  vsPairTick();
}
function vsPairDone(msg,color){
  vsPairStop();
  var err=document.getElementById('vsDevErr');
  err.style.color=color||'#ff6467'; err.textContent=msg;
}
function vsPairStop(){
  if(PAIR_WAIT.iv) clearInterval(PAIR_WAIT.iv);
  PAIR_WAIT={id:'',t:0,lastPoll:0,iv:0};
  var box=document.getElementById('vsTakeover'); if(box) box.remove();
}
function vsPairTick(){
  if(!PAIR_WAIT.id) return;
  var left=Math.max(0,600-Math.floor((Date.now()-PAIR_WAIT.t)/1000));
  var el=document.getElementById('vsTkCd');
  if(el) el.textContent=(left>0)
    ? '剩余 '+Math.floor(left/60)+':'+('0'+(left%60)).slice(-2)+' 没人确认就作废'
    : '请求已失效，重新扫码再来';
  if(left<=0){ vsPairDone('请求已失效，重新扫码再来'); return; }
  var now=Date.now();
  if(now-PAIR_WAIT.lastPoll<3000) return;
  PAIR_WAIT.lastPoll=now;
  var id=PAIR_WAIT.id;
  xpost('/r/pair_status',{id:id},function(d){
    if(!d||!d.ok||PAIR_WAIT.id!==id) return;
    if(d.status==='approved'){ vsPairStop(); devPairClose(); show('已接管这台电脑'); devLoad(); }
    else if(d.status==='denied'){ vsPairDone('原账号拒绝了这次接管'); }
    else if(d.status==='expired'){ vsPairDone('请求已失效，重新扫码再来'); }
  });
}
/* ── 原账号这一侧：别的账号想把这台开发机接走 ──────────────────────────
   中继也要机器本机确认，这里只是「原账号手机 App」那条路：前台每 20 秒问一次。
   允许/拒绝都要明确点（点遮罩只关弹窗，不表态）。不处理 10 分钟自动作废。 */
var TK_SHOWING='';
function tkPoll(){
  if(hiddenNow()) return;
  xpost('/r/takeover_list',{},function(d){
    if(!d||!d.ok||!d.reqs||!d.reqs.length) return;
    var r=d.reqs[0];
    if(r.id===TK_SHOWING||document.getElementById('dlg').classList.contains('on')) return;
    TK_SHOWING=r.id;
    askConfirm('开发机接管确认',
      '账号「'+esc(r.to_name||'另一个账号')+'」正在请求把「'+esc(r.dev_name||'这台电脑')+
      '」绑定到它的名下。<br><br>允许后这台电脑会从你的账号里移走（机器上的会话和历史不动），'+
      '你将无法再远程控制它。<br>不处理的话 10 分钟后自动作废。',
      '允许',true,
      function(){ tkAct(r.id,1); },
      function(){ tkAct(r.id,0); });
  });
}
function tkAct(id,ok){
  TK_SHOWING='';
  xpost('/r/takeover_act',{id:id,approve:ok?1:0},function(d){
    if(d&&d.ok){ show(ok?'已允许接管':'已拒绝'); if(ok) devLoad(); }
    else show((d&&(d.error||d.msg))||'操作失败',true);
  });
}
setInterval(tkPoll,20000);
setTimeout(tkPoll,5000);
/* ✎ 编辑：改名 / 删除都收在这个弹层里 */
var devEditId='';
function devEditOpen(){
  var id=document.getElementById('devSel').value;
  if(!id){ show('先选一台开发机'); return; }
  var v=null;
  (DEV.list||[]).forEach(function(x){ if(x.id===id) v=x; });
  devEditId=id;
  document.getElementById('devName').value=v?(v.name||''):'';
  document.getElementById('devEditErr').textContent='';
  var b=document.getElementById('devDelBtn'); b.__arm=0; b.textContent='删除';
  document.getElementById('esheet').classList.add('on');
  setTimeout(function(){ try{ document.getElementById('devName').focus(); }catch(e){} },120);
}
function devEditClose(){ document.getElementById('esheet').classList.remove('on'); }
function devSave(){
  var name=document.getElementById('devName').value.trim(), err=document.getElementById('devEditErr');
  if(!name){ err.textContent='名称不能为空'; return; }
  xpost('/r/dev_rename',{id:devEditId,name:name},function(d){
    if(d.ok){ devEditClose(); show('已改名'); devLoad(); }
    else err.textContent=d.error||'改名失败';
  });
}
/* WebView 默认 WebChromeClient 不弹 confirm()（静默 false）——删除用两击确认：
   第一击变"确认删除？"（3 秒内有效），第二击才真删。 */
function devDel(btn){
  var err=document.getElementById('devEditErr');
  if(!btn.__arm){
    btn.__arm=1; btn.textContent='确认删除？';
    setTimeout(function(){ btn.__arm=0; btn.textContent='删除'; },3000);
    return;
  }
  btn.__arm=0; btn.textContent='删除';
  /* 先确认「正在看的这台」是谁：cookie 在局域网/跨源时可能读不到，再补一个
     本机自报的设备号（/api/plan 里 host 直接给 relay 侧的 device_id）。 */
  if(PLAND&&PLAND.ok) devDelPost(err);
  else xget('/api/plan',function(d){ if(d&&d.ok) PLAND=d; devDelPost(err); });
}
function devDelPost(err){
  xpost('/r/dev_del',{id:devEditId},function(d){
    if(!d.ok){ err.textContent=d.error||'删除失败'; return; }
    var served=(PLAND&&PLAND.dev)||'';
    var wasCur=(devEditId===curDev())||!!(served&&devEditId===served);
    var left=(DEV.list||[]).filter(function(x){ return x.id!==devEditId; });
    devEditClose();
    if(!wasCur && left.length){ show('已删除'); devLoad(); return; }
    /* 删的是当前这台，或一台不剩：这张会话列表已经不属于任何开发机了。
       清掉选中，交给服务端分流：还有别的机器→/ 自动选一台跳 /d，一台不剩→接入页。 */
    document.cookie='cb_dev=;path=/;max-age=0';
    var gbase=DEV.base||(PLAND&&PLAND.relay_base)||'';
    location=gbase+'/';
  });
}
let detailSid=null;
function findSession(sid){ return (lastExt||[]).find(x=>x.sid===sid)||(lastWeb||[]).find(x=>x.sid===sid)||null; }
function showDetail(sid){
  detailSid=sid;
  const box=document.getElementById('dbox');
  const m=findSession(sid)||{sid:sid,title:'会话 '+String(sid).slice(0,8),state:'',last:0};
  const isCur=(sid===SID);
  const isExt=!!(lastExt||[]).find(x=>x.sid===sid);
  let h='<div class="card"><div class="name" style="font-size:15px;margin-bottom:4px">'+escHtml(m.title||m.name||('会话 '+String(sid).slice(0,8)))+'</div>';
  /* 改名入口：手机起的名字手机列表/推送/电脑终端 tab 一起用（详见 renameSess）。
     标题经 data-t 带过去——onclick 字符串里塞原文会被引号打断。 */
  h+='<div class="drow" data-t="'+escHtml(m.title||'')+'" onclick="closeDetail();renameSess(\''+sid+'\',this.getAttribute(\'data-t\'))"><span>会话名称</span><span>'+escHtml(m.title||'（未命名）')+' ✏</span></div>';
  h+='<div class="drow"><span>状态</span><span class="'+((m.state==='idle'||m.state==='exited')?'idle':'')+'">'+escHtml(stText(m.state||''))+'</span></div>';
  /* 类型行跟着 /api/ext_agents 的显示名走；品牌色收一个认一个，没收录的给中性灰 */
  const AG_C={claude:'#d97757',codex:'#2fbf9b'};
  if(m.agent) h+='<div class="drow"><span>类型</span><span style="color:'+(AG_C[m.agent]||'#838383')+'">'+escHtml(agentLabelOf(m.agent))+'</span></div>';
  if(isCur&&m.host) h+='<div class="drow"><span>终端</span><span>Codeboss 托管（可读写）</span></div>';
  else if(isCur) h+='<div class="drow"><span>全彩镜像</span><span>'+(fullColor?'<span class="live">✓ 已接入</span>':'<span class="dead">✗ 未接入</span>')+'</span></div>';
  if(isExt) h+='<div class="drow" onclick="closeDetail();openSModel(\''+sid+'\')"><span>模型（本会话）</span><span>'+(m.model?escHtml(m.model):'跟机器默认')+' 切换 ›</span></div>';
  if(m.last||m.started) h+='<div class="drow"><span>最近活动</span><span>'+ago(m.last||m.started)+'</span></div>';
  if(isCur&&curTotal>=0) h+='<div class="drow"><span>消息数</span><span>'+curTotal+'</span></div>';
  h+='<div class="drow"><span>会话 ID</span><span class="meta" onclick="copyText(\''+sid+'\')">'+sid+' 📋</span></div>';
  if(m.cwd) h+='<div class="drow"><span>工作目录</span><span class="meta" onclick="copyText('+JSON.stringify(m.cwd).replace(/"/g,'&quot;')+')">'+escHtml(m.cwd)+' 📋</span></div>';
  h+='</div>';
  if(isCur&&curMeta&&!fullColor&&!curMeta.host&&curMeta.state!=='gone'&&curMeta.agent!=='codex'){
    h+='<div class="card"><div class="row"><div class="meta" style="font-size:12px">该会话还没接入全彩镜像</div><button style="white-space:nowrap" onclick="closeDetail();doMigrateOne(\''+SID+'\')">🔄 转全彩</button></div></div>';
  }
  h+='<div class="card"><div class="row"><div class="meta" style="font-size:12px">提醒历史（进度 / 智能概括）</div><button class="gray" style="white-space:nowrap" onclick="closeDetail();nhOpen(\''+sid+'\')">🕘 查看</button></div></div>';
  h+='<div class="card"><div class="row"><div class="meta" style="font-size:12px">'+(isCur?'回到控制画面':'打开这个会话')+'</div><button style="white-space:nowrap" onclick="closeDetail();'+(isCur?'goPage(1)':(isExt?('switchTo(\''+sid+'\')'):('hostTermOpen(\''+sid+'\')')))+'">'+(isCur?'📱 去控制':'↪ 打开')+'</button></div></div>';
  box.innerHTML=h;
  document.getElementById('dsheet').classList.add('on');
}
function closeDetail(){ document.getElementById('dsheet').classList.remove('on'); }
/* ── 会话级模型（cc-switch B）：只给这一个会话传 --model，机器默认不动 ── */
let SM_SID='';
async function openSModel(sid){
  SM_SID=sid;
  const m=findSession(sid)||{};
  document.getElementById('smTitle').textContent=(m.title||String(sid).slice(0,8));
  document.getElementById('smBox').innerHTML='<div class="meta">读取中…</div>';
  document.getElementById('smsheet').classList.add('on');
  try{
    const r=await xj('/api/ext_model_session?sid='+encodeURIComponent(sid));
    const d=r.json||{};
    if(!d.ok){document.getElementById('smBox').innerHTML='<div class="meta">'+escHtml(d.error||'读取失败')+'</div>';return;}
    const cur=(d.current||{}).model||'';
    const opts=d.options||[];
    const mach=(d.machine||{}).model||'—';
    document.getElementById('smBox').innerHTML=
      '<div class="meta" style="margin-bottom:6px">机器默认：'+escHtml(mach)+'（切供应商在列表页「当前模型」）</div>'+
      opts.map(function(o){
        const on=cur?o.model===cur:!o.tier;
        return '<div class="trow" data-tier="'+escHtml(o.tier)+'" data-model="'+escHtml(o.model)+'">'+
          '<span>'+escHtml(o.label)+'</span><span class="meta">'+(on?'当前':'')+'</span></div>';
      }).join('');
  }catch(e){ document.getElementById('smBox').innerHTML='<div class="meta">网络错误</div>'; }
}
function closeSModel(){ document.getElementById('smsheet').classList.remove('on'); }
document.getElementById('smBox').addEventListener('click', async function(e){
  const r=e.target.closest('[data-tier]'); if(!r) return;
  const tier=r.getAttribute('data-tier')||'', model=r.getAttribute('data-model')||'';
  closeSModel();
  try{
    const x=await xj('/api/ext_model_session',{method:'POST',body:{sid:SM_SID,tier:tier,model:model}});
    const d=x.json||{};
    if(!d.ok){ toast(d.error||'切换失败',true); return; }
    toast(d.queued?'会话在跑，空闲后自动换模型':('已切到 '+(d.model||'机器默认')+'（仅本会话）'));
    setTimeout(loadListData,1500);
  }catch(e){ toast('网络错误',true); }
});
function migrateAll(btn){
  armConfirm(btn,'全彩迁移（未接入的自动 --resume 重开，运行中的跳过）',function(){doMigrateAll(btn);});
}
async function doMigrateAll(btn){
  btn.disabled=true; btn.textContent='迁移中…';
  try{
    const r=await xj('/api/ext_migrate',{method:'POST',body:{}});
    const d=r.json;
    let msg='迁移 '+(d.migrated||[]).length+' 个；跳过 '+(d.skipped||[]).length+' 个；失败 '+(d.failed||[]).length+' 个';
    (d.skipped||[]).forEach(x=>{msg+='；跳过 '+(x.title||x.sid)+'：'+(x.why||'');});
    (d.failed||[]).forEach(x=>{msg+='；失败 '+(x.title||x.sid)+'：'+(x.why||'');});
    toast(msg); loadListData();
  }catch(e){ toast('网络错误',true); }
  btn.textContent='🔄 全彩迁移'; btn.disabled=false;
}
/* ── 召回已退出的电脑会话 ── */
function resumeExt(btn,sid){
  armConfirm(btn,'召回会话（电脑上新开终端 --resume，历史保留）',function(){doResumeExt(sid);});
}
async function doResumeExt(sid){
  toast('拉起中…');
  try{
    const r=await xj('/api/ext_resume',{method:'POST',body:{sid:sid},timeout:35000});
    const d=r.json;
    if(!d.ok){toast(d.error||'拉起失败',true);return;}
  }catch(e){toast('网络错误',true);return;}
  /* 进程起来要几秒（发现是 2s 一轮）。以前只 reload 列表让用户自己再找一次，
     现在等到它真的活着就直接进会话——点了「重新拉起」就该落在会话里。 */
  toast('已拉起，等待进程就绪…');
  if(await waitAlive(sid)) switchTo(sid,{noResume:true});
  else toast('拉起超时，稍后在列表里点进去',true);
}
/* ── 模型切换（cc-switch providers）──
   全局只有一个服务商生效（cc-switch 就是机器级）：切了之后所有会话都跟着换。
   运行中的 claude 不会重读 settings.json，所以由 host 重启会话来生效（--resume，历史不丢）。 */
let mProvs=[],mProvId='',mTier='',mBase='',mAg='';
const hostOf=u=>(u||'').replace(/^https?:\/\//,'').split('/')[0];
async function openModel(){
  track('model_open');
  mProvId='';mTier='';
  /* 先开 sheet 再取数：点了立刻有反应（数据慢/失败也不至于像「没反应」） */
  document.getElementById('mcur').textContent='读取中…';
  document.getElementById('mprovs').innerHTML='<div class="meta">读取中…</div>';
  document.getElementById('mtiers').innerHTML='';
  document.getElementById('msheet').classList.add('on');
  try{
    const r=await xj('/api/ext_model'+(CUR_AG&&CUR_AG!=='claude'?('?agent='+encodeURIComponent(CUR_AG)):''));
    const d=r.json;
    if(!d||!d.ok){toast((d&&d.error)||('读取失败 HTTP '+(r&&r.status)),true);return;}
    mProvs=d.providers||[];
    /* 只有一种 agent 时 CUR_AG 是空（切换条隐藏）：用服务端按机器实际安装
       推出的 agent，别再把 Codex-only 的机器按 Claude 处理。 */
    mAg=CUR_AG||d.agent||'claude';
    const cur=d.current||d.machine||{};
    mBase=cur.base_url||'';
    mProvId=cur.provider_id||(mProvs.find(p=>p.base_url===mBase)||{}).id||'';
    mTier='';
    document.getElementById('mcur').textContent=(cur.provider_name||'')+(cur.model?(' · '+cur.model):'');
    renderModel();
  }catch(e){toast('网络错误：'+(e&&e.message||e),true);}
}
function closeModel(){document.getElementById('msheet').classList.remove('on');}
function pickProv(id){if(mProvId===id)return;mProvId=id;mTier='';renderModel();}
function renderModel(){
  document.getElementById('mprovs').innerHTML=mProvs.map(p=>
    '<div class="prov'+(p.id===mProvId?' on':'')+'" onclick="pickProv(\''+p.id+'\')">'+
    '<span>'+escHtml(p.name)+(p.current?' <span class="meta">机器默认</span>':'')+'</span>'+
    '<span class="meta">'+escHtml(hostOf(p.base_url))+'</span></div>').join('');
  const p=mProvs.find(x=>x.id===mProvId)||{};
  const ts=p.tiers||{},rows=[];
  for(const t of['haiku','sonnet','opus'])if(ts[t])rows.push([t,ts[t]]);
  if(!rows.length&&p.model)rows.push(['default',p.model]);
  document.getElementById('mtiers').innerHTML=rows.map(([t,m])=>
    '<div class="tier'+((mTier||'default')===t?' on':'')+'" onclick="mTier=\''+t+'\';renderModel()">'+
    '<span>'+t+'</span><span class="meta">'+escHtml(m)+'</span></div>').join('')||
    '<div class="meta">'+(mProvs.length?'该供应商无档位信息':
      (mAg&&mAg!=='claude'
        ?('这台机器还没有 '+escHtml(agentLabelOf(mAg))+' 的供应商：在 cc-switch 的 '+escHtml(agentLabelOf(mAg))+' 页配一个再回来')
        :'没有可用的模型档位'))+'</div>';
}
async function doSwitch(){
  if(!mProvId){toast('先选供应商',true);return;}
  track('model_switch');
  const btn=document.getElementById('mgo');
  /* 单击直接切（不再两击确认：切错可再切回，历史保留；两击的第一击只弹 toast，
     用户不知道要点第二次 = 「点了没反应」的观感来源） */
  if(btn.disabled) return;
  btn.disabled=true;btn.textContent='切换中…';
  try{
    const r=await xj('/api/ext_model_switch',{method:'POST',
      body:{provider_id:mProvId,tier:mTier==='default'?'':mTier,agent:mAg||CUR_AG||''}});
    const d=r.json;
    if(d.ok){
      closeModel();
      if(d.via==='noop') toast('已是该模型');
      else if(d.agent&&d.agent!=='claude'){
        /* Codex（以及以后别的 agent）在启动时读配置，不打断在跑的会话：
           说清楚「新开的会话才用新供应商」，别让人以为正在跑的那轮也换了。 */
        toast('已切换到 '+(d.provider_name||'')+(d.model?(' · '+d.model):'')+'（新开的会话生效）');
      }else{
        const n=(d.restarted||[]).length, w=(d.waiting||[]).length;
        toast('已切换到 '+(d.provider_name||'')+(d.model?(' · '+d.model):'')+
              (n?('，'+n+' 个会话已重启'):'')+(w?('，'+w+' 个在跑，空闲后自动切'):''));
      }
      refreshModelCard(true);
      setTimeout(loadListData,1500);
    }else toast(d.error||'切换失败',true);
  }catch(e){toast('网络错误',true);}
  btn.disabled=false;btn.textContent='切换';
}
function migrateOne(btn,sid){
  armConfirm(btn,'接管：会在原终端关闭并重开这个会话（历史保留），之后手机上就能输入。正在跑的子进程、没落盘的 shell 状态会丢',function(){doMigrateOne(sid);});
}
async function doMigrateOne(sid){
  try{
    const r=await xj('/api/ext_migrate/'+sid,{method:'POST'});
    const d=r.json;
    if((d.migrated||[]).length) toast('已转为全彩');
    else{ const x=(d.skipped||[])[0]||(d.failed||[])[0]||{}; toast('未迁移：'+(x.why||'未知'),true); }
    setTimeout(loadListData,1500);
  }catch(e){ toast('网络错误',true); }
}
async function act(url){try{await xj(url,{method:'POST'});}catch(e){} loadListData();}
/* 「已失效」的网页终端整行点按：那是它随服务重启后留下的占位条目（进程已不在），
   点一下就把原命令重新拉起来并直接进去；拉不起来明确说一声，别让人点了没反应。 */
async function respawnOpen(sid){
  toast('终端已结束，正在重新拉起…');
  try{ await xj('/respawn/'+sid,{method:'POST'}); }catch(e){}
  await loadListData();
  const n=(lastWeb||[]).find(x=>x.sid===sid);
  if(n&&n.alive&&!n.ghost) hostTermOpen(sid);
  else toast('重新拉起失败，点 ↻ 再试一次',true);
}
/* 目录头「＋」：在这个目录里开一个新会话（电脑上弹新终端，手机几秒后从列表里看到） */
/* 这台电脑装了哪些 agent（Claude / Codex…），一次请求缓存 60 秒。
   取不到（老版本 host / 网络抖）就按老行为直接开默认 agent——
   选单是锦上添花，不能挡着「开新会话」这条路。 */
let AGS=null, AGS_T=0, AG_CWD='';
async function agentList(){
  if(AGS && Date.now()-AGS_T<60000) return AGS;
  try{
    const r=await xj('/api/ext_agents'); const d=r.json||{};
    if(d.agents && d.agents.length){ AGS=d.agents; AGS_T=Date.now(); }
  }catch(e){}
  return AGS;
}
/* 显示名收短：Claude Code → Claude，别让按钮被截成省略号 */
function agLabel(s){ s=String(s||''); return s==='Claude Code'?'Claude':s; }
function agentLabelOf(k){ const a=(AGS||[]).find(x=>x.key===k); return agLabel(a&&a.label||k); }
/* ── agent 切换条 ──
   装了两种以上才出现（只有一种就没什么可切的）。选中的 agent 决定：
   列表看谁的会话、下面的「当前模型」显示/切换谁的、目录＋默认开谁。
   以后加 hermes / openclaw 只是 /api/ext_agents 多一项，这里不用改。 */
let AGB_SIG='';
let AGB_CMD='';      /* agBarInit 最近自动填进 n_cmd 的值 */
let AGB_DBG='';
async function agBarInit(){
  const ags=await agentList();
  const bar=document.getElementById('agBar');
  if(!bar) return;
  /* 显示状态一变就落一行开发机日志：下次再「tab 又没了」，直接看是哪一步
     （没数据 / 只装一种 / 正常显示）没成，不用靠猜。 */
  const _agNote=function(d){ if(AGB_DBG===d) return; AGB_DBG=d;
    try{ window.__cbDbg&&window.__cbDbg('Ag '+d+' n'+((ags||[]).length)+' cur'+(CUR_AG||'-'),40); }catch(e){} };
  if(!ags||!ags.length){ bar.style.display='none'; _agNote('none'); CUR_AG=''; return; }
  if(ags.length<2){ _agNote('one');
    /* 只装了一种：不分流，列表照旧全显示；清掉可能残留的旧选择 */
    bar.style.display='none'; CUR_AG='';
    const lb=document.getElementById('modelLbl');
    if(lb) lb.textContent='当前模型';
    return;
  }
  if(!CUR_AG||!ags.some(a=>a.key===CUR_AG)){
    /* 没手选过：默认跟「最近有动静的那种」走，别一进来就把用户最近的会话藏掉。
       这个默认不写 localStorage，下次还按最近的会话重新判断。 */
    const recent=(lastExt||[]).slice().sort((a,b)=>(b.last||b.started||0)-(a.last||a.started||0))[0];
    CUR_AG=(recent&&recent.agent)||ags[0].key;
    if(!ags.some(a=>a.key===CUR_AG)) CUR_AG=ags[0].key;
  }
  bar.style.display='flex'; _agNote('flex');
  const sig=ags.map(a=>a.key+'\u0001'+a.label).join('|')+'#'+CUR_AG;
  if(sig===AGB_SIG) return;
  AGB_SIG=sig;
  document.getElementById('agTabs').innerHTML=ags.map(function(a){
    const on=a.key===CUR_AG;
    return '<button class="agtab '+escHtml(a.key)+(on?' on':'')+
      '" data-ag="'+escHtml(a.key)+'">'+escHtml(agLabel(a.label||a.key))+'</button>';
  }).join('');
  const cmd=document.getElementById('n_cmd');
  /* 只覆盖「还是上次自动填的值」的输入框——用户手输过命令就不再动它 */
  if(cmd&&((!cmd.value.trim())||cmd.value===AGB_CMD)) cmd.value=CUR_AG;
  AGB_CMD=CUR_AG;
  renderList();
  MC.t=0;
  refreshModelCard(true);
}
function setAgent(k){
  if(!k||k===CUR_AG) return;
  CUR_AG=k;
  try{ localStorage.setItem('cb_ag',k); }catch(e){}
  AGB_SIG='';
  agBarInit();
}
document.getElementById('agTabs').addEventListener('click',function(e){
  const b=e.target.closest('[data-ag]');
  if(b) setAgent(b.getAttribute('data-ag'));
});
async function sessNew(btn,cwd){
  /* 新建 agent 会话对免费版也开放：没有这个，「免费版一个会话都开不了」。
     命令终端/识别命令仍然是 Pro 功能（各自的入口和网关单独挡）。 */
  /* cwd 空 = 空白列表里的入口：由 host 落到电脑登录用户的 home 目录 */
  cwd=cwd||'';
  const ags=await agentList();
  /* 顶上切换条已经选好了：直接按它开，不用再问一遍 */
  if(CUR_AG&&(!ags||ags.some(a=>a.key===CUR_AG))){ doSessNew(btn,cwd,CUR_AG); return; }
  if(ags && ags.length>1){ agPickOpen(cwd,ags); return; }   // 装了两种：先让用户选
  doSessNew(btn,cwd,(ags&&ags.length===1)?ags[0].key:'');
}
function agPickOpen(cwd,ags){
  AG_CWD=cwd;
  const pn=cwd?((cwd.replace(/\/+$/,'').split('/').pop())||cwd):'电脑 home 目录';
  document.getElementById('agTitle').textContent='新建会话 · '+pn;
  document.getElementById('agBox').innerHTML=ags.map(a=>{
    return '<div class="trow" data-ag="'+escHtml(a.key)+'">'+
      '<span class="ag '+escHtml(a.key)+'">'+escHtml(a.label||a.key)+'</span>'+
      '<span class="meta" style="flex:1">在电脑开一个 '+escHtml(agentLabelOf(a.key))+' 终端</span>'+
      '<span class="meta">›</span></div>';
  }).join('');
  document.getElementById('agsheet').classList.add('on');
}
function closeAgSheet(){ document.getElementById('agsheet').classList.remove('on'); }
document.getElementById('agBox').addEventListener('click',function(e){
  const r=e.target.closest('[data-ag]');
  if(!r) return;
  const k=r.getAttribute('data-ag');
  closeAgSheet();
  doSessNew(null,AG_CWD,k);
});
async function doSessNew(btn,cwd,agent){
  if(btn){ btn.disabled=true; btn.textContent='…'; }
  try{
    const r=await xj('/api/ext_sess_new',{method:'POST',body:{cwd:cwd,agent:agent||''}});
    const d=r.json||{};
    if(d.ok){
      if(d.sid){
        /* host 直接回了新会话 id（Windows 托管会话）：等它进列表再进去。
           直接 switchTo 会撞上「发现缓存还没刷到」→ 推流首包 gone → 一句「会话已结束」。 */
        (async function(){
          if(await waitAlive(d.sid)) switchTo(d.sid,{noResume:true});
          else toast('新会话已创建，稍后在列表里打开');
        })();
      }else{ toast('已在电脑开 '+escHtml(agentLabelOf(agent||'claude'))+' 新会话，几秒后出现在列表'); setTimeout(loadListData,2500); }
    }
    else toast(d.error||'开不起来',true);
  }catch(e){ toast('网络错误',true); }
  if(btn){ btn.disabled=false; btn.textContent='＋'; }
}
/* fork：新会话带着这段历史，原会话一个字节都不动。新会话是 claude 自己起的新 id，
   页面这边认不出是哪一个，等下一轮列表刷新自然出现。 */
async function forkSess(btn,sid,cwd){
  if(!termAllowed()){ toast('免费版不具备该功能，开通 Pro 后使用',true); return; }
  if(btn) btn.disabled=true;
  try{
    const r=await xj('/api/ext_sess_fork',{method:'POST',body:{sid:sid,cwd:cwd||''}});
    const d=r.json||{};
    if(d.ok){ toast('已 fork，新会话几秒后出现在列表'); setTimeout(loadListData,2500); }
    else toast(d.error||'fork 失败',true);
  }catch(e){ toast('网络错误',true); }
  if(btn) btn.disabled=false;
}
/* 改会话名：手机列表和电脑终端 tab（Terminal.app）一起变，两边从此叫一个名字。
   留空保存 = 恢复自动起名（ai-title/窗口名/首句话那套）。 */
function renameSess(sid,cur){
  askConfirm('改会话名',
    '<input id="rnIn" maxlength="60" value="'+escHtml(cur||'')+'" '+
    'style="width:100%;margin-top:8px;padding:8px 10px" placeholder="留空 = 恢复自动起名">',
    '保存',false,async function(){
      const el=document.getElementById('rnIn');
      const v=el?el.value.trim():'';
      try{
        const r=await xj('/api/ext_title',{method:'POST',body:{sid:sid,title:v}});
        const d=r.json||{};
        if(d.ok){
          const it=lastExt.find(x=>x.sid===sid); if(it) it.title=d.title;
          if(curMeta&&curMeta.sid===sid){ curMeta.title=d.title; refreshHeader(); }
          renderList();
          toast(v?('已改名：'+v):'已恢复自动起名');
        }else toast(d.error||'改名失败',true);
      }catch(e){ toast('网络错误',true); }
    });
  /* 弹层一开就聚焦选中，直接打字就是改名（60ms 等 class .on 渲染完） */
  setTimeout(function(){ const el=document.getElementById('rnIn'); if(el){ el.focus(); el.select(); } },60);
}
/* 删除会话：连转录一起删，删了就真找不回来 —— 必须先过一道说清后果的确认 */
function delSess(sid){
  askConfirm('删除会话',
    '这个会话的记录（转录）会一起删掉，<b>删了找不回来</b>。'+
    (sid===SID?'你现在正看着它，删完会回到列表。':'')+
    '<br><span style="color:#838383;font-size:12.5px">它还在跑的话会先结束进程。'+
    '还想留着这段历史，先点 fork 复制一份。</span>',
    '删除',true,function(){ doDelSess(sid); });
}
async function doDelSess(sid){
  try{
    const r=await xj('/api/ext_sess_del',{method:'POST',body:{sid:sid}});
    const d=r.json||{};
    if(d.ok===false){ toast(d.error||'删除失败',true); return; }
    toast('已删除');
    if(sid===SID){   // 删的就是正在看的那条：回列表，别再对着一个不存在的会话轮询
      SID=''; try{ localStorage.removeItem('codeboss_last_sid'); }catch(e){}
      goPage(0);
    }
  }catch(e){ toast('网络错误',true); }
  setTimeout(loadListData,600);
}
/* 网页终端（🌐）的「移除」：只删登记条目不碰磁盘；同样问一句，别一点就没 */
function confirmRemove(sid){
  askConfirm('移除会话','这条记录会从列表里去掉（转录还在，能重新拉起）。','移除',false,
             function(){ act('/remove/'+sid); });
}
/* 列表页「新建终端」卡：创建 PTY=手机上直接用的无窗口终端；打开电脑终端=Mac 弹 Terminal 窗口跑命令 */
async function termCreate(btn){
  if(!termAllowed()){ toast('免费版不具备该功能，开通 Pro 后使用',true); return; }
  const inp=document.getElementById('n_cwd');
  const cwd=inp?inp.value.trim():'';
  btn.disabled=true; btn.textContent='创建中…';
  try{
    const r=await xj('/api/term_new',{method:'POST',body:{cwd:cwd}});
    const d=r.json||{};
    if(d.ok&&d.sid){
      TERM.terms.unshift({sid:d.sid,name:'终端 '+d.sid.slice(0,4),alive:true,cwd:cwd});
      termDotSync();
      termView(d.sid);
    } else toast(d.error||'创建失败',true);
  }catch(e){ toast('网络错误',true); }
  btn.disabled=false; btn.textContent='创建 PTY';
}
async function macTermOpen(btn){
  if(!termAllowed()){ toast('免费版不具备该功能，开通 Pro 后使用',true); return; }
  const cwd=(document.getElementById('n_cwd')||{}).value||'';
  const cmd=(document.getElementById('n_cmd')||{}).value||'';
  btn.disabled=true; btn.textContent='打开中…';
  try{
    const r=await xj('/api/term_mac',{method:'POST',body:{cwd:cwd.trim(),cmd:cmd.trim()}});
    const d=r.json||{};
    if(d.ok) toast('已在电脑打开终端窗口');
    else toast('失败：'+(d.error||''),true);
  }catch(e){ toast('网络错误',true); }
  btn.disabled=false; btn.textContent='🖥 打开电脑终端';
}
/* ── 版本状态 ── */
async function vCheck(btn){
  if(btn){btn.disabled=true;btn.textContent='查询中…';}
  try{
    const r=await xj('/api/version'+(btn?'?force=1':''));
    const d=r.json;
    document.getElementById('vnum').textContent='v'+d.version;
    const msg=document.getElementById('vmsg');
    if(d.update){ msg.innerHTML='<span class="live">'+esc(d.note||('可更新（落后 '+d.behind+' 个提交）'))+'</span>';
      document.getElementById('vupd').style.display=''; }
    else msg.textContent=d.err?('检查失败：'+d.err):'已是最新版本';
  }catch(e){ document.getElementById('vmsg').textContent='检查失败：网络错误'; }
  if(btn){btn.disabled=false;btn.textContent='检查更新';}
}
async function vUpdate(btn){
  btn.disabled=true; btn.textContent='更新中…';
  try{
    const r=await xj('/api/update',{method:'POST'});
    const d=r.json;
    if(d.uptodate){ toast(d.why||'已是最新版本'); btn.disabled=false; btn.textContent='更新'; return; }
    if(d.ok){ toast('更新中，几秒后自动恢复…');
      setTimeout(()=>{ pageRemember(pageIdx); location.reload(); },9000); }
    else{ toast(d.why||'失败',true); btn.disabled=false; btn.textContent='更新'; }
  }catch(e){ toast('网络错误',true); btn.disabled=false; btn.textContent='更新'; }
}
/* ── 启动 ── */
async function init(){
  /* iPhone 装不了 APK：这个按钮就别摆出来（apkOpen 里还有一道兜底提示） */
  try{ if(isIOS()){ var _ab=document.getElementById('kApkBtn'); if(_ab) _ab.style.display='none'; } }catch(e){}
  /* 套餐/升级入口暂时整体隐藏：靠 body.plans-off 的 CSS，后面 applyPlanUI 再显示也压得住 */
  try{ if(PLANS_OFF) document.body.classList.add('plans-off'); }catch(e){}
  /* 页面每次加载/重载留一行：自动听写「刚开麦就被取消」时先分清是不是页面被换掉了 */
  try{ window.__cbDbg && window.__cbDbg('Nboot '+(location.hash||'-')); }catch(e){}
  /* DBG：给原生桥包一层。自动听写开麦 2 秒就被「cancel」收掉，而页面自己的按麦入口
     一行探针都没打——说明不是页面按的。包上这层，页面/壳内嵌页/音量键谁动的麦都留痕。查完删。 */
  try{
    var _rb=window.CB;
    if(_rb && !window.__cbBridged){
      window.__cbBridged=1;
      ['pttDown','pttUp','autoListenStart','autoListenStop'].forEach(function(k){
        var f=_rb[k];
        if(typeof f!=='function') return;
        try{
          _rb[k]=function(){
            try{
              window.__cbDbg && window.__cbDbg('Nbr '+k+' '+(window.__cbAutoInfo?window.__cbAutoInfo():'-')
                                                 +' st='+(_rb.pttState?_rb.pttState():'?'));
            }catch(e){}
            return f.apply(_rb,arguments);
          };
        }catch(e){}
      });
    }
  }catch(e){}
  /* 这次启动是「重载」还是「第一次打开」：重载才提示「找回了没发出去的内容」 */
  try{ sessionStorage.setItem('cb_draft_hint', sessionStorage.getItem('cb_booted')?'1':'0');
       sessionStorage.setItem('cb_booted','1'); }catch(e){}
  planRefresh();   // 先查套餐：免费版要收起终端入口（异步，到了会自行校正）
  usePollStart();  // 顶部今日远程时长：前台每 30 秒自动刷新
  const hm=location.hash.match(/^#p=([012])$/);   // #p=0 列表 / #p=1 控制 / #p=2 详情：可深链、可测试
  const hp=hm?parseInt(hm[1],10):null;
  // 先同步定位面板（goPage 里会显示 deck），再等数据——不能让页面停在未定位状态等接口
  let target;
  if(SID) target=(hp!==null?hp:1);
  else{
    let last=null; try{last=localStorage.getItem('codeboss_last_sid');}catch(e){}
    target=(hp!==null?hp:(last?1:0));   // 没有上次会话：落在列表
  }
  /* 重载回来回到原来那一屏（自更新 / 长跑自愈 / 壳的 reload / WebView 被回收）。
     先看 sessionStorage（同一次页面会话，最准）；没有了再看带时间戳的 localStorage
     ——WebView 进程被系统回收后 sessionStorage 可能已经丢了，但只认 2 分钟内的，
     免得隔一会儿重新打开 App 还停在设置页。 */
  let keep=null, keepSrc='-', wantSub='';
  try{
    const s=sessionStorage.getItem('cb_keep_page'); sessionStorage.removeItem('cb_keep_page');
    if(s!==null&&s!==''){ const n=parseInt(s,10); if(n>=0&&n<=3){ keep=n; keepSrc='s'; } }
  }catch(e){}
  if(keep===null){
    try{
      const m=JSON.parse(localStorage.getItem('cb_page_at')||'null');
      const age=m?Date.now()-Number(m.at||0):1e9;
      if(m&&m.p>=0&&m.p<=3&&age>=0&&age<120000){ keep=m.p; keepSrc='l'; }
    }catch(e){}
  }
  if(hp===null&&keep!==null){
    /* 2=终端子页 3=设置子页：先落到列表，再把子页盖回来（goPage 只管两格） */
    if(keep===2){ target=0; wantSub='term'; }
    else if(keep===3){ target=0; wantSub='set'; }
    else target=keep;
  }
  goPage(target);
  if(wantSub==='term') openTermPage();
  else if(wantSub==='set') openSettings();
  BOOT_INFO='pg'+pageIdx+' k'+keepSrc+(keep===null?'-':keep);
  /* 滚动位置恢复只对会话页有意义：开屏落在别的页就直接作废，免得之后翻回来突然跳走 */
  if(pageIdx!==1) SCROLL_WANT=null;
  /* 待恢复期间人一碰会话页就放手（别跟正在滑的人抢滚动条） */
  try{
    const box=ctrlBox();
    ['touchstart','touchmove','wheel','mousedown'].forEach(function(k){
      box.addEventListener(k,function(){ SCROLL_WANT=null; },{passive:true});
    });
  }catch(e){}
  /* 开屏落一行：重载后落在哪一屏、位置从哪来（下次「自己跳页」直接看日志）。
     __cbDbg 在脚本最后一段才定义，而这里（第一段末尾就调 init）可能比它早执行，
     所以轮询等它出现，别用一个固定延时赌解析速度。 */
  (function bootLog(n){
    try{
      if(window.__cbDbg){
        window.__cbDbg('Boot pg'+pageIdx+' keep'+keepSrc+(keep===null?'-':keep),40);
        return;
      }
    }catch(e){}
    if(n<20) setTimeout(function(){ bootLog(n+1); },500);
  })(0);
  /* 缓存里的会话列表先顶上：进列表页不用等中继一个来回才出内容（下面那次
     loadListData 照旧去拿新的，回来了原地替换，界面不闪） */
  try{ sessSeed(); }catch(e){}
  try{ termLoad(); }catch(e){}   // 拉一次终端列表：有终端才显示「命令终端」入口
  try{ prefLoad(); }catch(e){}   // 智能提醒本机开关的上报：开发机靠它知道这台手机还开着（进设置页也会再报）
  try{ dispStartup(); }catch(e){}   // 亮度/护眼/常亮存档回放（启动即生效，别等进设置页）
  if(!SID){   // / 直接打开：找回上次控制的会话
    let last=null; try{last=localStorage.getItem('codeboss_last_sid');}catch(e){}
    if(last){
      await loadListData();
      if((lastExt||[]).some(x=>x.sid===last)){ SID=last; try{history.replaceState(null,'','/ext/'+last);}catch(e){} }
      else{ SID=''; try{localStorage.removeItem('codeboss_last_sid');}catch(e){} goPage(0); }   // 上次会话已不在：回落列表
    }
  }
  if(SID){
    if(!hiddenNow()) seenMark(SID);
    try{ liveCacheSeed(); }catch(e){}   // 先把上次这一屏画出来（更新/重启后不用干等一个来回）
    try{ msgCachePrime(SID); }catch(e){}   // 聊天墙同理：缓存优先，没缓存才干等 host
    /* 内容加载别串在会话列表后面：两个请求各走各的，谁先回来谁先画 */
    loadListData(); poll();
  }else if(!hm){
    document.getElementById('live').textContent='→ 右滑或点「☰ 列表」选一个会话';
  }
  pushStart();   // 推送连上后轮询 interval 自动闸掉；断流自动回轮询
  directTry();   // P2P：探到直连就自动把 SSE/轮询切过去（切前走中继，零阻塞）
  try{ draftRestore(); }catch(e){}   // 重载回来：上一轮没发出去的字放回输入框
  hsWait();      // 点了更新后重启/页面自更新：回来时静默把「开发机服务」刷成最新
  setTimeout(function(){fitHeader();deckAlign();},400);
}
setView('live');  // 默认落在实时视图（聊天页已隐藏）
// 开屏默认页签分线：agentboss 落工作台（台账），codeboss 落会话列表（2026-11-19 计划阶段 0）
try{ listView(lineCaps(cbLine()).ledgerDefault?'ib':'sess'); }catch(e){}
// 入口按线显隐：测试红绿卡/交付是工头线的深度件（功能路由两条线都在，只是入口分线）
try{ if(!lineCaps(cbLine()).testCards){
  var _tb=document.getElementById('tcBtn'); if(_tb) _tb.style.display='none';
} }catch(e){}
try{ if(!lineCaps(cbLine()).deploy){
  var _db=document.getElementById('dpBtn'); if(_db) _db.style.display='none';
} }catch(e){}
// 回到上次的会话时也问一次项目能跑什么：入口按项目显隐（见 cmdsProbe）
try{ if(SID) cmdsProbe(SID); }catch(e){}
init();
/* 首次打开弹使用引导：跳过/开始都记住，不再打扰 */
function g1Close(){
  var o=document.getElementById('g1'); if(o) o.classList.remove('on');
  try{ localStorage.setItem('cb_guide1','1'); }catch(e){}
}
try{ if(!localStorage.getItem('cb_guide1')) document.getElementById('g1').classList.add('on'); }catch(e){}
/* 滑动换会话的那几百毫秒里先别轮询/重画：定时器正好撞上滑动，主线程一忙就会掉帧 */
setInterval(function(){ if(!SESS_DRAG) poll(); },1800);
setInterval(scrollRemember,1000);   // 记「读到哪儿」：重载回来接着看历史（位置没变不写盘）
window.addEventListener('pagehide',function(){ try{ scrollRemember(); }catch(e){} });
document.addEventListener('visibilitychange',function(){ if(document.hidden){ try{ scrollRemember(); }catch(e){} } });
setInterval(function(){ if(!SESS_DRAG) screenPoll(); },1200);
setInterval(function(){ if(!SESS_DRAG) rawPoll(); },1000);
setInterval(function(){ if(!SESS_DRAG) liveflowPoll(); },1000);
/* 推送活着时列表由 sessions 事件推过来，这里完全不请求；
   推送断了才兜底轮询——而且 20 秒一次（以前 5 秒，列表页开着就一直走中继）。 */
setInterval(()=>{ if(PUSH.st==='on'||hiddenNow()) return; if(pageIdx!==1) loadListData(true); },20000);
/* 收件箱自己也要跟最新：推送活着时上面那个列表轮询是关的，光靠推送事件收不到
   「这轮结束了、在等你」这种历史行（2026-11-14：开着收件箱看到的是旧内容）。 */
setInterval(function(){
  if(hiddenNow()||pageIdx!==0||LV!=='ib') return;
  try{ ibPendLoad(); }catch(e){}
  try{ ibDoneLoad(); }catch(e){}
},20000);
/* ── 长跑自愈：页面跑久了自己换一次 ─────────────────────────────────────
   WebView 的内存高水位不会还给系统，连着跑几天之后切页、滚动、键盘弹起都会慢半拍，
   重启一次 App 立刻恢复——但这不该让用户自己发现。到点就静默 reload：
   URL 不变、设置都在本地、会话在电脑上，重载完推送会自己连回来。
   后台（用户看不见）跑满 8 小时就换；前台一直亮着（桌面接管/盯着跑）等到
   24 小时并且真闲下来 10 分钟才换。录音中、输入框有焦点时一律不动。 */
var BOOT_AT=Date.now(), LAST_ACT=Date.now();
['touchstart','click','keydown'].forEach(function(ev){
  document.addEventListener(ev,function(){ LAST_ACT=Date.now(); },{passive:true});
});
setInterval(function(){
  var h=(Date.now()-BOOT_AT)/3600000;
  if(h<1) return;
  var bg=hiddenNow()||document.hidden;
  if(!(bg ? h>=8 : (h>=24 && Date.now()-LAST_ACT>=600000))) return;
  if(hasDraft()||voiceBusyNow()||viewingHold()) return;
  var ta=document.getElementById('txt');
  if(ta&&document.activeElement===ta) return;
  BOOT_AT=Date.now();          /* reload 万一被拦，也别每分钟重试一次 */
  pageRemember(pageIdx);       /* 重载后回到原来那一屏 */
  try{ location.reload(); }catch(e){}
},60000);
/* ── 草稿保护：页面被重载时，没发出去的字不能丢 ──────────────────────
   页面重载的来源不止一个（页面自更新、长跑自愈、壳探测重连、网络闪断被壳顶掉），
   上面那些护栏只能拦住页面自己发起的重载，壳和系统的动作拦不住。所以退一步：
   把输入框内容按会话存本地，重载回来再放回去——不管你正打到一半还是输入法拼到一半。 */
var DRAFT_KEY='cb_drafts', DRAFT_CACHE=null, DRAFT_TIMER=0;
function draftMap(){ try{ return JSON.parse(localStorage.getItem(DRAFT_KEY)||'{}')||{}; }catch(e){ return {}; } }
function draftSave(force){
  var t=document.getElementById('txt'); if(!t) return;
  var v=String(t.value||''), k=(typeof SID!=='undefined'?SID:'')||'';
  if(!force&&DRAFT_CACHE===k+'\u0000'+v) return;
  DRAFT_CACHE=k+'\u0000'+v;
  var m=draftMap(), now=Date.now();
  if(!v.trim()){ if(!(k in m)) return; delete m[k]; }
  else m[k]={t:v,ts:now};
  for(var x in m){ if(!m[x]||now-(m[x].ts||0)>86400000) delete m[x]; }   // 一天前的草稿不留
  try{ localStorage.setItem(DRAFT_KEY,JSON.stringify(m)); }catch(e){}
}
function draftSaveSoon(){
  if(DRAFT_TIMER) return;
  DRAFT_TIMER=setTimeout(function(){ DRAFT_TIMER=0; try{ draftSave(); }catch(e){} },400);
}
function draftRestore(){
  var t=document.getElementById('txt'); if(!t) return;
  if(String(t.value||'').trim()) return;      // 已经有字（比如语音刚填进来）：不动它
  var k=(typeof SID!=='undefined'?SID:'')||'';
  var m=draftMap(), d=m[k]; if(!d||!d.t) return;
  t.value=d.t; DRAFT_CACHE=k+'\u0000'+d.t;
  try{ fitTxt(); }catch(e){}
  try{ window.__cbDbg && window.__cbDbg('Ndraft restore '+d.t.length); }catch(e){}
  /* 只在「页面重载后找回来的」这种情况下提示，会话切换时恢复别弹 */
  try{ if(sessionStorage.getItem('cb_draft_hint')==='1'&&!hiddenNow()) toast('已找回上次没发出去的内容',false,2500); }catch(e){}
}
setInterval(function(){ try{ draftSave(); }catch(e){} },3000);     // 语音插入等非键盘改动也能兜到
window.addEventListener('pagehide',function(){ try{ draftSave(true); }catch(e){} });
document.addEventListener('visibilitychange',function(){ if(hiddenNow()){ try{ draftSave(true); }catch(e){} } });
/* ── 语音链路忙不忙 ───────────────────────────────────────────────────
   从「按下说话」到「转写结果落进输入框」，这一整段都不许重载页面。
   坑在转写那几秒：录音已经停了（pttState=idle、按钮也不亮了），以前所有护栏
   全部失效，正好赶上页面自更新就重载 → 这句话直接没了，用户得再说一遍。
   所以除了「正在录」，还要留一段转写宽限窗口（__cbVoiceUntil）。 */
function hasDraft(){
  /* 输入框里还有没发出去的字（说了半句、识别完还没发、改到一半）→ 不许重载。
     输入法拼写中 value 可能还是空的，所以另外看「在拼」和「刚打过字」。 */
  if(IME_ON) return true;
  if(LAST_TYPE&&Date.now()-LAST_TYPE<8000) return true;
  var t=document.getElementById('txt');
  return !!(t && String(t.value||'').trim());
}
function viewingHold(){
  /* 正看着东西（文档/图片预览、网页预览层、产出物页）→ 页面自更新先别动：
     重载会把眼前这一屏整个换掉、落回会话页，pageRemember 只记得住第几屏，
     记不住浮层（2026-11-20 用户：查产出物内容时整页刷新跳回会话页）。
     关掉之后下一个 60 秒周期照常更新。 */
  var ids=['fview','cbSheet','filePage'];
  for(var i=0;i<ids.length;i++){
    var el=document.getElementById(ids[i]);
    if(el&&el.classList.contains('on')) return true;
  }
  return false;
}
function voiceBusyNow(){
  if(window.__cbVoiceBusy) return true;
  if(window.__cbPttWait) return true;      /* 转写还在路上：重载就把这句话丢了 */
  if(window.__cbVoiceUntil && Date.now() < window.__cbVoiceUntil) return true;
  try{ if (window.__cbPttDown && window.__cbPttDown()) return true; }catch(e){}
  var p=document.getElementById('cbptt');
  if(p&&p.classList.contains('on')) return true;
  try{ if (window.CB && CB.pttState && CB.pttState()==='rec') return true; }catch(e){}
  return false;
}
/* 老壳兜底（手机上的 shim.js 要跟着 APK 才能换）：页面自己盯原生的录音状态，
   「录着 → 停了」这一刻起算 25 秒转写宽限。新 shim 会直接打标记，这里只是多一层。 */
(function(){
  var wasRec=0;
  setInterval(function(){
    var rec=false;
    try{ rec = !!(window.CB && CB.pttState && CB.pttState()==='rec'); }catch(e){}
    if(rec){ wasRec=1; window.__cbVoiceBusy=1; return; }
    if(wasRec){
      wasRec=0;
      window.__cbVoiceBusy=0;
      window.__cbVoiceUntil=Math.max(window.__cbVoiceUntil||0, Date.now()+25000);
    }
  },400);
})();
/* 页面自更新：host 上 ext.html 改过就重载（否则 WebView 一直停在旧页面，改了看不到） */
var PVER=0;
var PAGE_BUILD='2026-11-14b';   /* 构建戳：跟着版本检查一起报给 host，日志里能看手机跑的是哪一版 */
function pverCheck(){
  /* 后台/息屏不发请求：中继按「每自然分钟有没有请求」计时长，以前这里的 60 秒轮询
     在 WebView 被后台节流后仍会每分钟打一次 /api/page_ver，息屏也会继续计时。 */
  if(hiddenNow()) return;
  xget('/api/page_ver?b='+PAGE_BUILD,function(d){
    if(!d||!d.ok) return;
    /* host 会告诉现在下发的是哪个构建。跟自己不一样就是旧页：光靠 v 判断不出来——
       reload 之后又被中间缓存塞回旧 HTML，v 就一直不变，页面永远停在老代码
       （2026-11-13 实锤：手机上还在跑 media 之前的版本）。换带 pv= 的新 URL 硬刷，
       绕开任何缓存；同一个 v 只硬刷一次，刷不回来也不能死循环。 */
    if(d.b&&d.b!==PAGE_BUILD){
      if(hiddenNow()||voiceBusyNow()||IME_ON||typingHold()||viewingHold()) return;
      var k='pv'+d.v;
      try{ if(sessionStorage.getItem('pvtry')===k) return; sessionStorage.setItem('pvtry',k); }catch(e){}
      try{ draftSave(true); }catch(e){}
      pageRemember(pageIdx);
      var u=location.href;
      try{ var o=new URL(u); o.searchParams.set('pv',d.v); u=o.href; }
      catch(e){ u=u.replace(/([?&])pv=\d+&?/,'$1').replace(/[?&]$/,'')+((u.indexOf('?')<0)?'?':'&')+'pv='+d.v; }
      location.replace(u); return;
    }
    if(d.b){ try{ sessionStorage.removeItem('pvtry'); }catch(e){} }
    if(!PVER){ PVER=d.v; return; }
    if(d.v===PVER||hiddenNow()) return;
    /* 任何页都可自愈重载：发送回退/键盘顶栏补偿这类修复必须能到会话页，
       不然手机 WebView 一直停在旧页面（网络错误/顶栏悬在键盘上方都是这么来的）。
       护栏：输入框有草稿或正在录音/等转写 → 本轮跳过，下个周期再试 */
    /* 只要输入框里还有字，或者语音链路还没走完（录音/等转写），一律不重载：
       重载会把手指底下那个按钮从 DOM 里换掉，WebKit 的触摸序列就断了 → touchend
       再也送不到 → 松手停不下来，麦克风一直开着（iOS 实锤）；转写中重载则直接
       把这句话丢了；有草稿重载等于让人重说一遍。 */
    /* 草稿不再挡自更新：草稿本来就落了 localStorage（输入事件后 400ms draftSave，
       语音识别结果也走 input 事件），重载回来 draftRestore() 会放回输入框。
       以前这里是 hasDraft()，输入框里只要还剩一句话，页面就永远不换新 ——
       改好的修复到不了手机（2026-11 实测：连升两版，手机上还在跑老页面）。
       真正不能重载的只剩两种：正在录音/等转写（丢这句话）、输入法正在拼字。 */
    if(voiceBusyNow()||IME_ON||typingHold()||viewingHold()) return;
    try{ draftSave(true); }catch(e){}
    pageRemember(pageIdx);
    location.reload();
  });
}
document.addEventListener('visibilitychange',function(){ if(!hiddenNow()) pverCheck(); });
setInterval(pverCheck,60000);
setInterval(function(){   // P2P：局域网票据提前滚动；没连上就周期重探（内部有冷却栅栏）
  if(hiddenNow()) return;
  /* 局域网和 P2P 都要滚票：P2P 现在每 4 分钟把 dt 重发给 host 复验（dcKeepAuth），
     拿不到新票就等于通道到期。局域网续不到票直接退出；P2P 交给 host 到期回
     auth_expired——这里先断会把「中继抖一下」放大成断连。 */
  if(directOn()&&(DIRECT.mode==='lan'||DIRECT.mode==='dc')&&DIRECT.exp-Date.now()/1000<180)
    directTicket().then(function(ok){
      if(ok){ if(DIRECT.mode==='dc') dcKeepAuth(); return; }
      if(directOn()&&DIRECT.mode==='lan') directDown('ticket');
    });
  if(!directOn()) directTry();
  directDiag('tick');
},60000);
setTimeout(function(){ directDiag('boot'); },3000);
setTimeout(function(){ directDiag('t15'); },15000);
/* 开页面就探一次直连：以前只有「回到前台」和 60s 周期两处会探，
   冷启动后要干等一分钟才连上（这期间全走中继）。 */
setTimeout(function(){ try{ directTry(); }catch(e){} },1500);