// SPDX-License-Identifier: AGPL-3.0-or-later

package com.agentboss.terminal;

import android.app.PendingIntent;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.pm.PackageInstaller;
import android.util.Log;

/**
 * 安装会话结果的静态接收器（清单里注册，进程死了也能被叫起来）。
 *
 * 会话提交时的回调原来是**Activity PendingIntent**（直接把界面拉起来）：主界面还活着时
 * 没问题，可覆盖安装把自己杀掉之后，系统在部分 ROM（多亲 AOSP 9 实测）上这一路就断了
 * ——用户看到的是「装完回到桌面，什么也没发生」。
 *
 * 现在改成**广播 PendingIntent** 打到这个静态接收器：
 *   · 广播是显式的（component 写死），系统会直接把本进程拉起来投递，不走后台广播的批量延迟；
 *   · PendingIntent 上的 FLAG_INCLUDE_STOPPED_PACKAGES 让「刚被更新杀掉」的包也能收到；
 *   · 收到 SUCCESS 后走 Relaunch（闹钟/PendingIntent 拉起界面那一套）。
 *
 * 主界面还活着时（INSTANCE != null），本文件不抢事：同一条广播 MainActivity 里的动态
 * 接收器也在收，由它处理确认框和看门狗，这边只记一笔现场。
 */
public class UpdateStatusReceiver extends BroadcastReceiver {
    private static final String TAG = "Codeboss";

    @Override
    public void onReceive(Context ctx, Intent intent) {
        if (intent == null) return;
        final Context app = ctx.getApplicationContext();
        int st = intent.getIntExtra(PackageInstaller.EXTRA_STATUS, -999);
        String msg = intent.getStringExtra(PackageInstaller.EXTRA_STATUS_MESSAGE);
        Relaunch.trace(app, "安装状态广播 st=" + st + (msg == null ? "" : " " + msg));
        Log.i(TAG, "静态安装状态: " + st + " " + msg);

        /* 界面还活着：交给它处理（确认框、看门狗改期、页面按钮复位）。
           以前这里直接 return、想让页面里的动态接收器接住——可会话提交时用的是
           显式广播（component 写死本类），显式广播**只**送到清单里的这个接收器，
           动态接收器根本收不到。结果确认框没人转，页面停在「正在打开安装界面…」，
           只能等 25 秒看门狗兜底。现在显式转交。 */
        if (MainActivity.INSTANCE != null) {
            try {
                MainActivity.INSTANCE.onInstallStatus(intent);
                return;
            } catch (Throwable t) {
                Log.w(TAG, "转交界面处理失败: " + t);
            }
        }

        if (st == PackageInstaller.STATUS_PENDING_USER_ACTION) {
            Intent cf = intent.getParcelableExtra(Intent.EXTRA_INTENT);
            if (cf == null) { Relaunch.trace(app, "确认框 intent 缺失"); return; }
            cf.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
            /* 走 PendingIntent 代发：从后台 startActivity 会被「后台弹出界面」拦 */
            int fl = PendingIntent.FLAG_UPDATE_CURRENT;
            if (android.os.Build.VERSION.SDK_INT >= 31) fl |= PendingIntent.FLAG_MUTABLE;
            try {
                PendingIntent.getActivity(app, 9300, cf, fl).send();
                Log.i(TAG, "静态接收器转发安装确认框");
            } catch (Throwable t) {
                Log.w(TAG, "转发确认框失败: " + t);
                try { app.startActivity(cf); } catch (Throwable t2) { Log.w(TAG, "确认框: " + t2); }
            }
            return;
        }

        if (st == PackageInstaller.STATUS_SUCCESS) {
            /* 被更新杀掉后闹钟也被清了：先把「过几秒再试」重新排上，
               这一进程万一马上又被系统收走，还有下一路能回来 */
            try { Relaunch.rearm(app); } catch (Throwable ignored) { }
            Relaunch.go(app, goAsync());
        }
    }
}
