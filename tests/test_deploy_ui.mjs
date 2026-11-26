#!/usr/bin/env node
/* 交付页（A4）接线：node tests/test_deploy_ui.mjs
   发布/回滚都要先确认；结果靠轮询健康卡；入口只在 codeboss 线出现。 */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import assert from "node:assert";

const here = dirname(fileURLToPath(import.meta.url));
const js = readFileSync(join(here, "..", "host", "static", "src", "js", "05_cmd.js"), "utf8");
const html = readFileSync(join(here, "..", "host", "static", "src", "page.html"), "utf8");
const acc = readFileSync(join(here, "..", "host", "static", "src", "js", "07_account.js"), "utf8");

assert(/id="depPage"/.test(html), "page.html 里没有交付子页");
for (const fn of ["deployRun()", "deployRollback()", "deployLoad(true)"]) {
  assert(html.includes(fn), "交付页缺少按钮/刷新接线：" + fn);
}
assert(/\/api\/health_card/.test(js), "deployLoad 没接 /api/health_card");
assert(/\/api\/deploy_run/.test(js), "deployRun 没接 /api/deploy_run");
assert(/\/api\/deploy_rollback/.test(js), "deployRollback 没接 /api/deploy_rollback");
assert(/askConfirm\('发布/.test(js), "发布没有二次确认");
assert(/askConfirm\('回滚/.test(js), "回滚没有二次确认");
assert(/dbBadge|depRows|depHealth/.test(js), "交付页没有渲染函数");
assert(/lineCaps\(cbLine\(\)\)\.deploy/.test(acc), "dpBtn 没有按 lineCaps 分线显隐");

console.log("test_deploy_ui: 11 assertions passed");
