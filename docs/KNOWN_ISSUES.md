# 已知问题与技术债

这份清单是 v1.0.0 → 环境修复期间一次完整静态审计的产出，用来承接「还没做、但已经确认」的问题。
每条都带**文件:行号**与**为什么是问题**，方便直接开工。

状态标记：`已修`（本次修复）、`待修`（已确认，未动手）、`存疑`（静态推断，需真机或实跑复现）。

## 〇、当前测试保护情况

修复过程中建立了 pytest 回归套件（`tests/`，13 个文件）：

```
test.bat                        →  433 passed, 7 skipped   （约 14 秒，完全离线）
set BAMBU_RUN_SLOW=1 && test.bat -m slow
                                →  7 passed                （内置模拟器端到端，约 5 秒）
```

覆盖范围：机型识别与报文解析、配置读写与钳制、DPAPI 凭据、`build_auth_packet` 布局、
网页服务全部路由与鉴权、`PrinterSession` 通道选择与控制、HMS 文案层与数据文件降级、
发现协议解析、模拟器端到端（6000 画面 + MQTT 遥测）。

> **仍然没有自动化覆盖**的地方（改这些代码时请手动验证，并考虑顺手补测试）：
> `app/ui/` 全部界面逻辑（`main_window.py` 715 行、`tile.py` 522 行只有离屏装箱检查）、
> `app/bambu/tlsutil.select_context()` 的握手降级与缓存、
> `app/bambu/discovery.py` 的真实收发路径与网卡枚举、
> `app/web/page.py` 的前端 JS（无前端测试与语法检查）、
> `app/headless.py` 的 `--discover` / `--add-printer` 命令行管理流程。

### 安卓版踩过的坑（都在通用代码里，桌面版看不出来）

这三个问题**只在安卓上暴露**，但根因都在共用代码中，值得单独记一笔：

| 症状 | 根因 | 修法 |
| --- | --- | --- |
| 首次打开就是 `{"error": "unauthorized"}` | `AppConfig.load()` 在无配置文件时**不幂等**（每次调用新生成令牌且不落盘）；宿主与服务各 load 一次，拿到两个令牌 | 令牌解析收敛到 `app.headless.resolved_web_token()`，先落盘再返回 |
| 自动搜索里同一台设备出现两次 | `DiscoveryService._register` 只按 `serial or ip` 当键；同一台机器被两条通道发现、其中一条没带 `USN` 时，会以「序列号键」和「IP 键」各登记一次 | 改为按序列号与 IP **双重索引**；`WebHost.discover()` 再兜底去一次重 |
| 添加设备报「添加失败当前系统没有 DPAPI」，但设备其实加上了 | 安卓没有 DPAPI，`encrypt_text` 按设计退回明文保存 —— 这是**正常降级**，却写进了 `AppConfig.last_error`，而 `WebHost.add_printer` 见 `last_error` 非空即判失败 | 诊断分两级：`last_error`（阻断，写盘失败）与 `warnings`（提示，不阻断） |

回归测试：`tests/test_android_bootstrap_token.py`、
`tests/test_android_discovery_and_dpapi.py`（共 17 项）。

> 另一个只有 3.10/3.11 才暴露的坑见 `docs/PACKAGING.md`：
> 给 `threading.Thread` 子类挂 `self._stop` 会遮蔽 `Thread._stop()`，
> 让 `join()` 抛 `TypeError`。3.13 改了实现，所以桌面版一直正常。

---

## 一、已修（本次）

### 1. `selftest.bat` 永远报失败（已修）
`app/selftest.py:88` 打印 `✓`，Windows 默认 GBK 控制台无法编码该字符 →
`UnicodeEncodeError` → 自检明明全部通过却以退出码 1 结束。

修法：新增 `app/util/configure_stdio()`，在 `app/main.py`、`app/headless.py` 入口
把 stdout/stderr 切成 UTF-8（无法编码的字符降级为 `?`）。

### 2. 网页服务重启后画面宽度静默回到 720（已修）
`app/web/server.py` 的 `WebServer.start()` 重建 `WebFrameCache` 时漏传 `max_width`
（构造函数传了、`start()` 没传）→ 用户设置 480px，重启网页服务后变回 720px。

