# 打包与分发

四种形态，各自独立打包。本文记录**每个产物怎么来、怎么验证、有什么已知限制**。

## 0. 总览

| 形态 | 产物 | 构建命令 | 目标机需要什么 |
| --- | --- | --- | --- |
| **Windows** | `dist-onefile/BambuMonitor.exe`（单文件，约 93 MB） | `build-onefile.bat` | 什么都不用装 |
| **Linux** | `dist-onefile-linux/BambuMonitor-linux`（单文件，约 143 MB） | `bash linux/build-onefile-docker.sh` | 什么都不用装 |
| **Docker** | `dist-docker/bambu-monitor-latest-image.tar.gz`（单文件镜像） | `build-docker-image.ps1` | 只需 Docker |
| **安卓** | `app-debug.apk` | `android/build-apk.ps1` 或 Android Studio | Android 7.0+ |

「单文件」的含义：Windows/Linux 是**自带 Python 运行时**的单个可执行文件；
Docker 是**整个镜像**打成的一个 tar（`docker load` 即可用，目标机无需联网拉依赖）。

## 1. Windows 单文件

```bat
build-onefile.bat
:: 产物：dist-onefile\BambuMonitor.exe
```

配置见 [`BambuMonitor-onefile.spec`](../BambuMonitor-onefile.spec)：

* 打包了 `app/bambu/certs`（内置 CA）与 `app/bambu/data`（HMS 错误码文案表）；
* 显式列出 `app.core.*` 与 `app.adapters.*` 为 hiddenimports ——
  它们是**运行时按设备族动态导入**的，PyInstaller 的静态分析可能漏掉；
* 关掉 UPX（单文件 + UPX 极易被杀软误报），也排除了 QtWebEngine 等用不到的重型模块。

**已验证**（在本机实测）：

```
BambuMonitor.exe --version      → Bambu Monitor 1.0.0，退出码 0
BambuMonitor.exe --core-test    → 自检结果：全部通过 ✓，退出码 0（约 14 秒）
BambuMonitor.exe --sim --screenshot shot.png --exit-after 14
                                → 1280×800 截图，4 路画面 + 中文 + 状态条 + HMS 徽标全部正常
```

### ⚠️ 已知限制：windowed 打包后看不到命令行输出

打包成 `--windowed`（无控制台窗口）后 `sys.stdout/stderr` 为 `None`，
因此 `--help` / `--core-test` 的输出**不会显示在控制台里**（程序内部已做兜底，
不会崩，只是没输出）。应对方式：

* 看日志：`%APPDATA%\BambuMonitor\logs\app.log`（无界面模式的输出都写这里）；
* 需要命令行输出时，用源码运行（`.venv\Scripts\python.exe -m app ...`）。

改成 `console=True` 就能看到输出，但用户双击时会多一个黑色控制台窗口 ——
这是打包形态的固有取舍，本项目选择了"像正常桌面软件"。

## 2. Linux 单文件

PyInstaller **不能跨平台编译**，所以必须在 Linux 上构建。两条路径：

```bash
# A. 已经在 Linux 上（或 WSL 里）
bash linux/build-onefile.sh

# B. 宿主机不是 Linux —— 用容器构建（推荐，构建环境与部署环境一致）
bash linux/build-onefile-docker.sh
```

产物：`dist-onefile-linux/BambuMonitor-linux`（自带 Python 运行时，无需目标机装任何东西）。

### ⚠️ 两个必须知道的坑

**坑一：目标机缺 `libxcb.so.1` 会导致模拟器画面生成失败。**
带 Qt 的产物在**没有图形系统库**的服务器上跑 `--core-test` 时，
模拟器绘制画面要经过 Qt，而 Qt 依赖 `libxcb.so.1` 等库；缺了会报
`ImportError: libxcb.so.1: cannot open shared object file`。
因此 Linux 服务器场景请用**不含 Qt 的 headless 产物**：

```bash
python -m PyInstaller --noconfirm --clean \
  --distpath dist-onefile-headless --workpath build-onefile-headless \
  BambuMonitor-headless.spec
```

它排除了 PySide6，体积更小，也不再依赖图形库（模拟器会自动改用 OpenCV 绘制）。

