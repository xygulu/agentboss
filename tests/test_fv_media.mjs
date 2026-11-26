#!/usr/bin/env node
/* 媒体预览（批次三 Task 12）：扩展名 → 预览方式 / blob 的 MIME。
   错误的分类会把视频塞进 <img>（永远打不开），所以钉死。
   跑法： node tests/test_fv_media.mjs */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import assert from "node:assert";

const here = dirname(fileURLToPath(import.meta.url));
const src = readFileSync(join(here, "..", "host", "static", "src", "js", "02_view.js"), "utf8");
const m = src.match(/\/\*FVI_START\*\/([\s\S]*?)\/\*FVI_END\*\//);
assert(m, "02_view.js 里找不到 FVI 标记块");
const isCodeExt = e => ["py", "js", "ts", "html", "css", "json", "sh", "go"].includes(e);
const { fvKind, fvMime } = new Function("isCodeExt", m[1] + "; return {fvKind, fvMime};")(isCodeExt);

assert.equal(fvKind("png"), "image");
assert.equal(fvKind("JPEG"), "image");
assert.equal(fvKind("mp4"), "video");
assert.equal(fvKind("mov"), "video");
assert.equal(fvKind("mp3"), "audio");
assert.equal(fvKind("pdf"), "doc");
assert.equal(fvKind("docx"), "doc");
assert.equal(fvKind("md"), "text");
assert.equal(fvKind("py"), "code");
assert.equal(fvKind("stl"), "model");      /* 3D 模型：页面里自绘，不能塞给 <img> 或系统应用 */
assert.equal(fvKind("STL"), "model");
assert.equal(fvKind("html"), "page");      /* 网页：当页面打开，不是贴源码 */
assert.equal(fvKind("HTM"), "page");
assert.equal(fvKind("bin"), "other");

// 分块拼回的 blob 必须带对 MIME，否则 <video> 拒播
assert.equal(fvMime("mp4"), "video/mp4");
assert.equal(fvMime("webm"), "video/webm");
assert.equal(fvMime("png"), "image/png");

console.log("fv media ok");
