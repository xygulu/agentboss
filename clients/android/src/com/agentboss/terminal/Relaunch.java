// SPDX-License-Identifier: AGPL-3.0-or-later

package com.agentboss.terminal;

import android.app.ActivityManager;
import android.app.AlarmManager;
import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.job.JobInfo;
import android.app.job.JobScheduler;
import android.content.BroadcastReceiver;
import android.content.ComponentName;
import android.content.Context;
import android.content.Intent;
import android.os.Build;
import android.util.Log;

/**
 * 更新装完把自己拉回前台。
 *
 * 覆盖安装会把我们的进程杀掉，装好之后能叫醒我们的路有三条：
 *   ① MY_PACKAGE_REPLACED（系统在包被替换后发给本应用）
 *   ② 安装会话的 INSTALL_STATUS / STATUS_SUCCESS（我们自己 commit 时挂的 PendingIntent）
 *   ③ **装之前就排好的闹钟 / JobScheduler**（本文件 armWake）
 *
 * 实测 ①② 靠不住：AOSP（多亲 Android 9）会把覆盖安装后的后台广播直接跳过
 * （BroadcastQueue: Skipping delivery ... receivers:(1)，进程都没起来）；
 * MIUI 则以「process is not permitted to auto start」拒绝拉起进程，
 * 就算放行，startActivity 还会被「后台弹出界面」拦一道（Permission Denied Activity）。
 * 所以主力是 ③：闹钟和 job 由系统直接叫醒进程，不走后台广播限制，
 * 装完 12/30/75/150 秒各试一次，前面几个点正好覆盖国产 ROM 安装收尾的时间窗。
 * 都叫不醒/拉不起来，就发一条可点的通知兜底。
 */
final class Relaunch {
    private static final String TAG = "Codeboss";
    private static final String CH = "cb_upd";
    private static final int NID = 7788;
    private static final int NID_NOTE = 7789;      /* 装之前挂的「点一下回来」 */

    /** 闹钟/Job 用的动作，ReplacedReceiver 一并收。 */
    static final String ACTION_WAKE = "com.agentboss.terminal.UPD_WAKE";

    /** 拉起界面时带上的标记：MainActivity 据此亮屏 + 显示在锁屏之上（只在更新回来这一趟）。 */
    static final String EXTRA_WAKE = "cb_upd_wake";

    /* 秒，相对装之前的时刻。覆盖安装必然把自己的进程杀掉，系统那声 SUCCESS 状态
       在部分 ROM 上根本送不到（多亲 Pro 实测丢过）——这几路闹钟才是「装完自己回来」
       的保底。第一次 8s 通常还在安装器里（版本没变就 6 秒后再看一眼），
       装完最多 6 秒就能回来，比原来 20 秒一轮快得多。 */
    private static final long[] WAKE_AT = {8, 20, 45, 90};
    private static final int REQ_BASE = 9100;
    private static final int JOB_ID = 9100;

    private static long sLastTry = 0;
    private static volatile long sStartAt = 0;   /* 最近一次「拉起主界面」的时刻 */
    private static volatile long sFgAt = 0;      /* 主界面最近一次 onResume 的时刻 */
    private static final String PREF = "codeboss";
    private static final String KEY_ARM_CODE = "upd_arm_code";   /* 预约时自己的 versionCode */
    private static final String KEY_ARM_TRIES = "upd_arm_tries"; /* 安装还没完成时补排的次数 */
    private static final String KEY_LAUNCH_TS = "upd_launch_ts"; /* 我们主动拉起界面的时刻 */
    private static final int REQ_RETRY = REQ_BASE + 80;
    private static final int MAX_TRIES = 50;                     /* 50×6s ≈ 5 分钟，够用户慢慢点确认 */
    private static final String KEY_TRACE = "upd_trace";

    private Relaunch() {}

    /**
     * 更新后自愈的现场记录：哪一步走了、哪一步没走。
     *
     * 覆盖安装会把进程杀掉，装完能不能自己回来全靠几路系统回调；这些回调在真机上
     * （尤其多亲这种 AOSP 9）有没有被送到，logcat 拿不到就只能靠猜。这里把关键节点
     * 写进 SharedPreferences，下次 App 被打开时页面通过 CB.updTrace() 取走、打到
     * 开发机日志里——「装完什么都没发生」到底是闹钟没响、广播没到、还是拉起来了
     * 又被挡住，一眼能分清。只留最近 1500 字，写完即止，不占空间。
     */
    static void trace(Context c, String s) {
        try {
            android.content.SharedPreferences sp =
                    c.getApplicationContext().getSharedPreferences(PREF, Context.MODE_PRIVATE);
            String line = new java.text.SimpleDateFormat("MM-dd HH:mm:ss", java.util.Locale.US)
                    .format(new java.util.Date()) + " " + s;
            String old = sp.getString(KEY_TRACE, "");
            String all = old + (old.length() > 0 ? "\n" : "") + line;
            if (all.length() > 1500) all = all.substring(all.length() - 1500);
            sp.edit().putString(KEY_TRACE, all).apply();
        } catch (Throwable ignored) { }
    }

