// SPDX-License-Identifier: AGPL-3.0-or-later

/* 浮层的 ✕：命令终端是「关掉并杀进程」，电脑托管的会话只能收起浮层——
   把用户的 claude 会话杀掉可不是「关闭面板」。 */
function termExit(){ if(TERM.pin){ termBack(); return; } termClose(TERM.sid); }
/* 终端面板里的 /s/<sid> 发现会话已经没了（开发机重启/被关掉）会 postMessage 过来：
   把浮层关掉回列表，不然用户看到的就是一块死的空白终端。 */
window.addEventListener('message',function(e){
  if(e&&e.data&&e.data.cbTermGone){ termBack(); toast('终端已结束',true); }
});
/* 终端卡片点按：活的直接进浮层；「已失效」的占位行重新拉起再进。
   网页终端不在 /api/terms 里，浮层要 pin 住（否则 4 秒那拍会以为它结束把人踢出来）。 */
function termTap(sid){
  const w=(lastWeb||[]).find(function(x){return x.sid===sid;});
  if(w&&w.ghost){ respawnOpen(sid); return; }
  termView(sid, !!w);
}
async function termClose(sid){
  if(!sid) return;
  try{
    /* 「已失效」的占位条目没有进程可关：直接把这条记录删掉 */
    if((lastWeb||[]).some(function(x){return x.sid===sid&&x.ghost;})){
      await xj('/remove/'+sid,{method:'POST'});
    }else{
      const r=await xj('/api/term_close',{method:'POST',body:{sid:sid}});
      const d=r.json||{};
      if(!d.ok){ toast(d.error||'关闭失败',true); return; }
    }
    toast('终端已关闭');
  }catch(e){ toast('网络错误',true); return; }
  /* 刚关掉的这条先从本地列表摘掉再回列表页：网页终端的数据在 lastWeb 里，
     只重拉 /api/terms 它还在，卡片会一直挂着（用户实测「关掉不刷新」）。 */
  lastWeb=(lastWeb||[]).filter(function(x){return x.sid!==sid;});
  TERM.terms=TERM.terms.filter(function(x){return x.sid!==sid;});
  TERM_SIG='';
  termBack();
  loadListData();   // 后台再对一次真实列表（进程可能刚退，列表那边有缓存）
}
async function termNew(){
  if(!termAllowed()){ toast('免费版不具备该功能，开通 Pro 后使用',true); return; }
  track('sess_new');
  try{
    const r=await xj('/api/term_new',{method:'POST',body:'{}'});
    const d=r.json||{};
    if(d.ok&&d.sid){ TERM.terms.unshift({sid:d.sid,name:'终端 '+d.sid.slice(0,4),alive:true,cwd:''}); termDotSync(); termView(d.sid); }
    else toast(d.error||'创建失败',true);
  }catch(e){ toast('网络错误',true); }
}
/* ── 会话命令：长按 💻 会话行扫两屏终端文本识别 shell 命令，弹层选一条在电脑新标签运行；
   ⌨ 按钮随时翻历史。识别/历史都去重存电脑端 codeboss.db（cmd_hist 表）。 ── */
var CMDS={sid:'',cmds:[]};
function closeCmdSheet(){ document.getElementById('cmdsheet').classList.remove('on'); }
/* 两个来源形状不一样：/api/ext_scan 的 cmds 是**字符串数组**，
   /api/ext_cmdhist 的 hist 是 {cmd,ts} 对象数组。以前一律按 h.cmd 取，
   于是「本次识别」那一整块全是空行（有行数没内容，点 ▶ 也跑不了）。
   统一走这个取法，两种都能读。 */
function cmdOf(h){ return (typeof h==='string')?h:((h&&h.cmd)||''); }
/* 破坏性命令：跑之前必须再点一次。识别出来的命令可能带 sudo/rm，误点就没了。 */
var CMD_DANGER=[
  [/\brm\b/,'删除文件'], [/\brmdir\b/,'删除目录'], [/\bmv\b/,'移动/覆盖文件'],
  [/\bdd\b/,'直接写盘'], [/\bmkfs\b/,'格式化'], [/\bshred\b/,'擦除文件'],
  [/\btruncate\b/,'截断文件'],
  [/\bgit\s+(reset\s+--hard|clean\s+-[a-z]*f|push\b[^|;&]*(-f|--force))/,'git 破坏性操作'],
  [/\bsudo\b/,'以管理员身份运行'], [/\b(kill|pkill|killall)\b/,'结束进程'],
  [/\bchmod\s+-R\b|\bchown\s+-R\b/,'递归改权限'], [/-{2}purge\b|\buninstall\b/,'卸载/清除数据'],
  [/\bdrop\s+table\b|\bdelete\s+from\b/i,'删除数据']
];
function cmdDanger(c){
  var s=String(c||'');
  for(var i=0;i<CMD_DANGER.length;i++) if(CMD_DANGER[i][0].test(s)) return CMD_DANGER[i][1];
  return '';
}
function cmdRows(arr,emptyTip,canDel){
  if(!arr||!arr.length) return '<div class="meta" style="padding:8px 2px">'+emptyTip+'</div>';
  const base=CMDS.cmds.length; CMDS.cmds=CMDS.cmds.concat(arr);
  return arr.map(function(h,i){
    const n=base+i;
    return '<div class="crow"><span class="ccmd">'+escHtml(cmdOf(h))+'</span>'+
      '<button class="crun" title="拷贝" onclick="copyCmd('+n+')">📋</button>'+
      '<button class="crun'+(cmdDanger(cmdOf(h))?' warn':'')+'" title="'+
        (cmdDanger(cmdOf(h))?('会'+cmdDanger(cmdOf(h))+'，需二次确认'):'在终端运行')+
        '" onclick="runCmd('+n+',this)">▶</button>'+
      (canDel?('<button class="crun del" title="从历史里删除" onclick="delCmd(this,'+n+')">🗑</button>'):'')+
      '</div>';
  }).join('');
}
/* 从历史里删一条：两击确认（WebView 里 confirm() 不可靠，和「删开发机」同一套做法） */
function delCmd(btn,i){
  const cmd=cmdOf(CMDS.cmds[i]); if(!cmd) return;
  if(!btn.__arm){
    btn.__arm=1; btn.classList.add('arm'); btn.textContent='确认？';
    setTimeout(function(){ if(btn){ btn.__arm=0; btn.classList.remove('arm'); btn.textContent='🗑'; } },3000);
    return;
  }
  xj('/api/ext_cmddel/'+CMDS.sid,{method:'POST',body:{cmd:cmd}}).then(function(r){
    const d=r.json||{};
    if(d.ok){ CMDS.hist=d.hist||[]; renderCmdSheet(); toast('已从历史删除'); }
    else toast('删除失败：'+(d.error||''),true);
  }).catch(function(){ toast('网络错误',true); });
}
function copyCmd(i){ copyText(cmdOf(CMDS.cmds[i])); }
/* 老 WebView 没有 navigator.clipboard：隐藏 textarea + execCommand('copy')（用户手势内有效） */
/* 复制：优先 Clipboard API（https 下可用），不行再退到 execCommand。
   ⚠️ 兜底的 textarea 不能放到屏幕外（left:-9999px）—— iOS 的 execCommand 会失败。
   这个函数以前定义了两份（后一份覆盖前一份），只剩这一份。 */
function copyText(t){
  const v=(t==null)?'':String(t);
  const done=()=>toast('已复制，可粘贴到终端');
  const fb=()=>{
    try{
      const ta=document.createElement('textarea');
      ta.value=v; ta.setAttribute('readonly','');
      ta.style.cssText='position:fixed;top:0;left:0;width:1px;height:1px;opacity:0;border:0;padding:0';
      document.body.appendChild(ta);
      ta.select(); ta.setSelectionRange(0,v.length);
      const ok=document.execCommand('copy');
      ta.remove();
      ok?done():toast('复制失败',true);
    }catch(e){ toast('复制失败',true); }
  };
  try{
    if(navigator.clipboard&&navigator.clipboard.writeText) navigator.clipboard.writeText(v).then(done).catch(fb);
    else fb();
  }catch(e){ fb(); }
}
function openCmdSheet(sid,cmds,hist){
  CMDS={sid:sid,cmds:[],scan:(cmds||null),hist:(hist||[])};
  const m=(lastExt||[]).find(x=>x.sid===sid);
  document.getElementById('cmdSidT').textContent=(m&&m.title)||sid.slice(0,8);
  renderCmdSheet();
  document.getElementById('cmdsheet').classList.add('on');
}
function renderCmdSheet(){
  CMDS.cmds=[];
  document.getElementById('cmdBox').innerHTML=
    (CMDS.scan?('<div class="mtitle">本次识别</div>'+cmdRows(CMDS.scan,'未识别到命令')):'')+
    '<div class="mtitle">历史</div>'+cmdRows(CMDS.hist,'还没有历史',true);
}
/* 把「用户此刻在手机上看到的内容」取出来发上去 —— 识别要认的是这一屏，
   不是开发机终端那一屏。三种视图取当前可见的那个。 */
function curScreenText(){
  const shown=(el)=>{
    if(!el) return false;
    const st=getComputedStyle(el);
    return !(st.display==='none'||st.visibility==='hidden');
  };
  /* 取「现在真的在屏幕上」的那些块：容器里每个子块的矩形和可视区求交。
     ⚠️ 以前是取整个容器的 innerText 再切尾部 6000 字 —— 用户往上划去看历史时，
     送上去的仍然是最新那段，跟眼睛看到的完全对不上。 */
  const visOf=(el)=>{
    if(!shown(el)) return '';
    const cr=el.getBoundingClientRect();
    const top=Math.max(cr.top,0), bot=Math.min(cr.bottom, window.innerHeight||900);
    if(bot<=top) return '';
    const kids=el.children;
    if(!kids||!kids.length) return (el.innerText||'').trim();
    const out=[];
    for(let i=0;i<kids.length;i++){
      const r=kids[i].getBoundingClientRect();
      if(r.bottom>=top-40 && r.top<=bot+40) out.push(kids[i].innerText||'');
    }
    return out.join('\n').trim();
  };
  let parts=[];
  for(const id of ['feed','livehist','liveflow']){ const t=visOf(document.getElementById(id)); if(t) parts.push(t); }
  let out=parts.join('\n');
  if(!out){ out=(document.getElementById('live')||{}).innerText||''; }   // 原样视图
  if(!out){ try{ out=(document.body.innerText||'').trim(); }catch(e){} }  // 兜底
  return out.slice(-12000);
}
async function scanCmds(sid){
  /* 识别命令要调大模型，是 Pro / Max 功能（features.terminal）。免费版不发起请求，
     直接说清楚去哪儿开通——真正的主闸门在中继和开发机。 */
  if(!termAllowed()){ toast('识别命令是 Pro / Max 套餐的功能，开通后使用',true); return; }
  track('cmd_scan');
  toast('正在识别命令…');
  try{
    const r=await xj('/api/ext_scan/'+sid,{method:'POST',body:{text:curScreenText()}});
    const d=r.json||{};
    if(!r.ok||!d.ok){ toast('识别失败：'+(d.error||('HTTP '+((r&&r.status)||0))),true); return; }
    if(!(d.cmds||[]).length) toast('未识别到命令');
    /* 0 条也把弹层打开：至少让人看到「这次没认出来」和命令历史，而不是点完毫无反应 */
    openCmdSheet(sid,d.cmds||[],d.hist||[]);
  }catch(e){ toast('网络错误',true); }
}
async function cmdSheet(sid){   // ⌨：直接看历史并运行
  CMDS={sid:sid,cmds:[]};
  let hist=[];
  try{ const r=await xj('/api/ext_cmdhist/'+sid); const d=r.json||{}; hist=d.hist||[]; }catch(e){}
  openCmdSheet(sid,null,hist);
}
function runCmd(i,btn){
  const cmd=cmdOf(CMDS.cmds[i]); if(!cmd) return;
  if(!termAllowed()){ toast('免费版不具备该功能，开通 Pro 后使用',true); return; }
  /* 破坏性命令两击确认：第一下只把按钮变红提示原因，4 秒内再点才真跑。 */
  const dg=cmdDanger(cmd);
  if(dg&&btn&&!btn.__arm){
    btn.__arm=1; btn.classList.add('arm'); btn.textContent='确认？';
    toast('这条会'+dg+'，再点一次才运行',true);
    setTimeout(function(){ if(btn){ btn.__arm=0; btn.classList.remove('arm'); btn.textContent='▶'; } },4000);
    return;
  }
  track('cmd_run');
  toast('正在打开终端…');
  xj('/api/ext_cmdrun/'+CMDS.sid,{method:'POST',body:{cmd:cmd}}).then(function(r){
    const d=r.json||{};
    if(d.ok&&d.csid){   // 直接跳进跑这条命令的终端（跑完留交互 shell，可接着打字）
      closeCmdSheet();
      TERM.terms.unshift({sid:d.csid,name:cmd.slice(0,24),alive:true,cwd:''});
      termView(d.csid);      // 浮层自带返回/关闭，不占左滑序列
      termSyncIme();
    } else toast('运行失败：'+(d.error||('HTTP '+((r&&r.status)||0))),true);
  }).catch(function(e){ toast('网络错误：'+String((e&&e.message)||e),true); });
}
/* 长按识别：600ms 触发；滑动 12px 取消；触发后 touchend preventDefault 吃掉合成 click（不误切会话） */
var LP={t:null,fired:false,ts:0};
function lpFire(sid){
  if(LP.fired) return; LP.fired=true; LP.t=null;
  try{ navigator.vibrate&&navigator.vibrate(25); }catch(err){}
  if(sid) scanCmds(sid); else toast('该会话无运行窗口，不支持识别',true);
}
/* 绑定长按识别：el 区域内 420ms 触发 lpFire；滑动 12px 取消；触发后 touchend preventDefault 吃掉合成 click。
   sidFn(e)：返回会话id=识别；null=该行不支持（弹提示）；undefined=非目标区域（不绑）。 */
function lpBind(el,sidFn){
  el.addEventListener('touchstart',function(e){
    if(e.target.closest('button,a')) return;
    const sid=sidFn(e); if(sid===undefined) return;
    const t=e.touches[0]; const x=t.clientX,y=t.clientY;
    LP.fired=false; LP.ts=Date.now();
    /* mv 必须 {once:false} 常驻整个手势：once:true 时首个 touchmove（滚动起步常只有 1-2px）
       就把监听摘掉，之后怎么滑都取消不了定时器 → 滑动途中误触发识别（Qin 2 Pro 实测）。
       任何 move 超过 10px 即杀定时器——刻意长按的手指是纹丝不动的，滚动手势必然出 move */
    const mv=function(ev){ const p=ev.touches[0];
      if(Math.abs(p.clientX-x)>10||Math.abs(p.clientY-y)>10){ kill(); } };
    const kill=function(){ if(LP.t){ clearTimeout(LP.t); LP.t=null; }
      el.removeEventListener('touchmove',mv); };
    const end=function(ev){ kill();
      if(LP.fired){ ev.preventDefault(); LP.fired=false; } };
    /* 420ms：抢在 iOS/安卓内建长按判定（~500ms 派发 touchcancel+弹拷贝条）之前，否则定时器被清、长按永远不触发 */
    LP.t=setTimeout(function(){ kill(); lpFire(sid); },420);
    el.addEventListener('touchmove',mv,{passive:true});
    el.addEventListener('touchend',end,{once:true});
    el.addEventListener('touchcancel',end,{once:true});
  },{passive:false});
  /* iOS Safari 长按会派发 contextmenu（touchcancel 已清掉定时器）→ contextmenu 也作为触发信号；
     3s 内有过 touch 才认，桌面右键不触发。lpFire 内 LP.fired 去重防双发 */
  el.addEventListener('contextmenu',function(e){
    e.preventDefault();
    if(Date.now()-LP.ts<3000) lpFire(sidFn(e));
  });
}
/* 兜底：iOS 若仍开始文本选择，长按窗口期内立即清掉（菜单随选区一起消失） */
document.addEventListener('selectionchange',function(){
  if(LP.fired&&document.getSelection()&&!document.getSelection().isCollapsed){
    try{ document.getSelection().removeAllRanges(); }catch(err){}
  }
});
/* 长按识别已下线（用户要求）：滑动误触、太灵敏、且识别结果不理想。
   保留 lpBind/lpFire 函数体备用——恢复只需取消下面两行注释。 */
/*lpBind(document.getElementById('list'),function(e){
  const r=e.target.closest('.srow'); if(!r) return undefined;
  return r.getAttribute('data-csid');          // 有=识别，null=弹「无运行窗口」
});
lpBind(document.getElementById('pgCtrl'),function(e){
  if(!e.target.closest('#live,#livehist,#liveflow,#live2')) return undefined;
  return view==='live' ? SID : null;
});*/
/* 实时页的弹层选项：点一下直接选中（不用再去按键条慢慢挪 ↑/↓）。
   单选弹层 = 挪到目标项再 ⏎；多选弹层 = 挪过去按空格勾选，回车仍由用户按。

   以前是「按页面上那帧 DOM 的 data-cur 算差值，一口气盲按，最后无条件 ⏎ + 报成功」：
   页面那帧可能已经过期（推送慢半拍/人在电脑上也动过光标），差一拍就挪错；按错还
   照样回车，于是「提示已选第 N 项，其实什么都没选中」。
   现在每一步都**现读服务端重排的屏幕核对**：只有真挪到目标项才按 ⏎，挪不到就明说
   （绝不盲回车——那是权限弹层，按错就是放行/拒绝）。 */
