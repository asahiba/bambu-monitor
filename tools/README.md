# 开发/排障脚本

这些脚本用于在没有真机（或配合真机）时定位问题，普通使用不需要运行。

| 脚本 | 用途 |
| --- | --- |
| `control_check.py` | 验证控制指令与新增状态字段（AMS/外挂/HMS/WiFi/仓温/预计完成/灯光），**只在模拟器上测试** |
| `control_cli_check.py` | 验证无界面版命令行控制（`--control pause/stop/light`），同样只对模拟器 |
| `control_api_check.py <端口> [令牌]` | 验证网页控制接口 `/api/command`；**只对模拟器或你允许被控的机器使用** |
| `layout_check.py` | 离屏验证画面布局（顺序 + 重点画面跨格）的装箱结果 |
| `web_check.py [端口]` | 端到端验证网页服务：令牌鉴权 / JSON API / 单帧 / MJPEG 推流（自带模拟器） |
| `web_live_check.py [端口]` | 对**正在运行**的软件做网页检查（令牌从配置读取） |
| `perf_check.py [秒数] [IP…]` | 测量不同帧率上限下真实打印机会话的 CPU 占用 |
| `decode_bench.py [宽 高 目标宽 目标高 次数]` | 对比两种 JPEG 解码路径的耗时（性能优化证据） |
| `discovery_bench.py [次数] [秒数]` | 连续多次自动搜索，统计发现数量、稳定性与各通道命中情况 |
| `adapter_debug.py` | 打印 Windows 适配器表与枚举结果，排查「网卡列表缺了局域网网卡」这类问题 |
| `diagnose.py <IP> [--auto]` | **一站式诊断**：端口、TLS 参数、6000 画面、RTSPS 鉴权与实际拉流、MQTT 遥测 |
| `session_check.py [秒数] [IP…]` | 按配置跑完整会话（遥测 + 画面），真机验证通道选择，逐台打印结果 |
| `show_config.py` | 查看已保存的打印机配置（访问代码只显示位数） |
| `probe6000.py <IP…>` | 用错误口令观察 6000 端口反应，区分「不支持该协议」与「口令不对」 |
| `rtsp_describe.py <IP> [--auto]` | 手工对 322 端口发 RTSP DESCRIBE，判断 RTSP 服务状态与鉴权方式 |
| `rtsp_check.py <IP> [--auto]` | 验证 RTSPS 通道实际能否拉到画面、耗时多少 |
| `tls_timing.py <IP> [端口]` | 测量各 TLS 候选参数的握手耗时（验证上下文缓存效果） |
| `raw_camera_check.py <IP> [--auto]` | 直接用 socket 走完 6000 端口的 TLS + 鉴权包 + 帧格式 |
| `raw_mqtt_check.py <IP> <访问代码> [序列号]` | 用 paho 直连 8883，打印收到的状态报文 |
| `debug_mqtt_worker.py` | 单独调试 `MqttWorker`（使用内置模拟器） |
| `tls_matrix.py <IP> <端口…>` | 逐种 TLS 参数试探打印机端口，找出可用的握手配置 |
| `real_printer_check.py <IP…>` | 只读检查真实打印机端口与证书链（不发送任何鉴权/控制指令） |
| `auth_error_check.py <IP>` | 用错误访问代码验证「访问代码错误」提示路径 |
| `dialog_smoke.py` | 离屏创建各对话框，跑一次自动搜索，检查界面构建 |
| `inspect_shot.py <png> [列] [行]` | 分析界面截图的像素分布，确认画面/进度条/状态点已渲染 |

> 带 `--auto` 的脚本会从 `%APPDATA%\BambuMonitor\config.json` 读取该 IP 已保存的访问代码，
> 因此口令不会出现在命令行与日志里。

示例：

```bat
.venv\Scripts\python.exe tools\diagnose.py 192.168.1.50 --auto
.venv\Scripts\python.exe tools\session_check.py 30
.venv\Scripts\python.exe tools\inspect_shot.py _ui1.png 2 2
```
