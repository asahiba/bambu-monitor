# 架构与代码地图

本文面向**要改这个项目的人**，回答「哪段代码负责什么、线程怎么跑、改哪里会不会牵动别处」。
协议细节看 [`PROTOCOL.md`](PROTOCOL.md)，部署看 [`DEPLOY.md`](DEPLOY.md)，
上手与测试流程看 [`DEVELOPING.md`](DEVELOPING.md)，已知问题清单看 [`KNOWN_ISSUES.md`](KNOWN_ISSUES.md)。

代码规模（基线 v1.0.0）：`app/` 39 个 py 文件约 9200 行，`tools/` 33 个脚本约 2800 行。

## 1. 分层

依赖方向是**单向**的：上层可以依赖下层，下层绝不反过来依赖上层。

```
┌──────────────────────────────────────────────────────────────┐
│ 入口层                                                       │
│   app/main.py            桌面版：命令行参数 → Qt 界面         │
│   app/__main__.py        python -m app                       │
│   app/headless.py        Linux/Docker 无界面：网页 + 管理命令  │
│   run_app.py             PyInstaller 打包入口                 │
├──────────────────────────────────────────────────────────────┤
│ 设备无关内核（不依赖任何厂商模块，也不依赖 Qt）                │
│   app/core/__init__.py       DeviceSession 协议（界面依赖的契约）│
│   app/core/capabilities.py   DeviceCapabilities：设备「能做什么」│
│   app/core/device.py         DeviceStatus：通用状态 + apply_mapped │
│                             + display_status()：界面用的统一只读视图│
│   app/core/adapter.py        PollingDeviceSession：轮询型适配器基类│
│   app/core/registry.py       设备族注册表 + create_session()（唯一入口）│
├──────────────────────────────────────────────────────────────┤
│ 界面层（只有它依赖 Qt Widgets）                               │
│   app/ui/main_window.py   监控墙、工具栏、布局、轮巡、配置     │
│   app/ui/tile.py          单路画面 + 状态条（进度/温度/耗材）  │
│   app/ui/video_widget.py  画面绘制、时间戳叠加、离线提示       │
│   app/ui/frame_decoder.py 后台解码线程（按显示尺寸解码）       │
│   app/ui/*_dialog.py      搜索/添加/布局/设置/诊断/HMS/网页    │
│   app/ui/theme.py         配色与全局样式                      │
├──────────────────────────────────────────────────────────────┤
│ 网页层（不依赖 Qt Widgets；转码时才延迟 import PySide6）      │
│   app/web/server.py    HTTP 服务、路由、令牌鉴权、控制接口     │
│   app/web/page.py      单文件前端（HTML+CSS+JS 内嵌）          │
│   app/web/icons.py     PWA 图标（base64，机器生成）            │
│   app/web/service_worker.py  PWA 离线缓存脚本                  │
├──────────────────────────────────────────────────────────────┤
│ 会话层                                                        │
│   app/bambu/printer.py  PrinterSession：遥测 + 视频聚合成一台  │
│   app/core/registry.py  设备族表 + create_session()（唯一入口）│
│   app/adapters/moonraker/  Klipper/Moonraker 族（HTTP + WS）   │
├──────────────────────────────────────────────────────────────┤
│ 协议层（不含 GUI，可单独测试/复用）                            │
│   app/bambu/ports.py       端口与共享常量（8883/6000/322 等）  │
│   app/bambu/discovery.py   SSDP + 2021 广播自动搜索、网卡枚举  │
│   app/bambu/mqtt_worker.py MQTT over TLS 8883 遥测与控制下发   │
│   app/bambu/camera.py      TCP 6000 帧流（TLS + 80 字节鉴权）  │
│   app/bambu/rtsp.py        RTSPS 322（OpenCV 拉流，可选依赖）  │
│   app/bambu/rtsp_h264.py   同上但**不解码**：纯标准库取 H.264  │
│                            交给网页端 WebCodecs（安卓用这条）  │
│   app/bambu/tlsutil.py     打印机 TLS 上下文选择与记忆化       │
│   app/bambu/probe.py       「测试连接」探测                    │
│   app/bambu/models.py      机型识别、状态数据模型、报文解析    │
│   app/bambu/hms.py         HMS 错误码文案表（含 gz 数据文件）  │
├──────────────────────────────────────────────────────────────┤
│ 基础设施层                                                    │
│   app/config.py        配置读写（DPAPI 加密访问代码、备份）    │
│   app/util/secret.py   Windows DPAPI 封装 + 诊断状态           │
│   app/util/__init__.py configure_stdio()：控制台 UTF-8 兜底    │
│   app/sim/simulator.py 虚拟打印机（UDP 响应 + MQTT broker + 视频）│
└──────────────────────────────────────────────────────────────┘
```

