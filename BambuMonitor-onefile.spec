# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 单文件打包配置。

一次产出**两个**单文件可执行文件：

| 产物 | 引导器 | 用途 |
| --- | --- | --- |
| `BambuMonitor.exe` | windowed | 双击启动图形界面（无控制台窗口） |
| `BambuMonitor-cli.exe` | console | 命令行：`--version` / `--core-test` / `--headless` |

## 为什么要两个

打包成 `--windowed` 后**根本没有控制台句柄**，`sys.stdout/stderr` 为 `None`，
所以 ``BambuMonitor.exe --version`` 不会有任何输出 ——
不只是"看不到"，而是重定向/管道拿到的也是空的
（`> log.txt`、`| more`、CI 里捕获输出，全都是空文件）。

这一点曾经坑过很久：CI 的冒烟验证跑 ``BambuMonitor.exe --version``
拿到空字符串却看到进程正常结束，一度被当成"退出码取不到"的 PowerShell 怪癖。
真相是 windowed 引导器（`runw.exe`）压根不接控制台。

所以命令行用法一律用 `BambuMonitor-cli.exe`：

* `BambuMonitor-cli.exe --version`
* `BambuMonitor-cli.exe --core-test`（模拟器全链路自检，会打印结果）
* `BambuMonitor-cli.exe --headless`（无界面服务；输出也会进日志文件
  `%APPDATA%\\BambuMonitor\\logs\\app.log`）

两个产物共用同一份 `Analysis`/`PYZ`（只是 EXE 引导器不同），
所以打两个几乎不增加构建时间，只是各打一个包。

## 数据文件为什么要打进去

* `app/bambu/certs`  —— 打印机证书链校验用的内置 CA
* `app/bambu/data`   —— HMS 错误码中英文案表（缺失会退化成只显示错误码）

`app/core` 与 `app/adapters` 是动态导入的（设备族注册表按需 import），
PyInstaller 的静态分析可能漏掉，因此显式列进 `hiddenimports`。
"""

import os

from PyInstaller.utils.hooks import collect_submodules

block_cipher = None

#: 运行依赖：opencv 与 paho 都是可选导入，静态分析未必发现
HIDDEN = [
    "paho.mqtt.client",
    "paho.mqtt.publish",
    "cv2",
    # 设备无关内核与第三方设备族适配器（运行时按族动态导入）
    "app.core",
    "app.core.capabilities",
    "app.core.adapter",
    "app.core.device",
    "app.core.registry",
    "app.adapters",
    "app.adapters.moonraker",
    "app.adapters.moonraker.adapter",
    "app.adapters.moonraker.ws",
]
HIDDEN += collect_submodules("app.bambu")

DATAS = [
    ("app/bambu/certs", "app/bambu/certs"),
    ("app/bambu/data", "app/bambu/data"),
]

analysis = Analysis(
    ["run_app.py"],
    pathex=[os.path.abspath(".")],
    binaries=[],
    datas=DATAS,
    hiddenimports=HIDDEN,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # 排除明显用不到的重型模块，给单文件体积瘦身
    excludes=[
        "tkinter",
        "unittest",
        "pydoc_data",
        "PySide6.QtWebEngineCore",
        "PySide6.QtWebEngineWidgets",
        "PySide6.Qt3DCore",
        "PySide6.QtMultimedia",
        "PySide6.QtQuick",
        "PySide6.QtQml",
        "PySide6.QtDesigner",
        "PySide6.QtTest",
    ],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(analysis.pure, analysis.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    analysis.scripts,
    analysis.binaries,
    analysis.datas,
    [],
    name="BambuMonitor",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,  # UPX 常被杀软误报，单文件模式尤其明显，因此关闭
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,  # 无控制台窗口（双击启动图形界面用）
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)

#: 命令行版：同一份 Analysis，只是换成 console 引导器。
#: 没有它的话 `--version` / `--core-test` 这类命令看不到任何输出（见文件头说明）。
exe_cli = EXE(
    pyz,
    analysis.scripts,
    analysis.binaries,
    analysis.datas,
    [],
    name="BambuMonitor-cli",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,  # 关键：有控制台句柄，标准输出可被重定向/捕获
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)
