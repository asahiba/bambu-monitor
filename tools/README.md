# 开发/排障脚本

这些脚本用于在没有真机（或配合真机）时定位问题，**普通使用不需要运行**。

约定：脚本走「边打印边判断 + 用退出码表达结果（0 成功 / 1 失败）」，
它们是**诊断工具而不是单元测试**——会真的连设备、占端口。
想要可重复、离线的自动化验证，请用 `test.bat`（见 [`../docs/DEVELOPING.md`](../docs/DEVELOPING.md)）。

带 `--auto` 的脚本会从 `%APPDATA%\BambuMonitor\config.json` 读取该 IP 已保存的访问代码，
因此口令不会出现在命令行与日志里。

## 一站式诊断（最常用）

| 脚本 | 用途 |
| --- | --- |
| `diagnose.py <IP> [--auto]` | **首选**：端口、TLS 参数、6000 画面、RTSPS 鉴权与实际拉流、MQTT 遥测全跑一遍 |
| `session_check.py [秒数] [IP…]` | 按配置跑完整会话（遥测 + 画面），真机验证通道选择，逐台打印结果 |
| `real_printer_check.py <IP…>` | 只读检查真实打印机端口与证书链（**不发送任何鉴权/控制指令**） |
| `auth_error_check.py <IP>` | 用错误访问代码验证「访问代码错误」的提示路径 |
| `raw_fields.py <IP> [--auto]` | 打印打印机上报的**原始字段**（找新字段、核对字段名时用） |

## 视频通道

| 脚本 | 用途 |
| --- | --- |
| `probe6000.py <IP…>` | 观察 6000 端口反应，区分「不支持该协议」与「口令不对」 |
| `raw_camera_check.py <IP> [--auto]` | 直接用 socket 走完 6000 端口的 TLS + 80 字节鉴权包 + 帧格式 |
| `rtsp_describe.py <IP> [--auto]` | 手工对 322 端口发 RTSP DESCRIBE，判断服务状态与鉴权方式 |
| `rtsp_check.py <IP> [--auto]` | 验证 RTSPS 通道实际能否拉到画面、耗时多少 |

## TLS

| 脚本 | 用途 |
| --- | --- |
| `tls_matrix.py <IP> <端口…>` | 逐种 TLS 参数试探端口，找出可用的握手配置 |
| `tls_timing.py <IP> [端口]` | 测量各 TLS 候选参数的握手耗时（验证上下文缓存效果） |

## 遥测（MQTT）

| 脚本 | 用途 |
| --- | --- |
| `raw_mqtt_check.py <IP> <访问代码> [序列号]` | 用 paho 直连 8883，打印收到的状态报文 |
| `debug_mqtt_worker.py` | 单独调试 `MqttWorker`（使用内置模拟器） |

## 控制指令（暂停 / 继续 / 停止 / 开关灯 / 速度）

| 脚本 | 用途 |
| --- | --- |
| `control_check.py` | 验证控制指令与新增状态字段（AMS/外挂/HMS/WiFi/仓温/预计完成/灯光），**只在模拟器上测试** |
| `control_cli_check.py` | 验证无界面版命令行控制（`--control pause/stop/light`），同样只对模拟器 |
| `control_api_check.py <端口> [令牌]` | 验证网页控制接口 `/api/command`；**只对模拟器或你允许被控的机器使用** |

> 这三个脚本**不会**对你正在打印的真机下发指令。停止打印不可恢复，请勿改成直连真机。

## 网页服务

| 脚本 | 用途 |
| --- | --- |
| `web_check.py [端口]` | 端到端验证网页服务：令牌鉴权 / JSON API / 单帧 / MJPEG（自带模拟器） |
| `web_live_check.py [端口]` | 对**正在运行**的软件做网页检查（令牌从配置读取） |
| `headless_check.py` | 无界面（Linux/Docker）模式验证：**伪造无 Qt 环境**后跑服务端与 PWA 资源 |

## 界面与布局

| 脚本 | 用途 |
| --- | --- |
| `layout_fit_check.py` | 离屏验证画面布局的装箱与**真实字体度量**（重点画面 2×2 + 自动补空位） |
| `layout_check.py` | ⚠️ **已过时**，被 `layout_fit_check.py` 取代（无退出码、不做字体度量）。保留仅作对照 |
| `dialog_smoke.py` | 离屏创建各对话框，跑一次自动搜索，检查界面构建 |
| `inspect_shot.py <png> [列] [行]` | 分析界面截图的像素分布，确认画面/进度条/状态点已渲染 |

## 性能基准

| 脚本 | 用途 |
| --- | --- |
| `perf_check.py [秒数] [IP…]` | 测量不同帧率上限下真实打印机会话的 CPU 占用 |
| `decode_bench.py [宽 高 目标宽 目标高 次数]` | 对比两种 JPEG 解码路径的耗时（性能优化的证据来源） |
| `discovery_bench.py [次数] [秒数]` | 连续多次自动搜索，统计发现数量与稳定性 |

## 配置

| 脚本 | 用途 |
| --- | --- |
| `show_config.py` | 查看已保存的打印机配置（访问代码只显示位数） |
| `config_check.py` | 配置读写/备份/恢复的端到端检查（在临时目录里做，不动真实配置） |
| `check_export.py` | 验证 `--export-config` 导出的文件内容与可还原性 |
| `export_plain_config.py` | 导出**明文**配置（仅为迁移到 Linux 用；注意别把明文口令提交进仓库） |

## 网络与适配器

| 脚本 | 用途 |
| --- | --- |
| `adapter_debug.py` | 打印 Windows 适配器表与枚举结果，排查「网卡列表缺了局域网网卡」 |

## 代码检查（进 CI 的那类）

| 脚本 | 用途 |
| --- | --- |
| `check_qt_imports.py [目录…]` | 校验所有 PySide6 导入的属性真实存在（`from PySide6.QtWidgets import QStandardPaths` 这种错 pyflakes/ruff 查不出来），并提示正确来源模块 |
| `check_qt_imports_probe.py` | 自证上面那个工具：故意写错的必须被查出，正确的必须通过 |
| `check_workflow_ps.py` | 校验 GitHub 工作流里 `pwsh` 步骤的语法 |

## 崩溃复现（都是「关对话框导致进程消失」那一类）

| 脚本 | 用途 |
| --- | --- |
| `add_dialog_close_probe.py` | 复现/验证「点测试连接后立刻关闭添加对话框」是否还会带走进程（Qt fail-fast 0xC0000409） |
| `web_toggle_probe.py` | 复现/验证网页监控开关的启停与信号重入（开机自动开启时不应弹模态框） |

## 资源生成与公共库

| 脚本 | 用途 |
| --- | --- |
| `make_icons.py` | 重新生成 `app/web/icons.py` 里的 PWA 图标（base64） |
| `_common.py` | 公共库：让脚本能从仓库根目录导入 `app.*`、读取配置里的访问代码等。**不是可执行脚本** |

## 示例

```bat
.venv\Scripts\python.exe tools\diagnose.py 192.168.1.50 --auto
.venv\Scripts\python.exe tools\session_check.py 30
.venv\Scripts\python.exe tools\web_check.py
.venv\Scripts\python.exe tools\inspect_shot.py _ui1.png 2 2
```
