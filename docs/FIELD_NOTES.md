# 第三方与拓竹新机型的实测情报

本文件记录**从真实网络/真机观测到的事实**，与社区资料区分开。
凡是「未实测」的内容都会明确标注，避免后来者把推测当成结论。

---

## 1. 拓竹 A2L（2026 年实测发现）

**来源**：在开发机上运行 `python -m app --core-test`（SSDP 组播 + 2021 端口广播双通道搜索）时，
从真实局域网中搜到，非模拟器。

| 项 | 观测值 |
| --- | --- |
| IP | `192.168.2.243` |
| `devname` | `A2L` |
| `devmodel` | **空**（因此 `detect_model()` 只能靠 devname 里的 `A2L` 字符串与序列号前缀） |
| 序列号（USN / `dev_id`） | `26A00A000000000000`（18 位） |
| 固件版本（`devversion`） | `01.01.00.00` |
| 机型识别结果 | 「未知机型」——`A2L` 关键字与前缀 `26A` 都不在表中 |

`devversion` 是 `01.01.00.00`，属于**很早期的固件**（对照：P1S 已到 `01.10.00.00`、
X1C 到 `01.11.02.00`），说明 A2L 是较新的产品线，其固件行为不能照搬老机型的实测结论。

**已落地到代码**：`PrinterModel.A2L` 已加入枚举（`video_channel="tcp6000"`、
`supports_rtsp=False`、`has_chamber_sensor=False`），前缀 `26A` 与关键字 `a2l` 已加入识别表。
其中视频通道与 RTSPS 能力是**基于实测端口**得出的，可信；
``has_chamber_sensor=False`` 是**保守假设**（未实测），
若日后确认 A2L 有腔温传感器，把 ``A2L`` 加进 ``has_chamber_sensor`` 的集合即可。

**两点值得注意**：

1. 拓竹确实已有**未收录前缀的新机型**在真实网络中运行，需要补表。
2. **序列号长度不固定**：`app/bambu/discovery.py::_looks_like_bambu()` 用
   「15 位大写字母数字」判定陌生设备，但这台是 18 位。它之所以仍被发现，是因为
   SSDP 回包带了 `devmodel`/`devname` 头命中了更靠前的判定分支。
   若将来遇到只带 `ST` 头、不带 `dev*` 头的同类新机型，**会被漏判**。
   扩大机型支持时必须重新审视这条启发式规则。

**本轮实测结论（只做了 TCP 连通性与 TLS 握手，未发送任何鉴权包与控制指令）**：

| 端口 | 结果 | 说明 |
| --- | --- | --- |
| 8883 | 开放 ✓ | TLS 1.2 |
| 6000 | 开放 ✓ | TLS 1.2 |
| 322 | **不可达 ✗** | 该机型不提供 RTSPS（或未开启 Liveview） |
| 80 / 8080 / 7125 | 不可达 ✗ | 没有 HTTP 侧接口 |

证书信息（8883 与 6000 返回**同一张**证书）：

* CN = `26A00A000000000000`（即序列号，与老机型一致）
* 颁发者 = `CN=BBL Device CA N9, O=BBL Technologies Co. Ltd, C=CN`
* 有效期至 2036-05-09

**关键：内置 CA 能校验通过。** 项目内置的 `app/bambu/certs/ca_cert.pem` 里是
`BBL CA` / `BBL CA2 RSA` / `BBL CA2 ECC` 这套根与中间证书；A2L 虽然多了一层
`BBL Device CA N9` 中间证书，但整条链仍能回到 `BBL CA`。实测
`tlsutil.connect_tls(ip, 8883/6000, verify_chain=True)` 返回 `verified=True`。
→ **不需要为 A2L 改证书逻辑**，也不会出现 X2D 那种「每次连接先试两个无效 TLS 参数」的额外延迟。

**对机型配置的含义（重要）**：

A2L 的特征（8883 + 6000 可用、322 不可用）与 **A1 / A1 mini / P1P / P1S 完全一致**，
因此它应当被归入 `video_channel == "tcp6000"` 那一类，而不是 H2/X2D 的 `"rtsp"` 类。

代码层面的现状是安全的：由于 A2L 的序列号前缀 `26A` 未收录，`detect_model()` 返回
`PrinterModel.UNKNOWN`，而未知机型的 `video_channel` 是 `"auto"`（先试 RTSPS、失败再退 6000），
所以画面**能出**，只是首次连接会白等一次 RTSPS 超时。补上前缀即可消除这次浪费。

**仍未实测**（需要正确的局域网访问代码，属于只读遥测、无风险）：

- `8883` 上报的字段集合：是否仍是 `mc_percent` / `nozzle_temper` / `ams` / `hms` 这套；
  有无新字段（尤其腔温、双喷嘴）。
