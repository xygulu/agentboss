#!/usr/bin/env node
/* 产出物里点源码文件（.py/.js/.sh…）不显示（2026-11-20 用户：py 文件打不开）。
   fviewShow 的 code 分支先 return 了，没把 #fview 浮层打开——文件内容填进去了，
   但浮层始终是隐藏的，看着就是"点了没反应"。文本/model 分支都有这一句。
   跑法： node tests/test_fv_code_open.mjs */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import assert from "node:assert";

const here = dirname(fileURLToPath(import.meta.url));
const src = readFileSync(join(here, "..", "host", "static", "src", "js", "02_view.js"), "utf8");
const m = src.match(/async function fviewShow\(path,name\)\{[\s\S]*?\n\}/);
assert(m, "找不到 fviewShow()");

const shown = [];
const els = {};
const el = (id) => (els[id] = els[id] || {
  style: {},
  classList: { add: (c) => c === "on" && shown.push(id), remove: () => {} },
  set textContent(v) {}, set innerHTML(v) {},
});
const document = { getElementById: el };
const view = new Function("document", "fvKind", "fvReviewKind", "fvNoteDone",
  "fviewCode", "fviewClose", "hasBridge", "sheetOpen",
  "var fvPath='';" + m[0] + "; return fviewShow;")(
  document, () => "code", () => false, () => {},
  (p, l) => { view.got = [p, l]; }, () => {}, () => false, () => {});

await view("relay/relay.py", "relay.py");
assert.deepEqual(view.got, ["relay/relay.py", 1], "源码分支要调用 fviewCode");
assert.ok(shown.includes("fview"), "源码预览要把 #fview 打开，不然看着就是打不开");
