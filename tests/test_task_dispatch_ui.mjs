#!/usr/bin/env node
/* 派单（B2）的页面接线：工作台顶部有入口，点了弹「agent / 目录 / 要干的事」，
   提交走 /api/task_new；派单中/失败的两态要能在台账徽章上看见。
   跑法： node tests/test_task_dispatch_ui.mjs */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import assert from "node:assert";

const here = dirname(fileURLToPath(import.meta.url));
const js = (f) => readFileSync(join(here, "..", "host", "static", "src", "js", f), "utf8");
const cmd = js("05_cmd.js");
const settings = js("06_settings.js");

/* 工作台入口 + 徽章 */
assert(cmd.includes("data-ibnew"), "工作台要有派单入口");
assert(cmd.includes("/api/task_new"), "派单提交走 /api/task_new");
const badgeSrc = cmd.match(/function ledgerBadge\(s\)\{[\s\S]*?\n\}/);
assert(badgeSrc, "找不到 ledgerBadge()");
const badge = new Function(badgeSrc[0] + "; return ledgerBadge;")();
assert.equal(badge("dispatching"), "派单中");
assert.equal(badge("failed"), "派单失败");

/* 委托：点入口开对话框；task:<tid> 的卡片没有会话可进，点了别乱跳 */
assert(/data-ibnew\]'\)\)\{ taskNewDlg\(\)/.test(settings), "点入口要调 taskNewDlg()");
assert(/indexOf\('task:'\)!==0/.test(settings), "派单行没有会话，不能 switchTo");

/* 真跑一遍对话框：填了内容 -> POST {content,cwd,agent}；空内容 -> 不发 */
const dlgSrc = cmd.match(/function taskNewDlg\(\)\{[\s\S]*?\n\}\n/);
const postSrc = cmd.match(/async function taskNewPost\(d\)\{[\s\S]*?\n\}/);
assert(dlgSrc && postSrc, "找不到 taskNewDlg()/taskNewPost()");
const els = { dlgBody: { value: "把登录页改蓝" }, dlgCwd: { value: "/p" }, dlgAg: { value: "claude" } };
const env = new Function("AGS", "askConfirm", "agentLabelOf", "escHtml", "document",
  "toast", "xj", "LED_AT", "ledgerLoad",
  dlgSrc[0] + postSrc[0] + "; return {taskNewDlg, taskNewPost};");
function mk(posted, toasts) {
  return env(
    [{ key: "claude", label: "Claude" }, { key: "codex", label: "Codex" }],
    (title, html, okText, danger, fn) => { mk.html = html; mk.fn = fn; },
    () => "",                     /* 没查到显示名就用配置里的 label */
    (s) => s,
    { getElementById: (id) => els[id] || null },
    (m, bad) => toasts.push([m, !!bad]),
    async (url, opt) => { posted.push([url, opt.body]); return { ok: true, json: { ok: true } }; },
    0, () => {});
}
const posted = [], toasts = [];
mk(posted, toasts).taskNewDlg();
assert(mk.html.includes("dlgBody") && mk.html.includes("dlgCwd"), "对话框要有内容和目录");
assert(mk.html.includes("Claude") && mk.html.includes("Codex"), "agent 要能从下拉里选");
mk.fn();
await new Promise((r) => setTimeout(r, 0));
assert.deepEqual(posted, [["/api/task_new", { content: "把登录页改蓝", cwd: "/p", agent: "claude" }]]);
assert(toasts.length === 1 && toasts[0][1] === false, "派完了要说一声");

els.dlgBody.value = "   ";
mk.fn.call(null);
assert.equal(posted.length, 1, "没内容不该发出去");

/* 跨机器（B2.1）：别的机器上的任务行——标出机器名，不给「收起/通过/打回」
   （本地记账对异机的 sid 无意义），点开是「切到那台机器」而不是进会话。 */
const acct = js("07_account.js");
assert(cmd.includes("data-remote-dev="), "远程行要带机器 id");
assert(cmd.includes("🖥 "), "远程行要标机器名");
assert(cmd.includes("(p||a||remote)?''"), "远程行不给收起/验收按钮");
assert(/closest\('\[data-remote-dev\]'\)/.test(settings), "点远程行要拦下来");
assert(settings.includes("devGoto(dev)"), "确认换台后走 devGoto");
const goto = acct.match(/function devGoto\(id\)\{[\s\S]*?\n\}\n/);
assert(goto && goto[0].includes("'/d?dev='"), "切台要进那台机器的列表（/d?dev=）");
assert(/function devSwitch\(sel\)\{\s*devGoto\(sel\.value\);\s*\}/.test(acct),
  "下拉切台和远程行共用同一条切台路径");

console.log("task dispatch ui ok");
