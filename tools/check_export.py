"""开发用：校验一个导出的配置文件里有多少台设备、多少访问代码可用。

用法：``python tools/check_export.py <配置文件>``
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.util import secret  # noqa: E402
from tools._common import enable_utf8  # noqa: E402

enable_utf8()

path = sys.argv[1] if len(sys.argv) > 1 else ""
if not path or not os.path.exists(path):
    print(f"文件不存在：{path}")
    sys.exit(2)

data = json.load(open(path, encoding="utf-8"))
printers = data.get("printers", [])
usable = sum(1 for item in printers if len(secret.decrypt_text(str(item.get("access_code", "")))) == 8)
with_code = sum(1 for item in printers if item.get("access_code"))
print(f"文件：{path}")
print(f"打印机：{len(printers)} 台（其中 {with_code} 台带访问代码字段）")
print(f"可解密使用的访问代码：{usable} 个")
for item in printers:
    code = secret.decrypt_text(str(item.get("access_code", "")))
    print(f"  {item.get('name', ''):12} {item.get('ip', ''):16} {item.get('model', ''):8} "
          f"跨度={item.get('tile_span', 1)} 代码={'✓' if len(code) == 8 else '—'}")
sys.exit(0 if printers else 1)