修法：新增实例属性 `self.max_width`，`start()` 重建时原样传入。

### 3. 线程不可回收 / stop 竞态（已修）
* `app/web/server.py`：`WebFrameCache`（转码线程）与 `web-server` 线程都只置停止标志、从不 join
  → 反复开关网页监控会累积线程。现两者都在 `stop()` 里 join（各 2 秒）。
* `app/bambu/printer.py`：视频建立线程用局部变量持有，`stop()` 无法等待；
  `_switch_channel()` 的匿名线程同样游离。现统一经 `_spawn_video_thread()` 记录，
  `stop()` 里 `_join_video_threads(2.0)` 收尾。
* `app/bambu/printer.py`：看门狗换道与首帧建立之间没有互斥，可能互相把对方的流停掉
  （表现为「画面刚出来又断」）。现用 `_video_setup_lock` 串行化。
* `app/bambu/mqtt_worker.py`：`stop()` 发生在 TLS 探测之后、`_client` 赋值之前时，
  `_start_blocking()` 仍会 `connect_async()` + `loop_start()`，产生**永不回收的僵尸 MQTT 连接**。
  现已在该处检查 `_stop_requested` 并主动断开；`stop()` 也会 join `_start_blocking` 线程。

### 4. 配置保存与凭据加密失败被静默吞掉（已修）
`app/config.py` 的备份、`os.replace` 写盘失败原先是 `except OSError: pass`
→ 用户看到「配置已保存」，实际没落盘；`app/util/secret.py` 加密失败静默回退明文、
解密失败静默返回空串（表现为「画面不出来」，无从排查）。

修法：`AppConfig` 新增 `last_error` 字段；`secret.py` 新增 `last_error()` /
`clear_last_error()` 记录降级诊断并写日志；界面在保存/导出配置时弹警告，
启动时若读取阶段已有降级也会在状态栏提示。

### 5. `AppConfig.load()` 对「合法 JSON 但内容不可用」不鲁棒（已修）
README 承诺「主配置损坏时会自动从备份恢复」，但 `load()` 只捕获
`OSError` / `JSONDecodeError`。若 `config.json` 是合法 JSON、内容却不可用 ——
根是数组、`tile_span: "auto"`、`web_port: "http"` —— 异常会从 `_parse()` 里
一路抛到 `main()`，**程序启动即崩**，连备份都轮不到。

修法：`load()` 把 `_parse()` 也纳入 try，失败则继续尝试备份，最后回落到默认值
并把原因写进 `last_error`；`_parse()` 内部对每个字段用 `_coerce_int` / `_coerce_float`
优雅降级（类型不对 → 用默认值 + 记日志），`printers` 不是数组时也保留其余偏好。
回归用例见 `tests/test_config.py`。

### 6. `max_fps = 0`（注释写着「0 = 不限制帧率」）无法保存（已修）
`float(data.get("max_fps", 10.0) or 10.0)` —— `or` 把合法的 `0` 当成了「没填」，
于是「不限帧率」这个设置**存下来再读回永远是 10**，用户在设置里改不动。

修法：改用 `_coerce_float`（不再依赖真值判断）。同类问题一并处理：
`columns` / `tile_span` 的 `or` 写法换成了显式取值。

### 7. 带空格的机型名识别错误（已修）
真机 SSDP 的 `devmodel` 常见写法是 `Bambu Lab X1 Carbon`，而关键字表里只有
无空格的 `"x1carbon"`，于是会退化成命中靠后的 `"x1"` 关键字 —— **X1C 被显示成 X1**。

修法：新增 `_normalize_model_text()`，匹配前统一去掉空白与 `-`/`_` 并转小写；
关键字表本身按「更具体优先」排序，因此 `X1 Carbon / X1-Carbon / x1carbon`
都正确识别为 X1C。回归用例见 `tests/test_models.py`。

