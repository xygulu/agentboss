// SPDX-License-Identifier: AGPL-3.0-or-later

package com.agentboss.terminal;

import android.app.Notification;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.content.SharedPreferences;
import android.media.AudioAttributes;
import android.media.MediaPlayer;
import android.media.RingtoneManager;
import android.os.Build;
import android.os.Handler;
import android.os.IBinder;
import android.os.Looper;
import android.util.Log;
import android.webkit.CookieManager;

import org.json.JSONObject;

import java.io.BufferedReader;
import java.io.File;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.net.HttpURLConnection;
import java.net.URL;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;

/**
 * 后台提醒服务（黑屏/锁屏也能收到提醒）。
 *
 * 页面在 visibilitychange→hidden 时会主动断开 SSE（省电），安卓壳也会在 5 秒后
 * 冻结 WebView——以前这就等于「一黑屏就什么提醒都没有」。这里用前台服务自己维持
 * 一条只收提醒的 SSE：
 *   GET <base>/api/bg_push?since=<上一条 id>
 * 收到 notify 就发系统通知 +（按配置）播内置提醒语音；点通知直达会话。
 *
 * 使用时长口径不变：中继对 /api/bg_push 豁免计费（relay.py tunnel），
 * 只有页面在前台产生的请求才累计远程时长。黑屏期间这条流不算在用。
 */
public class BgService extends Service {
    private static final String TAG = "CodebossBg";
    private static final String PREFS = "codeboss";
    private static final String KEY_LAST = "bg_last_id";
    /* 后台真正响过的提醒 id（不是「收到」——前台时它故意让给页面）。
       页面重连补发时读这个值，跳过已经响过的，别响两遍。 */
    static final String KEY_ANNOUNCED = "bg_announced_id";
    private static final String KEY_BASE = "base";
    /* boss 系列共享中继：与 MainActivity 的 UPD_BASE 同源同值，改一处时另一处同步改。
       base/url 都没存时兜底指到中继配对页所在域（消费点仍有空守卫）。 */
    private static final String DEFAULT_BASE = "https://relay.example.com";

    private static final int NOTIF_ID = 0x0CB0;          // 常驻通知（前台服务必需）
    private static final String CH_SVC = "progress_bg_v1";
    private static final String CH_DONE = "progress_done_v1";
    private static final String CH_PERM = "progress_perm_v1";
    private static final String CH_INPUT = "progress_input_v1";
    private static final String CH_QUIET = "progress_quiet_v1";

    /* 合成语音缓存（cache/say_*.bin）：同一句话可以复用，但不能无限攒——
       以前每句「在做什么，完成了」都落一个新文件、永远不删，用久了就是几百兆。 */
    private static final long SAY_TTL_MS = 3L * 24 * 3600 * 1000;   /* 留 3 天 */
    private static final int SAY_MAX = 40;                          /* 最多留 40 个（约 1MB） */
    private volatile File sayPlaying;                               /* 正在放的那个别删 */
    /* 正在出声（念播报/内置音，含「已排上队、还在下载」的那段）。
       页面自动听写据此决定「能不能开麦」：光等完成回调不够——回调之前还有
       1 秒提示音 + 下载，回调之后扬声器也有尾巴，开早了就把播报自己录进去。 */
    private static volatile boolean SAYING = false;
    static boolean isSpeaking() { return SAYING; }
    /* 播报队列（2026-11-20 用户：播报会突然截断，要讲完整）。正在念的时候又来一条，
       不许掐断——存进等待槽，念完接着念。只留最新一条：旧提醒已被新提醒覆盖。 */
    private boolean sayBusy = false;
    private boolean qHas = false;
    private String qKind = "", qAbout = "", qPath = "", qSid = "";
    /* 最近一条通知的样子：播报这一步放不出来（合成音频取不到/解码失败）时，
       用它补一声提醒——不能让「没提醒」变成完全静默（见 sayFailed）。 */
    private String nfSid = "", nfTitle = "", nfBody = "", nfSnd = "done", nfRid = "";
    private boolean nfText = false, nfSound = true, nfSilent = false;
    /* 正在念哪条提醒（哪个会话）：念完要告诉页面「可以开麦了」（自动听写等这一刻） */
    private volatile String curSid = "";

    /* 服务现在在跑吗？页面用它决定「出声这件事归不归壳」（见 announces()）。
       开关关掉/被系统杀掉时这里是 false，页面自己在前台念，别让提醒变哑。 */
    private static volatile boolean RUNNING = false;
    public static boolean isRunning() { return RUNNING; }

    /* 设备提醒凭据（90 天，页面经 bridge 交过来，见 MainActivity.bgToken）。
       壳的 Java 端读不到 WebView 的 cookie（2026-11-19 S24 mini：getCookie 只回
       一个 cb_product），黑屏时页面又被冻结、没人能换 10 分钟的 dt——所以这条
       长期票由页面取好交给壳，壳自己带 X-CB-Bgd 头去拉提醒流和播报音频。 */
    static volatile String BG_TOK = "";

    static String bgToken() { return BG_TOK; }

    static void setBgToken(Context c, String tok) {
        String t = tok == null ? "" : tok.trim();
        BG_TOK = t;
        try {
            c.getSharedPreferences(PREFS, Context.MODE_PRIVATE).edit()
                    .putString("bgd_tok", t).apply();
        } catch (Throwable ignored) {}
        Log.i(TAG, "设备提醒凭据 " + (t.length() > 0 ? "已更新 len=" + t.length() : "已清空"));
    }

    private volatile boolean alive = true;
    private volatile HttpURLConnection conn;
    private Thread worker;
    private MediaPlayer cueMp;
    private final Handler main = new Handler(Looper.getMainLooper());
    private long lastId = -1;
    private String ua = "CodebossShell/bg";

    // ---- 启停 ----
    public static void startIfEnabled(Context c) {
        if (!enabled(c)) return;
        Intent i = new Intent(c, BgService.class);
        try {
            if (Build.VERSION.SDK_INT >= 26) c.startForegroundService(i);
            else c.startService(i);
        } catch (Throwable e) {
            Log.w(TAG, "启动后台提醒服务失败: " + e);
        }
    }

    public static void stop(Context c) {
        try { c.stopService(new Intent(c, BgService.class)); } catch (Throwable ignored) {}
    }

    /** 设置里的「息屏提醒」开关（cb_cfg.bgn，默认开） */
    public static boolean enabled(Context c) {
        try {
            String raw = c.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
                    .getString("cfg_raw", "");
            if (raw == null || raw.length() == 0) return true;
            JSONObject o = new JSONObject(raw);
            return !o.has("bgn") || o.getBoolean("bgn");
        } catch (Throwable e) {
            return true;
        }
    }

