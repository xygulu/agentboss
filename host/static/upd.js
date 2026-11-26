// SPDX-License-Identifier: AGPL-3.0-or-later

// host 新版本提示：麦克风上方的小圆点（不压标题）。
// 点一下跳到设置页「开发机服务」更新区并自动检查；版本更新后页面会重载。
(function(){
  var box=document.getElementById('upd'); if(!box) return;
  var info=null;
  var timer=null;

  window.updPaint=function(){
    if(!info||(typeof pageIdx!=='undefined'&&pageIdx===3)){
      box.style.display='none'; return;
    }
    box.title='可更新'+(info.ver?'到 v'+info.ver:'')+'，点开去更新';
    box.style.display='flex';
  };
  window.updTap=function(e){
    if(e) e.stopPropagation();
    try{
      goPage(3);
      setTimeout(function(){
        var el=document.getElementById('grpVer');
        if(el&&el.scrollIntoView) el.scrollIntoView({block:'start'});
      },80);   // 等设置页渲染完再滚
      if(window.hsCheck) hsCheck(1);   // 打开就强制查一次，直接露出「更新服务」
    }catch(_){}
    updPaint();
  };

  /* 更新完（开发机重启、版本追平）之后那个蓝箭头必须自己消失。
     以前固定 15 分钟查一次、且页面在后台/息屏时直接跳过 —— 手机上黑屏半小时回来，
     goPage 一画就是半小时前那份「有新版本」，于是「已经是最新了还挂着箭头」。
     现在：回前台立刻查一次；挂着箭头时 60 秒复查，没有时 3 分钟一次。
     /api/version 在开发机侧是 USE_PROBE_PATHS（不计使用时长），查得勤也不花钱。 */
  function sched(ms){
    if(timer) clearTimeout(timer);
    timer=setTimeout(chk, ms||(info?60000:180000));
  }
  function chk(){
    if(document.hidden){ sched(); return; }
    fetch('/api/version',{cache:'no-store'})
      .then(function(r){ return r.json(); })
      .then(function(v){
        info=(v&&v.update)?{ver:v.relay_ver||v.gh_tag||''}:null;
        updPaint(); sched();
      })
      .catch(function(){ sched(); });
  }
  document.addEventListener('visibilitychange',function(){
    if(document.hidden){ sched(); return; }
    chk();           // 回前台：先查再画，别让旧状态挂在那儿
  });
  chk();
})();
