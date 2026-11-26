// SPDX-License-Identifier: AGPL-3.0-or-later

/* 手机端会话墙的两个自检：媒体卡数量 + 「全量内容有没有变」的判断。
   直接从 static/ext.html 抠出真实函数跑，不另写一份。
   跑法：node tools/test_media_feed.js [ext.html]

   盯的是这两条真机反复出问题的链路：
     ① 一条消息里 7 个视频，app 上只出 3 个卡（mediaCards 以前硬砍到 3 个）
     ② 一轮里全是工具调用时，墙冻住不动——旧签名（条数+首条前40字+末条后40字）
        看不见中间的改动，host 明明推了新消息，app 却当没变（用户：桌面版里一大段，
        app 上后面的内容都没有）
 */
const fs = require('fs');
const path = require('path');

const SRC = fs.readFileSync(process.argv[2] ||
  path.join(__dirname, '..', 'static', 'ext.html'), 'utf8');

function extract(name) {
  const at = SRC.indexOf('function ' + name + '(');
  if (at < 0) throw new Error('missing function ' + name);
  const i = SRC.indexOf('{', at);
  let depth = 0;
  for (let j = i; j < SRC.length; j++) {
    if (SRC[j] === '{') depth++;
    else if (SRC[j] === '}' && !--depth) return SRC.slice(at, j + 1);
  }
  throw new Error('unbalanced ' + name);
}

const code = [extract('esc'), extract('mediaCards'), extract('sameFeed')].join('\n');
const build = what => new Function(code + '\nreturn ' + what + ';')();

let failed = 0;
function ok(cond, msg) {
  if (!cond) { failed++; console.log('  ✗ ' + msg); }
  else console.log('  ✓ ' + msg);
}

/* ① 一条消息里的 N 个视频 → N 张卡（以前只出 3 张）。
   mediaCards 引用顶层的 NO_PI（iPhone 不写 playsinline），从参数注入。 */
const buildCards = noPi => new Function('NO_PI', code + '\nreturn mediaCards;')(noPi);
const mediaCards = buildCards(false);
const N = 7;
let text = '已完成三项修改，7 个视频重新合成。\n\n';
for (let i = 1; i <= N; i++) {
  text += `![场景${i}](/home/dev/codeboss-work/codeboss-场景${i}.mp4)\n`;
}
const html = mediaCards(text);
const cards = (html.match(/<video /g) || []).length;
ok(cards === N, `一条消息里 ${N} 个视频 → ${cards} 张卡（要 ${N} 张）`);

/* 同一段文字里路径重复出现（md 图片语法 + 正文各一份）→ 只算一张卡 */
const dup = mediaCards('![a](/tmp/x.mp4) 和 ![b](/tmp/x.mp4)');
ok((dup.match(/<video /g) || []).length === 1, '同一个路径只出一张卡');

/* 图片也一样出卡（原来也是 3 个上限） */
const imgs = mediaCards('![1](/tmp/a.png) ![2](/tmp/b.png) ![3](/tmp/c.png) ![4](/tmp/d.png)');
ok((imgs.match(/<img /g) || []).length === 4, '4 张图 → 4 张卡');

/* 全屏键不加回来（用户：不要另外加按键）：安卓壳实现 CustomView 后原生键会亮；
   iPhone 不写 playsinline（点播放直接进系统全屏），其它平台照旧 playsinline。 */
ok(/<video [^>]*playsinline/.test(html), '安卓/桌面 → 带 playsinline（卡片内播，全屏走原生键）');
ok(!/<video [^>]*playsinline/.test(buildCards(true)(text)),
  'iPhone → 不带 playsinline（点播放直接全屏）');
ok(!/class="vwrap"|vfull|mediafull/.test(html), '不加自制 ⛶/✕/灯罩');

/* ② 同样的内容 → 认定没变（省掉整墙重画） */
const sameFeed = build('sameFeed');
const base = [
  { role: 'user', text: '再加一些手机屏幕的特写' },
  { role: 'assistant', text: '', tools: [{ name: 'exec', detail: 'ls' }] },
  { role: 'assistant', text: '已完成三项修改' },
];
ok(sameFeed(base, JSON.parse(JSON.stringify(base))) === true, '内容一样 → 认定没变');

/* 中间多了一条工具调用：首末两条没动，签名看不出来，但内容变了 */
const mid = JSON.parse(JSON.stringify(base));
mid[1].tools.push({ name: 'exec', detail: 'ffmpeg -i a.mp4' });
ok(sameFeed(base, mid) === false, '中间多一条工具调用 → 认定变了（墙要重画）');

/* 中间那条正文被追加 */
const mid2 = JSON.parse(JSON.stringify(base));
mid2[1].text = '场景3已修好。';
ok(sameFeed(base, mid2) === false, '中间那条正文变了 → 认定变了');

/* 工具条目的内容变了（条数一样） */
const mid3 = JSON.parse(JSON.stringify(base));
mid3[1].tools[0].detail = 'rm -rf /tmp/x';
ok(sameFeed(base, mid3) === false, '工具行的内容变了 → 认定变了');

/* 条数变了（窗口滑动） */
ok(sameFeed(base, base.concat([{ role: 'assistant', text: '新的' }])) === false,
  '多了一条消息 → 认定变了');

/* 空墙 */
ok(sameFeed([], []) === true, '两边都空 → 认定没变');

/* ③ 收到一帧 done → 手机端的「正在思考」必须撤掉。
   host 只在「这一轮还有草稿」时才发 draft 帧，手机端每 50 秒重连一次，
   断线重连后收不到那帧 done，「正在思考 · 65 分钟」就一直挂着（真机 2026-11-14）。 */
const setDraftSrc = extract('setDraft');
function mkDraft() {
  const sb = { view: 'chat', DRAFT: '', DRAFT_WAIT: false, DRAFT_ST: '', DRAFT_EL: 0,
    DRAFT_EL_AT: 0, DONE_EL: 0, DRAFT_STICK: false, renderFeed: () => {},
    atBottom: () => true, stickBottom: () => {}, draftTickStart: () => {}, Date };
  const f = new Function('sb', 'with(sb){' + setDraftSrc + '\nreturn setDraft;}');
  return { sb, setDraft: f(sb) };
}
{
  const { sb, setDraft } = mkDraft();
  setDraft({ done: false, st: '正在思考', waiting: false, text: '', el: 600 });
  ok(sb.DRAFT_ST === '正在思考', '收到「正在思考」→ 状态行挂上');
  setDraft({ done: true, st: '', waiting: false, text: '答完了', el: 620 });
  ok(sb.DRAFT_ST === '' && sb.DRAFT === '' && sb.DRAFT_WAIT === false,
    '收到 done 帧 → 「正在思考」撤掉（不再自己走表）');
}
{
  /* 空草稿的 done 帧（host 没有草稿时发的那种）也得能清掉状态 */
  const { sb, setDraft } = mkDraft();
  setDraft({ done: false, st: '正在思考', waiting: false, text: '', el: 600 });
  setDraft({ done: true, st: '', waiting: false, text: '', el: 0 });
  ok(sb.DRAFT_ST === '' && sb.DRAFT === '', '收到空的 done 帧 → 状态行也撤掉');
}

if (failed) { console.log('\n失败 ' + failed + ' 项'); process.exit(1); }
console.log('\n全过');
