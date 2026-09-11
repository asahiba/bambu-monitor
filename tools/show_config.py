"""开发用：查看已保存的打印机配置（访问代码只显示长度，不打印明文）。"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import config_path  # noqa: E402
from app.util import secret  # noqa: E402

path = config_path()
print("配置文件:", path, "存在:", os.path.exists(path))
if not os.path.exists(path):
    sys.exit(0)

data = json.load(open(path, encoding="utf-8"))
print("列数设置:", data.get("columns"), "自动连接:", data.get("auto_connect"))
for item in data.get("printers", []):
    code = secret.decrypt_text(item.get("access_code", ""))
    print(
        f"  {item.get('name','')!r:20} ip={item.get('ip',''):16} "
        f"model={item.get('model',''):10} serial={item.get('serial','')!r:20} "
        f"code={len(code)}位 mode={item.get('stream_mode','auto')}"
    )
