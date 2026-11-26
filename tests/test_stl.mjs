#!/usr/bin/env node
/* 3D 模型（STL）预览（2026-11-19）：二进制/ASCII 判型、三角面解析、
   法线与包围盒、旋转矩阵。手机上「圈选发指令」依赖这一帧画对。
   跑法： node tests/test_stl.mjs */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import assert from "node:assert";

const here = dirname(fileURLToPath(import.meta.url));
const src = readFileSync(join(here, "..", "host", "static", "src", "js", "10_stl.js"), "utf8");
const m = src.match(/\/\*STL_START\*\/([\s\S]*?)\/\*STL_END\*\//);
assert(m, "10_stl.js 里找不到 STL 标记块");
const S = new Function(m[1] + `; return {stlIsBin,stlParse,stlParseBin,stlParseAscii,
  stlCenter,stlMul,stlPersp,stlTrans,stlRot};`)();

const near = (a, b, eps = 1e-6) =>
  assert(Math.abs(a - b) <= eps, `期望 ${b}，实际 ${a}`);

/* 一个三角形 (0,0,0) (1,0,0) (0,1,0)，法线应为 (0,0,1) */
const TRI = [[0, 0, 0], [1, 0, 0], [0, 1, 0]];

function binStl(tris, header = "solid exported-by-test") {
  const buf = new ArrayBuffer(84 + tris.length * 50);
  const u8 = new Uint8Array(buf);
  for (let i = 0; i < header.length && i < 80; i++) u8[i] = header.charCodeAt(i);
  const dv = new DataView(buf);
  dv.setUint32(80, tris.length, true);
  tris.forEach((t, i) => {
    const o = 84 + i * 50;
    const v = t.flat();
    for (let k = 0; k < 9; k++) dv.setFloat32(o + 12 + k * 4, v[k], true);
  });
  return buf;
}

// 二进制文件头也以 "solid" 开头时，靠「长度对账」仍判成二进制
const bin = binStl([TRI]);
assert.equal(S.stlIsBin(bin), true, "长度对上的二进制 STL 不能被开头几个字节骗成 ASCII");

const mesh = S.stlParse(bin);
assert.equal(mesh.n, 1);
assert.equal(mesh.pos.length, 9);
near(mesh.pos[3], 1); near(mesh.pos[7], 1);
near(mesh.nrm[0], 0); near(mesh.nrm[1], 0); near(mesh.nrm[2], 1);

// ASCII STL 同样解析出一模一样的三角形
const ascii = `solid tri
 facet normal 0 0 0
  outer loop
   vertex 0 0 0
   vertex 1 0 0
   vertex 0 1 0
  endloop
 endfacet
endsolid tri`;
/* 浏览器里拿到的是 ArrayBuffer；node 的 Buffer 是 Uint8Array，先剥成精确的一段 */
const ab = (s) => {
  const b = Buffer.from(s);
  return b.buffer.slice(b.byteOffset, b.byteOffset + b.byteLength);
};
assert.equal(S.stlIsBin(ab(ascii)), false);
const am = S.stlParse(ab(ascii));
assert.equal(am.n, 1);
assert.deepEqual(Array.from(am.pos), Array.from(mesh.pos));
near(am.nrm[2], 1);

// 退化三角形不能除零出 NaN
const deg = S.stlParse(ab("solid d\nvertex 0 0 0\nvertex 0 0 0\nvertex 0 0 0\nendsolid d"));
assert.equal(deg.n, 1);
assert(Number.isFinite(deg.nrm[0]) && Number.isFinite(deg.nrm[2]));

// 包围盒：中心居中、半径取最大跨度的一半
const c = S.stlCenter(new Float32Array([-1, 0, 0, 3, 0, 0, 1, 2, 4]));
near(c.cx, 1); near(c.cy, 1); near(c.cz, 2);
near(c.r, 2);

// 矩阵：单位 × 单位 = 单位；0 旋转 = 单位；透视矩阵元素对
const I = new Float32Array([1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1]);
assert.deepEqual(Array.from(S.stlMul(I, I)), Array.from(I));
assert.deepEqual(Array.from(S.stlRot(0, 0)), Array.from(I));
near(S.stlTrans(-3)[14], -3);
near(S.stlPersp(Math.PI / 2, 1, 0.1, 100)[0], 1);

// 旋转矩阵改变朝向但不放大：三列都保持单位长度
const R = S.stlRot(0.7, -0.4);
for (const col of [[R[0], R[1], R[2]], [R[4], R[5], R[6]], [R[8], R[9], R[10]]]) {
  near(Math.hypot(...col), 1, 1e-6);
}

// 圈选要能截 WebGL 这一帧：预览层必须从 canvas 取图
const view = readFileSync(join(here, "..", "host", "static", "src", "js", "02_view.js"), "utf8");
const crop = view.match(/function fvCropOpen\(\)\{[\s\S]*?\n\}/);
assert(crop, "找不到 fvCropOpen()");
assert(crop[0].includes("querySelector('canvas')"), "圈选要认 3D 预览的 canvas");
assert(crop[0].includes("toDataURL"), "圈选要把当前视角截成 PNG");
assert(src.includes("preserveDrawingBuffer"), "WebGL 上下文要保留绘制缓冲，否则截出来是空白");

// 模型分支：整份 STL 用 xj 的二进制请求拿，再交给解析器
const show = view.match(/async function fviewShow\(path,name\)\{[\s\S]*?\n\}/);
assert(show, "找不到 fviewShow()");
assert(show[0].includes("stlParse(r.buf)"), "模型分支要调 stlParse");
assert(show[0].includes("{bin:true"), "STL 是二进制，要按 arraybuffer 取");

console.log("stl ok");