### 8. 模拟器序列号不符合真机格式（已修）
`SIM_MODELS` 用的是 13 位序列号，而真机规则要求 **15 位大写字母数字**
（`discovery._looks_like_bambu()`）。后果：模拟器「必须带 devmodel 头」才能被发现，
**「没有 dev* 头、仅凭序列号识别」这条发现路径在模拟器上永远走不通**，
也就一直没有被验证过。

修法：把模拟器序列号改成 15 位（`01P00A123456789` 等），并把 `docs/PROTOCOL.md`
里的示例同步成 15 位。

### 9. `_looks_like_bambu()` 只认 `NT` 不认 `ST`（已修）
设备主动发出的 NOTIFY 用 `NT`，而对 M-SEARCH 的**应答**用 `ST`。
只检查 `nt` 会让「只带 ST、序列号又不规范」的回包被漏判。现两者都检查，
并补了对应回归用例（`tests/test_discovery_parse.py`）。

### 10. 本次并行引入又修掉的一个缺陷（记录以防重犯）
给 `WebServer.stop()` 补线程 `join` 时写成无条件 `self.cache.join(timeout=2.0)`，
而 `__init__` 创建的那个 `WebFrameCache` **从未 `start()`** —— 对未启动的线程
`join()` 会抛 `RuntimeError`，导致网页服务**连第一次都启动不了**
（`main_window.py`、`headless.py` 的调用点都没包 try）。
现统一走 `_stop_cache()`：只在 `is_alive()` 时 join。回归用例见
`tests/test_web_server.py`（首次 start 即可用 / stop 未启动的服务不抛异常）。

---

## 二、待修

### P1：可维护性