- `6000` 端口是否仍接受 80 字节鉴权包、帧头是否仍是「16 字节头 + JPEG」。
  **这是「新机型解码方式是否不同」的关键验证点** —— 目前只能确认 TLS 层一致。
- 是否有 AMS、是否多喷嘴、是否有腔温传感器。

---

## 1.1 实测（二）：拿到访问码后的遥测读数

用访问码连上 8883 后读到的状态（**只读遥测，未下发任何控制指令**）：

| 字段 | 读数 | 解读 |
| --- | --- | --- |
| `gcode_state` | FINISH | 机器空闲，上一个任务已完成 |
| `mc_percent` / 层数 | 100% / 1625 层 | — |
| `subtask_name` | `1557-worker-and-kolkhoz-woman.stl` | — |
| 喷嘴 / 热床 | 25.1℃ / 24.6℃（目标均为 0） | 冷却状态 |
| **`chamber_temper`** | **5.0℃** | ⚠️ 与 P1S 报的无效值**完全一样**（`models.py` 注释里写着「实测 P1S 常年是 5.0℃」）→ 提示 A2L **可能没有腔温探头**。保留 `has_chamber_sensor=False`（不显示比显示假值安全，且 5.0℃ 这种读数本身就不可信） |
| AMS | 4 个槽位、`ams_exist=True` | 挂了一台 AMS，有 4 个槽位 |
| 外挂料盘 | 无 | — |
| HMS | 0 条 | — |
| WiFi | `-55dBm` | 与老机型同样的字符串格式 |
| `ipcam.rtsp_url` | **空** | 再次印证该机型不提供 RTSPS |

**结论：遥测字段与老机型完全同构**（`mc_percent` / `nozzle_temper` / `ams` / `hms` / `wifi_signal`
都是老名字、老格式），**没有出现需要新解析逻辑的字段**。

## 1.2 实测（三）：6000 端口一度拒绝连接的原因

拿到访问码后，第一次探测 6000 端口能完成 TLS 握手，但发送 80 字节鉴权包后**收不到任何帧**；
之后该端口直接变成 `ConnectionRefused`（连续 6 次、每次间隔 3 秒都是拒绝），
而 `8883` 始终正常、`ping` 也通。

**原因已确认：这台 A2L 当时正在更新固件**（用户告知）。

这解释了全部现象，并且给出两个有用的结论：

1. **固件更新期间摄像头服务会停止，但 MQTT 遥测可能仍然在线** —— 所以「遥测在线但画面不出来」
   不一定是我们代码的问题，也可能是设备正在升级。诊断流程与界面文案值得考虑这种情形。
2. 端口连通性会**随设备状态变化**，因此 `probe` 阶段的端口探测结果只是当时快照，
   不能作为「该机型永久不支持某通道」的唯一依据。

⚠️ **固件更新完成后必须重测**：固件版本、端口状态、以及最关键的那条 ——
**6000 端口发完 80 字节鉴权包后到底回什么**（正常 JPEG 帧？还是像 X1/X2D 那样的拒绝包？）。
这条决定了 A2L 是否需要新的解码路径，也是用户提出的「新机型解码方式是否不同」的直接答案。

---

## 1.3 调研结论：新老机型的真实分界线

一轮完整的联网调研（官方 Wiki + 官方源码 + 社区实测交叉核对）给出了三个对本项目
直接决定实现方式的结论。**这些是调研结论而非本机实测**，可信度单独标注。

### ✅ 结论一：新机型**没有**换解码方式

全仓库搜索确认**没有 H.265 / HEVC、没有 WebRTC、没有 AV1**。拓竹至今只有两套视频：

| 通道 | 编码 | 机型 |
| --- | --- | --- |
| TCP 6000 | **独立 JPEG 帧**（`FF D8 FF E0`…`FF D9`，16 字节小端头） | A1、A1 mini、**A2L**、P1P、P1S |
| RTSPS 322 | **H.264**（`yuv420p`，1920×1080） | X1、X1C、X1E、X2D、P2S、**H2C**、H2D、H2D Pro、H2S |

→ 我方的 JPEG 解码链路（`camera.py` + `ui/frame_decoder.py`）**不需要改动**。
用户提出的「新机型解码方式似乎不一样」，实际答案是：**分界线不在编码，而在下面这条**。

### ⚠️ 结论二：真正的分界线是 MQTT 的 Developer Mode（比编码重要得多）

* 打印机 MQTT 报文里的 `fun` 字段，**bit `0x20000000`** 表示「MQTT 命令需要签名校验」。
* 未在打印机触屏上开启 **Developer Mode** 时，**第三方下发的控制命令会被静默忽略**
  （机器回一条 HMS `0500-0500-0001-0007`，界面看起来就是「点了没反应」）。
