// SPDX-License-Identifier: AGPL-3.0-or-later

/* 客户端标识（在线客户端数用）：同一 Wi-Fi/出口 IP 下多台设备会被中继当成一台，
   这里给每个浏览器一个长期 cookie，中继按它计数。 */
try{
  if(!/(?:^|;\s*)cb_cid=/.test(document.cookie||'')){
    var _cid='c'+Date.now().toString(36)+Math.random().toString(36).slice(2,10);
    document.cookie='cb_cid='+_cid+';path=/;max-age=31536000;samesite=lax';
  }
}catch(e){}
/* xterm 的 open() 用 Chrome86+ 才有的 replaceChildren——老内核（红米 <86、坚果 62）在此炸掉，
   构造器已建、面板永远空白（控制台只见一帧 replaceChildren is not a function）。补等价实现。 */
if(typeof Element!=='undefined'&&!Element.prototype.replaceChildren){
  Element.prototype.replaceChildren=function(){
    this.textContent='';
    for(var i=0;i<arguments.length;i++){ var n=arguments[i]; if(n&&n.nodeType) this.appendChild(n); }
  };
}
/* 部分国产 OEM 内核阉割了 Array.prototype.values（xterm 的 SortedList.reset 用到；
   Chrome 67-73 也曾移除）。Symbol.iterator 是等价实现，坚果 62 实测缺、迭代器在。 */
if(typeof Array.prototype.values!=='function'&&typeof Symbol!=='undefined'&&Symbol.iterator){
  Array.prototype.values=function(){ return this[Symbol.iterator](); };
}
/* xterm 用到 Chrome71+ 才有的 queueMicrotask：老内核（坚果 62）没有它，
   new Terminal() 直接抛 ReferenceError，原样页只剩一片黑。Promise.then 等价。 */
if(typeof window.queueMicrotask!=='function'){
  window.queueMicrotask=function(cb){ Promise.resolve().then(cb); };
}