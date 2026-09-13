package com.bambumonitor;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.content.pm.ServiceInfo;
import android.net.wifi.WifiManager;
import android.os.Build;
import android.os.IBinder;
import android.os.PowerManager;
import android.util.Log;

import androidx.core.app.NotificationCompat;
import androidx.core.app.ServiceCompat;

import com.chaquo.python.Python;
import com.chaquo.python.android.AndroidPlatform;

import java.io.File;

/**
 * 前台服务：把内置的 Python 网页服务「钉」在后台运行。
 *
 * <h2>为什么必须有它</h2>
 *
 * <p>原来服务是跑在 {@code MainActivity} 的一个后台线程里的。一切正常，直到用户
 * 把应用切到后台或息屏 —— 安卓会：
 * <ul>
 *   <li>冻结后台进程的 CPU（App Standby / Doze），Python 的 MQTT 线程不再被调度，
 *       遥测立刻表现为「断连」；</li>
 *   <li>在内存紧张时直接杀掉整个进程，网页服务随之消失（平板作为监控屏时，
 *       这正是最不能接受的）；</li>
 *   <li>息屏后关闭 Wi-Fi 射频，TCP 连接被系统切断。</li>
 * </ul>
 *
 * <p>前台服务 + 常驻通知是安卓给出的**唯一正规解法**：进程被提到前台优先级，
 * 不会被冻结或随手杀掉。再配合下面两个锁，才算真正"后台保活"：
 *
 * <ul>
 *   <li>{@link PowerManager#PARTIAL_WAKE_LOCK}：CPU 继续跑（屏幕可以关）；</li>
 *   <li>{@link WifiManager.WifiLock}（HIGH_PERF）：息屏后 Wi-Fi 射频不掉，
 *       MQTT 长连接与网页服务不会因为射频休眠而断。</li>
 * </ul>
 *
 * <p>代价是耗电增加，所以界面上会提示用户「不需要长时间监控时可以退出应用」，
 * 并提供「忽略电池优化」的入口（见 {@link #isIgnoringBatteryOptimizations}）——
 * 厂商 ROM（小米/华为/OPPO…）在后台上比原生安卓更激进，不加白名单仍可能被杀。
 *
 * <h2>ChromeOS</h2>
 *
 * <p>ChromeOS 上安卓应用跑在容器里，但**前台服务与 WakeLock 的语义一致**，
 * 而且 ChromeOS 对后台进程比手机宽松得多（插电为主）。这里不需要为它做特殊分支，
 * 但要保证：{@code foregroundServiceType} 声明正确（Android 14 起强制校验，
 * 缺失会直接崩），以及窗口可自由缩放（见 AndroidManifest 的 resizeableActivity）。
 */
public class MonitorService extends Service {

    private static final String TAG = "BambuMonitorSvc";

    /** 通知渠道：常驻通知要低打扰（无声音、无角标）。 */
    private static final String CHANNEL_ID = "bambu-monitor-service";
    private static final int NOTIFICATION_ID = 1001;

    /** 内置服务端口。与 MainActivity、项目文档保持一致。 */
    public static final int SERVER_PORT = 8080;

    /** 供界面读取：服务当前给出的访问地址（带令牌）。空串表示还没起来。 */
    private static volatile String lastUrl = "";
    /** 供界面读取：启动失败原因。空串表示没有失败。 */
    private static volatile String lastError = "";
    /** 供界面读取：服务是否在跑（已 start 且未销毁）。 */
    private static volatile boolean running = false;

    private PowerManager.WakeLock wakeLock;
    private WifiManager.WifiLock wifiLock;
    private Thread pythonThread;

    // ---------------------------------------------------------------- 供界面查询

    /** 内置服务的访问地址（带令牌）；还没就绪时返回空串。 */
    public static String getUrl() {
        return lastUrl;
    }

    /** 启动失败的原因；正常时返回空串。 */
    public static String getError() {
        return lastError;
    }

    public static boolean isRunning() {
        return running;
    }

