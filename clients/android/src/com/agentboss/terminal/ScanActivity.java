// SPDX-License-Identifier: AGPL-3.0-or-later

package com.agentboss.terminal;

import android.app.Activity;
import android.content.Intent;
import android.graphics.Canvas;
import android.graphics.Color;
import android.graphics.Paint;
import android.graphics.Rect;
import android.graphics.RectF;
import android.hardware.Camera;
import android.os.Bundle;
import android.os.Handler;
import android.os.HandlerThread;
import android.util.Log;
import android.view.Gravity;
import android.view.SurfaceHolder;
import android.view.SurfaceView;
import android.view.View;
import android.view.WindowManager;
import android.widget.FrameLayout;
import android.widget.TextView;

import com.google.zxing.BinaryBitmap;
import com.google.zxing.DecodeHintType;
import com.google.zxing.MultiFormatReader;
import com.google.zxing.PlanarYUVLuminanceSource;
import com.google.zxing.Result;
import com.google.zxing.common.HybridBinarizer;

import java.util.EnumMap;
import java.util.List;
import java.util.Map;

/**
 * 扫码页：相机预览 + ZXing 原生解码。
 * 用页面里的 getUserMedia 不行——控制台走 http（非安全上下文），WebView 不给相机；
 * 所以扫码只能在这层原生做，解出的文本回传给 window.__cbQr。
 *
 * 画面方向：预览回调给的是传感器方向（横向）的 NV21，二维码图形本身与 90° 旋转无关，
 * 直接喂给 ZXing 即可，不做逐帧旋转（省掉每帧 90 万字节的拷贝）。
 */
public class ScanActivity extends Activity implements SurfaceHolder.Callback, Camera.PreviewCallback {
    private static final String TAG = "CodebossScan";
    public static final String EXTRA_TEXT = "text";

    private SurfaceView surface;
    private TextView tip;
    private Camera cam;
    private HandlerThread worker;
    private Handler wH;
    private final Handler ui = new Handler();
    private MultiFormatReader reader;
    private int pvW = 640, pvH = 480;
    private volatile boolean busy = false;
    private volatile boolean done = false;

    @Override
    protected void onCreate(Bundle b) {
        super.onCreate(b);
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);

        FrameLayout root = new FrameLayout(this);
        root.setBackgroundColor(Color.BLACK);

        surface = new SurfaceView(this);
        surface.getHolder().addCallback(this);
        /* 预览是 4:3（640x480 横屏 + 90° 转正 = 竖着 3:4）：
           按比例居中铺满宽度，别让 SurfaceView 拉伸变形 */
        FrameLayout.LayoutParams slp = new FrameLayout.LayoutParams(
                android.view.ViewGroup.LayoutParams.MATCH_PARENT,
                (int) (getResources().getDisplayMetrics().widthPixels * 4f / 3f));
        slp.gravity = Gravity.CENTER;
        root.addView(surface, slp);

        Mask mask = new Mask(this);
        root.addView(mask, new FrameLayout.LayoutParams(-1, -1));

        TextView title = new TextView(this);
        title.setText("扫描电脑上的二维码");
        title.setTextColor(0xffe6edf3);
        title.setTextSize(17);
        title.setGravity(Gravity.CENTER);
        title.setTypeface(null, android.graphics.Typeface.BOLD);
        FrameLayout.LayoutParams tl = new FrameLayout.LayoutParams(-1, -2);
        tl.topMargin = dp(54);
        root.addView(title, tl);

        TextView back = new TextView(this);
        back.setText("✕");
        back.setTextColor(0xffe6edf3);
        back.setTextSize(22);
        back.setGravity(Gravity.CENTER);
        back.setOnClickListener(new View.OnClickListener() {
            @Override public void onClick(View v) { finish(); }
        });
        FrameLayout.LayoutParams bl = new FrameLayout.LayoutParams(dp(52), dp(52));
        bl.leftMargin = dp(10);
        bl.topMargin = dp(36);
        root.addView(back, bl);

        tip = new TextView(this);
        tip.setText("对准电脑屏幕上的二维码");
        tip.setTextColor(0xffc9d1d9);
        tip.setTextSize(13);
        tip.setGravity(Gravity.CENTER);
        FrameLayout.LayoutParams pl = new FrameLayout.LayoutParams(-1, -2);
        pl.gravity = Gravity.BOTTOM;
        pl.bottomMargin = dp(96);
        root.addView(tip, pl);

