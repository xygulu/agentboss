#!/usr/bin/env node
/* 快捷键条里的「普通按钮」在触屏上必须能点（2026-11-19）：
   #keys 的 touch 委托只接管带 data-k 的按键；文件/图片/测试/发布是 onclick 按钮，
   不能被 preventDefault 吃掉合成 click。以前无条件 preventDefault →
   手机上这四个按钮点了毫无反应（桌面鼠标正常，所以一直没被发现）。
   跑法：node tests/test_keys_touch_ui.mjs */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import assert from "node:assert";

const here = dirname(fileURLToPath(import.meta.url));
const html = readFileSync(join(here, "..", "host", "static", "src", "page.html"), "utf8");
const net = readFileSync(join(here, "..", "host", "static", "src", "js", "04_net.js"), "utf8");

function block(re, what) {
  const m = re.exec(net);
  assert(m, what + "：找不到这段处理");
  return m[0];
}

const start = block(
  /ks\.addEventListener\('touchstart',function\(e\)\{[\s\S]*?\},\{passive:false\}\)/,
  "keys touchstart");
const end = block(
  /ks\.addEventListener\('touchend',function\(e\)\{[\s\S]*?\},\{passive:false\}\)/,
  "keys touchend");

for (const [name, src] of [["touchstart", start], ["touchend", end]]) {
  assert(/data-k/.test(src),
    name + " 没认 data-k：普通按钮（文件/图片/测试/发布）会被吃掉点击");
  const iGuard = src.search(/data-k|hasAttribute/);
  const iPrev = src.indexOf("e.preventDefault()");
  assert(iPrev < 0 || (iGuard >= 0 && iGuard < iPrev),
    name + "：data-k 判断必须在 preventDefault 之前");
}

/* 这四个按钮本来就没有 data-k —— 固定住，免得以后有人给它们补上 data-k 改成发键 */
for (const fn of ["filesOpen()", "testOpen()", "deployOpen()", "previewOpen()", "apkOpen()", "shotOpen()"]) {
  const re = new RegExp('<button[^>]*onclick="' + fn.replace(/[()]/g, "\\$&") + '"');
  const tag = re.exec(html);
  assert(tag, "找不到 onclick=" + fn + " 的按钮");
  assert(!/data-k/.test(tag[0]), fn + " 的按钮不该有 data-k（它是普通按钮）");
}

/* 第一排（.krow）和第二排（#kmore）之间要和 #kmore 内部行距一致。
   以前 #kmore 多了 margin-top:5px + padding-top:5px = 10px，比行距 4px 宽一截 */
const km = /#kmore\{([^}]*)\}/.exec(html);
assert(km, "找不到 #kmore 样式");
assert(/gap:4px/.test(km[1]), "#kmore 内部行距应是 4px");
assert(/margin-top:4px/.test(km[1]), "第一排到第二排的间距也应是 4px");
assert(!/padding-top/.test(km[1]), "#kmore 不要额外的 padding-top（会撑大第一排后的间距）");

console.log("test_keys_touch_ui: 11 assertions passed");
