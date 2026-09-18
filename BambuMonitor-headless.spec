# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 单文件打包配置（**无界面服务版**，用于 Linux / Docker）。

与 `BambuMonitor-onefile.spec` 的区别：

* **不打包 PySide6**：Linux 部署的既定形态就是无界面服务（`app/headless.py` +
  网页 + PWA），见 `docs/DEPLOY.md`。省掉 Qt 后产物体积大幅下降，
  也不再需要目标机装一堆图形系统库（libGL / xcb 等），
  在服务器、NAS、树莓派类环境里更容易跑起来。
* **带控制台**：服务模式需要看日志与 Ctrl+C 退出。

因此产物通过 `--headless` 运行::

    ./BambuMonitor-headless --headless --port 8080

⚠️ 由于不含 Qt，它**不能**打开桌面窗口；要图形界面请用 Windows 版，
或在 Linux 上按 `docs/DEPLOY.md` 用源码 + PySide6 运行。
"""

import os

block_cipher = None

HIDDEN = [
    "paho.mqtt.client",
    "paho.mqtt.publish",
    # ⚠️ cv2 与 cryptography 都是**函数内导入**的可选依赖（RTSPS 拉流 / 模拟器自签证书）。
    # PyInstaller 的静态分析通常能发现函数内 import，但这里显式列出更保险：
    # 缺 cv2 的后果是「X1/X2D/H2/P2S 这些只有 RTSPS 通道的机型看不到画面」，
    # 而 Linux 服务器正是这类用户的常见部署方式，值得多写两行。
    "cv2",
    "cryptography",
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

DATAS = [
    ("app/bambu/certs", "app/bambu/certs"),
    ("app/bambu/data", "app/bambu/data"),
]

analysis = Analysis(
    ["run_headless.py"],
    pathex=[os.path.abspath(".")],
    binaries=[],
    datas=DATAS,
    hiddenimports=HIDDEN,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # 无界面版不需要 Qt，也不算模拟器的 Qt 绘制路径（会自动退回 OpenCV）
        "PySide6",
        "shiboken6",
        "tkinter",
        "unittest",
        "pydoc_data",
        "app.ui",
        # ⚠️ 不要排除 app.web.icons：app/web/server.py 顶层就 `from .icons import
        # icon_bytes`（PWA 图标），排掉会让服务在启动时直接
        # ModuleNotFoundError: No module named 'app.web.icons'。
        # 它只有几十 KB（base64 图标），留着不值得省。
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
    name="BambuMonitor-headless",
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
