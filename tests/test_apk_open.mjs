#!/usr/bin/env node
/* 「装 App」在 iPhone 上会把整个页面导航到 APK 地址（2026-11-19 反馈）：
   iOS 不认 application/vnd.android.package-archive → 主框架被换成一个打不开的
   页面，看起来就是「跳到断网了，过一会儿才恢复」。
   钉住：iOS 上先拦住；其它端用临时 <a download> 触发下载，不再 location.href。
   跑法：node tests/test_apk_open.mjs */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import assert from "node:assert";

const here = dirname(fileURLToPath(import.meta.url));
const js = readFileSync(join(here, "..", "host", "static", "src", "js", "05_cmd.js"), "utf8");

const m = /function apkOpen\(\)\{[\s\S]*?\n\}/.exec(js);
assert(m, "找不到 apkOpen()");
const body = m[0];

const iIos = body.search(/isIOS\(\)/);
const iFetch = body.indexOf("xj(");
assert(iIos >= 0, "apkOpen 没认 iOS：iPhone 上会把主页面导航走");
assert(iFetch >= 0 && iIos < iFetch, "iOS 判断必须在请求/下载之前");

assert(!/location\.href\s*=/.test(body),
  "不能用 location.href 导航到 APK：主框架会被换掉，iOS 上直接白屏/断网");

assert(/createElement\('a'\)/.test(body) && /\.download\b/.test(body)
  && /\.click\(\)/.test(body),
  "应该用临时 <a download> 触发下载（WebView 的 DownloadListener 接得住，页面不跳走）");

const ios = /function isIOS\(\)\{[\s\S]*?\n\}/.exec(js);
assert(ios && /iPhone|iPad|iPod/i.test(ios[0]), "isIOS() 要认 iPhone/iPad/iPod");

/* iOS 上按钮直接不显示，不是点了再提示一句 */
const html = readFileSync(join(here, "..", "host", "static", "src", "page.html"), "utf8");
const btn = /<button[^>]*id="kApkBtn"[^>]*>/.exec(html);
assert(btn && /apkOpen\(\)/.test(btn[0]), "「装 App」按钮要有 id=kApkBtn 且 onclick=apkOpen()");

const acc = readFileSync(join(here, "..", "host", "static", "src", "js", "07_account.js"), "utf8");
const init = /async function init\(\)\{[\s\S]*?\n\}/.exec(acc);
assert(init && /isIOS\(\)/.test(init[0]) && /kApkBtn/.test(init[0]),
  "init() 要在 iOS 上把 kApkBtn 藏起来");

console.log("test_apk_open: 7 assertions passed");
