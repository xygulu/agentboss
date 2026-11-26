#!/usr/bin/env node
/* 命令终端页（term.html）的深底改写自检：node tests/test_term_sgr.mjs
   背景：Codex/Claude TUI 会按宿主终端配色画亮底/反显；term.html 以前把原始
   pty 字节直接写进 xterm，手机上就是一条条白底/亮条。这里断言那套改写。 */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import assert from "node:assert";

const here = dirname(fileURLToPath(import.meta.url));
const html = readFileSync(join(here, "..", "host", "static", "term.html"), "utf8");
const m = html.match(/\/\*CB_SGR_START\*\/([\s\S]*?)\/\*CB_SGR_END\*\//);
assert(m, "term.html 里找不到 CB_SGR 标记块");
const { cbBeautify } = new Function(m[1] + "; return {cbBeautify};")();

const dec = (u8) => Buffer.from(u8).toString("latin1");
const enc = (s) => new TextEncoder().encode(s);
const once = (s) => dec(cbBeautify(enc(s)));

// 亮底（真彩 #f9f9f9）→ 拿掉背景 + 灰字
let r = once("\x1b[48;2;249;249;249m x \x1b[0m");
assert(r.includes("49;38;2;139;148;158"), JSON.stringify(r));

// 深底照旧
r = once("\x1b[48;2;11;14;20m x \x1b[0m");
assert(r.includes("48;2;11;14;20m"), JSON.stringify(r));

// 256 色近白（231）→ 灰；深色 234 不动
assert(once("\x1b[48;5;231m").includes("49;38;2;139;148;158"));
assert(once("\x1b[48;5;234m").includes("48;5;234m"));

// 反显 → 深色高亮条（别让 xterm 拿浅前景当底）
r = once("\x1b[7m选中\x1b[27m");
assert(r.includes("38;2;255;255;255;48;2;58;65;80"), JSON.stringify(r));
assert(r.includes("\x1b[27;39;49m"), JSON.stringify(r));

// dim → 灰字（xterm 不画 dim，不换就整行全亮）
r = once("\x1b[2mWorked for 5s\x1b[22m");
assert(r.includes("38;2;139;148;158"), JSON.stringify(r));
assert(r.includes("22;39"), JSON.stringify(r));

// 亮底命名色（47 = white）→ 灰；深底命名色（40）不动
assert(once("\x1b[47m").includes("49;38;2;139;148;158"));
assert(once("\x1b[40m").includes("[40m"));

// 控制序列被切在两个 chunk 之间：不能吐进屏幕内容里
let out = dec(cbBeautify(enc("abc\x1b[48;2;249")));
assert(!out.includes("249"), JSON.stringify(out));       // 尾巴被扣住
out = dec(cbBeautify(enc(";249;249mZ")));
assert(out.startsWith("\x1b[49;38;2;139;148;158m"), JSON.stringify(out));

// UTF-8 多字节不能被改写弄坏（按字节过一遍再原样拼回）
const zh = "你好\x1b[0m";
const zhOut = Buffer.from(cbBeautify(enc(zh))).toString("utf8");
assert(zhOut.startsWith("你好"), JSON.stringify(zhOut));

console.log("ok");
