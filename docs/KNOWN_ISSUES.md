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
| 3 | 死代码：`secret.is_encrypted()`（仅被测试引用）、`discovery.local_ipv4_addresses()`、`discovery._broadcast_addresses()` 全文无调用。**后两个已删除**；`is_encrypted()` 保留（它是 `dpapi:` 前缀判断的反向工具，且有测试）。`discovery.merge_devices()` 有完整测试覆盖且语义有用，已从「死代码」改为「待接入」——理想是让搜索对话框的累积逻辑改用它，避免两套去重实现 | `app/util/secret.py` |
| 4 | 生产代码里唯一的 `assert`（`python -O` 下会静默失效） | `app/bambu/camera.py:59` |
| 5 | ~~端口硬编码散落~~ **已修**：新增 `app/bambu/ports.py` 集中 `MQTT_PORT`/`CAMERA_PORT`/`RTSP_PORT`/`DEFAULT_ACCESS_CODE`，`camera.py`、`mqtt_worker.py`、`rtsp.py`、`simulator.py`、`diagnose_dialog.py`、`main.py`、`headless.py`、`selftest.py` 全部改为引用常量。**超时常量尚未收敛**（见下方「仍待处理」） | `app/bambu/ports.py` |
| 6 | `tools/layout_check.py` 已被 `tools/layout_fit_check.py` 取代（前者无退出码、无字体度量）。**已加 `.. deprecated::` 说明**，未删除（保留作对照） | `tools/layout_check.py` |
| 7 | `tools/diagnose.py` 与 `app/ui/diagnose_dialog.py` 是同一套诊断流程的两份实现；`tools/rtsp_describe.py` 与 `diagnose_dialog.py` 重复实现 RTSP DESCRIBE。**未合并**：合并需要引入一个共用的诊断模块并同时改动 CLI 与 GUI，属于真正的重构，建议单独排期 | `tools/diagnose.py:43-96`、`app/ui/diagnose_dialog.py:52-150` |
| 8 | ~~`tools/README.md` 只登记 25 个脚本~~ **已修**：重新按用途分类登记全部 33 个，并标注已过时脚本与「不是可执行脚本」的 `_common.py` | `tools/README.md` |
| 9 | ~~导出配置默认目录用 `os.path.expanduser("~")`~~ **已修**：改用 `QStandardPaths.DocumentsLocation`，与抓拍保持一致 | `app/ui/main_window.py` |
| 10 | `import_from()` 只恢复 printers/columns/max_fps/refresh_ms/web_port/web_token/web_fps/web_max_width，**不恢复** `last_timeout` / `show_timestamp` / `auto_connect` / `web_enabled` / `window_geometry`；另外 `PrinterInfo.discovered` 会被 `to_json()` 写出但 `_parse()` 从不读取（往返后丢成 False） | `app/config.py` |

硬编码项明细（**端口与默认访问代码已收敛到 `app/bambu/ports.py`**，以下是尚未收敛的部分）：

* **各超时/间隔没有统一常量表**（只有 `tlsutil.py:83 HANDSHAKE_TIMEOUT=15.0` 与
  `web/server.py:28-38` 是集中的）：`camera.py:169`(30.0)`:202`(6.0)`:223`(25.0)`:240`(30.0)`；
  `mqtt_worker.py:127`(4.0)；`rtsp.py:38 open_timeout_ms=6000`；`probe.py:131` `min(12.0, timeout+4)`；
  `diagnose_dialog.py:61`(4.0)`:91`(10.0)`:104`(3.0)`:112`(5.0)`；`add_dialog.py:50`(10.0)`；
  `printer.py:94`(15.0)`:239`(4.0)`；`discovery.py:457`(1.5)`:463`(0.1)`:624,632`(0.05)`；
  `web/server.py:36 PASSTHROUGH_BYTES=90_000`、`:38 CLIENT_TTL=6.0`、`:440`(0.02)`:546`(0.05)`。
  对比：`config.py:69 last_timeout` 可配（15–60s），同类 MQTT/摄像头超时却不可配。
  建议参照 `ports.py` 再建一个 `app/bambu/timeouts.py`。
* **`time.sleep` 硬编码**：`headless.py:157`(0.3)`:182`(2.0)`、`printer.py:148`(0.2)`:239`(4.0)`、
  `probe.py:108`(0.2)、`selftest.py:31`(1.0)、`simulator.py` 若干。
