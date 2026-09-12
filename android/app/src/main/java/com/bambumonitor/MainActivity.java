package com.bambumonitor;

import android.annotation.SuppressLint;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.view.KeyEvent;
import android.view.View;
import android.view.ViewGroup;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Button;
import android.widget.FrameLayout;
import android.widget.ProgressBar;
import android.widget.TextView;
import android.widget.Toast;

import androidx.appcompat.app.AlertDialog;
import androidx.appcompat.app.AppCompatActivity;

import com.chaquo.python.PyObject;
import com.chaquo.python.Python;
import com.chaquo.python.android.AndroidPlatform;

import java.io.File;
import java.net.HttpURLConnection;
import java.net.URL;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/**
 * 拓竹打印机监控台 · 安卓版。
 *
 * <p>工作方式：**把 Python 内核跑在设备本机**，再用全屏 WebView 显示它提供的网页监控墙。
 *
 * <pre>
 *   [本 APK]
 *     ├─ Chaquopy 里的 CPython  →  app/ 全部代码（协议层 + 网页服务 + 模拟器）
 *     └─ WebView                →  http://127.0.0.1:8080/?token=...
 * </pre>
 *
 * <p>这样平板**不需要电脑在旁边**即可独立监控（服务在本机），
 * 同时服务绑在 0.0.0.0，同一 Wi-Fi 下的手机也能连这台平板看画面。
 *
 * <p>为什么复用 Python 而不是用 Java 重写：MQTT over TLS、SSDP 发现、
 * 6000 端口鉴权帧格式、RTSPS 拉流、HMS 错误码表都已有 649 项回归测试覆盖，
 * 重写等于把踩过的坑再踩一遍。
 */
public class MainActivity extends AppCompatActivity {

    /** 内置服务端口。与项目其它文档一致，便于对照排查。 */
    private static final int SERVER_PORT = 8080;

    /** 启动超时（毫秒）。首次启动要解压 Python 运行时与依赖，给足时间。 */
    private static final long START_TIMEOUT_MS = 60_000L;

    private FrameLayout webContainer;
    private LinearLayoutSplash splash;
    private final Handler ui = new Handler(Looper.getMainLooper());
    private final ExecutorService worker = Executors.newSingleThreadExecutor();
    private WebView webView;
    private boolean serverStarted;

    /** 启动提示层的小包装，避免到处 findViewById。 */
    private static final class LinearLayoutSplash {
        final View root;
        final TextView title;
        final TextView message;
        final ProgressBar progress;
        final Button retry;

