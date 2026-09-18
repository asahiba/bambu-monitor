package com.bambumonitor;

import android.annotation.SuppressLint;
import android.content.Intent;
import android.content.SharedPreferences;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.view.KeyEvent;
import android.view.View;
import android.view.ViewGroup;
import android.webkit.ValueCallback;
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
    public static final int SERVER_PORT = 8080;

    /** 启动超时（毫秒）。首次启动要解压 Python 运行时与依赖，给足时间。 */
    public static final long START_TIMEOUT_MS = 60_000L;

    /** 网页里 {@code <input type="file">} 的请求码（导入配置文件用）。 */
    private static final int FILE_CHOOSER_REQUEST = 1001;

    /** 正在等待结果的文件选择回调；没有请求时为 null。 */
    private ValueCallback<Uri[]> fileCallback;

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

    /**
     * 启动内置服务并把界面换成 WebView。
     *
     * <p><b>服务本体在前台服务里</b>（{@link MonitorService}），不在这里。
     * 这是后台保活的关键：跑在 Activity 后台线程里时，切后台/息屏会被系统冻结
     * CPU 甚至杀掉进程，遥测表现为「时不时断连」，平板当监控屏时最不能接受。
     * 这里只负责「拉起服务 + 等它就绪 + 显示」。
     */
    private void startServerAndShow() {
        if (serverStarted) {
            return;
        }
        serverStarted = true;

        // 先把服务拉起来（幂等：已经在跑就只是再来一次 onStartCommand）
        startMonitorService();
        requestNotificationPermissionIfNeeded();
        askBatteryWhitelistOnce();

        // 服务在自己的线程里启 Python（首次要解压运行时，可能要十几秒），
        // 这里只轮询它公开的状态。
        worker.execute(() -> {
            long deadline = System.currentTimeMillis() + START_TIMEOUT_MS;
            while (System.currentTimeMillis() < deadline) {
                String url = MonitorService.getUrl();
                if (!url.isEmpty()) {
                    ui.post(() -> showWebView(url));
                    return;
                }
                String error = MonitorService.getError();
                if (!error.isEmpty()) {
                    ui.post(() -> showError(error));
                    return;
                }
                try {
                    Thread.sleep(400);
                } catch (InterruptedException exc) {
                    Thread.currentThread().interrupt();
                    return;
                }
            }
            ui.post(() -> showError(
                    "内置服务在 " + (START_TIMEOUT_MS / 1000) + " 秒内没有就绪。\n"
                    + "可能是端口被占用，或设备资源紧张。"));
        });
    }

    private void startMonitorService() {
        try {
            Intent intent = new Intent(this, MonitorService.class);
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                startForegroundService(intent);
            } else {
                startService(intent);
            }
        } catch (Exception exc) {
            ui.post(() -> showError("无法启动后台服务：" + exc));
        }
    }

    /**
     * 请求通知权限（Android 13+）。
     *
     * <p>前台服务的常驻通知在 13+ 需要 POST_NOTIFICATIONS，否则通知不显示。
     * 服务本身仍能跑，但用户看不到「正在后台监控」的提示，也不知道怎么停。
     */
    private void requestNotificationPermissionIfNeeded() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.TIRAMISU) {
            return;
        }
        if (checkSelfPermission(android.Manifest.permission.POST_NOTIFICATIONS)
                == android.content.pm.PackageManager.PERMISSION_GRANTED) {
            return;
        }
        requestPermissions(new String[]{android.Manifest.permission.POST_NOTIFICATIONS}, 1);
    }

    /**
     * 引导用户把应用加入电池优化白名单 —— 只问一次。
     *
     * <p>为什么值得单独做一步：前台服务已经是安卓给出的正规保活手段，但**厂商 ROM
     * （小米 / 华为 / OPPO / vivo…）会更激进地限制后台**，不加白名单仍可能被杀，
     * 表现为「遥测时不时断连」。这里不申请
     * {@code REQUEST_IGNORE_BATTERY_OPTIMIZATIONS} 权限（那属于敏感权限，
     * 上架会被额外审查），而是跳到系统设置页让用户自己确认。
     */
    private void askBatteryWhitelistOnce() {
        if (MonitorService.isIgnoringBatteryOptimizations(this)) {
            return;
        }
        SharedPreferences prefs = getSharedPreferences("bambu-monitor", MODE_PRIVATE);
        if (prefs.getBoolean("battery_hint_shown", false)) {
            return;
        }
        prefs.edit().putBoolean("battery_hint_shown", true).apply();
        ui.post(() -> new AlertDialog.Builder(this)
                .setTitle(R.string.battery_hint_title)
                .setMessage(R.string.battery_hint_message)
                .setPositiveButton(R.string.battery_hint_go, (dialog, which) -> openBatterySettings())
                .setNegativeButton(R.string.battery_hint_later, null)
                .show());
    }

    private void openBatterySettings() {
        try {
            Intent intent = new Intent(
                    android.provider.Settings.ACTION_IGNORE_BATTERY_OPTIMIZATION_SETTINGS);
            startActivity(intent);
        } catch (Exception exc) {
            // 个别 ROM 没有这个页面，退到应用详情页
            try {
                Intent fallback = new Intent(
                        android.provider.Settings.ACTION_APPLICATION_DETAILS_SETTINGS,
                        android.net.Uri.parse("package:" + getPackageName()));
                startActivity(fallback);
            } catch (Exception ignored) {
                Toast.makeText(this, "请到系统设置 → 电池 → 应用省电策略里手动设置",
                        Toast.LENGTH_LONG).show();
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
        view.setWebChromeClient(new WebChromeClient() {
            /**
             * 让网页里的 {@code <input type="file">} 能用。
             *
             * <p>为什么必须要：网页端的「导入配置」有一个「从文件读取…」按钮
             * （选 .json 配置文件）。**WebView 默认不实现文件选择** ——
             * 不写这个回调，用户点下去毫无反应，而"配置能不能在平板上导入"
             * 正是我们要保证的事（见 app/web/page.py 的配置备份区块）。
             */
            @Override
            public boolean onShowFileChooser(WebView webView, ValueCallback<Uri[]> callback,
                                             FileChooserParams params) {
                if (fileCallback != null) {
                    fileCallback.onReceiveValue(null);  // 上一次没结束：先还回去
                }
                fileCallback = callback;
                try {
                    Intent intent = params.createIntent();
                    intent.addCategory(Intent.CATEGORY_OPENABLE);
                    startActivityForResult(intent, FILE_CHOOSER_REQUEST);
                    return true;
                } catch (Exception exc) {
                    fileCallback = null;
                    Toast.makeText(MainActivity.this,
                            "无法打开文件选择器：" + exc, Toast.LENGTH_LONG).show();
                    return false;
                }
            }
        });

        webContainer.addView(view);
        webView = view;
        view.loadUrl(url);
        splash.root.setVisibility(View.GONE);

        // 沉浸式全屏：平板当监控屏用时不该被状态栏占地方。
        //
        // ⚠️ 但**只在触摸设备上**这么做。ChromeOS / 桌面模式（DeX、接大屏的平板）
        // 里沉浸式会把窗口标题栏与系统栏一起藏掉，窗口拖动、最小化、切换应用都变得
        // 别扭 —— 桌面用户期待的是普通窗口。判断依据用触摸屏能力，
        // 而不是"屏幕大小"（ChromeOS 上大屏但没触摸屏，正好落在这条）。
        if (hasTouchscreen()) {
            getWindow().getDecorView().setSystemUiVisibility(
                    View.SYSTEM_UI_FLAG_IMMERSIVE_STICKY
                            | View.SYSTEM_UI_FLAG_FULLSCREEN
                            | View.SYSTEM_UI_FLAG_HIDE_NAVIGATION
                            | View.SYSTEM_UI_FLAG_LAYOUT_STABLE);
        }
    }

    /** 设备是否有触摸屏。ChromeOS 笔记本、桌面模式、电视盒子都没有。 */
    private boolean hasTouchscreen() {
        return getPackageManager().hasSystemFeature(
                android.content.pm.PackageManager.FEATURE_TOUCHSCREEN);
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
                        + "常见原因：系统为省电回收了后台服务。\n"
                        + "把本应用加入「电池优化白名单」可明显缓解。")
                .setPositiveButton("重新加载", (dialog, which) -> {
                    if (webView != null) {
                        webView.reload();
                    }
                })
                .setNeutralButton("去设置", (dialog, which) -> openBatterySettings())
                // 只是关掉界面；后台监控服务与常驻通知继续运行
                //（监控台本来就是要一直在的，想停就滑掉通知或从系统里停应用）
                .setNegativeButton("关闭界面", (dialog, which) -> finish())
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

    /**
     * 文件选择器返回结果 → 交回给网页（见 {@code onShowFileChooser}）。
     *
     * <p>不管结果如何都必须回调一次：`ValueCallback` 只有在被调用后才会释放，
     * 否则下次再点「从文件读取…」会被 WebView 忽略。
     */
    @Override
    protected void onActivityResult(int requestCode, int resultCode, Intent data) {
        if (requestCode == FILE_CHOOSER_REQUEST) {
            Uri[] results = null;
            if (resultCode == RESULT_OK && data != null) {
                if (data.getClipData() != null) {
                    // 多选（我们只用到单选，但按规范处理，避免拿到 null）
                    int count = data.getClipData().getItemCount();
                    results = new Uri[count];
                    for (int index = 0; index < count; index++) {
                        results[index] = data.getClipData().getItemAt(index).getUri();
                    }
                } else if (data.getData() != null) {
                    results = new Uri[]{data.getData()};
                }
            }
            if (fileCallback != null) {
                fileCallback.onReceiveValue(results);
                fileCallback = null;
            }
            return;  // 这个请求码归我们处理，不再交给父类
        }
        super.onActivityResult(requestCode, resultCode, data);
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
