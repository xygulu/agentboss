#!/usr/bin/env node
/* 测试/发布入口跟着项目走（2026-11-19）：node tests/test_cmds_ui.mjs
   会话项目里没配 lint/test/build（或发布脚本）就不显示那个按钮；
   开测试页/切会话时问一次 /api/cmds，按答案显隐。 */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import assert from "node:assert";

const here = dirname(fileURLToPath(import.meta.url));
const html = readFileSync(join(here, "..", "host", "static", "src", "page.html"), "utf8");
const view = readFileSync(join(here, "..", "host", "static", "src", "js", "02_view.js"), "utf8");
const cmd = readFileSync(join(here, "..", "host", "static", "src", "js", "05_cmd.js"), "utf8");

for (const k of ["test", "lint", "build"])
  assert(html.includes('id="cmdB_' + k + '"'), "一键按钮没有 id=cmdB_" + k + "，没法按项目显隐");
assert(html.includes('id="cmdNone"'), "没有「这个项目没配命令」的提示位");
assert(/async function testOpen[\s\S]{0,120}cmdsProbe\(/.test(view), "打开测试页没去问项目能跑什么");
assert(/async function cmdsProbe\(/.test(cmd), "没有 cmdsProbe");
assert(/\/api\/cmds\?sid=/.test(cmd), "cmdsProbe 没打 /api/cmds");
assert(/function applyCmdsUi\(/.test(cmd), "没有 applyCmdsUi");
assert(/cmdB_'\+k|cmdB_'\+/.test(cmd), "applyCmdsUi 没按 kind 显隐按钮");
assert(/cmdsProbe\(sid\)/.test(cmd), "切会话后没重新探测（换项目按钮不跟着变）");

console.log("test_cmds_ui: 9 assertions passed");