| # | 问题 | 位置 |
| --- | --- | --- |
| 1 | ~~`app/bambu/__init__.py` 的包级 re-export 无人使用~~ **已修**：移除 re-export 并写明理由，`from app.bambu import tlsutil` 不再连带拉起 paho | `app/bambu/__init__.py` |
| 2 | ~~`app/web/__init__.py` 的 `from .server import WebServer` 零调用方~~ **已修**：同样移除 | `app/web/__init__.py` |
| 3 | ~~死代码：`secret.is_encrypted()`（仅被测试引用）、`discovery.local_ipv4_addresses()`、`discovery._broadcast_addresses()` 全文无调用。**后两个已删除**；`is_encrypted()` 保留（它是 `dpapi:` 前缀判断的反向工具，且有测试）。`discovery.merge_devices()` 有完整测试覆盖且语义有用，已从「死代码」改为「待接入」——理想是让搜索对话框的累积逻辑改用它，避免两套去重实现~~ **已修**：`DiscoverDialog._add_row()` 改走 `merge_devices()`。原来对话框用 `serial or ip` 单键去重，而 SSDP 回包带序列号、2021 端口广播只认得出 IP —— 同一台打印机在列表里出现**两行**，访问代码只可能填在其中一行上。顺带把 `_known` 改成序列号/IP 双键（配置里只留了一个字段时也能认出「已添加」），新增 `tests/test_discover_dialog_dedup.py`（5 条，含用 HEAD 版实现自证旧行为确实列两行） | `app/ui/discover_dialog.py` |
| 4 | ~~生产代码里唯一的 `assert`（`python -O` 下会静默失效）~~ **已修**：`camera.build_auth_packet()` 的长度检查改成显式 `raise ValueError`，并抽出 `AUTH_PACKET_SIZE = 80` 常量。原来的 `assert` 在 `python -O` 下会被整条去掉，长度错了就静默发出一个畸形鉴权包（打印机只会断开连接，排查毫无线索） | `app/bambu/camera.py` |
| 5 | ~~端口硬编码散落~~ **已修**：新增 `app/bambu/ports.py` 集中 `MQTT_PORT`/`CAMERA_PORT`/`RTSP_PORT`/`DEFAULT_ACCESS_CODE`，`camera.py`、`mqtt_worker.py`、`rtsp.py`、`simulator.py`、`diagnose_dialog.py`、`main.py`、`headless.py`、`selftest.py` 全部改为引用常量。**超时常量尚未收敛**（见下方「仍待处理」） | `app/bambu/ports.py` |
| 6 | ~~`tools/layout_check.py` 已被 `tools/layout_fit_check.py` 取代（前者无退出码、无字体度量）~~ **已修并保留**：仍是「对照用」的过时脚本，但现在也补齐了退出码与重叠/占格检查，并修掉它自身两个坑（单画面模式下 `getItemPosition(-1)` 读到未初始化内存、脏数据导致 `range()` 展开上亿次 → 看起来像卡死）。优先用 `layout_fit_check.py` | `tools/layout_check.py` |
| 7 | ~~`tools/diagnose.py` 与 `app/ui/diagnose_dialog.py` 是同一套诊断流程的两份实现；`tools/rtsp_describe.py` 与 `diagnose_dialog.py` 重复实现 RTSP DESCRIBE。~~ **已修**：新增 `app/bambu/diagnostics.py`（探测返回结构化结果 + 渲染是纯函数 + `run()` 逐步产出事件），CLI 与界面都只是「跑一遍、把行打出来」。漂移的好处立刻体现出来：**界面以前只试 1 个 RTSP 路径、命令行试 3 个**，于是出现过「命令行说通了、界面说不行」；现在两条路必然给同一结论（`tests/test_diagnostics.py` 里有一条契约专门禁止界面/CLI 再自己写 socket/`connect_tls`）。编号也改成按实际顺序生成，界面少跑「真拉流」那步时不会再出现 ①②③④⑥ 跳号 | `app/bambu/diagnostics.py`、`tools/diagnose.py`、`app/ui/diagnose_dialog.py` |
| 8 | ~~`tools/README.md` 只登记 25 个脚本~~ **已修**：重新按用途分类登记全部 33 个，并标注已过时脚本与「不是可执行脚本」的 `_common.py` | `tools/README.md` |
| 9 | ~~导出配置默认目录用 `os.path.expanduser("~")`~~ **已修两轮**：先改成 `QStandardPaths.DocumentsLocation`，但当时把 `QStandardPaths` 从 `PySide6.QtWidgets` 导入了（它属于 **QtCore**）——pyflakes/ruff 都查不出来，只有用户点「导出配置」才炸，打包版报错里能看到 `MEI0000...\\PySide6\\QtWidgets.pyd`。现已修正导入，并补 `tests/test_ui_export_config.py`（直接调用按钮槽函数）与 `tests/test_qt_imports.py` + `tools/check_qt_imports.py`（全仓库校验 PySide6 导入） | `app/ui/main_window.py` |
| 10 | ~~`import_from()` 只恢复 printers/columns/max_fps/refresh_ms/web_port/web_token/web_fps/web_max_width，**不恢复** `last_timeout` / `show_timestamp` / `auto_connect` / `web_enabled` / `window_geometry`；另外 `PrinterInfo.discovered` 会被 `to_json()` 写出但 `_parse()` 从不读取（往返后丢成 False）~~ **已修**：补回 `last_timeout`/`show_timestamp`/`auto_connect`/`web_enabled`，`discovered` 在 `_parse()` 里读回。**`window_geometry` 仍然有意不导入**——那是屏幕坐标，从 4K 机器导到小屏笔记本会把窗口恢复到屏幕外（用户看到的是「导入配置后程序打不开了」），已写进 docstring 并由测试锁定。回归用例：`tests/test_config.py::test_export_import_roundtrip_keeps_界面偏好与discovered`、`::test_import_from_不导入窗口坐标` | `app/config.py` |

硬编码项明细：

