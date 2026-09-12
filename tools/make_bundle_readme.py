"""生成交付包（dist-all/）里的 README.md。

由 `make-bundle.ps1` 调用：

    python tools/make_bundle_readme.py <manifest.json> <out.md> <构建时间>

为什么单独放一个 .py 而不是写在 PowerShell 的 here-string 里：
**PowerShell 把反引号当转义字符**，Markdown 的代码围栏（三个反引号）会被吃掉，
产出的 README 没有代码块；用 here-string 传 Python 代码还容易在引号/换行上
出岔子。放到独立文件里这些问题一次都没有。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

#: manifest.json 里每一项的字段
FIELDS = ("Label", "File", "SizeMB")


def build_readme(items: list[dict], stamp: str) -> str:
    """按归集到的产物清单渲染 README。"""
    if not items:
        rows = "| （本次没有归集到任何产物） | - | - |"
    else:
        rows = "\n".join(
            "| {Label} | `{File}` | {SizeMB} MB |".format(
                Label=item.get("Label", "?"),
                File=(item.get("File") or "").replace("\\", "/"),
                SizeMB=item.get("SizeMB", "?"),
            )
            for item in items
        )

    return f"""# 拓竹打印机监控台 · 交付包

构建时间：{stamp}

## 包含的产物

| 版本 | 文件 | 大小 |
|---|---|---|
{rows}

## 各版本怎么用

### Windows（`windows/BambuMonitor.exe`）

双击即可运行，已自带 Python 运行时，**不需要**装 Python。
首次运行会生成配置目录（`%APPDATA%\\BambuMonitor`）。
杀毒软件可能对未签名的一次性打包 exe 报警，属正常现象。

### Linux（`linux/BambuMonitor-headless`，无界面服务版）

```
chmod +x BambuMonitor-headless
./BambuMonitor-headless --host 0.0.0.0 --port 8080
```

然后浏览器打开 `http://<这台机器的IP>:8080`。
带界面的是 `linux/BambuMonitor-linux-gui`（需要图形环境与 Qt 依赖库）。

### Docker（`docker/bambu-monitor-image.tar.gz`）

```
docker load -i bambu-monitor-image.tar.gz
docker run -d --name bambu-monitor --network host \\
  -v bambu-config:/config bambu-monitor:latest
```

`--network host` 是必须的：打印机发现（SSDP 广播）与视频流都要求容器
能直接看到局域网；桥接网络下搜不到打印机。

### 安卓（`android/BambuMonitor-arm64.apk`）

把 APK 传到平板/手机上安装（需要允许"未知来源"）。

应用**内置了完整的 Python 运行时**，在设备本机跑 HTTP 服务，界面用 WebView
打开，所以平板可以**完全脱离电脑独立使用**；又因为服务绑在 `0.0.0.0`，
同一 Wi-Fi 下的其它手机/电脑也能访问这台平板的页面。

* 架构：arm64-v8a（当前绝大多数平板与手机）
* 系统：Android 7.0+（API 24）
* 包内已含 OpenCV，支持 X1 / X2D / H2 / P2S 的 RTSPS 高清通道
* 这是 **debug 签名**的包，用于自用安装；上架应用商店需要换正式签名

## 全部版本共用的功能

各版本共用同一套 Python 内核（同一个 `app/` 目录），因此功能一致：
设备管理（自动发现 / 手动添加 / 改名 / 删除）、暂停 / 继续 / 停止、
腔体灯开关、打印速度、摄像头画面与帧率、HMS 错误码中文解释、
多台打印机并排监控、第三方设备族（Klipper / Moonraker，如 Snapmaker U1）。
**网页端是各平台统一的操作界面。**

## 校验

`SHA256SUMS.txt` 是无 BOM 的 UTF-8，可直接给 Linux 用：

```
# Linux / macOS
sha256sum -c SHA256SUMS.txt
# Windows PowerShell
Get-FileHash .\\android\\BambuMonitor-arm64.apk -Algorithm SHA256
```
"""


def main(argv: list[str]) -> int:
    if len(argv) != 4:
        print(__doc__, file=sys.stderr)
        return 2
    manifest_path, out_path, stamp = argv[1], argv[2], argv[3]

    # PowerShell 的 Set-Content -Encoding UTF8 会写 BOM，用 utf-8-sig 吃掉
    raw = Path(manifest_path).read_text(encoding="utf-8-sig")
    items = json.loads(raw)
    if isinstance(items, dict):  # 只有一个产物时 ConvertTo-Json 返回对象而非数组
        items = [items]

    for item in items:
        for field in FIELDS:
            item.setdefault(field, "")

    Path(out_path).write_text(build_readme(items, stamp), encoding="utf-8", newline="\n")
    print(f"  已生成说明文件：{out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
