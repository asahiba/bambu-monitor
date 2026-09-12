# 打包与分发

四种形态，各自独立打包。本文记录**每个产物怎么来、怎么验证、有什么已知限制**。

## 0. 总览

| 形态 | 产物 | 构建命令 | 目标机需要什么 |
| --- | --- | --- | --- |
| **Windows** | `dist-onefile/BambuMonitor.exe`（单文件，约 93 MB） | `build-onefile.bat` | 什么都不用装 |
| **Linux** | `dist-onefile-headless/BambuMonitor-headless`（单文件，约 89 MB） | `bash linux/build-headless-docker.sh` | 什么都不用装 |
| **Docker** | `dist-docker/bambu-monitor-latest-image.tar.gz`（单文件镜像，约 132 MB） | `build-docker-image.ps1` | 只需 Docker |
| **安卓** | `dist-android/BambuMonitor-1.0.0-arm64.apk`（约 33 MB） | `android/build-apk.ps1` | Android 7.0+（arm64） |

Linux 另有一个**带 Qt 的变体**（`linux/build-onefile-docker.sh`，约 143 MB），
能开图形界面；但服务器/NAS 场景请用上面的 headless 变体，原因见第 2 节。

### 已验证的产物（本机实测）

| 产物 | 验证方式 | 结果 |
| --- | --- | --- |
| Windows exe | `--core-test`（模拟器全链路） | 全部通过，退出码 0 |
| Windows exe | `--sim --screenshot` | 1280×800 截图，4 路画面 + 中文 + 状态条 + HMS 徽标正常 |
| Linux headless | 干净 `python:3.11-slim` 容器内起服务 | `/health` = 200；2 台模拟打印机**画面与遥测全部在线**；取到真实 JPEG 帧 30 KB；日志无错误 |
| Docker 镜像 | 容器内 `/health` + `/api/printers` | 通过（镜像已导出为 tar.gz） |
| 安卓 APK | `assembleDebug` 构建 + 包内容核查 | **已产出**；包内含 `libpython3.10.so`、`cv2.so`、numpy 全套原生扩展、`app/web/*`、内置 CA 与 HMS 文案表 |
| 安卓 APK | 本机运行 | **未能实测**：构建机无 Android 设备、CPU 无核显跑不了模拟器；仅做了静态核查 |

> 安卓 APK 的「未能在设备上实测」是当前唯一的验证缺口。替代验证是：
> 同样的 `app/` 代码在 **Python 3.10** 下跑完整回归测试（与 APK 内嵌的
> Python 版本一致），用于兜住"代码本身在 3.10 上不成立"这类问题。

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

### 一次产出两个 exe

| 产物 | 引导器 | 用途 |
| --- | --- | --- |
| `BambuMonitor.exe` | windowed（`runw.exe`） | 双击启动图形界面，无控制台窗口 |
| `BambuMonitor-cli.exe` | console（`run.exe`） | 命令行：`--version` / `--core-test` / `--headless` |

**为什么必须两个**：windowed 引导器**根本没有控制台句柄**，
`sys.stdout/stderr` 为 `None`，所以 ``BambuMonitor.exe --version``
不只是"看不到输出"，而是**重定向和管道拿到的也是空的**
（`> log.txt`、`| more`、CI 里捕获输出全是空文件），程序内部会把 print
丢进 `os.devnull`，静默无提示。

这一点坑过很久：CI 的冒烟验证跑 ``BambuMonitor.exe --version`` 拿到空字符串、
进程却正常结束，一度被误判成"PowerShell 取不到退出码"。
真相是引导器决定的。**所以命令行一律用 `BambuMonitor-cli.exe`。**

**已验证**（在本机实测）：

```
BambuMonitor-cli.exe --version    → Bambu Monitor 1.0.0（管道/重定向均可捕获）
BambuMonitor-cli.exe --core-test  → 自检结果：全部通过 ✓（约 14 秒）
BambuMonitor.exe --sim --screenshot shot.png --exit-after 14
                                  → 1280×800 截图，4 路画面 + 中文 + 状态条 + HMS 徽标全部正常
```

