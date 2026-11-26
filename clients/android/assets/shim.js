// SPDX-License-Identifier: AGPL-3.0-or-later

/* Codeboss 壳内注入（v4）
 * 由 APK 在页面加载完成 + 每 2.5s 心跳时 evaluateJavascript 注入。
 * 手机端"必须永远存在"的能力都挂在这里：🎤、结果处理、物理键发送。
 * （v4：⚙ 设置浮层已删——设置全部并进中继 ext.html 右上角的 ⚙ 页；
 *   这里只留录音必须的 UI 和回调。老版本注入过的 pill/hint/sheet 残留会顺手清掉。）
 *
 * 设计原则（踩过的坑）：
 *  1) 幂等且**非破坏性**：节点已存在就复用，绝不删除重建 ——
 *     以前重建会把正在按的 🎤 换掉，touchend 落到新节点上，录音就一直录下去（幽灵录音），
 *     等它的结果回来时把上一段识别结果覆盖掉。
 *  2) 状态以原生为准：CB.pttState() 说在录才认为在录。
 *  3) 回调打 __cbMine 标记；页面脚本把 __pttStart/__pttResult 覆盖回去后，心跳能发现并重新挂上。
 *  4) 触摸后 900ms 内浏览器补发的 mouse 事件一律忽略（否则松手后又开一段）。
 *  5) 一切 class 带 cbsh_ 前缀 + !important，避免被页面自己的 .row/.box 打乱。
 *  6) 填字不抢焦点 —— focus 会唤起输入法键盘。
 *  7) 填字一律**追加**，不覆盖已有内容；同一句 8 秒内重复回来按幽灵结果丢弃。
 */
