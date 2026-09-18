"""跨版本配置文件的可移植性（`app/util/secret.py` 的口令加密 + `app/config.py` 的导出/导入）。

## 为什么必须有这一层

本机加密天然不可移植，而用户的实际用法恰恰是**跨版本搬配置**：

* Windows 桌面版导出的 ``dpapi:`` 在 Linux / Docker / 安卓上解不开
  （DPAPI 绑定 Windows 用户）；
* Linux 导出的 ``fernet:`` 换台机器也解不开（密钥文件 ``secret.key`` 在那边）；
* **安卓**更特殊：APK 刻意不打包 ``cryptography``（Chaquopy 的预编译包是
  4096 字节对齐，在 16KB 内存页设备上会闪退，见 `docs/PACKAGING.md`），
  所以它连 Fernet 都没有。

于是导出时可以选择**用口令保护**：只用标准库实现
（PBKDF2-HMAC-SHA256 派生 + HMAC-SHA256 计数器流 + encrypt-then-MAC），
因此**任何版本、任何平台都能导入**。

本文件测三层：口令加密算法本身、`AppConfig` 的导出/导入、
以及「安卓式环境（没有 cryptography）也能解开」这条关键前提。
"""

from __future__ import annotations

import json

import pytest

from app import config
from app.bambu.models import PrinterInfo, PrinterModel
from app.util import secret

pytestmark = pytest.mark.usefixtures("isolated_config_dir")


def _cfg() -> config.AppConfig:
    cfg = config.AppConfig(columns=3, web_port=9090)
    cfg.printers = [
        PrinterInfo(
            ip="10.0.0.5",
            serial="01P00A0000001",
            name="车间甲",
            model=PrinterModel.P1S,
            access_code="12345678",
        ),
        PrinterInfo(ip="10.0.0.6", name="车间乙", model=PrinterModel.X2D, access_code="87654321"),
    ]
    return cfg


# --------------------------------------------------------------------------- 算法


def test_口令加密往返():
    blob = secret.encrypt_portable("12345678", "我的口令")
    assert blob.startswith("bmp1:"), blob
    assert "12345678" not in blob, "密文里不能出现明文"
    assert secret.decrypt_portable(blob, "我的口令") == "12345678"
    assert secret.is_portable(blob) is True
    assert secret.is_encrypted(blob) is True


def test_同一口令两次加密结果不同():
    """契约：每次都用新的盐与 nonce —— 否则相同口令的设备会暴露"两台一样"。"""
    first = secret.encrypt_portable("12345678", "p")
    second = secret.encrypt_portable("12345678", "p")
    assert first != second
    assert secret.decrypt_portable(first, "p") == secret.decrypt_portable(second, "p")


def test_口令不对时报错而不是给出垃圾明文():
    blob = secret.encrypt_portable("12345678", "对的")
    with pytest.raises(ValueError):
        secret.decrypt_portable(blob, "错的")


def test_密文被改动会被发现():
    """encrypt-then-MAC：改一个字节就必须认证失败，而不是解出乱码。"""
    blob = secret.encrypt_portable("12345678", "p")
    parts = blob.split(":")
    payload = parts[3]
    flipped = ("A" if payload[0] != "A" else "B") + payload[1:]
    tampered = ":".join([parts[0], parts[1], parts[2], flipped, parts[4]])
    with pytest.raises(ValueError):
        secret.decrypt_portable(tampered, "p")


def test_空口令被拒绝():
    with pytest.raises(ValueError):
        secret.encrypt_portable("12345678", "")


def test_一批凭据只派生一次密钥():
    """性能契约：一份配置里十几台设备，派生一次就够（20 万次 PBKDF2 约几百毫秒）。"""
    cipher = secret.PortableCipher("p")
    blobs = [cipher.encrypt(f"1234567{index}") for index in range(12)]
    same = secret.PortableCipher("p", cipher.salt)
    assert [same.decrypt(blob) for blob in blobs] == [f"1234567{index}" for index in range(12)]
    # 盐必须一致，否则说明各条各派生了一次（那就慢了 12 倍）
    assert len({blob.split(":")[1] for blob in blobs}) == 1


def test_安卓式环境没有cryptography也能口令解密(monkeypatch):
    """**核心前提**：APK 里没有 cryptography，但口令加密只用标准库，必须能解开。"""
    blob = secret.encrypt_portable("12345678", "p")
    monkeypatch.setattr(secret, "_fernet", lambda: None)  # 模拟没有 cryptography
    assert secret.decrypt_portable(blob, "p") == "12345678"


# --------------------------------------------------------------------------- 配置导出/导入


