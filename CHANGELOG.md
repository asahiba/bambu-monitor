# 更新日志

本文件记录值得用户注意的变更。格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [1.0.5] — 2026-09-18

> 这一版的两条主线：**「暂停/停止不能用」终于被说清楚了**，以及
> **配置可以在所有版本之间搬**（电脑导出、平板导入）。附带给网页/安卓补齐了
> 桌面版才有的配置备份、通道诊断与重点画面，并复查了全部发包形态。

### 新增

- **配置可以在**任何版本之间搬**（导出 / 导入带口令）**：以前导出的访问代码是按
  **本机方式**加密的（Windows 是 DPAPI、Linux 是 `secret.key`），换台机器就解不开 ——
  而"在电脑上导出、在平板上导入"恰恰是最自然的用法。
  现在导出时可选**口令**：带口令的文件用标准库算法加密（PBKDF2 + HMAC-SHA256 流加密 +
  先加密后认证），**Windows / Linux / Docker / 安卓通用**；安卓版也能解，因为它只用
  标准库（APK 刻意不打包 `cryptography`）。
  导入时若某些凭据解不开，会**逐个点名**是哪些设备，并提示下次导出带口令。
  入口：桌面版菜单、网页/平板「⚙ 设置 → 配置备份」、命令行
  `--export-config/--import-config/--config-passphrase`。
- **网页与安卓端补齐了桌面版的能力**：配置导出/导入、**通道诊断**
  （平板上没法跑 `tools/diagnose.py`，而"画面出不来"正是最需要它的场景）、
  **重点画面（2×2）**开关。诊断与命令行、桌面对话框**共用同一份实现**，结论必然一致。
- **「暂停/停止不能用」的提示写得更有用**：明确说明**只开局域网模式没有用**，
  必须先开「局域网模式」再开「开发者模式」（顺序不能反），此时**只有灯光能控制**，
  并推荐改用官方「农场管家 / Farm Manager」。桌面版被挡的按钮现在**点得动**，
  点一下弹出完整步骤（Qt 的禁用控件不接收鼠标事件，以前 tooltip 根本不会显示）。
- **安卓版 RTSPS-only 机型会明确说明为什么没有画面**：X1 / X1C / X2D / H2 / P2S
  在安卓上没有画面（APK 为兼容 16KB 内存页设备不内置 OpenCV），以前只显示"连接中"，
  用户会一直去查局域网模式与访问代码。现在网页端把那句说明**直接画在画面上**。

### 修复

- **「暂停/停止点了没反应」的指引写错了方向**：原文案写成「在打印机屏幕上**任选一条**
  放行即可」，把「局域网模式」与「开发者模式」并列成二选一。实机结论（12 台设备）是：
  **只开局域网模式没有用** —— 画面与遥测一切正常，但暂停/停止仍然被固件忽略，
  用户卡在这一步以为是软件坏了。现在三个界面（桌面 / 网页 / 命令行）都明确：
  **必须先开「局域网模式」、再开「开发者模式」，两步缺一不可、顺序不能反**；
  并**推荐**改用官方「农场管家 / Farm Manager」（它拿得到签名密钥，不需要开发者模式）；
  同时说清**此时只有灯光能控制**（灯控走 `system` 段）。
- **被挡住的按钮在桌面上看不到任何原因**：Qt 的**禁用控件不接收鼠标事件**，
  所以暂停/停止置灰后 tooltip 永远弹不出来、点了也没反应 —— 而那段文案正是
  用户唯一的线索。现在被挡的按钮**保持可点**（视觉上仍然是灰的），点下去弹出
  完整步骤；命令行 `--control pause` 也会打印同一段说明，而不是笼统的「发送失败」。
- **控制按钮的样式在第一次刷新后被冲掉**：`tile.py` 用 `setStyleSheet("color: …")`
  覆盖了构造函数里那套按钮样式，于是刷新一次后按钮就变回 Qt 默认外观。
  现在是在原样式上叠加。

