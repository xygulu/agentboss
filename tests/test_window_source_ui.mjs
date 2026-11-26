#!/usr/bin/env node
/* 窗口核心 Phase 1 · Task 4（手机侧）：源徽标 + 工头入口去除。
   (a) srcChip：screen stub 行 → 「屏幕源（未实装）」；protocol 行 → 「协议」；pty → 无
   (b) renderList 接线：smeta 行调用了 srcChip(s)
   (c) 工头入口已除：page.html 无 bossBtn、boss.js 无 /api/boss/health 探测、构建产物同口径
   跑法： node tests/test_window_source_ui.mjs */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import assert from "node:assert";

const here = dirname(fileURLToPath(import.meta.url));
const root = join(here, "..");
const cmd = readFileSync(join(root, "host", "static", "src", "js", "05_cmd.js"), "utf8");
const page = readFileSync(join(root, "host", "static", "src", "page.html"), "utf8");
const bossJs = readFileSync(join(root, "host", "static", "boss.js"), "utf8");
let extHtml = "";
try { extHtml = readFileSync(join(root, "host", "static", "ext.html"), "utf8"); } catch (e) {}

function fnSrc(src, name) {
  const i = src.indexOf("function " + name + "(");
  assert(i >= 0, "找不到 " + name);
  if (i >= 6 && src.slice(i - 6, i) === "async ") i -= 6;
  const s = src.indexOf("{", i);
  let d = 0;
  for (let k = s; k < src.length; k++) {
    if (src[k] === "{") d++;
    else if (src[k] === "}" && --d === 0) return src.slice(i, k + 1);
  }
  throw new Error("括号不配对：" + name);
}

/* (a) srcChip */
const srcChip = new Function("escHtml", "var agentLabelOf=function(a){return a;};\n"
  + fnSrc(cmd, "srcChip") + "\n;return srcChip;")(
  x => String(x));

{
  const stub = srcChip({ source: { kind: "screen", sem: 0, stub: 1 } });
  assert(stub.indexOf("屏幕源（未实装）") >= 0, "(a) stub 行要有「屏幕源（未实装）」徽标");

  const proto = srcChip({ source: { kind: "protocol", sem: 1, stub: 0 } });
  assert(proto.indexOf("协议") >= 0, "(a) protocol 行要有「协议」徽标");

  assert(srcChip({ source: { kind: "pty", sem: 1, stub: 0 } }) === "", "(a) pty 行不加新徽标");
  assert(srcChip({}) === "", "(a) 无 source 字段（老 host）不炸不标");
  console.log("a ok");
}

/* (b) renderList 接线 */
{
  assert(cmd.indexOf("srcChip(s)") >= 0, "(b) renderList 的行渲染要挂上 srcChip(s)");
  console.log("b ok");
}

/* (c) 工头入口去除（2026-11-25 拍板：UI 回经典列表；/api/boss/* 端点保留休眠） */
{
  assert(page.indexOf("bossBtn") < 0, "(c) page.html 不该再有工头入口按钮");
  assert(bossJs.indexOf("/api/boss/health") < 0, "(c) boss.js 不该再探测入口灰度");
  assert(bossJs.indexOf("window.bossOpen") >= 0, "(c) bossOpen 等 Phase 2 资产保留休眠");
  if (extHtml) assert(extHtml.indexOf('id="bossBtn"') < 0, "(c) 构建产物里入口按钮也没了");
  console.log("c ok");
}

console.log("window source ui: all ok");