* **监听地址**：`headless.py:41` 与 `web/server.py` 的 `--host` 默认 `0.0.0.0`（有意为之，
  但值得在文档里写明「会监听所有网卡」）。

### P1：平台与安全

| # | 问题 | 位置 |
| --- | --- | --- |
| 10 | Linux/Docker 无 DPAPI → 访问代码**明文**保存在 `config.json`（已加诊断提示，但存储方式未变）。若要真正加密，需引入用户口令或系统密钥环 | `app/util/secret.py:29-30`、`app/config.py:141` |
| 11 | 网卡枚举在非 Windows 上回退到「UDP connect 探测 + `getaddrinfo`」，**依赖外网连通**（探测 223.5.5.5/8.8.8.8/1.1.1.1）；纯内网环境可能枚举不到网段，导致搜索不全 | `app/bambu/discovery.py:245-275` |
| 12 | 无 Qt 环境（Docker 只装 `requirements-server.txt`）时 `_shrink_jpeg()` 永远返回 None → 大于 90KB 的帧**不缩放直接推原图**，手机流量与内存开销上升。可加 OpenCV 兜底缩放 | `app/web/server.py:41-63,138-141` |

### P2：测试与结构

| # | 问题 | 位置 |
| --- | --- | --- |
| 13 | 33 个 `tools/*check*.py` 是「内部 `ok` 布尔 + 打印 + 退出码」模式，**没有断言**，无法定位失败点，也无法按用例筛选 | `tools/` 全体 |
| 14 | 无退出码、进不了 CI：`tools/layout_check.py`、`tools/discovery_bench.py`、`tools/show_config.py`、`tools/dialog_smoke.py` | 同左 |
| 15 | 五个对话框（诊断/布局/设置/HMS/网页）无冒烟覆盖（`tools/dialog_smoke.py` 只构造添加与搜索两个） | `tools/dialog_smoke.py:18-19` |
| 16 | `app/web/page.py` 是近 500 行的单文件内嵌 HTML+CSS+JS，无前端构建、无语法检查、无测试 | `app/web/page.py` |
| 17 | `app/ui/tile.py:134-136` 的 `shutdown()` 只 `stop()` 解码线程不 join；`add_dialog.py:199`、`diagnose_dialog.py:200` 的 `QThread.wait()` 超时后未处理 | 同左 |

---

## 三、存疑（静态推断，需复现确认）

1. **`PrinterSession.restart()` 竞态**：`printer.py:restart()` 只 `sleep(0.2)` 就重新 `start()`，
   而上一轮的视频线程最长要 20 秒才退出。现在 `stop()` 会 join 线程，风险大减，
   但 `restart()` 的 0.2 秒等待仍不足以覆盖 RTSPS 场景，建议改成「等待 join 完成」而非定时。
2. **访问代码错误时的重试节奏**：`rtsp.py` 与 `camera.py` 在鉴权失败后都会退避重试，
   若用户在打印中改了访问代码，界面可能延迟较久才恢复。
3. **`_status_payload()` 的 `status_text` 取值**：`server.py` 里
   `(session.last_camera_detail or "连接中")[:14]` 会截断中文提示（14 个字符），
   有些文案（如「RTSPS(322) 未取到画面…」）被截得难以理解，建议改成固定短标签 + 悬浮详情。

---

## 四、环境层面的坑（不是代码问题，但会浪费半天）

1. **虚拟环境不可搬迁**：`.venv\pyvenv.cfg` 里写死基础解释器路径。本项目历史上从
   `D:\DSH\bambu-monitor` 搬到 `L:\DSH\bambu-monitor`，而 `D:\python` 又被删除，
   导致 `.venv\Scripts\python.exe` 存在但无法启动（退出码 103，报
   `did not find executable at 'D:\python\python.exe'`）。
   **`run.bat` 的自愈分支判断的是「python.exe 文件是否存在」，因此不会自动重建。**
2. 修好环境后建议跑一次 `test.bat`：它会先用 `python -c "import sys"` 验证解释器真的能启动，
   比 `if exist` 可靠。
3. `dist\BambuMonitor\` 是自包含的 PyInstaller 产物（约 240MB，241 项），
   即使源码环境坏了它也能跑 `--version` / `--core-test`，可作为对照基准。
4. 本项目**没有 CI**（无 `.github/`）。`test.bat` 与 `linux/run-tests.sh` 都返回标准退出码，
   接 CI 只需要一条命令。
