#!/usr/bin/env node
/* 产出物收件箱网格（批次三 Task 11）：渲染逻辑单测。
   跑法： node tests/test_art_grid.mjs */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import assert from "node:assert";

const here = dirname(fileURLToPath(import.meta.url));
const src = readFileSync(join(here, "..", "host", "static", "src", "js", "02_view.js"), "utf8");
const m = src.match(/\/\*ART_START\*\/([\s\S]*?)\/\*ART_END\*\//);
assert(m, "02_view.js 里找不到 ART 标记块");

const esc = s => String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;")
  .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
const fmtSz = n => n >= 1048576 ? (n / 1048576).toFixed(1) + "M"
  : n >= 1024 ? (n / 1024).toFixed(0) + "K" : n + "B";
const fmtTm = t => "11-18 20:0" + (t % 10);
const { fxArtHtml } = new Function("fmtSz", "fmtTm", "esc",
  m[1] + "; return {fxArtHtml};")(fmtSz, fmtTm, esc);

const d = (entries, truncated) => ({ entries, truncated: !!truncated });

// 图片：类型图标 + 文件名 + 大小 + 时间
let h = fxArtHtml(d([{ name: "图.png", rel: "图.png", size: 12288, mtime: 3, kind: "image" }]));
assert(h.includes("🖼"), "image 图标缺");
assert(h.includes("图.png"));
assert(h.includes("12K") && h.includes("11-18 20:03"), "大小/时间缺");
assert(!h.includes("fgrid\"></div>") && h.includes("fgrid"));

// 视频 / 文档 / 其它各有图标
assert(fxArtHtml(d([{ name: "a.mp4", rel: "a.mp4", size: 1, mtime: 1, kind: "video" }])).includes("🎬"));
assert(fxArtHtml(d([{ name: "a.pdf", rel: "a.pdf", size: 1, mtime: 1, kind: "doc" }])).includes("📄"));
assert(fxArtHtml(d([{ name: "a.bin", rel: "a.bin", size: 1, mtime: 1, kind: "other" }])).includes("📦"));
assert(fxArtHtml(d([{ name: "part.stl", rel: "part.stl", size: 1, mtime: 1, kind: "model" }])).includes("🧊"),
  "3D 模型要有自己的图标");
assert(fxArtHtml(d([{ name: "index.html", rel: "index.html", size: 1, mtime: 1, kind: "page" }])).includes("🌐"),
  "网页要有自己的图标");

// 子目录里的产出物：名字 + 所在目录，避免两张同名图分不清
h = fxArtHtml(d([{ name: "shot.png", rel: "out/shot.png", size: 1, mtime: 1, kind: "image" }]));
assert(h.includes("out"), "子目录名没显示");
assert(encodeURIComponent("out/shot.png").length && h.includes(encodeURIComponent("out/shot.png")));

// 截断提示
assert(fxArtHtml(d([{ name: "a.png", rel: "a.png", size: 1, mtime: 1, kind: "image" }], true))
  .includes("500"));

// 空收件箱说人话
assert(fxArtHtml(d([])).includes("还没产出"));

// 文件名注入必须转义
h = fxArtHtml(d([{ name: "<script>x</script>", rel: "<script>x</script>", size: 1, mtime: 1, kind: "other" }]));
assert(!h.includes("<script>x"), "文件名没转义");

console.log("art grid ok");