    /** 给页面/反馈用的现场记录（CB.updTrace 直接返回它）。 */
    static String traceText(Context c) {
        try {
            return c.getApplicationContext()
                    .getSharedPreferences(PREF, Context.MODE_PRIVATE)
                    .getString(KEY_TRACE, "");
        } catch (Throwable t) {
            return "";
        }
    }

    private static int codeOf(Context c) {
        try {
            return c.getPackageManager().getPackageInfo(c.getPackageName(), 0).versionCode;
        } catch (Throwable t) {
            return -1;
        }
    }

    /** 装之前调用：预约几路「装完叫醒我」。闹钟走系统，不受后台广播/自启动限制。 */
    static void armWake(Context c) {
        Context app = c.getApplicationContext();
        /* 记下现在的版本号：到点醒来先看一眼，还是这个版本说明用户还没装（或取消了），
           别把我们的界面怼到安装确认框上面去 */
        try {
            app.getSharedPreferences(PREF, Context.MODE_PRIVATE).edit()
                    .putInt(KEY_ARM_CODE, codeOf(app)).putInt(KEY_ARM_TRIES, 0)
                    .remove(KEY_LAUNCH_TS).apply();
            trace(app, "装前预约唤醒 code=" + codeOf(app));
        } catch (Throwable ignored) { }
        try {
            for (int i = 0; i < WAKE_AT.length; i++) {
                armOne(app, REQ_BASE + i, WAKE_AT[i] * 1000L);
            }
            Log.i(TAG, "更新后唤醒已预约 " + WAKE_AT.length + " 路");
        } catch (Throwable t) {
            Log.w(TAG, "预约唤醒失败: " + t);
        }
        try {
            JobScheduler js = (JobScheduler) app.getSystemService(Context.JOB_SCHEDULER_SERVICE);
            JobInfo j = new JobInfo.Builder(JOB_ID,
                    new ComponentName(app, WakeJob.class))
                    .setOverrideDeadline(20000)      /* 不管有无约束，20 秒内跑一次 */
                    .build();
            js.schedule(j);
            Log.i(TAG, "更新后唤醒 Job 已排");
        } catch (Throwable t) {
            Log.w(TAG, "排 Job 失败: " + t);
        }
    }

    /** 装完那一刻补排几路「过几秒再试」。覆盖安装会把进程连闹钟一起清掉，
        安装状态广播把我们叫起来后立刻重新排上，这一进程要是又被收走也有下一路。 */
    static void rearm(Context c) {
        try {
            Context app = c.getApplicationContext();
            for (int i = 0; i < 3; i++) armOne(app, REQ_BASE + 40 + i, 6000L * (i + 1) + 4000);
            trace(app, "装完补排 3 路唤醒");
        } catch (Throwable t) {
            Log.w(TAG, "装完补排唤醒失败: " + t);
        }
    }

    /** 排一路闹钟（显式广播，系统直接叫醒进程）。 */
    private static void armOne(Context app, int req, long delayMs) {
        AlarmManager am = (AlarmManager) app.getSystemService(Context.ALARM_SERVICE);
        Intent it = new Intent(app, ReplacedReceiver.class).setAction(ACTION_WAKE);
        int fl = PendingIntent.FLAG_UPDATE_CURRENT;
        if (Build.VERSION.SDK_INT >= 23) fl |= PendingIntent.FLAG_IMMUTABLE;
        PendingIntent pi = PendingIntent.getBroadcast(app, req, it, fl);
        long at = System.currentTimeMillis() + delayMs;
        try {
            am.setExactAndAllowWhileIdle(AlarmManager.RTC_WAKEUP, at, pi);
        } catch (Throwable t) {
            am.setAndAllowWhileIdle(AlarmManager.RTC_WAKEUP, at, pi);
        }
    }

