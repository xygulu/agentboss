#!/usr/bin/env node
/* /pv 局域网探针的行为测试：node tests/test_pv_lan_probe.mjs
   背景（2026-11-24）：探针原来用 fetch(u,{mode:'no-cors'})——壳「私网直连同网闸门」
   对跨网段请求代答 404（空 body），fetch 对任何 HTTP 响应都算成功 → 手机不在家网段
   时探针误判「局域网通」→ 浮层 iframe 直跳够不着的地址 → 闸门再代答空 404 →
   预览里所有服务黑屏。
   断言：探针必须像主页面 probePing 一样三重验证（XHR status 200 + ok:1 + pr 回声），
   不通过时行退回 /pv 反代；源码里不许再出现 no-cors fetch。 */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import assert from "node:assert";

const here = dirname(fileURLToPath(import.meta.url));
const py = readFileSync(join(here, "..", "host", "server.py"), "utf8");
const m = py.match(/_PV_LAN_PROBE_JS = r'''([\s\S]*?)'''/);
assert(m, "host/server.py 里找不到 _PV_LAN_PROBE_JS（r''' 三引号原样串）");
const script = m[1];

assert(!/no-cors/.test(script), "探针不许再用 mode:no-cors fetch（闸门假 404 会被当成「通」）");
assert(!/fetch\(/.test(script), "探针不许用 fetch（opaque 响应读不到状态，无法判假 404）");

const LANS = ["192.168.24.160", "192.168.24.1"];
const HOST_PORT = 8790;

function el(href, lan) {
  const a = {
    attrs: { href },
    getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; },
    setAttribute(k, v) { this.attrs[k] = v; },
    removeAttribute(k) { delete this.attrs[k]; },
    getAttributeNames() { return Object.keys(this.attrs); },
  };
  if (lan) a.setAttribute("data-lan", lan);
  return a;
}

function makeEnv() {
  const env = {
    docRows: [
      el("/pv/8780/", "http://192.168.24.160:8780/"),
      el("/pv/3001/", "http://192.168.24.160:3001/"),
      el("/pv/18789/"),                             // 没有 data-lan（回环绑定）：永远反代
    ],
    xhrs: [],
    timeouts: [],
  };
  env.document = {
    querySelectorAll: (sel) =>
      sel === "a[data-lan]" ? env.docRows.filter((a) => a.getAttribute("data-lan") !== null) : [],
  };
  function XMLHttpRequest() {
    this.__id = env.xhrs.length;
    env.xhrs.push(this);
  }
  XMLHttpRequest.prototype.open = function (_m, u) { this.__opened = true; this.__url = u; };
  XMLHttpRequest.prototype.send = function () { this.__sent = true; };
  env.XMLHttpRequest = XMLHttpRequest;
  env.setTimeout = function (fn, ms) { const t = { fn, ms, cleared: false }; env.timeouts.push(t); return t; };
  env.clearTimeout = function (t) { if (t) t.cleared = true; };
  env.__pvLan = new Function("document", "XMLHttpRequest", "setTimeout", "clearTimeout",
    script + "\nreturn __pvLan;")(env.document, env.XMLHttpRequest, env.setTimeout, env.clearTimeout);
  return env;
}

function respond(x, status, body) {
  x.status = status;
  x.responseText = body;
  x.readyState = 4;
  x.onreadystatechange();
}
function fail(x) { if (x.onerror) x.onerror(); }

const hrefs = (env) => env.docRows.map((a) => a.getAttribute("href"));
const lanOn = (env) => env.docRows.map((a) => a.getAttribute("data-lan") !== null);

// ── 1. ping 200 + ok:1 + pr 回声 → 行 href 换成局域网直连（赢的那台 IP）──
let env = makeEnv();
env.__pvLan(LANS, HOST_PORT);
assert.equal(env.xhrs.length, 2, "两个候选各发一个探针");
assert(env.xhrs[0].__opened && env.xhrs[0].__sent, "探针发出去了");
respond(env.xhrs[1], 200, JSON.stringify({ ok: 1, pr: env.xhrs[1].__url.match(/pr=([0-9a-f]+)/)[1] }));
assert.deepEqual(hrefs(env), [
  "http://192.168.24.1:8780/", "http://192.168.24.1:3001/", "/pv/18789/",
], "ping 验证通过 → 行切直连（赢的 IP），无 data-lan 行保持反代");

// ── 2. 壳闸门代答 404（无 CORS 头 → XHR onerror / status 0）→ 全部退回反代 ──
env = makeEnv();
env.__pvLan(LANS, HOST_PORT);
fail(env.xhrs[0]);
respond(env.xhrs[1], 0, "");
assert.deepEqual(hrefs(env), ["/pv/8780/", "/pv/3001/", "/pv/18789/"], "探针不通 → 行保持 /pv 反代");

// ── 3. status 200 但不是开发机（劫持门户页）→ 退回反代 ──
env = makeEnv();
env.__pvLan(LANS, HOST_PORT);
respond(env.xhrs[0], 200, "<html>欢迎光临</html>");
respond(env.xhrs[1], 200, JSON.stringify({ hello: 1 }));
assert.deepEqual(hrefs(env), ["/pv/8780/", "/pv/3001/", "/pv/18789/"], "身份验证不过 → 反代");

// ── 4. 真 404（有 CORS 的闸门也拦得住）→ 退回反代 ──
env = makeEnv();
env.__pvLan(LANS, HOST_PORT);
respond(env.xhrs[0], 404, "");
respond(env.xhrs[1], 404, "");
assert.deepEqual(hrefs(env), ["/pv/8780/", "/pv/3001/", "/pv/18789/"], "404 ≠ 通 → 反代");

// ── 5. 探针全挂住 → 超时兜底退反代 ──
env = makeEnv();
env.__pvLan(LANS, HOST_PORT);
assert.equal(env.timeouts.length, 1, "有超时兜底");
env.timeouts[0].fn();
assert.deepEqual(hrefs(env), ["/pv/8780/", "/pv/3001/", "/pv/18789/"], "超时 → 反代");
// 超时后再到的迟到响应不许翻盘
respond(env.xhrs[0], 200, JSON.stringify({ ok: 1 }));
assert.deepEqual(hrefs(env), ["/pv/8780/", "/pv/3001/", "/pv/18789/"], "迟到的成功不许翻盘");

// ── 6. 没有候选 IP → 不发探针直接反代 ──
env = makeEnv();
env.__pvLan([], HOST_PORT);
assert.equal(env.xhrs.length, 0, "无候选不发探针");
assert.deepEqual(hrefs(env), ["/pv/8780/", "/pv/3001/", "/pv/18789/"], "无候选 → 反代");

// ── 7. ES5 口径：不许箭头函数 / const / let / 模板串（WebView 70）──
assert(!/=>/.test(script), "探针必须全 ES5（WebView 70）");
assert(!/\b(const|let)\b/.test(script), "探针必须全 ES5（WebView 70）");
assert(!/`/.test(script), "探针必须全 ES5（WebView 70）");

console.log("pv 局域网探针行为测试全过");