- **`run.bat` 现在能自愈「虚拟环境坏了」**：它原来只判断 `.venv\Scripts\python.exe`
  是否存在，而基础 Python 被移动/删除后那个文件还在、一启动就退出（退出码 103）——
  用户双击只看到窗口一闪。现在会真的启动一次解释器，不行就重建 `.venv`
  （与 `test.bat` 同一条判据），并有 `tools/run_bat_selfheal_check.py` 造现场自证。
- **两个工具脚本「明明通过却被报成失败」**：`tools/check_qt_imports.py` /
  `check_qt_imports_probe.py` 打印 `✓`/`✗` 前没切 UTF-8，在 Windows 上只要输出被
  重定向（CI、`> log.txt`、`subprocess(capture_output=True)`）就 `UnicodeEncodeError`；
  `tools/check_workflow_ps.py` 则因为缺 `PyYAML` 直接甩 traceback。现在前者统一
  调用 `enable_utf8()`，后者缺依赖时给出安装提示并跳过（`PyYAML` 已加入
  `requirements-dev.txt`）。
- **`tools/layout_check.py` 会「卡死」**：单画面模式下不在网格里的画面
  `getItemPosition(-1)` 返回未初始化内存（实测拿到 `@-57146960,451 -54902140×451`），
  照着它 `range()` 展开就是上亿次循环。现在跳过不在网格中的画面、并给跨格加上限。
- **Linux / Docker / NAS 上访问代码不再明文落盘**：以前只有 Windows 的 DPAPI，
  其它平台一律明文保存 `config.json`。现在补了第二层：32 字节随机密钥存在配置目录的
  `secret.key`（权限 `0600`），或用 `BAMBU_MONITOR_SECRET` 口令派生密钥（不落盘）、
  用 `BAMBU_MONITOR_KEY_FILE` 把密钥挂到别处。它防得住「配置文件被单独复制走」，
  防不住能读整个目录的人 —— `SECURITY.md` 与 `docs/DEPLOY.md` 已写明。
  **安卓版仍是明文**：APK 刻意不含 `cryptography`（见 `docs/PACKAGING.md`）。
- **纯内网环境下自动搜索一台都搜不到**：网卡枚举原来会回退到「UDP 探测外网地址」，
  没有默认路由就什么也枚举不到。现在改用 POSIX `ioctl` 直接问内核（离线可用、带真实掩码），
  外网探测降级为最后一层兜底；还能用 `BAMBU_MONITOR_SUBNETS=192.168.1.0/24` 手工指定网段。
- **Docker / NAS 上手机流量翻倍**：没有 Qt 时 `_shrink_jpeg()` 直接返回 None，
  超过 90KB 的画面按原图推给手机，而且完全静默。现在用 OpenCV 兜底缩放。
- **「导出配置」一点就报错**：`export_config()` 里写成了
  `from PySide6.QtWidgets import QStandardPaths`，而 `QStandardPaths` 属于 **QtCore**。
  这类错误 pyflakes / ruff 都查不出来（模块存在，只是名字不在里面），
  只有真的点按钮才会抛 `ImportError`；打包版报错信息里会出现
  `cannot import name 'QStandardPaths' from 'PySide6.QtWidgets' (...MEI0000...\PySide6\QtWidgets.pyd)`，
  后面的解包路径正是 PyInstaller 的临时目录。
- **「导出配置 / 导入配置」少了一半字段**：`import_from()` 只恢复打印机列表与网页设置，
  `last_timeout` / `show_timestamp` / `auto_connect` / `web_enabled` 被静默丢弃；
  `PrinterInfo.discovered` 会被写出却从不读回。`window_geometry` 仍然有意不导入
  （屏幕坐标跨机器会把窗口丢到屏幕外）。