let dlgBusy=false;
function sleep(ms){ return new Promise(function(r){ setTimeout(r,ms); }); }
/* ── 按键排队：所有按键（快捷键条、点拨层、/model）走同一条队列 ──────────────
   以前每个键各发各的（直连超时还会各自回落到中继），网络一抖就乱序：
   连点两下 ↑ 只挪一格，或者 ⏎ 抢在方向键前面确认了别的选项——看着就是
   「按了没反应/选错了」。现在前一个回来了才发下一个；整段通道不通就一次性
   把排队的都判失败，不让用户一条一条干等超时。
   cid：同一个键重复送达（直连其实到了、中继又补一份）只执行一次。 */
let KEYQ=[], KEYBUSY=false;
function keySend(k){
  return new Promise(function(res){
    if(!SID){ res({status:0}); return; }   // 没选会话：发了也只会是 404（日志里那一串 [key] ok=0）
    KEYQ.push({k:k,sid:SID,res:res});   /* sid 记在入队时：中途切会话也别打到新会话上 */
    pumpKeys();
  });
}
async function pumpKeys(){
  if(KEYBUSY) return;
  KEYBUSY=true;
  try{
    while(KEYQ.length){
      const it=KEYQ.shift();
      let r=null;
      try{
        r=await xj('/api/ext_key',{method:'POST',away:true,dcto:5000,body:{
          sid:it.sid,key:it.k,
          cid:it.k+'-'+Date.now().toString(36)+'-'+Math.random().toString(36).slice(2,8)}});
      }catch(e){ r=null; }
      if(!r||r.status===0){                 /* 通道断了：后面排队的别再逐条等超时 */
        KEYQ.splice(0).forEach(function(q){ q.res({status:0}); });
      }
      it.res(r||{status:0});
    }
  }finally{ KEYBUSY=false; }
}
/* 弹层现况：直接问开发机要当前屏幕的重排结果（不拿页面上可能过期的 DOM）。 */
let DLG_WHY='';          /* null 的原因：'net'=没读到电脑画面，'gone'=弹层真没了 */
function dlgWhy(){
  return DLG_WHY==='net' ? '没读到电脑画面（网络/通道断了），再点一次' : '弹层已经不在屏幕上了';
}
async function dlgState(){
  if(!SID) return null;
  let r;
  try{ r=await xj('/api/ext_live/'+SID,{away:true,timeout:4000,dcto:4000}); }
  catch(e){ DLG_WHY='net'; return null; }
  if(!r||r.status===0||!r.json){ DLG_WHY='net'; return null; }
  const h=r.json.html||'';
  const m=/class="dblk"[^>]*data-cur="(\d+)"[^>]*data-multi="(\d+)"/.exec(h);
  if(!m){ DLG_WHY='gone'; return null; }
  const opts=[]; const re=/class="dopt(?: sel)?" data-opt="(\d+)"/g; let mm;
  while((mm=re.exec(h))) opts.push(parseInt(mm[1],10));
  DLG_WHY='';
  return {cur:parseInt(m[1],10)||0, multi:m[2]==='1', opts:opts, html:h};
}
function dlgOptText(html,n){
  const m=new RegExp('class="dopt[^"]*" data-opt="'+n+'">([\\s\\S]*?)</div>').exec(html||'');
  return m?m[1]:'';
}
/* 提醒卡/浮条点「确认」时先看屏幕：电脑上真有一个选项弹层 → 停在实时页让用户
   自己点（dlgPick 会现读屏幕核对后再按键），比往输入框塞一句「我确认」靠谱。
   没有弹层（模型是用文字问的）→ 返回 false，调用方照旧带进输入框。 */
async function dlgFirst(sid){
  if(!sid) return false;
  let r;
  try{ r=await xj('/api/ext_live/'+sid,{away:true,timeout:4000,dcto:4000}); }
  catch(e){ return false; }
  const h=(r&&r.json&&r.json.html)||'';
  if(h.indexOf('class="dblk"')<0) return false;
  try{ setView('live'); }catch(e){}
  toast('电脑上有确认弹层：点一下选项');
  return true;
}
/* 发一个键并如实回报：{ok:true,warn} / {ok:false,error}。失败绝不吞掉。 */
async function dlgKey(k){
  try{
    const r=await keySend(k);              /* 和快捷键条同一条队列：顺序不会乱 */
    const d=r&&r.json;
    if(d&&d.ok) return {ok:true,warn:d.warn||''};
    if(d&&d.error) return {ok:false,error:d.error};
    if(r&&r.status===0) return {ok:false,error:'没连上电脑（网络/通道断了）'};
    return {ok:false,error:'发送失败'+(r&&r.status?('（HTTP '+r.status+'）'):'')};
  }catch(e){ return {ok:false,error:'网络错误'}; }
}
function dlgEsc(s){
  return String(s||'').replace(/&lt;/g,'<').replace(/&gt;/g,'>')
    .replace(/&quot;/g,'"').replace(/&#39;/g,"'").replace(/&amp;/g,'&');
}
async function dlgPick(el){
  /* 正在处理上一次点选：明说，别让这一下悄悄消失（用户会以为「点不中」） */
  if(dlgBusy){ toast('上一个选择还在处理…'); return; }
  const box=el.closest('.dblk'); if(!box||!SID) return;
  const want=parseInt(el.getAttribute('data-opt'),10)||0; if(!want) return;
  dlgBusy=true;
  try{
    const st=await dlgState();
    /* 页面只传「我要第几项」+ 当时看到的文字。读屏、挪光标、核对、回车全在 host
       （/api/ext_dlg_pick）：页面这帧 DOM 可能已经过期，客户端盲按会在权限弹层上按错。 */
    const label=dlgEsc((st?dlgOptText(st.html,want):'').replace(/<[^>]*>/g,''))
      .replace(/\s+/g,' ').trim().slice(0,120);
    const pd=PEND[SID];
    let r;
    try{
      r=await xj('/api/ext_dlg_pick',{method:'POST',away:true,dcto:9000,body:{
        sid:SID,want:want,text:label,rid:(pd&&pd.rid)||'',
        cid:'dp-'+SID+'-'+Date.now().toString(36)+'-'+Math.random().toString(36).slice(2,8)}});
    }catch(e){ toast('没连上电脑（网络/通道断了）',true); return; }
    const d=(r&&r.json)||null;
    if(!d){ toast('发送失败'+(r&&r.status?('（HTTP '+r.status+'）'):''),true); return; }
    if(!d.ok){ toast(d.error||'没能选上，再点一次',true); return; }
    if(d.multi){                      /* 多选：空格勾选，回车仍由用户按 */
      toast(d.verified?'第 '+want+' 项已切换，选完按 ⏎'
        :'空格已发出，但没核对到勾选状态，请看电脑');
      return;
    }
    if(!d.verified){ toast(d.error||'已按回车，但没确认到结果，请看电脑',true); return; }
    /* 消息已经发出去了：确认卡立刻收掉，别占着屏幕等下一轮刷新（用户 2026-11-10）。
       lastFlowHtml 清空让下一轮轮询照实重画：电脑上真收了就保持收起，
       还没收（键丢了）就再冒出来，不会让人没了按钮。 */
    try{ box.remove(); }catch(e){}
    lastFlowHtml='';
    try{ if(PEND[SID]&&PEND[SID].kind==='perm') pendSent(); }catch(e){}
    toast('已选第 '+want+' 项');
  }finally{
    dlgBusy=false;
    setTimeout(liveflowPoll,400);
  }
}
document.getElementById('liveflow').addEventListener('click',function(e){
  const o=e.target.closest('.dopt'); if(o) dlgPick(o);
});
/* ── 命令终端页旧的只读输出视图已删：终端卡片（termCard）+ iframe /s/ 取代，还能打字 ── */
async function key(k){
  track('key', k);   /* 哪个键：enter/esc/ctrl_c…（键位名，不是用户输入） */
  try{
    /* 排队发送（见 keySend）：连点保序，失败如实报；不因为「页面以为自己在后台」被吞 */
    const r=await keySend(k);
    const d=r&&r.json;
    if(!d){ toast(r&&r.status===0?'没连上电脑（网络/通道断了）':'发送失败（'+(r&&r.status||'无响应')+'）',true); return; }
    if(!d.ok) toast('失败：'+(d.error||''),true);
    else if(d.warn) toastWarn(d.warn,3000);
    else toast({esc:'已发送 Esc','shift_tab':'已发送 ⇧Tab',tab:'已发送 Tab',ctrl_c:'已发送 ⌃C',ctrl_o:'已发送 ⌃O（展开/收起）',ctrl_l:'已发送 ⌃L',ctrl_a:'已发送 ⌃A（行首）',ctrl_e:'已发送 ⌃E（行尾）',ctrl_u:'已发送 ⌃U（删到行首）',slash_model:'已发送 /model',ctrl_k:'已发送 ⌃K（删到行尾）',ctrl_w:'已发送 ⌃W（删词）',ctrl_r:'已发送 ⌃R（搜历史）',up:'已发送 ↑',down:'已发送 ↓',left:'已发送 ←',right:'已发送 →',enter:'已发送 ⏎',space:'已发送 空格',backspace:'已发送 ⌫（退格）'}[k]||k);
    if(view==='live'&&fullColor) setTimeout(liveflowPoll,700);
    else if(view==='live') setTimeout(screenPoll,700);
    else if(view==='raw') setTimeout(rawPoll,700);
  }catch(e){ toast('网络错误',true); }
}
function migrateMe(btn){
  armConfirm(btn,'转全彩（会话退出并 --resume 重开，历史保留）',doMigrateMe);
}
async function doMigrateMe(){
  try{
    const r=await xj('/api/ext_migrate/'+SID,{method:'POST'});
    const d=r.json;
    toast((d.migrated||[]).length?'已转为全彩，重开中…':('未迁移：'+(((d.skipped||[])[0]||(d.failed||[])[0]||{}).why||'未知')));
    setTimeout(poll,3000);
  }catch(e){ toast('网络错误',true); }
}
function toast(m,err,ms){_toastSeq++;const t=document.getElementById('toast');t.textContent=m;t.className='toast'+(err?' err':'');setTimeout(()=>t.classList.add('on'),10);setTimeout(()=>t.classList.remove('on'),ms||1600);if(err){try{if(window.CB&&CB.log)CB.log('toast(err): '+m);}catch(e){}}}
/* 历史调用点写的是 show(msg,isErr)：给个别名，别让 ReferenceError 把后续流程（如 devLoad）打断 */
function show(m,e){ toast(m,e); }
/* 较长的警示：停在屏幕上久一点，单独计数避免被普通 toast 的定时器提前收掉 */
let _toastSeq=0;
function toastWarn(m,ms){
  const t=document.getElementById('toast'), id=++_toastSeq;
  t.textContent=m; t.className='toast warn';
  setTimeout(function(){ if(id===_toastSeq) t.classList.add('on'); },10);
  setTimeout(function(){ if(id===_toastSeq) t.classList.remove('on'); },ms||4500);
}
const ta=document.getElementById('txt');
/* 打字的两个时间戳：输入法还在拼（composition 中）时 textarea.value 可能还是空的，
   只盯 value 会被「正在输入就重载」钻空子，所以单独记（hasDraft 会看这两个）。 */
var IME_ON=false, LAST_TYPE=0;
ta.addEventListener('input',()=>{ LAST_TYPE=Date.now(); fitTxt(); draftSaveSoon(); });
/* 屏幕高度变了（键盘弹起、横竖屏）：上限跟着变，重新量一次 */
window.addEventListener('resize',function(){ try{ fitTxt(); }catch(e){} });
ta.addEventListener('compositionstart',()=>{ IME_ON=true; });
ta.addEventListener('compositionend',()=>{ IME_ON=false; LAST_TYPE=Date.now(); });
ta.addEventListener('keydown',e=>{ LAST_TYPE=Date.now(); if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();sendMsg();}});
/* 手指还在框里/刚动过 → 页面自更新不许 reload：重载把输入框整个换掉，软键盘
   还开着但焦点丢了，按什么都进不来（2026-11-18 实锤：部署后手机自动重载，
   用户停在框里 >8s 没打字，LAST_TYPE 护栏过期，键盘冻死）。聚焦一律拦，
   不看 8 秒——停着想话说停多久都可能。 */
function typingHold(){
  try{ if(document.activeElement===ta) return true; }catch(e){}
  return !!(LAST_TYPE && Date.now()-LAST_TYPE<8000);
}
/* ── 会话切换 ── */
/* 唤起后等它真的活着（发现是 2s 一轮，进程从 --resume 到能被发现要几秒）。
   以前固定睡 6 秒就进去，进程还没起来时进去是一片空。最多等 ~15 秒。 */
async function waitAlive(sid){
  for(let i=0;i<10;i++){
    await new Promise(r=>setTimeout(r,1500));
    await loadListData();
    const m=(lastExt||[]).find(x=>x.sid===sid);
    if(m&&m.state!=='exited') return true;
  }
  return false;
}
/* ── 开着会话的翻页序列 ──────────────────────────────────────────────
   左滑序列 = 还开着的会话，按「最近一次进入」倒序（进入时间记在本机，换手机各自排）。
   已关闭/已退出的会话不进这条序列，只留在列表里显示「已退出」、可以召回。 */
let OPEN=[];
let ENTER_AT={};
try{ ENTER_AT=JSON.parse(localStorage.getItem('cb_sess_enter')||'{}')||{}; }catch(e){ ENTER_AT={}; }
function enterMark(sid){
  if(!sid) return;
  ENTER_AT[sid]=Date.now();
  const ks=Object.keys(ENTER_AT);       // 只留最近 100 个，别把 localStorage 撑大
  if(ks.length>100){ ks.sort((a,b)=>ENTER_AT[a]-ENTER_AT[b]);
    for(let i=0;i<ks.length-100;i++) delete ENTER_AT[ks[i]]; }
  try{ localStorage.setItem('cb_sess_enter',JSON.stringify(ENTER_AT)); }catch(e){}
  buildOpen();
}
function buildOpen(){
  const live=(lastExt||[]).filter(s=>s.kind==='computer'&&s.state!=='exited'&&!s.ghost&&
                                     (!CUR_AG||!s.agent||s.agent===CUR_AG));
  live.sort(function(a,b){
    const ea=ENTER_AT[a.sid]||0, eb=ENTER_AT[b.sid]||0;
    if(ea!==eb) return eb-ea;                       // 最近进过的排前面
    return (b.last||b.started||0)-(a.last||a.started||0)   // 没进过的按活跃时间
        || (a.sid<b.sid?-1:1);         // 活跃时间一样（0 的僵尸会话）也得有稳定次序，否则滑动时前后乱跳
  });
  OPEN=live.map(function(s){return s.sid;});
  openDots();
}
function curOpenIdx(){ return OPEN.indexOf(SID); }
function openDots(){
  const box=document.getElementById('dots'); if(!box) return;
  let h='<button class="dot'+(pageIdx===0?' on':'')+'" onclick="goPage(0,{smooth:1})" title="列表"></button>';
  for(let k=0;k<OPEN.length;k++){
    const s=(lastExt||[]).find(function(x){return x.sid===OPEN[k];});
    const on=(pageIdx===1&&SID===OPEN[k])?' on':'';
    h+='<button class="dot'+on+'" onclick="goSessAt('+k+')" title="'+
       escHtml((s&&s.title)||OPEN[k].slice(0,8))+'"></button>';
  }
  box.innerHTML=h;
}
function goSessAt(k){
  const sid=OPEN[k]; if(!sid) return;
  if(sid===SID){ if(pageIdx!==1) goPage(1,{smooth:1}); return; }
  sessSlide(k>curOpenIdx()?-1:1);
}
function nextSess(){ sessSlide(-1); }
function prevSess(){ sessSlide(1); }
/* 会话页左右滑（#pgCtrl 上 touch-action:pan-y，横向手势不会被 #deck 抢走）。
   跟手：横拖时整页跟着手指挪（最多 35% 屏宽），松手过 22% 就换。
   换的那一刻接上 sessSlide 的整页滑出/滑入——以前是先把页面弹回原位、再播一段 26%
   的小位移动画，两次动作叠在一起，手感比列表页原生翻页差很多（2026-11-13）。 */