        LinearLayoutSplash(View root, TextView title, TextView message,
                           ProgressBar progress, Button retry) {
            this.root = root;
            this.title = title;
            this.message = message;
            this.progress = progress;
            this.retry = retry;
        }
    }

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_main);

        webContainer = findViewById(R.id.webContainer);
        splash = new LinearLayoutSplash(
                findViewById(R.id.splash),
                findViewById(R.id.splashTitle),
                findViewById(R.id.splashMessage),
                findViewById(R.id.splashProgress),
                findViewById(R.id.retryButton));

        splash.retry.setOnClickListener(v -> {
            splash.retry.setVisibility(View.GONE);
            splash.progress.setVisibility(View.VISIBLE);
            splash.title.setText(R.string.starting_title);
            splash.message.setText(R.string.starting_message);
            startServerAndShow();
        });

        // 长按启动界面可看错误详情（排查用）
        splash.root.setOnLongClickListener(v -> {
            Toast.makeText(this, "正在启动内置服务…", Toast.LENGTH_SHORT).show();
            return true;
        });

        startServerAndShow();
    }

    /** 启动 Python 服务，成功后把界面换成 WebView。 */
    private void startServerAndShow() {
        if (serverStarted) {
            return;
        }
        serverStarted = true;

        worker.execute(() -> {
            try {
                if (!Python.isStarted()) {
                    Python.start(new AndroidPlatform(getApplicationContext()));
                }
                Python py = Python.getInstance();
                PyObject bootstrap = py.getModule("bootstrap");

                // 配置目录必须落在应用私有目录：安装目录是只读的
                File configDir = new File(getFilesDir(), "bambu-config");
                if (!configDir.exists() && !configDir.mkdirs()) {
                    throw new IllegalStateException("无法创建配置目录：" + configDir);
                }
                bootstrap.callAttr("configure_environment", configDir.getAbsolutePath());
                bootstrap.callAttr("serve", "0.0.0.0", SERVER_PORT);

                if (waitForHealth()) {
                    String url = bootstrap.callAttr("url_for", "127.0.0.1").toString();
                    ui.post(() -> showWebView(url));
                } else {
                    PyObject status = bootstrap.callAttr("status");
                    String error = status.getDict().get("error") == null
                            ? "" : status.getDict().get("error").toString();
                    String message = error.isEmpty()
                            ? "内置服务在 " + (START_TIMEOUT_MS / 1000) + " 秒内没有就绪。\n"
                            + "可能是端口被占用，或设备资源紧张。"
                            : error;
                    ui.post(() -> showError(message));
                }
            } catch (Throwable exc) {
                String detail = exc.getClass().getSimpleName() + ": " + exc.getMessage();
                ui.post(() -> showError(detail));
            }
        });
    }

    /** 轮询 /health，直到服务真正可接受请求。 */
    private boolean waitForHealth() {
        long deadline = System.currentTimeMillis() + START_TIMEOUT_MS;
        while (System.currentTimeMillis() < deadline) {
            if (isHealthy()) {
                return true;
            }
            try {
                Thread.sleep(500);
            } catch (InterruptedException exc) {
                Thread.currentThread().interrupt();
                return false;
            }
        }
        return false;
    }

    private boolean isHealthy() {
        HttpURLConnection connection = null;
        try {
            URL url = new URL("http://127.0.0.1:" + SERVER_PORT + "/health");
            connection = (HttpURLConnection) url.openConnection();
            connection.setConnectTimeout(1500);
            connection.setReadTimeout(1500);
            return connection.getResponseCode() == 200;
        } catch (Exception ignored) {
            return false;
        } finally {
            if (connection != null) {
                connection.disconnect();
            }
        }
    }

    @SuppressLint("SetJavaScriptEnabled")
    private void showWebView(String url) {
        if (isFinishing() || webView != null) {
            return;
        }
        WebView view = new WebView(this);
        view.setLayoutParams(new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));

        WebSettings settings = view.getSettings();
        settings.setJavaScriptEnabled(true);
        // 前端把访问令牌存在 cookie/localStorage 里，且用 fetch 流式读多路复用数据
        settings.setDomStorageEnabled(true);
        settings.setDatabaseEnabled(true);
        settings.setLoadWithOverviewMode(true);
        settings.setUseWideViewPort(true);
        settings.setMediaPlaybackRequiresUserGesture(false);
        settings.setCacheMode(WebSettings.LOAD_NO_CACHE);

        view.setWebViewClient(new WebViewClient() {
            @Override
            public boolean shouldOverrideUrlLoading(WebView v, WebResourceRequest request) {
                String target = request.getUrl().toString();
                // 只允许访问本机服务，避免误点外链跳出监控界面
                return !target.startsWith("http://127.0.0.1");
            }

            @Override
            public void onReceivedError(WebView v, WebResourceRequest request,
                                        android.webkit.WebResourceError error) {
                if (request.isForMainFrame()) {
                    showServiceLostDialog();
                }
            }
        });
        view.setWebChromeClient(new WebChromeClient());

        webContainer.addView(view);
        webView = view;
        view.loadUrl(url);
        splash.root.setVisibility(View.GONE);

        // 沉浸式全屏：平板当监控屏用时不该被状态栏占地方
        getWindow().getDecorView().setSystemUiVisibility(
                View.SYSTEM_UI_FLAG_IMMERSIVE_STICKY
                        | View.SYSTEM_UI_FLAG_FULLSCREEN
                        | View.SYSTEM_UI_FLAG_HIDE_NAVIGATION
                        | View.SYSTEM_UI_FLAG_LAYOUT_STABLE);
    }

    private void showError(String detail) {
        splash.progress.setVisibility(View.GONE);
        splash.retry.setVisibility(View.VISIBLE);
        splash.title.setText(R.string.start_failed_title);
        splash.message.setText(getString(R.string.start_failed_message) + "\n\n" + detail);
    }

    /** 服务中途失联时给出可操作提示，而不是白屏。 */
    private void showServiceLostDialog() {
        if (isFinishing()) {
            return;
        }
        new AlertDialog.Builder(this)
                .setTitle("内置服务已停止")
                .setMessage("监控服务不再响应。\n\n"
                        + "常见原因：系统为省电回收了后台服务（把本应用加入电池白名单可缓解）。")
                .setPositiveButton("重新加载", (dialog, which) -> {
                    if (webView != null) {
                        webView.reload();
                    }
                })
                .setNegativeButton("退出", (dialog, which) -> finish())
                .show();
    }

    @Override
    public boolean onKeyDown(int keyCode, KeyEvent event) {
        if (keyCode == KeyEvent.KEYCODE_BACK && webView != null && webView.canGoBack()) {
            webView.goBack();
            return true;
        }
        return super.onKeyDown(keyCode, event);
    }

    @Override
    protected void onDestroy() {
        if (webView != null) {
            webView.destroy();
            webView = null;
        }
        worker.shutdownNow();
        super.onDestroy();
    }
}