(function () {
  if (!document.body) { return; }   /* 页面还没 body：等下一拍，别挂到 <html> 上 */
  if (window.__cbpttVer) { return; } /* 反代（中继页/浏览器调试）已经注入过 */

  /* 已经装好、回调也还是我们这套 → 立刻返回。
     千万不能"每 2.5s 重跑一遍"：那样每次都会生成新的闭包，
     录音状态（down 标志）会分裂到不同实例里，表现成"按了没反应"。 */
  if (window.__cbShim === 'v4' && document.getElementById('cbshim_mic') &&
      window.__pttStart && window.__pttStart.__cbMine === 1 &&
      window.__pttResult && window.__pttResult.__cbMine === 1) {
    return;
  }

  /* 老版本（v3 及以前）注入过的设置浮层残留：清掉，避免出现两个入口 */
  var olds = ['cbshim_pill', 'cbshim_hint', 'cbshim_sheet'], oi;
  for (oi = 0; oi < olds.length; oi++) {
    var old = document.getElementById(olds[oi]);
    if (old) { try { old.parentNode.removeChild(old); } catch (e) { } }
  }

  var DOCK = 'var(--dockh,100px)';

  function el(tag, id, html) {
    var d = document.createElement(tag);
    if (id) { d.id = id; }
    if (html != null) { d.innerHTML = html; }
    return d;
  }
  function add(n) { (document.body || document.documentElement).appendChild(n); }

  /* ── 样式：只建一次（v4 会整体覆盖老版本留下的 cbshim_css） ── */
  var css = document.getElementById('cbshim_css');
  if (!css) {
    css = document.createElement('style');
    css.id = 'cbshim_css';
    (document.head || document.documentElement).appendChild(css);
  }
  css.textContent =
    '#cbshim_st{position:fixed!important;right:10px;bottom:calc(' + DOCK + ' + 74px);max-width:70vw;' +
      'background:#21262d!important;border:1px solid #2a3240;color:#e6edf3!important;font:12px/1.5 ' +
      '-apple-system,"PingFang SC",sans-serif;padding:7px 11px;border-radius:9px;z-index:99991;display:none;' +
      'white-space:pre-wrap;word-break:break-all;box-shadow:0 2px 10px rgba(0,0,0,.5)}' +
    '#cbshim_mic{position:fixed!important;right:10px;bottom:calc(' + DOCK + ' + 10px);width:54px;height:54px;' +
      'border-radius:50%;z-index:99990;display:none!important;align-items:center;justify-content:center;font-size:24px;' +
      'background:rgba(88,166,255,.16)!important;border:1px solid #58a6ff;color:#58a6ff!important;' +
      '-webkit-user-select:none;user-select:none;touch-action:none;box-shadow:0 2px 10px rgba(0,0,0,.45)}' +
    '#cbshim_mic.on{background:rgba(248,81,73,.28)!important;border-color:#f85149;color:#f85149!important}' +
    /* 语音按钮只跟会话页（有输入框）走；列表/设置页不出现 */
    'body.pg1 #cbshim_mic{display:flex!important}';

  /* ── 节点：缺哪个补哪个，已有的复用 ── */
  var st = document.getElementById('cbshim_st');
  if (!st) { st = el('div', 'cbshim_st', ''); add(st); }

  /* ── 配置（读写与 ext.html 设置页同一份：壳 CB.getCfg/setCfg，浏览器落 localStorage） ── */
  var cfg = { mode: 'hold', autosend: true };
  var coolUntil = 0;   /* 出结果后短暂闭麦：防止结果刚回来又被误触/残影触发一段新录音 */
  function loadCfg() {
    var raw = null;
    try { if (window.CB && CB.getCfg) { raw = CB.getCfg(); } } catch (e) { }
    if (raw) {
      try {
        var o = JSON.parse(raw);
        if (o && o.mode) { cfg = { mode: o.mode, autosend: o.autosend !== false }; return; }
      } catch (e) { }
    }
    try {
      var s = localStorage.getItem('cb_cfg') || localStorage.getItem('cbvs');
      if (s) { var p = JSON.parse(s); if (p && p.mode) { cfg = { mode: p.mode, autosend: p.autosend !== false }; } }
    } catch (e) { }
  }
  function saveCfg() {
    try { if (window.CB && CB.setCfg) { CB.setCfg(JSON.stringify(cfg)); } } catch (e) { }
    try { localStorage.setItem('cb_cfg', JSON.stringify(cfg)); } catch (e) { }
  }

  function show(msg, bad) {
    st.textContent = msg;
    st.style.display = 'block';
    st.style.color = bad ? '#f85149' : '#e6edf3';
    clearTimeout(st._h);
    st._h = setTimeout(function () { st.style.display = 'none'; }, bad ? 5000 : 2600);
  }

  /* ── 填框 / 发送 ── */
  /* 绝不覆盖已有内容（任何一段迟到的幽灵结果都不能把前面填好的字冲掉），
     但也不是无条件追加——用户把光标挪到中间再说话，结果就该落在光标处：
     ① 输入框还有焦点 → 用实时选区（点 🎤 时这里 touchstart 里 preventDefault，焦点不会丢）；
     ② 键盘收起导致失焦的，用失焦那一刻记下的选区（60 秒内、内容没变才作数）；
     ③ 都没有（自动听写 / 从没碰过输入框）→ 才接在末尾另起。 */
  var caretMark = null, caretMarkT = 0;
  document.addEventListener('focusout', function (ev) {
    var t = ev.target;
    if (!t || t.id !== 'txt' || typeof t.selectionStart !== 'number') return;
    caretMark = { s: t.selectionStart, e: t.selectionEnd, n: (t.value || '').length };
    caretMarkT = Date.now();
  });
  var lastFill = { text: '', at: 0 };
  function fill(text) {
    var ta = document.getElementById('txt');
    if (!ta) { return 'noinput'; }
    var t = String(text || '').replace(/^\s+|\s+$/g, '');
    if (!t) { return 'empty'; }
    var now = Date.now();
    /* 同一段文字 8 秒内重复回来 = 幽灵录音的重复结果，丢弃，避免同一句写两遍 */
    if (lastFill.text === t && (now - lastFill.at) < 8000) { return 'dup'; }
    var had = (document.activeElement === ta);
    var cur = String(ta.value || '');
    var cs = null, ce = null;
    if (had && typeof ta.selectionStart === 'number') {
      cs = ta.selectionStart; ce = ta.selectionEnd;
    } else if (caretMark && (now - caretMarkT) < 60000 && caretMark.n === cur.length) {
      cs = caretMark.s; ce = caretMark.e;
    }
    if (cs === null) {
      if (cur) {
        var a = cur.charAt(cur.length - 1), b = t.charAt(0);
        /* 中英混排才补空格：中文直接相接，不插空隙 */
        if (/[A-Za-z0-9]$/.test(a) && /^[A-Za-z0-9]/.test(b)) { cur += ' '; }
      }
      ta.value = cur + t;
      cs = ta.value.length;
    } else {
      cs = Math.max(0, Math.min(cs, cur.length));
      ce = Math.max(cs, Math.min(ce, cur.length));
      ta.value = cur.slice(0, cs) + t + cur.slice(ce);
      cs = cs + t.length;
    }
    lastFill = { text: t, at: now };
    caretMark = null;
    try { ta.selectionStart = ta.selectionEnd = cs; } catch (e) { }
    try { ta.dispatchEvent(new Event('input', { bubbles: true })); } catch (e) { }
    if (had) { try { ta.focus(); } catch (e) { } }   /* 不主动抢焦点，免得弹键盘 */
    return 'ok';
  }
  function doSend() {
    if (typeof window.sendMsg === 'function') { try { window.sendMsg(); return true; } catch (e) { } }
    var b = document.getElementById('send');
    if (b) { try { b.click(); return true; } catch (e) { } }
    return false;
  }

  var onResult = function (json) {
    loadCfg();   /* 按最新开关执行 */
    coolUntil = Date.now() + 800;   /* 结果回来后的 0.8s 内不接新的按下 */
    window.__cbVoiceBusy = 0; window.__cbVoiceUntil = Date.now() + 5000;   /* 落字/发送这几百毫秒别被打断 */
    var d;
    try { d = JSON.parse(json); } catch (e) { show('返回异常：' + String(json).slice(0, 100), true); return; }
    if (!d || !d.ok) { show('转写失败：' + ((d && d.error) || '未知'), true); return; }
    var text = (d.text || '').trim();
    if (!text) { show('没听清，再说一次', true); return; }
    /* 设置页「问题与建议」的录音：结果交给页面钩子，绝不填会话输入框、更不自动发。
       这个是页面在按下前挂的（window.__cbPttTarget），用完即清。 */
    if (window.__cbPttTarget) {
      var tfn = window.__cbPttTarget; window.__cbPttTarget = null;
      try { tfn(text); } catch (e) { }
      show('已识别：' + text);
      return;
    }
    var r = fill(text);
    if (r === 'dup') { show('重复结果，已忽略：' + text); return; }
    if (r === 'empty') { show('没听清，再说一次', true); return; }
    if (r === 'noinput') { show(text); return; }
    if (cfg.autosend) {
      if (doSend()) { show('已发送：' + text); } else { show('已填好，但没找到发送按钮', true); }
    } else {
      show('已填到输入框，改完点发送（或按音量下键）');
    }
  };
  window.__pttResult = onResult;
  window.__pttResult.__cbMine = 1;

  window.__cbSend = function (byKey) {
    loadCfg();
    var ta = document.getElementById('txt');
    if (!ta || !ta.value.trim()) { if (byKey) { show('没有可发送的内容', true); } return; }
    if (doSend()) { show('已发送'); } else { show('这个页面没有发送按钮', true); }
  };
  window.__cbSend.__cbMine = 1;

  /* ── 🎤 ── */
  (function () {
    var gone;
    while ((gone = document.getElementById('cbptt'))) { try { gone.parentNode.removeChild(gone); } catch (e) { break; } }

    var mic = document.getElementById('cbshim_mic');
    if (!mic) { mic = el('div', 'cbshim_mic', '🎤'); add(mic); }

    var down = false;
    function on() {
      down = true; mic.textContent = '■'; mic.className = 'on'; window.__cbBusy = 1;
      /* 语音忙标记：录音中 + 松手后等转写的那一段，页面都不许自更新重载 */
      window.__cbVoiceBusy = 1; window.__cbVoiceUntil = 0;
      try { if (window.CB && CB.log) { CB.log('ui.start'); } } catch (e) { }
      show('● 录音中… ' + (cfg.mode === 'hold' ? '松手结束' : '再按一下结束'));
    }
    function off() {
      down = false; mic.textContent = '🎤'; mic.className = ''; window.__cbBusy = 0;
      /* 录音停了、转写还在路上：留 25 秒宽限，重载早了这句话就丢了 */
      window.__cbVoiceBusy = 0; window.__cbVoiceUntil = Date.now() + 25000;
      try { if (window.CB && CB.log) { CB.log('ui.stop'); } } catch (e) { }
    }

    window.__pttStart = function () { on(); };
    window.__pttStart.__cbMine = 1;
    window.__pttDone = function (ms) { off(); show('转写中… ' + ((ms || 0) / 1000).toFixed(1) + 's'); };
    window.__pttDone.__cbMine = 1;
    window.__pttCancel = function () { off(); st.style.display = 'none'; };
    window.__pttCancel.__cbMine = 1;
    /* 给看门狗用：原生已经停了就把本地标志也清掉，避免"按了没反应" */
    window.__cbClearDown = function () { down = false; };
    window.__pttError = function (e) { off(); show('录音失败：' + e, true); };
    window.__pttError.__cbMine = 1;

    function nativeRec() {
      try { return !!(window.CB && CB.pttState && CB.pttState() === 'rec'); } catch (e) { return false; }
    }
    function log(m) { try { if (window.CB && CB.log) { CB.log(m); } } catch (e) { } }
    /* "在不在录"的判据不只看本地 down 标志：本地标志一旦漏清（回调没回来），
       就会表现为"点 🎤 毫无反应"——以原生 + UI 标志共同判断，能从这种卡死里恢复。 */
    function busyNow() { return down || window.__cbBusy === 1 || nativeRec(); }
    function press() {
      loadCfg();
      if (Date.now() < coolUntil) { log('press ignored (cooldown)'); return; }
      var nb = busyNow();
      log('press mode=' + cfg.mode + ' busy=' + nb + ' nat=' + (nativeRec() ? 'rec' : 'idle') + ' down=' + down);
      if (cfg.mode === 'toggle' && nb) { try { CB.pttUp(); } catch (e) { } return; }
      if (nb) { return; }
      try { CB.pttDown(); } catch (e) { show('壳没有接上录音接口', true); }
    }
    function release() {
      loadCfg();
      if (cfg.mode === 'toggle') { return; }
      var nb = busyNow();
      log('release busy=' + nb + ' nat=' + (nativeRec() ? 'rec' : 'idle') + ' down=' + down);
      if (!nb) { return; }
      try { CB.pttUp(); } catch (e) { }
    }

    if (!mic.__cbBound) {           /* 同一节点只绑一次，避免重复触发 */
      mic.__cbBound = 1;
      var lastTouch = 0;
      var hasTouch = ('ontouchstart' in window) || (navigator.maxTouchPoints > 0);
      mic.addEventListener('touchstart', function (e) {
        e.preventDefault(); lastTouch = Date.now(); press();
      }, { passive: false });
      mic.addEventListener('touchend', function (e) {
        e.preventDefault(); lastTouch = Date.now(); release();
      }, { passive: false });
      mic.addEventListener('touchcancel', function () { lastTouch = Date.now(); release(); });
      /* 触摸设备上不绑鼠标事件：WebView 会在 touchend 后补发 mousedown，那是幽灵录音的头号来源。
         （调试用鼠标时走桌面浏览器/反代那份注入。） */
      if (!hasTouch) {
        mic.addEventListener('mousedown', function (e) { e.preventDefault(); press(); });
        mic.addEventListener('mouseup', function (e) { e.preventDefault(); release(); });
      }
    }
  })();

  /* 焦点换输入框时让原生重新量缺口：键盘已经开着再换框不会触发一次窗口布局，
     原生那条 OnGlobalLayout 收不到，密码框就可能还压在键盘底下。 */
  if (!window.__cbImeBound) {
    window.__cbImeBound = 1;
    var kick = function () { setTimeout(function () { try { CB.ime && CB.ime(); } catch (e) { } }, 60); };
    document.addEventListener('focusin', kick);
    document.addEventListener('focusout', kick);
  }

  /* UI 与原生对齐：原生停了但 UI 还卡在"录音中"就复位（幽灵录音会让旧结果被覆盖） */
  if (!window.__cbSyncTimer) {
    window.__cbSyncTimer = setInterval(function () {
      var nb = false;
      try { nb = !!(window.CB && CB.pttState && CB.pttState() === 'rec'); } catch (e) { }
      if (!nb && window.__cbBusy) {
        window.__cbBusy = 0;
        try { if (window.__cbClearDown) { window.__cbClearDown(); } } catch (e) { }
        var m = document.getElementById('cbshim_mic');
        if (m) { m.textContent = '🎤'; m.className = ''; }
      }
    }, 2000);
  }

  window.__cbShim = 'v4';
  loadCfg();
})();
