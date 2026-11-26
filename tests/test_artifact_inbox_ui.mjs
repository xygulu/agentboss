#!/usr/bin/env node
/* 产出物收件箱渲染（B1）：node tests/test_artifact_inbox_ui.mjs
   卡片带 sid/文件名；文件名转义；空列表不占位。 */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import assert from "node:assert";

const here = dirname(fileURLToPath(import.meta.url));
const src = readFileSync(join(here, "..", "host", "static", "src", "js", "05_cmd.js"), "utf8");

function pick(name) {
  const m = src.match(new RegExp("function " + name + "\\([^)]*\\)\\{[\\s\\S]*?\\n\\}"));
  assert(m, "05_cmd.js 里找不到 " + name);
  return m[0];
}
const escHtml = (s) => String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmtSz = (n) => String(n) + "B";

function render(items) {
  const { ibArtsHtml } = new Function("escHtml", "fmtSz", "ART_INBOX",
    pick("ibArtsHtml") + "; return { ibArtsHtml }; ")(escHtml, fmtSz, items);
  return ibArtsHtml();
}

assert.equal(render([]), "", "没有产出物时不许占位");

const h = render([
  { sid: "s1", title: "做图任务", name: 'a"<b>.png', rel: "out/a.png", size: 2048, mtime: 9, kind: "image" },
  { sid: "s2", title: "剪视频", name: "b.mp4", rel: "b.mp4", size: 1024, mtime: 8, kind: "video" },
]);
assert(h.includes("产出物 · 最新 2 件"));
assert(h.includes("&quot;&lt;b&gt;"), "文件名没转义");
assert(h.includes('data-art-sid="s1"') && h.includes('data-art-sid="s2"'));
assert(h.includes('data-art="out%2Fa.png"'), "rel 要 URI 编码后进 data 属性");

console.log("test_artifact_inbox_ui: 6 assertions passed");

/* 收件箱工件卡点击派发：document 级派发（06_settings.js）必须把 [data-art]
   点按送到 ibArtOpen——参数取自 data-art（URI 编码的 rel）与 data-art-sid。
   ibArtOpen 在 05_cmd.js 里一直是孤儿（没有任何派发调它）。 */
const src6 = readFileSync(join(here, "..", "host", "static", "src", "js", "06_settings.js"), "utf8");
const clickHandlers = [...src6.matchAll(/document\.addEventListener\('click',function\(e\)\{[\s\S]*?\n\}\);/g)].map(function(m){ return m[0]; });
const dispatchSrc = clickHandlers.find(function(c){ return c.indexOf("[data-arts]") !== -1; });
assert(dispatchSrc, "06_settings.js 里找不到 document 级收件箱点击派发");

const noop = function(){};
let opened = null;
const ibArtOpenStub = function(enc, name, sid){ opened = { enc: enc, name: name, sid: sid }; };
const artCard = {
  getAttribute: function(k){
    return { "data-art": "out%2Fa.png", "data-art-sid": "s1", "data-art-name": "a.png" }[k] || null;
  },
};
const fakeE = { preventDefault: noop, target: { closest: function(sel){ return sel === "[data-art]" ? artCard : null; } } };
const buildDispatch = new Function("ibArtOpen","artsPeek","switchTo","inboxPaint","taskNewDlg","findRun",
  "taskViewAct","askConfirm","devGoto","ledgerTaskAccept","ledgerTaskReject","testRun",
  "IB_VW","FIND_Q","IB_Q","document",
  dispatchSrc.replace(/^document\.addEventListener\('click',function\(e\)\{/, "var dispatch=function(e){").replace(/\}\);$/, "};") +
  "; return { dispatch: dispatch };");
const api = buildDispatch(ibArtOpenStub, noop, noop, noop, noop, noop, noop, noop, noop, noop, noop, noop, "", "", "", { getElementById: function(){ return null; } });
api.dispatch(fakeE);
assert(opened, "点 [data-art] 卡片没有调 ibArtOpen");
assert.equal(opened.sid, "s1", "ibArtOpen 的 sid 要取自 data-art-sid");
assert.equal(opened.enc, "out%2Fa.png", "ibArtOpen 的 enc 原样传（decode 在 ibArtOpen 内做）");
assert.equal(opened.name, "a.png", "ibArtOpen 的 name 取自 data-art-name");

console.log("test_artifact_inbox_ui: dispatch assertions passed");