* 新机型（H2C / H2S / X2D / P2S / A2L）默认就需要它。

**因此「暂停/停止/开灯」在新机型上可能无声失败，而画面与遥测一切正常。**
这解释了「监控正常但控制没用」这类投诉。

#### 放行方式：必须**先局域网模式、再开发者模式**（顺序不能反）

第三方客户端（含本项目）只走**局域网**接口，所以：

| 顺序 | 方式 | 位置 | 说明 |
| --- | --- | --- | --- |
| 1 | **局域网模式** | 设置 → 网络 → 局域网模式 / LAN Mode Only | **必须先开这一项**；单独开它**不能**放行 `print` 段命令 |
| 2 | **开发者模式** | 设置 → 通用 → 开发者模式 / Developer Mode | 开完它 `print` 段命令才真正放行（两步都要做） |
| ★ | **农场管家（推荐）** | 官方 Farm Manager 接管设备 | 签名密钥只给官方应用，**不需要开发者模式**，最省事 |

> ⚠️ **只开「局域网模式」是没有用的**（实机结论，2026-09 用户 12 台设备验证）：
> 画面与遥测会一切正常，但 `print/pause`、`print/stop` 依旧被固件静默忽略 ——
> 现象就是"按钮点了没反应"。必须**先局域网模式、再开发者模式**，两步都开完
> （建议重启设备）。
> 本文件早先写成"任选一条放行即可"，把用户引到了没有用的那条路上。

> ⚠️ **只在切片软件里登录拓竹账号是没用的。** 那只影响云端；
> 第三方客户端走局域网接口，必须在打印机本机放行。
> 实测确认：登录账号 + 未开局域网模式时，控制命令依然被忽略。

**签名只覆盖 `print` 段命令**（`pause` / `resume` / `stop` / `print_speed`），
**灯控走 `system` 段，不受影响** —— 所以「暂停不能用但灯能开关」是正常现象，
不是软件坏了（**此时能控制的只有灯**）。界面提示必须按这个范围如实描述
（曾经笼统写成「控制不可用」，用户看到后去点灯发现能用，反而更困惑）。

已落入代码：`PrinterSession.command_blocked()` 按命令判定；
`controls_blocked_reason`（完整步骤）与 `controls_blocked_short`（一句话版）
给出顺序、农场管家与「只能控灯」的说明；桌面端 `tile.py`、网页端 `page.py`、
命令行 `app.headless --control` 三处都按命令分别处理并显示提示
（桌面端被挡的按钮**保持可点**，点下去弹完整说明 —— Qt 的禁用控件不接收鼠标事件，
tooltip 根本不会显示）。

### 📋 结论三：机型表有**官方**来源，且换主板会改序列号

官方 Wiki <https://wiki.bambulab.com/en/general/find-sn> 逐机型列出序列号前 3 位，
不再是社区逆向。**同页官方警告：更换 AP 板/主板后，机器实际序列号会与机身贴纸不同。**

→ 识别判据的优先级必须是「**完整型号名 → devmodel 代号 → 序列号前缀**」，
不能只靠序列号。已据此调整 `detect_model()`。

已落入代码的表（`models.py`）：前缀 `31B`=H2C、`239`=H2D Pro；
代号 `C11`=P1P、`C12`=P1S、`N9`=A2L、`O1C`/`O1C2`=H2C、`O1E`/`O2D`=H2D Pro。

补充事实：

* **X1P 不存在**（五个独立来源都没有），不要为它留机型槽。
* **X1 与 P1P 已于 2025-11 停产下架**，但在网设备仍需识别。
* **H2C**：官方 2025-11-18 发布，**双喷嘴 + 6 位喷嘴架**（Vortek，架位 ID 16–21），
  有腔温传感器与有源腔温加热器（≤65℃）。⚠️ 喷嘴 1 在架侧，**不遵循 0=右的惯例**。
* **A2L**：官方 2026-06-01 发布，**单喷嘴、无腔温传感器**、摄像头是低帧率 1080P，
  实测 5.0℃ 的腔温读数因此确认是无意义值。
* **AMS 前缀**：`006`=AMS、`03C`=AMS lite、`19C`=AMS 2 Pro、`19F`=AMS HT。
* **H2 系 RTSPS 默认关闭**（`ipcam.rtsp_url == "disable"`），需用户在触屏开
  「局域网实时画面」，否则纯局域网拿不到流 —— 与现有看门狗告警文案一致。
* **任务 URL 格式分两派**：A2L/P2S/H2C/H2D/H2D Pro/H2S/X2D 用 `ftp:///`；
  老机型用 `file:///sdcard/`。⚠️ 这里 A2L 与 A1 属于不同阵营。

