// SPDX-License-Identifier: AGPL-3.0-or-later

// 直接抽取 APK 里那份 shim.js 的 fill()，用桩 DOM 跑真实的追加逻辑。
// 目的：验证「识别结果只追加、不覆盖」，以及幽灵结果的去重。
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');

// 抠出 fill() 及其依赖（lastFill）
const m = src.match(/var lastFill = \{[\s\S]*?\n  \}\n  function doSend/);
if (!m) { console.log('FAIL: 找不到 fill()'); process.exit(1); }
const body = m[0].replace(/function doSend$/, '');

const dialog = {};
function makeTa(v) {
  return {
    value: v,
    selectionStart: 0, selectionEnd: 0,
    listeners: [],
    dispatchEvent(e) { this.listeners.push(e.type); },
    focus() { dialog.activeElement = this; }
  };
}
const ta = makeTa('');
global.document = { getElementById: (id) => (id === 'txt' ? ta : null), activeElement: null };
global.window = {};
global.Event = function (t) { this.type = t; };

const fill = new Function('document', 'window', 'Event', body + '\nreturn fill;')(global.document, global.window, global.Event);

const cases = [];
function t(name, before, text, expectVal, expectRet) {
  ta.value = before;
  const r = fill(text);
  const ok = (ta.value === expectVal) && (r === expectRet);
  cases.push({ name, ok, got: JSON.stringify(ta.value), ret: r });
}

// 1 首次：空框 -> 填入
t('空框填入', '', '第一句话。', '第一句话。', 'ok');
// 2 第二次（不同内容）：追加，不覆盖  ← 本次修复的目标
t('二次追加不覆盖', '第一句话。', '第二句话。', '第一句话。第二句话。', 'ok');
// 3 第三次继续追加
t('三次追加', '第一句话。第二句话。', '第三句。', '第一句话。第二句话。第三句。', 'ok');
// 4 幽灵录音返回同一句：丢弃，不重复追加
ta.value = '第一句话。'; fill('别的。');     // 先垫一次，重置去重窗口
ta.value = '第一句话。别的。';
const dup = fill('别的。');
cases.push({ name: '重复结果丢弃', ok: dup === 'dup' && ta.value === '第一句话。别的。', got: JSON.stringify(ta.value), ret: dup });
// 5 中英混排才补空格
t('英文接英文补空格', 'hello', 'world', 'hello world', 'ok');
t('中文相接不补空格', '你好', '世界', '你好世界', 'ok');
// 6 空白结果不写
t('空结果拒写', '保留我', '   ', '保留我', 'empty');
// 7 没有输入框时返回 noinput（不崩）
const saved = global.document.getElementById;
global.document.getElementById = () => null;
const r7 = fill('x');
cases.push({ name: '无输入框返回 noinput', ok: r7 === 'noinput', got: '-', ret: r7 });
global.document.getElementById = saved;

let bad = 0;
for (const c of cases) {
  console.log((c.ok ? 'PASS  ' : 'FAIL  ') + c.name + '  -> value=' + c.got + ' ret=' + c.ret);
  if (!c.ok) bad++;
}
console.log(bad === 0 ? '\n全部通过（' + cases.length + ' 项）' : '\n失败 ' + bad + ' 项');
process.exit(bad === 0 ? 0 : 1);
