#!/usr/bin/env node
/* 找会话（全文检索）的页面接线：工作台有入口，进去是输入框 + 结果，
   结果里点一下进那个会话；回车也能搜，空词不发请求。
   跑法： node tests/test_search_ui.mjs */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import assert from "node:assert";

const here = dirname(fileURLToPath(import.meta.url));
const js = (f) => readFileSync(join(here, "..", "host", "static", "src", "js", f), "utf8");
const cmd = js("05_cmd.js");
const settings = js("06_settings.js");

/* 入口 + 子页 */
assert(cmd.includes("data-ibfind"), "工作台要有搜索入口");
/* 2026-11-21 用户：输入框直接放工作台上，不用先点进子页再输入 */
assert(cmd.includes('id="ibQ"'), "工作台要有内联搜索输入框");
assert(/id==='ibQ'[\s\S]{0,200}FIND_Q/.test(settings), "工作台输入框回车要直接搜");
assert(cmd.includes("IB_VW==='find'"), "找会话是工作台的子页");
assert(cmd.includes("data-findgo"), "子页要有搜索按钮");
assert(cmd.includes("data-findsid="), "结果行要带会话 id");
assert(cmd.includes('/api/search?q='), "搜索走 /api/search");
assert(settings.includes("closest('[data-ibfind]')"), "点入口要进子页");
assert(settings.includes("closest('[data-findsid]')"), "点结果要进会话");
assert(/e\.target\.id==='findQ'/.test(settings), "回车也要能搜");

/* 结果高亮：先转义再包 <mark>，关键词含 HTML 也不能把标记带进来 */
const core = js("01_core.js");
const hlSrc = core.match(/function hlq\(s,q\)\{[\s\S]*?\n\}/);
assert(hlSrc, "找不到 hlq()");
const hlqF = new Function("escHtml",
  hlSrc[0] + "; return hlq;")(
  (s) => String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;"));
assert(hlqF("把登录页改成蓝色", "登录页").includes('<mark class="hl">登录页</mark>'),
  "命中的词要包 mark");
assert(!hlqF("<b>登录页", "登录页").includes("<b>"), "片段里的标签要先转义");
assert(!hlqF("登录页", "<i>").includes("<i>"), "关键词本身也要转义");
assert.equal(hlqF("什么也没有", "登录页"), "什么也没有", "没命中就原样");
assert(cmd.includes("hlq("), "结果片段要过高亮");

/* 别的机器上的命中：标机器名，点了走换台（本机没有那个会话） */
assert(cmd.includes("data-remote-dev="), "远程结果要带机器 id");
assert(cmd.includes("🖥 "), "远程结果要标机器名");
assert(settings.includes("devGoto(dev)"), "远程结果和台账共用换台路径");

/* 焦点还在工作台搜索框时左右滑到别的页：台账几秒重画一次会把焦点 focus 回来，
   页面就被拽回工作台（2026-11-21 用户）。滑走就收焦点，重画也只在框还在屏上时回填。 */
const visSrc = cmd.match(/function cbInputOnScreen\(el\)\{[\s\S]*?\n\}/);
assert(visSrc, "要能判断输入框还在不在屏幕上");
const vis = new Function("window", visSrc[0] + "; return cbInputOnScreen;")({ innerWidth: 400 });
assert.equal(vis({ getBoundingClientRect: () => ({ width: 100, height: 30, left: 10, right: 110 }) }),
  true, "在屏上的输入框");
assert.equal(vis({ getBoundingClientRect: () => ({ width: 100, height: 30, left: -390, right: -290 }) }),
  false, "滑到会话页后它在左边屏外，不该再回填焦点");
assert.equal(vis({ getBoundingClientRect: () => ({ width: 0, height: 0, left: 10, right: 10 }) }),
  false, "被藏起来的框");
assert.equal(vis(null), false, "没有元素");
assert(/const qFocus=[^;]*cbInputOnScreen\(/.test(cmd), "重画回填焦点前要看框还在不在屏上");
assert(/function blurOffscreenInput\(/.test(cmd), "滑走时要能收掉焦点");
assert(/blurOffscreenInput\(\)/.test(core), "翻页（goPage）时要收掉屏外输入框的焦点");

/* 输入栏要钉在底部（键盘弹起时页面整页上推，顶部输入框会被推出屏幕） */
assert(/id="findBar"[\s\S]{0,200}position:fixed/.test(cmd), "输入栏要固定");
assert(cmd.includes("var(--dockh,100px)"), "输入栏钉在底部 dock 之上");
assert(cmd.includes("getElementById('findQ')){"), "骨架只建一次，别每次轮询都换掉输入框");

/* 真跑一遍 findRun：带空格的关键词 -> 编码后请求；空词 -> 不发；
   重画只碰消息行和结果区（整块重画会丢焦点、键盘跟着收） */
const src = cmd.match(/async function findRun\(\)\{[\s\S]*?\n\}/);
assert(src, "找不到 findRun()");
const mk = () => new Function(
  "document", "xj", "findRender",
  "var FIND_Q='',FIND_ITEMS=[],FIND_AT=0,IB_VW='find';" + src[0] +
  "; return {findRun, st:()=>({FIND_Q,FIND_ITEMS,FIND_AT})};");

const calls = [];
const renders = [];
const el = { value: "  登录页 " };
const run = (value, items) => {
  el.value = value;
  return mk()(
    { getElementById: (id) => (id === "findQ" ? el : null) },
    async (url) => { calls.push(url); return { json: { items } }; },
    () => renders.push(1));
};

let env = run("  登录页 ", [{ sid: "S1", title: "改样式", snips: ["把登录页改成蓝色"] }]);
await env.findRun();
assert.equal(calls.length, 1);
assert.equal(calls[0], "/api/search?q=" + encodeURIComponent("登录页"), "关键词要去空格并编码");
assert.deepEqual(env.st().FIND_ITEMS.map((x) => x.sid), ["S1"], "结果要留下");
assert(env.st().FIND_AT > 0, "问过了才显示「没找到」而不是「还没搜」");
assert(renders.length === 2, "搜索前后各画一次结果区（不重建输入框）");

env = run("   ", []);
await env.findRun();
assert.equal(calls.length, 1, "空词不该发请求");

console.log("search ui ok");
