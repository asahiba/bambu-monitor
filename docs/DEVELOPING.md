# 开发上手

面向要在这个项目上继续写代码的人。架构看 [`ARCHITECTURE.md`](ARCHITECTURE.md)，
已知问题看 [`KNOWN_ISSUES.md`](KNOWN_ISSUES.md)。

## 1. 环境

### 1.1 解释器与虚拟环境

| 项 | 值 |
| --- | --- |
| Python | **3.13.1**（虚拟环境记录的基础解释器） |
| 虚拟环境 | `.venv\`（Windows）/ `.venv/`（Linux） |
| 运行依赖 | `requirements.txt`（PySide6-Essentials、paho-mqtt、opencv-python、cryptography） |
| 开发依赖 | `requirements-dev.txt`（pytest、ruff） |
| 服务端依赖 | `requirements-server.txt`（Linux/Docker，不含 Qt） |

> ⚠️ **`.venv` 不可搬迁、不可复制**。`pyvenv.cfg` 里写死了基础解释器的绝对路径。
> 本项目历史上从 `D:\DSH\bambu-monitor` 搬到 `L:\DSH\bambu-monitor`，又遇上
> `D:\python` 被删除，导致 `.venv\Scripts\python.exe` **文件存在但一启动就报
> `did not find executable at 'D:\python\python.exe'`（退出码 103）**。
> `run.bat` 用 `if not exist` 判断，这种情况**不会**触发自动重建。
> 判断环境是否可用，用这个（比 `if exist` 可靠）：
>
> ```powershell
> .venv\Scripts\python.exe -c "import sys; print(sys.version)"
> ```
>
> 真的坏了就删掉 `.venv` 重跑 `run.bat`。

常见陷阱：本机 PATH 上的 `python` / `python3` 是 Windows 应用商店的**占位符**
（0 字节的转发器，执行后没有输出或直接失败），`py` 启动器也可能不存在。
**所有命令都显式写 `.venv\Scripts\python.exe`**，不要依赖 `python`。

### 1.2 从零建立

```bat
:: Windows
run.bat                  :: 自动建 .venv + 装 requirements.txt，然后启动界面
.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
```

```bash
# Linux / Docker / NAS
bash linux/install.sh              # 服务端依赖（不含 Qt）
bash linux/install.sh --with-gui   # 另外装 PySide6，可跑图形界面
```

## 2. 跑起来

| 目的 | 命令 |
| --- | --- |
| 正常使用（真实打印机） | `run.bat` 或 `.venv\Scripts\python.exe -m app` |
| **无真机开发/演示** | `demo.bat`（= `-m app --sim`，4 台虚拟打印机 + 界面） |
| 无界面端到端自检 | `selftest.bat`（= `-m app --core-test`） |
| 只跑模拟器（供别的程序连） | `sim.bat`（= `-m app.sim --count 4`） |
| Linux/Docker 服务模式 | `.venv\Scripts\python.exe -m app.headless --sim 4 --port 8080` |
| 界面自检（打印每路绑定结果） | `.venv\Scripts\python.exe -m app --sim --ui-selftest` |
| 自动截图 | `.venv\Scripts\python.exe -m app --sim --screenshot shot.png --exit-after 12` |
| 打包 exe | `build_exe.bat` → `dist\BambuMonitor\BambuMonitor.exe` |

无头环境（没有显示器 / CI）请设 `QT_QPA_PLATFORM=offscreen`，
否则 Qt 会因为拿不到窗口系统而直接终止进程。

## 3. 测试

```bat
test.bat                     :: = .venv\Scripts\python.exe -m pytest
test.bat -k web              :: 只跑名字含 web 的用例
test.bat -x                  :: 第一个失败就停
set BAMBU_RUN_SLOW=1 && test.bat -m slow    :: 端到端慢测试（内置模拟器）
```

```bash
bash linux/run-tests.sh
```

* 配置在 `pyproject.toml` 的 `[tool.pytest.ini_options]`。
* `tests/local_config.py` 是注册过的 pytest 插件，提供两个夹具用来**避免测试
  读写用户真实的 `%APPDATA%\BambuMonitor` 配置**：`isolated_config_dir`（走 tmp_path）、
  `no_real_config`。写新测试时请优先用它们。
* 慢测试默认跳过（用 `BAMBU_RUN_SLOW=1` 打开），因为它们要真的起模拟器、
  占 8883/6000 端口。它们是最有价值的测试——能离线验证「搜索 → 遥测 → 画面」全链路。
* `app/selftest.py`（`--core-test`）是独立于 pytest 的端到端冒烟，返回标准退出码，
  也可以直接进 CI。

## 4. 静态检查

```powershell
.venv\Scripts\python.exe -m ruff check app tools tests
.venv\Scripts\python.exe -m ruff check --fix app tools tests   # 自动修 import 排序等
```

`pyproject.toml` 里只开了低噪音规则（`E4/E7/E9/F/I`）：语法错误、未定义名字、
未使用导入、import 顺序。**不追求全量 lint 合规**，重点是不让低级错误进主干。

## 5. 调试手段（`tools/` 目录，33 个脚本）

日常最常用的几个：

| 脚本 | 用途 |
| --- | --- |
| `tools/diagnose.py "IP 访问代码"` | 对**真机**逐项体检：8883/6000/322 端口、TLS、遥测、画面 |
| `tools/probe6000.py "IP 访问代码"` | 单独验证 6000 端口鉴权与帧格式 |
| `tools/tls_matrix.py "IP"` | 四种 TLS 组合逐个试，定位握手问题 |
| `tools/rtsp_describe.py "IP 访问代码"` | RTSPS 的 DESCRIBE 交互 |
| `tools/raw_fields.py "IP 访问代码"` | 打印打印机上报的**原始字段**（找新字段时用） |
| `tools/web_check.py` | 网页服务端到端（401/首页/单帧/MJPEG） |
| `tools/control_cli_check.py` | 命令行控制指令（pause/resume/stop/light/speed） |
| `tools/headless_check.py` | 无界面模式（伪造无 Qt 环境） |
| `tools/layout_fit_check.py` | 画面装箱布局（重点画面 2×2 等） |
| `tools/perf_check.py` / `tools/decode_bench.py` | 帧率与解码开销基准 |

完整清单见 [`../tools/README.md`](../tools/README.md)。这些脚本的约定是
「打印过程 + 返回 0/1 退出码」，不是单元测试——它们会真的连设备。

## 6. 改代码的约定

1. **协议层不许 import Qt**（`app/bambu/`、`app/web/`）。`web/server.py` 只在转码时
   延迟 import PySide6 —— 无 Qt 的 Docker 要能跑起来。OpenCV 同理，一律延迟 import。
2. **线程必须可回收**：新建常驻线程时，把句柄存起来，并在对应的 `stop()` 里
   set 事件 + join（带超时）。见 `ARCHITECTURE.md` 第 2 节的线程表。
   `join()` 之前先判断 `is_alive()`——对从未 `start()` 的线程 join 会抛 `RuntimeError`。
3. **锁的顺序**：`_video_setup_lock` → `_stream_lock`，不要反向获取。
4. **不要静默吞异常**：`except Exception: pass` 只允许用在「确实无关紧要」的地方
   （画时间戳、关 socket、清理回调）。涉及**落盘、凭据、网络请求结果**的失败必须
   记录（日志 + `last_error` 之类的可见状态），否则用户会看到「成功」的假象。
   现有的反例与已修正案例见 `KNOWN_ISSUES.md`。
5. **中文文案统一用 UTF-8**；命令行输出不要依赖本地编码（入口已调用
   `app.util.configure_stdio()`，新入口也要调用）。
6. 新增机型/字段/指令时，请对照 `ARCHITECTURE.md` 最后一节的「牵连清单」逐项同步。
7. 提交信息用中文描述**为什么**改，不要只写「更新代码」。

## 7. 配置文件与日志位置

| 内容 | 路径 |
| --- | --- |
| 配置（含加密访问代码） | `%APPDATA%\BambuMonitor\config.json` |
| 自动备份 | `%APPDATA%\BambuMonitor\config.backup.json` |
| 日志 | `%APPDATA%\BambuMonitor\logs\app.log`（滚动，1MB × 3） |
| 模拟器证书与截图 | `.sim\`（已被 git 忽略） |
| 抓拍输出 | 「图片」目录下的 `BambuMonitor` |

Linux/Docker 用环境变量 `BAMBU_MONITOR_CONFIG_DIR` 把配置目录指到 `./data`。
可以用 `tools/show_config.py` 查看当前配置，`tools/export_plain_config.py` 导出明文
（仅在需要迁移到 Linux 时用，注意别把明文口令提交进仓库）。
