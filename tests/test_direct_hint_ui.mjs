#!/usr/bin/env node
/* iOS 直连卡片文案：WebKit 混合内容拦掉 http 探针是 iOS 的常态（安卓壳放行混合
   内容所以没事），不能拿「局域网不通」吓 iPhone 用户——iOS UA 下改成中性说明，
   非 iOS 保持原样（错误原因照旧显示，安卓/浏览器排查还得靠它）。
   跑法： node tests/test_direct_hint_ui.mjs */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import assert from "node:assert";

const here = dirname(fileURLToPath(import.meta.url));
const view = readFileSync(join(here, "..", "host", "static", "src", "js", "02_view.js"), "utf8");

function fnSrcFrom(src, name) {
  let i = src.indexOf("function " + name + "(");
  assert(i >= 0, "找不到 " + name);
  const s = src.indexOf("{", i);
  let d = 0;
  for (let k = s; k < src.length; k++) {
    if (src[k] === "{") d++;
    else if (src[k] === "}" && --d === 0) return src.slice(i, k + 1);
  }
  throw new Error("括号不配对：" + name);
}

function run(ua, direct) {
  const el = { textContent: "", title: "" };
  /* 抽出来的是函数声明，new Function 的 body 里要补一句 return 把它递出来 */
  const fn = new Function("navigator", "document", "DIRECT", "Date",
    fnSrcFrom(view, "vsDirectRefresh") + "; return vsDirectRefresh;");
  fn({ userAgent: ua },
     { getElementById: () => el },
     direct,
     { now: () => 1000 })();
  return el;
}

const ERR = "网络错误（多半是 https 页面发 http 被浏览器拦了）";

{
  /* iOS（壳 UA 带 iPhone）：不再说「局域网不通」，说清是内核策略且直连没受影响 */
  const el = run("Mozilla/5.0 (iPhone; CPU iPhone OS 18_7 like Mac OS X) AppleWebKit/605.1.15",
    { st: "on", mode: "dc", lanErr: ERR, cool: 0, trying: false, diag: "" });
  assert(!el.textContent.includes("局域网不通"), "iOS 还在显示局域网不通：" + el.textContent);
  assert(el.textContent.includes("iOS"), "iOS 没有说明原因：" + el.textContent);
  assert(el.textContent.includes("P2P"), "iOS 没有说明直连走 P2P：" + el.textContent);
}

{
  /* 非 iOS（安卓/浏览器）：原因原样保留——排查还靠它 */
  const el = run("Mozilla/5.0 (Linux; Android 9; Qin 2 Pro) AppleWebKit/537.36",
    { st: "on", mode: "dc", lanErr: ERR, cool: 0, trying: false, diag: "" });
  assert(el.textContent.includes("局域网不通"), "非 iOS 丢了原因：" + el.textContent);
  assert(el.textContent.includes("超时") || el.textContent.includes(ERR.slice(0, 10)),
    "非 iOS 原因被截没了");
}

{
  /* 局域网真连上时，两边都不带括号备注 */
  for (const ua of ["Mozilla/5.0 (iPhone", "Mozilla/5.0 (Linux; Android 9"]) {
    const el = run(ua, { st: "on", mode: "lan", lanErr: "", cool: 0, trying: false, diag: "" });
    assert(el.textContent.startsWith("局域网直连"), "lan 模式文案不对：" + el.textContent);
  }
}

console.log("test_direct_hint_ui: 3/3 ok");
