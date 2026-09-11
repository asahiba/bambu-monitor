"""开发用：验证「保存 / 导出 / 导入配置」链路（含界面按钮的代码路径）。

用法：``python tools/config_check.py``
不修改真实配置：界面部分使用临时配置对象，导出写到系统临时目录。
"""

from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QFileDialog  # noqa: E402

from app.bambu.models import PrinterInfo, PrinterModel  # noqa: E402
from app.config import AppConfig, backup_path, config_path  # noqa: E402
from app.ui.main_window import MainWindow  # noqa: E402
from tools._common import enable_utf8  # noqa: E402

enable_utf8()

ok = True
temp_dir = tempfile.mkdtemp(prefix="bambu-config-check-")
export_file = os.path.join(temp_dir, "exported.json")

print("① 真实配置的导出 / 重新导入（只读，不改动原文件）")
real = AppConfig.load()
print(f"   真实配置：{len(real.printers)} 台，"
      f"{sum(1 for p in real.printers if len(p.access_code) == 8)} 个可用访问代码")
if not real.export_to(export_file):
    print("   ✗ 导出失败")
    ok = False
else:
    print(f"   导出成功：{export_file}（{os.path.getsize(export_file)} 字节）")
    restored = AppConfig()
    if not restored.import_from(export_file):
        print("   ✗ 重新导入失败")
        ok = False
    else:
        codes_before = sorted(p.access_code for p in real.printers)
        codes_after = sorted(p.access_code for p in restored.printers)
        same = (
            len(restored.printers) == len(real.printers)
            and codes_before == codes_after
            and [p.ip for p in restored.printers] == [p.ip for p in real.printers]
        )
        print(f"   重新导入：{len(restored.printers)} 台，访问代码一致：{codes_before == codes_after}")
        if not same:
            print("   ✗ 导出/导入后数据不一致")
            ok = False

print("\n② 界面按钮的实际代码路径（monkeypatch 文件对话框）")
app = QApplication.instance() or QApplication(sys.argv)
config = AppConfig()
config.persist = False  # 绝不写真实配置
config.auto_connect = False
config.printers = [
    PrinterInfo(ip=f"10.0.0.{index + 10}", name=f"测试{index}", serial=f"TESTSERIAL{index:04d}",
                model=PrinterModel.P1S, access_code=f"1234567{index}")
    for index in range(3)
]
window = MainWindow(config)

ui_export = os.path.join(temp_dir, "ui-export.json")
QFileDialog.getSaveFileName = staticmethod(lambda *a, **k: (ui_export, "JSON 文件 (*.json)"))
try:
    window.export_config()
except Exception as exc:  # noqa: BLE001
    print(f"   ✗ 导出按钮报错：{exc.__class__.__name__}: {exc}")
    ok = False
else:
    if os.path.exists(ui_export):
        print(f"   导出按钮正常：{os.path.getsize(ui_export)} 字节")
    else:
        print("   ✗ 导出按钮没有生成文件")
        ok = False

# 导入：先清空窗口，再从文件恢复
for session in list(window.sessions):
    session.stop()
for tile in list(window.tiles):
    tile.shutdown()
    tile.setParent(None)
    tile.deleteLater()
window.sessions.clear()
window.tiles.clear()
QFileDialog.getOpenFileName = staticmethod(lambda *a, **k: (ui_export, "JSON 文件 (*.json)"))
try:
    window.import_config()
except Exception as exc:  # noqa: BLE001
    print(f"   ✗ 导入按钮报错：{exc.__class__.__name__}: {exc}")
    ok = False
else:
    count = len(window.tiles)
    print(f"   导入按钮正常：恢复到 {count} 台画面")
    if count != 3:
        print("   ✗ 导入后画面数量不对")
        ok = False

window.close()

print("\n③ 配置保护")
print(f"   主配置：{config_path()}")
print(f"   自动备份：{backup_path()}（存在：{os.path.exists(backup_path())}）")

print("\n" + "=" * 60)
print("配置链路检查：" + ("全部通过 ✓" if ok else "存在失败项 ✗"))
print("=" * 60)
sys.exit(0 if ok else 1)
