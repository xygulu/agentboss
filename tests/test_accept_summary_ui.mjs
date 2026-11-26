#!/usr/bin/env node
/* 验收摘要浮层（A5）接线：node tests/test_accept_summary_ui.mjs
   点「通过」先拉 /api/accept_summary，摘要转义后进确认框；拉不到也照样弹确认。 */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import assert from "node:assert";

const here = dirname(fileURLToPath(import.meta.url));
const src = readFileSync(join(here, "..", "host", "static", "src", "js", "05_cmd.js"), "utf8");

const m = src.match(/async function ledgerTaskAccept\(sid\)\{[\s\S]*?\n\}/);
assert(m, "05_cmd.js 里找不到 ledgerTaskAccept");
const body = m[0];

const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

let asked = null, posted = null;
const askConfirm = (title, msg, okText, danger, fn) => { asked = { title, msg, okText, fn }; };

function make(xj) {
  return new Function("xj", "askConfirm", "esc", "ledgerAcceptPost",
    body + "; return { ledgerTaskAccept };")(xj, askConfirm, esc, (sid) => { posted = sid; });
}

const calls = [];
const ok = make(async (url) => { calls.push(url); return { json: { ok: true, text: "<已改完> & 测试全绿" } }; });
await ok.ledgerTaskAccept("s1");
assert.equal(calls[0], "/api/accept_summary?sid=s1", "没拉验收摘要");
assert(asked.msg.includes("&lt;已改完&gt; &amp; 测试全绿"), "摘要没转义就进 innerHTML");
assert(asked.msg.includes("确认通过这个任务？"), "原确认文案丢了");
assert.equal(asked.okText, "通过");
asked.fn(); assert.equal(posted, "s1", "确认后没走验收提交");

asked = null;
const bad = make(async () => { throw new Error("net"); });
await bad.ledgerTaskAccept("s2");
assert(asked && asked.msg.includes("确认通过这个任务？"), "摘要拉不到就不弹确认了（误触放行的闸没了）");

console.log("test_accept_summary_ui: 6 assertions passed");
