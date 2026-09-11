package com.bambumonitor.viewer;

import android.annotation.SuppressLint;
import android.os.Bundle;
import android.view.KeyEvent;
import android.view.View;
import android.view.ViewGroup;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.FrameLayout;
import android.widget.Toast;

import androidx.appcompat.app.AppCompatActivity;

/**
 * 监控墙的安卓壳：一个全屏 WebView 指向已运行的网页监控服务。
 *
 * <p>设计要点（都是为了"像原生应用"而不是"像浏览器"）：
 * <ul>
 *   <li>全屏 + 沉浸式，隐藏状态栏与导航栏，观感与监控软件一致；</li>
 *   <li>开启 DOM storage 与混合内容，否则前端的多路复用与 cookie 令牌会失效；</li>
 *   <li>返回键：网页内能后退就后退，否则退出（避免误触直接退出应用）；</li>
 *   <li>出错时给出**可操作**的提示，而不是默认的"网页无法打开"——
 *       这个软件最常见的失败原因是"手机没连和打印机同一个 Wi-Fi"，
 *       所以提示里直接写这一点。</li>
 * </ul>
 *
 * <p>服务地址来源：编译期常量 {@link #DEFAULT_URL}，或 Intent extra {@code url}。
 * 也可以装好后第一次运行时长按屏幕顶部输入地址（见 {@link #promptForUrl()}）。
 */
public class MainActivity extends AppCompatActivity {

    /**
     * 默认监控地址。
     *
     * <p>构建前请改成你自己那台跑服务端的电脑的局域网地址（含令牌），例如：
     * <pre>http://192.168.1.10:8080/?token=你的令牌</pre>
     * 令牌在服务端启动日志或「网页监控」对话框里能看到。
     */
    public static final String DEFAULT_URL = "http://192.168.1.10:8080/";

    private static final String PREFS = "bambu_monitor";
    private static final String KEY_URL = "url";

    private WebView webView;

    @SuppressLint("SetJavaScriptEnabled")
    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);

        FrameLayout root = new FrameLayout(this);
        root.setLayoutParams(new ViewGroup.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));

        webView = new WebView(this);
        webView.setLayoutParams(new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));
        root.addView(webView);
        setContentView(root);

        WebSettings settings = webView.getSettings();
        settings.setJavaScriptEnabled(true);
        // 前端用 fetch 流式读取多路复用数据，且把令牌存在 cookie/localStorage 里
        settings.setDomStorageEnabled(true);
        settings.setDatabaseEnabled(true);
        settings.setLoadWithOverviewMode(true);
        settings.setUseWideViewPort(true);
        settings.setMediaPlaybackRequiresUserGesture(false);
        // 局域网服务是 http，页面本身也是 http，这里允许混合内容是防御性的
        settings.setMixedContentMode(WebSettings.MIXED_CONTENT_COMPATIBILITY_MODE);
        settings.setCacheMode(WebSettings.LOAD_NO_CACHE);

        webView.setWebViewClient(new WebViewClient() {
            @Override
            public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
                // 站内跳转留在 WebView 内，外部链接交给系统浏览器
                String url = request.getUrl().toString();
                return !url.startsWith("http://") && !url.startsWith("https://");
            }

            @Override
            public void onReceivedError(WebView view, WebResourceRequest request,
                                        android.webkit.WebResourceError error) {
                if (request.isForMainFrame()) {
                    showConnectionHelp();
                }
            }
        });
        webView.setWebChromeClient(new WebChromeClient());

        // 长按屏幕 = 修改服务器地址（换机器不用重新打包；
        // WebView 默认会弹文本选择菜单，这里用长按监听覆盖掉）
        webView.setOnLongClickListener(v -> {
            promptForUrl();
            return true;
        });
        webView.setLongClickable(true);

        // 沉浸式全屏
        getWindow().getDecorView().setSystemUiVisibility(
                View.SYSTEM_UI_FLAG_IMMERSIVE_STICKY
                        | View.SYSTEM_UI_FLAG_FULLSCREEN
                        | View.SYSTEM_UI_FLAG_HIDE_NAVIGATION
                        | View.SYSTEM_UI_FLAG_LAYOUT_STABLE);

        String url = getIntent().getStringExtra("url");
        if (url == null || url.isEmpty()) {
            url = getSharedPreferences(PREFS, MODE_PRIVATE).getString(KEY_URL, DEFAULT_URL);
        }
        webView.loadUrl(url);
    }

    /** 长按屏幕：允许临时改服务器地址（不用重新打包就能换机器）。 */
    public void promptForUrl() {
        final android.widget.EditText input = new android.widget.EditText(this);
        input.setText(webView.getUrl() == null ? DEFAULT_URL : webView.getUrl());
        new androidx.appcompat.app.AlertDialog.Builder(this)
                .setTitle("监控服务地址")
                .setMessage("填跑服务端那台电脑的局域网地址，例如 http://192.168.1.10:8080/?token=xxx")
                .setView(input)
                .setPositiveButton("打开", (dialog, which) -> {
                    String value = input.getText().toString().trim();
                    if (!value.isEmpty()) {
                        getSharedPreferences(PREFS, MODE_PRIVATE).edit()
                                .putString(KEY_URL, value).apply();
                        webView.loadUrl(value);
                    }
                })
                .setNegativeButton("取消", null)
                .show();
    }

    /**
     * 连不上时给出可操作的提示。
     *
     * <p>刻意不照搬"网页无法打开"：本软件最常见的失败原因是网络位置不对，
     * 直接说清楚能省掉大量困惑。
     */
    private void showConnectionHelp() {
        new androidx.appcompat.app.AlertDialog.Builder(this)
                .setTitle("连不上监控服务")
                .setMessage("请依次确认：\n\n"
                        + "1. 手机与跑服务端的电脑/打印机在**同一个 Wi-Fi**；\n"
                        + "2. 电脑上的「网页监控」已开启（或服务端已启动）；\n"
                        + "3. 地址里的端口与令牌与服务端显示的一致；\n"
                        + "4. 电脑防火墙允许该程序访问「专用网络」。\n\n"
                        + "长按屏幕可修改服务器地址。")
                .setPositiveButton("重试", (dialog, which) -> webView.reload())
                .setNeutralButton("改地址", (dialog, which) -> promptForUrl())
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
        super.onDestroy();
    }
}
