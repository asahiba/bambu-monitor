"""开发用：把当前（Windows DPAPI 加密的）配置导出成明文版，供 Linux/Docker 使用。

用法：``python tools/export_plain_config.py <输出路径>``

注意：生成的明文文件包含访问代码，用完请立即删除，不要提交到仓库。
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import AppConfig  # noqa: E402
from tools._common import enable_utf8  # noqa: E402

enable_utf8()

target = sys.argv[1] if len(sys.argv) > 1 else "data/config.json"
config = AppConfig.load()
data = json.loads(config.to_json())
for item in data.get("printers", []):
    # to_json 里是加密后的密文；这里换成明文，Linux 上没有 DPAPI
    source = next((p for p in config.printers if p.ip == item.get("ip")), None)
    if source is not None:
        item["access_code"] = source.access_code

os.makedirs(os.path.dirname(os.path.abspath(target)), exist_ok=True)
with open(target, "w", encoding="utf-8") as handle:
    json.dump(data, handle, ensure_ascii=False, indent=2)
os.chmod(target, 0o600)
print(f"已写出明文配置：{target}（{len(data.get('printers', []))} 台，权限 600）")
