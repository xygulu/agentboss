// SPDX-License-Identifier: AGPL-3.0-or-later

/* 用时浮标的读秒自检：直接从 static/ext.html 抠出真实函数跑，不另写一份。
   跑法：node tools/test_use_float.js [ext.html]
   盯的是「说完一句话，浮标到底会不会走字」这条真机上反复出问题的链路：
     ① 安卓壳原生录音 → 回调是整段 JSON
     ② 没有壳/壳没有原生录音 → 页面自己上传，回调是纯文本（以前这条漏了记账）
     ③ 识别失败 → 一分一秒都不许扣
     ④ 服务器钟正好卡在计费格边界（phase=0）→ 以前锚点条件永远不成立，浮标冻住
     ⑤ 壳的 shim.js 抢走回调
     ⑥ 对账请求跨中继来回两秒、又卡在格边界
     ⑦ 中继自己接的 POST（rtc_offer 等）不记账 → 页面也不许读秒
     ⑧ 万一误判记了一笔账，一分钟后对账要把显示追回服务器数（以前只许往下修，永久偏低）
 */
const fs = require('fs');
const path = require('path');

const SRC = fs.readFileSync(process.argv[2] ||
  path.join(__dirname, '..', 'static', 'ext.html'), 'utf8');

function extract(name) {
  const at = SRC.indexOf('function ' + name + '(');
  if (at < 0) throw new Error('missing function ' + name);
  const i = SRC.indexOf('{', at);
  let depth = 0;
  for (let j = i; j < SRC.length; j++) {
    if (SRC[j] === '{') depth++;
    else if (SRC[j] === '}' && !--depth) return SRC.slice(at, j + 1);
  }
  throw new Error('unbalanced ' + name);
}

/* var NAME=[...]; —— 数组字面量，按方括号配平截取 */
function extractVar(name) {
  const at = SRC.indexOf('var ' + name + '=');
  if (at < 0) throw new Error('missing var ' + name);
  const i = SRC.indexOf('[', at);
  let depth = 0;
  for (let j = i; j < SRC.length; j++) {
    if (SRC[j] === '[') depth++;
    else if (SRC[j] === ']' && !--depth) return SRC.slice(at, j + 1) + ';';
  }
  throw new Error('unbalanced var ' + name);
}

const NAMES = ['useActive', 'usePackSec', 'useDurSec', 'useDurM', 'useDurC',
  'useDurTick', 'useUnit', 'useSrvPhase', 'useBillPTT', 'useTickStart', 'usePaint', 'planRefresh',
  'hookPttResult', 'billWant'];
const code = extractVar('BILL_SKIP') + '\n' + NAMES.map(extract).join('\n');
const build = what => new Function('sb', 'with(sb){' + code + '\nreturn ' + what + ';}');

/* 一套最小现场：DOM 占位、假钟、假的 /api/plan。
   srvUsed = 服务器账上的「今天已用秒」（假 xj 回它）；账没动就传不涨的数。 */
function mkEnv(opts) {
  opts = opts || {};
  const clk = { t: 1700000000123 };
  /* 服务器钟：中继/开发机给的 srv_ts（秒）。clientPhase=0 就是「正好卡在格边界」 */
  const srv = 1700000000 + (opts.clientPhase == null ? 3.4 : opts.clientPhase);
  const els = {};
  ['useFloat', 'ufV', 'ufP'].forEach(id => els[id] =
    { id, style: {}, textContent: '', title: '', className: '' });
  const sb = {
    USEST: { ok: 1, plan: 'pro', used: 200, cap: 30420, daily: 30220, credit: 0,
             phase: 0, tk: 0, at: 0, el: 0, fb: 0, hold: -1, totSec: 30220, unit: 10,
             live: 0, froze: 0 },
    LASTOK: clk.t - 60000, LASTBILL: 0, PLAND: { ok: true, plan: 'pro' }, pageIdx: 1,
    hiddenNow: () => false, Date: { now: () => clk.t },
    Math, Number, JSON, isNaN,
    document: { getElementById: id => els[id] || null },
    toast: () => {}, quotaSheet: () => {}, tierPaint: () => {}, keepHintSync: () => {},
    applyPlanUI: () => {}, vsPlanName: () => 'pro', vsPayLabel: () => '',
    DIRECT: {}, directOn: () => false, directTry: () => {}, QUOTA_WALL: 0,
    xj: () => {
      /* 跨中继一个来回：请求发出去到响应回来过了 slowPlan 毫秒（手机上是 1~3 秒） */
      if (opts.slowPlan) clk.t += opts.slowPlan;
      return Promise.resolve({ status: 200, hdr: () => null, json: {
        ok: true, plan: 'pro', limits: { use_min_day: 507 }, srv_ts: srv,
        usage: { use_used_sec: opts.srvUsed == null ? 210 : opts.srvUsed,
                 use_cap_sec: 30420, use_credit_sec: 0, use_unit: 10 },
      } });
    },
  };
  return { sb, els, clk };
}

