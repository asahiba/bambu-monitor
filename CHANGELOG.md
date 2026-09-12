# 更新日志

本文件记录值得用户注意的变更。格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [未发布]

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

[未发布]: https://github.com/asahiba/bambu-monitor/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/asahiba/bambu-monitor/releases/tag/v1.0.0