    @Override
    public void onCreate() {
        super.onCreate();
        RUNNING = true;
        try { ua = "CodebossShell/" + getPackageManager()
                .getPackageInfo(getPackageName(), 0).versionName + " bg"; } catch (Throwable ignored) {}
        lastId = getSharedPreferences(PREFS, MODE_PRIVATE).getLong(KEY_LAST, -1);
        BG_TOK = getSharedPreferences(PREFS, MODE_PRIVATE).getString("bgd_tok", "");
        ensureChannels();
        startForeground(NOTIF_ID, serviceNotification());
        /* 回答同机另一家的询问：这条提醒我认领了（要出声）就回结果码 1 */
        try {
            arbRx = new BroadcastReceiver() {
                @Override public void onReceive(Context c, Intent i) {
                    try {
                        long id = i.getLongExtra("id", -1);
                        if (id >= 0 && claimed(i.getStringExtra("sid"), id)) setResultCode(1);
                    } catch (Throwable ignored) {}
                }
            };
            registerReceiver(arbRx, new IntentFilter(ARB_ACTION));
        } catch (Throwable e) { Log.w(TAG, "认领 receiver 注册失败: " + e); }
        /* 这里**不再初始化系统朗读**：播报音色是用户在「播报风格」里选的那一个，
           只有开发机合成的音频对得上；系统朗读是手机自带的另一个嗓音，一降级就
           变成「听到了别人的声音」。念不出来就不念，见 playRemoteSay。 */
        /* 开机/重启服务时先收一遍合成语音缓存（后台线程，别拖慢 onCreate） */
        try {
            new Thread(new Runnable() {
                @Override public void run() { pruneSayCache(); }
            }).start();
        } catch (Throwable ignored) { }
        Log.i(TAG, "onCreate ver=" + ua + " last=" + lastId);
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        startForeground(NOTIF_ID, serviceNotification());   // 每次 start 都保证前台态
        ensureWorker();
        main.removeCallbacks(saTick);
        main.post(saTick);                                  // 系统静音态跟着免打扰时段走
        if (!enabled(this)) { stopSelf(); return START_NOT_STICKY; }
        /* 通知按钮「允许/拒绝」：PendingIntent 落到这里，网络和读屏都在工作线程做，
           别占主线程（开发机要挪光标 + 重读屏幕，可能要一两秒）。 */
        if (intent != null && intent.getBooleanExtra("pick", false)) {
            final String sid = intent.getStringExtra("sid") == null ? "" : intent.getStringExtra("sid");
            final String act = intent.getStringExtra("action") == null ? "" : intent.getStringExtra("action");
            final String rid = intent.getStringExtra("rid") == null ? "" : intent.getStringExtra("rid");
            if (sid.length() > 0) {
                new Thread(new Runnable() {
                    @Override public void run() { pick(sid, act, rid); }
                }, "cb-pick").start();
            }
        }
        return START_STICKY;
    }

    /** 通知按钮：允许/拒绝。选哪一项由开发机按屏幕上的文字现认，
        认不出（或两个方向都像）就不动作，回「点开处理」——不猜。 */
    private void pick(String sid, String action, String rid) {
        String base = baseUrl();
        if (base == null || base.length() == 0) return;
        HttpURLConnection c = null;
        try {
            URL u = new URL(base + "/api/ext_dlg_pick");
            c = (HttpURLConnection) u.openConnection();
            c.setConnectTimeout(12000);
            c.setReadTimeout(25000);          // 挪光标 + 逐次核对，比普通键慢
            c.setRequestMethod("POST");
            c.setDoOutput(true);
            c.setRequestProperty("Content-Type", "application/json");
            c.setRequestProperty("User-Agent", ua);
            String cookie = cookieFor(base + "/api/ext_dlg_pick");
            if (cookie != null && cookie.length() > 0) c.setRequestProperty("Cookie", cookie);
            JSONObject b = new JSONObject();
            b.put("sid", sid);
            b.put("action", action);
            b.put("rid", rid);
            b.put("cid", "nb-" + rid + "-" + action);   // 重复点同一按钮只执行一次
            byte[] out = b.toString().getBytes("UTF-8");
            c.setFixedLengthStreamingMode(out.length);
            c.getOutputStream().write(out);
            int code = c.getResponseCode();
            InputStream in = code >= 400 ? c.getErrorStream() : c.getInputStream();
            StringBuilder sb = new StringBuilder();
            if (in != null) {
                BufferedReader br = new BufferedReader(new InputStreamReader(in, "UTF-8"));
                String ln;
                while ((ln = br.readLine()) != null) sb.append(ln);
            }
            JSONObject r = new JSONObject(sb.toString());
            boolean ok = r.optBoolean("ok", false);
            resultNotify(sid, ok, action, r.optString("error", ""));
            Log.i(TAG, "pick sid=" + sid + " " + action + " ok=" + ok
                    + " " + r.optString("reason", ""));
        } catch (Throwable e) {
            resultNotify(sid, false, action, "没连上电脑（网络/通道断了）");
            Log.w(TAG, "pick failed: " + e);
        } finally {
            try { if (c != null) c.disconnect(); } catch (Throwable ignored) {}
        }
    }

