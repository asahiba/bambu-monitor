# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller **目录式**（onedir）打包配置 —— 遗留方式，保留作兼容。

⚠️ 推荐用 `BambuMonitor-onefile.spec`（单文件，见 `build-onefile.bat`）。
本文件产出 `dist/BambuMonitor/` 整个目录，必须连同 `_internal` 一起拷贝。

## 这份配置曾经与单文件版**不一致**

它只列了 `paho.mqtt.client` / `cv2` 两个 hiddenimport，没有 `app.core.*` 与
`app.adapters.*` —— 那部分设备族适配器是**运行时按族动态导入**的，
PyInstaller 的静态分析可能漏掉，于是"目录版能用、单文件版也能用，
但目录版接第三方设备族会 ImportError"这种只在特定场景炸的差异。

现在两边的 HIDDEN/excludes 列表保持一致（单文件版是参考实现），
并且 `build_exe.bat` 也改成直接用本 spec —— 以前它是把参数**写在 bat 里**的，
于是同一件事有两处定义，改一处忘一处就是上面那种症状。
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

a = Analysis(
    ['run_app.py'],
    pathex=[os.path.abspath(".")],
    binaries=[],
    datas=DATAS,
    hiddenimports=HIDDEN,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # 与单文件版一致的瘦身列表（用不到的重型模块）
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
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='BambuMonitor',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,  # UPX 常被杀软误报
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='BambuMonitor',
)