(function(){
  const pg=document.getElementById('pgCtrl'); if(!pg) return;
  const mv=document.getElementById('pgMove')||pg;
  let x0=0,y0=0,on=false,horiz=false,done=false,moved=0,animT=0,canNext=false;
  let vx=0,lx=0,lt=0;                  // 松手速度（px/ms）：滑出去的时长跟着手指走，别一律 150ms
  /* 本页常驻一个合成层：横拖时只改 transform，不再每挪一点就把几万节点的正文重画一遍
     （#pgMove 里没有 position:fixed 的子元素，提层不影响布局）。 */
  try{ mv.style.willChange='transform'; }catch(e){}
  /* 这次横拖「迎面来的那一页」：
     pvEl  = 邻居会话的预排层，横拖时就跟着手指一起进来（以前松手后才出现，拖的时候
             只看见本页在挪、露出来的是一片底色 —— 用户说的「不跟手」）；
     toList= 第一个会话再往右是回列表，列表本来就是 #deck 的一格，直接把 deck 的
             scrollLeft 按手指位置写进去，跟原生翻页一模一样。 */
  let pvEl=null, toList=false;
  /* 横拖/换页动画期间别重画正文：整块 innerHTML 一换就是几万节点重排，
     手指还在屏幕上时会一顿一顿的（2026-11-13 用户反馈「滑起来不顺」）。
     松手半秒后恢复，下一轮轮询自己会补齐。 */
  function dragHold(){ SESS_DRAG=1; clearTimeout(dragHold.t); dragHold.t=setTimeout(function(){ SESS_DRAG=0; },500); }
  function rest(animate){
    clearTimeout(animT);
    mv.style.transition=animate?'transform .16s ease-out':'';
    mv.style.transform='';
    if(animate) animT=setTimeout(function(){ mv.style.transition=''; },200);
  }
  pg.addEventListener('touchstart',function(e){
    if(e.touches.length!==1||pageIdx!==1){ on=false; return; }
    on=true; horiz=false; done=false; moved=0; pvEl=null; toList=false;
    if(deck&&deck.style.scrollSnapType) deck.style.scrollSnapType='';   // 上一次没来得及复原
    const i=curOpenIdx();
    canNext=(i<0)?OPEN.length>0:i+1<OPEN.length;
    x0=e.touches[0].clientX; y0=e.touches[0].clientY;
    vx=0; lx=x0; lt=performance.now();
    rest(false);                        // 上一次的位移/过渡先收干净
  },{passive:true});
  pg.addEventListener('touchmove',function(e){
    if(!on||done) return;
    const t=e.touches[0]; if(!t) return;
    const dx=t.clientX-x0, dy=t.clientY-y0;
    const w=pg.clientWidth||window.innerWidth||360;
    if(!horiz){
      if(Math.abs(dx)>10&&Math.abs(dx)>Math.abs(dy)*1.2){
        horiz=true; dragHold();
        const i=curOpenIdx();
        /* 迎面那一页先摆出来：右边拖 = 上一个会话/列表，左边拖 = 下一个会话 */
        const sid=(dx>0)?(i>0?OPEN[i-1]:null):((i>=0&&canNext)?OPEN[i+1]:null);
        if(dx>0&&i<=0&&deck&&pageIdx===1){   // 第一个会话再往右：回列表
          toList=true; deck.style.scrollSnapType='none';
        }else if(sid&&view==='live'){    // 预排的全彩正文只对得上实时视图
          pvEl=pvTake(sid);
          if(pvEl) pvShow(pvEl,(dx>0?-w:w)+dx);
        }
      }
      else return;
    }
    dragHold();
    const now=performance.now(), dt=now-lt;
    if(dt>0){ vx=vx*0.6+((t.clientX-lx)/dt)*0.4; lx=t.clientX; lt=now; }   // 平滑一下，单帧抖动不算速度
    if(toList){
      const dw=deckSlotW()||w;               // deck 的 scrollLeft 是内容坐标系
      deck.scrollLeft=Math.max(0,Math.min(dw,dw-dx));
      return;
    }
    const lim=pvEl?w:w*0.35;                 // 有迎面页就跟手跟满一屏，没有就别拉开太多
    moved=Math.max(-lim,Math.min(lim,dx));
    if(moved<0&&!canNext) moved=dx*0.18;     // 没有下一个会话：橡皮筋，别让人以为能翻
    mv.style.transform='translateX('+moved+'px)';
    if(pvEl) pvEl.style.transform='translateX('+((moved<0?1:-1)*w+moved)+'px)';
  },{passive:true});
  pg.addEventListener('touchend',function(){
    if(!on) return; on=false;
    if(!horiz){ rest(true); return; }
    dragHold();                          // 滑出去的这一下里也别重画
    const w=pg.clientWidth||window.innerWidth||360;
    if(toList){
      toList=false;
      const dw=deckSlotW()||w;
      const frac=(dw-deck.scrollLeft)/dw;      // 往列表拖走了多少
      if(frac>0.22||vx>0.5){
        done=true;
        /* snap 先关着滚到 0，滚稳了再吸附：不然一松手就被吸到最近一格，中间那段白跑 */
        goPage(0,{smooth:1});
        setTimeout(function(){ deck.style.scrollSnapType=''; },600);
      }else{
        deckNavUntil=Date.now()+400;           // 别让 deckSettle 半路瞬移对齐
        deck.scrollTo({left:dw,behavior:'smooth'});
        setTimeout(function(){ deck.style.scrollSnapType=''; },400);
      }
      return;
    }
    if(Math.abs(moved)>w*0.22){
      done=true;
      /* 还要走多远、按当前速度大概要多久：快甩就快点滑完，慢拖就慢慢滑，
         一律 150ms 会让每一次甩都像在等动画（用户：滑起来不顺） */
      const dir=moved<0?-1:1, spd=Math.abs(vx);
      const dur=(vx*dir>0.05)
        ? Math.max(80,Math.min(210,Math.round((w-Math.abs(moved))/Math.max(spd,0.5))))
        : 150;
      sessSlide(dir, null, dur);         // 从这个位置直接滑出去，不要先弹回
    }else{
      if(pvEl){                          // 迎面页跟着滑回去，两块看着是一整块
        pvEl.style.transition='transform .16s ease-out';
        pvEl.style.transform='translateX('+((moved<0?1:-1)*w)+'px)';
        const e=pvEl; pvEl=null;
        e.__pvHideT=setTimeout(function(){ pvHide(e); },180);
      }
      rest(true);
    }
  },{passive:true});
  pg.addEventListener('touchcancel',function(){
    on=false; horiz=false;
    if(toList){                          // 拖到一半被打断：摆回会话页那一格
      toList=false;
      const dw=deckSlotW()||pg.clientWidth||360;
      deckNavUntil=Date.now()+400;
      deck.scrollTo({left:dw,behavior:'smooth'});
      setTimeout(function(){ deck.style.scrollSnapType=''; },400);
    }
    if(pvEl){ const e=pvEl; pvEl=null; pvHide(e); }
    if(!done){ rest(true); }
  },{passive:true});
})();
/* 关闭会话：结束电脑上的进程，历史保留（列表里变「已退出」可召回）。
   关完按「前一个开着的 → 下一个 → 都没有就回列表」的顺序换过去。 */
function closeSess(){
  const m=(lastExt||[]).find(x=>x.sid===SID);
  if(!m||m.kind!=='computer'||m.state==='exited') return;
  askConfirm('关闭会话','关闭后电脑上这个会话的进程会结束，历史保留；列表里显示「已退出」，随时可以召回。',
    '关闭',true,async function(){
      const sid=SID, i=OPEN.indexOf(sid);
      const prev=OPEN[i-1], nxt=prev||OPEN[i+1]||null;
      try{
        const r=await xj('/api/ext_sess_close',{method:'POST',body:{sid:sid}});
        const d=r.json||{};
        if(!d.ok){ toast(d.error||'关闭失败',true); return; }
      }catch(e){ toast('网络错误',true); return; }
      delete ENTER_AT[sid];
      try{ localStorage.setItem('cb_sess_enter',JSON.stringify(ENTER_AT)); }catch(e){}
      await loadListData();
      /* 进程刚杀，列表那边还有 2~4s 的发现缓存：等它真的变成「已退出」再下结论，
         别杀成功了却弹「没关掉」（2026-11-13 用户实测）。 */
      let still=null;
      for(let n=0;n<6;n++){
        still=(lastExt||[]).find(x=>x.sid===sid);
        if(!still||still.state==='exited') break;
        await new Promise(r=>setTimeout(r,500));
        await loadListData();
      }
      if(still&&still.state!=='exited'){ toast('没关掉：进程还在，稍后再试',true); return; }
      toast('会话已关闭');
      if(nxt&&OPEN.indexOf(nxt)>=0) sessSlide(prev?1:-1, nxt);
      else goPage(0,{smooth:1});
    });
}
/* 会话页的「关闭会话」按钮只在「正在看一个还开着的电脑会话」时出现 */
function syncCloseBtn(){
  const b=document.getElementById('closeSessBtn'); if(!b) return;
  const m=(lastExt||[]).find(x=>x.sid===SID);
  const can=pageIdx===1&&m&&m.kind==='computer'&&m.state!=='exited';
  b.style.display=can?'flex':'none';
}
/* 换会话的过渡：整页滑出去、新会话从另一侧滑进来（和列表页原生翻页同一个观感）。
   以前是弹回原位再播一段 26% 的位移动画，看着像「抖一下」，手感很差（2026-11-13）。
   dir: -1 = 下一个会话；1 = 上一个会话（到头就是列表）。 */
function sessRest(animate){
  const c=document.getElementById('pgMove')||document.getElementById('pgCtrl'); if(!c) return;
  c.style.transition=animate?'transform .16s ease-out':'';
  c.style.transform='';
  if(animate) setTimeout(function(){ c.style.transition=''; },200);
}
function sessSlide(dir, targetSid, dur){
  const c=document.getElementById('pgMove')||document.getElementById('pgCtrl');
  const w=(document.getElementById('pgCtrl')||c||{}).clientWidth||window.innerWidth||360;
  const dOut=Math.max(70,Math.min(260,Math.round(dur||150)));   // 滑出去的时长（快甩短、慢拖长）
  const i=curOpenIdx();
  let sid=null, toList=false;
  if(targetSid) sid=(OPEN.indexOf(targetSid)>=0)?targetSid:null;
  else if(dir<0) sid=(i<0)?(OPEN[0]||null):(OPEN[i+1]||null);
  else if(i>0) sid=OPEN[i-1]; else toList=true;
  if(!sid&&!toList){ toast('已经是最后一个会话'); sessRest(true); return; }
  if(toList&&pageIdx!==1){ goPage(0,{smooth:1}); return; }
  if(pageIdx!==1){ switchTo(sid,{quiet:1,slide:1}); goPage(1,{smooth:1}); return; }  // 还在列表页：交给 deck 自己滑过去
  /* 有预排页：当前页滑出去、它从另一侧同时滑进来（两页都只动 transform），
     中间不排版不换 DOM，手感和原生翻页一致。落地后再悄悄把内容换到会话页上。 */
  /* 只在实时视图里用预排页：原样(xterm)/对话墙是另一套渲染，预排的全彩正文对不上 */
  const pv=(sid&&!toList&&view==='live')?pvTake(sid):null;
  if(pv){
    const cur=(function(){ const m=((c&&c.style.transform)||'').match(/-?[\d.]+/); return m?parseFloat(m[0]):0; })();
    const cb='cubic-bezier(.22,.61,.36,1)';
    pvShow(pv,(dir<0?w:-w)+cur);
    if(c){ c.style.transition='transform '+dOut+'ms '+cb; c.style.transform='translateX('+(dir<0?-w:w)+'px)'; }
    pv.style.transition='transform '+dOut+'ms '+cb;
    pv.style.transform='translateX(0)';
    setTimeout(function(){
      /* 预排页已经盖满屏幕：这会儿换内容/重置位移都看不见 */
      try{ switchTo(sid,{quiet:1,slide:1}); }catch(e){}
      if(c){ c.style.transition='none'; c.style.transform=''; }
      pvHide(pv);
      setTimeout(function(){ if(c) c.style.transition=''; },60);
    },dOut+12);
    return;
  }
  /* 松手后按手指惯性继续减速滑出去（ease-out），不是突然加速（ease-in 像被弹走） */
  if(c){ c.style.transition='transform '+dOut+'ms cubic-bezier(.22,.61,.36,1)'; c.style.transform='translateX('+(dir<0?-w:w)+'px)'; }
  setTimeout(function(){
    if(toList){
      goPage(0,{smooth:1});
      /* 面板滑回列表再放开位移：早放开会在滑动途中把会话页闪回来 */
      (function waitHome(n){
        if((pageIdx===0&&Math.abs(deck.scrollLeft)<2)||n>16){ sessRest(); return; }
        setTimeout(function(){ waitHome(n+1); },60);
      })(0);
      return;
    }
    switchTo(sid,{quiet:1,slide:1});
    if(!c) return;
    c.style.transition='none';
    c.style.transform='translateX('+(dir<0?w:-w)+'px)';   // 新会话先摆到另一侧
    requestAnimationFrame(function(){ requestAnimationFrame(function(){
      c.style.transition='transform 210ms cubic-bezier(.2,.7,.2,1)';
      c.style.transform='';
      setTimeout(sessRest,240);
    });});
  },dOut);
}
/* ── 全屏子页：终端列表 / 设置 ──────────────────────────────────────
   从列表页的入口点开，带 ← 返回；不参与左滑序列。z-index 5，压在终端浮层(6)之下。 */
function subOn(id,on){
  const p=document.getElementById(id); if(!p) return;
  p.classList[on?'add':'remove']('on');
  if(on) try{ p.querySelector('.pgin').scrollTop=0; }catch(e){}
  /* 开关子页也要记页面维：开着的子页算「人在哪」，关掉就回到底下的列表/会话页 */
  try{ trackPage(trkPageNow()); }catch(e){}
}
function termPageOpen(){ const p=document.getElementById('termPage'); return !!(p&&p.classList.contains('on')); }
function setPageOpen(){ const p=document.getElementById('setPage'); return !!(p&&p.classList.contains('on')); }
function openTermPage(){ subOn('termPage',true); try{ pageRemember(2); }catch(e){} termLoad(); }
function closeTermPage(){ subOn('termPage',false); try{ pageRemember(pageIdx); }catch(e){} }
function openSettings(){
  subOn('setPage',true);
  track('settings_open');   /* 放在 subOn 之后：这条要落在设置页上（页面维） */
  try{ pageRemember(3); }catch(e){}
  try{ renderVoice(); }catch(e){}
  try{ vsVerRefresh(); hsShowCached(); vsNetRefresh(); }catch(e){}
}
function closeSettings(){ subOn('setPage',false); try{ pageRemember(pageIdx); }catch(e){} }
async function switchTo(sid,opt){
  track('sess_switch');
  const m=(lastExt||[]).find(x=>x.sid===sid);
  /* 点开会话失败时（打不开/召不回）靠这行知道点到了谁、状态是什么 */
  try{ window.__cbDbg&&window.__cbDbg('Sw '+(sid||'').slice(0,8)+' '+
        ((m&&m.state)||'-')+(m&&m.resumable?' r':''),40); }catch(e){}
  /* noResume：调用方已经拉起过了（重新拉起按钮），别再唤一遍 */
  if(m&&m.state==='exited'&&m.resumable&&!(opt&&opt.noResume)){        // 死会话：选中即自动唤起，再进入
    toast('会话已退出，唤起中…');
    try{
      /* host 要在终端里开窗口并等它起来（最多 18s），默认 15s 会在成功前先断——
         用户看到「网络错误」再点一次，于是同一会话开两个窗口 */
      const r=await xj('/api/ext_resume',{method:'POST',body:{sid:sid},timeout:35000});
      const d=r.json;
      try{ window.__cbDbg&&window.__cbDbg('SwRs '+(sid||'').slice(0,8)+' '+
            (d.ok?'ok':String(d.error||'').slice(0,40)),60); }catch(e){}
      if(!d.ok){toast(d.error||'唤起失败',true);return;}
    }catch(e){toast('唤起失败',true);return;}
    toast('已唤起，等待进程就绪…');
    if(!await waitAlive(sid)){ toast('唤起超时，稍后在列表里点进去',true); return; }
  }
  goPage(1);
  if(sid===SID){ if(!(opt&&opt.quiet)) toast('已在控制该会话'); return; }
  try{ draftSave(true); }catch(e){}   // 上一个会话没发完的字先存好（换回来时还在）
  try{ liveCacheSave(true); }catch(e){}   // 走之前把这一屏整份存下：4 秒节流可能只存到半屏
  SID=sid;
  /* 记「最近进入」（左滑顺序、列表排序都用它）。但左右滑过来的不记：enterMark 会把
     刚进来的排到第 0 位，滑动序列就跟着变，下一次滑又指回来，看着像「划不动」，
     还会每次整屏重渲染卡一下。滑的过程中序列必须不动。 */
  if(!(opt&&opt.slide)) enterMark(sid);
  /* 输入框跟着会话走：清掉上一个会话的字，这一会话有草稿就放回来。
     可写状态也要复位——上一个只读会话（桌面版/只读终端）关掉的输入框不能带到这来
     （推送模式下没人再发 caps 回来重新打开它）。 */
  try{
    const ct=document.getElementById('txt');
    if(ct){ ct.value=''; ct.disabled=false;
      if(ct.dataset.ro){ delete ct.dataset.ro; ct.placeholder='说点什么…'; }
      fitTxt(); }
    const sb=document.getElementById('send'); if(sb) sb.disabled=false;
    DRAFT_CACHE=null; draftRestore();
  }catch(e){}
  delete NOTIFY[sid];      // 进会话 = 已读
  seenMark(sid);   // 停止重复提醒（后台请求，不记时长）
  try{ window.CB&&CB.setView&&CB.setView(sid); }catch(e){}
  try{ localStorage.setItem('codeboss_last_sid',sid); }catch(e){}
  try{ history.replaceState(null,'','/ext/'+sid); }catch(e){}
  // 复位控制面板状态
  lastTotal=-1;curTotal=-1;viewChosen=false;autoSwitched=false;fullColor=false;curMeta=null;
  lastFlowHtml='';lastHistHtml='';lastFrameHash=null;hostWin=null;rawRowsMin=0;curWin='';feedMsgs=[];DOC_MSGS=[];DRAFT='';DRAFT_WAIT=false;DRAFT_ST='';DRAFT_EL=0;DRAFT_EL_AT=0;DONE_EL=0;DLG_OPT_ON=false;
  try{ msgCachePrime(sid,380); }catch(e){}   // 墙的本地缓存：等滑动落定再画（同 seedFullHist 的理由），host 真值回来原地替换
  try{ pendRender(); pendLoad(); }catch(e){}
  try{ if(xt&&xt.dispose) xt.dispose(); }catch(e){}
  xt=null;
  document.getElementById('live2').innerHTML='';
  document.getElementById('liveflow').innerHTML='';
  document.getElementById('livehist').innerHTML='';
  document.getElementById('feed').innerHTML='';
  const lv=document.getElementById('live'); lv.style.display='block'; lv.textContent='加载中…';
  try{ liveCacheSeed(); }catch(e){}    // 这个会话刚看过：先把上次那一屏画出来，别干等一趟隧道
  setView('live');
  pfNeighbors();                       // 当前屏出来后再把左右邻居取好：下一次滑过去直接有内容
  /* 先把画面通道拉起来，列表放后台刷：loadListData 走一趟隧道，在手机上要 300~700ms，
     以前 await 它，滑过来先看到「加载中…」停一拍，人以为卡住了（2026-11-13 实测）。 */
  poll();
  try{ cmdsProbe(sid); }catch(e){}       // 换了项目：测试/发布入口跟着项目显隐
  if(PUSH.st==='on') pushReconnect(0);   // 新 sid：重开推送流，bootstrap 即 catch-up
  loadListData().then(function(){ openDots(); syncCloseBtn(); }).catch(function(){});
  try{ const be=document.getElementById('bar');
       window.__cbDbg&&window.__cbDbg('SwE '+(sid||'').slice(0,8)+' pg'+pageIdx
         +' bar'+(be.style.display||'css')+'h'+Math.round(be.offsetHeight),40); }catch(e){};
  if(!(opt&&opt.quiet)) toast('已切换：'+((curMeta&&curMeta.title)||sid.slice(0,8)));
}
/* 壳的系统通知点开：落到对应会话（安卓 MainActivity 调）。
   子页（终端/设置）盖在上面时要先关掉，否则切过去了还看不见。 */