- **自动搜索列表里同一台打印机出现两次**：对话框用「序列号或 IP」单键去重，
  而 SSDP 回包带序列号、2021 端口广播只认得出 IP。现在改走 `discovery.merge_devices()`
  的双键合并，「已添加」的判定也一并按双键查。
- **网页状态行把中文提示截成半句话**：`status_text` 原来是 `detail[:14]`。
  现在桌面角标与网页共用 `app.core.camera_status_text()`（短标签 + 完整说明），
  完整原因放在悬浮提示里。
- **`python -O` 下发出的鉴权包可能畸形**：`build_auth_packet()` 的长度检查是 `assert`，
  优化模式下会被整条去掉。已改成显式 `ValueError`。
- **重连时新旧两轮线程并行**：`PrinterSession.restart()` 与轮询型适配器的
  `PollingDeviceSession.restart()` 都是 `stop()` 后 `sleep(0.2)` 就重启，
  而旧线程可能还在跑（RTSPS 取帧最长 20 秒）。现在改为等旧线程真正退出。
- **关掉「通道诊断」/「添加打印机」对话框可能让程序整个消失**：
  两个对话框的 `QThread` 只等 2~3 秒，而一轮诊断要 20 秒以上；
  等不到就继续析构，**运行中的 QThread 被析构会让 Qt 直接 fail-fast**
  （Windows 退出码 `0xC0000409`，没有 Python traceback、没有日志）。
  现在探测与诊断支持取消（`probe.py` / `camera.py` 新增 `should_stop`），
  并在 `app/ui/qt_threads.py` 里加了「进程退出前等它跑完」的兜底。
- **网页监控开关的信号重入**：`start_web_server()` 里 `action_web.setChecked(True)`
  会触发 `toggled` → 重新进入 `toggle_web_server()` → 再以 `show_dialog=True`
  走一遍，于是**每次启动都弹出「网页信息」模态窗口**（无界面环境下表现为阻塞）。
  已用 `blockSignals` 掐断这次重入。
- **移除画面时不再漏等解码线程**（`tile.shutdown()` 补 `join`）：解码线程仍在访问
  `QImage` 时 Qt 已开始拆对象，属于同一类 fail-fast 风险。

### 变更

- **通道诊断只剩一份实现**：`tools/diagnose.py` 与界面「通道诊断」原来各写了一遍
  （连 RTSPS 候选路径都不一样 —— 命令行试 3 个、界面只试 1 个，于是出现过
  「命令行说通了、界面说不行」）。现在流程在 `app/bambu/diagnostics.py`，
  两个入口共用；步骤编号按实际顺序生成，界面少跑「真拉流」那步也不会跳号。
- **超时与等待时长集中到 `app/bambu/timeouts.py`**（与端口表 `ports.py` 同一条纪律：
  不导入任何其它 app 模块）。协议层 33 个常量分组命名（`*_TIMEOUT` / `*_JOIN` /
  `*_INTERVAL` / `*_CAP` / `*_SLACK`），`camera.py`、`rtsp.py`、`mqtt_worker.py`、
  `probe.py`、`printer.py`、`discovery.py`、`diagnostics.py` 与添加对话框全部改为引用它。
  数值一个都没改，只是终于能一处看全、一处调优。
- **`--host` 的警告写清楚了**：默认 `0.0.0.0` 是**监听所有网卡**（为了让同网段的手机
  能看），现在帮助文本、`SECURITY.md`、`docs/DEPLOY.md` 都写明了，并新增 `BAMBU_HOST`
  环境变量方便只想本机访问的部署。

### 修复（第二轮：命令行与打包）

- **命令行 `--import-config` 曾经"报成功但没保存"**：`import_from()` 只改内存、
  不落盘（调用方决定何时保存），命令行入口忘了存 —— 命令说"已导入 1 台"，
  重启后配置却没变。现在两个入口都会 save 并检查写盘错误。
