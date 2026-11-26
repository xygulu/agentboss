// SPDX-License-Identifier: AGPL-3.0-or-later

package com.agentboss.terminal;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;

/**
 * 更新装完自己回来：覆盖安装会把我们的进程杀掉，系统装好后广播 MY_PACKAGE_REPLACED。
 * 拉起主界面 + 起不来就发通知的逻辑统一在 {@link Relaunch} 里
 * （后台拉 Activity 需要悬浮窗权限；没有时退化成一条可点的通知）。
 */
public class ReplacedReceiver extends BroadcastReceiver {
    @Override
    public void onReceive(Context ctx, Intent intent) {
        if (intent == null) return;
        String a = intent.getAction();
        /* 更新装完：系统广播 + 装之前排的闹钟（armWake）都走这里 */
        if (Intent.ACTION_MY_PACKAGE_REPLACED.equals(a) || Relaunch.ACTION_WAKE.equals(a)) {
            Relaunch.trace(ctx, "收到 " + (Intent.ACTION_MY_PACKAGE_REPLACED.equals(a)
                    ? "MY_PACKAGE_REPLACED" : "唤醒闹钟/Job"));
            Relaunch.go(ctx, goAsync());
        }
    }
}