window.__cbGoSess=function(sid){
  if(!sid) return;
  try{ closeTermPage(); closeSettings(); }catch(e){}
  switchTo(sid,{quiet:1});
};
/* ── 列表 / 详情 渲染 ── */
/* ── 会话列表的本地缓存 ────────────────────────────────────────────────
   以前每次进列表页/每 5 秒都去中继拿一遍整份列表：手机上表现为「点开列表先
   白一下」「按键被重建吞掉」，中继/开发机那头也多走一遍隧道。
   现在列表落 localStorage（按开发机分开存），开页先用缓存秒出，新加载/推送/
   手动动作才真正去拿；拿回来顺手更新缓存。 */
const SCK='codeboss_sess_cache_v1';
let LIST_AT=0, LIST_BUSY=false, listStaleAt=0;   /* >0 = 这轮列表是断网时的缓存，渲染要带降级标记 */
/* 缓存按「哪台开发机」分开存：cb_dev 这个 cookie 页面一加载就有，
   而 DEV.base 要等 /r/devices 回来才有值（那时缓存早就该用上了）。 */
function sessDevKey(){ try{ return curDev()||(location.host||''); }catch(e){ return ''; } }
function sessSave(){
  try{
    const cut=a=>(a||[]).slice(0,80);   // 只留最近的，别把 localStorage 撑爆
    localStorage.setItem(SCK,JSON.stringify({t:LIST_AT||Date.now(),dev:sessDevKey(),
      web:cut(lastWeb),ext:cut(lastExt)}));
  }catch(e){}
}
function sessSeed(){                   // 开页先用缓存画一版（没有任何网络等待）
  try{
    const d=JSON.parse(localStorage.getItem(SCK)||'null');
    if(!d||!Array.isArray(d.ext)||!d.ext.length) return false;
    if(d.dev!==sessDevKey()) return false;        // 换了开发机：旧列表不认
    lastWeb=d.web||[]; lastExt=d.ext||[]; LIST_AT=d.t||0;
    curMeta=lastExt.find(x=>x.sid===SID)||null;
    refreshHeader(); renderList();
    return true;
  }catch(e){ return false; }
}
/* soft=true：手上已经有列表、且 15 秒内刚刷新过 → 只重画（时间文案刷新），不发请求。
   新加载、动作之后、推送断流后的兜底轮询都传 false/不传，保证拿到真数据。 */
async function loadListData(soft){
  if(LIST_BUSY) return;
  if(soft&&lastExt.length&&Date.now()-LIST_AT<15000){ renderList(); return; }
  LIST_BUSY=true;
  let web=[],ext=[],gotExt=false,gotWeb=false;
  try{
    try{const r=await xj('/api/sessions');
      /* 403 且不是 JSON（套餐超时是 JSON 带 quota）：说明选中的开发机已被删除/无权。
         401 同理：匿名设备被后台删除后设备会话失效，中继回「先在 /r/ 登录」。
         页面还挂着旧设备的会话列表就是脏数据——回入口让中继重新选台（没有设备就是
         扫码/接入页；匿名用户直接就是扫码页）。 */
      if((r.status===401||r.status===403)&&!r.json){ location=(DEV.base||(PLAND&&PLAND.relay_base)||'')+'/'; return; }
      if(r.ok&&r.json){ web=r.json.sessions; gotWeb=true; }}catch(e){}
    try{const r=await xj('/api/ext_sessions'); if(r.ok&&r.json){ ext=r.json.sessions; gotExt=true;
      /* agent 列表跟着列表一起回来（老版本 host 不带 → 留空，走 /api/ext_agents）。
         切换条只要这一路成功就在，不再怕单独那次请求抖掉。 */
      if(r.json.agents){ AGS=r.json.agents; AGS_T=Date.now(); }
      /* 新建终端的目录下拉也跟着回来：整页 HTML 里不再嵌开发机目录（嵌了每台机器的
         页面就不一样，中继没法跨设备复用同一份缓存，冷切要重拉 611KB 等十几秒）。
         老 host 不带 dirs → 留空，用户自己填。 */
      if(r.json.dirs){
        const dl=document.getElementById('dirs'), cw=document.getElementById('n_cwd');
        if(dl) dl.innerHTML=r.json.dirs.map(d=>'<option value="'+esc(d)+'">').join('');
        if(cw&&!cw.value&&r.json.dirs.length) cw.value=r.json.dirs[0];
      } }}catch(e){}
    /* 这一轮没拿到（断网/中继抖）：继续用缓存里的列表，别把页面清空 */
    if(gotWeb) lastWeb=web;
    if(gotExt){ lastExt=ext; LIST_AT=Date.now(); listStaleAt=0; }
    else if(lastExt.length){ listStaleAt=Date.now(); }   /* 断网给了缓存：横幅用 LIST_AT 算数据年龄 */
    curMeta=lastExt.find(x=>x.sid===SID)||null;
    refreshHeader();
    renderList();
    if(gotExt) sessSave();
    if(!DEV.init) devLoad();          // 进来时没取到就补一次；成功过就不再重复请求
    refreshModelCard(false);
    agBarInit();                  // 装了两种以上才出现；选中态决定列表/模型卡/＋
    renderDetailLite();
  }finally{ LIST_BUSY=false; }
}
/* 品牌标（Simple Icons 的路径，CC0）：Anthropic / OpenAI。颜色由 .ag.claude/.codex 给。 */
const ICO_CLAUDE='<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m4.7144 15.9555 4.7174-2.6471.079-.2307-.079-.1275h-.2307l-.7893-.0486-2.6956-.0729-2.3375-.0971-2.2646-.1214-.5707-.1215-.5343-.7042.0546-.3522.4797-.3218.686.0608 1.5179.1032 2.2767.1578 1.6514.0972 2.4468.255h.3886l.0546-.1579-.1336-.0971-.1032-.0972L6.973 9.8356l-2.55-1.6879-1.3356-.9714-.7225-.4918-.3643-.4614-.1578-1.0078.6557-.7225.8803.0607.2246.0607.8925.686 1.9064 1.4754 2.4893 1.8336.3643.3035.1457-.1032.0182-.0728-.164-.2733-1.3539-2.4467-1.445-2.4893-.6435-1.032-.17-.6194c-.0607-.255-.1032-.4674-.1032-.7285L6.287.1335 6.6997 0l.9957.1336.419.3642.6192 1.4147 1.0018 2.2282 1.5543 3.0296.4553.8985.2429.8318.091.255h.1579v-.1457l.1275-1.706.2368-2.0947.2307-2.6957.0789-.7589.3764-.9107.7468-.4918.5828.2793.4797.686-.0668.4433-.2853 1.8517-.5586 2.9021-.3643 1.9429h.2125l.2429-.2429.9835-1.3053 1.6514-2.0643.7286-.8196.85-.9046.5464-.4311h1.0321l.759 1.1293-.34 1.1657-1.0625 1.3478-.8804 1.1414-1.2628 1.7-.7893 1.36.0729.1093.1882-.0183 2.8535-.607 1.5421-.2794 1.8396-.3157.8318.3886.091.3946-.3278.8075-1.967.4857-2.3072.4614-3.4364.8136-.0425.0304.0486.0607 1.5482.1457.6618.0364h1.621l3.0175.2247.7892.522.4736.6376-.079.4857-1.2142.6193-1.6393-.3886-3.825-.9107-1.3113-.3279h-.1822v.1093l1.0929 1.0686 2.0035 1.8092 2.5075 2.3314.1275.5768-.3218.4554-.34-.0486-2.2039-1.6575-.85-.7468-1.9246-1.621h-.1275v.17l.4432.6496 2.3436 3.5214.1214 1.0807-.17.3521-.6071.2125-.6679-.1214-1.3721-1.9246L14.38 17.959l-1.1414-1.9428-.1397.079-.674 7.2552-.3156.3703-.7286.2793-.6071-.4614-.3218-.7468.3218-1.4753.3886-1.9246.3157-1.53.2853-1.9004.17-.6314-.0121-.0425-.1397.0182-1.4328 1.9672-2.1796 2.9446-1.7243 1.8456-.4128.164-.7164-.3704.0667-.6618.4008-.5889 2.386-3.0357 1.4389-1.882.929-1.0868-.0062-.1579h-.0546l-6.3385 4.1164-1.1293.1457-.4857-.4554.0608-.7467.2307-.2429 1.9064-1.3114Z"/></svg>';
const ICO_CODEX='<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M22.2819 9.8211a5.9847 5.9847 0 0 0-.5157-4.9108 6.0462 6.0462 0 0 0-6.5098-2.9A6.0651 6.0651 0 0 0 4.9807 4.1818a5.9847 5.9847 0 0 0-3.9977 2.9 6.0462 6.0462 0 0 0 .7427 7.0966 5.98 5.98 0 0 0 .511 4.9107 6.051 6.051 0 0 0 6.5146 2.9001A5.9847 5.9847 0 0 0 13.2599 24a6.0557 6.0557 0 0 0 5.7718-4.2058 5.9894 5.9894 0 0 0 3.9977-2.9001 6.0557 6.0557 0 0 0-.7475-7.0729zm-9.022 12.6081a4.4755 4.4755 0 0 1-2.8764-1.0408l.1419-.0804 4.7783-2.7582a.7948.7948 0 0 0 .3927-.6813v-6.7369l2.02 1.1686a.071.071 0 0 1 .038.052v5.5826a4.504 4.504 0 0 1-4.4945 4.4944zm-9.6607-4.1254a4.4708 4.4708 0 0 1-.5346-3.0137l.142.0852 4.783 2.7582a.7712.7712 0 0 0 .7806 0l5.8428-3.3685v2.3324a.0804.0804 0 0 1-.0332.0615L9.74 19.9502a4.4992 4.4992 0 0 1-6.1408-1.6464zM2.3408 7.8956a4.485 4.485 0 0 1 2.3655-1.9728V11.6a.7664.7664 0 0 0 .3879.6765l5.8144 3.3543-2.0201 1.1685a.0757.0757 0 0 1-.071 0l-4.8303-2.7865A4.504 4.504 0 0 1 2.3408 7.872zm16.5963 3.8558L13.1038 8.364 15.1192 7.2a.0757.0757 0 0 1 .071 0l4.8303 2.7913a4.4944 4.4944 0 0 1-.6765 8.1042v-5.6772a.79.79 0 0 0-.407-.667zm2.0107-3.0231l-.142-.0852-4.7735-2.7818a.7759.7759 0 0 0-.7854 0L9.409 9.2297V6.8974a.0662.0662 0 0 1 .0284-.0615l4.8303-2.7866a4.4992 4.4992 0 0 1 6.6802 4.66zM8.3065 12.863l-2.02-1.1638a.0804.0804 0 0 1-.038-.0567V6.0742a4.4992 4.4992 0 0 1 7.3757-3.4537l-.142.0805L8.704 5.459a.7948.7948 0 0 0-.3927.6813zm1.0976-2.3654l2.602-1.4998 2.6069 1.4998v2.9994l-2.5974 1.4997-2.6067-1.4997Z"/></svg>';
const ICO_SPK='<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 9.5v5h3.6L13 19V5L7.6 9.5z"/><path d="M16.8 8.6a4.6 4.6 0 0 1 0 6.8"/></svg>';
const ICO_MUTE='<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 9.5v5h3.6L13 19V5L7.6 9.5z"/><path d="M16.5 9.5l5 5M21.5 9.5l-5 5"/></svg>';
/* 标题栏的静音指示：免打扰/静音正在生效时是划掉的喇叭（红），否则正常喇叭（灰）。
   用户就是在这里分不清「提醒坏了」和「自己被静音了」（2026-11-13 免打扰时段）。 */
function hMuteSync(){
  const b=document.getElementById('hMute'); if(!b) return;
  let q=false, why='';
  try{
    if(typeof muteOn==='function'&&muteOn()){ q=true; why='已静音（长按音量下键恢复）'; }
    else if(typeof dndActive==='function'&&dndActive()){ q=true; why='免打扰时段：不出声、不播报'; }
  }catch(e){ return; }
  const k=q?'1':'0';
  if(b.getAttribute('data-q')===k) return;
  b.setAttribute('data-q',k);
  b.className='hm'+(q?' q':'');
  b.innerHTML=q?ICO_MUTE:ICO_SPK;
  b.title=why||'提醒音与语音播报开着';
  b.style.display='';
}
try{ setInterval(hMuteSync,1000); }catch(e){}
function refreshHeader(){
  /* 标题前的品牌图标：不只是 agent 标识，它本身就是会话状态灯，
     所以有当前会话就显示。品牌表收一个显示一个（没收录的不占位）。 */
  const AG_ICO={claude:ICO_CLAUDE,codex:ICO_CODEX};
  const ag=document.getElementById('hAg');
  if(ag){
    if(curMeta){
      const k=curMeta.agent||'claude';
      const ico=AG_ICO[k]||'';
      ag.className='ag '+escHtml(k);
      ag.innerHTML=ico;   /* 品牌标代替文字：小标签里放不下「Claude」还把标题挤走 */
      ag.style.display=ico?'':'none';
      stPaint();
    }else ag.style.display='none';
  }
  try{ hMuteSync(); }catch(e){}
  if(curMeta){
    document.getElementById('hTitle').textContent=curMeta.title;
    document.title=curMeta.title+' · '+cbBrand().label;
  }else if(!SID){
    document.getElementById('hTitle').textContent=cbBrand().app;
    document.title=cbBrand().app;
  }
}
let MC={t:0,txt:'',ag:''};
async function refreshModelCard(force){
  const ag=CUR_AG||'claude';
  if(!force && MC.ag===ag && Date.now()-MC.t<15000) return;
  MC.t=Date.now(); MC.ag=ag;
  try{
    const r=await xj('/api/ext_model'+(ag!=='claude'?('?agent='+encodeURIComponent(ag)):''));
    const d=r.json||{};
    if(d.ok){const c=d.current||d.machine||{};
      MC.txt=(c.provider_name||'')+(c.model?(' · '+c.model):'');}
  }catch(e){}
  const el=document.getElementById('modelVal');
  if(el) el.textContent=MC.txt||'—';
  const lb=document.getElementById('modelLbl');
  if(lb){
    const an=(AGS||[]).find(a=>a.key===ag);
    lb.textContent=(CUR_AG&&an)?('当前模型 · '+agLabel(an.label||CUR_AG)):'当前模型';
  }
}
/* ── 收件箱：待你决定 + 刚完成 ─────────────────────────────────────────
   列表页的两个子页（页签切换）：收件箱是核心，开屏默认就停在这一页；
   会话列表原样保留在「会话」页签里。不是卡片入口——内容直接铺在页面上。
     待你决定：开发机提醒队列（/api/ext_pending）——和通知栏是同一份。
     刚完成  ：提醒历史（notify_hist，重启不丢），✅/❌/⛔ 来自转录证据。
   8 秒内不重复请求——列表本来就每 5 秒刷一次，别把开发机打爆。 */
