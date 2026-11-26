#!/usr/bin/env node
/* 编码模式（窗口核心 Phase 2 Task 2）：lineCaps.coding 每条线的默认值 + codingOn()
   生效值（设置页显式开关优先）+ pv 预览入口按生效值显隐。
   跑法： node tests/test_coding_mode.mjs */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import assert from "node:assert";

const here = dirname(fileURLToPath(import.meta.url));
const cmdSrc = readFileSync(join(here, "..", "host", "static", "src", "js", "05_cmd.js"), "utf8");
const viewSrc = readFileSync(join(here, "..", "host", "static", "src", "js", "02_view.js"), "utf8");

function pick(src, name) {
  const m = src.match(new RegExp("function " + name + "\\([^)]*\\)\\{[\\s\\S]*?\\n\\}"));
  assert(m, "找不到 " + name);
  return m[0];
}
const noop = function(){};

// ---- lineCaps 每条线带 coding 默认值（codeboss 开 / agentboss 关 / 未知回落） ----
const lineCapsOf = new Function(
  pick(cmdSrc, "lineCaps") + "; return lineCaps;");
assert.strictEqual(lineCapsOf()("codeboss").coding, true, "codeboss 线默认开");
assert.strictEqual(lineCapsOf()("agentboss").coding, false, "agentboss 线默认关");
assert.strictEqual(lineCapsOf()("whatline").coding, true, "未知线回落工头线（开）");

// ---- codingOn() 生效值：CODING_ON undefined 跟线走，显式值优先 ----
const codingOnOf = new Function("cbLine", "CODING_ON", "lineCaps",
  pick(cmdSrc, "codingOn") + "; return codingOn;");
assert.strictEqual(codingOnOf(function(){ return "codeboss"; }, undefined, lineCapsOf())(), true,
  "没上报过开关：跟产品线默认（codeboss 开）");
assert.strictEqual(codingOnOf(function(){ return "agentboss"; }, undefined, lineCapsOf())(), false,
  "没上报过开关：跟产品线默认（agentboss 关）");
assert.strictEqual(codingOnOf(function(){ return "codeboss"; }, false, lineCapsOf())(), false,
  "显式关过：codeboss 线也不许开");
assert.strictEqual(codingOnOf(function(){ return "agentboss"; }, true, lineCapsOf())(), true,
  "显式开过：agentboss 线也要开");

// ---- pv 预览两个入口按生效值显隐 ----
let opened = 0, warned = 0;
const sheetOpen = function(){ opened += 1; };
const toast = function(){ warned += 1; };
const fakeLoc = { origin: "http://x" };
const fvTap = new Function("codingOn", "fviewClose", "sheetOpen", "toast", "SID", "location", "fvPath",
  pick(viewSrc, "fvPreviewTap") + "; return fvPreviewTap;");

opened = 0; warned = 0;
fvTap(function(){ return false; }, noop, sheetOpen, toast, "s1", fakeLoc, "index.html")();
assert.strictEqual(opened, 0, "编码关：看效果（预览）不许开");
assert.ok(warned > 0, "编码关：点预览要有看得见的解释");

opened = 0; warned = 0;
fvTap(function(){ return true; }, noop, sheetOpen, toast, "s1", fakeLoc, "index.html")();
assert.strictEqual(opened, 1, "编码开：看效果（预览）正常开");

const pvOpen = new Function("codingOn", "sheetOpen", "toast", "SID", "location",
  pick(cmdSrc, "previewOpen") + "; return previewOpen;");
opened = 0; warned = 0;
pvOpen(function(){ return false; }, sheetOpen, toast, "s1", fakeLoc)();
assert.strictEqual(opened, 0, "编码关：👁 预览入口不许开");
assert.ok(warned > 0, "编码关：点 👁 预览要有看得见的解释");
opened = 0; warned = 0;
pvOpen(function(){ return true; }, sheetOpen, toast, "s1", fakeLoc)();
assert.strictEqual(opened, 1, "编码开：👁 预览正常开");

console.log("test_coding_mode: 11 assertions passed");
