#!/usr/bin/env node
/* 页面把长期设备凭据交给壳：node tests/test_bg_token_page.mjs

   2026-11-19 S24 mini：壳的 Java 端读不到 WebView 的 cookie，黑屏时页面又被冻结，
   没人能给壳换 10 分钟的 dt。所以页面用登录态向中继要一把 90 天的设备凭据
   （/api/bg_ticket），交给壳（CB.bgToken）。

   规则（本测试钉住）：
   1. 缓存里是同一条设备、还没到期的票 → 直接交给壳，不再请求。
   2. 没缓存 → 取一次，存下来再交给壳。
   3. 缓存是别的设备的票（切过机器）→ 重新取，别把 A 机的票交给 B 机。
   4. 取不到（没登录/开发机不在线）→ 什么都不做，壳继续用旧票或裸请求。
*/
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import assert from "node:assert";

const here = dirname(fileURLToPath(import.meta.url));
const src = readFileSync(join(here, "..", "host", "static", "src", "js", "04_net.js"), "utf8");
const m = src.match(/function bgTokSync\(\)\{[\s\S]*?\n\}/);
const mDev = src.match(/function bgTokDev\(\)\{[\s\S]*?\n\}/);
assert(m, "04_net.js 里找不到 bgTokSync");
assert(mDev, "04_net.js 里找不到 bgTokDev");

const NOW = Date.now() / 1000;

function boot(opt) {
  opt = opt || {};
  const calls = { tok: [], fetch: [] };
  const store = {};
  if (opt.cache) store.cb_bgtok = JSON.stringify(opt.cache);
  const G = {
    window: { CB: opt.cb === null ? null : { bgToken: function (t) { calls.tok.push(t); } } },
    CB: null,
    localStorage: {
      getItem: function (k) { return k in store ? store[k] : null; },
      setItem: function (k, v) { store[k] = String(v); },
    },
    document: { cookie: opt.cookie === undefined ? "cb_dev=D1" : opt.cookie },
    xj: function (ep) {
      calls.fetch.push(ep);
      return Promise.resolve(opt.resp === undefined
        ? { ok: true, json: { ok: true, tok: "NEWTOK", exp: NOW + 90 * 86400 } }
        : opt.resp);
    },
    Date: Date, JSON: JSON, String: String,
  };
  G.CB = G.window.CB;
  const fn = new Function("G", "with(G){" + mDev[0] + "\n" + m[0] + "\nreturn bgTokSync;}")(G);
  return { G: G, store: store, calls: calls, sync: fn };
}

/* 1) 同一条设备、没到期 → 直接用，不请求 */
{
  const env = boot({ cache: { dev: "D1", tok: "CACHED", exp: NOW + 30 * 86400 } });
  env.sync();
  assert.deepEqual(env.calls.tok, ["CACHED"]);
  assert.deepEqual(env.calls.fetch, [], "票还新鲜就别再要");
}

/* 1b) 快到期（<7 天）→ 换新的 */
{
  const env = boot({ cache: { dev: "D1", tok: "OLD", exp: NOW + 3 * 86400 } });
  env.sync();
  await Promise.resolve(); await Promise.resolve();
  assert.deepEqual(env.calls.fetch, ["/api/bg_ticket"]);
  assert.deepEqual(env.calls.tok, ["NEWTOK"]);
}

/* 2) 没缓存 → 取一次、存下来、交给壳 */
{
  const env = boot({});
  env.sync();
  await Promise.resolve(); await Promise.resolve();
  assert.deepEqual(env.calls.fetch, ["/api/bg_ticket"]);
  assert.deepEqual(env.calls.tok, ["NEWTOK"]);
  const saved = JSON.parse(env.store.cb_bgtok);
  assert.equal(saved.tok, "NEWTOK");
  assert.equal(saved.dev, "D1");
}

/* 3) 缓存是别的设备的票 → 不能拿它交差 */
{
  const env = boot({ cache: { dev: "D9", tok: "OTHER", exp: NOW + 30 * 86400 } });
  env.sync();
  assert.deepEqual(env.calls.tok, [], "先别把别的机器的票交给壳");
  await Promise.resolve(); await Promise.resolve();
  assert.deepEqual(env.calls.tok, ["NEWTOK"]);
}

/* 4) 取不到 → 不交任何东西（壳继续用旧的/裸请求） */
{
  const env = boot({ resp: { ok: false, status: 401, json: { ok: false } } });
  env.sync();
  await Promise.resolve(); await Promise.resolve();
  assert.deepEqual(env.calls.tok, []);
  assert.equal(env.store.cb_bgtok, undefined);
}

/* 5) 没有壳（浏览器）→ 一次请求都不发 */
{
  const env = boot({ cb: null });
  env.sync();
  assert.deepEqual(env.calls.fetch, []);
}

console.log("test_bg_token_page: ok");