let PEND_LIST=[], PEND_AT=0, DONE_LIST=[], ASK_LIST=[], DONE_AT=0, LV='ib', IB_AUTO=1;
let LED_LIST=[], LED_AT=0;
function listView(v,manual){
  if(manual) IB_AUTO=0;              /* 用户自己点的页签：后续空/满都不再抢默认 */
  LV=(v==='sess')?'sess':'ib';
  const pg=document.getElementById('pgList');
  if(pg) pg.classList.toggle('ibview', LV!=='sess');
  const a=document.getElementById('ltIb'), b=document.getElementById('ltSess'),
        lb=document.getElementById('ltIbL');
  if(a) a.classList.toggle('on', LV==='ib');
  if(b) b.classList.toggle('on', LV==='sess');
  /* 这一页叫「工作台」：收件箱的内容 + 台账合并（用户 2026-11-19 拍板，两条线同一页） */
  if(lb) lb.textContent='工作台';
  const ip=document.getElementById('ibPane');
  if(ip) ip.style.display=(LV==='ib')?'block':'none';
  if(LV==='ib'){                     /* 进这一页就拉最新：节流戳归零 */
    PEND_AT=0; DONE_AT=0; LED_AT=0; inboxPaint();
    try{ ibPendLoad(); }catch(e){}
    try{ ibDoneLoad(); }catch(e){}
    try{ ledgerLoad(); }catch(e){}
    try{ ibArtsLoad(); }catch(e){}
  }
}
/* ── 台账：任务（=sid）生命周期，老板视图（批次二 #5）────────────────── */
/* 本壳是哪条产品线：query 优先，cb_product cookie 兜底（壳每次启动种）。
   缺省 codeboss——老壳/浏览器直开都是工头线（PRODUCT_DEFAULT 同款语义）。 */
function cbLine(){
  var m, p='';
  if((m=(location.search||'').match(/[?&]product=([^&]*)/))) p=m[1];
  if(!p && (m=(document.cookie||'').match(/(?:^|;\s*)cb_product=([^;]*)/))) p=m[1];
  return (p||'').trim()||'codeboss';
}
/* 两条线的能力差异总开关（2026-11-19 计划阶段 0）：只管默认视图和入口显隐，
   功能路由两条线都在——是体验差异不是权限边界。未知产品线回落 codeboss。
   分线门禁（2026-11-20）：页面里的产品字面量只准出现在 cbLine/lineCaps/lineName
   三个函数里（tests/test_line_architecture.py），别处写死 'codeboss' 就是红的。 */
function lineCaps(line){
  if(line==='agentboss')
    return {ledgerDefault:true, testCards:false, deploy:false, artifactsInbox:true,
            dispatch:true, coding:false};
  if(line==='codeboss')
    return {ledgerDefault:false, testCards:true, deploy:true, artifactsInbox:false,
            dispatch:false, coding:true};
  /* 未知/老壳回落工头线。新线要在上面加自己的一支：
     tests/test_line_architecture.py 盯住「服务端认、页面不认」的线。 */
  return lineCaps('codeboss');
}
/* 编码模式生效值（窗口核心 Phase 2）：设置页的显式开关优先（CODING_ON 由
   prefLoad 从开发机带回，ext_pref 按线算默认），没带回就跟产品线默认。
   pv 预览 / 产出物编辑入口都过这道闸。 */
var CODING_ON;
function codingOn(){
  if(CODING_ON===undefined) return lineCaps(cbLine()).coding;
  return !!CODING_ON;
}
/* 线名表：新增一条线只改这里（和 lineCaps），别在别处拼产品名。 */
function lineName(line){
  if(line==='agentboss') return {label:'代理工头', app:'AgentBoss'};
  if(line==='codeboss') return {label:'口袋工头', app:'CodeBoss'};
  return lineName('codeboss');   /* 未知/老壳回落工头线 */
}
/* 页面入口登记表（2026-11-20）：每个 data-* 入口属于哪条线、为什么，写在这里。
   加新入口不登记 → tests/test_page_entries.mjs 红；单线入口挂的 cap 必须和
   lineCaps 一一对上（表不能是摆设）。改归属先改这张表，再改渲染代码。 */
const PAGE_ENTRIES = {
  act:      {lines:'both', why:'通用 onclick 动作名（按钮都经它分发）'},
  sid:      {lines:'both', why:'会话 id，所有卡片/行点进去都是这个会话'},
  csid:     {lines:'both', why:'终端视图的会话 id'},
  ag:       {lines:'both', why:'agent 名参数（哪家 agent 在干）'},
  cwd:      {lines:'both', why:'会话工作目录参数'},
  p:        {lines:'both', why:'目录/文件路径参数'},
  q:        {lines:'both', why:'搜索关键词'},
  cn:       {lines:'both', why:'连接名参数'},
  via:      {lines:'both', why:'按键来源参数（ctl/plat，按键日志用）'},
  opt:      {lines:'both', why:'审批选项序号'},
  k:        {lines:'both', why:'按键名参数'},
  n:        {lines:'both', why:'命令输出引用块序号'},
  t:        {lines:'both', why:'会话标题（重命名对话框经属性带过去）'},
  cur:      {lines:'both', why:'命令块高亮位置标记'},
  multi:    {lines:'both', why:'命令块多选标记'},
  do:       {lines:'both', why:'确认按钮的动作参数'},
  done:     {lines:'both', why:'已验收任务卡（点进会话看结果）'},
  pend:     {lines:'both', why:'待决定卡片（审批/选择）'},
  ledger:   {lines:'both', why:'任务台账行（点进会话）'},
  ibview:   {lines:'both', why:'工作台页签切换'},
  tv:       {lines:'both', why:'任务收起/恢复/删除'},
  dp:       {lines:'both', why:'文档卡片（docOpen 预览）'},
  ibfind:   {lines:'both', why:'工作台上的「搜索」'},
  findgo:   {lines:'both', why:'执行搜索'},
  findsid:  {lines:'both', why:'搜索结果进对应会话'},
  respawn:  {lines:'both', why:'失效会话重新拉起'},
  hostterm: {lines:'both', why:'电脑上开着的终端（接着看/接管）'},
  'remote-dev':  {lines:'both', why:'跨机结果的机器 id'},
  'remote-name': {lines:'both', why:'跨机结果的机器名'},
  tier:     {lines:'both', why:'账号页套餐行'},
  model:    {lines:'both', why:'账号页套餐行里的模型参数'},
  kick:     {lines:'both', why:'账号页设备踢下线'},
  tcrun:    {lines:'codeboss', cap:'testCards', why:'测试卡「重跑」——工头线才做测试验收'},
  ibnew:    {lines:'agentboss', cap:'dispatch', why:'工作台「派单」——指定 agent+目录开新会话，工头线没有派单这回事'},
  arts:     {lines:'agentboss', cap:'artifactsInbox', why:'台账行的产出物快捷查看'},
  art:      {lines:'agentboss', cap:'artifactsInbox', why:'产出物收件箱条目'},
  'art-sid':  {lines:'agentboss', cap:'artifactsInbox', why:'产出物所属会话'},
  'art-name': {lines:'agentboss', cap:'artifactsInbox', why:'产出物文件名'},
};
/* 这个会话的项目能跑什么（2026-11-19）：测试/发布入口跟着项目目录走，不再写死 npm。
   项目里没配就不显示，点了也不会白跑一张红卡。探测失败（老 host/网络）维持现状。 */
let CMDS_AVAIL=null;
async function cmdsProbe(sid){
  if(!sid) return;
  try{
    const r=await xj('/api/cmds?sid='+encodeURIComponent(sid));
    if(!r||!r.json||!r.json.ok) return;
    CMDS_AVAIL=r.json; CMDS_AVAIL.sid=sid;
  }catch(e){ return; }
  if(SID===sid) applyCmdsUi();
}
function applyCmdsUi(){
  const c=(CMDS_AVAIL&&CMDS_AVAIL.cmds)||{}, d=(CMDS_AVAIL&&CMDS_AVAIL.deploy)||{};
  const any=['test','lint','build'].some(function(k){ return !!c[k]; });
  const tb=document.getElementById('tcBtn');
  if(tb) tb.style.display=(lineCaps(cbLine()).testCards&&any)?'':'none';
  const db=document.getElementById('dpBtn');
  if(db) db.style.display=(lineCaps(cbLine()).deploy&&d.run)?'':'none';
  ['test','lint','build'].forEach(function(k){
    const b=document.getElementById('cmdB_'+k);
    if(b) b.style.display=c[k]?'':'none';
  });
  const nn=document.getElementById('cmdNone');
  if(nn) nn.style.display=any?'none':'';
}
/* 台账行内产出物（B4）：切到那个会话，再看它的产出物（B1 收件箱的分会话视图） */
async function artsPeek(sid){
  try{ await switchTo(sid); }catch(e){}
  setTimeout(function(){ try{ filesOpen(); }catch(e){} }, 400);
}
/* ── 交付链（2026-11-19 计划 A4）: 发布 → 健康卡 → 不对就回滚 ────────
   跑什么由服务端 products/<线>/product.json 的 deploy 配置定；这里只做触发和呈现。
   发布可能跑几分钟：POST 只开跑，结果靠轮询健康卡。 */
let DEP_AT=0;
/* 网页预览（2026-11-19）：agent 起的 dev server 在手机上直接看真页面。
   端口列表由 host 的 /pv 页给（本机在听的），浮层里点一个就进。
   带 sid 是因为列表顶上还有一条「电脑上正在看」——file:// 页面要按这个会话的
   目录范围换成 /pvfile 才能打开。 */
function previewOpen(){
  if(!codingOn()){ toast('编码模式没开：设置 → 外观 → 编码模式',true); return; }
  const base=location.origin||'';
  if(!base){ toast('这个页面拿不到本机地址',true); return; }
  sheetOpen(base+'/pv?sid='+encodeURIComponent(SID||''), '预览');
}
/* 会话里点 agent 给出的本机网页地址（已换成 /pv/<端口>/）：浮层里打开。
   地址要先补成绝对地址：sheetOpen 只认和中继同源的绝对地址，/pv/... 会被它判成
   跨源、直接 location.href 整页跳走——用户点了就全屏，返回不回来（2026-11-20）。 */
function pvOpen(href){
  var h=String(href||'');
  try{ if(h.charAt(0)==='/') h=(location.origin||'')+h; }catch(e){}
  try{ sheetOpen(h, '预览'); }
  catch(e){ location.href=h; }
}
function isIOS(){ return /iPhone|iPad|iPod/i.test(navigator.userAgent||''); }
/* 装到手机：会话项目里构建出的 APK，点了直接下载安装（壳收到 APK 走系统安装器） */
async function apkOpen(){
  if(!SID){ toast('先打开一个会话（APK 在它的项目里找）',true); return; }
  /* iPhone 不认 APK：导航过去主框架会被换成一个打不开的页面，看着像「断网了」 */
  if(isIOS()){ toast('这是安卓安装包（APK），iPhone 装不了',true); return; }
  let r;
  try{ r=await xj('/api/apk_find?sid='+encodeURIComponent(SID)); }
  catch(e){ toast('网络错误',true); return; }
  const items=(r&&r.json&&r.json.items)||[];
  if(!items.length){ toast('这个项目里没找到 .apk（先构建一次）',true); return; }
  if(items.length>1) toast('装最新的 '+items[0].name+'（还有 '+(items.length-1)+' 个）');
  /* 不能 location.href：主框架会被导航成下载请求。临时 <a download> 让
     WebView 的 DownloadListener / 浏览器下载接管，页面留在原地。 */
  const a=document.createElement('a');
  a.href=items[0].url; a.download=items[0].name||'app.apk'; a.style.display='none';
  document.body.appendChild(a); a.click();
  setTimeout(function(){ a.remove(); }, 1000);
}
/* 拍屏：按需拍一张开发机屏幕，在浮层里看（不自动推：整屏里有别人的窗口） */
function shotOpen(){
  const base=location.origin||'';
  if(!base){ toast('这个页面拿不到本机地址',true); return; }
  sheetOpen(base+'/api/shot', '拍屏');
}
/* 模型路由：手机上看/切各 agent 的模型（底座是本机的 magpie 网关，不在就明说降级） */
function routerOpen(){
  const base=location.origin||'';
  if(!base){ toast('这个页面拿不到本机地址',true); return; }
  sheetOpen(base+'/api/model_router/ui', '模型路由');
}
function deployOpen(){
  if(!SID){ toast('先打开一个会话（发布在那个项目的目录里跑）',true); return; }
  subOn('depPage',true); deployLoad(true);
}
function deployClose(){ subOn('depPage',false); }
function depBadge(st){ return st==='pass'?'🟢 成功':(st==='fail'?'🔴 失败':'⚪ 未跑'); }
function depHealth(h){
  return '<div class="fcard" style="padding:10px 12px;margin-bottom:10px">'
    +'<div style="display:flex;align-items:center;gap:8px">'
    +'<span style="flex:none;font-size:12px">'+(h.ok?'🟢 线上正常':'🔴 探活失败')+'</span>'
    +'<span class="meta" style="flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">'
    +esc(h.url||'没配健康检查地址')+'</span></div>'
    +'<div class="meta">'+(h.code?('HTTP '+h.code+' · '):'')+(h.ms||0)+'ms'
    +(h.error?(' · '+esc(h.error)):'')+'</div></div>';
}
function depRows(items){
  if(!items.length) return '<div class="nhEmpty">还没有发布记录</div>';
  return items.map(function(it){
    const tail=it.tail?('<pre style="white-space:pre-wrap;word-break:break-all;font-size:11px;max-height:120px;overflow:auto;background:#111111;border-radius:6px;padding:6px;margin:6px 0 0">'+esc(it.tail)+'</pre>'):'';
    return '<div class="fcard" style="padding:10px 12px;margin-bottom:10px">'
      +'<div style="display:flex;align-items:center;gap:8px">'
      +'<span style="flex:none;font-size:12px">'+depBadge(it.state)+'</span>'
      +'<code style="flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:12px;color:#cccccc">'
      +esc((it.kind==='rollback'?'回滚 ':'发布 ')+it.cmd)+'</code></div>'
      +'<div class="meta">'+fmtTm(it.ts)+' · '+Math.round(it.dur||0)+'s · 退出码 '+it.rc+'</div>'+tail+'</div>';
  }).join('');
}
async function deployLoad(force){
  if(!force && Date.now()-DEP_AT<3000) return null;
  DEP_AT=Date.now();
  const h=document.getElementById('depHealth'), l=document.getElementById('depList');
  try{
    const r=await xj('/api/health_card',{timeout:20000});
    const d=(r&&r.json)||{};
    if(h) h.innerHTML=depHealth(d.health||{});
    if(l) l.innerHTML=depRows(d.deploys||[]);
    return d.deploys||[];
  }catch(e){ if(h) h.innerHTML='<div class="nhEmpty">读不到健康卡</div>'; return null; }
}
async function deployRun(){
  askConfirm('发布','确认发布到线上？发布跑在当前会话的项目目录里。','发布',false,
    function(){ deployPost('/api/deploy_run'); });
}
async function deployRollback(){
  askConfirm('回滚','回滚到上一个成功发布点？','回滚',true,
    function(){ deployPost('/api/deploy_rollback'); });
}
async function deployPost(path){
  const items=(await deployLoad(true))||[];
  const prevTs=items.length?items[0].ts:0;
  const l=document.getElementById('depList');
  if(l) l.innerHTML='<div class="nhEmpty">正在跑…（结果出来自动刷新）</div>';
  let r;
  try{ r=await xj(path,{method:'POST',body:{sid:SID}}); }
  catch(e){ toast('网络错误',true); deployLoad(true); return; }
  if(!(r&&r.json&&r.json.ok)){
    toast('没跑起来'+((r&&r.json&&r.json.error)?'：'+r.json.error:''),true);
    deployLoad(true); return;
  }
  depPoll(prevTs,200);                  /* 3s × 200 ≈ 10 分钟上限 */
}
function depPoll(prevTs,left){
  setTimeout(async function(){
    const items=await deployLoad(true);
    if(!items) return;
    const newest=items.length?items[0].ts:0;
    if(newest>prevTs) return;
    if(left<=0){ toast('发布还在跑，稍后回来看',true); return; }
    depPoll(prevTs,left-1);
  },3000);
}
/* 端名/系列名由 host 吐（/api/brand，批次二）：标题、关于页、页脚都用这一份。
   拿不到（离线/老 host）回落内置常量——文案缺省值绝不能变成空白或报错。 */