- **打包配置不一致**：`BambuMonitor.spec`（目录式打包）漏了 `app.core.*` /
  `app.adapters.*` 的 hiddenimports（那部分是运行时按设备族动态导入的），
  而 `build_exe.bat` 又把打包参数**另写了一份在 bat 里** —— 同一件事两处定义。
  现在两边列表一致，bat 直接调用 spec。`BambuMonitor-headless.spec` 也补上了
  `cv2` / `cryptography`（缺 cv2 的后果是"X1/X2D 这类只有 RTSPS 通道的机型没画面"）。
- **`RELEASING.md` 里"APK 是 debug 签名"是过时说明**：工作流早就改成固定发布签名，
  并强制拒绝 debug 签名（否则用户无法覆盖安装）。文档已按实际行为改写。
- **网页状态行的「本机出不了画面」原因**：见上面"新增"最后一条 —— 安卓端 RTSPS-only
  机型以前只显示"连接中"，用户完全被误导。

### 测试

- 新增 `tests/test_config_portable.py`（15 条）、`tests/test_cli_config_transfer.py`（7 条）、
  `tests/test_video_availability.py`（8 条）；`test_web_management.py` 与
  `test_web_server.py` 覆盖新的网页接口（配置备份 / 诊断 / 画面大小）。
- 真跨平台验证：Windows 带口令导出 → **WSL（真实 Linux）**用标准库导入，
  访问代码 / 设备名 / 界面偏好 / 重点画面标记全部保留；口令错时报错；
  Windows 本机加密的文件在 Linux 上导入时逐个点名并提示带口令。
- Windows 单文件 exe 重新打包并冒烟（`--core-test` 全部通过），
  并验证新命令 `--export-config --config-passphrase` 在冻结产物里可用。
  编排顺序/编号/取消行为，以及一条「界面与 CLI 不得再自己写 socket / `connect_tls`」
  的契约（防止两份实现再次漂移）。
- 新增 `tests/test_tools_offline.py`：把**离线可跑**的工具脚本接进 CI（子进程断言退出码），
  并静态守住「打印 ✓/✗ 的脚本必须切 UTF-8」「`*check*.py` 必须有退出码」
  「`tools/*.py` 语法必须合法」「新工具要登记到 `tools/README.md`」。
- 新增 `tests/test_discovery_interfaces.py`（15 条）：网段手工指定、去重、三层来源的
  优先级，以及「没有外网也能枚举出网卡」。
- 新增 `tests/test_web_shrink_jpeg.py`（7 条）：Qt / OpenCV / 两者都没有 三条路径。
- 新增 `tests/test_camera_status_text.py`（9 条）：状态标签的优先级与「完整说明不截断」。
- 新增 `tests/test_timeouts.py`（14 条）：取值关系（重连等待必须比停止等待宽、
  读切片必须远小于帧超时）、模块纪律（不导入其它 app 模块），以及用 AST / `inspect`
  反查「常量真的被用上、使用方不再自己写字面量」。
- 新增 `tests/test_discover_dialog_dedup.py`（5 条）：同一台设备带 IP / 带序列号时只列一行
  （已用修复前的实现自证确实会列两行）。
- 扩充 `tests/test_secret.py`：本机密钥加密、密钥文件 `0600`、跨进程复用（子进程验证）、
  口令优先、密钥文件丢失的提示。
- 新增 `tests/test_ui_export_config.py`：真实主窗口 + 打桩的文件对话框，
  直接调用「导出 / 导入配置」按钮的槽函数（原来只测了 `config.export_to`，
  界面那段代码从未被执行，所以 bug 漏到了用户手里）。
- 新增 `tests/test_ui_smoke.py`：app 包全模块导入 + 所有对话框构造 +
  工具栏/右键菜单槽函数逐个调用 + 画面按钮，保证「点一下才执行」的代码不再有死角。
- 新增 `tests/test_ui_thread_shutdown.py`：用**子进程**断言退出码，
  覆盖「关掉对话框导致进程消失」的两个入口（崩溃会直接带走 pytest，只能这么测）。
