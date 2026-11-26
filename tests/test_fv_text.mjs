#!/usr/bin/env node
/* 文本预览（2026-11-19）：打开 JSON/MD/TXT 显示「bad token」的回归门禁。
   两个坑都必须钉住：
   ① 缓存参数不能叫 t —— 中继转发时会再补一个 &t=<host token>，require_token
      只读第一个 t（= 时间戳），于是一律 403 bad token；
   ② 请求要走 xj（会处理票据/直连/后台闸），裸 fetch 在老 WebView 上连 cookie 都不带。
   跑法： node tests/test_fv_text.mjs */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import assert from "node:assert";

const here = dirname(fileURLToPath(import.meta.url));
const src = readFileSync(join(here, "..", "host", "static", "src", "js", "02_view.js"), "utf8");
const m = src.match(/\/\*FVT_START\*\/([\s\S]*?)\/\*FVT_END\*\//);
assert(m, "02_view.js 里找不到 FVT 标记块");

let calls = [];
const xj = async (u) => {
  calls.push(u);
  return { ok: true, status: 200, text: '{"a":1}' };
};
const S = new Function("SID", "fvPath", "xj",
  m[1] + "; return {fvUrl, fvText};")("s 1", "out/a.json", xj);

/* ① URL 上不能有 t 参数；缓存参数用 _ */
const u = new URL("http://x" + S.fvUrl());
assert.equal(u.searchParams.get("t"), null, "缓存参数叫 t 会和 host token 撞名");
assert(u.searchParams.get("_"), "要有 _ 缓存参数，避免预览读到旧文件");
assert.equal(u.searchParams.get("sid"), "s 1");
assert.equal(u.searchParams.get("path"), "out/a.json");

/* ② 文本内容来自 xj 的响应 */
assert.equal(await S.fvText(), '{"a":1}');
assert.equal(calls.length, 1);
assert(calls[0].startsWith("/api/ext_artifact?"));

/* 403（bad token）时不能把错误正文当成文件内容贴出来 */
const bad = new Function("SID", "fvPath", "xj",
  m[1] + "; return {fvText};")("s", "a.json",
  async () => ({ ok: false, status: 403, text: "bad token" }));
await assert.rejects(bad.fvText(), (e) => /403/.test(e.message) && /bad token/.test(e.message));

/* 空文件给占位，不留空白面板 */
const empty = new Function("SID", "fvPath", "xj",
  m[1] + "; return {fvText};")("s", "e.txt",
  async () => ({ ok: true, status: 200, text: "" }));
assert.equal(await empty.fvText(), "(空)");

/* 文本分支不能再用裸 fetch */
const show = src.match(/async function fviewShow\(path,name\)\{[\s\S]*?\n\}/);
assert(show, "找不到 fviewShow()");
assert(show[0].includes("await fvText()"), "文本预览要走 fvText（内部是 xj）");
assert(!/await fetch\(/.test(show[0]), "预览层不该再出现裸 fetch");

console.log("fv text ok");
