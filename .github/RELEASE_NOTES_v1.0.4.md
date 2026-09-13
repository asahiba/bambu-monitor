# v1.0.4

**修三个安卓上的真问题：ARM Chromebook 闪退、启动报 `ModuleNotFoundError: bootstrap`、
图标显示空白。另外换成固定签名，以后能覆盖更新了。**

> ⚠️ **这一版的签名和以前不一样，需要先卸载旧版再装**（原因见下）。

## 1. ARM Chromebook 闪退 —— 16 KB 内存页问题

安卓版的依赖里有 `opencv-python-headless` / `numpy` / `cryptography`。
Chaquopy 仓库里给它们的预编译库是 **4096 字节对齐**的，而
**16 KB 内存页的设备**（新 ARM Chromebook、Pixel 8+ 等）**拒绝加载**这类库 ——
Python 启动时一导入就抛异常，表现就是**闪退**。

Chaquopy 17.0.0 的发布说明原文就写了：

> "any Android wheels built before October 2024 will still fail to load on 16 KB devices."

**处理**：安卓构建不再安装这三个包，只装纯 Python 的 `paho-mqtt`。
它们在代码里本来就都是可选的：

| 去掉的包 | 影响 |
| --- | --- |
| `opencv` / `numpy` | RTSPS(322) 高清通道不可用，自动退回 6000 端口 JPEG。**A1 / P1 / A2L 走 6000 端口，不受影响**；只提供 RTSPS 的机型（X1 / X2D / H2）在安卓上看不到画面 |
| `cryptography` | 只给内置模拟器生成自签证书用，安卓版不用模拟器 |

顺带的好处：**APK 从 33 MB 降到 19 MB**。

如果你更需要 RTSPS 画面、且设备不是 16 KB 页，可以自行把这三个包加回
`android/app/build.gradle` 的 `pip` 段 —— 但那会让 16 KB 页设备闪退。

## 2. 启动报 `ModuleNotFoundError: No module named 'bootstrap'`

安卓侧的 Python 入口模块原本叫 `bootstrap.py`，而 **Chaquopy 自己也有一个
叫 `bootstrap` 的资产**（`bootstrap.imy`，装它的 `java` 桥）。两者撞名后，
用户那份会被**静默丢弃**：

- 本地增量构建有时还能带上（缓存里留着上次的产物），
- CI 的干净构建则稳定缺失 —— 于是装到真机上启动就报模块找不到。

**处理**：入口模块改名 `device_server.py`，并加了契约测试锁住
"入口模块名不得与 Chaquopy 保留名冲突"，以及发布流程里的校验
（`app.imy` 里必须真的有 `device_server.pyc`，否则拒绝发布）。

## 3. 图标显示空白

桌面图标位图一直都在，缺的是**自适应图标**（`mipmap-anydpi-v26`）。
Android 8+ 会把图标按自适应图标处理，找不到定义就渲染成空白/默认方块 ——
看起来就像"图标丢了"。

**处理**：补上 `mipmap-anydpi-v26/ic_launcher.xml`（背景色 + 前景图），
并加了契约测试。

## 4. 换成固定签名（以后能覆盖更新）

以前每次构建都用 **debug 签名**，而 debug keystore 是**每台机器、每次 CI
各自生成**的。同一个包名用不同签名安装时，系统会直接拒绝
（提示"应用未安装"），用户只能卸载重装、配置全丢。

**处理**：生成了一个固定的发布 keystore（RSA 4096，有效期 30 年），
debug 与 release 构建都用它签名；密钥存在 GitHub Secrets 里、
**绝不入库**（`.gitignore` 已挡住）。发布流程会校验签名，
签错就直接失败，不会把用户装不上的包发出去。

> ⚠️ **因此这一版需要先卸载旧版再安装。** 之前的版本用的是 debug 签名，
> 与新的发布签名不同，系统不允许覆盖。**卸载会清掉应用内的打印机配置**
> （访问代码等），需要重新添加。
>
> 从这一版开始，后续版本都能正常覆盖更新了。

## 下载

| 你的机器 | 下载 |
| --- | --- |
| **安卓平板 / 手机 / ChromeOS** | `BambuMonitor-android-arm64.apk` ← **本次主要变更** |
| Windows | `BambuMonitor-windows-x64.exe` |
| Windows（命令行） | `BambuMonitor-windows-x64-cli.exe` |
| Linux 服务器 / NAS | `BambuMonitor-linux-headless-x64` |
| Linux 桌面 | `BambuMonitor-linux-gui-x64` |
| Docker | `BambuMonitor-docker-image.tar.gz` |

安卓包 `versionName` 为 `1.0.4`（`versionCode 5`）。
校验：`sha256sum -c SHA256SUMS.txt`。

## 仍然需要你反馈的

安卓版**依旧没有在真机上跑过完整回归**（开发机没有安卓设备、
也没有核显跑模拟器）。这一版改的是打包与签名，属于"静态检查 + 包内容核查通过、
但必须真机确认"的改动。装好后请留意：

1. 能否正常启动（不再闪退、不再报模块找不到）；
2. 桌面图标是否正常显示；
3. 切后台/息屏后遥测是否稳定（这是上一版加的前台服务的验证点）；
4. A1 / P1 / A2L 的画面是否正常（走 6000 端口，不受本次改动影响）。