* ~~**各超时/间隔没有统一常量表**~~ **已修**：新增 `app/bambu/timeouts.py`（28 个常量，
  分组：遥测 / 6000 画面 / RTSPS / 会话看门狗 / 自动搜索 / 探测与诊断），
  `camera.py`、`rtsp.py`、`mqtt_worker.py`、`probe.py`、`printer.py`、`discovery.py`、
  `add_dialog.py`、`diagnose_dialog.py` 全部改为引用常量；`printer.MQTT_STUCK_SECONDS`
  也移过去了（在原处保留导入，兼容既有调用方与测试）。
  与 `ports.py` 同一条纪律：本模块不导入任何其它 app 模块，所以协议层、界面、
  命令行都能安全引用。回归用例 `tests/test_timeouts.py`(14 条) 除了查取值，
  还用 AST/`inspect` 反查「常量真的被用上」并禁止使用方再写字面量。
  **仍未收敛**：`web/server.py` 自己的那组（`PASSTHROUGH_BYTES` / `CLIENT_TTL` /
  两个 sleep）与 `headless.py` / `selftest.py` / `simulator.py` 的等待时间 ——
  它们是服务进程与演示模式的节奏，不属于协议层参数，留在原处并已在注释里写明。
* **`time.sleep` 硬编码**（剩余）：`headless.py`(0.3/2.0)、`selftest.py`(1.0)、
  `simulator.py` 若干、`web/server.py`(0.02/0.05)。协议层里的都已收敛。
* **监听地址**：`headless.py` 与 `web/server.py` 的 `--host` 默认 `0.0.0.0`（有意为之：
  为了让同网段的手机也能看）。已在 `SECURITY.md` 的威胁模型与 `docs/DEPLOY.md`
  里写明「会监听所有网卡，别暴露到公网」，`--host` 帮助文本也标了「默认监听所有网卡」。

### P1：平台与安全

| # | 问题 | 位置 |
| --- | --- | --- |
| 10 | ~~Linux/Docker 无 DPAPI → 访问代码**明文**保存在 `config.json`（已加诊断提示，但存储方式未变）。若要真正加密，需引入用户口令或系统密钥环~~ **已修**：新增第二层加密（`fernet:` 前缀）——32 字节随机密钥落在配置目录的 `secret.key`（`os.open(..., 0o600)`，先建文件再写内容，避免"世界可读"窗口），或用 `BAMBU_MONITOR_SECRET` 口令 PBKDF2 派生（密钥不落盘），或用 `BAMBU_MONITOR_KEY_FILE` 挂到 Docker secrets。**安卓仍然明文**：APK 刻意不含 `cryptography`（4096 字节对齐的 wheel 会让 16 KB 内存页设备闪退，见 `docs/PACKAGING.md`），这条路必须保留。文档同步更新了 `SECURITY.md`（明确「防住配置文件被单独复制走、防不住能读目录的人」）与 `docs/DEPLOY.md` | `app/util/secret.py`、`app/config.py` |
| 11 | ~~网卡枚举在非 Windows 上回退到「UDP connect 探测 + `getaddrinfo`」，**依赖外网连通**（探测 223.5.5.5/8.8.8.8/1.1.1.1）；纯内网环境可能枚举不到网段，导致搜索不全~~ **已修**：新增 POSIX `ioctl` 枚举（Linux/macOS，离线可用、能拿到真实掩码），并把依赖外网的探测降级为最后一层兜底；再加环境变量 `BAMBU_MONITOR_SUBNETS` 手工指定网段（纯内网用户的补救手段）；三层结果按 IP 去重（先出现的优先）。枚举不出任何可扫描网卡时会打一条日志告诉用户怎么手工指定。回归用例 `tests/test_discovery_interfaces.py`(15 条) | `app/bambu/discovery.py:250-300` |
| 12 | ~~无 Qt 环境（Docker 只装 `requirements-server.txt`）时 `_shrink_jpeg()` 永远返回 None → 大于 90KB 的帧**不缩放直接推原图**，手机流量与内存开销上升。可加 OpenCV 兜底缩放~~ **已修**：拆成 `_shrink_with_qt` / `_shrink_with_cv2` 两条路径，OpenCV 兜底（`requirements-server.txt` 本来就装了 headless 版）；两条都不可用时**只提示一次**的日志（原来完全静默）。回归用例 `tests/test_web_shrink_jpeg.py`(7 条) | `app/web/server.py:41-63,138-141` |

### P2：测试与结构

