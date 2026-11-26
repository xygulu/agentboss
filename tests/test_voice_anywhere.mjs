#!/usr/bin/env node
/* 语音输入泛化（2026-11-21 用户口径）：
   焦点在哪个输入框就写哪个框（搜索/派单/回退意见…）；焦点不在输入框时写会话输入框
   （盲操：任何页面说一句都落到最近的会话，按「自动发送」开关决定发不发）；
   密码/验证码这类敏感框一律不写。壳里 shim 的麦克风拿不到页面事件，所以目标在
   focusin 时就挂到 __cbPttTarget 上——shim 和页面两条结果通路都认它。
   跑法： node tests/test_voice_anywhere.mjs */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import assert from "node:assert";

const here = dirname(fileURLToPath(import.meta.url));
const js = (f) => readFileSync(join(here, "..", "host", "static", "src", "js", f), "utf8");
const ptt = js("08_ptt.js");
const page = readFileSync(join(here, "..", "host", "static", "src", "page.html"), "utf8");

/* ── 纯逻辑：哪个框能收语音 ── */
const m = ptt.match(/function voiceKind\(el\)\{[\s\S]*?\n  \}\n/);
assert(m, "找不到 voiceKind()");
const voiceKind = new Function(m[0] + "; return voiceKind;")();
const el = (tag, extra) => Object.assign(
  { tagName: tag, type: "text", readOnly: false, disabled: false,
    getAttribute: () => "" }, extra || {});
assert.equal(voiceKind(el("TEXTAREA")), "field", "多行输入框要能收语音");
assert.equal(voiceKind(el("INPUT")), "field", "普通文本框要能收语音");
assert.equal(voiceKind(el("INPUT", { type: "search" })), "field", "搜索框要能收语音");
assert.equal(voiceKind(el("INPUT", { type: "password" })), "sensitive", "密码框不许");
assert.equal(voiceKind(el("INPUT", { type: "tel" })), "sensitive", "电话框不许");
assert.equal(voiceKind(el("INPUT", { getAttribute: () => "numeric" })), "sensitive",
  "验证码（inputmode=numeric）不许");
assert.equal(voiceKind(el("INPUT", { readOnly: true })), "sensitive", "只读框不许");
assert.equal(voiceKind(el("INPUT", { type: "checkbox" })), "", "多选框不是文本输入");
assert.equal(voiceKind(el("DIV")), "", "不是输入框");
assert.equal(voiceKind(null), "", "空元素");

/* ── 接线：焦点变化挂目标、结果写任意框、玻璃窗（shim）也认 ── */
assert(/function insertIntoEl\(el, text\)/.test(ptt), "要能把结果写进任意输入框");
assert(/addEventListener\('focusin', voiceSyncFocus\)/.test(ptt),
  "焦点变化时要重新算目标");
assert(/__cbPttTarget\s*=\s*voiceTargetFn/.test(ptt),
  "目标要挂到 __cbPttTarget（壳的 shim.js 只认这个钩子）");
assert(/不支持语音输入/.test(ptt), "敏感框要明确拒绝，不能退回会话框把话发出去");
assert(/a\.id\s*!==\s*'txt'/.test(ptt), "会话框走老路（自动发送），不走目标钩子");

/* ── 麦克风：所有页面都能出现 ── */
assert(/#cbptt\{[^}]*display:flex/.test(page) || /body\.pg1 #cbptt\{display:flex\}/.test(page),
  "页面的麦克风不能只靠 pg1 才显示");
assert(!/body:not\(\.pg1\) #cbpttst\{display:none!important\}/.test(page),
  "状态条（录音中/已识别）在别的页面也要看得见");
assert(/html body #cbshim_mic\{[^}]*display:flex!important/.test(page),
  "安卓壳的麦克风（shim 注入）要在非会话页也能盖出来");

console.log("voice anywhere ok");
