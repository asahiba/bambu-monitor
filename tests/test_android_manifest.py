"""安卓工程清单的契约测试（不需要 Android SDK，纯文本/XML 解析）。

这些断言盯的是**只在真机上才会炸、而且往往炸得很晚**的东西：
Android 14 起前台服务的类型必须声明且与调用一致，漏了会直接抛
`MissingForegroundServiceTypeException`；权限漏了则表现为"服务看起来在跑，
但常驻通知不出现"或"息屏就断连"。

放在契约测试里，是因为它们**必须在每次改清单时被检查**，
而不是等打包出 APK 装到设备上才发现。
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET

import pytest

from tests.test_contracts import PROJECT_ROOT

ANDROID = "{http://schemas.android.com/apk/res/android}"
MANIFEST = PROJECT_ROOT / "android" / "app" / "src" / "main" / "AndroidManifest.xml"
JAVA_DIR = PROJECT_ROOT / "android" / "app" / "src" / "main" / "java" / "com" / "bambumonitor"
STRINGS = PROJECT_ROOT / "android" / "app" / "src" / "main" / "res" / "values" / "strings.xml"


@pytest.fixture(scope="module")
def manifest() -> ET.Element:
    assert MANIFEST.is_file(), f"找不到清单：{MANIFEST}"
    return ET.parse(MANIFEST).getroot()


def _permissions(root: ET.Element) -> set[str]:
    return {
        element.get(ANDROID + "name", "")
        for element in root.findall("uses-permission")
    }


def test_前台服务声明了类型且权限齐全(manifest):
    """契约：前台服务声明 + Android 14 要求的类型权限。

    后台保活完全依赖这个服务；少一样都会在真机上出问题，而单元测试看不见。
    """
    services = manifest.findall("application/service")
    assert services, "清单里没有 service 声明"

    monitor = next(
        (s for s in services if (s.get(ANDROID + "name") or "").endswith("MonitorService")),
        None,
    )
    assert monitor is not None, "找不到 MonitorService 声明"
    assert monitor.get(ANDROID + "exported") == "false", "服务只给自己用，不该导出"
    assert monitor.get(ANDROID + "foregroundServiceType") == "dataSync", (
        "必须声明 foregroundServiceType；Android 14 起缺失或不匹配会直接崩"
    )

    perms = _permissions(manifest)
    for needed in (
        "android.permission.FOREGROUND_SERVICE",
        "android.permission.FOREGROUND_SERVICE_DATA_SYNC",
        "android.permission.WAKE_LOCK",
        "android.permission.POST_NOTIFICATIONS",
        "android.permission.ACCESS_WIFI_STATE",
        "android.permission.CHANGE_WIFI_STATE",
        "android.permission.INTERNET",
    ):
        assert needed in perms, f"缺少权限：{needed}"


def test_不申请敏感的位置与电池优化权限(manifest):
    """契约：不申请位置权限、也不申请"直接忽略电池优化"的敏感权限。

    位置权限常被用来推断 Wi-Fi 列表，本项目不需要；申请了会让用户警惕。
    `REQUEST_IGNORE_BATTERY_OPTIMIZATIONS` 属于应用商店会额外审查的敏感权限，
    所以改为在界面上引导用户自己去系统设置（见 MainActivity 的引导对话框）。
    """
    perms = _permissions(manifest)
    forbidden = {
        "android.permission.ACCESS_FINE_LOCATION",
        "android.permission.ACCESS_COARSE_LOCATION",
        "android.permission.REQUEST_IGNORE_BATTERY_OPTIMIZATIONS",
    }
    overlap = perms & forbidden
    assert not overlap, f"出现了不该申请的权限：{sorted(overlap)}"


def test_触摸屏声明为可选以兼容ChromeOS与桌面模式(manifest):
    """契约：`touchscreen` 必须 required=false，且 Activity 可自由缩放。

    ChromeOS 笔记本、DeX/桌面模式、电视盒子都没有触摸屏。若隐式要求触摸屏，
    这些设备在应用商店里会被直接过滤掉；不可缩放的窗口在桌面环境下也很难用。
    """
    touch = [
        f for f in manifest.findall("uses-feature")
        if (f.get(ANDROID + "name") or "") == "android.hardware.touchscreen"
    ]
    assert touch, "应当显式声明 touchscreen 这个 feature"
    assert touch[0].get(ANDROID + "required") == "false", "触摸屏必须声明为可选"

    activities = manifest.findall("application/activity")
    target = next(
        (a for a in activities if (a.get(ANDROID + "name") or "").endswith("MainActivity")),
        None,
    )
    assert target is not None, "找不到 MainActivity"
    assert target.get(ANDROID + "resizeableActivity") == "true", (
        "必须允许自由缩放，否则在 ChromeOS/桌面模式里是个不听话的窗口"
    )


def test_沉浸式全屏只在有触摸屏时才用():
    """契约：桌面环境（ChromeOS/DeX）不要进沉浸式。

    沉浸式会把窗口标题栏与系统栏一起藏掉，桌面用户没法拖窗口、最小化、
    切换应用。判据要用**触摸屏能力**而不是屏幕大小 —— ChromeOS 大屏但无触摸，
    正好落在这条上。
    """
    source = (JAVA_DIR / "MainActivity.java").read_text(encoding="utf-8")
    assert "hasTouchscreen()" in source, "应当按触摸屏能力决定是否沉浸式"
    assert "FEATURE_TOUCHSCREEN" in source, "判据应当是 FEATURE_TOUCHSCREEN"

    # 沉浸式那几行必须包在 hasTouchscreen() 分支里
    match = re.search(r"if \(hasTouchscreen\(\)\) \{(.{0,400}?)\n        \}", source, re.S)
    assert match is not None, "找不到 hasTouchscreen() 的分支"
    assert "SYSTEM_UI_FLAG_IMMERSIVE_STICKY" in match.group(1), (
        "沉浸式标志应当在该分支内设置"
    )


def test_WebView_里网页的文件选择能用():
    """契约：网页端「配置备份 → 从文件读取」在安卓 WebView 里必须点得动。

    WebView **默认不实现** ``<input type="file">``：没有 `onShowFileChooser`，
    用户点「从文件读取…」毫无反应，而且没有任何报错 —— 而"导出的配置文件
    能不能在平板上导入"正是这一版要保证的事（见 app/web/page.py 的配置备份）。
    拿到结果后必须把 `ValueCallback` 还回去，否则**下一次点击会被 WebView 忽略**。
    """
    source = (JAVA_DIR / "MainActivity.java").read_text(encoding="utf-8")
    assert "onShowFileChooser" in source, "缺少 WebView 文件选择回调"
    assert "FileChooserParams" in source
    assert "onActivityResult" in source, "结果必须回调给网页"
    assert "FILE_CHOOSER_REQUEST" in source, "应当用请求码区分自己的选择请求"
    assert "onReceiveValue" in source, "必须把结果交回 ValueCallback"
    # 未完成的选择请求要先还回去，避免 WebView 认为还有请求在路上
    assert "fileCallback.onReceiveValue(null)" in source


def test_服务用到的字符串资源都已定义(manifest):
    """契约：Java 里引用的 R.string.* 必须在 strings.xml 里存在。

    漏一个就是编译期报错（还好），但更常见的是**加了字符串却忘了用在通知里**，
    导致常驻通知显示空白 —— 用户不知道服务在跑。
    """
    strings_text = STRINGS.read_text(encoding="utf-8")
    defined = set(re.findall(r'<string name="([^"]+)"', strings_text))
    assert defined, "strings.xml 里没有字符串"

    for java_file in JAVA_DIR.glob("*.java"):
        source = java_file.read_text(encoding="utf-8")
        for name in re.findall(r"R\.string\.(\w+)", source):
            assert name in defined, (
                f"{java_file.name} 引用了未定义的字符串资源 R.string.{name}"
            )

    # 前台服务的通知文案必须齐（服务在跑就要说清楚）
    for needed in (
        "service_channel_name",
        "service_channel_desc",
        "service_notification_starting",
        "service_notification_running",
        "service_notification_failed",
    ):
        assert needed in defined, f"缺少前台服务通知文案：{needed}"
