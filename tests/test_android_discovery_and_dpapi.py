"""安卓版「搜索重复」与「添加失败：当前系统没有 DPAPI」的回归测试。

两个 bug 都只在安卓上暴露，但根因都在通用代码里：

1. **搜索重复**：同一台打印机被两条通道分别发现，一条带序列号、一条只认得出
   IP，`DiscoveryService._register` 只按 ``serial or ip`` 当键，于是登记成两条。

2. **添加失败当前系统没有 DPAPI**：安卓没有 Windows DPAPI，`encrypt_text`
   按设计退回明文保存（这是**正常且无法避免**的降级），但它把这个提示写进了
   `AppConfig.last_error`，而 `WebHost.add_printer` 看到 `last_error` 非空就
   判定失败 —— 设备其实已经加进去了，界面却报失败，用户以为没加上。
"""

from __future__ import annotations

from app.bambu.discovery import DiscoveryService, merge_devices
from app.bambu.models import PrinterInfo, PrinterModel
from app.util import secret

# --------------------------------------------------------------- 搜索去重


def test_同一台设备两条通道发现只出现一次():
    """带序列号的登记之后再遇到同 IP 的无序列号记录，应合并而不是新增。"""
    found: list[PrinterInfo] = []
    service = DiscoveryService(found.append, timeout=0.1)

    service._register(
        PrinterInfo(
            ip="192.168.2.243",
            serial="26A00A000000000000",
            name="车间 A2L",
            model=PrinterModel.A2L,
            discovered=True,
        )
    )
    assert len(service.results) == 1

    # 另一条通道只认得出 IP（没带 USN），名字也空
    service._register(
        PrinterInfo(ip="192.168.2.243", serial="", name="", model=PrinterModel.UNKNOWN)
    )

    results = service.results
    assert len(results) == 1, f"同一台设备被登记了 {len(results)} 次：{results}"
    merged = results[0]
    assert merged.serial == "26A00A000000000000"
    assert merged.name == "车间 A2L", "合并时不该丢掉已有的名字"
    # 重复登记不应再次回调前端（否则列表里会闪出两条）
    assert len(found) == 1, f"on_found 被调用了 {len(found)} 次，应当只有 1 次"


def test_先无序列号后有序列号也能合并():
    """顺序反过来同样要合并，并把序列号补上。"""
    found: list[PrinterInfo] = []
    service = DiscoveryService(found.append, timeout=0.1)

    service._register(PrinterInfo(ip="192.168.2.50", serial="", model=PrinterModel.UNKNOWN))
    assert len(service.results) == 1

    service._register(
        PrinterInfo(ip="192.168.2.50", serial="01P00A1234567", name="P1S", model=PrinterModel.P1S)
    )

    results = service.results
    assert len(results) == 1, f"应当合并成 1 条，实际 {len(results)} 条"
    assert results[0].serial == "01P00A1234567", "合并后应补上序列号"
    assert results[0].model == PrinterModel.P1S


def test_同名不同设备不会被误合并():
    """不同 IP、不同序列号的两台设备必须各自保留。"""
    service = DiscoveryService(lambda info: None, timeout=0.1)
    service._register(PrinterInfo(ip="192.168.2.10", serial="01P00A0000001", name="一号"))
    service._register(PrinterInfo(ip="192.168.2.11", serial="01P00A0000002", name="一号"))
    assert len(service.results) == 2


def test_merge_devices_同样按序列号与IP双重去重():
    """`merge_devices` 也有同样的去重缺陷，要一起守住。"""
    existing = [
        PrinterInfo(ip="192.168.2.243", serial="26A00A000000000000", name="A2L"),
    ]
    found = [
        # 同一台，但只有 IP
        PrinterInfo(ip="192.168.2.243", serial="", name=""),
        # 另一台，确实不同
        PrinterInfo(ip="192.168.2.99", serial="01P00A9999999", name="别的机器"),
    ]
    merged = merge_devices(existing, found)
    assert len(merged) == 2, f"应当合并成 2 台，实际 {len(merged)} 台"
    ips = sorted(item.ip for item in merged)
    assert ips == ["192.168.2.243", "192.168.2.99"]


# --------------------------------------------------------------- DPAPI 提示不应阻断


