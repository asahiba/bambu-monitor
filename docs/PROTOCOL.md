# 协议与实现说明

本文记录本项目使用的拓竹局域网接口（基于公开逆向资料与实测），便于后续维护。
所有端口均为打印机本地端口，不涉及云端。

## 1. 自动搜索

两条通道并行，任意一条命中即可（`app/bambu/discovery.py`）。

### 1.1 SSDP（新固件）

```text
发送到 239.255.255.250:1990
M-SEARCH * HTTP/1.1
HOST: 239.255.255.250:1990
MAN: "ssdp:discover"
MX: 2
```

打印机**单播**回包（也可被动接收 `ssdp:alive` 通知）：

```text
HTTP/1.1 200 OK
Location: 192.168.1.50          ← IP
USN: 01P00A1234567              ← 序列号
devmodel.bambu.com: P1S         ← 机型
devname.bambu.com: 3DP-001      ← 名称
devversion.bambu.com: 01.05.00.00
devconnect.bambu.com: lan
```

### 1.2 旧版 JSON 广播（3DPrinterOS 风格）

向 `255.255.255.255:2021`（以及各网卡 /24 定向广播地址）发送 `M99999`，
打印机把 JSON 回给源端口：

```json
{"dev_ip":"192.168.1.50","dev_id":"01P00A1234567","dev_name":"3DP-001",
 "dev_version":"01.05.00.00","dev_signal":"-50","dev_connect":"lan"}
```

### 1.3 机型识别

优先用 `devmodel`/`devname` 关键字，其次用序列号前 3 位：

| 前缀 | 机型 | 前缀 | 机型 |
| --- | --- | --- | --- |
| `00M` | X1C | `030` | A1 mini |
| `00W` | X1 | `039` | A1 |
| `03W` | X1E | `22E` | P2S |
| `01S` | P1P | `093` | H2S |
| `01P` | P1S | `094` | H2D |

新机型（如 X2D）若前缀未收录，会在界面上显示「未知机型」，但**连接与显示不受影响**。

## 2. 遥测（MQTT over TLS）

* 地址：`mqtts://<IP>:8883`，TLS（证书 CN = 序列号 → 需要 SNI）
* 用户名 `bblp`，密码 = 局域网访问代码
* 订阅 `device/<序列号>/report`；未填序列号时用通配 `device/#` 反推序列号
* 下发 `device/<序列号>/request`：

```json
{"pushing":{"sequence_id":"1","command":"pushall","version":1,"push_target":1}}
```

* 上报报文关键字段（`print` 对象内）：

| 字段 | 含义 |
| --- | --- |
| `gcode_state` | IDLE / RUNNING / PAUSE / FINISH / FAILED |
| `mc_percent` | 打印进度 % |
| `mc_remaining_time` | 剩余分钟 |
| `nozzle_temper` / `nozzle_target_temper` | 喷嘴当前/目标温度 |
| `bed_temper` / `bed_target_temper` | 热床当前/目标温度 |
| `chamber_temper` | 腔温（部分机型） |
| `layer_num` / `total_layer_num` | 当前层 / 总层 |
| `subtask_name` | 当前文件名 |
| `hms` / `print_error` | 故障与错误码 |
| `ipcam.rtsp_url` | 本地 RTSPS 地址（关闭时为 `disable`） |

> P1 系列为省资源只推送**变化过**的字段，因此实现中必须做字段合并；
> 官方建议 `pushall` 间隔 ≥ 5 分钟（本项目为连接时请求一次 + P1 每 5 分钟一次）。

## 3. 画面

### 3.1 TCP 6000（全部机型）

1. 建立 TCP 连接后立刻做 TLS 握手（上限 TLS 1.2）。
2. 发送 80 字节鉴权包：

| 偏移 | 长度 | 内容 |
| --- | --- | --- |
| 0 | 4 | `0x40`（内联负载长度） |
| 4 | 4 | `0x3000`（类型） |
| 8 | 8 | 0 |
| 16 | 32 | 用户名 `bblp`，ASCII，右侧补 `\0` |
| 48 | 32 | 访问代码，ASCII，右侧补 `\0` |

3. 之后服务器持续发送帧：

| 偏移 | 长度 | 内容 |
| --- | --- | --- |
| 0 | 4 | JPEG 长度（小端，不含帧头） |
| 4 | 4 | itrack = 0 |
| 8 | 4 | flags = 1 |
| 12 | 4 | 0 |
| 16 | N | JPEG 数据（`FF D8` 开头，`FF D9` 结尾） |

访问代码错误时：

