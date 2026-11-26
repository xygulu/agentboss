// SPDX-License-Identifier: AGPL-3.0-or-later

/* 功能使用埋点（2026-11-21）：每个功能入口打一个点，中继按「天×页面×明细×人」
   聚合成计数，后台「分析」页看页面停留/排行/趋势。
   每条 = [事件, 明细, 页面, 次数]：
     明细 k = 点了界面上的哪一项/切到哪个值（按钮名，中文可以）；
     页面 pg = 当时在哪个页面（home/sess/term/set/files/test/deploy），自动带上。
   页面停留：切进一个页面记 ev=visit，离开时把这页停留的秒数记成 ev=dwell（n=秒）。
   只记按钮名，不记聊天内容、会话标题、文件路径；登录与否都记（匿名按 cb_cid 认）。
   点只攒不发：攒满 40 条、每 20 秒、退到后台时各发一次；发不出去就丢。 */
/* 这个文件被拼在整个页面脚本的最后，但前面的块在初始化时就会调 track()，
   那时下面这行还没执行（var 只提升声明）。缓冲必须按需创建，否则
   undefined.push 抛错会把后面的初始化整块打断（2026-11-21 线上事故）。 */
var TRK_Q, TRK_MAX = 40, TRK_PG = '', TRK_SEC = 0;

/* ── 页面维 ────────────────────────────────────────────────────────
   子页是全屏 .subpage（带 .on 就是开着），没有子页时看左滑格子在列表还是会话页。
   多个子页同时开（文件页压在会话页上）时取最先命中的那层。 */
var TRK_SUB = [['depPage', 'deploy'], ['filePage', 'files'], ['testPage', 'test'],
               ['setPage', 'set'], ['termPage', 'term']];
function trkPageNow() {
  if (typeof document === 'undefined' || !document.getElementById) return '';
  for (var i = 0; i < TRK_SUB.length; i++) {
    var p = document.getElementById(TRK_SUB[i][0]);
    if (p && p.classList && p.classList.contains('on')) return TRK_SUB[i][1];
  }
  return (typeof pageIdx !== 'undefined' && pageIdx === 1) ? 'sess' : 'home';
}
function track(ev, k, n) {
  if (!ev) return;
  if (!TRK_Q) TRK_Q = [];
  var kk = (k === undefined || k === null) ? '' : String(k).replace(/[\r\n\t]/g, ' ').trim();
  TRK_Q.push([String(ev), kk.slice(0, 24), TRK_PG || '', n || 1]);
  if (TRK_Q.length >= TRK_MAX) trackFlush();
}
/* 把当前这一页攒的秒数结成一笔 dwell，清零（切页/退后台时用） */
function trackDwell() {
  if (TRK_PG && TRK_SEC > 0) {
    if (!TRK_Q) TRK_Q = [];
    TRK_Q.push(['dwell', '', TRK_PG, TRK_SEC]);
    TRK_SEC = 0;
  }
}
/* 切页：离开的页面结 dwell，新页面记一次 visit；立刻发（顺序有意义） */
function trackPage(pg) {
  pg = pg || '';
  if (pg === TRK_PG) return;
  trackDwell();
  TRK_PG = pg;
  TRK_SEC = 0;
  if (pg) {
    if (!TRK_Q) TRK_Q = [];
    TRK_Q.push(['visit', '', pg, 1]);
    trackFlush();
  }
}
function trackFlush() {
  if (!TRK_Q || !TRK_Q.length) return;
  var e = TRK_Q; TRK_Q = [];
  var body;
  try { body = JSON.stringify({ e: e }); } catch (_) { return; }
  try {
    if (navigator.sendBeacon) {
      navigator.sendBeacon('/api/track', new Blob([body], { type: 'application/json' }));
    } else {
      fetch('/api/track', { method: 'POST', body: body,
        headers: { 'Content-Type': 'application/json' }, keepalive: true });
    }
  } catch (_) { /* 埋点不许挡住功能 */ }
}
/* 停留心跳：页面可见才累加（后台/息屏不算停留） */
setInterval(function () {
  if (typeof document !== 'undefined' && !document.hidden && TRK_PG) TRK_SEC++;
}, 1000);
setInterval(function () { trackDwell(); trackFlush(); }, 20000);
document.addEventListener('visibilitychange', function () {
  if (document.hidden) { trackDwell(); trackFlush(); }
});

/* ── 点在哪一项 ────────────────────────────────────────────────────
   只取「界面上的名字」：按钮自己的字、所在行的标题（.tl）、图标按钮的 title。
   会话卡片/收件箱里的东西是用户内容（会话标题），一律不取。 */
var TRK_LBL = { devAddBtn: '接入开发机', devEditOpenBtn: '改开发机', ltIb: '收件箱',
  ltSess: '会话', bindCard: '绑定开发机', modelCard: '当前模型',
  newSessCard: '新建会话', termEntryCard: '终端', termNewBtn: '新开终端',
  ltIbN: '收件箱' };
function trkLabel(el) {
  var btn = el.closest ? el.closest('button') : null;
  var bt = (btn && btn.textContent) ? btn.textContent.trim().replace(/\s+/g, '') : '';
  for (var n = el; n && n.nodeType === 1 && n !== document.body; n = n.parentNode) {
    if (n.id && TRK_LBL[n.id]) return TRK_LBL[n.id];
    if (n.tagName === 'BUTTON' && n.title) return n.title;
    if (n.classList && n.classList.contains('tr')) {
      var tl = n.querySelector('.tl');
      var t = tl ? tl.textContent.trim() : '';
      if (t && bt && bt !== t) return t + '·' + bt;
      return t || bt;
    }
  }
  return bt;
}
function trkTap(scopeId, ev) {
  if (typeof document === 'undefined' || !document.getElementById) return;
  var s = document.getElementById(scopeId);
  if (!s || !s.addEventListener) return;
  s.addEventListener('click', function (e) {
    var t = e.target;
    if (!t || t.nodeType !== 1) return;
    /* 输入框、会话列表、收件箱：字是用户写的，不进报表 */
    if (t.closest && t.closest('input,textarea,select,#list,#ibPane,.nhItem,.fbItem')) return;
    var k = trkLabel(t);
    if (k) track(ev, k);
  });
}
trkTap('pgList', 'home_tap');
trkTap('pgCtrl', 'sess_tap');
trkTap('setPage', 'set_tap');
trkTap('termPage', 'term_tap');

trackPage(trkPageNow());
track('page_open');
trackFlush();   /* 打开页面这一下立刻发：退后台/被杀之前至少这次记录了 */
