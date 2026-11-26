// SPDX-License-Identifier: AGPL-3.0-or-later

/* 工头（新界面）—— 与老 App 并存的全屏子页。
 *
 * 只做三件事：把 host 算好的「活」摆成三块板；点开一件活能回到原会话；成果能看效果。
 * 状态全部来自 /api/boss/board（事实层），页面不自己猜；老接口一个不改：
 *   原会话 = switchTo()，原设置 = openSettings()，预览 = /pv，审批 = /api/ext_dlg_pick。
 */
(function () {
  'use strict';

  var BOSS = { on: false, data: null, view: 'list', work: null, timer: 0, loading: false };

  var CSS = [
    '#bossPage{background:#171310;color:#f4ead9;',
    'font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Helvetica Neue",Arial,sans-serif}',
    '#bossPage *{box-sizing:border-box}',
    '#bossPage .bhead{flex:none;padding:calc(10px + env(safe-area-inset-top,0px)) 16px 12px;',
    'background:linear-gradient(180deg,#1d1712 0%,#171310 100%);border-bottom:1px solid rgba(244,234,217,.08)}',
    '#bossPage .brow{display:flex;align-items:center}',
    '#bossPage .brow>*{margin-right:8px}',
    '#bossPage .brow>*:last-child{margin-right:0}',
    '#bossPage .bback{background:none;border:0;color:#a1937f;font-size:13px;padding:4px 0;font-family:inherit}',
    '#bossPage .bmach{flex:1;text-align:center;color:#a1937f;font-size:12px;',
    'white-space:nowrap;overflow:hidden;text-overflow:ellipsis}',
    '#bossPage .bic{background:rgba(244,234,217,.06);border:1px solid rgba(244,234,217,.12);',
    'color:#f4ead9;border-radius:9px;width:32px;height:32px;font-size:15px;line-height:1;padding:0;font-family:inherit}',
    '#bossPage .bsay{font-size:21px;line-height:1.45;font-weight:600;margin-top:14px;letter-spacing:.2px}',
    '#bossPage .bcnt{color:#a1937f;font-size:12.5px;margin-top:8px}',
    '#bossPage .bbody{flex:1 1 auto;min-height:0;overflow-y:auto;overflow-x:hidden;',
    '-webkit-overflow-scrolling:touch;overscroll-behavior-y:contain;padding:6px 16px 24px}',
    '#bossPage .bsec{margin-top:20px}',
    '#bossPage .bsec h3{margin:0 0 8px;font-size:12px;font-weight:600;letter-spacing:.12em;color:#a1937f}',
    '#bossPage .bsec h3 b{color:#f4ead9;font-weight:600;margin-left:4px}',
    '#bossPage .bcard{position:relative;background:#201a16;border:1px solid rgba(244,234,217,.12);',
    'border-radius:14px;padding:13px 14px;margin-bottom:9px}',
    '#bossPage .bcard:active{border-color:#c9f54e}',
    '#bossPage .bcard.wait{border-color:rgba(251,150,84,.5);background:#241a13}',
    '#bossPage .bcard.done{border-color:rgba(244,234,217,.08)}',
    '#bossPage .bct{display:flex;align-items:center;min-height:18px}',
    '#bossPage .bdot{flex:none;width:8px;height:8px;border-radius:50%;margin-right:8px;background:#6f6355}',
    '#bossPage .bdot.running{background:#c9f54e;box-shadow:0 0 0 3px rgba(201,245,78,.14)}',
    '#bossPage .bdot.idle{background:#8b7d6a}',
    '#bossPage .bdot.waiting{background:#fb9654;box-shadow:0 0 0 3px rgba(251,150,84,.14)}',
    '#bossPage .bdot.done{background:#f6ebd8}',
    '#bossPage .bct b{font-size:15px;font-weight:600;flex:1;min-width:0;white-space:nowrap;',
    'overflow:hidden;text-overflow:ellipsis}',
    '#bossPage .bmeta{flex:none;color:#8b7d6a;font-size:11.5px;margin-left:8px}',
    '#bossPage .bcs{color:#c9bda9;font-size:13.5px;line-height:1.55;margin-top:6px;',
    'display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden}',
    '#bossPage .bwhen{color:#6f6355;font-size:11.5px;margin-top:6px}',
    '#bossPage .bempty{color:#8b7d6a;font-size:13px;padding:6px 2px 2px}',
    /* 卡片右下角的麦克风：按住就说给这件活；拖滚动/点卡片仍是原行为。 */
    '#bossPage .bcard{padding-right:58px}',
    '#bossPage .bmic{position:absolute;right:10px;bottom:10px;width:40px;height:40px;border-radius:50%;',
    'border:1px solid rgba(201,245,78,.35);background:rgba(201,245,78,.1);color:#c9f54e;',
    'font-size:17px;line-height:1;padding:0;font-family:inherit;touch-action:none;',
    '-webkit-user-select:none;user-select:none}',
    '#bossPage .bmic.on{background:#c9f54e;color:#171310;box-shadow:0 0 0 5px rgba(201,245,78,.16)}',
    '#bossPage .bquick{position:absolute;left:0;right:0;bottom:0;z-index:8;background:#241d17;',
    'border-top:1px solid rgba(244,234,217,.14);padding:10px 16px calc(10px + env(safe-area-inset-bottom,0px))}',
    '#bossPage .bqhead{display:flex;align-items:center;font-size:12.5px;color:#a1937f;margin-bottom:6px}',
    '#bossPage .bqhead b{color:#f4ead9;font-weight:600;white-space:nowrap;overflow:hidden;',
    'text-overflow:ellipsis;max-width:55%}',
    '#bossPage .bqstate{flex:1;margin-left:10px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}',
    '#bossPage .bqclose{background:none;border:0;color:#8b7d6a;font-size:22px;line-height:1;padding:0 2px;',
    'font-family:inherit}',
    '#bossPage .bdhead{display:flex;align-items:center;margin:10px 0 4px}',
    '#bossPage .bgap{flex:1}',
    '#bossPage .bdback{background:none;border:0;color:#c9f54e;font-size:14px;padding:6px 0;font-family:inherit}',
    '#bossPage .bdtitle{font-size:20px;line-height:1.4;font-weight:650;margin:8px 0 2px}',
    '#bossPage .bdsub{color:#a1937f;font-size:12.5px;margin-bottom:12px}',
    '#bossPage .bdfact{background:#201a16;border:1px solid rgba(244,234,217,.1);border-radius:12px;',
    'padding:12px 14px;margin-bottom:10px}',
    '#bossPage .bdfact .k{color:#8b7d6a;font-size:11.5px;letter-spacing:.08em;margin-bottom:3px}',
    '#bossPage .bdfact .v{font-size:14px;line-height:1.55;color:#f4ead9;word-break:break-word}',
    '#bossPage .bdfact .v.warn{color:#fb9654}',
    '#bossPage .bdtl{border-left:1px solid rgba(244,234,217,.12);margin:6px 0 0 6px;padding:2px 0 2px 14px}',
    '#bossPage .bdtl .t{position:relative;margin-bottom:11px;color:#c9bda9;font-size:13px;line-height:1.5}',
    '#bossPage .bdtl .t:before{content:"";position:absolute;left:-19px;top:6px;width:7px;height:7px;',
    'border-radius:50%;background:#6f6355}',
    '#bossPage .bdtl .t:first-child:before{background:#c9f54e}',
    '#bossPage .bdtl .ts{color:#6f6355;font-size:11px;margin-right:6px}',
    '#bossPage .bact{display:flex;flex-wrap:wrap;margin:12px -4px 0}',
    '#bossPage .bact button{flex:1 1 40%;margin:4px;border-radius:11px;padding:11px 8px;font-size:14px;',
    'font-family:inherit;font-weight:600;border:1px solid rgba(244,234,217,.16);background:#201a16;color:#f4ead9}',
    '#bossPage .bact button.pri{background:#c9f54e;color:#171310;border-color:#c9f54e}',
    '#bossPage .bact button.warn{background:#fb9654;color:#171310;border-color:#fb9654}',
    '#bossPage .bact button.deny{color:#fb9654}',
    '#bossPage .binput{width:100%;background:#201a16;border:1px solid rgba(244,234,217,.16);',
    'color:#f4ead9;border-radius:11px;padding:11px 12px;font-family:inherit;font-size:15px;line-height:1.4;',
    'resize:none;min-height:46px;margin-top:10px}',
    '#bossPage .barts{margin-top:10px}',
    '#bossPage .bart{display:flex;align-items:center;background:#201a16;border:1px solid rgba(244,234,217,.1);',
    'border-radius:10px;padding:8px 10px;margin-bottom:7px}',
    '#bossPage .bart .ico{flex:none;width:34px;height:34px;border-radius:7px;background:#2a231c;margin-right:10px;',
    'display:flex;align-items:center;justify-content:center;font-size:15px;overflow:hidden}',
    '#bossPage .bart .ico img{width:100%;height:100%;object-fit:cover}',
    '#bossPage .bart .nm{flex:1;min-width:0;font-size:13px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}',
    '#bossPage .bart .sz{flex:none;color:#8b7d6a;font-size:11px;margin-left:8px}',
    '#bossPage .bhint{color:#8b7d6a;font-size:12px;line-height:1.6;margin-top:8px}',
    '#bossPage .bdnote{color:#8b7d6a;font-size:12px;line-height:1.6;margin-top:6px}',
    '#bossPage .bdtool{font-size:12.5px;line-height:1.6;color:#c9f54e;word-break:break-all;margin-bottom:4px}',
    '#bossPage .bload{color:#a1937f;text-align:center;padding:40px 0;font-size:14px}',
    '#bossBtn{font-weight:700}'
  ].join('');

  function $(id) { return document.getElementById(id); }

  function esc2(s) { try { return esc(s); } catch (e) { return String(s == null ? '' : s); } }

  function ago(ts) {
    var s = Date.now() / 1000 - (ts || 0);
    if (s < 0) s = 0;
    if (s < 60) return '刚刚';
    if (s < 3600) return Math.floor(s / 60) + ' 分钟前';
    if (s < 86400) return Math.floor(s / 3600) + ' 小时前';
    return Math.floor(s / 86400) + ' 天前';
  }

  function size(n) {
    n = Number(n || 0);
    if (n >= 1048576) return (n / 1048576).toFixed(1) + 'M';
    if (n >= 1024) return Math.round(n / 1024) + 'K';
    return n + 'B';
  }

  function icon(kind) {
    return ({ image: '🖼', video: '▶', audio: '♫', page: '🌐', doc: '📄',
              model: '◇' })[kind] || '·';
  }

  function build() {
    if ($('bossPage')) return;
    var st = document.createElement('style');
    st.id = 'bossCss';
    st.textContent = CSS;
    document.head.appendChild(st);
    var d = document.createElement('div');
    d.id = 'bossPage';
    d.className = 'subpage';
    d.innerHTML =
      '<div class="bhead">' +
        '<div class="brow">' +
          '<button class="bback" onclick="bossClassic()">← 经典版</button>' +
          '<span class="bmach" id="bossMach">工头</span>' +
          '<button class="bic" onclick="bossSessions()" title="全部会话" aria-label="全部会话">⌂</button>' +
          '<button class="bic" onclick="openSettings()" title="设置" aria-label="设置">⚙</button>' +
        '</div>' +
        '<div class="bsay" id="bossSay">…</div>' +
        '<div class="bcnt" id="bossCnt"></div>' +
      '</div>' +
      '<div class="bbody" id="bossBody"><div class="bload">正在问工头…</div></div>' +
      /* 说完先落在这里：不用点进一件活，看一眼、改一改就发。 */
      '<div class="bquick" id="bossQuick" style="display:none">' +
        '<div class="bqhead"><b id="bossQTitle">对这件活说话</b>' +
          '<span class="bqstate" id="bossQState"></span>' +
          '<button class="bqclose" onclick="bossQuickClose()" aria-label="关掉">×</button></div>' +
        '<textarea class="binput" id="bossQuickInput" rows="2" ' +
          'placeholder="按住卡片上的麦克风说，或在这里写一句…"></textarea>' +
        '<div class="bact" style="margin-top:8px">' +
          '<button class="pri" onclick="bossQuickSend()">发出去</button></div>' +
      '</div>';
    var set = $('setPage');
    if (set && set.parentNode) set.parentNode.insertBefore(d, set);
    else document.body.appendChild(d);
    /* 左右滑 = 上一件/下一件：手机上不用退回板上再点（页内元素会被重画，绑在外层）。 */
    var body = $('bossBody');
    var SW = { x: 0, y: 0 };
    body.addEventListener('touchstart', function (e) {
      if (BOSS.view !== 'work' || e.touches.length !== 1) { SW.x = 0; return; }
      SW.x = e.touches[0].clientX; SW.y = e.touches[0].clientY;
    }, { passive: true });
    body.addEventListener('touchend', function (e) {
      if (BOSS.view !== 'work' || !SW.x) return;
      var t = e.changedTouches && e.changedTouches[0];
      if (!t) return;
      var dx = t.clientX - SW.x, dy = t.clientY - SW.y;
      SW.x = 0;
      if (Math.abs(dx) < 70 || Math.abs(dy) > 60) return;
      step(dx < 0 ? 1 : -1);       /* 往左滑 = 下一件 */
    });
  }

  function show(on) {
    build();
    subOn('bossPage', on);
    if (on) {
      BOSS.on = true;
      hookSend();
      load();
      if (!BOSS.timer) BOSS.timer = setInterval(function () {
        if (!BOSS.on || document.hidden) return;
        hookSend();               // shim 心跳可能把 __cbSend 换回老实现，20 秒一补
        if (BOSS.view === 'list') load(true);
        else voiceArm();          // 语音键继续归这一页，别因为超时又掉回老会话框
      }, 20000);
    } else {
      BOSS.on = false;
      stopWatch();
      voiceDisarm();
      if (BOSS.timer) { clearInterval(BOSS.timer); BOSS.timer = 0; }
    }
  }

  function head(data) {
    var m = data.meta || {};
    var say = data.say || '…';
    var when = new Date();
    var hh = ('0' + when.getHours()).slice(-2) + ':' + ('0' + when.getMinutes()).slice(-2);
    if ($('bossSay')) $('bossSay').textContent = say;
    if ($('bossCnt')) $('bossCnt').textContent = (m.machine || '这台电脑') + ' · ' + hh;
    if ($('bossMach')) $('bossMach').textContent = '工头';
  }

  function card(w, cls) {
    var st = w.state || '';
    var meta = [w.agent, w.project].filter(Boolean).join(' · ');
    var sid = esc2(w.sid);
    return '<div class="bcard ' + cls + '" onclick="bossWork(\'' + sid + '\')">' +
      '<div class="bct"><span class="bdot ' + esc2(st) + '"></span><b>' + esc2(w.title) + '</b>' +
      (meta ? '<span class="bmeta">' + esc2(meta) + '</span>' : '') + '</div>' +
      '<div class="bcs">' + esc2(w.say || '') + '</div>' +
      '<div class="bwhen">' + esc2(ago(w.ts)) + '</div>' +
      '<button class="bmic" aria-label="按住说话，说给这件活"' +
        ' ontouchstart="return bossMicDown(event,\'' + sid + '\')"' +
        ' ontouchend="return bossMicUp(event)" ontouchcancel="return bossMicUp(event)"' +
        ' onmousedown="return bossMicDown(event,\'' + sid + '\')"' +
        ' onmouseup="return bossMicUp(event)"' +
        ' onclick="event.stopPropagation();return false">🎙</button>' +
      '</div>';
  }

  /* 空板只留顶栏那一句（'这会儿没活，等你派。'），不再摆三条「没有…」；
     块头也不再重复数字——句子里已经说了几件，重复三遍是噪音。 */
  function sec(title, items, cls) {
    var h = '<div class="bsec"><h3>' + title + '</h3>';
    for (var i = 0; i < items.length; i++) h += card(items[i], cls);
    return h + '</div>';
  }

  function render() {
    var d = BOSS.data || {};
    var now = d.now || [], idle = d.idle || [];
    var dec = d.decide || [], done = d.done || [];
    var h = '';
    if (now.length) h += sec('现在', now, '');
    if (dec.length) h += sec('等你定', dec, 'wait');
    if (idle.length) h += sec('等你吩咐', idle, 'idle');
    if (done.length) h += sec('已交活', done, 'done');
    if ($('bossBody')) $('bossBody').innerHTML = h;
    /* 板上的顺序就是左右滑的顺序：在板上从哪儿看，滑开就到隔壁那件。 */
    BOSS.order = now.concat(dec, idle, done).map(function (w) { return w.sid; });
    head(d);
  }

  function load(quiet) {
    if (BOSS.view !== 'list') return;
    if (BOSS.loading) return;
    BOSS.loading = true;
    if (!quiet && $('bossBody')) $('bossBody').innerHTML = '<div class="bload">正在问工头…</div>';
    xj('/api/boss/board?dev=' + encodeURIComponent(devId()), { timeout: 12000 }).then(function (r) {
      BOSS.loading = false;
      var d = (r && r.json) || {};
      if (!d.enabled) {
        if ($('bossBody')) $('bossBody').innerHTML =
          '<div class="bload">这台手机没开「工头（新）」<br>在设置里打开，或回到经典版。</div>';
        return;
      }
      BOSS.data = d;
      render();
    }).catch(function () {
      BOSS.loading = false;
      if ($('bossBody')) $('bossBody').innerHTML =
        '<div class="bload">连不上这台电脑，稍后自动重试。</div>';
    });
  }

  function detail(p) {
    if ($('bossBody')) $('bossBody').innerHTML = '<div class="bload">' + p + '</div>';
    if ($('bossFoot')) $('bossFoot').style.display = 'none';
  }

  function findWork(sid) {
    var d = BOSS.data || {};
    var all = (d.now || []).concat(d.idle || [], d.decide || [], d.done || []);
    for (var i = 0; i < all.length; i++) if (all[i].sid === sid) return all[i];
    return null;
  }

  function work() {
    var sid = BOSS.cur;
    if (!sid) return;
    BOSS.view = 'work';
    quickClose();
    startWatch();
    detail('正在翻这件活的记录…');
    xj('/api/boss/work/' + encodeURIComponent(sid) + '?dev=' + encodeURIComponent(devId()),
       { timeout: 12000 }).then(function (r) {
      var d = (r && r.json) || {};
      var w = d.work || findWork(sid) || { sid: sid, title: sid.slice(0, 8), state: '', say: '' };
      BOSS.work = { w: w, timeline: d.timeline || [], artifacts: d.artifacts || w.artifacts || [],
                    live: d.live || {} };
      renderWork();
    }).catch(function () {
      var w = findWork(sid);
      if (w) { BOSS.work = { w: w, timeline: [], artifacts: w.artifacts || [], live: {} }; renderWork(); }
      else detail('这件活已经不在板上了。');
    });
  }

  /* ── 发出去之后看得到它在跑 ───────────────────────────────────────
     以前发完就只剩结果句，正在跑的那几分钟什么都看不见。现在一件活页
     每 5 秒问一次：它刚说了什么、手上在干什么（转录尾巴，不是终端回放）。 */
  function liveHtml(w, l) {
    l = l || {};
    var st = (w || {}).state || '';
    var tool = String(l.tool || '').trim();
    var say = String(l.say || '').trim();
    if (!tool && !say) return '';
    var head = st === 'running' ? '它正在干' :
               st === 'waiting' ? '停在等你定' : '停在上一轮';
    var h = '<div class="bdfact"><div class="k">' + head + ' · ' + esc2(ago(w.ts)) + '</div>';
    if (tool) h += '<div class="bdtool">⚙ ' + esc2(tool) + '</div>';
    if (say) h += '<div class="v">' + esc2(say) + '</div>';
    return h + '</div>';
  }

  function fillLive() {
    var box = $('bossLive');
    if (!box) return;
    var x = BOSS.work || {};
    box.innerHTML = liveHtml(x.w || {}, x.live);
  }

  function refreshWork() {
    if (BOSS.view !== 'work' || !BOSS.cur || BOSS.wLoading) return;
    var sid = BOSS.cur;
    BOSS.wLoading = 1;
    xj('/api/boss/work/' + encodeURIComponent(sid) + '?dev=' + encodeURIComponent(devId()),
       { timeout: 12000 }).then(function (r) {
      BOSS.wLoading = 0;
      if (BOSS.view !== 'work' || BOSS.cur !== sid) return;
      var d = (r && r.json) || {};
      var w = d.work;
      if (!w) return;
      var old = (BOSS.work || {}).w || {};
      BOSS.work = { w: w, timeline: d.timeline || [], artifacts: d.artifacts || w.artifacts || [],
                    live: d.live || {} };
      if ((w.state || '') !== (old.state || '')) {
        /* 在跑 → 等你吩咐/交了活：整页换一次（标题、按钮都变了），
           但不许吞掉正在敲的字。 */
        var draft = ($('bossInput') || {}).value || '';
        renderWork();
        if ($('bossInput') && draft) $('bossInput').value = draft;
      } else {
        fillLive();
      }
    }).catch(function () { BOSS.wLoading = 0; });
  }

  function startWatch() {
    if (BOSS.wtimer) return;
    BOSS.wtimer = setInterval(function () {
      if (!BOSS.on || document.hidden || BOSS.view !== 'work') return;
      refreshWork();
    }, 5000);
  }

  function stopWatch() { if (BOSS.wtimer) { clearInterval(BOSS.wtimer); BOSS.wtimer = 0; } }

  function renderWork() {
    var x = BOSS.work || {};
    var w = x.w || {};
    var arts = x.artifacts || [];
    var st = w.state || '';
    var stateName = st === 'running' ? '在跑' : st === 'waiting' ? '等你定' :
                    st === 'done' ? '已交活' : st === 'idle' ? '等你吩咐' : '—';
    var h = '<div class="bdhead"><button class="bdback" onclick="bossList()">← 三块板</button>' +
            '<span class="bgap"></span>' +
            '<button class="bdback" onclick="bossStep(-1)">‹ 上一件</button>' +
            '<button class="bdback" style="margin-left:14px" onclick="bossStep(1)">下一件 ›</button></div>';
    h += '<div class="bdtitle">' + esc2(w.title || '') + '</div>';
    h += '<div class="bdsub">' + esc2([stateName, w.agent, w.project, w.machine].filter(Boolean).join(' · ')) + '</div>';
    h += '<div id="bossLive">' + liveHtml(w, x.live) + '</div>';

    /* 输入框在最上面：一进这件活就是要说一句，不用先滚到底；
       产出物沉到最下面（2026-11-22 用户：输入框放最上面，产出放最下面）。 */
    h += '<div class="bdfact" style="margin-top:12px">' +
         '<div class="k">对这件活插一句</div>' +
         '<textarea class="binput" id="bossInput" placeholder="说一句，直接发进原会话…"></textarea>' +
         '<div class="bact"><button class="pri" onclick="bossSend(\'' + esc2(w.sid) + '\')">发出去</button></div>' +
         '<div class="bhint">右下角麦克风直接按住说就行，不用先点这个框；' +
           '发出去和你在老会话里打字是同一条通道。</div></div>';

    if (st === 'waiting') {
      var ask = w.ask || {};
      var risk = (w.risk && w.risk[0]) || null;
      h += fact('确认什么', [ask.tool, ask.action].filter(Boolean).join(' · ') || '等你点头');
      if (ask.target) h += fact('对象', ask.target);
      if (risk && risk.text) h += fact('后果', risk.text, true);
      if (w.why) h += fact('为什么现在问', w.why);
      h += options(w);
    } else {
      /* 只说一遍：以前先摆 w.say（'交了活：<摘要>'）再单独摆一次摘要，同一句话两遍。
         现在有摘要就只摆摘要；没有摘要才退回它的状态句。 */
      var res = String(w.summary || '').trim();
      var line = res || String(w.say || '').trim();
      if (line) h += '<div class="bdfact"><div class="v">' + esc2(line) + '</div></div>';
      /* 大脑降级要说出来：这句是事实句，不是工头说的（还在算的不算降级）。 */
      if (!w.say_pending && w.say_src === 'facts')
        h += '<div class="bdnote">中继这会儿没答上，这句是事实句不是工头说的。</div>';
      var about = String(w.about || '').trim();
      if (about && about !== res && about !== String(w.title || '').trim())
        h += fact('你要的', about);
    }

    /* 时间线的最后一条通常就是上面那句结果，跳掉同样的句子，别把结果又抄一遍 */
    var tl = x.timeline || [];
    var seen = {};
    var rows = [];
    for (var j = tl.length - 1; j >= 0 && rows.length < 8; j--) {
      var t = tl[j] || {};
      var txt = String(t.text || t.act || '').trim();
      if (!txt || seen[txt] || txt === String(w.summary || '').trim()) continue;
      seen[txt] = 1;
      rows.unshift(t);
    }
    if (rows.length) {
      h += '<div class="bdfact"><div class="k">它都干了什么</div><div class="bdtl">';
      for (var k = 0; k < rows.length; k++) {
        var r = rows[k] || {};
        h += '<div class="t"><span class="ts">' + esc2(ago(r.ts)) + '</span>' +
             esc2(String(r.text || r.act || '').trim()) + '</div>';
      }
      h += '</div></div>';
    }

    h += '<div class="bact">';
    h += '<button class="pri" onclick="bossOpenSession(\'' + esc2(w.sid) + '\')">打开原会话</button>';
    if (st === 'done') {
      h += '<button onclick="bossPreview(\'' + esc2(w.sid) + '\')">看效果</button>';
      h += '<button onclick="bossVerdict(\'' + esc2(w.sid) + '\',\'accepted\')">收下</button>';
      h += '<button class="deny" onclick="bossReject(\'' + esc2(w.sid) + '\')">退回重做</button>';
    } else {
      h += '<button onclick="bossPreview(\'' + esc2(w.sid) + '\')">看效果</button>';
    }
    h += '</div>';

    /* 产出物放最下面：前面是「它做了什么」，这里是能点开看的东西。 */
    if (arts.length) {
      h += '<div class="bdfact"><div class="k">产出物</div><div class="barts">';
      for (var i = 0; i < arts.length; i++) {
        var a = arts[i] || {};
        var thumb = a.kind === 'image' && a.path
          ? '<img src="/api/ext_file?p=' + encodeURIComponent(a.path) + '&sid=' +
            encodeURIComponent(w.sid) + '" alt="">' : icon(a.kind);
        h += '<div class="bart" onclick="bossArtifact(\'' + esc2(w.sid) + '\',\'' +
             esc2(a.rel || a.name || '') + '\',\'' + esc2(a.kind || '') + '\',\'' +
             esc2(a.path || '') + '\')">' +
             '<span class="ico">' + thumb + '</span>' +
             '<span class="nm">' + esc2(a.name || a.rel || '') + '</span>' +
             '<span class="sz">' + size(a.size) + '</span></div>';
      }
      h += '</div></div>';
    }

    if ($('bossBody')) {
      $('bossBody').innerHTML = h;
      $('bossBody').scrollTop = 0;
    }
    if ($('bossFoot')) $('bossFoot').style.display = 'none';
    voiceArm();
  }

  function fact(k, v, warn) {
    return '<div class="bdfact"><div class="k">' + esc2(k) + '</div>' +
           '<div class="v' + (warn ? ' warn' : '') + '">' + esc2(v || '—') + '</div></div>';
  }

  function options(w) {
    var os = w.options || [];
    var choice = os.length && !(os.length === 2 && String(os[0].id) === 'allow');
    var h = '<div class="bact">';
    if (choice) {
      for (var i = 0; i < os.length; i++) {
        var o = os[i] || {};
        h += '<button class="' + (o.is_deny ? 'deny' : '') + '" onclick="bossPick(\'' +
             esc2(w.sid) + '\',\'' + esc2(w.rid) + '\',' + Number(o.id) + ',\'' +
             esc2(o.label || '') + '\')">' + esc2(o.label || ('选项 ' + o.id)) + '</button>';
      }
    } else {
      h += '<button class="pri" onclick="bossPick(\'' + esc2(w.sid) + '\',\'' +
           esc2(w.rid) + '\',null,\'\',\'allow\')">允许</button>';
      h += '<button class="deny" onclick="bossPick(\'' + esc2(w.sid) + '\',\'' +
           esc2(w.rid) + '\',null,\'\',\'deny\')">打回</button>';
    }
    /* 「去原会话看」不再放这里：下面「打开原会话」已经有了，同一页两个同义按钮 */
    h += '</div><div class="bhint">和你在电脑屏幕上点、在通知栏点是同一条审批通道；' +
         '对不上屏幕时它会拒绝代点。</div>';
    return h;
  }

  function pick(sid, rid, want, label, action) {
    var body = { sid: sid, cid: 'bp' + Date.now() + Math.random().toString(36).slice(2, 7) };
    if (rid) body.rid = rid;
    if (action) body.action = action;
    else { body.want = want; body.text = label || ''; }
    toast(action === 'deny' ? '正在打回…' : '正在处理…');
    xj('/api/ext_dlg_pick', { method: 'POST', away: true, dcto: 9000, timeout: 20000,
                              body: body }).then(function (r) {
      var d = (r && r.json) || {};
      if (d.ok) {
        toast(action === 'deny' ? '已打回' : '已处理');
        setTimeout(bossList, 400);
      } else {
        toast((d.error || '没点上，请打开原会话处理'), true, 3200);
      }
    }).catch(function () { toast('网络错误，请打开原会话处理', true, 3200); });
  }

  function reject(sid) {
    var t = $('bossInput');
    var text = t ? String(t.value || '').trim() : '';
    if (!text) {
      if (t) { t.focus(); t.placeholder = '写一句退回的原因，再点一次「退回重做」'; }
      toast('先写一句要他改什么', true);
      return;
    }
    bossSend(sid, function () {
      xj('/api/boss/verdict', { method: 'POST',
        body: { sid: sid, verdict: 'rejected', text: text, dev: devId() } }).then(function () {
        toast('退回去了');
        setTimeout(bossList, 400);
      });
    });
  }

  /* 发一句话进原会话——和你在老会话里打字是同一条通道（cid 幂等防重）。 */
  function sendText(sid, text, cb) {
    text = String(text || '').trim();
    if (!sid || !text) return false;
    xj('/api/ext_send', { method: 'POST', timeout: 20000,
                          body: { sid: sid, text: text,
                                  cid: 'bs' + Date.now() + Math.random().toString(36).slice(2, 7) }
    }).then(function (r) {
      var d = (r && r.json) || {};
      if (d.ok) { toast('发出去了'); if (cb) cb(); }
      else toast(d.error || '没发出去', true, 3000);
    }).catch(function () { toast('网络错误', true, 3000); });
    return true;
  }

  function send(sid, cb) {
    var t = $('bossInput');
    var text = t ? String(t.value || '').trim() : '';
    if (!text) { if (t) t.focus(); return; }
    var b = $('bossSendBtn');
    if (b) b.disabled = true;
    sendText(sid, text, function () {
      if (t) t.value = '';
      /* 马上给一句「等它动」，再 900ms 后补上真实动静：别发完一片死寂。 */
      var box = $('bossLive');
      if (box && BOSS.view === 'work') {
        box.innerHTML = '<div class="bdfact"><div class="k">发出去了，等它动…</div></div>';
      }
      setTimeout(refreshWork, 900);
      if (cb) cb();
    });
  }

  /* ── 板上的「按住说」──────────────────────────────────────────────
     不想说一句就得先点进一件活：卡片右下角按住麦克风，说的是这一件活；
     松手后转写落在底部速写条里，看一眼/改一改再发（语音误识别不直接动手）。
     轻点麦克风 = 只想打字，速写条直接开。 */
  var QUICK = { sid: '', on: false, rec: false, t0: 0, timer: 0 };

  function quickBar(sid) {
    var q = $('bossQuick');
    if (!q) return;
    var fresh = q.style.display === 'none' || QUICK.sid !== sid;
    QUICK.sid = sid;
    q.style.display = '';
    if (fresh) {
      var w = findWork(sid) || {};
      if ($('bossQTitle')) $('bossQTitle').textContent = '对《' + (w.title || sid.slice(0, 8)) + '》说话';
      var box = $('bossQuickInput');
      if (box) box.value = '';
    }
    if ($('bossBody')) $('bossBody').style.paddingBottom = '200px';
  }

  function quickState(t) { if ($('bossQState')) $('bossQState').textContent = t || ''; }

  function quickText(t) {
    var box = $('bossQuickInput');
    if (!box) return;
    var cur = String(box.value || '');
    box.value = cur ? cur.replace(/\s+$/, '') + '\n' + t : t;
    try { box.focus(); } catch (e) {}
    quickState('说完了，看一眼再发。');
  }

  /* 速写条的语音钩子：右下角麦克风也说给它（焦点在速写条里时老逻辑会自动处理，
     这里兜住「没焦点也能说」）。 */
  function voiceArmQuick() {
    var fn = function (t) { quickText(t); };
    fn.__boss = 1;
    window.__cbPttTarget = fn; window.__cbPttTargetAt = Date.now();
  }

  function quickClose() {
    var q = $('bossQuick');
    if (q) q.style.display = 'none';
    if ($('bossBody')) $('bossBody').style.paddingBottom = '';
    QUICK.sid = '';
    voiceDisarm();
  }

  function quickSend() {
    var box = $('bossQuickInput');
    var text = box ? String(box.value || '').trim() : '';
    if (!text) { if (box) box.focus(); return; }
    var sid = QUICK.sid;
    /* 发出去了就直接进这件活看它跑：别把人留在一块静态的板上。 */
    if (sendText(sid, text, function () {
      quickClose();
      BOSS.cur = sid;
      work();
    })) quickState('正在发…');
  }

  function micDown(ev, sid) {
    try { ev.preventDefault(); ev.stopPropagation(); } catch (e) {}
    QUICK.on = true; QUICK.rec = false; QUICK.t0 = Date.now();
    QUICK.btn = ev.currentTarget || null;
    quickBar(sid);
    quickState('按住说，松手就转写…');
    /* 220ms 才算「按住」：轻点不启动录音，只开速写条打字。 */
    clearTimeout(QUICK.timer);
    QUICK.timer = setTimeout(function () {
      QUICK.rec = true;
      try { if (QUICK.btn) QUICK.btn.classList.add('on'); } catch (e) {}
      if ($('bossQuick')) $('bossQuick').style.borderTopColor = '#c9f54e';
      quickState('正在听…');
      voiceArmQuick();
      /* 走老页面的录音入口：有壳用壳，没壳用网页录音，别自己另起一套。 */
      try {
        if (window.__ptt) window.__ptt(1);
        else if (window.CB && CB.pttDown) CB.pttDown();
      } catch (e) {}
      try { if (navigator.vibrate) navigator.vibrate(12); } catch (e) {}
    }, 220);
    return false;
  }

  function micUp(ev) {
    try { ev.stopPropagation(); } catch (e) {}
    if (!QUICK.on) return false;
    QUICK.on = false;
    clearTimeout(QUICK.timer);
    try { if (QUICK.btn) QUICK.btn.classList.remove('on'); } catch (e) {}
    if ($('bossQuick')) $('bossQuick').style.borderTopColor = '';
    var held = Date.now() - QUICK.t0;
    if (QUICK.rec) {
      QUICK.rec = false;
      try {
        if (window.__ptt) window.__ptt(0);
        else if (window.CB && CB.pttUp) CB.pttUp();
      } catch (e) {}
      quickState('正在转写…');
    } else if (held < 400) {
      quickState('写一句发过去，或者按住麦克风说。');
      var box = $('bossQuickInput');
      if (box) setTimeout(function () { try { box.focus(); } catch (e) {} }, 60);
    }
    return false;
  }

  /* 一件活看完，左右滑 / 点「上一件下一件」就到隔壁，不用退回板上再点。 */
  function step(dir) {
    var order = BOSS.order || [];
    var i = order.indexOf(BOSS.cur);
    if (i < 0 || !order.length) return;
    var j = i + dir;
    if (j < 0 || j >= order.length) {
      toast(dir > 0 ? '这就是最后一件了' : '这就是第一件了', false, 1400);
      return;
    }
    BOSS.cur = order[j];
    work();
  }

  /* 语音键（页面右下角那个麦克风）在工头页也要能用，而且不用先点输入框：
     老页面的转写结果只会交给「当前焦点框」，没焦点就塞进老会话框 #txt（在这一页看不见）。
     所以进到一件活就把它接管过来，写完再接管回去，可以接着说。 */
  function voiceArm() {
    var el = $('bossInput');
    if (!el) return;
    hookSend();
    var fn = function (t) {
      var box = $('bossInput');
      if (box) {
        var cur = String(box.value || '');
        box.value = cur ? cur.replace(/\s+$/, '') + '\n' + t : t;
        try { box.setSelectionRange(box.value.length, box.value.length); } catch (e) {}
        try { box.scrollIntoView({ block: 'center' }); } catch (e) {}
      }
      voiceArm();
    };
    fn.__boss = 1;
    window.__cbPttTarget = fn;
    window.__cbPttTargetAt = Date.now();
  }

  function voiceDisarm() {
    var t = window.__cbPttTarget;
    if (t && t.__boss) { window.__cbPttTarget = null; window.__cbPttTargetAt = 0; }
  }

  /* ── 音量下键 = 发送 ─────────────────────────────────────────────
     壳调的是 window.__cbSend，老实现只认老会话的 #txt；工头页开着时那个框藏在
     页面下面、看不见，按音量下键就把老会话里的草稿发了出去（用户实测：按了没发
     这一页的字，发的是会话里的内容）。所以工头页开着就接管：先发这一页看得见的
     字，一个字没有就明说，绝不去动老会话。shim 心跳有可能把它重装回去，每次
     voiceArm（20 秒一拍）再挂一次。 */
  function hookSend() {
    var orig = window.__cbSend;
    if (typeof orig !== 'function' || orig.__bossSend) return;
    var fn = function (byKey) {
      if (!BOSS.on) return orig.apply(window, arguments);
      var q = $('bossQuick'), qb = $('bossQuickInput');
      if (q && q.style.display !== 'none' && qb && String(qb.value || '').trim()) {
        quickSend(); return;
      }
      if (BOSS.view === 'work') {
        var t = $('bossInput');
        if (t && String(t.value || '').trim()) { send(BOSS.cur); return; }
        if (t) { try { t.focus(); } catch (e) {} }
        if (byKey) toast('先写一句再按发送', true, 1500);
        return;
      }
      if (byKey) toast('先写一句再按发送', true, 1500);
    };
    fn.__bossSend = 1;
    window.__cbSend = fn;
  }

  function preview(sid) {
    try { sheetOpen((location.origin || '') + '/pv?sid=' + encodeURIComponent(sid), '预览'); }
    catch (e) { toast('预览打不开', true); }
  }

  /* 产出物预览：网页交给 /api/ext_web（有在跑的服务就用它）；
     其余（日志/文档/图片/视频）走 /api/ext_artifact——它按会话 cwd 解析、
     按真实 mime 回源文件。/api/ext_file 只认媒体，别拿它开日志。 */
  function artifact(sid, rel, kind) {
    var base = location.origin || '';
    try {
      if (kind === 'page') {
        sheetOpen(base + '/api/ext_web?sid=' + encodeURIComponent(sid) +
                  '&path=' + encodeURIComponent(rel) + '&open=1', '效果');
      } else {
        sheetOpen(base + '/api/ext_artifact?sid=' + encodeURIComponent(sid) +
                  '&path=' + encodeURIComponent(rel) + '&_=' + Date.now(), '文件');
      }
    } catch (e) { toast('打不开这个文件', true); }
  }

  function verdict(sid, v) {
    xj('/api/boss/verdict', { method: 'POST',
      body: { sid: sid, verdict: v, dev: devId() } }).then(function (r) {
      if (r && r.json && r.json.ok) {
        toast(v === 'accepted' ? '收下了' : '退回去了');
        setTimeout(bossList, 300);
      } else toast('没记下来', true);
    }).catch(function () { toast('网络错误', true); });
  }

  function list() {
    BOSS.view = 'list';
    BOSS.work = null;
    stopWatch();
    quickClose();
    voiceDisarm();
    render();
    if (BOSS.data) head(BOSS.data);
  }

  function openSession(sid) {
    show(false);
    try { switchTo(sid); } catch (e) { goPage(0); }
  }

  // ── 对老 App 暴露的入口 ────────────────────────────────────────────
  window.bossOpen = function () {
    build();
    BOSS.view = 'list';
    show(true);
    load();
  };
  window.bossClassic = function () { show(false); try { goPage(0); } catch (e) {} };
  window.bossSessions = function () { show(false); try { goPage(0); } catch (e) {} };
  window.bossList = list;
  window.bossWork = function (sid) { BOSS.cur = sid; work(); };
  window.bossOpenSession = openSession;
  window.bossPreview = preview;
  window.bossArtifact = artifact;
  window.bossPick = pick;
  window.bossVerdict = verdict;
  window.bossReject = reject;
  window.bossSend = send;
  window.bossMicDown = micDown;
  window.bossMicUp = micUp;
  window.bossQuickSend = quickSend;
  window.bossQuickClose = quickClose;
  window.bossStep = step;

  // 入口探测已按 2026-11-25 拍板去除（UI 回经典列表）：bossOpen 等 window.* 入口
  // 保留休眠——Phase 2 复用这套界面与 /api/boss/* 端点，需要时再挂新入口。
})();
