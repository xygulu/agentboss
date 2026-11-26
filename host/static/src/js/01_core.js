// SPDX-License-Identifier: AGPL-3.0-or-later

let SID="__SID__";
let pageIdx=1;                       // 0=会话列表 1=会话页（左右滑在会话之间切换）
let curMeta=null, lastExt=[], lastWeb=[], curTotal=-1, curState='';
/* 列表页顶上的 agent 切换（Claude / Codex / …）。空串 = 还没选过，用第一个已装的。 */
let CUR_AG='';
try{ CUR_AG=(localStorage.getItem('cb_ag')||'').trim(); }catch(e){}
let lastTotal=-1, view='live', fullColor=false, xt=null, curWin='', lastW=0, lastFlowHtml='', lastHistHtml='', viewChosen=false, autoSwitched=false, rawRowsMin=0, hostWin=null, lastFrameHash=null, DLG_OPT_ON=false;
/* 视图记忆：用户手选过「原样」后，刷新/重进会话都停在原样，不再被智能默认拽回实时。
   空串 = 没手选过（仍走智能默认：全彩会话落到实时）。 */
let wantView=''; try{ wantView=localStorage.getItem('pty_view')||''; }catch(e){}
if(wantView!=='live'&&wantView!=='raw') wantView='';
function pick(v){
  viewChosen=true;
  setView(v);
  // 只记住真正生效的选择（非全彩会话点原样会被拦回实时，那就不存）
  if(view===v){ wantView=v; try{ localStorage.setItem('pty_view',v); }catch(e){} }
  document.getElementById('tabs').classList.remove('on');
}
const deck=document.getElementById('deck');
/* 🎨 浮层里的「识别命令」：收起浮层再扫当前会话的屏幕（只读当前一屏） */
function scanFromTabs(){
  document.getElementById('tabs').classList.remove('on');
  if(!SID){ toast('先打开一个会话',true); return; }
  scanCmds(SID);
}
/* 🎨 浮层里的「命令历史」：不扫描，直接翻历史（cmdSheet 本来就是这个用途） */
function histFromTabs(){
  document.getElementById('tabs').classList.remove('on');
  if(!SID){ toast('先打开一个会话',true); return; }
  cmdSheet(SID);
}
function esc(s){return (s||'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));}
function escHtml(s){return esc(s);}
/* 外网链接：壳的 WebView 不加载站外页（桥挂在整个 WebView 上，站外导航被掐），
   在页面里点 target=_blank 会毫无反应——壳里经桥交系统浏览器打开；
   浏览器里才回落新标签。安卓桥 CB.openUrl，iOS 桥 cb openExternal。 */
function extOpen(u){
  try{ if(window.CB&&CB.openUrl){ CB.openUrl(u); return false; } }catch(e){}
  try{ if(window.webkit&&webkit.messageHandlers&&webkit.messageHandlers.cb){
        webkit.messageHandlers.cb.postMessage({m:'openExternal',v:u}); return false; } }catch(e){}
  window.open(u,'_blank','noopener');
  return false;
}
/* 自建中继能力开关：/api/brand 的 caps 缺字段或整体缺失 = 官方（fail-open）。
   caps[k]===false 才藏入口——自建运营者配齐了依赖的，该亮的亮。 */
function capsOk(k){
  try{ var c=(typeof __cbBrand!=="undefined"&&__cbBrand&&__cbBrand.caps)||null;
       return !c||c[k]!==false; }catch(e){ return true; }
}
/* caps 落地：/api/brand 回来后调一次（05_cmd.js cbBrand 成功回调）。
   只藏入口不藏功能路径——被藏入口的后端在自建中继上本来就是 404。 */
function capsApply(){
  function hid(id,off){ var el=document.getElementById(id); if(el) el.style.display=off?'none':''; }
  hid('boxPlan', !capsOk('plans'));     /* 套餐卡（会员档位/额度/开通按钮） */
  hid('vsQuota', !capsOk('plans'));     /* 设备列表头上的读秒角标 */
  hid('trNVoice', !capsOk('tts'));      /* 语音播报开关行 */
  hid('cbptt', !capsOk('asr'));         /* 按住说话悬浮钮 */
}
/* 高亮搜索词：先转义再包 <mark>，关键词里的 <>& 也是文本，不会变成标签 */
function hlq(s,q){
  const t=escHtml(String(s==null?'':s)); q=String(q||'').trim();
  if(!q) return t;
  const eq=escHtml(q); let out='', rest=t, i;
  while((i=rest.toLowerCase().indexOf(eq.toLowerCase()))>=0){
    out+=rest.slice(0,i)+'<mark class="hl">'+rest.slice(i,i+eq.length)+'</mark>';
    rest=rest.slice(i+eq.length);
  }
  return out+rest;
}
function ago(ts){if(!ts)return'';const d=Date.now()/1000-ts;if(d<60)return'刚刚';if(d<3600)return Math.floor(d/60)+'分钟前';if(d<86400)return Math.floor(d/3600)+'小时前';return Math.floor(d/86400)+'天前';}
/* ── 请求层：一律 XHR。老安卓 WebView（≤Chromium 66）的 fetch 既不带 cookie 也不落
      Set-Cookie（0.5.12 在登录页踩过一次），经中继的接口全会撞 401 登录墙。 ── */
/* 页面不可见 = 用户没在看：页面层一律不发请求。
   中继按「某自然分钟里有过请求」累计使用时长，后台残留的定时器（老 WebView 尤其爱
   把 setInterval 压到分钟级而不是完全停）会让人息屏了还在计时长。这里做成总闸：
   所有请求都走 xj/xjX/xget/xpost，后台直接返回失败，不产生任何网络流量。
   后台提醒由安卓壳的前台服务（/api/bg_push，中继豁免计费）负责，不依赖页面；
   回前台时 visibilitychange 会主动补拉一轮。
   CBVIS = 壳报的前后台（iOS 的 WKWebView 后台时 document.hidden 不变，只靠壳靠谱）。
   SHELLBG = 壳明确说了「进后台」——这条比心跳权威，心跳不许把它顶回来。
   ALIVE = 壳心跳的时间戳（壳每 2.5~3s 推一次；心跳一停 = 壳被挂起 = 当后台）。 */
var CBVIS=true;
var SHELLBG=false;
var ALIVE=0, HASBEAT=false;
function hiddenNow(){ return document.hidden || !CBVIS; }   /* 页面看不见（浏览器切走）或壳报的后台 */
/* 「在用」的另一半：页面虽然亮着，但没人碰（设备摆桌上当看板、屏幕常亮挂着）。
   以前只看 visibility——只要页面在前台，轮询就按墙钟每分钟记一分钟，一台放在
   桌上没动过的手机一上午能烧掉六个小时。现在只在「最近一分钟里有人碰过屏幕」
   时才真正计时；挂着的轮询带 a=0，中继和开发机照样应答，但不记分钟。
   一分钟是宽限：中继按自然分钟记账，发完指令看输出最多多算紧接的一分钟；
   超过就冻结，再碰屏幕才恢复。只认真实输入（点/按/滚轮），不认 scroll——
   终端输出自动滚动也会发 scroll 事件，那样又变成永远「在用」了。 */
var LASTACT=Date.now(), ACT_MS=60*1000;
/* 最近一次「任意请求拿到了响应」的时刻：证明中继/开发机至少有一端可达、还在记账。
   读秒要同时满足「前台 + 最近一分钟碰过 + 这里还新鲜」，否则冻结（离线时不能虚构时间）。 */
var LASTOK=Date.now();
/* 最近一次「会记分钟的请求」成功的时间：读秒靠它判断「当前这一分钟中继记账了没有」。
   页面上的轮询（消息/画面/推流）在中继和开发机那边都是豁免计费的长连接，盯着看
   一整天也不记分钟；只有真正的动作（发消息/按键/开会话/看列表…）才记。
   所以不能拿「有请求」当记账依据，否则停着读输出时倒计时会每过一分钟先走 60 秒
   再弹回去。 */
var LASTBILL=0;
/* 和中继/开发机的豁免口径一致：长连接、探针、静态资源、本机上报、套餐查询都不记账。
   ext_snap 是「原样」视图 1 秒一次的终端帧：和实时画面是同一件事（看终端），
   三处豁免表必须一起加，否则这里以为记了账、账上没记，倒计时会凭空往下走。 */
var BILL_SKIP=['/api/ext_push','/api/ext_msg/','/api/ext_live/','/api/ext_screen/',
  '/api/ext_snap/','/api/ext_term_out/','/api/ext_pref','/api/seen',
  '/api/page_ver','/api/version','/api/apk_ver','/api/ping','/api/plan',
  '/api/bg_push','/api/session_event','/static/','/r/',
  /* 中继自己接的 POST：走到中继就地处理，不进隧道，也就不记账。
     rtc_offer 开屏就打一次（打洞协商），以前没豁免 → 页面当成"记了一格"往下读 10 秒，
     而中继账上一分没动；读秒只许往下修，于是整块显示永久比账上少 10 秒（2026-11-09
     实测：客户端 8:19:40 / 中继 8:19:50）。ptt_fix/ptt_proj/llm/pay_* 同理。 */
  '/api/rtc_','/api/ptt_fix','/api/ptt_proj','/api/llm','/api/pay_create','/api/pay_status'];
function billWant(ep,opt){
  /* 看不计时、写计时（2026-11-09）：只读请求（GET/HEAD）一律不算账——
     看消息、翻会话、看输出、开设置页都不扣时间；只有发消息/按键/开会话/
     跑命令这类写操作才记格。口径和中继 tunnel()、开发机 _use_touch 一致。 */
  var m=String((opt&&opt.method)||'GET').toUpperCase();
  if(m==='GET'||m==='HEAD'||m==='OPTIONS') return false;
  var s=String(ep||'');
  for(var i=0;i<BILL_SKIP.length;i++) if(s.indexOf(BILL_SKIP[i])>=0) return false;
  return true;
}
(function(){
  /* 停手一阵再碰屏幕：马上对一次账（挂着的这几十秒中继没记分钟，锚点要立刻收回，
     不然得干等最多 30 秒的下一次轮询）。useAnchorSoon 自己做了 3 秒节流。 */
  var mark=function(){
    var was=(Date.now()-LASTACT>=5000);
    LASTACT=Date.now();
    if(was) useAnchorSoon();
    /* 自愈：页面以为自己在后台（壳报过后台、或心跳断过），可人明明正在碰屏幕——
       触摸事件只有前台才送得到。这时候如果还按「后台」处理，按键会被 awayNow
       直接吞掉、轮询也不跑，表现就是「按了键一点反应没有」。以手为证：立刻回前台。 */
    if(!document.hidden&&(SHELLBG||!CBVIS)){
      try{ window.__cbVisible(true); }catch(e){}
    }
  };
  ['touchstart','touchend','mousedown','pointerdown','keydown','wheel'].forEach(function(t){
    try{ document.addEventListener(t,mark,{capture:true,passive:true}); }
    catch(e){ try{ document.addEventListener(t,mark,true); }catch(e2){} }
  });
})();
/* 标已读是页面自己做的（开页/回前台/提醒看完），不是用户操作：一律按后台请求送，
   不记使用时长。以前它是 POST（actTag 只标 GET），结果页面挂着一分钟一条，白烧额度。 */
function seenMark(sid){
  if(!sid) return;
  try{ xj('/api/seen?a=0',{method:'POST',body:{sid:sid}}).catch(function(){}); }catch(e){}
}
/* 没人碰就把这一条请求标成「挂着看」（a=0）；只标读请求，发送/语音这类写操作照常计时 */
function actTag(u){
  if(!u||Date.now()-LASTACT<ACT_MS) return u;
  return u+(u.indexOf('?')<0?'?':'&')+'a=0';
}
function awayNow(opt){ return hiddenNow() && !(opt&&opt.away); }
function awayRes(){ return Promise.resolve({ok:false,status:0,json:null,text:'',buf:null,away:true,
  hdr:function(){ return null; }}); }
function xj(ep,opt){opt=opt||{};
  if(awayNow(opt)) return awayRes();
  if(!opt.method||opt.method==='GET') ep=actTag(ep);   /* 挂着看：不参与使用时长 */
  if(directWant(ep)&&DIRECT.mode==='dc'){
    return dcReq(ep,opt).then(function(r){
      if(r.status===0) return xjRelay(ep,opt);   // DC 通道僵死（host 重启/打洞超时）：回退中继，别把发送卡死
      /* 403 且不是额度墙：host 拒了这条路径（票据/白名单不一致，比如 notify_hist
         以前漏在 DT_PATHS 外）——中继是普通 HTTP，走它能拿到同一份数据。 */
      if(r.status===403&&!(r.json&&r.json.quota)) return xjRelay(ep,opt);
      return r;
    });
  }
  const r0=xjX(xb(ep),opt);
  if(directWant(ep)&&DIRECT.mode==='lan'&&xb(ep)!==ep){
    return r0.then(function(r){
      if(r.status===0) return xjRelay(ep,opt);
      /* 票据过期/被拒（host 403）：以前直接把 403 抛给调用方，不回落中继，
         局域网模式于是卡死在「每 1~2 秒一个 403」上（中继断了 10 分钟后就是这样，
         即使中继恢复也不会自己好）。这里立刻退出局域网模式，再走一次中继。 */
      /* 额度用完也是 403，但那不是票据坏了：退出直连 + 回落中继只会把同一个 403
         再拿一遍，用户还看不到原因。按额度处理，页面内直接给浮层。 */
      if(r.status===403&&r.json&&r.json.quota) return r;
      if(r.status===403&&directOn()){ directDown('ticket'); return xjRelay(ep,opt); }
      return r;
    });
  }
  return r0;
}
function xjRelay(ep,opt){   // 纯中继（origin 同源）路径
  return xjX(ep,opt);
}
function xjX(ep,opt){       // XHR 实现（ep 已是最终 URL：中继相对路径或局域网绝对地址，不再二次 xb 重写）
  if(awayNow(opt)) return awayRes();   // 见 awayNow：后台不发请求（覆盖绕过 xj 的直接调用）
  const t0=Date.now();                // 发出时刻：中继按请求「到达」的整分钟记账，取靠近的一端
  return new Promise(function(res){
    const x=new XMLHttpRequest();
    /* 自己主动 abort 的（局域网探测超时、推流重连）不是「网络坏了」：
       打个标记，别让它算进断网判定，否则换网/重连时会误判成没网 */
    const _abort=x.abort.bind(x);
    x.abort=function(){ try{ x.__cbAbort=1; }catch(e){} return _abort(); };
    x.open(opt.method||'GET',ep,true);
    /* 默认超时：老 WebView 的 XHR 不设 timeout 会无限挂起（直连地址半死时），
       「点开模型页没反应」就是这种挂起。慢接口（切换/扫描/语音）调用方自己放宽。 */
    x.timeout=opt.timeout||(opt.bin?30000:15000);
    if(opt.bin) x.responseType='arraybuffer';      // 二进制（xterm 快照）
    /* FormData 的 Content-Type 必须由浏览器自己带（含 boundary），手写上去 multipart 就废了 */
    if(opt.body&&!(typeof FormData!=='undefined'&&opt.body instanceof FormData))
      x.setRequestHeader('Content-Type',opt.ctype||'application/json');
    /* 额外头：语音上传要带纠错上下文（X-CB-Ctx / X-CB-Nofix） */
    if(opt.headers){ for(var _hk in opt.headers){ try{ x.setRequestHeader(_hk,opt.headers[_hk]); }catch(e){} } }
    // 对象体必须序列化：XHR.send(object) 会串成 "[object Object]"，host 端 JSON 解析必炸。
    // Blob/ArrayBuffer/FormData（语音上传等）是合法 XHR 体，绝不能动。
    if(opt.body&&typeof opt.body==='object'
       &&!(typeof Blob!=='undefined'&&opt.body instanceof Blob)
       &&!(typeof ArrayBuffer!=='undefined'&&opt.body instanceof ArrayBuffer)
       &&!(typeof FormData!=='undefined'&&opt.body instanceof FormData)) opt.body=JSON.stringify(opt.body);
    x.onreadystatechange=function(){
      if(x.readyState!==4)return;
      if(!x.__cbAbort){ try{ netFailTick(x.status, ep.indexOf('/')!==0?1:0); }catch(e){} }
      /* 第二参：绝对 URL = 局域网直连路径（中继走相对路径）。直连发不出去不算
         「没网」——它自己的债自己还（netFailTick 里拆直连回中继），别把好端端的
         中继链路陪葬进壳离线页（2026-11-18 aliyun 误建直连整 App 卡死）。 */
      /* 计费端可达的实证：2xx/3xx/403（额度墙也是账算到了）才算；502/超时/断网不算。
         /api/plan 不算——中继自己就能回它，开发机掉线时它照样成功，但那时没有任何
         请求能记账，读秒必须停（见 useActive）。 */
      const good=((x.status>=200&&x.status<400)||x.status===403);
      if(good){
        if(ep.indexOf('/api/plan')<0) LASTOK=Date.now();
        if(ep.indexOf('a=0')<0&&billWant(ep,opt)){
          LASTBILL=Math.max(LASTBILL,t0);
          useTickStart();            /* 记了账就读一格，读完整好落在服务器的数上 */
          /* 这一分钟第一次记账：立刻对一次账，读秒马上走起来（别等最多 30 秒的轮询） */
          if(!USEST.tk) useAnchorSoon();
        }
      }
      // bin 请求（responseType='arraybuffer'）读 responseText 浏览器必抛 DOMException，
      // res() 不执行 → rawSnap/rawPoll 的 await 永久挂起 → 原样页空白（e1f77fe 迁移引入）。
      let d=null,txt=null;
      if(!opt.bin){ txt=x.responseText; try{d=JSON.parse(txt)}catch(e){} }
      /* 今日时长用完：中继对 API 回 403+quota，对整页导航回「额度用完」页。
         发现超额就重新加载当前地址，让中继把升级页直接顶上来（不留在会话页空转）。 */
      /* 直连（局域网绝对地址）拿到的额度墙没有中继页面可顶：刷过去只会变成「开发机离线」，
         或者干脆白屏。这类在页面内直接盖浮层。 */
      /* 今日时长用完（写操作才会拿到 403+quota）：页面内盖升级浮层，不再整页重载。
         以前这里 reload 是想让中继把升级页顶上来，但「看不计时」之后重载后的 GET 一定
         放行，用户再点一次又 403 → 重载循环，还把当前会话屏丢了。 */
      if(x.status===403&&d&&d.quota){ quotaSheet(d); }
      res({ok:x.status>=200&&x.status<300,status:x.status,json:d,text:txt,buf:x.response,
        hdr:function(n){try{return x.getResponseHeader(n)}catch(e){return null}}});
    };
    x.send(opt.body||null);
  });
}
let QUOTA_WALL=false;
function quotaWall(){
  if(QUOTA_WALL) return;
  QUOTA_WALL=true;
  setTimeout(function(){ pageRemember(pageIdx); location.reload(); },80);   // 80ms：先把当前执行栈走完（重载后回到原来那一屏）
}
function quotaSheet(q){   // 直连/P2P 额度墙：页面内浮层（不依赖中继能打开页面）
  /* 把中继/开发机给的原始额度带回落进开发机日志：再出现「明明没到额度却说用完」时，
     日志里直接能看到 used/cap 和来源，不用翻库猜。 */
  try{ if(window.__cbDbg) window.__cbDbg('Nquota '+JSON.stringify(q||{})); }catch(e){}
  /* 开发机本地估算（中继连不上）不顶全屏浮层：那是估算，不是账。给个提示就够了，
     否则「新绑定/刚重连」时本地缓存一过期就整屏挡住，看着像彻底没额度了。 */
  if(q && (q.fallback || q.estimate)){
    var now=Date.now();
    if(now-(window._qFallAt||0)>8000){   // 限流：别每发一条都弹一次
      window._qFallAt=now;
      try{ toastWarn('额度暂时没对上账（开发机本地估算），稍后自动恢复'); }catch(e){}
    }
    return;
  }
  /* 数字自己对不上就不顶全屏：给了 used/cap 却没到上限 = 陈旧或串了来源的判定，
     顶上去只会让人以为彻底没额度了（2026-11 实测：免费账号中继账上 15/60，手机上却
     整屏「今日使用时长已用完」）。没有权威数字（cap=0）同样不顶。
     载荷形状：中继/开发机是 {"quota":{used,cap},…}，旧接口/直连把数字放顶层，两处都认。 */
  var qq=(q&&q.quota)||q||{};
  /* 新中继/host 带秒字段（used_s/cap_s，和 use_unit 同口径）；老端只有分钟（used/cap）。
     秒在就以秒为准——分钟是四舍五入过的，可能明明到了上限还显示 used<cap。 */
  var _sec=(qq.used_s!=null||qq.cap_s!=null);
  var _cu,_cc;
  if(_sec){
    _cu=parseInt(qq.used_s||0,10)||0; _cc=parseInt(qq.cap_s||0,10)||0;
  }else{
    _cu=(parseInt(qq.used||0,10)||0)*60; _cc=(parseInt(qq.cap||0,10)||0)*60;
  }
  if(!_cc||(_sec?(_cu<_cc):(qq.used!=null&&_cu<_cc))){
    /* 已经顶着的浮层要撤掉：浮层是「一次性」的，判定改了不会自己消失，
       之前误判过的那台手机就会一直卡在「用完了」——撤掉并恢复直连。 */
    var wall=document.getElementById('qwall');
    if(wall){
      wall.remove(); QUOTA_WALL=false;
      DIRECT.cool=0;                       // directDown 给了 1 分钟冷却：判定翻案就立刻重探
      try{ if(!directOn()) directTry(); }catch(e){}
    }
    /* 不弹提示：数字对不上是判定端的问题，用户没做错什么，喊「额度异常」只会
       让人以为自己的额度坏了（2026-11 实测就是这样被误报的）。留一条探针进
       开发机日志（Nquota）用于排查，界面保持安静。 */
    try{ if(window.__cbDbg) window.__cbDbg('Nquota-mis '+(q&&JSON.stringify(q)||'')); }catch(e){}
    return;
  }
  if(document.getElementById('qwall')) return;
  QUOTA_WALL=true;
  directDown('quota',true);   // 静默退出直连：额度判定由 host 给的，中继那边同样会拒
  var el=document.createElement('div');
  el.id='qwall';
  el.style.cssText='position:fixed;left:0;right:0;top:0;bottom:0;z-index:60;'
    +'background:#000c;display:flex;align-items:center;justify-content:center;padding:22px';
  var cap=_cc, used=_cu;
  el.innerHTML='<div class="card" style="max-width:320px;text-align:center;padding:20px 18px">'
    +'<div style="font-size:30px;line-height:1.2">⏳</div>'
    +'<div style="font-size:16px;font-weight:600;margin:8px 0 6px">今日使用时长已用完</div>'
    +'<div style="font-size:13px;color:#838383">'
    +(cap?('已用 '+useDurTick(used)+' / '+useDurTick(cap)):'')
    +'<br>'+(PLANS_OFF?'可等明天恢复（语音也一样）。'
                      :'可等明天恢复，或买使用时长包 / 升级套餐继续用（语音也一样）。')+'</div>'
    +(PLANS_OFF?'':'<button onclick="quotaGo()" style="margin-top:14px;width:100%;padding:11px;'
    +'border:0;border-radius:9px;background:#238636;color:#fff;font-size:15px;font-family:inherit">'
    +'买使用时长包</button>')
    +'<button onclick="location.reload()" style="margin-top:8px;width:100%;padding:10px;'
    +'border:1px solid #191919;border-radius:9px;background:transparent;color:#838383;'
    +'font-size:14px;font-family:inherit">刷新</button></div>';
  document.body.appendChild(el);
}
function quotaGo(){       /* 额度用完了：能拼出中继地址就去买包，拼不出就重载等中继页面顶上 */
  if(PLANS_OFF){ toast('套餐升级暂未开放，先等等'); return; }
  var go=function(b){
    b=String(b||'').replace(/\/+$/,'');
    if(b){ sheetOpen(b+'/r/plans?emb=1','套餐与续费'); return; }
    location.reload();
  };
  if(PLAND&&PLAND.relay_base){ go(PLAND.relay_base); return; }
  toast('正在连接中继…');
  xget('/api/plan',function(d){
    go(d&&d.relay_base);
  });
}
/* ── 标题栏电量：壳走 CB.battery()（BatteryManager，无权限），桌面浏览器回落 navigator.getBattery ── */
var BATT={native:null,last:-1};
function battPaint(l,ch){
  var box=document.getElementById('batt'),bar=document.getElementById('battBar'),
      txt=document.getElementById('battTxt');
  if(!box||!bar||!txt) return;
  box.style.display='';
  bar.style.background=ch?'currentColor':'linear-gradient(to right,currentColor '+(l>0?l:1)+'%,transparent '+(l>0?l:1)+'%)';
  txt.textContent=l;   // 只显示数字，% 不显示（图标本身就是电池）
  box.className='batt'+(ch?' chg':(l<=20?' low':(l<=40?' mid':'')));   // >40 绿 / 20-40 黄 / ≤20 红橙；充电恒绿
  BATT.last=l;
}
function battTick(){
  try{
    if(window.CB&&CB.battery){
      var s=CB.battery();
      if(s&&s.length){ var d=JSON.parse(s); if(d&&typeof d.l==='number'&&d.l>=0) battPaint(d.l,!!d.c); return; }
    }
  }catch(e){}
  if(!BATT.native&&navigator.getBattery){                 // 桌面浏览器 / 老 WebView 回落
    navigator.getBattery().then(function(b){ BATT.native=b;
      b.addEventListener('levelchange',battTick);
      b.addEventListener('chargingchange',battTick);
      battTick();
    }).catch(function(){});
    return;
  }
  if(BATT.native) battPaint(Math.round(BATT.native.level*100),BATT.native.charging);
}
setInterval(function(){ if(!hiddenNow()) battTick(); },30000);   // 后台不碰：省电
document.addEventListener('visibilitychange',function(){ if(!hiddenNow()) battTick(); });
battTick();
/* 壳的默认 WebChromeClient 不弹 confirm/alert 框（静默 false，点了没反应）：
   危险操作改两击确认——第一击提示，4 秒内再点一次才执行。 */
function armConfirm(btn,msg,fn){
  if(btn.__arm){ btn.__arm=0; if(btn.__armT)clearTimeout(btn.__armT); fn(); return; }
  btn.__arm=1; toast(msg+'：再点一次确认');
  btn.__armT=setTimeout(function(){btn.__arm=0;},4000);
}
/* 通用确认浮层（#dlg）：删除会话这类不可撤销的动作，两击确认太轻——要给一句
   说清楚后果的话，再点「删除」。回调存在 DLGF，取消/点遮罩就丢掉。 */
let DLGF=null, DLGC=null;
function askConfirm(title,msg,okText,danger,fn,onCancel){
  DLGF=fn||null; DLGC=onCancel||null;
  document.getElementById('dlgTitle').textContent=title||'确认';
  document.getElementById('dlgMsg').innerHTML=msg||'';
  const b=document.getElementById('dlgOk');
  b.textContent=okText||'确定';
  b.style.background=danger?'#b62324':'';
  b.style.borderColor=danger?'#b62324':'';
  document.getElementById('dlg').classList.add('on');
}
function dlgClose(){ document.getElementById('dlg').classList.remove('on'); DLGF=null; DLGC=null; }
/* 「取消」按钮：有的确认要两种明确表态（允许 / 拒绝），取消也算一次表态。
   点遮罩只关掉，不触发回调——手滑不该替用户做决定。 */
function dlgCancel(){ const f=DLGC; dlgClose(); if(f){ try{ f(); }catch(e){} } }
function dlgRun(){ const f=DLGF; dlgClose(); if(f){ try{ f(); }catch(e){} } }
/* ── 面板导航 ── */
/* 记住「人停在哪一屏」。重载的来源不止页面自己：ext.html 一改，pverCheck 60 秒内
   自更新重载；壳的 Bridge.onReload（刷新/重试）、断网恢复的 loadServer、12 秒离线
   重试、WebView 进程被系统回收，也都会重新加载页面。以前只有「长跑自愈 / 自更新」
   写 cb_keep_page，别的来源重载回来一律落回会话页——「停在设置页，过一会儿自己
   跳回会话页」就是这么来的（2026-11-08 iOS 实测：日志里每分钟一次 Cfg 开屏）。
   现在每次换页都记，任何来源的重载都能回到原来那一屏。 */
function pageRemember(i){
  try{ sessionStorage.setItem('cb_keep_page',String(i)); }catch(e){}
  try{ localStorage.setItem('cb_page_at',JSON.stringify({p:i,at:Date.now()})); }catch(e){}
  try{ scrollRemember(); }catch(e){}   // 重载前的最后一刻再存一次滚动位置（不等 1 秒的心跳）
}
/* 重载回来把「读到哪儿」也接上：页面自更新 / 壳重载 / WebView 被回收都会把会话页拉回
   底部，正在往上翻历史的人得重新滑一遍（2026-11-09）。记 {sid,view,top}，新页面第一轮
   内容渲染出来后把 scrollTop 放回原处；内容还没长够就先挂着，等下一轮再放。
   - 只在同一会话、同一视图、3 分钟内有效；用户自己一碰屏幕就作废，别跟人抢。
   - 恢复期间不许覆盖存档（否则先存了个 0，把原本的位置吃掉）。 */
let SCROLL_KEEP=null;
try{
  const raw=sessionStorage.getItem('cb_keep_scroll')||localStorage.getItem('cb_scroll_at');
  const m=raw?JSON.parse(raw):null;
  const age=m?Date.now()-Number(m.at||0):1e9;
  if(m&&typeof m.top==='number'&&m.top>=0&&age>=0&&age<180000) SCROLL_KEEP=m;
}catch(e){}
let SCROLL_WANT=SCROLL_KEEP?Math.round(SCROLL_KEEP.top):null;   // 待恢复的滚动位置（null=不用恢复）
let scrollLastKey='';
function scrollRemember(){
  if(pageIdx!==1||!SID||SCROLL_WANT!==null) return;
  const b=ctrlBox(); if(!b) return;
  const top=Math.round(b.scrollTop), key=SID+'|'+view+'|'+top;
  if(key===scrollLastKey) return;
  scrollLastKey=key;
  const rec={sid:SID,view:view,top:top,at:Date.now()};
  try{ localStorage.setItem('cb_scroll_at',JSON.stringify(rec)); }catch(e){}
  try{ sessionStorage.setItem('cb_keep_scroll',JSON.stringify(rec)); }catch(e){}
}
function scrollRestoreTry(){
  if(SCROLL_WANT===null) return;
  if(pageIdx!==1) return;
  if(SCROLL_KEEP&&SCROLL_KEEP.sid&&SID&&SCROLL_KEEP.sid!==SID){ SCROLL_WANT=null; return; }   // 换了会话
  if(SCROLL_KEEP&&SCROLL_KEEP.view&&SCROLL_KEEP.view!==view){ SCROLL_WANT=null; return; }     // 换了视图
  const b=ctrlBox(); if(!b) return;
  const want=SCROLL_WANT;
  if(b.scrollHeight-b.clientHeight+2<want) return;   // 内容还没长够：不提前放，等下一轮
  SCROLL_WANT=null;
  b.scrollTop=want;
  scrollLastKey='';                                   // 让下一轮照常记新位置
}
/* 换页落一行开发机日志：以后再「自己跳页」，日志里直接看到是翻页(tap/scroll)
   还是代码(flow)换的、从哪屏到哪屏。低频（只在真换页时发一次）。 */
function pageLog(a,b,opt){
  try{ if(a===b) return;
    window.__cbDbg&&window.__cbDbg('Pg '+a+'>'+b+' '+(!opt?'flow':(opt.fromScroll?'scroll':(opt.smooth?'tap':'set'))),24);
  }catch(e){}
}
/* 重载后想回到「终端页 / 设置页」：这两页现在是子页，开屏落点是列表，
   由 openTermPage/openSettings 在 goPage 之后盖回来（见启动段 wantSub）。 */
/* 开屏落点（pg/keep）：挂到 5 秒后的 Cfg 探针一起发。Boot 那条探针在刚开页时
   多半还在走中继，中继自己应答 /api/plan、不转发，开发机日志里看不到——
   Cfg 发的时候通常已经切到局域网直连，能落进日志。 */
let BOOT_INFO='';
/* 左滑序列只剩两格：列表(0) / 会话(1)。终端列表和设置改成了从列表入口点开的
   全屏子页（见 openTermPage / openSettings），不再占格子。 */
function pageSlot(i){
  return i;
}
function goPage(i,opt){
  const fromPg=pageIdx;
  pageIdx=Math.max(0,Math.min(1,i));
  /* 从列表滑进会话页，而当前 SID 不是开着的会话（没选过/选的是已退出的）：
     直接进「最近进入」的那一个。一个开着的都没有就留在列表。
     注意别在这里 return：下面那一段才是把输入框/键条/语音按钮摆出来的地方，
     一旦 switchTo 中途出岔子，人就停在「会话页但底下什么都没有、点哪都没反应」。 */
  if(pageIdx===1&&opt&&opt.fromScroll&&(!SID||OPEN.indexOf(SID)<0)){
    if(OPEN.length) switchTo(OPEN[0],{quiet:1});
    else { pageIdx=0; toast('没有打开着的会话'); }
  }
  /* 自动听写只属于会话页：切走立刻收麦 */
  if(pageIdx!==1){ try{ window.__cbAutoEnd&&window.__cbAutoEnd(); }catch(e){} }
  /* 面板位置 = 原生横向滚动位置（一格一屏，见 #deck 的 scroll-snap）。
     opt.smooth 是点导航/方向键这类「明确跳页」用的；滚动落定后回填用瞬移。 */
  const dw=deckSlotW();
  const slot=pageSlot(pageIdx), want=slot*(dw||1);
  if(!dw){
    /* 还没拿到面板宽度（WebView 首帧、后台冻结后恢复）：这时候按 1 像素定位，会被
       scroll-snap 吸附到第 0 格（列表页），之后再被 deckAlign 拽回来——人看到的就是
       「过一会儿自己跳回会话页」。先藏着，等宽度有了再定位再显示。 */
    try{ window.__cbDbg&&window.__cbDbg('Gz'+pageIdx,24); }catch(e){}
    deck.style.visibility='hidden';
    let gn=0;
    (function gwait(){
      if(deckSlotW()>0){ try{ deckAlign(); }catch(e){} deck.style.visibility='visible'; return; }
      if(gn++<60) setTimeout(gwait,50);
      else deck.style.visibility='visible';
    })();
  }else{
    if(Math.abs(deck.scrollLeft-want)>1){
      if(opt&&opt.smooth){ deckNavUntil=Date.now()+900; deck.scrollTo({left:want,behavior:'smooth'}); }
      else deck.scrollLeft=want;
    }
    deck.style.visibility='visible';   // 定位完成才显示，避免首帧闪出错位面板
  }
  openDots();
  const ctrl=(pageIdx===1);
  document.getElementById('tabbtn').style.display=ctrl?'':'none';
  document.getElementById('keys').style.display=ctrl?'':'none';
  if(ctrl) kSetLevel(0);   // 进会话页：快捷键条默认收起，只留拉手
  document.getElementById('bar').style.display=ctrl?'':'none';
  /* 输入框/键条/语音按钮全挂在 ctrl 上：滑到会话页却「底下啥都没有」时，
     靠这行分辨是 goPage 没跑到、还是跑到了但元素没量到高度（2026-11-13）。 */
  try{ const be=document.getElementById('bar');
       window.__cbDbg&&window.__cbDbg('Go'+pageIdx+' bar='+(be.style.display||'css')
         +' h'+Math.round(be.offsetHeight)+' vw'+window.innerWidth
         +' sc'+(window.visualViewport?visualViewport.scale.toFixed(2):'?'),40); }catch(e){}
  document.body.classList[ctrl?'add':'remove']('pg1');   // 语音按钮只在会话页显示（见 #cbptt / shim CSS）
  /* 键盘开着时从会话页切到列表页（或反过来）：上推量没变、摆法变了，重排一次 */
  try{ if(typeof kbLayout==='function') kbLayout(); }catch(e){}
  try{ pendRender(); }catch(e){}   // 会话页浮条只在会话页显示
  deck.style.bottom=ctrl?'':'0';   // 只有控制页让位给快捷键条+输入框；列表/详情铺满到底
  if(!ctrl) document.getElementById('tabs').classList.remove('on');
  syncBarH();
  fitHeader();
  if(pageIdx===0){ loadListData(true); devLoad(); }   // 列表（含顶部开发机选择条）：15 秒内刚刷过就只重画
  /* 回到会话页：一次性重同步。推送模式下事件只推「变化」，人在别的页时收到的
     live/hist 会被 renderLive/renderHist 挡掉；这里主动补拉一次，回来就是全的，
     不用等下一段输出才补齐。 */
  if(ctrl&&fromPg!==1&&SID){
    try{ if(fullColor){ liveflowPoll(true); } else { screenPoll(true); } poll(true); }catch(e){}
  }
  usePaint();                              // 倒计时：只在会话页（麦克风页）显示
  try{ if(window.updPaint) updPaint(); }catch(e){}   // 新版本小胶囊：设置页隐藏
  try{ termPageSync(); }catch(e){}         // 终端浮层跟着面板走（离开终端页收起、回来恢复）
  try{ termDotSync(); }catch(e){}          // 列表页「终端」入口的数量
  try{ syncCloseBtn(); }catch(e){}         // 会话页才显示「关闭会话」
  /* 滑走了就把屏外输入框的焦点收掉（键盘跟着收）：不然重画/滚动会为了露出它把页面拽回去 */
  try{ if(typeof blurOffscreenInput==='function') blurOffscreenInput(); }catch(e){}
  pageRemember(pageIdx);
  pageLog(fromPg,pageIdx,opt);
  try{ trackPage(trkPageNow()); }catch(e){}   // 埋点页面维 + 上一页停留时长
}
/* 悬浮按钮弹出的"实时/原样/字号"浮层 */
function toggleTabs(e){
  if(e) e.stopPropagation();
  document.getElementById('tabs').classList.toggle('on');
}
document.addEventListener('click',e=>{
  if(!e.target.closest('#tabs,#tabbtn')) document.getElementById('tabs').classList.remove('on');
});
/* 按键条要贴着输入条上沿：输入框会长高，用真实高度顶上去。
   kmore 在 #keys 内部（display 切换，布局立即可测），高度自适应无需特判。
   面板下沿也跟着 --dockh 上收，贴底看的人要重新贴底（见函数内注释）。 */
function syncBarH(){
  const b=document.getElementById('bar'), k=document.getElementById('keys');
  const pg=document.getElementById('pgCtrl');
  /* 量在 --dockh 改写之前：这时面板还是旧高度，贴底才是真的贴底。
     收起/展开、输入框长高都会把面板下沿往上收：正贴着底看的人十有八九是在看
     「要你选」的弹层、或者按 ⇧Tab 后终端最后一行 auto mode on。滚动位置不会自己
     跟着变，不收完重新贴底，最后那几十像素就被挤出可视区，看着像被键条盖住了。
     往上滚着看历史的人不算贴底（差 >8px），不能被拽回底部。 */
  const pin=!!pg&&(pg.scrollTop+pg.clientHeight>=pg.scrollHeight-8);
  document.documentElement.style.setProperty('--barh',(b.offsetHeight||46)+'px');
  document.documentElement.style.setProperty('--dockh',((b.offsetHeight||46)+(k.offsetHeight||50))+'px');
  if(pin){ pg.scrollTop=pg.scrollHeight; }   /* 贴着底的：按新高度重新贴底 */
}
/* 输入框跟着字长往上长：先归零再量（不归零 scrollHeight 只会越量越大），
   上限跟屏幕走（约三分之一屏）。超过就在框里滚，别把整页顶没。
   语音插入、草稿找回、清空都走这里，保证「有多少字就多高」。 */
function fitTxt(){
  const t=document.getElementById('txt'); if(!t) return;
  /* 上限按「看得见的高度」算：键盘弹起时 iPhone 的 innerHeight 仍是整屏 852，
     只有 visualViewport 会缩到 455——按 innerHeight 算，输入框能长到半屏高，
     会话区被顶没，多出来的那几行等于长到看不见的地方去了。 */
  const vh=Math.round((window.visualViewport&&window.visualViewport.height)||window.innerHeight||640);
  const min=44, max=Math.max(120, Math.round(vh*0.32));
  t.style.height='auto';
  const h=Math.max(min, Math.min(max, t.scrollHeight||min));
  t.style.height=h+'px';
  /* 框高过发送键两倍才给「清空」（用户口径）：按发送键实测高度算，改版也不会对歪。 */
  const clr=document.getElementById('clr'), sb=document.getElementById('send');
  if(clr) clr.style.display=(h>(sb?sb.offsetHeight:44)*2)?'':'none';
  syncBarH();
}
/* 清空输入框：两击确认（壳里 confirm() 弹不出来，和删会话同一套做法） */
function clearTxt(btn){
  const t=document.getElementById('txt'); if(!t) return;
  armConfirm(btn,'清空输入框',function(){
    t.value='';
    try{ t.dispatchEvent(new Event('input',{bubbles:true})); }catch(e){ try{fitTxt();}catch(_){} }
  });
}
/* 摸拉手不该把输入法叫出来：输入框还留着焦点（输入法已被返回键收起）时，Android WebView
   会把「点了页面」当成再点输入框，或者因为按键条变高把输入框滚进视野，重新弹出输入法，
   正好盖住刚展开的键。动布局前先把焦点摘掉。
   键盘本来就开着（termInset>0）就别摘：那是正在打字、多半只是想多要几个键。 */
function kbDismiss(){
  try{
    const ae=document.activeElement;
    if(!ae) return;
    if(ae.tagName!=='TEXTAREA'&&ae.tagName!=='INPUT') return;
    if(termInset()>0) return;
    ae.blur();
    if(window.CB&&CB.hideKb) try{ CB.hideKb(); }catch(e){}
  }catch(e){}
}
/* 快捷键条两档：0 收起（默认，只留拉手）/ 1 展开**全部**（主键排 + 更多键一起出）。
   点拉手切换；上拉展开、下滑收起。早先是 0→1→2 三档，要再点一次才见到 Esc/←/→，
   手快的人以为第一次点没反应。 */
var kLevel=0;
function kSetLevel(n){
  kLevel=(n|0)>0?1:0;
  document.getElementById('keys').classList.toggle('kopen',kLevel===1);
  syncBarH();
}
function kMoreToggle(open){
  if(!document.getElementById('kmore')) return;
  kbDismiss();
  if(open===true) kSetLevel(1);
  else if(open===false) kSetLevel(0);
  else kSetLevel(kLevel?0:1);
}
/* 拉手：点一下（桌面 click / 触屏 tap）切换。触屏那条路已在 touchend 里处理过，
   随后的 click 要忽略，否则一次点击会切两次（等于没动）。 */
function kGrabClick(){
  if(window.__kGrabTouch && (Date.now()-window.__kGrabTouch)<600) return;
  kMoreToggle();
}
function fitHeader(){
  document.getElementById('deck').style.top=document.querySelector('header').offsetHeight+'px';
}
/* 转屏 / 窗口宽度变化：把当前页重新对齐到它的格子。原生滚动位置是像素值，
   宽度一变就会停错位；只在真正需要时瞬移，别去抢普通翻页的 smooth 动画。 */
/* 最近一次「布局变化」（转屏/键盘/终端页收放/开屏重排）导致面板被挪动的时刻。
   这段时间内的位移不当成用户翻页意图；超出这段时间面板却停在别的格子上，就认它是
   真滑过去的——以前只认 touchmove 的横拖标志，慢滑/惯性大的滑动漏判，面板停在
   设置页而 pageIdx 还是会话页，之后一有 resize（键盘）就被拽回会话页。 */
let deckLayoutAt=Date.now();
/* 一格的真实宽度。scrollLeft 量的是 #track 内容坐标系，所以要和内容盒子对齐，
   不能随手读一次 #deck.clientWidth：安卓实测这两个值会短暂对不上（312 ↔ 625），
   用 clientWidth 除出来的页码就会把「设置页」算成「会话页」，落定后又被
   deckAlign 拽回去——「滑到设置页，过一会儿自己跳回会话」就是这么来的。
   track.scrollWidth / 可见格子数 跟 scrollLeft 同源，露出的格子一律按 100% 算。 */
function deckSlotW(){
  const tr=document.getElementById('track');
  if(tr&&tr.scrollWidth>0){
    let n=0;
    for(let i=0;i<tr.children.length;i++){
      const el=tr.children[i];
      if(el&&el.offsetWidth>0) n++;
    }
    if(n>0) return tr.scrollWidth/n;
  }
  return deck.clientWidth||0;
}
let deckW0=0;   /* 上一次对齐时的格子宽度（取整后）：宽度没变，位置就不该是被布局改的 */
function deckAlign(){
  const w=deckSlotW(); if(!w) return;
  deckLayoutAt=Date.now();
  const wi=Math.round(w), wChanged=(deckW0!==wi);
  deckW0=wi;
  /* 宽度没变、面板却稳稳停在另一格上：这是人滑过去停住的，不是布局重算的结果。
     以前这里一律按 pageIdx 拽回会话页，用户 2026-11-09 报的「切到别的页又自己
     弹回会话页」就是这么来的（日志里一串 Align pg1 x>313）。现在跟着它换页：
     人停在哪一屏就是哪一屏。半格中间的位置不认，仍然按 pageIdx 摆正。 */
  if(!wChanged){
    const sl=deck.scrollLeft/w, near=Math.round(sl);
    if(Math.abs(sl-near)<0.05){
      const i=deckPageFromScroll();
      if(i!==pageIdx){ goPage(i,{fromScroll:1}); return; }
    }
  }
  const want=pageSlot(pageIdx)*w;
  if(Math.abs(deck.scrollLeft-want)>1){
    /* 自己把面板拉回去也要留一行：网页原生 scroll-snap 在布局变化后会重新算位置，
       停了设置页却被扯回会话页时，日志里能分辨是谁动的（Settle/Align 成对出现）。 */
    try{ window.__cbDbg&&window.__cbDbg('Align pg'+pageIdx+' '+Math.round(deck.scrollLeft)+'>'+Math.round(want)+' w'+Math.round(w),40); }catch(e){}
    deck.scrollLeft=want;
  }
}
window.addEventListener('resize',()=>{
  /* 键盘/地址栏/转屏都会走到这里：留一行，「过一会儿自己跳页」时能分辨是不是布局变化 */
  try{ window.__cbDbg&&window.__cbDbg('Rz pg'+pageIdx+' sl'+Math.round(deck.scrollLeft)+' w'+deck.clientWidth,40); }catch(e){}
  fitHeader();syncBarH();deckAlign();
});
document.addEventListener('visibilitychange',function(){
  if(document.hidden||!deck.clientWidth) return;
  try{ window.__cbDbg&&window.__cbDbg('Vz pg'+pageIdx+' sl'+Math.round(deck.scrollLeft)+' w'+deck.clientWidth,40); }catch(e){}
  deckAlign();   // 后台回来，WebView 可能重排过：先摆正再让人看
});
/* 横滑翻页已交给 #deck 的原生 scroll-snap（拖拽在合成线程，主线程忙也不掉帧）。
   这里只做两件事：滚动途中轻量更新页码点（只在页号变了才写 DOM），
   停稳后补上目标页的初始化（拉数据、渲染设置页等，拖动中不做重活）。 */
let deckSettleT=0, deckSeen=-1, deckDown=0, deckTouchAt=0, deckNavUntil=0, deckMidN=0;
/* 这一次手指到底有没有「横着拖」：起点到当前点的位移。以前只看「1.5 秒内碰过屏幕」，
   在设置页点一下开关就算用户翻页——同期布局变化（终端页出现/收起、键盘、字体）
   让原生 scroll-snap 把面板挪了半格，落定就直接换页（iOS 实测：3>1 scroll）。
   现在只认真正的横向拖动。 */
let deckSwipeX=0, deckTX=0, deckTY=0;
let deckTouchN=0;   /* 面板上收到过几次触摸：心跳里能看到「人的手到底有没有落到面板上」 */
function deckPageFromScroll(){
  const w=deckSlotW()||1;
  return Math.max(0,Math.min(1,Math.round(deck.scrollLeft/w)));
}
function deckDots(i){
  if(i===deckSeen) return;
  deckSeen=i;
  openDots();
}
/* 手指到底还按着没有。deckDown 只是个计数器：安卓 WebView 上偶尔漏一次 touchend
   （手势被原生滚动接管、渲染中途重排、touchcancel 打到别的节点），它就一直挂着 >0，
   于是 deckSettle 永远在「等松手」、repairUI 永远以为「有人按着」——画面停在会话页
   而 pageIdx 还是 0，左右滑全被 pageIdx!==1 挡掉，只剩上下滑；连点屏幕都修不好。
   2026-11-13 心跳日志实锤：p0 d1 sl312/313 卡了 10 秒以上，期间 Fix/Settle 一条都没有。
   1.5 秒没有任何触摸事件，就当手已经松了。 */
function fingerDown(){
  if(deckDown>0&&Date.now()-deckTouchAt>1500) deckDown=0;
  return deckDown>0;
}
function deckSettle(){
  /* 手指还按在屏幕上（拖着页面停住想事）：不算落定，等松手再说。
     否则 140ms 一到就往最近一格瞬移，页面在手指底下自己跳。 */
  if(fingerDown()){ clearTimeout(deckSettleT); deckSettleT=setTimeout(deckSettle,140); return; }
  /* 点导航点的 smooth 动画进行中：别把中间位置当成落定页 */
  if(Date.now()<deckNavUntil){ clearTimeout(deckSettleT); deckSettleT=setTimeout(deckSettle,140); return; }
  /* 还停在两格中间（惯性没停、scroll-snap 还没吸附）：先别判页。以前这里会按
     round() 取最近的一格，半格的位置也能瞬移换页；现在等它落稳（最多 6 次），
     还停不住就按当前页对齐回去，不猜。 */
  {
    const w0=deckSlotW()||1;
    const near=Math.round(deck.scrollLeft/w0)*w0;
    if(Math.abs(deck.scrollLeft-near)>w0*0.3){
      if(deckMidN++<6){ clearTimeout(deckSettleT); deckSettleT=setTimeout(deckSettle,120); return; }
      deckMidN=0;
    }else deckMidN=0;
  }
  const i=deckPageFromScroll();
  /* 只有「手指真的横着拖过」才算用户想换页。布局变化（终端页出现/收起、键盘、横竖屏、
     字体渲染完成）也会让原生 scroll-snap 重新算一次位置并触发 scroll 事件——以前
     只要 1.5 秒内碰过屏幕（比如点了个开关）就被当成翻页 → 停在设置页过一会儿自己
     跳到会话页（iOS 实测）。现在要求这次触摸确实横拖过 24px 且横多于竖。
     程序滚动一律只对齐回当前页，不换页。 */
  const userDriven=!!deckSwipeX&&(Date.now()-deckTouchAt)<1500;
  /* 没抓到横拖标志、但最近也没有布局变化 → 面板自己停到别的格子上，只能是真滑的
     （慢滑、惯性、触控板）。认它，别把人拽回去。 */
  const layoutRecent=(Date.now()-deckLayoutAt)<1500;
  /* 面板稳稳停在整格上，就是那一页——不管这次有没有抓到横拖标志。安卓实测一次
     左滑一个 touchmove 都没报（日志 hx0），于是画面停在会话页、pageIdx 还是 0：
     输入框/键条按 pageIdx 藏起来，换会话手势又被 pageIdx!==1 挡掉，只剩上下滑。 */
  const w1=deckSlotW()||1, atSlot=Math.abs(deck.scrollLeft-i*w1)<=w1*0.25;
  if(i!==pageIdx){
    if(atSlot||userDriven||!layoutRecent){ goPage(i,{fromScroll:1}); }
    else{
      try{ window.__cbDbg&&window.__cbDbg('Settle i'+i+' pg'+pageIdx+' sl'+Math.round(deck.scrollLeft)
            +' w'+Math.round(deckSlotW())+' hx'+deckSwipeX+' ud'+(userDriven?1:0),60); }catch(e){}
      deckAlign();
    }
  }else deckAlign();
  deckSwipeX=0;   /* 一次触摸只兑一次：之后再有位移（布局重算）一律按程序滚动处理 */
}
deck.addEventListener('scroll',function(){
  deckDots(deckPageFromScroll());
  clearTimeout(deckSettleT);
  deckSettleT=setTimeout(deckSettle,140);
},{passive:true});
deck.addEventListener('touchstart',function(e){
  deckDown++;deckTouchAt=Date.now();deckSwipeX=0;deckTouchN++;
  try{ const t=e.touches&&e.touches[0]; if(t){ deckTX=t.clientX; deckTY=t.clientY; } }catch(x){}
},{passive:true});
deck.addEventListener('touchmove',function(e){
  deckTouchAt=Date.now();
  try{
    const t=e.touches&&e.touches[0]; if(!t) return;
    const dx=t.clientX-deckTX, dy=t.clientY-deckTY;
    if(Math.abs(dx)>24&&Math.abs(dx)>Math.abs(dy)) deckSwipeX=1;   // 横向拖动才算翻页意图
  }catch(x){}
},{passive:true});
deck.addEventListener('touchend',function(){deckDown=Math.max(0,deckDown-1);deckTouchAt=Date.now();},{passive:true});
deck.addEventListener('touchcancel',function(){deckDown=Math.max(0,deckDown-1);deckTouchAt=Date.now();},{passive:true});
/* 状态自愈：画面停在哪一格，pageIdx 就跟到哪一格。
   正常情况由 #deck 的 scroll → deckSettle 收尾；但 WebView 冻结后恢复、惯性/原生
   scroll-snap 盖掉程序滚动时，这个事件可能压根不来，人就停在「画面是会话页、
   pageIdx 还是 0」：输入框/键条按 pageIdx 藏起来，换会话手势又被 pageIdx!==1
   挡掉，而 touch-action:pan-y 已经把横向手势从原生滚动里拿走了——只剩上下滑
   （2026-11-13 实测）。这里兜底认画面：手指松着、稳在整格上，就认它。 */
let deckFixAt=0;
function repairUI(){
  /* 后台也要修：WebView 切后台被重排过，回来正好是「画面/状态对不上」的时候 */
  if(fingerDown()||!deck.clientWidth) return;
  const now=Date.now(); if(now-deckFixAt<500) return;
  const bar=document.getElementById('bar');
  /* 状态说是会话页、输入框却量不到高度：底下的东西根本没摆出来，重摆一次 */
  if(pageIdx===1&&bar&&bar.offsetHeight<10){
    deckFixAt=now;
    try{ window.__cbDbg&&window.__cbDbg('FixB bar'+bar.offsetHeight+' sl'+Math.round(deck.scrollLeft),24); }catch(e){}
    goPage(1);
    return;
  }
  const w=deckSlotW()||0; if(!w) return;
  const sl=deck.scrollLeft/w, i=Math.round(sl);
  if(i===pageIdx||i<0||i>1) return;
  if(Math.abs(sl-i)>0.15) return;             /* 滚动中/半格：等它落稳，别抢 */
  deckFixAt=now;
  try{ window.__cbDbg&&window.__cbDbg('Fix i'+i+' pg'+pageIdx+' sl'+Math.round(deck.scrollLeft)
        +' w'+Math.round(w)+' bar'+Math.round(bar?bar.offsetHeight:-1),24); }catch(e){}
  goPage(i,{fromScroll:1});
}
setInterval(repairUI,700);
/* 卡住时人第一反应是点一下屏幕：顺便就对一次账，别让人非等 700ms 那一拍 */
document.addEventListener('touchend',repairUI,{passive:true});
document.addEventListener('click',repairUI,true);
/* 终端浮层跟着面板**实际滚动位置**走：滑动过程中 pageIdx 要等落定才更新，
   只看它会短暂出现「浮层该藏没藏 / 该显示没显示」的混乱状态。 */
var termScrollRaf=0;
deck.addEventListener('scroll',function(){
  if(!TERM.view||termScrollRaf) return;
  termScrollRaf=requestAnimationFrame(function(){
    termScrollRaf=0;
    try{ termSyncOverlay(); }catch(e){}
  });
},{passive:true});
/* 桌面/鼠标横滑（触控板）也算用户驱动 */
deck.addEventListener('wheel',function(){deckTouchAt=Date.now();deckSwipeX=1;},{passive:true});
document.addEventListener('keydown',e=>{   // 桌面浏览器：方向键翻面板
  if(e.target.closest('input,textarea'))return;
  if(e.key==='ArrowLeft'){ if(pageIdx>0) goPage(0,{smooth:1}); else prevSess(); }   // 左=回列表
  if(e.key==='ArrowRight'){ if(pageIdx===0) goPage(1,{smooth:1}); else nextSess(); } // 右=下一个会话
});
/* ── 控制面板滚动辅助：滚动都发生在 #pgCtrl 内部 ── */
function ctrlBox(){return document.getElementById('pgCtrl');}
function atBottom(){const b=ctrlBox();return b.scrollTop+b.clientHeight>=b.scrollHeight-140;}
function toBottom(){const b=ctrlBox();b.scrollTop=b.scrollHeight;}
/* ── 字号 ── */
let liveFs=12;
try{ liveFs=parseInt(localStorage.getItem('pty_live_fs')||'12',10)||12; }catch(e){}
liveFs=Math.max(9,Math.min(24,liveFs));
function applyFs(){ try{
  document.documentElement.style.setProperty('--live-fs',liveFs+'px');
  // 代码/表格的字号按终端列数算（和原样同尺寸），这个无单位倍数让它跟着字号按钮缩放
  // （CSS 的 calc 里长度不能当倍数，所以单独传一个数）
  document.documentElement.style.setProperty('--fs-scale',String(liveFs/12));
}catch(e){} }
function fsAdj(d){
  liveFs=Math.max(9,Math.min(24,liveFs+d));
  applyFs();
  try{ localStorage.setItem('pty_live_fs',String(liveFs)); }catch(e){}
  toast('实时字号 '+liveFs+'px');
}
applyFs();
/* 会话状态（AppleScript 的 app state）→ 中文 */
const ST_TXT={busy:'运行中',running:'运行中',live:'运行中',idle:'空闲',waiting:'等待中',suspended:'已挂起',terminated:'已结束',gone:'已结束',exited:'已退出'};
function stText(s){return ST_TXT[s]||s||'运行中';}
/* 会话状态只用标题前的品牌图标表示，不再写文字：
   在跑=呼吸动，等你=橙色脉冲，空闲=静止，挂起=琥珀半透明，结束=灰掉。
   状态名与传输方式（推送/轮询）放进 title / aria-label，长按能看到。 */
let ST_K='busy';
function setSt(s){ ST_K=s||'busy'; stPaint(); }
function stPaint(){
  const k=ST_K;
  let c='';
  if(k==='waiting') c='wait';
  else if(k==='suspended') c='susp';
  else if(k==='terminated'||k==='gone'||k==='exited') c='off';
  else if(k!=='idle') c='run';
  /* 图标和标题同步：图标当状态灯，标题文字再带一层动效，扫一眼就能看出在不在跑 */
  ['hAg','hTitle'].forEach(id=>{
    const el=document.getElementById(id); if(!el) return;
    el.classList.remove('run','wait','susp','off');
    if(c) el.classList.add(c);
  });
  const ag=document.getElementById('hAg'); if(!ag) return;
  const isCx=ag.classList.contains('codex');
  let t=(isCx?'Codex':'Claude')+' · '+stText(k);
  if(window.PUSH&&PUSH.st==='on') t+=' · 推送中';
  else if(vsCfg().stream!==false) t+=' · 轮询中';
  ag.title=t; ag.setAttribute('aria-label',t);
}
/* ── 原样视图（xterm） ── */
function initXT(cols,rows,fs){
  curWin=cols+'x'+rows;
  const box=document.getElementById('live2');
  lastW=box.clientWidth||0;
  xt=new Terminal({cols:cols,rows:rows,fontFamily:'ui-monospace,Menlo,monospace',fontSize:fs||12,
    // 粗体只加粗不换亮色：实时视图里粗体只是 font-weight，两边要一致
    drawBoldTextInBrightColors:false,
    // ANSI 16 色必须与实时视图同色板（server.py 的 _CSS16）：
    // 否则同一条输出在原样里用 xterm 默认色、在实时里用另一套色，看起来「样式不一样」
    theme:{background:'#0a0a0a',foreground:'#f5f5f5',
      ansi:['#3d3d3d','#f87171','#4ade80','#fbbf24','#60a5fa','#c084fc','#22d3ee','#f5f5f5',
            '#9ca3af','#fca5a5','#86efac','#fde047','#93c5fd','#d8b4fe','#67e8f9','#ffffff']},
    cursorBlink:true,scrollback:1200});
  xt.open(box);
}
function resetXT(cols,rows,fs){
  try{ if(xt&&xt.dispose) xt.dispose(); }catch(e){}
  xt=null;
  try{ document.getElementById('live2').innerHTML=''; }catch(e){}
  initXT(cols,rows,fs);
}
function rawDims(){
  // 原样网格：列数恒等于电脑端（内容与电脑完全一致不重排），字号按屏宽适配，
  // 行数按面板高叠加（手机一屏 ≈ 两三个电脑屏的高度）
  const box=document.getElementById('live2');
  const vw=Math.max(280,(box.clientWidth||window.innerWidth-10)-8);
  const cols=(hostWin&&hostWin[0])||80;
  const fs=Math.max(6,Math.floor(vw/cols/0.62));
  const m=document.getElementById('lhprobe');
  m.style.fontSize=fs+'px';
  const lh=m.offsetHeight||Math.round(fs*1.3);
  const headH=document.querySelector('header').offsetHeight||96;
  const keysH=document.getElementById('keys').offsetHeight||52;
  const barH=document.getElementById('bar').offsetHeight||58;
  const availH=window.innerHeight-headH-keysH-barH-14;
  return {fs:fs,cols:cols,rows:Math.max(24,Math.floor(availH/lh),rawRowsMin)};
}
window.addEventListener('resize',()=>{  // 转屏等尺寸变化：网格按新尺寸整帧重建
  if(view!=='raw'||!fullColor||!xt) return;
  const d=rawDims();
  if(curWin===d.cols+'x'+d.rows){ syncRawMode(); return; }
  rawSnap();
});
/* 手指在终端上滑动时先别往 xterm 里写帧：轮询每秒写一次，正好落在惯性滑动
   中间就卡一下（滑动不跟手的根因之一）。松手 450ms 后恢复。 */
let rawBusyUntil=0;
(function(){
  const box=document.getElementById('live2');
  if(!box) return;
  ['touchstart','touchmove','touchend','touchcancel'].forEach(t=>
    box.addEventListener(t,()=>{ rawBusyUntil=Date.now()+450; },{passive:true,capture:true}));
})();
var RAW_INF=null;   /* 同 poll/liveflowPoll：上一发没回来就不再发（每秒定时器不会等） */
async function rawPoll(){
  if(pageIdx!==1||view!=='raw'||!fullColor||hiddenNow()) return;
  if(RAW_INF&&RAW_INF===SID) return;
  if(Date.now()<rawBusyUntil) return;
  // 已经往上翻了：别更新（更新会让回滚区错位），等回到底部再继续
  try{ const b=xt&&xt.buffer&&xt.buffer.active; if(b&&b.viewportY<b.baseY) return; }catch(e){}
  RAW_INF=SID;
  const rme=SID;
  try{
    const d=rawDims();
    if(xt&&curWin!==d.cols+'x'+d.rows){ await rawSnap(); return; }  // 转屏等网格变了：整帧重建
    const r=await xj('/api/ext_snap/'+SID+'?rows='+d.rows+'&full=0',{bin:true});
    if(!r.ok){  // 没接 wrapper → 降级纯文本
      fullColor=false;
      document.getElementById('live2').style.display='none';
      document.getElementById('live').style.display='block';
      screenPoll();
      return;
    }
    const hw=(r.hdr('X-Host-Winsize')||'').split(',');
    if(hw.length===2&&hostWin&&(parseInt(hw[0],10)!==hostWin[0]||parseInt(hw[1],10)!==hostWin[1])){
      await rawSnap(); return;  // 电脑端窗口尺寸变了
    }
    const h=r.hdr('X-Frame-Hash');
    if(h&&h===lastFrameHash) return;  // 画面没变，跳过重绘省流量
    lastFrameHash=h;
    const buf=new Uint8Array(r.buf||[]);
    if(buf.length&&xt) xt.write(buf);
  }catch(e){}
  finally{ if(RAW_INF===rme) RAW_INF=null; }
}