    /** 广播/闹钟/job 里调用。pr 是 goAsync() 的 PendingResult（可为 null）：撑住进程跑完重试。 */
    static void go(final Context ctx, final BroadcastReceiver.PendingResult pr) {
        final Context c = ctx.getApplicationContext();
        long now = System.currentTimeMillis();
        if (now - sLastTry < 1200) {                     /* 几条路同时到，别叠着拉 */
            if (pr != null) try { pr.finish(); } catch (Throwable ignored) { }
            return;
        }
        sLastTry = now;
        try {
            android.content.SharedPreferences sp =
                    c.getSharedPreferences(PREF, Context.MODE_PRIVATE);
            int armed = sp.getInt(KEY_ARM_CODE, 0);
            int cur = codeOf(c);
            trace(c, "唤醒到达 armed=" + armed + " cur=" + cur);
            if (armed > 0 && cur > 0 && armed == cur) {
                /* 用户还没点完安装（或取消了）：隔 20 秒再看一眼，最多 5 分钟。
                   不能直接拉界面——那会把安装确认框顶掉。 */
                int tries = sp.getInt(KEY_ARM_TRIES, 0);
                if (tries < MAX_TRIES) {
                    sp.edit().putInt(KEY_ARM_TRIES, tries + 1).apply();
                    armOne(c, REQ_RETRY, 6000);
                    Log.i(TAG, "唤醒到点但版本没变（" + cur + "），安装还没完成，6 秒后再看");
                } else {
                    sp.edit().remove(KEY_ARM_CODE).remove(KEY_ARM_TRIES).apply();
                    Log.i(TAG, "等太久了，先停（可能用户取消了安装）");
                }
                if (pr != null) try { pr.finish(); } catch (Throwable ignored) { }
                return;
            }
            if (armed > 0) {
                sp.edit().remove(KEY_ARM_CODE).remove(KEY_ARM_TRIES).apply();
            }
        } catch (Throwable ignored) { }
        start(c);
        new Thread(new Runnable() {
            @Override public void run() {
                try {
                    /* 系统刚装完还在收尾，一次拉不起来很常见：隔几秒再补两次，
                       两次都没到前台 → 发通知兜底（goAsync 大约给 10s，别超） */
                    Thread.sleep(2500);
                    if (!inFront(c)) start(c);
                    Thread.sleep(4000);
                    if (!inFront(c)) notifyUser(c);
                } catch (Throwable ignored) {
                } finally {
                    if (pr != null) try { pr.finish(); } catch (Throwable ignored) { }
                }
            }
        }).start();
    }

    /** 主界面 resumed：说明「装完拉回前台」这条路走通了。
        系统进程列表在不少 ROM 上查不到自己（inFront 会误判），用这个标记才准。 */
    static void noteForeground(Context c) {
        sFgAt = System.currentTimeMillis();
        if (c != null) trace(c, "主界面 resumed");
        /* 刚试着拉起过：日志里留一句「确实到前台了」。只看到「更新后拉起主界面」
           不能证明界面出来（有的 ROM 吞掉启动也照样不报错），两句一起才是真的走通。 */
        if (sStartAt > 0 && sFgAt - sStartAt < 120000) {
            Log.i(TAG, "更新后已回到前台");
        }
    }

