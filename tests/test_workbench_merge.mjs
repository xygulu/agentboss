#!/usr/bin/env node
/* 工作台是「一条会话一行」：台账、待你决定、等你回话、刚完成四路数据
   按 sid 并成一条记录——同一件事在同一页出现两次就是没整合。
   跑法:node tests/test_workbench_merge.mjs */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import assert from "node:assert";

const here = dirname(fileURLToPath(import.meta.url));
const src = readFileSync(join(here, "..", "host", "static", "src", "js", "05_cmd.js"), "utf8");

function grab(name) {
  const m = src.match(new RegExp("function " + name + "\\([^)]*\\)\\{[\\s\\S]*?\\n\\}"));
  assert(m, "05_cmd.js 里找不到 " + name);
  return m[0];
}

const code = grab("ibRows") + "\n" + grab("ibRank");
function merge(led, pend, ask, done) {
  return new Function("LED_LIST", "PEND_LIST", "ASK_LIST", "DONE_LIST",
    code + "; return { ibRows, ibRank };")(led, pend, ask, done);
}

// 1) 台账行 + 同 sid 的结构化待决定 → 一条记录，两样信息都在
{
  const { ibRows, ibRank } = merge(
    [{ sid: "s1", title: "改登录", state: "deciding", last_ts: 100 }],
    [{ sid: "s1", title: "改登录", rid: "r1", ask: { action: "Bash", target: "rm -rf /tmp/x" } }],
    [], []);
  const rows = ibRows();
  assert.equal(rows.length, 1, "同一 sid 在台账和待决定里各出现一次，必须并成一行");
  assert(rows[0].led && rows[0].pend, "合并行必须同时带台账和待决定信息");
  assert.equal(ibRank(rows[0]), 0, "待决定排最前");
}

// 2) 只有一句 act 的 perm → 归「等你回话」；ASK_LIST 同 sid 不重复
{
  const { ibRows, ibRank } = merge(
    [{ sid: "s2", title: "跑测试", state: "deciding", last_ts: 200 }],
    [{ sid: "s2", title: "跑测试", ask: {}, act: "要继续吗" }],
    [{ sid: "s2", act: "要继续吗" }, { sid: "s3", act: "另一个问题" }],
    []);
  const rows = ibRows();
  assert.equal(rows.length, 2, "s2 只许一行；s3 台账没有也要出现");
  const s2 = rows.find((r) => r.sid === "s2");
  assert(s2.ask && !s2.pend, "无 rid/action/target 的候选项归「等你回话」");
  assert.equal(ibRank(s2), 1);
  assert.equal(ibRank(rows.find((r) => r.sid === "s3")), 1);
}

// 3) 刚完成挂到台账行上；台账「办结待验收」排在待决定/回话之后
{
  const { ibRows, ibRank } = merge(
    [{ sid: "s4", title: "发版", state: "done", last_ts: 300 },
     { sid: "s5", title: "在跑", state: "running", last_ts: 400 }],
    [], [],
    [{ sid: "s4", text: "✅ 已发布 v1", ts: 300 }]);
  const rows = ibRows();
  assert.equal(rows.length, 2);
  const s4 = rows.find((r) => r.sid === "s4");
  assert(s4.done && s4.led, "刚完成的文字要挂到同一行");
  assert.equal(ibRank(s4), 2, "办结待验收：要人出手，但排在问话之后");
  assert.equal(ibRank(rows.find((r) => r.sid === "s5")), 3, "普通任务最后");
}

// 4) 一行只能有一个类别：有问话（回复类）就不再摆「通过 / 打回」（审批类）
{
  const cardCode = [grab("ibCard"), grab("ledgerActions"), grab("ledgerBadge"), grab("fmtLedgerDur")].join("\n");
  const mkCard = new Function("escHtml", "agentLabelOf", "ago", "pendPick",
    cardCode + "; return ibCard;")((x) => String(x), (k) => k, () => "刚刚", () => []);
  const askDone = mkCard({ sid: "s1", led: { sid: "s1", state: "done", title: "发版" },
                           ask: { act: "要继续吗" } });
  assert(askDone.includes("等你回话"), "有问话就归「等你回话」");
  assert(!askDone.includes("通过"), "同一行又给问话又给通过/打回：用户不知道是回复还是审批");
  const doneOnly = mkCard({ sid: "s2", led: { sid: "s2", state: "done", title: "发版" } });
  assert(doneOnly.includes("办结待验收") && doneOnly.includes("通过"), "没问话的办结才走审批");
  const pendDone = mkCard({ sid: "s3", led: { sid: "s3", state: "done", title: "发版" },
                            pend: { sid: "s3", rid: "r1", ask: { action: "Bash", target: "rm x" } } });
  assert(pendDone.includes("待你决定") && !pendDone.includes("通过"), "待决定就是待决定，不再叠审批");
}

