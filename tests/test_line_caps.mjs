#!/usr/bin/env node
/* 两条线的能力差异总开关：node tests/test_line_caps.mjs
   lineCaps 是纯映射：只决定默认视图和入口显隐，功能路由两条线都在
   （是体验差异不是权限边界）。未知产品线一律回落 codeboss。 */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import assert from "node:assert";

const here = dirname(fileURLToPath(import.meta.url));
const js = (f) => readFileSync(join(here, "..", "host", "static", "src", "js", f), "utf8");
const src = js("05_cmd.js");

function pick(name, file = src) {
  const m = file.match(new RegExp("function " + name + "\\([^)]*\\)\\{[\\s\\S]*?\\n\\}"));
  assert(m, "05_cmd.js 里找不到 " + name);
  return new Function(m[0] + "; return { " + name + " };")();
}

const { lineCaps } = pick("lineCaps");

const CODE = { ledgerDefault: false, testCards: true, deploy: true, artifactsInbox: false,
               dispatch: false, coding: true };
const AGENT = { ledgerDefault: true, testCards: false, deploy: false, artifactsInbox: true,
                dispatch: true, coding: false };

assert.deepEqual(lineCaps("codeboss"), CODE);
assert.deepEqual(lineCaps("agentboss"), AGENT);
assert.deepEqual(lineCaps(""), CODE, "未知/空产品线回落 codeboss");
assert.deepEqual(lineCaps("nonsense"), CODE, "脏产品线回落 codeboss");

// 派单是代理工头（经营面）的入口：工头线（编码面）看到它没有意义（2026-11-20 用户）。
// 搜索两条线都要（是导航/检索，不是经营）。同一个入口行里中间还夹着搜索输入框，
// 窗口放宽到 1200 字符。
const ent = src.match(/data-ibnew[\s\S]{0,1200}?data-ibfind/);
assert(ent, "工作台入口行找不到派单/搜索");
assert(/dispatch\)/.test(src) || /\.dispatch\b/.test(src), "派单入口没按 lineCaps 显隐");
const gate = src.slice(Math.max(0, (src.indexOf("data-ibnew") - 500)), src.indexOf("data-ibnew"));
assert(/dispatch/.test(gate), "派单入口前面要按 caps.dispatch 判断，不能两条线都出");

// 默认视图分线：开屏 codeboss 落会话列表，agentboss 落工作台（台账）
const acc = js("07_account.js");
assert(
  /listView\(lineCaps\(cbLine\(\)\)\.ledgerDefault\s*\?\s*'ib'\s*:\s*'sess'\)/.test(acc),
  "07_account.js 开屏默认页签没有按 lineCaps 分线"
);

// 「更新 App」的兜底下载：按本线取包——写死 /codeboss.apk 会让代理工头装上第二个口袋工头
const settingsSrc = js("06_settings.js");
assert(!settingsSrc.includes("location.href='/codeboss.apk'"), "更新兜底写死了 codeboss 的包");
assert(/location\.href='\/'\+cbLine\(\)\+'\.apk'/.test(settingsSrc), "更新兜底要按 cbLine() 取包");

console.log("test_line_caps: 10 assertions passed");
