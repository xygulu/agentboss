// SPDX-License-Identifier: AGPL-3.0-or-later

/* 按住说话：页面里有 window.CB（App 壳）走原生录音，否则浏览器 getUserMedia。
   两条路最后都 POST /api/ptt，识别结果填 #txt 并发送。 */
(function(){
  if (window.__cbpttReady) return;
  window.__cbpttReady = 1;

  var btn = document.getElementById('cbptt');
  var st  = document.getElementById('cbpttst');
  var down = false, stream = null, mr = null, chunks = [], t0 = 0, pendingStop = false, watch = null;

  /* 光标位置：语音结果插到光标处，而不是永远追加到末尾。
     点麦克风会让输入框失焦，所以失焦那一刻记下选区（60 秒内、内容没变才算数）；
     结果回来时如果焦点还在输入框里，就用实时选区。 */
  var caretMark = null, caretMarkT = 0;
  document.addEventListener('focusout', function(ev){
    var t = ev.target;
    if (!t || t.id !== 'txt' || typeof t.selectionStart !== 'number') return;
    caretMark = {s: t.selectionStart, e: t.selectionEnd, n: (t.value || '').length};
    caretMarkT = Date.now();
  });

  function show(msg, bad){
    st.textContent = msg;
    st.style.display = 'block';
    st.style.color = bad ? '#ff6467' : '#f5f5f5';
    clearTimeout(st._h);
  }
  function hide(ms){
    clearTimeout(st._h);
    st._h = setTimeout(function(){ st.style.display = 'none'; }, ms || 2000);
  }
  function ui(on){
    down = on;
    /* 只切 class，不换 innerHTML：换 innerHTML 会把手指按着的那个 <svg>
       从 DOM 里摘掉，WebKit 的触摸序列就断了，touchend 再也送不到页面，
       松手停不下来（iOS 实锤：松手还在录）。 */
    if (on) btn.classList.add('on');
    else { btn.classList.remove('on'); pttViaTouch = 0; pttFinger = 0; pttStarted = 0; }
  }
  /* 显示与否交给 CSS：body.pg1（会话页）才出现语音按钮 */

  /* ── 原生通路回调（App 内有 CB 接口时走这条） ── */
  /* 语音忙标记：页面自更新/长跑自愈看到 __cbVoiceBusy 或宽限窗口未过就不重载。
     收起录音后留给转写 25 秒——ASR 在网络差时能磨这么久，重载早了这句话就丢了。 */
  function voiceRec(on){ window.__cbVoiceBusy = on ? 1 : 0; if(!on) window.__cbVoiceUntil = Date.now() + 25000; }

  /* ── 转写等待：有进度 + 有终点 ──
     以前这里只 show 一句「转写中…」就再没人管：请求卡住（P2P 僵尸通道要等 60 秒、
     中继那头 ASR 慢、壳把 __pttGo 丢了）时，界面就永远停在「转写中」。
     现在每秒报一次已等多久；到点强行收摊并作废这一轮，迟到的结果也不再插字。 */
  var pttGen = 0, pttTick = null, pttAt = 0, pttLen = 0;
  var pttRecTick = null, pttRecAt = 0;
  var PTT_WAIT_MAX = 135000;      /* 中继 120 秒 + 余量 */

  /* 录音计时：不显示时间的话，松手事件一丢，界面看着就是「一直在录」，
     人根本不知道已经录了四十秒（最后识别出来的是一大段废话 + 很久的转写）。 */
  function pttRecStop(){ if (pttRecTick){ clearInterval(pttRecTick); pttRecTick = null; } }
  function pttRecStart(){
    pttRecStop(); pttRecAt = Date.now();
    var paint = function(){
      var s = (Date.now() - pttRecAt) / 1000;
      show('● 录音中… ' + s.toFixed(0) + 's' +
        (s > 50 ? '（快 1 分钟上限了）' : '') +
        ' · ' + (pttMode()==='toggle' ? '再按一下结束' : '松手结束'));
    };
    paint();
    pttRecTick = setInterval(paint, 1000);
  }
  function pttWaitStop(){
    window.__cbPttWait = 0; pttRecStop();
    if (pttTick){ clearInterval(pttTick); pttTick = null; }
    /* 停表时把「转写中…」那行收掉：壳里结果是由 shell 自己的回调处理并显示在
       #cbshim_st 上的，页面这行没人来改——以前会永远停在最后画出来的
       「转写中… 1s（录音 1.3s）」（2026-11 实测）。别的提示（已说/失败/已取消）
       不进这个判断，各自 show 完自己会 hide。 */
    try{ if (st && /^\s*(●\s*)?转写中/.test(st.textContent || '')) st.style.display = 'none'; }catch(e){}
  }
  function pttWaitStart(ms){
    pttLen = ms || 0; pttAt = Date.now();
    pttWaitStop();
    window.__cbPttWait = 1;      /* 等结果的整段时间都不许自更新重载 */
    var paint = function(){
      var s = (Date.now() - pttAt) / 1000;
      if (s * 1000 > PTT_WAIT_MAX){
        pttGen++;                 /* 作废本轮：之后回来的结果一律不插字，免得重复 */
        pttWaitStop();
        show('转写没等到结果（网络不稳），这句话没说上，请再说一次', true); hide(7000);
        return;
      }
      show('转写中… ' + s.toFixed(0) + 's' + (pttLen ? '（录音 ' + (pttLen/1000).toFixed(1) + 's）' : ''));
    };
    paint();
    pttTick = setInterval(paint, 1000);
  }
  /* 语音上传。三条硬要求（iOS 上「一直转写中」就是这三条没做到）：
     1) 不走 P2P 直连 —— 中继自己就有 ASR key，手机→中继→识别是最短路径；
        走 DataChannel 时通道一僵死，页面要白等 60 秒才回落，人看到的就是卡住。
     2) 超时放宽到 120 秒 —— 实测识别 1~17 秒，长录音加排队常超 15 秒；
        老的默认值 15 秒会把已经成功的识别砍掉，然后报一个莫名的失败。
     3) 任何结局都必须收掉「转写中」：成功、失败、超时、页面在后台，都收。 */
  function pttCwd(){
    try{ var m=(typeof findSession==='function')?findSession(SID):null; return (m&&m.cwd)||''; }
    catch(e){ return ''; }
  }
  function pttProj(){
    try{ var m=(typeof findSession==='function')?findSession(SID):null; return (m&&m.proj)||''; }
    catch(e){ return ''; }
  }
  /* 纠错上下文（base64，走 header 免得 URL 过长）：环境吵的时候 ASR 会把整句听成
     同音字、甚至凭空多出词（「发布上线」→「钱钱拿去啊」），只靠项目词表救不回来。
     把屏幕上最近的对话/输入框内容带上，中继按上下文把错字改回来。 */
  function pttCtx(){
    try{
      var s='';
      var t=document.getElementById('txt');
      if(t && t.value) s += '输入框：' + String(t.value).slice(-80);
      var d=document.getElementById('deck');
      if(d){
        var m=String(d.innerText||'').replace(/\s+/g,' ').trim();
        /* 屏幕内容会跟着音频一起发给识别服务：先把像密钥的东西抹掉
           （长串 token/hash、key=value 形式的凭据），别把机密递出去 */
        m = m.replace(/(token|secret|password|passwd|api[_-]?key|密钥|密码|口令)\s*[:=]\s*\S+/gi, '$1=…')
             .replace(/[A-Za-z0-9_\-+/]{24,}/g, '…');
        if(m) s += (s?'\n':'') + '屏幕：' + m.slice(-220);
      }
      s = s.slice(0,320);
      if(!s || typeof btoa!=='function') return '';
      return btoa(unescape(encodeURIComponent(s)));
    }catch(e){ return ''; }
  }
  window.__cbPttCtx = pttCtx;      /* 浏览器兜底录音那一段在另一个 IIFE 里，只能挂到 window */
  /* 确认阶段只等「发送/取消」两个词——这两句被纠错改掉，整个流程就断了，明确不纠 */
  function pttNoFix(){
    try{ return (window.__cbAutoInfo && String(window.__cbAutoInfo()).indexOf('confirm')===0) ? '1' : '0'; }
    catch(e){ return '0'; }
  }
  function pttRequest(blob, ctype, ms, gen, auto){
    /* 带上当前会话的工作目录/项目键：开发机据此算项目键（git 根目录名），
       中继按项目词表拼提示词并在转写后纠正术语。 */
    var fail = function(msg){
      /* 自动听写失败也要收摊，别让它永远停在「转写中」 */
      if (auto && window.__autoResult) {
        try{ window.__autoResult(JSON.stringify({ok:false, error:msg})); return; }catch(e){}
      }
      show(msg, true); hide(6000);
    };
    var _hd = {}, _cx = pttCtx();
    if(_cx) _hd['X-CB-Ctx'] = _cx;
    if(pttNoFix() === '1') _hd['X-CB-Nofix'] = '1';
    return xjX('/api/ptt?ms=' + ms + '&cwd=' + encodeURIComponent(pttCwd())
               + '&p=' + encodeURIComponent(pttProj()),
      {method:'POST', ctype:ctype, body:blob, timeout:120000, away:true, headers:_hd})
      .then(function(r){
        if (gen !== undefined && gen !== pttGen) return;   /* 已判超时：迟到的结果丢掉 */
        pttWaitStop();
        if (r && r.status === 0) { fail('网络没过去，转写没成功，请再说一次'); return; }
        /* 今日时长用完：中继回 429/403 + quota，直接把买时长的浮层顶上来，
           别只丢一句「转写失败」让人以为坏了。
           注意**不能只看 quota 在不在**：中继转写成功的响应里也带 quota
           （{"ok":true,"text":…,"quota":{used,cap,credit}}），以前这段只要看见
           quota 就当成超额——手机走中继（不在开发机局域网）时说话永远回
           「额度用完」，转出来的字被丢在门外。2026-11 实测：免费账号账上 19/60，
           手机上却说额度用完了。现在要求「确实是失败」才算。 */
        var _j = (r && r.json) || null;
        if (_j && _j.quota && (_j.ok === false || r.status === 403 || r.status === 429)) {
          try{ quotaSheet(_j); }catch(e){}
          fail(_j.error || '额度用完'); return;
        }
        if (auto && window.__autoResult) window.__autoResult(r.text);
        else if (window.__pttResult) window.__pttResult(r.text);
        else handleResult(r.text);
      })
      .catch(function(e){
        if (gen !== undefined && gen !== pttGen) return;
        pttWaitStop();
        fail('上传失败：' + (e && e.message || e));
      });
  }

  window.__pttStart  = function(){
    /* 自建中继没配 ASR（caps.asr=false）：悬浮钮已被 capsApply 藏掉，
       但音量键/自动听写等旁路还会进这里——明确拒绝，别录了发不出转写 */
    if(!capsOk('asr')){ toast('当前服务器未开通语音转写', true); return; }
    nDbg('NpttStart a='+(AUTO.armed?1:0)+' m='+(AUTO.mine?1:0));
    pttStarted = 1; voiceRec(1);
    /* 已经在录了（重复的按下事件/壳又调了一次）就别把计时清零——
       否则录到第 40 秒时来一下，秒数归零，看着像刚开始，容易错过 1 分钟上限 */
    if (!down || !pttRecTick){ pttWaitStop(); ui(true); pttRecStart(); }
    pttWatchStart();
  };
  window.__pttDone   = function(ms){ nDbg('NpttDone '+ms); voiceRec(0); ui(false); pttWaitStart(ms); pttWatchStop();
    try{ window.__pttEndedAt=Date.now(); }catch(e){} };
  window.__pttError  = function(e){
    nDbg('NpttErr '+String(e).slice(0,30));
    voiceRec(0); ui(false); pttWaitStop(); pttWatchStop();
    if(AUTO.mine){ AUTO.mine=false; autoEnd('fail','录音失败：' + e); return; }   /* 自动听写失败：收掉，别卡着 */
    show('录音失败：' + e, true); hide(5000);
  };
  window.__pttCancel = function(){ nDbg('NpttCancel'); voiceRec(0); ui(false); pttWaitStop(); show('已取消'); hide(1200); pttWatchStop();
    try{ window.__pttEndedAt=Date.now(); }catch(e){} };  /* 壳在「没在录」时调它回空闲态 */
  window.__pttResult = function(json){
    voiceRec(0); pttWaitStop();
    if(AUTO.mine){ autoResult(json); return; }           /* 自动听写：结果走自己的状态机 */
    useBillPTT(json);                  /* 语音也计时：原生录音是壳发的请求，补上「记了账」 */
    window.__cbVoiceUntil = Date.now() + 5000; handleResult(json);
  };
  window.__pttResult.__cbBills = 1;    /* 它自己已经记账（see hookPttResult：外面那层别再补一次） */

  /* iOS 壳用音量键当「说话（上）/ 发送（下）」：松手后壳会把音量拉回基准，
     这个回弹有时被误判成一次「音量下键」，于是刚说完话就调 __cbSend(true)——
     此时转写还没回来、输入框是空的，就弹「没有可发送的内容」。
     在页面这层兜住：刚结束录音 2.5 秒内、输入框为空时的「按键发送」直接忽略
     （用户真要发会再按一次；转写完框里有字时不受影响）。 */
  (function(){
    var nativeSend = window.__cbSend;
    window.__cbSend = function(byKey){
      var ta = document.getElementById('txt');
      var empty = !ta || !String(ta.value || '').trim();
      if (byKey && empty && Date.now() - (window.__pttEndedAt || 0) < 2500) return;
      if (typeof nativeSend === 'function') return nativeSend(byKey);
    };
  })();

  /* ── 壳 shim 会抢走「转写结果」回调，记账锚点必须跟着走 ────────────────
     安卓壳每 2.5 秒心跳注入一次 shim.js，它把 window.__pttResult 换成自己的
     onResult（填框 + 可选发送），上面这版当场作废。字照样进输入框，所以看不出
     异常——但 useBillPTT 永远不执行：LASTBILL 不更新 → 30 秒后 planRefresh 一
     算「这一格没记账」→ USEST.tk=0 → 浮标数字冻住（0.7.52 中兴实测：诊断里
     [boot] 有、[bill] 一条都没有，中继那头 ASR 明明扣了时长）。
     在 window 上装存取器：谁来当 __pttResult 都接住，先记一笔账再交给它。
     必须保留 __cbMine：shim 心跳发现「回调不是我的」会一直重装，等于把字和
     记账各拆一半。 */
  function hookPttResult(target){
    target = target || window;
    var cur = null;
    function wrap(f){
      if(typeof f !== 'function' || f.__cbHooked) return f;
      var w = function(json){
        try{
          if(!f.__cbBills){
            /* 只认壳回的整段 JSON：失败（ok:false）不扣，报错页/纯文本也不算账 */
            var ok = false;
            try{ var d = JSON.parse(json); ok = !!d && d.ok !== false; }catch(e){}
            if(ok) useBillPTT(json);
          }
        }catch(e){}
        return f.apply(this, arguments);
      };
      w.__cbMine   = (f.__cbMine === 1) ? 1 : 0;
      w.__cbBills  = f.__cbBills || 1;
      w.__cbHooked = 1;
      return w;
    }
    var had = target.__pttResult;
    try{
      Object.defineProperty(target, '__pttResult', {
        configurable: true,
        get: function(){ return cur; },
        set: function(v){ cur = wrap(v); }
      });
      cur = wrap(had);
    }catch(e){                     /* 极老的 WebView 不支持：退化成每秒对一次 */
      var fix = function(){
        if(target.__pttResult && !target.__pttResult.__cbHooked){
          target.__pttResult = wrap(target.__pttResult);
        }
      };
      fix();
      if(typeof setInterval === 'function') setInterval(fix, 600);
    }
  }
  hookPttResult();

  /* ── 自动听写：AI 干完活/要确认时，自动开麦等用户说话 ─────────────────
     用户拍板的四条边界：
       ① 只有说「发送」才真的发（绝不自动发）；
       ② 只对当前打开的这个会话生效；
       ③ 只亮屏前台，切走/黑屏立刻收麦（壳的 onPause 兜底）；
       ④ 接受批量识别 2~5 秒的延迟。
     流程：提醒到 → 等播报念完（__cbSayDone）→ 开麦 → VAD 判说完自动停
          → 转写 → 填进输入框 → 再开麦只等一句确认 → 说「发送」才发。
     一直没人说话 / 按了别处 / 切走 → 收掉，不上传（不花额度）。 */
  var AUTO = {armed:false, phase:'', mine:false, sid:'', rounds:0, miss:0,
              timer:null, waitTimer:null, beginTimer:null, cueTimer:null, waitCue:false, waitSay:false,
              tick:null, upTimer:null, pollTimer:null, echo:'', at:0, armAt:0};
  /* DBG：让页面其它作用域（__cbVisible / 桥包装）能一句话问到自动听写的当前态 */
  window.__cbAutoInfo=function(){ return (AUTO.phase||'-')+'/'+(AUTO.armed?'arm':'-')
                                        +'/'+(AUTO.mine?'mine':'-'); };
  var AUTO_KINDS = {done:1, perm:1, ask:1, input:1};
  var AUTO_MAX_ROUNDS = 3;
  var AUTO_CANCEL = /^(取消|算了|不发|不要发|别发|不用发|别发送|先不|清空|重来|撤销|不用了|再改改|等一下|等下)/;
  /* 「发送」不能只认标准词：用户会说「发出去吧」「帮我发了」「可以发送了」「对，就这样发」。
     先整句归一化，再看是不是「短句里含发送意思」——长句（真在补充内容）不误判。 */
  var AUTO_SEND   = /^(发送|发出去|发过去|发给我|发吧|发呀|发|确认|确定|提交|发出|就这样|可以发|可以了|好发送|发了|发掉|传过去)/;
  var AUTO_SEND_ANY = /发送|发出去|发过去|发一下|发给我|发出|提交|确认|确定|帮我发|给我发|发这句|可以发|就这样发|发送吧/;
  /* 开麦提示音被录进去的典型转写（「叮。」这类）：当没听见，重开一次麦，
     别把它当内容填进输入框、更别当成确认。 */
  var AUTO_NOISE  = /^(叮+|咚+|嘀+|滴+|嘟+|哔+|叮咚|叮当|嗯+|呃+|啊+|哦+|噢+|唉+|哎+)$/;

  function autoSupported(){
    try{ return !!(window.CB && typeof CB.autoListenStart === 'function'); }catch(e){ return false; }
  }
  var AUTO_ON_LAST = null;   /* 上次读到的开关值：页面刚重载、壳配置还没就绪时别误判成「关」 */
  function autoEnabled(){
    if(!autoSupported()) return false;
    /* 免费版不提供自动听写：套餐确认没有这项时，即使本地开关还留着也绝不开麦
       （降级/退订后不留后门）。 */
    if(!autoPlanOK()) return false;
    var v = null;
    try{ v = vsCfg().autolisten; }catch(e){ v = null; }
    if(v !== true && v !== false){
      /* 壳还没就绪/配置读坏了：直接看页面自己那份，别把开关误判成关 */
      try{ v = (JSON.parse(localStorage.getItem('cb_cfg') || '{}') || {}).autolisten; }catch(e2){}
    }
    if(v === true || v === false){ AUTO_ON_LAST = v; return v; }
    return AUTO_ON_LAST === true;
  }
  function autoNorm(s){ return String(s||'').replace(/[\s，。！？、,.!?~～…\-—_]/g,''); }
  /* 录到了喇叭里刚念的那句播报？转写和播报文字高度重合就当没听见——
     时序上差一点（下载慢、扬声器尾巴）也不至于把 AI 的话当用户说的话。 */
  function autoLooksLikeEcho(t){
    var a = autoNorm(t), e = autoNorm(AUTO.echo || '');
    if(!a || e.length < 4) return false;
    if(a === e) return true;
    /* 整段播报被切掉一截（录到的比念的短很多）→ 回声 */
    if(a.length >= 6 && a.length * 2 >= e.length && e.indexOf(a) >= 0) return true;
    /* 录到的就是那句播报（长句）→ 回声。短播报（「已完成」「需要确认」）不做包含判断：
       用户完全可能在话里提到同样的词。 */
    if(e.length >= 8 && a.indexOf(e) >= 0) return true;
    return false;
  }
  function autoClearTimers(){
    if(AUTO.timer){ clearTimeout(AUTO.timer); AUTO.timer=null; }
    if(AUTO.waitTimer){ clearTimeout(AUTO.waitTimer); AUTO.waitTimer=null; }
    if(AUTO.beginTimer){ clearTimeout(AUTO.beginTimer); AUTO.beginTimer=null; }
    if(AUTO.cueTimer){ clearTimeout(AUTO.cueTimer); AUTO.cueTimer=null; }
    AUTO.waitCue = false;
    AUTO.waitSay = false;
    if(AUTO.tick){ clearInterval(AUTO.tick); AUTO.tick=null; }
    if(AUTO.upTimer){ clearTimeout(AUTO.upTimer); AUTO.upTimer=null; }
    if(AUTO.pollTimer){ clearTimeout(AUTO.pollTimer); AUTO.pollTimer=null; }
  }
  /* 麦克风键的「录音中」外观：壳里是 shim 建的 #cbshim_mic，浏览器是页面自己的 #cbptt。
     手动按住会走 __pttStart；自动听写不走那条通路，得自己把按钮点亮，否则人看不出它在录。 */
  function autoMic(on){
    try{
      var m=document.getElementById('cbshim_mic');
      if(m){ m.textContent = on ? '■' : '🎤'; m.className = on ? 'on' : ''; }
      var b=document.getElementById('cbptt');
      if(b){ b.classList[on?'add':'remove']('on'); }
    }catch(e){}
  }
  function autoIsSend(t){
    t = autoNorm(t);
    if(!t) return false;
    if(AUTO_CANCEL.test(t)) return false;              /* 「不发送」「别发」先归取消 */
    /* 否定词在前几个字里就不算发送：「先不发」「这个不用提交」 */
    if(/^(不|别|勿|没|先不)/.test(t)) return false;
    if(AUTO_SEND.test(t)) return true;
    /* 自然口语：短句（≤10 字）里含发送语义就认；长句是在补充内容，别误发。
       「帮我发出去吧」「可以发送了」「发送嘛」都算。 */
    if(AUTO_SEND_ANY.test(t) && t.length <= 10) return true;
    /* 短促的一句「发」「送」：ASR 常把「发送」切成一个字，别当没听见 */
    return t.length <= 2 && /[发送]/.test(t);
  }
  function autoIsCancel(t){ return AUTO_CANCEL.test(autoNorm(t)); }
  /* 确认阶段的纯口头语（「好的」「嗯」）：既不是发送也不是补充，别塞进输入框 */
  function autoIsFiller(t){ return /^(好的?|好|嗯+|行|ok|okay|知道了|收到|对)$/i.test(autoNorm(t)); }
  function autoIsNoise(t){ return AUTO_NOISE.test(autoNorm(t)); }
  /* 确认阶段那句提示语（「说发送就发，说取消就不发」）被麦克风录进来：
     一句话里同时出现「发送」和「取消/不发」——那不是用户在回答。 */
  function autoIsPromptEcho(t){
    var s = autoNorm(t);
    if(s.length < 6) return false;
    return /发送|发出|发出去/.test(s) && /取消|不发|别发|算了/.test(s);
  }

  /* DBG：把自动听写的状态落进开发机日志（走 /api/plan 的 query，
     开发机直连通道会把路径打进 server.log）。查完可以删。 */
  /* 调试上报走的是真网络请求，不能反过来拖慢用户：一分钟最多 30 条，
     页面真进了「每秒抛一次错」的循环时也不至于把带宽/电量耗在这上面。 */
  var _dgT=0,_dgN=0;
  function nDbg(t, lim){
    try{
      var now=Date.now();
      if(now-_dgT>60000){ _dgT=now; _dgN=0; }
      if(++_dgN>30) return;
      var u='/api/plan?cbdbg='+encodeURIComponent(String(t).slice(0, lim || 60));
      /* 必须走 xj：P2P 直连时裸 fetch 会撞混合内容/CORS，请求根本到不了开发机 */
      if(typeof xj==='function'){ var p=xj(u); if(p&&p.catch) p.catch(function(){}); }
      else if(window.fetch) fetch(u,{cache:'no-store'}).catch(function(){});
    }catch(e){}
  }
  window.__cbDbg = nDbg;
  /* 心跳：手机上的症状没法连调试器，就每 5 秒把「画面和状态到底在哪」落一行到
     开发机日志（H 开头）：pg=状态里的页码 d=面板实际停在第几格 sl/w=滚动位置
     b/k=输入框和键条高度 t=面板上收到过几次触摸 lg=主线程最长卡了多少毫秒
     el=屏幕最底下那条压着谁。
     单独算额度：nDbg 一分钟 30 条，出问题那一分钟 Go/Sw 就把它用光了，
     反而出事那段一条都留不下（2026-11-13 排查就是这么被坑的）。
     查完可以删掉这一整段。 */
  var _hzT=0,_hzN=0;
  function nHz(t){
    try{
      var now=Date.now();
      if(now-_hzT>60000){ _hzT=now; _hzN=0; }
      if(++_hzN>16) return;
      var u='/api/plan?cbhud='+encodeURIComponent(String(t).slice(0,96));
      if(typeof xj==='function'){ var p=xj(u); if(p&&p.catch) p.catch(function(){}); }
    }catch(e){}
  }
  window.__cbLag=0;
  (function(){
    var last=Date.now();
    setInterval(function(){
      var now=Date.now(), dt=now-last-1000; last=now;
      if(dt>window.__cbLag) window.__cbLag=dt;
    },1000);
    setInterval(function(){
      if(document.hidden) return;
      var d=document.getElementById('deck');
      var b=document.getElementById('bar'), k=document.getElementById('keys');
      if(!d||!b||!k) return;
      var w=Math.round(deckSlotW()), sl=Math.round(d.scrollLeft);
      var el='-';
      try{ var e=document.elementFromPoint(Math.round(window.innerWidth/2),(window.innerHeight||0)-4);
           if(e) el=(e.id||e.className||e.tagName||'').toString().slice(0,14); }catch(x){}
      var lag=window.__cbLag; window.__cbLag=0;
      nHz('H p'+pageIdx+' d'+Math.round(sl/(w||1))+' sl'+sl+'/'+w+' b'+b.offsetHeight
          +' k'+k.offsetHeight+' t'+deckTouchN+' D'+deckDown+' lg'+lag+' '+el);
    },5000);
  })();
  /* 页面自己炸了（渲染/取数抛异常）以前只在手机屏幕上留一块空白，开发机日志里一个字
     都没有。统一抓起来打一条 JS，白屏也能定位到是哪一行。 */
  window.addEventListener('error',function(e){
    try{ nDbg('JS '+((e&&e.message)||'')+' @'+((e&&e.filename)||'').split('/').pop()
               +':'+((e&&e.lineno)||0),200); }catch(_){}
  });
  window.addEventListener('unhandledrejection',function(e){
    try{ var r=e&&e.reason;
         nDbg('JSrej '+String((r&&r.stack)||(r&&r.message)||r||'').slice(0,180),220); }catch(_){}
  });
  /* 开屏把「播报相关的几项」落进开发机日志：不用等来一条提醒就能查
     「为什么没有语音播报」（nv/ns=语音·提示音开关 b=息屏服务 d=免打扰
     f/t=时段 sv=播报音量 a=壳记的已响过水位 sh=有没有壳）。 */
  setTimeout(function(){
    /* 播报音量默认值上调 80→90（只对没动过滑条的设备生效：存过的值原样保留）。
       壳的播放器读的是 cfg 里的 speechVol，页面这边光改 JS 默认值影响不到原生播放，
       所以把默认值落进 cfg 一次。 */
    try{
      if(typeof vsCfg==='function'&&typeof vsSave==='function'){
        var _vc=vsCfg();
        if(typeof _vc.speechVol!=='number'){ _vc.speechVol=90; vsSave(_vc); }
      }
    }catch(e){}
    try{
      if(typeof vsCfg!=='function'||typeof speechVol!=='function') return;
      var c=vsCfg(), a=-9;
      try{ if(window.CB&&CB.bgAnnounced) a=CB.bgAnnounced(); }catch(e){}
      nDbg('Cfg nv'+(c.nvoice!==false?1:0)+' ns'+(c.nsound!==false?1:0)
        +' b'+(c.bgn!==false?1:0)+' d'+(c.dnd===true?1:0)
        +' f'+String(c.dndF||'22:00').replace(':','')
        +' t'+String(c.dndT||'08:00').replace(':','')
        +' sv'+speechVol()+' a'+a
        +' sh'+((window.CB&&CB.announces)?1:0)
        +' '+BOOT_INFO);   /* 开屏落在哪一屏、位置从哪来：排查「自己跳回会话页」 */
    }catch(e){}
    /* 「装完不会自己打开」的现场：壳把闹钟/广播/拉起每个节点记在 SharedPreferences，
       这里取最近 400 字打到开发机日志（变了才打，别每次开页都刷）。 */
    try{
      if(window.CB && CB.updTrace){
        var ut=String(CB.updTrace()||'').replace(/\s+/g,' ').trim();
        var seen='';
        try{ seen=localStorage.getItem('cb_updtrace')||''; }catch(e){}
        if(ut && ut!==seen){
          try{ localStorage.setItem('cb_updtrace', ut); }catch(e){}
          nDbg('UPD '+ut.slice(-400), 460);
        }
      }
    }catch(e){}
  },5000);
  /* onNotify 布防：只在「正在看这个会话 + 亮屏前台 + 开关开」时 */
  window.__cbAutoArm = function(d, nc, shellAnn){
    var age = d ? (Date.now()/1000 - (d.ts || 0)) : -1;
    var why = '';
    if(!autoEnabled()) why = 'off';
    else if(!d || !AUTO_KINDS[d.kind]) why = 'kind';
    else if(d.sid !== SID || pageIdx !== 1 || hiddenNow()) why = 'ctx';
    /* 只认「刚发生」的提醒：打开页面时补发的旧提醒不自动开麦（人会以为 App 在偷听）。
       只挡「太旧」的补发；时间戳在「未来」一律当新鲜——那只能是中继和手机
       时钟差（实测差零点几秒就够把刚到的提醒判成过期，直接不动麦）。 */
    else if(age > 60) why = 'age';
    else if(AUTO.armed){
      /* 上一轮布防卡死了（页面没收到收摊信号）：超过 45 秒还没开麦就强制收摊，
         否则后面每一条提醒都被 busy 挡掉，表现为「自动录音再也不启动」。 */
      if(!AUTO.mine && Date.now() - (AUTO.armAt || 0) > 45000){ nDbg('Nstale'); autoEnd('stale'); }
      else why = 'busy';
    }
    if(why){
      var av = -9;
      try{ var v0 = vsCfg().autolisten; av = (v0 === true) ? 1 : (v0 === false ? 0 : -1); }catch(e){ av = -2; }
      nDbg('N'+why+' k='+(d&&d.kind)+' pg='+pageIdx+' h='+(hiddenNow()?1:0)
           +' a='+Math.round(age)+' on='+av+' sup='+(autoSupported()?1:0));
      return;
    }
    var voiceOn = !!(nc && nc.voice !== false && d.voice);
    var willSay = !!(voiceOn && String(d.say || '').trim());
    nDbg('NARM '+d.kind+(willSay?' say':' nosay'));
    AUTO.armed = true; AUTO.sid = d.sid; AUTO.rounds = 0; AUTO.phase = ''; AUTO.mine = false;
    AUTO.armAt = Date.now();
    /* 这一轮播报念的可能就是这条文字：转写出来高度重合就当作录到了喇叭。 */
    AUTO.echo = String(d.saytext || d.text || '');
    try{ show('🎤 等播报念完就开麦…'); }catch(e){}
    if(willSay || voiceOn){
      /* 必须等播报**真的**念完再开麦——否则麦克风把播报自己录进去，转写出来的
         就是「AI 的播报内容」。两个信号：壳的完成回调（__cbSayDone）为主，
         固定秒数只做兜底（整句 30 秒，只有内置音 8 秒），而且开麦前再问一次
         「还在放吗」（见 autoQuietBegin）。 */
      AUTO.waitSay = true;
      AUTO.waitTimer = setTimeout(function(){
        AUTO.waitTimer = null;
        if(AUTO.armed && !AUTO.mine) autoQuietBegin('dictate', 8000, 500);
      }, willSay ? 30000 : 8000);
    } else {
      /* 没有播报：让提示音响完就开麦 */
      AUTO.timer = setTimeout(function(){
        AUTO.timer = null;
        if(AUTO.armed && !AUTO.mine) autoBegin('dictate');
      }, (nc && nc.sound !== false) ? 1200 : 500);
    }
  };
  /* 壳播完一条语音（MainActivity/BgService 的 __cbSayDone） */
  window.__cbSayDone = function(sid){
    if(AUTO.mine) return;
    if(!AUTO.armed){
      /* 页面没收到那条提醒（推送断过/重连丢了），但壳刚把这一轮的播报念完 ——
         这就是「AI 干完了」的可靠信号，直接布防开麦，别等一个永远不来的 notify。
         这是「自动录音经常不启动」最常见的成因之一。 */
      if(sid && SID && sid !== SID) return;
      if(!autoEnabled() || pageIdx !== 1 || hiddenNow()) return;
      if(AUTO.armed) return;
      nDbg('NARM saydone');
      AUTO.armed = true; AUTO.sid = SID; AUTO.rounds = 0; AUTO.phase = ''; AUTO.mine = false;
      AUTO.armAt = Date.now();
      AUTO.echo = '';
      AUTO.waitSay = true;
      try{ show('🎤 等播报念完就开麦…'); }catch(e){}
    } else if(!AUTO.waitSay){
      /* 不在等播报（已经听过一轮、正在等确认）：迟到的完成回调不能把麦克风
         从「确认态」抢回「口述态」。 */
      return;
    }
    if(AUTO.phase) return;                 /* 已经在听/已经在等结果：迟到的「播报结束」别重开麦 */
    if(sid && AUTO.sid && sid !== AUTO.sid) return;
    nDbg('Nsaydone');
    autoClearTimers();
    /* 回调说念完了，再确认壳真的不在出声（扬声器尾巴/紧接着的第二段）。 */
    autoQuietBegin('dictate', 8000, 500);
  };
  window.__cbAutoEnd = function(){ if(AUTO.armed || AUTO.mine) autoEnd('external'); };
  /* 壳播报没放出来（合成音频取不到/放不了）：壳已经补了一声通知提示音，
     这里留一行进开发机日志——查「为什么没语音」时不用再靠猜。 */
  window.__cbSayFail = function(kind){ nDbg('Nsayfail '+(kind||'?')); };

  /* 布防后到真正开麦之间隔了播报的时间（最长 15 秒）：这期间人可能已经切页、
     锁屏、关开关。条件不再成立就安静收摊，别在后台偷偷录音。 */
  function autoStillHere(){
    return autoEnabled() && AUTO.sid === SID && pageIdx === 1 && !hiddenNow();
  }
  /* 壳是不是还在出声（播报/提示语）。true/false；老壳没有这个接口 → null。
     播报念完的「完成回调」有时比扬声器真正安静早一点，而且下载音频、排队播放
     都发生在回调之前，光靠固定秒数必然会「把播报录进去」。所以开麦前再问一次。 */
  function autoVoiceBusy(){
    try{
      if(window.CB && typeof CB.voiceBusy === 'function') return !!CB.voiceBusy();
    }catch(e){}
    if(SAYBUSY) return true;
    return null;
  }
  function autoHasVoiceBusy(){
    try{ return !!(window.CB && typeof CB.voiceBusy === 'function'); }catch(e){ return false; }
  }
  /* 等真正安静下来再开麦：连续 quietMs 都报「没在放」才开。
     壳不报状态（老版本）就直接开——和以前一样，别把人晾着。 */
  function autoQuietBegin(phase, maxMs, quietMs){
    /* 只挡「正在录音」：AUTO.phase 在拿到转写结果后会短暂残留上一阶段，
       拿它当闸门会把确认阶段的麦克风永久挡住（2026-11 实测：说「发送」毫无反应）。 */
    if(!AUTO.armed || AUTO.mine) return;
    var need = quietMs || 500, quiet = 0, t0 = Date.now();
    if(!autoHasVoiceBusy()){ autoBegin(phase); return; }
    (function poll(){
      AUTO.pollTimer = null;
      if(!AUTO.armed || AUTO.mine) return;
      if(!autoStillHere()){ autoEnd('gone'); return; }
      var b = autoVoiceBusy();
      if(b !== true && !SAYBUSY) quiet += 250; else quiet = 0;
      if(quiet >= need){ nDbg('Nquiet '+phase); autoBegin(phase); return; }
      if(Date.now() - t0 > (maxMs || 8000)){ nDbg('Nquietmax '+phase); autoBegin(phase); return; }
      AUTO.pollTimer = setTimeout(poll, 250);
    })();
  }
  /* 确认阶段的开麦提示：以前这里是壳里内置的一句语音「说发送就发，说取消就不发」，
     念完再开麦。用户要求去掉这句话——只留一声提示音（autoBegin 里的 ping「叮」），
     该说「发送」还是「取消」看屏幕上的状态条就行。
     仍然先等播报安静下来（autoQuietBegin）：别把 AI 播报的尾巴录进麦里。 */
  function autoConfirmPrompt(){
    if(!AUTO.armed || AUTO.mine) return;
    if(!autoStillHere()){ autoEnd('gone'); return; }
    autoClearTimers();
    nDbg('Ncueping');
    autoQuietBegin('confirm', 8000, 450);
  }
  /* 壳放完一条内置提示音（MainActivity.playCue） */
  window.__cbCueDone = function(k){
    nDbg('Ncuedone '+(k||'-'));
    if(!AUTO.waitCue) return;
    if(k && k !== 'confirm') return;
    AUTO.waitCue = false;
    if(AUTO.cueTimer){ clearTimeout(AUTO.cueTimer); AUTO.cueTimer = null; }
    if(AUTO.armed && !AUTO.mine) autoQuietBegin('confirm', 8000, 450);
  };

  function autoBegin(phase){
    if(!AUTO.armed) return;
    if(!autoStillHere()){ nDbg('Nbegin-gone'); autoEnd('gone'); return; }
    nDbg('Nbegin '+phase);
    autoClearTimers();
    AUTO.phase = phase === 'confirm' ? 'confirm' : 'dictate';
    var ta = document.getElementById('txt');
    if(ta) { try{ ta.blur(); }catch(e){} }            /* 收起键盘：麦克风状态条别被挡住 */
    if(!autoSupported()){ autoEnd('unsupported'); return; }
    /* 开麦提示音：让人知道「现在在听你说」。响完再开麦——不然麦克风把提示音
       自己录进去，VAD 还可能把它当成有人在说话（实测转写就是「叮。」）。
       口述和确认用两种不同的音，一听就知道现在是「说内容」还是「说发送/取消」。
       WebAudio 挂起时 beep 不响，那就少等一点，别让人干等。 */
    var beeped = false;
    try{ if(!muteOn()&&!dndActive()) beeped = (beep(AUTO.phase === 'confirm' ? 'ask' : 'ping') === true); }catch(e){}
    AUTO.beginTimer = setTimeout(function(){
      AUTO.beginTimer = null;
      if(!AUTO.armed || AUTO.mine) return;
      if(!autoStillHere()){ nDbg('Ntimergone'); autoEnd('gone'); return; }
      nDbg('Ncall');
      try{ CB.autoListenStart(AUTO.phase); }
      catch(e){ nDbg('Ncallerr'); autoEnd('fail', '自动听写启动失败'); }
    }, beeped ? 800 : 350);
  }

  /* 壳 → 页面的自动录音状态 */
  window.__autoListen = function(kind, info){
    kind = String(kind || '');
    if(kind === 'started'){
      nDbg('Nmic-on');
      AUTO.mine = true; AUTO.at = Date.now(); voiceRec(1); autoMic(true);
      autoPaint();
      if(AUTO.tick) clearInterval(AUTO.tick);
      AUTO.tick = setInterval(autoPaint, 1000);
      return;
    }
    if(kind === 'level'){                 /* 每秒一次原始振幅：判定门限看现场数据调 */
      nDbg('Nlvl ' + (info || ''));
      return;
    }
    if(kind === 'speech'){
      nDbg('Nspeech ' + (info || ''));
      show(AUTO.phase === 'confirm' ? '🎤 听到了，说完自动识别' : '🎤 听到了，说完自动停');
      return;
    }
    if(kind === 'uploading'){
      if(AUTO.tick){ clearInterval(AUTO.tick); AUTO.tick = null; }
      autoMic(false); voiceRec(0);
      var up0 = Date.now();
      show('转写中…');
      /* 让等待可见：没有秒数时，网络慢/排队久会像「卡死」 */
      AUTO.tick = setInterval(function(){
        show('转写中… ' + Math.floor((Date.now() - up0) / 1000) + 's');
      }, 1000);
      /* 网络卡住时别永远挂着：70 秒没收摊就自己关 */
      if(AUTO.upTimer) clearTimeout(AUTO.upTimer);
      AUTO.upTimer = setTimeout(function(){
        AUTO.upTimer = null;
        if(AUTO.mine) autoEnd('fail', '转写超时，自动听写已关闭');
      }, 70000);
      return;
    }
    if(kind === 'idle'){
      nDbg('Nidle ' + (info || ''));
      var wasConfirm = (AUTO.phase === 'confirm');
      /* 一次没听到不算完：多数是开口晚了/声音小。收回麦、提示一句再听一次
         （最多一轮），比直接关掉「自动听写」更像在帮忙。 */
      if(AUTO.rounds < 1 && autoStillHere()){
        AUTO.rounds++;
        AUTO.mine = false; AUTO.phase = '';
        if(AUTO.tick){ clearInterval(AUTO.tick); AUTO.tick = null; }
        autoMic(false); voiceRec(0);
        show('没听到，再说一次', true);
        setTimeout(function(){
          if(!AUTO.armed) return;
          if(!autoStillHere()){ autoEnd('gone'); return; }
          if(wasConfirm) autoConfirmPrompt(); else autoBegin('dictate');
        }, 700);
        return;
      }
      autoEnd('idle', wasConfirm ? '没听到确认，自动听写已关闭' : '没听到你说话，自动听写已关闭');
      return;
    }
    if(kind === 'nodata'){
      autoEnd('nodata', '这台手机读不到麦克风音量，自动听写用不了（可以手按 🎤）');
      return;
    }
    if(kind === 'busy'){ autoEnd('busy'); return; }
    if(kind === 'cancel'){ nDbg('Ncancel ' + (info || '-')); autoEnd('cancel'); return; }
    if(kind === 'error'){ autoEnd('fail', '录音失败：' + (info || '')); return; }
  };

  function autoPaint(){
    if(!AUTO.mine) return;
    var sec = Math.floor((Date.now() - AUTO.at) / 1000);
    if(AUTO.phase === 'confirm')
      show('🎤 在听…说「发送」发出，说「取消」算了（' + sec + 's）');
    else
      show('🎤 在听…说完了自动识别（' + sec + 's）');
  }

  /* 收摊：清计时器、停麦、恢复页面状态。msg 给了就提示一句。 */
  function autoEnd(reason, msg){
    var phase = AUTO.phase;
    nDbg('Nend '+reason+(phase?('/'+phase):''));
    autoClearTimers();
    var wasMine = AUTO.mine;
    AUTO.armed = false; AUTO.mine = false; AUTO.phase = ''; AUTO.sid = ''; AUTO.echo = '';
    try{ if(window.CB && CB.autoListenStop) CB.autoListenStop(); }catch(e){}
    autoMic(false);
    if(wasMine) voiceRec(0);
    if(msg){ show(msg, true); hide(2800); }
    else if(phase){ try{ st.style.display = 'none'; }catch(e){} }
  }

  /* 转写结果（AUTO.mine 时会走到这里） */
  function autoResult(json){
    nDbg('Nresult '+(AUTO.phase||'-')+(AUTO.armed?'':' unarmed'));
    if(!AUTO.armed) return;        /* 已经收摊了（用户取消/切走）：迟到的结果别再往框里塞 */
    track('asr_result');
    var phase = AUTO.phase;
    AUTO.mine = false;
    AUTO.phase = '';        /* 结果到了就不再「在听」：残留会让下一阶段的闸门误判 */
    autoClearTimers();
    var d = null;
    try{ d = JSON.parse(json); }catch(e){}
    /* 页面自己上传的那条路回纯文本（成功才会回调）：包成同形状，别当成转写失败 */
    if(!d && typeof json==='string' && json) d = {ok: true, text: json};
    if(!d || !d.ok){ autoEnd('fail', '转写失败：' + ((d && d.error) || '未知')); return; }
    useBillPTT(d);          /* 自动听写这条路是壳直接回调的（不经 __pttResult），在这里补记账 */
    var text = (d.text || '').trim();
    nDbg('Ntxt ' + ((AUTO.phase || phase) || '-') + ':' + text.slice(0, 30));
    if(text && asrEcho(text)) text = '';
    if(text && autoLooksLikeEcho(text)){ nDbg('Necho'); text = ''; }
    if(text && autoIsPromptEcho(text)){ nDbg('Necho2'); text = ''; }
    if(!text){
      if(phase === 'confirm' && AUTO.miss++ < AUTO_MAX_ROUNDS){
        show('没听清，再说一次「发送」或「取消」', true);
        setTimeout(function(){ if(AUTO.armed) autoBegin('confirm'); }, 700);
        return;
      }
      /* 口述阶段没转出字：再给一次机会，别一句话没听清就把自动听写关掉 */
      if(phase !== 'confirm' && AUTO.rounds < 1 && autoStillHere()){
        AUTO.rounds++;
        nDbg('Nempty');
        show('没听清，再说一次', true);
        setTimeout(function(){ if(AUTO.armed) autoBegin('dictate'); }, 600);
        return;
      }
      autoEnd('fail', '没听清，自动听写已关闭');
      return;
    }
    if(phase === 'confirm'){
      if(autoIsCancel(text)){
        /* 说「取消」= 这段不要了：输入框一起清掉（用户要求，别让半句话留在框里） */
        var taC = document.getElementById('txt');
        if(taC){
          taC.value = '';
          try{ fitTxt(); }catch(e){}
        }
        try{ window.__cbAsrSnap = null; }catch(e){}
        autoEnd('cancel', '👌 已取消，输入框清空了');
        return;
      }
      if(autoIsSend(text)){
        var ta0 = document.getElementById('txt');
        if(!ta0 || !(ta0.value || '').trim()){ autoEnd('fail', '输入框是空的，没发出去'); return; }
        autoEnd('');
        show('发送…'); hide(900);
        setTimeout(function(){ try{ sendMsg(); }catch(e){} }, 80);
        return;
      }
      /* 不是「发送/取消」：先滤掉「好的/嗯」这类口头语，再当用户接着说。
         用户要求：说「发送」之前可以一直往下补内容，补多少轮都行——
         所以内容分支**不消耗次数**（原来补 3 次就整段关掉，等于不让连续输入）。
         次数（AUTO.miss）只留给「没听清」的重问，防止对着麦瞎转圈。 */
      if(autoIsFiller(text)){
        show('要说「发送」才会发出，说「取消」就不发', true);
        if(++AUTO.miss <= AUTO_MAX_ROUNDS)
          setTimeout(function(){ if(AUTO.armed) autoConfirmPrompt(); }, 700);
        else autoEnd('fail', '没确认发送，已关闭（内容还在框里）');
        return;
      }
      if(text.length <= 6){
        /* 短句有风险（可能是「发送」被听成「发松」），不当补充塞进框里，重问一次 */
        AUTO.miss++;
        show('没听清（听到「' + text + '」），再说一次「发送」或「取消」', true);
        if(AUTO.miss <= AUTO_MAX_ROUNDS)
          setTimeout(function(){ if(AUTO.armed) autoConfirmPrompt(); }, 700);
        else autoEnd('fail', '没确认发送，已关闭（内容还在框里）');
        return;
      }
      AUTO.miss = 0;                     /* 正常补内容：把「没听清」的计数清回去 */
      insertAsrText(text);
      show('加上：' + text + '（还可以继续说，说「发送」才发）'); hide(2000);
      setTimeout(function(){ if(AUTO.armed) autoConfirmPrompt(); }, 700);
      return;
    }
    /* 口述阶段只听到「叮」这类开麦提示音/口水音：不是内容，重开一次麦，
       别把它填进输入框（填了就变成「没听懂我说的话」）。 */
    if(autoIsNoise(text) && AUTO.rounds < 2){
      AUTO.rounds++;
      nDbg('Nnoise ' + text);
      show('没听清，再说一次', true);
      setTimeout(function(){ if(AUTO.armed) autoBegin('dictate'); }, 500);
      return;
    }
    /* 口述阶段：填框 → 语音问一句「说发送就发」→ 再开麦等确认。
       用壳里的内置提示语（和播报同一个嗓音），念完再开麦，别把提示音自己录进去。 */
    insertAsrText(text);
    show('已说：' + text + '\n说「发送」发出，说「取消」算了');
    setTimeout(function(){ if(AUTO.armed) autoConfirmPrompt(); }, 700);
  }
  /* 壳把自动听写的转写结果直接递进来（不走 __pttResult：那个会被 shim 抢走） */
  window.__autoResult = autoResult;

  /* ── 原生音频转页上传：壳的 getCookie 在老 WebView 读不到 httponly cookie
     （坚果 Chromium 62 实锤）时，壳把 m4a base64 分块递进来，由页面用
     WebView 网络栈上传（cookie 自动带上）。n=0 即重新积累。 ── */
  var pttBuf = [];
  window.__pttAudioChunk = function(n, total, b64){
    if (n === 0) pttBuf = [];
    pttBuf.push(String(b64));
  };
  window.__pttGo = function(ms, auto){
    var b64 = pttBuf.join(''); pttBuf = [];
    if (!b64) { pttWaitStop(); if(!AUTO.mine) { show('没收到音频', true); hide(4000); } else autoEnd('fail','没收到音频'); return; }
    if (!pttTick && !AUTO.mine) pttWaitStart(ms);   /* 通常 __pttDone 已经起了计时，这里兜底 */
    var gen = pttGen;
    try {
      var bin = atob(b64), arr = new Uint8Array(bin.length);
      for (var i = 0; i < bin.length; i++) arr[i] = bin.charCodeAt(i);
      var blob = new Blob([arr], {type: 'audio/mp4'});
      pttRequest(blob, 'audio/mp4', ms, gen, !!auto);
    } catch (e) { pttWaitStop(); if(auto) autoEnd('fail', '音频解码失败'); else { show('音频解码失败：' + e, true); hide(5000); } }
  };

  /* ── 网页通路（Mac 浏览器 / iOS Safari） ──
     iOS 坑：首次按住会弹麦克风授权框，手势被弹窗吃掉——touchend 先到而
     getUserMedia 还没回来，此时松手若只置空 mr，授权后 mr.start() 就没人
     叫它停，按钮永远「录音中」还占着麦克风。所以授权期间松手要记成
     pendingStop，授权回来立刻空停丢弃；再加 60s 看门狗兜底。 */
  function tear(){
    if (watch) { clearTimeout(watch); watch = null; }
    if (mr) { try{ mr.onstop = null; mr.stop(); }catch(e){} mr = null; }
    if (stream) { try{ stream.getTracks().forEach(function(t){t.stop();}); }catch(e){} stream = null; }
  }
  function webStart(){
    if (!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia)) {
      show('这个页面拿不到麦克风（需 App 壳或 https）', true); hide(5000); return;
    }
    if (stream || mr) { tear(); ui(false); }          /* 残留状态先拆干净再录 */
    t0 = Date.now(); chunks = []; pendingStop = false;
    ui(true);
    navigator.mediaDevices.getUserMedia({audio:true}).then(function(s){
      stream = s;
      var mime = 'audio/webm';
      try{
        if (window.MediaRecorder && MediaRecorder.isTypeSupported) {
          if (MediaRecorder.isTypeSupported('audio/webm;codecs=opus')) mime = 'audio/webm;codecs=opus';
          else if (MediaRecorder.isTypeSupported('audio/mp4')) mime = 'audio/mp4';   /* iOS Safari */
        }
      }catch(e){}
      try{
        mr = new MediaRecorder(s, {mimeType: mime});
      }catch(e1){
        try{ mr = new MediaRecorder(s); }catch(e2){
          show('MediaRecorder 不可用：' + e2.name, true); hide(5000);
          tear(); ui(false); return;
        }
      }
      mr.ondataavailable = function(e){ if (e.data && e.data.size) chunks.push(e.data); };
      mr.onstop = function(){
        var ms = Date.now() - t0;
        var drop = pendingStop; pendingStop = false;
        var type = (mr && mr.mimeType) || mime;
        var blob = new Blob(chunks, {type: type});
        chunks = [];
        tear();
        ui(false);
        if (drop) { show('已取消'); hide(1200); return; }      /* 授权弹窗吃掉的那次 */
        if (!blob.size || ms < 400) { show('太短了，按住再说', true); hide(2000); return; }
        var gen = pttGen;
        window.__pttDone(ms);
        pttRequest(blob, type, ms, gen);
      };
      mr.start();
      window.__pttStart();
      watch = setTimeout(function(){ try{ mr.stop(); }catch(e){} }, 60000);  /* 卡死兜底 */
      if (pendingStop) { try{ mr.stop(); }catch(e){} }       /* 松手发生在授权期间 */
    }).catch(function(e){
      tear(); ui(false);
      show('麦克风不可用：' + e.name + ' — ' + e.message, true); hide(6000);
    });
  }

  function webStop(){
    if (mr && mr.state !== 'inactive') { try{ mr.stop(); }catch(e){} }
    else if (!mr) { pendingStop = true; }     /* 还在等授权：回来就丢 */
    ui(false);
  }

  /* ── 结果处理：填进输入框并发送 ── */
  /* 静音/极短录音时，云端 ASR 会把「场景提示词」原样吐回来（不是你说的话）。
     兜一道：看起来是提示词就直接丢掉，别填进输入框、更别自动发出去。 */
  function asrEcho(t){
    var s = String(t || '').replace(/\s+/g, '');
    return s.indexOf('下面是常见技术词') >= 0 || s.indexOf('不要自行纠正拼写') >= 0
        || s.indexOf('开发者用手机语音') >= 0 || s.indexOf('ClaudeCode,Claude,Codeboss') >= 0;
  }
  /* 把识别文本写进输入框（光标处/末尾），返回输入框；没有输入框就只弹出文字。
     自动听写和手动说话共用这一段。 */
  function insertAsrText(text){
    var ta = document.getElementById('txt');
    if (!ta) { show(text); hide(5000); return null; }
    /* 光标在输入框里（或刚在）→ 插到光标处；替换选中内容。否则接在末尾另起一行。 */
    var cur = ta.value || '';
    var s = null, e = null;
    if (document.activeElement === ta && typeof ta.selectionStart === 'number') {
      s = ta.selectionStart; e = ta.selectionEnd;
    } else if (caretMark && Date.now() - caretMarkT < 60000 && caretMark.n === cur.length) {
      s = caretMark.s; e = caretMark.e;
    }
    if (s === null) {
      /* 多次识别当增量：接着上次的说，不覆盖（说三句 → 框里三行 → 一次派活） */
      var tail = cur.replace(/\s+$/, '');
      ta.value = tail ? tail + '\n' + text : text;
      s = ta.value.length;
    } else {
      s = Math.max(0, Math.min(s, cur.length));
      e = Math.max(s, Math.min(e, cur.length));
      ta.value = cur.slice(0, s) + text + cur.slice(e);
      s = s + text.length;
    }
    caretMark = null;
    try { ta.setSelectionRange(s, s); } catch (x) {}
    ta.dispatchEvent(new Event('input', {bubbles:true}));
    /* 留一份「识别后输入框的样子」：用户改完再发时，用差异学项目词表。 */
    try { window.__cbAsrSnap = {full: ta.value, at: Date.now(), cwd: pttCwd(), proj: pttProj()}; } catch (x) {}
    return ta;
  }
  /* ── 语音输入写到哪个框（2026-11-21 用户） ──
     以前结果只往会话输入框 #txt 里塞。现在：焦点在哪个输入框就写哪个（搜索/派单/
     反馈…）；焦点不在输入框 → 还是 #txt（盲操：任何页面说一句都落到当前会话，
     按「自动发送」开关决定发不发）。密码/验证码这类框不写——宁可拒绝，也不能把
     「6868」听成一句话发到会话里去。 */
  function voiceKind(el){
    if (!el || !el.tagName) return '';
    var tag = String(el.tagName).toUpperCase();
    if (tag !== 'INPUT' && tag !== 'TEXTAREA') return '';
    if (el.readOnly || el.disabled) return 'sensitive';
    var im = '';
    try { im = String(el.getAttribute('inputmode') || '').toLowerCase(); } catch (x) {}
    if (im === 'numeric' || im === 'tel') return 'sensitive';
    if (tag === 'INPUT') {
      var ty = String(el.type || 'text').toLowerCase();
      if (ty === 'password' || ty === 'tel' || ty === 'number') return 'sensitive';
      if (ty !== 'text' && ty !== 'search' && ty !== 'url' && ty !== 'email') return '';
    }
    return 'field';
  }
  /* 写进任意输入框：有焦点插光标处，没焦点接末尾。写完发 input 事件——
     各页面靠它同步草稿（工作台搜索框的 IB_Q 就是这么拿到的）。 */
  function insertIntoEl(el, text){
    var cur = String(el.value || '');
    var s = null, e = null;
    if (document.activeElement === el && typeof el.selectionStart === 'number') {
      s = el.selectionStart; e = el.selectionEnd;
    }
    if (s === null) {
      el.value = cur + text; s = el.value.length;
    } else {
      s = Math.max(0, Math.min(s, cur.length));
      e = Math.max(s, Math.min(e, cur.length));
      el.value = cur.slice(0, s) + text + cur.slice(e);
      s = s + text.length;
    }
    try { el.setSelectionRange(s, s); } catch (x) {}
    el.dispatchEvent(new Event('input', {bubbles:true}));
    return el;
  }
  /* 给某个焦点元素算出「结果交给谁」。返回 null = 走 #txt 的老路。 */
  var voicedSeq = 0;
  function voiceTargetFn(a){
    if (!a) return null;
    var k = voiceKind(a);
    if (k === 'sensitive') return function(){
      /* 调用方（页面 handleResult / 壳里 shim）紧接着会自己 show('已识别')，
         所以晚一拍再报拒绝，别让它盖掉。 */
      setTimeout(function(){ show('这个框不支持语音输入（密码/验证码），请手动输入', true); hide(4000); }, 0);
    };
    if (k !== 'field') return null;
    /* fb* = 设置页「问题与建议」那一套（fbText/fbTitle…）：它自己带麦克风、结果
       也不该走这里（会把它的「转写中」标签卡住），整块让给它。 */
    if (a.id !== 'txt' && !/^fb/.test(a.id || '')) {
      var id = a.id;
      if (!id) { id = 'cbvf' + (++voicedSeq); try { a.id = id; } catch (x) {} }
      return function(t){
        var el = document.getElementById(id);
        if (!el) { insertAsrText(t); return; }     /* 框被重画掉了：退回会话框，别把话丢了 */
        insertIntoEl(el, t);
        try { el.focus(); el.setSelectionRange(el.value.length, el.value.length); } catch (x) {}
        setTimeout(voiceSyncFocus, 0);             /* 结果用掉了钩子，再挂回去接着听 */
      };
    }
    return null;   /* 会话框：#txt 走老路（自动发送开关只在会话页生效） */
  }
  function voiceSyncFocus(){
    window.__cbPttTarget = voiceTargetFn(document.activeElement);
    window.__cbPttTargetAt = window.__cbPttTarget ? Date.now() : 0;
  }
  document.addEventListener('focusin', voiceSyncFocus);
  /* 失焦（键盘收起/点了别处）也要重算：盒子还在不在、变成哪个了。
     晚一拍——失焦之后可能马上又聚焦到新框，别用中间的空白态把目标清掉。 */
  document.addEventListener('focusout', function(){ setTimeout(voiceSyncFocus, 0); });

  function handleResult(json){
    var d;
    try{ d = JSON.parse(json); }catch(e){ show('返回异常：' + String(json).slice(0,120), true); hide(6000); return; }
    /* 设置页的「问题与建议」录音：结果交给它，别往命令输入框里塞。
       超过 3 分钟没结果就作废，免得下一次在会话页说话被塞进看不见的反馈框。 */
    var target = window.__cbPttTarget;
    if (target && Date.now() - (window.__cbPttTargetAt || 0) > 180000) {
      window.__cbPttTarget = null; target = null;
    }
    if (!d || !d.ok) {
      window.__cbPttTarget = null;
      show('转写失败：' + ((d && d.error) || '未知'), true); hide(6000); return;
    }
    var text = (d.text || '').trim();
    if (text && asrEcho(text)) text = '';
    if (!text) {
      window.__cbPttTarget = null;
      show('没听清，再说一次', true); hide(2600); return;
    }
    if (target) {
      window.__cbPttTarget = null;
      try { target(text); } catch (e) {}
      show('已识别'); hide(1200);
      return;
    }
    var ta = insertAsrText(text);
    if (!ta) return;
    show('已说：' + text);
    hide(1600);
    if (typeof sendMsg === 'function') {
      /* 默认只填框不自动发：识别错一句就可能让 Claude 直接动手。
         要自动发就在 ⚙ 语音里显式选「自动发送」。 */
      var auto = false;
      try{
        var c = (typeof vsCfg === 'function') ? vsCfg() : {};
        if (c && c.autosend === true) auto = true;
      }catch(e){}
      if (auto) setTimeout(function(){ try{ sendMsg(); }catch(e){} }, 120);
    }
  }

  /* 说话方式（设置页的「按住 / 点按」）：安卓壳用自己的 shim.js 实现这套，
     iOS 没有 shim.js，页面自己来——不然 iOS 上「点按」这个开关完全没用。 */
  function pttMode(){
    try{
      const c = vsCfg();
      return (c && c.mode === 'toggle') ? 'toggle' : 'hold';
    }catch(e){ return 'hold'; }
  }
  function pttBusy(){          /* 正在录吗：页面标志 + 壳的原生状态 */
    if (down) return true;
    try{ return !!(window.CB && CB.pttState && CB.pttState() === 'rec'); }catch(e){ return false; }
  }

  /* 松手看门狗。两种「松手丢了」都要能收：
     ①页面以为松了（release 调过）但原生还在录 —— 老逻辑；
     ②手指已经离开屏幕，但 touchend 压根没送到页面（iOS 老毛病：触摸序列一断就再也收不到）。
       这时 down 还是 true，老逻辑完全不管，于是一路录到壳的 60 秒上限——用户看到的就是
       「这一句怎么转写这么久」，而且识别出来一大段废话。判据换成「屏幕上还有没有手指」：
       文档级 touchend/touchcancel 兜底记，只要这次按下来自触摸，就按这个收。 */
  var pttWatch = null, pttFinger = 0, pttViaTouch = 0, pttDownAt = 0, pttStarted = 0;
  var TOUCHY = ('ontouchstart' in window) || (navigator.maxTouchPoints > 0);
  function pttWatchStop(){ if (pttWatch){ clearInterval(pttWatch); pttWatch = null; } }
  function pttWatchStart(){
    pttWatchStop();
    pttWatch = setInterval(function(){
      var rec = false;
      try{ rec = !!(window.CB && CB.pttState && CB.pttState() === 'rec'); }catch(e){}
      if (!rec){ pttWatchStop(); return; }        /* 已经停了 */
      if (!down){                                  /* ①页面早松手了，原生还在录 */
        pttWatchStop();
        down = false;
        try{ if (window.CB && CB.pttUp) CB.pttUp(); }catch(e){}
        return;
      }
      /* ②手指没了（touchend 丢了）：按下 1.5 秒后手上还没手指，就当已经松手 */
      if (TOUCHY && pttViaTouch && pttMode() !== 'toggle' &&
          !pttFinger && Date.now() - pttDownAt > 1500){
        pttWatchStop();
        down = false; relSent = true;
        try{ if (window.CB && CB.pttUp) CB.pttUp(); }catch(e){}
      }
    }, 1500);
  }

  var relSent = false;             /* 一次按下只发一次「松手」 */
  function press(byTouch){
    nDbg('Npress t='+(byTouch?1:0)+' a='+(AUTO.armed?1:0)+' m='+(AUTO.mine?1:0));
    /* 手动按麦 → 自动听写收摊（别让两路抢同一支麦克风） */
    try{ if(window.__cbAutoEnd) window.__cbAutoEnd(); }catch(e){}
    /* 点按模式：再按一下 = 停（安卓 shim 的同一套语义） */
    if (pttMode() === 'toggle' && pttBusy()){
      down = false;
      try{ if (window.CB && CB.pttUp) { CB.pttUp(); return; } }catch(e){}
      webStop(); return;
    }
    track('ptt');
    relSent = false;
    pttDownAt = Date.now();
    pttViaTouch = byTouch ? 1 : 0;
    if (byTouch) pttFinger = 1;
    if (window.CB && CB.pttDown) {
      /* 原生起录要几十毫秒（AVAudioRecorder prepareToRecord），手指先松开的话
         __pttStart 还没回来 → down 还是 false → release() 直接 return → 录音
         停不下来、按钮永远停在「录音中」。所以按下态在这里就置上。 */
      try{ ui(true); CB.pttDown(); return; }catch(e){}
    }
    webStart();
  }
  function release(){
    if (pttMode() === 'toggle') return;      /* 点按模式：抬手不停，等再按一下 */
    if (relSent) return;                    /* 幂等：touchcancel + 文档级监听会各来一次 */
    var was = pttBusy();                    /* 判据看「在不在录」，不看单一标志 */
    nDbg('Nrel busy='+(was?1:0)+' a='+(AUTO.armed?1:0)+' m='+(AUTO.mine?1:0));
    down = false;
    if (!was) return;
    relSent = true;
    if (window.CB && CB.pttUp) { try{ CB.pttUp(); return; }catch(e){} }
    webStop();
  }

  btn.addEventListener('touchstart', function(e){ e.preventDefault(); press(true); }, {passive:false});
  btn.addEventListener('touchend',   function(e){ e.preventDefault(); release(); }, {passive:false});
  btn.addEventListener('touchcancel',function(e){ release(); });
  btn.addEventListener('mousedown',  function(e){ e.preventDefault(); press(false); });
  btn.addEventListener('mouseup',    function(e){ e.preventDefault(); release(); });
  /* 手指滑出按钮 / 页面被切走时，touchend 可能落在别的元素上（或者干脆不来）：
     在文档上兜一层，只要还按着就松开，别让录音卡住。 */
  document.addEventListener('touchend', function(e){
    if (down && !btn.contains(e.target)) release();
  }, {passive:false});
  document.addEventListener('touchcancel', function(){ if (down) release(); });
  /* 「屏幕上还有没有手指」：只做记录，不动状态机。touchend 就算没送到按钮上，
     也会先过这里。用 capture 抢在页面里任何 stopPropagation 前面。 */
  document.addEventListener('touchend', function(e){
    if (!e.touches || e.touches.length === 0) pttFinger = 0;
  }, {passive:true, capture:true});
  document.addEventListener('touchcancel', function(){ pttFinger = 0; }, {passive:true, capture:true});
  document.addEventListener('visibilitychange', function(){ if (hiddenNow() && down) release(); });

  /* 音量键由 App 直接接管；旧壳只发 __ptt(true/false) 的也兼容。
     那是壳明确给的「开始/结束」信号，不该被「点按」模式改写。 */
  window.__ptt = function(d){
    nDbg('NpttK '+(d?'down':'up')+' a='+(AUTO.armed?1:0)+' m='+(AUTO.mine?1:0));
    if (d) {
      /* 音量键/壳触发的录音没有「手指按在屏幕上」这回事：
         清掉触摸标记，免得被「松手丢了」的看门狗当成卡住而掐掉 */
      pttViaTouch = 0; pttFinger = 0; pttDownAt = Date.now(); relSent = false;
      try{ if (window.CB && CB.pttDown) { ui(true); CB.pttDown(); return; } }catch(e){}
      webStart(); return;
    }
    try{ if (window.CB && CB.pttUp) { down = false; CB.pttUp(); return; } }catch(e){}
    webStop();
  };

  /* 页面按着不放（给「页面自更新」的护栏用：录音中别重载） */
  window.__cbPttDown = function(){ return !!down; };

  /* 页面被换掉/重载前把原生录音停掉：ext.html 一改，手机上的页面会自动重载，
     那一刻手指底下的按钮被换掉，WebKit 的触摸序列断了 → touchend 送不到 →
     松手停不下来，麦克风一直开着。离场前主动停一次，宁可少录一句。 */
  function stopOnLeave(){
    try{ if(window.__cbAutoEnd) window.__cbAutoEnd(); }catch(e){}
    try{
      if (window.CB && CB.pttState && CB.pttState()==='rec' && window.CB.pttUp) window.CB.pttUp();
    }catch(e){}
  }
  window.addEventListener('pagehide', stopOnLeave);
  window.addEventListener('beforeunload', stopOnLeave);

  /* 刚打开页面发现原生还在录 → 上一轮页面被换掉时留下的孤儿录音，补停。 */
  setTimeout(function(){
    try{
      if (window.CB && CB.pttState && CB.pttState()==='rec'){
        if (window.CB.pttUp) window.CB.pttUp();
        show('上一段录音被中断了'); hide(2500);
      }
      /* 自动听写是上一页留下的：直接收掉，别一打开就偷听 */
      if (window.CB && CB.pttState && CB.pttState()==='auto' && window.CB.autoListenStop)
        window.CB.autoListenStop();
    }catch(e){}
  }, 1500);

  /* ── 按钮状态对账（iOS 没有 shim.js 那层每秒对账，这里必须有） ──
     按钮的颜色、页面里的 down、原生录音器是三个各自记账的东西，任何一次
     回调丢了（__pttStart 没送到、壳的 js() 打在旧 WebView 上、原生自己到
     60 秒上限停了）就会分家，表现就是「按钮一直红着」「明明在录按钮却是灰的」。
     每 2 秒以原生为准对一次：它在录而页面不知道 → 接管显示；它早停了页面
     还按着 → 收掉并说一句，别让人对着一个假的「录音中」发呆。 */
  setInterval(function(){
    /* 安卓壳的 shim.js 自己有一套对账，别抢它的活（否则两个状态条打架） */
    if (window.__cbShim || document.getElementById('cbshim_mic')) return;
    var nst = null;
    try{ nst = (window.CB && CB.pttState) ? CB.pttState() : null; }catch(e){}
    if (nst === null) return;                 /* 没有壳：网页录音走另一套，不用管 */
    if (nst === 'auto') return;               /* 自动听写有自己一套状态，别按按住说话来对账 */
    if (nst === 'rec'){
      /* 正在等转写结果：壳那边的 ptt 位可能还慢半拍，别把「录音中」又顶回来 */
      if (window.__cbPttWait) return;
      if (!down){
        down = true; pttViaTouch = 0; pttFinger = 0; pttDownAt = Date.now() - 3000;
        pttStarted = 1; ui(true); pttRecStart(); pttWatchStart();
      }
      return;
    }
    if (down && Date.now() - pttDownAt > 3000){
      var started = pttStarted;
      ui(false); pttWatchStop(); pttRecStop();
      /* 开起来过 → 只是收尾回调丢了，音频还在路上，别吓唬人；
         压根没开起来 → 告诉人再按一次，不然他以为自己在说话。 */
      if (started) { show('录音已结束'); hide(1200); }
      else { show('没录上，再按一次', true); hide(2500); }
    }
  }, 1000);
})();