* **A1 / P1 系列**：服务器在收到鉴权包后立即关闭连接（`recv` 返回空）。
* **X1 / X2D 等新固件**：服务器回一个 24 字节的拒绝包，结构为
  「16 字节帧头（size=8, type=0x0003013f）+ 8 字节负载（错误码 -1）」，
  例如 `08 00 00 00 3f 01 03 00 00 00 00 00 00 00 00 00 ff ff ff ff 00 00 00 00`。

两种情况本项目都会识别为「访问代码错误」，不会误判成画面卡顿。

### 3.1.1 握手慢带来的坑（实测）

X2D 的 6000 端口 TLS 握手比其它机型慢得多：

| 机型 | 单次握手耗时 |
| --- | --- |
| P1S | 0.92s |
| X1C | 0.16s |
| X2D | 1.6～5.7s |

更糟的是 X2D 的证书链**无法**通过内置 Bambu Lab CA 校验（`CERTIFICATE_VERIFY_FAILED`），
于是每次连接都要先试两个「校验证书」的上下文（各 1.6～2.6s）才能落到可用的不校验上下文，
首次连接累计约 6～7 秒，正好卡在原本 6 秒的握手超时附近，于是表现为「时通时不通 / 干脆没画面」。

因此 `tlsutil` 做了两件事：

1. 每台打印机**记住上次成功的 TLS 上下文**（`_CONTEXT_CACHE`），后续连接直接命中，
   实测从 6.8s 降到 1.7s；
2. 握手超时单独放宽到 15 秒（`HANDSHAKE_TIMEOUT`），与 TCP 连接超时解耦。

### 3.2 RTSPS 322（X1 / X1E / P2S / H2D / H2S / X2D）

```text
rtsps://bblp:<访问代码>@<IP>:322/streaming/live/1
```

* 部分固件默认关闭，需要在打印机屏幕上开启「LAN Mode Liveview」；
  开启前 `push_status` 里 `ipcam.rtsp_url` 为 `disable`，322 端口会立即断开连接。
* A1 / P1 系列没有该接口，只能用 6000 端口。
* 本项目通过 OpenCV(FFmpeg) 拉流，并设置 `OPENCV_FFMPEG_CAPTURE_OPTIONS=rtsp_transport;tcp|tls_verify;0`
  以接受打印机自签证书。

## 4. TLS 注意事项

* 打印机证书由 Bambu Lab CA 签发，CN 为序列号，因此需要 SNI；本项目内置 CA
  (`app/bambu/certs/ca_cert.pem`) 做证书链校验，并关闭主机名校验（IP ≠ CN）。
* Python 3.13 起 `create_default_context()` 默认开启 X509 严格校验，拓竹 CA 缺 key usage
  扩展会报错，需要 `context.verify_flags &= ~ssl.VERIFY_X509_STRICT`。
* 部分新固件（如 P2S 01.02.00.00）不回应 TLS 1.3 ClientHello，会导致握手长时间挂起，
  因此统一 `context.maximum_version = ssl.TLSVersion.TLSv1_2`。
* **实测（X1C 固件 01.08）：6000 端口的 TLS 服务低于 OpenSSL 3 默认安全级别**，
  用默认参数握手只会收到 `sslv3 alert handshake failure`；必须
  `context.set_ciphers("DEFAULT:@SECLEVEL=0")` 才能连上。322 端口的 RTSPS 没有这个问题。

因此 `tlsutil` 按「先安全、后兼容」的顺序尝试四种组合，取第一个握手成功的：

| 顺序 | 证书链 | 安全级别 | 结果 |
| --- | --- | --- | --- |
| 1 | 校验 BBL CA | 默认 | P1/A1、322 端口、MQTT 8883 |
| 2 | 校验 BBL CA | SECLEVEL=0 | X1C 的 6000 端口（实测可校验通过） |
| 3 | 不校验 | 默认 | 证书异常但安全级别正常的固件 |
| 4 | 不校验 | SECLEVEL=0 | 最后的兼容兜底 |

MQTT（paho）不能在建连后切换上下文，所以先用 `select_context()` 做一次握手探测，
选出可用参数再交给 paho。实测发现「先给校验上下文、失败后再换」的做法在 paho 上
并不会真正重连成功。

## 5. 模拟器

`app/sim/simulator.py` 在本机回环地址上伪造打印机，用于无真机开发与自检：

* 使用 `127.0.0.2 / 127.0.0.3 …`，因此各台虚拟打印机可以共用 8883、6000 标准端口
* 启动时会用 `cryptography` 生成自签证书（CN 为序列号），并运行一个极简 MQTT Broker
* 共用一套 UDP 响应器，同时回应 SSDP `M-SEARCH` 与 `M99999` 广播

```bat
.venv\Scripts\python.exe -m app.sim --count 4      :: 只跑模拟器
.venv\Scripts\python.exe -m app --sim             :: 模拟器 + 软件界面
.venv\Scripts\python.exe -m app --core-test       :: 无界面全链路自检
```
