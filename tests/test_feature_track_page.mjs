#!/usr/bin/env node
/* 页面埋点的「页面维 + 停留时长」（2026-11-21 用户）：每条事件带「当时在哪个
   页面、点了哪一项」；页面停留用 ev=visit（进入一次）+ ev=dwell（n=秒）记，
   秒数只在页面可见时累计。
   跑法： node tests/test_feature_track_page.mjs */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import assert from "node:assert";

const here = dirname(fileURLToPath(import.meta.url));
const src = readFileSync(join(here, "..", "host", "static", "src", "js", "11_track.js"), "utf8");

function boot() {
  const posts = [], timers = [];
  const doc = { hidden: false, addEventListener: () => {}, getElementById: () => null };
  const api = new Function(
    "navigator", "fetch", "setInterval", "document", "Blob",
    src + "\n;return {track:track,trackPage:trackPage,trackFlush:trackFlush," +
          "q:function(){return TRK_Q;},sec:function(){return TRK_SEC;}};")(
    { sendBeacon: (u, b) => { posts.push({ u, b }); return true; } },
    () => Promise.resolve({ ok: true }),
    (f, t) => timers.push([f, t]), doc, Blob);
  const beat = timers.find(x => x[1] === 1000);
  return { api, posts, timers, doc, beat: () => beat[0]() };
}

async function body(p) { return JSON.parse(await p.b.text()); }
const last = async t => body(t.posts[t.posts.length - 1]);

/* ── 事件带页面维：条目是 [事件, 明细, 页面, 次数] ── */
{
  const t = boot();
  assert.deepEqual((await body(t.posts[0])).e[0], ["visit", "", "home", 1],
                   "打开页面先进 home 一次");
  t.api.track("home_tap", "会话");
  t.api.trackFlush();
  assert.deepEqual((await last(t)).e[0], ["home_tap", "会话", "home", 1]);
  for (let i = 0; i < 30; i++) t.beat();
  assert.equal(t.api.sec(), 30);
  /* 明细只收界面上的短标签，太长的截到 24 字（服务端也这么卡） */
  t.api.track("set_tap", "一".repeat(40));
  t.api.trackFlush();
  assert.equal((await last(t)).e[0][1], "一".repeat(24));
}

/* ── 切页：上一页结一笔 dwell（秒数），新页记一次 visit，立刻发 ── */
{
  const t = boot();
  const n0 = t.posts.length;
  for (let i = 0; i < 12; i++) t.beat();
  t.api.trackPage("sess");
  assert.equal(t.posts.length, n0 + 1, "切页立刻发，不等 20 秒");
  assert.deepEqual((await last(t)).e,
                   [["dwell", "", "home", 12], ["visit", "", "sess", 1]]);
  t.api.track("key", "enter");       // 之后的点落在新页面
  t.api.trackFlush();
  assert.deepEqual((await last(t)).e[0], ["key", "enter", "sess", 1]);
}

/* ── 同一页重复进不算新访问；没停够一秒不记 dwell ── */
{
  const t = boot();
  const n0 = t.posts.length;
  t.api.trackPage("home");
  assert.equal(t.posts.length, n0, "还在 home：不发");
  t.api.trackPage("set");
  assert.equal(t.posts.length, n0 + 1);
  assert.deepEqual((await last(t)).e, [["visit", "", "set", 1]],
                   "零秒停留不记 dwell");
}

/* ── 后台不算停留：页面藏起来时心跳不累加 ── */
{
  const t = boot();
  t.doc.hidden = true;
  for (let i = 0; i < 10; i++) t.beat();
  assert.equal(t.api.sec(), 0, "看不见的页面不算停留");
  t.doc.hidden = false;
  for (let i = 0; i < 3; i++) t.beat();
  assert.equal(t.api.sec(), 3);
}

console.log("feature track page ok");
