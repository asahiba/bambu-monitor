"""拓竹（Bambu Lab）局域网协议实现。

本包不依赖任何 GUI 框架，可独立用于命令行测试。

**注意**：这里**故意不做**包级 re-export。以前这里有
``from .printer import PrinterSession``，副作用是任何 ``from app.bambu import tlsutil``
都会连带导入 ``printer`` → ``camera``/``mqtt_worker``（进而拉起 paho），
让 `tools/tls_matrix.py` 这类纯 TLS 脚本被无谓地拖上重量级依赖。
需要什么就从对应子模块直接导入：``from app.bambu.models import PrinterStatus``。
"""
