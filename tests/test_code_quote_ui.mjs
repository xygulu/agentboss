#!/usr/bin/env node
/* 源码行引用（A3）：node tests/test_code_quote_ui.mjs
   点行号挑一行/一段 → 预填 [引用 path:120-135] + 原文。
   行号是全局行号，翻页后 from>1 也要对；越界钳到本页范围。 */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import assert from "node:assert";

const here = dirname(fileURLToPath(import.meta.url));
const src = readFileSync(join(here, "..", "host", "static", "src", "js", "02_view.js"), "utf8");

function pick(name) {
  const m = src.match(new RegExp("function " + name + "\\([^)]*\\)\\{[\\s\\S]*?\\n\\}"));
  assert(m, "02_view.js 里找不到 " + name);
  return new Function(m[0] + "; return { " + name + " };")()[name];
}
const codeQuoteText = pick("codeQuoteText");

const L = ["int main(){", "  return 0;", "}"];
assert.equal(
  codeQuoteText("a.c", 1, 2, 2, L),
  "[引用 a.c:2-2]\n  return 0;\n",
  "单行引用格式不对"
);
assert.equal(
  codeQuoteText("a.c", 1, 3, 1, L),
  "[引用 a.c:1-3]\nint main(){\n  return 0;\n}\n",
  "反向挑（先点后行）没有交换成 1-3"
);
assert.equal(
  codeQuoteText("a.c", 10, 11, 99, L),
  "[引用 a.c:11-12]\n  return 0;\n}\n",
  "翻页后起始行不是 1 时行号/切片算错了，越界没有钳到本页"
);
assert.equal(
  codeQuoteText("a.c", 1, 0, 0, L),
  "[引用 a.c:1-1]\nint main(){\n",
  "0 或负行号没有钳到第一行"
);

// 接线断言：行号可点、点了走 codeQuoteTap，引用结果落到输入框
assert(/data-cn=/.test(src), "代码行号没挂 data-cn，点不到具体行");
assert(/onclick="codeQuoteTap\(/.test(src), "点行号没有接到 codeQuoteTap");
assert(/document\.getElementById\('txt'\)[\s\S]{0,120}value=t\+/.test(src), "引用结果没有预填到输入框");

console.log("test_code_quote_ui: 7 assertions passed");