| # | 问题 | 位置 |
| --- | --- | --- |
| 13 | ~~33 个 `tools/*check*.py` 是「内部 `ok` 布尔 + 打印 + 退出码」模式，**没有断言**，无法定位失败点，也无法按用例筛选~~ **部分已修**：拿真机才能跑的诊断脚本**不该**改写成 pytest 用例（它们要连设备、占端口），但**离线可跑的那批**现在真的进了 CI —— 新增 `tests/test_tools_offline.py`：把 `check_qt_imports_probe.py` / `check_workflow_ps.py` / `run_bat_selfheal_check.py` 当子进程跑并断言退出码 0（每条一个用例，失败点一眼可见），同时静态守住「会打印 ✓/✗ 的脚本必须切 UTF-8」「`*check*.py` 必须有退出码」「`tools/*.py` 语法必须合法」。剩下需要真机的脚本维持现状（它们本来就是诊断工具，见 `tools/README.md` 开头的约定） | `tools/`、`tests/test_tools_offline.py` |
| 14 | ~~无退出码、进不了 CI：`tools/layout_check.py`、`tools/discovery_bench.py`、`tools/show_config.py`、`tools/dialog_smoke.py`~~ **已修**：`layout_check.py` 补上退出码并顺手修掉自身两个坑（单画面模式下 `getItemPosition(-1)` 读到未初始化内存、脏数据导致 `range()` 展开上亿次 → 脚本看着像卡死）；`*check*.py` 的退出码由 `tests/test_tools_offline.py` 静态守住。`discovery_bench.py` 仍未加退出码（它是**基准测量**，结论是数字不是成败，已在 `tools/README.md` 标注） | 同左 |
| 15 | ~~五个对话框（诊断/布局/设置/HMS/网页）无冒烟覆盖（`tools/dialog_smoke.py` 只构造添加与搜索两个）~~ **已修**：`tests/test_ui_smoke.py::test_dialogs_construct_and_close` 现在把六个对话框（含诊断/布局/设置/HMS/网页）在 offscreen 下全部构造并关闭，另外逐条调用工具栏/右键菜单槽函数 | `tests/test_ui_smoke.py` |
| 16 | ~~`app/web/page.py` 是近 500 行的单文件内嵌 HTML+CSS+JS，无前端构建、无语法检查、无测试~~ **已修**：`tests/test_contracts.py` 用 `esprima` 真正解析内嵌 JS（语法错直接失败），CI 也装上 esprima；此外还有 PWA 清单、令牌鉴权、多路复用流等契约断言。仍然**没有前端构建**（有意为之：单文件内嵌是「不装 Node 也能跑」的取舍） | `app/web/page.py`、`tests/test_contracts.py` |
| 17 | ~~`app/ui/tile.py:134-136` 的 `shutdown()` 只 `stop()` 解码线程不 join；`add_dialog.py:199`、`diagnose_dialog.py:200` 的 `QThread.wait()` 超时后未处理~~ **已修**：三处都补齐。后两处原来会让**整个进程 fail-fast 消失**（运行中的 QThread 被析构 = 0xC0000409，用户看到的是「点开诊断/点测试连接后随手关掉 → 程序没了」）——现在探测与诊断都支持取消（`probe.py`/`camera.py` 的 `should_stop`），并新增 `app/ui/qt_threads.py` 在进程退出前兜底等待；回归测试见 `tests/test_ui_thread_shutdown.py`（用子进程断言退出码，因为崩溃会直接带走 pytest） | `app/ui/tile.py`、`app/ui/add_dialog.py`、`app/ui/diagnose_dialog.py`、`app/ui/qt_threads.py` |

---

## 三、存疑（静态推断，需复现确认）

1. ~~**`PrinterSession.restart()` 竞态**：`printer.py:restart()` 只 `sleep(0.2)` 就重新 `start()`，
   而上一轮的视频线程最长要 20 秒才退出。现在 `stop()` 会 join 线程，风险大减，
   但 `restart()` 的 0.2 秒等待仍不足以覆盖 RTSPS 场景，建议改成「等待 join 完成」而非定时。~~
   **已修**：`restart()` 改成 `stop()` + `_join_video_threads(5.0)` + `start()`，不再用定时等待；
   `app/core/adapter.py` 的轮询型适配器同样问题（`time.sleep(0.2)`）也一并修掉——`stop()` 记下
   被停掉的线程（`_retired_thread`），`restart()` 等它真的退出，超时才记日志继续。