### 2.0 关于「按能力而不是按机型」

`app/core/capabilities.py` 的 `DeviceCapabilities` 解决一个具体的历史问题：
界面与网页里曾有三处**重复的机型分支**（`ui/tile.py`、`web/server.py`、`main.py`
各写了一遍 `info.model.has_chamber_sensor`），以及「只看遥测在线就显示灯按钮」
（导致开放机型 A1/A1 mini/A2L 得到一个按不动的灯按钮）。

现在的规则是：

* **要功能** → 问 `session.capabilities`（例如 `has_chamber_sensor`、`can_control_light`）；
* **要读数** → 读 `session.snapshot()`（例如 `chamber_temper`）；
* **不要**直接读 `session.info.model.*` 来判断显示什么。

`app/core/__init__.py` 里的 `DeviceSession` 是结构化协议（不是基类），
把「界面与网页实际依赖会话的哪些成员」写成可检查的契约，由
`tests/test_contracts.py` 与 `tests/test_capabilities.py` 双重锚定。
接入第三方设备族时，只要新适配器满足该协议、并按自己的能力表回答上述问题，
**界面与网页不需要改动**。

### 2.0.1 接入一个新设备族要动什么

`app/core/` 已经把差异收敛掉了，因此新增一个族（例如 Moonraker）只需要：

1. 写一个适配器。若设备是**轮询型**（第三方基本都是），继承
   `app/core/adapter.py::PollingDeviceSession` 并只实现三个方法：
   `_fetch_status()`（返回通用字段字典，键取自 `MAPPABLE_FIELDS`）、
   `_fetch_frame()`（返回 **JPEG** 字节，无摄像头则返回 None）、
   `_send_command()`。
   基类负责线程回收、限流、错误抑制与退避、增量合并 —— 这四件事都是本项目
   历史上真踩过的坑，因此不要在适配器里重写。
2. 在 `app/core/registry.py` 里 `register(FamilyDescriptor(...))`：
   声明族 id、展示名、凭据策略（`CredentialPolicy`）、端口与发现方式，
   并在该族的模块里提供 `create_session(info, **options)` 作为 ``session_factory``。
   **只登记已经能用的族** —— 提前登记会让界面出现一个选了也没用的选项
   （`tests/test_family_wiring.py` 会断言"登记了就必须有工厂"）。
3. 在配置文件里给该族设备写入 `family` 字段（以及需要时的 `port` / 凭据字段）。
   老配置没有该字段时一律解析为拓竹（`resolve_family`），因此**老用户不需要迁移**。

界面侧**不需要改动**，因为它拿到的两样东西都是族无关的：

* 会话 —— 全部经 `registry.create_session(info)` 创建（桌面版 / 网页版 / 命令行四处）；
* 状态 —— 全部经 `device.display_status(session.snapshot())` 转成界面认识的那套字段名
  （第三方族的 `job_state` / `progress_percent` 会被翻译成 `gcode_state` / `progress`…）。

仍然需要按族处理的差异只有三处，且都在适配器内部：
**状态机映射**（各家取值完全不同，映射到 `JOB_*`）、**剩余时间来源**
（有的有原生字段，有的要从文件元数据算）、**视频形态**
（MJPEG / 快照轮询 / RTSPS…）。

**关键约束**：协议层不得 `import` 任何 Qt。`app/web/server.py` 只在转码时**延迟
import** PySide6，因此无 Qt 的 Docker 也能跑（代价是转码失败，见 KNOWN_ISSUES）。
`app/bambu/rtsp.py` 只在需要时延迟 import OpenCV，所以 `opencv-python` 是可选的。
**没有 OpenCV 时 RTSPS 通道也不会退化成"没画面"**：改走 `app/bambu/rtsp_h264.py`
（纯标准库取流、不在本机解码），码流经 `/api/live` 的 `KIND_H264` 记录交给
网页端的 `VideoDecoder`。这是安卓版唯一的画面通路，详见 `KNOWN_ISSUES.md` §13。

## 2. 线程模型

这是本项目最容易踩坑的地方：**每台打印机都有若干常驻线程**，`stop()` 必须把它们收干净。

