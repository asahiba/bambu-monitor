# 部署到 Linux / Docker / 安卓

同一个内核，三种跑法：**Windows 桌面版**、**Linux / Docker 无界面服务版**、**安卓（网页 PWA）**。
它们的协议实现、网页前端完全一致，只是运行方式不同。

---

## 1. Linux

### 1.1 直接跑（推荐：服务器 / NAS / 树莓派）

```bash
git clone <本项目> bambu-monitor && cd bambu-monitor
chmod +x linux/*.sh                # 从 Windows 拷贝过来时可能需要
bash linux/install.sh              # 建虚拟环境 + 装服务端依赖（不含 Qt，体积小）
bash linux/run-headless.sh         # 启动无界面服务，默认 http://<本机IP>:8080
```

想要**图形界面**（Linux 桌面环境）：

```bash
bash linux/install.sh --with-gui   # 额外安装 PySide6
./.venv/bin/python -m app          # 与 Windows 版完全一致的界面
```

一键冒烟自检（会在 `~/bambu-linux-test` 里跑 4 台模拟打印机并检查接口）：

```bash
bash linux/wsl-test.sh
```

### 1.2 首次配置打印机

```bash
./.venv/bin/python -m app.headless --discover
./.venv/bin/python -m app.headless --add-printer "X2D 192.168.31.110 12345678"
./.venv/bin/python -m app.headless --list
```

控制指令（暂停 / 继续 / 停止 / 开关灯 / 速度档位）：

```bash
./.venv/bin/python -m app.headless --control pause  --target 192.168.31.110
./.venv/bin/python -m app.headless --control light  --value off --target all
./.venv/bin/python -m app.headless --control speed  --value 3 --target 192.168.31.110
./.venv/bin/python -m app.headless --control stop   --target 192.168.31.110 --yes
```

> `stop` 不可恢复，必须显式加 `--yes`；遥测未连接时不会下发任何指令。

配置文件默认在 `./data/config.json`（可用环境变量 `BAMBU_MONITOR_CONFIG_DIR` 改），
**Linux 上没有 Windows 的 DPAPI**，访问代码会以明文保存在该文件里，请注意目录权限：

```bash
chmod 700 ./data && chmod 600 ./data/config.json
```

### 1.3 开机自启（systemd）

```bash
sudo mkdir -p /opt/bambu-monitor
sudo cp -r . /opt/bambu-monitor/
sudo cp linux/bambu-monitor.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now bambu-monitor
journalctl -u bambu-monitor -f
```

### 1.4 打包成单个可执行文件

在 Linux 机器上执行（PyInstaller 不能跨平台编译）：

```bash
./linux/build-linux.sh             # 产物 dist/BambuMonitor（约 90 MB，目标机无需装 Python）
```

### 1.5 环境变量

| 变量 | 作用 | 默认 |
| --- | --- | --- |
| `BAMBU_MONITOR_CONFIG_DIR` | 配置目录 | `%APPDATA%\BambuMonitor` / `./data` |
| `BAMBU_WEB_PORT` | 网页端口 | `8080` |
| `BAMBU_TOKEN` | 网页访问令牌 | 配置文件里的随机令牌 |
| `BAMBU_WEB_FPS` | 网页帧率 | `4` |
| `BAMBU_MAX_FPS` | 每路画面最大帧率 | `10` |

---

## 2. Docker

### 2.1 一键启动

```bash
docker compose up -d
docker compose logs -f
```

`docker-compose.yml` 默认使用 **host 网络**：局域网的自动搜索（SSDP 组播 + 2021 广播）
与手机直连都依赖宿主网络，桥接网络下搜不到打印机。

### 2.2 手工运行

```bash
docker build -t bambu-monitor .
docker run -d --name bambu-monitor \
  --network host \
  -v "$PWD/data:/data" \
  -e BAMBU_WEB_FPS=4 -e BAMBU_MAX_FPS=10 \
  bambu-monitor
```

### 2.3 容器内管理打印机

```bash
docker exec -it bambu-monitor python -m app.headless --discover
docker exec -it bambu-monitor python -m app.headless --add-printer "X2D 192.168.31.110 12345678"
docker exec -it bambu-monitor python -m app.headless --list
```

> **首次启动没有打印机时会怎样**：服务**照常启动**，监控墙为空，网页可以打开。
> 这是刻意设计——`docker-compose.yml` 里配的是 `restart: unless-stopped`，
> 若空配置就退出，容器会陷入"重启 → 又没配置 → 再退出"的无限循环，
> 用户既看不到任何有效信息，也没有途径自助添加打印机。
> 现在只需打开网页（或按上面的命令加设备）即可。

### 2.4 没有打印机也能验证（演示模式）

```bash
docker run --rm -p 8080:8080 bambu-monitor python -m app.headless --sim 4 --status-interval 0
# 浏览器打开 http://127.0.0.1:8080  →  4 台虚拟打印机，画面/进度/温度齐全
```

### 2.5 单文件镜像（离线部署）

如果目标机不能联网拉依赖，可以把整个镜像打包成一个文件：

```powershell
# 在构建机上（Windows）
powershell -ExecutionPolicy Bypass -File build-docker-image.ps1
# 产物：dist-docker\bambu-monitor-latest-image.tar.gz（约 132 MB）
```

