#!/usr/bin/env node
/* 聊天墙 markdown 排版自检：node tests/test_md.mjs
   Codex 桌面版的回答是 md 原文，ext.html 以前直接 esc() 显示成 ** / # / ```。
   这里断言 mdToHtml 的排版结果和转义（任何输入都不能变成标签）。 */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import assert from "node:assert";

const here = dirname(fileURLToPath(import.meta.url));
const html = readFileSync(join(here, "..", "host", "static", "ext.html"), "utf8");
const m = html.match(/\/\*MD_START\*\/([\s\S]*?)\/\*MD_END\*\//);
assert(m, "ext.html 里找不到 MD 标记块");
const { mdToHtml } = new Function(m[1] + "; return {mdToHtml};")();

// 行内：粗体 / 斜体 / 行内代码 / 链接
assert.equal(mdToHtml("**粗** 和 *斜*"), "<p><b>粗</b> 和 <i>斜</i></p>");
assert.equal(mdToHtml("用 `npm i` 装"), "<p>用 <code>npm i</code> 装</p>");
assert.equal(mdToHtml("[官网](https://example.com)"), '<p><a href="https://example.com" onclick="return extOpen(this.href)">官网</a></p>');

/* 裸外网地址（agent 常把 https://… 直接写进回复，codex 2026-11-23 反馈点不开）：
   也变成可点的链接——壳的 WebView 不加载站外页，点了会没反应，要走 extOpen 交系统浏览器 */
assert.equal(mdToHtml("已部署：https://example.com/demo 请看"),
  '<p>已部署：<a href="https://example.com/demo" onclick="return extOpen(this.href)">https://example.com/demo</a> 请看</p>');
// 带查询串：& 转义后 href 里保持 &amp;（浏览器解码回 &）
assert.equal(mdToHtml("文档 https://docs.example.com/a?x=1&y=2。"),
  '<p>文档 <a href="https://docs.example.com/a?x=1&amp;y=2" onclick="return extOpen(this.href)">https://docs.example.com/a?x=1&amp;y=2</a>。</p>');
// 英文句尾的句点不算链接的一部分
assert.equal(mdToHtml("Visit https://example.com."),
  '<p>Visit <a href="https://example.com" onclick="return extOpen(this.href)">https://example.com</a>.</p>');
// URL 里的 path:line 不能把链接切碎（不能嵌套 <a>）
assert.equal(mdToHtml("看 https://x.com/src/app.py:12 这行"),
  '<p>看 <a href="https://x.com/src/app.py:12" onclick="return extOpen(this.href)">https://x.com/src/app.py:12</a> 这行</p>');
// 行内代码里的外网地址也不转（那是代码，不是入口）
assert.equal(mdToHtml("`https://example.com`"), "<p><code>https://example.com</code></p>");

// 行内代码里的 ** 不能被当强调
assert.equal(mdToHtml("`a**b**c`"), "<p><code>a**b**c</code></p>");

// 标题 / 引用 / 列表 / 分隔线
assert.equal(mdToHtml("## 小标题"), "<h2>小标题</h2>");
assert.equal(mdToHtml("> 引用一行"), "<blockquote>引用一行</blockquote>");
assert.equal(mdToHtml("- a\n- b"), "<ul><li>a</li><li>b</li></ul>");
assert.equal(mdToHtml("1. a\n2. b"), "<ol><li>a</li><li>b</li></ol>");
assert.equal(mdToHtml("---"), "<hr>");

// 代码块：保留原样、里面不转义成标签；流式输出中途没收尾也要收
assert.equal(mdToHtml("```js\nif (a < b) {}\n```"),
  "<pre><code>if (a &lt; b) {}</code></pre>");
assert.equal(mdToHtml("```\nvar x = 1\n"), "<pre><code>var x = 1</code></pre>");

// 段间空行 / 多段
assert.equal(mdToHtml("第一段\n\n第二段"), "<p>第一段</p><p>第二段</p>");

/* 会话里 agent 说起的本机网页地址（dev server / 生成的网页效果图）：换成同源
   /pv/<端口>/，手机上点得开（2026-11-19 3D 打印项目反馈）。外网链接不动。 */
assert.equal(mdToHtml("[效果图](http://localhost:3000/out/print.html)"),
  '<p><a href="/pv/3000/out/print.html" onclick="pvOpen(this.href);return false">效果图</a></p>');
assert.equal(mdToHtml("打开 http://127.0.0.1:5173/ 看看"),
  '<p>打开 <a href="/pv/5173/" onclick="pvOpen(this.href);return false">http://127.0.0.1:5173/</a> 看看</p>');
assert.equal(mdToHtml("http://localhost:8000"),
  '<p><a href="/pv/8000/" onclick="pvOpen(this.href);return false">http://localhost:8000</a></p>');
assert.equal(mdToHtml("http://localhost:3000/a?x=1&y=2"),
  '<p><a href="/pv/3000/a?x=1&amp;y=2" onclick="pvOpen(this.href);return false">http://localhost:3000/a?x=1&amp;y=2</a></p>');
// 中文句子里的地址：句号/逗号不能吞进链接
assert.equal(mdToHtml("看 http://localhost:3000。"),
  '<p>看 <a href="/pv/3000/" onclick="pvOpen(this.href);return false">http://localhost:3000</a>。</p>');
// 行内代码里的地址不转链接（那是代码，不是入口）
assert.equal(mdToHtml("`http://localhost:3000`"), "<p><code>http://localhost:3000</code></p>");
// 不是本机地址：作为普通外网链接可点
assert.equal(mdToHtml("http://example.com:3000"),
  '<p><a href="http://example.com:3000" onclick="return extOpen(this.href)">http://example.com:3000</a></p>');
// 域名里带 localhost 的不是本机地址，也不许冒充链接
assert.equal(mdToHtml("http://localhost.evil.com:3000"), "<p>http://localhost.evil.com:3000</p>");

// 注入：标签必须被转义
assert.equal(mdToHtml("<script>alert(1)</script>"),
  "<p>&lt;script&gt;alert(1)&lt;/script&gt;</p>");
assert.ok(!mdToHtml("**<img src=x onerror=1>**").includes("<img"));
// 非 http(s) 链接不生成 a 标签
assert.ok(!mdToHtml("[x](javascript:alert(1))").includes("<a "));

/* 生成的 onclick 指向的函数必须真存在（md 块单独 eval，抓不到拼装错误） */
assert.ok(/function pvOpen\(/.test(html), "ext.html 里没有 pvOpen()：点了本机网页地址会报错");
assert.ok(/function extOpen\(/.test(html), "ext.html 里没有 extOpen()：点了外网链接会没反应");

console.log("ok");
