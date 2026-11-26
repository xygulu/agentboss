#!/usr/bin/env node
/* 壳取播报音频的 URL 上要带 dt 票据：node tests/test_say_dt_url.mjs

   2026-11-19 S24 mini：前台播报取音频 401。壳的 Java HttpURLConnection 只能带
   CookieManager.getCookie() 给的 cookie（这台只回一个 cb_product=codeboss），
   而页面自己取音频是好的。中继那边 /api/say/<id> 现在认 ?dt= 的短命票据
   （中继签给这台设备、10 分钟有效，见 relay.dt_dev），所以页面把地址交给壳时
   要把票放上去——在家 Wi-Fi、在外面流量都一样。

   规则（本测试钉住）：
   1. 手上有新鲜 dt（>60 秒）→ 立刻带票据交给壳。
   2. 没有/快过期 → 先 directTicket() 换新，再带新票交给壳。
   3. 换不到票据 → 照旧交裸地址（别因为拿票失败把播报弄哑了）。
   4. 老壳（只有 playSay）→ 页面自己放，裸地址（页面自己带 cookie）。
*/
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import assert from "node:assert";

const here = dirname(fileURLToPath(import.meta.url));
const src = readFileSync(join(here, "..", "host", "static", "src", "js", "03_voice.js"), "utf8");
const m = src.match(/function speakKind\(k, about, sayUrl, sayText\)\{[\s\S]*?\n\}/);
assert(m, "03_voice.js 里找不到 speakKind");

function boot(opt) {
  opt = opt || {};
  const calls = { say: [], play: [], ticket: 0 };
  const G = {
    window: { CB: opt.cb === null ? null : (opt.cb || {
      sayUrl: function (u) { calls.say.push(u); } }) },
    location: { origin: "https://relay.example.com" },
    DIRECT: opt.direct || { dt: "", exp: 0 },
    directTicket: function () {
      calls.ticket++;
      if (opt.ticket) DIRECT_REF.dt = "NEWTICKET", DIRECT_REF.exp = Date.now() / 1000 + 500;
      return Promise.resolve(!!opt.ticket);
    },
    playSay: function (u) { calls.play.push(u); },
    Date: Date, String: String, encodeURIComponent: encodeURIComponent,
  };
  G.CB = G.window.CB;   // 页面上 CB 是全局的
  // directTicket 的假实现要能改到同一个 DIRECT 对象
  const DIRECT_REF = G.DIRECT;
  const fn = new Function("G", "with(G){" + m[0] + "\nreturn speakKind;}")(G);
  return { calls: calls, speak: fn };
}

const SAY = "/api/say/abc123";
const ABS = "https://relay.example.com/api/say/abc123";
const NOW = () => Date.now() / 1000;

/* 1) 新鲜 dt → 立刻带票交给壳 */
{
  const env = boot({ direct: { dt: "FRESH", exp: NOW() + 500 } });
  env.speak("done", "", SAY, "");
  assert.deepEqual(env.calls.say, [ABS + "?dt=FRESH"], "新鲜票据应当场带上");
  assert.equal(env.calls.ticket, 0, "有新鲜票据就别再换");
}

/* 1b) 地址自带 query → 用 & 接 */
{
  const env = boot({ direct: { dt: "FRESH", exp: NOW() + 500 } });
  env.speak("done", "", "/api/say/abc?x=1", "");
  assert.deepEqual(env.calls.say, [ABS.replace("abc123", "abc") + "?x=1&dt=FRESH"]);
}

/* 2) 没有 dt / 快过期（<60 秒）→ 换新再交 */
for (const direct of [{ dt: "", exp: 0 }, { dt: "OLD", exp: NOW() + 30 }]) {
  const env = boot({ direct: direct, ticket: true });
  env.speak("done", "", SAY, "");
  assert.deepEqual(env.calls.say, [], "先别交，等票据");
  await Promise.resolve();
  await Promise.resolve();
  assert.deepEqual(env.calls.say, [ABS + "?dt=NEWTICKET"], "换来新票后带上");
}

/* 3) 换不到票 → 照旧交裸地址（别把播报弄哑） */
{
  const env = boot({ direct: { dt: "", exp: 0 }, ticket: false });
  env.speak("done", "", SAY, "");
  await Promise.resolve();
  await Promise.resolve();
  assert.deepEqual(env.calls.say, [ABS], "拿票失败也得把地址交出去");
}

/* 4) 老壳（只有 playSay）→ 页面自己放，不带 dt */
{
  const env = boot({ cb: { cue: function () {} },
                     direct: { dt: "FRESH", exp: NOW() + 500 } });
  env.speak("done", "", SAY, "");
  assert.deepEqual(env.calls.play, [ABS], "页面自己放：cookie 本来就带得上");
  assert.deepEqual(env.calls.say, []);
}

console.log("test_say_dt_url: ok");