var __cbBrand=null;
function cbBrand(){
  if(__cbBrand) return __cbBrand;
  var line=cbLine(), nm=lineName(line);
  __cbBrand={series:'aiboss',product:line,label:nm.label,app:nm.app};
  try{
    var x=new XMLHttpRequest();
    x.open('GET','/api/brand',true);
    x.onload=function(){
      try{
        var d=JSON.parse(x.responseText||'{}');
        if(d&&d.series){__cbBrand=d; cbTitle(); capsApply();}
      }catch(e){}
    };
    x.send();
  }catch(e){}
  return __cbBrand;
}
/* 标题按品牌刷新一次（cbBrand 异步回来后调） */
function cbTitle(){
  var b=cbBrand();
  if(typeof curMeta!=='undefined'&&curMeta){
    document.title=curMeta.title+' · '+b.label;
  }else if(typeof SID!=='undefined'&&!SID){
    document.title=b.app;
    var t=document.getElementById('hTitle');
    if(t) t.textContent=b.app;
  }
}
function ledgerBadge(s){
  return ({running:'进行中',deciding:'等你拍板',done:'办结待验收',
           accepted:'已通过',rejected:'已打回',
           dispatching:'派单中',failed:'派单失败'})[s]||'进行中';
}
function fmtLedgerDur(sec){
  sec=Math.round(Number(sec)||0);
  if(!sec) return '—';
  if(sec<3600) return Math.round(sec/60)+' 分钟';
  return (Math.round(sec/360)/10)+' 小时';
}
/* 验收按钮:只有「办结待验收」态有;已验收/在跑/拍板中一律没有(重复验收=搅账)。
   sid 走 data 属性+文档级委托(和 data-ledger 同路),不进 onclick 的 JS 字符串——
   内联拼接是 HTML 属性+JS 字符串双上下文,实体转义防不住。 */
function ledgerActions(state, sid){
  if(state!=='done') return '';
  return '<div style="display:flex;gap:8px;margin-top:8px">'+
    '<button class="lt" data-act="accept" style="flex:1;background:#1d3a2a;color:#7ee2a8;border-color:#2a5a3d">通过</button>'+
    '<button class="lt" data-act="reject" style="flex:1;background:#3a1d1d;color:#e88;border-color:#5a2a2a">打回</button>'+
    '</div>';
}
/* 验收一律走页面内浮层(#dlg),不用原生 confirm()/prompt()——WebView 里原生对话框
   要么静默返回、要么弹个带网址的系统框,用户点了没反应（2026-11-19 实报）。 */
async function ledgerTaskAccept(sid){
  /* 点通过前先看清这轮干了什么（A5 三句摘要；拉不到/算不出就照旧弹确认） */
  let sum='';
  try{
    const r=await xj('/api/accept_summary?sid='+encodeURIComponent(sid));
    sum=(r&&r.json&&r.json.text)||'';
  }catch(e){}
  /* 误触通过=把没验收的活放行:二次确认必须挡一道 */
  askConfirm('通过验收',
    (sum?'<div style="white-space:pre-wrap;font-size:13px;color:#cccccc;background:#141414;border-radius:8px;padding:8px;margin-bottom:8px">'+esc(sum)+'</div>':'')
    +'确认通过这个任务？','通过',false,function(){ ledgerAcceptPost(sid); });
}
async function ledgerAcceptPost(sid){
  const r=await xj('/api/task_accept',{method:'POST',body:{sid:sid,verdict:'accept'}});
  toast((r&&r.json&&r.json.ok)?'已通过':'验收失败', !(r&&r.json&&r.json.ok));
  if(r&&r.json&&r.json.ok){ LED_AT=0; ledgerLoad(); }   /* 刚改的状态立刻重画，别吃节流 */
}
function ledgerTaskReject(sid){
  askConfirm('打回',
    '<textarea id="dlgNote" placeholder="打回意见（会发给 agent）" '+
    'style="width:100%;height:96px;box-sizing:border-box;background:#141414;color:#f5f5f5;'+
    'border:1px solid #333;border-radius:8px;padding:8px;font-size:14px"></textarea>',
    '打回',true,function(){
      const el=document.getElementById('dlgNote');
      const n=el?String(el.value||'').trim():'';
      if(!n){ toast('写点打回意见再提交',true); return; }
      ledgerRejectPost(sid,n);
    });
}
async function ledgerRejectPost(sid,n){
  const r=await xj('/api/task_accept',{method:'POST',body:{sid:sid,verdict:'reject',note:n}});
  const ok=r&&r.json&&r.json.ok;
  toast(ok?'已打回，意见已发给 agent':'打回失败'+((r&&r.json&&r.json.error)?('：'+r.json.error):''), !ok);
  if(ok){ LED_AT=0; ledgerLoad(); }
}
async function ledgerLoad(){
  if(Date.now()-LED_AT<8000) return;   /* 和收件箱两路一起被 5 秒轮询带着跑，节流 */
  LED_AT=Date.now();
  try{
    const r=await xj('/api/task_ledger',{away:true,timeout:8000,dcto:8000});
    const d=r&&r.json; if(!d||!d.ok) return;
    LED_LIST=d.items||[];
    inboxPaint();                     /* 工作台是一个页：台账数据并进收件箱一起画 */
    try{ maybeTriage(); }catch(e){}   /* 顺手让模型过一遍没判断过的（自带节流） */
  }catch(e){}                        /* 读不到就保留上一份 */
}
/* 工作台整理：模型判断没价值（测试/废弃会话）的不透出，用户可隐藏/删除。
   只影响工作台这一页——会话和历史本身不动。 */
let IB_VW='', TRI_AT=0;   /* 工作台的子页：''=主表 / hid=已收起 / acc=已验收 / find=搜索 */
let FIND_Q='', FIND_ITEMS=[], FIND_AT=0;   /* 搜索：最后一次问的词、结果、有没有问过 */
let IB_Q='';   /* 工作台主表那个内联搜索框里的草稿（工作台几秒重画一次，草稿要留住） */
/* 只重画消息行和结果区——整块重画会把输入框一起换掉，焦点一丢键盘就收，
   手机上等于「点了输入框就没了」。骨架只在首次进子页时建。 */
function findRender(){
  const m=document.getElementById('findMsg'), r=document.getElementById('findRes');
  if(!m||!r) return;
  m.textContent = !FIND_AT
    ? '搜的是所有会话的聊天记录和工具调用（改过哪些文件也搜得到）'
    : (FIND_ITEMS.length ? '找到 '+FIND_ITEMS.length+' 个会话 · 点开进入'
                         : '没找到「'+FIND_Q+'」');
  r.innerHTML = (FIND_ITEMS||[]).map(function(it){
    /* 别的机器上的命中：本机没有这个会话，点开是「切到那台机器」 */
    const rem=!!it.remote;
    return '<div class="card tap"'+(rem
        ?' data-remote-dev="'+escHtml(it.dev||'')+'" data-remote-name="'+escHtml(it.machine||'')+'"'
        :' data-findsid="'+escHtml(it.sid)+'"')+' style="margin-top:6px">'+
      '<div style="display:flex;justify-content:space-between;gap:8px;align-items:baseline">'+
        '<div class="name" style="font-size:14px">'+
        escHtml(it.title||String(it.sid).slice(0,8))+'</div>'+
        '<span class="meta" style="flex:none">'+
        (rem?'🖥 '+escHtml(it.machine||'')+' · ':'')+
        (it.agent?(agIcon(it.agent)+escHtml(agentLabelOf(it.agent))+' · '):'')+
        (it.last?ago(it.last):'')+'</span></div>'+
      (it.snips||[]).map(function(sn){
        return '<div class="meta" style="margin-top:4px;color:#cccccc">'+
          hlq(String(sn).slice(0,160), FIND_Q)+'</div>';
      }).join('')+'</div>';
  }).join('');
}
async function findRun(){
  const el=document.getElementById('findQ');
  FIND_Q=((el&&el.value)||'').trim();
  FIND_AT=0; FIND_ITEMS=[]; findRender();
  if(!FIND_Q) return;
  try{
    const r=await xj('/api/search?q='+encodeURIComponent(FIND_Q));
    const d=(r&&r.json)||{};
    FIND_ITEMS=d.items||[];
  }catch(e){ FIND_ITEMS=[]; }
  FIND_AT=Date.now()/1000;
  findRender();
}
async function maybeTriage(){
  if(Date.now()-TRI_AT<60000) return;
  TRI_AT=Date.now();
  try{
    const r=await xj('/api/task_triage',{method:'POST',body:{},away:true,timeout:30000,dcto:30000});
    if(r&&r.json&&r.json.ok&&r.json.judged){ LED_AT=0; ledgerLoad(); }
  }catch(e){}
}
/* ── 派单（B2）：手机上指定 agent + 目录 + 要干的事，电脑上开新会话干活。
      先落任务（tid）再后台开会话：开会话失败也是台账里一行「派单失败」，
      不会点了没反应。目录默认电脑 home，和「＋ 新建会话」同一个口径。 ── */
function taskNewDlg(){
  const ags=(typeof AGS!=='undefined'&&AGS)||[];
  const inp='width:100%;box-sizing:border-box;background:#141414;color:#f5f5f5;'+
    'border:1px solid #333;border-radius:8px;padding:8px;font-size:14px';
  askConfirm('派单',
    (ags.length?'<select id="dlgAg" style="'+inp+'">'+ags.map(function(a){
      return '<option value="'+escHtml(a.key)+'">'+escHtml(agentLabelOf(a.key)||a.label||a.key)+'</option>';
    }).join('')+'</select>':'')+
    '<input id="dlgCwd" placeholder="目录（默认电脑 home）" style="'+inp+';margin:6px 0">'+
    '<textarea id="dlgBody" placeholder="要它干什么（第一行会当任务标题）" '+
    'style="'+inp+';height:110px"></textarea>',
    '派出去', false, function(){
      const b=document.getElementById('dlgBody'), c=document.getElementById('dlgCwd');
      const a=document.getElementById('dlgAg');
      const body=b?String(b.value||'').trim():'';
      if(!body){ toast('写点要干的事再派', true); return; }
      taskNewPost({content:body, cwd:c?String(c.value||'').trim():'', agent:a?a.value:''});
    });
}
async function taskNewPost(d){
  const r=await xj('/api/task_new',{method:'POST',body:d});
  const ok=r&&r.json&&r.json.ok;
  toast(ok?'已派出去，电脑上正在开会话':('派单失败'+
    ((r&&r.json&&r.json.error)?('：'+r.json.error):'')), !ok);
  if(ok){ LED_AT=0; ledgerLoad(); }   /* 派单中那一行立刻画出来 */
}
async function taskViewAct(act, sid){
  const r=await xj('/api/task_view',{method:'POST',body:{sid:sid,action:act}});
  const ok=r&&r.json&&r.json.ok;
  toast(ok?({hide:'已收起',unhide:'已恢复',delete:'已删除',keep:'已恢复'})[act]||'已处理':'操作失败',!ok);
  if(ok){ LED_AT=0; ledgerLoad(); }
}
/* 工作台的信息合并：台账是骨架（这个任务到哪了），收件箱三路是「正在等你什么」，
   按 sid 并进同一条记录——同一件事在同一页出现两次就是没整合（2026-11-19 用户）。 */
function ibRows(){
  const m={};
  function rec(sid){
    sid=String(sid||''); if(!sid) return null;
    return m[sid]||(m[sid]={sid:sid});
  }
  /* 模型判「没用」的、用户收起的，都不进主表；它们去页底「已收起」里 */
  LED_LIST.forEach(function(it){
    if(it.hidden||it.keep===0) return;              /* 收起的不进主表 */
    if(it.state==='accepted'||it.state==='rejected') return;   /* 已验收的进「已验收」子页 */
    const r=rec(it.sid); if(r) r.led=it;
  });
  const seen={};
  PEND_LIST.forEach(function(it){
    const r=rec(it.sid); if(!r||seen[it.sid]) return;
    const a=it.ask||{}; seen[it.sid]=1;
    if(it.rid||a.action||a.target) r.pend=it; else r.ask=it;
  });
  ASK_LIST.forEach(function(it){
    const r=rec(it.sid); if(!r||seen[it.sid]||r.ask||r.pend) return;
    seen[it.sid]=1; r.ask=it;
  });
  DONE_LIST.forEach(function(it){ const r=rec(it.sid); if(r&&!r.done) r.done=it; });
  return Object.keys(m).map(function(k){ return m[k]; });
}
/* 要你出手的排前面：待决定 > 等你回话 > 办结待验收 > 其他 */
function ibRank(r){
  if(r.pend) return 0;
  if(r.ask) return 1;
  if(r.led&&r.led.state==='done') return 2;
  return 3;
}
/* 工作台上的 agent 品牌标：Claude 火花、Codex（OpenAI）结、OpenCode 方块。
   hermes-agent 没有公开品牌标，用首字母徽章——不编一个假的 logo 出来。 */
const AG_MARK={
  claude:{c:'#d97757',p:'m4.7144 15.9555 4.7174-2.6471.079-.2307-.079-.1275h-.2307l-.7893-.0486-2.6956-.0729-2.3375-.0971-2.2646-.1214-.5707-.1215-.5343-.7042.0546-.3522.4797-.3218.686.0608 1.5179.1032 2.2767.1578 1.6514.0972 2.4468.255h.3886l.0546-.1579-.1336-.0971-.1032-.0972L6.973 9.8356l-2.55-1.6879-1.3356-.9714-.7225-.4918-.3643-.4614-.1578-1.0078.6557-.7225.8803.0607.2246.0607.8925.686 1.9064 1.4754 2.4893 1.8336.3643.3035.1457-.1032.0182-.0728-.164-.2733-1.3539-2.4467-1.445-2.4893-.6435-1.032-.17-.6194c-.0607-.255-.1032-.4674-.1032-.7285L6.287.1335 6.6997 0l.9957.1336.419.3642.6192 1.4147 1.0018 2.2282 1.5543 3.0296.4553.8985.2429.8318.091.255h.1579v-.1457l.1275-1.706.2368-2.0947.2307-2.6957.0789-.7589.3764-.9107.7468-.4918.5828.2793.4797.686-.0668.4433-.2853 1.8517-.5586 2.9021-.3643 1.9429h.2125l.2429-.2429.9835-1.3053 1.6514-2.0643.7286-.8196.85-.9046.5464-.4311h1.0321l.759 1.1293-.34 1.1657-1.0625 1.3478-.8804 1.1414-1.2628 1.7-.7893 1.36.0729.1093.1882-.0183 2.8535-.607 1.5421-.2794 1.8396-.3157.8318.3886.091.3946-.3278.8075-1.967.4857-2.3072.4614-3.4364.8136-.0425.0304.0486.0607 1.5482.1457.6618.0364h1.621l3.0175.2247.7892.522.4736.6376-.079.4857-1.2142.6193-1.6393-.3886-3.825-.9107-1.3113-.3279h-.1822v.1093l1.0929 1.0686 2.0035 1.8092 2.5075 2.3314.1275.5768-.3218.4554-.34-.0486-2.2039-1.6575-.85-.7468-1.9246-1.621h-.1275v.17l.4432.6496 2.3436 3.5214.1214 1.0807-.17.3521-.6071.2125-.6679-.1214-1.3721-1.9246L14.38 17.959l-1.1414-1.9428-.1397.079-.674 7.2552-.3156.3703-.7286.2793-.6071-.4614-.3218-.7468.3218-1.4753.3886-1.9246.3157-1.53.2853-1.9004.17-.6314-.0121-.0425-.1397.0182-1.4328 1.9672-2.1796 2.9446-1.7243 1.8456-.4128.164-.7164-.3704.0667-.6618.4008-.5889 2.386-3.0357 1.4389-1.882.929-1.0868-.0062-.1579h-.0546l-6.3385 4.1164-1.1293.1457-.4857-.4554.0608-.7467.2307-.2429 1.9064-1.3114Z'},
  codex:{c:'#2fbf9b',p:'M22.2819 9.8211a5.9847 5.9847 0 0 0-.5157-4.9108 6.0462 6.0462 0 0 0-6.5098-2.9A6.0651 6.0651 0 0 0 4.9807 4.1818a5.9847 5.9847 0 0 0-3.9977 2.9 6.0462 6.0462 0 0 0 .7427 7.0966 5.98 5.98 0 0 0 .511 4.9107 6.051 6.051 0 0 0 6.5146 2.9001A5.9847 5.9847 0 0 0 13.2599 24a6.0557 6.0557 0 0 0 5.7718-4.2058 5.9894 5.9894 0 0 0 3.9977-2.9001 6.0557 6.0557 0 0 0-.7475-7.0729zm-9.022 12.6081a4.4755 4.4755 0 0 1-2.8764-1.0408l.1419-.0804 4.7783-2.7582a.7948.7948 0 0 0 .3927-.6813v-6.7369l2.02 1.1686a.071.071 0 0 1 .038.052v5.5826a4.504 4.504 0 0 1-4.4945 4.4944zm-9.6607-4.1254a4.4708 4.4708 0 0 1-.5346-3.0137l.142.0852 4.783 2.7582a.7712.7712 0 0 0 .7806 0l5.8428-3.3685v2.3324a.0804.0804 0 0 1-.0332.0615L9.74 19.9502a4.4992 4.4992 0 0 1-6.1408-1.6464zM2.3408 7.8956a4.485 4.485 0 0 1 2.3655-1.9728V11.6a.7664.7664 0 0 0 .3879.6765l5.8144 3.3543-2.0201 1.1685a.0757.0757 0 0 1-.071 0l-4.8303-2.7865A4.504 4.504 0 0 1 2.3408 7.872zm16.5963 3.8558L13.1038 8.364 15.1192 7.2a.0757.0757 0 0 1 .071 0l4.8303 2.7913a4.4944 4.4944 0 0 1-.6765 8.1042v-5.6772a.79.79 0 0 0-.407-.667zm2.0107-3.0231l-.142-.0852-4.7735-2.7818a.7759.7759 0 0 0-.7854 0L9.409 9.2297V6.8974a.0662.0662 0 0 1 .0284-.0615l4.8303-2.7866a4.4992 4.4992 0 0 1 6.6802 4.66zM8.3065 12.863l-2.02-1.1638a.0804.0804 0 0 1-.038-.0567V6.0742a4.4992 4.4992 0 0 1 7.3757-3.4537l-.142.0805L8.704 5.459a.7948.7948 0 0 0-.3927.6813zm1.0976-2.3654l2.602-1.4998 2.6069 1.4998v2.9994l-2.5974 1.4997-2.6067-1.4997Z'},
  opencode:{c:'#838383',p:'M22 24H2V0h20zM17 4.8H7v14.4h10z'}
};
function sicChip(agent, title){
  const k=String(agent||'').toLowerCase();
  const m=AG_MARK[k];
  if(m) return '<span class="sic chip" title="'+(title||'')+'" style="background:'+m.c+'22">'+agIcon(k,15)+'</span>';
  return '<span class="sic chip c-web" title="'+(title||'电脑终端')+'">WEB</span>';
}
/* 窗口来源徽标（WindowSource）：协议通道（Codex 桌面版 /rpc、ACP）标「协议」，
   屏幕源占位标「未实装」——降级看得见；pty 是主流，不标。老 host 行没有
   source 字段：不炸不标（向后兼容）。 */
