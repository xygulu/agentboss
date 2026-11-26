#!/usr/bin/env node
/* 壳自更新检查必须带产品线参数：node tests/test_vs_ver_api.mjs
   2026-11-18 事故：vsVerApi() 返回裸 /api/apk_ver，中继缺省落 codeboss 槽，
   agentboss 壳「检查更新」被提示装 codeboss 的包。
   壳原生层（MainActivity /api/apk_ver?product=）一直是对的，漏的是前端这一处。
   断言 vsVerApi() 的三个来源优先级：URL query > cookie cb_product > 无参。 */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import assert from "node:assert";

const here = dirname(fileURLToPath(import.meta.url));
const src = readFileSync(join(here, "..", "host", "static", "src", "js", "06_settings.js"), "utf8");
const m = src.match(/function vsVerApi\(\)\{[\s\S]*?\n\}/);
assert(m, "06_settings.js 里找不到 vsVerApi 函数");

function vsVerApiIn(locSearch, cookie) {
  const location = { search: locSearch };
  const document = { cookie: cookie || "" };
  const window = { CB: { apkVer: "3" } };              // 安卓壳：走 apk_ver 通道
  // 浏览器里 window.CB 与裸名 CB 同指；函数体两种写法都可能出现，都注入
  const { vsVerApi } = new Function(
    "window", "CB", "location", "document",
    m[0] + "; return {vsVerApi};"
  )(window, window.CB, location, document);
  return vsVerApi();
}

// 壳 defaultUrl() 打开的页面：query 优先
assert.equal(vsVerApiIn("?product=agentboss", "cb_product=codeboss"),
             "/api/apk_ver?product=agentboss");
// 配对 URL 可能不带 query：回退壳种的 cookie
assert.equal(vsVerApiIn("", "cb_product=agentboss; other=x"),
             "/api/apk_ver?product=agentboss");
// 都没有（老壳/浏览器直开）：裸路径，中继缺省 codeboss，零感知
assert.equal(vsVerApiIn("", ""), "/api/apk_ver");
assert.equal(vsVerApiIn("?other=1", "r_sess=xyz"), "/api/apk_ver");

console.log("test_vs_ver_api: 4 assertions passed");
