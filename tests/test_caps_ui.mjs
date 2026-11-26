#!/usr/bin/env node
/* 自建中继 caps 降级自检：node tests/test_caps_ui.mjs
   caps[k]===false 才藏入口；caps 缺失/缺字段 = 官方全开（fail-open）。
   生成物约束：断言打在 ext.html（真实下发物）上，过期会被 build_page 抓。 */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import assert from "node:assert";

const here = dirname(fileURLToPath(import.meta.url));
const html = readFileSync(join(here, "..", "host", "static", "ext.html"), "utf8");
const page = readFileSync(join(here, "..", "host", "static", "src", "page.html"), "utf8");

/* capsOk / capsApply 必须存在，且 capsOk 的实现带 fail-open 分支 */
assert.ok(/function capsOk\(/.test(html), "ext.html 里没有 capsOk()：自建模式入口藏不掉");
assert.ok(/function capsApply\(/.test(html), "ext.html 里没有 capsApply()：caps 拿到了也没人执行");
const capsOkSrc = html.match(/function capsOk\([^)]*\)\{[\s\S]{0,400}?\}/);
assert.ok(capsOkSrc && /c\[k\]!==false/.test(capsOkSrc[0]),
  "capsOk 不是「仅 caps[k]===false 才关」的 fail-open 语义");

/* 品牌应答到达后必须跑一次 capsApply（cbBrand 成功回调里） */
assert.ok(/__cbBrand=d;\s*cbTitle\(\);[^}]*capsApply\(\)|__cbBrand=d;[\s\S]{0,200}?capsApply\(\)/.test(html),
  "cbBrand 拿到应答后没有调 capsApply()");

/* 五类入口各自挂了对应 caps 键 */
assert.ok((html.match(/capsOk\('plans'\)/g) || []).length >= 1, "套餐/读秒没挂 capsOk('plans')");
assert.ok((html.match(/capsOk\('tts'\)/g) || []).length >= 2,
  "语音播报没挂 capsOk('tts')（入口行 + voice_on 上报都要）");
assert.ok((html.match(/capsOk\('asr'\)/g) || []).length >= 1, "按住说话没挂 capsOk('asr')");
assert.ok((html.match(/capsOk\('apk'\)|\bapkok\b/g) || []).length >= 4,
  "TestFlight/更新区没挂 capsOk('apk')（vsVerRefresh 里至少四处闸）");

/* 被藏的入口元素必须有稳定 id（capsApply 按 id 藏） */
assert.ok(/id="trNVoice"/.test(page), "语音播报开关行没有 id=trNVoice，capsApply 藏不掉它");
assert.ok(/id="boxPlan"/.test(page) && /id="cbptt"/.test(page), "套餐卡/按住说话钮 id 丢失");
assert.ok(/capsApply[\s\S]{0,300}boxPlan/.test(html) || /boxPlan[\s\S]{0,300}capsApply/.test(html),
  "capsApply 没有覆盖套餐卡 boxPlan");

/* ASR 关掉时，手动按住说话给出明确提示（不静默） */
assert.ok(/未开通语音转写/.test(html), "ASR 被 caps 关掉时没有提示文案");

console.log("ok");