function srcChip(s){
  const c=s&&s.source;
  if(!c) return '';
  const st=function(color,bg){ return 'class="chip" style="font-size:11px;font-weight:700;letter-spacing:.3px;margin-right:5px;white-space:nowrap;border-radius:4px;padding:1px 4px;display:inline-flex;align-items:center;color:'+color+';background:'+bg+'"'; };
  if(c.kind==='protocol') return '<span '+st('#7ea0ff','rgba(52,107,241,.14)')+' title="结构化协议通道：决策/审批事实直接来自通道本体">协议</span>';
  if(c.stub) return '<span '+st('#d9a765','rgba(217,167,101,.14)')+' title="屏幕源通道：Phase 1 未实装，内容暂不可实时查看">屏幕源（未实装）</span>';
  return '';
}
function agIcon(key, sz){
  sz=sz||12;
  const k=String(key||'').toLowerCase();
  const m=AG_MARK[k];
  if(!m)
    return '<span style="display:inline-flex;align-items:center;justify-content:center;width:'+sz+
      'px;height:'+sz+'px;border-radius:3px;background:#3a3f4b;color:#cccccc;font-size:'+(sz-3)+
      'px;font-weight:700;line-height:1;vertical-align:-2px;margin-right:4px">'+
      escHtml(String(key||'?').slice(0,1).toUpperCase()||'?')+'</span>';
  return '<svg viewBox="0 0 24 24" width="'+sz+'" height="'+sz+'" fill="'+m.c+
    '" style="vertical-align:-2px;margin-right:4px"><path d="'+m.p+'"/></svg>';
}
/* 一条会话一张卡：问话（待决定/等你回话）当主行，台账的状态、耗时、最近进展当副行；
   不问话时台账标题当主行、五态徽章进右上角。 */
function ibCard(r){
  const it=r.led||{}, p=r.pend, a=r.ask, d=r.done, st=it.state||'';
  const agent=(p&&p.agent)||(a&&a.agent)||it.agent||'';
  const title=(p&&p.title)||it.title||(a&&a.title)||(d&&d.title)||'';
  const dur=(it.first_ts&&it.last_ts&&it.last_ts>it.first_ts)
    ?fmtLedgerDur(it.last_ts-it.first_ts):'';
  const extra=[];
  let main='', badge='', bcol='', edge='';
  if(p){
    const q=p.ask||{}, risk=(p.risk||[])[0], opt=pendPick(p);
    main=opt.length?(q.target||q.action||'选一项')
      :((q.action||'确认')+(q.target?('：'+q.target):''));
    if(risk) extra.push('<div class="meta" style="color:#ff6467">⚠ '+escHtml(risk.text)+'</div>');
    if(opt.length) extra.push('<div class="meta" style="color:#f0a43c">选一项：'+escHtml(opt.slice(0,6).join(' / '))+'</div>');
    badge='待你决定'; bcol='#f0a43c'; edge=';border-color:rgba(240,164,60,.45)';
  }else if(a){
    main=String(a.act||a.summary||a.text||'').replace(/\s+/g,' ').trim()||'这轮跑完在等你回话';
    badge='等你回话'; bcol='#4b7cf3'; edge=';border-color:rgba(121,192,255,.4)';
  }else{
    main=it.title||(d&&d.title)||r.sid.slice(0,8);
    badge=st?ledgerBadge(st):'刚完成';
    bcol=(st==='rejected')?'#e88':(st==='accepted'||st==='done'||!st)?'#34d399':'var(--mut)';
  }
  const ts=it.last_ts||(a&&a.ts)||0;
  /* 跨机器（B2.1）：这一行在别的机器上——标出来，点一下是「切过去」不是进会话 */
  const remote=!!it.remote;
  const sub=[remote?('<span title="在另一台机器上">🖥 '+escHtml(it.machine||'')+'</span>'):'',
             agent?(agIcon(agent)+escHtml(agentLabelOf(agent))):'',
             (p||a)?escHtml(title):'',
             (st==='deciding'&&it.waited)?('已等 '+fmtLedgerDur(it.waited)):((p||a)?'':dur),
             ts?ago(ts):''].filter(Boolean).join(' · ');
  const sum=String(it.last_summary||(d&&d.text)||'').replace(/\s+/g,' ').trim().slice(0,120);
  return '<div class="card tap" data-ledger="'+escHtml(r.sid)+'"'+
    (remote?(' data-remote-dev="'+escHtml(it.dev||'')+'" data-remote-name="'+escHtml(it.machine||'')+'"'):'')+
    ' style="margin-top:6px'+edge+'">'+
    '<div style="display:flex;justify-content:space-between;gap:8px;align-items:baseline">'+
      '<div class="name" style="font-size:14px">'+escHtml(String(main).slice(0,200))+'</div>'+
      '<span style="flex:none;display:flex;gap:8px;align-items:baseline;white-space:nowrap">'+
        '<span class="meta" style="color:'+bcol+'">'+escHtml(badge)+'</span>'+
        ((p||a||remote)?'':'<span class="meta" data-tv="hide" data-sid="'+escHtml(r.sid)+'" style="color:var(--mut)">收起</span>')+
      '</span>'+
    '</div>'+
    (sub?'<div class="meta">'+sub+'</div>':'')+
    extra.join('')+
    (sum&&sum.slice(0,24)!==String(main).slice(0,24)
      ?'<div class="meta" style="margin-top:4px;color:#cccccc">'+escHtml(sum)+'</div>':'')+
    /* 这一单出了什么（B4）：按 sid 从收件箱那份数据里数，不新开接口 */
    (function(){
      const n=(typeof ART_INBOX!=='undefined'?ART_INBOX:[])
        .filter(function(x){ return x.sid===r.sid; }).length;
      return n?'<div class="meta" style="margin-top:4px"><span data-arts="'+escHtml(r.sid)+
        '" style="cursor:pointer;color:#4b7cf3">📎 产出物 '+n+' 件 · 查看</span></div>':'';
    })()+
    /* 一行只有一个类别：有问话就只回复，不再叠「通过/打回」——同一件事摆两种
       处理方式，用户分不清到底是回复还是审批（2026-11-19 用户）。 */
    ((p||a||remote)?'':ledgerActions(st, r.sid))+
    '</div>';
}
async function ibPendLoad(){
  if(Date.now()-PEND_AT<8000) return;
  PEND_AT=Date.now();
  try{
    const r=await xj('/api/ext_pending',{away:true,timeout:5000,dcto:5000});
    const d=r&&r.json; if(!d||!d.ok) return;
    PEND_LIST=d.items||[];
  }catch(e){ return; }        /* 读不到就保留上一份：别让「待决定」忽然消失 */
  inboxPaint();
}
async function ibDoneLoad(){
  if(Date.now()-DONE_AT<8000) return;
  DONE_AT=Date.now();
  try{
    const r=await xj('/api/notify_hist?days=1&limit=40&_='+Date.now(),
                     {away:true,timeout:5000,dcto:5000});
    const d=r&&r.json; if(!d||!d.ok||!Array.isArray(d.items)) return;
    /* 一轮结束有两种落点：真正干完（kind=done）和在问你（done 被智能提醒改报成 perm，
       act 里是那句要你拍板的话）。只看 done 的话，「刚完成」会永远停在最后一次
       没有提问的轮次上（2026-11-14：手机上看到的是 13 分钟前的）。 */
    const seen={}, aseen={}, out=[], asks=[];
    d.items.forEach(function(it){          /* 接口按 ts 倒序：每个会话留最新一条 */
      const s=it.sid||''; if(!s) return;
      if(it.kind==='done'){
        if(seen[s]) return; seen[s]=1; out.push(it); return;
      }
      if(it.kind!=='perm'||it.state==='done') return;   /* state=done：已被后面一轮取代 */
      if(!String(it.act||'').trim()||!String(it.text||'').trim()) return;
      if(aseen[s]) return; aseen[s]=1; asks.push(it);
    });
    DONE_LIST=out.slice(0,20);
    ASK_LIST=asks.slice(0,10);
  }catch(e){ return; }        /* 读不到就保留上一份 */
  inboxPaint();
}
/* 产出物收件箱（B1）：代理工头工作台顶部——所有任务最新产出收在一处，点开即看。
   数据来自 /api/artifact_inbox（跨会话聚合）；点卡片=切到那个会话再预览。 */
let ART_INBOX=[], ART_IN_AT=0;
async function ibArtsLoad(){
  if(!lineCaps(cbLine()).artifactsInbox) return;
  if(Date.now()-ART_IN_AT<15000) return;
  ART_IN_AT=Date.now();
  try{
    const r=await xj('/api/artifact_inbox',{away:true,timeout:8000,dcto:8000});
    const d=r&&r.json; if(!d||!d.ok) return;
    ART_INBOX=d.items||[];
    inboxPaint();
  }catch(e){}                       /* 读不到就保留上一份 */
}
function ibArtsHtml(){
  if(!ART_INBOX.length) return '';
  const es=ART_INBOX.slice(0,8);
  return '<div class="meta" style="margin:12px 2px 6px;color:var(--mut)">产出物 · 最新 '+
    ART_INBOX.length+' 件</div><div class="fgrid">'+es.map(function(e){
      return '<div class="fcard" data-art="'+escHtml(encodeURIComponent(e.rel||e.name||''))+'"'+
        ' data-art-sid="'+escHtml(e.sid||'')+'" data-art-name="'+escHtml(e.name||'')+'">'+
        '<div class="fic">'+((typeof ART_ICON!=='undefined'&&ART_ICON[e.kind])||'📦')+'</div>'+
        '<div class="fnm">'+escHtml(e.name||'')+'</div>'+
        '<div class="meta">'+fmtSz(e.size||0)+(e.title?(' · '+escHtml(String(e.title).slice(0,12))):'')+'</div></div>';
    }).join('')+'</div>';
}
async function ibArtOpen(enc, name, sid){
  try{ await switchTo(sid); }catch(e){}      /* 预览按会话取文件：先切回那个会话 */
  fvPath=decodeURIComponent(enc); fvName=name;
  fviewShow(fvPath, name);
}
/* 这个输入框还在屏幕上吗（左右滑到别的页后它就在屏外了）。 */
function cbInputOnScreen(el){
  if(!el || !el.getBoundingClientRect) return false;
  const r=el.getBoundingClientRect();
  if(!r.width || !r.height) return false;
  return r.left>-4 && r.right<(window.innerWidth||0)+4;
}
/* 翻页时把屏外输入框的焦点收掉。不收的话：工作表几秒重画一次会把焦点 focus 回来，
   手机上的滚动容器为了露出它把页面拽回工作台（2026-11-21 用户，滑到会话页又自己回去）。 */
