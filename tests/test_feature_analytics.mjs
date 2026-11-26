#!/usr/bin/env node
/* 页面埋点缓冲（2026-11-21）：跟踪点只攒不发，攒满/定时/退后台才发；
   发不出去就丢——埋点绝不能挡功能。
   跑法： node tests/test_feature_analytics.mjs */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import assert from "node:assert";

const here = dirname(fileURLToPath(import.meta.url));
const src = readFileSync(join(here, "..", "host", "static", "src", "js", "11_track.js"), "utf8");

function boot(opt) {
  opt = opt || {};
  const posts = [], fetches = [], timers = [];
  const navigator = opt.beacon ? {
    sendBeacon: (u, b) => { posts.push({ u, b }); return true; }
  } : {};
  const fetch = (...a) => { fetches.push(a); return Promise.resolve({ ok: true }); };
  const document = { hidden: false, addEventListener: () => {},
                     getElementById: () => null };
  const fn = new Function(
    "navigator", "fetch", "setInterval", "document", "Blob",
    src + "\n;return {track:track,trackFlush:trackFlush,q:function(){return TRK_Q;}};");
  const api = fn(navigator, fetch, (f, t) => timers.push([f, t]), document, Blob);
  return { api, posts, fetches, timers };
}

async function body(p) { return JSON.parse(await p.b.text()); }

/* ── 早调用：页面是几十个块拼成的一段脚本，前面的块在初始化时就会调 track
   （setView('live') → track('view_switch')），那时本文件的 var TRK_Q=[] 还没
   执行。2026-11-21 线上就是这么炸的：undefined.push 抛错，把后面整个初始化
   块打断，会话列表点了没反应。早调用必须不死，也不许丢。 ── */
{
  const posts = [];
  const navigator = { sendBeacon: (u, b) => { posts.push({ u, b }); return true; } };
  const fn = new Function(
    "navigator", "fetch", "setInterval", "document", "Blob",
    "track('early');\n" + src + "\n;return {q:function(){return TRK_Q;}};");
  const api = fn(navigator, () => Promise.resolve({ ok: true }),
                 () => {}, { hidden: false, addEventListener: () => {} }, Blob);
  /* 这个假 document 没有页面结构，页面维留空——但事件一条都不能丢 */
  assert.equal(posts.length, 1, "早调用之后 page_open 仍然立刻发出去");
  assert.deepEqual(await body(posts[0]),
                   { e: [["early", "", "", 1], ["page_open", "", "", 1]] },
                   "早调用那条不能丢");
  assert.equal(api.q().length, 0);
}

/* ── 打开页面自动记一条 ── */
let t = boot({ beacon: true });
assert.equal(t.posts.length, 2, "加载就要把 visit/page_open 发出去");
assert.equal(t.posts[0].u, "/api/track");
assert.deepEqual(await body(t.posts[0]), { e: [["visit", "", "home", 1]] });
assert.deepEqual(await body(t.posts[1]), { e: [["page_open", "", "home", 1]] });

/* ── 攒批：两次点击合成一个请求 ── */
t.api.track("send_msg");
t.api.track("key", "enter");
assert.equal(t.posts.length, 2, "没攒满不乱发");
t.api.trackFlush();
assert.equal(t.posts.length, 3);
assert.deepEqual(await body(t.posts[2]),
                 { e: [["send_msg", "", "home", 1], ["key", "enter", "home", 1]] });
assert.equal(t.api.q().length, 0, "发完清空");

/* ── 空队列不发请求 ── */
t.api.trackFlush();
assert.equal(t.posts.length, 3);

/* ── 攒满 40 条自动发，别等到定时器 ── */
t = boot({ beacon: true });
const n0 = t.posts.length;
for (let i = 0; i < 40; i++) t.api.track("key");
assert.equal(t.posts.length, n0 + 1, "满一批立刻发（page_open 之后再来一批）");
assert.equal(t.api.q().length, 0);

/* ── 没有 sendBeacon 时用 fetch 兜底，且 keepalive ── */
t = boot({});
assert.equal(t.fetches.length, 2, "兜底走 fetch（visit 一发、page_open 一发）");
const [u, opt] = t.fetches[1];
assert.equal(u, "/api/track");
assert.equal(opt.method, "POST");
assert.equal(opt.keepalive, true, "退后台期间的最后一发不能因为页面没了被取消");
assert.deepEqual(JSON.parse(opt.body).e[0], ["page_open", "", "home", 1]);

/* ── 定时器：1 秒停留心跳 + 20 秒一发 ── */
assert.deepEqual(t.timers.map(x => x[1]).sort((a, b) => a - b), [1000, 20000]);

console.log("feature analytics ok");
