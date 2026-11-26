#!/usr/bin/env node
/* 圈选（批次三 Task 13）：裁剪几何 + 终端引用的纯逻辑。
   跑法： node tests/test_crop_quote.mjs */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import assert from "node:assert";

const here = dirname(fileURLToPath(import.meta.url));
const src = readFileSync(join(here, "..", "host", "static", "src", "js", "02_view.js"), "utf8");
const crop = src.match(/\/\*CROP_START\*\/([\s\S]*?)\/\*CROP_END\*\//);
const quote = src.match(/\/\*QUOTE_START\*\/([\s\S]*?)\/\*QUOTE_END\*\//);
assert(crop, "02_view.js 里找不到 CROP 标记块");
assert(quote, "02_view.js 里找不到 QUOTE 标记块");
const { cropBox, cropScale } = new Function(crop[1] + "; return {cropBox, cropScale};")();
const { cellsLines, quoteLines } = new Function(quote[1] + "; return {cellsLines, quoteLines};")();

// 显示 400×300，原图 1600×1200：框选坐标要 ×4 映射回原图
assert.deepEqual(cropBox({ sx: 100, sy: 50, ex: 300, ey: 200 }, 400, 300, 1600, 1200),
  { x: 400, y: 200, w: 800, h: 600 });

// 反向拖（从右下往左上）一样
assert.deepEqual(cropBox({ sx: 300, sy: 200, ex: 100, ey: 50 }, 400, 300, 1600, 1200),
  { x: 400, y: 200, w: 800, h: 600 });

// 拖出图片外：夹回原图边界
assert.deepEqual(cropBox({ sx: -50, sy: -50, ex: 500, ey: 400 }, 400, 300, 1600, 1200),
  { x: 0, y: 0, w: 1600, h: 1200 });

// 长边 ≤1024；小图不放大
assert.deepEqual(cropScale(2000, 1000), { w: 1024, h: 512 });
assert.deepEqual(cropScale(600, 400), { w: 600, h: 400 });

// cells（颜色游程）→ 一行纯文本，行尾空白去掉
const cells = [[["default", "default", false, false, false, false, "npm "],
                ["default", "default", false, false, false, false, "test   "]],
               [["default", "default", false, false, false, false, "  ok  "]]];
assert.deepEqual(cellsLines(cells), ["npm test", "  ok"]);

// 引用 a-b 行（1 基、含端点），带行号标记
assert.equal(quoteLines(["a", "b", "c", "d"], 2, 3), "[引用终端 2-3 行]\nb\nc\n");

// 反着选 / 越界都夹住
assert.equal(quoteLines(["a", "b", "c"], 3, 2), "[引用终端 2-3 行]\nb\nc\n");
assert.equal(quoteLines(["a", "b", "c"], 0, 99), "[引用终端 1-3 行]\na\nb\nc\n");

console.log("crop/quote ok");
