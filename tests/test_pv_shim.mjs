#!/usr/bin/env node
/* /pv 运行时请求 shim 的行为测试：node tests/test_pv_shim.mjs（pytest 里没 node 会跳过）
   背景：App 预览是 /pv/<port> 子路径反代，静态属性服务端已改写，但 SPA 在运行时
   自己拼的同源绝对路径（fetch/XHR/WebSocket/EventSource）会打到中继根上——
   deeptutor 登录 "request failed" 就是这一类（2026-11-24）。
   断言：同源绝对路径补前缀；已带前缀/跨源/页内相对路径原样放行；重复 patch 不叠加。 */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import assert from "node:assert";

const here = dirname(fileURLToPath(import.meta.url));
const py = readFileSync(join(here, "..", "host", "server.py"), "utf8");
const m = py.match(/_PV_SHIM_JS = r'''([\s\S]*?)'''/);
assert(m, "host/server.py 里找不到 _PV_SHIM_JS（r''' 三引号原样串）");
const shim = m[1];

const H = "r.example";

function makeWin(proto) {
  const w = {
    location: { protocol: proto, host: H, origin: proto + "//" + H,
                href: proto + "//" + H + "/pv/3782/login" },
    URL,
    fetchCalls: [], xhrOpens: [], wsUrls: [], esUrls: [],
  };
  w.fetch = function (i) { w.fetchCalls.push(typeof i === "string" ? i : String(i)); return Promise.resolve({}); };
  w.XMLHttpRequest = function () {};
  w.XMLHttpRequest.prototype.open = function (_m, u) { w.xhrOpens.push(u); };
  w.WebSocket = function (u) { w.wsUrls.push(u); };
  w.WebSocket.CONNECTING = 0;
  w.EventSource = function (u) { w.esUrls.push(u); };
  return w;
}

function applyShim(w) {
  const pvShim = new Function("window", "document", shim + "\nreturn window.__pvShim;")(w, undefined);
  assert(typeof pvShim === "function", "shim 必须暴露 __pvShim(pre)");
  pvShim("/pv/3782");
  return w;
}

// ── fetch ──
let w = applyShim(makeWin("https:"));
w.fetch("/api/auth/login");
w.fetch("/pv/3782/api/auth/login");                    // 已带前缀：不动
w.fetch("https://other.example/x");                    // 跨源：不动
w.fetch("api/rel");                                    // 页内相对（解析后在 /pv/3782/ 下）：不动
w.fetch(new URL("/x?y=1", w.location.origin));         // URL 对象同源：补前缀
assert.deepEqual(w.fetchCalls, [
  "/pv/3782/api/auth/login",
  "/pv/3782/api/auth/login",
  "https://other.example/x",
  "api/rel",
  "/pv/3782/x?y=1",
], "fetch 改写不对: " + JSON.stringify(w.fetchCalls));

// ── XHR ──
w = applyShim(makeWin("https:"));
w.XMLHttpRequest.prototype.open("POST", "/api/auth/login", true);
w.XMLHttpRequest.prototype.open("GET", "/pv/3782/a");
assert.deepEqual(w.xhrOpens, ["/pv/3782/api/auth/login", "/pv/3782/a"], "XHR 改写不对");

// ── WebSocket ──
w = applyShim(makeWin("https:"));
new w.WebSocket("/ws?token=1");
new w.WebSocket("wss://" + H + "/pv/3782/ws?token=1");
assert.deepEqual(w.wsUrls, [
  "wss://" + H + "/pv/3782/ws?token=1",
  "wss://" + H + "/pv/3782/ws?token=1",
], "WS 改写不对: " + JSON.stringify(w.wsUrls));

// ── EventSource ──
w = applyShim(makeWin("https:"));
new w.EventSource("/api/settings/tests/a/b/events");
assert.deepEqual(w.esUrls, ["/pv/3782/api/settings/tests/a/b/events"], "ES 改写不对");

// ── 幂等：同一窗口重复 patch 不叠加前缀 ──
w = makeWin("https:");
const pvShim = new Function("window", "document", shim + "\nreturn window.__pvShim;")(w, undefined);
pvShim("/pv/3782");
pvShim("/pv/3782");
w.fetch("/api/a");
assert.deepEqual(w.fetchCalls, ["/pv/3782/api/a"], "重复 patch 不应叠加");

// ── http 页面走 ws:（不是 wss:）──
w = applyShim(makeWin("http:"));
new w.WebSocket("/ws");
assert.deepEqual(w.wsUrls, ["ws://" + H + "/pv/3782/ws"], "http 页 WS 协议不对");

console.log("pv shim 行为测试全过");
