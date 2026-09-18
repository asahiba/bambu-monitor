"""``app/ui/discover_dialog.py`` 的列表去重契约测试（offscreen，全程离线）。

## 为什么单独立一条

搜索对话框原来用 ``key = info.serial or info.ip`` 单键去重，而
``discovery.merge_devices()``（当时没有任何调用方）用的是「序列号与 IP 双键查」。
两者差别在真实网络里很常见：SSDP 回包里带了序列号，2021 端口广播回的记录只
认得出 IP。单键去重就会把**同一台打印机列成两行** —— 用户看到的是「搜索列表里
同一台设备出现两次」，而且访问代码只可能填在其中一行上，另一行加进监控墙后
就是「访问代码未填写」。

现在 ``_add_row`` 走 ``merge_devices``，本文件锁住这条行为。

全部离线：``DiscoveryService.start`` 被替换成空操作，设备记录直接喂给
``_add_row``（与线程回调走的是同一个入口）。
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402

# ⚠️ 先判断再导入：CI 刻意不装 PySide6，模块级导入在没有 Qt 时是收集期错误
# （pytest 会直接失败，而不是跳过本文件）。
pytest.importorskip("PySide6.QtWidgets", reason="界面测试需要 PySide6（CI 不装它）")

from PySide6.QtWidgets import QApplication, QCheckBox  # noqa: E402

from app.bambu.models import PrinterInfo, PrinterModel  # noqa: E402

pytestmark = pytest.mark.usefixtures("isolated_config_dir", "no_network")


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


@pytest.fixture()
def dialog_factory(monkeypatch):
    """构造一个不真正扫描的 DiscoverDialog。"""
    _app()
    from app.ui import discover_dialog as module

    monkeypatch.setattr(module.DiscoveryService, "start", lambda self: None)
    monkeypatch.setattr(module.DiscoveryService, "stop", lambda self: None)

    created = []

    def make(known: list[PrinterInfo], timeout: float = 5.0):
        dialog = module.DiscoverDialog(known=known, timeout=timeout, parent=None)
        created.append(dialog)
        return dialog

    yield make
    for dialog in created:
        dialog.close()


def _rows(dialog) -> list[dict]:
    out = []
    for row in range(dialog.table.rowCount()):
        box = dialog.table.cellWidget(row, 0)
        check = box.findChild(QCheckBox) if box is not None else None
        out.append(
            {
                "name": dialog.table.item(row, 1).text(),
                "model": dialog.table.item(row, 2).text(),
                "ip": dialog.table.item(row, 3).text(),
                "serial": dialog.table.item(row, 4).text(),
                "code": dialog.table.item(row, 5).text(),
                "checked": check.isChecked() if check is not None else None,
            }
        )
    return out


def test_同一台设备只带ip和带序列号时只列一行(dialog_factory):
    """核心回归：``serial or ip`` 单键去重会列成两行，双键合并后只有一行。"""
    dialog = dialog_factory(known=[])

    # 先来一条只有 IP 的（例如 2021 端口广播回的记录）
    dialog._add_row(PrinterInfo(ip="10.0.0.9", model=PrinterModel.UNKNOWN))
    assert dialog.table.rowCount() == 1

    # 再来一条带序列号的同设备记录（例如 SSDP 回包）：必须并进同一行，且补全字段
    dialog._add_row(
        PrinterInfo(ip="10.0.0.9", serial="03900A1234567", name="车间 A1", model=PrinterModel.A1)
    )
    rows = _rows(dialog)
    assert len(rows) == 1, f"同一台设备被列成了多行：{rows}"
    assert rows[0]["serial"] == "03900A1234567"
    assert rows[0]["name"] == "车间 A1"
    assert rows[0]["model"] == PrinterModel.A1.label

    # 第三条：只有序列号、没有 IP（顺序反过来同样要合并）
    dialog._add_row(PrinterInfo(serial="03900A1234567", firmware="01.10.00.00"))
    rows = _rows(dialog)
    assert len(rows) == 1, f"只有序列号的记录没被合并：{rows}"
    assert rows[0]["ip"] == "10.0.0.9"


def test_已保存设备按ip或序列号都能认出已添加(dialog_factory):
    """配置里只存了序列号时，搜到只有 IP 的记录也必须认出来（并标「已添加」）。"""
    known = [
        PrinterInfo(
            ip="10.0.0.20",
            serial="03900A7654321",
            name="已保存机",
            model=PrinterModel.P1S,
            access_code="12345678",
        )
    ]
    dialog = dialog_factory(known=known)
    # 构造时已保存的打印机应当被列出来，且默认不勾选（避免重复添加）
    rows = _rows(dialog)
    assert len(rows) == 1
    assert rows[0]["checked"] is False, "已保存的打印机不该默认勾选"
    assert "已添加" in rows[0]["name"]

    # 只带 IP 的同设备记录：认出是同一台，不新增行
    dialog._add_row(PrinterInfo(ip="10.0.0.20", model=PrinterModel.P1S))
    rows = _rows(dialog)
    assert len(rows) == 1, f"已保存的设备被重复列出：{rows}"
    assert "已添加" in rows[0]["name"]

    # 只带序列号的同设备记录：同样不新增行
    dialog._add_row(PrinterInfo(serial="03900A7654321", model=PrinterModel.P1S))
    assert dialog.table.rowCount() == 1


def test_全选新发现跳过已保存的行(dialog_factory):
    known = [PrinterInfo(ip="10.0.0.30", serial="SERIAL-KNOWN", name="旧的")]
    dialog = dialog_factory(known=known)
    dialog._add_row(PrinterInfo(ip="10.0.0.31", serial="SERIAL-NEW", name="新的"))

    dialog._set_all(True, only_new=True)
    rows = _rows(dialog)
    assert rows[0]["checked"] is False, "「已添加」的行不该被「全选新发现」勾上"
    assert rows[1]["checked"] is True

    dialog._set_all(False)
    assert [row["checked"] for row in _rows(dialog)] == [False, False]


def test_确认时勾选的新设备带上填写的访问代码(dialog_factory):
    dialog = dialog_factory(known=[])
    dialog._add_row(PrinterInfo(ip="10.0.0.40", serial="SERIAL-A", name="甲"))

    # 模拟用户在「访问代码」列里手填
    dialog.table.item(0, 5).setText("87654321")
    dialog._accept()
    assert len(dialog.selected) == 1
    picked = dialog.selected[0]
    assert picked.ip == "10.0.0.40"
    assert picked.serial == "SERIAL-A"
    assert picked.name == "甲"
    assert picked.access_code == "87654321"
    assert picked.discovered is True


def test_重新搜索会清空本轮累积但保留已保存设备(dialog_factory):
    known = [PrinterInfo(ip="10.0.0.50", serial="SERIAL-KEEP", name="保留")]
    dialog = dialog_factory(known=known)
    dialog._add_row(PrinterInfo(ip="10.0.0.51", serial="SERIAL-TEMP", name="临时"))
    dialog._add_row(PrinterInfo(ip="10.0.0.51", name="临时-只有IP"))
    assert dialog.table.rowCount() == 2

    dialog._start_service(clear=True)
    rows = _rows(dialog)
    assert len(rows) == 1, f"清空后应只剩已保存的设备：{rows}"
    assert rows[0]["serial"] == "SERIAL-KEEP"
