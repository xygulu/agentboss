#!/usr/bin/env node
/* 壳说“出声归我”，可它的后台通道可能是断的：node tests/test_notify_shell_fallback.mjs

   2026-11-19 用户：小手机「语音播报、提醒音全都没有，设置里试听正常」。
   现场（Redmi，com.codeboss.terminal 0.11.24）：
       I CodebossBg: bg_push HTTP 401 cookie=24      ← 每 60 秒一次，整天
   壳的 /api/bg_push 连不上（中继只给账号登录态发 cb_bg），一条提醒也收不到；
   而页面这边 CB.announces() 只看「服务在跑 + 开关开着」就判定“壳在管”，
   自己一声不响地 return —— 两边都以为是对方，结果谁都不响、连通知都没有。

   规则（本测试钉住）：
   1. 壳在管、也确实认领了这条（bgAnnounced >= id）→ 页面不出声（老行为不变）。
   2. 壳说在管、但 4 秒后水位还没到这条 → 页面自己补：提示音、语音、系统通知，
      并且先 CB.seenNotify，壳晚点重连补发时不再重响。
   3. 没有壳（浏览器）→ 立刻走页面自己那一套。
*/
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import assert from "node:assert";

const here = dirname(fileURLToPath(import.meta.url));
const src = readFileSync(join(here, "..", "host", "static", "src", "js", "04_net.js"), "utf8");

const mOn = src.match(/function onNotify\(d\)\{[\s\S]*?\n\}/);
assert(mOn, "04_net.js 里找不到 onNotify");
const mPa = src.match(/function pageAnnounce\(d[^)]*\)\{[\s\S]*?\n\}/);
assert(mPa, "04_net.js 里找不到 pageAnnounce（壳没认领时页面自己补的那一套）");
const mShell = mOn[0].match(/var shellAnn=([\s\S]*?);\n/);
assert(mShell, "onNotify 里找不到 shellAnn 判定");

const CODE = [mOn[0], mPa[0], "return {onNotify,pageAnnounce};"].join("\n");

function boot(bridge, opt) {
  opt = opt || {};
  const calls = { beep: [], toast: [], say: [], notify: [], seen: [], cbdbg: [] };
  const timers = [];
  const G = {
    CB: bridge,
    SID: "s1", pageIdx: (opt.pageIdx === undefined ? 1 : opt.pageIdx),
    NOTIFY: {}, NOTIF_SEEN: {}, NOTIF_MAXID: 0,
    PUSH: { seq: 9 },
    lastExt: [], lastWeb: [],
    VIS_AT: 0, HID_AT: 0,
    pendNote: function () {},
    nwLoad: function () { return { last: 0, at: 0 }; },
    nwBump: function () {},
    smartOn: function () { return true; },
    notifyCfg: function () { return { text: true, sound: true, voice: true }; },
    dndActive: function () { return false; },
    muteOn: function () { return false; },
    hiddenNow: function () { return false; },
    bgRemindOn: function () { return true; },
    renderList: function () {},
    nbLabel: function () { return "完成"; },
    xj: function () { return Promise.resolve(); },
    toast: function (s) { calls.toast.push(s); },
    beep: function (k) { calls.beep.push(k); },
    speakKind: function (k, a, say) { calls.say.push({ k: k, say: say }); },
    setTimeout: function (fn, ms) { timers.push({ fn: fn, ms: ms }); },
    Date: Date, JSON: JSON, Object: Object, String: String, Number: Number,
    Math: Math, isFinite: isFinite, console: console,
  };
  G.window = { CB: bridge };
  G.window.__cbDbg = function (s) { calls.cbdbg.push(s); };
  G.document = { cookie: "" };
  const mod = new Function("G", "with(G){" + CODE + "}")(G);
  return { G: G, mod: mod, calls: calls, timers: timers, flush: function (ms) {
    const due = timers.filter(function (t) { return t.ms <= (ms === undefined ? 4000 : ms); });
    due.forEach(function (t) { timers.splice(timers.indexOf(t), 1); t.fn(); });
  } };
}

const d = { id: 7, ts: Date.now() / 1000, sid: "s1", kind: "done", text: "跑完了",
            title: "agentboss", voice: true, say: "abcdef" };

/* 1) 壳在管、也认领了 → 页面一声不响 */
{
  const env = boot({ announces: function () { return true; }, bgAnnounced: function () { return 7; },
                     seenNotify: function (id) { env.calls.seen.push(id); },
                     notify2: function () { env.calls.notify.push(1); } });
  env.mod.onNotify(d);
  env.flush();
  assert.deepEqual(env.calls.beep, [], "壳收下了，页面不该再响");
  assert.deepEqual(env.calls.notify, [], "壳收下了，页面不该再挂通知");
  assert.deepEqual(env.calls.seen, [], "壳在管时不报 seenNotify（报了壳会跳过）");
}

/* 2) 壳说在管、但通道断了（水位一直不动）→ 页面 4 秒后自己补 */
{
  const env = boot({ announces: function () { return true; }, bgAnnounced: function () { return 3; },
                     seenNotify: function (id) { env.calls.seen.push(id); },
                     notify2: function () { env.calls.notify.push(1); } });
  env.mod.onNotify(d);
  assert.deepEqual(env.calls.beep, [], "4 秒内别抢壳的活");
  env.flush(4000);
  assert.equal(env.calls.beep.length + env.calls.say.length > 0, true, "壳没认领：页面得自己响");
  assert.deepEqual(env.calls.seen, [7], "补完要告诉壳，晚点重连别再补一遍");
}

/* 2b) 同上，但人在列表页（不是正在看这个会话）：系统通知也得补上 */
{
  const env = boot({ announces: function () { return true; }, bgAnnounced: function () { return 3; },
                     seenNotify: function (id) { env.calls.seen.push(id); },
                     notify2: function () { env.calls.notify.push(1); } }, { pageIdx: 0 });
  env.mod.onNotify(d);
  assert.deepEqual(env.calls.notify, [], "4 秒内别抢壳的活");
  env.flush(4000);
  assert.deepEqual(env.calls.notify, [1], "壳没认领：通知也得补上");
  assert.equal(env.calls.beep.length, 1, "提示音照响");
}

/* 3) 没有壳（浏览器）→ 立刻自己响 */
{
  const env = boot({}, {});
  env.mod.onNotify(d);
  assert.equal(env.calls.beep.length, 1, "没壳：立刻响");
}

/* 4) 壳没有 bgAnnounced 这个接口（老壳）→ 不许因此变哑 */
{
  const env = boot({ announces: function () { return true; } });
  env.mod.onNotify(d);
  env.flush(4000);
  assert.equal(env.calls.beep.length, 1, "老壳报不了水位：页面自己响");
}

console.log("test_notify_shell_fallback: ok");