**未找到可靠来源、待真机确认**：A2L 的 `devmodel` 是否为 `N9`（仅源码，未见官方；
本机 A2L 的 devmodel 当时读到为空，需固件更新后再抓一次 SSDP 原文确认）；
H2D Pro 的代号到底是 `O1E` 还是 `O2D`（来源不一致，两个都收）。

---

## 1.4 实测（四）：固件更新后重测 —— 两个关键问题当场定论

A2L 从 `01.01.00.00` 更新到 **`01.01.05.00`** 后端口恢复（8883 与 6000 都开放），
随即完成了两项关键实测。**结论直接回答了用户提出的「新机型解码方式是否不同」。**

### ✅ 解码方式**没有**变化（实测确认，不再是调研推断）

用项目自己的 TLS 工具连接 6000 端口、发送标准 80 字节鉴权包后，连续收到 3 帧：

| 帧 | size | itrack | flags | 负载 | JPEG |
| --- | --- | --- | --- | --- | --- |
| 1 | 60964 | 0 | 1 | 60964 | ✓ `ffd8ffe0` |
| 2 | 163546 | 0 | 1 | 163546 | ✓ `ffd8ffe0` |
| 3 | 176512 | 0 | 1 | 176512 | ✓ `ffd8ffe0` |

即 **「16 字节小端帧头 + JPEG」老协议逐字段一致**（`itrack=0`、`flags=1`）。
→ **我方 JPEG 解码链路无需任何改动**，`camera.py` 与 `frame_decoder.py` 可原样用于 A2L。
→ 之前那次「发完鉴权包收不到帧」纯粹是固件更新期间的临时状态。

### ⚠️ 但 `fun` 字段是**十六进制字符串**，且该位确已置位

A2L 的 MQTT 报文里：

```json
{"print": {"fun": "100d122002fbd", ...}}      ← 注意是字符串，不是数字
```

实测 `fun = 0x000100D122002FBD`，**bit `0x20000000` 已置位** → 该机要求 MQTT 命令签名，
即**未在触屏开启 Developer Mode 时，暂停/停止/开灯会被固件静默忽略**。

⚠️ 调研资料把 `fun` 当成整数处理，**实际是十六进制字符串**；用 `int()` 解析会失败并
退化成 0，从而**漏掉这个关键信息**。已为此单独实现 `_as_hex_int()`（容忍 `0x` 前缀、
大小写、以及少数固件直接给整数），并新增 `PrinterStatus.needs_mqtt_signature` /
`developer_mode` 两个属性（**无法解析时返回 `None` 而不是 `False`**，
以免把没有该字段的老机型误判成需要开发者模式而禁用控制按钮）。

### 顺带确认

* 报文 `print` 段共 **86 个字段**，字段名与老机型完全同名（`mc_percent`、`nozzle_temper`、
  `ams`、`hms`、`chamber_temper`、`ipcam`…），**没有出现需要新解析逻辑的字段**。
* 顶层仍然只有 `print` 一个键。

### 仍待确认

* MQTT 里的 `devmodel`/`product_name` 字段（用于回答「A2L 的代号到底是不是 N9」）。
* 320 端口不存在（已确认 322 不可达），故 A2L 固定走 6000 —— 与代码中的声明一致。

---

## 1.5 实测（五）：A2L **有舱灯** —— 纠正一次「按规格推断」的错误

**背景**：A2L 是开放式机型（open-frame Cartesian），我据此在代码里把它归入
「无舱灯」（与 A1 / A1 mini 同列），于是桌面界面的灯按钮被隐藏，用户无法开关灯。

**实测结果（2026-09，固件 01.01.05.00）**：A2L 明确上报

```json
"lights_report": [{ "node": "chamber_light", "mode": "off" }]
```

并且用与 `PrinterSession.set_light()` **完全相同**的官方 MQTT 报文
（`system/ledctrl` + `led_node=chamber_light`）实测：

| 操作 | 下发 | 回读 `lights_report[0].mode` | 结果 |
| --- | --- | --- | --- |
| 开灯 | `led_mode=on` | `on` | ✓ 成功 |
| 关灯 | `led_mode=off` | `off` | ✓ 成功 |

**结论与修正**：

1. A2L 有舱灯，且协议与老机型一致 —— 控制报文不需要任何改动，
   问题**纯粹出在界面的能力判定上**。
2. **机型规格推不出灯光能力。** 已做两处修正：
   * `PrinterModel.A2L` 加入 `has_enclosure_light`；
   * 更根本的：`PrinterSession.capabilities` 与界面改为**以设备实际上报为准** ——
     只要 `lights_report` 里有节点就允许控制，不再依赖机型猜测。
     这样即使将来又出现"规格说没有、实际有"的机型，也不会再把按钮藏掉。