// 4b) agent 品牌标：Claude 火花 / Codex（OpenAI）结 / OpenCode 方块；hermes 用首字母徽章
{
  const markSrc = src.slice(src.indexOf("const AG_MARK={"), src.indexOf("function agIcon("));
  assert(markSrc.length > 100, "找不到 AG_MARK");
  const ai = new Function("escHtml", markSrc + grab("agIcon") + "; return agIcon;")((x) => String(x));
  assert(ai("claude").includes("<svg") && ai("claude").includes("#d97757"), "Claude 用品牌色火花");
  assert(ai("codex").includes("<svg") && ai("codex").includes("#2fbf9b"), "Codex 用 OpenAI 结");
  assert(ai("opencode").includes("M22 24H2V0h20z"), "OpenCode 用它的方块标");
  const hm = ai("hermes");
  assert(hm.includes(">H<") && !hm.includes("<svg"), "没有公开品牌标的用首字母徽章，不编 logo");
  const cardCode2 = [grab("ibCard"), grab("ledgerActions"), grab("ledgerBadge"), grab("fmtLedgerDur")].join("\n");
  const card = new Function("escHtml", "agentLabelOf", "ago", "pendPick", "agIcon",
    cardCode2 + "; return ibCard;")((x) => String(x), () => "Claude", () => "刚刚", () => [], ai);
  assert(card({ sid: "s9", led: { sid: "s9", state: "running", title: "t", agent: "claude" } }).includes("<svg"),
    "工作台卡片上要带品牌图标");
}

// 5) 模型判没用的（keep=0）、用户收起的（hidden）不进主表
{
  const { ibRows } = merge(
    [{ sid: "k", title: "好任务", state: "running", last_ts: 1 },
     { sid: "b", title: "测试会话", state: "running", last_ts: 2, keep: 0, vwhy: "测试" },
     { sid: "h", title: "收起的", state: "running", last_ts: 3, hidden: 1 }],
    [], [], []);
  const rows = ibRows();
  assert.equal(rows.length, 1, "没价值的和收起的不许出现在主表");
  assert.equal(rows[0].sid, "k");
}

// 5b) 通过/打回过的任务从主表挪进「已验收」子页，主表留入口
{
  const { ibRows } = merge(
    [{ sid: "a", title: "已通过", state: "accepted", last_ts: 1 },
     { sid: "r", title: "已打回", state: "rejected", last_ts: 2 },
     { sid: "w", title: "待验收", state: "done", last_ts: 3 }],
    [], [], []);
  const rows = ibRows();
  assert.equal(rows.length, 1, "验收过的任务不该还占主表");
  assert.equal(rows[0].sid, "w");
}
assert(src.includes('data-ibview="acc"'), "要有个地方翻已通过/已打回的账");
assert(src.includes("IB_VW==='acc'"), "已验收要有自己的子页");
assert(!src.includes("IB_HID"), "旧的单标志已被 IB_VW 取代");

// 6) 收起/恢复/删除的入口都在页面上
assert(src.includes('data-ibview="hid"'), "主表要有「已收起 · 查看」入口");
assert(src.includes("function taskViewAct("), "隐藏/恢复/删除要真的调到 /api/task_view");
assert(src.includes("maybeTriage"), "台账刷新后要触发模型判断");

// 7) inboxPaint 只出一张列表：不再分「待你决定 / 等你回话 / 刚完成」三段
assert(src.includes("function ibRows(") && src.includes("function ibRank("),
  "合并逻辑必须是独立纯函数（ibRows/ibRank），不能埋在画页里");
assert(!src.includes("待你决定 · "), "页面上不该再有独立的「待你决定」分组标题");
assert(!src.includes("刚完成 · "), "页面上不该再有独立的「刚完成」分组标题");
assert(!src.includes("等你回话 · "), "页面上不该再有独立的「等你回话」分组标题");
assert(src.includes("ibRows()"), "inboxPaint 必须画合并后的行");

console.log("test_workbench_merge: passed");
