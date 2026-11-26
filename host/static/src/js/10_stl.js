// SPDX-License-Identifier: AGPL-3.0-or-later

/* ── 3D 模型（STL）预览（2026-11-19）────────────────────────────────
   手机上没有任何应用能打开 .stl，所以在页面里自绘：单指转动、双指缩放。
   转好视角点「圈选发指令」→ fvCropOpen 把这个 WebGL 帧截成 PNG 走原有裁剪链路。
   不引 three.js：页面是单文件下发，老 WebView（Chromium 70）也要跑得动。 */
/*STL_START*/
/* STL 判型：二进制 = 80 字节头 + uint32 三角形数 + 每个 50 字节
   （法线 3、三个顶点 9、属性 2）。先按长度对账——有些二进制文件头也以
   "solid" 开头，只看开头会误判成 ASCII。 */
function stlIsBin(buf){
  if(!buf||buf.byteLength<84) return false;
  const n=new DataView(buf).getUint32(80,true);
  if(84+n*50===buf.byteLength) return true;
  const b=new Uint8Array(buf,0,5);
  let s='';
  for(let i=0;i<5;i++) s+=String.fromCharCode(b[i]);
  return s.toLowerCase()!=='solid';
}
/* 三角面法线（右手定则）；退化三角形给 (0,0,1)，省得后面 normalize 出 NaN */
function stlFaceN(out,o,ax,ay,az,bx,by,bz,cx,cy,cz){
  const ux=bx-ax, uy=by-ay, uz=bz-az, vx=cx-ax, vy=cy-ay, vz=cz-az;
  let nx=uy*vz-uz*vy, ny=uz*vx-ux*vz, nz=ux*vy-uy*vx;
  const L=Math.sqrt(nx*nx+ny*ny+nz*nz)||1;
  out[o]=nx/L; out[o+1]=ny/L; out[o+2]=nz/L;
}
function stlParseBin(buf){
  const dv=new DataView(buf), n=dv.getUint32(80,true);
  if(84+n*50>buf.byteLength) throw new Error('STL 数据不完整');
  const pos=new Float32Array(n*9), nrm=new Float32Array(n*9);
  for(let i=0;i<n;i++){
    const o=84+i*50, a=i*9;
    for(let k=0;k<9;k++) pos[a+k]=dv.getFloat32(o+12+k*4,true);
    stlFaceN(nrm,a,pos[a],pos[a+1],pos[a+2],pos[a+3],pos[a+4],pos[a+5],
             pos[a+6],pos[a+7],pos[a+8]);
  }
  return {pos:pos,nrm:nrm,n:n};
}
function stlParseAscii(txt){
  const re=/vertex\s+([-+0-9.eE]+)\s+([-+0-9.eE]+)\s+([-+0-9.eE]+)/g;
  const v=[];
  let m;
  while((m=re.exec(txt))) v.push(+m[1],+m[2],+m[3]);
  const n=Math.floor(v.length/9);
  if(!n) throw new Error('没找到三角形');
  const pos=new Float32Array(n*9), nrm=new Float32Array(n*9);
  for(let i=0;i<n;i++){
    const a=i*9;
    for(let k=0;k<9;k++) pos[a+k]=v[a+k];
    stlFaceN(nrm,a,pos[a],pos[a+1],pos[a+2],pos[a+3],pos[a+4],pos[a+5],
             pos[a+6],pos[a+7],pos[a+8]);
  }
  return {pos:pos,nrm:nrm,n:n};
}
function stlParse(buf){
  if(!buf||!buf.byteLength) throw new Error('空文件');
  if(stlIsBin(buf)) return stlParseBin(buf);
  return stlParseAscii(new TextDecoder('utf-8').decode(new Uint8Array(buf)));
}
/* 包围盒 → 中心与半径：模型多大都缩到眼前，不用用户自己找 */
function stlCenter(pos){
  let x0=Infinity,y0=Infinity,z0=Infinity,x1=-Infinity,y1=-Infinity,z1=-Infinity;
  for(let i=0;i<pos.length;i+=3){
    const x=pos[i],y=pos[i+1],z=pos[i+2];
    if(x<x0)x0=x; if(y<y0)y0=y; if(z<z0)z0=z;
    if(x>x1)x1=x; if(y>y1)y1=y; if(z>z1)z1=z;
  }
  return {cx:(x0+x1)/2, cy:(y0+y1)/2, cz:(z0+z1)/2,
          r:Math.max(x1-x0,y1-y0,z1-z0)/2||1};
}
/* 列主序 4×4：a*b（和 WebGL 的 uniformMatrix4fv 同一口径） */
function stlMul(a,b){
  const o=new Float32Array(16);
  for(let c=0;c<4;c++) for(let r=0;r<4;r++){
    o[c*4+r]=a[r]*b[c*4]+a[4+r]*b[c*4+1]+a[8+r]*b[c*4+2]+a[12+r]*b[c*4+3];
  }
  return o;
}
function stlPersp(fovy,aspect,near,far){
  const f=1/Math.tan(fovy/2), nf=1/(near-far), o=new Float32Array(16);
  o[0]=f/aspect; o[5]=f; o[10]=(far+near)*nf; o[11]=-1; o[14]=2*far*near*nf;
  return o;
}
function stlTrans(z){ const o=new Float32Array(16); o[0]=o[5]=o[10]=o[15]=1; o[14]=z; return o; }
function stlRot(yaw,pitch){
  const cy=Math.cos(yaw), sy=Math.sin(yaw), cp=Math.cos(pitch), sp=Math.sin(pitch);
  /* Rx(pitch) * Ry(yaw)：先转水平再低头。列主序：o[col*4+row] */
  const o=new Float32Array(16);
  o[0]=cy;        o[1]=sp*sy;  o[2]=-cp*sy;
  o[4]=0;         o[5]=cp;     o[6]=sp;
  o[8]=sy;        o[9]=-sp*cy; o[10]=cp*cy;
  o[15]=1;
  return o;
}
/*STL_END*/