    private static void start(Context c) {
        sStartAt = System.currentTimeMillis();
        trace(c, "拉起主界面");
        try {
            c.getSharedPreferences(PREF, Context.MODE_PRIVATE).edit()
                    .putLong(KEY_LAUNCH_TS, System.currentTimeMillis()).apply();
        } catch (Throwable ignored) { }
        Intent i = new Intent(c, MainActivity.class);
        i.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_REORDER_TO_FRONT);
        i.putExtra(EXTRA_WAKE, true);
        miuiAllow(i);
        try {
            /* 走 PendingIntent 发出去：MIUI 的「后台弹出界面」校验对「PendingIntent 代发」的
               启动直接放行（它只拦直接从后台 startActivity），比申请那个权限省事。 */
            int fl = PendingIntent.FLAG_UPDATE_CURRENT;
            if (Build.VERSION.SDK_INT >= 23) fl |= PendingIntent.FLAG_IMMUTABLE;
            PendingIntent.getActivity(c, 9200, i, fl).send();
            Log.i(TAG, "更新后拉起主界面");
            return;
        } catch (Throwable t) {
            Log.w(TAG, "PendingIntent 拉起失败: " + t);
        }
        try {
            c.startActivity(i);
            Log.i(TAG, "更新后拉起主界面(direct)");
        } catch (Throwable t) {
            Log.w(TAG, "更新后拉起失败: " + t);
        }
    }

    /** MIUI 的「后台弹出界面」(appop 10021) 没开时，startActivity 会被系统直接丢掉
        （MIUILOG- Permission Denied Activity）。Intent 上的 MIUI flag 0x2 是 MIUI
        给「允许这次启动」留的口子，能用就用；拿不到就正常走，失败会退到通知。 */
    private static void miuiAllow(Intent i) {
        try {
            Intent.class.getMethod("setMiuiFlags", int.class).invoke(i, 2);
        } catch (Throwable ignored) {
        }
    }

    /** 我们自己的进程在不在前台。主界面刚 resumed 过就是准的；
        进程列表在国产 ROM 上经常查不到自己，只当辅助。判不出来按「不在」处理。 */
    private static boolean inFront(Context c) {
        if (System.currentTimeMillis() - sFgAt < 20000) return true;
        try {
            ActivityManager am = (ActivityManager) c.getSystemService(Context.ACTIVITY_SERVICE);
            if (am == null) return false;
            for (ActivityManager.RunningAppProcessInfo p : am.getRunningAppProcesses()) {
                if (p.processName.equals(c.getPackageName()))
                    return p.importance <= ActivityManager.RunningAppProcessInfo.IMPORTANCE_FOREGROUND;
            }
        } catch (Throwable ignored) { }
        return false;
    }

    /** 装之前先挂一条通知（0.10.77）。
        覆盖安装会把自己 force-stop，有的 ROM 顺手把闹钟也清了、界面又拦住不让弹——
        那种机器上我们连一句兜底通知都跑不到，用户看到的就是「装完什么也没发生」。
        通知栏里先放一个「点一下回到 Codeboss」，不依赖任何后台能力；回到前台自动撤掉。 */
    static void armNote(Context c) {
        try {
            NotificationManager nm =
                    (NotificationManager) c.getSystemService(Context.NOTIFICATION_SERVICE);
            if (nm == null) return;
            if (Build.VERSION.SDK_INT >= 26) {
                NotificationChannel ch = new NotificationChannel(
                        CH, "更新完成", NotificationManager.IMPORTANCE_DEFAULT);
                ch.setShowBadge(false);
                nm.createNotificationChannel(ch);
            }
            Intent it = new Intent(c, MainActivity.class);
            it.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_REORDER_TO_FRONT);
            int fl = PendingIntent.FLAG_UPDATE_CURRENT;
            if (Build.VERSION.SDK_INT >= 23) fl |= PendingIntent.FLAG_IMMUTABLE;
            PendingIntent pi = PendingIntent.getActivity(c, NID_NOTE, it, fl);
            Notification.Builder nb = (Build.VERSION.SDK_INT >= 26)
                    ? new Notification.Builder(c, CH) : new Notification.Builder(c);
            nb.setSmallIcon(R.drawable.ic_stat_codeboss)
              .setContentTitle("Codeboss 正在更新")
              .setContentText("装好后自动回到这里；没回来就点这一下")
              .setAutoCancel(true)
              .setOngoing(false)
              .setContentIntent(pi);
            nm.notify(NID_NOTE, nb.build());
            Log.i(TAG, "更新前已挂「点一下回来」通知");
        } catch (Throwable t) {
            Log.w(TAG, "更新前通知失败: " + t);
        }
    }

    /** 回到前台/安装失败：撤掉装之前挂的那条通知。 */
    static void clearNote(Context c) {
        try {
            NotificationManager nm =
                    (NotificationManager) c.getSystemService(Context.NOTIFICATION_SERVICE);
            if (nm != null) nm.cancel(NID_NOTE);
        } catch (Throwable ignored) { }
    }

    private static void notifyUser(Context c) {
        try {
            NotificationManager nm =
                    (NotificationManager) c.getSystemService(Context.NOTIFICATION_SERVICE);
            if (nm == null) return;
            if (Build.VERSION.SDK_INT >= 26) {
                NotificationChannel ch = new NotificationChannel(
                        CH, "更新完成", NotificationManager.IMPORTANCE_DEFAULT);
                ch.setShowBadge(false);
                nm.createNotificationChannel(ch);
            }
            Intent it = new Intent(c, MainActivity.class);
            it.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_REORDER_TO_FRONT);
            int fl = PendingIntent.FLAG_UPDATE_CURRENT;
            if (Build.VERSION.SDK_INT >= 23) fl |= PendingIntent.FLAG_IMMUTABLE;
            PendingIntent pi = PendingIntent.getActivity(c, NID, it, fl);
            Notification.Builder nb = (Build.VERSION.SDK_INT >= 26)
                    ? new Notification.Builder(c, CH) : new Notification.Builder(c);
            nb.setSmallIcon(R.drawable.ic_stat_codeboss)
              .setContentTitle("Codeboss 已更新")
              .setContentText("点这里继续使用")
              .setAutoCancel(true)
              .setContentIntent(pi);
            nm.notify(NID, nb.build());
            trace(c, "主界面没起来，发兜底通知");
            Log.i(TAG, "更新后前台起不来，已发通知兜底");
        } catch (Throwable t) {
            Log.w(TAG, "更新通知失败: " + t);
        }
    }
}