- 新增 `tests/test_qt_imports.py` 与 `tools/check_qt_imports.py`：
  全仓库校验 PySide6 导入的属性是否真实存在，并提示正确来源模块。

### 已知限制

- **安卓版看不到 RTSPS-only 机型的画面**（X1 / X1C / X2D / H2 / P2S）。
  APK 为兼容 16KB 内存页设备刻意不打包 OpenCV，这类机型在安卓上只有遥测。
  现在界面会**直接说明原因**并告诉你去哪儿看画面（桌面版 / 服务端 / 网页），
  不再只显示"连接中"。A1 / A1 mini / P1P / P1S / A2L 走 6000 端口，不受影响。
- **Android 包固定用发布证书签名**：与 1.0.4 及更早的包签名一致才能覆盖安装；
  如果你之前装的是更早的 debug 签名包，需要先卸载再装。
- Windows exe 无代码签名，可能被杀毒软件误报（要根治只能买代码签名证书）。
- 页面与控制走 HTTP（局域网），网页令牌不是身份认证 —— 不要暴露到公网，
  远程访问请自备 VPN 或带 TLS 的反向代理（见 `SECURITY.md`）。

## [1.0.4] — 2026-09-14

**安卓：修 16 KB 内存页闪退、入口模块被漏打包、图标空白，并换成固定签名。**

### 修复

- **16 KB 内存页设备（新 ARM Chromebook / Pixel 8+）闪退**：
  `opencv-python-headless` / `numpy` / `cryptography` 在 Chaquopy 仓库里的
  预编译库是 **4096 字节对齐**的，16 KB 页设备**拒绝加载** —— Python 启动时
  一导入就抛异常，表现为闪退。Chaquopy 17.0.0 发布说明已明确警告：
  "any Android wheels built before October 2024 will still fail to load on
  16 KB devices."

  现在安卓构建**不再安装这三个包**（它们在代码里都是可选的）：
  RTSPS 通道不可用时自动退回 6000 端口 JPEG，A1/P1/A2L 不受影响；
  cryptography 只给内置模拟器用。**APK 从 33 MB 降到 19 MB。**

- **启动报 `ModuleNotFoundError: No module named 'bootstrap'`**：
  安卓侧入口模块原名 `bootstrap.py`，与 Chaquopy 自己的 `bootstrap.imy`
  （装它的 `java` 桥）撞名，用户那份被**静默丢弃**。本地增量构建因缓存
  有时还能带上，CI 干净构建则稳定缺失。已改名 `device_server.py`，
  并加契约测试与发布校验（`app.imy` 里必须真的有 `device_server.pyc`）。

- **桌面图标空白**：缺自适应图标（`mipmap-anydpi-v26`），Android 8+ 找不到
  定义就渲染成空白/默认方块。已补上背景色 + 前景图。

### 变更

- **换用固定发布签名**：以前每次都不同（debug keystore 每台机器/每次 CI
  各自生成），导致用户**无法覆盖安装**（系统报"应用未安装"）。
  现在 debug 与 release 共用同一个固定 keystore，密钥存在 GitHub Secrets、
  绝不入库。发布流程会校验签名，签错直接失败。
  ⚠️ **本版签名与之前不同，需先卸载旧版再安装**（会清掉应用内配置）。

### 测试

- 新增 `tests/test_android_packaging.py`（6 项）：入口模块名不得与 Chaquopy
  保留名冲突、pip 段不得安装 4096 对齐的原生包、必须配置固定签名且密钥
  不得入库、必须有自适应图标、构建脚本不得再下载 numpy wheel。
- 新增 `tools/check_workflow_ps.py`：校验工作流里 `pwsh` 步骤的语法
  （工作流里的 `run:` 没有编译期检查，写错只能等 CI 炸）。

## [1.0.3] — 2026-09-13

**安卓版后台保活 + ChromeOS / 桌面模式兼容。** 其他平台的产物与 1.0.2 相同。

