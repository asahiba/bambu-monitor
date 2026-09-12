# 拓竹打印机监控台 · Bambu Monitor

[![CI](https://github.com/asahiba/bambu-monitor/actions/workflows/ci.yml/badge.svg)](https://github.com/asahiba/bambu-monitor/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/asahiba/bambu-monitor?include_prereleases&sort=semver)](https://github.com/asahiba/bambu-monitor/releases)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%20%7C%203.11%20%7C%203.12%20%7C%203.13-blue.svg)](https://www.python.org/)

一个**局域网内**的拓竹（Bambu Lab）打印机多画面实时监控台：上方是实时画面墙，
每一路画面下方显示该打印机当前的工作进度、喷嘴/热床/腔体温度与料盘余量。

**四种交付形态，同一套内核**：Windows 单文件 exe、Linux 单文件、Docker 单文件镜像、
安卓 APK（内置 Python，平板可脱离电脑独立运行）。**网页端是各平台统一的操作界面。**

* 通过 **IP + 访问代码**（局域网访问码）直连打印机，**不经过 Bambu 云端**
* **自动搜索**：SSDP 组播 + 单播补扫 + 2021 端口广播三通道，一键发现局域网内打印机
* 支持机型：**A1 / A1 mini / P1P / P1S / X1 / X1C / X1E / A2L / P2S / H2D / H2D Pro / H2C / X2D**
  （未知机型也能连，协议通用）
* 不止拓竹：通过**设备族抽象**接入第三方开源/品牌打印机，已内置 Klipper / Moonraker
  （覆盖 Snapmaker U1 等）—— 见 [架构说明](docs/ARCHITECTURE.md)
* 监控墙特性：自适应分屏、单画面放大、轮巡、抓拍、时间戳叠加、离线提示、断线自动重连
* **完全离线可开发**：内置打印机模拟器，没有真机也能跑通全链路

---

## 0. 下载安装包（不想自己编译）

到 [Releases](https://github.com/asahiba/bambu-monitor/releases) 下载对应平台的单文件产物，
目标机**什么都不用装**：

| 平台 | 下载文件 | 用法 |
| --- | --- | --- |
| Windows | `BambuMonitor-windows-x64.exe` | 双击运行（图形界面） |
| Windows（命令行/无界面） | `BambuMonitor-windows-x64-cli.exe` | 终端里跑 `--core-test` / `--headless` 等 |
| Linux（无界面服务版） | `BambuMonitor-linux-headless-x64` | `chmod +x` 后 `./BambuMonitor-linux-headless-x64 --port 8080` |
| Linux（带界面版） | `BambuMonitor-linux-gui-x64` | `chmod +x` 后直接运行（需图形环境） |
| Docker | `BambuMonitor-docker-image.tar.gz` | `docker load -i ...` 见下 |
| 安卓 | `BambuMonitor-android-arm64.apk` | 传到平板/手机安装（需允许"未知来源"） |

每个 Release 都附带 `SHA256SUMS.txt`，下载后可核对：

```bash
sha256sum -c SHA256SUMS.txt        # Linux / macOS
```

> **安卓版说明**：APK 内置完整的 Python 运行时与 OpenCV，在平板本机跑 HTTP 服务，
> 界面用 WebView 打开，所以**平板可以完全脱离电脑独立使用**；服务绑在 `0.0.0.0`，
> 同一 Wi-Fi 下的其它设备也能访问这台平板的页面。
> 目前是 debug 签名，仅供自用安装；上架应用商店需要换正式签名。
>
> ⚠️ 安卓版尚未在真机上跑过完整回归（开发机没有安卓设备）。如果你遇到问题，
> 欢迎提 issue —— 出错时应用会把 Python 异常直接显示在界面上，方便定位。

---

## 1. 快速开始（从源码）

```bash
git clone https://github.com/asahiba/bambu-monitor.git
cd bambu-monitor

python -m venv .venv
.venv/Scripts/activate         # Windows
# source .venv/bin/activate    # Linux / macOS
pip install -r requirements.txt
```

**方式 A：图形界面**

```
python -m app                  # 或双击 run.bat（首次会自动建环境装依赖）
```

**方式 B：无界面服务版（浏览器当界面）**

```
python -m app.headless --host 0.0.0.0 --port 8080 --status-interval 0
```

首次启动会生成随机访问令牌，日志里会打印带令牌的完整地址。
网页端也支持「添加到主屏幕」当 PWA 用。

**方式 C：Linux / Docker** —— 见 [`docs/DEPLOY.md`](docs/DEPLOY.md)、[`docs/PACKAGING.md`](docs/PACKAGING.md)

```
./linux/run-headless.sh                           # 已装依赖时直接起服务
docker compose up -d                              # Docker（host 网络，可自动搜索）
```

**手边没有打印机？** 内置模拟器会伪造完整的打印机（含 TLS、JPEG 帧、发现协议）：

```bash
# 任意平台，从源码
python -m app --sim 4          # 4 台虚拟打印机 + 图形界面
python -m app --core-test      # 无界面自检（搜索 → 遥测 → 画面）

# Linux 辅助脚本
bash linux/run-headless.sh --sim 4      # 起服务 + 模拟打印机，浏览器看
bash linux/run-tests.sh                 # 跑测试

# Docker
docker run --rm -p 8080:8080 bambu-monitor python -m app.headless --sim 4
```

Windows 上另有快捷方式：`demo.bat`（模拟器 + 界面）、`sim.bat`（仅模拟器）、
`selftest.bat`（无界面自检）、`test.bat`（跑测试）。

```

命令行方式：

```bat
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe -m app            :: 正常启动
.venv\Scripts\python.exe -m app --sim      :: 演示模式（虚拟打印机）
.venv\Scripts\python.exe -m app --core-test:: 无界面自检
```

## 2. 打印机侧准备（重要）

1. 打印机升级到较新的固件，并与电脑处在**同一局域网/同一网段**。
2. 打开 **局域网模式**：
   * **A1 / A1 mini / P1P / P1S**：屏幕 → 设置 → 网络 → 打开「局域网模式」（LAN Only），记下屏幕上显示的 **访问代码**（8 位）。
   * **X1 / X1C / X1E**：设置 → 网络 → 局域网模式 → 打开，并打开 **「局域网模式实时画面 / LAN Mode Liveview」**（否则 322 端口不通，软件会自动改用 6000 端口取画面）。
   * **H2D / H2S / P2S / X2D**：同上；部分固件默认关闭本地 RTSPS，需要在屏幕上手动打开「LAN Mode Liveview」。
3. 记录打印机的 **IP 地址**（屏幕 → 网络，或用软件自动搜索）。
4. Windows 防火墙若弹出提示，请允许该程序访问**专用网络**。

> 访问代码就是 Bambu Studio 里「设备 → 局域网访问代码」，是 8 位字符。

## 3. 使用说明

### 工具栏

| 按钮 | 说明 |
| --- | --- |
| 🔍 自动搜索 (F5) | 扫描局域网，勾选发现的打印机、填好访问代码后「加入监控墙」；默认**持续搜索**，设备会陆续出现 |
| ＋ 添加打印机 (Ctrl+N) | 手动填写 IP / 访问代码，可点「测试连接」自动识别序列号与机型 |
| ▶ 全部连接 / ■ 全部断开 | 批量开关所有通道 |
| 布局 | 自适应、1/2/3/4 列分屏 |
| ⟳ 轮巡 | 每 10 秒自动切换一路单画面（监控软件常见功能） |
| ▦ 画面布局 | 调整画面顺序与大小，可把主力机型设为「重点画面」占 2×2 格 |
| 🌐 网页监控 | 开启内置网页服务，手机/其它电脑用浏览器远程观看 |
| 📷 全部抓拍 | 保存所有画面的当前帧到「图片\BambuMonitor」 |
| ⛶ 全屏 (F11) | 全屏监控墙 |

### 每一路画面

```
┌──────────────────────────────────────────────┐
│  名称 / IP                     ● 在线         │   ← 左上：设备名、IP、机型
│                                              │   ← 右上：连接状态
│              实 时 画 面                      │
│                        2026-01-01 12:00:00   │   ← 右下：时间戳 / 帧率
├──────────────────────────────────────────────┤
│ [打印中]  47%    benchy.gcode      剩余 1小时23分 │   ← 状态 / 进度 / 文件名 / 剩余时间
│ ████████████░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░   │   ← 进度条
│ 喷嘴 245/250℃      热床 60/60℃      层 12/240 │   ← 喷嘴温度 / 热床温度 / 层数
└──────────────────────────────────────────────┘
```

* **双击画面** = 单画面放大 / 还原；**Esc** 退出单画面
* **右键菜单**：单画面、重新连接、通道诊断、抓拍、画面顺序与大小、编辑、复制 IP、移除

### 每路画面显示的信息

```
[打印中] 47%  benchy.gcode  [⚠ HMS×1]  剩余 1小时23分
████████████░░░░░░░░░░░░░░░░░░░░░░░░░░░░   ← 进度条
喷嘴 245/250℃  热床 60/60℃  仓温 33℃  层 12/240  WiFi ▮▮▮▯ -58dBm  预计完成 14:32
耗材  ▮AMS1-1 PLA 92%  ▮AMS1-2 PETG 16%  ▮AMS1-3 ABS 75%  ▮AMS1-4 PLA-CF 48%  ▮外挂 PLA 42%
                              [⏸ 暂停]  [⏹ 停止]  [💡 开灯]
```

| 信息 | 说明 |
| --- | --- |
| 进度 / 剩余时间 / 预计完成 | 预计完成按「剩余分钟数 + 本地时钟」换算，跨天显示「明天 HH:MM」 |
| 喷嘴 / 热床 / **仓温** | 仓温部分机型才有；H2D 等双喷嘴机型会显示第二路温度 |
| **WiFi 信号** | 显示原始 dBm 与 ▮▮▮▯ 信号格（-55 以上 4 格，逐级递减） |
| **HMS 提示** | 顶部出现红色 `⚠ HMS×N`，**点击查看中文说明 + 官方 wiki 说明页**（内置中英文案表） |
| **AMS 耗材** | 每个槽位的色块、材料类型、余量百分比；**正在使用的料卷会高亮** |
| **外挂料盘** | 与 AMS 并排显示，同样有色块/类型 |
| 层数 / 文件名 / 错误码 | 与之前一致，错误码附带中文说明 |

> **余量只在可信时才显示**：只有带 RFID 标签的官方料卷才会被真正计量。
> 第三方料卷（无标签）与外挂料盘读不到余量，此时界面**只显示材料类型、不显示百分比**
> （把鼠标移到耗材行可看到「余量未知（非官方料卷 / 无 RFID 标签）」的说明），
> 避免固件上报的假数值（常见是恒定的 100%）误导判断。

> **画面自动适配窗口**：无论多少路画面，网格都会自动按窗口宽度排布并缩放，
> 不会出现画面跑到屏幕外要手动拖动的情况（重点画面跨格后同样成立）。

### 控制功能（暂停 / 继续 / 停止 / 开关灯）

三种方式任选：

| 位置 | 操作 |
| --- | --- |
| 桌面版 | 画面下方按钮：`⏸ 暂停 / ▶ 继续`、`⏹ 停止`、`💡 开灯/关灯`（遥测未连接时按钮置灰） |
| 网页版 | 每路画面右下角同样的三个按钮（手机也能点） |
| 命令行 | `python -m app.headless --control pause --target 192.168.1.50` |

命令行支持 `pause / resume / stop / light / speed`：

```bash
python -m app.headless --control light  --value off --target all
python -m app.headless --control pause  --target 192.168.31.110
python -m app.headless --control stop   --target 192.168.31.110 --yes   # 必须显式确认
```

> **安全设计**：停止打印不可恢复，桌面/网页端都会二次确认，命令行必须加 `--yes`；
> 遥测未连接时一律拒绝下发。
>
> **测试说明**：按照要求，**暂停/停止/开关灯只在内置模拟器上做了验证**（含 MQTT 报文格式、
> 状态回读、模拟器是否真的收到指令），没有对你正在打印的真机下发过任何控制指令。

### 画面顺序与大小（重点画面）

想让 X2D 占大格、其余占小格：

* **快捷**：右键该画面 → 「画面顺序与大小」→ 「设为重点画面（占 2×2）」；
  同一菜单里还有 上移 / 下移 / 移到最前 / 移到最后。
* **批量**：工具栏「▦ 画面布局」→ 列表里拖动排序、每行右侧选「普通 / 重点(2×2) / 超大(3×3)」→ 应用。
* 布局会**自动装箱**：重点画面占 2×2 后，其余画面会自动补满周围空位，不会留洞。
* 顺序与大小都会写进配置，下次启动保持一致。

### 网页监控（远程观看）

1. 点工具栏「🌐 网页监控」，弹出的窗口里会列出可直接打开的地址（带访问令牌），点「复制」即可。
   列表会优先给出**打印机所在网段**的地址——手机通常和打印机连同一个 Wi-Fi，用这个最稳。
2. 手机连同一个 Wi-Fi，用浏览器打开 `http://<电脑IP>:端口/?token=...` 就能看到同样的监控墙：
   进度条、进度百分比、剩余时间、喷嘴/热床温度、层数、任务名一应俱全，支持点开单画面全屏。
3. 页面上有 **自动布局 / 1-4 列** 下拉框可自选分屏；重点画面的跨格在网页端同样生效。
4. 安全：地址里的 `token` 是访问口令（首次启动自动生成），不带令牌的请求一律 401；
   服务只监听局域网，不会穿透到公网。
5. 性能：网页端的 JPEG 只在**真的有客户端在看**时转码，且多客户端共享同一份缓存；
   帧率与最大宽度可配置（默认 4fps / 720px），手机上很省流量。
6. 首次访问若连不上，请检查 Windows 防火墙是否允许该程序访问「专用网络」。

#### 为什么以前只能显示 6 路（已修复）

浏览器对**同一个域名最多 6 条长连接**，而每一路 MJPEG 各占一条，所以第 7 路开始就一直排队。
现在网页默认走**单连接多路复用**：一条 `/api/live` 连接里同时推送所有画面的 JPEG 与状态 JSON，
记录格式为 `BM`(2) + 类型(1) + 画面序号(2) + 长度(4) + 负载，前端用 `fetch` 流式解析后
转成 blob URL 显示。实测 **8 路画面同一条连接推送正常**，几十路也不受 6 连接限制影响。

若浏览器不支持流式读取，页面会自动退回「逐路 MJPEG + 轮询状态」的兼容模式（此模式下仍受 6 连接限制），
顶栏的标识会显示当前处于哪种模式。

命令行方式：`BambuMonitor-cli.exe --web 8080`（启动即开启网页监控，`--web` 不带端口则用配置里的端口）。

> ⚠️ 命令行走 **`BambuMonitor-cli.exe`**，不是 `BambuMonitor.exe`。
> 后者是 windowed 打包（双击启动界面、无控制台窗口），**没有控制台句柄**，
> 任何命令行输出都拿不到 —— 连 `> log.txt` 重定向都是空文件。
> 原因与取舍见 [`docs/PACKAGING.md`](docs/PACKAGING.md) 的"一次产出两个 exe"。

### 性能与帧率

软件在「多路 1080p 同时看」这件事上做了几处针对性优化，下面是实测数据：

| 优化 | 实测效果 |
| --- | --- |
| **解码搬到后台线程** | 原来每路画面的解码都在界面线程里做，12 路会直接把界面拖卡；现在每路一个解码线程，界面只负责贴图 |
| **按显示尺寸解码**（`QImageReader.setScaledSize`，libjpeg DCT 缩放） | 1080p → 480×270 单帧 **17.78 ms → 2.71 ms（快 6.6 倍）**；折算 12 路 8fps 时是 169% → 26% 单核，而且不再占用界面线程 |
| **RTSPS 先缩放再编码** | 单帧体积 **X2D 124-177 KB → 31 KB、X1C 68 KB → 22 KB**（约 1/5），解码、内存带宽、网页流量同步下降 |
| **先节流再读帧** | 原来「先解码再丢帧」，白白烧 CPU；改成按帧率上限等待后再读帧，两路 RTSPS 画面在 8fps 上限下 **81% → 18% 单核**（4fps 时 12.5%） |
| **只更新变化的内容** | 文本标签、进度条、样式只在数值变化时才刷新，避免每 150ms 触发一次布局重算 |
| **网页按需转码** | 没有观众就不转码；小于 90 KB 的帧直接复用原始 JPEG，不重复编码 |

整体对照（本机 12 路画面，其中 2 路 RTSPS 约 8fps、1 路 6000 端口约 1fps，同时有网页客户端在拉流）：

| 场景 | 优化前 | 优化后 |
| --- | --- | --- |
| 12 路监控墙 + 网页推流 | 87% 单核 | **49% 单核** |
| 网页 MJPEG 推流 | — | 4.3 fps（与设置一致） |

帧率与开销都可以在工具栏「⚙ 设置」里调整：每路最大帧率（省电 4fps / 标准 8 / 流畅 10 / 很流畅 15 / 不限）、
界面刷新间隔、网页帧率与网页画面宽度。P1/A1 的 6000 通道本身只有约 1fps，调高上限不会有变化。

### 自动重连

画面或遥测断开后软件会自动重试（指数退避）；访问代码错误会在画面上明确提示「访问代码错误」，
不会无限空转刷屏。

## 4. 支持的机型与协议

| 机型 | 遥测 | 视频通道 |
| --- | --- | --- |
| A1 / A1 mini / P1P / P1S | MQTT over TLS 8883 | 仅 **TCP 6000**（TLS + 鉴权包，1280×720 JPEG，约 1 fps） |
| X2D / H2D / H2S / P2S | 同上 | 仅 **RTSPS 322**（约 8 fps，需要在打印机上开启「局域网实时画面」） |
| X1 / X1C / X1E | 同上 | 优先 **RTSPS 322**，失败再退 TCP 6000（老固件只有 6000） |

上表是**真机实测**结果（并非照搬社区文档）：

| 机型（固件） | 6000 端口 | RTSPS 322 |
| --- | --- | --- |
| P1S（01.10.00.00） | ✓ 1.8 秒出画面 | ✗ 端口未开放 |
| X1C（01.11.02.00） | ✗ 正确口令也被拒 | ✓ 1.5 秒出画面 |
| X2D（01.01.01.00） | ✗ 正确口令也被拒 | ✓ 3.1 秒出画面 |

> 注意：X1 系列**不能用 6000 端口**取画面是较新固件的行为，很多网上资料仍然写着「X1 用 6000 端口」，
> 那是老固件的情况。软件会按机型自动选通道，并在通道确实不通时自动换道重试，无需手动配置。

协议要点（均来自官方局域网接口的公开逆向资料）：

* **自动搜索**：向 `239.255.255.250:1990` 发 SSDP `M-SEARCH`，同时向 `255.255.255.255:2021` 发 `M99999`；响应里带 IP、序列号、机型、固件版本。
* **遥测**：`mqtts://<IP>:8883`，用户名 `bblp`，密码 = 访问代码，订阅 `device/<序列号>/report`，下发 `pushall` 取全量状态。关心的字段：`mc_percent`（进度）、`mc_remaining_time`（剩余分钟）、`nozzle_temper` / `nozzle_target_temper`、`bed_temper` / `bed_target_temper`、`gcode_state`、`layer_num` / `total_layer_num`、`subtask_name`。
* **画面**：`<IP>:6000`，TLS 连接后发送 80 字节鉴权包（`0x40`、`0x3000`、`bblp`、访问代码），随后每帧为「16 字节帧头 + JPEG 数据」。
* **TLS 细节**：打印机证书由 Bambu Lab CA 签发（CN = 序列号），因此需要 SNI；部分新固件对 TLS 1.3 ClientHello 不响应，软件统一将 TLS 上限压到 **1.2**。

更详细的说明见 `docs/PROTOCOL.md`。

## 5. 常见问题

**搜不到打印机 / 每次都只搜到一部分？**
* 已针对这类问题优化过（见下），现在搜索会**反复进行并累积结果**，30 秒内基本可以搜全；
  已保存的打印机会一直列在对话框里，不会因为某轮没搜到就消失。
* 确认电脑和打印机在同一网段；多网卡/虚拟机网卡环境已经做了逐网卡发送，不再只看默认路由。
* 跨 VLAN/子网时广播无法到达，属于网络结构限制，请手动添加 IP。

### 自动搜索为什么以前不稳（已修复）

| 问题 | 原因 | 处理 |
| --- | --- | --- |
| 每次只搜到一部分、结果随机 | 组播默认只从「默认路由」那张网卡发出；本机同时存在 VMware/Hyper-V/代理网卡时，探测包往往压根没发到打印机所在网段 | 逐网卡建立独立 socket（绑定本机地址 + `IP_MULTICAST_IF`），保证每张网卡都发包 |
| 网卡列表偶尔少了真实局域网网卡 | 靠主机名解析枚举网卡并不可靠（实测会漏掉 `192.168.31.x`） | 改用 Windows 适配器表 `GetAdaptersAddresses`，并读取真实子网掩码 |
| 固件对 SSDP 有节流，单次探测收不全 | 打印机不会对每次搜索都回应 | 搜索窗口内每秒重发 M-SEARCH、每 4 秒整网段单播扫描一次，并持续排空接收缓冲 |
| 一次没搜到就再也看不到 | 列表只显示单轮结果 | 对话框默认**持续搜索** + 预列已保存设备，结果只增不减 |

实测同一网段 13 台设备：12 秒窗口平均 8-11 台、15 秒 10-13 台、**20-30 秒稳定 13/13**。

**有画面但没有温度/进度？**
* 遥测需要**序列号**（用于订阅主题）。自动搜索会带出序列号；手动添加时留空也可以，软件会用通配订阅
  `device/#` 自动识别序列号（在「测试连接」里最直观）。

**能监控，但暂停 / 停止 / 调速点了没反应？**

这是**最常见的一个疑问**，而且不是软件坏了 —— 是打印机固件要求命令签名，
而签名密钥只给官方应用，第三方拿不到。请在打印机屏幕上任选一条放行
（改完**重启设备**）：

| 方式 | 位置 |
| --- | --- |
| **局域网模式** | 设置 → 网络 → 局域网模式 / LAN Mode Only |
| **开发者模式** | 设置 → 通用 → 开发者模式 / Developer Mode |
| **农场管家** | 改用官方 Farm Manager 接管这台设备 |

> ⚠️ **只在切片软件里登录拓竹账号是没用的。** 那只影响云端；
> 本项目走局域网接口，必须在打印机本机放行。
>
> 新机型（A2L / H2C / H2S / X2D / P2S / H2D）默认就需要，老机型通常不用。
> 设备报 `HMS_0500-0500-0001-0007` 就是这个原因。

**为什么「暂停不能用，但灯能开关」？**

因为签名要求**只覆盖 `print` 段的命令**（暂停 / 继续 / 停止 / 速度档位），
而**灯控走 `system` 段，不在该机制范围内**。所以灯一直是可用的 ——
界面会按命令分别置灰，并在状态条上说明哪部分被挡、怎么放行。

**画面提示「访问代码错误」？**
* 重新在打印机屏幕上确认 8 位局域网访问代码；注意不要填成 Bambu 账号密码。

**某一台机器有进度但没有画面？**
* 右键该画面 → **「画面通道诊断…」**，它会逐项检查端口、TLS、6000 端口画面、RTSPS 鉴权与遥测，
  跑完点「复制报告」即可把结论发给开发者。
* 常见原因：X1/X2D/H2 的「局域网实时画面（LAN Mode Liveview）」没开（此时 322 端口不可用，
  软件会用 6000 端口取画面）；或该机型只提供其中一个通道——软件会自动在两个通道之间轮换重试，
  不需要手动切换。

**为什么访问代码只在我点「添加」时保存？**
* 现在已经**即时写盘**了（早期版本只在关闭程序时保存，异常退出会丢配置）。

**X1/X2D 画面很卡？**
* 6000 端口是低帧率抽帧流（约 1-2 fps），这是打印机取景接口的固有节奏。X1/P2S/H2/X2D 请安装
  `opencv-python` 并在打印机上开启「LAN Mode Liveview」，软件会优先走 RTSPS 拿到更流畅的画面；
  拉流失败会在约 2 秒内自动回退到 6000 端口，不会一直卡着。

**X1 系列提示「证书/握手失败」？**
* 实测 X1C 的 6000 端口 TLS 服务低于 OpenSSL 3 默认安全级别，必须降到 `SECLEVEL=0` 才能握手。
  软件已按「先安全、后兼容」的顺序自动尝试（详见 `docs/PROTOCOL.md`），无需手动配置。

**打包成 exe？**
* 双击 `build_exe.bat`，产物在 `dist\BambuMonitor\BambuMonitor.exe`（首次需要联网安装 PyInstaller）。
* 发布包里的单文件版同样支持演示与自检 —— 但**要用 `BambuMonitor-cli.exe`**：
  `BambuMonitor-cli.exe --sim`（演示模式）、`BambuMonitor-cli.exe --core-test`（模拟器全链路自检）。

**配置文件在哪？**
* `%APPDATA%\BambuMonitor\config.json`，日志在 `%APPDATA%\BambuMonitor\logs\app.log`。
* 访问代码使用 **Windows DPAPI 加密**后保存，仅当前 Windows 用户可解密。
* 配置有保护：每次保存前会把上一份备份成 `config.backup.json`；主配置损坏时会自动从备份恢复。
* 工具栏「💾 保存配置」可随时手动保存；旁边的下拉菜单还能**导出 / 导入**配置（换机或备份用，
  导出的文件里访问代码同样是加密的，换 Windows 用户后需要重新填写）。
* 命令行也能备份：`BambuMonitor-cli.exe --export-config D:\backup.json`（不打开界面，导完即退出）。

## 6. 目录结构

```
bambu-monitor/
├─ run.bat / demo.bat / sim.bat / selftest.bat / build_exe.bat
├─ test.bat                      跑自动化测试（Windows）
├─ requirements.txt              运行依赖
├─ requirements-dev.txt          开发依赖（pytest / ruff）
├─ pyproject.toml                pytest 与 ruff 配置
├─ run_app.py                    打包与开发入口
├─ app/
│  ├─ main.py                    命令行入口与演示模式
│  ├─ config.py                  配置读写（DPAPI 加密访问代码）
│  ├─ selftest.py                无界面端到端自检
│  ├─ headless.py                Linux/Docker 无界面服务模式
│  ├─ bambu/                     协议层（不依赖 GUI，可单独测试）
│  │  ├─ ports.py                端口与共享常量（8883 / 6000 / 322）
│  │  ├─ discovery.py            自动搜索（SSDP + 2021 广播）
│  │  ├─ mqtt_worker.py          MQTT over TLS 遥测
│  │  ├─ camera.py               6000 端口 JPEG 流
│  │  ├─ rtsp.py                 RTSPS(322) 备用通道（OpenCV）
│  │  ├─ probe.py                「测试连接」探测
│  │  ├─ printer.py              单台设备会话（遥测 + 画面聚合）
│  │  ├─ models.py               机型识别 / 状态数据模型
│  │  └─ tlsutil.py              打印机 TLS 上下文
│  ├─ ui/                        PySide6 界面（监控墙 / 单路状态条 / 对话框）
│  ├─ web/                       内置网页服务与 PWA 前端
│  ├─ sim/simulator.py           虚拟打印机（发现 + MQTT + 画面）
│  └─ util/                      DPAPI 封装、控制台编码兜底
├─ tests/                        pytest 测试（离线，基于内置模拟器）
├─ tools/                        诊断与自检脚本（连真机用，见 tools/README.md）
├─ linux/                        Linux 安装/服务/打包脚本
└─ docs/
   ├─ PROTOCOL.md                协议与实现说明
   ├─ DEPLOY.md                  Linux / Docker / 安卓部署
   ├─ ARCHITECTURE.md            架构、线程模型、代码地图（开发前先看这个）
   ├─ DEVELOPING.md              开发环境、测试、调试手段
   └─ KNOWN_ISSUES.md            已知问题与技术债清单
```

## 7. 开发与测试

改代码前建议先读 [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)（分层、线程模型、
改一处要同步改哪几处），环境与调试手段见 [`docs/DEVELOPING.md`](docs/DEVELOPING.md)，
已知问题与待办见 [`docs/KNOWN_ISSUES.md`](docs/KNOWN_ISSUES.md)。

```
test.bat                                   :: 跑自动化测试（Windows）
bash linux/run-tests.sh                    :: 同上（Linux）
set BAMBU_RUN_SLOW=1 && test.bat -m slow    :: 端到端慢测试（内置模拟器，离线验证全链路）
```

测试不需要真实打印机：内置模拟器会在回环地址上伪造完整的打印机
（UDP 发现响应 + MQTT broker + 6000 端口画面），因此「搜索 → 遥测 → 画面」全链路都能离线验证。
第三方设备族（Klipper / Moonraker，含 Snapmaker U1）也用假服务器做离线端到端测试。

> 环境提示：本项目虚拟环境不可搬迁（`.venv\pyvenv.cfg` 写死了基础解释器路径）。
> 如果 `.venv\Scripts\python.exe` 报 `did not find executable at ...`，删掉 `.venv`
> 重新运行 `run.bat` 即可。判断环境是否可用不要用 `if exist`，直接跑
> `.venv\Scripts\python.exe -c "import sys"`。

### 打包成单文件

四种形态各自打包，详见 [`docs/PACKAGING.md`](docs/PACKAGING.md)：

| 形态 | 命令 | 产物 |
| --- | --- | --- |
| Windows | `build-onefile.bat` | `dist-onefile\BambuMonitor.exe`（约 93 MB，单文件） |
| Linux | `bash linux/build-headless-docker.sh` | `dist-onefile-headless\BambuMonitor-headless`（约 89 MB） |
| Docker | `build-docker-image.ps1` | `dist-docker\bambu-monitor-latest-image.tar.gz`（约 132 MB） |
| 安卓 | `android/build-apk.ps1` 或 Android Studio | `app-debug.apk` |

Windows / Linux 产物**自带 Python 运行时**，目标机不需要装任何东西；
Docker 产物是**整个镜像**打成一个 tar，`docker load` 即可用。

## 8. 已验证情况

本项目在交付前跑过以下验证（`selftest.bat`、`demo.bat` 可复现）：

| 验证项 | 结果 |
| --- | --- |
| 自动搜索（SSDP + 2021 双通道） | ✓ 同时发现模拟器与真实局域网内的 11 台拓竹打印机（X1C / X2D / P1S×5 / A1×2 / A1 mini / P1P） |
| 遥测（MQTT 8883） | ✓ 进度 / 剩余时间 / 喷嘴温度 / 热床温度 全部正确刷新 |
| 画面（6000 端口 TLS + 鉴权包） | ✓ 取得 1280×720 JPEG 并持续刷新 |
| 界面数据绑定 | ✓ 4 路画面同时在线，状态条显示进度 43%/32%/24%/30%、喷嘴 220/220℃、热床 60/60℃ |
| 界面渲染 | ✓ 截图像素分析确认画面区、进度条、在线状态点、文字均正常绘制 |
| 真机 TLS 证书链 | ✓ X1C / P1S / X2D 的 8883、6000 端口均可通过内置 Bambu Lab CA 校验 |
| 真机错误路径 | ✓ 访问代码错误时准确提示「访问代码错误」，且 1-2 秒内快速失败，不空转 |
| 真机会话（修复后） | ✓ X2D / X1C / P1S 三台同时 **画面 + 遥测全部在线**：X2D 约 8fps（RTSPS）、X1C 约 7.5fps（RTSPS）、P1S 约 0.7fps（6000 端口硬件限制） |
| 画面布局 | ✓ 离屏验证装箱：重点画面 2×2 + 其余画面自动填空位；顺序调整、单画面、固定 4 列均符合预期 |
| 网页监控 | ✓ 端到端通过：无令牌 401、带令牌返回状态、首页 HTML、单帧 JPEG、MJPEG 推流分帧正常 |
| 网页多路复用 | ✓ 实测 **8 路 / 13 路画面全部通过一条 `/api/live` 连接推送**（浏览器 6 连接限制已绕过） |
| 13 台真机同时在线 | ✓ 13 台全部「画面 + 遥测」在线：X2D 9-10fps、X1C 8-10fps（RTSPS），11 台 P1/A1 约 0.4-0.9fps（6000 端口硬件限制） |
| Linux 无界面模式 | ✓ 在**完全无法导入 PySide6** 的环境下启动成功：4 路模拟画面（自动改用 OpenCV 绘制）、PWA 资源、多路复用推流全部正常 |
| Docker 版 | ✓ 镜像构建通过、健康检查 healthy；挂载配置后**连上 13 台真机全部在线**，容器内管理命令（`--list / --add-printer / --discover`）可用 |
| 安卓 PWA | ✓ manifest / 192+512 图标 / Service Worker / apple-touch-icon 全部 200，「添加到主屏幕」后全屏运行 |
| 配置安全 | ✓ 保存前后 13 台打印机与 13 个访问代码完全一致；自动生成 `config.backup.json`；支持手动保存 / 导出 / 导入 |
| 性能优化 | ✓ 解码 17.78ms → 2.71ms；RTSPS 帧 124-177KB → 22-32KB；两路 RTSPS 在 8fps 上限下 CPU 81% → 18% |
| 打包版 exe | ✓ `dist\BambuMonitor\BambuMonitor.exe` 12 路画面 + 网页推流同时在线，截图确认渲染正常 |

### 曾经的问题：X2D 有进度没有画面（已修复）

诊断结论（真机实测）：

1. X2D 的 6000 端口在**访问代码完全正确**的情况下仍然回一个拒绝包 —— 该机型只提供 RTSPS(322)；
2. 旧版本把视频通道当成「一次性选择」：RTSPS 7 秒内没出画面就永久退回 6000，而 6000 在 X2D 上必然失败，
   于是再也回不到能用的通道；
3. 另外 X2D 的证书链无法通过内置 CA 校验，每次连接都要先试两个无效的 TLS 参数（各 1.6～2.6 秒），
   首次建连累计 6～7 秒，正好压在原 6 秒握手超时线上。

修复：按机型固定可用通道、TLS 参数记忆化、画面长时间无更新时自动换道重试、RTSPS 首帧等待从 7 秒放宽到 20 秒并重试 3 次。

仍未在真机上验证的一处：RTSPS(322) **成功**拉流（需要真实访问代码）。
X1C 的 322 端口已确认可连通并能完成 TLS 握手与鉴权交互（错误代码返回 401，路径正确）。

## 9. 说明与免责

* 本项目使用打印机**官方局域网接口**（MQTT/摄像头/SSDP），数据只在本地网络内传输，不上传云端。
* 证书校验：默认使用内置的 Bambu Lab CA 做证书链校验；若个别固件证书异常，会自动回退为
  「仅加密不校验证书」并在界面提示（局域网的常见折中做法）。
* HMS 错误码中文文案取自 [ha-bambulab](https://github.com/greghesp/ha-bambulab)（MIT 许可），
  随程序分发的数据文件在 `app/bambu/data/`；若该目录缺失，界面会退化为只显示错误码，
  并可点击跳转官方 wiki 说明页。
* 控制指令（暂停/继续/停止/开关灯/速度档位）通过打印机官方 MQTT 接口下发，与 Bambu Studio 行为一致；
  停止不可恢复，桌面端与网页端都有二次确认，命令行必须加 `--yes`。
* 本项目非 Bambu Lab 官方软件，机型与固件差异可能导致个别功能不可用，欢迎按 `docs/PROTOCOL.md` 补充。
* 本项目与 Bambu Lab 无隶属关系，也未获得其背书。商标归各自所有者。

## 10. 参与贡献

欢迎 issue 与 PR。**不需要真打印机**就能开发 —— 内置模拟器会把整条链路
（TLS 遥测、6000 端口 JPEG、发现协议）都伪造出来：

```bash
pip install -r requirements-dev.txt
python -m pytest -q          # 回归套件，完全离线
python -m pytest -q -m slow  # 慢速端到端（模拟器 + 真实端口）
ruff check .                 # 静态检查
```

改动前建议先读 [`CONTRIBUTING.md`](CONTRIBUTING.md)（含几条硬性约定，
以及 PowerShell 脚本的两个格式坑）；要接新品牌请看
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) 的设备族抽象一节。

安全问题请按 [`SECURITY.md`](SECURITY.md) 私下报告，不要开公开 issue。

## 11. 许可证

[MIT](LICENSE) © 2026 asahiba

第三方资源的许可与出处：

* HMS 错误码中文文案取自 [ha-bambulab](https://github.com/greghesp/ha-bambulab)（MIT），
  见 `app/bambu/data/`；
* 内置的 Bambu Lab CA 证书用于校验打印机自签证书；
* 安卓版通过 [Chaquopy](https://chaquo.com/chaquopy/) 内嵌 CPython。