**坑二：构建容器缺 `libgssapi-krb5-2` 时 PyInstaller 的 Qt 钩子直接失败。**
报错长这样：`ImportError: libgssapi_krb5.so.2: cannot open shared object file`。
原因是 PyInstaller 的 Qt 钩子会 `import QtNetwork` 去探测 OpenSSL 支持，
而 QtNetwork 依赖 krb5。`linux/Dockerfile.build` 已经把它装上了。

### 验证方式（自包含性）

把产物放进一个**干净的、没装任何依赖的**容器里跑，能跑通才叫自包含：

```bash
docker run --rm -v "$PWD/dist-onefile-linux:/app" python:3.11-slim \
    /app/BambuMonitor-linux --version
```

## 3. Docker 单文件

```powershell
powershell -ExecutionPolicy Bypass -File build-docker-image.ps1
```

流程：构建镜像 → 起容器跑 `/health` 健康检查 → `docker save` 成 tar → gzip 压缩。
产物在 `dist-docker/`，拷到目标机后：

```bash
docker load -i bambu-monitor-latest-image.tar.gz
docker run -d --name bambu-monitor --network host -v $PWD/data:/data bambu-monitor:latest
```

> `--network host` 是自动搜索（SSDP 组播 + 2021 广播）能工作的前提；
> 桥接网络下搜不到打印机，只能手动填 IP。详见 [`DEPLOY.md`](DEPLOY.md)。

## 4. 安卓 APK

见 [`../android/README.md`](../android/README.md)。要点先说在这里：

**这个 APK 不是"把软件搬到手机上跑"，而是一个全屏网页壳。**
监控软件的主体是连局域网里的打印机（MQTT / 摄像头 / 发现），
把 Python 运行时塞进手机要多几百 MB 且手机必须一直前台，实用价值为负。
正确形态是：**电脑/服务器跑服务端，手机看画面**。

前置条件：JDK 17+ 与 Android SDK（本仓库不包含，体积太大）。
`android/build-apk.ps1` 会先做前置检查并明确告诉你缺什么、怎么装；
用 Android Studio 打开 `android/` 目录是最省事的路径。

> 只是想在手机上看监控墙的话，**其实不需要 APK**：
> 浏览器打开服务端地址 → 「添加到主屏幕」，这就是原有的 PWA 方案（已实测可用）。

## 5. 构建时的环境坑（都不是代码问题，但会浪费半天）

### PowerShell 脚本必须带 UTF-8 BOM

本仓库的 `.ps1` 脚本里有中文。**Windows PowerShell 5.1 读取无 BOM 的 UTF-8 文件时
会按 GBK 解码**，中文字符被拆坏后会连带破坏语法，报出一堆莫名其妙的
`Unexpected token`，而且**报错行号对不上真实内容**（这是最容易被误导的地方）。

症状识别：报错位置指向的行内容看着完全正常，且错误信息里的中文是乱码。

处理：给 `.ps1` 文件加 UTF-8 BOM（`EF BB BF`）。可以用编辑器设置，
或用 PowerShell 一行搞定：

```powershell
$p = "android\build-apk.ps1"
$t = [IO.File]::ReadAllText($p, [Text.Encoding]::UTF8)
[IO.File]::WriteAllText($p, $t, (New-Object Text.UTF8Encoding($true)))
```

### Git Bash / MSYS 会改写容器内的路径

在 Git Bash 里执行 `docker run -w /src` 时，`/src` 会被 MSYS 转换成
`C:/Program Files/Git/src`，docker 报 `working directory is invalid`。
构建脚本里已设 `MSYS_NO_PATHCONV=1` 与 `MSYS2_ARG_CONV_EXCL="*"`。

### `java -version` 把版本写到 stderr

配合 `$ErrorActionPreference = "Stop"` 会把正常的版本输出当成错误而中断脚本。
需要临时放宽 `$ErrorActionPreference` 再读取。

## 6. 各产物与源码的关系

**打包产物不包含测试**，也不包含 `tools/` 里的诊断脚本（它们需要连真机）。
产物包含的是 `app/` 全部运行时代码 + 证书 + HMS 文案表。

因此：改了代码要重新打包才能体现在产物里；日常开发请直接用源码运行，
只有要交付给别人时才打包。