def test_没有DPAPI时加密只记提示不记错误(monkeypatch):
    """非 Windows 上没 DPAPI 是平台事实，属于**提示**，不是错误。"""
    secret.clear_last_error()
    monkeypatch.setattr(secret.sys, "platform", "linux")

    out = secret.encrypt_text("12345678")

    assert out == "12345678", "无法加密时应原样返回，保证功能可用"
    assert secret.last_error() is None, (
        "「没有 DPAPI」被记成了错误 —— 这会让安卓上成功的添加被报成失败"
    )
    assert secret.last_warning(), "应当留下一条提示，让用户知道凭据是明文"


def test_有错误时错误通道仍然工作(monkeypatch):
    """提示不能把真正的错误盖掉，反之亦然。"""
    secret.clear_last_error()
    monkeypatch.setattr(secret.sys, "platform", "linux")
    secret._remember("写盘失败", secret.LEVEL_ERROR)
    secret._remember("明文保存", secret.LEVEL_WARNING)
    assert secret.last_error() == "写盘失败"
    assert secret.last_warning() == "明文保存"


def test_clear会同时清掉两条通道(monkeypatch):
    secret._remember("e", secret.LEVEL_ERROR)
    secret._remember("w", secret.LEVEL_WARNING)
    secret.clear_last_error()
    assert secret.last_error() is None
    assert secret.last_warning() is None


def test_添加设备不因明文提示而失败(isolated_config_dir, monkeypatch):
    """**核心回归**：模拟安卓（无 DPAPI）下从网页添加一台打印机。

    设备必须真的加上（返回 ok=True、会话建出来），
    同时把「凭据是明文」作为 warning 告诉用户，而不是报失败。
    """
    from app.config import AppConfig
    from app.web.host import WebHost

    # 让加密必定失败，模拟安卓没有 DPAPI
    monkeypatch.setattr(secret.sys, "platform", "linux")
    monkeypatch.setattr(secret, "_crypt", lambda data, protect: (_ for _ in ()).throw(OSError("DPAPI 仅在 Windows 上可用")))

    config = AppConfig.load()
    sessions: list = []
    host = WebHost(config, lambda: sessions)

    result = host.add_printer(name="车间 A2L", ip="192.168.2.243", access_code="12345678",
                              model_label="A2L", serial="26A00A000000000000")

    assert result["ok"] is True, f"添加竟然失败了：{result}"
    assert "当前系统没有 DPAPI" not in result.get("detail", ""), (
        "提示被塞进了失败原因里 —— 这正是用户看到的「添加失败当前系统没有 DPAPI」"
    )
    assert "warning" in result, "应当把明文保存作为 warning 告诉用户"
    assert "DPAPI" in result["warning"]

    # 设备确实落盘了
    assert any(item.ip == "192.168.2.243" for item in AppConfig.load().printers)
    # 而且会话也建出来了（否则用户加完看不到画面）
    assert len(sessions) == 1, "应当立即建立会话"
    try:
        sessions[0].stop()
    except Exception:
        pass


def test_真正写盘失败时仍然算失败(isolated_config_dir, monkeypatch):
    """别把错误一起放过了：写盘失败必须返回 ok=False。"""
    from app.config import AppConfig
    from app.web.host import WebHost

    config = AppConfig.load()
    host = WebHost(config, lambda: [])

    def boom(*args, **kwargs):
        raise OSError("磁盘满了")

    monkeypatch.setattr("builtins.open", boom)
    result = host.add_printer(name="x", ip="192.168.1.2", access_code="1")
    assert result["ok"] is False, "写盘失败必须报失败"
    assert result["detail"], "应当给出失败原因"


def test_搜索结果里同一台只出现一次_端到端(isolated_config_dir, monkeypatch):
    """端到端：`WebHost.discover()` 出去的列表里，同一台设备只应有一条。"""
    from app.config import AppConfig
    from app.web.host import WebHost

    dup = [
        PrinterInfo(ip="192.168.2.243", serial="26A00A000000000000",
                    name="车间 A2L", model=PrinterModel.A2L, discovered=True),
        PrinterInfo(ip="192.168.2.243", serial="", name="", model=PrinterModel.UNKNOWN,
                    discovered=True),
    ]
    monkeypatch.setattr("app.bambu.discovery.discover", lambda timeout=0: dup)

    config = AppConfig.load()
    host = WebHost(config, lambda: [])
    found = host.discover()

    ips = [item["ip"] for item in found]
    assert len(ips) == len(set(ips)), f"搜索列表里有重复 IP：{ips}"
