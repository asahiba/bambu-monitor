# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 单文件打包配置（**Linux 带界面版**）。

与 `BambuMonitor-onefile.spec`（Windows）基本一致，差异只有两点：

1. 产物名带 `-linux` 后缀，避免与 Windows 产物混淆；
2. 保留控制台（`console=True`）——Linux 用户习惯从终端启动，
   且能直接看到日志与 Ctrl+C 退出。

⚠️ 构建必须在 Linux 上进行（PyInstaller 不能跨平台编译）。
本仓库提供两条路径：

* `./linux/build-onefile.sh`         —— 在 Linux 机器 / WSL 里直接构建
* `./linux/build-onefile-docker.sh`  —— 用 Docker 容器构建（宿主机不需要 Linux）
"""

import os

from PyInstaller.utils.hooks import collect_submodules

block_cipher = None

HIDDEN = [
    "paho.mqtt.client",
    "paho.mqtt.publish",
    "cv2",
    "cryptography",
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
    name="BambuMonitor-linux",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)
