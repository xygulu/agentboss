// SPDX-License-Identifier: AGPL-3.0-or-later

package com.agentboss.terminal;

import android.app.admin.DevicePolicyManager;
import android.content.ComponentName;
import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.os.Build;
import android.os.UserManager;
import android.util.Log;

/**
 * 专用终端策略。只有本 App 被设为 Device Owner（adb dpm set-device-owner）后才生效；
 * 否则整段静默跳过，不影响普通安装试跑。全部免 root。
 */
public final class Kiosk {

    private static final String TAG = "codeboss-kiosk";

    private Kiosk() {}

    public static boolean isOwner(Context ctx) {
        DevicePolicyManager dpm = (DevicePolicyManager) ctx.getSystemService(Context.DEVICE_POLICY_SERVICE);
        return dpm != null && dpm.isDeviceOwnerApp(ctx.getPackageName());
    }

    public static void applyPolicies(Context ctx) {
        DevicePolicyManager dpm = (DevicePolicyManager) ctx.getSystemService(Context.DEVICE_POLICY_SERVICE);
        if (dpm == null || !dpm.isDeviceOwnerApp(ctx.getPackageName())) {
            Log.i(TAG, "skip: not device owner");
            return;
        }
        ComponentName admin = new ComponentName(ctx, AdminReceiver.class);

        // 1) 锁定任务：允许本包 + 系统设置进入，且关掉 Home/最近任务/通知等所有 feature。
        //    为什么要放 com.android.settings：手机没网时离线页要开系统「互联网连接」浮层/
        //    WiFi 设置页（Android 10+ 第三方 App 不能直接开关 WiFi）。只放本包时这些
        //    startActivity 会被锁任务拦掉——表现就是点「打开」没反应。
        //    systemui 一并放行：部分 ROM 的浮层面板由 SystemUI 提供。
        //    securitycenter：MIUI「后台弹出界面」等私有权限页在这个包里。
        try {
            dpm.setLockTaskPackages(admin, new String[]{
                    ctx.getPackageName(), "com.android.settings", "com.android.systemui",
                    "com.miui.securitycenter"});
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.P) {
                dpm.setLockTaskFeatures(admin, 0);
            }
        } catch (Throwable t) {
            Log.e(TAG, "lockTask failed", t);
        }

        // 2) 应用限制：不许装、不许卸、不许加账号、不许安全模式
        // ⚠️ DISALLOW_FACTORY_RESET 默认注释掉：开了之后连"恢复出厂"都失效，
        //    验证阶段先别开，等整套跑顺再打开这一行。
        // restrict(dpm, admin, UserManager.DISALLOW_FACTORY_RESET);
        restrict(dpm, admin, UserManager.DISALLOW_ADD_USER);
        restrict(dpm, admin, UserManager.DISALLOW_SAFE_BOOT);
        restrict(dpm, admin, UserManager.DISALLOW_INSTALL_APPS);
        restrict(dpm, admin, UserManager.DISALLOW_UNINSTALL_APPS);
        restrict(dpm, admin, UserManager.DISALLOW_INSTALL_UNKNOWN_SOURCES);
        restrict(dpm, admin, UserManager.DISALLOW_CONFIG_DATE_TIME);

        // 3) 干掉状态栏、锁屏、截图
        try {
            dpm.setStatusBarDisabled(admin, true);
        } catch (Throwable t) {
            Log.w(TAG, "statusBar: " + t.getMessage());
        }
        try {
            dpm.setKeyguardDisabled(admin, true);
        } catch (Throwable t) {
            Log.w(TAG, "keyguard: " + t.getMessage());
        }
        try {
            dpm.setScreenCaptureDisabled(admin, true);
        } catch (Throwable t) {
            Log.w(TAG, "capture: " + t.getMessage());
        }

        // 4) 把默认桌面永久钉死在自己身上（开机第一时间进 codeboss）
        try {
            IntentFilter filter = new IntentFilter(Intent.ACTION_MAIN);
            filter.addCategory(Intent.CATEGORY_HOME);
            filter.addCategory(Intent.CATEGORY_DEFAULT);
            dpm.addPersistentPreferredActivity(admin, filter, new ComponentName(ctx, MainActivity.class));
        } catch (Throwable t) {
            Log.w(TAG, "home: " + t.getMessage());
        }
    }

    /** 解除 applyPolicies 的一切（切回 App 模式用；非 owner 静默跳过）。 */
    public static void teardownPolicies(Context ctx) {
        DevicePolicyManager dpm = (DevicePolicyManager) ctx.getSystemService(Context.DEVICE_POLICY_SERVICE);
        if (dpm == null || !dpm.isDeviceOwnerApp(ctx.getPackageName())) {
            Log.i(TAG, "teardown skip: not device owner");
            return;
        }
        ComponentName admin = new ComponentName(ctx, AdminReceiver.class);
        try {
            dpm.setLockTaskPackages(admin, new String[]{});
        } catch (Throwable t) { Log.w(TAG, "unlockTask: " + t.getMessage()); }
        try {
            dpm.setStatusBarDisabled(admin, false);
        } catch (Throwable t) { Log.w(TAG, "statusBar: " + t.getMessage()); }
        try {
            dpm.setKeyguardDisabled(admin, false);
        } catch (Throwable t) { Log.w(TAG, "keyguard: " + t.getMessage()); }
        try {
            dpm.setScreenCaptureDisabled(admin, false);
        } catch (Throwable t) { Log.w(TAG, "capture: " + t.getMessage()); }
        try {
            dpm.clearPackagePersistentPreferredActivities(admin, ctx.getPackageName());
        } catch (Throwable t) { Log.w(TAG, "home: " + t.getMessage()); }
        unrestrict(dpm, admin, UserManager.DISALLOW_ADD_USER);
        unrestrict(dpm, admin, UserManager.DISALLOW_SAFE_BOOT);
        unrestrict(dpm, admin, UserManager.DISALLOW_INSTALL_APPS);
        unrestrict(dpm, admin, UserManager.DISALLOW_UNINSTALL_APPS);
        unrestrict(dpm, admin, UserManager.DISALLOW_INSTALL_UNKNOWN_SOURCES);
        unrestrict(dpm, admin, UserManager.DISALLOW_CONFIG_DATE_TIME);
    }

    private static void unrestrict(DevicePolicyManager dpm, ComponentName admin, String key) {
        try {
            dpm.clearUserRestriction(admin, key);
        } catch (Throwable t) { Log.w(TAG, "unrestrict " + key + ": " + t.getMessage()); }
    }

    private static void restrict(DevicePolicyManager dpm, ComponentName admin, String key) {
        try {
            dpm.addUserRestriction(admin, key);
        } catch (Throwable t) {
            Log.w(TAG, "restriction " + key + " unsupported: " + t.getMessage());
        }
    }
}
