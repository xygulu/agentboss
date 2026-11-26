#!/usr/bin/env node
/* 页面自更新不得偷输入框焦点：node tests/test_pver_typing_guard.mjs
   2026-11-18 事故：08:04 部署新 ext.html 后手机自动 reload，用户手指停在
   输入框（>8s 没打字，LAST_TYPE 护栏过期）→ 输入框 DOM 被换掉、软键盘还
   开着但焦点丢了 → 按任何键（含删除）都没反应。
   断言两件事：
   1. pverCheck() 的两个 reload 分支（构建戳硬刷 + 版本软刷）都有 typingHold() 护栏
   2. typingHold() 行为：输入框聚焦 → true；8 秒内打过字 → true；其余 → false */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import assert from "node:assert";

const here = dirname(fileURLToPath(import.meta.url));
const js = (n) => readFileSync(join(here, "..", "host", "static", "src", "js", n), "utf8");

// ── 1. pverCheck 两个分支都有 typingHold 护栏 ──
const acc = js("07_account.js");
const pver = acc.match(/function pverCheck\(\)\{[\s\S]*?\n\}/);
assert(pver, "07_account.js 里找不到 pverCheck 函数");
const calls = pver[0].match(/typingHold\(\)/g) || [];
assert.ok(calls.length >= 2,
  "pverCheck 的 reload 护栏必须两处（硬刷+软刷）都调 typingHold()，现在只有 " + calls.length + " 处");

// ── 2. typingHold 行为 ──
const core = js("05_cmd.js");
const mfn = core.match(/function typingHold\(\)\{[\s\S]*?\n\}/);
assert(mfn, "05_cmd.js 里找不到 typingHold 函数");
const ta = { id: "txt" }; // 输入框替身（真 ta 是 DOM 节点，聚焦比较只看引用）

function hold(active, lastTypeMs) {
  const document = { activeElement: active };
  const LAST_TYPE = lastTypeMs;               // null = 从没打过字
  const { typingHold } = new Function(
    "ta", "document", "LAST_TYPE", mfn[0] + "; return {typingHold};"
  )(ta, document, LAST_TYPE);
  return typingHold();
}

assert.equal(hold(ta, null), true, "手指在输入框里（聚焦）必须拦");
assert.equal(hold(ta, Date.now() - 90000), true, "聚焦但 90 秒没动也必须拦（旧护栏的 8 秒空档）");
assert.equal(hold(null, Date.now() - 3000), true, "3 秒前刚打过字必须拦");
assert.equal(hold(null, Date.now() - 90000), false, "90 秒没动且不在框里：放行 reload");
assert.equal(hold(null, null), false, "没打过字也不在框里：放行 reload");

// ── 3. 正看着东西的时候不许整页重载 ──
/* 2026-11-20 用户：查产出物内容时整页自动刷新，跳回会话页。就是这里——
   每次发版 ext.html 变了，pverCheck 60 秒内无条件 reload，把开着的预览层/
   产出物页整个换掉（pageRemember 只记得住第几屏，记不住浮层）。 */
const vh = acc.match(/function viewingHold\(\)\{[\s\S]*?\n\}/);
assert(vh, "07_account.js 里找不到 viewingHold 函数");
assert.ok((pver[0].match(/viewingHold\(\)/g) || []).length >= 2,
  "pverCheck 的 reload 护栏两处（硬刷+软刷）都要调 viewingHold()");
assert(/if\(hasDraft\(\)\|\|voiceBusyNow\(\)\|\|viewingHold\(\)\) return;/.test(acc),
  "长跑自愈 reload 也要拦住「正在看东西」");

function holdView(ids) {
  const document = { getElementById: (id) => ids[id] || null };
  const { viewingHold } = new Function("document", vh[0] + "; return {viewingHold};")(document);
  return viewingHold();
}
const on = { classList: { contains: (c) => c === "on" } };
const off = { classList: { contains: () => false } };
assert.equal(holdView({ fview: on }), true, "文档/图片预览开着：不许重载");
assert.equal(holdView({ cbSheet: on }), true, "网页预览层开着：不许重载");
assert.equal(holdView({ filePage: on }), true, "产出物页开着：不许重载");
assert.equal(holdView({ fview: off, cbSheet: off, filePage: off }), false, "都关着：放行");
assert.equal(holdView({}), false, "元素都不在：放行");

console.log("test_pver_typing_guard: 15 assertions passed");