3. 同类教训值得记牢：本项目已经因为"按规格推断"错过两次
   （另一次是把 A2L 归入 `has_chamber_sensor=False`，虽然腔温 5.0℃ 确实可疑，
   但那是**读数可疑**，不是**功能不存在**）。**能实测就不要推断。**

---

## 1.6 实测（六）：签名要求**只挡住 `print` 段命令**，灯控不受影响

**背景**：上一条修正之后，我又把「固件要求命令签名」当成全局门槛 ——
`can_control` 在 `fun` bit 0x20000000 置位时一律返回 False，
结果**本来能用的灯控被锁死**，网页端点开关灯报「遥测未连接，无法下发指令」
（提示还是误导的：遥测明明连着）。用户反馈「A2L 先前在网页上开关灯会报错」正是指这个。

**逐命令实测（A2L，固件 01.01.05.00，`needs_mqtt_signature=True`）**：

| 命令 | MQTT 段 | 下发后回读 | 结论 |
| --- | --- | --- | --- |
| `ledctrl`（开关灯） | `system` | `lights_report` 真的在 on/off 之间变化 | **生效** ✓ |
| `print_speed`（速度档位） | `print` | `spd_lvl` 始终不变 | 被设备忽略 ✗ |
| `pause`（暂停） | `print` | `gcode_state` 始终不变 | 被设备忽略 ✗ |

**官方依据**：MQTT 签名机制的覆盖范围是「**顶层带 `print` 的报文**」
（"Firmware rejects unsigned `print` commands when Developer Mode is off"），
`system` / `info` 段不在其中。

**修正**：门禁改为**按命令区分** —— 新增 `PrinterSession.command_blocked(command)`：

* `pause` / `resume` / `stop` / `speed` → 签名要求未满足时明确拒绝并说明原因；
* `light` → **不受影响**，照常下发；
* `can_control` 回归「遥测是否在线」的原义（否则界面无法表达"灯能用、暂停不能用"）。

**最终验证**（通过网页 API 走完整 HTTP 路径，持续轮询回读）：
开灯 → 状态变 `on` ✓；关灯 → 状态变 `off` ✓；
暂停 → 返回明确原因（提示开启开发者模式，并说明开关灯不受影响）✓。

**给用户的结论**：A2L 的开关灯**一直可用**，不需要开开发者模式；
需要开的是**暂停 / 停止 / 速度**这些 `print` 段功能。


---

## 2. 第三方设备：Snapmaker U1（调研结论）

> **接入状态（v1.1.0）**：Moonraker 族**已经接进产品**了 —— 界面（桌面 + 网页/安卓）
> 与命令行都能添加并监控，凭据字段叫 API Key、端口默认 7125（U1 上 80 也通）。
> 详见 `CHANGELOG.md` 的 1.1.0 与 `docs/ARCHITECTURE.md` §2.0.1。
> ⚠️ **只用假服务器（`app/adapters/moonraker/fake.py`）验证过，没有真实 U1/Voron 真机**。

**U1 = 改版 Klipper + 改版 Moonraker，原厂固件即可集成，无需改装。**
协议是标准 Moonraker HTTP + WebSocket JSON-RPC（JSON-RPC 2.0）。
**不需要 aiortc、不需要 ffmpeg、不需要 MQTT 证书** —— 接入难度显著低于拓竹。

| 维度 | 结论 |
| --- | --- |
| 发现 | **主通道是 mDNS `_snapmaker._tcp.local.`**（默认启用）。⚠️ **原厂固件默认不开 SSDP**（`enable_ssdp` 默认 False），照搬拓竹的 SSDP 扫描会一台都发现不到。手动填 IP 是必备兜底 |
| 端口 | ⚠️ mDNS SRV 里的 **1884 是内部 MQTT 端口，不是 API 端口**；API 是 **80**（nginx 反代），回退 **7125** |
| 遥测 | `ws://<ip>/websocket` → `printer.objects.subscribe`，收 `notify_status_update`。对象：`print_stats`、`virtual_sdcard`、`extruder`(+`extruder1/2/3`)、`heater_bed`、`toolhead`、`display_status`、`webhooks` |
| 控制 | `POST /printer/print/pause` `/resume` `/cancel`；灯光用 G-code `SET_LED LED=cavity_led WHITE=1\|0` |
| 视频 | ⚠️ **只有 1 个腔体摄像头，原厂固件没有 RTSP / WebRTC / 标准 MJPEG**。正确做法是 **WS 保活 + JPEG 快照轮询**：每 2–10 秒发 `camera.start_monitor`（**只能走 WebSocket**，HTTP 发不通），再 GET `/server/files/camera/monitor.jpg`。保活一断画面就静止，**必须做看门狗** |
| 鉴权 | 内网默认免密钥（`trusted_clients` 含 `192.0.0.0/8` 等）；**非标准网段会 401**，因此必须支持填 API Key（`X-Api-Key` 头 / WS 用 `?token=`） |
| 机型识别 | mDNS TXT 里的 `machine_type == "Snapmaker U1"` 是唯一可靠字段 |
| 开源属性 | ⚠️ **不是开源硬件**（OSHWA 无 Snapmaker，无原理图）。Klipper/Moonraker/Fluidd 的 GPL 衍生部分官方已开源，触屏 UI 与摄像头守护进程 `unisrv` 闭源。准确说法是「跑在 Klipper/Moonraker 开源生态上的商业产品」 |