var STL_VS='attribute vec3 aP;attribute vec3 aN;uniform mat4 uM;uniform mat3 uR;'+
  'uniform float uZ;varying vec3 vN;'+
  'void main(){gl_Position=uM*vec4(aP*uZ,1.0);vN=uR*aN;}';
var STL_FS='precision mediump float;varying vec3 vN;'+
  'void main(){vec3 n=normalize(vN);'+
  'float a=abs(dot(n,normalize(vec3(0.35,0.55,0.75))));'+
  'float b=abs(dot(n,normalize(vec3(-0.6,-0.2,0.4))));'+
  'float d=0.24+0.62*a+0.30*b;'+
  'gl_FragColor=vec4(vec3(0.60,0.76,0.96)*d,1.0);}';

function stlShader(gl,type,src){
  const s=gl.createShader(type);
  gl.shaderSource(s,src); gl.compileShader(s);
  if(!gl.getShaderParameter(s,gl.COMPILE_STATUS))
    throw new Error('WebGL 着色器失败：'+String(gl.getShaderInfoLog(s)||'').slice(0,80));
  return s;
}
function stlView(body,mesh){
  track('stl_view');
  const c=document.createElement('canvas');
  c.style.width='100%'; c.style.height='60vh'; c.style.display='block';
  c.style.background='#0a0a0a'; c.style.borderRadius='8px';
  c.style.touchAction='none'; c.style.webkitUserSelect='none';
  body.appendChild(c);
  /* preserveDrawingBuffer：圈选时直接 toDataURL，不必再重画一帧 */
  const gl=c.getContext('webgl',{antialias:true,preserveDrawingBuffer:true})
        ||c.getContext('experimental-webgl',{antialias:true,preserveDrawingBuffer:true});
  if(!gl) throw new Error('这台手机不支持 WebGL');
  const prog=gl.createProgram();
  gl.attachShader(prog,stlShader(gl,gl.VERTEX_SHADER,STL_VS));
  gl.attachShader(prog,stlShader(gl,gl.FRAGMENT_SHADER,STL_FS));
  gl.linkProgram(prog);
  if(!gl.getProgramParameter(prog,gl.LINK_STATUS)) throw new Error('WebGL 程序链接失败');
  gl.useProgram(prog);

  const bb=stlCenter(mesh.pos);
  const pos=new Float32Array(mesh.pos.length);
  for(let i=0;i<pos.length;i+=3){
    pos[i]=(mesh.pos[i]-bb.cx)/bb.r;
    pos[i+1]=(mesh.pos[i+1]-bb.cy)/bb.r;
    pos[i+2]=(mesh.pos[i+2]-bb.cz)/bb.r;
  }
  function buf(data){
    const b=gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER,b);
    gl.bufferData(gl.ARRAY_BUFFER,data,gl.STATIC_DRAW);
    return b;
  }
  const bP=buf(pos), bN=buf(mesh.nrm);
  const aP=gl.getAttribLocation(prog,'aP'), aN=gl.getAttribLocation(prog,'aN');
  const uM=gl.getUniformLocation(prog,'uM'), uR=gl.getUniformLocation(prog,'uR');
  const uZ=gl.getUniformLocation(prog,'uZ');
  gl.enable(gl.DEPTH_TEST);
  gl.clearColor(0.043,0.055,0.078,1);

  let yaw=-0.6, pitch=0.35, zoom=1.5, drag=null, pinch=null, pending=false;
  function draw(){
    const dpr=Math.min(window.devicePixelRatio||1,2);
    const w=Math.max(1,Math.round(c.clientWidth*dpr));
    const h=Math.max(1,Math.round(c.clientHeight*dpr));
    if(c.width!==w||c.height!==h){ c.width=w; c.height=h; }
    gl.viewport(0,0,w,h);
    gl.clear(gl.COLOR_BUFFER_BIT|gl.DEPTH_BUFFER_BIT);
    const R=stlRot(yaw,pitch);
    const M=stlMul(stlMul(stlPersp(45*Math.PI/180,w/h,0.1,100),
                          stlTrans(-3.4)),R);
    gl.uniformMatrix4fv(uM,false,M);
    gl.uniformMatrix3fv(uR,false,new Float32Array([R[0],R[1],R[2],
      R[4],R[5],R[6],R[8],R[9],R[10]]));
    gl.uniform1f(uZ,zoom);
    gl.bindBuffer(gl.ARRAY_BUFFER,bP); gl.enableVertexAttribArray(aP);
    gl.vertexAttribPointer(aP,3,gl.FLOAT,false,0,0);
    gl.bindBuffer(gl.ARRAY_BUFFER,bN); gl.enableVertexAttribArray(aN);
    gl.vertexAttribPointer(aN,3,gl.FLOAT,false,0,0);
    gl.drawArrays(gl.TRIANGLES,0,mesh.n*3);
  }
  function schedule(){
    if(pending) return;
    pending=true;
    requestAnimationFrame(function(){ pending=false; draw(); });
  }
  function dist2(t){
    return Math.sqrt(Math.pow(t[0].clientX-t[1].clientX,2)
                    +Math.pow(t[0].clientY-t[1].clientY,2));
  }
  c.addEventListener('touchstart',function(e){
    if(e.touches.length===1){
      drag={x:e.touches[0].clientX,y:e.touches[0].clientY}; pinch=null;
    }else if(e.touches.length===2){
      drag=null; pinch={d:dist2(e.touches),z:zoom};
    }
    e.preventDefault();
  },{passive:false});
  c.addEventListener('touchmove',function(e){
    if(e.touches.length===1&&drag){
      yaw+=(e.touches[0].clientX-drag.x)*0.01;
      pitch=Math.max(-1.45,Math.min(1.45,pitch+(e.touches[0].clientY-drag.y)*0.01));
      drag={x:e.touches[0].clientX,y:e.touches[0].clientY};
      schedule();
    }else if(e.touches.length===2&&pinch){
      const d=dist2(e.touches);
      if(d>0) zoom=Math.max(0.4,Math.min(5,pinch.z*(d/(pinch.d||d))));
      schedule();
    }
    e.preventDefault();
  },{passive:false});
  c.addEventListener('touchend',function(e){
    if(!e.touches.length){ drag=null; pinch=null; }
  },{passive:false});
  /* 桌面上也能转（调试/电脑浏览器打开） */
  c.addEventListener('mousedown',function(e){
    drag={x:e.clientX,y:e.clientY}; e.preventDefault();
  });
  c.addEventListener('mousemove',function(e){
    if(!drag) return;
    yaw+=(e.clientX-drag.x)*0.01;
    pitch=Math.max(-1.45,Math.min(1.45,pitch+(e.clientY-drag.y)*0.01));
    drag={x:e.clientX,y:e.clientY}; schedule();
  });
  document.addEventListener('mouseup',function(){ drag=null; });
  c.addEventListener('wheel',function(e){
    zoom=Math.max(0.4,Math.min(5,zoom*(e.deltaY>0?0.92:1.08)));
    e.preventDefault(); schedule();
  },{passive:false});
  /* 关掉预览后 canvas 从 DOM 摘掉：监听器自己退场，别让每开一次模型留一份网格 */
  const onResize=function(){
    if(!c.parentNode){ window.removeEventListener('resize',onResize); return; }
    schedule();
  };
  window.addEventListener('resize',onResize);

  const hint=document.createElement('div');
  hint.className='meta'; hint.style.marginTop='6px';
  hint.textContent='单指转动 · 双指缩放 · 点「✂️ 圈选发指令」把当前视角发给 agent';
  body.appendChild(hint);
  draw();
}