    /** 系统是否已把本应用排除在电池优化之外。 */
    public static boolean isIgnoringBatteryOptimizations(Context context) {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.M) {
            return true; // 6.0 以下没有这项机制
        }
        PowerManager pm = (PowerManager) context.getSystemService(Context.POWER_SERVICE);
        if (pm == null) {
            return true;
        }
        return pm.isIgnoringBatteryOptimizations(context.getPackageName());
    }

    // ---------------------------------------------------------------- 生命周期

    @Override
    public void onCreate() {
        super.onCreate();
        running = true;
        createChannel();
        acquireLocks();
        // 立刻进前台：Android 要求 startForeground 必须在 startForegroundService 之后
        // 尽快调用（约 5 秒内），否则系统会抛 ANR/崩溃。放在启动 Python 之前。
        startForegroundCompat(getString(R.string.service_notification_starting));
        startPython();
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        // 被系统杀掉后自动重建：监控服务没有"手动重启"的必要，用户装它就是想让它在。
        return START_STICKY;
    }

    @Override
    public void onDestroy() {
        running = false;
        releaseLocks();
        stopPython();
        super.onDestroy();
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null; // 只 start，不 bind
    }

    // ---------------------------------------------------------------- 前台通知

    private void createChannel() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) {
            return;
        }
        NotificationManager manager = getSystemService(NotificationManager.class);
        if (manager == null) {
            return;
        }
        NotificationChannel channel = new NotificationChannel(
                CHANNEL_ID,
                getString(R.string.service_channel_name),
                NotificationManager.IMPORTANCE_LOW); // LOW：不出声、不弹窗，但常驻
        channel.setDescription(getString(R.string.service_channel_desc));
        channel.setShowBadge(false);
        manager.createNotificationChannel(channel);
    }

    private Notification buildNotification(String text) {
        Intent open = new Intent(this, MainActivity.class);
        open.setFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP | Intent.FLAG_ACTIVITY_CLEAR_TOP);
        int pendingFlags = PendingIntent.FLAG_UPDATE_CURRENT;
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) {
            pendingFlags |= PendingIntent.FLAG_IMMUTABLE;
        }
        PendingIntent contentIntent = PendingIntent.getActivity(this, 0, open, pendingFlags);

        return new NotificationCompat.Builder(this, CHANNEL_ID)
                .setSmallIcon(R.mipmap.ic_launcher)
                .setContentTitle(getString(R.string.app_name))
                .setContentText(text)
                .setContentIntent(contentIntent)
                .setOngoing(true)          // 不可滑掉：滑掉就等于停服务
                .setPriority(NotificationCompat.PRIORITY_LOW)
                .setCategory(NotificationCompat.CATEGORY_SERVICE)
                .setShowWhen(false)
                .build();
    }

    /**
     * 进入前台。
     *
     * <p>Android 14（API 34）起 `foregroundServiceType` 是强制的：声明了
     * `dataSync` 就必须在调用时**显式带上同一类型**，否则系统抛
     * `MissingForegroundServiceTypeException` 直接崩。这里按版本分支处理。
     */
    private void startForegroundCompat(String text) {
        Notification notification = buildNotification(text);
        int type = 0;
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            type = ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC;
        }
        try {
            ServiceCompat.startForeground(this, NOTIFICATION_ID, notification, type);
        } catch (Exception exc) {
            // 极少数 ROM 会在这里抛（例如通知权限被收回）。退化为普通 startForeground，
            // 至少不让服务起不来 —— 保活效果打折，但服务还在。
            Log.w(TAG, "startForeground 带类型失败，退回不带类型", exc);
            startForeground(NOTIFICATION_ID, notification);
        }
    }

    private void updateNotification(String text) {
        NotificationManager manager =
                (NotificationManager) getSystemService(Context.NOTIFICATION_SERVICE);
        if (manager != null) {
            manager.notify(NOTIFICATION_ID, buildNotification(text));
        }
    }

    // ---------------------------------------------------------------- 锁

    /**
     * 获取 CPU 与 Wi-Fi 锁。
     *
     * <p>两个都带超时并在到期后自动续：长期持有的无超时锁一旦泄漏（进程被杀），
     * 会一直耗电；带超时 + 续约既保证有效，又不会留下永久耗电的坑。
     */
    private void acquireLocks() {
        try {
            PowerManager pm = (PowerManager) getSystemService(Context.POWER_SERVICE);
            if (pm != null) {
                wakeLock = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "bambu-monitor:service");
                wakeLock.setReferenceCounted(false);
                wakeLock.acquire(10 * 60 * 1000L); // 10 分钟，之后由看护线程续
            }
        } catch (Exception exc) {
            Log.w(TAG, "获取 WakeLock 失败（不影响服务运行）", exc);
        }
        try {
            WifiManager wifi = (WifiManager) getApplicationContext()
                    .getSystemService(Context.WIFI_SERVICE);
            if (wifi != null) {
                int mode = WifiManager.WIFI_MODE_FULL_HIGH_PERF;
                wifiLock = wifi.createWifiLock(mode, "bambu-monitor:wifi");
                wifiLock.setReferenceCounted(false);
                wifiLock.acquire();
            }
        } catch (Exception exc) {
            // 没有 Wi-Fi 硬件（例如 ChromeOS 上的某些容器/纯网线设备）会失败，无妨
            Log.i(TAG, "获取 WifiLock 失败（可能没有 Wi-Fi 硬件）", exc);
        }
    }

    private void releaseLocks() {
        if (wakeLock != null && wakeLock.isHeld()) {
            try {
                wakeLock.release();
            } catch (Exception ignored) {
                // 已释放/已超时
            }
        }
        wakeLock = null;
        if (wifiLock != null && wifiLock.isHeld()) {
            try {
                wifiLock.release();
            } catch (Exception ignored) {
                // 同上
            }
        }
        wifiLock = null;
    }

    // ---------------------------------------------------------------- Python

    private void startPython() {
        lastError = "";
        lastUrl = "";
        pythonThread = new Thread(() -> {
            try {
                if (!Python.isStarted()) {
                    Python.start(new AndroidPlatform(getApplicationContext()));
                }
                com.chaquo.python.PyObject bootstrap =
                        Python.getInstance().getModule("bootstrap");

                File configDir = new File(getFilesDir(), "bambu-config");
                if (!configDir.exists() && !configDir.mkdirs()) {
                    throw new IllegalStateException("无法创建配置目录：" + configDir);
                }
                bootstrap.callAttr("configure_environment", configDir.getAbsolutePath());
                // 绑 0.0.0.0：同一 Wi-Fi 下的其它设备也能访问这台平板
                bootstrap.callAttr("serve", "0.0.0.0", SERVER_PORT);

                if (!waitForHealth()) {
                    com.chaquo.python.PyObject status = bootstrap.callAttr("status");
                    com.chaquo.python.PyObject errorObj = status.callAttr("get", "error");
                    String detail = (errorObj == null) ? "" : errorObj.toString();
                    lastError = detail.isEmpty()
                            ? "内置服务在 " + (MainActivity.START_TIMEOUT_MS / 1000)
                              + " 秒内没有就绪（可能是端口被占用）"
                            : detail;
                    updateNotification(getString(R.string.service_notification_failed));
                    Log.e(TAG, "内置服务未就绪：" + lastError);
                    return;
                }

                // 用回环地址给本机 WebView；局域网地址由网页里的「在其它设备上打开」提供
                lastUrl = bootstrap.callAttr("url_for", "127.0.0.1").toString();
                updateNotification(getString(R.string.service_notification_running,
                        String.valueOf(SERVER_PORT)));
                Log.i(TAG, "内置服务已就绪：" + lastUrl);

                // 一直在后台续锁：WakeLock 带 10 分钟超时，这里每 5 分钟续一次，
                // 保证长时间监控时 CPU 不被冻结。
                keepAliveLoop();
            } catch (Throwable exc) {
                lastError = exc.getClass().getSimpleName() + ": " + exc.getMessage();
                updateNotification(getString(R.string.service_notification_failed));
                Log.e(TAG, "内置服务启动失败", exc);
            }
        }, "bambu-python-server");
        pythonThread.setDaemon(true);
        pythonThread.start();
    }

    private void keepAliveLoop() {
        while (running) {
            try {
                Thread.sleep(5 * 60 * 1000L);
            } catch (InterruptedException exc) {
                Thread.currentThread().interrupt();
                return;
            }
            if (!running) {
                return;
            }
            try {
                if (wakeLock != null && !wakeLock.isHeld()) {
                    wakeLock.acquire(10 * 60 * 1000L);
                }
            } catch (Exception exc) {
                Log.w(TAG, "续 WakeLock 失败", exc);
            }
        }
    }

    /** 轮询 `/health`，直到服务真正可接受请求。 */
    private boolean waitForHealth() {
        long deadline = System.currentTimeMillis() + MainActivity.START_TIMEOUT_MS;
        while (System.currentTimeMillis() < deadline && running) {
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
        java.net.HttpURLConnection connection = null;
        try {
            java.net.URL url = new java.net.URL("http://127.0.0.1:" + SERVER_PORT + "/health");
            connection = (java.net.HttpURLConnection) url.openConnection();
            connection.setConnectTimeout(2000);
            connection.setReadTimeout(2000);
            return connection.getResponseCode() == 200;
        } catch (Exception exc) {
            return false;
        } finally {
            if (connection != null) {
                connection.disconnect();
            }
        }
    }

    private void stopPython() {
        try {
            com.chaquo.python.PyObject bootstrap =
                    Python.getInstance().getModule("bootstrap");
            bootstrap.callAttr("stop");
        } catch (Throwable exc) {
            // 进程退出会一并回收，这里失败无所谓
            Log.i(TAG, "停止内置服务时出错（可忽略）", exc);
        }
    }
}
