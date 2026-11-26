#!/usr/bin/env node
/* 手机端产出物编辑 UI（窗口核心 Phase 2 Task 4）：
   ✏️ 入口按 codingOn 显隐；保存 POST 全参；成功回 ref_msg 自动 sendMsg；
   conflict 不发消息只提示。跑法： node tests/test_art_edit_ui.mjs */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import assert from "node:assert";

const here = dirname(fileURLToPath(import.meta.url));
const viewSrc = readFileSync(join(here, "..", "host", "static", "src", "js", "02_view.js"), "utf8");

function pick(name) {
  const m = viewSrc.match(new RegExp("function " + name + "\\([^)]*\\)\\{[\\s\\S]*?\\n\\}"));
  assert(m, "02_view.js 里找不到 " + name);
  return m[0];
}
const AsyncFunction = Object.getPrototypeOf(async function(){}).constructor;
function pickAsync(name) {
  const m = viewSrc.match(new RegExp("async function " + name + "\\([^)]*\\)\\{[\\s\\S]*?\\n\\}"));
  assert(m, "02_view.js 里找不到 async " + name);
  return m[0];
}

// ---- (a) ✏️ 入口：coding 开才渲染 ----
const btnOf = new Function("codingOn",
  pick("fvEditBtnHtml") + "; return fvEditBtnHtml;");
assert.ok(btnOf(function(){ return true; })().includes("fvEditOpen"), "编码开：要渲染 ✏️ 编辑钮");
assert.equal(btnOf(function(){ return false; })(), "", "编码关：不许渲染编辑钮");

// ---- fvEditOpen：拉全文进 textarea ----
const el = (props) => Object.assign({ style: {}, classList: { add(){}, remove(){} } }, props);
const els = {};
const doc = { getElementById: (id) => els[id] };
let xjCalls = [];
const xjStub = function(url, opt){
  const rec = { url: url, opt: opt || {} };
  xjCalls.push(rec);
  return xjStub.next(rec);
};
xjStub.next = function(){ return { status: 200, json: { ok: true } }; };

const openOf = new AsyncFunction("codingOn", "document", "xj", "toast", "SID", "fvPath", "FV_EDIT", "escHtml",
  pickAsync("fvEditOpen") + "; return fvEditOpen;");
els.fvBody = el({ innerHTML: "" });

// 成功拉全文：textarea 出现、FV_EDIT 记 hash
xjStub.next = function(){ return { status: 200, json: { ok: true, text: "# hi\n", hash: "ab12", size: 5 } }; };
els.fvEditTa = el({ value: "" });
xjCalls = [];
await (await openOf(function(){ return true; }, doc, xjStub, function(){}, "s1", "note.md", null, (s) => s))();
assert.equal(xjCalls[0].url, "/api/ext_art_text?sid=s1&rel=note.md", "GET 要带 sid+rel");
assert.ok(els.fvEditTa.value !== undefined, "textarea 建出来了");
await new Promise((r2) => setTimeout(r2, 0));
assert.equal(els.fvEditTa.value, "# hi\n", "全文填进 textarea");
assert.ok(xjCalls[0] && true, "调用记录在");

// 编码关：入口函数直接拒绝，不发请求
xjCalls = [];
await (await openOf(function(){ return false; }, doc, xjStub, function(){}, "s1", "note.md", null, (s) => s))();
assert.equal(xjCalls.length, 0, "编码关：fvEditOpen 不发请求");

// ---- (b) fvEditSave 成功：POST 全参 + ref_msg 自动 sendMsg ----
const saveOf = new AsyncFunction("document", "xj", "toast", "sendMsg", "fviewClose", "FV_EDIT", "SID",
  pickAsync("fvEditSave") + "; return fvEditSave;");
let sentMsg = null, closed = 0, toasts = [];
els.txt = el({ value: "" });
els.fvEditSaveBtn = el({ disabled: false, textContent: "保存" });
els.fvEditSaveBtn.id = "fvEditSave";
const docSave = { getElementById: (id) => (id === "fvEditSave" ? els.fvEditSaveBtn : els[id]) };
const refMsg = "手机改了 note.md：diff 在 .boss/edits/1.diff，新全文在同名 .new，请按 diff 应用";
xjCalls = [];
xjStub.next = function(rec){
  return { status: 200, json: { ok: true, ref_dir: "/p/.boss/edits", ref_msg: refMsg } };
};
const fvEditState = { hash: "ab12", rel: "note.md" };
await (await saveOf(docSave, xjStub,
  (m) => toasts.push(m), () => { sentMsg = els.txt.value; }, () => { closed += 1; },
  fvEditState, "s1"))();
await new Promise((r3) => setTimeout(r3, 0));
const postRec = xjCalls.find((c) => String(c.url).indexOf("/api/ext_art_edit") !== -1);
assert.ok(postRec, "要 POST /api/ext_art_edit");
assert.deepEqual([postRec.opt.body.sid, postRec.opt.body.rel,
                  postRec.opt.body.base_hash], ["s1", "note.md", "ab12"], "POST 体带 sid/rel/base_hash");
assert.equal(typeof postRec.opt.body.content, "string");
assert.equal(sentMsg, refMsg, "成功后 ref_msg 自动发进会话");
assert.ok(closed >= 1, "保存成功要关浮层");

// ---- (c) conflict：toast「文件已变」且不 sendMsg ----
sentMsg = null; closed = 0; toasts = [];
xjStub.next = function(){ return { status: 409, json: { ok: false, conflict: true,
  error: "文件在你打开后被改过（可能 agent 刚动了它）。重新打开看最新内容再改。" } }; };
await (await saveOf(docSave, xjStub,
  (m) => toasts.push(m), () => { sentMsg = els.txt.value; }, () => { closed += 1; },
  fvEditState, "s1"))();
await new Promise((r4) => setTimeout(r4, 0));
assert.equal(sentMsg, null, "conflict 不许发消息");
assert.ok(toasts.some((t) => String(t).includes("文件已变")), "conflict 要提示「文件已变」");

// ---- 网络/服务失败：toast 可重试（按钮恢复）----
sentMsg = null; toasts = [];
els.fvEditSaveBtn.disabled = true;
xjStub.next = function(){ throw new Error("network down"); };
await (await saveOf(docSave, xjStub,
  (m) => toasts.push(m), () => { sentMsg = els.txt.value; }, () => { closed += 1; },
  fvEditState, "s1"))();
await new Promise((r5) => setTimeout(r5, 0));
assert.equal(sentMsg, null);
assert.equal(els.fvEditSaveBtn.disabled, false, "失败后按钮恢复，可重试");
assert.ok(toasts.some((t) => String(t).includes("保存失败")), "失败要有看得见的 toast");

console.log("test_art_edit_ui: all assertions passed");