**WebRTC 不建议投入**：它只存在于社区扩展固件且信令未公开；同一扩展固件下
RTSP `rtsp://<ip>:8554/stream` 能拿到同样画质且成本≈0。检测到扩展固件时优先用 RTSP。

### 更大的收获：按「协议生态」抽象，而不是按品牌

调研确认存在**两个高覆盖统一接入层**，一处适配就能吃下一大批机器：

| 接入层 | 覆盖 | 发现 | 遥测 | 视频 |
| --- | --- | --- | --- | --- |
| **Moonraker** | 所有 Klipper 机器（Voron、RatRig、刷 Klipper 的 Creality/Elegoo/Anycubic、U1…） | mDNS `_moonraker._tcp` ⚠️**默认不开** | HTTP/WS 7125 | MJPEG / 快照 |
| **OctoPrint** | 所有 OctoPrint 机器 | mDNS `_octoprint._tcp` | REST + `X-Api-Key` | MJPEG |

Voron 与 RatOS **不需要任何专用代码** —— 它们官方就是 Klipper + Moonraker。

⚠️ **PrusaLink 会同时注册 `_octoprint._tcp`**（官方为兼容旧切片器），但 API 不是
OctoPrint 那套，需要单独适配；而且 **PrusaLink 只有静态快照、没有视频流**，
在一面「实时画面墙」上只能低频刷新。Creality 主通道是私有 JSON WebSocket（TCP 9999），
属于逆向协议，优先级最低。

---

## 2.1 ⚠️ 接入第三方设备时必须遵守的三条安全/架构约束

这三条来自对各家**官方源码**的核对，违反其中任何一条都会造成用户可见的严重故障。
在动手写任何第三方适配器之前请先读这一节。

### 🚨 红线一：绝不对逆向协议做「命令扫描」或盲试命令码

**Elegoo Centauri Carbon（CC1）收到未识别的 Cmd 码会崩掉整个 `app` 守护进程。**
该进程同时承担 HTTP UI + SDCP + 摄像头 + **内嵌 Klipper 运动栈**，
因此**正在打印的任务会一起死掉**，只能墙断电重启。

→ 规则：对**逆向出来的**协议（Elegoo SDCP、Creality WS 9999、Anycubic、FlashForge 等）
只允许使用**有来源、被实证过**的确定命令；禁止「遍历命令码探测设备能力」这类做法。
`tools/` 里的诊断脚本也要遵守这一条 —— 拓竹的诊断流程不能照搬给第三方。

### 🚨 红线二：视频必须「单上游 + 服务端扇出」，不能让每个画面各自直连打印机

* Elegoo CC1 的摄像头 HTTP 服务**槽位会泄漏**（客户端断开后滞留 FIN-WAIT-2 占槽，
  耗尽后新连接收不到帧），且**只允许 5 个并发 WebSocket**（第 6 个返回 HTTP 500）。
* Elegoo CC2 通常**只允许 1 路**视频连接。
* Anycubic 必须先发 MQTT `startCapture` 才有流，而且**启动采集会强制点亮腔体 LED 且无法覆盖**。

→ 规则：无论多少路画面、多少个网页客户端，对同一台设备**只保持一条上游视频连接**，
在服务端缓存最新帧再扇出（现有 `WebFrameCache` 已经是这个模型的雏形，
`web/server.py` 的多路复用也只是把它推给多个客户端，方向是对的）。

### ⚠️ 约束三：发现层必须多路并行，不能只靠 mDNS

mDNS 的覆盖率比预期低：

* Moonraker 的 `[zeroconf]` 是**可选组件段，默认不开启**；
* Creality **完全没有**专属 mDNS 服务类型（且 K2 的 hostname 官方实现里就不能 mDNS 解析）；
* Elegoo 官方 SDK README 声称支持 mDNS，但把 `src/lan/` 全量源码 grep
  `mdns|avahi|bonjour|zeroconf` **零命中** —— 是文档措辞错误。

