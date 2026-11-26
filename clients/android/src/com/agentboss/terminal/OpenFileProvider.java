// SPDX-License-Identifier: AGPL-3.0-or-later

package com.agentboss.terminal;

import android.content.ContentProvider;
import android.content.ContentValues;
import android.database.Cursor;
import android.database.MatrixCursor;
import android.net.Uri;
import android.os.ParcelFileDescriptor;
import android.provider.OpenableColumns;
import android.webkit.MimeTypeMap;

import java.io.File;

/** 用系统应用打开产物（批次一）：壳把 /api/ext_file 下到 cacheDir/openwith/ 后，
    本 provider 以 content:// 收口给 ACTION_VIEW——targetSdk≥24 不允许直接递 file://。
    只服务 cacheDir/openwith/ 下这一个目录，exported=false + 临时授权。 */
public class OpenFileProvider extends ContentProvider {
    static final String AUTHORITY = "com.agentboss.terminal.openfile";
    static File dir(File cacheDir) {
        return new File(cacheDir, "openwith");
    }
    /** 拍照输出目录（B1）：相机把照片写进 cacheDir/capture/，再按 content:// 回给页面。 */
    static File capDir(File cacheDir) {
        return new File(cacheDir, "capture");
    }
    static Uri capUri(android.content.Context ctx, String name) {
        return new Uri.Builder().scheme("content").authority(AUTHORITY)
                .appendPath("capture").appendPath(name).build();
    }

    @Override public boolean onCreate() { return true; }

    /** 路径两种：<name>（老 URI，openwith 只读）/ capture/<name>（拍照输出，可写）。 */
    private File file(Uri uri) {
        java.util.List<String> seg = uri.getPathSegments();
        File root;
        String name;
        if (seg.size() == 1) { root = dir(getContext().getCacheDir()); name = seg.get(0); }
        else if (seg.size() == 2 && "capture".equals(seg.get(0))) {
            root = capDir(getContext().getCacheDir()); name = seg.get(1);
        } else return null;
        if (name == null || name.contains("/") || name.contains("\\") || name.startsWith(".")) return null;
        File f = new File(root, name);
        try {
            if (!f.getCanonicalPath().startsWith(root.getCanonicalPath() + File.separator)) return null;
        } catch (Throwable t) { return null; }
        return f;
    }

    @Override public ParcelFileDescriptor openFile(Uri uri, String mode) throws java.io.FileNotFoundException {
        File f = file(uri);
        if (f == null) throw new java.io.FileNotFoundException(uri.toString());
        if (mode != null && mode.contains("w")) {
            /* 只有拍照输出目录可写；之外的写请求一律拒（老 URI 仍只读）。 */
            java.util.List<String> seg = uri.getPathSegments();
            if (seg.size() != 2 || !"capture".equals(seg.get(0))) {
                throw new java.io.FileNotFoundException("只读：" + uri);
            }
            File parent = f.getParentFile();
            if (parent != null) parent.mkdirs();
            return ParcelFileDescriptor.open(f, ParcelFileDescriptor.MODE_WRITE_ONLY
                    | ParcelFileDescriptor.MODE_CREATE | ParcelFileDescriptor.MODE_TRUNCATE);
        }
        if (!f.isFile()) throw new java.io.FileNotFoundException(uri.toString());
        return ParcelFileDescriptor.open(f, ParcelFileDescriptor.MODE_READ_ONLY);
    }

    @Override public Cursor query(Uri uri, String[] proj, String sel, String[] args, String sort) {
        File f = file(uri);
        if (f == null) return null;
        String[] cols = {OpenableColumns.DISPLAY_NAME, OpenableColumns.SIZE};
        MatrixCursor c = new MatrixCursor(cols, 1);
        c.addRow(new Object[]{f.getName(), f.length()});
        return c;
    }

    @Override public String getType(Uri uri) {
        File f = file(uri);
        String ext = f == null ? "" : MimeTypeMap.getFileExtensionFromUrl(f.getName().toLowerCase());
        String m = ext == null || ext.isEmpty() ? null
                : MimeTypeMap.getSingleton().getMimeTypeFromExtension(ext);
        return m == null ? "application/octet-stream" : m;
    }

    @Override public Uri insert(Uri uri, ContentValues values) { return null; }
    @Override public int delete(Uri uri, String sel, String[] args) { return 0; }
    @Override public int update(Uri uri, ContentValues values, String sel, String[] args) { return 0; }
}
