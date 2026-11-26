// SPDX-License-Identifier: AGPL-3.0-or-later

package com.agentboss.terminal;

import android.Manifest;
import android.app.Activity;
import android.content.SharedPreferences;
import android.content.ComponentName;
import android.content.pm.PackageManager;
import android.graphics.Rect;
import android.media.MediaRecorder;
import android.net.ConnectivityManager;
import android.net.Network;
import android.net.NetworkCapabilities;
import android.net.NetworkRequest;
import android.os.Bundle;
import android.util.Base64;
import android.util.DisplayMetrics;
import android.view.KeyEvent;
import android.view.View;
import android.view.ViewGroup;
import android.view.ViewTreeObserver;
import android.view.WindowManager;
import android.view.inputmethod.EditorInfo;
import android.view.inputmethod.InputConnection;
import android.webkit.CookieManager;
import android.webkit.JavascriptInterface;
import android.webkit.PermissionRequest;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.util.Log;

import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.net.HttpURLConnection;
import java.net.URL;

/**
 * 唯一界面：全屏 WebView 指向 Mac 上的 codeboss。
 * - 开机/按 Home 都进这里（已注册 HOME）
 * - 音量键 = 按住说话；页面上的 🎤 按钮也走同一套原生录音
 * - 录音走 App 自己的 MediaRecorder（MIUI 上 WebView 的 getUserMedia 采不到音）
 * - 录完直接把 m4a POST 给 Mac 上的反代 /api/ptt，反代做 ASR，回文字给页面
 */
public class MainActivity extends Activity {

    private static final String TAG = "Codeboss";
    private static final String PREFS = "codeboss";
    private static final String KEY_URL = "url";
    private static final String KEY_PROMPTED = "apk_prompted_code";   /* [codeboss] 已提示安装过的 APK 版本号 */
    private static final String KEY_MIUI_HINT = "miui_upd_hint";      /* [codeboss] 小米「更新后自动回来」的开关提示过没有 */
    private static final String KEY_RUNMODE = "runMode";              /* 运行模式："app"（默认）| "kiosk" */
    private static final String KEY_GUIDED = "guided";                /* 首次运行引导已看过 */
    private static final int REQ_HOME_ROLE = 4001;                    /* RoleManager HOME 角色申请 */
    private static final int REQ_SCAN = 4002;                         /* 扫码配对（ScanActivity） */
    private static final int REQ_FILE = 4003;                         /* 页面 <input type=file>：选截图/视频 */
    private static final int PERM_CAMERA_FILE = 4;                    /* 拍照派活：起相机前要 CAMERA 权限 */
    /* 拍照派活（B1）：chooser 带相机；相机把照片写到 EXTRA_OUTPUT，返回的 data 是 null，
       所以要记住预先给的 content://，结果分支里按它回填给页面。 */
    private android.net.Uri pendingCapture;
    private boolean pendFileImg, pendFileVid, pendFileMulti;
    private android.webkit.ValueCallback<android.net.Uri[]> fileCb;   /* 选完要回填给 WebView */
    /* 视频原生全屏（onShowCustomView）：播放器控制条那个全屏键，壳不实现 CustomView
       它就一直是灰的。fsCb 必须留着——退出时回调 WebView，它才把视频收回卡片。 */
    private View fsView;
    private WebChromeClient.CustomViewCallback fsCb;
    /* 安装状态改走 Activity 的 PendingIntent：覆盖安装会 force-stop 本包，
       把闹钟/Job 全清掉，静态广播也会被当后台广播跳过（AOSP 9 实测
       "Skipping delivery of ordered [background] … MY_PACKAGE_REPLACED"）。
       系统发起的 Activity PendingIntent 不受后台启动限制，能直接把冷进程拉起来。 */
    static final String ACTION_INSTALL = "com.agentboss.terminal.INSTALL_STATUS";
    private boolean updIntentSeen = false;   /* 这次启动是装完状态拉起来的 */
    private static final int PERM_CAMERA_SCAN = 3;                    /* 扫码前申请相机权限 */
    /* boss 系列共享中继（中继复用，见设计 2026-11-17）：产品线靠 ?product= 区分，
       更新检查的官方源 = 中继（relay 按 ?product= 答对应产品的包）。 */
    private static final String UPD_BASE = "https://relay.example.com";
    private static String sProduct;     /* 构建期按产品写入的资源值：双包各取各的 */
    private String product() {
        if (sProduct == null) sProduct = getString(R.string.cb_product);
        return sProduct;
    }
    private String defaultUrl() { return UPD_BASE + "/?product=" + product(); }

    /* WebView 的导航白名单：页面里可能点到站外链接，而 CB 桥是挂在整个 WebView 上的
       （addJavascriptInterface 不分页面）。不拦的话，任何被导航过去的站点都能拿到
       CB.clipboard()/CB.getCfg() 这些原生能力。只放行自家域名、当前 base、局域网私网。 */
    private static final java.util.regex.Pattern PRIVATE_HOST = java.util.regex.Pattern.compile(
            "^https?://(10\\.\\d{1,3}\\.\\d{1,3}\\.\\d{1,3}"
            + "|192\\.168\\.\\d{1,3}\\.\\d{1,3}"
            + "|172\\.(1[6-9]|2\\d|3[01])\\.\\d{1,3}\\.\\d{1,3}"
            + "|127\\.0\\.0\\.1|localhost)(:\\d+)?(/.*)?$");

    private boolean trustedNav(String url) {
        if (url == null || url.isEmpty()) return false;
        String u = url.toLowerCase(java.util.Locale.ROOT);
        if (u.startsWith("about:") || u.startsWith("data:") || u.startsWith("blob:")
                || u.startsWith("javascript:") || u.startsWith("file://")) return true;
        if (PRIVATE_HOST.matcher(u).matches()) return true;
        /* boss 系列共享中继（与 codeboss 壳同口径；下载走系统 DownloadManager，不白名单下载域） */
        if (u.equals("https://relay.example.com") || u.startsWith("https://relay.example.com/")
                || u.equals("http://relay.example.com") || u.startsWith("http://relay.example.com/"))
            return true;
        /* 自建/开发部署：页面自己保存过 base（saveBase），那就是它认可的服务端 */
        try {
            String base = getSharedPreferences(PREFS, MODE_PRIVATE).getString("base", "");
            if (base != null && !base.isEmpty()) {
                String b = base.toLowerCase(java.util.Locale.ROOT);
                if (u.equals(b) || u.startsWith(b + "/") || u.startsWith(b + ":")) return true;
            }
        } catch (Throwable ignored) {}
        return false;
    }

    /** 页面是否在前台：后台提醒服务（BgService）据此避免和页面重复提醒 */
    static volatile boolean VISIBLE = false;
    static volatile long PAUSED_AT = 0;
    /* 页面正在看哪个会话（页面切会话时调 setView）：只用来决定「这条还要不要发系统通知」，
       不决定念不念——正在盯着会话等结果时，语音照念（没声会以为提醒坏了）。 */
    static volatile String VIEW_SID = "";

    private WebView web;
    /* 页面活性看门狗：老 WebView 长时间后台后整页会冻死——画面还停在最后一帧，
       点/滑全无反应（web.onPause 之后没回来、渲染进程被系统回收都会这样），
       以前只能杀进程再进。壳每 2.5s 让页面回一声（evaluateJavascript 的回调），
       断 12 秒就当页面已死，直接重建界面（recreate）：页面从本地缓存回到原会话。 */
    private volatile long pongAt = 0L;        /* 最近一次页面回话；0=还没加载好，不监控 */
    private volatile long probeAt = 0L;
    private long recoverAt = 0L;              /* 上次重建时刻：一分钟内只修一次 */
    private volatile boolean rendererDead = false;
    private static final String KEY_WDG = "wdg_rebuilt";   /* 重建过：下次页面加载时打进日志 */
    private long lastSendAt = 0L;      // 上次用音量下键发送的时刻（防连发）
    /* 音量下键：短按发送、长按 0.8s = 静音开关。按下只起计时，松手才决定做哪件
       （见 handleKey / volDownUp）——以前按下即发送，长按就没法再赋予第二个含义。 */
    private static final long VOL_LONG_MS = 800;
    private Runnable volHold;          // 长按计时器（到点在按住中触发）
    private boolean volPressed;        // 这次按下归我们管（已起计时）
    private boolean volLong;           // 已判定长按（松手不再发送）
    private final android.os.Handler ui = new android.os.Handler(android.os.Looper.getMainLooper());
    private MediaRecorder rec;
    private String recPath;
    private long recStart = 0L;
    private Runnable recCap;          /** 录音上限（60s）自停，防止漏停把麦克风占死 */

    /* ── 自动听写（AI 回复完 / 等你确认时自动开麦） ──────────────────────
       autoRec=true 表示这次录音不是「按住说话」：不回调 __pttStart（按钮不亮）、
       由 VAD 判断开口/说完，空闲超时直接丢弃不上传（不花 ASR 额度）。
       autoPhase：dictate=说指令，confirm=只说「发送」/「取消」。 */
    private boolean autoRec = false;
    /* 自动听写是「放着手机、用嘴说」的场景：这期间屏幕一黑（系统息屏）壳就 onPause，
       按规矩立刻收麦——用户看到的就是「刚开麦就退出」。所以听写期间临时常亮，
       收麦后按设置把常亮还回去（用户没开常亮就别给他留常亮）。 */
    private boolean autoKeepOn = false;
    private String autoPhase = "";
    private Runnable autoVad;
    private long autoStartAt = 0L, autoLastLoud = 0L;
    /* 底噪用「下限跟踪」：掉下去立刻跟（停顿/换气），回升要慢。
       不能用「前 0.8 秒均值」当底噪——用户常常一开麦就说话，那段均值就是人声，
       门限被抬到人声之上，整段都判成静音（2026-11 实测「听不到声音」就是这么来的）。 */
    private float autoFloor = -1f, autoThr = 0f;
    /* 说话后判「说完了」不看绝对门限：有的 ROM 环境底噪高、有的 getMaxAmplitude
       干脆返回「开始以来的最大值」（永远不回落），绝对门限一辈子不成立——2026-11
       实测：说完一句后录满 45 秒上限，然后把整段丢掉报「没听到」。
       改成跟「最近有多响」比：autoRecent 缓慢衰减，音量掉到它的 30% 就算停。 */
    private float autoRecent = 0f;
    private int autoPeak = 0;
    private int autoLoudCnt = 0, autoZeroCnt = 0, autoFrame = 0;
    private long autoSpokeAt = 0L;
    private boolean autoSpoke = false;
    static volatile MainActivity INSTANCE;   /* BgService 播完语音要回调页面 */
    /* 通知渠道：按提醒类型分开，音型不同（用户可在系统设置里各自调）。
       渠道一旦建好音型就固定，改音要换 id——所以带 v1 后缀。 */
    private static final String CH_DONE = "progress_done_v1";
    private static final String CH_PERM = "progress_perm_v1";
    private static final String CH_INPUT = "progress_input_v1";
    private static final String CH_QUIET = "progress_quiet_v1";   /* 前台已经出过声 → 只留通知，不出声 */
    private static final String PRE_TTS_VOICE = "tts_voice";      /* 用户挑过的朗读嗓音名 */
    private android.speech.tts.TextToSpeech tts;
    private volatile boolean ttsOk = false;
    private volatile android.media.MediaPlayer cueMp;     /* 内置提醒语音（assets/tts），不依赖系统 TTS 引擎 */
    private volatile android.media.MediaPlayer sayMp;     /* 前台侧播报（息屏提醒关着时用），和 cueMp 分开 */
    /* 页面把 /api/say 交给壳之后、真正开始出声之前（下载/缓存）也算「在播」：
       自动听写这时开麦就会把播报录进去。 */
    private volatile boolean fgSayBusy = false;
    /* 上一句还在念时，新来的一句先排在这（2026-11-20：播报不许半路截断） */
    private volatile java.io.File fgPend;
    private volatile String pendingSid = null;   /** 通知点开要落的会话（页面可能还没就绪） */

    private void cancelRecCap() {
        if (recCap != null) { ui.removeCallbacks(recCap); recCap = null; }
    }

    /** 播报结束（前台 sayMp 放完，或 BgService 放完）：告诉页面「现在可以开麦了」。
        自动听写必须等这句话念完再录，否则麦克风会把播报自己录进去。 */
    static void notifySayDone(String sid) {
        final MainActivity a = INSTANCE;
        if (a == null) return;
        final String s = sid == null ? "" : sid;
        a.ui.post(new Runnable() {
            @Override public void run() {
                a.js("window.__cbSayDone && window.__cbSayDone(" + quote(s) + ")");
            }
        });
    }

    /** 播报没放出来（合成音频取不到/解码失败）：留一行进开发机日志。
        以前这里静默，开发机只看到「已播报」的水位，查不出「为什么没声」。 */
    static void notifySayFail(String sid, String kind) {
        final MainActivity a = INSTANCE;
        if (a == null) return;
        final String s = sid == null ? "" : sid;
        final String k = kind == null ? "" : kind;
        a.ui.post(new Runnable() {
            @Override public void run() {
                a.js("window.__cbSayFail && window.__cbSayFail(" + quote(k) + "," + quote(s) + ")");
            }
        });
    }

    /** 内置提醒音放完（页面用它决定什么时候再开麦，例如确认阶段的提示语） */
    static void notifyCueDone(String kind) {
        final MainActivity a = INSTANCE;
        if (a == null) return;
        final String k = kind == null ? "" : kind;
        a.ui.post(new Runnable() {
            @Override public void run() {
                a.js("window.__cbCueDone && window.__cbCueDone(" + quote(k) + ")");
            }
        });
    }

    /** 自愈心跳：每 2.5s 重新调一次 shim，节点被页面重绘掉就重建 */
    private final Runnable shimBeat = new Runnable() {
        @Override public void run() {
            /* 离线页上不要注入 ⚙/🎤：那页没有输入框，🎤 按了只会白白上传失败 */
            String u = null;
            try { u = web.getUrl(); } catch (Throwable ignored) { }
            if (u == null || !u.startsWith(OFFLINE_PREFIX)) { injectShim(); }
            /* 页面心跳：App 被挂起时心跳会停，页面据此判后台（自己睡，不发请求）。
               前台时顺便当活性探针：页面被冻住这一发永远不回，看门狗据此重建。 */
            final String probe = "window.__cbAlive&&window.__cbAlive(1)";
            if (VISIBLE && web != null) {
                probeAt = android.os.SystemClock.elapsedRealtime();
                try {
                    web.evaluateJavascript(probe, new android.webkit.ValueCallback<String>() {
                        @Override public void onReceiveValue(String v) {
                            pongAt = android.os.SystemClock.elapsedRealtime();
                        }
                    });
                } catch (Throwable ignored) { }
                if (pongAt > 0 && probeAt - pongAt > 12000) recoverWeb("no-pong");
            } else {
                js(probe);
            }
            ui.postDelayed(this, 2500);
        }
    };

    /* 网络看门狗：MIUI 等 ROM 上 registerNetworkCallback 有时不回调，
       所以再加一道：每 15 秒自己探一次，网络一通就重载。离线也能自己活回来。 */
    private final Runnable netWatch = new Runnable() {
        @Override public void run() {
            if (netFail && retryRun == null) {
                Log.i(TAG, "看门狗：还在离线，主动探一次");
                probeAndReload();
            }
            ui.postDelayed(this, 15000);
        }
    };

    /** 页面冻死（心跳不回/渲染进程没了）：重建界面，别让用户自己杀进程。
        重建后页面从 localStorage 找回落在了哪个会话、翻到了哪一屏。 */
    private void recoverWeb(String why) {
        long now = android.os.SystemClock.elapsedRealtime();
        if (recoverAt > 0 && now - recoverAt < 60000) return;   /* 一分钟只修一次，防连环重建 */
        recoverAt = now;
        pongAt = now;                          /* 先归零：同一次卡死里别连发 */
        Log.w(TAG, "页面无响应(" + why + ")，重建界面");
        try {
            getSharedPreferences(PREFS, MODE_PRIVATE).edit().putBoolean(KEY_WDG, true).apply();
        } catch (Throwable ignored) { }
        ui.post(new Runnable() {
            @Override public void run() {
                try {
                    recreate();
                } catch (Throwable t) {
                    Log.w(TAG, "重建失败，改重载页面: " + t);
                    try { if (web != null && lastUrl != null) web.loadUrl(lastUrl); } catch (Throwable ignored) { }
                }
            }
        });
    }

    private final Runnable webSleeper = new Runnable() {
        @Override public void run() { try { web.onPause(); } catch (Exception e) {} }
    };

    /* ── 屏幕亮/灭：用系统广播拿权威状态 ──────────────────────────────
       onPause / document.hidden / 壳心跳都是间接推断，实测有 ROM 在息屏后
       把 WebView 的 setInterval 节流成「每分钟一次」而不是停掉；老页面若没有
       后台判断，就会按这个节奏一直打点，使用时长整夜被记。这里熄屏时直接
       停掉 WebView 的全部定时器（不管页面是新版还是旧版），亮屏再恢复；
       页面真正唤醒仍只由 onResume 负责（亮屏可能还压在锁屏下）。 */
    private final Runnable screenPause = new Runnable() {
        @Override public void run() { try { web.onPause(); } catch (Throwable ignored) { } }
    };
    private android.content.BroadcastReceiver screenRc;

    private void setupScreenState() {
        screenRc = new android.content.BroadcastReceiver() {
            @Override public void onReceive(android.content.Context c, android.content.Intent in) {
                String a = in == null ? "" : in.getAction();
                if (android.content.Intent.ACTION_SCREEN_OFF.equals(a)) {
                    Log.i(TAG, "屏幕熄灭：页面全睡 + 停 WebView 定时器");
                    js("window.__cbVisible&&window.__cbVisible(0)");
                    try { web.pauseTimers(); } catch (Throwable ignored) { }
                    ui.removeCallbacks(webSleeper);
                    ui.removeCallbacks(screenPause);
                    /* 留 600ms 让页面先把 SSE 断开；立刻冻会连断连逻辑一起冻住 */
                    ui.postDelayed(screenPause, 600);
                } else if (android.content.Intent.ACTION_SCREEN_ON.equals(a)) {
                    /* 先撤掉还没执行的「熄屏暂停」：600ms 内又亮屏时它会把一个
                       已经 resumed 的 WebView 又冻回去（页面从此点/滑全无反应）。 */
                    ui.removeCallbacks(screenPause);
                    try { web.resumeTimers(); } catch (Throwable ignored) { }
                    /* 亮屏≠回前台（可能还压在锁屏下），所以只有 Activity 真的在
                       resumed 态才叫醒页面；否则仍交给 onResume。 */
                    if (VISIBLE) {
                        try { web.onResume(); } catch (Throwable ignored) { }
                        js("window.__cbVisible&&window.__cbVisible(1)");
                    }
                }
            }
        };
        android.content.IntentFilter f = new android.content.IntentFilter();
        f.addAction(android.content.Intent.ACTION_SCREEN_OFF);
        f.addAction(android.content.Intent.ACTION_SCREEN_ON);
        registerReceiver(screenRc, f);
    }

    @Override
    protected void onResume() {
        super.onResume();
        VISIBLE = true;
        /* 装完回来的这一趟：界面真正 resumed 时再点一次屏幕（第一下有时点不亮） */
        if (updWake) wakeNow();
        Relaunch.noteForeground(this);   /* 更新装完的「拉回前台」是否成功，以此为准 */
        Relaunch.clearNote(this);    /* 已经回来了：装之前挂的「点一下回来」撤掉 */
        /* 亮屏那一下排的「熄屏暂停」如果还没跑，别让它落在回前台之后 */
        ui.removeCallbacks(screenPause);
        /* 息屏时停过 WebView 定时器；回到前台（哪怕只是解锁）必须恢复，
           否则页面的轮询/倒计时会整块哑掉。 */
        try { web.resumeTimers(); } catch (Throwable ignored) { }
        /* 给页面 12 秒证明自己还活着（看门狗从这一刻重新计时） */
        pongAt = android.os.SystemClock.elapsedRealtime();
        /* 渲染进程在后台被系统回收过：这一趟不用等了，直接重建界面 */
        if (rendererDead) { rendererDead = false; recoverWeb("renderer-dead"); return; }
        /* 回前台：自动亮度重新开始读光线（进后台时注销过，省电） */
        if (autoBrightOn) attachLightSensor();
        /* 回前台：页面补一轮全量 + 顺带重探直连 */
        js("window.__cbVisible&&window.__cbVisible(1)");
        BgService.startIfEnabled(this);   // 服务被杀/开关刚打开：回前台顺手补起
        web.removeCallbacks(webSleeper);
        web.onResume();   /* 页面恢复跑（后台期间 webSleeper 已冻结它；visibilitychange→visible 会立即重连） */
        /* 回前台时还是没网：接着按「6 秒后切网络页」的节奏走（后台期间排的会自己撤掉） */
        if (!netOnline()) scheduleOfflineSoon();
        final String qr = pendingQr;
        if (qr != null) {
            pendingQr = null;
            ui.postDelayed(new Runnable() {
                @Override public void run() {
                    js("window.__cbQr&&window.__cbQr(" + quote(qr) + ")");
                }
            }, 250);
        }
        deliverSid();                         /* 通知点开：页面已加载时回前台直接切过去 */
        if (!viewInstall) applyOverlays();   /* 回前台补挂悬浮层（授权页回来/首次创建都覆盖）；
                                               兜底安装期间不挂——罩会盖在系统安装器上 */
        js("window.__cbEyeSys&&window.__cbEyeSys()");
        /* 从系统面板/设置回来时，立刻探一次（用户可能刚把 WiFi 打开） */
        if (netFail) {
            ui.postDelayed(new Runnable() {
                @Override public void run() {
                    if (netFail && retryRun == null) { probeAndReload(); }
                }
            }, 800);
        }
    }

    /** 退出视频原生全屏：撤掉全屏视图、亮回 WebView、回调 WebView 收尾。
        看门狗 recreate 重建过界面的话 fsCb 可能已失效——全程 try/catch，别把壳搞崩。 */
    private void hideFs() {
        View v = fsView;
        fsView = null;
        WebChromeClient.CustomViewCallback cb = fsCb;
        fsCb = null;
        if (v != null) {
            try {
                v.setKeepScreenOn(false);
                ViewGroup p = (ViewGroup) v.getParent();
                if (p != null) p.removeView(v);
            } catch (Throwable ignored) {}
        }
        try { web.setVisibility(View.VISIBLE); } catch (Throwable ignored) {}
        if (cb != null) { try { cb.onCustomViewHidden(); } catch (Throwable ignored) {} }
    }

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        INSTANCE = this;
        /* 原生请求（版本检查、直下安装包、语音上传）用的是 http.agent 这个系统属性，
           默认只有 Dalvik/2.1.0 (…Build/…)——中继的「终端」页从 UA 里读 CodebossShell/x.y.z
           才显示版本，于是安卓机那一列永远是空的（WebView 的请求带了，但直连开发机时不经过中继）。
           这里补上壳的标记，和 WebView 那边保持一致。 */
        try {
            System.setProperty("http.agent", shellUa());
        } catch (Throwable ignored) { }
        if (keeponOn()) getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        updWakeShow(getIntent());   /* 装完把我们拉起来：亮屏 + 盖在锁屏上，别让用户只看到黑屏 */

        /* 桌面接管策略只在 kiosk 模式应用；App 模式下曾开过接管的，开机先清干净 */
        if ("kiosk".equals(runMode()) && Kiosk.isOwner(this)) {
            Kiosk.applyPolicies(this);
        } else if (Kiosk.isOwner(this)) {
            Kiosk.teardownPolicies(this);
        }
        setHomeGate("kiosk".equals(runMode()));   /* HOME 入口随模式同步：App 模式启动即关——升级前卡在默认桌面的机器一重启即自动还原 */

        if (checkSelfPermission(Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(new String[]{Manifest.permission.RECORD_AUDIO}, 1);
        }
        applyOverlays();   /* 护眼/压暗悬浮层（键盘、状态栏也要罩到） */

        web = new CBWebView(this);
        /* 只在 debuggable 包里开 CDP（chrome://inspect）：release 包任何 USB 连接方
           都能拿到页面上下文，直接调 CB 桥。构建脚本默认 debuggable=false。 */
        WebView.setWebContentsDebuggingEnabled(
                (getApplicationInfo().flags & android.content.pm.ApplicationInfo.FLAG_DEBUGGABLE) != 0);
        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);
        /* 混合内容：页面从 https://relay.example.com 加载，局域网探针是
           http://192.168.x.x:8790/api/ping —— WebView 默认 COMPATIBILITY_MODE
           会拦掉这种「主动型」混合内容（XHR），探针必挂 → 同一个局域网也显示
           「中继转发」。壳自己发请求不受影响，只有页面里的 fetch 受限，所以
           在壳这一层放开。 */
        s.setMixedContentMode(WebSettings.MIXED_CONTENT_ALWAYS_ALLOW);
        /* 预览浮层里直开局域网页面时，预览站自己的会话 cookie 属「第三方」
           （顶层是中继源，iframe 是 http://192.168.x.x:port）——targetSdk 28
           默认不收，登录了也存不住。壳只导航到自家域名/当前 base/私网
           （trustedNav 管着），这里放开到普通浏览器同款口径（2026-11-24）。 */
        android.webkit.CookieManager.getInstance().setAcceptThirdPartyCookies(web, true);
        s.setDomStorageEnabled(true);
        s.setMediaPlaybackRequiresUserGesture(false);
        s.setUseWideViewPort(true);
        s.setLoadWithOverviewMode(true);
        s.setCacheMode(WebSettings.LOAD_NO_CACHE);
        /* UA 里打上壳的标记：局域网代理据此判断"这个客户端自己会注入，别再塞 ptt_inject.html"，
           否则会出现两套 🎤/设置入口（蓝药丸是旧样式，填字还是覆盖式）。 */
        try {
            String ua = s.getUserAgentString();
            String ver = "0";
            try {
                ver = getPackageManager().getPackageInfo(getPackageName(), 0).versionName;
            } catch (Throwable ignored) {
            }
            if (ua != null && !ua.contains("CodebossShell")) {
                s.setUserAgentString(ua + " CodebossShell/" + ver);
            }
        } catch (Throwable ignored) {
        }

        web.addJavascriptInterface(new Bridge(), "CB");

        /* 不许长按：WebView 长按链接/文字会弹系统菜单（复制链接/打开/分享），
           菜单里带完整 URL（域名 + 端口 + 会话 id，甚至 ?t= token）——这是错误页之外第二个泄露口。 */
        web.setLongClickable(false);
        web.setHapticFeedbackEnabled(false);
        web.setOnLongClickListener(new View.OnLongClickListener() {
            @Override public boolean onLongClick(View v) { return true; }   // 吞掉，不弹菜单
        });
        s.setSaveFormData(false);
        s.setJavaScriptCanOpenWindowsAutomatically(false);

        web.setWebChromeClient(new WebChromeClient() {
            /* 视频原生全屏：全屏视图铺到内容层最上面，WebView 暂时藏起来；
               退出（播放器收起键 / 返回键）原样收回。看门狗不用让路——全屏
               只是盖住内容，页面 JS 探针照常回话，不算走神。 */
            @Override
            public void onShowCustomView(View view, CustomViewCallback cb) {
                if (fsView != null) {   /* 已经在全屏里又来一个：拒掉新的，别叠两层 */
                    try { cb.onCustomViewHidden(); } catch (Throwable ignored) {}
                    return;
                }
                fsView = view;
                fsCb = cb;
                try {
                    view.setBackgroundColor(0xFF000000);
                    view.setKeepScreenOn(true);   /* 看到一半息屏最烦 */
                    ViewGroup content = (ViewGroup) findViewById(android.R.id.content);
                    content.addView(view, new ViewGroup.LayoutParams(
                            ViewGroup.LayoutParams.MATCH_PARENT,
                            ViewGroup.LayoutParams.MATCH_PARENT));
                    web.setVisibility(View.INVISIBLE);
                } catch (Throwable t) {
                    Log.w(TAG, "fullscreen video failed: " + t);
                    hideFs();
                }
            }

            @Override
            public void onHideCustomView() { hideFs(); }

            @Override
            public void onPermissionRequest(final PermissionRequest request) {
                runOnUiThread(new Runnable() {
                    @Override public void run() {
                        // 授音频，其它资源（视频/画中画）一律拒
                        java.util.List<String> ok = new java.util.ArrayList<String>();
                        for (String r : request.getResources()) {
                            if (PermissionRequest.RESOURCE_AUDIO_CAPTURE.equals(r)) ok.add(r);
                        }
                        if (ok.isEmpty()) {
                            request.deny();
                        } else {
                            request.grant(ok.toArray(new String[0]));
                        }
                    }
                });
            }

            /* 设置页「问题与建议」要挑截图/录屏：老 WebView 没有文件选择器，
               不接这个回调，点「＋ 截图/视频」什么都不发生（连系统选择器都不弹）。 */
            @Override
            public boolean onShowFileChooser(WebView view,
                    final android.webkit.ValueCallback<android.net.Uri[]> cb,
                    FileChooserParams params) {
                if (fileCb != null) { try { fileCb.onReceiveValue(null); } catch (Throwable ignored) {} }
                fileCb = cb;
                try {
                    boolean img = false, vid = false;
                    String[] acc = params.getAcceptTypes();
                    if (acc != null) for (String a : acc) {
                        if (a == null) continue;
                        String s = a.trim().toLowerCase(java.util.Locale.ROOT);
                        if (s.contains("image")) img = true;
                        if (s.contains("video")) vid = true;
                    }
                    boolean multi = params.getMode() == FileChooserParams.MODE_OPEN_MULTIPLE;
                    /* kiosk（设成默认桌面）时，系统选图器一冒头本 App 就失焦，
                       看门狗 250ms 内把本 App 拉回前台 → 选择器被顶掉，用户看到
                       「点一下就弹回来」。跟扫码页一样：先置 expectAway 让看门狗让路。 */
                    expectAway = true;
                    fileOpen = true;
                    ui.removeCallbacks(awayCap);   /* 清掉可能在途的「恢复看门狗」定时器，别提前收标志 */
                    ui.removeCallbacks(fileCap);
                    ui.postDelayed(fileCap, 180000);   /* 兜底：选完/取消后没回调也不卡住让路标志 */
                    if (img && checkSelfPermission(Manifest.permission.CAMERA)
                            != PackageManager.PERMISSION_GRANTED) {
                        /* 本包声明了 CAMERA：未授权直接起 ACTION_IMAGE_CAPTURE 会 SecurityException。
                           先申请，回来再开 chooser（onRequestPermissionsResult）。 */
                        pendFileImg = true; pendFileVid = vid; pendFileMulti = multi;
                        requestPermissions(new String[]{Manifest.permission.CAMERA}, PERM_CAMERA_FILE);
                    } else {
                        startImgChooser(img, vid, multi);
                    }
                    return true;
                } catch (Throwable t) {
                    Log.w(TAG, "file chooser failed: " + t);
                    fileCb = null;
                    fileOpen = false;
                    expectAway = false;
                    return false;
                }
            }
        });

