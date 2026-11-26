// SPDX-License-Identifier: AGPL-3.0-or-later

package com.agentboss.terminal;

import android.app.job.JobParameters;
import android.app.job.JobService;
import android.util.Log;

/**
 * 更新装完的第三路兜底：装之前排的 JobScheduler 任务。
 * job 由系统按时叫醒进程，不走后台广播那套限制（所以比 MY_PACKAGE_REPLACED 可靠）。
 * 到点后交给 {@link Relaunch}，它负责拉起界面 / 重试 / 发通知。
 */
public class WakeJob extends JobService {
    private static final String TAG = "Codeboss";

    @Override
    public boolean onStartJob(JobParameters p) {
        Log.i(TAG, "更新唤醒 job 到点");
        Relaunch.go(this, null);
        jobFinished(p, false);
        return false;
    }

    @Override
    public boolean onStopJob(JobParameters p) {
        return false;
    }
}