### 新增

- **安卓：前台服务保活**（`MonitorService`）。原来 Python 服务跑在界面的后台
  线程里，切后台/息屏后会被系统冻结 CPU（Doze/App Standby）甚至杀掉进程，
  网页服务随之消失 —— 平板当监控屏时最不能接受。现在：
  - 常驻通知（低打扰、不可滑掉）把进程提到前台优先级；
  - `PARTIAL_WAKE_LOCK`：屏幕关了 CPU 继续跑（带 10 分钟超时 + 后台自动续，
    避免泄漏成永久耗电）；
  - `WifiLock(HIGH_PERF)`：息屏后 Wi-Fi 射频不掉，MQTT 长连接不断；
  - `START_STICKY`：被系统回收后自动重建。
- **安卓：电池白名单引导**。厂商 ROM 比原生安卓更激进，不加白名单仍可能被杀。
  首次启动问一次并跳到系统设置页。刻意**不申请**
  `REQUEST_IGNORE_BATTERY_OPTIMIZATIONS`（敏感权限，会被应用商店额外审查）。
- **ChromeOS / 桌面模式兼容**：
  - `touchscreen` 显式声明 `required="false"` —— ChromeOS 笔记本、DeX、
    电视盒子都没有触摸屏，隐式要求会被应用商店直接过滤掉；
  - `resizeableActivity=true`，窗口可自由缩放；
  - 沉浸式全屏**只在有触摸屏时**启用 —— 桌面环境里沉浸式会把窗口标题栏与
    系统栏一起藏掉，窗口拖不动、切换应用别扭。判据用 `FEATURE_TOUCHSCREEN`
    而不是屏幕大小。

### 变更

- 安卓通知栏常驻一条「后台运行中（端口 8080）」，点它回到界面。
- 服务失联对话框增加「去设置」入口；「退出」改为「关闭界面」并说明后台仍在运行。

### 测试

- 新增 `tests/test_android_manifest.py`（5 项）：前台服务类型与权限齐全、
  不申请位置与敏感电池权限、触摸屏可选 + 可缩放、沉浸式只在触摸设备上、
  Java 引用的 `R.string.*` 都有定义。这些**只在真机上才会出问题**，
  所以放进契约测试在每次改清单时拦住。

## [1.0.2] — 2026-09-12

修两个安卓上真实遇到的问题：遥测会断连且不再自愈、令牌看不到导致没法从局域网打开。

### 修复

- **遥测（MQTT）时不时断连、而且不再自愈**：
  `paho` 自己会重连，但它**也会卡死** —— Android 息屏/切网之后底层 socket
  状态坏掉，退避重连一直失败却不再报错；初始 connect 一直失败时它也不会重走
  一遍 TLS 探测。更糟的是连接流程里 **TLS 探测之后没有异常保护**，一旦抛异常
  后台线程会静默退出且永不重试，遥测就永久停在断连状态，界面一直显示
  「画面正常·遥测断开」，只能手动点重连。

  现在：连接流程兜住所有异常（把原因写进状态，而不是静默死掉）；
  看门狗每 15 秒多做一件事 —— **90 秒没有成功连接过就整条重建**。
  判据是"连接成功次数有没有涨"，所以不会和 paho 自身的重连打架，
  也不会因为打印机偶尔断开就频繁重建。

- **网页里看不到访问令牌，导致无法从局域网打开**：
  令牌只在打开页面那一刻出现在 URL 里，随后被前端 `history.replaceState`
  抹掉（免得截图/分享时泄露）；而安卓版没有终端，看不到启动时打印的地址。
  结果就是**平板本机能用，却拿不到令牌、没法在电脑上打开**。
  桌面版有「网页信息」对话框，安卓版只能靠网页自己给。

  现在：新增 `GET /api/info`（需令牌鉴权），网页「⚙ 设置」底部多了
  「**在其它设备上打开**」—— 逐条列出带令牌的地址 + 复制按钮，
  访问令牌单独一行也带复制（触屏上复制比手抄 16 位令牌现实得多）。

