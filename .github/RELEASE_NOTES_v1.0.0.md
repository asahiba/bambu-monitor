# v1.0.0

局域网内拓竹（Bambu Lab）打印机多画面实时监控台的**首个公开版本**。
四种交付形态共用同一套 Python 内核，**网页端是各平台统一的操作界面**。

## 下载哪个

| 你的机器 | 下载 | 目标机需要装什么 |
| --- | --- | --- |
| Windows | `BambuMonitor-windows-x64.exe` | 什么都不用装，双击即运行 |
| Windows（命令行） | `BambuMonitor-windows-x64-cli.exe` | 终端里跑 `--core-test` / `--headless` 等 |
| Linux 服务器 / NAS | `BambuMonitor-linux-headless-x64` | 什么都不用装 |
| Linux 桌面（要窗口） | `BambuMonitor-linux-gui-x64` | 什么都不用装（需图形环境） |
| Docker | `BambuMonitor-docker-image.tar.gz` | 只需 Docker |
| 安卓平板 / 手机 | `BambuMonitor-android-arm64.apk` | Android 7.0+（arm64） |

全部是**单文件**：Windows/Linux 自带 Python 运行时，Docker 是整镜像打包，
安卓 APK 内嵌 Python 与 OpenCV。

## 怎么用（最短路径）

**Windows**：双击 `BambuMonitor-windows-x64.exe` → 点「🔍 自动搜索」→
填访问代码（打印机屏幕 → 设置 → 网络 → 局域网访问代码）→ 添加。

> 需要命令行（自检、无界面服务、备份配置）请用 `BambuMonitor-windows-x64-cli.exe`。
> 图形版是 windowed 打包，**没有控制台句柄**，连 `> log.txt` 重定向都是空文件。

**Linux / NAS**：

```bash
chmod +x BambuMonitor-linux-headless-x64
./BambuMonitor-linux-headless-x64 --host 0.0.0.0 --port 8080 --status-interval 0
```

首次启动会生成随机访问令牌，日志里会打印带令牌的完整地址，浏览器打开即可。

**Docker**：

```bash
docker load -i BambuMonitor-docker-image.tar.gz
docker run -d --name bambu-monitor --network host \
  -v bambu-config:/config bambu-monitor:latest
```

> `--network host` 是必须的：自动搜索靠 SSDP 组播广播，桥接网络下搜不到打印机。

**安卓**：把 APK 传到平板安装（需允许"未知来源"）。
应用**内置完整的 Python 服务**，在设备本机跑 HTTP 服务、用 WebView 显示界面，
所以平板可以完全脱离电脑独立使用；服务绑在 `0.0.0.0`，
同一 Wi-Fi 下的其它手机/电脑也能访问这台平板的页面。

## 主要能力

- **遥测**：MQTT over TLS（8883）订阅状态报文 —— 进度、剩余时间、
  喷嘴/热床/腔体温度、AMS 各料盘余量与类型、层数、HMS 错误码中文解释
- **画面**：6000 端口鉴权 JPEG 流 + RTSPS（322，H.264，需 OpenCV）双通道，
  按机型与实际连通性自动选择并回退
- **发现**：SSDP 组播 + 单播补扫 + 旧版 2021 端口广播三通道
- **控制**：暂停 / 继续 / 停止 / 打印速度 / 腔体灯
- **设备管理**：自动搜索、手动添加、改名、改访问代码、删除、重连；多台并排
- **机型**：A1 / A1 mini / P1P / P1S / X1 / X1C / X1E / A2L / P2S /
  H2D / H2D Pro / H2C / X2D（未知机型也能连，协议通用）
- **第三方设备族**：通过设备族抽象接入 Klipper / Moonraker（覆盖 Snapmaker U1 等）
- **网页端**：单连接多路复用（一条连接推所有画面与状态），绕开浏览器同域
  6 连接限制；支持「添加到主屏幕」当 PWA
- **内置模拟器**：没有真机也能跑通全链路（开发与 CI 都靠它）

## 校验下载文件

```bash
sha256sum -c SHA256SUMS.txt          # Linux / macOS
```

```powershell
Get-FileHash .\BambuMonitor-windows-x64.exe -Algorithm SHA256   # Windows
```

## 已知限制

- **安卓 APK 尚未在真机上跑过完整回归** —— 构建机没有安卓设备、也无核显跑模拟器。
  已做的验证是：包内容与字节码核查、清单/签名/ABI 检查，以及
  同一套代码在 Python 3.10（APK 内嵌版本）下的全量回归。
  如果你遇到问题欢迎提 issue：**出错时应用会把 Python 异常直接显示在界面上**。
- 固件开启 Developer Mode 签名要求时，`print` 段命令（暂停/继续/停止/调速）
  会被设备静默忽略 —— 界面会明确提示原因，灯控不受影响（走 `system` 段）。
- Linux / Docker / 安卓没有 Windows DPAPI，打印机访问代码是**明文存储**，
  程序会提示；请自行保证配置文件权限，且**不要暴露到公网**。
- APK 是 debug 签名，仅供自用安装。

## 文档

- [README](https://github.com/asahiba/bambu-monitor#readme) —— 完整说明
- [架构说明](https://github.com/asahiba/bambu-monitor/blob/main/docs/ARCHITECTURE.md) —— 模块划分与设备族抽象
- [协议说明](https://github.com/asahiba/bambu-monitor/blob/main/docs/PROTOCOL.md) —— 机型识别与报文解析
- [实测笔记](https://github.com/asahiba/bambu-monitor/blob/main/docs/FIELD_NOTES.md) —— 机型差异与踩坑记录
- [打包说明](https://github.com/asahiba/bambu-monitor/blob/main/docs/PACKAGING.md) —— 四种形态怎么构建
- [已知问题](https://github.com/asahiba/bambu-monitor/blob/main/docs/KNOWN_ISSUES.md)
- [更新日志](https://github.com/asahiba/bambu-monitor/blob/main/CHANGELOG.md)

## 许可证

[MIT](https://github.com/asahiba/bambu-monitor/blob/main/LICENSE)。
本项目非 Bambu Lab 官方软件，与其无隶属关系。