→ 规则：发现层要做成「**mDNS 多服务类型 ∥ 固定端口探测（7125/9999/8898 等）
∥ UDP 广播（3000 / 52700 / 19000+48899 等）∥ 手工录入**」四路并行，
并且**手工录入必须是永远可用的兜底**（跨 VLAN、禁多播的环境下它是唯一出路）。

### 与 U1 直接相关的两条校正（来自真机探测记录）

* **`/printer/emergency_stop` 与整个 `/printer/control/*` 系列是 WebSocket-only**
  （官方源码里它们都带 `transports = all & ~HTTP`）。**HTTP-only 的客户端无法急停。**
  因此接 Moonraker 系设备**必须常驻一条 WebSocket 连接**（遥测本来也需要），
  急停与灯光/风扇/温度/速度都走它。`print/pause|resume|cancel|start` 与 `restart`
  没有该限制，HTTP 可用。
* **WS 层调灯的参数名是 `name`**（不是 `led`）：`{"name":"cavity_led","white":1}`；
  且该层用整数取色 → **WS 只能 0/1 开关，调光必须走 G-code `SET_LED`**。
* ⚠️ **U1 的 `output_pin` 是 `e0_heat_sw`…`e3_heat_sw`（加热使能线），绝不要 `SET_PIN`。**
* ⚠️ **不要套用 Snapmaker Luban 的发现协议**（UDP 20054 / `_printer._tcp` / SACP 8889）——
  那是 2.0/Artisan/J1 世代，与 U1 无关，套上去既发现不到 U1 还可能误判老机型。

### 各家状态机取值完全不同（归一化层必须逐族映射）

Moonraker 是字符串（`standby/printing/paused/complete/error/cancelled`）、
Creality 是 `0..5`、Elegoo CC1 是 `0..22`、FlashForge 是字符串、PrusaLink 是大写字符串。
**通用状态模型的 `job_state` 必须由各适配器负责映射**，不能假设各家一致。

### 两个功能缺口（影响 UI 该显示什么）

* Moonraker 与 OctoPrint **都没有统一的「开灯」端点**（前者要枚举 `gcode/help`/`configfile`，
  后者靠插件）；U1 要用 `SET_LED LED=cavity_led WHITE=1`。
* **OctoPrint 的 `/api/job` 没有层数字段**；Moonraker 的层数需要切片器写
  `SET_PRINT_STATS_INFO`。若画面墙要显示层数，这是选型时的真实缺口。
* Moonraker 的剩余时间有正经来源：`GET /server/files/metadata?filename=<path>` 返回
  `estimated_time` / `layer_count` / `filament_weight_total`，比按进度自算准。

---

## 2.2 Voron 2.4 真机实测（2026-09-18，Moonraker v0.11.0）

设备：`192.168.31.55:7125`，hostname `voron24`，Klipper（`/server/info` **不上报**
`klipper_version`，只有 `moonraker_version` 与 `klippy_state`），webhooks 状态 `ready`。
扫描方式：`tools/moonraker_find.py`（按端口扫网段，只读）。

### ✅ 摄像头：URL 是**相对路径**，且挂在主机 80 端口上（不是 Moonraker 端口）

`/server/webcams/list` 返回：

```json
{"name": "Printer", "service": "mjpegstreamer", "location": "printer",
 "stream_url": "/webcam/?action=stream", "snapshot_url": "/webcam/?action=snapshot",
 "target_fps": 30, "target_fps_idle": 15, "enabled": true}
```

两个地址都实测过：

| 地址 | 结果 |
| --- | --- |
| `http://192.168.31.55/webcam/?action=snapshot` | **200 image/jpeg 约 28 KB** ← 能出画面 |
| `http://192.168.31.55:7125/webcam/?action=snapshot` | **404 application/json** |
| `http://192.168.31.55/webcam/?action=stream` | 200 multipart/x-mixed-replace（MJPEG 长连接） |

结论：`/webcam/` 由 nginx 代理到 crowsnest 的 ustreamer，**在主机 80 端口**；
Moonraker 自己的端口后面没有这个路径。程序原来把相对地址拼在 Moonraker 端口上，
于是「遥测正常、端口也通，但永远没有画面」。现在按「主机根优先、Moonraker 端口兜底」
解析并记住成功的那一个（`app/adapters/moonraker/adapter.py` 的 `_candidate_bases`）。

### ⚠️ 多路画面：设备端配了几路，未必都在跑

| 路径 | 结果 |
| --- | --- |
| `/webcam/` | 200，正常出图 |
| `/webcam2/` `/webcam3/` `/webcam4/` | **502**（nginx 有这个 location，但上游 ustreamer 不在） |
| `/webcam5/` | 200 text/html（前端 SPA 的兜底页，不是摄像头） |

