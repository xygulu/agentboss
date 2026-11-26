#!/usr/bin/env node
/* 键盘弹起时「看得见的那一条」怎么算、焦点框怎么滚回去（2026-11-21 用户：
   焦点在搜索框时键盘把整页顶上去，输入框反而看不见了）。
   安卓壳把整页上推 --ime、iOS 缩 visualViewport：两种都要能把焦点框摆回可见带。
   跑法： node tests/test_keyboard_focus.mjs */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import assert from "node:assert";

const here = dirname(fileURLToPath(import.meta.url));
const js = (f) => readFileSync(join(here, "..", "host", "static", "src", "js", f), "utf8");
const net = js("04_net.js");
const acc = js("07_account.js");
const core = js("01_core.js");
const page = readFileSync(join(here, "..", "host", "static", "src", "page.html"), "utf8");

function pick(src, name) {
  const m = src.match(new RegExp("function " + name + "\\([^)]*\\)\\{[\\s\\S]*?\\n\\}"));
  assert(m, "04_net.js 里找不到 " + name);
  return new Function(m[0] + "; return " + name + ";")();
}
const kbBandCalc = pick(net, "kbBandCalc");
const kbScrollDelta = pick(net, "kbScrollDelta");

/* ── 可见带（和 getBoundingClientRect 同一坐标系） ──
   安卓壳整页上推 ime：页面坐标 0..ime 已经在屏幕外（会话页整页上推是同一套，
   列表页则把 body 缩到键盘以上，rect 里看还是 [ime, H]）。
   iOS 是 visualViewport 缩小 + 被系统滚走 offsetTop。 */
assert.deepEqual(kbBandCalc(0, 0, 800, 800), { top: 0, bot: 800 },
  "没键盘：整屏都是可见的");
assert.deepEqual(kbBandCalc(300, 0, 800, 800), { top: 300, bot: 800 },
  "安卓上推 300：页坐标 0..300 在屏幕外");
assert.deepEqual(kbBandCalc(0, 0, 500, 800), { top: 0, bot: 500 },
  "iOS 只缩可视区：键盘占掉底下 300");
assert.deepEqual(kbBandCalc(0, 60, 500, 800), { top: 60, bot: 560 },
  "iOS 被系统滚走 60：可见带整体下移");

/* ── 滚动多少 ── */
assert.equal(kbScrollDelta({ top: 600, bottom: 640 }, { top: 0, bot: 500 }), 152,
  "被键盘盖住：往上滚（152 = 640 - (500-12)）");
assert.equal(kbScrollDelta({ top: -100, bottom: -60 }, { top: 300, bot: 800 }), -412,
  "顶到屏幕外：往下滚（负值 = scrollTop 减小）");
assert.equal(kbScrollDelta({ top: 100, bottom: 140 }, { top: 0, bot: 500 }), 0,
  "看得见就别动");
assert.equal(kbScrollDelta({ top: 12, bottom: 60 }, { top: 0, bot: 500 }, 12), 0,
  "正好贴边（留白 12）不算被盖");
assert.equal(kbScrollDelta(null, { top: 0, bot: 500 }), 0, "没有元素不动");

/* ── 贴着可见带下沿的输入框不许被「摆正」（2026-11-21 iOS 会话页持续抖）──
   会话页输入条本来就是贴着键盘上沿摆的：r.bottom == band.bot（再差个内边距几个像素）
   永远在「留白 12」里面。按老算法每次都算出一笔正位移 → window.scrollBy → 又触发
   visualViewport scroll → kbSync → kbUnscroll 复位 → 再摆正……一秒钟抖好几下。 */
assert.equal(kbScrollDelta({ top: 800, bottom: 850 }, { top: 0, bot: 856 }, 12), 0,
  "贴着下沿的输入框（差 6px）是正常摆位，不是被盖住");
assert.equal(kbScrollDelta({ top: 800, bottom: 880 }, { top: 0, bot: 856 }, 12), 36,
  "真被盖住才滚：880 - (856-12) = 36，滚到位后正好停在留白线上");
/* 滚完就收敛：把内容上移 d 之后再看一次必须是 0，否则就是上面那条自激循环 */
{
  const befor = { top: 800, bottom: 880 }, band = { top: 0, bot: 856 };
  const d = kbScrollDelta(befor, band, 12);
  const after = { top: befor.top - d, bottom: befor.bottom - d };
  assert.equal(kbScrollDelta(after, band, 12), 0, "摆一次就到位，不能每帧都在挪");
}

/* ── 接线 ── */
assert(/function kbReveal\(\)/.test(net), "要能把焦点框滚回可见带");
assert(/function kbLayout\(\)/.test(net), "键盘开合时要按当前页面重排");
assert(/--kbtop/.test(net) && /--hime/.test(net), "重排要写 --kbtop / --hime");
assert(/revealSoon\(\)/.test(net.match(/function kbSync\(\)\{[\s\S]*?\n\}/)[0]),
  "可见区一变（键盘弹/收）就要摆正焦点框");
assert(/kbLayout\(\)/.test(acc.match(/window\.__imeShift=[\s\S]*?\n\};/)[0]),
  "壳报上推量后要重排");
assert(/classList\[ctrl\?'add':'remove'\]\('pg1'\)[\s\S]{0,400}kbLayout\(\)/.test(core),
  "会话页/其它页切换后要重排（上推量不变，摆法变了）");
assert(/addEventListener\('focusin'[\s\S]{0,300}revealSoon\(\)/.test(net),
  "点进输入框后也要摆正");

/* ── CSS：列表页把 body 缩到键盘以上、顶栏按模式补偿 ── */
assert(/body\{[^}]*top:var\(--kbtop,0px\)[^}]*height:calc\(var\(--vvh,100%\) - var\(--kbtop,0px\)\)/.test(page),
  "body 要按键盘高度收成「看得见的那一块」");
assert(/header\{[^}]*translateY\(var\(--hime,0px\)\)/.test(page),
  "顶栏补偿量要单独一个变量（列表页已经收过 body，不能再补一次）");

console.log("keyboard focus ok");