### 无界面/自动化场景

* 日志：`%APPDATA%\BambuMonitor\logs\app.log`（无界面模式的输出都写这里）；
* 要命令行输出：用 `BambuMonitor-cli.exe`（打包版）或
  `.venv\Scripts\python.exe -m app ...`（源码运行）。

GUI 版刻意保留 `console=False`：改成 `console=True` 用户双击时会多一个黑色
控制台窗口 —— 这是打包形态的固有取舍，本项目选择"像正常桌面软件"，
再单独给一个 CLI 版把命令行能力补回来。

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

**坑一：目标机缺图形库会导致画面生成失败 —— 必须用 headless 变体。**
带 Qt 的产物在**没有图形系统库**的服务器上跑时，模拟器绘制画面要经过 Qt，
而 Qt 依赖 `libxcb.so.1` 等库；缺了会报
`ImportError: libxcb.so.1: cannot open shared object file`。

更隐蔽的一层：**构建镜像里装的 opencv 版本也决定产物能不能在没有图形库的机器上跑**。
完整版 `opencv-python` 链接 `libGL`，缺了会报 `ImportError: libGL.so.1`；
`opencv-python-headless` 才没有这个依赖。项目自己的 `requirements-server.txt`
本来就是选的 headless 版，构建镜像必须与之一致 ——
`linux/Dockerfile.headless-build` 就是为此单独做的（用 `requirements-server.txt`）。

因此 Linux 服务器场景请用 headless 产物：

```bash
bash linux/build-headless-docker.sh      # 产出 dist-onefile-headless/BambuMonitor-headless
```

它不含 Qt、用 opencv-headless，体积 89 MB（带 Qt 的变体是 143 MB），
也不再依赖任何图形库。

**坑二：构建容器缺 `libgssapi-krb5-2` 时 PyInstaller 的 Qt 钩子直接失败。**
报错长这样：`ImportError: libgssapi_krb5.so.2: cannot open shared object file`。
原因是 PyInstaller 的 Qt 钩子会 `import QtNetwork` 去探测 OpenSSL 支持，
而 QtNetwork 依赖 krb5。`linux/Dockerfile.build` 已经把它装上了。

**坑三：headless 产物不要再加 `--headless`。**
`--headless` 是 `app.main`（桌面入口）用来切到无界面模式的开关；
而 `BambuMonitor-headless` 这个产物的入口本来就是 `app.headless`，
多传这个参数会被 argparse 拒绝并直接退出（`unrecognized arguments: --headless`）。
正确用法：

```bash
./BambuMonitor-headless --port 8080
./BambuMonitor-headless --sim 4 --status-interval 0     # 演示模式
```

**坑四：Docker 里跑时配置目录必须可写。**
`BAMBU_MONITOR_CONFIG_DIR` 指向的目录若不存在或只读，`AppConfig.load()`
会在 `os.makedirs` 上抛 `PermissionError`，容器直接起不来。
用挂载卷或可写路径：

```bash
docker run ... -v bambu-cfg:/data -e BAMBU_MONITOR_CONFIG_DIR=/data ...
```

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

**这个 APK 内嵌了完整的 Python 运行时**，在设备本机跑和桌面版同一个
`app/` 服务，界面用 WebView 打开 `http://127.0.0.1:8080`。所以平板可以
**完全脱离电脑独立使用**；又因为服务绑在 `0.0.0.0`，同一 Wi-Fi 下的其它
设备也能连这台平板的页面。

前置条件：JDK 17+、Android SDK、以及 **Python 3.10**
（`android/build-apk.ps1` 会先做前置检查并明确告诉你缺什么、怎么装）。
用 Android Studio 打开 `android/` 目录也可以。

> 只是想在手机上看监控墙的话，**其实不需要 APK**：
> 浏览器打开服务端地址 → 「添加到主屏幕」，这就是原有的 PWA 方案（已实测可用）。

