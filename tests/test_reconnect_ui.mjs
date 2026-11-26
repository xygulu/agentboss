#!/usr/bin/env node
/* 窗口核心 Phase 1 · Task 3：断链重连端到端（手机侧）。
   (A) loadListData 断网保缓存时置 listStaleAt、成功清零
   (B) renderList 在 stale 时渲染「离线缓存」横幅（降级可见，T3 之鉴）
   (C) pushFeed 的 hello 在掉线期过后触发 liveResync
   (D) liveResync 判 seq 倒退 → 整窗重画（force）+ 作废本地缓存；seq 前进 → 增量
   跑法： node tests/test_reconnect_ui.mjs */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import assert from "node:assert";

const here = dirname(fileURLToPath(import.meta.url));
const net = readFileSync(join(here, "..", "host", "static", "src", "js", "04_net.js"), "utf8");
const cmd = readFileSync(join(here, "..", "host", "static", "src", "js", "05_cmd.js"), "utf8");
const view = readFileSync(join(here, "..", "host", "static", "src", "js", "02_view.js"), "utf8");

function fnSrcFrom(src, name) {
  let i = src.indexOf("function " + name + "(");
  assert(i >= 0, "找不到 " + name);
  if (i >= 6 && src.slice(i - 6, i) === "async ") i -= 6;   /* 保留 async 前缀 */
  const s = src.indexOf("{", i);
  let d = 0;
  for (let k = s; k < src.length; k++) {
    if (src[k] === "{") d++;
    else if (src[k] === "}" && --d === 0) return src.slice(i, k + 1);
  }
  throw new Error("括号不配对：" + name);
}

const noop = () => {};
/* 04/05 的函数是 async——普通 new Function 装不下 await，用 AsyncFunction 构造器 */
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;

/* ---------- (A) loadListData ---------- */
function makeListEnv(xjStub, initialExt) {
  return new AsyncFunction("xj", "lastExt0",
    "var LIST_BUSY=false, LIST_AT=Date.now()-60000, lastWeb=[], lastExt=lastExt0, listStaleAt=0;\n" +
    "var DEV={init:true}, PLAND=null, CUR_AG='', AGS=null, AGS_T=0, SID='', curMeta=null;\n" +
    "var esc=function(x){return String(x);};\n" +
    "var refreshHeader=function(){}, renderList=function(){}, sessSave=function(){}," +
    " devLoad=function(){}, refreshModelCard=function(){}, agBarInit=function(){}," +
    " renderDetailLite=function(){};\n" +
    fnSrcFrom(cmd, "loadListData") + "\n" +
    ";return {go:loadListData, stale:function(){ return listStaleAt; }};")(xjStub, initialExt);
}

{
  const fail = () => ({ ok: false });
  const env = await makeListEnv(fail, [{ sid: "a", kind: "computer", cwd: "/x", last: 1 }]);
  await env.go();
  assert(env.stale() > 0, "(A) 断网保缓存：listStaleAt 要置位");

  const okExt = u => u.indexOf("ext_sessions") >= 0
    ? { ok: true, json: { sessions: [{ sid: "a", kind: "computer", cwd: "/x", last: 2 }] } }
    : { ok: true, json: { sessions: [] } };
  const env2 = await makeListEnv(okExt, [{ sid: "a", kind: "computer", cwd: "/x", last: 1 }]);
  await env2.go();
  assert(env2.stale() === 0, "(A) 成功回合：listStaleAt 清零");
  console.log("A ok");
}

/* ---------- (B) renderList 横幅 ---------- */
function makeRenderEnv(staleAt, listAt, ext) {
  let listHtml = "";
  const el = () => ({ style: {}, get innerHTML() { return listHtml; }, set innerHTML(v) { listHtml = v; } });
  const document = { getElementById: id => (id === "list" ? el() : el()) };
  const rows = ext || [{ sid: "a", kind: "computer", cwd: "/x", agent: "claude",
                          title: "t", state: "idle", last: 5 }];
  return { api: new Function("document", "staleAt", "listAt", "rows",
    "var listStaleAt=staleAt, LIST_AT=listAt, lastExt=rows, LIST_BUSY=false, CUR_AG='';\n" +
    "var AGS=[], DEV={init:true,na:false,list:[]}, SID='', NOTIFY={};\n" +
    "var escHtml=function(x){return String(x);}, ago=function(){return '刚刚';}," +
    " stText=function(){return '闲置';}, agentLabelOf=function(a){return a;}," +
    " sicChip=function(){return '';}, nbLabel=function(){return '';}," +
    " srcChip=function(){return '';};\n" +
    "var buildOpen=function(){}, ibPendLoad=function(){}, ibDoneLoad=function(){}," +
    " ledgerLoad=function(){};\n" +
    fnSrcFrom(cmd, "renderList") + "\n" +
    ";return renderList;")(document, staleAt, listAt, rows), html: () => listHtml };
}

