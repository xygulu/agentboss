#!/usr/bin/env node
/* 播报不许被下一条截断（2026-11-20 用户：播报会突然截断，要讲完整）。
   老壳没有 CB.sayUrl 时页面自己放（03_voice.js playSay）：新的一句来了
   只能排队，不能 pause 掉正在念的那句。
   跑法： node tests/test_say_queue.mjs */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import assert from "node:assert";

const here = dirname(fileURLToPath(import.meta.url));
const voice = readFileSync(join(here, "..", "host", "static", "src", "js", "03_voice.js"), "utf8");

function fnSrc(name) {
  const i = voice.indexOf("function " + name + "(");
  assert(i >= 0, "03_voice.js 里找不到 " + name);
  const s = voice.indexOf("{", i);
  let d = 0;
  for (let k = s; k < voice.length; k++) {
    if (voice[k] === "{") d++;
    else if (voice[k] === "}" && --d === 0)
      return voice.slice(voice.indexOf("function " + name + "("), k + 1);
  }
  throw new Error("括号不配对：" + name);
}

const made = [];
class FakeAudio {
  constructor(u) { this.src = u; this.paused = false; made.push(this); }
  play() { return { catch() {} }; }
  pause() { this.paused = true; }
}

const { playSay } = new Function(
  "Audio",
  "var SAYAUD=null, SAYBUSY=false, SAYPEND=null;\n" + fnSrc("playSay") +
  "; return {playSay, st: () => ({busy: SAYBUSY, pend: SAYPEND})};"
)(FakeAudio);

let doneA = 0, doneB = 0, doneC = 0;

// 1) 第一句正常开念
assert.equal(playSay("a.mp4", () => doneA++), true);
assert.equal(made.length, 1, "第一句要立刻念");

// 2) 念到一半来第二句：不许打断、不许丢掉第一句
playSay("b.mp4", () => doneB++);
assert.equal(made.length, 1, "正在念的时候不能另起一个播放器（那就是截断）");
assert.equal(made[0].paused, false, "正在念的那句被 pause 掉了 = 突然截断");

// 3) 第一句念完：第二句接着念。第一句的 done 先别回调——
//    done 是给页面「可以开麦了」的信号，排队那句还在念就不能开
made[0].onended();
assert.equal(doneA, 0, "排队那句还在念，不能告诉页面「念完了」");
assert.equal(made.length, 2, "排队的第二句要接着念");
assert.equal(made[1].src, "b.mp4", "接着念的应当是排队那句");

// 4) 第二句放失败：队列空了，done 也得回调（不然页面一直等）
made[1].onerror();
assert.equal(doneB, 1, "播失败也要回调，页面不能一直等");

// 5) 队列空了：新的一句立刻就能念（不会被卡住）
playSay("c.mp4", () => doneC++);
assert.equal(made.length, 3, "队列清空后新的一句要立刻念");
made[2].onended();
assert.equal(doneC, 1, "队列空了念完就该回调 done");

// 6) 连来两句：只保留最新的一句排队（旧提醒已被新提醒覆盖）
playSay("d.mp4", () => {});
playSay("e.mp4", () => {});
assert.equal(made.length, 4, "还在念的时候都不能另起");
made[3].onended();
assert.equal(made.length, 5);
assert.equal(made[4].src, "e.mp4", "排队有多句时念最新的那句");

console.log("test_say_queue: 13 assertions passed");