| 线程 | 位置 | 职责 | 生命周期 |
| --- | --- | --- | --- |
| `mqtt-<ip>` | `mqtt_worker.py:start()` | TLS 探测 + paho `loop_start()` 网络循环 | `MqttWorker.stop()` 置位并 join（5s） |
| `camera-<name>` | `camera.py`（继承 Thread） | 6000 端口收帧 + 退避重连 | `PrinterSession._stop_streams()` join（3s） |
| `rtsp-*` | `rtsp.py` | OpenCV 拉流 + 按目标尺寸编码 | 同上 |
| `rtsp-h264-*` | `rtsp_h264.py`（继承 Thread） | 纯标准库拉 RTSPS，**不解码**，把 AVCC 访问单元交给网页端（无 OpenCV 时走这条） | `PrinterSession._stop_streams()`（与上两条同一处理） |
| `video-setup-<ip>` | `printer.py:_spawn_video_thread()` | 建立视频通道（RTSPS 首帧最长等 20s） | `PrinterSession.stop()` join（2s，收在 `_video_threads`） |
| `video-switch-<ip>` | 同上（看门狗触发） | 换道重试 | 同上 |
| `video-watch-<ip>` | `printer.py:_video_watchdog()` | 每 15s 检查画面是否卡住、决定是否换道 | `_watchdog_stop` 事件 + join（2s） |
| `web-server` | `web/server.py:start()` | `ThreadingHTTPServer.serve_forever` | `WebServer.stop()` shutdown + join（2s） |
| `web-frame-cache` | `web/server.py`（继承 Thread） | 按需把 JPEG 缩到网页尺寸 | `WebServer.stop()` join（2s） |
| `decode-*` | `ui/frame_decoder.py` | 把 JPEG 解码到显示尺寸 | `CameraTile.shutdown()` |

界面**不通过信号传帧**：`PrinterSession.latest_frame()` 返回「序号 + 最新一帧」，
Qt 定时器按 `refresh_ms` 轮询，画面落后时旧帧直接丢弃。这样高频帧不会把事件队列压爆。

**锁的顺序**（避免死锁）：`_video_setup_lock` → `_stream_lock`。
`_stop_streams()` 只取 `_stream_lock`，可以安全地在持 `_video_setup_lock` 时调用；反过来不行。

## 3. 数据流

**遥测**：打印机 → `device/<序列号>/report` → `MqttWorker._on_message()` →
`PrinterSession._handle_report()` → `PrinterStatus.apply_report()`（**增量合并**，
因为 P1 系列只推送变化过的字段）→ 界面/web 轮询 `snapshot()`。

**视频**：打印机 → 6000 端口帧流 / RTSPS → 流对象的 `latest_frame()` →
（桌面）`FrameDecoder` 解码后贴到 `VideoWidget`；（网页）`WebFrameCache` 缩放后
由 `/api/live` 单连接多路复用推送。

**视频（没有 OpenCV 时，安卓走的就是这条）**：打印机 → RTSPS →
`RtspH264Client` 拆包成 AVCC 访问单元 → `PrinterSession.latest_h264()` →
`/api/live` 的 `KIND_H264` 记录 → 网页端 `VideoDecoder` 解码 → `<canvas>`。
这条通路**不在服务端解码**，因此不需要任何原生库。

**控制**：界面/网页/命令行 → `PrinterSession.pause_print()` 等 →
`MqttWorker.publish_command()` → `device/<序列号>/request`。
**遥测不在线时一律拒绝下发**（`PrinterSession.can_control`）。

## 4. 视频通道选择（最容易改错的地方）

机型能力写在 `PrinterModel.video_channel`，实测结论见 `PROTOCOL.md` 与 `models.py` 的注释：

| 机型 | 首选通道 |
| --- | --- |
| A1 / A1 mini / P1P / P1S | `tcp6000`（这些机型没有 RTSPS） |
| X2D / H2D / H2S / P2S | `rtsp`（这些机型的 6000 端口**拒绝正确口令**） |
| X1 / X1C / X1E / 未知机型 | `auto`：先 RTSPS，失败再退 6000（老固件只有 6000） |

`PrinterInfo.stream_mode`（`auto`/`tcp6000`/`rtsp`）是**用户级覆盖**，优先级最高，
模拟器就是靠它强制走 6000 端口。

`_video_watchdog()` 每 15 秒检查一次 `frame_count` 是否增长，约 60 秒无新帧时换道；
另外「6000 端口拒绝正确口令 + 遥测在线」也会触发换道。改这里的逻辑时请注意两条不变式：

1. `video_channel == "rtsp"` 的机型**绝不能**退回 6000（那边必然失败）；
   ⚠️ 这里曾经踩过：判据一度是「没有 OpenCV 就退 6000」，于是**安卓版永远没有画面**。
   现在没有 OpenCV 会转 `_start_h264()`（见第 3 节），首选通道不变（`KNOWN_ISSUES.md` §13）；
2. 换道必须经 `_spawn_video_thread()`，否则线程无法被 `stop()` 回收。

## 5. 配置与凭据

* 路径：`%APPDATA%\BambuMonitor\config.json`（`BAMBU_MONITOR_CONFIG_DIR` 可覆盖，
  Linux/Docker 下用这个变量指向 `./data`）。
* 访问代码用 **Windows DPAPI** 加密后以 `dpapi:` 前缀保存，只有同一 Windows 用户能解；
  非 Windows 平台没有 DPAPI，会退回明文并记录诊断（`secret.last_error()`），
  由 `AppConfig.last_error` 冒到界面提示。
