// SPDX-License-Identifier: AGPL-3.0-or-later

/* ── 设置页「问题与建议」：文字 + 语音输入 + 截图/视频，提交到中继存档 ──
   限制在两头都做：这里先挡一遍（挑得中、看得见），中继再按魔数/大小/条数硬校验
   （改前端绕不过去）。语音走和主输入框同一条录音+ASR 链路，只是结果落进反馈框。 */
var FBK = '问题', FBF = [];
function fbKind(k){
  FBK = k;
  var a = document.getElementById('fbKindBug'), b = document.getElementById('fbKindIdea');
  if (a) a.className = 'gray' + (k === '问题' ? ' on' : '');
  if (b) b.className = 'gray' + (k === '建议' ? ' on' : '');
}
function fbTip(m, bad){
  var h = document.getElementById('fbHint');
  if (!h) return;
  h.textContent = m;
  h.style.color = bad ? '#ff6467' : '#838383';
}
function fbPick(){
  var f = document.getElementById('fbFile');
  if (f) f.click();
}
function fbPaint(){
  var box = document.getElementById('fbPrev');
  if (!box) return;
  box.innerHTML = '';
  FBF.forEach(function(x, i){
    var d = document.createElement('div');
    d.style.cssText = 'position:relative;width:64px;height:64px;border-radius:8px;' +
      'overflow:hidden;border:1px solid #191919;background:#111111';
    var tag = /^video\//.test(x.f.type)
      ? '<video src="' + x.u + '" muted playsinline style="width:100%;height:100%;object-fit:cover"></video>'
      : '<img src="' + x.u + '" style="width:100%;height:100%;object-fit:cover">';
    d.innerHTML = tag + '<span style="position:absolute;right:0;top:0;width:20px;height:20px;' +
      'background:rgba(0,0,0,.7);color:#fff;font-size:13px;line-height:20px;text-align:center;' +
      'border-bottom-left-radius:6px;cursor:pointer">×</span>';
    d.lastChild.onclick = function(){
      try { URL.revokeObjectURL(FBF[i].u); } catch (e) {}
      FBF.splice(i, 1);
      fbPaint();
    };
    box.appendChild(d);
  });
}
function fbFiles(inp){
  var fs = (inp && inp.files) || [];
  var total = FBF.reduce(function(s, x){ return s + x.f.size; }, 0);
  for (var i = 0; i < fs.length; i++){
    var f = fs[i];
    if (FBF.length >= 4){ fbTip('最多 4 个附件', true); break; }
    if (!/^image\//.test(f.type) && !/^video\//.test(f.type)){ fbTip('只能发截图或视频', true); continue; }
    if (f.size > 12 * 1024 * 1024){ fbTip('「' + (f.name || '这个文件') + '」超过 12MB', true); continue; }
    if (total + f.size > 30 * 1024 * 1024){ fbTip('附件合计不能超过 30MB', true); break; }
    total += f.size;
    FBF.push({f: f, u: URL.createObjectURL(f)});
    fbTip('已选 ' + FBF.length + ' 个');
  }
  try { inp.value = ''; } catch (e) {}
  fbPaint();
}
function fbAppend(t){
  var ta = document.getElementById('fbText');
  if (!ta || !t) return;
  var cur = String(ta.value || '').replace(/\s+$/, '');
  ta.value = cur ? cur + '\n' + t : t;
  try { ta.focus(); ta.setSelectionRange(ta.value.length, ta.value.length); } catch (e) {}
}
/* 语音输入。壳里用原生录音（和中继 ASR 同一条路，长按随时撒手）；网页里退回 MediaRecorder。 */
var fbRec = null, fbChunks = [], fbRecAt = 0;
function fbMicLabel(t, bad){
  var b = document.getElementById('fbMic');
  if (!b) return;
  b.textContent = t;
  b.style.color = bad ? '#ff6467' : '';
}
function fbVoiceStart(e){
  if (e && e.preventDefault) e.preventDefault();
  if (fbRec || (window.CB && CB.pttState && (function(){ try { return CB.pttState() === 'rec'; } catch (x) { return false; } })())) return;
  if (window.CB && CB.pttDown){
    /* 结果别进命令输入框：handleResult 看到这个钩子就交给反馈框 */
    window.__cbPttTarget = function(t){ fbAppend(t); fbTip('已识别，看看有没有错字'); fbMicLabel('🎤 按住说'); };
    window.__cbPttTargetAt = Date.now();
    try {
      CB.pttDown();
      fbMicLabel('● 录音中… 松手结束', true);
      return;
    } catch (x) { window.__cbPttTarget = null; }
  }
  if (!(navigator.mediaDevices && window.MediaRecorder)){ fbTip('这个浏览器拿不到麦克风，请用 App', true); return; }
  fbRecAt = Date.now(); fbChunks = [];
  navigator.mediaDevices.getUserMedia({audio: true}).then(function(s){
    var mime = 'audio/webm';
    try {
      if (MediaRecorder.isTypeSupported && MediaRecorder.isTypeSupported('audio/webm;codecs=opus'))
        mime = 'audio/webm;codecs=opus';
      else if (MediaRecorder.isTypeSupported && MediaRecorder.isTypeSupported('audio/mp4'))
        mime = 'audio/mp4';
    } catch (x) {}
    try { fbRec = new MediaRecorder(s, {mimeType: mime}); } catch (x1) {
      try { fbRec = new MediaRecorder(s); } catch (x2) {
        try { s.getTracks().forEach(function(t){ t.stop(); }); } catch (x3) {}
        fbTip('这台设备录不了音：' + (x2 && x2.name), true);
        return;
      }
    }
    fbRec.ondataavailable = function(ev){ if (ev.data && ev.data.size) fbChunks.push(ev.data); };
    fbRec.onstop = function(){
      var ms = Date.now() - fbRecAt, type = (fbRec && fbRec.mimeType) || mime;
      try { s.getTracks().forEach(function(t){ t.stop(); }); } catch (x) {}
      fbRec = null;
      fbMicLabel('🎤 按住说');
      var blob = new Blob(fbChunks, {type: type});
      fbChunks = [];
      if (!blob.size || ms < 400){ fbTip('太短了，按住再说', true); return; }
      fbTip('转写中…');
      var _hd2 = {}, _cx2 = (window.__cbPttCtx ? window.__cbPttCtx() : '');
      if(_cx2) _hd2['X-CB-Ctx'] = _cx2;
      xjX('/api/ptt?ms=' + ms, {method: 'POST', ctype: type, body: blob, timeout: 120000, away: true, headers: _hd2})
        .then(function(r){
          /* 同 pttRequest：成功响应也带 quota，只有失败+带额度才是真超额（否则远程说话永远失败） */
          var _jq = r && r.json;
          if (_jq && _jq.quota && (_jq.ok === false || (r && r.status === 403) || (r && r.status === 429))){
            try { quotaSheet(_jq); } catch (x) {}
            fbTip((_jq && _jq.error) || '额度用完', true); return;
          }
          var d = null;
          try { d = JSON.parse(r.text); } catch (x) {}
          if (!d || !d.ok){ fbTip('转写失败：' + ((d && d.error) || ('HTTP ' + (r && r.status))), true); return; }
          var t = String(d.text || '').trim();
          if (!t){ fbTip('没听清，再说一次', true); return; }
          fbAppend(t);
          fbTip('已识别，看看有没有错字');
        })
        .catch(function(x){ fbTip('上传失败：' + (x && x.message || x), true); });
    };
    fbRec.start();
    fbMicLabel('● 录音中… 松手结束', true);
    if (fbStopWanted){ fbStopWanted = 0; try { fbRec.stop(); } catch (x) {} }   /* 授权框吃掉松手 */
  }).catch(function(x){
    fbMicLabel('🎤 按住说');
    fbTip('没有麦克风权限（' + (x && x.name) + '）', true);
  });
}
var fbStopWanted = 0;
function fbVoiceStop(e){
  if (e && e.preventDefault) e.preventDefault();
  if (window.CB && CB.pttUp && !fbRec){
    fbMicLabel('🎤 转写中…');
    try { CB.pttUp(); } catch (x) {}
    setTimeout(function(){
      if (!window.__cbPttTarget) fbMicLabel('🎤 按住说');
    }, 15000);
    return;
  }
  if (fbRec){ try { if (fbRec.state !== 'inactive') fbRec.stop(); } catch (x) {} }
  else fbStopWanted = 1;
}
(function(){
  var b = document.getElementById('fbMic');
  if (!b || b.__fbBound) return;
  b.__fbBound = 1;
  b.addEventListener('touchstart', fbVoiceStart, {passive: false});
  b.addEventListener('touchend', fbVoiceStop, {passive: false});
  b.addEventListener('touchcancel', fbVoiceStop, {passive: false});
  b.addEventListener('mousedown', fbVoiceStart);
  b.addEventListener('mouseup', fbVoiceStop);
})();
function fbSubmit(){
  var ta = document.getElementById('fbText');
  var t = ta ? String(ta.value || '').trim() : '';
  if (!t && !FBF.length){ toast('写点内容，或者加一张截图', true); return; }
  track('feedback_submit');
  if (t.length > 800) t = t.slice(0, 800);
  var b = document.getElementById('fbSend');
  if (b){ b.disabled = true; b.textContent = '提交中…'; }
  var fd = new FormData();
  fd.append('text', t);
  fd.append('kind', FBK);
  try { fd.append('ver', (window.CB && CB.appVer) ? String(CB.appVer() || '') : ''); } catch (e) {}
  try {
    fd.append('plat', ((window.CB && CB.native_ && CB.native_()) ? 'android-shell ' + (navigator.userAgent || '')
                                                                 : (navigator.userAgent || '')).slice(0, 40));
  } catch (e) {}
  FBF.forEach(function(x){ fd.append('f', x.f, x.f.name || 'shot'); });
  /* 直连中继提交（同源、带登录 cookie），大附件不经过开发机那条整包隧道；
     页面是开发机本地开的时候没有 /r/feedback，退回 /api/feedback 由开发机代转。 */
  xjX('/r/feedback', {method: 'POST', body: fd, timeout: 180000})
    .then(function(r){
      if (r && (r.status === 0 || r.status === 401 || r.status === 404 || r.status === 405))
        return xjX('/api/feedback', {method: 'POST', body: fd, timeout: 180000});
      return r;
    })
    .then(function(r){
      var d = (r && r.json) || {};
      if (r && r.ok && d && d.ok){
        var nid = d.id;
        if (ta) ta.value = '';
        FBF.forEach(function(x){ try { URL.revokeObjectURL(x.u); } catch (e) {} });
        FBF = []; fbPaint();
        fbTip('已经收到，谢谢你的反馈！');
        toast('反馈已提交，谢谢！');
        FBV.compose = 0;
        var cc = document.getElementById('fbCompose'); if (cc) cc.style.display = 'none';
        if (nid) fbDetail(nid); else fbListLoad();
      } else {
        fbTip('没提交上：' + ((d && d.error) || ('HTTP ' + (r && r.status))), true);
      }
    })
    .catch(function(e){ fbTip('提交失败：' + (e && e.message || e), true); })
    .then(function(){ if (b){ b.disabled = false; b.textContent = '提交反馈'; } });
}

/* ── 问题建议：工单列表 / 详情 / 回复 ──
   像阿里云工单：一条反馈是一个工单，官方回复后还能接着回，直到工单被关闭。
   手机直连中继 → /r/feedback/*（带登录 cookie）；页面是开发机本地开的 → 中继接口不存在，
   自动回落到 /api/feedback* 由开发机代转（带设备凭证，中继据此认账号）。 */
var FBST = ['待处理', '处理中', '已关闭'];
var FBV = {view: 'list', id: 0, compose: 0};
function fbCall(relEp, apiEp, opt){
  opt = opt || {};
  var tmo = opt.timeout || 12000;
  function once(ep){
    /* xjX 没有 onerror/ontimeout：网络不通时那个 Promise 永远不落地，
       这里套一层超时，免得页面一直停在「加载中…」。 */
    return Promise.race([xjX(ep, opt), new Promise(function(res){
      setTimeout(function(){ res({ok: false, status: 0, json: null}); }, tmo + 4000);
    })]);
  }
  return once(relEp).then(function(r){
    if (r && (r.status === 0 || r.status === 404 || r.status === 405)) return once(apiEp);
    return r;
  }).then(function(r){ return (r && r.json) || {}; });
}
function fbTime(ts){
  if (!ts) return '';
  try {
    var d = new Date(ts * 1000);
    function p(n){ return (n < 10 ? '0' : '') + n; }
    return (d.getMonth() + 1) + '-' + p(d.getDate()) + ' ' + p(d.getHours()) + ':' + p(d.getMinutes());
  } catch (e){ return ''; }
}
function fbMedia(id, media){
  if (!media || !media.length) return '';
  var h = '<div class="fbmedia">';
  media.forEach(function(m){
    var nm = (m && m.n) || '';
    if (!nm) return;
    var rel = '/r/feedback/media/' + encodeURIComponent(id) + '/' + encodeURIComponent(nm);
    var api = '/api/feedback_media/' + encodeURIComponent(id) + '/' + encodeURIComponent(nm);
    var fb = 'this.onerror=null;this.src=\'' + api + '\'';   /* 中继路径不通就换开发机代转的 */
    if (/^video\//.test((m && m.t) || ''))
      h += '<video src="' + rel + '" controls preload="metadata" onerror="' + fb + '"></video>';
    else
      h += '<img src="' + rel + '" onerror="' + fb + '">';
  });
  return h + '</div>';
}
function fbView(v){
  FBV.view = v;
  var det = document.getElementById('fbDetail'), lst = document.getElementById('fbList'),
      cmp = document.getElementById('fbCompose'), ttl = document.getElementById('fbTitle');
  if (ttl) ttl.textContent = (v === 'detail') ? ('工单 #' + FBV.id) : '问题建议';
  if (det) det.style.display = (v === 'detail') ? '' : 'none';
  if (lst) lst.style.display = (v === 'detail') ? 'none' : '';
  if (cmp && v === 'list') cmp.style.display = FBV.compose ? '' : 'none';
}
function fbOpen(){
  var p = document.getElementById('fbPage');
  if (!p) return;
  track('feedback');
  p.classList.add('on');
  fbView('list');
  fbListLoad();
}
function fbClose(){
  var p = document.getElementById('fbPage');
  if (p) p.classList.remove('on');
}
function fbBack(){
  if (FBV.view === 'detail'){ fbView('list'); fbListLoad(); return; }
  fbClose();
}
function fbNew(){
  var c = document.getElementById('fbCompose');
  if (!c) return;
  FBV.compose = FBV.compose ? 0 : 1;      /* 再点一次＝收起来 */
  c.style.display = FBV.compose ? '' : 'none';
  if (FBV.compose){ try { var ta = document.getElementById('fbText'); if (ta) ta.focus(); } catch (e) {} }
}
function fbListRender(items){
  var box = document.getElementById('fbList');
  if (!box) return;
  if (!items.length){
    box.innerHTML = '<div class="fbempty">还没有工单。<br>点右上角「＋ 新反馈」提一条，'
                  + '我们回复后可以接着聊。</div>';
    return;
  }
  var h = '';
  items.forEach(function(it){
    var st = parseInt(it.state || 0, 10);
    h += '<div class="fbcard tap" onclick="fbDetail(' + it.id + ')">'
      + '<div class="fbtop"><span class="fbkind">' + esc(it.kind || '问题') + '</span>'
      + (it.n_reply ? '<span class="fbkind">' + it.n_reply + ' 条回复</span>' : '')
      + '<span class="fbstate s' + st + '">' + (FBST[st] || FBST[0]) + '</span></div>'
      + '<div class="fbtext clamp">' + esc(it.text || '（只有附件）') + '</div>'
      + '<div class="fbmeta">' + fbTime(it.ts)
      + (it.last_ts ? ' · 最后回复 ' + fbTime(it.last_ts) : '') + '</div></div>';
  });
  box.innerHTML = h;
}
function fbListLoad(){
  var box = document.getElementById('fbList');
  if (!box) return;
  box.innerHTML = '<div class="fbempty">加载中…</div>';
  fbCall('/r/feedback/list', '/api/feedback_list')
    .then(function(d){
      if (!d || !d.ok){
        box.innerHTML = '<div class="fbempty">' + esc((d && d.error) || '加载失败，稍后再试') + '</div>';
        return;
      }
      fbListRender(d.items || []);
    });
}
function fbDetail(id){
  FBV.id = id;
  fbView('detail');
  var box = document.getElementById('fbDetail');
  if (!box) return;
  box.innerHTML = '<div class="fbempty">加载中…</div>';
  fbCall('/r/feedback/detail?id=' + encodeURIComponent(id),
         '/api/feedback_detail?id=' + encodeURIComponent(id))
    .then(function(d){
      if (!d || !d.ok){
        box.innerHTML = '<div class="fbempty">' + esc((d && d.error) || '打不开这条工单') + '</div>';
        return;
      }
      var it = d.item || {}, reps = d.replies || [], st = parseInt(it.state || 0, 10);
      var h = '<div class="fbcard"><div class="fbtop">'
            + '<span class="fbkind">' + esc(it.kind || '问题') + '</span>'
            + '<span class="fbstate s' + st + '">' + (FBST[st] || FBST[0]) + '</span></div>'
            + '<div class="fbtext">' + esc(it.text || '') + '</div>'
            + fbMedia(it.id, it.media)
            + '<div class="fbmeta">' + fbTime(it.ts) + '</div></div>';
      if (!reps.length) h += '<div class="fbempty" style="padding:18px 10px">还没有回复</div>';
      reps.forEach(function(r){
        var isAd = (r.who === 'admin');
        h += '<div class="fbwho">' + (isAd ? '官方' : '我') + ' · ' + fbTime(r.ts) + '</div>'
          + '<div class="fbbub ' + (isAd ? 'a' : 'u') + '">' + esc(r.text || '') + '</div>'
          + fbMedia(it.id, r.media);
      });
      if (st === 2){
        h += '<div class="fbclosed">这条工单已关闭。有新问题请另开一条。</div>';
      } else {
        h += '<div class="fbrow"><textarea id="fbReplyText" placeholder="接着说…"></textarea>'
          + '<button onclick="fbReplySend(' + it.id + ')">发送</button></div>';
      }
      box.innerHTML = h;
    });
}
function fbReplySend(id){
  var ta = document.getElementById('fbReplyText');
  var t = ta ? String(ta.value || '').trim() : '';
  if (!t){ toast('写点内容再发', true); return; }
  fbCall('/r/feedback/reply', '/api/feedback_reply',
        {method: 'POST', ctype: 'application/json', body: JSON.stringify({id: id, text: t})})
    .then(function(d){
      if (d && d.ok){ toast('已回复'); fbDetail(id); }
      else toast((d && d.error) || '回复失败，稍后再试', true);
    });
}
fbKind('问题');