function blurOffscreenInput(){
  const a=document.activeElement;
  if(!a || a===document.body) return;
  const t=String(a.tagName||'').toUpperCase();
  if(t!=='INPUT' && t!=='TEXTAREA') return;
  if(!cbInputOnScreen(a)){ try{ a.blur(); }catch(e){} }
}
function inboxPaint(){
  const body=document.getElementById('ibPane');
  /* 一张表：四路数据按 sid 并成一条会话记录，要你出手的排前面，
     其余按最近动静。不再分「待你决定 / 等你回话 / 刚完成」三段——
     那是把同一件事摆了两次。 */
  const rows=ibRows();
  const hid=LED_LIST.filter(function(it){ return it.hidden||it.keep===0; });
  /* 「已验收」：通过/打回过的任务从主表挪出来，留个地方翻账 */
  const acc=LED_LIST.filter(function(it){
    return !it.hidden&&it.keep!==0&&(it.state==='accepted'||it.state==='rejected');
  }).sort(function(x,y){ return (y.last_ts||0)-(x.last_ts||0); });
  if(IB_VW==='find'){                 /* 找会话：一句话搜所有会话的聊天记录和工具入参 */
    /* 骨架只建一次：台账每几秒重画一次工作台，整块重建会把输入框换掉、焦点丢掉。
       输入栏钉在底部（dock 上方）：会话页键盘弹起时整页上推，顶上的输入框会被推到
       屏幕外，底部的不会——键盘上沿就在它下面。 */
    if(!document.getElementById('findQ')){
      body.innerHTML=
        '<div class="meta" data-ibview="main" style="margin:12px 2px 8px;color:#4b7cf3">'+
        '← 返回工作台</div>'+
        '<div id="findRes" style="padding-bottom:120px"></div>'+
        '<div id="findBar" style="position:fixed;left:10px;right:10px;z-index:3;'+
        'bottom:calc(var(--dockh,100px) + env(safe-area-inset-bottom,0px) + 6px);'+
        'background:#0a0a0a;border-top:1px solid #191919;padding:8px 2px 4px">'+
        '<div class="meta" id="findMsg" style="margin:0 2px 6px"></div>'+
        '<div style="display:flex;gap:8px">'+
        '<input id="findQ" placeholder="说过 / 改过什么？比如 登录页、部署" '+
        'value="'+escHtml(FIND_Q)+'" style="flex:1;min-width:0;background:#141414;color:#f5f5f5;'+
        'border:1px solid #2a2a2a;border-radius:9px;padding:9px 11px;font-size:15px;outline:none">'+
        '<button class="lt" data-findgo="1" style="flex:none">搜索</button></div></div>';
    }
    findRender();
    return;
  }
  if(IB_VW==='acc'){
    let g='<div class="meta" data-ibview="main" style="margin:12px 2px 8px;color:#4b7cf3">'+
          '← 返回工作台</div>'+
          '<div class="meta" style="margin:8px 2px 6px;color:var(--mut)">已验收 · '+acc.length+'</div>';
    acc.forEach(function(it){
      const ok=it.state==='accepted';
      g+='<div class="card tap" data-ledger="'+escHtml(it.sid)+'" style="margin-top:6px">'+
         '<div style="display:flex;justify-content:space-between;gap:8px;align-items:baseline">'+
           '<div class="name" style="font-size:14px">'+escHtml(it.title||String(it.sid).slice(0,8))+'</div>'+
           '<span class="meta" style="flex:none;color:'+(ok?'#34d399':'#e88')+'">'+ledgerBadge(it.state)+'</span>'+
         '</div>'+
         '<div class="meta">'+(it.agent?(agIcon(it.agent)+escHtml(agentLabelOf(it.agent))+' · '):'')+
           (it.last_ts?ago(it.last_ts):'')+'</div>'+
         (it.last_summary?'<div class="meta" style="margin-top:4px;color:#cccccc">'+
           escHtml(String(it.last_summary).slice(0,120))+'</div>':'')+
         '</div>';
    });
    if(!acc.length) g+='<div class="nhEmpty">还没有验收过的任务</div>';
    body.innerHTML=g;
    return;
  }
  if(IB_VW==='hid'){                  /* 「已收起」：模型判没用的 + 用户收起的 */
    let g='<div class="meta" data-ibview="main" style="margin:12px 2px 8px;color:#4b7cf3">'+
          '← 返回工作台</div>'+
          '<div class="meta" style="margin:8px 2px 6px;color:var(--mut)">已收起 · '+hid.length+'</div>';
    hid.forEach(function(it){
      const why=it.vwhy||'你收起的';
      g+='<div class="card" data-ledger="'+escHtml(it.sid)+'" style="margin-top:6px">'+
         '<div style="display:flex;justify-content:space-between;gap:8px;align-items:baseline">'+
           '<div class="name" style="font-size:14px">'+escHtml(it.title||String(it.sid).slice(0,8))+'</div>'+
           '<span class="meta" style="flex:none;color:var(--mut)">'+escHtml(why)+'</span>'+
         '</div>'+
         '<div style="display:flex;gap:8px;margin-top:8px">'+
           '<button class="lt" data-tv="keep" data-sid="'+escHtml(it.sid)+'" style="flex:1">恢复</button>'+
           '<button class="lt" data-tv="delete" data-sid="'+escHtml(it.sid)+'" style="flex:1;background:#3a1d1d;color:#e88;border-color:#5a2a2a">删除</button>'+
         '</div></div>';
    });
    if(!hid.length) g+='<div class="nhEmpty">没有收起的任务</div>';
    body.innerHTML=g;
    return;
  }
  const tsOf=function(r){
    return (r.led&&r.led.last_ts)||(r.ask&&r.ask.ts)||(r.done&&r.done.ts)||0;
  };
  rows.sort(function(x,y){ return (ibRank(x)-ibRank(y))||(tsOf(y)-tsOf(x)); });
  const need=rows.filter(function(r){ return ibRank(r)<3; }).length;
  /* 空页面不占着开屏：还没手动切过页签、几份数据都回来了、确实一条都没有，
     就直接落到会话列表；有内容才留在这一页。 */
  if(IB_AUTO&&LV==='ib'&&PEND_AT&&DONE_AT&&LED_AT&&!rows.length&&!hid.length&&!acc.length
     &&!ART_INBOX.length){
    listView('sess'); return;
  }
  /* 页签上的角标：需要你出手的（待决定、在问你、待验收）才需要一眼看见 */
  const n=document.getElementById('ltIbN');
  if(n){
    if(need){ n.textContent=need; n.style.display=''; }
    else{ n.style.display='none'; n.textContent=''; }
  }
  if(!body) return;
  /* 代理工头：派单入口 + 产出物在最上面，任务在下面。
     派单是经营面的动作用语（工头线上派单=多此一举：会话本来就随手开），
     只给代理工头；搜索是检索，两条线都要（2026-11-20 用户）。
     搜索框直接摆在工作台上（2026-11-21 用户：不用先点进子页再输入）。 */
  const caps=lineCaps(cbLine());
  /* 只有搜索框还在屏幕上才回填焦点：左右滑走之后它在屏外，focus() 会把页面拽回来 */
  const qFocus=document.activeElement&&document.activeElement.id==='ibQ'&&cbInputOnScreen(document.activeElement);
  let h='<div style="display:flex;gap:8px;margin:10px 2px 4px;align-items:center">'+
        (caps.dispatch
          ? '<div class="meta" data-ibnew="1" title="指定 agent 和目录，开个新会话干活" '+
            'style="flex:none;text-align:center;color:#4b7cf3">＋ 派单</div>'
          : '')+
        '<input id="ibQ" placeholder="搜索：说过 / 改过什么" value="'+escHtml(IB_Q)+'" '+
        'style="flex:1;min-width:0;background:#141414;color:#f5f5f5;border:1px solid #2a2a2a;'+
        'border-radius:9px;padding:9px 11px;font-size:15px;outline:none">'+
        '<button class="lt" data-ibfind="1" title="搜所有会话的聊天记录和工具入参" '+
        'style="flex:none">搜索</button></div>'+
        ibArtsHtml();
  if(rows.length){
    h+='<div class="meta" style="margin:12px 2px 6px;color:var(--mut)">任务 · 近 30 天 · '+
       rows.length+'</div>';
    rows.forEach(function(r){ h+=ibCard(r); });
  }
  if(acc.length)
    h+='<div class="meta" data-ibview="acc" style="margin:16px 2px;text-align:center;color:var(--mut)">'+
       '已验收 '+acc.length+' 个（通过/打回）· 查看</div>';
  if(hid.length)
    h+='<div class="meta" data-ibview="hid" style="margin:16px 2px;text-align:center;color:var(--mut)">'+
       '已收起 '+hid.length+' 个不重要的 · 查看</div>';
  /* 还没发过任何一次请求（开屏瞬间）：别先闪一下「没有」 */
  if(!h) h=(!PEND_AT&&!DONE_AT)
    ? '<div class="nhEmpty">加载中…</div>'
    : '<div class="nhEmpty">现在没有要你处理的事</div>';
  const qEl=qFocus?document.activeElement:null;
  const qCaret=qEl?qEl.selectionStart:0;
  body.innerHTML=h;
  /* 台账几秒重画一次工作台：正在输入的时候把那一下的焦点和光标位置还回去，
     不然手机上一打字键盘就收。 */
  if(qFocus){
    const q=document.getElementById('ibQ');
    if(q){ try{ q.focus(); q.setSelectionRange(qCaret,qCaret); }catch(e){} }
  }
}
function renderList(){
  try{ buildOpen(); }catch(e){}   // 左滑序列跟着列表走（开着的、按最近进入排序）
  try{ ibPendLoad(); }catch(e){}
  try{ ibDoneLoad(); }catch(e){}
  try{ ledgerLoad(); }catch(e){}   /* 工作台：台账数据跟着列表轮询一起刷（自带节流/工头线短路） */
  /* 网页终端（🌐，电脑上开着的 shell 会话）不在这里：它们归「终端」页（termLoad）。
     以前混在主列表里，服务每次重启都会把当时开着的终端变成「已失效」占位行，堆一片。
     主列表只留电脑上的 agent 会话。 */
  const everything=(lastExt||[]).filter(s=>s.kind!=='web');
  /* 顶上选了哪个 agent 就只看哪个的会话。没有 agent 字段的（网页终端、老记录）
     跟 agent 无关，两边都留；不然切到 Codex 连普通终端都看不见了。 */
  const all=CUR_AG?everything.filter(s=>!s.agent||s.agent===CUR_AG):everything;
  /* 当前 agent 一个会话都没有：在列表上方给一个「新建会话」入口（默认开在电脑 home）。
     有会话时它不出现，避免和目录头的 ＋ 重复。 */
  const agSess=lastExt.filter(s=>!CUR_AG||s.agent===CUR_AG);
  const noDev=DEV.init&&!DEV.na&&!(DEV.list||[]).length;
  const nsc=document.getElementById('newSessCard');
  if(nsc) nsc.style.display=(agSess.length||noDev)?'none':'';
  /* agent 标识：没开切换条（只装了一种 agent）但列表里混着多种 agent 时才逐个标；
     开着切换条时看的就是一种，行上不用再标。文案/配色全跟 /api/ext_agents 走。 */
  const kinds=new Set(all.map(s=>s.agent||'').filter(Boolean));
  const mixed=!CUR_AG&&kinds.size>1;
  const ag=s=>{
    if(CUR_AG||!mixed||!s.agent) return '';
    return '<span class="ag '+escHtml(s.agent)+'">'+escHtml(agentLabelOf(s.agent))+'</span>';
  };
  const el=document.getElementById('list');
  if(!all.length){
    const an=(AGS||[]).find(a=>a.key===CUR_AG);
    el.innerHTML='<div class="card"><div class="meta">'+
      (CUR_AG?('还没有'+escHtml(agLabel(an&&an.label||CUR_AG))+'会话。点上面的「新建会话」，默认开在电脑 home 目录。')
             :'还没有会话。点上面的「新建会话」，默认开在电脑 home 目录。')+'</div></div>';
    return;
  }
  const groups={};
  all.forEach(s=>{const k=s.cwd||'(未知目录)';(groups[k]=groups[k]||[]).push(s);});
  const lk=k=>Math.max(...groups[k].map(s=>s.last||s.started||0));
  const keys=Object.keys(groups).sort((a,b)=>lk(b)-lk(a));
  let h='';
  /* 断网期给的是缓存（loadListData 置位）：降级要看得见，别装在线（T3 之鉴）。 */
  if(listStaleAt&&lastExt.length){
    const mins=LIST_AT?Math.max(0,Math.round((Date.now()-LIST_AT)/60000)):0;
    h+='<div class="meta" style="text-align:center;margin:0 8px 8px">离线缓存'+(mins?(' · '+mins+' 分钟前的数据'):'')+'</div>';
  }
  for(const k of keys){
    const arr=groups[k].slice().sort((a,b)=>(b.last||b.started||0)-(a.last||a.started||0));
    const pn=k.replace(/\/+$/,'').split('/').pop()||k;
    /* 目录头右边一个「＋」：在这个目录里开一个新会话（电脑上弹新终端）。
       目录是绝对路径才给按钮——「(未知目录)」这种没地方开。 */
    const canNew=!!k&&k!=='(未知目录)'&&(k.charAt(0)==='/'||/^[A-Za-z]:[\\/]/.test(k));
    h+='<div class="card"><div class="prow"><div class="name dname" style="flex:1;min-width:0" title="'+escHtml(k)+'">'+escHtml(pn)+'</div><span class="gline"></span>'+
       (canNew?'<button class="ib sm" style="align-self:center" title="在这个目录新建会话" data-act="new" data-cwd="'+escHtml(k)+'">＋</button>':'')+
       '<div class="meta">'+arr.length+'</div></div>';
    for(const s of arr){
      if(s.kind==='computer'){
        const sel=(s.sid===SID);
        if(s.state==='exited'){
          h+='<div class="srow tap" data-csid="'+s.sid+'">'+sicChip(s.agent,(s.agent?escHtml(agentLabelOf(s.agent))+' 会话':'电脑终端'))+
             '<div style="min-width:0;flex:1"><div class="name sname" style="font-size:16px;font-weight:600" title="'+escHtml(s.title)+'">'+escHtml(s.title)+'</div>'+
             '<div class="meta smeta">'+ag(s)+srcChip(s)+'<span class="dead">已退出</span> · '+ago(s.last)+' · 点按唤起</div></div>'+
             '<div class="sbtn"><button class="ib sm" title="详情" data-act="detail" data-sid="'+s.sid+'">ⓘ</button>'+
             '<button class="ib sm" title="召回会话（--resume，历史保留）" data-act="resume" data-sid="'+s.sid+'">↻</button>'+
             '<button class="ib sm" title="fork 一份：新会话带着这段历史，原会话不动" data-act="fork" data-sid="'+s.sid+'" data-cwd="'+escHtml(s.cwd||'')+'">fork</button>'+
             '<button class="ib sm del" title="删除会话（连转录一起删）" data-act="del" data-sid="'+s.sid+'">🗑</button></div></div>';
          continue;
        }
        const fc=s.full_color?' <span class="fc" title="全彩镜像">🎨</span>':'';
        /* 能力徽标：让「为什么不能打字」一眼可见，而不是点了发送才报错。
           wrapped/attached 不打扰；readonly 明确标「只读」。 */
        const capBadge=(s.link==='readonly')?' <span class="meta" title="'+escHtml(s.why||'这个终端没有可写入的通道')+'">只读</span>'
          :((s.link==='attached')?' <span class="meta" title="'+(s.term?escHtml(s.term):'终端')+'">可输入</span>':'');
        const stc=(s.state==='idle')?'idle':'live';
        /* 按钮用 data-act 委托而不是内联 onclick：列表每 5s 被 innerHTML 重建，
           内联 onclick 随元素一起被换掉 → 用户按下时元素已换，点击丢失（按钮"点了没反应"）。
           委托挂在 #list 上，重建不影响。 */
        /* 有全彩镜像 = 有控制通道，跟有没有 Terminal 窗口无关：Warp/iTerm/VS Code
           这类第三方终端抓不到窗口，但读写都能走 wrapper 的 socket 和日志。 */
        const ctl=((s.window_id||s.full_color||s.host)?'':(s.desktop?'<span class="meta" title="Codex 桌面版：没有窗口画面">桌面版</span>':'<span class="meta">无窗口</span>'))+
          (s.host?'<button class="ib sm" title="打开原始终端" data-act="hostterm" data-sid="'+s.sid+'">↪</button>':'')+
          '<button class="ib sm" title="命令历史 / 识别命令" data-act="cmd" data-sid="'+s.sid+'">⌨</button>'+
          '<button class="ib sm" title="详情" data-act="detail" data-sid="'+s.sid+'">ⓘ</button>'+
          ((s.full_color||s.host||(s.caps||[]).indexOf('takeover')<0)?'':'<button class="ib sm" title="接管：在原终端重开这个会话（历史保留，之后能在手机上输入）" data-act="migrate" data-sid="'+s.sid+'">🔄</button>')+
          '<button class="ib sm" title="fork 一份：新会话带着这段历史，原会话不动" data-act="fork" data-sid="'+s.sid+'" data-cwd="'+escHtml(s.cwd||'')+'">fork</button>'+
          '<button class="ib sm del" title="删除会话（先结束进程，再连转录一起删）" data-act="del" data-sid="'+s.sid+'">🗑</button>';
        /* 整行点按 = 去控制（和详情里的「去控制」同一个动作）。以前只有有窗口的
           会话才绑 data-csid，没窗口的会话点上去毫无反应，只能先进详情再点。 */
        h+='<div class="srow tap'+(sel?' sel':'')+'" data-csid="'+s.sid+'">'+
           ''+sicChip(s.agent,(s.agent?escHtml(agentLabelOf(s.agent))+' 会话':'电脑终端'))+
           '<div style="min-width:0;flex:1"><div class="name sname" style="font-size:16px;font-weight:600" title="'+escHtml(s.title)+'">'+escHtml(s.title)+fc+'</div>'+
           '<div class="meta smeta">'+ag(s)+srcChip(s)+capBadge+(NOTIFY[s.sid]?'<span class="nb">● '+nbLabel(NOTIFY[s.sid].kind)+'</span> · ':'')+'<span class="'+stc+'">'+escHtml(stText(s.state))+'</span> · '+ago(s.last)+'</div></div>'+
           '<div class="sbtn">'+ctl+'</div></div>';
      }else{
        let st,cls,btns;
        if(s.ghost){st='已失效';cls='dead';btns=`<button class="ib" title="详情" data-act="detail" data-sid="${s.sid}">ⓘ</button><button class="ib" title="重新拉起" data-act="respawn" data-sid="${s.sid}">↻</button><button class="ib" title="移除" data-act="remove" data-sid="${s.sid}">🗑</button>`;}
        else if(s.alive){st='运行中';cls='live';btns=`<button class="ib" title="详情" data-act="detail" data-sid="${s.sid}">ⓘ</button><button class="ib" title="进入" data-act="hostterm" data-sid="${s.sid}">↪</button><button class="ib" title="结束" data-act="kill" data-sid="${s.sid}">⏹</button>`;}
        else{st='已退出';cls='dead';btns=`<button class="ib" title="详情" data-act="detail" data-sid="${s.sid}">ⓘ</button><button class="ib" title="重新拉起" data-act="respawn" data-sid="${s.sid}">↻</button><button class="ib" title="移除" data-act="remove" data-sid="${s.sid}">🗑</button>`;}
        const ht=(s.alive&&!s.ghost)?' tap" data-hostterm="'+s.sid
          :(s.ghost?' tap" data-respawn="'+s.sid:'');   /* 已失效的占位行：点按=重新拉起再进去 */
        h+='<div class="srow'+ht+'">'+sicChip('','网页终端')+
           '<div style="min-width:0;flex:1"><div class="name sname" style="font-size:16px;font-weight:600" title="'+escHtml(s.name)+'">'+escHtml(s.name)+'</div>'+
           '<div class="meta smeta"><span class="'+cls+'">'+st+'</span> · '+ago(s.started)+(s.ghost?' · 点按重新拉起':'')+'</div></div>'+
           '<div class="sbtn">'+btns+'</div></div>';
      }
    }
    h+='</div>';
  }
  el.innerHTML=h;
}
function renderDetailLite(){ if(document.getElementById('dsheet').classList.contains('on')&&detailSid) showDetail(detailSid); }