"""安卓构建的契约测试：入口模块、原生依赖、签名、图标。

这几条全部对应**真实发生过、而且在真机上才炸**的问题，
所以放在契约测试里，每次改安卓构建时都会被检查。

| 曾经的问题 | 症状 | 这里的护栏 |
| --- | --- | --- |
| Python 入口模块叫 `bootstrap`，与 Chaquopy 自己的 `bootstrap.imy` 撞名 | `ModuleNotFoundError: No module named 'bootstrap'` | 模块名不得与 Chaquopy 保留名冲突 |
| 装了 opencv/numpy/cryptography，wheel 是 4096 字节对齐 | 16 KB 内存页设备（新 ARM Chromebook）**闪退** | pip 段不得安装预编译原生包 |
| 一直用 debug 签名 | 换签名后无法覆盖安装（"应用未安装"） | 必须配置固定签名 + 密钥不得入库 |
| 缺 `mipmap-anydpi-v26` | Android 8+ 桌面图标显示空白 | 必须有自适应图标 |
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from tests.test_contracts import PROJECT_ROOT

ANDROID_DIR = PROJECT_ROOT / "android"
APP_DIR = ANDROID_DIR / "app"
GRADLE = APP_DIR / "build.gradle"
PY_SRC = APP_DIR / "src" / "main" / "python"
RES = APP_DIR / "src" / "main" / "res"
JAVA_DIR = APP_DIR / "src" / "main" / "java" / "com" / "bambumonitor"

#: Chaquopy 自己占用的资产名。用户模块撞上这些名字会被**静默丢弃**
#: （Chaquopy 的 bootstrap.imy 装它自己的 java 桥，用户那份就不见了）。
#: 见 Chaquopy 的 assets/chaquopy/ 目录：bootstrap.imy / bootstrap-native/。
CHAQUOPY_RESERVED = {"bootstrap", "bootstrap-native", "stdlib", "requirements", "app", "java"}


@pytest.fixture(scope="module")
def gradle_text() -> str:
    assert GRADLE.is_file(), f"找不到 {GRADLE}"
    return GRADLE.read_text(encoding="utf-8-sig")


def test_入口模块名不得与Chaquopy保留名冲突():
    """**核心回归**：安卓侧的 Python 入口模块不能叫 `bootstrap`。

    踩过的坑：入口模块原名 `bootstrap.py`，而 Chaquopy 自己会生成
    `assets/chaquopy/bootstrap.imy`（装它的 `java` 桥）。两者撞名后，
    用户那份被**静默丢弃** —— 本地增量构建有时还能带上（缓存里留着旧产物），
    CI 干净构建则稳定缺失，于是真机上启动就报
    `ModuleNotFoundError: No module named 'bootstrap'`。

    现在改名 `device_server.py`，并在这里锁住：既不能撞保留名，
    也要与 Java 侧的 `getModule(...)` 调用一致。
    """
    entries = {p.stem for p in PY_SRC.glob("*.py")}
    assert entries, f"{PY_SRC} 下没有 Python 入口文件"

    clash = entries & CHAQUOPY_RESERVED
    assert not clash, (
        f"入口模块名与 Chaquopy 保留名冲突，会被静默丢弃：{sorted(clash)}。"
        "改名（例如 device_server.py）并同步 Java 侧的 getModule 调用。"
    )

    # Java 侧调用的模块名必须真实存在
    java_text = "\n".join(p.read_text(encoding="utf-8") for p in JAVA_DIR.glob("*.java"))
    called = set(re.findall(r'getModule\("([^"]+)"\)', java_text))
    assert called, "Java 侧没有调用 getModule —— 服务起不来"
    for name in called:
        assert (PY_SRC / f"{name}.py").is_file() or (PY_SRC / name).is_dir(), (
            f"Java 调用了 getModule(\"{name}\")，但 {PY_SRC} 下没有对应模块"
        )


def test_安卓不得安装4096对齐的原生包(gradle_text):
    """**核心回归**：pip 段只能装纯 Python 包。

    Chaquopy 仓库里 opencv-python-headless / numpy / cryptography 的预编译
    wheel 是 **4096 字节对齐**的，而 16 KB 内存页的设备（新 ARM Chromebook、
    Pixel 8+ 等）**拒绝加载**这类库 —— Python 启动时导入它们会抛 Java 异常，
    用户看到的是**闪退**。

    Chaquopy 17.0.0 发布说明原文：
    "any Android wheels built before October 2024 will still fail to load on
     16 KB devices."

    这三项在代码里都是可选的（opencv 缺失 -> 退回 6000 端口 JPEG；
    cryptography 只给模拟器）。所以**不要**把它们加回来，
    除非确认有了 16 KB 对齐的 wheel。
    """
    match = re.search(r"^\s*pip \{.*?^\s*\}", gradle_text, re.S | re.M)
    assert match is not None, "找不到 pip 段"
    pip_block = match.group(0)

    # 只取真正 install 的包名（去掉注释行）
    installed = re.findall(r'^\s*install\s+"([^"]+)"', pip_block, re.M)
    assert "paho-mqtt" in installed, "paho-mqtt 是纯 Python 包，应当保留"

    forbidden = {"opencv-python-headless", "opencv-python", "numpy", "cryptography", "cffi"}
    bad = forbidden & set(installed)
    assert not bad, (
        f"安卓构建又装上了 4096 对齐的原生包：{sorted(bad)}。\n"
        "它们在 16 KB 内存页设备上会导致**闪退**（详见本测试的 docstring）。"
    )


def test_必须配置固定签名且密钥不得入库(gradle_text):
    """契约：debug 与 release 共用固定签名，密钥文件必须被 git 忽略。

    一直用 debug 签名会让用户**无法覆盖安装**：debug keystore 每台机器/
    每次 CI 都不同，同一个包名换签名安装时系统直接拒绝（"应用未安装"），
    用户只能卸载重装、配置全丢。
    """
    assert "signingConfigs" in gradle_text, "没有配置 signingConfigs"
    assert "hasReleaseSigning" in gradle_text, "没有读取固定签名配置"
    assert re.search(r"debug \{.*?signingConfig signingConfigs\.release", gradle_text, re.S), (
        "debug 构建也必须用固定签名，否则装过一版后就覆盖不上了"
    )

    # 密钥绝不能入库
    gitignore = (PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8")
    for pattern in (".android-signing", "keystore.properties"):
        assert pattern in gitignore, f".gitignore 里缺少 {pattern}（密钥会泄露）"


def test_必须有自适应图标():
    """契约：Android 8+ 需要 `mipmap-anydpi-v26` 的自适应图标。

    只给位图 `mipmap-*/ic_launcher.png` 时，Android 8+ 会按自适应图标处理，
    找不到 anydpi-v26 定义就**显示空白/默认方块** —— 用户以为"图标丢了"。
    """
    adaptive = RES / "mipmap-anydpi-v26" / "ic_launcher.xml"
    assert adaptive.is_file(), (
        "缺少 mipmap-anydpi-v26/ic_launcher.xml —— Android 8+ 桌面图标会显示空白"
    )

    root = ET.parse(adaptive).getroot()
    assert root.tag == "adaptive-icon", f"根节点应当是 adaptive-icon，实际 {root.tag}"
    ns = "{http://schemas.android.com/apk/res/android}"
    children = {child.tag for child in root}
    assert "background" in children and "foreground" in children, (
        "自适应图标必须同时有 background 与 foreground"
    )
    for child in root:
        assert child.get(ns + "drawable"), f"{child.tag} 缺少 android:drawable"

    # 引用的资源要真的存在
    colors = (RES / "values" / "colors.xml").read_text(encoding="utf-8")
    assert "ic_launcher_background" in colors, (
        "自适应图标引用了 @color/ic_launcher_background，但 colors.xml 里没有定义"
    )
    assert (RES / "drawable" / "ic_launcher_foreground.png").is_file(), (
        "自适应图标引用了 @drawable/ic_launcher_foreground，但文件不存在"
    )


def test_构建脚本不得再下载numpy_wheel():
    """契约：build-apk.ps1 里不该再有下载 numpy wheel 的步骤。

    既然安卓构建不再安装 numpy（见上），那个预先下载步骤就是白等一次网络请求，
    还会让人误以为构建仍然需要它。
    """
    script = (ANDROID_DIR / "build-apk.ps1").read_text(encoding="utf-8")
    assert "numpy-1.26" not in script, (
        "build-apk.ps1 还在下载 numpy wheel —— 安卓构建已不需要它"
    )
