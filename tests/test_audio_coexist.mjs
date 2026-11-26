#!/usr/bin/env node
/* CodeBoss / AgentBoss 同装时的音量滑条（2026-11-21 用户：两个 App 别打架）。
   两家的「提醒音/语音播报」滑条写的是同一套系统音量：
   · 打开设置页时以系统真值为准（另一家改过，这里不能还显示自己的旧值）；
   · 另一家 App 正在整机静音时，拖这里的滑条不许把它的静音悄悄打开。
   跑法： node tests/test_audio_coexist.mjs */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import assert from "node:assert";

const here = dirname(fileURLToPath(import.meta.url));
const settings = readFileSync(
  join(here, "..", "host", "static", "src", "js", "06_settings.js"), "utf8");

function grab(name) {
  const m = settings.match(new RegExp(`function ${name}\\(\\)\\{[\\s\\S]*?\\n\\}`));
  assert(m, `找不到 ${name}()`);
  return m[0];
}

/* 搭一个最小环境：cfg、壳接口、滑条真值都从外面给 */
function mk(fns, opt) {
  opt = opt || {};
  const saved = [];
  const cbv = opt.cb === undefined ? { st: opt.st } : opt.cb;
  const env = {
    window: { CB: cbv },
    CB: cbv,
    muteOn: () => !!opt.mute,
    dndActive: () => !!opt.dnd,
    vsCfg: () => Object.assign({}, opt.cfg || {}),
    vsSave: (c) => saved.push(c),
    alertVol: () => (typeof (opt.cfg || {}).alertVol === "number" ? opt.cfg.alertVol : 60),
    speechVol: () => (typeof (opt.cfg || {}).speechVol === "number" ? opt.cfg.speechVol : 90),
    vsErr: () => {},
  };
  return { env, saved, run: (name) => new Function(
    ...Object.keys(env), fns[name] + `; return ${name};`)(...Object.values(env)) };
}

const fns = {
  vsVolAdopt: grab("vsVolAdopt"),
  sysMutedNotByUs: grab("sysMutedNotByUs"),
};
/* 壳的 sysAudioState() 原样返回它 */
const CB = (st) => ({ sysAudioState: () => JSON.stringify(st) });

/* ── 以系统为准：另一家设过音量，这边打开设置就跟着显示系统真值 ── */
let t = mk(fns, { cb: CB({ ring: 8, ringMx: 15, music: 4, musicMx: 15 }), cfg: { alertVol: 60, speechVol: 90 } });
t.run("vsVolAdopt")();
assert.equal(t.saved.length, 1, "系统音量明显不同时要采纳");
assert.deepEqual(
  { a: Math.round(t.saved[0].alertVol), s: Math.round(t.saved[0].speechVol) },
  { a: 53, s: 27 }, "按系统流的百分比回填两条滑条");

t = mk(fns, { cb: CB({ ring: 8, ringMx: 15, music: 4, musicMx: 15 }), cfg: { alertVol: 55, speechVol: 25 } });
t.run("vsVolAdopt")();
assert.equal(t.saved.length, 0, "差几个点（取整误差）不采纳，免得来回抖");

t = mk(fns, { cb: CB({ ring: 0, ringMx: 15, music: 0, musicMx: 15 }), cfg: { alertVol: 60, speechVol: 90 } });
t.run("vsVolAdopt")();
assert.equal(t.saved.length, 0, "两条流都 0（多半是另一家开着整机静音）不许把 0 采成用户的滑条");

t = mk(fns, { cb: CB({ ring: 8, ringMx: 15, music: 0, musicMx: 15 }), cfg: { alertVol: 60, speechVol: 90 } });
t.run("vsVolAdopt")();
assert.equal(t.saved.length, 1, "只有一条流有值时，那条也要采纳");
assert.equal(t.saved[0].alertVol !== undefined, true);
assert.equal(t.saved[0].speechVol, 90, "0 的那条不动（可能是另一家静音时的残留）");

t = mk(fns, { cb: CB({ ring: 8, ringMx: 15, music: 4, musicMx: 15 }), cfg: { alertVol: 60 }, mute: true });
t.run("vsVolAdopt")();
assert.equal(t.saved.length, 0, "自己在静音：系统是 0/被写死，不采纳");
t = mk(fns, { cb: CB({ ring: 8, ringMx: 15, music: 4, musicMx: 15 }), cfg: { alertVol: 60 }, dnd: true });
t.run("vsVolAdopt")();
assert.equal(t.saved.length, 0, "免打扰时段内不采纳");

t = mk(fns, { cb: null, cfg: { alertVol: 60 } });
t.run("vsVolAdopt")();
assert.equal(t.saved.length, 0, "浏览器/老壳没有 sysAudioState：什么都不做");

/* ── 另一家静的音，拖滑条不许替它打开 ── */
const mk2 = (st, mute, dnd) => mk(fns, { cb: CB(st), mute, dnd }).run("sysMutedNotByUs");
assert.equal(mk2({ ring: 0, ringMx: 15, music: 0, musicMx: 15 })(), true,
  "系统整机静音着、又不是我们要求的 → 别写");
assert.equal(mk2({ ring: 9, ringMx: 15, music: 0, musicMx: 15 })(), false,
  "系统没静音：照常写");
assert.equal(mk2({ ring: 0, ringMx: 15, music: 0, musicMx: 15 }, true)(), false,
  "是我们自己静的音：滑条怎么写都无所谓，别拦");
assert.equal(mk2({ ring: 0, ringMx: 15, music: 0, musicMx: 15 }, false, true)(), false,
  "免打扰是我们自己要的静音");

/* ── 接线 ── */
assert(/function vsVolSync\(\)\{[\s\S]{0,200}vsVolAdopt\(\)/.test(settings),
  "打开设置回填滑条时要先采纳系统真值");
assert(/function vsVolApplyRing\(\)\{[\s\S]{0,300}sysMutedNotByUs\(\)/.test(settings),
  "写铃声/通知音量前要看是不是另一家在静音");
assert(/function vsVolApplyMedia\(\)\{[\s\S]{0,300}sysMutedNotByUs\(\)/.test(settings),
  "写媒体音量前要看是不是另一家在静音");

console.log("audio coexist ok");