    /** 决定的结果必须看得见：成功/失败都替换掉那条确认通知，失败可点开去处理。 */
    private void resultNotify(String sid, boolean ok, String action, String err) {
        try {
            NotificationManager nm = (NotificationManager) getSystemService(NOTIFICATION_SERVICE);
            if (nm == null) return;
            String what = "allow".equals(action) ? "允许" : "拒绝";
            Intent it = new Intent(this, MainActivity.class);
            it.addFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP);
            it.putExtra("sid", sid);
            int flags = PendingIntent.FLAG_UPDATE_CURRENT;
            if (Build.VERSION.SDK_INT >= 23) flags |= PendingIntent.FLAG_IMMUTABLE;
            PendingIntent pi = PendingIntent.getActivity(this, sid.hashCode(), it, flags);
            Notification.Builder nb = (Build.VERSION.SDK_INT >= 26)
                    ? new Notification.Builder(this, CH_QUIET) : new Notification.Builder(this);
            nb.setSmallIcon(R.drawable.ic_stat_codeboss)
              .setContentTitle(ok ? "已" + what : "没能" + what + "，点开处理")
              .setContentText(ok ? "电脑已收到决定"
                      : (err.length() > 0 ? err : "请打开 App 处理"))
              .setAutoCancel(true)
              .setWhen(System.currentTimeMillis())
              .setContentIntent(pi);
            nm.notify(("cb_" + sid).hashCode(), nb.build());
        } catch (Throwable e) {
            Log.w(TAG, "resultNotify failed: " + e);
        }
    }

    @Override
    public void onDestroy() {
        alive = false;
        RUNNING = false;
        try { if (conn != null) conn.disconnect(); } catch (Throwable ignored) {}
        if (worker != null) worker.interrupt();
        main.removeCallbacks(saTick);
        releaseCue();
        try { if (arbRx != null) unregisterReceiver(arbRx); } catch (Throwable ignored) {}
        arbRx = null;
        Log.i(TAG, "onDestroy");
        super.onDestroy();
    }

    @Override
    public IBinder onBind(Intent intent) { return null; }

    private void ensureWorker() {
        if (worker != null && worker.isAlive()) return;
        worker = new Thread(new Runnable() {
            @Override public void run() { loop(); }
        }, "cb-bg-push");
        worker.setDaemon(true);
        worker.start();
    }

    // ---- SSE 客户端 ----
    private void loop() {
        int backoff = 2;
        int fails = 0;               // 连续失败次数：多了就把常驻通知改成「通道断开」（看得见）
        while (alive) {
            long t0 = System.currentTimeMillis();
            try {
                // 页面在前台时已经提醒过的 id：重连时从它之后开始，别补发成两遍
                long seen = getSharedPreferences(PREFS, MODE_PRIVATE).getLong("bg_seen_id", 0);
                if (seen > lastId) { lastId = seen; saveLast(); }
                String base = baseUrl();
                if (base == null || base.length() == 0) { sleep(15000); continue; }
                String url = base + "/api/bg_push" + (lastId >= 0 ? ("?since=" + lastId) : "");
                String cookie = cookieFor(base + "/api/bg_push");
                HttpURLConnection c = (HttpURLConnection) new URL(url).openConnection();
                conn = c;
                c.setConnectTimeout(12000);
                c.setReadTimeout(75000);          // 服务端 15s 一个心跳
                c.setRequestProperty("Accept", "text/event-stream");
                c.setRequestProperty("User-Agent", ua);
                c.setRequestProperty("Cache-Control", "no-store");
                if (cookie != null && cookie.length() > 0) c.setRequestProperty("Cookie", cookie);
                if (BG_TOK.length() > 0) c.setRequestProperty("X-CB-Bgd", BG_TOK);
                int code = c.getResponseCode();
                if (code != 200) {
                    Log.i(TAG, "bg_push HTTP " + code + " cookie=" + (cookie == null ? 0 : cookie.length())
                            + " tok=" + BG_TOK.length());
                    c.disconnect();
                    fails++;
                    if (fails == 5) notifySvc("通知通道断开（HTTP " + code + "），点开修复");
                    sleep(Math.min(60000, backoff * 1000L));
                    backoff = Math.min(60, backoff * 2);
                    continue;
                }
                backoff = 2;
                readSse(c);
            } catch (Throwable e) {
                if (alive) Log.i(TAG, "bg_push 断开: " + e);
                fails++;
                if (fails == 5) notifySvc("通知通道断开，点开修复");
            } finally {
                try { if (conn != null) conn.disconnect(); } catch (Throwable ignored) {}
                conn = null;
            }
            long ran = System.currentTimeMillis() - t0;
            if (!alive) break;
            // 活了 15 秒以上算真连上过：失败计数清零，常驻通知恢复「运行中」
            if (ran > 15000) {
                if (fails >= 5) notifySvc(null);
                fails = 0;
            }
            // 连接很快就失败时退避；正常跑过一段（55 分钟退役）就立刻重连
            sleep(ran > 60000 ? 500 : Math.min(60000, backoff * 1000L));
            if (ran <= 60000) backoff = Math.min(60, backoff * 2);
        }
    }

    private void readSse(HttpURLConnection c) throws Exception {
        BufferedReader br = new BufferedReader(new InputStreamReader(c.getInputStream(), "UTF-8"));
        String ev = "";
        StringBuilder data = new StringBuilder();
        String line;
        while (alive && (line = br.readLine()) != null) {
            if (line.length() == 0) {
                if ("notify".equals(ev)) handleNotify(data.toString());
                else if ("hello".equals(ev)) handleHello(data.toString());
                ev = "";
                data.setLength(0);
                continue;
            }
            if (line.startsWith(":")) continue;              // 注释/保活
            if (line.startsWith("event:")) ev = line.substring(6).trim();
            else if (line.startsWith("data:")) data.append(line.substring(5).trim());
        }
    }

    private void handleHello(String s) {
        try {
            long seq = new JSONObject(s).optLong("seq", -1);
            // host 重启后序号归零：本地记的旧 id 比它还大 → 从当前开始
            if (seq >= 0 && lastId > seq) { lastId = seq; saveLast(); }
            if (seq >= 0) {   // 同一个道理：页面那边的「已响过」水位也要退回本代
                SharedPreferences sp = getSharedPreferences(PREFS, MODE_PRIVATE);
                if (sp.getLong(KEY_ANNOUNCED, -1) > seq)
                    sp.edit().putLong(KEY_ANNOUNCED, seq).apply();
            }
        } catch (Throwable ignored) {}
    }

    private void handleNotify(String s) {
        JSONObject d;
        try { d = new JSONObject(s); } catch (Throwable e) { return; }
        long id = d.optLong("id", -1);
        if (id >= 0 && id > lastId) { lastId = id; saveLast(); }
        String sid = d.optString("sid", "");
        if (sid.length() == 0) return;
        /* 补发：超过 3 分钟的不再出声、也不再发通知（那一刻早过去了），只留水位。
           和页面那条 age>180 同一条线，避免重连时把一屏旧提醒一起炸出来。 */
        long ts = d.optLong("ts", 0);
        long nowS = System.currentTimeMillis() / 1000;
        if (ts > 0 && nowS - ts > 180) {
            Log.i(TAG, "旧补发不出声 id=" + id + " age=" + (nowS - ts));
            return;
        }
        /* 从这里往下，提醒的音和话**全部**由这里负责，不看前台/后台。
           以前前台（或刚切走 6 秒内）会「交给页面」，可页面那时可能已经被
           web.pauseTimers 冻住了——于是谁都没响，亮屏补发时才第一次出声，
           正是「息屏提醒发了、亮屏又念一遍」的来源。页面那边有 announces()
            探测到本壳后会一句都不念，不会再有两边都响的问题。 */
        SharedPreferences sp = getSharedPreferences(PREFS, MODE_PRIVATE);
        boolean text = true, sound = true, voice = true, dnd = false, qmute = false;
        /* 智能提醒是每台手机各管各的（页面「智能提醒」总开关 → cfg_raw.smart）。
           别的手机开着时开发机照样会生成语音，但这台关着的手机不能念——
           开发机层只管「有没有一台要」，设备层管「这一台要不要」。 */
        boolean smart = true;
        try {
            String raw = sp.getString("cfg_raw", "");
            if (raw != null && raw.length() > 0) {
                JSONObject o = new JSONObject(raw);
                text = !o.has("ntext") || o.getBoolean("ntext");
                sound = !o.has("nsound") || o.getBoolean("nsound");
                voice = !o.has("nvoice") || o.getBoolean("nvoice");
                smart = !o.has("smart") || o.getBoolean("smart");
                /* 免打扰时段：照常弹通知（走静音渠道），但不响提示音、不念语音。
                   页面 onNotify 里判同一套 keys；可后台收 /api/bg_push 时页面可能已被
                   web.pauseTimers 冻住，所以本服务必须自己再判一遍。 */
                dnd = inDnd(o);
                /* 静音（长按音量下键切的那个开关）：和免打扰同样只降声音，
                   通知照弹（走静音渠道）。页面已冻结时只有这里能判。 */
                qmute = o.optBoolean("mute", false);
                if (dnd || qmute) { sound = false; voice = false; }
            }
        } catch (Throwable ignored) {}
        /* 同机装了另一家 Boss App：同一条提醒别两家一起响（回声/重音）。
           要出声就先认领；另一家来问时我们会答「接了」。另一边（agentboss）
           出声前问一声，对面接了这条就不响不念，只弹静音通知。 */
        boolean noisy = !dnd && !qmute && smart && (voice || sound);
        if (noisy) claimId(sid, id);
        if (noisy && peerClaimed(sid, id)) {
            sound = false; voice = false;
            Log.i(TAG, "这条由同机另一家出，本机不出声 id=" + id);
        }
        String kind = d.optString("kind", "done");
        String title = d.optString("title", "");
        String body = d.optString("text", "");
        String snd = d.optString("sound", kind);
        /* 本机智能提醒关着：开发机可能因为别的手机开着做了智能判断（完成→待确认），
           这台手机按规则结果提示（kind_rule/sound_rule 是开发机带的原始规则值）。 */
        if (!smart) {
            String kr = d.optString("kind_rule", "");
            if (kr.length() > 0) kind = kr;
            String sr = d.optString("sound_rule", "");
            if (sr.length() > 0) snd = sr;
        }
        String about = d.optString("about", "");
        /* 开发机念好的整句音频（手机没 TTS 引擎时用）。null = 老开发机根本没这个字段；
           "" = 有新字段但合成失败——这两种要分开：前者退回内置音，后者**不念**。 */
        String sayPath = d.has("say") ? d.optString("say", "") : null;
        boolean wantVoice = voice && smart && d.optBoolean("voice", false);
        String tag = label(kind);
        /* 正在看这个会话：系统通知走静默渠道（通知栏留个痕迹，不出声、不弹横幅），
           但**播报照念**——亮屏亮着不等于人真的在看。念不念只看 voice/smart，
           跟 viewing 无关（用户 2026-11-09：「正在看会话时也要播放语音」）。 */
        boolean viewing = MainActivity.VISIBLE && sid.equals(MainActivity.VIEW_SID);
        /* 响过（或念过）就记水位：补发/重连时靠它去重，页面也靠它判断要不要补喊。 */
        if ((text || wantVoice) && id > sp.getLong(KEY_ANNOUNCED, -1))
            sp.edit().putLong(KEY_ANNOUNCED, id).apply();
        /* 记下这条通知：播报放不出来时要用它补一声提醒 */
        nfSid = sid; nfTitle = title; nfBody = body; nfSnd = snd;
        nfText = text; nfSound = sound; nfSilent = !sound || viewing;
        nfRid = d.optString("rid", "");
        if (text)
            sysNotify(sid, "🔔 " + tag + (title.length() > 0 ? " · " + title : ""),
                    body, snd, nfSilent, nfRid);
        if (wantVoice) {
            /* 提示音和语音别叠在一起：先让通知那声提示音响完（1 秒）再念结论。
               没开提示音就不用等。 */
            final String k2 = kind, ab2 = about, sy2 = sayPath;
            curSid = sid;                    /* 念完回调页面时带上，页面据此判是不是自己等的会话 */
            SAYING = true;                   /* 从现在起（含下载/排队）页面都不该开麦 */
            long wait = sound ? 1000 : 0;
            if (wait == 0) say(k2, ab2, sy2);
            else new android.os.Handler(getMainLooper()).postDelayed(
                    new Runnable() { @Override public void run() { say(k2, ab2, sy2); } }, wait);
        }
        Log.i(TAG, "后台提醒 id=" + id + " kind=" + kind + " text=" + text
                + " voice=" + wantVoice + (dnd ? " dnd=1" : "")
                + (sound ? "" : " sound=0"));
    }

    private void saveLast() {
        getSharedPreferences(PREFS, MODE_PRIVATE).edit().putLong(KEY_LAST, lastId).apply();
    }

    /** "22:00" → 1320（当天第几分钟）；格式不对返回 -1。和页面 _dndHM 同一套规则。 */
    private static int hhmm(String s) {
        if (s == null) return -1;
        s = s.trim();
        int c = s.indexOf(':');
        if (c <= 0) return -1;
        try {
            int h = Integer.parseInt(s.substring(0, c).trim());
            int m = Integer.parseInt(s.substring(c + 1).trim());
            if (h < 0 || h > 23 || m < 0 || m > 59) return -1;
            return h * 60 + m;
        } catch (Throwable e) { return -1; }
    }

    /** 配置里的免打扰时段是否覆盖「此刻」；结束早于开始表示跨午夜（22:00→08:00）。 */
    private static boolean inDnd(JSONObject o) {
        try {
            if (o == null || !o.optBoolean("dnd", false)) return false;
            int f = hhmm(o.optString("dndF", "22:00"));
            int t = hhmm(o.optString("dndT", "08:00"));
            if (f < 0 || t < 0 || f == t) return false;
            java.util.Calendar c = java.util.Calendar.getInstance();
            int m = c.get(java.util.Calendar.HOUR_OF_DAY) * 60 + c.get(java.util.Calendar.MINUTE);
            return f < t ? (m >= f && m < t) : (m >= f || m < t);
        } catch (Throwable e) { return false; }
    }

    /* ---- App 的静音/免打扰 = 整机的静音键（用户 2026-11-15：App 退出也不恢复）----
       目标态 = cfg.mute || 免打扰时段覆盖此刻。进：铃声/通知/闹钟/媒体四条音量
       全部归零 + 铃声模式静音（只切模式有的 ROM 渠道声照响，只零音量有的 ROM
       模式卡住——两个都做）；出：**按 App 设置页的滑条刻度**把音量写回去——
       App 滑条就是系统音量的真值（遥控器模型），不再记/还系统旧值：之前版本
       卡过的脏状态（比如媒体停在 0）装上新版即自愈。
       驱动点：① 服务里 30 秒一查（免打扰时段进出、黑屏页面冻住也能跟）；
       ② 页面改完开关立刻调 CB.applySysAudio（即时生效，不等 30 秒）。 */
    private static final String SA_WE = "sysaudio_we_silent";
    private final Runnable saTick = new Runnable() {
        @Override public void run() {
            enforceSysAudio(BgService.this);
            main.postDelayed(this, 30 * 1000);
        }
    };

    /** 按百分比设某条流音量（pct 0~100）。铃声/通知的 0 值必须带
        FLAG_ALLOW_RINGER_MODES，否则系统认为「0 会切铃声模式」直接钳掉。 */
    private static void setVolPct(android.media.AudioManager am, int stream, double pct) {
        try {
            int mx = am.getStreamMaxVolume(stream);
            if (mx <= 0) return;
            am.setStreamVolume(stream, (int) Math.round(pct / 100.0 * mx),
                    android.media.AudioManager.FLAG_ALLOW_RINGER_MODES);
            if (pct <= 0 && am.getStreamVolume(stream) > 0)
                am.adjustStreamVolume(stream, android.media.AudioManager.ADJUST_MUTE, 0);
        } catch (Throwable ignored) {}              // 有的 ROM 个别流不让设：跳过
    }

    private static double cfgPct(JSONObject o, String key, double dft) {
        try {
            double v = o == null ? dft : o.optDouble(key, dft);
            if (v < 0 || v > 100) v = dft;
            return v;
        } catch (Throwable e) { return dft; }
    }

    /** 系统现在真的「静着」吗：铃声模式 + 四条流音量都读真值。
        不信任 SA_WE 记账——CodeBoss / AgentBoss 同装时两家写同一套系统音量，
        另一家的滑条拖一下（0 值写铃声流会顺手解除铃声静音）就能把这边静音
        悄悄打开，而这边记账还「以为已静音，不用动」（2026-11-21 用户：别打架）。 */
    private static boolean sysSilentNow(android.media.AudioManager am) {
        try {
            if (am.getRingerMode() != android.media.AudioManager.RINGER_MODE_SILENT) return false;
            return am.getStreamVolume(android.media.AudioManager.STREAM_RING) == 0
                    && am.getStreamVolume(android.media.AudioManager.STREAM_NOTIFICATION) == 0
                    && am.getStreamVolume(android.media.AudioManager.STREAM_ALARM) == 0
                    && am.getStreamVolume(android.media.AudioManager.STREAM_MUSIC) == 0;
        } catch (Throwable e) { return false; }
    }

    public static void enforceSysAudio(android.content.Context ctx) {
        try {
            android.media.AudioManager am = (android.media.AudioManager)
                    ctx.getSystemService(android.content.Context.AUDIO_SERVICE);
            if (am == null) return;
            SharedPreferences sp = ctx.getSharedPreferences(PREFS, MODE_PRIVATE);
            JSONObject o = null;
            try {
                String raw = sp.getString("cfg_raw", "");
                if (raw != null && raw.length() > 0) o = new JSONObject(raw);
            } catch (Throwable ignored) {}
            boolean want = o != null && (o.optBoolean("mute", false) || inDnd(o));
            boolean we = sp.getBoolean(SA_WE, false);
            if (!want) {
                /* 恢复只在「账上记着是我们静的」时做：系统静音可能是另一家 App 要的，
                   不替它打开（那边一关，这家下次 tick 会跟着恢复）。 */
                if (we) restoreSysAudio(ctx);
                return;
            }
            /* 静音方向是**电平**：每次 tick 跟系统真值比对，漂了就重写。
               只要还有一家要静音，整机就是静的——同装的两家不会互相把静音弄丢。 */
            if (!we) sp.edit().putBoolean(SA_WE, true).apply();
            if (sysSilentNow(am)) return;
            setVolPct(am, android.media.AudioManager.STREAM_RING, 0);
            setVolPct(am, android.media.AudioManager.STREAM_NOTIFICATION, 0);
            setVolPct(am, android.media.AudioManager.STREAM_ALARM, 0);
            setVolPct(am, android.media.AudioManager.STREAM_MUSIC, 0);
            am.setRingerMode(android.media.AudioManager.RINGER_MODE_SILENT);
            Log.i(TAG, "系统静音：提醒音/媒体音量归零 + 铃声模式静音");
        } catch (Throwable e) { Log.w(TAG, "enforceSysAudio: " + e); }
    }

    /** 恢复系统声音：按 App 设置页滑条刻度直接写系统音量 + 铃声模式 NORMAL。
        不做边沿判断——页面显式关静音/关免打扰也走这里（壳里的 SA_WE 记账一旦跟
        实际状态脱节，边沿机就会一直「以为不用动」，用户看到的就是关了静音没声音；
        之前版本升级留下的脏记账就是这么卡死的。写就完了，写完把账清成「没静音」）。
        先翻 NORMAL 再写音量：有的 ROM 切 NORMAL 会顺手重置铃声刻度，音量写在后面不被盖。 */
    public static void restoreSysAudio(android.content.Context ctx) {
        try {
            android.media.AudioManager am = (android.media.AudioManager)
                    ctx.getSystemService(android.content.Context.AUDIO_SERVICE);
            if (am == null) return;
            SharedPreferences sp = ctx.getSharedPreferences(PREFS, MODE_PRIVATE);
            JSONObject o = null;
            try {
                String raw = sp.getString("cfg_raw", "");
                if (raw != null && raw.length() > 0) o = new JSONObject(raw);
            } catch (Throwable ignored) {}
            am.setRingerMode(android.media.AudioManager.RINGER_MODE_NORMAL);
            double alert = cfgPct(o, "alertVol", 60);    // 提醒音滑条 → 铃声/通知/闹钟
            double speech = cfgPct(o, "speechVol", 90);  // 语音播报滑条 → 媒体
            setVolPct(am, android.media.AudioManager.STREAM_RING, alert);
            setVolPct(am, android.media.AudioManager.STREAM_NOTIFICATION, alert);
            setVolPct(am, android.media.AudioManager.STREAM_ALARM, alert);
            setVolPct(am, android.media.AudioManager.STREAM_MUSIC, speech);
            sp.edit().putBoolean(SA_WE, false).apply();
            Log.i(TAG, "系统恢复：按 App 刻度还原音量（提醒 " + (int) alert
                    + "% 媒体 " + (int) speech + "%）");
        } catch (Throwable e) { Log.w(TAG, "restoreSysAudio: " + e); }
    }

    // ---- 同机双 App 的提醒去重（2026-11-21 用户：两家一起响，回声/重音） ----
    /* 两家的 BgService 各连一条 /api/bg_push，同一条提醒（同一个 id）会各响一遍、
       各念一遍。规则：占优的产品线（codeboss）念；另一家出声之前先向对方问一句
       「这条你接了吗」——对方答「接了」就只弹静音通知，不响不念。问法是对对方
       包发有序广播、接收方用结果码回答；对方没装/没开后台提醒没人接（结果码
       0），照常出声。认领表只在本进程内存里，服务重启丢了也就多一条重复。 */
    private static final String ARB_ACTION = "com.codeboss.peer.ANNOUNCE_CLAIM";
    /* 键是「会话 id + 提醒序号」：两个 App 用不同账号/不同开发机时序号会撞车，
       不能只认序号。 */
    private static final java.util.HashMap<String, Long> CLAIMS = new java.util.HashMap<>();
    private BroadcastReceiver arbRx;

    /** 认领一条提醒（3 分钟内有效）：另一家来问时据此回答。 */
    private static void claimId(String sid, long id) {
        if (id < 0) return;
        String k = sid + "#" + id;
        long now = System.currentTimeMillis();
        synchronized (CLAIMS) {
            CLAIMS.put(k, now);
            if (CLAIMS.size() > 60) {
                java.util.Iterator<java.util.Map.Entry<String, Long>> it = CLAIMS.entrySet().iterator();
                while (it.hasNext()) {
                    if (now - it.next().getValue() > 180000) it.remove();
                }
            }
        }
    }

    private static boolean claimed(String sid, long id) {
        synchronized (CLAIMS) {
            Long t = CLAIMS.get(sid + "#" + id);
            return t != null && System.currentTimeMillis() - t < 180000;
        }
    }

    /** 这条提醒是否已被同机的另一家接了？占优那家不问；对方没装也不问。 */
    private boolean peerClaimed(String sid, long id) {
        String me = "";
        try { me = getString(R.string.cb_product); } catch (Throwable ignored) {}
        if (!"agentboss".equals(me)) return false;      // codeboss 占优：只管认领，不问
        String peer = "com.codeboss.terminal";
        try { getPackageManager().getPackageInfo(peer, 0); }
        catch (Throwable e) { return false; }           // 对面没装：没人抢
        sleep(700);         // 对面也刚收到同一条：给它记上认领的时间（同一台开发机推两条流）
        final CountDownLatch l = new CountDownLatch(1);
        final boolean[] hit = {false};
        BroadcastReceiver rr = new BroadcastReceiver() {
            @Override public void onReceive(Context c, Intent i) {
                hit[0] = getResultCode() != 0;
                l.countDown();
            }
        };
        try {
            Intent q = new Intent(ARB_ACTION);
            q.setPackage(peer);
            q.putExtra("id", id);
            q.putExtra("sid", sid);
            sendOrderedBroadcast(q, null, rr, main, 0, null, null);
        } catch (Throwable e) { return false; }
        try { l.await(400, TimeUnit.MILLISECONDS); } catch (InterruptedException ignored) {}
        try { unregisterReceiver(rr); } catch (Throwable ignored) {}
        return hit[0];
    }

    // ---- 播报的音频焦点（CodeBoss / AgentBoss 同装时别叠音） ----
    /* 两家的播报都是直接 MediaPlayer/TTS start()：同时提醒就是两句话叠在一起。
       播报前要 TRANSIENT_MAY_DUCK 焦点；另一家正在念，会收到「可压低」回调，把
       自己压到 20% 说小点——**不掐断**，播报不许截断（2026-11-20 用户）。
       焦点要不到/放不掉都照播：提醒比礼节重要。 */
    private static android.media.AudioManager.OnAudioFocusChangeListener AF_L;
    static volatile MediaPlayer AF_MP;          /* 当前在出声的播放器（被压低的对象） */

    static void focusForSpeech(Context ctx) {
        try {
            android.media.AudioManager am = (android.media.AudioManager)
                    ctx.getSystemService(Context.AUDIO_SERVICE);
            if (am == null) return;
            if (AF_L == null) {
                AF_L = new android.media.AudioManager.OnAudioFocusChangeListener() {
                    @Override public void onAudioFocusChange(int f) {
                        if (f == android.media.AudioManager.AUDIOFOCUS_LOSS_TRANSIENT_CAN_DUCK)
                            duckSpeech(true);
                        else if (f == android.media.AudioManager.AUDIOFOCUS_GAIN
                                || f == android.media.AudioManager.AUDIOFOCUS_LOSS
                                || f == android.media.AudioManager.AUDIOFOCUS_LOSS_TRANSIENT)
                            duckSpeech(false);
                    }
                };
            }
            int r = am.requestAudioFocus(AF_L,
                    android.media.AudioManager.STREAM_MUSIC,
                    android.media.AudioManager.AUDIOFOCUS_GAIN_TRANSIENT_MAY_DUCK);
            if (r != android.media.AudioManager.AUDIOFOCUS_REQUEST_GRANTED)
                Log.i(TAG, "音频焦点没拿到，照播");
        } catch (Throwable e) { Log.w(TAG, "focusForSpeech: " + e); }
    }

    static void focusDone(Context ctx) {
        try {
            android.media.AudioManager am = (android.media.AudioManager)
                    ctx.getSystemService(Context.AUDIO_SERVICE);
            if (am != null && AF_L != null) am.abandonAudioFocus(AF_L);
        } catch (Throwable ignored) {}
    }

    /** 另一家 App 在念：把自己压到 20%，别关（关了就变成截断）。 */
    private static void duckSpeech(boolean on) {
        android.media.MediaPlayer mp = AF_MP;
        try { if (mp != null) mp.setVolume(on ? 0.2f : 1.0f, on ? 0.2f : 1.0f); }
        catch (Throwable ignored) {}
    }

    private static String label(String kind) {
        if ("perm".equals(kind)) return "需要确认";
        if ("ask".equals(kind)) return "等待回复";
        if ("input".equals(kind)) return "等你输入";
        if ("limit".equals(kind)) return "额度提醒";
        if ("stuck".equals(kind)) return "可能卡住";
        return "任务完成";
    }

    // ---- 网络/凭据 ----
    private String baseUrl() {
        SharedPreferences sp = getSharedPreferences(PREFS, MODE_PRIVATE);
        String b = sp.getString(KEY_BASE, "");
        if (b != null && b.length() > 0) return b;
        String u = sp.getString("url", "");
        try {
            URL x = new URL(u);
            int p = x.getPort();
            return x.getProtocol() + "://" + x.getHost() + (p > 0 ? ":" + p : "");
        } catch (Throwable ignored) {}
        return DEFAULT_BASE;   /* 没有地址（首启还没进页面）：兜底指中继，不指别家产品线 */
    }

    /** CookieManager 必须在有 Looper 的线程调；后台线程等结果 */
    private String cookieFor(final String url) {
        final String[] out = new String[1];
        final CountDownLatch l = new CountDownLatch(1);
        main.post(new Runnable() {
            @Override public void run() {
                try { out[0] = CookieManager.getInstance().getCookie(url); }
                catch (Throwable ignored) {}
                l.countDown();
            }
        });
        try { l.await(5, TimeUnit.SECONDS); } catch (InterruptedException ignored) {}
        return out[0];
    }

    // ---- 系统通知 ----
    private Notification serviceNotification() { return serviceNotification(null); }

    private Notification serviceNotification(String sub) {
        Intent it = new Intent(this, MainActivity.class);
        it.addFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP);
        int flags = PendingIntent.FLAG_UPDATE_CURRENT;
        if (Build.VERSION.SDK_INT >= 23) flags |= PendingIntent.FLAG_IMMUTABLE;
        PendingIntent pi = PendingIntent.getActivity(this, 0, it, flags);
        Notification.Builder nb = (Build.VERSION.SDK_INT >= 26)
                ? new Notification.Builder(this, CH_SVC) : new Notification.Builder(this);
        nb.setSmallIcon(R.drawable.ic_stat_codeboss)
          .setContentTitle("Codeboss")
          .setContentText(sub == null ? "后台提醒运行中（息屏也能收到）" : sub)
          .setOngoing(true)
          .setContentIntent(pi);
        return nb.build();
    }

    /** 通道状态可见：连不上就说连不上，别让人以为「没提醒 = 没事」。 */
    private void notifySvc(String sub) {
        try {
            NotificationManager nm = (NotificationManager) getSystemService(NOTIFICATION_SERVICE);
            if (nm != null) nm.notify(NOTIF_ID, serviceNotification(sub));
        } catch (Throwable ignored) {}
    }

    private PendingIntent pickIntent(String sid, String action, String rid, int req) {
        Intent i = new Intent(this, BgService.class);
        i.putExtra("pick", true);
        i.putExtra("sid", sid);
        i.putExtra("action", action);
        i.putExtra("rid", rid);
        int flags = PendingIntent.FLAG_UPDATE_CURRENT;
        if (Build.VERSION.SDK_INT >= 23) flags |= PendingIntent.FLAG_IMMUTABLE;
        return PendingIntent.getService(this, req, i, flags);
    }

    private void sysNotify(String sid, String title, String body, String sound, boolean silent,
                           String rid) {
        try {
            NotificationManager nm = (NotificationManager) getSystemService(NOTIFICATION_SERVICE);
            if (nm == null) return;
            String chId = CH_DONE;
            if ("alert".equals(sound)) chId = CH_PERM;
            else if ("ping".equals(sound)) chId = CH_INPUT;
            if (silent) chId = CH_QUIET;
            Intent it = new Intent(this, MainActivity.class);
            it.addFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP);
            it.putExtra("sid", sid);
            int flags = PendingIntent.FLAG_UPDATE_CURRENT;
            if (Build.VERSION.SDK_INT >= 23) flags |= PendingIntent.FLAG_IMMUTABLE;
            PendingIntent pi = PendingIntent.getActivity(this, sid.hashCode(), it, flags);
            String t = (title == null || title.length() == 0) ? "Codeboss" : title;
            String b = body == null ? "" : body;
            Notification.Builder nb = (Build.VERSION.SDK_INT >= 26)
                    ? new Notification.Builder(this, chId) : new Notification.Builder(this);
            nb.setSmallIcon(R.drawable.ic_stat_codeboss)
              .setContentTitle(t)
              .setContentText(b)
              .setStyle(new Notification.BigTextStyle().bigText(b))
              .setAutoCancel(true)
              .setWhen(System.currentTimeMillis())
              .setContentIntent(pi);
            /* 要你确认的这件事，能在通知栏里直接拍板：两个按钮，不用先开 App。
               选哪一项由开发机按屏幕文字现认（不是写死的「1=允许」）。
               锁屏也要能签字：Android 锁屏默认 PRIVATE，正文和按钮一起被藏——
               待拍板通知必须 public；普通完成通知不设，正文不亮锁屏。 */
            if (rid != null && rid.length() > 0) {
                nb.setVisibility(Notification.VISIBILITY_PUBLIC);
                nb.addAction(0, "允许", pickIntent(sid, "allow", rid, sid.hashCode() * 2 + 1));
                nb.addAction(0, "拒绝", pickIntent(sid, "deny", rid, sid.hashCode() * 2 + 2));
            }
            if (silent && Build.VERSION.SDK_INT < 26) {
                nb.setSound(null);
                nb.setVibrate(null);
            }
            nm.notify(("cb_" + sid).hashCode(), nb.build());
        } catch (Throwable e) {
            Log.w(TAG, "notify failed: " + e);
        }
    }

    /** 渠道与 MainActivity 的同名同 id：用户在系统设置里调过的音型不会被重置 */
    private void ensureChannels() {
        if (Build.VERSION.SDK_INT < 26) return;
        try {
            NotificationManager nm = (NotificationManager) getSystemService(NOTIFICATION_SERVICE);
            if (nm == null) return;
            mkChannel(nm, CH_DONE, "任务完成", RingtoneManager.TYPE_NOTIFICATION,
                    new long[]{0, 180});
            mkChannel(nm, CH_PERM, "需要确认", RingtoneManager.TYPE_ALARM,
                    new long[]{0, 300, 150, 300});
            mkChannel(nm, CH_INPUT, "等待回复", RingtoneManager.TYPE_NOTIFICATION,
                    new long[]{0, 120, 100, 120});
            android.app.NotificationChannel q = new android.app.NotificationChannel(
                    CH_QUIET, "静默提醒", NotificationManager.IMPORTANCE_DEFAULT);
            q.setDescription("正在看的会话，只留通知不出声");
            q.setSound(null, null);
            q.enableVibration(false);
            nm.createNotificationChannel(q);
            android.app.NotificationChannel svc = new android.app.NotificationChannel(
                    CH_SVC, "后台提醒服务", NotificationManager.IMPORTANCE_LOW);
            svc.setDescription("保持息屏时也能收到提醒；关掉开关即停");
            svc.setSound(null, null);
            svc.enableVibration(false);
            nm.createNotificationChannel(svc);
        } catch (Throwable ignored) {}
    }

    private void mkChannel(NotificationManager nm, String id, String name,
                           int toneType, long[] pattern) {
        android.app.NotificationChannel ch = new android.app.NotificationChannel(
                id, name, NotificationManager.IMPORTANCE_HIGH);
        ch.setDescription("Codeboss 任务提醒");
        ch.enableVibration(true);
        ch.setVibrationPattern(pattern);
        try {
            android.net.Uri u = RingtoneManager.getDefaultUri(toneType);
            if (u != null) {
                ch.setSound(u, new AudioAttributes.Builder()
                        .setUsage(AudioAttributes.USAGE_NOTIFICATION)
                        .setContentType(AudioAttributes.CONTENT_TYPE_SONIFICATION)
                        .build());
            }
        } catch (Throwable ignored) {}
        nm.createNotificationChannel(ch);
    }

    // ---- 语音播报 ----
    /** 开发机带了念好的整句（say）就放它。sayPath=null 表示老开发机没有这个字段，
        退回内置真人音；sayPath="" 表示新开发机合成失败——不念，别拿别的嗓音顶。 */
    private void say(String kind, String about, String sayPath) {
        synchronized (this) {
            if (sayBusy) {                   /* 正在念：排后面，绝不半路掐断 */
                qHas = true;
                qKind = kind; qAbout = about; qPath = sayPath; qSid = curSid;
                return;
            }
            sayBusy = true;
        }
        sayNow(kind, about, sayPath);
    }

    /** 这一句念完了（放完或失败都算）：排队里还有就接着念，没有才收工。
        所有结束出口都汇到这里——各自收尾的话，排队那句会被吞掉，或者页面一直等开麦。 */
    private void sayFinished() {
        boolean next;
        String k, a, p, s;
        synchronized (this) {
            next = qHas;
            qHas = false;
            sayBusy = next;
            k = qKind; a = qAbout; p = qPath; s = qSid;
            if (!next) SAYING = false;
        }
        if (next) { curSid = s; sayNow(k, a, p); return; }
        AF_MP = null;
        focusDone(this);                 /* 念完了放手：另一家被压低的音量恢复 */
        MainActivity.notifySayDone(curSid);
    }

    private void sayNow(String kind, String about, String sayPath) {
        if (sayPath == null) {
            /* 老开发机不带 say：只有内置真人音（固定几句），这是历史兜底，不参与风格 */
            playCue(kind);
            return;
        }
        String p = sayPath.trim();
        if (p.length() > 0) { playRemoteSay(p, kind, about); return; }
        /* 有新字段但是空的：开发机这次没念出来（中继超时/没绑账号）。宁可不出声，
           也不要换成系统朗读或预录音——那正是「怎么变成别人的声音」的来源。
           但提醒不能因此整条消失：补一声通知提示音（不是换嗓音）。 */
        Log.i(TAG, "这条不念了（开发机没合成出音频）: kind=" + kind);
        sayFailed(kind);
        sayFinished();                   /* 不出声也别让页面一直等（队列里有就接着念） */
    }

    /** 播报这一步没放出来（合成音频取不到/放不了）：用通知提示音补一声，
        别让这一条提醒彻底无声无息。只补「原本静音了的」那条通知——已经响过
        提示音的就不重复响。页面侧留一行日志（Nsayfail），下次能一眼定位。 */
    private void sayFailed(String kind) {
        Log.w(TAG, "播报没放出来，补一声提示音: kind=" + kind);
        if (nfText && nfSound && nfSilent) {
            nfSilent = false;
            sysNotify(nfSid, "🔔 " + label(kind)
                            + (nfTitle.length() > 0 ? " · " + nfTitle : ""),
                      nfBody, nfSnd, false, nfRid);
        }
        MainActivity.notifySayFail(curSid, kind);
    }

    /** 开发机合成的整句（m4a/wav）：带 cookie 下到缓存再放。
        多亲这类机器一个朗读引擎都没装，预录音又只有「完成了」三个字——
        整句「这一轮在做什么，完成了」只能由开发机念好再传下来。 */
    private void playRemoteSay(final String path, final String kind, final String about) {
        final String url = path.startsWith("http") ? path : baseUrl() + path;
        new Thread(new Runnable() {
            @Override public void run() {
              String name = "say2_" + Integer.toHexString(path.hashCode()) + ".bin";
              File f = new File(getCacheDir(), name);
              /* 2026-11-24 第二轮：换名 say_ → say2_。0.11.37 之前下载不核对长度，
                 连接被中继重启/断链干净掐断时残件 ≥500B 就被当完整文件缓存（m4a 是
                 moov-first，缺尾 = 念半截没声），且每次播放还会刷新 3 天 TTL——毒缓存
                 近乎永生。换名让存量毒件全部失配，跟着 TTL 自然淘汰。 */
              File tmp = new File(getCacheDir(), name + ".part");
              for (int attempt = 1; attempt <= 2; attempt++) {
                try {
                    if (!f.exists() || f.length() < 500) {
                        java.net.HttpURLConnection c =
                                (java.net.HttpURLConnection) new URL(url).openConnection();
                        c.setConnectTimeout(6000);
                        c.setReadTimeout(10000);
                        c.setRequestProperty("User-Agent", ua);
                        String ck = cookieFor(url);
                        if (ck != null && ck.length() > 0) c.setRequestProperty("Cookie", ck);
                        if (BG_TOK.length() > 0) c.setRequestProperty("X-CB-Bgd", BG_TOK);
                        int code = c.getResponseCode();
                        if (code != 200) throw new java.io.IOException("http " + code);
                        long clen = c.getContentLength();   /* -1=分块无长度，只能放行 */
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
                        /* 干净断链会让 read() 正常返回 -1 而不抛异常：只有对上
                           Content-Length 才算完整，否则重试，绝不把残件转正当名 */
                        if (clen >= 0 && tmp.length() != clen)
                            throw new java.io.IOException("short " + tmp.length() + "/" + clen);
                        if (tmp.length() < 500) throw new java.io.IOException("empty");
                        if (!tmp.renameTo(f)) throw new java.io.IOException("rename");
                    }
                    try { f.setLastModified(System.currentTimeMillis()); } catch (Throwable ignored) { }
                    sayPlaying = f;
                    playFile(f, kind);
                    pruneSayCache();          /* 新句子落盘后顺手收一次旧文件 */
                    return;
                } catch (Throwable e) {
                    Log.w(TAG, "取合成语音失败(" + attempt + "/2): " + e);
                    try { tmp.delete(); } catch (Throwable ignored) {}
                    if (attempt < 2) {
                        try { Thread.sleep(1500); } catch (InterruptedException ignored) {}
                    }
                }
              }
              /* 两次都取不到：**不换别的嗓音顶替**。系统朗读和预录音都不是用户在
                 「播报风格」里选的那个音色，一降级就变成「怎么是别人的声音」。
                 宁可只留提示音 + 通知，也不放错声音。 */
              Log.w(TAG, "这条不念了（音频取不到）: kind=" + kind + " about=" + about);
              sayFailed(kind);                      /* 别整条静默：补一声提示音 */
              sayFinished();                        /* 没声也要让页面知道「轮到它了」 */
            }
        }).start();
    }

    /** 收合成语音缓存：超过 3 天的删，最多留 40 个（按最近使用时间），正在放的不动。
        文件名是「句子哈希」，同一句话还是能命中复用——只是不会再无限长下去。 */
    private void pruneSayCache() {
        try {
            File[] fs = getCacheDir().listFiles();
            if (fs == null) return;
            long now = System.currentTimeMillis();
            java.util.ArrayList<File> keep = new java.util.ArrayList<File>();
            for (File f : fs) {
                String n = f.getName();
                /* .part 是没下完的临时件（进程被杀会留下）：跟着 .bin 一起按 TTL 收走。
                   say_ 前缀是 0.11.38 之前的旧缓存（含毒件），一并按 TTL 收走 */
                if ((!n.startsWith("say_") && !n.startsWith("say2_"))
                        || (!n.endsWith(".bin") && !n.endsWith(".part"))) continue;
                File cur = sayPlaying;
                if (cur != null && cur.getAbsolutePath().equals(f.getAbsolutePath())) continue;
                if (now - f.lastModified() > SAY_TTL_MS) {
                    try { f.delete(); } catch (Throwable ignored) { }
                    continue;
                }
                keep.add(f);
            }
            if (keep.size() <= SAY_MAX) return;
            java.util.Collections.sort(keep, new java.util.Comparator<File>() {
                @Override public int compare(File a, File b) {
                    return Long.valueOf(b.lastModified()).compareTo(a.lastModified());
                }
            });
            for (int i = SAY_MAX; i < keep.size(); i++) {
                try { keep.get(i).delete(); } catch (Throwable ignored) { }
            }
        } catch (Throwable ignored) { }
    }

    private void playFile(File f, String kind) {
        try {
            releaseCue();
            final MediaPlayer mp = new MediaPlayer();
            mp.setAudioAttributes(new AudioAttributes.Builder()
                    .setUsage(AudioAttributes.USAGE_MEDIA)
                    .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH)
                    .build());
            mp.setDataSource(f.getAbsolutePath());
            final File bad = f;                 /* onError 里捕获用 */
            mp.setOnCompletionListener(new MediaPlayer.OnCompletionListener() {
                @Override public void onCompletion(MediaPlayer m) {
                    try { m.release(); } catch (Throwable ignored) {}
                    if (cueMp == m) cueMp = null;
                    if (AF_MP == m) AF_MP = null;
                    sayPlaying = null;          /* 放完就允许下次清理它 */
                    sayFinished();              /* 念完了：排队有就接着念，否则页面可以开始自动听写 */
                }
            });
            /* 播放中途的解码错误：MediaPlayer 默认把 error 当 completion 上报，
               听感就是「念半截没了下文」。这里显式接管：坏缓存删掉，下次重取。 */
            mp.setOnErrorListener(new MediaPlayer.OnErrorListener() {
                @Override public boolean onError(MediaPlayer m, int what, int extra) {
                    try { m.release(); } catch (Throwable ignored) {}
                    if (cueMp == m) cueMp = null;
                    if (AF_MP == m) AF_MP = null;
                    sayPlaying = null;
                    try { bad.delete(); } catch (Throwable ignored) {}
                    Log.w(TAG, "播报解码失败 what=" + what + " extra=" + extra);
                    sayFailed(kind);
                    sayFinished();
                    return true;
                }
            });
            mp.prepare();
            /* 音量全归系统媒体流：「语音播报」滑条直接写系统媒体音量（vsVolApplyMedia），
               这里不再自己缩放——以前双重衰减，系统 50% 时滑条拉满也只有一半响 */
            focusForSpeech(this);               /* 另一家的播报这时会被压小，而不是叠音 */
            cueMp = mp;
            AF_MP = mp;
            mp.start();
        } catch (Throwable e) {
            sayPlaying = null;
            Log.w(TAG, "放合成语音失败: " + e);
            sayFailed(kind);                          /* 别整条静默：补一声提示音 */
            sayFinished();                            /* 没念出来也别让页面一直等 */
        }
    }

    // ---- 内置提醒语音 ----
    private void playCue(String kind) {
        String k = kind == null ? "" : kind.trim();
        if (!(k.equals("done") || k.equals("perm") || k.equals("ask")
                || k.equals("input") || k.equals("limit") || k.equals("stuck")
                || k.equals("test"))) k = "done";
        try {
            releaseCue();
            File f = new File(getCacheDir(), "cue_" + k + ".wav");
            if (!f.exists() || f.length() < 1000) {
                InputStream in = getAssets().open("tts/" + k);
                FileOutputStream out = new FileOutputStream(f);
                try {
                    byte[] buf = new byte[8192];
                    int n;
                    while ((n = in.read(buf)) > 0) out.write(buf, 0, n);
                } finally {
                    try { out.close(); } catch (Throwable ignored) {}
                    try { in.close(); } catch (Throwable ignored) {}
                }
            }
            final MediaPlayer mp = new MediaPlayer();
            mp.setAudioAttributes(new AudioAttributes.Builder()
                    .setUsage(AudioAttributes.USAGE_MEDIA)
                    .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH)
                    .build());
            mp.setDataSource(f.getAbsolutePath());
            mp.setOnCompletionListener(new MediaPlayer.OnCompletionListener() {
                @Override public void onCompletion(MediaPlayer m) {
                    try { m.release(); } catch (Throwable ignored) {}
                    if (cueMp == m) cueMp = null;
                    if (AF_MP == m) AF_MP = null;
                    sayFinished();              /* 内置音念完：排队有就接着念，否则页面可以开始自动听写 */
                }
            });
            mp.prepare();
            /* 音量全归系统媒体流（同 playFile，滑条写的是系统媒体音量） */
            focusForSpeech(this);               /* 另一家播报时压小它，别叠音 */
            cueMp = mp;
            AF_MP = mp;
            mp.start();
        } catch (Throwable e) {
            Log.w(TAG, "播报语音失败: " + e);
            sayFinished();
        }
    }

    private void releaseCue() {
        if (cueMp != null) {
            try { cueMp.release(); } catch (Throwable ignored) {}
            if (AF_MP == cueMp) AF_MP = null;
            cueMp = null;
        }
    }

    private static void sleep(long ms) {
        try { Thread.sleep(ms); } catch (InterruptedException ignore) {}
    }
}
