#!/usr/bin/env node
/* 台账行内产出物（B4）：node tests/test_ledger_arts_ui.mjs
   卡片上按 sid 显示「📎 产出物 n 件」，点了切到那个会话并打开产出物页；
   数据来自 B1 的 /api/artifact_inbox（已带 sid），不新开接口。 */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import assert from "node:assert";

const here = dirname(fileURLToPath(import.meta.url));
const cmd = readFileSync(join(here, "..", "host", "static", "src", "js", "05_cmd.js"), "utf8");
const st = readFileSync(join(here, "..", "host", "static", "src", "js", "06_settings.js"), "utf8");

assert(/data-arts="/.test(cmd), "台账卡片没有产出物入口");
assert(/ART_INBOX[\s\S]{0,40}filter\(/.test(cmd), "产出物没按 sid 过滤（会串到别的任务上）");
assert(/async function artsPeek\(sid\)/.test(cmd), "没有 artsPeek：点了不知道切会话+开文件页");
assert(/data-arts/.test(st), "data-arts 没接文档级委托（卡片在 #list 外，挂列表上收不到）");
assert(st.indexOf("data-arts") < st.indexOf("data-pend],[data-ledger]"), "产出物点击要先于「进会话」分支处理");

console.log("test_ledger_arts_ui: 5 assertions passed");
