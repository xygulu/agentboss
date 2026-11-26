#!/usr/bin/env node
/* 测试红绿卡渲染（A1）：node tests/test_test_cards_ui.mjs
   红绿徽章；命令里的引号必须转义（cmd 进 data 属性和 HTML 两处）。 */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import assert from "node:assert";

const here = dirname(fileURLToPath(import.meta.url));
const src = readFileSync(join(here, "..", "host", "static", "src", "js", "02_view.js"), "utf8");

function pick(name) {
  const m = src.match(new RegExp("function " + name + "\\([^)]*\\)\\{[\\s\\S]*?\\n\\}"));
  assert(m, "02_view.js 里找不到 " + name);
  return m[0];
}

const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmtTm = () => "11-19 10:00";
const { testBadge, testCardHtml } = new Function("esc", "fmtTm",
  pick("testBadge") + pick("testCardHtml") + "; return { testBadge, testCardHtml }; ")(esc, fmtTm);

assert(testBadge("pass").includes("通过"));
assert(testBadge("fail").includes("失败"));
assert(testBadge("").includes("未跑"), "未知状态不许空白");

const h = testCardHtml({ cmd: 'pytest -q "x"', state: "fail", rc: 1, ts: 0, dur: 2.4, tail: "1 failed" });
assert(h.includes("&quot;x&quot;"), "命令引号没转义，data 属性会被截断");
assert(!/<code[^>]*>pytest -q "x"/.test(h), "命令被原样写进 HTML");
assert(h.includes("退出码 1") && h.includes("1 failed"));

const ok = testCardHtml({ cmd: "npm test", state: "pass", rc: 0, ts: 0, dur: 10, tail: "" });
assert(ok.includes("npm test") && !ok.includes("<pre"), "通过的卡不带输出块");

console.log("test_test_cards_ui: 8 assertions passed");