### 顺带修掉的两个真 bug

- `/api/info` 原本读 `self.port`，而 `_Handler` 上没有这个属性 —— 实际返回 500。
- 接口返回的端口取的是 `WebServer.port`，`port=0`（系统分配临时端口）时它是 `0`，
  用户会拿到 `http://192.168.x.x:0/...` 这种**直接不可用**的地址。
  改为读实际绑定的端口。

### 说明

- 地址枚举失败时降级为"只给令牌"（令牌才是用户真正拿不到的东西），
  不再让整个接口 500。
- 地址算法抽成 `build_share_urls()`，与 `WebServer.urls()` 共用一份实现，
  避免出现"界面给的地址打不开、终端里那条却是对的"。

## [1.0.1] — 2026-09-12

只改提示文案，**没有功能变更**。但值得单独发一版：旧文案会把用户引向错误的排查方向。

### 修复

- **网页端控制提示文案不准确**：状态条的提示原本文案是「⚠ 控制不可用」，
  而它的触发条件是「固件要求 MQTT 命令签名」。问题有两个：
  1. **把范围说大了** —— 签名只覆盖 `print` 段的暂停/继续/停止/速度档位，
     而**灯控走 `system` 段、不受影响**。用户看到"控制不可用"却发现灯明明能开关，
     提示与事实矛盾，比不提示更糟。
  2. **解法只写了开发者模式** —— 实际有三条可行路径，而**局域网模式**才是最常见的解法。

  现在按具体被挡的命令逐条说明，并给出完整解法：

  | 方式 | 位置 |
  | --- | --- |
  | 局域网模式 | 设置 → 网络 → 局域网模式 / LAN Mode Only |
  | 开发者模式 | 设置 → 通用 → 开发者模式 / Developer Mode |
  | 农场管家 | 改用官方 Farm Manager 接管设备 |

  同时明确写出：**只在切片软件里登录拓竹账号是没用的**（那只影响云端，
  第三方客户端走局域网接口），以及「开关灯不受影响」的原因。
  详见 [`docs/FIELD_NOTES.md`](docs/FIELD_NOTES.md) 结论二。

### 文档

- `docs/FIELD_NOTES.md`：签名那节补上三条放行方式、账号登录无效的实测结论、
  以及「签名只覆盖 print 段、灯控走 system 段」这个事实。
- `README.md` 常见问题新增两条：「能监控但暂停/停止点了没反应」与
  「为什么暂停不能用但灯能开关」。

### 测试

- 契约测试加严：提示必须同时提到「局域网模式」与「开发者模式」；
  完整原因里必须出现「登录」「账号」（说清登录无效）与「灯」。
- 内嵌 JS 现在会真的用 esprima 解析一遍（CI 也装上该依赖）。

## [1.0.0] — 2026-09-12

首个公开版本。四种交付形态共用同一套 Python 内核，**网页端是各平台统一的操作界面**。

### 新增

- **监控与遥测**：MQTT over TLS（8883）订阅状态报文，展示进度、剩余时间、
  喷嘴/热床/腔体温度、AMS 各料盘余量与类型、层数、HMS 错误码中文解释。
- **摄像头**：6000 端口鉴权 JPEG 流 + RTSPS（322，H.264，需 OpenCV）双通道，
  按机型与实际连通性自动选择并回退。
- **自动发现**：SSDP 组播 + 单播补扫 + 旧版 2021 端口广播，覆盖不响应组播的机型。
- **设备管理**：自动搜索、手动添加、改名、改访问代码、删除、重连；多台并排监控。
- **控制**：暂停 / 继续 / 停止 / 打印速度 / 腔体灯。
- **网页端**：单连接多路复用（一条连接同时推所有画面与状态），
  绕开浏览器同域 6 连接限制；内置 PWA 清单，可「添加到主屏幕」。
