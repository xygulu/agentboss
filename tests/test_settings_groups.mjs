#!/usr/bin/env node
/* 窗口核心 Phase 1 · Task 7：设置页 T3 分组重排。
   (a) page.html 恰好 5 个组头，标题与顺序 = 连接/提醒与语音/外观/账号/关于
   (b) grpToggle：折叠/展开切换 + 写 localStorage cb_setgrp（默认全展开）
   (c) 所有设置卡归属且仅归属一个组（按 id 清单断言，无遗漏无重复）
   跑法： node tests/test_settings_groups.mjs */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import assert from "node:assert";

const here = dirname(fileURLToPath(import.meta.url));
const root = join(here, "..");
const page = readFileSync(join(root, "host", "static", "src", "page.html"), "utf8");
const setjs = readFileSync(join(root, "host", "static", "src", "js", "06_settings.js"), "utf8");

function fnSrc(src, name) {
  const i = src.indexOf("function " + name + "(");
  assert(i >= 0, "找不到 " + name);
  const s = src.indexOf("{", i);
  let d = 0;
  for (let k = s; k < src.length; k++) {
    if (src[k] === "{") d++;
    else if (src[k] === "}" && --d === 0) return src.slice(i, k + 1);
  }
  throw new Error("括号不配对：" + name);
}

/* (a) 组头数量/标题/顺序 */
{
  const seg = page.slice(page.indexOf('id="pgVoice"'), page.indexOf("</section>", page.indexOf('id="pgVoice"')));
  const heads = [...seg.matchAll(/<div class="grp[^"]*" id="(grp\w+)"[^>]*>([^<]*(?:<span[^>]*>[^<]*<\/span>)?)/g)]
    .map(m => m[2].replace(/<[^>]+>/g, "").trim() || null);
  const titles = [...seg.matchAll(/<span class="gl">([^<]+)<\/span>/g)].map(m => m[1]);
  assert.deepStrictEqual(titles, ["连接", "提醒与语音", "外观", "账号", "关于"],
    "(a) 组头要恰好是这 5 个、按 T3 顺序");
  console.log("a ok");
}

/* (b) grpToggle 折叠 + cb_setgrp 记忆 */
{
  const store = {};
  const ls = { getItem: k => (k in store ? store[k] : null),
               setItem: (k, v) => { store[k] = String(v); } };
  const mk = () => ({ className: "grp t3g", style: {} });
  const els = {};
  const document = { getElementById: id => (els[id] = els[id] || mk()) };
  const grpToggle = new Function("document", "localStorage",
    fnSrc(setjs, "grpToggle") + "\n;return grpToggle;")(document, ls);

  grpToggle("Conn");                       // 默认展开 → 点一下 = 折叠
  assert(els.grpConn.className.indexOf("closed") >= 0, "(b) 点一下变折叠态");
  assert(els.boxConn.style.display === "none", "(b) 折叠 = 内容隐藏");
  assert(JSON.parse(store.cb_setgrp).conn === true, "(b) 折叠写进 cb_setgrp");

  grpToggle("Conn");                       // 再点 = 展开
  assert(els.grpConn.className.indexOf("closed") < 0, "(b) 再点恢复展开");
  assert(els.boxConn.style.display === "", "(b) 展开 = 内容可见");
  assert(JSON.parse(store.cb_setgrp).conn === false, "(b) 展开也写回");
  console.log("b ok");
}

/* (c) 每张卡恰属一组 */
{
  const seg = page.slice(page.indexOf('id="pgVoice"'), page.indexOf("</section>", page.indexOf('id="pgVoice"')));
  const chunks = seg.split(/<div class="grp/).slice(1);   // [0] 是组头之前的内容
  assert(chunks.length === 5, "(c) 分组切出来要正好 5 块，实际 " + chunks.length);
  const ids = ["vsNetCard", "vsRunCard", "vsDispCard", "vsAccCard", "vsSmartLock",
               "boxSmartSub", "vsVolAlertBox", "vsVolSpeechBox", "vsOutCard"];
  for (const id of ids) {
    const hit = chunks.filter(c => c.indexOf('id="' + id + '"') >= 0);
    assert(hit.length === 1, "(c) " + id + " 要恰好属于一个组，实际 " + hit.length);
  }
  // 套餐卡在账号组（boxPlan 的卡在 grpAcct 块里）
  assert(chunks[3].indexOf('id="boxPlan"') >= 0, "(c) 套餐归账号组");
  // 开发机服务（host 更新）挪回关于组：跟客户端更新挨着好找（用户反馈 2026-11-25）
  assert(chunks[4].indexOf('id="hsBtn"') >= 0, "(c) 开发机服务归关于组");
  assert(chunks[0].indexOf('id="hsBtn"') < 0, "(c) 连接组不该再有开发机服务");
  // 退出登录在账号组
  assert(chunks[3].indexOf('id="vsOutCard"') >= 0, "(c) 退出登录归账号组");
  console.log("c ok");
}

/* (d) 两个更新按钮不同名：分组重排那版把「开发机服务」挪去连接组，
       结果两个同名按钮分居页面首尾，用户找不到该点哪个（用户反馈 2026-11-25） */
{
  const seg = page.slice(page.indexOf('id="pgVoice"'), page.indexOf("</section>", page.indexOf('id="pgVoice"')));
  const labels = [...seg.matchAll(/<button[^>]*>([^<]*)<\/button>/g)].map(m => m[1].trim());
  assert(labels.indexOf("检查客户端更新") >= 0, "(d) 客户端更新按钮要有专属名字");
  assert(labels.indexOf("检查服务更新") >= 0, "(d) 服务更新按钮要有专属名字");
  assert(labels.indexOf("检查更新") < 0, "(d) 别再留裸的「检查更新」按钮");
  console.log("d ok");
}

console.log("settings groups: all ok");
