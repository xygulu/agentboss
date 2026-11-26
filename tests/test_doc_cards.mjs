#!/usr/bin/env node
/* 工头把该看的东西递到手边（2026-11-20 用户口径）：
   agent 在会话里说到设计文档/方案/原型页 → 那句话下面出一张文档卡，点开直接看；
   预览页底下「✅ 确认 / 💬 提建议」，就地闭环回到会话。
   跑法： node tests/test_doc_cards.mjs */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import assert from "node:assert";

const here = dirname(fileURLToPath(import.meta.url));
const view = readFileSync(join(here, "..", "host", "static", "src", "js", "02_view.js"), "utf8");
const page = readFileSync(join(here, "..", "host", "static", "src", "page.html"), "utf8");

/* 函数体里有嵌套大括号，正则截不准：按括号配对抠出来 */
function fnSrc(name) {
  const i = view.indexOf("function " + name + "(");
  assert(i >= 0, "02_view.js 里找不到 " + name);
  const s = view.indexOf("{", i);
  let d = 0;
  for (let k = s; k < view.length; k++) {
    if (view[k] === "{") d++;
    else if (view[k] === "}" && --d === 0)
      return view.slice(view.indexOf("function " + name + "("), k + 1);
  }
  throw new Error("括号不配对：" + name);
}

const names = ["docPaths", "docCards", "fvReviewKind", "fvReviewOk", "fvReviewAdvice",
  "docSeenAll", "docMarkSeen", "docWebLinks", "docFloatPick", "docFloatRender",
  "docFloatOpen", "docFloatClose", "pvLocalUrl"];
const built = new Function(
  "esc", "document", "toast", "sendMsg", "fitTxt", "fviewClose", "fvPath", "fvName",
  "localStorage", "feedMsgs", "SID", "pageIdx", "DOC_FLOAT", "fviewShow", "pvOpen",
  "var DOC_MSGS=[];\n" + names.map(fnSrc).join("\n") +
  "; return {docPaths,docCards,fvReviewKind,fvReviewOk,fvReviewAdvice,docWebLinks," +
  "docSeenAll,docMarkSeen,docFloatPick,docFloatRender,docFloatOpen,docFloatClose," +
  "setDoc:(ms)=>{DOC_MSGS=ms;}};"
);

const esc = (s) => String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/"/g, "&quot;");
const ta = { value: "", focus() {} };
const sent = [];
let toastText = "";
const store = {};
const localStorage = {
  getItem: (k) => (k in store ? store[k] : null),
  setItem: (k, v) => { store[k] = String(v); },
  removeItem: (k) => { delete store[k]; },
};
const bar = { on: false, classList: { add() { bar.on = true; }, remove() { bar.on = false; } } };
const els = { txt: ta, docBar: bar, docBarIc: { textContent: "" }, docBarName: { textContent: "" }, docBarMeta: { textContent: "" } };
const shown = [];
const pvShown = [];
const feed = [{ role: "assistant", text: "设计文档在 docs/esp32c3-design.md，你看下。" }];
const api = built(esc,
  { getElementById: (id) => els[id] || { value: "", classList: { add() {}, remove() {} } } },
  (m) => { toastText = m; }, () => sent.push(ta.value), () => {},
  () => {}, "docs/x.md", "x.md",
  localStorage, feed, "s1", 1, {},
  (p, n) => shown.push(p), (h) => pvShown.push(h));

// 1) 相对路径、子目录、中文文件名都要出卡
let h = api.docCards("设计文档我放在 docs/设计文档.md 和 docs/superpowers/specs/a-b-c.md 了。");
assert(h.includes('data-dp="docs/设计文档.md"'), "相对路径的设计文档没出卡");
assert(h.includes("设计文档.md"), "卡片要显示文件名");
assert(h.includes('data-dp="docs/superpowers/specs/a-b-c.md"'), "多个文档只出了一张");
assert(h.includes("onclick=\"docOpen(this)\""), "卡片点开要直接看");

// 2) HTML 原型页是网页图标；PDF 是文档图标
assert(api.docCards("看 static/proto/index.html").includes("🌐"));
assert(api.docCards("报价单 out/quote.pdf").includes("📕"));