### 为什么安卓版锁定 Python 3.10（以及 numpy 为什么要离线补）

这两项**不是随便选的**，而是被 Chaquopy 预编译 wheel 的覆盖范围卡死的。
Chaquopy 把 pip 指向自己的仓库 `https://chaquo.com/pypi-13.1`，并且只接受
**平台标签精确等于 `android_<minSdk>_<abi>`** 的 wheel（见其
`build-packages.zip` 里 `PipInstall.platform_tag`）。本项目
minSdk=24、abi=arm64-v8a，于是标签固定为 `android_24_arm64_v8a`：

| 包 | android_24_arm64_v8a 上存在的 Python 版本 |
|---|---|
| `opencv-python-headless` | **只有 cp310**（4.5.1.48） |
| `numpy` | **只有 cp313**（1.26.2） |
| `cryptography` / `cffi` | cp310 / cp311 / cp312 / cp313 |

也就是说 opencv 与 numpy **没有任何一个 Python 版本能同时满足**。而
RTSPS 画面要 opencv，`cv2` 又硬依赖 numpy（缺了会抛
`OpenCV bindings requires "numpy" package`）。解法是：

1. 选 **3.10**（也正是 Chaquopy 17 的默认版本），走 opencv 那条路；
2. numpy 用 `android_21_arm64_v8a` 的那份 wheel **离线补上** ——
   `build-apk.ps1` 会把它下到 `android/offline-wheels/`，`build.gradle`
   用**文件路径**直接安装，绕开索引的平台标签匹配。
   （该 wheel 是给 API 21 编的，但 NDK 向后兼容，装在 API 24 上可用。）

桌面版仍然用 3.13，两者互不影响：`app/` 下的代码同时兼容 3.10 与 3.13。

> ⚠️ 顺带一个只有 3.10 才暴露的坑：**不要给 `threading.Thread` 子类挂
> `self._stop` 属性**。3.10/3.11 的 `Thread.join()` 在收尾时会调用内部的
> `_stop()` 方法，被 `Event` 覆盖后 join 会抛
> `TypeError: 'Event' object is not callable`。3.13 改了这段实现，所以在
> 桌面版上完全看不出来 —— 本仓库统一用 `self._stop_event`。

### 与桌面版的依赖差异

| | 桌面版 | 安卓版 |
|---|---|---|
| Python | 3.13（`.venv`） | 3.10（`.venv310`，构建用） |
| OpenCV | `opencv-python` 4.8+ | `opencv-python-headless` 4.5.1.48（Chaquopy 预编译） |
| GUI | PySide6 | 无（界面是 WebView） |

> 另外，`android/app/src/main/python/` 是 `sync-python.ps1` 每次构建时从
> 仓库根 `app/` 复制过去的**镜像**，不要直接改那里的文件（改动会被覆盖），
> 它也已加入 `.gitignore`。

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

### 含 here-string 的脚本必须用 CRLF 换行

另一个同样隐蔽的坑：**PowerShell 5.1 不把「只有 LF 的换行」当作 here-string
（`@"` … `"@`）的结束符**。整个脚本若被工具重写成 LF 结尾，会因为找不到
`"@` 而报：

```
The string is missing the terminator: "@.
```

这个报错同样指不到真正的原因（只会让人去查引号配对）。PowerShell 7 对 LF
是宽容的，所以只有用 5.1 跑才暴露。

处理：把文件换成 CRLF 换行（`.ps1` 在 Windows 上本来就是 CRLF 更自然）：

```powershell
$p = "make-bundle.ps1"
$t = [IO.File]::ReadAllText($p, [Text.Encoding]::UTF8)
$t = ($t -replace "`r`n", "`n") -replace "`n", "`r`n"
[IO.File]::WriteAllText($p, $t, (New-Object Text.UTF8Encoding($true)))
```

这两条都有契约测试兜底，见 `tests/test_contracts.py` 里的
`test_含中文的PowerShell脚本必须带UTF8_BOM` 与
`test_含here_string的PowerShell脚本必须用CRLF换行`。

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
