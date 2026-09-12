# v1.0.1

只改**提示文案**，没有功能变更。但值得单独发一版 —— 旧文案会把用户引向错误的排查方向。

## 修了什么

如果你之前看到「**控制不可用**」，然后随手点了一下灯却发现**能用** —— 你没看错，
是提示写错了。这条提示的触发条件是「固件要求 MQTT 命令签名」，但：

**1. 把范围说大了**

签名要求**只覆盖 `print` 段的命令**（暂停 / 继续 / 停止 / 速度档位），
而**灯控走 `system` 段，不在该机制范围内** —— 所以灯一直是可用的。
原来笼统写成"控制不可用"，用户看到提示去点灯发现能用，反而更困惑。

**2. 解法只写了一半**

原来只写「开启开发者模式」，而实测**局域网模式**才是最常见、最有效的解法。
现在把三条路径都列出来：

| 方式 | 位置 |
| --- | --- |
| **局域网模式** | 设置 → 网络 → 局域网模式 / LAN Mode Only |
| **开发者模式** | 设置 → 通用 → 开发者模式 / Developer Mode |
| **农场管家** | 改用官方 Farm Manager 接管这台设备 |

**3. 没说清最常见的那道坎**

> ⚠️ **只在切片软件里登录拓竹账号是没用的。**
> 那只影响云端；本项目走局域网接口，必须在打印机本机放行。

现在这些都会显示在提示里（点状态条上的提示可看完整说明）。

## 现在提示长什么样

| 情况 | 状态条 |
| --- | --- |
| 正在打印、暂停/停止被挡 | `⚠ 2 项被固件挡住：请开局域网模式或开发者模式（点此查看）` |
| 空闲（没有暂停/停止按钮） | `ℹ 暂停/停止/速度被固件挡住，灯不受影响；开局域网模式或开发者模式即可（点此查看）` |

点一下会弹出完整原因与三条放行方式。

## 下载

| 你的机器 | 下载 |
| --- | --- |
| Windows | `BambuMonitor-windows-x64.exe`（双击运行） |
| Windows（命令行） | `BambuMonitor-windows-x64-cli.exe` |
| Linux 服务器 / NAS | `BambuMonitor-linux-headless-x64` |
| Linux 桌面 | `BambuMonitor-linux-gui-x64` |
| Docker | `BambuMonitor-docker-image.tar.gz` |
| 安卓平板 / 手机 | `BambuMonitor-android-arm64.apk` |

安卓包内 `versionName` 已升到 `1.0.1`（`versionCode 2`），覆盖安装即可。

校验：`sha256sum -c SHA256SUMS.txt`（Linux / macOS）。

## 完整变更

见 [CHANGELOG.md](https://github.com/asahiba/bambu-monitor/blob/main/CHANGELOG.md#101--2026-09-12)。
协议层的实测结论记在
[docs/FIELD_NOTES.md 结论二](https://github.com/asahiba/bambu-monitor/blob/main/docs/FIELD_NOTES.md)。

v1.0.0 的产物仍在，功能与 1.0.1 完全相同，只有这段提示文案不同。