// 3) 网址不是本机文件——不给卡
h = api.docCards("参考 https://example.com/guide.md 就好");
assert(!h.includes("data-dp"), "网址被当成本机文档了");

// 4) 没提文档就不出空盒子
assert.equal(api.docCards("这次跑完了，用时 12 秒。"), "");

// 5) 能渲染的预览都有确认/提建议；交给系统应用的 PDF 没有
for (const k of ["text", "code", "page", "model", "image"]) assert(api.fvReviewKind(k), k);
for (const k of ["doc", "other"]) assert(!api.fvReviewKind(k), k);

// 6) 确认 = 一句话发回会话；输入框里有草稿时并进去，不许冲掉
ta.value = "";
api.fvReviewOk();
assert.equal(sent.length, 1, "确认要直接发回会话");
assert(sent[0].includes("docs/x.md") && sent[0].includes("已确认"), "确认消息要带上文档路径");

ta.value = "顺便把接口文档也更新一下";
api.fvReviewOk();
assert.equal(sent.length, 1, "有草稿时不能偷偷发出去");
assert(ta.value.includes("已确认") && ta.value.includes("顺便把接口文档也更新一下"),
  "草稿要和确认并在一起");
assert(toastText.includes("草稿"), "要告诉用户确认和草稿一起发");

// 7) 建议 = 预填一段，用户接着输入（语音）就发
ta.value = "";
api.fvReviewAdvice();
assert(ta.value.startsWith("[文档: docs/x.md]") && ta.value.includes("建议"),
  "提建议要预填文档路径和提示");

// 8) 会话墙要把卡片挂上去；预览层要有那两个按钮
assert(/mediaCards\(m\.text\)\+docCards\(m\.text\)/.test(view), "renderFeed 没挂文档卡");
assert(view.includes("fvReviewKind(kind)"), "预览打开时要按类型显隐表态按钮");
assert(page.includes('id="fvReview"'), "预览层缺「确认 / 提建议」这一行");
assert(page.includes("fvReviewOk()") && page.includes("fvReviewAdvice()"), "按钮没接上处理函数");

// 9) 会话里直接浮出来（2026-11-20 用户：不能只在产出物里能看到）
api.docFloatRender();
assert.equal(bar.on, true, "这一轮写了文档，会话里要浮一条");
assert.equal(els.docBarName.textContent, "esp32c3-design.md");
assert.equal(els.docBarIc.textContent, "📝");
api.docFloatOpen();
assert.deepEqual(shown, ["docs/esp32c3-design.md"], "点「查看」要直接进那篇文档的查看页");
assert.equal(bar.on, false, "看过了就不再浮");

// 10) 划掉的也别再冒出来；旧回答里提过的文档不浮（只看最后一条正文）
api.docFloatRender();
assert.equal(bar.on, false, "同一份文档不该反复浮");
feed.length = 0;
feed.push({ role: "assistant", text: "这份是新的：out/report.md" });
api.docMarkSeen("out/report.md");
api.docFloatRender();
assert.equal(bar.on, false, "划掉/看过的不再浮");
feed.push({ role: "assistant", text: "跑完了，用时 12 秒。" });   // 最新一条没提文档
api.docFloatRender();
assert.equal(bar.on, false, "最新一条没提文档就不浮");

// 11) 浮条的挂载点：会话墙重画时重判；实时/原样视图也要浮 ——
//     Claude 会话（desktop:false）默认就落在实时视图，那时 renderFeed 根本不跑，
//     浮条只在对话墙里判的话，手机上永远看不到（2026-11-20 用户：浮层还是没有出来）
assert(/mediaHydrate\(\);\s*try\{ docFloatRender\(\); \}catch\(e\)\{\}/.test(view),
  "renderFeed 重画后要重判浮条");
assert(!fnSrc("setView").includes("docBar"),
  "setView 里别再把浮条收起来：离开会话页由 docFloatRender 自己按 pageIdx 收，切视图不该收");
