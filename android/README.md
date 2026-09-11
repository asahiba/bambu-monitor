# 安卓版（APK 壳）

## 先说清楚它是什么

这个 APK **不是**把监控软件搬到手机上跑，而是一个**全屏网页壳**：

```
[电脑 / 服务器 / NAS]  跑服务端（连打印机、解码、推流）
        ↓  局域网 HTTP
[手机]  APK 全屏显示监控墙
```

**为什么这样设计**：监控软件的主体是「连局域网里的打印机」——MQTT 遥测、摄像头取流、
自动发现。把整个 Python 运行时塞进手机要多出几百 MB，而且手机必须一直前台运行才有意义，
实用价值为负。所以手机端的正确定位是**看画面**，不是当服务端。

> 如果你只是想在手机上看监控墙，**其实不需要这个 APK**：
> 用服务端给出的地址（带 `?token=`）在浏览器打开 → 「添加到主屏幕」即可全屏运行，
> 这就是本项目原有的 PWA 方案（见 [`../docs/DEPLOY.md`](../docs/DEPLOY.md)）。
> APK 的价值在于：可安装分发、图标固定、免输地址、离线壳缓存。

## 构建前置条件

APK 需要 Android SDK + Gradle，本仓库**不包含**它们（体积太大）。二选一：

### 方式 A：用 Android Studio（推荐，最省事）

1. 安装 [Android Studio](https://developer.android.com/studio)（自带 SDK 与 Gradle）
2. `File → Open` 选择本目录（`bambu-monitor/android`）
3. 首次会提示同步 Gradle，等它下载完依赖
4. `Build → Build Bundle(s) / APK(s) → Build APK(s)`
5. 产物：`android/app/build/outputs/apk/debug/app-debug.apk`

### 方式 B：命令行

需要：JDK 17+、Android SDK（`ANDROID_HOME` 指向它）、Gradle 8.9+。

```bash
cd android
gradle wrapper            # 首次生成 gradlew（仓库里只有 wrapper 配置）
./gradlew assembleDebug   # 或 assembleRelease
```

Windows：
```bat
cd android
gradle wrapper
gradlew.bat assembleDebug
```

也可以用本目录下的 `build-apk.ps1`，它会先做前置检查并给出缺什么。

## 装到手机前要改一个地方

打开 `app/src/main/java/com/bambumonitor/viewer/MainActivity.java`，把

```java
public static final String DEFAULT_URL = "http://192.168.1.10:8080/";
```

改成跑服务端那台电脑的**局域网地址**，并带上令牌（服务端启动日志或
「网页监控」对话框里能看到），例如：

```java
public static final String DEFAULT_URL = "http://192.168.1.10:8080/?token=abc123def456";
```

> 不想改代码重新打包也行：装上后**长按屏幕**会弹出地址输入框，
> 改完会记住（存在 SharedPreferences 里）。

## 安装

手机上需要允许「安装未知来源应用」。把 APK 拷过去点开即可。

## 常见问题

**打开是白屏 / 提示连不上？**
按这个顺序排查（应用里的提示也是这几条）：
1. 手机与电脑在**同一个 Wi-Fi**（不是同一个网段就会连不上）；
2. 电脑上的服务端已启动、网页监控已开启；
3. 地址里的端口与令牌正确；
4. 电脑防火墙允许该程序访问「专用网络」。

**为什么 manifest 里开了明文流量（`usesCleartextTraffic`）？**
局域网监控服务是 `http://`，安卓 9+ 默认禁止明文流量，不开这个开关页面会白屏。
局域网内自用是可接受的折中；**要放到公网请改用 HTTPS**，并把令牌当成口令保护。

**为什么不申请存储/相机/位置权限？**
这个壳只做一件事——显示网页。位置权限常被拿来推断 Wi-Fi 列表，本应用不需要，
申请了反而会让用户警惕。

## 目录结构

```
android/
├─ settings.gradle / build.gradle / gradle.properties   工程配置
├─ gradle/wrapper/gradle-wrapper.properties             Gradle 版本
├─ build-apk.ps1                                        一键构建（含前置检查）
└─ app/
   ├─ build.gradle                                      模块配置（AGP 8.7 / SDK 34 / minSdk 24）
   ├─ proguard-rules.pro
   └─ src/main/
      ├─ AndroidManifest.xml
      ├─ java/com/bambumonitor/viewer/MainActivity.java  WebView 壳
      └─ res/
         ├─ values/{strings,themes,colors}.xml           深色主题（与桌面版同配色）
         └─ mipmap-*/ic_launcher.png                     图标（由 app/web/icons.py 生成）
```