- **设备族抽象**：`app/core/` 与厂商无关，`app/adapters/<family>/` 放具体实现。
  已内置 Klipper / Moonraker 适配器（覆盖 Snapmaker U1 等）。
- **机型覆盖**：X1 / X1C / X1E / P1P / P1S / A1 / A1 mini / A2L / P2S /
  H2D / H2D Pro / H2C / X2D，含序列号前缀与固件代号（`devmodel`）双重识别。
- **内置模拟器**：伪造完整的打印机（含 TLS、JPEG 帧、发现协议），
  开发与测试完全不需要真机。

### 四种交付形态

| 形态 | 产物 | 目标机需要什么 |
| --- | --- | --- |
| Windows | 单文件 exe（自带 Python 运行时） | 什么都不用装 |
| Linux | 单文件（无界面服务版 / 带界面版） | 什么都不用装 |
| Docker | 单文件镜像 tar.gz（`docker load` 即用） | 只需 Docker |
| 安卓 | APK（arm64，内置 Python + OpenCV） | Android 7.0+ |

### 修复

这些是开发过程中真实踩到、且**只在特定平台暴露**的坑：

- **安卓首次打开报 `unauthorized`**：`AppConfig.load()` 在配置文件不存在时
  并不幂等（每次调用新生成令牌且不落盘），宿主与服务各取一次就拿到两个令牌。
  令牌解析已收敛到单一来源 `resolved_web_token()`，先落盘再返回。
- **安卓自动搜索里同一台设备出现两次**：去重键只有 `serial or ip`，
  同一台机器被两条通道发现、其中一条没带 `USN` 时会登记成两条。
  改为按序列号与 IP 双重索引。
- **安卓添加设备报「添加失败当前系统没有 DPAPI」**（设备其实已加上）：
  安卓没有 DPAPI，凭据退回明文保存是**正常降级**，却被写进 `last_error`
  而被当成失败。诊断已分两级：`last_error`（阻断）与 `warnings`（提示）。
- **`threading.Thread` 子类的属性遮蔽**：`self._stop` 覆盖了 `Thread._stop()`，
  3.10/3.11 的 `join()` 会抛 `TypeError: 'Event' object is not callable`；
  `FrameDecoder._target` 覆盖 `Thread._target` 影响所有版本。
  3.13 改了实现，所以桌面版一直看不出来。
- **A2L 无法开关灯**：签名门禁原先一刀切，实际固件只挡 `print` 段命令，
  灯控走 `system` 段不受影响。改为按命令判断。
- **Docker 容器起不来会无限重启**：空配置时进程直接退出，被重启策略反复拉起。
  现在空配置也会正常起服务。

### 已知限制

- 安卓 APK 尚未在真机上跑过完整回归（构建机无安卓设备、也无核显跑模拟器）；
  已做的验证见 `docs/PACKAGING.md`。
- 固件开启 Developer Mode 签名要求时，`print` 段命令（暂停/继续/停止/调速）
  会被设备静默忽略，界面会明确提示原因。
- Linux / 安卓平台没有 DPAPI，打印机访问代码是**明文存储**，见 `SECURITY.md`。
- APK 是 debug 签名，仅供自用安装；上架应用商店需换正式签名。

[未发布]: https://github.com/asahiba/bambu-monitor/compare/v1.0.4...HEAD
[1.0.4]: https://github.com/asahiba/bambu-monitor/releases/tag/v1.0.4
[1.0.3]: https://github.com/asahiba/bambu-monitor/releases/tag/v1.0.3
[1.0.2]: https://github.com/asahiba/bambu-monitor/releases/tag/v1.0.2
[1.0.1]: https://github.com/asahiba/bambu-monitor/releases/tag/v1.0.1
[1.0.0]: https://github.com/asahiba/bambu-monitor/releases/tag/v1.0.0