* 每次保存前把上一份复制成 `config.backup.json`；主配置损坏时 `AppConfig.load()` 自动回退。
* 写盘失败**不再静默**：失败原因进 `AppConfig.last_error` 并记日志，
  界面在「保存配置」时弹警告框（`main_window.save_config_now()`）。

## 6. 网页服务

`app/web/server.py` 用标准库 `http.server`，路由如下（除 `/health` 外都要令牌）：

| 路由 | 说明 |
| --- | --- |
| `GET /` `/index.html` | 监控墙页面（`page.py` 内嵌前端） |
| `GET /health` | 存活检查，**不需要令牌** |
| `GET /api/printers` | 全部打印机的状态 JSON |
| `GET /api/live` | **单连接多路复用**：一条连接同时推所有画面与状态 |
| `GET /api/frame/<i>` | 单张快照 JPEG |
| `GET /stream/<i>` | 单路 MJPEG（兼容模式，受浏览器 6 连接限制） |
| `POST /api/command` | 控制指令 `{"index":0,"action":"pause"}` |
| `GET /manifest.webmanifest` `/sw.js` `/icon-*.png` | PWA 资源 |

多路复用协议：`BM`(2B 魔数) + 类型(1B：1=帧 2=状态) + 画面序号(2B LE) + 长度(4B LE) + 负载。
前端用 `fetch` 流式解析。**为什么要这么做**：浏览器对同域最多 6 条长连接，
每路 MJPEG 各占一条，第 7 路起就会一直排队。

### 令牌只有一个来源：`app.headless.resolved_web_token()`

`/` 本身也要令牌（见上表），所以**打开页面的那一个请求就必须带上令牌**。
令牌的解析集中在 `app.headless.resolved_web_token()`，它**先落盘再返回**。

这里的坑值得记住：`AppConfig.load()` 在**配置文件还不存在**时，每次调用都会
新生成一个 `web_token` 且**不落盘**，也就是说它并不幂等：

```python
AppConfig.load().web_token   # 'a1b2…'  第一次
AppConfig.load().web_token   # 'c3d4…'  第二次，又换了一个！
```

安卓版曾经自己去 `AppConfig.load()` 取令牌交给 WebView，而随后启动服务时
`run_headless()` 内部又 load() 了一次 —— 两边各拿一个，于是**首次安装打开
必然是 `{"error": "unauthorized"}`**（桌面版因为这个文件早就存在，看不出来）。

所以规则是：**任何宿主都不要自己 `load()` 取令牌**，一律调
`resolved_web_token()`；它同时被 `run_headless()` 使用，从根上保证宿主与
服务端用的是同一个值。回归测试见 `tests/test_android_bootstrap_token.py`。

## 7. 模拟器（无真机开发的基石）

`app/sim/simulator.py` 在 `127.0.0.2`、`127.0.0.3`… 上伪造完整打印机：

* UDP 响应器同时应答 SSDP `M-SEARCH` 与 2021 端口 `M99999` 广播；
* 自带极简 MQTT broker（TLS 8883，用户名 `bblp`）；
* 6000 端口 TLS 视频，帧内容用 Qt 绘制（无 Qt 时退回 OpenCV）；
* 自签证书缓存在 `.sim/`（CN 为序列号，与真机结构一致）。

因为每台虚拟机用不同的回环 IP，所以它们可以共用标准的 8883/6000 端口 ——
这也是能离线跑端到端测试（`tests/test_simulator_e2e.py`）的原因。

## 8. 改代码时的常见牵连

| 你改了什么 | 还要同步改什么 |
| --- | --- |
| 新增机型 | `PrinterModel` 枚举、`SERIAL_PREFIX_MODEL`、`MODEL_NAME_HINTS`、`video_channel`/`supports_rtsp`/`has_chamber_sensor`，以及 README 的机型表 |
| 新增状态字段 | `PrinterStatus` 字段 + `apply_report()` 解析 + `models.py` 的解析辅助函数；桌面 `tile.py` 与网页 `server.py:_status_payload()` 两处展示 |
| 新增控制指令 | `PrinterSession` 方法、`MqttWorker.publish_command()` 调用、`web/server.py:_control()` 分支、`headless.py --control` 选项、前端按钮与二次确认 |
| 改网页协议 | `server.py` 与 `page.py` 必须同时改（前端解析在 `page.py` 里） |
| 新增配置项 | `AppConfig` 字段 + `_parse()` 钳制 + `to_json()`；界面在 `settings_dialog.py` 暴露 |
| 改线程生命周期 | 上面第 2 节的表 + `KNOWN_ISSUES.md` 里「线程回收」相关条目 |
