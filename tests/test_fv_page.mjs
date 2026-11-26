#!/usr/bin/env node
/* 网页：用户关心的是效果，优先预览，没有预览才看源码（2026-11-20 用户定的口径）。
   以前 .html 在文件浮层里走 /pvfile 直接渲染——点 ext.html 等于把 App 自己
   又启动一遍，看着就是「打不开」。现在：
   - fvKind 仍把 html 归 page，但 page 分支先问 /api/ext_web：
     已经在跑的服务答得出这个文件就用它，没有就起静态服务，然后跳预览查看页；
   - 预览起不来（接口失败）才在同一个浮层里退回源码；
   - 电脑上打开的网页（file://）仍走 /pvfile，那是浏览器地址，不是产出物。
   跑法： node tests/test_fv_page.mjs */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import assert from "node:assert";

const here = dirname(fileURLToPath(import.meta.url));
const js = (f) => readFileSync(join(here, "..", "host", "static", "src", "js", f), "utf8");

const view = js("02_view.js");
const show = view.match(/async function fviewShow\(path,name\)\{[\s\S]*?\n\}/);
assert(show, "找不到 fviewShow()");
assert(show[0].includes("kind==='page'"), "网页要有「优先预览」的分支");
assert(!show[0].includes("/pvfile/"), "点击产出物不该把文件当页面直接渲染（App 自启动那口锅）");
assert(show[0].includes("/api/ext_web"), "网页预览要先找/起服务（/api/ext_web）");
const page = show[0].match(/kind==='page'\)\{([\s\S]*?)return;/);
assert(page, "找不到网页分支");
assert(page[1].includes("fviewCode("), "预览起不来的兜底是源码");
assert(page[1].includes("sheetOpen("), "预览在浮层里打开");

/* html 归 page：抽 CODE_EXT/isCodeExt/fvKind 真跑一遍 */
const kindSrc = view.match(/const CODE_EXT=\[[\s\S]*?function fvKind\(ext\)\{[\s\S]*?\n\}/);
assert(kindSrc, "找不到 fvKind()");
const kindOf = new Function(kindSrc[0] + "; return fvKind;")();
assert.equal(kindOf("html"), "page", "网页优先给效果（预览）");
assert.equal(kindOf("htm"), "page");
assert.equal(kindOf("py"), "code");

/* 源码页里「看效果」进预览：服务端 /api/ext_web?open=1 直接 302 到查看页 */
const code = view.match(/async function fviewCode\(path,line\)\{[\s\S]*?\n\}/);
assert(code && code[0].includes("/\\.html?$/i"), "源码页要识别 html");
const tap = view.match(/function fvPreviewTap\(\)\{[\s\S]*?\n\}/);
assert(tap, "找不到 fvPreviewTap()");
assert(tap[0].includes("/api/ext_web?open=1"), "看效果要交给 /api/ext_web（复用/起服务）");
assert(tap[0].includes("sheetOpen("), "效果在预览浮层里看");

const cmd = js("05_cmd.js");
const prev = cmd.match(/function previewOpen\(\)\{[\s\S]*?\n\}/);
assert(prev, "找不到 previewOpen()");
assert(prev[0].includes("'/pv?sid='"), "预览列表要带 sid（file:// 页面按会话目录解析）");

// 浮条/正文里的本机网页服务：要在可返回的浮层里开，不能整页跳走
// （2026-11-20 用户：点了就全屏了，返回不回来）。sheetOpen 只认和中继同源的
// 绝对地址，拿 /pv/... 这种相对地址过去会被判成跨源、直接 location.href 跳走。
const pv = cmd.match(/function pvOpen\(href\)\{[\s\S]*?\n\}/);
assert(pv, "找不到 pvOpen()");
assert(pv[0].includes("sheetOpen("), "本机网页服务要在浮层里开（有返回来路）");
assert(pv[0].includes("location.origin"),
  "相对地址要先补成绝对地址：不然 sheetOpen 判成跨源，整页跳走就回不来了");
const opened = [];
const call = new Function("sheetOpen", "location", pv[0] + "; return pvOpen;")(
  (u, t) => opened.push([u, t]),
  { origin: "http://h:8790", href: "" });
assert.equal(call("/pv/8765/"), undefined);
assert.deepEqual(opened, [["http://h:8790/pv/8765/", "预览"]],
  "点浮条里的网页服务，要盖一层 iframe 打开（同一个「预览」浮层）");

// 返回：服务页 → 端口列表 → 收起浮层。连点两下时 iframe 还没走完，
// 第二次读到的还是旧路径——要有标记兜住，不然看着像退不出来。
const acct = js("07_account.js");
const sc = acct.match(/function sheetClose\(\)\{[\s\S]*?\n\}\n/);
assert(sc, "找不到 sheetClose()");
function sheetEnv(path) {
  const f = { src: "", contentWindow: { location: { pathname: path } } };
  const s = { on: true, classList: { add() { s.on = true; }, remove() { s.on = false; } } };
  const doc = { getElementById: (id) => (id === "cbSheet" ? s : id === "cbSheetF" ? f : { textContent: "" }) };
  const close = new Function("document", sc[0] + "; return sheetClose;")(doc);
  return { f, s, close };
}
const env = sheetEnv("/pv/8765/");
env.close();
assert.equal(env.f.src, "/pv", "返回先回端口列表，不要一下收起落回会话页");
assert.equal(env.s.on, true);
env.close();                                   /* 连点：iframe 还没加载完 */
assert.equal(env.s.on, false, "连点两下返回要能退出浮层，不能卡住");

console.log("fv page ok");
