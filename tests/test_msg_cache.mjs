#!/usr/bin/env node
/* 聊天墙本地缓存：关闭/召回后墙先显示上次看到的内容（缓存优先），
   host 真值回来逐条比对、一样不重画；没缓存才干等 host（2026-11-25 用户）。
   跑法： node tests/test_msg_cache.mjs */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import assert from "node:assert";

const here = dirname(fileURLToPath(import.meta.url));
const view = readFileSync(join(here, "..", "host", "static", "src", "js", "02_view.js"), "utf8");

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

function makeEnv(sid, prefill) {
  const store = {};
  const ls = {
    getItem: k => (k in store ? store[k] : null),
    setItem: (k, v) => { store[k] = String(v); },
    removeItem: k => { delete store[k]; },
  };
  let renders = 0;
  const toasts = [];
  const api = new Function(
    "localStorage", "SID", "feedMsgs", "renderFeed", "toast", "getRenders", "getToasts",
    "var MSG_CK='codeboss_msg_cache_v1', MSG_CK_N=4, MSG_CK_AT={}, MSG_CK_ARM={};\n" +
    [fnSrc("msgCacheMap"), fnSrc("msgCacheSave"), fnSrc("msgCachePrime"),
     fnSrc("msgCacheDisarm"), fnSrc("msgCacheOfflineNote")].join("\n") +
    "; return {msgCacheMap, msgCacheSave, msgCachePrime, msgCacheDisarm," +
    " msgCacheOfflineNote, feed: () => feedMsgs, renders: getRenders," +
    " toasts: getToasts, armed: () => MSG_CK_ARM};"
  )(ls, sid, prefill || [], () => { renders++; }, m => toasts.push(m),
    () => renders, () => toasts);
  return { api, store };
}

const M = n => Array.from({ length: n }, (_, i) => ({ role: i % 2 ? "assistant" : "user", text: "m" + i, tools: [] }));

/* 1. 存进去 → 播出来：召回（换页重进）墙立刻有上次的最近 300 条 */
{
  const { api, store } = makeEnv("s1");
  api.msgCacheSave("s1", M(350), true);
  assert(store["codeboss_msg_cache_v1"], "全量帧必须落盘");
  api.msgCachePrime("s1");
  assert.equal(api.feed().length, 300, "召回要先画上次的墙（裁到 300）");
  assert.equal(api.feed()[299].text, "m349", "留的是最新的尾巴");
  assert.equal(api.renders(), 1, "prime 要重画一次墙");
  assert(api.armed()["s1"], "显示的是缓存：离线提示要武装上");
}

/* 2. 只存最近 300 条：localStorage 不是仓库 */
{
  const { api, store } = makeEnv("s1");
  api.msgCacheSave("s1", M(400), true);
  const d = JSON.parse(store["codeboss_msg_cache_v1"]);
  assert.equal(d["s1"].m.length, 300, "每个会话最多留 300 条");
  assert.equal(d["s1"].m[299].text, "m399", "留的是最新的尾巴");
}

/* 3. LRU：最多 4 个会话，挤掉最旧的 */
{
  const { api, store } = makeEnv("s1");
  for (const s of ["s0", "s1", "s2", "s3"]) api.msgCacheSave(s, M(1), true);
  api.msgCacheSave("s4", M(1), true);
  const d = JSON.parse(store["codeboss_msg_cache_v1"]);
  assert.deepEqual(Object.keys(d).sort(), ["s1", "s2", "s3", "s4"], "最多留 4 个会话");
}

/* 4. 节流：15s 内的增量不落盘；force（全量帧）绕过节流 */
{
  const { api, store } = makeEnv("s1");
  api.msgCacheSave("s1", M(1), true);
  api.msgCacheSave("s1", M(2));            // 15s 内：跳过
  let d = JSON.parse(store["codeboss_msg_cache_v1"]);
  assert.equal(d["s1"].m.length, 1, "节流期内不落盘");
  api.msgCacheSave("s1", M(3), true);      // 全量帧：强制
  d = JSON.parse(store["codeboss_msg_cache_v1"]);
  assert.equal(d["s1"].m.length, 3, "force 绕过节流");
}

/* 5. 过期：24h 前的缓存不播 */
{
  const { api, store } = makeEnv("s1");
  api.msgCacheSave("s1", M(5), true);
  const d = JSON.parse(store["codeboss_msg_cache_v1"]);
  d["s1"].at = Date.now() - 25 * 3600 * 1000;
  store["codeboss_msg_cache_v1"] = JSON.stringify(d);
  api.msgCachePrime("s1");
  assert.equal(api.renders(), 0, "过期缓存不播");
}

/* 6. 让位：滑走了 / 真数据先到，缓存不许覆盖 */
{
  const { api } = makeEnv("s1");
  api.msgCacheSave("s1", M(5), true);
  api.msgCachePrime("s2");                       // 已滑到别的会话
  assert.equal(api.renders(), 0, "sid 对不上不播");
  const { api: b } = makeEnv("s1", M(9));        // 真数据先到
  b.msgCachePrime("s1");
  assert.equal(b.renders(), 0, "墙已有内容不覆盖");
  assert.equal(b.feed().length, 9);
}

/* 7. 坏数据不炸：缓存 JSON 被写坏 / 会话没存过，prime 当无事发生 */
{
  const { api, store } = makeEnv("s1");
  store["codeboss_msg_cache_v1"] = "{oops";
  api.msgCachePrime("s1");
  assert.equal(api.renders(), 0, "坏缓存不播也不炸");
  api.msgCachePrime("never-saved");
  assert.equal(api.renders(), 0);
}

/* 8. 离线提示：播了缓存且网真断了 → 吱一声且只吱一次；网没断（真帧到达 disarm）不吱 */
{
  const { api } = makeEnv("s1");
  api.msgCacheSave("s1", M(5), true);
  api.msgCachePrime("s1");
  api.msgCacheOfflineNote("s1");
  assert.equal(api.toasts().length, 1, "网断要吱一声");
  api.msgCacheOfflineNote("s1");
  assert.equal(api.toasts().length, 1, "只吱一次");
  const { api: b } = makeEnv("s1");
  b.msgCacheSave("s1", M(5), true);
  b.msgCachePrime("s1");
  b.msgCacheDisarm("s1");                        // 真帧到达
  b.msgCacheOfflineNote("s1");
  assert.equal(b.toasts().length, 0, "网没断不吱");
}

console.log("msg cache: 8/8 ok");