function run(scenario, clientPhase, viaShim, slowPlan) {
  const { sb, els, clk } = mkEnv({ clientPhase, slowPlan });
  let shim = null;
  if (viaShim) {
    /* 真机现场：页面先挂了自己的回调（自己记账），壳的 shim.js 心跳注进来
       把它换掉（只填框、不记账）。没有 hookPttResult 的话浮标从此不动。 */
    const win = {};
    win.__pttResult = function(){ win.pageCalled = (win.pageCalled || 0) + 1; };
    win.__pttResult.__cbBills = 1;
    build('hookPttResult')(sb)(win);
    win.__pttResult = function(){ win.shimCalled = (win.shimCalled || 0) + 1; };
    win.__pttResult.__cbMine = 1;
    win.__pttResult(scenario);                 // 壳把转写结果递进来
    shim = { mine: win.__pttResult.__cbMine, called: win.shimCalled || 0,
             page: win.pageCalled || 0 };
  } else {
    build('useBillPTT')(sb)(scenario);         // 模拟转写回调
  }
  return new Promise(r => setTimeout(r, 30)).then(() => {
    const paint = build('usePaint');
    clk.t += 1000; paint(sb)();                // 1 秒后：「记账 ✓」还亮着
    const mark = els.ufP.style.display === '' ? els.ufP.textContent : '(隐藏)';
    clk.t += 3000; paint(sb)();                // 4 秒后：正常读秒
    const t1 = els.ufV.textContent;
    clk.t += 5000; paint(sb)();                // 再过 5 秒：必须少 5 秒
    const t2 = els.ufV.textContent;
    const paused = /pause/.test(els.useFloat.className);
    clk.t += 2000; paint(sb)();                // 记账后满一格（10 秒）：读秒必须停
    return { tk: sb.USEST.tk, used: sb.USEST.used, t1, t2,
             t3: els.ufV.textContent, paused11: /pause/.test(els.useFloat.className),
             mark, paused, shim };
  });
}

/* ⑦ 中继在自己这里处理、根本不记账的 POST —— 页面也不许读秒 */
function billWantCase() {
  const { sb } = mkEnv();
  const bw = build('billWant')(sb);
  const cases = [
    ['/api/rtc_offer', { method: 'POST' }, false],
    ['/api/ptt_fix', { method: 'POST' }, false],
    ['/api/ptt', { method: 'POST' }, true],          // 语音是实打实的远程使用
    ['/api/ext_send', { method: 'POST' }, true],     // 发消息
    ['/api/ext_key', { method: 'POST' }, true],      // 按键
    ['/api/ext_snap/x', { method: 'POST' }, false],
    ['/api/plan?a=0', { method: 'GET' }, false],
  ];
  return cases.map(([ep, opt, want]) => {
    const got = bw(ep, opt);
    return { ep, want, got, ok: got === want };
  });
}

/* ⑧ 误判记账（开屏 rtc_offer）：读满一格后显示偏低 10 秒；
   一分钟内不许乱跳，一分钟后的对账必须追回服务器数。 */