assert(fnSrc("setView").includes("docFloatRender()"),
  "切到实时/原样后要重判浮条（对话墙不画了，浮条仍得在）");
const rm = fnSrc("renderMsg");
assert(rm.includes("DOC_MSGS"), "renderMsg 要在提前返回之前把消息存进 DOC_MSGS（实时视图没有对话墙）");
const _a = rm.indexOf("docFloatRender()"), _b = rm.indexOf("if(view!=='chat')");
assert(_a >= 0 && _b >= 0 && _a < _b,
  "renderMsg 判浮条要放在「非聊天视图提前返回」之前，不然实时视图永远判不到");
assert(fnSrc("docFloatPick").includes("DOC_MSGS"), "没有对话墙时 docFloatPick 要退回看 DOC_MSGS");
assert(page.includes('id="docBar"') && page.includes("docFloatOpen()"), "页面缺文档浮条");
assert(page.includes("docFloatClose()"), "浮条要能划走");

// 12) 实时视图实测：对话墙是空的（feedMsgs 没被 renderFeed 填过），浮条照样浮
const bar2 = { on: false, classList: { add() { bar2.on = true; }, remove() { bar2.on = false; } } };
const els2 = { docBar: bar2, docBarIc: { textContent: "" },
  docBarName: { textContent: "" }, docBarMeta: { textContent: "" } };
const api2 = built(esc,
  { getElementById: (id) => els2[id] || { value: "", classList: { add() {}, remove() {} } } },
  () => {}, () => {}, () => {}, () => {}, "", "",
  localStorage, [], "s2", 1, {}, () => {}, () => {});
const now = { role: "assistant", text: "固件方案写好了：docs/superpowers/plans/fw-plan.md" };
api2.setDoc([now]);
api2.docFloatRender();
assert.equal(bar2.on, true, "实时/原样视图没有对话墙，浮条也必须浮出来");
assert.equal(els2.docBarName.textContent, "fw-plan.md", "浮条要显示文档名");
api2.docMarkSeen("docs/superpowers/plans/fw-plan.md");
api2.docFloatRender();
assert.equal(bar2.on, false, "实时视图里看过/划掉的同样不再浮");

// 13) 网页：agent 起好的本机网页服务才是「跑起来的样子」——消息里给了
//     http://localhost:5173 就把它排在浮条第一位，点开走 /pv/<端口>/（host 反代）；
//     同一句里提到的 .html 文件只是外壳，排后面。外部网址不许当成本机服务。
assert.deepEqual(api.docWebLinks("页面起好了：http://localhost:5173/"), ["/pv/5173/"]);
assert.deepEqual(api.docWebLinks("看 https://example.com/guide 就好"), [],
  "外部网址不是本机服务，不许塞进浮条");
assert.deepEqual(api.docWebLinks("127.0.0.1:3000/app 也行"), ["/pv/3000/app"]);

const bar3 = { on: false, classList: { add() { bar3.on = true; }, remove() { bar3.on = false; } } };
const els3 = { docBar: bar3, docBarIc: { textContent: "" },
  docBarName: { textContent: "" }, docBarMeta: { textContent: "" } };
const pv3 = [];
const api3 = built(esc,
  { getElementById: (id) => els3[id] || { value: "", classList: { add() {}, remove() {} } } },
  () => {}, () => {}, () => {}, () => {}, "", "",
  localStorage, [{ role: "assistant",
    text: "页面起好了：http://localhost:5173/ ，源码在 docs/proto/index.html" }],
  "s3", 1, {}, () => {}, (h) => pv3.push(h));
api3.docFloatRender();
assert.equal(bar3.on, true, "agent 起好了网页服务，浮条要浮出来");
assert.equal(els3.docBarName.textContent, "网页服务 :5173", "浮条先说清这是网页服务");
api3.docFloatOpen();
assert.deepEqual(pv3, ["/pv/5173/"], "点一下要直接进跑着的网页服务，不是贴文件");
assert.equal(bar3.on, true, "服务看过后，同一句里的 .html 还留着接着看");
assert.equal(els3.docBarName.textContent, "index.html");

console.log("doc cards ok");