        web.setWebViewClient(new WebViewClient() {
            /* 支付宝付款走 alipays:// 直接唤起 App（拿二维码链接换 startapp），WebView 自己打不开这种 scheme，
               必须交给系统；没装支付宝就提示改用二维码兜底。 */
            private boolean extScheme(String url) {
                if (url == null) return false;
                String u = url.toLowerCase(java.util.Locale.ROOT);
                if (!(u.startsWith("alipays:") || u.startsWith("alipay:") || u.startsWith("weixin:"))) return false;
                try {
                    android.content.Intent i = new android.content.Intent(
                            android.content.Intent.ACTION_VIEW, android.net.Uri.parse(url));
                    i.addFlags(android.content.Intent.FLAG_ACTIVITY_NEW_TASK);
                    startActivity(i);
                } catch (Throwable t) {
                    toast("没能唤起支付宝，请改用二维码付款");
                    js("window.__cbPayFail&&__cbPayFail()");
                }
                return true;
            }

            /* 断网时页面里的任何跳转（按钮、location=…、链接、表单、拉起支付宝）都不让去：
               WebView 一加载就是系统那张「网页无法打开」，还会把完整地址打在屏幕上；
               拉起支付宝也一样，没网只会弹它自己的错误。直接换成我们自己的「网络」页。
               网络恢复后原有的探测/重载流程会把页面接回来。 */
            private boolean blockIfOffline(String url) {
                if (url == null || url.startsWith(OFFLINE_PREFIX)) return false;
                /* 只看真的要出网的动作：about:/data:/javascript:/file: 这些本地导航不需要网络，
                   拦了反而会把正常操作变成「断网」 */
                String u = url.toLowerCase(java.util.Locale.ROOT);
                boolean needsNet = u.startsWith("http://") || u.startsWith("https://")
                        || u.startsWith("alipays:") || u.startsWith("alipay:")
                        || u.startsWith("weixin:");
                if (!needsNet) return false;
                if (netOnline()) return false;
                Log.i(TAG, "断网，页面跳转改走离线页");
                showOffline();
                return true;
            }

            @Override
            public boolean shouldOverrideUrlLoading(WebView view, String url) {
                if (blockIfOffline(url)) return true;
                if (extScheme(url)) return true;
                return !trustedNav(url);   /* 站外页面不加载：桥挂在整个 WebView 上 */
            }

            @Override
            public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest req) {
                String url = req.getUrl() == null ? null : req.getUrl().toString();
                if (blockIfOffline(url)) return true;
                if (extScheme(url)) return true;
                return !trustedNav(url);
            }

            /* ── 私网直连同网闸门 ─────────────────────────────
               开发机上报的「局域网 IP」可能是机房内网(阿里云 VPC=172.19.x):手机永远
               够不着那个网段,但探针在网络路径异常(代理/运营商)下可能误报「通了」,
               页面于是把全部请求切到直连 → 整页半死 → 壳断网页(2026-11-18 aliyun)。
               壳是唯一知道「手机自己网段」的地方:目标私网 IP 与本机任一 IPv4 不在同一
               前两段网段 → 代答 404,探针必败,页面自然走中继;真同一 WiFi 的 192.168.x
               直连不受影响。 */
            private String privHostIp(String url) {
                if (url == null) return null;
                String u = url.toLowerCase(java.util.Locale.ROOT);
                if (!u.startsWith("http://")) return null;   /* 页面是 https 源时本就发不了 http 直连 */
                String rest = u.substring(7);
                int slash = rest.indexOf('/');
                String hostport = slash >= 0 ? rest.substring(0, slash) : rest;
                int colon = hostport.indexOf(':');
                String host = colon >= 0 ? hostport.substring(0, colon) : hostport;
                boolean digits = !host.isEmpty();
                for (int i = 0; i < host.length() && digits; i++) {
                    char c = host.charAt(i);
                    if (!((c >= '0' && c <= '9') || c == '.')) digits = false;
                }
                if (!digits) return null;                    /* 域名不管,只看 IP 字面量 */
                if (host.startsWith("10.") || host.startsWith("192.168.")) return host;
                if (host.startsWith("172.")) {
                    try {
                        int o = Integer.parseInt(host.split("\\.")[1]);
                        if (o >= 16 && o <= 31) return host;
                    } catch (Exception e) { }
                }
                return null;
            }

            /* 本机任一 IPv4 与目标同前两段网段(家用/办公网足够) → 放行 */
            private boolean lanAllowed(String hostIp) {
                String[] b = hostIp.split("\\.");
                try {
                    java.util.Enumeration<java.net.NetworkInterface> nis =
                            java.net.NetworkInterface.getNetworkInterfaces();
                    while (nis != null && nis.hasMoreElements()) {
                        java.util.Enumeration<java.net.InetAddress> ias =
                                nis.nextElement().getInetAddresses();
                        while (ias.hasMoreElements()) {
                            String ip = ias.nextElement().getHostAddress();
                            if (ip == null || ip.indexOf(':') >= 0) continue;
                            String[] a = ip.split("\\.");
                            if (a.length == 4 && b.length == 4
                                    && a[0].equals(b[0]) && a[1].equals(b[1])) return true;
                        }
                    }
                } catch (Exception e) { }
                return false;
            }

            @Override
            public android.webkit.WebResourceResponse shouldInterceptRequest(WebView view, WebResourceRequest req) {
                String host = privHostIp(req.getUrl() == null ? null : req.getUrl().toString());
                if (host != null && !lanAllowed(host)) {
                    Log.i(TAG, "私网直连同网闸门拦截:" + host + "(本机不在该网段)");
                    try {
                        android.webkit.WebResourceResponse r = new android.webkit.WebResourceResponse("text/plain", "utf-8",
                                new java.io.ByteArrayInputStream(new byte[0]));
                        r.setStatusCodeAndReasonPhrase(404, "blocked");
                        return r;
                    } catch (Exception e) { }
                }
                return super.shouldInterceptRequest(view, req);
            }

            @Override
            public void onPageStarted(WebView view, String url, android.graphics.Bitmap fav) {
                pageErr = false;      /* 每次新加载重置"这一趟是不是失败了" */
                if (url != null && !url.startsWith(OFFLINE_PREFIX)) {
                    lastUrl = url;    /* 记住"该打开哪个地址"，重试用它，绝不依赖地址栏/错误页 */
                    offlineShown = false;
                    saveBase(url);
                }
            }

            /* 渲染进程被系统回收（长时间后台最常见）：WebView 从此不再响应任何输入，
               页面停在最后一帧，用户以为「App 卡死」。返回 true 表示我们接管收尾，
               别让系统把整个 App 杀掉；回前台时重建界面（recoverWeb）。 */
            @Override
            public boolean onRenderProcessGone(WebView view, android.webkit.RenderProcessGoneDetail detail) {
                Log.w(TAG, "WebView 渲染进程结束 didCrash=" + (detail != null && detail.didCrash()));
                rendererDead = true;
                return true;
            }

            @Override
            public void onPageFinished(WebView view, String url) {
                /* 我们自己的离线页：它自己管状态，别当成功、也别往里注入 🎤 */
                if (url != null && url.startsWith(OFFLINE_PREFIX)) { return; }

                if (pageErr) {
                    if (isDownloadNav()) { pageErr = false; return; }   /* 下载掐断的导航，不是断网 */
                    /* 加载失败时 WebView 也会走 onPageFinished（那张"网页无法打开"也算页面），
                       绝不能当成成功；立刻换成我们自己的离线页，把自带错误页顶掉。 */
                    netFail = true;
                    showOffline();
                    return;
                }
                netFail = false;
                cancelOfflineSoon();
                retryMs = 3000;
                if (retryRun != null) { ui.removeCallbacks(retryRun); retryRun = null; }
                /* 页面就绪：看门狗从这一刻起算，给 12 秒回第一声 */
                pongAt = android.os.SystemClock.elapsedRealtime();
                /* 上一次是「页面冻死被重建」的：落一行给开发机，下次复现不用猜 */
                try {
                    android.content.SharedPreferences sp = getSharedPreferences(PREFS, MODE_PRIVATE);
                    if (sp.getBoolean(KEY_WDG, false)) {
                        sp.edit().remove(KEY_WDG).apply();
                        js("window.__cbDbg&&window.__cbDbg('Nwdg rebuilt')");
                    }
                } catch (Throwable ignored) { }
                injectShim();
                maybeGuide();
                deliverSid();     /* 冷启动自通知：页面就绪后落到该会话 */
                /* 会话内容页（有 #deck）键盘弹出时才整页上推；
                   登录/注册/列表这些页输入框都在上半屏，一推就把输入框顶出屏幕。
                   判定方式照搬 vibecoding-terminal 的新壳。 */
                view.evaluateJavascript("(!!document.getElementById('deck'))?1:0",
                        new android.webkit.ValueCallback<String>() {
                            @Override public void onReceiveValue(String v) {
                                sessPage = v != null && v.contains("1");
                                Log.i(TAG, "sessPage=" + sessPage);
                            }
                        });
            }

            @Override
            public void onReceivedError(WebView view, WebResourceRequest req, WebResourceError err) {
                if (req != null && req.isForMainFrame()) {
                    /* 点了下载：浏览器/下载器接手后 WebView 会掐断这次导航并报错，
                       那不是断网，别把整页顶成「网络」页。 */
                    if (isDownloadNav()) { Log.i(TAG, "下载触发的导航中断，不当断网"); return; }
                    Log.i(TAG, "加载失败: " + (err == null ? "?" : err.getErrorCode()));
                    pageErr = true;
                    netFail = true;
                    /* 不等 onPageFinished：立刻把系统那张「网页无法打开」换成我们的网络页，
                       免得用户先看到域名/端口/会话 id 再被换走。 */
                    view.post(new Runnable() {
                        @Override public void run() { showOffline(); }
                    });
                }
            }

            /* 老 WebView（API < 23 一档）只会回调这个旧版本；新的两个都回调，靠 pageErr 去重 */
            @Override
            public void onReceivedError(WebView view, int errorCode, String desc, String failingUrl) {
                /* 旧回调在老系统上也会报子资源（图片/接口）失败：只认主文档，
                   否则一张图加载失败就会把人扔到「没网」页 */
                String cur = lastUrl;
                if (failingUrl != null && cur != null
                        && !failingUrl.equals(cur) && !failingUrl.startsWith(cur)) return;
                if (isDownloadNav()) { Log.i(TAG, "下载触发的导航中断，不当断网(旧回调)"); return; }
                Log.i(TAG, "加载失败(旧回调): " + errorCode);
                pageErr = true;
                netFail = true;
                view.post(new Runnable() {
                    @Override public void run() { showOffline(); }
                });
            }

            /* 主文档 5xx：中继/开发机那侧真出事了。也换网络页（那页统一只说「断网」，不再分是谁的网），
               4xx 不动——额度墙、未登录这些是正常页面，不能被顶掉。 */
            @Override
            public void onReceivedHttpError(WebView view, WebResourceRequest req,
                                            android.webkit.WebResourceResponse resp) {
                if (req == null || !req.isForMainFrame()) return;
                int code = resp == null ? 0 : resp.getStatusCode();
                if (code < 500 || isDownloadNav()) return;
                Log.i(TAG, "主文档 HTTP " + code + "，改走离线页");
                pageErr = true;
                netFail = true;
                view.post(new Runnable() {
                    @Override public void run() { showOffline(); }
                });
            }
        });

        /* [codeboss] 页面里的 .apk 链接交给系统下载器（WebView 默认对下载不做任何事） */
        web.setDownloadListener(new android.webkit.DownloadListener() {
            @Override public void onDownloadStart(String url, String ua, String cd, String mime, long len) {
                dlNavT = System.currentTimeMillis();
                /* dmg/exe/tar.gz 这些桌面安装包手机上装不了，交系统浏览器去下。
                   以前不分青红皂白全塞给 enqueueApk：包名写成 codeboss-terminal.apk、
                   下完还弹安装器；而且这次导航被 WebView 掐断会回调 onReceivedError，
                   壳把它当成断网，整页被顶成「网络」页。 */
                if (!isOurApk(url, mime)) {
                    Log.i(TAG, "非 APK 下载交系统浏览器 mime=" + (mime == null ? "?" : mime));
                    openExternal(url);
                    return;
                }
                /* 没网时点下载只会留一个失败的下载任务：直接给网络页，联网后再点 */
                if (!netOnline()) {
                    Log.i(TAG, "断网，下载改为跳离线页: " + (mime == null ? "?" : mime));
                    showOffline();
                    return;
                }
                enqueueApk(url, ua);
            }
        });

        setContentView(web);
        setupImeResize();
        setupNetworkWatch();

        /* 从系统通知点进来：冷启动时页面还没加载，先存着，onPageFinished 再投 */
        if (getIntent() != null && getIntent().getStringExtra("sid") != null) {
            pendingSid = getIntent().getStringExtra("sid");
        }
        String url = getIntent() == null ? null : getIntent().getStringExtra("url");
        SharedPreferences sp = getSharedPreferences(PREFS, MODE_PRIVATE);
        if (url != null && url.length() > 0) {
            sp.edit().putString(KEY_URL, url).apply();
        } else {
            url = sp.getString(KEY_URL, defaultUrl());
        }
        lastUrl = url;
        /* 已配对过：启动再种一次产品线 cookie（清数据/过期兜底），赶在首屏 loadUrl 前。
           首装/刚配对时 base 还是空的，再按这次要打开的地址种一遍——不然第一个请求
           没有 cb_product，host 按自己的线渲染，两个 App 打开一模一样（2026-11-20）。 */
        seedProductCookie(this, getSharedPreferences(PREFS, MODE_PRIVATE).getString("base", ""));
        seedProductCookie(this, originOf(url));
        if (netOnline()) {
            web.loadUrl(withProduct(url));
        } else {
            /* 启动就没网：别让 WebView 先去撞一次「网页无法打开」，直接上网络页 */
            Log.i(TAG, "启动时没有网络，直接进离线页");
            saveBase(url);
            showOffline();
        }

        initTts();
        ui.postDelayed(shimBeat, 2000);
        ui.postDelayed(netWatch, 15000);
        setupApkUpdate();   /* [codeboss] 客户端自更新：启动查一次 + 每 4 小时查一次 */
        setupScreenState(); /* [codeboss] 亮/灭屏用系统广播，熄屏直接停 WebView 定时器 */
        handleInstallIntent(getIntent());   /* 系统装完把我们拉起来时带的状态：先接住，别当成"没回来" */
        checkLastUpdate();  /* 上次更新有没有自己回来：没回来就把小米引导恢复，下次更新前再带一次路 */

        hideSystemUi();
        if ("kiosk".equals(runMode()) && Kiosk.isOwner(this)) {
            try {
                startLockTask();
            } catch (Throwable ignored) {
            }
        }
    }

    /** 更新装完把我们拉起来的那一趟：亮屏 + 盖在锁屏上。
        装完时手机常常已经灭屏/上了锁，界面其实已经被拉到前台，用户看到的却还是黑屏
        ——「没自动打开」有一半是这么来的（系统日志里窗口已经 focused）。只在两条
        更新路径上开（我们自己的拉起、安装状态 intent），普通打开不动：否则锁屏上
        谁拿起来都能看到会话内容。 */
    private boolean updWake = false;        /* 这一趟是「装完回来」，需要亮屏 */
    private boolean updWakeTimed = false;   /* 一分钟的锁屏显示已经排过（别重复排） */

    private void updWakeShow(android.content.Intent in) {
        if (in == null) return;
        boolean upd = in.getBooleanExtra(Relaunch.EXTRA_WAKE, false);
        if (!upd && ACTION_INSTALL.equals(in.getAction())) {
            /* 安装状态 intent 也可能在很久以后被重投一次（系统重试）：只有刚装完才算 */
            long lu = 0;
            try {
                lu = getPackageManager().getPackageInfo(getPackageName(), 0).lastUpdateTime;
            } catch (Throwable ignored) { }
            upd = lu > 0 && System.currentTimeMillis() - lu < 15 * 60 * 1000L;
        }
        if (!upd) return;
        updWake = true;
        wakeNow();
        if (!updWakeTimed) {
            /* 只在这「装完回来」的一分钟里盖着锁屏：之后收回，别让锁着的手机一直亮着会话内容 */
            updWakeTimed = true;
            ui.postDelayed(new Runnable() {
                @Override public void run() {
                    updWake = false;
                    try { setShowWhenLocked(false); } catch (Throwable ignored) { }
                }
            }, 60000);
        }
    }

    /** 真的去点亮屏幕。onCreate/onNewIntent 调用时界面可能还没 resumed（实测第一下
        有时点不亮），所以 onResume 里再补一次——两处都调是刻意的。 */
    private void wakeNow() {
        try {
            if (android.os.Build.VERSION.SDK_INT >= 27) {
                setShowWhenLocked(true);   /* 锁屏界面之上直接显示本页 */
                setTurnScreenOn(true);     /* 灭屏时把屏幕点亮 */
            } else {
                getWindow().addFlags(android.view.WindowManager.LayoutParams.FLAG_SHOW_WHEN_LOCKED
                        | android.view.WindowManager.LayoutParams.FLAG_TURN_SCREEN_ON);
            }
            android.os.PowerManager pm = (android.os.PowerManager) getSystemService(POWER_SERVICE);
            if (pm != null && !pm.isInteractive()) {
                pm.newWakeLock(android.os.PowerManager.SCREEN_BRIGHT_WAKE_LOCK
                                | android.os.PowerManager.ACQUIRE_CAUSES_WAKEUP, "codeboss:upd")
                        .acquire(4000);   /* 4 秒后自动释放；只负责把屏幕点亮 */
            }
            Log.i(TAG, "更新回来：亮屏 + 显示在锁屏上");
        } catch (Throwable t) {
            Log.w(TAG, "更新后亮屏失败: " + t);
        }
    }

    /** 更新装完有没有自己回来。看两个记号：装之前记的版本号、我们主动拉起的时刻。
        版本变了但两分钟内没有「我们拉起的」痕迹 → 这次是用户自己点开的，自动拉起失败：
        把小米那两个开关的一次性引导恢复，下次更新前重新带路（用户不用自己猜）。
        「装完两分半内就进到本页」一律算回来——不管是谁拉起来的：安装状态 intent、
        闹钟、还是用户自己手快点的，都不该再吓唬他。 */
    private void checkLastUpdate() {
        try {
            if (updIntentSeen) return;   /* 已经由安装状态 intent 拉起来了，不算"没回来" */
            final android.content.SharedPreferences sp = getSharedPreferences(PREFS, MODE_PRIVATE);
            final int armed = sp.getInt("upd_arm_code", 0);
            if (armed <= 0) return;
            android.content.pm.PackageInfo pi =
                    getPackageManager().getPackageInfo(getPackageName(), 0);
            int cur = pi.versionCode;
            long ts = sp.getLong("upd_launch_ts", 0);
            long sinceInstall = pi.lastUpdateTime > 0
                    ? System.currentTimeMillis() - pi.lastUpdateTime : Long.MAX_VALUE;
            boolean ours = (ts > 0 && System.currentTimeMillis() - ts < 120000)
                    || sinceInstall < 150000;   /* 刚装完就进来了 = 自动回来的 */
            sp.edit().remove("upd_arm_code").remove("upd_arm_tries").remove("upd_launch_ts").apply();
            if (cur == armed) return;                      /* 安装没做（用户取消），不算失败 */
            if (ours) {
                Log.i(TAG, "更新后自动拉起成功（" + armed + "→" + cur + "，装完 "
                        + (sinceInstall / 1000) + "s）");
                return;
            }
            Log.i(TAG, "上次更新没能自动回到前台（" + armed + "→" + cur + "）");
            sp.edit().putBoolean(KEY_MIUI_HINT, false).apply();
            final String hint = isMiui()
                    ? "上次更新后没能自动打开：请在小米设置里给本应用打开「自启动」和「后台弹出界面」"
                    : "上次更新后没能自动打开：请在系统设置里允许本应用「自启动 / 后台弹出界面」，下次更新就能自己回来";
            ui.postDelayed(new Runnable() {
                @Override public void run() {
                    js("typeof toast==='function'&&toast(" + quote(hint) + ", true)");
                }
            }, 6000);
        } catch (Throwable ignored) { }
    }

    /** 安装会话状态（Activity intent 版本）。历史路径，现在主链路走广播接收器，
        但系统重投/旧版本回调仍可能把人拉到界面：统一交给 onInstallStatus。 */
    private void handleInstallIntent(android.content.Intent in) {
        if (in == null || !ACTION_INSTALL.equals(in.getAction())) return;
        updIntentSeen = true;
        onInstallStatus(in);
    }

    /** 安装会话状态：动态接收器、Activity intent、静态接收器转交（界面还活着）都走这里。
        确认框一律用 PendingIntent 代发（某些 ROM 会拦后台 startActivity）；
        失败/取消要把页面上的「更新 App」按钮复位——否则一直停在「正在打开安装界面…」。 */
    void onInstallStatus(android.content.Intent in) {
        if (in == null) return;
        int st = in.getIntExtra(android.content.pm.PackageInstaller.EXTRA_STATUS, -999);
        String msg = in.getStringExtra(android.content.pm.PackageInstaller.EXTRA_STATUS_MESSAGE);
        Relaunch.trace(this, "收到安装状态 st=" + st);
        Log.i(TAG, "安装状态=" + st + " " + msg + " extras="
                + (in.getExtras() == null ? "-" : in.getExtras().keySet()));
        if (st == android.content.pm.PackageInstaller.STATUS_PENDING_USER_ACTION) {
            if (sessPhase == 2) return;   /* 同一条广播重复到达：确认框已经转过了 */
            ui.removeCallbacks(updWatch);
            sessPhase = 2;
            updPending = true;
            ui.postDelayed(updWatch, 5 * 60 * 1000);   /* 确认框已弹：5 分钟没点完再拉回 */
            android.content.Intent cf = in.getParcelableExtra(android.content.Intent.EXTRA_INTENT);
            if (cf != null) {
                cf.addFlags(android.content.Intent.FLAG_ACTIVITY_NEW_TASK);
                if (forwardConfirm(cf)) jsInstallSt(-1);   /* 页面：请在系统界面确认 */
                else Log.w(TAG, "安装确认框唤起失败");
            }
            return;
        }
        updPending = false;
        sessPhase = 0;
        updGrace = false;
        final boolean wasDirect = directInstall;
        directInstall = false;
        ui.removeCallbacks(updWatch);
        if (st == android.content.pm.PackageInstaller.STATUS_SUCCESS) {
            try {
                getSharedPreferences(PREFS, MODE_PRIVATE).edit()
                        .remove("upd_arm_code").remove("upd_arm_tries").remove("upd_launch_ts")
                        .apply();
            } catch (Throwable ignored) { }
            jsInstallSt(0);
            Log.i(TAG, "更新装完，已自动回到前台 v" + appVerName());
            return;
        }
        /* 失败/取消：按钮复位（否则卡在「正在打开安装界面…」且点不动） */
        jsInstallSt(st);
        /* 会话被系统拒（MIUI 上常见）：回退 ACTION_VIEW，否则永远装不上。
           冷启动时 sessDlId 是 0（内存态丢了），没法回退，只记日志。 */
        if (st > 0 && !sessFailFall) {
            sessFailFall = true;
            if (sessDlId > 0) fallbackViewInstall();
            else if (wasDirect) directFallbackDownload();
        }
    }

    /** 转发系统安装确认框：用 PendingIntent 代发——从后台/浮窗被限的机型上
        startActivity 会被拦，PendingIntent 走的是系统自己的启动身份。 */
    private boolean forwardConfirm(android.content.Intent cf) {
        int fl = android.app.PendingIntent.FLAG_UPDATE_CURRENT;
        if (android.os.Build.VERSION.SDK_INT >= 31) fl |= android.app.PendingIntent.FLAG_MUTABLE;
        try {
            android.app.PendingIntent.getActivity(this, 9301, cf, fl).send();
            return true;
        } catch (Throwable t) {
            Log.w(TAG, "confirm(pi): " + t);
        }
        try { startActivity(cf); return true; }
        catch (Throwable t) { Log.w(TAG, "confirm: " + t); return false; }
    }

    /** 把安装状态告诉页面：-1 确认框已弹 / 0 装成功 / >0 失败或取消 / -2 超时收尾 */
    private void jsInstallSt(final int st) {
        ui.post(new Runnable() { @Override public void run() {
            js("typeof __cbInstallSt==='function'&&__cbInstallSt(" + st + ")");
        }});
    }

    private String appVerName() {
        try {
            return getPackageManager().getPackageInfo(getPackageName(), 0).versionName;
        } catch (Throwable t) { return "?"; }
    }

    /* 中继的「终端」页从 UA 末尾的 CodebossShell/x.y.z 读版本；原生 HttpURLConnection
       默认只发 Dalvik/...（Build/...），所以每个请求都把标记补上。 */
    private String shellUa() {
        return "Dalvik/2.1.0 (Linux; U; Android "
                + android.os.Build.VERSION.RELEASE + "; " + android.os.Build.MODEL + ")"
                + " CodebossShell/" + appVerName();
    }

    private HttpURLConnection conn(String url) throws java.io.IOException {
        HttpURLConnection c = (HttpURLConnection) new URL(url).openConnection();
        c.setRequestProperty("User-Agent", shellUa());
        return c;
    }

    @Override
    protected void onNewIntent(android.content.Intent intent) {
        super.onNewIntent(intent);
        setIntent(intent);
        updWakeShow(intent);
        handleInstallIntent(intent);
        String url = intent == null ? null : intent.getStringExtra("url");
        if (url != null && url.length() > 0) {
            getSharedPreferences(PREFS, MODE_PRIVATE).edit().putString(KEY_URL, url).apply();
            if (web != null) {
                if (netOnline()) {
                    seedProductCookie(this, originOf(url));
                    web.loadUrl(withProduct(url));
                } else {
                    /* 没网时点通知：别去撞「网页无法打开」，直接给网络页 */
                    lastUrl = url;
                    saveBase(url);
                    showOffline();
                }
            }
        }
        /* 通知点开：直接落到那个会话（页面 __cbGoSess 负责切页） */
        String sid = intent == null ? null : intent.getStringExtra("sid");
        if (sid != null && sid.length() > 0) {
            pendingSid = sid;
            deliverSid();
        }
    }

    /** 通知点进来的会话：页面可能在后台/还没加载完，存起来等到能投的时候再投 */
    private void deliverSid() {
        if (pendingSid == null) return;
        ui.postDelayed(new Runnable() {
            @Override public void run() {
                final String sid = pendingSid;
                if (sid == null) return;
                pendingSid = null;
                js("window.__cbGoSess&&window.__cbGoSess(" + quote(sid) + ")");
            }
        }, 400);
    }

    @Override
    protected void onActivityResult(int requestCode, int resultCode, android.content.Intent data) {
        super.onActivityResult(requestCode, resultCode, data);
        if (requestCode == REQ_HOME_ROLE) {
            Log.i(TAG, "home role result=" + resultCode);
            ui.removeCallbacks(awayCap);
            expectAway = false;
            if ("kiosk".equals(runMode())) {
                ui.postDelayed(new Runnable() {
                    @Override public void run() {
                        try { startLockTask(); } catch (Throwable ignored) {}
                    }
                }, 800);
            }
        } else if (requestCode == REQ_SCAN) {
            /* 扫码页回来了：恢复 kiosk 看门狗（启动扫码前置的 expectAway） */
            scanOpen = false;
            ui.removeCallbacks(scanCap);
            expectAway = false;
            Log.i(TAG, "scan result=" + resultCode);
            if (resultCode == RESULT_OK && data != null) {
                String t = data.getStringExtra(ScanActivity.EXTRA_TEXT);
                if (t != null && t.length() > 0) {
                    /* 这里是 onResume 之前：页面还没恢复跑，直接 evaluateJavascript 可能被丢。
                       先存，onResume 之后再投。 */
                    pendingQr = t;
                }
            }
        } else if (requestCode == REQ_FILE) {
            /* 选图器回来了：恢复 kiosk 看门狗（打开时置的 expectAway） */
            fileOpen = false;
            ui.removeCallbacks(fileCap);
            ui.removeCallbacks(awayCap);
            ui.postDelayed(awayCap, 1500);   /* 焦点回来后再恢复，1.5s 免得闪屏 */
            android.net.Uri[] out = null;
            if (resultCode == RESULT_OK && data != null) {
                android.content.ClipData cd = data.getClipData();
                if (cd != null && cd.getItemCount() > 0) {
                    out = new android.net.Uri[cd.getItemCount()];
                    for (int k = 0; k < out.length; k++) out[k] = cd.getItemAt(k).getUri();
                } else if (data.getData() != null) {
                    out = new android.net.Uri[]{ data.getData() };
                }
            }
            /* 拍照：系统相机把照片写进 EXTRA_OUTPUT，返回 data=null——按预给的 URI 回填 */
            if (resultCode == RESULT_OK && out == null && pendingCapture != null) {
                out = new android.net.Uri[]{ pendingCapture };
            }
            pendingCapture = null;
            Log.i(TAG, "file pick result=" + resultCode + " n=" + (out == null ? 0 : out.length));
            if (fileCb != null) {
                try { fileCb.onReceiveValue(out); } catch (Throwable ignored) {}
                fileCb = null;
            }
        }
    }

    /** 选图/拍照 chooser（B1）：相册是主体，拍照作为初始 Intent 挂进去（有权限才挂）。 */
    private void startImgChooser(boolean img, boolean vid, boolean multi) {
        android.content.Intent i = new android.content.Intent(android.content.Intent.ACTION_GET_CONTENT);
        i.addCategory(android.content.Intent.CATEGORY_OPENABLE);
        java.util.ArrayList<String> mimes = new java.util.ArrayList<String>();
        if (img) mimes.add("image/*");
        if (vid) mimes.add("video/*");
        if (mimes.isEmpty()) i.setType("*/*");
        else if (mimes.size() == 1) i.setType(mimes.get(0));
        else {
            i.setType("*/*");
            i.putExtra(android.content.Intent.EXTRA_MIME_TYPES, mimes.toArray(new String[0]));
        }
        if (multi) i.putExtra(android.content.Intent.EXTRA_ALLOW_MULTIPLE, true);
        android.content.Intent chooser =
                android.content.Intent.createChooser(i, img ? "拍照或选图" : "选择截图或视频");
        if (img && checkSelfPermission(Manifest.permission.CAMERA) == PackageManager.PERMISSION_GRANTED) {
            java.io.File cap = new java.io.File(OpenFileProvider.capDir(getCacheDir()),
                    "camera_" + System.currentTimeMillis() + ".jpg");
            pendingCapture = OpenFileProvider.capUri(this, cap.getName());
            android.content.Intent cam = new android.content.Intent(
                    android.provider.MediaStore.ACTION_IMAGE_CAPTURE);
            cam.putExtra(android.provider.MediaStore.EXTRA_OUTPUT, pendingCapture);
            cam.addFlags(android.content.Intent.FLAG_GRANT_WRITE_URI_PERMISSION
                    | android.content.Intent.FLAG_GRANT_READ_URI_PERMISSION);
            chooser.putExtra(android.content.Intent.EXTRA_INITIAL_INTENTS,
                    new android.content.Intent[]{cam});
        }
        startActivityForResult(chooser, REQ_FILE);
    }

    @Override
    public void onRequestPermissionsResult(int requestCode, String[] permissions, int[] grantResults) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults);
        if (requestCode == PERM_CAMERA_FILE) {
            boolean ok = grantResults.length > 0
                    && grantResults[0] == PackageManager.PERMISSION_GRANTED;
            Log.i(TAG, "file camera perm granted=" + ok);
            try { startImgChooser(pendFileImg, pendFileVid, pendFileMulti); }
            catch (Throwable t) {
                Log.w(TAG, "file chooser failed: " + t);
                fileCb = null; fileOpen = false; expectAway = false;
            }
            return;
        }
        if (requestCode == PERM_CAMERA_SCAN) {
            boolean ok = grantResults.length > 0
                    && grantResults[0] == PackageManager.PERMISSION_GRANTED;
            Log.i(TAG, "camera perm granted=" + ok);
            if (ok) openScanner();
            else {
                /* 不自动跳系统设置：kiosk 下会被看门狗顶回来；给页面一句话就够了 */
                js("window.__cbQrDenied&&window.__cbQrDenied()");
            }
        }
    }

    /** 页面「📷 扫码」：kiosk 下必须先让看门狗让路，否则扫码页 250ms 就被拉回来 */
    private void requestScan() {
        expectAway = true;
        try {
            if (checkSelfPermission(Manifest.permission.CAMERA) == PackageManager.PERMISSION_GRANTED) {
                openScanner();
            } else {
                requestPermissions(new String[]{Manifest.permission.CAMERA}, PERM_CAMERA_SCAN);
            }
        } catch (Throwable t) {
            expectAway = false;
            Log.w(TAG, "requestScan 失败: " + t);
        }
    }

    private void openScanner() {
        expectAway = true;
        scanOpen = true;
        /* 兜底：扫码页异常退出（进程被杀等）收不到结果时，3 分钟后恢复看门狗，
           否则 expectAway 永远为真 = kiosk 失守 */
        ui.removeCallbacks(scanCap);
        ui.postDelayed(scanCap, 180000);
        try {
            startActivityForResult(new android.content.Intent(this, ScanActivity.class), REQ_SCAN);
        } catch (Throwable t) {
            expectAway = false;
            scanOpen = false;
            ui.removeCallbacks(scanCap);
            Log.w(TAG, "打开扫码页失败: " + t);
        }
    }

    private final Runnable scanCap = new Runnable() {
        @Override public void run() {
            if (scanOpen) {
                Log.w(TAG, "扫码页超时未返回，恢复 kiosk 看门狗");
                scanOpen = false;
                expectAway = false;
            }
        }
    };

    /** 选图器超时未返回（用户按返回键、或 ROM 吞了回调）：恢复 kiosk 看门狗 */
    private final Runnable fileCap = new Runnable() {
        @Override public void run() {
            if (fileOpen) {
                Log.w(TAG, "选图器超时未返回，恢复 kiosk 看门狗");
                fileOpen = false;
                expectAway = false;
            }
        }
    };

    private void hideSystemUi() {
        View d = getWindow().getDecorView();
        d.setSystemUiVisibility(
                View.SYSTEM_UI_FLAG_IMMERSIVE_STICKY
                        | View.SYSTEM_UI_FLAG_FULLSCREEN
                        | View.SYSTEM_UI_FLAG_HIDE_NAVIGATION
                        | View.SYSTEM_UI_FLAG_LAYOUT_STABLE
                        | View.SYSTEM_UI_FLAG_LAYOUT_HIDE_NAVIGATION
                        | View.SYSTEM_UI_FLAG_LAYOUT_FULLSCREEN);
    }

    /* ── kiosk 走神看门狗：没 Device Owner 时通知栏/系统设置仍能打开，
          但只要本 App 失焦（被盖上）超 5 秒，就把自己的任务拉回前台——
          设置里点什么都待不住。真正的无缝锁定等 set-device-owner 后由 Lock Task 完成。
          让路的三种情况：主动去授权页(expectAway)、更新确认框(updPending)、
          首次麦克风权限弹窗(未授权时看门狗不启动)。 ── */
    private volatile boolean focused = true;
    private boolean expectAway = false;            /* 本 App 主动离开前台（悬浮窗授权页等） */
    private volatile boolean updPending = false;   /* 更新会话进行中（确认框可能在前台） */
    private volatile boolean relockTask = false;   /* 为装更新临时解了锁定，回到前台要重新上锁 */
    private volatile boolean scanOpen = false;     /* 扫码页在前台（它是本 App 的另一个 Activity，看门狗要让路） */
    private volatile boolean fileOpen = false;     /* 系统选图/选视频器在前台（同上：看门狗必须让路，否则一点就弹回） */
    private volatile String pendingQr = null;      /* 扫码结果先存着：结果回调早于 onResume，WebView 还在暂停态 */
    /* ── 自动亮度（App 自己做的）────────────────────────────────────────
       用户不想跳系统页，所以不申请「修改系统设置」：直接读光线传感器调窗口亮度。
       系统那个「自动调节亮度」开关能静默写就顺手写上，写不了也不影响使用。 */
    private android.hardware.SensorManager sensMgr = null;
    private android.hardware.Sensor lightSensor = null;
    private boolean autoBrightOn = false;
    private float smoothLux = -1f;                 /* 指数平滑后的环境光，防抖 */
    private volatile long pendingInstall = 0;      /* 等用户去开「安装未知应用」，回来接着装 */
    private volatile boolean viewInstall = false;  /* ACTION_VIEW 兜底安装中（无回调，焦点回归即收尾） */
    private long sessDlId = 0;                     /* 本次更新轮的下载 id（去重 + 兜底用） */
    private long stagedRetryFor = 0;               /* 这个下载 id 已经因为坏包重下过一次 */
    private boolean miuiSkip = false;              /* 本轮用户已选「继续安装」，别再拿权限提示挡路 */
    private boolean sessFailFall = false;          /* 本轮兜底只发一次 */
    private volatile int sessPhase = 0;            /* 0 无 1 已提交待状态 2 确认框已弹 */
    private boolean updGrace = false;              /* 会话提交后已经宽限过一轮 */
    /* ── 应用内直下安装包（不走 DownloadManager）─────────────────────────
       实测（2026-11 红米/多亲）：DownloadManager 入队后要排 30s 以上才发第一个
       请求，用户看到的就是「准备下载…」一直不动；874KB 的包真正下载只要 3 秒。
       所以改成本进程 HttpURLConnection 直接下、进度实时回显；任何一步失败
       （没网/超时/包校验不过）再退回原来那条系统下载通道，行为不变。 */
    private volatile boolean directDlOn = false;       /* 直下进行中（防重复触发） */
    private volatile String directApkUrl = null;       /* 本轮直下的地址（兜底重下用） */
    private volatile java.io.File directApk = null;    /* 直下并校验通过、等着装的包 */
    private volatile boolean directInstall = false;    /* 本轮装的是直下的包（没有下载 id） */
    private volatile java.io.File pendingApkFile = null;   /* 等用户去开「安装未知应用」，回来接着装 */
    private boolean kioskWatchOn = false;
    private final Runnable kioskWatch = new Runnable() {
        @Override public void run() {
            if (!"kiosk".equals(runMode())) { kioskWatchOn = false; return; }
            /* 让路期只暂缓「拉回前台」，绝不收工：以前这里直接 return，
               去系统设置解了固定后没人再排期 → 用户按 Home 回到桌面就再也回不来
               （只能手动点图标）。现在让路一结束（awayCap/回到前台）立刻接着看。 */
            if (updPending) { ui.postDelayed(this, 1000); return; }
            if (expectAway) { ui.postDelayed(this, 600); return; }
            if (checkSelfPermission(Manifest.permission.RECORD_AUDIO)
                    != PackageManager.PERMISSION_GRANTED) { kioskWatchOn = false; return; }   // 首次授权弹窗期不动
            if (!focused && !hasWindowFocus()) {   /* 双查：MIUI 秒开秒杀的弹窗吞掉回焦回调后，
                focused 会卡在 false——看门狗每 5s 空拉一次，resume 反复重挂压暗罩＝闪屏循环。
                hasWindowFocus() 是实时状态查询，回调丢了也能纠偏 */
                if (viewInstall) {   /* 兜底安装中：安装器在前台时绝不顶掉它（用户要在这点确认） */
                    String tp = topPkg();
                    if (isInstaller(tp)) {
                        ui.postDelayed(this, 2000);
                        return;
                    }
                }
                Log.i(TAG, "kioskWatch: 失焦被盖 → 拉回前台");
                try {
                    android.content.Intent i =
                            new android.content.Intent(MainActivity.this, MainActivity.class);
                    i.addFlags(android.content.Intent.FLAG_ACTIVITY_REORDER_TO_FRONT);
                    startActivity(i);
                } catch (Throwable ignored) { }
            }
            ui.postDelayed(this, 250);   /* kiosk：250ms 一查，上滑/最近任务基本来不及停留 */
        }
    };

    @Override
    public void onWindowFocusChanged(boolean hasFocus) {
        super.onWindowFocusChanged(hasFocus);
        focused = hasFocus;
        if (hasFocus) {
            hideSystemUi();
            awayBackAt = android.os.SystemClock.elapsedRealtime();
            if (awayPill != null) {   /* 从系统页回来了：收起「回到 App」浮钮，让路定时器作废 */
                hideAwayPill();
                ui.removeCallbacks(awayCapNet);
                Log.i(TAG, "系统页结束，浮钮收起");
            }
            /* 扫码页收起前别清让路标志，否则看门狗把扫码页顶掉。
               系统弹窗（选桌面/固定屏幕）刚关时焦点会先回一下再走，延 1.5s 再恢复看门狗。 */
            if (!scanOpen && !fileOpen) { ui.removeCallbacks(awayCap); ui.postDelayed(awayCap, 1500); }
            /* 系统权限框（定位）/设置页回来：设置页刷新网络与版本行 */
            js("typeof vsNetRefresh==='function'&&pageIdx===2&&vsNetRefresh();1");
            if (pendingInstall != 0 && canInstall()) {   /* 用户从授权页回来了 → 接着装 */
                final long pid = pendingInstall; pendingInstall = 0;
                sessDlId = -1;                            /* 绕开「同一下载 id 只装一次」去重 */
                ui.postDelayed(new Runnable() { @Override public void run() { promptInstall(pid); } }, 400);
            }
            if (pendingApkFile != null && canInstall()) {  /* 直下的包：同样从授权页回来接着装 */
                final java.io.File pf = pendingApkFile; pendingApkFile = null;
                ui.postDelayed(new Runnable() { @Override public void run() { promptInstallFile(pf); } }, 400);
            }
            if (relockTask && !updPending && !viewInstall) {   /* 安装让路结束 → 把锁上回去 */
                relockTask = false;
                try { startLockTask(); Log.i(TAG, "安装结束，重新上锁"); } catch (Throwable ignored) { }
            }
            if (viewInstall) {
                /* 让路标志不能靠焦点回调清：installer 起来前我们也会短暂拿回焦点，
                   回调先到 → 标志被清 → 看门狗 5s 后把 installer 顶掉（=用户说的跳回 App）。
                   交给 viewInstallClear 按前台实际包名判定。 */
                ui.removeCallbacks(viewInstallClear);
                ui.postDelayed(viewInstallClear, 3000);
            }
            ui.removeCallbacks(kioskWatch);
            kioskWatchOn = false;
        } else if ("kiosk".equals(runMode()) && !kioskWatchOn) {
            kioskWatchOn = true;
            ui.postDelayed(kioskWatch, 150);
        }
    }

    @Override
    protected void onPause() {
        super.onPause();
        VISIBLE = false;
        /* 切走时还没松手：长按计时和「这次按键归我们管」都作废，
           免得在后台莫名切一次静音 */
        if (volHold != null) { ui.removeCallbacks(volHold); volHold = null; }
        volPressed = false;
        volLong = false;
        /* 自动听写只做亮屏前台（用户已拍板）：切走/黑屏立刻收麦，绝不后台偷听 */
        if (autoRec) {
            autoStop(true, true);
            js("window.__autoListen && window.__autoListen('cancel','pause')");
        }
        if (autoBrightOn) detachLightSensor();   /* 后台不读光线，省电 */
        PAUSED_AT = System.currentTimeMillis();
        /* 告诉页面「用户看不见了」：页面据此全睡（不发请求、不计时长）。
           Android 的 document.hidden 在 WebView.onPause() 之后才变，而 onPause 到
           真正冻结还有 5 秒（webSleeper），这期间页面照样在轮询。 */
        js("window.__cbVisible&&window.__cbVisible(0)");
        /* 进入后台才需要前台服务：页面马上会断开 SSE，由 BgService 接手提醒。
           先 start 再让页面睡，中间不留「两头都没连」的缝。 */
        BgService.startIfEnabled(this);
        /* 黑屏/切走后别立刻冻结：先让页面把 visibilitychange→pushSleep 跑完
           （abort 掉 SSE 长连接），5 秒后还在后台才冻结渲染器。
           立刻 onPause 会连 JS 一起冻住——页面的断连逻辑没机会执行，
           长连接整夜挂着（服务端还在往里推帧），反而费电。 */
        web.removeCallbacks(webSleeper);
        web.postDelayed(webSleeper, 5000);
        removeOverlays();   /* app 模式切走时别把暖色罩罩在别人应用上；kiosk 回来 onResume 会补挂 */
    }

    /** 供页面上的 🎤 按钮调用，和音量键走同一条路 */
    /* ── 网络开关（离线页里用，不跳系统设置）─────────────────────────
       Android 7~9：WifiManager.setWifiEnabled 对第三方 App 有效，真的能开关。
       Android 10+：系统禁止第三方 App 直接开关 WiFi/移动数据，只能拉起系统「网络」面板浮层
                    （Settings.Panel.ACTION_INTERNET_CONNECTIVITY）——它盖在我们自己的界面上，
                    不离开 App，这是平台允许的最小打扰做法。 */
    /* 读「移动数据开关」真实状态。AOSP 的 mobile_data 是总开关，双卡机器上真正生效的是
       mobile_data1 / mobile_data2（实测红米：mobile_data=0 但 mobile_data1=1，面板显示"开"）。
       读 Settings.Global 不需要权限；读不到再退到 TelephonyManager.getDataState()。 */
    private boolean readMobileOn() {
        try {
            android.content.ContentResolver cr = getContentResolver();
            int a  = android.provider.Settings.Global.getInt(cr, "mobile_data", -1);
            int s1 = android.provider.Settings.Global.getInt(cr, "mobile_data1", -1);
            int s2 = android.provider.Settings.Global.getInt(cr, "mobile_data2", -1);
            Log.i(TAG, "mobile_data=" + a + " mobile_data1=" + s1 + " mobile_data2=" + s2);
            if (s1 == 1 || s2 == 1) return true;
            if (a == 1) return true;
            if (a == 0 && s1 != 1 && s2 != 1) return false;
        } catch (Throwable t) {
            Log.w(TAG, "读 mobile_data 失败: " + t);
        }
        try {
            android.telephony.TelephonyManager tm =
                    (android.telephony.TelephonyManager) getSystemService(TELEPHONY_SERVICE);
            return tm != null && tm.getDataState() == android.telephony.TelephonyManager.DATA_CONNECTED;
        } catch (Throwable t) {
            return false;
        }
    }

    private boolean hasFineLoc() {
        try {
            return checkSelfPermission(Manifest.permission.ACCESS_FINE_LOCATION)
                    == PackageManager.PERMISSION_GRANTED;
        } catch (Throwable t) {
            return false;
        }
    }

    private String netStateJson() {
        boolean wifi = false, mobile = false, mobileOn = false, online = false, wifiOn = false;
        String ssid = "";
        try {
            ConnectivityManager cm = (ConnectivityManager) getSystemService(CONNECTIVITY_SERVICE);
            android.net.NetworkInfo ni = cm == null ? null : cm.getActiveNetworkInfo();
            online = ni != null && ni.isConnected();
            android.net.NetworkInfo w = cm == null ? null : cm.getNetworkInfo(ConnectivityManager.TYPE_WIFI);
            android.net.NetworkInfo m = cm == null ? null : cm.getNetworkInfo(ConnectivityManager.TYPE_MOBILE);
            wifi = w != null && w.isConnected();
            mobile = m != null && m.isConnected();
            android.net.wifi.WifiManager wm =
                    (android.net.wifi.WifiManager) getApplicationContext().getSystemService(WIFI_SERVICE);
            if (wm != null) {
                wifiOn = wm.isWifiEnabled();          /* 开关状态（不等于已连上） */
                if (wifi && wm.getConnectionInfo() != null) {
                    String s = wm.getConnectionInfo().getSSID();
                    if (s != null) {
                        s = s.replace("\"", "").trim();
                        /* Android 10+ 没定位权限/定位关着时系统给的就是这个占位串，
                           当成空值处理，页面据此出「读取名称」按钮 */
                        if (s.equalsIgnoreCase("<unknown ssid>") || s.equalsIgnoreCase("unknown ssid")
                                || s.equals("<unknown>") || s.equals("0x")) s = "";
                        ssid = s;
                    }
                }
            }
        } catch (Throwable ignored) {
        }
        mobileOn = readMobileOn();
        try {
            org.json.JSONObject o = new org.json.JSONObject();
            o.put("wifi", wifi);
            o.put("wifiOn", wifiOn);
            o.put("mobile", mobileOn);      /* 开关状态 */
            o.put("mobileConn", mobile);    /* 是否已连上蜂窝网 */
            o.put("panel", netPanelAvail());/* 本机有没有系统「互联网连接」浮层 */
            o.put("online", online);
            o.put("ssid", ssid);
            o.put("gen", netGen);         /* 拓扑代数：变了=换网了（wifi↔流量、断网恢复、换热点都算） */
            o.put("locPerm", hasFineLoc() ? "1" : "0");   /* 读 SSID 的前置条件，页面据此提示授权 */
            return o.toString();
        } catch (Throwable t) {
            return "{\"wifi\":false,\"wifiOn\":false,\"mobile\":false,\"mobileConn\":false,\"panel\":false,\"online\":false,\"ssid\":\"\"}";
        }
    }

    private void pushNetState() {
        js("window.__cbNetState && window.__cbNetState(" + org.json.JSONObject.quote(netStateJson()) + ")");
    }

    private String lastNetSig = "";

    /** 当前「实际上网那条路」的指纹：在线与否 + 走的是哪张网卡。
        同值 = 同一条网，不算换网。 */
    private String netSig() {
        try {
            ConnectivityManager cm = (ConnectivityManager) getSystemService(CONNECTIVITY_SERVICE);
            android.net.NetworkInfo ni = cm == null ? null : cm.getActiveNetworkInfo();
            if (ni == null || !ni.isConnected()) return "off";
            return "on|" + ni.getTypeName();
        } catch (Throwable t) {
            return "?";
        }
    }

    /** 只有上网路径真的变了才把 gen +1。
        ⚠️ 以前 onAvailable 里无条件 netGen++：手机同时有 WiFi 和蜂窝时，
        蜂窝上线/掉线也会触发回调 → gen 变了 → 页面把刚打通的 P2P DataChannel
        当成「换网」拆掉重连（表现为直连每几十秒重建一次）。 */
    private void bumpNetGen() {
        String s = netSig();
        if (s.equals(lastNetSig)) return;
        lastNetSig = s;
        netGen++;
    }

    /** WiFi 开关：只在 App 内做。Android 7~9 有效；10+ 若被系统拒绝就返回 false（不再跳系统设置） */
    private boolean doToggleWifi() {
        try {
            android.net.wifi.WifiManager wm =
                    (android.net.wifi.WifiManager) getApplicationContext().getSystemService(WIFI_SERVICE);
            if (wm == null) return false;
            boolean target = !wm.isWifiEnabled();
            boolean ok = wm.setWifiEnabled(target);
            Log.i(TAG, "setWifiEnabled(" + target + ") -> " + ok + " (targetSdk="
                    + getApplicationInfo().targetSdkVersion + ")");
            return ok;
        } catch (Throwable t) {
            Log.w(TAG, "setWifiEnabled 异常: " + t);
            return false;
        }
    }

    /** 移动数据开关：<10 走反射；>=10 系统禁止，直接 false */
    private boolean doToggleMobile() {
        if (android.os.Build.VERSION.SDK_INT >= 29) {
            Log.i(TAG, "移动数据：Android 10+ 不允许 App 直接开关");
            return false;
        }
        try {
            android.telephony.TelephonyManager tm =
                    (android.telephony.TelephonyManager) getSystemService(TELEPHONY_SERVICE);
            if (tm == null) return false;
            /* 当前状态必须来自 Settings.Global：getDataEnabled() 实测在这类 ROM 上一直返回 false，
               拿它当基准会导致"只能开、关不掉"（每按一次都 setDataEnabled(true)）。 */
            boolean target = !readMobileOn();
            java.lang.reflect.Method s = tm.getClass().getMethod("setDataEnabled", boolean.class);
            s.invoke(tm, target);
            Log.i(TAG, "setDataEnabled(" + target + ") ok");
            return true;
        } catch (Throwable t) {
            Log.w(TAG, "setDataEnabled 失败: " + t);
            return false;
        }
    }

    /* 移动数据开关用不了时（Android 10+ 无公开 API / 厂商拒绝），跳到系统的移动网络开关页。
       优先 INTERNET_CONNECTIVITY 面板：它是浮层，盖在当前界面上，不算彻底离开 App；
       厂商没实现面板才退到设置里的移动数据页（见下面 chain 的顺序注释）。
       这里只对移动数据开放跳转 —— WiFi 仍然全程留在 App 内。 */
    /* 这台机器有没有系统的「互联网连接」浮层面板（Android 10+ 且厂商实现了才算）。
       红米 Note 7 (MIUI/Android 10) 有 → WiFi 和移动数据都能在浮层里设置；
       坚果 Pro (Android 7.1.1) 实测 query-activities 返回 "No activities found" → 没有，
       只能继续用页面内的扫描/密码连接。 */
    private Boolean panelAvail = null;

    private boolean netPanelAvail() {
        if (panelAvail != null) return panelAvail.booleanValue();
        boolean ok = false;
        try {
            if (android.os.Build.VERSION.SDK_INT >= 29) {
                android.content.Intent it = new android.content.Intent(
                        "android.settings.panel.action.INTERNET_CONNECTIVITY");
                java.util.List<android.content.pm.ResolveInfo> li =
                        getPackageManager().queryIntentActivities(it, 0);
                ok = li != null && !li.isEmpty();
            }
        } catch (Throwable t) {
            Log.w(TAG, "探测网络浮层失败: " + t);
        }
        panelAvail = Boolean.valueOf(ok);
        Log.i(TAG, "系统网络浮层可用=" + ok + " (sdk=" + android.os.Build.VERSION.SDK_INT + ")");
        return ok;
    }

    /** 离线页「打开系统网络设置」：浮层优先，没有/打不开就退到 WiFi 设置页。
        ⚠️ 必须走 openSystemAway 设置 expectAway：kiosk 看门狗只认这个标志，
        否则浮层刚弹出来 250ms 就被拉回前台——用户看到的就是「点了没反应」。 */
    private void openNetPanelOnly() {
        Log.i(TAG, "打开系统网络设置 (panel=" + netPanelAvail() + ")");
        openSystemAway(new String[][]{
                {"android.settings.panel.action.INTERNET_CONNECTIVITY", "网络浮层"},
                {"android.settings.WIFI_SETTINGS", "WiFi 设置"},
                {"android.settings.WIRELESS_SETTINGS", "无线和网络"},
                {"android.settings.SETTINGS", "系统设置"}});
    }

    /* ── 去系统页期间的「回到 App」浮钮 ─────────────────────────────
       屏幕固定（非设备所有者的 kiosk）开系统设置必须先解固定，固定一解，
       用户在设置里按 Home / 返回桌面就落到桌面上，看门狗又让着路——
       结果就是「点了 WiFi 就回不来，只能手动点图标」。这里在系统页上面挂一个
       小按钮，点一下立刻回来；让路结束（90s）后看门狗也会自己收回前台。 */
    private android.view.View awayPill = null;
    private long awayBackAt = 0;                  /* 最近一次「从系统页回到前台」的时刻 */
    private final Runnable awayCapNet = new Runnable() {
        @Override public void run() {
            if (!expectAway) return;
            expectAway = false;
            Log.i(TAG, "系统页让路结束，看门狗收回前台");
        }
    };

    private boolean inKiosk() { return "kiosk".equals(runMode()); }

    /** 解固定 + 挂浮钮。设备所有者不用解（settings 在锁任务白名单里），调用方自己保证 */
    private void releaseLockForAway() {
        try {
            android.app.ActivityManager am =
                    (android.app.ActivityManager) getSystemService(ACTIVITY_SERVICE);
            if (am != null && am.getLockTaskModeState()
                    != android.app.ActivityManager.LOCK_TASK_MODE_NONE) {
                stopLockTask();
                if (inKiosk()) { relockTask = true; showAwayPill(); }
                Log.i(TAG, "临时解除固定，去开系统页");
            }
        } catch (Throwable t) {
            Log.w(TAG, "解除固定失败: " + t);
        }
    }

    /** 按顺序开第一个有人接的系统页；返回「是否发出去了」 */
    private boolean startSystemChain(String[][] chain) {
        for (int i = 0; i < chain.length; i++) {
            if (i == 0 && android.os.Build.VERSION.SDK_INT < 29) continue;   // 浮层面板 Android 10+
            try {
                android.content.Intent it = new android.content.Intent(chain[i][0]);
                /* 先确认真的有人接：某些 ROM 的 query-activities 说有面板，
                   真正 startActivity 时却静默不弹（kiosk/设备所有者限制同理）。
                   没人接就换下一条，别在这一条上假成功。 */
                if (it.resolveActivity(getPackageManager()) == null) {
                    Log.i(TAG, "没人接 " + chain[i][1] + "，换下一个");
                    continue;
                }
                it.addFlags(android.content.Intent.FLAG_ACTIVITY_NEW_TASK);
                startActivity(it);
                Log.i(TAG, "系统网络页 → " + chain[i][1] + " [" + chain[i][0] + "]");
                return true;
            } catch (Throwable t) {
                Log.w(TAG, "打不开 " + chain[i][1] + ": " + t);
            }
        }
        return false;
    }

    private void awayFailed() {
        expectAway = false;
        ui.removeCallbacks(awayCapNet);
        hideAwayPill();
        if (relockTask) {   /* 刚才解了固定却没开成任何页面：把锁上回去 */
            relockTask = false;
            try { startLockTask(); } catch (Throwable ignored) { }
        }
        /* 一条都打不开：让离线页把话说出来（别让用户对着「打开」反复点） */
        ui.post(new Runnable() { @Override public void run() {
            js("window.__cbPanelFail&&window.__cbPanelFail()");
        }});
    }

    private void showAwayPill() {
        if (awayPill != null || !inKiosk() || !overlayPerm()) return;
        try {
            android.widget.TextView v = new android.widget.TextView(this);
            v.setText("← 回到口袋工头");
            v.setTextSize(13);
            v.setTextColor(0xFFFFFFFF);
            v.setGravity(android.view.Gravity.CENTER);
            android.graphics.drawable.GradientDrawable g = new android.graphics.drawable.GradientDrawable();
            g.setColor(0xE62EA043);
            g.setCornerRadius(dp(28));
            v.setPadding(dp(16), dp(8), dp(16), dp(8));
            v.setBackground(g);
            v.setElevation(dp(6));
            v.setOnClickListener(new android.view.View.OnClickListener() {
                @Override public void onClick(android.view.View x) { comeBackFromAway(); }
            });
            int type = android.os.Build.VERSION.SDK_INT >= 26
                    ? WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY
                    : WindowManager.LayoutParams.TYPE_PHONE;
            WindowManager.LayoutParams lp = new WindowManager.LayoutParams(
                    WindowManager.LayoutParams.WRAP_CONTENT,
                    WindowManager.LayoutParams.WRAP_CONTENT,
                    type,
                    WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE
                            | WindowManager.LayoutParams.FLAG_NOT_TOUCH_MODAL,
                    android.graphics.PixelFormat.TRANSLUCENT);
            /* 挂屏幕上方：设 WiFi 密码时键盘会盖住下半屏，底部浮钮会挡住空格键；
               上面只压到设置页的标题栏，不碍事 */
            lp.gravity = android.view.Gravity.TOP | android.view.Gravity.CENTER_HORIZONTAL;
            lp.y = dp(34);
            WindowManager wm = (WindowManager) getSystemService(WINDOW_SERVICE);
            wm.addView(v, lp);
            awayPill = v;
            Log.i(TAG, "浮钮已挂");
        } catch (Throwable t) {
            Log.w(TAG, "浮钮挂不上: " + t);
            awayPill = null;
        }
    }

    private void hideAwayPill() {
        if (awayPill == null) return;
        try {
            ((WindowManager) getSystemService(WINDOW_SERVICE)).removeView(awayPill);
        } catch (Throwable ignored) { }
        awayPill = null;
    }

    /** 浮钮被点：立刻结束让路，回到主界面（回到前台后 onWindowFocusChanged 会重新上锁） */
    private void comeBackFromAway() {
        Log.i(TAG, "浮钮：回到主界面");
        ui.removeCallbacks(awayCapNet);
        expectAway = false;
        hideAwayPill();
        if (inKiosk()) relockTask = true;
        try {
            android.content.Intent i =
                    new android.content.Intent(MainActivity.this, MainActivity.class);
            i.addFlags(android.content.Intent.FLAG_ACTIVITY_NEW_TASK
                    | android.content.Intent.FLAG_ACTIVITY_REORDER_TO_FRONT);
            startActivity(i);
        } catch (Throwable t) {
            Log.w(TAG, "回到主界面失败: " + t);
        }
    }

    /** 打开系统页/浮层前先给 kiosk 看门狗让路：否则它把我们拉回前台，浮层当场被顶掉。
        回到前台时 onWindowFocusChanged 结束让路；人一直不回来就 90s 兜底。 */
    private void openSystemAway(final String[][] chain) {
        expectAway = true;
        ui.removeCallbacks(awayCapNet);
        /* 90s：输 WiFi 密码、等连接都够；再久说明人走了，看门狗把界面收回前台 */
        ui.postDelayed(awayCapNet, 90000);
        /* 设备所有者的锁任务白名单里本来就有 com.android.settings：不解固定也能开，
           解了反而会丢掉「回到 App 就自动上锁」的那层保护。非所有者必须解（会被系统吞掉）。 */
        final boolean keepLock = inKiosk() && Kiosk.isOwner(this);
        final long t0 = android.os.SystemClock.elapsedRealtime();
        if (!keepLock) releaseLockForAway();
        boolean launched = startSystemChain(chain);
        if (launched && keepLock) {
            /* 万一锁任务还是把启动吞了（策略没写进去/ROM 特例）：1.2s 后系统页没盖上来
               → 退回老路「解固定 + 再开一次」。 */
            ui.postDelayed(new Runnable() { @Override public void run() {
                if (!expectAway || awayPill != null) return;   /* 已经回来了 / 已经解过 */
                if (awayBackAt > t0) return;                   /* 用户自己已经回到 App */
                if (!hasWindowFocus()) return;                 /* 系统页在前台，正常 */
                Log.w(TAG, "锁任务挡住了系统页 → 解固定后重开");
                releaseLockForAway();
                if (!startSystemChain(chain)) awayFailed();
            }}, 1200);
            return;
        }
        if (!launched) awayFailed();
    }

    private void openMobileDataSettings() {
        /* 顺序按"能否一眼看到移动数据开关"排：
           1) 系统「互联网连接」浮层面板（Android 10+，MIUI 实测可用，盖在 App 上不跳走）
           2) 数据用量页（AOSP 顶部就是移动数据开关）
           3) 无线和网络（老 ROM 里移动网络入口在这里；坚果 Pro 实测可用）
           4) 双卡与移动网络（有些 ROM 直接指向"运营商选择"，放最后兜底） */
        String[][] chain = new String[][] {
                {"android.settings.panel.action.INTERNET_CONNECTIVITY", "网络浮层面板(移动数据+WiFi)"},
                {"android.settings.DATA_USAGE_SETTINGS",                   "数据用量"},
                {"android.settings.WIRELESS_SETTINGS",                     "无线和网络"},
                {"android.settings.NETWORK_OPERATOR_SETTINGS",             "双卡与移动网络"}
        };
        for (int i = 0; i < chain.length; i++) {
            if (i == 0 && android.os.Build.VERSION.SDK_INT < 29) continue;
            try {
                android.content.Intent it = new android.content.Intent(chain[i][0]);
                it.addFlags(android.content.Intent.FLAG_ACTIVITY_NEW_TASK);
                startActivity(it);
                Log.i(TAG, "移动数据 → 打开系统页: " + chain[i][1] + " [" + chain[i][0] + "]");
                return;
            } catch (Throwable t) {
                Log.w(TAG, "打不开 " + chain[i][1] + ": " + t);
            }
        }
        Log.w(TAG, "移动数据 → 四个系统页全部打不开");
    }

    /* ── WiFi 扫描 / 连接（都在 App 内完成）─────────────────────────
       Android 10+ 拿扫描结果需要「定位」权限（系统规定），没有权限就返回空列表。
       Android 10+ 连接用 WifiNetworkSpecifier：系统会在我们的界面上弹一个确认条，
       不切走 App；连上后回调 onAvailable。Android 7~9 走 WifiConfiguration 老接口，静默连接。 */
    private ConnectivityManager.NetworkCallback netReqCb;

    private String buildWifiJson() {
        org.json.JSONArray arr = new org.json.JSONArray();
        try {
            android.net.wifi.WifiManager wm =
                    (android.net.wifi.WifiManager) getApplicationContext().getSystemService(WIFI_SERVICE);
            if (wm == null) return arr.toString();
            java.util.HashSet<String> saved = new java.util.HashSet<String>();
            try {
                java.util.List<android.net.wifi.WifiConfiguration> cfgs = wm.getConfiguredNetworks();
                if (cfgs != null) {
                    for (android.net.wifi.WifiConfiguration c : cfgs) {
                        if (c != null && c.SSID != null) saved.add(c.SSID.replace("\"", ""));
                    }
                }
            } catch (Throwable ignored) {
            }
            java.util.List<android.net.wifi.ScanResult> rs = wm.getScanResults();
            java.util.HashSet<String> seen = new java.util.HashSet<String>();
            if (rs != null) {
                for (android.net.wifi.ScanResult r : rs) {
                    if (r == null || r.SSID == null) continue;
                    String ssid = r.SSID;
                    if (ssid.length() == 0 || seen.contains(ssid)) continue;
                    seen.add(ssid);
                    org.json.JSONObject o = new org.json.JSONObject();
                    o.put("ssid", ssid);
                    o.put("level", android.net.wifi.WifiManager.calculateSignalLevel(r.level, 4));
                    String cap = r.capabilities == null ? "" : r.capabilities;
                    o.put("secured", cap.contains("WPA") || cap.contains("WEP") || cap.contains("SAE") || cap.contains("EAP"));
                    o.put("saved", saved.contains(ssid));
                    arr.put(o);
                }
            }
        } catch (Throwable t) {
            Log.w(TAG, "扫描结果读取失败: " + t);
        }
        return arr.toString();
    }

    private void doScanWifi() {
        try {
            android.net.wifi.WifiManager wm =
                    (android.net.wifi.WifiManager) getApplicationContext().getSystemService(WIFI_SERVICE);
            if (wm == null) return;
            if (!wm.isWifiEnabled()) { wm.setWifiEnabled(true); }
            try { wm.startScan(); } catch (Throwable t) { Log.w(TAG, "startScan: " + t); }
        } catch (Throwable t) {
            Log.w(TAG, "doScanWifi: " + t);
        }
        ui.postDelayed(new Runnable() {
            @Override public void run() {
                String json = buildWifiJson();
                Log.i(TAG, "扫描结果: " + json);
                js("window.__cbWifiList && window.__cbWifiList(" + org.json.JSONObject.quote(json) + ")");
            }
        }, 1800);
    }

    /* ── WiFi 连接：老接口优先，绝不永久卡在"正在连接" ────────────────
       教训（0.9.2）：
       1) 0.9.1 里 Android 10+ 一律走 WifiNetworkSpecifier，而 requestNetwork 需要
          CHANGE_NETWORK_STATE（清单里漏了）→ 抛 SecurityException → 被 catch 只打日志，
          **没有回调 __cbWifiFail**，于是页面永远显示"正在连接"。
       2) MIUI 的 specifier 实现会拉起系统"选择 WLAN"界面 → 用户看到"跳到系统 WiFi 设置"。
       targetSdk=28 时 addNetwork/enableNetwork 这些老接口在 Android 10 上仍然可用（同一套
       targetSdk 兼容判定），而且**没有任何系统界面**，所以先走老接口；只有老接口真的不行
       才退到 specifier（并在 30 秒后强制报失败）。 */
    private int connSeq = 0;

    /** 老接口连接：返回 networkId（>=0 表示已提交），失败返回 -1 */
    private int legacyConnect(String ssid, String psk) {
        try {
            android.net.wifi.WifiManager wm =
                    (android.net.wifi.WifiManager) getApplicationContext().getSystemService(WIFI_SERVICE);
            if (wm == null) return -1;
            if (!wm.isWifiEnabled()) {
                boolean on = wm.setWifiEnabled(true);
                Log.i(TAG, "连接前先开 WiFi -> " + on);
            }
            int id = -1;
            String found = "";
            try {
                java.util.List<android.net.wifi.WifiConfiguration> cfgs = wm.getConfiguredNetworks();
                if (cfgs != null) {
                    for (android.net.wifi.WifiConfiguration c : cfgs) {
                        if (c != null && c.SSID != null && c.SSID.replace("\"", "").equals(ssid)) {
                            id = c.networkId;
                            found = c.SSID;
                            break;
                        }
                    }
                }
                Log.i(TAG, "已保存配置命中=" + found + " id=" + id
                        + " (共 " + (cfgs == null ? -1 : cfgs.size()) + " 条)");
            } catch (Throwable t) {
                Log.w(TAG, "getConfiguredNetworks 失败: " + t);
            }
            if (id < 0) {
                if (psk == null || psk.length() == 0) {
                    Log.w(TAG, "没有已保存配置、也没给密码 → 放弃（避免生成无密码坏配置）");
                    return -1;
                }
                android.net.wifi.WifiConfiguration c = new android.net.wifi.WifiConfiguration();
                c.SSID = "\"" + ssid + "\"";
                c.preSharedKey = "\"" + psk + "\"";
                id = wm.addNetwork(c);
                Log.i(TAG, "addNetwork(" + ssid + ") -> " + id);
            } else {
                Log.i(TAG, "复用已保存配置 id=" + id + "（不新建，避免顶掉原密码）");
            }
            if (id < 0) return -1;
            /* disconnect() 在 MIUI 上是慢调用（实测卡 11 秒，放 UI 线程整页僵住）。
               已经连着这条就不必断；要断也在后台线程做（调用方已在后台线程）。 */
            String curSsid = "";
            try {
                android.net.wifi.WifiInfo wi = wm.getConnectionInfo();
                if (wi != null && wi.getSSID() != null) curSsid = wi.getSSID().replace("\"", "");
            } catch (Throwable ignored) { }
            boolean dis = false;
            if (!curSsid.equals(ssid)) dis = wm.disconnect();
            boolean en = wm.enableNetwork(id, true);
            boolean rc = wm.reconnect();
            try { wm.saveConfiguration(); } catch (Throwable ignored) { }
            Log.i(TAG, "current=" + curSsid + " disconnect=" + dis
                    + " enableNetwork(" + id + ")=" + en + " reconnect=" + rc);
            return en ? id : -1;
        } catch (Throwable t) {
            Log.w(TAG, "老接口连接失败: " + t);
            return -1;
        }
    }

    /** 后台轮询直到连上或超时（老接口没有回调，只能问 WifiManager）。
        必须在后台线程跑：WifiManager 这些调用在 MIUI 上一次能阻塞几百毫秒到十几秒。 */
    private void pollLoop(final String ssid, final int seq) {
        for (int t = 0; t <= 15; t++) {
            if (seq != connSeq) return;                   /* 已被新的连接请求取代 */
            String cur = "";
            boolean assoc = false;
            try {
                android.net.wifi.WifiManager wm =
                        (android.net.wifi.WifiManager) getApplicationContext().getSystemService(WIFI_SERVICE);
                android.net.wifi.WifiInfo wi = wm == null ? null : wm.getConnectionInfo();
                if (wi != null) {
                    if (wi.getSSID() != null) cur = wi.getSSID().replace("\"", "");
                    /* 坑：MIUI 在"只是选中了这条配置、还在握手"时就已让 getSSID() 返回目标 SSID，
                       只比 SSID 会误报成功（实测错密码也是 2 秒内报"已连上"）。
                       必须 supplicant 握手完成 + 系统认为 WiFi 已连接。 */
                    if (wi.getSupplicantState() == android.net.wifi.SupplicantState.COMPLETED) assoc = true;
                }
                ConnectivityManager cm2 = (ConnectivityManager) getSystemService(CONNECTIVITY_SERVICE);
                android.net.NetworkInfo ni2 = cm2 == null ? null : cm2.getNetworkInfo(ConnectivityManager.TYPE_WIFI);
                if (ni2 == null || !ni2.isConnected()) assoc = false;
            } catch (Throwable e) {
                Log.w(TAG, "读取关联状态失败: " + e);
            }
            if (cur.equals(ssid) && assoc) {
                Log.i(TAG, "WiFi 已连上(" + t + "s): " + ssid);
                ui.post(new Runnable() {
                    @Override public void run() {
                        pushNetState();
                        js("window.__cbWifiJoined && window.__cbWifiJoined("
                                + org.json.JSONObject.quote(ssid) + ")");
                    }
                });
                return;
            }
            try { Thread.sleep(1000); } catch (InterruptedException e) { return; }
        }
        Log.w(TAG, "WiFi 连接超时(15s): " + ssid);
        ui.post(new Runnable() {
            @Override public void run() {
                js("window.__cbWifiFail && window.__cbWifiFail()");
                pushNetState();
            }
        });
    }

    private void doConnectWifi(final String ssid, final String psk) {
        if (ssid == null || ssid.length() == 0) return;
        final int seq = ++connSeq;
        Log.i(TAG, "连接 WiFi: " + ssid + " (secure=" + (psk != null && psk.length() > 0)
                + ", sdk=" + android.os.Build.VERSION.SDK_INT + ", targetSdk="
                + getApplicationInfo().targetSdkVersion + ")");
        /* 全部放后台线程：WifiManager 的 addNetwork/enableNetwork/disconnect 在 MIUI 上会阻塞数秒 */
        new Thread(new Runnable() {
            @Override public void run() {
                int id = legacyConnect(ssid, psk);
                if (id >= 0) { pollLoop(ssid, seq); return; }
                if (android.os.Build.VERSION.SDK_INT >= 29) {
                    Log.i(TAG, "老接口不可用 → 退到 WifiNetworkSpecifier（会有系统确认界面）");
                    ui.post(new Runnable() {
                        @Override public void run() { specifierConnect(ssid, psk, seq); }
                    });
                    return;
                }
                Log.w(TAG, "连接失败：没有可用通路");
                ui.post(new Runnable() {
                    @Override public void run() { js("window.__cbWifiFail && window.__cbWifiFail()"); }
                });
            }
        }, "cb-wifi-connect").start();
    }

    /** 最后兜底：Android 10+ 的 app-scoped 网络（系统会弹确认/选择界面） */
    private void specifierConnect(final String ssid, final String psk, final int seq) {
        try {
            android.net.wifi.WifiNetworkSpecifier.Builder b =
                    new android.net.wifi.WifiNetworkSpecifier.Builder().setSsid(ssid);
            if (psk != null && psk.length() >= 8) { b.setWpa2Passphrase(psk); }
            NetworkRequest req = new NetworkRequest.Builder()
                    .addTransportType(NetworkCapabilities.TRANSPORT_WIFI)
                    .setNetworkSpecifier(b.build())
                    .build();
            final ConnectivityManager cm = (ConnectivityManager) getSystemService(CONNECTIVITY_SERVICE);
            if (cm == null) { js("window.__cbWifiFail && window.__cbWifiFail()"); return; }
            if (netReqCb != null) {
                try { cm.unregisterNetworkCallback(netReqCb); } catch (Throwable ignored) { }
            }
            final boolean[] done = new boolean[] { false };
            netReqCb = new ConnectivityManager.NetworkCallback() {
                @Override public void onAvailable(Network n) {
                    if (seq != connSeq) { return; }
                    done[0] = true;
                    Log.i(TAG, "WiFi(specifier) 已连上: " + ssid);
                    ui.post(new Runnable() {
                        @Override public void run() {
                            pushNetState();
                            js("window.__cbWifiJoined && window.__cbWifiJoined("
                                    + org.json.JSONObject.quote(ssid) + ")");
                        }
                    });
                }
                @Override public void onUnavailable() {
                    if (seq != connSeq) { return; }
                    done[0] = true;
                    Log.w(TAG, "WiFi(specifier) 失败/被取消: " + ssid);
                    ui.post(new Runnable() {
                        @Override public void run() {
                            js("window.__cbWifiFail && window.__cbWifiFail()");
                        }
                    });
                }
            };
            cm.requestNetwork(req, netReqCb);
            /* specifier 可能既不回调 available 也不回调 unavailable，加个硬超时 */
            ui.postDelayed(new Runnable() {
                @Override public void run() {
                    if (seq != connSeq || done[0]) return;
                    Log.w(TAG, "specifier 15s 无结果 → 判失败");
                    js("window.__cbWifiFail && window.__cbWifiFail()");
                }
            }, 15000);
        } catch (Throwable t) {
            Log.w(TAG, "specifier 连接失败: " + t);
            js("window.__cbWifiFail && window.__cbWifiFail()");
        }
    }

    /** 删掉一条 WiFi 配置（清理调试时误存的条目） */
    private void doForgetWifi(int id) {
        try {
            android.net.wifi.WifiManager wm =
                    (android.net.wifi.WifiManager) getApplicationContext().getSystemService(WIFI_SERVICE);
            if (wm == null) return;
            boolean ok = wm.removeNetwork(id);
            wm.saveConfiguration();
            Log.i(TAG, "removeNetwork(" + id + ") -> " + ok);
        } catch (Throwable t) {
            Log.w(TAG, "removeNetwork 失败: " + t);
        }
    }

    private void askLocation() {
        try {
            if (checkSelfPermission(Manifest.permission.ACCESS_FINE_LOCATION) != PackageManager.PERMISSION_GRANTED) {
                requestPermissions(new String[]{
                        Manifest.permission.ACCESS_FINE_LOCATION,
                        Manifest.permission.ACCESS_COARSE_LOCATION}, 2);
            }
        } catch (Throwable t) {
            Log.w(TAG, "申请定位权限失败: " + t);
        }
    }

    /* ── 自动亮度：光线传感器 → 窗口亮度 ────────────────────────────────
       不用系统的「自动调节亮度」开关，也就不需要「修改系统设置」权限，
       用户在设置页点一下就生效，不会跳系统页。 */
    private final android.hardware.SensorEventListener lightListener =
            new android.hardware.SensorEventListener() {
        @Override public void onSensorChanged(android.hardware.SensorEvent e) {
            float lux = e.values[0];
            smoothLux = (smoothLux < 0f) ? lux : (smoothLux * 0.72f + lux * 0.28f);
            applyAutoBright();
        }
        @Override public void onAccuracyChanged(android.hardware.Sensor s, int a) {}
    };

    /** 亮度曲线：对数映射（人眼感知和环境光的跨度都是对数级的）。
        1 lux≈4% · 10≈23% · 100≈42% · 1000≈61% · 10000≈80% · 50000≈93% */
    private void applyAutoBright() {
        float lux = smoothLux < 0f ? 200f : smoothLux;
        double v = 0.04 + 0.19 * Math.log10(Math.max(1.0, lux));
        final float b = (float) Math.min(1.0, Math.max(0.04, v));
        runOnUiThread(new Runnable() {
            @Override public void run() {
                WindowManager.LayoutParams lp = getWindow().getAttributes();
                lp.screenBrightness = b;
                getWindow().setAttributes(lp);
            }
        });
    }

    private void attachLightSensor() {
        try {
            if (sensMgr == null)
                sensMgr = (android.hardware.SensorManager) getSystemService(
                        android.content.Context.SENSOR_SERVICE);
            if (lightSensor == null && sensMgr != null)
                lightSensor = sensMgr.getDefaultSensor(android.hardware.Sensor.TYPE_LIGHT);
            if (sensMgr != null && lightSensor != null) {
                sensMgr.registerListener(lightListener, lightSensor,
                        android.hardware.SensorManager.SENSOR_DELAY_NORMAL);
                applyAutoBright();          /* 别等传感器第一拍：先用上一次的环境光设一次 */
                return;
            }
        } catch (Throwable t) { Log.w(TAG, "light sensor: " + t); }
        /* 没有光线传感器（少数机型）：退回「跟随系统」，窗口不覆盖 */
        runOnUiThread(new Runnable() {
            @Override public void run() {
                WindowManager.LayoutParams lp = getWindow().getAttributes();
                lp.screenBrightness = WindowManager.LayoutParams.BRIGHTNESS_OVERRIDE_NONE;
                getWindow().setAttributes(lp);
            }
        });
    }

    private void detachLightSensor() {
        try { if (sensMgr != null) sensMgr.unregisterListener(lightListener); } catch (Throwable t) {}
    }

    /** 页面切到 / 离开「自动」档时调用（Bridge.setAutoBright 转发过来） */
    void setAutoBrightSensor(final boolean on) {
        runOnUiThread(new Runnable() {
            @Override public void run() {
                if (on == autoBrightOn) { if (on) applyAutoBright(); return; }
                autoBrightOn = on;
                if (on) {
                    attachLightSensor();
                    /* 已经授权过「修改系统设置」就顺手把系统那个开关也打开：
                       用户在 App 里设一次，系统设置里也一致，全程不弹页 */
                    try {
                        if (android.provider.Settings.System.canWrite(MainActivity.this)
                                && android.provider.Settings.System.getInt(getContentResolver(),
                                        android.provider.Settings.System.SCREEN_BRIGHTNESS_MODE)
                                != android.provider.Settings.System.SCREEN_BRIGHTNESS_MODE_AUTOMATIC) {
                            android.provider.Settings.System.putInt(getContentResolver(),
                                    android.provider.Settings.System.SCREEN_BRIGHTNESS_MODE,
                                    android.provider.Settings.System.SCREEN_BRIGHTNESS_MODE_AUTOMATIC);
                            Log.i(TAG, "autoBright: 已同步打开系统自动亮度");
                        }
                    } catch (Throwable t) { Log.w(TAG, "syncSystemAutoBright: " + t); }
                } else {
                    detachLightSensor();
                    smoothLux = -1f;
                }
            }
        });
    }

    /** 后台提醒服务要的 base：页面可能停在 /ext/<sid>，只留协议+主机+端口 */
    /** 当前服务端 origin（协议+主机+端口）。离线页「开发机列表」用它拼 /?product=… */
    private String origin() {
        try {
            java.net.URL u = new java.net.URL(lastUrl);
            int port = u.getPort();
            return u.getProtocol() + "://" + u.getHost() + (port > 0 ? ":" + port : "");
        } catch (Throwable ignored) {}
        String b = getSharedPreferences(PREFS, MODE_PRIVATE).getString("base", "");
        return (b == null || b.isEmpty()) ? UPD_BASE : b;
    }

    /** URL 的 origin（协议+主机+端口）；解析不了返回空串。 */
    private static String originOf(String url) {
        try {
            java.net.URL u = new java.net.URL(url);
            int port = u.getPort();
            return u.getProtocol() + "://" + u.getHost() + (port > 0 ? ":" + port : "");
        } catch (Throwable ignored) {}
        return "";
    }

    /** 首屏 URL 带上产品线（2026-11-20）：种 cookie 和 WebView 发第一个请求之间
        有个窗口——装机第一次打开 / 刚配对换地址时第一个请求没有 cb_product，
        host 只能按自己的线渲染，两个 App 打开一模一样的页面，页头图标都是同一条线的。
        已经带 product= 的不重复拼；离线页不碰。 */
    private String withProduct(String url) {
        try {
            if (url == null || url.isEmpty() || url.startsWith(OFFLINE_PREFIX)) return url;
            if (url.matches(".*[?&]product=.*")) return url;
            int hash = url.indexOf('#');
            String head = hash >= 0 ? url.substring(0, hash) : url;
            String tail = hash >= 0 ? url.substring(hash) : "";
            return head + (head.indexOf('?') >= 0 ? "&" : "?") + "product=" + product() + tail;
        } catch (Throwable ignored) {
            return url;
        }
    }

    private void saveBase(String url) {
        try {
            String base = originOf(url);
            if (base.isEmpty()) return;
            getSharedPreferences(PREFS, MODE_PRIVATE).edit().putString("base", base).apply();
            seedProductCookie(this, base);
        } catch (Throwable ignored) {}
    }

    /** 产品线 cookie：host 按它做能力面过滤（工头只见 claude/codex，老板全量）。
        产品名来自资源（products/<线>/product.json 同源），双包各取各的；
        必须在首屏 loadUrl 前种好，页面所有请求自带。 */
    static void seedProductCookie(android.content.Context ctx, String base) {
        if (ctx == null || base == null || base.isEmpty()) return;
        try {
            android.webkit.CookieManager ck = android.webkit.CookieManager.getInstance();
            ck.setAcceptCookie(true);
            ck.setCookie(base, "cb_product=" + ctx.getString(R.string.cb_product)
                    + "; Max-Age=31536000; Path=/");
            ck.flush();
        } catch (Throwable ignored) {}
    }

    private class Bridge {
        /** 输入框里长按要出系统「粘贴/全选」菜单：页面在 touchstart 时告诉我们这一下
            落在可编辑控件里，才把长按放开；其余地方照旧吞掉（防 URL 泄露）。 */
        @JavascriptInterface
        public void setLongPress(final boolean on) {
            runOnUiThread(new Runnable() {
                @Override public void run() {
                    web.setLongClickable(on);
                    web.setOnLongClickListener(on ? null : new View.OnLongClickListener() {
                        @Override public boolean onLongClick(View v) { return true; }
                    });
                }
            });
        }

        /** 设置页「扫码配对」：开原生扫码页（页面是 http，getUserMedia 用不了） */
        /** 页面取来的设备提醒凭据（90 天，/api/bg_ticket）：壳读不到 WebView 的
            cookie，黑屏时页面又被冻结——后台提醒流和播报音频靠它认人。 */
        @JavascriptInterface
        public void bgToken(final String tok) {
            BgService.setBgToken(MainActivity.this, tok);
        }

        @JavascriptInterface
        public void scanQr() {
            Log.i(TAG, "bridge.scanQr");
            runOnUiThread(new Runnable() {
                @Override public void run() { requestScan(); }
            });
        }

        /** 焦点换到另一个输入框（键盘已经开着时不会再触发一次布局）：
            让原生重新量一遍缺口，见 applyImeResize 的非会话页分支。 */
        @JavascriptInterface
        public void ime() {
            runOnUiThread(new Runnable() {
                @Override public void run() {
                    if (web != null) applyImeResize(web.getRootView());
                }
            });
        }

        /** 离线页「返回上一台」：切到一台不可达的开发机被拦进网络页时，人还回得去
            （切台是 location 跳转，历史栈里就是原来那台开发机的页面）。 */
        @JavascriptInterface
        public void back() {
            Log.i(TAG, "bridge.back");
            runOnUiThread(new Runnable() {
                @Override public void run() {
                    if (web.canGoBack()) {
                        netFail = false;
                        offlineShown = false;
                        web.goBack();
                    }
                }
            });
        }

        /** 离线页「开发机列表」：切到一台连不上的开发机被拦进网络页时，直接回中继的
            会话列表（/ 会选在线的那台跳 /d，没有在线的就出设备列表），换台继续干活。
            goBack() 只能沿 history 退一格——退回去还是那台坏了的开发机页面，
            所以这里必须重新载入，不走历史栈。 */
        @JavascriptInterface
        public void home() {
            Log.i(TAG, "bridge.home");
            runOnUiThread(new Runnable() {
                @Override public void run() {
                    if (!netOnline()) {
                        Log.i(TAG, "bridge.home：手机没网");
                        pushNetState();
                        js("window.__cbHomeOffline&&window.__cbHomeOffline()");
                        return;
                    }
                    final String u = origin() + "/?product=" + product();
                    retryMs = 3000;
                    /* 挂着的那次探测只会去重载旧地址，先停掉（新地址加载失败会重新排） */
                    if (retryRun != null) { ui.removeCallbacks(retryRun); retryRun = null; }
                    netFail = false;
                    offlineShown = false;
                    lastUrl = u;
                    try { if (web != null) web.loadUrl(u); } catch (Throwable ignored) { }
                }
            });
        }

        /** 离线页「重试」按钮 */
        @JavascriptInterface
        public void retry() {
            Log.i(TAG, "bridge.retry");
            runOnUiThread(new Runnable() {
                @Override public void run() {
                    retryMs = 3000;
                    if (retryRun != null) { ui.removeCallbacks(retryRun); retryRun = null; }
                    if (!netOnline()) {
                        /* 手机自己都没网：别去撞一次注定失败的加载（会闪一下空白），
                           只把状态推给离线页、继续退避探测 */
                        Log.i(TAG, "bridge.retry：仍然没有网络");
                        pushNetState();
                        js("window.__cbRetryOffline&&window.__cbRetryOffline()");
                        scheduleReload();
                        return;
                    }
                    netFail = false;
                    offlineShown = false;
                    try { if (web != null) web.loadUrl(lastUrl); } catch (Throwable ignored) { }
                }
            });
        }

        /** 页面主动请求切到离线页：接口连续失败（断网/开发机没响应）时用，
            免得用户对着一堆点不动的按钮，或者撞上系统那张「网页无法打开」。 */
        @JavascriptInterface
        public void offline() {
            Log.i(TAG, "bridge.offline");
            runOnUiThread(new Runnable() {
                @Override public void run() { showOffline(); }
            });
        }

        /** 页面请求用系统浏览器打开 http(s) 地址（下载开发机客户端、官网这类
            WebView 干不了的活；只认 http/https，别的 scheme 一律不理） */
        @JavascriptInterface
        public void openUrl(final String url) {
            Log.i(TAG, "bridge.openUrl");
            runOnUiThread(new Runnable() {
                @Override public void run() { openExternal(url); }
            });
        }

        /** 登录页「使用自建服务器」入口：把壳切到自建中继并整页重载。
            桥挂在整个 WebView 上——任何页面都能调，所以只收 http(s)
            （javascript:/content: 一类一律不理），且必须过原生确认框：
            静默切服等于劫持，这条不许省。 */
        @JavascriptInterface
        public void setBase(final String url) {
            Log.i(TAG, "bridge.setBase");
            if (url == null) return;
            String t = url.trim();
            while (t.endsWith("/")) t = t.substring(0, t.length() - 1);
            final String u = t;
            if (!u.startsWith("http://") && !u.startsWith("https://")) return;
            runOnUiThread(new Runnable() {
                @Override public void run() {
                    new android.app.AlertDialog.Builder(MainActivity.this)
                        .setTitle("切换服务器")
                        .setMessage("切换到\n" + u + "\n\n注册和账号都在新服务器上，与本机现有账号不通用。")
                        .setPositiveButton("切换", new android.content.DialogInterface.OnClickListener() {
                            @Override public void onClick(android.content.DialogInterface d, int w) {
                                getSharedPreferences(PREFS, MODE_PRIVATE).edit().putString(KEY_URL, u).apply();
                                saveBase(u);
                                lastUrl = u;
                                if (web != null) web.loadUrl(withProduct(u));
                            }
                        })
                        .setNegativeButton("取消", null)
                        .show();
                }
            });
        }

        /** 离线页读网络状态（同步返回 JSON 字符串） */
        @JavascriptInterface
        public String netState() {
            return netStateJson();
        }

        /** 更新自愈的现场记录（Relaunch.trace 攒下的）：页面开机打给开发机日志，
            排查「装完不会自己回来」到底是闹钟没响、广播没到还是拉起来被拦。 */
        @JavascriptInterface
        public String updTrace() {
            return Relaunch.traceText(MainActivity.this);
        }

        /** 剪贴板文本（同步返回）。终端里长按 → 「粘贴」用：
            WebView 里 navigator.clipboard.readText() 读不到（clipboard-read 权限
            默认不给，页面多半还是 http 明文、不是安全上下文），走原生
            ClipboardManager 最稳：不用授权条，也不用用户再点一次系统菜单。 */
        @JavascriptInterface
        public String clipboard() {
            try {
                android.content.ClipboardManager cm =
                        (android.content.ClipboardManager) getSystemService(CLIPBOARD_SERVICE);
                if (cm == null || !cm.hasPrimaryClip()) return "";
                android.content.ClipData cd = cm.getPrimaryClip();
                if (cd == null || cd.getItemCount() == 0) return "";
                android.content.ClipData.Item it = cd.getItemAt(0);
                if (it == null) return "";
                CharSequence cs = it.coerceToText(MainActivity.this);
                return cs == null ? "" : cs.toString();
            } catch (Throwable t) {
                return "";
            }
        }

        /** 产物用系统应用打开（批次一）：host 的 /api/ext_artifact 下到 cache，再经
            OpenFileProvider 以 content:// 递给 ACTION_VIEW（targetSdk≥24 禁 file://）。
            sid/path 由页面传（会话 cwd 内的相对路径）。 */
        @JavascriptInterface
        public void openWith(final String sid, final String path) {
            new Thread(new Runnable() {
                @Override public void run() {
                    try {
                        String base = baseUrl();
                        String url = base + "/api/ext_artifact?sid=" + java.net.URLEncoder.encode(sid, "UTF-8")
                                + "&path=" + java.net.URLEncoder.encode(path, "UTF-8");
                        String ck = cookieOnUiThread(url);
                        java.io.File dir = OpenFileProvider.dir(getCacheDir());
                        dir.mkdirs();
                        String name = path;
                        int slash = name.lastIndexOf('/');
                        if (slash >= 0) name = name.substring(slash + 1);
                        name = name.replaceAll("[^A-Za-z0-9._\\-\\u4e00-\\u9fa5]", "_");
                        if (name.isEmpty() || name.startsWith(".")) name = "file_" + System.currentTimeMillis();
                        java.io.File dst = new File(dir, name);
                        java.net.HttpURLConnection c = (java.net.HttpURLConnection) new java.net.URL(url).openConnection();
                        c.setConnectTimeout(8000);
                        c.setReadTimeout(20000);
                        if (ck != null && !ck.isEmpty()) c.setRequestProperty("Cookie", ck);
                        java.io.InputStream in = c.getInputStream();
                        java.io.FileOutputStream fo = new java.io.FileOutputStream(dst);
                        byte[] buf = new byte[16384];
                        int n;
                        while ((n = in.read(buf)) > 0) fo.write(buf, 0, n);
                        fo.close();
                        in.close();
                        final java.io.File f = dst;
                        ui.post(new Runnable() {
                            @Override public void run() {
                                try {
                                    android.net.Uri uri = android.net.Uri.parse("content://" + OpenFileProvider.AUTHORITY + "/" + java.net.URLEncoder.encode(f.getName(), "UTF-8"));
                    String mime = getTypeFor(f.getName());
                                    android.content.Intent i = new android.content.Intent(android.content.Intent.ACTION_VIEW);
                                    i.setDataAndType(uri, mime);
                                    i.addFlags(android.content.Intent.FLAG_GRANT_READ_URI_PERMISSION);
                                    startActivity(android.content.Intent.createChooser(i, "用其他应用打开"));
                                } catch (Throwable t) {
                                    toast("手机上没有能打开这种文件的应用");
                                }
                            }
                        });
                    } catch (Throwable t) {
                        ui.post(new Runnable() {
                            @Override public void run() { toast("下载失败，无法打开"); }
                        });
                    }
                }
            }, "openwith").start();
        }

        private String getTypeFor(String name) {
            String ext = android.webkit.MimeTypeMap.getFileExtensionFromUrl(name.toLowerCase(java.util.Locale.ROOT));
            String m = (ext == null || ext.isEmpty()) ? null
                    : android.webkit.MimeTypeMap.getSingleton().getMimeTypeFromExtension(ext);
            return m == null ? "application/octet-stream" : m;
        }

        /** 事件提醒 → 系统通知。页面只在「没在看这个会话」时调（见 ext.html onNotify）。
            点通知 → MainActivity 带 sid 回来 → 页面 __cbGoSess 切到该会话。
            旧页面（4 参数）走这个。 */
        @JavascriptInterface
        public void notify(final String sid, final String title, final String body, final String path) {
            Log.i(TAG, "bridge.notify " + sid);
            runOnUiThread(new Runnable() {
                @Override public void run() { sysNotify(sid, title, body, null, false); }
            });
        }

        /** 新版：按类型选提醒音（sound=done/alert/ping），voice=后台语音播报，
            silent=前台页面已经出过声、通知别再响一遍。 */
        @JavascriptInterface
        public void notify2(final String sid, final String title, final String body, final String path,
                            final String sound, final boolean voice, final boolean silent) {
            Log.i(TAG, "bridge.notify2 " + sid + " sound=" + sound + " voice=" + voice + " silent=" + silent);
            runOnUiThread(new Runnable() {
                @Override public void run() { sysNotify(sid, title, body, sound, silent); }
            });
            if (voice) speakText(body);
        }

        /** 页面收到过的提醒 id：后台服务重连补发时跳过这些，避免和页面重复提醒 */
        @JavascriptInterface
        public void seenNotify(int id) {
            try {
                SharedPreferences sp = getSharedPreferences(PREFS, MODE_PRIVATE);
                if (id > sp.getLong("bg_seen_id", 0))
                    sp.edit().putLong("bg_seen_id", id).apply();
            } catch (Throwable ignored) {}
        }

        /** 后台服务已经「真的响过」的提醒 id：页面重连补发时跳过这些，别响两遍 */
        @JavascriptInterface
        public long bgAnnounced() {
            try {
                return getSharedPreferences(PREFS, MODE_PRIVATE)
                        .getLong(BgService.KEY_ANNOUNCED, -1);
            } catch (Throwable t) { return -1; }
        }

        /** 静音/免打扰 = 整机静音键：页面改完开关立刻切系统铃声模式（App 退出不恢复，
            这正是要的）。30 秒兜底轮询在 BgService——黑屏页面冻住时时段进出也能跟。 */
        @JavascriptInterface
        public void applySysAudio() {
            BgService.enforceSysAudio(MainActivity.this);
        }

        /** 页面显式关静音/关免打扰：不做边沿判断，直接按滑条刻度把系统音量写回去。
            （enforceSysAudio 的 SA_WE 记账跟实际状态脱节时会一直「以为不用动」，
            用户看到的就是关了静音没声音——显式操作必须绕过记账机。） */
        @JavascriptInterface
        public void restoreSysAudio() {
            BgService.restoreSysAudio(MainActivity.this);
        }

        /** 壳里看到的静音/免打扰真值（cfg + SA_WE 账 + 系统音量读回）：
            页面关静音后调一次，toast 直接报壳里到底写了什么。 */
        @JavascriptInterface
        public String sysAudioState() {
            try {
                SharedPreferences sp = getSharedPreferences(PREFS, MODE_PRIVATE);
                org.json.JSONObject c = new org.json.JSONObject();
                try {
                    String raw = sp.getString("cfg_raw", "");
                    if (raw != null && raw.length() > 0) c = new org.json.JSONObject(raw);
                } catch (Throwable ignored) { }
                android.media.AudioManager a = am();
                org.json.JSONObject o = new org.json.JSONObject();
                o.put("mute", c.optBoolean("mute", false));
                o.put("dnd", c.optBoolean("dnd", false));
                o.put("dndF", c.optString("dndF", "22:00"));
                o.put("dndT", c.optString("dndT", "08:00"));
                o.put("alertVol", c.optDouble("alertVol", 60));
                o.put("speechVol", c.optDouble("speechVol", 90));
                o.put("we", sp.getBoolean("sysaudio_we_silent", false));
                o.put("ring", a.getStreamVolume(android.media.AudioManager.STREAM_RING));
                o.put("ringMx", a.getStreamMaxVolume(android.media.AudioManager.STREAM_RING));
                o.put("music", a.getStreamVolume(android.media.AudioManager.STREAM_MUSIC));
                o.put("musicMx", a.getStreamMaxVolume(android.media.AudioManager.STREAM_MUSIC));
                return o.toString();
            } catch (Throwable t) { return "{}"; }
        }

        /** 前台页面自己判断要念时调（页面没有 speechSynthesis 也能出声） */
        @JavascriptInterface
        public void speak(final String text) {
            runOnUiThread(new Runnable() {
                @Override public void run() { speakText(text); }
            });
        }

        /** 壳现在还在出声吗（播报/提示语，含「已交给壳、正在下载」的那段）。
            自动听写开麦前问这个：光等「播报结束」回调不够——回调之前还有下载和
            1 秒提示音，回调之后也有扬声器尾巴，开早了就会把播报自己录进去。 */
        @JavascriptInterface
        public boolean voiceBusy() {
            try {
                if (BgService.isSpeaking()) return true;
                if (fgSayBusy) return true;
                if (cueMp != null && cueMp.isPlaying()) return true;
                if (sayMp != null && sayMp.isPlaying()) return true;
            } catch (Throwable ignored) {}
            return false;
        }

        /** 念一句内置提醒语音（kind=done/perm/ask/input/limit/stuck/test）。云端真人音，
            不依赖系统语音引擎——没有 TTS 的手机（如多亲）也能播报。 */
        @JavascriptInterface
        public void cue(final String kind) {
            runOnUiThread(new Runnable() {
                @Override public void run() { playCue(kind); }
            });
        }

        /** 前台侧播报：页面把开发机合成好的整句地址递过来，壳自己下载播放。
            只在「息屏提醒关着」时用到——开着的时候整件事归 BgService（CB.announces），
            页面一句都不念。取不到就静默，**不换别的嗓音顶替**（理由见
            BgService.playRemoteSay：降级就会变成「怎么是别人的声音」）。 */
        @JavascriptInterface
        public void sayUrl(final String url) {
            final String u = url == null ? "" : url.trim();
            if (u.length() == 0) return;
            /* 后台服务正在念（页面的 4 秒兜底把同一条地址又递了一遍）：两条音轨叠着
               放，听感就是卡顿/截断——让后台把它念完，这里不重复出声。 */
            if (BgService.isSpeaking()) return;
            fgSayBusy = true;                    /* 下载+播放期间都算在出声 */
            new Thread(new Runnable() {
                @Override public void run() { playRemoteSayForeground(u); }
            }).start();
        }

        /** 新版壳有内置语音（页面据此决定是否还要显示「换嗓音」） */
        @JavascriptInterface
        public boolean hasCue() {
            return true;
        }

        /** 壳负责出声吗？有壳时提醒音和语音播报**全部**由 BgService 负责——它不看
            前台/后台，收到就响一遍，只认自己那份「已响过」的水位。页面据此不再自己
            念，否则两边各响一次（用户听到的就是「亮屏两个提示音」）。
            老壳没有这个接口 → 页面照旧自己念，不会因此变哑。 */
        @JavascriptInterface
        public boolean announces() {
            return BgService.isRunning() && BgService.enabled(MainActivity.this);
        }

        /** 系统朗读可用吗？页面据此决定语音里念整句「在做什么，完成了」，
            还是退回内置真人音（只能念预设的状态句，没有 TTS 的手机如多亲靠它）。 */
        @JavascriptInterface
        public boolean hasTts() {
            return ttsOk && tts != null;
        }

        /** 换一个中文嗓音（页面「换一个」按钮）：循环本地中文嗓音，返回新嗓音名，空=没得换。 */
        @JavascriptInterface
        public String nextTtsVoice() {
            try {
                final java.util.List<android.speech.tts.Voice> vs = zhVoices();
                if (vs.isEmpty()) return "";
                android.content.SharedPreferences sp = getSharedPreferences("codeboss", 0);
                String cur = sp.getString(PRE_TTS_VOICE, "");
                int idx = -1;
                for (int i = 0; i < vs.size(); i++) {
                    if (vs.get(i).getName().equals(cur)) { idx = i; break; }
                }
                idx = (idx + 1) % vs.size();
                final android.speech.tts.Voice pick = vs.get(idx);
                sp.edit().putString(PRE_TTS_VOICE, pick.getName()).apply();
                runOnUiThread(new Runnable() {
                    @Override public void run() {
                        try { if (tts != null) tts.setVoice(pick); } catch (Throwable ignored) {}
                    }
                });
                return pick.getName();
            } catch (Throwable e) {
                Log.w(TAG, "换嗓音失败: " + e);
                return "";
            }
        }

        /** 当前嗓音（设置页显示用）。 */
        @JavascriptInterface
        public String ttsVoice() {
            try {
                if (tts == null) return "";
                android.speech.tts.Voice v = tts.getVoice();
                return v == null ? "" : v.getName();
            } catch (Throwable e) { return ""; }
        }

        /** 页面报告当前正看的会话：壳据此撤掉它的通知（进会话 = 已读） */
        @JavascriptInterface
        public void setView(final String sid) {
            VIEW_SID = sid == null ? "" : sid;
            runOnUiThread(new Runnable() {
                @Override public void run() {
                    try {
                        android.app.NotificationManager nm =
                                (android.app.NotificationManager) getSystemService(NOTIFICATION_SERVICE);
                        if (nm != null && sid != null) nm.cancel(("cb_" + sid).hashCode());
                    } catch (Throwable ignored) {
                    }
                }
            });
        }

        /** 离线页「WiFi 切换」：只在 App 里做，失败也不跳系统设置 */
        @JavascriptInterface
        public void toggleWifi() {
            Log.i(TAG, "bridge.toggleWifi");
            runOnUiThread(new Runnable() {
                @Override public void run() {
                    boolean ok = doToggleWifi();
                    if (!ok) {
                        /* 平台不给权限：把话说明白，留在本页，交给用户选网络或手动处理 */
                        js("window.__cbWifiBlocked && window.__cbWifiBlocked()");
                    }
                    ui.postDelayed(new Runnable() { @Override public void run() { pushNetState(); } }, 900);
                }
            });
        }

        /** 离线页「移动数据 切换」：开关不了就跳系统的移动网络开关页（这是唯一允许离开 App 的入口） */
        @JavascriptInterface
        public void toggleMobile() {
            Log.i(TAG, "bridge.toggleMobile");
            runOnUiThread(new Runnable() {
                @Override public void run() {
                    boolean ok = doToggleMobile();
                    if (!ok) {
                        js("window.__cbMobileBlocked && window.__cbMobileBlocked()");
                        openMobileDataSettings();
                    }
                    ui.postDelayed(new Runnable() { @Override public void run() { pushNetState(); } }, 900);
                }
            });
        }

        /** 离线页「扫描 WiFi」 */
        @JavascriptInterface
        public void wifiScan() {
            Log.i(TAG, "bridge.wifiScan");
            runOnUiThread(new Runnable() {
                @Override public void run() { doScanWifi(); }
            });
        }

        /** 离线页「连接 WiFi」 */
        @JavascriptInterface
        public void connectWifi(final String ssid, final String psk) {
            Log.i(TAG, "bridge.connectWifi " + ssid);
            runOnUiThread(new Runnable() {
                @Override public void run() { doConnectWifi(ssid, psk); }
            });
        }

        /** 离线页「打开系统网络浮层」：有浮层的机器（红米 Android 10）用它管 WiFi + 移动数据 */
        @JavascriptInterface
        public void openNetPanel() {
            Log.i(TAG, "bridge.openNetPanel");
            runOnUiThread(new Runnable() {
                @Override public void run() { openNetPanelOnly(); }
            });
        }

        /** 设置页「WiFi」：优先系统网络浮层（盖在 App 上），没有就落系统 WiFi 页 */
        @JavascriptInterface
        public void openWifiSettings() {
            Log.i(TAG, "bridge.openWifiSettings");
            runOnUiThread(new Runnable() {
                @Override public void run() {
                    openSystemAway(new String[][]{
                            {"android.settings.panel.action.INTERNET_CONNECTIVITY", "网络浮层"},
                            {"android.settings.WIFI_SETTINGS", "WiFi 设置"},
                            {"android.settings.WIRELESS_SETTINGS", "无线和网络"}});
                }
            });
        }

        /** 设置页「移动数据」：同上，落到带移动数据开关的系统页 */
        @JavascriptInterface
        public void openMobileSettings() {
            Log.i(TAG, "bridge.openMobileSettings");
            runOnUiThread(new Runnable() {
                @Override public void run() {
                    openSystemAway(new String[][]{
                            {"android.settings.panel.action.INTERNET_CONNECTIVITY", "网络浮层"},
                            {"android.settings.DATA_USAGE_SETTINGS", "数据用量"},
                            {"android.settings.WIRELESS_SETTINGS", "无线和网络"},
                            {"android.settings.NETWORK_OPERATOR_SETTINGS", "双卡与移动网络"}});
                }
            });
        }

        /** 扫描前要定位权限（Android 10+ 的规定） */
        @JavascriptInterface
        public void requestLocation() {
            runOnUiThread(new Runnable() {
                @Override public void run() { askLocation(); }
            });
        }

        /** 手动刷新当前页面（任何页面都能用：⚙ 面板里的「刷新页面」） */
        @JavascriptInterface
        public void refresh() {
            Log.i(TAG, "bridge.refresh");
            runOnUiThread(new Runnable() {
                @Override public void run() {
                    retryMs = 3000;
                    if (retryRun != null) { ui.removeCallbacks(retryRun); retryRun = null; }
                    if (!netOnline()) {
                        /* 没网还刷新只会得到空白/「网页无法打开」：直接给网络页 */
                        Log.i(TAG, "bridge.refresh：没有网络，改走离线页");
                        showOffline();
                        return;
                    }
                    netFail = false;
                    offlineShown = false;
                    try { if (web != null) web.loadUrl(lastUrl); } catch (Throwable ignored) { }
                }
            });
        }

        /** 兼容旧页面：现在什么都不做（不再跳系统设置） */
        @JavascriptInterface
        public void openNetworkSettings() {
            Log.i(TAG, "bridge.openNetworkSettings 忽略（只在 App 内设置）");
        }

        @JavascriptInterface
        public void pttDown() {
            Log.i(TAG, "bridge.pttDown rec=" + (rec != null));
            runOnUiThread(new Runnable() {
                @Override public void run() {
                    /* 正在自动听：手动按麦 = 我来说，先收掉自动听，按普通方式录 */
                    if (autoRec) {
                        autoStop(true, true);
                        js("window.__autoListen && window.__autoListen('cancel','ptt_down')");
                    }
                    if (rec != null) {
                        // 原生已经在录了：只把 UI 拉回"录音中"，不要开第二条
                        js("window.__pttStart && window.__pttStart()");
                        return;
                    }
                    ptt(true);
                }
            });
        }

        @JavascriptInterface
        public void pttUp() {
            Log.i(TAG, "bridge.pttUp rec=" + (rec != null));
            runOnUiThread(new Runnable() {
                @Override public void run() {
                    /* 自动听写中的"松手"不是用户的操作（页面 onPause 兜底），丢掉这段 */
                    if (autoRec) {
                        autoStop(true, true);
                        js("window.__autoListen && window.__autoListen('cancel','ptt_up')");
                        return;
                    }
                    if (rec == null) {
                        // 原生没在录：让页面回到空闲态，避免 UI 卡在"录音中"
                        js("window.__pttCancel && window.__pttCancel()");
                        return;
                    }
                    ptt(false);
                }
            });
        }

        /** 页面日志（排查用） */
        @JavascriptInterface
        public void log(String m) { Log.i(TAG, "js: " + m); }

        /** 页面可查原生是否真在录音：「rec」/「auto」/「idle」。UI 状态以它为准，防止两边跑偏。
            auto = 自动听写（页面别把它当按住说话来对账） */
        @JavascriptInterface
        public String pttState() { return rec != null ? (autoRec ? "auto" : "rec") : "idle"; }

        /** 自动听写：等用户说话。phase = "dictate"（说指令）/ "confirm"（只说发送/取消）。
            结果走 __autoResult（页面按自己的自动态分流），说完自动停。 */
        @JavascriptInterface
        public void autoListenStart(final String phase) {
            Log.i(TAG, "bridge.autoListenStart " + phase);
            runOnUiThread(new Runnable() {
                @Override public void run() { autoStart(phase); }
            });
        }

        /** 收掉自动听写（不发这段话）。 */
        @JavascriptInterface
        public void autoListenStop() {
            Log.i(TAG, "bridge.autoListenStop auto=" + autoRec);
            runOnUiThread(new Runnable() {
                @Override public void run() { autoStop(true, true); }   /* 页面自己叫停：别再回 idle */
            });
        }

        @JavascriptInterface
        public String native_() { return "2"; }

        /** 页面读配置：已知五键取实值（壳侧 setRunMode 等会直接改，以实值为准），
            其余键（bright 等）由 setCfg 存的原始 JSON 直通——页面以后加新键不用改壳 */
        @JavascriptInterface
        public String getCfg() {
            String base = "{\"mode\":\"" + mode() + "\",\"autosend\":" + (autosend() ? "true" : "false")
                    + ",\"stream\":" + (streamOn() ? "true" : "false")
                    + ",\"keepon\":" + (keeponOn() ? "true" : "false")
                    + ",\"eye\":" + (eyeOn() ? "true" : "false") + "}";
            try {
                String raw = getSharedPreferences(PREFS, MODE_PRIVATE).getString("cfg_raw", "");
                if (raw != null && raw.length() > 0) {
                    org.json.JSONObject out = new org.json.JSONObject(raw);
                    out.put("mode", mode());
                    out.put("autosend", autosend());
                    out.put("stream", streamOn());
                    out.put("keepon", keeponOn());
                    out.put("eye", eyeOn());
                    return out.toString();
                }
            } catch (Throwable ignored) { }
            return base;
        }

        /** 页面写配置：整包 JSON 原样存 cfg_raw（新键随存），已知五键照旧落各自偏好。
            旧壳没有 cfg_raw：新键丢弃，页面 localStorage 兜底 */
        @JavascriptInterface
        public void setCfg(String json) {
            try {
                org.json.JSONObject o = new org.json.JSONObject(json);
                SharedPreferences.Editor e = getSharedPreferences(PREFS, MODE_PRIVATE).edit();
                e.putString("cfg_raw", o.toString());
                if (o.has("mode")) e.putString("mode", o.getString("mode"));
                if (o.has("autosend")) e.putBoolean("autosend", o.getBoolean("autosend"));
                if (o.has("stream")) e.putBoolean("stream", o.getBoolean("stream"));
                if (o.has("keepon")) e.putBoolean("keepon", o.getBoolean("keepon"));
                if (o.has("eye")) e.putBoolean("eye", o.getBoolean("eye"));
                e.apply();
                if (o.has("bgn")) {   /* 息屏提醒开关：开了起服务，关了立刻停 */
                    if (o.getBoolean("bgn")) BgService.startIfEnabled(MainActivity.this);
                    else BgService.stop(MainActivity.this);
                }
            } catch (Throwable ignored) { }
        }

        /** 物理快捷键「发送」：把手里的文字送进终端 */
        @JavascriptInterface
        public void send() {
            runOnUiThread(new Runnable() {
                @Override public void run() { js("window.__cbSend && window.__cbSend()"); }
            });
        }

        /** 运行模式读取："app" | "kiosk" */
        @JavascriptInterface
        public String getRunMode() { return runMode(); }

        /** 运行模式切换：kiosk=桌面接管（开机自启+锁定），app=普通应用（随时可退出） */
        @JavascriptInterface
        public void setRunMode(final String m) {
            Log.i(TAG, "bridge.setRunMode " + m);
            runOnUiThread(new Runnable() {
                @Override public void run() { applyRunMode(m, true); }
            });
        }

        /** 首次运行引导完成：记住，摘掉浮层 */
        @JavascriptInterface
        public void guideDone() {
            getSharedPreferences(PREFS, MODE_PRIVATE).edit().putBoolean(KEY_GUIDED, true).apply();
            runOnUiThread(new Runnable() {
                @Override public void run() {
                    js("var g=document.getElementById('cbguide');if(g&&g.parentNode)g.parentNode.removeChild(g);");
                }
            });
        }

        /* ── 屏幕显示：亮度/音量/常亮（设置页「屏幕显示」卡用）────────────── */

        /** 读窗口亮度：页面换算成百分比。
            跟随系统时窗口属性是 -1，直接返回会让滑条恒显示 100%（跟真实屏幕对不上），
            这里换成系统当前亮度，App 里的滑条才和系统设置保持同步。读不到按满亮。 */
        @JavascriptInterface
        public String getBrightness() {
            float v = getWindow().getAttributes().screenBrightness;
            if (v < 0f) {
                try {
                    v = android.provider.Settings.System.getInt(getContentResolver(),
                            android.provider.Settings.System.SCREEN_BRIGHTNESS) / 255f;
                } catch (Throwable t) { v = 1f; }
            }
            return String.valueOf(v);
        }

        /** 设窗口亮度 v∈(0,1]；v≤0 视为「跟随系统」。0.02=近最暗背光（页面还有黑色压暗层可再暗）。
            只影响本 App 窗口，退出/重开即回系统亮度 */
        @JavascriptInterface
        public void setBrightness(final float v) {
            runOnUiThread(new Runnable() {
                @Override public void run() {
                    WindowManager.LayoutParams lp = getWindow().getAttributes();
                    lp.screenBrightness = (v <= 0f)
                            ? WindowManager.LayoutParams.BRIGHTNESS_OVERRIDE_NONE : Math.min(v, 1.0f);
                    getWindow().setAttributes(lp);
                }
            });
        }

        /** 系统的「自动调节亮度」：1=开 0=关 -1=读不到。页面用它和 App 的「自动」档对齐。 */
        @JavascriptInterface
        public String getAutoBright() {
            try {
                return String.valueOf(android.provider.Settings.System.getInt(getContentResolver(),
                        android.provider.Settings.System.SCREEN_BRIGHTNESS_MODE)
                        == android.provider.Settings.System.SCREEN_BRIGHTNESS_MODE_AUTOMATIC ? 1 : 0);
            } catch (Throwable t) { return "-1"; }
        }

        /** App 自己的自动亮度（安卓）：用光线传感器直接调窗口亮度。
            不依赖系统的「自动调节亮度」开关，也不需要「修改系统设置」权限——
            用户在设置页一点就生效，**不会跳系统页**。 */
        @JavascriptInterface
        public void setAutoBright(final boolean on) { setAutoBrightSensor(on); }

        /** 静默把系统的「自动调节亮度」也打开（仅当已经授权过「修改系统设置」）。
            没授权就返回 -1，不弹页、不打断——App 自己的感光已经够用了。 */
        @JavascriptInterface
        public String syncSystemAutoBright() {
            try {
                if (!android.provider.Settings.System.canWrite(MainActivity.this)) return "-1";
                if (!"1".equals(getAutoBright())) {
                    android.provider.Settings.System.putInt(getContentResolver(),
                            android.provider.Settings.System.SCREEN_BRIGHTNESS_MODE,
                            android.provider.Settings.System.SCREEN_BRIGHTNESS_MODE_AUTOMATIC);
                }
                return "1";
            } catch (Throwable t) { Log.w(TAG, "syncSystemAutoBright: " + t); return "-1"; }
        }

        /** 媒体音量（当前值/最大值）：页面拉滑条；MODIFY_AUDIO_SETTINGS 已在 manifest */
        @JavascriptInterface
        public int getVolume() {
            try { return am().getStreamVolume(android.media.AudioManager.STREAM_MUSIC); }
            catch (Throwable t) { return 0; }
        }

        @JavascriptInterface
        public int getMaxVolume() {
            try { return am().getStreamMaxVolume(android.media.AudioManager.STREAM_MUSIC); }
            catch (Throwable t) { return 15; }
        }

        @JavascriptInterface
        public void setVolume(final int v) {
            runOnUiThread(new Runnable() {
                @Override public void run() {
                    try {
                        int m = am().getStreamMaxVolume(android.media.AudioManager.STREAM_MUSIC);
                        am().setStreamVolume(android.media.AudioManager.STREAM_MUSIC,
                                Math.max(0, Math.min(v, m)), 0);
                    } catch (Throwable ignored) { }
                }
            });
        }

        /* ── 多路音量：媒体之外，提醒声走通知流、需要确认走闹钟流，得能单独调 ──
           老页面只调 getVolume()/setVolume()（媒体），新页面用带流名的这组；
           保留旧方法，壳与页面版本错开时不会互相打不通。 */
        private int streamOf(String s) {
            if ("ring".equals(s)) return android.media.AudioManager.STREAM_RING;
            if ("notif".equals(s)) return android.media.AudioManager.STREAM_NOTIFICATION;
            if ("alarm".equals(s)) return android.media.AudioManager.STREAM_ALARM;
            if ("call".equals(s)) return android.media.AudioManager.STREAM_VOICE_CALL;
            if ("system".equals(s)) return android.media.AudioManager.STREAM_SYSTEM;
            return android.media.AudioManager.STREAM_MUSIC;
        }

        /** 某一路的当前音量；取不到返回 -1（页面据此隐藏该行） */
        @JavascriptInterface
        public int getVol(String name) {
            try { return am().getStreamVolume(streamOf(name)); } catch (Throwable t) { return -1; }
        }

        @JavascriptInterface
        public int getMaxVol(String name) {
            try {
                int mx = am().getStreamMaxVolume(streamOf(name));
                return mx > 0 ? mx : -1;   // 有些 ROM 对通话/系统流给 0，等于不可调
            } catch (Throwable t) { return -1; }
        }

        @JavascriptInterface
        public void setVol(final String name, final int v) {
            runOnUiThread(new Runnable() {
                @Override public void run() {
                    try {
                        int st = streamOf(name);
                        int mx = am().getStreamMaxVolume(st);
                        am().setStreamVolume(st, Math.max(0, Math.min(v, mx)), 0);
                        /* 通知与铃声在多数机型上联动：只调通知流时铃声不跟，提醒声会“没变”。
                           一起设一遍，两边滑条都动，用户听得到变化。 */
                        if (st == android.media.AudioManager.STREAM_NOTIFICATION) {
                            try {
                                int rm = am().getStreamMaxVolume(android.media.AudioManager.STREAM_RING);
                                am().setStreamVolume(android.media.AudioManager.STREAM_RING,
                                        Math.max(0, Math.min(v, rm)), 0);
                            } catch (Throwable ignored) { }
                        }
                    } catch (Throwable ignored) { }
                }
            });
        }

        /** 试听：在该流上放一声短提示音（音量滑条松手时页面调一次） */
        @JavascriptInterface
        public void beepVol(final String name) {
            runOnUiThread(new Runnable() {
                @Override public void run() {
                    android.media.ToneGenerator tg = null;
                    try {
                        tg = new android.media.ToneGenerator(streamOf(name), 80);
                        tg.startTone(android.media.ToneGenerator.TONE_PROP_BEEP, 260);
                        final android.media.ToneGenerator f = tg;
                        new android.os.Handler(getMainLooper()).postDelayed(new Runnable() {
                            @Override public void run() { try { f.release(); } catch (Throwable ignored) { } }
                        }, 700);
                    } catch (Throwable t) {
                        try { if (tg != null) tg.release(); } catch (Throwable ignored) { }
                    }
                }
            });
        }

        /** 屏幕常亮开关：FLAG_KEEP_SCREEN_ON 只点亮本 App 在前台时的屏幕，切走即失效，不算系统级常亮 */
        @JavascriptInterface
        public void setKeepOn(final boolean on) {
            runOnUiThread(new Runnable() {
                @Override public void run() {
                    if (on) getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
                    else getWindow().clearFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
                }
            });
        }

        /* ── 护眼/压暗系统悬浮层：键盘、状态栏是系统窗口，页面内的贴层够不着。
              壳画全屏不可交互悬浮层（需悬浮窗权限）：护眼=暖色罩，压暗=黑罩，与
              页面贴层同色同透。返回 "ok"=悬浮层接管 / "page"=无权限（页面自己罩，
              顺手拉起授权页，授完 onResume 自动补挂）。 ── */

        /** 护眼开关（壳侧持久化 + 全屏悬浮层，含键盘/状态栏） */
        @JavascriptInterface
        public String setEye(final boolean on) {
            getSharedPreferences(PREFS, MODE_PRIVATE).edit().putBoolean("eye", on).apply();
            final boolean needPerm = on && !overlayPerm();
            runOnUiThread(new Runnable() {
                @Override public void run() {
                    if (needPerm) {
                        expectAway = true;   // 授权页是别人家，kiosk 看门狗让路
                        ui.postDelayed(new Runnable() { @Override public void run() {
                            expectAway = false;   // 兜底：60s 后不再让路
                        }}, 60000);
                        try {
                            startActivity(new android.content.Intent(
                                    android.provider.Settings.ACTION_MANAGE_OVERLAY_PERMISSION,
                                    android.net.Uri.parse("package:" + getPackageName())));
                        } catch (Throwable ignored) { }
                    } else {
                        applyOverlays();
                    }
                }
            });
            return needPerm ? "page" : "ok";
        }

        /** 电量：{"l":83,"c":1}（l=百分比 c=1 充电中）。页面标题栏显示用；
            系统 BatteryManager 无需权限，耗电统计读数比 navigator 稳（老 WebView 没有该 API）。 */
        @JavascriptInterface
        public String battery() {
            try {
                android.os.BatteryManager bm = (android.os.BatteryManager)
                        getSystemService(BATTERY_SERVICE);
                int l = bm.getIntProperty(android.os.BatteryManager.BATTERY_PROPERTY_CAPACITY);
                int st = bm.getIntProperty(android.os.BatteryManager.BATTERY_PROPERTY_STATUS);
                boolean ch = st == android.os.BatteryManager.BATTERY_STATUS_CHARGING
                        || st == android.os.BatteryManager.BATTERY_STATUS_FULL;
                return "{\"l\":" + l + ",\"c\":" + (ch ? 1 : 0) + "}";
            } catch (Throwable t) {
                return "";
            }
        }

        /** 页面查护眼是否已由系统悬浮层接管（是→页面藏自己的暖色层，避免双重叠色） */
        @JavascriptInterface
        public String eyeSys() {
            return (eyeOn() && overlayPerm() && eyeOv != null) ? "1" : "0";
        }

        /** 页面问：压暗能不能交给壳的悬浮层（有悬浮窗权限）？能→页面别再自己罩一层，
            否则页面层 + 悬浮层两道黑罩叠加，屏幕比滑条标的值暗得多且忽明忽暗。 */
        @JavascriptInterface
        public String dimCap() {
            return overlayPerm() ? "1" : "0";
        }

        /** 黑色压暗层（亮度暗档联动）：o∈[0,0.85]，0=摘掉。同样罩到键盘 */
        @JavascriptInterface
        public void setDim(final float o) {
            final int a = Math.max(0, Math.min(217, Math.round(o * 255f)));
            getSharedPreferences(PREFS, MODE_PRIVATE).edit().putInt("dimA", a).apply();
            runOnUiThread(new Runnable() {
                @Override public void run() { applyOverlays(); }
            });
        }

        /* ── 客户端版本与手动检查更新（设置页「客户端」卡用）────────────────── */

        /** 读本机已装客户端的版本标记（AndroidManifest 烧进 APK：versionCode/versionName） */
        @JavascriptInterface
        public String apkVer() {
            String ver = "?";
            try { ver = getPackageManager().getPackageInfo(getPackageName(), 0).versionName; }
            catch (Throwable ignored) { }
            return "{\"code\":" + installedCode() + ",\"ver\":\"" + ver + "\"}";
        }

        /** 「安装未知应用」是否已授权（Android 8+）；1=可以装 0=要先去系统设置开 */
        @JavascriptInterface
        public String installPerm() { return canInstall() ? "1" : "0"; }

        /** 网页侧「授权并升级」：直接跳本应用的「安装未知应用」开关 */
        @JavascriptInterface
        public void grantInstallPerm() {
            runOnUiThread(new Runnable() { @Override public void run() { openInstallPerm(); } });
        }

        /** 手动「检查更新」：绕过「同版本只提示一次」的限制；已是最新也回一句话 */
        @JavascriptInterface
        public void checkUpdate() {
            final String base;                              /* WebView 方法必须在 UI 线程调，老内核后台调用直接抛 */
            try { base = baseUrl(); } catch (Throwable t) { Log.w(TAG, "checkUpdate baseUrl: " + t); return; }
            String ck0 = null;
            try { ck0 = android.webkit.CookieManager.getInstance().getCookie(base); } catch (Throwable ignored) { }
            final String cookie = ck0;
            new Thread(new Runnable() {
                @Override public void run() {
                    try { checkApkUpdate(true, base, cookie); } catch (Throwable t) { Log.w(TAG, "apk_ver: " + t); }
                }
            }).start();
        }
    }

    private String mode() { return getSharedPreferences(PREFS, MODE_PRIVATE).getString("mode", "hold"); }
    /** 默认「只填框」：语音识别错一句就可能让 Claude 直接动手。
        旧版默认 true 且随设置保存落盘 → as_v2 标记做一次性翻转，之后用户自己在
        ⚙ 语音里的选择才算数。 */
    private boolean autosend() {
        SharedPreferences p = getSharedPreferences(PREFS, MODE_PRIVATE);
        if (!p.getBoolean("as_v2", false)) {
            p.edit().putBoolean("as_v2", true).putBoolean("autosend", false).apply();
            return false;
        }
        return p.getBoolean("autosend", false);
    }
    private boolean streamOn() { return getSharedPreferences(PREFS, MODE_PRIVATE).getBoolean("stream", true); }
    private boolean keeponOn() { return getSharedPreferences(PREFS, MODE_PRIVATE).getBoolean("keepon", false); }
    private boolean eyeOn() { return getSharedPreferences(PREFS, MODE_PRIVATE).getBoolean("eye", false); }

    private android.media.AudioManager am() {
        return (android.media.AudioManager) getSystemService(AUDIO_SERVICE);
    }

    private String runMode() {
        return getSharedPreferences(PREFS, MODE_PRIVATE).getString(KEY_RUNMODE, "app");
    }

    private void toast(String msg) {
        try { android.widget.Toast.makeText(this, msg, android.widget.Toast.LENGTH_LONG).show(); } catch (Throwable ignored) {}
    }

    /* 运行模式落地（UI 线程）：
     * kiosk=桌面接管：开机自启（BootReceiver 常开）+ Home 永远回本 App + 尽力锁定。
     *   设备拥有者（adb dpm set-device-owner）= 真锁定不可退出；
     *   否则 = HOME 角色（弹系统「选择桌面」）+ 屏幕固定（可退出但 Home 必回）。
     * app=普通应用：解除全部接管策略 + 关掉 HOME 入口（系统自动把默认桌面还原），随时可退出。 */
    private void applyRunMode(final String m, final boolean fromUser) {
        final String mode = "kiosk".equals(m) ? "kiosk" : "app";
        getSharedPreferences(PREFS, MODE_PRIVATE).edit().putString(KEY_RUNMODE, mode).apply();
        Log.i(TAG, "runMode=" + mode + (fromUser ? " (user)" : ""));
        try { if (web != null) js("window.__cbRunMode&&window.__cbRunMode('" + mode + "')"); } catch (Throwable ignored) {}
        if ("kiosk".equals(mode)) {
            setHomeGate(true);   /* HOME 入口打开：本 App 成为桌面候选 */
            if (Kiosk.isOwner(this)) {
                Kiosk.applyPolicies(this);
                try { startLockTask(); } catch (Throwable ignored) {}
                toast("已进入桌面接管：开机自启，锁定在 Codeboss");
            } else if (miuiNoLauncher()) {
                /* 小米/红米/HyperOS 明确不让第三方当桌面（「仅支持官方认证的三方应用」），
                   弹了也选不了 → 不走 HOME 角色，只用屏幕固定的 Lock Task。 */
                setHomeGate(false);
                letAway(120000);
                ui.postDelayed(new Runnable() {
                    @Override public void run() {
                        try {
                            startLockTask();
                            toast("桌面接管：确认「屏幕固定」后 Home 键回不来");
                        } catch (Throwable t) {
                            toast("这台手机拦第三方桌面，全锁需 adb dpm set-device-owner");
                        }
                    }
                }, 800);
            } else {
                letAway(120000);   /* 系统「选择桌面」弹窗期间看门狗让路，否则 250ms 就被顶回去 */
                requestHomeRole(true);
                ui.postDelayed(new Runnable() {
                    @Override public void run() {
                        try {
                            startLockTask();
                            toast("桌面接管：Home 键始终回 Codeboss；确认「屏幕固定」后全屏锁定");
                        } catch (Throwable t) {
                            toast("已接管 Home 键；完整锁定需设备拥有者（adb dpm set-device-owner）");
                        }
                    }
                }, 1500);
            }
        } else {
            try { stopLockTask(); } catch (Throwable ignored) {}
            Kiosk.teardownPolicies(this);
            setHomeGate(false);   /* HOME 组件消失 → 系统自动清默认桌面、回落原桌面 */
            if (fromUser) {
                if (Kiosk.isOwner(this)) {
                    toast("已切回 App 模式：按 Home 即可回系统桌面");
                } else {
                    releaseHomeRole();
                    toast("已切回 App 模式：桌面已自动还原");
                }
            }
        }
    }

    /* 系统弹窗（申请桌面角色 / 屏幕固定确认）期间给看门狗让路：
       红米上「选择桌面」一弹出来，我们 250ms 就被拉回前台 → 弹窗秒关，用户根本点不到。
       超时兜底：用户不点也最多让路 2 分钟，之后看门狗照常。 */
    private final Runnable awayCap = new Runnable() {
        @Override public void run() {
            if (expectAway) { expectAway = false; Log.i(TAG, "让路结束，看门狗恢复"); }
        }
    };
    private void letAway(long ms) {
        expectAway = true;
        ui.removeCallbacks(awayCap);
        ui.postDelayed(awayCap, ms);
    }

    /* 小米系（MIUI/HyperOS）不允许第三方应用当桌面：
       「第三方桌面应用可能造成数据泄露，仅支持官方认证的三方应用」→ 别走 HOME 角色。 */
    private static boolean miuiNoLauncher() {
        String s = (android.os.Build.MANUFACTURER + " " + android.os.Build.BRAND
                + " " + android.os.Build.DISPLAY).toLowerCase();
        return s.contains("xiaomi") || s.contains("redmi") || s.contains("poco");
    }

    /* HOME 入口软开关：manifest 里 HOME 过滤器在 activity-alias HomeGate 身上。
       关=本 App 退出桌面候选——系统发现默认桌面组件被禁用会自动清掉该偏好，
       下一次按 Home 直接回原桌面（老系统也能自动切回，不用进设置）。 */
    private void setHomeGate(final boolean on) {
        try {
            getPackageManager().setComponentEnabledSetting(
                    new ComponentName(this, "com.agentboss.terminal.HomeGate"),
                    on ? PackageManager.COMPONENT_ENABLED_STATE_ENABLED
                       : PackageManager.COMPONENT_ENABLED_STATE_DISABLED,
                    PackageManager.DONT_KILL_APP);
            Log.i(TAG, "homeGate " + (on ? "on" : "off"));
        } catch (Throwable t) { Log.w(TAG, "homeGate: " + t); }
    }

    /* 让系统弹「选择桌面」：API29+ 走 RoleManager；老系统无入口，给指引文字。 */
    private void requestHomeRole(final boolean acquire) {
        try {
            if (android.os.Build.VERSION.SDK_INT >= 29) {
                android.app.role.RoleManager rm = getSystemService(android.app.role.RoleManager.class);
                if (rm != null && rm.isRoleAvailable(android.app.role.RoleManager.ROLE_HOME)) {
                    boolean held = rm.isRoleHeld(android.app.role.RoleManager.ROLE_HOME);
                    if (held != acquire) {   /* 目标态不同才弹（取=没持有 / 还=还持有） */
                        startActivityForResult(rm.createRequestRoleIntent(
                                android.app.role.RoleManager.ROLE_HOME), REQ_HOME_ROLE);
                    }
                    return;
                }
            }
        } catch (Throwable ignored) {}
        if (acquire) toast("请在 系统→默认应用→主屏幕 里选择 Codeboss");
    }

    /* 切回 App 模式交还 HOME：老系统靠 HomeGate 关闭自动还原（无弹窗）；
       API29+ 若角色还挂着，补一个角色弹窗让系统正式回收（用户点一下原桌面即可）。 */
    private void releaseHomeRole() {
        try {
            if (android.os.Build.VERSION.SDK_INT >= 29) {
                android.app.role.RoleManager rm = getSystemService(android.app.role.RoleManager.class);
                if (rm != null && rm.isRoleHeld(android.app.role.RoleManager.ROLE_HOME)) {
                    startActivityForResult(rm.createRequestRoleIntent(
                            android.app.role.RoleManager.ROLE_HOME), REQ_HOME_ROLE);
                }
            }
        } catch (Throwable ignored) {}
    }

    /* 首次运行引导浮层：录音权限/自启动/运行模式三件事一次说清，选完摘掉。
       KEY_GUIDED 记住只弹一次；离线/错误页不弹。HTML 经 quote() 转义进 insertAdjacentHTML。 */
    private static final String GUIDE_HTML =
        "<div id=\"cbguide\" style=\"position:fixed;left:0;right:0;top:0;bottom:0;background:#000d;z-index:9999\">"
      + "<div style=\"position:absolute;left:0;right:0;bottom:0;background:#11151f;color:#e6edf3;"
      + "border-radius:14px 14px 0 0;padding:18px 16px 14px;font-size:13px;line-height:1.7\">"
      + "<div style=\"font-weight:bold;font-size:15px;margin-bottom:8px\">👋 首次使用，花 30 秒设置</div>"
      + "<div>🎤 <b>麦克风</b>：系统弹权限时选「允许」；拒绝过就到 系统设置→应用→Codeboss 打开。</div>"
      + "<div>🔔 <b>自启动</b>：在手机管家/自启动管理里允许 Codeboss，重启后才能自动进入。</div>"
      + "<div>🏠 <b>运行模式</b>：App 模式＝普通 App，随开随用；桌面接管＝开机自动打开、Home 键始终回本 App、"
      + "尽力锁定不可退出（适合旧手机当专用遥控器）。建议先用 App 模式，设置页随时可改。</div>"
      + "<div style=\"display:flex;gap:8px;margin-top:12px\">"
      + "<button onclick=\"CB.setRunMode('app');CB.guideDone()\" style=\"flex:1;padding:11px 0;border:0;"
      + "border-radius:8px;background:#238636;color:#fff;font-size:14px\">📱 App 模式（推荐）</button>"
      + "<button onclick=\"CB.setRunMode('kiosk');CB.guideDone()\" style=\"flex:1;padding:11px 0;border:1px solid #2a3240;"
      + "border-radius:8px;background:#2a3240;color:#e6edf3;font-size:14px\">🏠 桌面接管</button>"
      + "</div>"
      + "<div style=\"text-align:center;margin-top:10px\">"
      + "<span onclick=\"CB.guideDone()\" style=\"color:#8b949e;font-size:12px\">稍后再说（保持 App 模式）</span></div>"
      + "</div></div>";

    private void maybeGuide() {
        if (getSharedPreferences(PREFS, MODE_PRIVATE).getBoolean(KEY_GUIDED, false)) return;
        if (web == null || pageErr) return;   // 离线/错误页不引导
        js("if(!document.getElementById('cbguide'))document.body.insertAdjacentHTML('beforeend',"
                + quote(GUIDE_HTML) + ");");
    }

    /** 物理键分工：音量上键＝说话，音量下键＝发送（见 onKeyDown）。
        旧的"双击发送 / 长按发送"已移除 —— 它们和「点一下开始 · 再点结束」的录音节奏撞车：
        说完再按一下结束，紧接着的那次按下会被判成双击的第一击或长按，录音和发送互相打断。 */

    private void ptt(boolean down) {
        if (down) startNativeRec(); else stopNativeRec();
    }

    private void js(String code) {
        if (web != null) web.evaluateJavascript(code, null);
    }

    /** 系统通知：按 sid 归并（同一会话的提醒只留最新一条），点开落到会话 */
    private void sysNotify(String sid, String title, String body, String sound, boolean silent) {
        if (sid == null || sid.length() == 0) sid = "codeboss";
        try {
            android.app.NotificationManager nm =
                    (android.app.NotificationManager) getSystemService(NOTIFICATION_SERVICE);
            if (nm == null) return;
            String chId = CH_DONE;
            if ("alert".equals(sound)) chId = CH_PERM;
            else if ("ping".equals(sound)) chId = CH_INPUT;
            if (silent) chId = CH_QUIET;
            if (android.os.Build.VERSION.SDK_INT >= 26) ensureChannels(nm);
            android.content.Intent it = new android.content.Intent(this, MainActivity.class);
            it.addFlags(android.content.Intent.FLAG_ACTIVITY_SINGLE_TOP);
            it.putExtra("sid", sid);
            int flags = android.app.PendingIntent.FLAG_UPDATE_CURRENT;
            if (android.os.Build.VERSION.SDK_INT >= 23) flags |= android.app.PendingIntent.FLAG_IMMUTABLE;
            android.app.PendingIntent pi = android.app.PendingIntent.getActivity(
                    this, sid.hashCode(), it, flags);
            String t = (title == null || title.length() == 0) ? "Codeboss" : title;
            String b = body == null ? "" : body;
            android.app.Notification.Builder nb = (android.os.Build.VERSION.SDK_INT >= 26)
                    ? new android.app.Notification.Builder(this, chId)
                    : new android.app.Notification.Builder(this);
            nb.setSmallIcon(R.drawable.ic_stat_codeboss)
              .setContentTitle(t)
              .setContentText(b)
              .setStyle(new android.app.Notification.BigTextStyle().bigText(b))
              .setAutoCancel(true)
              .setWhen(System.currentTimeMillis())
              .setContentIntent(pi);
            if (silent && android.os.Build.VERSION.SDK_INT < 26) {   /* 26+ 靠 CH_QUIET 渠道静音 */
                nb.setSound(null);
                nb.setVibrate(null);
            }
            nm.notify(("cb_" + sid).hashCode(), nb.build());
            Log.i(TAG, "notify " + sid + " " + t);
        } catch (Throwable e) {
            Log.w(TAG, "notify failed: " + e);
        }
    }

    /** 三类提醒各建一个渠道：完成=通知音、待确认=闹钟音、待回复=通知音+短振 */
    private void ensureChannels(android.app.NotificationManager nm) {
        try {
            mkChannel(nm, CH_DONE, "任务完成", android.media.RingtoneManager.TYPE_NOTIFICATION,
                    new long[]{0, 180});
            mkChannel(nm, CH_PERM, "需要确认", android.media.RingtoneManager.TYPE_ALARM,
                    new long[]{0, 300, 150, 300});
            mkChannel(nm, CH_INPUT, "等待回复", android.media.RingtoneManager.TYPE_NOTIFICATION,
                    new long[]{0, 120, 100, 120});
            android.app.NotificationChannel q = new android.app.NotificationChannel(
                    CH_QUIET, "静默提醒", android.app.NotificationManager.IMPORTANCE_DEFAULT);
            q.setDescription("正在看的会话，只留通知不出声");
            q.setSound(null, null);
            q.enableVibration(false);
            nm.createNotificationChannel(q);
        } catch (Throwable ignored) {
        }
    }

    private void mkChannel(android.app.NotificationManager nm, String id, String name,
                           int toneType, long[] pattern) {
        android.app.NotificationChannel ch = new android.app.NotificationChannel(
                id, name, android.app.NotificationManager.IMPORTANCE_HIGH);
        ch.setDescription("Codeboss 任务提醒");
        ch.enableVibration(true);
        ch.setVibrationPattern(pattern);
        try {
            android.net.Uri u = android.media.RingtoneManager.getDefaultUri(toneType);
            if (u != null) {
                ch.setSound(u, new android.media.AudioAttributes.Builder()
                        .setUsage(android.media.AudioAttributes.USAGE_NOTIFICATION)
                        .setContentType(android.media.AudioAttributes.CONTENT_TYPE_SONIFICATION)
                        .build());
            }
        } catch (Throwable ignored) {
        }
        nm.createNotificationChannel(ch);
    }

    /** 语音播报（重要提醒）。TTS 不可用就静默跳过，绝不因此影响通知。 */
    private void initTts() {
        try {
            tts = new android.speech.tts.TextToSpeech(this,
                    new android.speech.tts.TextToSpeech.OnInitListener() {
                @Override public void onInit(int status) {
                    try {
                        if (status == android.speech.tts.TextToSpeech.SUCCESS && tts != null) {
                            int r = tts.setLanguage(java.util.Locale.CHINA);
                            ttsOk = r != android.speech.tts.TextToSpeech.LANG_MISSING_DATA
                                    && r != android.speech.tts.TextToSpeech.LANG_NOT_SUPPORTED;
                            if (ttsOk) pickZhVoice();
                            try {
                                tts.setSpeechRate(0.95f);   /* 稍慢一点更像人说话 */
                                tts.setPitch(1.0f);
                            } catch (Throwable ignored) {}
                            try {
                                /* 念完/失败/被打断都把音频焦点放掉：另一家被压低的
                                   播报要能恢复（speakText 里申请的焦点）。 */
                                tts.setOnUtteranceProgressListener(
                                        new android.speech.tts.UtteranceProgressListener() {
                                    @Override public void onStart(String id) {}
                                    @Override public void onDone(String id) { BgService.focusDone(MainActivity.this); }
                                    @Override public void onError(String id) { BgService.focusDone(MainActivity.this); }
                                    @Override public void onStop(String id, boolean interrupted) { BgService.focusDone(MainActivity.this); }
                                });
                            } catch (Throwable ignored) {}
                        }
                    } catch (Throwable t) { ttsOk = false; }
                    Log.i(TAG, "tts ok=" + ttsOk);
                }
            });
        } catch (Throwable e) {
            Log.w(TAG, "tts 初始化失败: " + e);
        }
    }

    /** 在已装的本地中文嗓音里挑质量最高的一个（默认嗓音常是最机械的那个）。 */
    private void pickZhVoice() {
        try {
            if (tts == null) return;
            java.util.List<android.speech.tts.Voice> vs = zhVoices();
            if (vs.isEmpty()) return;
            String saved = getSharedPreferences("codeboss", 0).getString(PRE_TTS_VOICE, "");
            android.speech.tts.Voice pick = null;
            for (android.speech.tts.Voice v : vs) {
                if (v.getName().equals(saved)) { pick = v; break; }
            }
            if (pick == null) pick = vs.get(0);      /* 已按质量排序 */
            tts.setVoice(pick);
            Log.i(TAG, "tts voice=" + pick.getName() + " q=" + pick.getQuality());
        } catch (Throwable e) { Log.w(TAG, "tts 选嗓音失败: " + e); }
    }

    /** 本地（离线、已安装）中文嗓音，质量高的排前面。 */
    private java.util.List<android.speech.tts.Voice> zhVoices() {
        final java.util.ArrayList<android.speech.tts.Voice> out =
                new java.util.ArrayList<android.speech.tts.Voice>();
        try {
            if (tts == null) return out;
            java.util.Set<android.speech.tts.Voice> vs = tts.getVoices();
            if (vs == null) return out;
            for (android.speech.tts.Voice v : vs) {
                if (v == null || v.getLocale() == null) continue;
                if (!"zh".equals(v.getLocale().getLanguage())) continue;
                if (v.isNetworkConnectionRequired()) continue;      /* 离线优先，省流量也不卡 */
                try {
                    if (v.getFeatures() != null
                            && v.getFeatures().contains("notInstalled")) continue;
                } catch (Throwable ignored) {}
                out.add(v);
            }
            java.util.Collections.sort(out, new java.util.Comparator<android.speech.tts.Voice>() {
                @Override public int compare(android.speech.tts.Voice a, android.speech.tts.Voice b) {
                    return b.getQuality() - a.getQuality();
                }
            });
        } catch (Throwable e) { Log.w(TAG, "取嗓音列表失败: " + e); }
        return out;
    }

    private void speakText(String s) {
        if (s == null || s.length() == 0) return;
        if (!ttsOk || tts == null) { Log.i(TAG, "tts 不可用，跳过语音"); return; }
        try {
            /* 音量全归系统媒体流：「语音播报」滑条直接写系统媒体音量，TTS 满增益念 */
            /* 另一家 App（CodeBoss/AgentBoss）的播报这时会被压小，而不是叠在一起；
               TTS 本身没法按句压低（没有音量参数），念完在 UtteranceProgressListener
               里放手（见 initTts）。 */
            BgService.focusForSpeech(this);
            tts.speak(s, android.speech.tts.TextToSpeech.QUEUE_FLUSH, null,
                    "cb" + System.currentTimeMillis());
        } catch (Throwable e) {
            Log.w(TAG, "tts speak 失败: " + e);
            BgService.focusDone(this);
        }
    }

    /** 播放内置提醒语音（云端 TTS 生成的 wav，随包发布）。系统 TTS 引擎缺失也照播。
        assets 里的文件被压缩过，openFd 用不了 → 首次解到 cache 目录再播。 */
    private void playCue(String kind) {
        String k = kind == null ? "" : kind.trim();
        if (!(k.equals("done") || k.equals("perm") || k.equals("ask")
                || k.equals("input") || k.equals("limit") || k.equals("stuck")
                || k.equals("confirm") || k.equals("test"))) k = "done";
        final String kk = k;      /* 回调里用，得是 final */
        try {
            if (cueMp != null) { try { cueMp.release(); } catch (Throwable ignored) {} cueMp = null; }
            java.io.File f = new java.io.File(getCacheDir(), "cue_" + k + ".wav");
            if (!f.exists() || f.length() < 1000) {
                java.io.InputStream in = getAssets().open("tts/" + k);
                java.io.FileOutputStream out = new java.io.FileOutputStream(f);
                try {
                    byte[] buf = new byte[8192];
                    int n;
                    while ((n = in.read(buf)) > 0) out.write(buf, 0, n);
                } finally {
                    try { out.close(); } catch (Throwable ignored) {}
                    try { in.close(); } catch (Throwable ignored) {}
                }
            }
            final android.media.MediaPlayer mp = new android.media.MediaPlayer();
            mp.setAudioAttributes(new android.media.AudioAttributes.Builder()
                    .setUsage(android.media.AudioAttributes.USAGE_MEDIA)
                    .setContentType(android.media.AudioAttributes.CONTENT_TYPE_SPEECH)
                    .build());
            mp.setDataSource(f.getAbsolutePath());
            mp.setOnCompletionListener(new android.media.MediaPlayer.OnCompletionListener() {
                @Override public void onCompletion(android.media.MediaPlayer m) {
                    try { m.release(); } catch (Throwable ignored) {}
                    if (cueMp == m) cueMp = null;
                    if (BgService.AF_MP == m) { BgService.AF_MP = null; BgService.focusDone(MainActivity.this); }
                    notifyCueDone(kk);       /* 提示语念完了（例如「说发送就发」） */
                }
            });
            mp.prepare();
            /* 音量全归系统媒体流（「语音播报」滑条写的是系统媒体音量） */
            BgService.focusForSpeech(this);      /* 另一家播报时压小它，别叠音 */
            cueMp = mp;
            BgService.AF_MP = mp;
            mp.start();
            Log.i(TAG, "cue " + k);
        } catch (Throwable e) {
            Log.w(TAG, "播报语音失败: " + e);
            BgService.focusDone(this);
            notifyCueDone(kk);
        }
    }

    /* 前台侧播报（息屏提醒关着、App 在前台时）：页面把 /api/say/<id> 递过来，壳下载
       到缓存再放。和 BgService.playRemoteSay 同一套做法（带 cookie、缓存 say_<hash>.bin、
       取不到就不念）。 */
    private void playRemoteSayForeground(final String url) {
        File f = new File(getCacheDir(), "say_" + Integer.toHexString(url.hashCode()) + ".bin");
        /* 下载先写 .part，完整落盘才改成正式名：中断的残件会被当成完整音频
           「念半截就结束」（播报突然截断，2026-11-24）。同 BgService.playRemoteSay。 */
        File tmp = new File(getCacheDir(), f.getName() + ".part");
        try {
            if (!f.exists() || f.length() < 500) {
                HttpURLConnection c = conn(url);
                c.setConnectTimeout(6000);
                c.setReadTimeout(10000);
                String ck = cookieOnUiThread(url);
                if (ck != null && ck.length() > 0) c.setRequestProperty("Cookie", ck);
                String bt = BgService.bgToken();
                if (bt != null && bt.length() > 0) c.setRequestProperty("X-CB-Bgd", bt);
                int code = c.getResponseCode();
                if (code != 200) throw new java.io.IOException("http " + code);
                InputStream in = c.getInputStream();
                FileOutputStream out = new FileOutputStream(tmp);
                try {
                    byte[] buf = new byte[8192];
                    int n;
                    while ((n = in.read(buf)) > 0) out.write(buf, 0, n);
                } finally {
                    try { out.close(); } catch (Throwable ignored) {}
                    try { in.close(); } catch (Throwable ignored) {}
                }
                if (tmp.length() < 500) throw new java.io.IOException("empty");
                if (!tmp.renameTo(f)) throw new java.io.IOException("rename");
            }
            try { f.setLastModified(System.currentTimeMillis()); } catch (Throwable ignored) {}
            final File ff = f;
            runOnUiThread(new Runnable() {
                @Override public void run() { playSayFile(ff); }
            });
        } catch (Throwable e) {
            /* 取不到就不念：不换别的嗓音顶替（见 BgService.playRemoteSay） */
            Log.w(TAG, "前台播报取音频失败，这条不念: " + e);
            try { tmp.delete(); } catch (Throwable ignored) {}
            fgSayBusy = false;
        }
    }

    private void playSayFile(File f) {
        try {
            if (sayMp != null && sayMp.isPlaying()) {   /* 上一句还在念：排后面，不掐断 */
                fgPend = f;
                return;
            }
            if (sayMp != null) { try { sayMp.release(); } catch (Throwable ignored) {} sayMp = null; }
            final android.media.MediaPlayer mp = new android.media.MediaPlayer();
            mp.setAudioAttributes(new android.media.AudioAttributes.Builder()
                    .setUsage(android.media.AudioAttributes.USAGE_MEDIA)
                    .setContentType(android.media.AudioAttributes.CONTENT_TYPE_SPEECH)
                    .build());
            mp.setDataSource(f.getAbsolutePath());
            mp.setOnCompletionListener(new android.media.MediaPlayer.OnCompletionListener() {
                @Override public void onCompletion(android.media.MediaPlayer m) {
                    try { m.release(); } catch (Throwable ignored) {}
                    if (sayMp == m) sayMp = null;
                    if (BgService.AF_MP == m) BgService.AF_MP = null;
                    java.io.File n = fgPend;
                    if (n != null) {         /* 排队的那句接着念，先别告诉页面「念完了」 */
                        fgPend = null;
                        playSayFile(n);
                        return;
                    }
                    BgService.focusDone(MainActivity.this);   /* 念完放手，另一家被压低的恢复 */
                    fgSayBusy = false;
                    notifySayDone("");       /* 播报完了，页面可以开始自动听写 */
                }
            });
            mp.prepare();
            /* 音量全归系统媒体流（同内置语音） */
            BgService.focusForSpeech(this);      /* 另一家播报时压小它，别叠音 */
            sayMp = mp;
            BgService.AF_MP = mp;
            mp.start();
            Log.i(TAG, "say " + f.getName());
        } catch (Throwable e) {
            Log.w(TAG, "前台播报失败: " + e);
            java.io.File n = fgPend;
            if (n != null) {                 /* 这声没出来，排队的照念 */
                fgPend = null;
                playSayFile(n);
                return;
            }
            BgService.focusDone(this);
            fgSayBusy = false;
            notifySayDone("");               /* 没念出来也别让页面一直等 */
        }
    }

    /** CookieManager 要在有 Looper 的线程调；下载线程等结果 */
    private String cookieOnUiThread(final String url) {
        final String[] out = new String[1];
        final java.util.concurrent.CountDownLatch l = new java.util.concurrent.CountDownLatch(1);
        runOnUiThread(new Runnable() {
            @Override public void run() {
                try { out[0] = CookieManager.getInstance().getCookie(url); }
                catch (Throwable ignored) {}
                l.countDown();
            }
        });
        try { l.await(3, java.util.concurrent.TimeUnit.SECONDS); } catch (Throwable ignored) {}
        return out[0];
    }

    /** 把 assets/shim.js 注入当前页面（幂等，重复调用无副作用） */
    private void injectShim() {
        String code = asset("shim.js");
        if (code != null && code.length() > 0) js(code);
    }

    private String asset(String name) {
        InputStream in = null;
        try {
            in = getAssets().open(name);
            ByteArrayOutputStream bo = new ByteArrayOutputStream();
            byte[] buf = new byte[8192];
            int n;
            while ((n = in.read(buf)) > 0) bo.write(buf, 0, n);
            return new String(bo.toByteArray(), "UTF-8");
        } catch (Throwable t) {
            return null;
        } finally {
            try { if (in != null) in.close(); } catch (Throwable ignored) { }
        }
    }

    private void startNativeRec() {
        if (rec != null) { Log.i(TAG, "startRec: 已在录，忽略"); return; }
        if (checkSelfPermission(Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) {
            Log.w(TAG, "startRec: 没有录音权限");
            if (autoRec) {
                autoRec = false;
                js("window.__autoListen && window.__autoListen('error','App 未拿到录音权限')");
            } else {
                js("window.__pttError && window.__pttError('App 未拿到录音权限')");
            }
            return;
        }
        try {
            File dir = new File(getCacheDir(), "ptt");
            if (!dir.exists()) dir.mkdirs();
            recPath = new File(dir, "ptt.m4a").getAbsolutePath();
            rec = new MediaRecorder();
            rec.setAudioSource(MediaRecorder.AudioSource.MIC);
            rec.setOutputFormat(MediaRecorder.OutputFormat.MPEG_4);
            rec.setAudioEncoder(MediaRecorder.AudioEncoder.AAC);
            rec.setAudioEncodingBitRate(32000);
            rec.setAudioSamplingRate(16000);
            rec.setAudioChannels(1);
            rec.setOutputFile(recPath);
            rec.prepare();
            rec.start();
            recStart = System.currentTimeMillis();
            Log.i(TAG, "startRec: 开始录音" + (autoRec ? "（自动听写）" : ""));
            /* 自动听写不亮麦克风按钮（不是用户在按），状态由 __autoListen 显示 */
            if (!autoRec) js("window.__pttStart && window.__pttStart()");
            cancelRecCap();
            recCap = new Runnable() {
                @Override public void run() {
                    if (rec == null) return;
                    if (autoRec) autoStop(true); else stopNativeRec();
                }
            };
            ui.postDelayed(recCap, 60000);
        } catch (Throwable t) {
            try { if (rec != null) rec.release(); } catch (Throwable ignored) { }
            rec = null;
            Log.w(TAG, "startRec 失败: " + t);
            String msg = esc(t.getClass().getSimpleName() + ": " + t.getMessage());
            if (autoRec) {
                autoRec = false;
                js("window.__autoListen && window.__autoListen('error','" + msg + "')");
            } else {
                js("window.__pttError && window.__pttError('" + msg + "')");
            }
        }
    }

    private void stopNativeRec() {
        if (rec == null) return;
        final boolean wasAuto = autoRec;
        cancelRecCap();
        long ms = System.currentTimeMillis() - recStart;
        try { rec.stop(); } catch (Throwable ignored) { }
        try { rec.release(); } catch (Throwable ignored) { }
        rec = null;
        autoRec = false;
        /* 任何路径把自动听写收掉（含音量键），都把「听写期间常亮」还回去 */
        if (autoKeepOn) {
            autoKeepOn = false;
            if (!keeponOn()) getWindow().clearFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        }
        final String path = recPath;
        Log.i(TAG, "stopRec: " + ms + "ms" + (wasAuto ? "（自动听写）" : ""));
        if (wasAuto) js("window.__autoListen && window.__autoListen('uploading','')");
        else js("window.__pttDone && window.__pttDone(" + ms + ")");
        if (ms < 300) {
            // 极短一击：可能是双击发送的第一击，静默丢弃，不弹错
            if (wasAuto) { js("window.__autoListen && window.__autoListen('idle','')"); return; }
            js("window.__pttCancel && window.__pttCancel()");
            return;
        }
        String page = web == null ? lastUrl : web.getUrl();
        String cookie = null;
        try { cookie = CookieManager.getInstance().getCookie(page); } catch (Throwable ignored) { }
        if (cookie != null && cookie.contains("r_sess=")) {
            upload(path, ms, baseUrl(), cookie, wasAuto);
        } else {
            // 老 WebView 的 getCookie 不吐 httponly cookie（坚果 Chromium 62 实锤：
            // WebView 页面正常、Java 上传裸奔撞中继登录墙）→ 音频切块递给页面，
            // 由页面用 WebView 网络栈上传（cookie 自动带上，全机型铁通）
            uploadViaPage(path, ms, wasAuto);
        }
    }

    /* ── 自动听写：开麦 → VAD 判开口/说完 → 停录上传 ────────────────────
       没有 VAD 专用的 API，用 MediaRecorder.getMaxAmplitude()（0~32767）每 100ms
       探一次振幅当简易 VAD：
         · 阈值 = max(1500, 底噪*2.5)，底噪在静音段慢慢跟（环境吵也能自适应）；
         · 连续 3 次超阈值 = 开口（通知页面「听到了」）；
         · 开口后静 900ms = 说完了 → 停录、上传识别；
         · 一直没人说话 20 秒 → 直接丢弃（不上传 = 不花额度）；
         · 确认态最长 10 秒、口述态最长 30 秒（开口了就再多给 15 秒，按已说的算）。
       持续读不到振幅（部分 ROM 只喂 0）= 这机器给不了 VAD，报 nodata 让页面退回手动。 */
    /** 振幅统计一并报给页面（进开发机日志），门限才能按真机数据继续调 */
    private String autoStats() {
        return "peak=" + autoPeak + " floor=" + Math.round(autoFloor) + " thr=" + Math.round(autoThr);
    }

    private void autoStart(String phase) {
        if (rec != null) {
            /* 手动正在录：麦克风只有一路，别抢。让页面别等 */
            js("window.__autoListen && window.__autoListen('busy','')");
            return;
        }
        autoPhase = "confirm".equals(phase) ? "confirm" : "dictate";
        autoRec = true;
        /* 听写期间别让屏幕自己黑：黑了 = onPause = 收麦，自动听写就成了「2 秒自动退出」 */
        if (!autoKeepOn) {
            autoKeepOn = true;
            try { getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON); }
            catch (Throwable t) { autoKeepOn = false; }
        }
        startNativeRec();
        if (rec == null) return;             /* startNativeRec 已经回调 error */
        autoStartAt = System.currentTimeMillis();
        autoLastLoud = 0L;
        autoLoudCnt = 0;
        autoZeroCnt = 0;
        autoFrame = 0;
        autoSpokeAt = 0L;
        autoRecent = 0f;
        autoSpoke = false;
        autoFloor = -1f;
        autoThr = 0f;
        autoPeak = 0;
        js("window.__autoListen && window.__autoListen('started','" + autoPhase + "')");
        if (autoVad != null) ui.removeCallbacks(autoVad);
        autoVad = new Runnable() {
            @Override public void run() {
                if (rec == null || !autoRec) return;
                long now = System.currentTimeMillis();
                long el = now - autoStartAt;
                int amp = 0;
                try { amp = rec.getMaxAmplitude(); } catch (Throwable ignored) { }
                if (amp > autoPeak) autoPeak = amp;
                /* 下限跟踪：一帧比下限低就立刻跟下去（字词间的换气、句间停顿），
                   比下限高则极慢回升（应对环境底噪整体变大）。
                   开口后冻结——说话本身会抬下限，说到一半被判成静音就麻烦了。 */
                if (amp <= 0) {
                    /* 有的 ROM 振幅恒为 0：连续 2.5 秒还读不到就认输（这机器不能自动听） */
                    if (++autoZeroCnt >= 25) {
                        Log.w(TAG, "自动听写：读不到振幅，这台机器不支持");
                        autoStop(true, true);     /* 静默收麦，下面单独报 nodata */
                        js("window.__autoListen && window.__autoListen('nodata','')");
                        return;
                    }
                } else {
                    autoZeroCnt = 0;
                    if (!autoSpoke) {
                        if (autoFloor < 0f || amp < autoFloor) autoFloor = amp;
                        else autoFloor = autoFloor * 1.02f + 25f;
                        if (autoFloor < 60f) autoFloor = 60f;
                        if (autoFloor > 8000f) autoFloor = 8000f;
                    }
                }
                if (autoFloor < 0f) autoFloor = 300f;     /* 一帧都没读到时的兜底 */
                /* 门限：比底噪高一截；再给「绝对够响」兜底，底噪学歪了也不至于全聋。
                   开口后放宽——已经确定在说话了，别被偏高的门限把后半句切掉。 */
                float thr = Math.min(Math.max(700f, autoFloor * 1.9f + 250f), 4500f);
                autoThr = thr;
                boolean loud;
                /* 开口后按「相对下降」判停顿：环境整体吵、或 ROM 的振幅不回落时，
                   绝对门限永远成立，一句说完能录满上限（实测 45 秒）然后整段丢掉。 */
                if (autoSpoke) loud = amp >= Math.max(500f, autoRecent * 0.30f);
                else           loud = amp >= 700f && (amp >= thr || amp >= 3500f);
                if (loud) {
                    autoLastLoud = now;
                    if (++autoLoudCnt == 3 && !autoSpoke) {
                        autoSpoke = true;
                        autoSpokeAt = now;
                        autoRecent = amp;
                        js("window.__autoListen && window.__autoListen('speech',\"" + autoStats() + "\")");
                    }
                } else {
                    autoLoudCnt = 0;
                }
                if (autoSpoke) {
                    /* 最近有多响：涨了就记，落了慢慢忘（每 100ms 衰减 3%，约 2.3 秒减半） */
                    if (amp > autoRecent) autoRecent = amp; else autoRecent *= 0.97f;
                    if ((autoFrame++ % 10) == 0) {   /* 每秒把原始振幅报给页面（进开发机日志） */
                        js("window.__autoListen && window.__autoListen('level',\""
                           + amp + "/" + Math.round(autoRecent) + "/" + Math.round(autoFloor) + "\")");
                    }
                }
                if (autoSpoke && now - autoLastLoud >= 1200) {
                    autoStop(false);          /* 说完了 → 停录，走正常上传/识别 */
                    return;
                }
                /* 已经开了口：不管 VAD 判不判得出停顿，15 秒内必须收尾去识别。
                   （振幅不回落的老 ROM 只能靠这条兜底，否则永远等不到「说完」） */
                if (autoSpoke && autoSpokeAt > 0 && now - autoSpokeAt >= 15000) {
                    Log.i(TAG, "自动听写：说了 15 秒还没判到停顿，按已说的识别");
                    autoStop(false);
                    return;
                }
                /* 已经开口了就多给点时间（别把半句话截断）：到点按已说的一段识别 */
                long cap = ("confirm".equals(autoPhase) ? 10000 : 30000) + (autoSpoke ? 15000 : 0);
                if (el >= cap || (!autoSpoke && el >= 20000)) {
                    String st = autoStats();
                    if (autoSpoke) {
                        /* 听到了就识别，别白录——以前这里整段丢掉，用户看到的是「说了没反应」 */
                        Log.i(TAG, "自动听写：到时限，按已说的一段识别 " + st);
                        autoStop(false);
                        return;
                    }
                    /* 没人开口 = 白录，直接丢弃；把真实振幅报给页面（进开发机日志），
                       门限还能再调 */
                    Log.i(TAG, "自动听写放弃：" + st);
                    autoStop(true, true);
                    js("window.__autoListen && window.__autoListen('idle',\"" + st + "\")");
                    return;
                }
                ui.postDelayed(this, 100);
            }
        };
        ui.postDelayed(autoVad, 100);
    }

    /** 收掉自动听写。discard=true：这段不上传（没说话 / 用户按了别处 / 切走）。
        quiet=true 时不再回调 __autoListen('idle')：调用方自己会发更具体的状态。 */
    private void autoStop(boolean discard) { autoStop(discard, false); }

    private void autoStop(boolean discard, boolean quiet) {
        if (autoVad != null) { ui.removeCallbacks(autoVad); autoVad = null; }
        /* 收麦了就把「听写期间常亮」还回去；用户自己开了常亮就保持 */
        if (autoKeepOn) {
            autoKeepOn = false;
            if (!keeponOn()) getWindow().clearFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        }
        /* 手按着说话时（不是自动听写）别把人家的录音收掉 */
        if (!autoRec && discard) return;
        boolean was = autoRec;
        if (rec == null) {
            autoRec = false;
            if (was && discard && !quiet) js("window.__autoListen && window.__autoListen('idle','')");
            return;
        }
        if (discard) {
            autoRec = false;
            cancelRecCap();
            try { rec.stop(); } catch (Throwable ignored) { }
            try { rec.release(); } catch (Throwable ignored) { }
            rec = null;
            if (!quiet) js("window.__autoListen && window.__autoListen('idle','')");
            return;
        }
        stopNativeRec();     /* 里面会把 autoRec 清掉并通知页面 uploading */
    }

    /** 音频 base64 分块经 evaluateJavascript 递给页面（__pttAudioChunk），完毕 __pttGo 由页面上传 */
    private void uploadViaPage(final String path, final long ms, final boolean auto) {
        new Thread(new Runnable() {
            @Override public void run() {
                try {
                    byte[] data = readAll(new File(path));
                    String b64 = Base64.encodeToString(data, Base64.NO_WRAP);
                    final int chunk = 65536;
                    final int total = (b64.length() + chunk - 1) / chunk;
                    int idx = 0;
                    for (int i = 0; i < total; i++) {
                        final String part = b64.substring(idx, Math.min(idx + chunk, b64.length()));
                        final int n = i;
                        runOnUiThread(new Runnable() { @Override public void run() {
                            js("window.__pttAudioChunk&&window.__pttAudioChunk(" + n + "," + total + ",\"" + part + "\")");
                        }});
                        idx += chunk;
                    }
                    runOnUiThread(new Runnable() { @Override public void run() {
                        js("window.__pttGo&&window.__pttGo(" + ms + "," + (auto ? 1 : 0) + ")");
                    }});
                } catch (final Throwable t) {
                    runOnUiThread(new Runnable() { @Override public void run() {
                        String m = esc(t.getClass().getSimpleName() + ": " + t.getMessage());
                        if (auto) js("window.__autoResult && window.__autoResult("
                                + quote("{\"ok\":false,\"error\":\"读取音频失败\"}") + ")");
                        else js("window.__pttError && window.__pttError('读取音频失败: " + m + "')");
                    }});
                }
            }
        }).start();
    }

    /** 必须在主线程调用（web.getUrl() 是 WebView 方法） */
    private String baseUrl() {
        String page = null;
        try { page = (web == null) ? null : web.getUrl(); } catch (Throwable ignored) { }
        if (page == null || page.length() == 0) page = lastUrl;
        try {
            URL u = new URL(page);
            int port = u.getPort();
            return u.getProtocol() + "://" + u.getHost() + (port > 0 ? ":" + port : "");
        } catch (Throwable t) {
            return "http://localhost:8791";
        }
    }

    /** 把 m4a 发给 Mac 上的反代 /api/ptt，反代做 ASR 后回 JSON。
        auto=true：结果交给页面的自动听写状态机（__autoResult），不走普通填框 */
    private void upload(final String path, final long ms, final String base, final String cookie,
                        final boolean auto) {
        new Thread(new Runnable() {
            @Override public void run() {
                String resp = "";
                try {
                    byte[] data = readAll(new File(path));
                    HttpURLConnection c = conn(base + "/api/ptt");
                    c.setRequestMethod("POST");
                    c.setDoOutput(true);
                    c.setConnectTimeout(8000);
                    c.setReadTimeout(90000);
                    c.setRequestProperty("Content-Type", "audio/mp4");
                    c.setRequestProperty("X-Dur-Ms", String.valueOf(ms));
                    if (cookie != null && cookie.length() > 0) {
                        c.setRequestProperty("Cookie", cookie);   // 中继靠它选机器
                    }
                    c.setFixedLengthStreamingMode(data.length);
                    c.getOutputStream().write(data);
                    c.getOutputStream().flush();
                    c.getOutputStream().close();

                    int code = c.getResponseCode();
                    InputStream in = code >= 400 ? c.getErrorStream() : c.getInputStream();
                    resp = readText(in);
                    if (code >= 400 && resp.length() == 0) {
                        resp = "{\"ok\":false,\"error\":\"HTTP " + code + "\"}";
                    }
                } catch (Throwable t) {
                    resp = "{\"ok\":false,\"error\":\"" + esc(t.getClass().getSimpleName() + ": " + t.getMessage()) + "\"}";
                }
                final String r = resp;
                runOnUiThread(new Runnable() {
                    @Override public void run() {
                        String cb = auto ? "__autoResult" : "__pttResult";
                        js("window." + cb + " && window." + cb + "(" + quote(r) + ")");
                    }
                });
            }
        }).start();
    }

    private static String quote(String s) {
        StringBuilder b = new StringBuilder("\"");
        for (int i = 0; i < s.length(); i++) {
            char ch = s.charAt(i);
            switch (ch) {
                case '"':  b.append("\\\""); break;
                case '\\': b.append("\\\\"); break;
                case '\n': b.append("\\n"); break;
                case '\r': b.append("\\r"); break;
                case '\t': b.append("\\t"); break;
                default:
                    if (ch < 0x20 || ch > 0x7e) b.append(String.format("\\u%04x", (int) ch));
                    else b.append(ch);
            }
        }
        return b.append("\"").toString();
    }

    private static String esc(String s) {
        if (s == null) return "";
        return s.replace("\\", "/").replace("'", "").replace("\"", "").replace("\n", " ");
    }

    private static byte[] readAll(File f) throws Exception {
        ByteArrayOutputStream bo = new ByteArrayOutputStream();
        FileInputStream in = new FileInputStream(f);
        byte[] buf = new byte[8192];
        int n;
        while ((n = in.read(buf)) > 0) bo.write(buf, 0, n);
        in.close();
        return bo.toByteArray();
    }

    private static String readText(InputStream in) throws Exception {
        if (in == null) return "";
        ByteArrayOutputStream bo = new ByteArrayOutputStream();
        byte[] buf = new byte[4096];
        int n;
        while ((n = in.read(buf)) > 0) bo.write(buf, 0, n);
        in.close();
        return new String(bo.toByteArray(), "UTF-8");
    }

    /* ── 物理键分工（0.6.8）──
       音量上键 = 说话：按住模式下按住说、松手停；点按模式下按一下开始、再按一下结束。
       音量下键 = 发送：按一下就把输入框里的内容发出去，和上键完全不重叠。
       旧的"双击发送 / 长按发送"已删：它们和点按录音的节奏撞车（说完再按一下结束，
       紧接的那次按下会被当成双击/长按），这次按你的要求彻底让两个键各管一件事。 */
    /* 按键派发第一站：输入框聚焦时 WebView 会把音量键吃掉/漏给系统调音量，
       Activity.onKeyDown 收不到——必须抢在视图树之前拦截音量键 */
    @Override
    public boolean dispatchKeyEvent(KeyEvent event) {
        if (isPttKey(event.getKeyCode())) return pttKeyEvent(event);
        return super.dispatchKeyEvent(event);
    }

    /** 音量键统一入口（Activity 与 WebView 两层共用）：DOWN 录音/发送，UP 收尾 */
    private boolean pttKeyEvent(KeyEvent event) {
        if (event.getAction() == KeyEvent.ACTION_DOWN) {
            onKeyDown(event.getKeyCode(), event);
        } else {
            onKeyUp(event.getKeyCode(), event);
        }
        return true;
    }

    /* 输入框聚焦时音量键"回到系统"（开音量条、不录音）。根因：输入法把输入框升成
       全屏编辑窗（extract / fullscreen IME UI）后，那个编辑窗自己吃硬件键，
       Activity 派发链压根收不到。两处一起防：
       ① 告诉输入法别升全屏编辑窗（IME_FLAG_NO_EXTRACT_UI / NO_FULLSCREEN）；
       ② 视图层再兜一次（onKeyPreIme / dispatchKeyEvent），事件若从 WebView 下来也能接住。 */
    private final class CBWebView extends WebView {
        CBWebView(android.content.Context c) { super(c); }

        @Override
        public InputConnection onCreateInputConnection(EditorInfo out) {
            InputConnection ic = super.onCreateInputConnection(out);
            if (out != null) {
                out.imeOptions |= EditorInfo.IME_FLAG_NO_EXTRACT_UI | EditorInfo.IME_FLAG_NO_FULLSCREEN;
            }
            return ic;
        }

        @Override
        public boolean onKeyPreIme(int keyCode, KeyEvent event) {
            if (isPttKey(keyCode)) return pttKeyEvent(event);
            return super.onKeyPreIme(keyCode, event);
        }

        @Override
        public boolean dispatchKeyEvent(KeyEvent event) {
            if (isPttKey(event.getKeyCode())) return pttKeyEvent(event);
            return super.dispatchKeyEvent(event);
        }
    }

    @Override
    public boolean onKeyDown(int keyCode, KeyEvent event) {
        if (isPttKey(keyCode)) {
            handleKey(keyCode, event.getRepeatCount());
            return true;
        }
        if (keyCode == KeyEvent.KEYCODE_BACK || keyCode == KeyEvent.KEYCODE_MENU) {
            return true;   // 不许退出
        }
        return super.onKeyDown(keyCode, event);
    }

    /** 真正的按键语义（Activity 与 CBWebView 两层共用同一条路） */
    private void handleKey(int keyCode, int repeatCount) {
        if (repeatCount > 0) return;                    // 长按的重复事件丢掉，只认第一下

        if (keyCode == KeyEvent.KEYCODE_VOLUME_DOWN) {
            /* 录音中不抢（那一键该留给"结束录音"的上键） */
            if (rec != null) {
                Log.i(TAG, "keyDown VOL_DOWN rec=1 -> 录音中，忽略");
                return;
            }
            if (volPressed) return;                 // 按住时系统的重复事件
            volPressed = true;
            volLong = false;
            volHold = new Runnable() { @Override public void run() {
                volHold = null;
                if (!volPressed) return;
                volLong = true;                     // 松手时别再发出去
                Log.i(TAG, "keyDown VOL_DOWN long -> mute toggle");
                js("window.__cbMuteToggle && window.__cbMuteToggle()");
            } };
            ui.postDelayed(volHold, VOL_LONG_MS);
            return;
        }

        /* 剩下的就是音量上键：说话 */
        Log.i(TAG, "keyDown VOL_UP repeat=" + repeatCount
                + " mode=" + mode() + " rec=" + (rec != null));
        if ("toggle".equals(mode())) {
            if (rec == null) startNativeRec();   // 空闲：开始录
            else stopNativeRec();                // 录音中：再按一下结束
        } else {
            startNativeRec();                    // 按住说话
        }
    }

    /* ── 键盘弹起时别把输入框盖住 ──
       现象：点 #txt 唤起输入法后，键盘直接压在页面底部（#txt 就在底部 fixed 条里），
       看不到自己正在打的字。
       原因：本壳是全屏沉浸（LAYOUT_FULLSCREEN / HIDE_NAVIGATION），系统默认按
       adjustPan 处理，只把窗口整体往上推一点，不会给页面让出键盘的高度。
       做法：
         1) 清单里显式声明 adjustResize，先禁掉"整页上推"这套系统默认行为（它推不到底）。
         2) 再用 getWindowVisibleDisplayFrame 量出键盘占掉的高度，**把整个 WebView 上移同样的高度**。
            不压缩高度：压缩会让页面重新布局，会话/终端的底部被裁掉（用户实测反馈），
            平移只是挪位置，布局尺寸一点没变，输入框刚好停在键盘上沿。
       这样页面里的 position:fixed;bottom:0（#bar/#txt）自然落在键盘上方。 */
    private int lastShiftY = 0;   /* 键盘弹出时整页上移的位移（0 = 没键盘） */
    /* 当前是不是"会话内容页"（页面里有 #deck）。登录页/列表页输入框都在上半屏，
       整页上推反而把输入框顶出屏幕 —— 这个判定照搬自 vibecoding-terminal 的新壳。 */
    private volatile boolean sessPage = false;

    /* ── 网络：断网 / 换网络后自动恢复 ─────────────────────────────
       手机是在移动网络里跑的：WiFi 断开切移动数据、没信号又回到 WiFi、开热点、进电梯……
       页面一旦加载失败，WebView 会一直停在错误页上，不会自己回来。
       做法：
         1) 失败立刻换成**我们自己的离线页**（assets/offline.html），
            因为 WebView 自带的「网页无法打开」会把完整 URL（域名/端口/会话 id/token）打在屏幕上；
         2) 监听网络恢复（WiFi / 移动数据 / 以太网），一恢复马上重载；
         3) 失败时按 3s→6s→12s→30s 退避重试。
       权限：INTERNET 本身就同时覆盖 WiFi 与移动数据，不需要额外申请"联网权限"；
             ACCESS_NETWORK_STATE 只是让我们能"知道"网络变了。 */
    private static final String OFFLINE_PREFIX = "file:///android_asset/offline";
    private static final String OFFLINE_URL = "file:///android_asset/offline.html";
    private boolean netFail = false;
    private boolean pageErr = false;   /* 这一趟加载是否失败（onReceivedError 置位，onPageStarted 复位） */
    private boolean offlineShown = false;
    private String lastUrl;                 /* onCreate 里给默认；重试只用它，界面上一个字符都不露 */
    private int retryMs = 3000;
    private int probeFails = 0;   /* Java 探测连续失败次数（UI 线程上读写） */
    private Runnable retryRun;
    private int netGen = 0;   /* 网络拓扑代数：每次可用/断开都 +1，页面据此感知「换网了」 */

    /** 现在这台手机有没有可用的网络（系统「已连接」口径，和离线页的 online 字段一致）。
        判断不了就当有网：宁可让请求自己失败，也别把人误扔到离线页。 */
    private boolean netOnline() {
        try {
            ConnectivityManager cm = (ConnectivityManager) getSystemService(CONNECTIVITY_SERVICE);
            android.net.NetworkInfo ni = cm == null ? null : cm.getActiveNetworkInfo();
            return ni != null && ni.isConnected();
        } catch (Throwable t) {
            return true;
        }
    }

    /* 断网后过一会儿再跳离线页：切 Wi-Fi/进电梯的瞬断不该把正在看的内容顶走。
       定时器在下一次加载成功、网络恢复、或已经显示离线页时取消。 */
    private static final int OFFLINE_AFTER_LOST_MS = 6000;
    private Runnable offlineSoon;

    private void cancelOfflineSoon() {
        if (offlineSoon != null) { ui.removeCallbacks(offlineSoon); offlineSoon = null; }
    }

    /** 问一次页面：输入框是不是空的（真的没在打字）。页面死了/还没起来、或 1.5 秒没回话，
        一律按「空」处理——页面没响应的时候更该切网络页。两个回调都在 UI 线程跑。 */
    private void whenDraftIdle(final Runnable idle, final Runnable busy) {
        final boolean[] done = {false};
        final Runnable sure = new Runnable() {
            @Override public void run() {
                if (done[0]) return;
                done[0] = true;
                idle.run();
            }
        };
        ui.postDelayed(sure, 1500);
        if (web == null) { ui.removeCallbacks(sure); sure.run(); return; }
        try {
            web.evaluateJavascript(
                "(function(){try{return (window.hasDraft&&hasDraft())?'1':'0'}catch(e){return '0'}})()",
                new android.webkit.ValueCallback<String>() {
                    @Override public void onReceiveValue(String v) {
                        if (done[0]) return;
                        done[0] = true;
                        ui.removeCallbacks(sure);
                        if (v != null && v.contains("1")) busy.run(); else idle.run();
                    }
                });
        } catch (Throwable t) {
            ui.removeCallbacks(sure);
            sure.run();
        }
    }

    private void scheduleOfflineSoon() {
        if (offlineShown || offlineSoon != null) return;
        offlineSoon = new Runnable() {
            @Override public void run() {
                offlineSoon = null;
                /* 人在后台时不抽页面：回来时 onResume 会重新排；已经在离线页/网络回来了也撤 */
                if (!VISIBLE || offlineShown || netOnline()) return;
                /* 正在录音时不抽：话说到一半被切走，这条语音就废了 —— 等录完再说 */
                if (rec != null) { scheduleOfflineSoon(); return; }
                /* 输入框里还有没发出去的字：先别抽页面（页面自己也会拦这一步，
                   壳这边再确认一次 —— 草稿虽能找回，但把人正打的字顶掉很烦） */
                whenDraftIdle(new Runnable() {
                    @Override public void run() {
                        /* 问页面的这一两秒里情况可能又变了：再核一次 */
                        if (!VISIBLE || offlineShown || netOnline()) return;
                        Log.i(TAG, "断网超过 " + OFFLINE_AFTER_LOST_MS + "ms，切离线页");
                        showOffline();
                    }
                }, new Runnable() {
                    @Override public void run() {
                        Log.i(TAG, "输入框里还有草稿，这一轮先不切");
                        scheduleOfflineSoon();
                    }
                });
            }
        };
        ui.postDelayed(offlineSoon, OFFLINE_AFTER_LOST_MS);
    }

    private void showOffline() {
        cancelOfflineSoon();
        if (!offlineShown) {
            offlineShown = true;
            Log.i(TAG, "显示离线页");
            /* web 可能正被销毁/重建（渲染进程崩溃、teardown 后残留的回调）：
               2026-11-18 实测 NPE 崩溃。null 就等重试循环里再抽。 */
            try { if (web != null) web.loadUrl(OFFLINE_URL); } catch (Throwable ignored) { }
        }
        scheduleReload();
    }

    private void scheduleReload() {
        if (retryRun != null) return;
        final int wait = retryMs;
        retryRun = new Runnable() {
            @Override public void run() {
                retryRun = null;
                Log.i(TAG, "离线，探测（本次间隔 " + wait + "ms）");
                probeAndReload();
            }
        };
        ui.postDelayed(retryRun, wait);
        retryMs = Math.min(retryMs * 2, 30000);
    }

    /** 后台探一探目标地址通不通；通了才让 WebView 去加载。
        直接 reload 的话，离线页每 3 秒会被顶掉重来一次（闪一下），体验很差。
        坚果 OD105 实测坑：网络正常时 Java 层 HttpURLConnection 也会**瞬败**
        （14ms 就抛异常，ROM 的进程内 DNS/网络绑定坏了），而 WebView 自己的
        网络栈一切正常 —— 曾经因此永远卡在离线页。所以：探测失败要打出异常；
        连败 3 次后不再信 Java，让 WebView 直接去加载目标页；真断网时加载
        失败走 onReceivedError -> showOffline()，退避循环照旧。 */
    private void probeAndReload() {
        new Thread(new Runnable() {
            @Override public void run() {
                boolean ok = false;
                String why0 = null;
                HttpURLConnection c = null;
                try {
                    c = conn(lastUrl);
                    c.setConnectTimeout(3000);
                    c.setReadTimeout(3000);
                    c.setRequestMethod("GET");
                    c.setInstanceFollowRedirects(false);
                    /* 5xx ≠ 通：2026-11-18 手机路径上的代理对黑洞连接回真 HTTP 504，
                       「有响应就算通」→ 通过 → WebView 加载 → 504 → 离线页 → 死循环。
                       2xx/3xx/4xx 是目标服务器的应答（链路通）；5xx = 路上有东西替它
                       答话或服务端真病了，一律按不通继续退避探测。 */
                    int code = c.getResponseCode();
                    ok = code > 0 && code < 500;
                } catch (Throwable e) {
                    why0 = e.getClass().getSimpleName() + ": " + e.getMessage();
                } finally {
                    if (c != null) {
                        try { c.disconnect(); } catch (Throwable ignored) { }
                    }
                }
                final boolean good = ok;
                final String why = why0;
                ui.post(new Runnable() {
                    @Override public void run() {
                        if (good) {
                            Log.i(TAG, "探测通过 -> 重载页面");
                            probeFails = 0;
                            retryMs = 3000;
                            netFail = false;
                            offlineShown = false;
                            try { if (web != null) web.loadUrl(lastUrl); } catch (Throwable ignored) { }
                        } else {
                            probeFails++;
                            if (probeFails >= 3) {
                                /* Java 网络栈可能自身故障（坚果实测瞬败），换 WebView 直试 */
                                Log.i(TAG, "Java 探测连败 3 次(" + why + ")，WebView 直试");
                                probeFails = 0;
                                retryMs = 3000;
                                netFail = false;
                                offlineShown = false;
                                try { if (web != null) web.loadUrl(lastUrl); } catch (Throwable ignored) { }
                            } else {
                                Log.i(TAG, "探测失败(" + why + "，第 " + probeFails + " 次)，退避");
                                scheduleReload();
                            }
                        }
                    }
                });
            }
        }).start();
    }

    private void setupNetworkWatch() {
        try {
            lastNetSig = netSig();   /* 先记基线：注册回调后第一趟不算「换网」 */
            final ConnectivityManager cm = (ConnectivityManager) getSystemService(CONNECTIVITY_SERVICE);
            if (cm == null) return;
            NetworkRequest req = new NetworkRequest.Builder()
                    .addCapability(NetworkCapabilities.NET_CAPABILITY_INTERNET).build();
            cm.registerNetworkCallback(req, new ConnectivityManager.NetworkCallback() {
                @Override public void onAvailable(Network n) {
                    Log.i(TAG, "网络可用: " + netName(cm, n));
                    ui.post(new Runnable() {
                        @Override public void run() {
                            cancelOfflineSoon();             /* 回来了：别再多此一举跳离线页 */
                            bumpNetGen();                    /* 不管页面好坏都广播：页面自己决定要不要重连 */
                            pushNetState();
                            if (!netFail) return;            /* 页面本来好好的，不走重载这一趟 */
                            netFail = false;
                            retryMs = 3000;
                            if (retryRun != null) { ui.removeCallbacks(retryRun); retryRun = null; }
                            Log.i(TAG, "网络恢复 -> 探测后重载");
                            probeAndReload();
                        }
                    });
                }
                @Override public void onLost(Network n) {
                    Log.i(TAG, "网络断开: " + netName(cm, n));
                    netFail = true;
                    ui.post(new Runnable() {
                        @Override public void run() {
                            bumpNetGen();
                            pushNetState();   /* 让活着的页面立刻知道断网/换网，别等推流超时 */
                            /* 页面还在屏幕上（只是没网）：过 6 秒还连不上就换成网络页，
                               别让人对着一堆点不动的按钮干等。期间网络回来会取消。 */
                            scheduleOfflineSoon();
                        }
                    });
                }
            });
        } catch (Throwable t) {
            Log.w(TAG, "网络监听注册失败: " + t);
        }
    }

    private static String netName(ConnectivityManager cm, Network n) {
        try {
            NetworkCapabilities c = cm.getNetworkCapabilities(n);
            if (c == null) return String.valueOf(n);
            if (c.hasTransport(NetworkCapabilities.TRANSPORT_WIFI)) return "WiFi";
            if (c.hasTransport(NetworkCapabilities.TRANSPORT_CELLULAR)) return "移动数据";
            if (c.hasTransport(NetworkCapabilities.TRANSPORT_ETHERNET)) return "以太网";
            return String.valueOf(n);
        } catch (Throwable t) {
            return String.valueOf(n);
        }
    }

    private int dp(float v) {
        DisplayMetrics m = getResources().getDisplayMetrics();
        return Math.round(v * m.density);
    }

    private void setupImeResize() {
        final View rootV = web.getRootView();
        rootV.getViewTreeObserver().addOnGlobalLayoutListener(new ViewTreeObserver.OnGlobalLayoutListener() {
            @Override
            public void onGlobalLayout() {
                applyImeResize(rootV);
            }
        });
    }

    private void applyImeResize(View rootV) {
        Rect r = new Rect();
        web.getWindowVisibleDisplayFrame(r);
        int visH = r.bottom - r.top;
        int rootH = rootV.getHeight();
        if (visH <= 0 || rootH <= 0) return;

        int hidden = rootH - visH;
        /* 状态栏/导航栏远小于 dp(130)；超过 85% 屏高必然是量出来的荒唐值（实测报过 968px），
           照它平移会把整页推出屏幕。 */
        boolean ime = hidden > dp(130) && hidden < rootH * 0.85;

        /* 会话内容页（onPageFinished 里按 #deck 判定）整页上推。
           登录/列表页不能整页推（输入框在上半屏，一推就顶出屏幕），但也不能不动：
           键盘弹起时窗口不缩，下半屏的密码框直接被键盘盖住、页面又没有滚动余量。
           这里改成按缺口推——只把当前焦点输入框露到键盘上方那么多。 */
        if (!sessPage) {
            if (!ime) { resetImeShift(); return; }
            shiftToFocus(visH, hidden);
            return;
        }

        /* 整页上推，而不是压缩：高度保持铺满屏幕，只把整个 WebView 往上平移键盘那么高。
           压缩会让页面重新布局——会话/终端的底部被裁掉；平移不动布局，输入框刚好露在键盘上沿。
           系统若自己 resize 过（visibleFrame 已经变小），hidden≈0 → 位移 0，不会双重抬高。
           上推的代价是 fixed 顶栏一起被推出屏幕——把位移量同步给页面（__imeShift），
           页面用 CSS 变量 --ime 把 header 单独反向平移回屏幕顶；底栏/快捷键仍贴键盘上沿。 */
        int target = ime ? -hidden : 0;
        if (target == lastShiftY) return;
        lastShiftY = target;
        web.setTranslationY(target);
        /* 单位别混：setTranslationY 吃物理像素，页面里 --ime / translateY 吃 CSS 像素。
           两倍/三倍屏上直接把 hidden 塞给页面，顶栏会被按 2~3 倍高度往下推（高一点的键盘
           还会撞上页面侧 75% 的荒唐值夹逼、被抹成 0 → 顶栏跟着整页跑出屏幕）。 */
        int cssShift = Math.round((-target) / getResources().getDisplayMetrics().density);
        js("window.__imeShift&&window.__imeShift(" + cssShift + ")");
        Log.i(TAG, "ime hidden=" + hidden + " visH=" + visH + " rootH=" + rootH
                + " -> shiftY=" + target + " css=" + cssShift);
    }

    private void resetImeShift() {
        if (lastShiftY == 0) return;
        lastShiftY = 0;
        web.setTranslationY(0);
        js("window.__imeShift&&window.__imeShift(0)");   // 顶栏补偿位移一起归零（会话页用）
    }

    /* 非会话页：问页面当前焦点输入框在屏幕上的底边（换算成物理像素），
       被键盘盖住多少就把整页往上推多少，最多推到键盘高度、最少 0。
       rect 是布局视口坐标，键盘引起 Chrome 自己滚屏时用 visualViewport.offsetTop 扣掉。
       读的 rect 不受 setTranslationY 影响（页面不知道自己在被平移），不会来回震荡。 */
    private void shiftToFocus(final int visH, final int hidden) {
        web.evaluateJavascript(
            "(function(){var e=document.activeElement;if(!e)return '';"
          + "var t=(e.tagName||'').toUpperCase();"
          + "if(t!=='INPUT'&&t!=='TEXTAREA'&&!e.isContentEditable)return '';"
          + "var r=e.getBoundingClientRect();"
          + "var o=(window.visualViewport?window.visualViewport.offsetTop:0)||0;"
          + "return (r.bottom-o);})()",
            new android.webkit.ValueCallback<String>() {
                @Override public void onReceiveValue(String v) {
                    int target = 0;
                    try {
                        String s = (v == null ? "" : v.trim()).replace("\"", "");
                        if (s.length() > 0 && !"null".equals(s)) {
                            float bottomPx = Float.parseFloat(s) * getResources().getDisplayMetrics().density;
                            int need = Math.round(bottomPx) + dp(24) - visH;   // 留 24dp 边
                            if (need > 0) target = -Math.min(need, hidden);
                        }
                    } catch (Throwable ignored) { }
                    if (target == lastShiftY) return;
                    lastShiftY = target;
                    if (web != null) web.setTranslationY(target);
                    Log.i(TAG, "ime(page) shiftY=" + target + " visH=" + visH + " hidden=" + hidden);
                }
            });
    }

    private static boolean isPttKey(int k) {
        return k == KeyEvent.KEYCODE_VOLUME_DOWN || k == KeyEvent.KEYCODE_VOLUME_UP;
    }

    /** 音量下键松手：已经长按切过静音就收工，否则按一次 = 发送 */
    private void volDownUp() {
        if (!volPressed) return;
        boolean wasLong = volLong;
        volPressed = false;
        volLong = false;
        if (volHold != null) { ui.removeCallbacks(volHold); volHold = null; }
        if (wasLong) {
            Log.i(TAG, "keyUp VOL_DOWN -> 长按已处理（静音），不发送");
            return;
        }
        if (rec != null) {
            Log.i(TAG, "keyUp VOL_DOWN rec=1 -> 录音中，发送忽略");
            return;
        }
        long now = System.currentTimeMillis();
        if (now - lastSendAt < 400) {
            Log.i(TAG, "keyUp VOL_DOWN -> 400ms 内连按，忽略");
            return;
        }
        lastSendAt = now;
        Log.i(TAG, "keyUp VOL_DOWN -> send");
        js("window.__cbSend && window.__cbSend(true)");
    }

    @Override
    public boolean onKeyUp(int keyCode, KeyEvent event) {
        if (keyCode == KeyEvent.KEYCODE_VOLUME_DOWN) {
            volDownUp();
            return true;
        }
        if (keyCode == KeyEvent.KEYCODE_VOLUME_UP) {
            if ("hold".equals(mode()) && rec != null) stopNativeRec();
            return true;
        }
        return super.onKeyUp(keyCode, event);
    }

    @Override
    public void onBackPressed() {
        /* 全屏视频优先：返回键 = 收起全屏，回到会话墙（kiosk 平时整个吞掉返回键）。
           锁任务下系统可能不投递——收不到还有播放器自己的收起键。 */
        if (fsView != null) { hideFs(); return; }
        // 整个吞掉，不退出
    }

    /* ══ [codeboss] 客户端 APK 下载 + 自更新 ════════════════════════════
       host 出 /codeboss.apk 下载与 /api/apk_ver 版本接口（token/cookie 保护）。
       启动 8s 首查，之后每 4h：远端 code > 已装 code 且 > 已提示过的 code
       → toast 告知 → DownloadManager 后台下载 → 下载完成广播 → 弹系统安装。 */

    private Runnable apkBeat;
    private android.content.BroadcastReceiver apkRc;
    private android.content.BroadcastReceiver apkRc2;
    /* 下载进度回显 + 卡死自愈：点「更新 App」后到安装界面要等下载（手机上几百 KB
       也可能十几秒），什么都不显示就像「点了没反应」。这里每 0.7s 把百分比推给页面，
       顺便盯着字节数：12s 一个字节没动就换个 id 重下一次（DownloadManager 偶发卡死）。 */
    private volatile long updDlT0 = 0;
    private long updDlLastBytes = -1;
    private int updDlStall = 0;
    private boolean updDlRetried = false;
    private final Runnable dlProg = new Runnable() {
        @Override public void run() {
            long id = updDlId;
            if (id <= 0) return;
            long done = -1, total = -1;
            int st = -1;
            try {
                android.database.Cursor c = ((android.app.DownloadManager)
                        getSystemService(DOWNLOAD_SERVICE)).query(
                        new android.app.DownloadManager.Query().setFilterById(id));
                if (c != null) {
                    try {
                        if (c.moveToFirst()) {
                            int di = c.getColumnIndex(
                                    android.app.DownloadManager.COLUMN_BYTES_DOWNLOADED_SO_FAR);
                            int ti = c.getColumnIndex(
                                    android.app.DownloadManager.COLUMN_TOTAL_SIZE_BYTES);
                            int si = c.getColumnIndex(android.app.DownloadManager.COLUMN_STATUS);
                            if (di >= 0) done = c.getLong(di);
                            if (ti >= 0) total = c.getLong(ti);
                            if (si >= 0) st = c.getInt(si);
                        }
                    } finally { c.close(); }
                }
            } catch (Throwable ignored) { }
            if (st == android.app.DownloadManager.STATUS_SUCCESSFUL) {
                js("typeof __cbDlProg==='function'&&__cbDlProg(100,-1,-1)");
                return;
            }
            if (st == android.app.DownloadManager.STATUS_FAILED || st < 0) {
                js("typeof __cbDlProg==='function'&&__cbDlProg(-2,-1,-1)");
                return;
            }
            /* 卡死：一直是 0 字节且超过 12s → 重下一轮（只重试一次） */
            if (done == updDlLastBytes && done >= 0) updDlStall++;
            else updDlStall = 0;
            updDlLastBytes = done;
            if (updDlStall >= 17 && done <= 0 && !updDlRetried) {
                updDlRetried = true;
                Log.w(TAG, "下载 12s 没动 → 重新下一轮 id=" + id);
                Relaunch.trace(MainActivity.this, "下载卡住 12s，重下");
                try {
                    ((android.app.DownloadManager) getSystemService(DOWNLOAD_SERVICE)).remove(id);
                } catch (Throwable ignored) { }
                updDlId = 0;
                js("typeof __cbDlProg==='function'&&__cbDlProg(-3,-1,-1)");
                checkApkUpdate(true);
                return;
            }
            int pct = (total > 0 && done >= 0) ? (int) (done * 100 / total) : -1;
            js("typeof __cbDlProg==='function'&&__cbDlProg(" + pct + "," + done + "," + total + ")");
            ui.postDelayed(this, 700);
        }
    };
    private volatile long updDlId = 0;

    private void startDlProg(long id) {
        updDlId = id;
        updDlLastBytes = -1;
        updDlStall = 0;
        ui.removeCallbacks(dlProg);
        ui.postDelayed(dlProg, 500);
    }

    /* ── 护眼/压暗悬浮层实现（见桥 setEye/eyeSys/setDim）────────────────── */
    private android.view.View eyeOv, dimOv;

    private boolean overlayPerm() {
        try { return android.provider.Settings.canDrawOverlays(this); }
        catch (Throwable t) { return false; }
    }

    /** 摘掉重挂两张悬浮层（幂等）。无权限/未开启则只摘不挂 */
    private void applyOverlays() {
        removeOverlays();
        if (!overlayPerm()) return;
        WindowManager wm = (WindowManager) getSystemService(WINDOW_SERVICE);
        if (eyeOn()) {
            try { eyeOv = addOverlay(wm, 0x21FF9329); } catch (Throwable t) { Log.w(TAG, "eyeOv: " + t); }
        }
        int da = getSharedPreferences(PREFS, MODE_PRIVATE).getInt("dimA", 0);
        if (da > 0) {
            try { dimOv = addOverlay(wm, (da << 24) & 0xFF000000); } catch (Throwable t) { Log.w(TAG, "dimOv: " + t); }
        }
    }

    private android.view.View addOverlay(WindowManager wm, int color) {
        android.widget.FrameLayout v = new android.widget.FrameLayout(this);
        v.setBackgroundColor(color);
        int type = android.os.Build.VERSION.SDK_INT >= 26
                ? WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY
                : WindowManager.LayoutParams.TYPE_PHONE;   /* API 24-25 */
        WindowManager.LayoutParams lp = new WindowManager.LayoutParams(
                WindowManager.LayoutParams.MATCH_PARENT, WindowManager.LayoutParams.MATCH_PARENT,
                type,
                WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE
                        | WindowManager.LayoutParams.FLAG_NOT_TOUCHABLE
                        | WindowManager.LayoutParams.FLAG_LAYOUT_IN_SCREEN
                        | WindowManager.LayoutParams.FLAG_LAYOUT_NO_LIMITS,
                android.graphics.PixelFormat.TRANSLUCENT);
        wm.addView(v, lp);
        return v;
    }

    private void removeOverlays() {
        WindowManager wm = (WindowManager) getSystemService(WINDOW_SERVICE);
        if (eyeOv != null) { try { wm.removeView(eyeOv); } catch (Throwable ignored) { } eyeOv = null; }
        if (dimOv != null) { try { wm.removeView(dimOv); } catch (Throwable ignored) { } dimOv = null; }
    }

    private int installedCode() {
        try {
            return getPackageManager().getPackageInfo(getPackageName(), 0).versionCode;
        } catch (Throwable t) { return 0; }
    }

    private void setupApkUpdate() {
        apkRc = new android.content.BroadcastReceiver() {
            @Override public void onReceive(android.content.Context c, android.content.Intent in) {
                long id = in == null ? -1 : in.getLongExtra(
                        android.app.DownloadManager.EXTRA_DOWNLOAD_ID, -1);
                Log.i(TAG, "下载完成 id=" + id);
                if (updDlT0 > 0)
                    Relaunch.trace(MainActivity.this,
                            "下载完成 用时" + ((System.currentTimeMillis() - updDlT0) / 1000) + "s");
                promptInstall(id);
            }
        };
        android.content.IntentFilter f =
                new android.content.IntentFilter(android.app.DownloadManager.ACTION_DOWNLOAD_COMPLETE);
        if (android.os.Build.VERSION.SDK_INT >= 33) {
            registerReceiver(apkRc, f, android.content.Context.RECEIVER_NOT_EXPORTED);
        } else {
            registerReceiver(apkRc, f);
        }

        /* PackageInstaller 安装状态：确认框转发（PENDING_USER_ACTION 带的 intent 必须由
           我们 startActivity 才弹）+ 成败记录 + 看门狗改期。
           注意：会话提交时用的是**显式广播**（component 写死 UpdateStatusReceiver），
           显式广播不会送到这里的动态接收器——主链路是静态接收器把 intent 转交给
           onInstallStatus；这个动态接收器留着兜 OEM 把广播改成隐式的情况。 */
        apkRc2 = new android.content.BroadcastReceiver() {
            @Override public void onReceive(android.content.Context c, android.content.Intent in) {
                Relaunch.trace(MainActivity.this, "动态收到安装状态");
                onInstallStatus(in);
            }
        };
        android.content.IntentFilter fi =
                new android.content.IntentFilter("com.agentboss.terminal.INSTALL_STATUS");
        if (android.os.Build.VERSION.SDK_INT >= 33) {
            registerReceiver(apkRc2, fi, android.content.Context.RECEIVER_NOT_EXPORTED);
        } else {
            registerReceiver(apkRc2, fi);
        }

        apkBeat = new Runnable() {
            int round = 0;
            @Override public void run() {
                try { checkApkUpdate(); } catch (Throwable t) { Log.w(TAG, "apk check: " + t); }
                ui.postDelayed(this, (round++ == 0) ? 8000 : 4 * 3600 * 1000L);
            }
        };
        ui.post(apkBeat);
    }

    private void checkApkUpdate() { checkApkUpdate(false); }

    private void checkApkUpdate(final boolean force) {
        final String base = baseUrl();                     /* 主线程算好（WebView 方法） */
        String ck0 = null;
        try { ck0 = android.webkit.CookieManager.getInstance().getCookie(base); } catch (Throwable ignored) { }
        checkApkUpdate(force, base, ck0);
    }

    private org.json.JSONObject verJson(String base, String cookie) {
        try {
            /* ?product=…：中继按产品线答包（base 是纯 origin，query 只在此一处拼） */
            java.net.HttpURLConnection c = conn(base + "/api/apk_ver?product=" + product());
            c.setConnectTimeout(8000);
            c.setReadTimeout(15000);
            if (cookie != null && cookie.length() > 0) c.setRequestProperty("Cookie", cookie);
            int code = c.getResponseCode();
            String body = readText(code >= 400 ? c.getErrorStream() : c.getInputStream());
            if (code >= 400) { Log.w(TAG, "apk_ver " + base + " HTTP " + code + " " + body.substring(0, Math.min(120, body.length()))); return null; }
            return new org.json.JSONObject(body);
        } catch (Throwable t) {
            Log.w(TAG, "apk_ver " + base + ": " + t);
            return null;
        }
    }

    private void checkApkUpdate(final boolean force, final String base, final String cookie) {
        new Thread(new Runnable() {
            @Override public void run() {
                try {
                    /* 先问「这台电脑」（页面所在的 host）：它本机构建过就直接给 LAN 地址，
                       没有构建就把中继的答案透传过来（url 是绝对地址，走公网）。
                       手机和电脑通常在同一个 WiFi 上，1MB 的包没必要绕公网。
                       本机说「没有比我现在更新的」时才去问中继——除非本机已经给出新包，
                       否则中继永远有最终发言权（可能在别的机器上构建后只上了中继）。 */
                    String cb = base == null ? UPD_BASE : base.replaceAll("/+$", "");
                    org.json.JSONObject o = UPD_BASE.equals(cb) ? null : verJson(cb, cookie);
                    String src = cb;
                    if (o == null || o.optInt("code", 0) <= installedCode()) {
                        org.json.JSONObject oWan = verJson(UPD_BASE, null);
                        if (oWan != null && (o == null || oWan.optInt("code", 0) >= o.optInt("code", 0))) {
                            o = oWan;
                            src = UPD_BASE;
                        }
                    }
                    if (o == null) return;
                    final int rcode = o.optInt("code", 0);
                    final String rver = o.optString("ver", "?");
                    final String url = o.optString("url", "/" + product() + ".apk");
                    /* 不管有没有新版本都清：旧「下载完成」通知点开会报「解析包时出问题」，
                       每次更新留下的几十兆 APK 也不该一直躺在手机里 */
                    cleanStaleApks(rcode <= installedCode());
                    if (rcode <= installedCode()) {                           /* 已是最新 */
                        if (force) ui.post(new Runnable() { @Override public void run() {
                            js("typeof toast==='function'&&toast('客户端已是最新：v" + rver + "（code " + rcode + "）')");
                        }});
                        return;
                    }
                    SharedPreferences sp = getSharedPreferences(PREFS, MODE_PRIVATE);
                    if (!force && rcode <= sp.getInt(KEY_PROMPTED, 0)) return;   /* 自动检查：这版提示过就不吵；手动检查不受限 */
                    Log.i(TAG, "发现新客户端 v" + rver + " (code " + rcode + ")，开始下载");
                    miuiSkip = false;           /* 新一轮更新，权限提示重新生效 */
                    ui.post(new Runnable() { @Override public void run() {
                        js("typeof toast==='function'&&toast('发现新客户端 v" + rver + "，后台下载中…')");
                    }});
                    String full = url.startsWith("http") ? url : src + url;
                    sp.edit().putInt(KEY_PROMPTED, rcode).apply();
                    /* 先本进程直下：DownloadManager 排队 30s+ 才是「点了没反应」的真凶 */
                    startDirectDownload(full);
                } catch (Throwable t) {
                    Log.w(TAG, "apk_ver: " + t);
                }
            }
        }).start();
    }

    /** 清掉以前的安装包下载（连同文件本身和它们的「下载完成」/失败通知）。
        两件事都靠它：
          1) 已经在最新版还留着这些通知 → 点开就是旧的半截包/读不到的 content URI，
             系统安装器只会说一句「解析包时出问题」，重启也不会自己消失；
          2) 每更新一次就多一个几十兆的 APK，攒着纯属浪费手机空间。
        成功过的记：已经在最新版就一律清（正在用的 sessDlId 除外）；还有新版本时只清
        30 分钟前的——刚下好、用户还没点安装的那个包不能动，不然通知点开就装不上。
        失败/取消的也清，它们没用了。每次检查更新的线程里都调。 */
    private void cleanStaleApks(boolean alreadyLatest) {
        try {
            android.app.DownloadManager dm =
                    (android.app.DownloadManager) getSystemService(DOWNLOAD_SERVICE);
            int n = 0;
            long fresh = System.currentTimeMillis() - 30 * 60 * 1000L;
            for (int st : new int[]{android.app.DownloadManager.STATUS_SUCCESSFUL,
                                    android.app.DownloadManager.STATUS_FAILED}) {
                android.database.Cursor c = null;
                try {
                    c = dm.query(new android.app.DownloadManager.Query().setFilterByStatus(st));
                    if (c == null) continue;
                    int idc = c.getColumnIndex(android.app.DownloadManager.COLUMN_ID);
                    int tc = c.getColumnIndex(android.app.DownloadManager.COLUMN_TITLE);
                    int mc = c.getColumnIndex(
                            android.app.DownloadManager.COLUMN_LAST_MODIFIED_TIMESTAMP);
                    while (c.moveToNext()) {
                        long id = c.getLong(idc);
                        String t = tc >= 0 ? c.getString(tc) : null;
                        if (t == null || !t.toLowerCase(java.util.Locale.US).contains("codeboss"))
                            continue;
                        if (id == sessDlId) continue;             /* 正在用的别动 */
                        long mod = mc >= 0 ? c.getLong(mc) : 0;
                        if (st == android.app.DownloadManager.STATUS_SUCCESSFUL
                                && !alreadyLatest && mod > fresh) continue;   /* 可能还没装 */
                        try { dm.remove(id); n++; } catch (Throwable ignored) { }
                    }
                } catch (Throwable ignored) {
                } finally {
                    if (c != null) try { c.close(); } catch (Throwable ignored) { }
                }
            }
            if (n > 0) Log.i(TAG, "清掉旧安装包下载 " + n + " 个");
        } catch (Throwable t) {
            Log.w(TAG, "清理旧安装包失败: " + t);
        }
        cleanUpdStaging();
    }

    /** 清 cache/upd 里暂存的安装包：正常流程装完就删了，装到一半被系统杀掉会留下几十兆。
        只删 10 分钟前的——本轮正在准备的包可能刚写好还没提交。 */
    private void cleanUpdStaging() {
        try {
            java.io.File dir = new java.io.File(getCacheDir(), "upd");
            java.io.File[] fs = dir.listFiles();
            if (fs == null) return;
            long now = System.currentTimeMillis();
            int n = 0;
            for (java.io.File f : fs) {
                if (directApk != null && directApk.getAbsolutePath().equals(f.getAbsolutePath()))
                    continue;                       /* 直下好、正等着装的包别删 */
                if (now - f.lastModified() < 10 * 60 * 1000L) continue;
                try { if (f.delete()) n++; } catch (Throwable ignored) { }
            }
            if (n > 0) Log.i(TAG, "清掉暂存安装包 " + n + " 个");
        } catch (Throwable ignored) { }
    }

    /* 刚把一次下载交给系统（浏览器/下载器）的时刻：WebView 会掐断这次导航并回调
       onReceivedError(ERR_ABORTED)，那说明「开始下载了」，不是断网，别顶离线页。 */
    private volatile long dlNavT = 0;
    private boolean isDownloadNav() {
        return dlNavT > 0 && System.currentTimeMillis() - dlNavT < 5000;
    }

    /** 页面里点的是不是我们自己的更新包：只有它才走「下完直接装」那套。 */
    private boolean isOurApk(String url, String mime) {
        if (mime != null && mime.toLowerCase(java.util.Locale.ROOT)
                .contains("android.package-archive")) return true;
        if (url == null) return false;
        String u = url.toLowerCase(java.util.Locale.ROOT);
        int q = u.indexOf('?');
        if (q >= 0) u = u.substring(0, q);
        return u.endsWith(".apk");
    }

    /** 交系统浏览器打开（只认 http/https）：桌面安装包、官网页面这类 WebView 干不了的活 */
    private void openExternal(String url) {
        if (url == null) return;
        final String u = url.trim();
        if (!(u.startsWith("http://") || u.startsWith("https://"))) return;
        try {
            android.content.Intent i = new android.content.Intent(
                    android.content.Intent.ACTION_VIEW, android.net.Uri.parse(u));
            i.addFlags(android.content.Intent.FLAG_ACTIVITY_NEW_TASK);
            startActivity(i);
        } catch (Throwable t) {
            Log.w(TAG, "没能唤起浏览器: " + t);
            toast("没能打开浏览器");
        }
    }

    private void enqueueApk(String url, String ua) {
        try {
            android.app.DownloadManager.Request rq =
                    new android.app.DownloadManager.Request(android.net.Uri.parse(url));
            rq.setMimeType("application/vnd.android.package-archive");
            rq.setTitle("codeboss-terminal.apk");
            rq.setDescription("Codeboss 安卓客户端");
            rq.setNotificationVisibility(android.app.DownloadManager.Request.VISIBILITY_VISIBLE_NOTIFY_COMPLETED);
            rq.allowScanningByMediaScanner();
            try {
                String ck = android.webkit.CookieManager.getInstance().getCookie(url);
                if (ck != null && ck.length() > 0) rq.addRequestHeader("Cookie", ck);
                if (ua != null && ua.length() > 0) rq.addRequestHeader("User-Agent", ua);
            } catch (Throwable ignored) { }
            long id = ((android.app.DownloadManager) getSystemService(DOWNLOAD_SERVICE)).enqueue(rq);
            Log.i(TAG, "已入队下载 id=" + id + " " + url);
            updDlT0 = System.currentTimeMillis();
            updDlRetried = false;
            updDlLastBytes = -1;
            updDlStall = 0;
            Relaunch.trace(this, "入队下载 id=" + id);
            startDlProg(id);
        } catch (Throwable t) {
            Log.w(TAG, "下载入队失败: " + t);
            js("typeof toast==='function'&&toast('下载失败：' + " + quote(String.valueOf(t)) + ", true)");
        }
    }

    /** 下载完成 → PackageInstaller 会话直装：确认界面只会是系统安装器，
        UC 等第三方安装器（注册了 APK MIME）插不进来选——kiosk 不会被带出界面。
        静默安装要 Device Owner（MIUI 不给），仍需用户点一下确认。
        会话异常时回退老 ACTION_VIEW 路径保底。 */
    /** 装更新前先解掉锁定任务/屏幕固定：固定状态下系统禁止启动安装器（别的 App），
        表现就是闪一下「如需取消固定该 app，向上滑动并按住」然后什么都没装。
        装完/取消回到前台，onWindowFocusChanged 里再上锁（仅设备拥有者）。 */
    private void unlockForInstaller() {
        try {
            android.app.ActivityManager am =
                    (android.app.ActivityManager) getSystemService(ACTIVITY_SERVICE);
            int st = am.getLockTaskModeState();
            if (st == android.app.ActivityManager.LOCK_TASK_MODE_NONE) return;
            stopLockTask();
            relockTask = "kiosk".equals(runMode()) && Kiosk.isOwner(this);
            Log.i(TAG, "安装前解除锁定 state=" + st + " relock=" + relockTask);
        } catch (Throwable t) {
            Log.w(TAG, "stopLockTask: " + t);
        }
    }

    /** Android 8+ 装 APK 还要用户给「安装未知应用」：没给就跳系统授权页 */
    private boolean canInstall() {
        try {
            if (android.os.Build.VERSION.SDK_INT >= 26) return getPackageManager().canRequestPackageInstalls();
        } catch (Throwable ignored) { }
        return true;
    }

    private void openInstallPerm() {
        try {
            if (android.os.Build.VERSION.SDK_INT >= 26) {
                android.content.Intent i = new android.content.Intent(
                        android.provider.Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES,
                        android.net.Uri.parse("package:" + getPackageName()));
                i.addFlags(android.content.Intent.FLAG_ACTIVITY_NEW_TASK);
                unlockForInstaller();                      /* 固定状态下连设置页也起不来 */
                expectAway = true;
                ui.postDelayed(new Runnable() { @Override public void run() { expectAway = false; } }, 60000);
                startActivity(i);
                Log.i(TAG, "打开「安装未知应用」授权页");
                js("typeof toast==='function'&&toast('请允许 Codeboss 安装应用，回来再点一次升级')");
                return;
            }
        } catch (Throwable t) { Log.w(TAG, "打开安装授权页失败: " + t); }
        js("typeof toast==='function'&&toast('请在系统设置里允许本应用安装应用', true)");
    }

    /** 小米/红米/POCO：MIUI 对「后台拉起界面」有自己的一套开关，需要提前带路。 */
    private static boolean isMiui() {
        try {
            Class<?> sp = Class.forName("android.os.SystemProperties");
            Object v = sp.getMethod("get", String.class).invoke(null, "ro.miui.ui.version.name");
            if (v != null && String.valueOf(v).length() > 0) return true;
        } catch (Throwable ignored) { }
        String m = String.valueOf(android.os.Build.MANUFACTURER).toLowerCase(java.util.Locale.US);
        return m.contains("xiaomi") || m.contains("redmi") || m.contains("poco");
    }

    /* MIUI 私有 appop：10021 = 「后台弹出界面」。只开「自启动」不够——实测红米上
       自启动开着，装完我们排的闹钟照样到点，但 startActivity/PendingIntent 被这条
       appop 丢掉，界面就是不出来，用户看到的是「不会自动打开」。 */
    private static final int MIUI_OP_BG_START_ACTIVITY = 10021;

    /** MIUI 的「后台弹出界面」：1 开着 / 0 关着 / -1 查不出来（系统改号等）。
        开了这个，装完系统才肯放我们把自己的界面拉回前台。 */
    private int miuiBgStartState() {
        if (!isMiui()) return 1;
        try {
            android.app.AppOpsManager aom =
                    (android.app.AppOpsManager) getSystemService(APP_OPS_SERVICE);
            if (aom == null) return 1;
            /* 公开 SDK 只有 String 版 checkOpNoThrow；int 版是隐藏 API，
               编译期拿不到，运行时靠反射。 */
            java.lang.reflect.Method m = android.app.AppOpsManager.class.getMethod(
                    "checkOpNoThrow", int.class, int.class, String.class);
            int mode = ((Integer) m.invoke(aom, MIUI_OP_BG_START_ACTIVITY,
                    android.os.Process.myUid(), getPackageName())).intValue();
            Log.i(TAG, "MIUI 后台弹出界面 op=" + mode);
            return mode == android.app.AppOpsManager.MODE_ALLOWED ? 1 : 0;
        } catch (Throwable t) {
            Log.w(TAG, "查 MIUI 后台弹出界面失败: " + t);
            return -1;
        }
    }

    /** 直接开到小米的「应用权限管理」页（自启动/后台弹出界面都在这里），
        打不开再退回系统应用详情页。 */
    private void openMiuiPermEditor() {
        String[][] cands = {
            {"com.miui.securitycenter", "com.miui.permcenter.permissions.PermissionsEditorActivity"},
            {"com.miui.securitycenter", "com.miui.permcenter.permissions.AppPermissionsEditorActivity"},
            {"com.miui.securitycenter", "com.miui.permcenter.permissions.PAppEditorActivity"},
        };
        for (String[] c : cands) {
            try {
                android.content.Intent i = new android.content.Intent("miui.intent.action.APP_PERM_EDITOR");
                i.setClassName(c[0], c[1]);
                i.putExtra("extra_pkgname", getPackageName());
                i.addFlags(android.content.Intent.FLAG_ACTIVITY_NEW_TASK);
                startActivity(i);
                Log.i(TAG, "打开 MIUI 权限编辑页 " + c[1]);
                return;
            } catch (Throwable ignored) { }
        }
        openAppInfoForMiui();
    }

    /** 装更新前提示：MIUI 上要开「自启动」+「后台弹出界面」，装完才能自己回来。
        选「去开启」→ 打开权限页，回来后在 onWindowFocusChanged 里接着装。
        file != null 表示这轮是本进程直下的包（没有下载 id）。 */
    private void showMiuiHint(final long id) { showMiuiHint(id, null); }

    private void showMiuiHint(final long id, final java.io.File file) {
        try {
            new android.app.AlertDialog.Builder(this)
                .setTitle("小米手机：先放行两个开关")
                .setMessage("还要打开「自启动」和「权限管理 → 其他权限 → 后台弹出界面」。"
                        + "两项都开了，更新装完 Codeboss 才会自己回到前台；只开自启动不够。"
                        + "设一次以后更新都不用管。")
                .setPositiveButton("去开启", new android.content.DialogInterface.OnClickListener() {
                    @Override public void onClick(android.content.DialogInterface d, int w) {
                        getSharedPreferences(PREFS, MODE_PRIVATE).edit()
                                .putBoolean(KEY_MIUI_HINT, true).apply();
                        if (file != null) pendingApkFile = file;
                        else pendingInstall = id;
                        openMiuiPermEditor();
                    }
                })
                .setNegativeButton("继续安装", new android.content.DialogInterface.OnClickListener() {
                    @Override public void onClick(android.content.DialogInterface d, int w) {
                        getSharedPreferences(PREFS, MODE_PRIVATE).edit()
                                .putBoolean(KEY_MIUI_HINT, true).apply();
                        miuiSkip = true;        /* 本轮别再弹权限提示 */
                        if (file != null) {
                            ui.postDelayed(new Runnable() {
                                @Override public void run() { promptInstallFile(file); }
                            }, 200);
                            return;
                        }
                        sessDlId = -1;          /* 绕开「同一下载 id 只装一次」去重 */
                        ui.postDelayed(new Runnable() {
                            @Override public void run() { promptInstall(id); }
                        }, 200);
                    }
                })
                .setCancelable(false)
                .show();
            Log.i(TAG, "小米首次更新：提示放行自启动/后台弹出界面");
        } catch (Throwable t) {
            Log.w(TAG, "小米提示框失败: " + t);
            getSharedPreferences(PREFS, MODE_PRIVATE).edit()
                    .putBoolean(KEY_MIUI_HINT, true).apply();
            ui.postDelayed(new Runnable() {
                @Override public void run() {
                    if (file != null) promptInstallFile(file); else promptInstall(id);
                }
            }, 200);
        }
    }

    private void openAppInfoForMiui() {
        try {
            android.content.Intent i = new android.content.Intent(
                    android.provider.Settings.ACTION_APPLICATION_DETAILS_SETTINGS,
                    android.net.Uri.parse("package:" + getPackageName()));
            i.addFlags(android.content.Intent.FLAG_ACTIVITY_NEW_TASK);
            unlockForInstaller();
            expectAway = true;
            ui.postDelayed(new Runnable() {
                @Override public void run() { expectAway = false; }
            }, 120000);
            startActivity(i);
            js("typeof toast==='function'&&toast('打开「自启动」和「权限管理→其他权限→后台弹出界面」，回来自动继续安装')");
            Log.i(TAG, "打开小米应用详情页（自启动/权限）");
        } catch (Throwable t) {
            Log.w(TAG, "打开应用详情页失败: " + t);
        }
    }

    /** 把下载件先落到私有 cache 并让系统包解析器验一遍。
        以前是「边读边塞会话」：任何一次短读/半截文件都会在安装器里变成
        「解析软件包出错」，用户看到的就是这句话，我们却拿不到可行动的原因。
        返回 null 表示这个包不能用（调用方重下一次）。 */
    private java.io.File stageApk(long id) {
        java.io.File out = null;
        try {
            android.app.DownloadManager dm =
                    (android.app.DownloadManager) getSystemService(DOWNLOAD_SERVICE);
            android.net.Uri u = dm.getUriForDownloadedFile(id);
            if (u == null) { Log.w(TAG, "暂存失败：下载文件缺失 id=" + id); return null; }
            long expect = -1;
            try (android.database.Cursor c = dm.query(
                    new android.app.DownloadManager.Query().setFilterById(id))) {
                if (c != null && c.moveToFirst()) {
                    int ci = c.getColumnIndex(android.app.DownloadManager.COLUMN_TOTAL_SIZE_BYTES);
                    if (ci >= 0) expect = c.getLong(ci);
                }
            } catch (Throwable ignored) { }
            java.io.File dir = new java.io.File(getCacheDir(), "upd");
            if (!dir.exists()) dir.mkdirs();
            out = new java.io.File(dir, "cb-" + id + ".apk");
            long n = 0;
            try (java.io.InputStream in = getContentResolver().openInputStream(u);
                 java.io.OutputStream os = new java.io.FileOutputStream(out)) {
                byte[] buf = new byte[65536];
                int r;
                while ((r = in.read(buf)) > 0) { os.write(buf, 0, r); n += r; }
                os.flush();
            }
            Log.i(TAG, "暂存安装包 " + n + " 字节" + (expect > 0 ? "（应为 " + expect + "）" : ""));
            if (n < 10240 || (expect > 0 && n != expect)) {
                Log.w(TAG, "暂存失败：大小不对 " + n + "/" + expect);
                out.delete();
                return null;
            }
            android.content.pm.PackageInfo pi =
                    getPackageManager().getPackageArchiveInfo(out.getAbsolutePath(), 0);
            if (pi == null) {   /* 解析器都说看不懂：必然是坏包（半截/HTML/签名块缺失） */
                Log.w(TAG, "暂存失败：系统解析不了这个安装包（多半是下载不完整）");
                out.delete();
                return null;
            }
            if (pi.packageName == null || !getPackageName().equals(pi.packageName)) {
                Log.w(TAG, "暂存失败：包名不对 " + pi.packageName);
                out.delete();
                return null;
            }
            if (pi.versionCode <= installedCode()) {
                Log.i(TAG, "暂存失败：版本没变（" + pi.versionCode + "）");
                out.delete();
                return null;
            }
            Log.i(TAG, "安装包校验通过 v" + pi.versionName + "(" + pi.versionCode + ")");
            return out;
        } catch (Throwable t) {
            Log.w(TAG, "暂存安装包异常: " + t);
            if (out != null) { try { out.delete(); } catch (Throwable ignored) { } }
            return null;
        }
    }

    private void promptInstall(long id) {
        if (sessDlId == id) {   /* DownloadManager 偶发双通知（实测一轮两个会话）→ 第二次跳过 */
            Log.i(TAG, "重复下载回调 id=" + id + "，跳过");
            return;
        }
        sessDlId = id;
        directInstall = false;          /* 走系统下载这条老路，兜底仍用 content URI */
        sessFailFall = false;
        sessPhase = 0;
        unlockForInstaller();
        if (!canInstall()) { pendingInstall = id; openInstallPerm(); return; }   /* 引导：先去开「安装未知应用」，回来自动接着装 */
        /* 小米把「更新装完自己回来」卡在两个系统开关上（自启动 / 后台弹出界面），
           App 自己开不了（安全中心会把 appops 改回去），第一次更新前带路让用户点一下。 */
        /* MIUI：只要「后台弹出界面」还是关的就每次更新都提醒——
           红米实测自启动开着、这项关着，装完照样不回来，用户只会觉得「又坏了」。
           查不出状态（系统改号）时每版提醒一次，别永远不吭声。 */
        if (!miuiSkip && isMiui()) {
            int op = miuiBgStartState();
            boolean needHint = (op == 0) || (op < 0 &&
                    getSharedPreferences(PREFS, MODE_PRIVATE).getInt("miui_op_code", 0)
                            != installedCode());
            if (needHint) {
                getSharedPreferences(PREFS, MODE_PRIVATE).edit()
                        .putInt("miui_op_code", installedCode()).apply();
                showMiuiHint(id);
                return;
            }
        }
        /* 装之前先把「装完叫醒我」排好：出厂广播在 MIUI/部分 AOSP 上根本到不了，
           闹钟和 job 是系统直接叫醒进程，更靠得住（见 Relaunch.armWake） */
        Relaunch.armWake(this);
        /* 再加一层最笨的兜底：通知栏先放一条「点一下回到 Codeboss」。
           有的 ROM 连闹钟都清了、界面也拦着不让弹，那也不能让用户装完干瞪眼。 */
        Relaunch.armNote(this);
        try {
            java.io.File apkFile = stageApk(id);
            if (apkFile == null) {
                /* 坏包（下载不完整最常见）：删掉重下一次；同一轮只重试一次，
                   还不行就走老的唤起安装器让用户自己决定。 */
                try {
                    android.app.DownloadManager dm =
                            (android.app.DownloadManager) getSystemService(DOWNLOAD_SERVICE);
                    dm.remove(id);
                } catch (Throwable ignored) { }
                if (stagedRetryFor != id) {
                    stagedRetryFor = id;
                    sessDlId = 0;
                    js("typeof toast==='function'&&toast('安装包没下载完整，正在重新下载…')");
                    checkApkUpdate(true);
                    return;
                }
                Log.w(TAG, "重下后仍然不是有效安装包，放弃本轮");
                Relaunch.clearNote(this);    /* 这轮不装了，别在通知栏留条没用的 */
                js("typeof toast==='function'&&toast('安装包下载有问题，请稍后再试（已重试一次）', true)");
                return;
            }
            commitApk(apkFile, id);
        } catch (Throwable t) {
            Log.w(TAG, "准备安装异常: " + t);
            Relaunch.clearNote(this);
            js("typeof toast==='function'&&toast('安装准备失败，请稍后再试', true)");
        }
    }

    /** 把已校验通过的安装包交给系统安装器（PackageInstaller 会话直装）。
        dlId > 0：包在 DownloadManager 里，兜底还能拿到它的 content URI；
        dlId <= 0：本进程直下的文件，兜底只能退回系统下载通道。 */
    private void commitApk(final java.io.File apkFile, final long dlId) {
        /* 1MB 的包要一个字节一个字节塞进系统安装会话（还要 fsync），在主线程上做
           就是把 WebView 冻住——用户看到的正是「正在打开安装界面…」一动不动。 */
        new Thread(new Runnable() { @Override public void run() { commitApkNow(apkFile, dlId); } }).start();
    }

    private void commitApkNow(java.io.File apkFile, final long dlId) {
        try {
            java.io.InputStream in = new java.io.FileInputStream(apkFile);
            android.content.pm.PackageInstaller pi = getPackageManager().getPackageInstaller();
            android.content.pm.PackageInstaller.SessionParams ps =
                    new android.content.pm.PackageInstaller.SessionParams(
                            android.content.pm.PackageInstaller.SessionParams.MODE_FULL_INSTALL);
            ps.setAppPackageName(getPackageName());   /* 覆盖安装自己 */
            int sid = pi.createSession(ps);
            android.content.pm.PackageInstaller.Session ss = pi.openSession(sid);
            java.io.OutputStream os = ss.openWrite("cbapk", 0, -1);
            byte[] buf = new byte[65536];
            long wrote = 0;
            try {
                int n;
                while ((n = in.read(buf)) > 0) { os.write(buf, 0, n); wrote += n; }
                ss.fsync(os);
            } finally {
                try { os.close(); } catch (Throwable ignored) { }
                try { in.close(); } catch (Throwable ignored) { }
            }
            if (wrote != apkFile.length()) {   /* 会话里少了字节 = 装到一半必报解析错误 */
                Log.w(TAG, "会话写入不完整 " + wrote + "/" + apkFile.length() + "，放弃本次会话");
                try { ss.abandon(); } catch (Throwable ignored) { }
                Relaunch.clearNote(this);
                js("typeof toast==='function'&&toast('安装包写入不完整，请稍后再试', true)");
                return;
            }
            /* 安装状态回调用**广播** PendingIntent 打到清单里的静态接收器，不再用 Activity：
               覆盖安装会把自己杀掉，Activity 版的回调在部分 ROM（多亲 AOSP 9 实测）上
               装完就断了，界面不会被拉起。广播是显式的、进程死了系统也会叫起来投递；
               FLAG_INCLUDE_STOPPED_PACKAGES 让「刚被更新杀掉」的包也能收到。
               主界面还活着时，同一条广播仍由本页的动态接收器处理确认框和看门狗。 */
            android.content.Intent ri = new android.content.Intent(
                    this, UpdateStatusReceiver.class);
            ri.setAction(ACTION_INSTALL);
            ri.addFlags(android.content.Intent.FLAG_INCLUDE_STOPPED_PACKAGES
                    | android.content.Intent.FLAG_RECEIVER_FOREGROUND);
            /* 必须是「可变」PendingIntent：系统回调时用 fillIn 把 EXTRA_STATUS /
               EXTRA_INTENT 灌进来，IMMUTABLE 会把这层 extras 直接丢掉——实测 Android 9
               上就变成「什么都没收到」。API 31+ 要显式 MUTABLE。 */
            int pfl = android.app.PendingIntent.FLAG_UPDATE_CURRENT;
            if (android.os.Build.VERSION.SDK_INT >= 31) pfl |= android.app.PendingIntent.FLAG_MUTABLE;
            android.app.PendingIntent st =
                    android.app.PendingIntent.getBroadcast(this, 1, ri, pfl);
            ss.commit(st.getIntentSender());
            Relaunch.trace(this, "安装会话已提交 sid=" + sid);
            sessPhase = 1;
            updGrace = false;
            updPending = true;
            ui.removeCallbacks(updWatch);
            ui.postDelayed(updWatch, 25 * 1000);   /* 25s 无任何安装状态 → 兜底拉回前台 */
            Log.i(TAG, "PackageInstaller 会话已提交 sid=" + sid);
            try { apkFile.delete(); } catch (Throwable ignored) { }   /* 已进会话，缓存件不留 */
        } catch (Throwable t) {
            Log.w(TAG, "会话安装失败，回退唤起安装器: " + t);
            if (dlId <= 0) {
                /* 直下的包没有 DownloadManager 的 content URI（本包没有 FileProvider），
                   ACTION_VIEW 给不了文件 → 退回系统下载通道，那边有完整的兜底链 */
                try { apkFile.delete(); } catch (Throwable ignored) { }
                directFallbackDownload();
                return;
            }
            try {
                android.app.DownloadManager dm =
                        (android.app.DownloadManager) getSystemService(DOWNLOAD_SERVICE);
                android.net.Uri u = dm.getUriForDownloadedFile(dlId);
                android.content.Intent i = new android.content.Intent(android.content.Intent.ACTION_VIEW);
                i.setDataAndType(u, "application/vnd.android.package-archive");
                i.addFlags(android.content.Intent.FLAG_ACTIVITY_NEW_TASK
                        | android.content.Intent.FLAG_GRANT_READ_URI_PERMISSION);
                startActivity(i);
            } catch (Throwable t2) {
                Log.w(TAG, "唤起安装失败: " + t2);
                Relaunch.clearNote(this);
                js("typeof toast==='function'&&toast('下载好了，去通知栏点开安装', true)");
            }
        }
    }

    /* ── 应用内直下安装包 ──────────────────────────────────────────────
       为什么不走 DownloadManager：入队到真正发请求之间要排 30s 以上（系统下载服务
       的 job/网络约束），手机上就是「准备下载…」一动不动；包只有 874KB，自己下
       3 秒就完了。这里自己下、自己校验，通过后交给同一条 PackageInstaller 安装
       链路；任何一步失败（没网/超时/包不对）都退回 enqueueApk，行为同以前。 */
    private void startDirectDownload(final String url) {
        if (directDlOn) { Log.i(TAG, "直下已在进行，忽略重复请求"); return; }
        directDlOn = true;
        directApkUrl = url;
        directApk = null;
        Log.i(TAG, "应用内直下开始 " + url);
        Relaunch.trace(this, "应用内直下开始");
        new Thread(new Runnable() {
            @Override public void run() {
                java.io.File out = null;
                try {
                    java.io.File dir = new java.io.File(getCacheDir(), "upd");
                    if (!dir.exists()) dir.mkdirs();
                    out = new java.io.File(dir, "cb-direct.apk");
                    if (out.exists()) out.delete();
                    java.net.HttpURLConnection c = conn(url);
                    c.setConnectTimeout(8000);
                    c.setReadTimeout(15000);     /* 15s 一个字节都没来 → 当卡死，退回系统下载 */
                    c.setInstanceFollowRedirects(true);
                    try {
                        String ck = android.webkit.CookieManager.getInstance().getCookie(url);
                        if (ck != null && ck.length() > 0) c.setRequestProperty("Cookie", ck);
                    } catch (Throwable ignored) { }
                    int code = c.getResponseCode();
                    if (code != 200) throw new java.io.IOException("HTTP " + code);
                    long total = -1;
                    try { total = c.getContentLengthLong(); } catch (Throwable ignored) { }
                    long n = 0, lastUi = 0, t0 = System.currentTimeMillis();
                    byte[] buf = new byte[65536];
                    try (java.io.InputStream in = c.getInputStream();
                         java.io.OutputStream os = new java.io.FileOutputStream(out)) {
                        int r;
                        while ((r = in.read(buf)) > 0) {
                            os.write(buf, 0, r);
                            n += r;
                            long now = System.currentTimeMillis();
                            if (now - lastUi < 150) continue;
                            lastUi = now;
                            final int pct = total > 0 ? (int) (n * 100 / total) : -1;
                            final long dn = n, tt = total;
                            ui.post(new Runnable() { @Override public void run() {
                                js("typeof __cbDlProg==='function'&&__cbDlProg("
                                        + pct + "," + dn + "," + tt + ")");
                            }});
                        }
                        os.flush();
                    }
                    Log.i(TAG, "直下完成 " + n + " 字节（应为 " + total + "）用时 "
                            + ((System.currentTimeMillis() - t0) / 1000) + "s");
                    if (n < 10240 || (total > 0 && n != total))
                        throw new java.io.IOException("大小不对 " + n + "/" + total);
                    android.content.pm.PackageInfo pi = getPackageManager()
                            .getPackageArchiveInfo(out.getAbsolutePath(), 0);
                    if (pi == null) throw new java.io.IOException("系统解析不了安装包");
                    if (pi.packageName == null || !getPackageName().equals(pi.packageName))
                        throw new java.io.IOException("包名不对 " + pi.packageName);
                    if (pi.versionCode <= installedCode())
                        throw new java.io.IOException("版本没变 " + pi.versionCode);
                    Log.i(TAG, "直下校验通过 v" + pi.versionName + "(" + pi.versionCode + ")");
                    directApk = out;
                    final java.io.File got = out;
                    ui.post(new Runnable() { @Override public void run() {
                        js("typeof __cbDlProg==='function'&&__cbDlProg(100,-1,-1)");
                        promptInstallFile(got);
                    }});
                } catch (final Throwable t) {
                    Log.w(TAG, "应用内直下失败，退回系统下载: " + t);
                    Relaunch.trace(MainActivity.this, "直下失败→系统下载: " + t);
                    if (out != null) { try { out.delete(); } catch (Throwable ignored) { } }
                    ui.post(new Runnable() { @Override public void run() {
                        js("typeof toast==='function'&&toast('换系统下载通道…')");
                        enqueueApk(url, null);
                    }});
                } finally {
                    directDlOn = false;
                }
            }
        }).start();
    }

    /** 直下的包装好以后：和 promptInstall 同一套闸门（安装权限 / 小米提示 / 装完叫醒），
        只是没有下载 id，改用文件本身。 */
    private void promptInstallFile(final java.io.File f) {
        if (f == null || !f.exists()) return;
        if (directInstall) { Log.i(TAG, "直下安装已在进行，跳过重复"); return; }
        directInstall = true;
        sessDlId = 0;               /* 没有下载 id：兜底判断走直下分支 */
        sessFailFall = false;
        sessPhase = 0;
        updGrace = false;
        unlockForInstaller();
        if (!canInstall()) {        /* 引导：先去开「安装未知应用」，回来自动接着装 */
            pendingApkFile = f;
            directInstall = false;
            openInstallPerm();
            return;
        }
        if (!miuiSkip && isMiui()) {
            int op = miuiBgStartState();
            boolean needHint = (op == 0) || (op < 0 &&
                    getSharedPreferences(PREFS, MODE_PRIVATE).getInt("miui_op_code", 0)
                            != installedCode());
            if (needHint) {
                getSharedPreferences(PREFS, MODE_PRIVATE).edit()
                        .putInt("miui_op_code", installedCode()).apply();
                directInstall = false;
                showMiuiHint(0, f);
                return;
            }
        }
        Relaunch.armWake(this);
        Relaunch.armNote(this);
        commitApk(f, 0);
    }

    /** 直下这条路装不了（会话被系统拒/提交失败）：退回系统下载通道再来一轮。
        那条路有 content URI，ACTION_VIEW 兜底和通知栏提示都能用。 */
    private void directFallbackDownload() {
        directInstall = false;
        jsInstallSt(-3);          /* 页面：改用系统下载（否则停在「正在打开安装界面…」） */
        Relaunch.trace(this, "直下安装失败 → 退回系统下载");
        js("typeof toast==='function'&&toast('改用系统下载…')");
        if (directApkUrl != null) enqueueApk(directApkUrl, null);
    }

    /* 更新防走失：commit 后 25s 没等到安装状态（确认框没弹出来/被吞）→ 拉回前台；
       确认框弹出（PENDING_USER_ACTION）后改为 5 分钟没点完 → 拉回。kiosk 下用户
       在确认框里乱跑也会被看门狗接住。 */
    /** ACTION_VIEW 兜底安装：会话路径被系统安装器拒（或干脆没响应）时走老路。
        无安装状态回调可收——靠 viewInstall 标志让 kiosk 看门狗让路，
        用户点完装完回到本 App（焦点回归）即收尾；5 分钟没回也有 updWatch 接住。 */
    private void fallbackViewInstall() {
        updWatchTick = 0;
        try {
            android.app.DownloadManager dm =
                    (android.app.DownloadManager) getSystemService(DOWNLOAD_SERVICE);
            android.net.Uri u = dm.getUriForDownloadedFile(sessDlId);
            if (u == null) { Log.w(TAG, "兜底安装: 下载文件缺失 id=" + sessDlId); return; }
            android.content.Intent i = new android.content.Intent(android.content.Intent.ACTION_VIEW);
            i.setDataAndType(u, "application/vnd.android.package-archive");
            i.addFlags(android.content.Intent.FLAG_ACTIVITY_NEW_TASK
                    | android.content.Intent.FLAG_GRANT_READ_URI_PERMISSION);
            unlockForInstaller();
            viewInstall = true;
            updPending = true;
            sessPhase = 0;
            Relaunch.trace(this, "会话装不了，改走系统安装器 ACTION_VIEW");
            removeOverlays();   /* 罩是系统级浮窗，会盖在安装器上（安装确认界面被压暗＝看着像"闪屏"）。
                                    安装期间摘掉，焦点回来 onResume 会补挂 */
            ui.removeCallbacks(updWatch);
            ui.postDelayed(updWatch, 5 * 60 * 1000);
            startActivity(i);
            Log.i(TAG, "兜底 ACTION_VIEW 安装已发起 id=" + sessDlId);
        } catch (Throwable t) {
            viewInstall = false;
            updPending = false;
            Log.w(TAG, "兜底唤起失败: " + t);
            js("typeof toast==='function'&&toast('下载好了，去通知栏点开安装', true)");
        }
    }

    /** 兜底安装期间：按「前台到底是不是安装器」决定让路何时结束。
        安装器在前台 → 继续让路（用户正在点确认，kiosk 看门狗不许顶掉它）；
        安装器不在前台（装完/用户取消/被别的系统界面顶走）→ 收尾，恢复看门狗。 */
    private final Runnable viewInstallClear = new Runnable() {
        @Override public void run() {
            if (!viewInstall) return;
            String top = topPkg();
            if (isInstaller(top)) {
                Log.i(TAG, "安装器仍在前台(" + top + ") → 继续让路");
                ui.postDelayed(this, 5000);
                return;
            }
            /* 判不出前台包名（MIUI 限了 getRunningTasks，实测会返回我们自己）→
               保守继续让路：宁可多让一会儿路，也不把安装器顶掉。
               真正的收尾靠两件事：①安装器 Activity 关闭时本进程会被杀重建（安装成功）
               ②updWatch 5 分钟硬超时 */
            if (top == null || top.equals(getPackageName())) {
                if (updWatchTick++ < 12) {   /* 约 60s 内仍判不出 → 再等等 */
                    Log.i(TAG, "前台包名判不出(top=" + top + ") → 继续让路");
                    ui.postDelayed(this, 5000);
                    return;
                }
                Log.i(TAG, "前台包名长期判不出 → 按超时收尾");
            }
            viewInstall = false;
            updPending = false;
            sessPhase = 0;
            ui.removeCallbacks(updWatch);
            Log.i(TAG, "安装器已不在前台(top=" + top + ") → 兜底让路结束");
        }
    };

    private int updWatchTick = 0;

    private boolean isInstaller(String top) {
        if (top == null) return false;
        return top.contains("packageinstaller") || top.contains("packageInstaller")
                || top.contains("permissioncontroller") || top.contains("installer");
    }

    /** 当前前台包名（拿不到就返回 null）。 */
    private String topPkg() {
        try {
            android.app.ActivityManager am =
                    (android.app.ActivityManager) getSystemService(ACTIVITY_SERVICE);
            java.util.List<android.app.ActivityManager.RunningTaskInfo> ts =
                    am.getRunningTasks(1);
            if (ts != null && !ts.isEmpty()) {
                android.content.ComponentName c = ts.get(0).topActivity;
                if (c != null) return c.getPackageName();
            }
        } catch (Throwable ignored) { }
        return null;
    }

    /** 系统安装器是不是前台进程。topPkg() 靠 getRunningTasks，没 GET_TASKS 权限时只看得到
        本进程的任务（一直返回自己），靠它判断「确认框还在不在」永远为假；进程重要度不受限。 */
    private boolean installerInFront() {
        try {
            android.app.ActivityManager am =
                    (android.app.ActivityManager) getSystemService(ACTIVITY_SERVICE);
            if (am == null) return false;
            for (android.app.ActivityManager.RunningAppProcessInfo p : am.getRunningAppProcesses()) {
                if (p.importance == android.app.ActivityManager.RunningAppProcessInfo.IMPORTANCE_FOREGROUND
                        && isInstaller(p.processName)) return true;
            }
        } catch (Throwable ignored) { }
        return false;
    }

    private final Runnable updWatch = new Runnable() {
        @Override public void run() {
            if (!updPending) return;
            if (viewInstall) {   /* 兜底安装器还在前台 → 续期，别把它顶掉 */
                String tp = topPkg();
                if (isInstaller(tp) || tp == null || tp.equals(getPackageName())) {
                    ui.postDelayed(this, 60 * 1000);
                    return;
                }
            }
            /* 确认框已经弹出来、而且还在台前 = 用户正对着它：继续等，别把自己拉到前台把
               系统确认界面顶掉。以前 5 分钟一到就拉回，安装界面被我们盖掉，用户什么都没
               点成，页面停在「正在打开安装界面…」（2026-11-18 多亲实测）。 */
            if (sessPhase == 2 && installerInFront()) {
                ui.postDelayed(this, 60 * 1000);
                return;
            }
            /* 会话提交后 25s 没等到任何状态（含系统秒拒没发广播的机型）→ 先兜底再拉回；
               确认框/兜底界面已在台前（phase 2/viewInstall）超时 → 只拉回，别再叠一层安装器 */
            if (sessPhase == 1 && !sessFailFall && !viewInstall) {
                /* 第一次到点先别推倒重来：低端机上系统安装器（冷启动 PackageInstaller）
                   慢十几秒是常态，25s 就把一个已经提交好的会话扔掉、再走一遍系统下载
                   （入队 30s+ 再下一遍 1MB）才是「更新很慢」的主因。宽限一轮再兜底。 */
                if (!updGrace) {
                    updGrace = true;
                    jsInstallSt(-4);
                    ui.postDelayed(this, 20 * 1000);
                    return;
                }
                sessFailFall = true;
                if (sessDlId > 0) {
                    Log.w(TAG, "updWatch: 会话无状态 → ACTION_VIEW 兜底");
                    fallbackViewInstall();
                } else {
                    Log.w(TAG, "updWatch: 直下会话无状态 → 退回系统下载");
                    directFallbackDownload();
                }
                return;
            }
            Log.w(TAG, "updWatch: 更新确认超时 → 拉回前台");
            js("typeof toast==='function'&&toast('更新未完成，回到 Codeboss', true)");
            jsInstallSt(-2);   /* 页面按钮复位：别一直停在「正在打开安装界面…」 */
            try {
                android.content.Intent i = new android.content.Intent(MainActivity.this, MainActivity.class);
                i.addFlags(android.content.Intent.FLAG_ACTIVITY_NEW_TASK
                        | android.content.Intent.FLAG_ACTIVITY_REORDER_TO_FRONT);
                startActivity(i);
            } catch (Throwable ignored) { }
            updPending = false;
            /* 这一轮彻底作废。不清 directInstall 的话，之后每次更新都在 promptInstallFile
               里判「直下安装已在进行」直接跳过，页面永远停在「正在打开安装界面…」且再也
               装不上，只能杀进程（2026-11-18 用户报「一直在正在打开安装界面」）。 */
            directInstall = false;
            sessDlId = 0;
            sessPhase = 0;
            sessFailFall = false;
            updGrace = false;
        }
    };

    @Override
    protected void onDestroy() {
        if (INSTANCE == this) INSTANCE = null;
        if (autoVad != null) { ui.removeCallbacks(autoVad); autoVad = null; }
        autoRec = false;
        ui.removeCallbacks(shimBeat);
        ui.removeCallbacks(netWatch);
        if (apkBeat != null) ui.removeCallbacks(apkBeat);
        if (apkRc != null) { try { unregisterReceiver(apkRc); } catch (Throwable ignored) { } }
        if (apkRc2 != null) { try { unregisterReceiver(apkRc2); } catch (Throwable ignored) { } }
        if (screenRc != null) { try { unregisterReceiver(screenRc); } catch (Throwable ignored) { } }
        /* 界面重来一次就 new 一个 WebView；不销毁旧的，渲染进程会越攒越多
           （低配机上更容易被系统回收/整页冻死）。先摘出视图树再 destroy。 */
        if (web != null) {
            try {
                android.view.ViewGroup p = (android.view.ViewGroup) web.getParent();
                if (p != null) p.removeView(web);
            } catch (Throwable ignored) { }
            try { web.stopLoading(); web.destroy(); } catch (Throwable ignored) { }
            web = null;
        }
        super.onDestroy();
    }
}
