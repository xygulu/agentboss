// SPDX-License-Identifier: AGPL-3.0-or-later

package com.agentboss.terminal;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;

/** 兜底自启：即使默认桌面没能钉死，开完机也自己拉起来。 */
public class BootReceiver extends BroadcastReceiver {
    @Override
    public void onReceive(Context ctx, Intent intent) {
        if (intent == null || !Intent.ACTION_BOOT_COMPLETED.equals(intent.getAction())) return;
        try {
            Intent go = new Intent(ctx, MainActivity.class);
            go.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
            ctx.startActivity(go);
        } catch (Throwable ignored) {
        }
    }
}