async function healCase() {
  const { sb, els, clk } = mkEnv({ srvUsed: 200 });   // 账上一分没动
  const refresh = build('planRefresh'), paint = build('usePaint');
  const tick = build('useTickStart');
  const wait = () => new Promise(r => setTimeout(r, 20));
  const truth = 30220;                                 // 30420 - 200
  refresh(sb)(); await wait();
  const h0 = sb.USEST.hold;                            // 首次对账 = 服务器数
  clk.t += 1000;
  sb.LASTBILL = clk.t;                                 // 页面把 rtc_offer 当成一笔账
  tick(sb)();
  clk.t += 5000; paint(sb)();
  const low = sb.USEST.hold;                           // 读到一半：比账上低 5 秒
  clk.t += 5000; paint(sb)();                          // 读满一格：停在这个偏低的数上
  clk.t += 30000; refresh(sb)(); await wait();
  const mid = sb.USEST.hold;                           // 一分钟内：保持不动
  clk.t += 30000; refresh(sb)(); await wait();
  const back = sb.USEST.hold;                          // 超过一分钟：追回服务器数
  return { h0, low, held: truth - 5, mid, back, truth, v: els.ufV.textContent,
           ok: h0 === truth && low === truth - 5 && mid === truth - 5 &&
               back === truth };
}

(async () => {
  let bad = 0;
  for (const c of billWantCase()) {
    console.log((c.ok ? 'PASS ' : 'FAIL ') + '豁免口径 ' + c.ep +
      ' → ' + c.got + (c.ok ? '' : '（应为 ' + c.want + '）'));
    if (!c.ok) bad++;
  }

  const good = [
    ['壳的 JSON', JSON.stringify({ ok: true, text: '你好', quota: { used_s: 210 } })],
    ['页面的纯文本', '你好'],
    ['格边界 phase=0', JSON.stringify({ ok: true, text: '你好' }), 0],
  ];
  for (const [name, payload, ph] of good) {
    const r = await run(payload, ph);
    const ok = r.tk && r.t1 !== r.t2 && !r.paused && r.mark === '记账 ✓' &&
      r.used === 210 && r.t2 !== r.t1 && r.paused11;
    console.log((ok ? 'PASS ' : 'FAIL ') + name, JSON.stringify(r));
    if (!ok) bad++;
  }
  const f = await run(JSON.stringify({ ok: false, error: '识别失败' }));
  const fok = !f.tk && f.used === 200 && f.mark === '(隐藏)';
  console.log((fok ? 'PASS ' : 'FAIL ') + '识别失败不记账', JSON.stringify(f));
  if (!fok) bad++;

  /* 第 ⑤ 条：壳的 shim.js 抢走 __pttResult（真机上字进得去、浮标就是不动） */
  const s = await run(JSON.stringify({ ok: true, text: '你好', quota: { used_s: 210 } }), null, true);
  const sok = s.tk && s.t1 !== s.t2 && !s.paused && s.mark === '记账 ✓' && s.used === 210 &&
    s.shim && s.shim.mine === 1 && s.shim.called === 1;
  console.log((sok ? 'PASS ' : 'FAIL ') + 'shim 抢走回调也要记账', JSON.stringify(s));
  if (!sok) bad++;
  const sf = await run(JSON.stringify({ ok: false, error: '识别失败' }), null, true);
  const sfok = !sf.tk && sf.used === 200 && sf.shim && sf.shim.called === 1;
  console.log((sfok ? 'PASS ' : 'FAIL ') + 'shim 回识别失败不记账', JSON.stringify(sf));
  if (!sfok) bad++;

  /* 第 ⑥ 条：对账请求跨中继来回 2 秒、又正好卡在格边界 —— 比「响应到达时刻」就永远不成立 */
  const slow = await run(JSON.stringify({ ok: true, text: '你好' }), 0, false, 2000);
  const slowok = slow.tk && slow.t1 !== slow.t2;
  console.log((slowok ? 'PASS ' : 'FAIL ') + '对账来回 2 秒卡格边界也读秒', JSON.stringify(slow));
  if (!slowok) bad++;

  /* 第 ⑧ 条：误判的账不许永久压低显示 */
  const h = await healCase();
  console.log((h.ok ? 'PASS ' : 'FAIL ') + '误判记账一分钟后追平服务器', JSON.stringify(h));
  if (!h.ok) bad++;

  console.log(bad ? bad + ' 项不合格' : '✓ 全部通过');
  process.exit(bad ? 1 : 0);
})();
