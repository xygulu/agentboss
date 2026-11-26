#!/usr/bin/env node
/* 页面入口登记表门禁（2026-11-20）：每个 data-* 入口都必须登记「属于哪条线、为什么」。
   新增入口不登记 → 红；登记成单线的还必须和一个 lineCaps 能力位对上，防止表是摆设。
   跑法： node tests/test_page_entries.mjs */
import { readFileSync, readdirSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import assert from "node:assert";

const here = dirname(fileURLToPath(import.meta.url));
const jsDir = join(here, "..", "host", "static", "src", "js");
const files = readdirSync(jsDir).filter((f) => f.endsWith(".js"));
const srcOf = (f) => readFileSync(join(jsDir, f), "utf8");
// 注释里的提及不算入口（整段 /* */ 抹成等长空格，行号不变）
const strip = (s) => s.replace(/\/\*[\s\S]*?\*\//g, (m) => " ".repeat(m.length));

const cmd = strip(srcOf("05_cmd.js"));

/* ── 登记表 ── */
const m = cmd.match(/const PAGE_ENTRIES\s*=\s*\{[\s\S]*?\n\};/);
assert(m, "05_cmd.js 里没有 PAGE_ENTRIES 登记表");
const PAGE_ENTRIES = new Function(m[0].replace(/^const PAGE_ENTRIES\s*=\s*/, "return ") + "")();
/* 线名不写死：products/ 下有哪几条线，就只准登记哪几条（加线不用改这份测试） */
const prodDir = join(here, "..", "products");
const LINES = new Set(readdirSync(prodDir, { withFileTypes: true })
  .filter((d) => d.isDirectory()
    && readdirSync(join(prodDir, d.name)).includes("product.json"))
  .map((d) => d.name));
assert(LINES.has("codeboss") && LINES.has("agentboss"), "products/ 里找不到两条产品线");
const LINE_OF = { both: true };
for (const l of LINES) LINE_OF[l] = true;

/* 表本身要说得清：每条都有 lines + why；单线的必须挂能力位 */
for (const [k, e] of Object.entries(PAGE_ENTRIES)) {
  assert(LINE_OF[e.lines], `入口 ${k} 的 lines 只能是 both/` + [...LINES].join("/"));
  assert(e.why && String(e.why).trim(), `入口 ${k} 没写为什么`);
  if (e.lines !== "both") assert(e.cap, `单线入口 ${k} 要挂一个 lineCaps 能力位`);
}

/* ── 页面里出现的每个 data-* 都必须登记 ── */
const used = new Set();
for (const f of files) {
  for (const mm of strip(srcOf(f)).matchAll(/data-([a-zA-Z0-9_-]+)/g)) used.add(mm[1]);
}
const missing = [...used].filter((k) => !(k in PAGE_ENTRIES)).sort();
assert.equal(missing.length, 0,
  "这些入口没登记归属（新增入口请在 05_cmd.js 的 PAGE_ENTRIES 里写清哪条线、为什么）："
  + missing.join(", "));

/* 表里也不能留僵尸行 */
const dead = Object.keys(PAGE_ENTRIES).filter((k) => !used.has(k)).sort();
assert.equal(dead.length, 0, "登记了但页面里已经没有的入口（删掉）：" + dead.join(", "));

/* ── 单线入口的能力位必须和 lineCaps 对得上（表不能是摆设） ── */
const fn = cmd.match(/function lineCaps\([^)]*\)\{[\s\S]*?\n\}/);
assert(fn, "找不到 lineCaps()");
const lineCaps = new Function(fn[0] + "; return lineCaps;")();
for (const [k, e] of Object.entries(PAGE_ENTRIES)) {
  if (e.lines === "both") continue;
  const want = lineCaps(e.lines)[e.cap];
  const other = [...LINES].find((l) => l !== e.lines);
  assert.equal(want, true, `入口 ${k} 说自己属于 ${e.lines}，但 lineCaps 的 ${e.cap} 在那条线是 ${want}`);
  assert.equal(lineCaps(other)[e.cap], false,
    `入口 ${k} 只属于 ${e.lines}，但 lineCaps 的 ${e.cap} 在 ${other} 也是开的`);
}

console.log(`test_page_entries: ${Object.keys(PAGE_ENTRIES).length} 个入口登记齐全`);