2. ~~**访问代码错误时的重试节奏**：`rtsp.py` 与 `camera.py` 在鉴权失败后都会退避重试，
   若用户在打印中改了访问代码，界面可能延迟较久才恢复。~~
   **已确认不是问题（两侧入口都会主动重连）**：改访问代码的两条路径都会立刻整条重建会话 ——
   桌面版 `main_window.edit_printer()` 与网页版 `web/host.manage_printer(action="update")`
   都在改完 `access_code` 后调用 `session.restart()`（而 `restart()` 现在会等旧线程退出，
   见上一条）。退避只影响「改之前就连错」的那几秒，并且已经收敛进
   `app/bambu/timeouts.py`：失败退避 `CAMERA_BACKOFF_START` → `CAMERA_BACKOFF_MAX`（×1.6），
   鉴权失败单独用 `CAMERA_AUTH_BACKOFF`。
3. ~~**`_status_payload()` 的 `status_text` 取值**：`server.py` 里
   `(session.last_camera_detail or "连接中")[:14]` 会截断中文提示（14 个字符），
   有些文案（如「RTSPS(322) 未取到画面…」）被截得难以理解，建议改成固定短标签 + 悬浮详情。~~
   **已修**：新增 `app.core.camera_status_text()`（短标签 + 完整说明），桌面角标与网页状态行
   **共用同一份判据**（原来两处各写一遍，顺序还不一样），完整说明改走 `status_detail` +
   悬浮提示，不再截断。回归用例 `tests/test_camera_status_text.py`。

---

## 四、环境层面的坑（不是代码问题，但会浪费半天）

1. **虚拟环境不可搬迁**：`.venv\pyvenv.cfg` 里写死基础解释器路径。本项目历史上从
   `D:\DSH\bambu-monitor` 搬到 `L:\DSH\bambu-monitor`，而 `D:\python` 又被删除，
   导致 `.venv\Scripts\python.exe` 存在但无法启动（退出码 103，报
   `did not find executable at 'D:\python\python.exe'`）。
   ~~**`run.bat` 的自愈分支判断的是「python.exe 文件是否存在」，因此不会自动重建。**~~
   **已修**：`run.bat` 改成真的启动一次解释器（`-c "import sys"`，与 `test.bat` 同一条判据），
   启动不了就删掉 `.venv` 重建；`tools/run_bat_selfheal_check.py` 会造一个「文件在但启动不了」
   的假 `.venv` 来证明这条分支真的会走（已进 CI）。
2. 修好环境后建议跑一次 `test.bat`：它会先用 `python -c "import sys"` 验证解释器真的能启动，
   比 `if exist` 可靠。
3. `dist\BambuMonitor\` 是自包含的 PyInstaller 产物（约 240MB，241 项），
   即使源码环境坏了它也能跑 `--version` / `--core-test`，可作为对照基准。
4. ~~本项目**没有 CI**（无 `.github/`）。~~ **已过时**：仓库现在有 `.github/workflows/ci.yml`
   （Linux 3.13 / Linux 3.10 / Windows 3.13 三档跑 `pytest -q` 与 `-m slow`，外加 `ruff check`）
   与 `release.yml`（Windows 打包与 APK 签名）。`tools/check_workflow_ps.py` 校验工作流里
   `pwsh` 步骤的语法，`tests/test_tools_offline.py` 把离线可跑的检查脚本也接进了这条流水线。
5. **工具脚本的 UTF-8 坑**：Windows 默认控制台是 GBK，脚本里打印 `✓`/`✗` 时只要输出被
   重定向（CI、`> log.txt`、`subprocess(capture_output=True)`）就会 `UnicodeEncodeError` ——
   「检查通过」被报成「工具自己崩了」。约定：这类脚本开头必须调用
   `tools/_common.enable_utf8()`（或 `sys.stdout.reconfigure(encoding="utf-8")`），
   由 `tests/test_tools_offline.py` 静态守住。