```bash
# 在目标机上
docker load -i bambu-monitor-latest-image.tar.gz
docker run -d --name bambu-monitor --network host -v $PWD/data:/data bambu-monitor:latest
```

该脚本会先起容器跑 `/health` 健康检查，确认镜像真的能用，再导出。

### 2.5 Windows / macOS 上的 Docker Desktop（实测结论）

在 Windows 的 Docker Desktop 上实测：

| 项目 | 结果 |
| --- | --- |
| 容器**出网**访问局域网打印机 | ✓ 正常（容器能直连 192.168.31.x 的 8883/6000/322，实测 13 台真机全部在线） |
| `network_mode: host` 下的**端口映射** | ✗ 不可用 —— Docker Desktop 的 "host" 是那台 Linux 虚拟机，宿主机访问不到容器端口 |
| 桥接 + `-p 8080:8080` | ✓ 网页可访问，打印机按 IP 直连也正常；**只有自动搜索会失效**（广播/组播被 NAT 隔离） |

因此：

* **Windows / macOS**：推荐「桥接 + 手动添加 IP」（`docker-compose.yml` 末尾有现成配置），
  功能上只少了自动搜索；
* **真正的 Linux 宿主机**：`network_mode: host` 完全可用，自动搜索与端口都正常（推荐）。

### 2.6 健康检查

```bash
docker inspect --format '{{.State.Health.Status}}' bambu-monitor
curl http://127.0.0.1:8080/health      # -> ok
```

### 2.7 实测记录（本机 Docker Desktop + 13 台真机）

```
③ /api/printers -> 共 13 台 · 画面在线 13 · 遥测在线 13 · 打印中 8
   · X2D    RTSPS   10.2fps  76%→100%  喷嘴 231/230℃ 热床 55/55℃
   · X1C    RTSPS   10.0fps
   · p1s×7 / A1×2 / A1 mini×2   TCP6000 约 0.4-0.9fps（硬件限制）
④ 单连接多路复用：13 路画面全部通过一条连接推送
   PWA 资源（manifest / 图标 / sw.js）全部 200
```

### 2.8 容器内也能跑演示模式（无需任何打印机）

```bash
docker run --rm -p 8080:8080 bambu-monitor python -m app.headless --sim 4 --status-interval 0
```

镜像里没有 Qt，模拟画面会自动改用 OpenCV 绘制，效果一致。

---

## 3. 安卓（PWA：加到手机主屏，全屏运行）

网页前端已升级为 **PWA**，安卓手机无需安装 APK：

1. 手机连上与电脑/服务器**同一个 Wi-Fi**。
2. Chrome 打开软件给出的网页地址（带 `?token=...`）。
3. 右上角「⋮」→ **添加到主屏幕 / 安装应用**（页面顶栏也会出现「安装到桌面」按钮）。
4. 从主屏图标启动即全屏运行，状态栏隐藏，和原生 App 观感一致；
   布局会自动适配竖屏（单列），重点画面跨格也照常生效。

> 局域网是 `http://`，浏览器会跳过 Service Worker（需要 HTTPS），但**不影响安装与使用**；
> 若想部署到公网并启用完整 PWA 能力，请放在 HTTPS 反向代理后面（如 Caddy/Nginx + 自签或 Let's Encrypt 证书）。

### 想要真正的 APK？

两条路，按需选择：

1. **WebView 壳**（最省事，1 小时）：用 Android Studio 建一个空项目，
   放一个全屏 `WebView` 加载你的监控地址（`settings.javaScriptEnabled = true`），
   打包 APK 即可。适合内部分发。
2. **Kivy / python-for-android**（纯 Python 打包）：本项目的协议层不依赖 Qt，
   只保留 `--headless` 内核即可，但需要 Linux 构建机 + Android SDK/NDK（约 10 GB），
   构建一次 30 分钟以上。项目已提供无界面入口 `app/headless.py` 作为内核，
   `linux/build-linux.sh` 可作为打包脚本的参考。

> 说明：APK 需要 Android SDK/NDK 工具链，本项目未附带预编译 APK；
> PWA 路线是当前验证过的安卓方案（见下）。

---

## 4. 三种方式的功能对照

| 功能 | Windows 桌面版 | Linux/Docker 无界面版 | 安卓 PWA |
| --- | --- | --- | --- |
| 实时画面（6000 端口 / RTSPS） | ✓ | ✓ | ✓ |
| 进度 / 喷嘴 / 热床 / 层数 / 任务名 | ✓ | ✓ | ✓ |
| 局域网自动搜索 | ✓ | ✓（需 host 网络/同网段） | —（由服务端搜索） |
| 画面顺序与大小（重点画面） | ✓（鼠标调整） | ✓（桌面端调整后同步） | ✓（跟随服务端配置） |
| 多画面实时观看上限 | 不限（原生渲染） | —（看不到画面，只做服务端） | **单连接多路复用，不受 6 连接限制** |
| 网页访问令牌 | ✓ | ✓ | ✓ |
| 配置保存/导出/导入 | ✓ | 命令行 `--discover/--add-printer` | — |
