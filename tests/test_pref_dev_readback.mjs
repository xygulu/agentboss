#!/usr/bin/env node
/* 评审修复 2：prefLoad 拉偏好要带 dev=——每设备编码模式刷新后得跟着回来。
   跑法： node tests/test_pref_dev_readback.mjs */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import assert from "node:assert";

const here = dirname(fileURLToPath(import.meta.url));
const src = readFileSync(join(here, "..", "host", "static", "src", "js", "03_voice.js"), "utf8");

const m = src.match(/function prefLoad\(\)\{[\s\S]*?\n\}/);
assert(m, "找不到 prefLoad");

let gotUrl = "";
const xj = function(url, opts){
  gotUrl = url;
  return Promise.resolve({ json: { ok: true, coding: { on: true } } });
};
const els = {
  HPREF: {}, CODING_ON: undefined,
  devId: function(){ return "dev-9"; },
  swSet: function(){}, hpReply: function(){}, hpNa: function(){},
  vsCfg: function(){ return {}; }, vsSave: function(){}, smartOn: function(){ return false; },
  nRender: function(){}, prefReport: function(){},
};
const prefLoad = new Function("xj", "HPREF", "CODING_ON", "devId", "swSet",
  "hpReply", "hpNa", "vsCfg", "vsSave", "smartOn", "nRender", "prefReport",
  m[0] + "; return prefLoad;");
prefLoad(xj, els.HPREP || els.HPREF, els.CODING_ON, els.devId, els.swSet,
  els.hpReply, els.hpNa, els.vsCfg, els.vsSave, els.smartOn, els.nRender, els.prefReport)(
  xj, els.HPREP || els.HPREF, els.CODING_ON, els.devId, els.swSet,
  els.hpReply, els.hpNa, els.vsCfg, els.vsSave, els.smartOn, els.nRender, els.prefReport);
await new Promise(r => setTimeout(r, 10));

assert.ok(gotUrl.indexOf("dev=") >= 0, "prefLoad 的 GET 要带 dev=（不带就只回共享值，每设备编码模式刷新即丢）");
assert.ok(gotUrl.indexOf("dev-9") >= 0, "dev= 要带本机 devId");

console.log("test_pref_dev_readback: 2 assertions passed");