{
  const a = makeRenderEnv(Date.now(), Date.now() - 120000);
  a.api();
  assert(a.html().indexOf("离线缓存") >= 0, "(B) stale 要渲染离线缓存横幅");
  assert(a.html().indexOf("2 分钟") >= 0, "(B) 横幅带数据年龄");

  const b = makeRenderEnv(0, Date.now());
  b.api();
  assert(b.html().indexOf("离线缓存") < 0, "(B) 在线时不渲染横幅");
  console.log("B ok");
}

/* ---------- (C) pushFeed hello 触发回拉 ---------- */
async function makePushEnv(wasDown) {
  let resyncs = 0;
  const api = await new AsyncFunction("liveResync",
    "var PUSH={gotHello:false, fail:2, why:'x', seq:-1, wasDown:" + (wasDown ? "true" : "false") + "};\n" +
    "var NOTIF_SEEN={}, NOTIF_MAXID=0;\n" +
    "var vsPushRefresh=function(){}, nwLoad=function(){return {last:0,at:0};}, nwSave=function(){};\n" +
    "var onNotify=function(){}, speakAfter=function(){}, voiceOn=false, bgOwns=false, vis=true;\n" +
    "var toast=function(){}, setSt=function(){}, pushReconnect=function(){}, pushDown=function(){};\n" +
    "var lastExt=[], LIST_AT=0, listStaleAt=0, sessSave=function(){}, curMeta=null, SID='';\n" +
    "var applyCaps=function(){}, refreshHeader=function(){}, renderList=function(){}," +
    " renderDetailLite=function(){}, renderMsg=function(){}, renderLive=function(){}," +
    " renderHist=function(){}, renderScreen=function(){}, setDraft=function(){};\n" +
    fnSrcFrom(net, "pushFeed") + "\n" +
    ";return {feed:pushFeed};")(function () { resyncs++; });
  return { api, resyncs: () => resyncs };
}

{
  const a = await makePushEnv(true);
  a.api.feed("hello", JSON.stringify({ seq: 9 }));
  assert(a.resyncs() === 1, "(C) 掉线期后的 hello 要触发 liveResync");
  assert(a.resyncs() === 1, "(C) wasDown 是一次性的");

  const b = await makePushEnv(false);
  b.api.feed("hello", JSON.stringify({ seq: 9 }));
  assert(b.resyncs() === 0, "(C) 没掉过线不触发");

  const c = await makePushEnv(true);
  c.api.feed("hello", JSON.stringify({ seq: 9 }));
  c.api.feed("hello", JSON.stringify({ seq: 10 }));
  assert(c.resyncs() === 1, "(C) 第二个 hello 不再触发");
  console.log("C ok");
}

/* ---------- (D) liveResync 判 seq 倒退 ---------- */
function makeResyncEnv(prevSeq, serverSeq) {
  const store = {};
  const ls = { getItem: k => (k in store ? store[k] : null),
               setItem: (k, v) => { store[k] = String(v); },
               removeItem: k => { delete store[k]; } };
  const calls = [];
  const windowObj = { __cbHistSeq: { sid: "s1", seq: prevSeq } };
  const api = new Function("window", "localStorage", "xj", "renderLive", "renderHist",
    "var LIVE_CK='codeboss_live_cache_v1', PF={}, pageIdx=1, view='live', SID='s1';\n" +
    fnSrcFrom(view, "liveResync") + "\n" +
    ";return liveResync;")(windowObj, ls,
    () => Promise.resolve({ ok: true, json: { html: "<x/>", hist: "", seq: serverSeq } }),
    (d, force) => calls.push(["live", force]), (d, force) => calls.push(["hist", force]));
  return { go: api, calls, store, windowObj };
}

{
  store_seed: {
    const env = makeResyncEnv(9, 2);            // 服务器重启过：seq 倒退
    env.store["codeboss_live_cache_v1"] = JSON.stringify(
      { m: { s1: { flow: "old", hist: "old", at: 1 }, s9: { flow: "k", hist: "k", at: 2 } } });
    await env.go();
    assert(env.calls.some(c => c[0] === "live" && c[1] === true), "(D) seq 倒退 → force 整窗重画");
    const saved = JSON.parse(env.store["codeboss_live_cache_v1"]);
    assert(!saved.m.s1, "(D) 本会话的旧缓存要作废");
    assert(saved.m.s9, "(D) 别的会话缓存不动");
    assert.deepEqual(env.windowObj.__cbHistSeq, { sid: "s1", seq: 2 }, "(D) 游标更新");
  }
  {
    const env = makeResyncEnv(9, 15);           // 正常增量：seq 前进
    await env.go();
    assert(env.calls.some(c => c[0] === "live" && c[1] === false), "(D) seq 前进 → 增量接续，不 force");
    assert(env.windowObj.__cbHistSeq.seq === 15, "(D) 游标更新");
  }
  console.log("D ok");
}

console.log("reconnect ui: all ok");
