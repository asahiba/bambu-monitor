# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 单文件打包配置。

产物是**单个可执行文件**（Windows: `BambuMonitor.exe`，Linux: `BambuMonitor`），
启动时把自身解包到临时目录再运行 —— 用户拿到一个文件就能跑，不需要装 Python。

## 为什么用 windowed（无控制台）

打包成 `--windowed` 后 `sys.stdout/stderr` 为 `None`，因此 `app/main.py` 里
有专门的兜底（把 print 重定向到空写入器）。**代价是 `--help` / `--core-test`
这些命令行输出看不到**。为此：

* CLI 用法请用打包时同时产出的**命令行版**（见 `build_packaging.py` 里的
  ``console`` 目标），或直接用源码运行；
* 无界面服务模式（`--headless`）的输出会写进日志文件
  （`%APPDATA%\\BambuMonitor\\logs\\app.log`）。

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
    console=False,  # 无控制台窗口；CLI 输出见文件头的说明
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)