        setContentView(root);
        reader = new MultiFormatReader();
        Map<DecodeHintType, Object> hints = new EnumMap<DecodeHintType, Object>(DecodeHintType.class);
        java.util.Vector<com.google.zxing.BarcodeFormat> fmts =
                new java.util.Vector<com.google.zxing.BarcodeFormat>();
        fmts.add(com.google.zxing.BarcodeFormat.QR_CODE);
        hints.put(DecodeHintType.POSSIBLE_FORMATS, fmts);
        hints.put(DecodeHintType.TRY_HARDER, Boolean.TRUE);
        reader.setHints(hints);

        worker = new HandlerThread("cb-scan");
        worker.start();
        wH = new Handler(worker.getLooper());

        /* 深浅色系统状态下都别糊：沉浸即可 */
        View d = getWindow().getDecorView();
        d.setSystemUiVisibility(View.SYSTEM_UI_FLAG_IMMERSIVE_STICKY
                | View.SYSTEM_UI_FLAG_FULLSCREEN
                | View.SYSTEM_UI_FLAG_HIDE_NAVIGATION
                | View.SYSTEM_UI_FLAG_LAYOUT_STABLE
                | View.SYSTEM_UI_FLAG_LAYOUT_HIDE_NAVIGATION
                | View.SYSTEM_UI_FLAG_LAYOUT_FULLSCREEN);
    }

    private int dp(int v) {
        return (int) (v * getResources().getDisplayMetrics().density + .5f);
    }

    @Override
    public void surfaceCreated(SurfaceHolder h) {
        openCamera(h);
    }

    @Override
    public void surfaceChanged(SurfaceHolder h, int fmt, int w, int ht) {
        if (cam == null) { openCamera(h); return; }   /* 面重建后相机跟着重建 */
        if (cam != null) {
            try { cam.stopPreview(); } catch (Throwable ignored) {}
            try { cam.startPreview(); } catch (Throwable ignored) {}
        }
    }

    @Override
    public void surfaceDestroyed(SurfaceHolder h) {
        closeCamera();
    }

    @SuppressWarnings("deprecation")
    private void openCamera(SurfaceHolder holder) {
        if (cam != null) return;
        /* 必须在 surfaceCreated 之后开：先开相机再拿到 Surface 的话，
           setPreviewDisplay 传的是空面（日志 "app passed NULL surface"）→ 预览全黑。
           onResume 早于 surfaceCreated，所以只在 surface 回调里开。 */
        if (holder == null || holder.getSurface() == null || !holder.getSurface().isValid()) {
            Log.i(TAG, "surface 未就绪，等 surfaceCreated 再开相机");
            return;
        }
        try {
            cam = Camera.open();
        } catch (Throwable t) {
            Log.w(TAG, "Camera.open 失败: " + t);
            fail("相机打不开（可能被其他应用占用）");
            return;
        }
        try {
            Camera.Parameters p = cam.getParameters();
            Camera.Size best = null;
            List<Camera.Size> sizes = p.getSupportedPreviewSizes();
            for (Camera.Size s : sizes) {
                if (s.width < 640 || s.height < 480) continue;
                if (best == null) { best = s; continue; }
                /* 取面积最小、够分辨率的：解码够用，帧也快 */
                if ((long) s.width * s.height < (long) best.width * best.height) best = s;
            }
            if (best == null && sizes.size() > 0) best = sizes.get(0);
            if (best != null) { p.setPreviewSize(best.width, best.height); pvW = best.width; pvH = best.height; }
            try {
                List<String> modes = p.getSupportedFocusModes();
                if (modes != null && modes.contains(Camera.Parameters.FOCUS_MODE_CONTINUOUS_PICTURE))
                    p.setFocusMode(Camera.Parameters.FOCUS_MODE_CONTINUOUS_PICTURE);
            } catch (Throwable ignored) {}
            cam.setParameters(p);
            cam.setDisplayOrientation(90);   /* 竖屏：传感器横向 → 视觉转正 */
            cam.setPreviewDisplay(holder);
            int sz = pvW * pvH * 3 / 2;
            cam.addCallbackBuffer(new byte[sz]);
            cam.addCallbackBuffer(new byte[sz]);
            cam.setPreviewCallbackWithBuffer(this);
            cam.startPreview();
            Log.i(TAG, "相机已开 " + pvW + "x" + pvH);
        } catch (Throwable t) {
            Log.w(TAG, "相机初始化失败: " + t);
            fail("相机初始化失败");
        }
    }

    @SuppressWarnings("deprecation")
    private void closeCamera() {
        if (cam == null) return;
        try { cam.setPreviewCallbackWithBuffer(null); } catch (Throwable ignored) {}
        try { cam.stopPreview(); } catch (Throwable ignored) {}
        try { cam.release(); } catch (Throwable ignored) {}
        cam = null;
    }

    @Override
    public void onPreviewFrame(final byte[] data, Camera c) {
        if (cam == null) return;
        if (done) { try { cam.addCallbackBuffer(data); } catch (Throwable ignored) {} return; }
        if (busy) { try { cam.addCallbackBuffer(data); } catch (Throwable ignored) {} return; }
        busy = true;
        final int w = pvW, h = pvH;
        wH.post(new Runnable() {
            @Override public void run() {
                String text = null;
                try {
                    text = decode(data, w, h);
                } catch (Throwable ignored) {
                } finally {
                    busy = false;
                    /* 缓冲区还回去，下一帧继续 */
                    ui.post(new Runnable() {
                        @Override public void run() {
                            if (cam != null) {
                                try { cam.addCallbackBuffer(data); } catch (Throwable ignored) {}
                            }
                        }
                    });
                }
                if (text != null && text.length() > 0) {
                    final String t = text;
                    ui.post(new Runnable() { @Override public void run() { ok(t); } });
                }
            }
        });
    }

    private String decode(byte[] data, int w, int h) {
        PlanarYUVLuminanceSource src =
                new PlanarYUVLuminanceSource(data, w, h, 0, 0, w, h, false);
        BinaryBitmap bmp = new BinaryBitmap(new HybridBinarizer(src));
        try {
            Result r = reader.decodeWithState(bmp);
            return r == null ? null : r.getText();
        } catch (Throwable t) {
            return null;   /* NotFound / Checksum / Format 都是常态，不记日志免得刷屏 */
        } finally {
            reader.reset();
        }
    }

    private void ok(String text) {
        if (done) return;
        done = true;
        Log.i(TAG, "扫码命中，长度=" + text.length());
        Intent r = new Intent();
        r.putExtra(EXTRA_TEXT, text);
        setResult(RESULT_OK, r);
        finish();
    }

    private void fail(final String msg) {
        ui.post(new Runnable() {
            @Override public void run() {
                if (tip != null) {
                    tip.setText(msg + "\n点这里返回");
                    tip.setTextColor(0xfff85149);
                    tip.setOnClickListener(new View.OnClickListener() {
                        @Override public void onClick(View v) { finish(); }
                    });
                }
            }
        });
    }

    @Override
    protected void onPause() {
        super.onPause();
        closeCamera();
    }


    @Override
    protected void onDestroy() {
        super.onDestroy();
        done = true;
        closeCamera();
        if (worker != null) {
            try { worker.quit(); } catch (Throwable ignored) {}
            worker = null;
        }
    }

    /** 半透明遮罩 + 取景方框 + 四角亮线；尺寸随屏幕自适应 */
    private class Mask extends View {
        private final Paint dim = new Paint();
        private final Paint edge = new Paint();
        private final Paint corner = new Paint();

        Mask(Activity a) {
            super(a);
            dim.setColor(0x99000000);
            edge.setColor(0x666e7681);
            edge.setStyle(Paint.Style.STROKE);
            edge.setStrokeWidth(dp(1));
            corner.setColor(0xff3fb950);
            corner.setStyle(Paint.Style.STROKE);
            corner.setStrokeWidth(dp(3));
            corner.setStrokeCap(Paint.Cap.ROUND);
        }

        @Override
        protected void onDraw(Canvas cv) {
            int w = getWidth(), h = getHeight();
            int side = Math.min(w - dp(48), dp(260));
            float cx = w / 2f, cy = h / 2f - dp(18);
            RectF box = new RectF(cx - side / 2f, cy - side / 2f, cx + side / 2f, cy + side / 2f);
            Rect r = new Rect(0, 0, w, h);
            /* 只压暗取景框外（裁剪差集），框内保持原始亮度——否则整个画面糊成黑的，看不见对没对准 */
            cv.save();
            cv.clipRect(box, android.graphics.Region.Op.DIFFERENCE);
            cv.drawRect(r, dim);
            cv.restore();

            cv.drawRoundRect(box, dp(14), dp(14), edge);

            float L = dp(26);
            /* 四角 */
            cv.drawLine(box.left, box.top, box.left + L, box.top, corner);
            cv.drawLine(box.left, box.top, box.left, box.top + L, corner);
            cv.drawLine(box.right, box.top, box.right - L, box.top, corner);
            cv.drawLine(box.right, box.top, box.right, box.top + L, corner);
            cv.drawLine(box.left, box.bottom, box.left + L, box.bottom, corner);
            cv.drawLine(box.left, box.bottom, box.left, box.bottom - L, corner);
            cv.drawLine(box.right, box.bottom, box.right - L, box.bottom, corner);
            cv.drawLine(box.right, box.bottom, box.right, box.bottom - L, corner);
        }
    }
}
