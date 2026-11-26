#!/usr/bin/env node
/* 一键 lint/test/build（A2）接线：node tests/test_cmd_run_ui.mjs
   测试页有三个按钮 → cmdRun(kind) → POST /api/cmd_run（只带 sid+kind）。 */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import assert from "node:assert";

const here = dirname(fileURLToPath(import.meta.url));
const js = readFileSync(join(here, "..", "host", "static", "src", "js", "02_view.js"), "utf8");
const html = readFileSync(join(here, "..", "host", "static", "src", "page.html"), "utf8");

for (const k of ["test", "lint", "build"]) {
  assert(html.includes("cmdRun('" + k + "')"), "测试页缺少「" + k + "」按钮");
}
assert(/async function cmdRun\(kind\)/.test(js), "02_view.js 里没有 cmdRun");
assert(/\/api\/cmd_run/.test(js), "cmdRun 没接 /api/cmd_run");
assert(/\{sid:SID,kind:kind\}/.test(js), "cmd_run 只该带 sid+kind（命令由服务端配置决定）");
assert(/testPoll\(prevTs,200\)/.test(js), "开跑后没有轮询结果");

console.log("test_cmd_run_ui: 6 assertions passed");