也就是说 crowsnest 里可能配了 2–4 路摄像头，但只有第一路在跑；502 那几路在界面上
应该显示成「不可用」，而不是静默消失。Moonraker 的 webcams 数据库里只登记了 1 路。

### 📋 设备能力（`/printer/objects/list`，共 60 个）

值得用起来的对象（程序当前只查了 8 个）：

| 对象 | 能拿到什么 |
| --- | --- |
| `print_stats` | `filename`、`state`、`message`、`total_duration`、`print_duration`（秒）、**`filament_used`（mm）**、`info`（层数，需切片器写 `SET_PRINT_STATS_INFO`） |
| `virtual_sdcard` | `progress`（0–1）、`is_active`、`file_path`、**`file_position` / `file_size`**（字节级进度） |
| `display_status` | `message`（M117 屏幕提示） |
| `toolhead` | `homed_axes`（`xyz`）、`axis_minimum/maximum`（**300×300×270**）、`position`、`print_time`、`stalls` |
| `gcode_move` | `speed_factor`、`extrude_factor`、`speed`、`homing_origin` |
| `fan` / `heater_fan *` | 风扇转速（`speed`）与是否在转 |
| `temperature_sensor EBBCan` | 工具头板温度 |
| `filament_switch_sensor 断料监测` | 断料开关状态 |
| `filament_motion_sensor 转堵监测` | 堵料检测（`filament_detected`） |
| `system_stats` | 主机 CPU / 内存 / 负载 |
| `mcu` / `canbus_stats *` | MCU 版本、负载、CAN 总线统计 |

**剩余时间**：Klipper 没有原生字段，但有两个正经来源 ——
`print_stats.print_duration / virtual_sdcard.progress`（按已打印时长外推），
或 `GET /server/files/metadata?filename=<path>` 的 `estimated_time`（切片器给的，更准）。
实测 23% / 已打印 67 分钟时外推得 3 小时 45 分，与文件名的 `4h37m` 估算基本吻合。
进度小于 5% 或没有 `print_duration` 时**不猜**（显示 `--`）。

### ⚠️ 两个料丝传感器的语义（照抄字段会造出假警报）

| 对象 | 原始值（打印中实测） | 正确读法 |
| --- | --- | --- |
| `filament_switch_sensor 断料监测` | `filament_detected: true` | **true = 有料**。写成"无料"就是反的 |
| `filament_motion_sensor 转堵监测` | `filament_detected: false` | 这是**瞬时**值：挤出间隙/回抽/空驶时读到 false 很正常，**不能据此报"堵料"** |

第一版把前者写反、把后者当成故障诊断，结果打印机正常打印时界面上出现
「无料」「可能堵料」两条假警报。现在前者按"有料/无料（已触发）"、后者按
"检测到走料/未检测到（瞬时值）"如实转述。

顺带一个 UI 事实：这些读数一共十几条，**不能全塞进卡片**（会把卡片撑爆），
所以走 `details()` 这条展示通路，默认收在「详情」里。

### 🔧 可用操作（gcode_macro，共 16 个）

`HOME_FULL_QGL`、`HOME_XYZ`、`HOME_X/Y/Z`、`PAUSE`、`RESUME`、`CANCEL_PRINT`、
`SET_PAUSE_NEXT_LAYER`、`SET_PAUSE_AT_LAYER`、`PRINT_START`、`PRINT_END`、
`_CLIENT_EXTRUDE`、`_CLIENT_RETRACT`、`_CLIENT_LINEAR_MOVE`、`_PROBE_TEMP_GUARD`、
`SET_PRINT_STATS_INFO`。

其中 `_CLIENT_*` 三个是 Fluidd / Mainsail 的客户端宏（挤丝、回抽、直线移动），
正是界面做「移动 / 挤出」时该用的东西；`quad_gantry_level`、`bed_mesh`、`probe`、
`manual_probe`、`exclude_object`、`idle_timeout` 也都是对象（可直接查询/调用）。

### ⚠️ 保活：普通 Moonraker **没有** `camera.start_monitor`

U1 需要它，Voron 上调用会返回 `-32601 Method not found`。程序原来每 5 秒试一次、
每次打一条告警。现在一旦确认设备不认识它就永久关掉（`_keepalive_supported`）。

---

## 3. 待补充

* A2L 固件更新完成后：重抓 SSDP 原文确认 `devmodel`，并实测 6000 端口鉴权包的真实回应。
* A2L / H2C 的 Developer Mode `fun` 字段实测（验证结论二）。
* 任一台 U1 实机的 `/server/info` 与 `/printer/objects/list` 输出（验证上表）。
* Voron 上 `print_stats.info` 是否有值（取决于切片器有没有写 `SET_PRINT_STATS_INFO`）。



