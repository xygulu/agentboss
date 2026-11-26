#!/usr/bin/env node
/* 台账页的状态徽章与耗时格式:node tests/test_ledger_page.mjs
   五态→中文徽章;未知态一律「进行中」(不许渲染出空白徽章)。
   耗时:<1h 显示「N 分钟」,≥1h「N.N 小时」。 */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import assert from "node:assert";

const here = dirname(fileURLToPath(import.meta.url));
const src = readFileSync(join(here, "..", "host", "static", "src", "js", "05_cmd.js"), "utf8");

function pick(name) {
  const m = src.match(new RegExp("function " + name + "\\([^)]*\\)\\{[\\s\\S]*?\\n\\}"));
  assert(m, "05_cmd.js 里找不到 " + name);
  return new Function(m[0] + "; return { " + name + " };")();
}

const { ledgerBadge, fmtLedgerDur } = {
  ledgerBadge: pick("ledgerBadge").ledgerBadge,
  fmtLedgerDur: pick("fmtLedgerDur").fmtLedgerDur,
};

assert.equal(ledgerBadge("running"), "进行中");
assert.equal(ledgerBadge("deciding"), "等你拍板");
assert.equal(ledgerBadge("done"), "办结待验收");
assert.equal(ledgerBadge("accepted"), "已通过");
assert.equal(ledgerBadge("rejected"), "已打回");
assert.equal(ledgerBadge("exited"), "进行中", "未知态兜底不许空白");
assert.equal(ledgerBadge(undefined), "进行中");

assert.equal(fmtLedgerDur(0), "—", "零耗时(零轮次)显示破折号");
assert.equal(fmtLedgerDur(37 * 60), "37 分钟");
assert.equal(fmtLedgerDur(3.2 * 3600), "3.2 小时");
assert.equal(fmtLedgerDur(7200), "2 小时");

console.log("test_ledger_page: 11 assertions passed");

// ── 验收按钮:只有 done(办结待验收)态渲染;已验收/在跑/拍板中一律没有 ──
const mk = src.match(new RegExp("function ledgerActions\\([^)]*\\)\\{[\\s\\S]*?\\n\\}"));
assert(mk, "05_cmd.js 里找不到 ledgerActions(验收按钮)");
const { ledgerActions } = new Function("escHtml", mk[0] + "; return { ledgerActions };")((x) => String(x).replace(/[&<>'"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])));
assert(ledgerActions("done", "s1").includes("通过") && ledgerActions("done", "s1").includes("打回"));
assert(!ledgerActions("accepted", "s1"), "已通过不许再有按钮");
assert(!ledgerActions("rejected", "s1"), "已打回不许再有按钮");
assert(!ledgerActions("running", "s1"));
assert(!ledgerActions("deciding", "s1"));

// ── 老板旗舰挂线:cbLine 判定(query 优先/cookie 兜底/缺省 codeboss)──
const ml = src.match(new RegExp("function cbLine\\(\\)\\{[\\s\\S]*?\\n\\}"));
assert(ml, "05_cmd.js 里找不到 cbLine(产品线判定)");
function lineIn(search, cookie) {
  const document = { cookie: cookie || "" };
  const location = { search: search };
  const { cbLine } = new Function("location", "document", ml[0] + "; return { cbLine };")(location, document);
  return cbLine();
}
assert.equal(lineIn("?product=agentboss", "cb_product=codeboss"), "agentboss", "query 优先");
assert.equal(lineIn("", "cb_product=agentboss; x=1"), "agentboss", "cookie 兜底");
assert.equal(lineIn("", ""), "codeboss", "缺省工头线");
assert.equal(lineIn("?other=1", ""), "codeboss", "无关 query 不误判");

// ── 终审 C2 钉子:验收 POST 必须用 method:'POST'(xj 只认 opt.method,
//    写成 m:'POST' 会以 GET 发出 → 405 → 验收永远失败)──
assert(src.includes("{method:'POST',body:{sid:sid,verdict:'accept'}}"),
  "ledgerAcceptPost 的 xj 调用丢了 method:'POST'");

// ── 2026-11-19 口径：两条线都叫「工作台」，台账不再老板线专属 ──
assert(!src.includes(")'工作台':'收件箱'"), "页签名不该再按产品线分叉");
assert(src.includes("lb.textContent='工作台'"), "页签固定叫工作台");
assert(!src.includes("if(cbLine()!=='agentboss') return;"), "台账加载不该按产品线短路");
assert(!src.includes("if(!boss&&DONE_LIST.length)"), "「刚完成」区块已并入待验收");
assert(src.includes("{method:'POST',body:{sid:sid,verdict:'reject',note:n}}"),
  "ledgerRejectPost 的 xj 调用丢了 method:'POST'");
assert(!src.includes("m:'POST'"), "xj 不认 m: 选项——验收请求会退化成 GET");