def test_带口令导出的文件不含明文且能跨机器导入(tmp_path):
    """回归：带口令导出的配置，在**另一台机器**（新 AppConfig）能完整恢复。"""
    target = tmp_path / "portable.json"
    assert _cfg().export_to(str(target), "共享口令") is True

    raw = target.read_text(encoding="utf-8")
    assert "12345678" not in raw and "87654321" not in raw
    data = json.loads(raw)
    assert data["portable"] is True and data["format"] >= config.EXPORT_FORMAT

    fresh = config.AppConfig()
    assert config.AppConfig.needs_passphrase(str(target)) is True
    assert fresh.import_from(str(target), "共享口令") is True
    assert [info.access_code for info in fresh.printers] == ["12345678", "87654321"]
    assert [info.name for info in fresh.printers] == ["车间甲", "车间乙"]
    assert fresh.columns == 3, "界面偏好也要一起回来"
    assert fresh.web_port == 9090


def test_没给口令时明确报错而不是静默导入(tmp_path):
    target = tmp_path / "portable.json"
    _cfg().export_to(str(target), "共享口令")

    fresh = config.AppConfig()
    assert fresh.import_from(str(target)) is False
    assert "口令" in fresh.last_error
    assert fresh.printers == [], "口令缺失时不该导入一半"


def test_口令错时报错且不导入(tmp_path):
    target = tmp_path / "portable.json"
    _cfg().export_to(str(target), "共享口令")

    fresh = config.AppConfig()
    assert fresh.import_from(str(target), "另一个口令") is False
    assert "口令" in fresh.last_error
    assert fresh.printers == []


def test_不带口令的导出仍能本机导入(tmp_path):
    """向后兼容：老用法（不设口令）必须继续可用。"""
    target = tmp_path / "local.json"
    assert _cfg().export_to(str(target)) is True
    assert config.AppConfig.needs_passphrase(str(target)) is False

    fresh = config.AppConfig()
    assert fresh.import_from(str(target)) is True
    assert [info.access_code for info in fresh.printers] == ["12345678", "87654321"]


def test_老版本导出的文件仍然能识别(tmp_path):
    """老文件没有 ``format``/``portable`` 字段，也必须照常导入。"""
    legacy = {
        "printers": [
            {"ip": "10.0.0.9", "name": "老配置", "model": "P1S", "access_code": "11112222"}
        ],
        "columns": 2,
    }
    path = tmp_path / "legacy.json"
    path.write_text(json.dumps(legacy, ensure_ascii=False), encoding="utf-8")
    assert config.AppConfig.needs_passphrase(str(path)) is False

    fresh = config.AppConfig()
    assert fresh.import_from(str(path)) is True
    assert fresh.printers[0].access_code == "11112222"
    assert fresh.columns == 2


def test_换机器导入本机加密的文件会点名并给出下一步(tmp_path, monkeypatch):
    """契约：解不开的凭据要**逐个点名**，并告诉用户下次导出带口令。

    这是用户最容易踩的坑：在电脑上导出、在平板上导入，结果访问代码全空，
    却只看到一句笼统的"导入成功"。
    """
    target = tmp_path / "local.json"
    _cfg().export_to(str(target))
    raw = json.loads(target.read_text(encoding="utf-8"))
    # 模拟「这份文件是在别的机器上加密的」：把凭据换成另一台机器的 dpapi 串
    for item in raw["printers"]:
        item["access_code"] = "dpapi:" + "QUJDREVGRw=="
    target.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")

    fresh = config.AppConfig()
    assert fresh.import_from(str(target)) is True, "设备列表本身应当导入成功"
    assert any("解不开" in fresh.warnings for _ in [0])
    assert "车间甲" in fresh.warnings and "车间乙" in fresh.warnings, "要逐个点名"
    assert "口令" in fresh.warnings, "要提示下次导出带口令"
    assert [info.access_code for info in fresh.printers] == ["", ""]


def test_导出文件里的界面偏好与设备字段齐全(tmp_path):
    """契约：带口令导出也不能丢字段（曾经 import 少恢复一半字段）。"""
    cfg = config.AppConfig(
        columns=2,
        show_timestamp=False,
        auto_connect=False,
        last_timeout=35.0,
        web_enabled=True,
        web_fps=6.0,
    )
    cfg.printers = [
        PrinterInfo(
            ip="10.0.0.7",
            serial="03900A1111111",
            model=PrinterModel.X1C,
            discovered=True,
            tile_span=2,
            access_code="12345678",
        )
    ]
    target = tmp_path / "full.json"
    assert cfg.export_to(str(target), "p") is True

    fresh = config.AppConfig()
    assert fresh.import_from(str(target), "p") is True
    assert fresh.show_timestamp is False
    assert fresh.auto_connect is False
    assert fresh.last_timeout == 35.0
    assert fresh.web_enabled is True
    assert fresh.web_fps == 6.0
    assert fresh.printers[0].tile_span == 2
    assert fresh.printers[0].discovered is True
    assert fresh.printers[0].model is PrinterModel.X1C


def test_needs_passphrase_读不到文件时不抛异常(tmp_path):
    assert config.AppConfig.needs_passphrase(str(tmp_path / "不存在.json")) is False
    broken = tmp_path / "broken.json"
    broken.write_text("{ 不是 json", encoding="utf-8")
    assert config.AppConfig.needs_passphrase(str(broken)) is False
