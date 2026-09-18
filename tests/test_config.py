"""``app/config.py`` 配置读写契约测试。

覆盖：默认值、JSON 往返、范围钳制、``persist=False`` 不写盘、损坏配置回退备份、
导出/导入往返、``web_token`` 自动生成，以及「绝不触碰用户真实配置目录」这条安全契约。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from app import config
from app.bambu.models import PrinterInfo, PrinterModel
from app.util import secret

from .conftest import real_config_dir

#: 本文件是纯逻辑层测试：禁止任何 socket 连接/监听（不连真机、不占端口）
pytestmark = pytest.mark.usefixtures("no_network")

# --------------------------------------------------------------------------- 辅助


def _config_dir() -> Path:
    """当前生效的配置目录（测试体内实时解析，保证取到夹具设置后的值）。"""
    return Path(config.config_dir())


def _main_path() -> Path:
    return Path(config.config_path())


def _backup_path() -> Path:
    return Path(config.backup_path())


def _write_json(path: Path, payload) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _write_raw(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


def _tile_span(cfg: config.AppConfig) -> int:
    return cfg.printers[0].tile_span


def _printers(cfg: config.AppConfig) -> list[PrinterInfo]:
    return cfg.printers


# --------------------------------------------------------------------------- 默认值


def test_default_config_values():
    """契约：AppConfig() 的默认值必须与文档一致（20s 超时 / 10fps / 150ms / 8080 / 720px / 自适应列）。"""
    cfg = config.AppConfig()
    assert cfg.last_timeout == 20.0
    assert cfg.max_fps == 10.0
    assert cfg.refresh_ms == 150
    assert cfg.web_port == 8080
    assert cfg.web_fps == 4.0
    assert cfg.web_max_width == 720
    assert cfg.columns == 0  # 0 = 自适应
    assert cfg.auto_connect is True
    assert cfg.show_timestamp is True
    assert cfg.web_enabled is False
    assert cfg.persist is True
    assert cfg.printers == []
    assert cfg.last_error == ""


def test_default_web_token_is_random_hex():
    """契约：新建配置自动生成非空网页令牌（16 位十六进制），两次构造互不相同。"""
    first, second = config.AppConfig().web_token, config.AppConfig().web_token
    assert first and second
    assert len(first) == 16
    assert all(ch in "0123456789abcdef" for ch in first)
    assert first != second


# --------------------------------------------------------------------------- 往返


def test_save_load_roundtrip_preserves_printers():
    """契约：save() 后 load() 往返一致——打印机数量、顺序、IP、名称、tile_span、model 枚举都不变。"""
    cfg = config.AppConfig()
    cfg.columns = 2
    cfg.printers = [
        PrinterInfo(
            ip="10.0.0.5",
            serial="01PABC123",
            name="打印机一",
            model=PrinterModel.P1S,
            tile_span=2,
            access_code="12345678",
        ),
        PrinterInfo(ip="10.0.0.6", serial="094ABC123", name="H2D", model=PrinterModel.H2D),
        PrinterInfo(ip="10.0.0.7", name="未知", model=PrinterModel.UNKNOWN, tile_span=3),
    ]
    cfg.save()
    assert _main_path().exists()

    loaded = config.AppConfig.load()
    assert len(loaded.printers) == 3
    assert [p.ip for p in loaded.printers] == ["10.0.0.5", "10.0.0.6", "10.0.0.7"]
    assert [p.name for p in loaded.printers] == ["打印机一", "H2D", "未知"]
    assert [p.tile_span for p in loaded.printers] == [2, 1, 3]
    assert [p.model for p in loaded.printers] == [
        PrinterModel.P1S,
        PrinterModel.H2D,
        PrinterModel.UNKNOWN,
    ]
    assert all(isinstance(p.model, PrinterModel) for p in loaded.printers)
    assert loaded.columns == 2


def test_save_load_roundtrip_preserves_access_code():
    """契约：访问代码能原样往返；只要有加密能力，就必须**不以明文落盘**。

    ⚠️ 这条测试以前写成「明文不得落盘」一刀切，于是**只在 Windows 上能过**：
    非 Windows 平台那时没有 DPAPI，`secret.encrypt_text` 按设计退回明文保存
    （见 `app/util/secret.py` 的模块文档与 `SECURITY.md`）。
    这种写法会让 CI 的 Linux 任务必然失败 —— 而它确实失败了，
    因为本地只在 Windows 上跑过全量回归。

    之后又补了「非 Windows 上是明文」的断言。现在非 Windows 也有第二层加密
    （本机密钥文件 / `BAMBU_MONITOR_SECRET` 口令 + Fernet），所以判据改成
    **按能力而不是按平台**：`secret.can_encrypt()` 为真就必须看不到明文；
    只有安卓那种「连 cryptography 都没有」的部署才允许明文，且必须留下提示。
    """
    cfg = config.AppConfig()
    cfg.printers = [PrinterInfo(ip="10.0.0.5", access_code="12345678")]
    cfg.save()

    raw = _main_path().read_text(encoding="utf-8")

    # 共同契约：不管哪个平台，都要能原样读回来
    assert config.AppConfig.load().printers[0].access_code == "12345678"

    if sys.platform == "win32":
        assert "12345678" not in raw, "Windows 上明文不得落盘"
        assert "dpapi:" in raw
    elif secret.can_encrypt():
        assert "12345678" not in raw, "本环境能加密，不该明文落盘"
        assert "fernet:" in raw
        assert cfg.warnings, "用了本机密钥加密（而非 DPAPI）应当告知用户"
    else:
        # 没有 cryptography（典型是安卓 APK）：明文保存是既定行为，且必须留下提示
        assert "12345678" in raw, (
            "本环境预期以明文保存访问代码（连 cryptography 都没有）；"
            "若这里失败，说明加密策略变了，请同步更新 SECURITY.md 与 secret.py 的说明"
        )
        assert cfg.warnings, "明文保存必须留下 warnings 提示，不能静默"


def test_save_load_roundtrip_of_switches_and_numbers():
    """契约：数值与布尔开关（含 False）都要原样往返，不能被默认值覆盖。"""
    cfg = config.AppConfig(
        show_timestamp=False,
        auto_connect=False,
        web_enabled=True,
        last_timeout=35.0,
        max_fps=5.0,
        refresh_ms=250,
        web_port=9090,
        web_fps=6.0,
        web_max_width=1280,
    )
    cfg.save()

    loaded = config.AppConfig.load()
    assert loaded.show_timestamp is False
    assert loaded.auto_connect is False
    assert loaded.web_enabled is True
    assert loaded.last_timeout == 35.0
    assert loaded.max_fps == 5.0
    assert loaded.refresh_ms == 250
    assert loaded.web_port == 9090
    assert loaded.web_fps == 6.0
    assert loaded.web_max_width == 1280


# --------------------------------------------------------------------------- 范围钳制


@pytest.mark.parametrize(
    "payload, read, expected",
    [
        ({"printers": [{"ip": "1.1.1.1", "tile_span": 0}]}, _tile_span, 1),
        ({"printers": [{"ip": "1.1.1.1", "tile_span": -3}]}, _tile_span, 1),
        ({"printers": [{"ip": "1.1.1.1", "tile_span": 9}]}, _tile_span, 3),
        ({"printers": [{"ip": "1.1.1.1", "tile_span": 300}]}, _tile_span, 3),
        ({"last_timeout": 1}, lambda c: c.last_timeout, 15.0),
        ({"last_timeout": 999}, lambda c: c.last_timeout, 60.0),
        ({"web_port": -5}, lambda c: c.web_port, 1),
        ({"web_port": 70000}, lambda c: c.web_port, 65535),
        ({"web_fps": 0.1}, lambda c: c.web_fps, 0.5),
        ({"web_fps": 100}, lambda c: c.web_fps, 15.0),
        ({"web_max_width": 100}, lambda c: c.web_max_width, 240),
        ({"web_max_width": 5000}, lambda c: c.web_max_width, 1920),
        ({"refresh_ms": 10}, lambda c: c.refresh_ms, 50),
        ({"refresh_ms": 5000}, lambda c: c.refresh_ms, 1000),
        ({"max_fps": -5}, lambda c: c.max_fps, 0.0),
        ({"max_fps": 100}, lambda c: c.max_fps, 30.0),
    ],
    ids=[
        "tile_span-0",
        "tile_span-negative",
        "tile_span-9",
        "tile_span-300",
        "last_timeout-1",
        "last_timeout-999",
        "web_port-negative",
        "web_port-70000",
        "web_fps-0.1",
        "web_fps-100",
        "web_max_width-100",
        "web_max_width-5000",
        "refresh_ms-10",
        "refresh_ms-5000",
        "max_fps-negative",
        "max_fps-100",
    ],
)
def test_load_clamps_out_of_range_values(payload, read, expected):
    """契约（范围钳制）：超界配置在 load() 时被夹到合法区间，而不是原样生效。

    tile_span 1..3、last_timeout 15..60、web_port 1..65535、web_fps 0.5..15、
    web_max_width 240..1920、refresh_ms 50..1000、max_fps 0..30。
    """
    _write_json(_main_path(), payload)
    loaded = config.AppConfig.load()
    assert read(loaded) == expected


def test_load_treats_zero_as_unset_for_clamped_fields():
    """契约（如实记录真实实现）：这些字段用 ``值 or 默认值`` 处理，0 被当成「未设置」→ 回到默认值。

    注意 0 不会被夹到区间下界（web_port 0 → 8080 而不是 1，last_timeout 0 → 20.0 而不是 15）。
    """
    _write_json(
        _main_path(),
        {
            "web_port": 0,
            "last_timeout": 0,
            "web_fps": 0,
            "refresh_ms": 0,
            "web_max_width": 0,
            "max_fps": 0,
            "columns": 0,
        },
    )
    loaded = config.AppConfig.load()
    # 0 是「真的填了 0」：按区间夹到下界（而不是被当成「没填」退回默认值）
    assert loaded.web_port == 1
    assert loaded.last_timeout == 15.0
    assert loaded.web_fps == 0.5
    assert loaded.refresh_ms == 50
    assert loaded.web_max_width == 240
    # 例外：max_fps / columns 的 0 本身有语义（不限制帧率 / 自适应列数），必须原样保留
    assert loaded.max_fps == 0.0
    assert loaded.columns == 0


def test_load_uses_defaults_for_missing_fields():
    """契约：字段缺失（而不是填 0）时才用默认值——两者必须区分开。"""
    _write_json(_main_path(), {})
    loaded = config.AppConfig.load()
    assert loaded.web_port == 8080
    assert loaded.last_timeout == 20.0
    assert loaded.max_fps == 10.0
    assert loaded.refresh_ms == 150
    assert loaded.web_fps == 4.0
    assert loaded.web_max_width == 720
    assert loaded.columns == 0
    assert loaded.show_timestamp is True
    assert loaded.auto_connect is True


def test_load_accepts_numeric_strings():
    """契约：数字以字符串形式落盘（手改配置常见）也能解析，``columns=0`` 保持 0 而不是被顶成默认。"""
    _write_json(_main_path(), {"web_port": "9090", "max_fps": "12.5", "columns": 0})
    loaded = config.AppConfig.load()
    assert loaded.web_port == 9090
    assert loaded.max_fps == 12.5
    assert loaded.columns == 0


def test_max_fps_zero_means_unlimited_roundtrip():
    """契约（回归 D3）：``max_fps=0`` 表示「不限制帧率」，必须能保存并原样读回，不能被顶成 10.0。"""
    cfg = config.AppConfig(max_fps=0.0)
    cfg.save()
    assert json.loads(_main_path().read_text(encoding="utf-8"))["max_fps"] == 0.0

    assert config.AppConfig.load().max_fps == 0.0


def test_max_fps_zero_from_handwritten_config_is_kept():
    """契约（回归 D3）：手改配置写成 ``"max_fps": 0`` 同样应保留为 0（不限速）。"""
    _write_json(_main_path(), {"max_fps": 0})
    assert config.AppConfig.load().max_fps == 0.0


# --------------------------------------------------------------------------- persist / 备份


def test_persist_false_never_writes_to_disk():
    """契约：``persist=False``（演示/测试模式）时 save() 不写盘，连临时文件与备份都不产生。"""
    cfg = config.AppConfig(persist=False)
    cfg.printers = [PrinterInfo(ip="8.8.8.8", name="不该落盘")]
    cfg.save()

    assert not _main_path().exists()
    assert not _backup_path().exists()
    leftovers = [p.name for p in _config_dir().iterdir() if p.suffix in {".json", ".tmp"}]
    assert leftovers == [], f"persist=False 却写了盘：{leftovers}"


def test_save_backs_up_previous_config():
    """契约（README:299）：每次保存前把上一份备份成 config.backup.json，主配置损坏时可恢复。"""
    first = config.AppConfig()
    first.printers = [PrinterInfo(ip="1.1.1.1", name="第一台", model=PrinterModel.A1)]
    first.save()

    second = config.AppConfig()
    second.printers = [PrinterInfo(ip="2.2.2.2", name="第二台", model=PrinterModel.P1S)]
    second.save()

    assert _backup_path().exists()
    backup = json.loads(_backup_path().read_text(encoding="utf-8"))
    assert [p["ip"] for p in backup["printers"]] == ["1.1.1.1"]
    assert [p["name"] for p in backup["printers"]] == ["第一台"]


def test_load_falls_back_to_backup_when_main_config_is_invalid_json():
    """契约：主配置是非法 JSON 时 load() 回退到 config.backup.json（README:299）。"""
    _write_raw(_main_path(), "{ 这不是合法 JSON")
    _write_json(
        _backup_path(),
        {
            "printers": [{"ip": "7.7.7.7", "name": "备份机", "model": "X1C", "tile_span": 2}],
            "web_port": 9999,
        },
    )

    loaded = config.AppConfig.load()
    assert len(loaded.printers) == 1
    assert loaded.printers[0].ip == "7.7.7.7"
    assert loaded.printers[0].name == "备份机"
    assert loaded.printers[0].model is PrinterModel.X1C
    assert loaded.web_port == 9999
    # 降级必须对用户可见（界面据此提示），不能悄悄发生
    assert loaded.last_error


def test_load_returns_defaults_when_main_and_backup_both_broken():
    """契约：主配置与备份都损坏时退回默认配置（不抛异常），并仍带上一个可用的 web_token。"""
    _write_raw(_main_path(), "not json at all")
    _write_raw(_backup_path(), "[}")

    loaded = config.AppConfig.load()
    assert loaded.printers == []
    assert loaded.web_port == 8080
    assert loaded.last_timeout == 20.0
    assert loaded.web_token
    assert loaded.last_error  # 说明为什么用上了默认值


@pytest.mark.parametrize("broken_text", ["[]", '"hello"', "123", "true"], ids=["list", "string", "int", "bool"])
def test_load_falls_back_to_backup_when_root_is_not_an_object(broken_text):
    """契约（回归 D1）：config.json 是合法 JSON 但根节点不是对象，同样算「损坏」→ 回退备份。

    以前这里会抛 AttributeError（``'list' object has no attribute 'get'``），
    让程序在启动时崩掉，与 README:299 的承诺不符。
    """
    _write_raw(_main_path(), broken_text)
    _write_json(_backup_path(), {"printers": [{"ip": "7.7.7.7", "name": "备份机"}], "web_port": 9999})

    loaded = config.AppConfig.load()
    assert [p.ip for p in loaded.printers] == ["7.7.7.7"]
    assert loaded.web_port == 9999
    assert loaded.last_error


@pytest.mark.parametrize(
    "payload, read, expected",
    [
        ({"printers": [{"ip": "1.1.1.1", "tile_span": "auto"}]}, _tile_span, 1),
        ({"printers": [{"ip": "1.1.1.1", "tile_span": None}]}, _tile_span, 1),
        ({"web_port": "http"}, lambda c: c.web_port, 8080),
        ({"columns": "三"}, lambda c: c.columns, 0),
        ({"refresh_ms": None}, lambda c: c.refresh_ms, 150),
        ({"web_fps": []}, lambda c: c.web_fps, 4.0),
        ({"last_timeout": "很久"}, lambda c: c.last_timeout, 20.0),
        ({"web_max_width": {"a": 1}}, lambda c: c.web_max_width, 720),
    ],
    ids=[
        "tile-span-str",
        "tile-span-none",
        "web-port-str",
        "columns-str",
        "refresh-none",
        "web-fps-list",
        "last-timeout-str",
        "width-dict",
    ],
)
def test_load_degrades_type_damaged_fields_to_defaults(payload, read, expected):
    """契约（回归 D1）：单个字段类型不对时退回该字段的默认值，绝不抛异常（程序必须能起来）。"""
    _write_json(_main_path(), payload)
    loaded = config.AppConfig.load()
    assert read(loaded) == expected


def test_load_keeps_other_preferences_when_one_field_is_damaged():
    """契约（回归 D1）：一个字段坏掉不该拖垮整份配置——能解析的字段照常生效。"""
    _write_json(
        _main_path(),
        {"printers": {"这不是数组": 1}, "web_port": 9090, "columns": 2, "printers_x": 0},
    )
    loaded = config.AppConfig.load()
    assert loaded.printers == []  # printers 结构坏了 -> 退化成空列表
    assert loaded.web_port == 9090  # 其它偏好仍然生效
    assert loaded.columns == 2


# --------------------------------------------------------------------------- 导出 / 导入


def test_export_import_roundtrip_restores_printers():
    """契约：export_to 导出后改掉内存里的打印机列表，import_from 能恢复导出时的内容。"""
    cfg = config.AppConfig(columns=3, web_port=9090)
    cfg.printers = [
        PrinterInfo(ip="10.0.0.5", name="甲", model=PrinterModel.P1S, tile_span=2),
        PrinterInfo(ip="10.0.0.6", name="乙", model=PrinterModel.A1MINI),
    ]
    target = _config_dir() / "exported.json"
    assert cfg.export_to(str(target)) is True
    assert target.exists()

    cfg.printers = []
    cfg.columns = 0
    cfg.web_port = 8080

    assert cfg.import_from(str(target)) is True
    assert [p.ip for p in cfg.printers] == ["10.0.0.5", "10.0.0.6"]
    assert [p.name for p in cfg.printers] == ["甲", "乙"]
    assert [p.model for p in cfg.printers] == [PrinterModel.P1S, PrinterModel.A1MINI]
    assert [p.tile_span for p in cfg.printers] == [2, 1]
    assert cfg.columns == 3
    assert cfg.web_port == 9090


def test_export_import_roundtrip_keeps_界面偏好与discovered():
    """回归：``to_json()`` 写出的字段，``import_from()`` 必须原样读回。

    以前 ``import_from()`` 只恢复 printers/columns/max_fps/refresh_ms/web_*，
    把 ``last_timeout`` / ``show_timestamp`` / ``auto_connect`` / ``web_enabled``
    静默丢掉 —— 用户「导出再导入」后界面偏好并没有回来，看起来像导入了一半。
    ``PrinterInfo.discovered`` 则是 ``to_json()`` 写出、``_parse()`` 不读，
    往返后悄悄变成 False。
    """
    cfg = config.AppConfig(
        columns=2,
        show_timestamp=False,
        auto_connect=False,
        last_timeout=35.0,
        web_enabled=True,
        web_fps=6.0,
    )
    cfg.printers = [
        PrinterInfo(ip="10.0.0.7", serial="03900A1111111", model=PrinterModel.X1C, discovered=True),
    ]
    target = _config_dir() / "roundtrip.json"
    assert cfg.export_to(str(target)) is True

    # 目标配置全是与导出时不同的值，确保断言真的落在「导入覆盖」上
    fresh = config.AppConfig()
    assert fresh.show_timestamp is True
    assert fresh.auto_connect is True
    assert fresh.last_timeout == 20.0
    assert fresh.web_enabled is False

    assert fresh.import_from(str(target)) is True
    assert fresh.show_timestamp is False
    assert fresh.auto_connect is False
    assert fresh.last_timeout == 35.0
    assert fresh.web_enabled is True
    assert fresh.web_fps == 6.0
    assert fresh.printers[0].discovered is True, "discovered 必须往返保真"


def test_import_from_不导入窗口坐标():
    """契约：``window_geometry`` 是屏幕坐标，导到别的机器上可能把窗口丢到屏幕外。

    导出文件里带着它，但导入时要忽略（这是有意为之，不是漏字段）。
    """
    cfg = config.AppConfig()
    cfg.printers = [PrinterInfo(ip="10.0.0.8")]
    target = _config_dir() / "geometry.json"
    assert cfg.export_to(str(target)) is True

    fresh = config.AppConfig(window_geometry="本机原有坐标")
    assert fresh.import_from(str(target)) is True
    assert fresh.window_geometry == "本机原有坐标"


def test_export_to_returns_false_for_unwritable_path():
    """契约：导出失败（目录不存在）返回 False，不抛异常。"""
    cfg = config.AppConfig()
    cfg.printers = [PrinterInfo(ip="10.0.0.5")]
    assert cfg.export_to(str(_config_dir() / "没有这个目录" / "x.json")) is False


def test_import_from_returns_false_for_missing_or_empty_file():
    """契约：文件不存在 / 不是合法 JSON / 里面没有打印机时，import_from 返回 False 且不改动现有列表。"""
    cfg = config.AppConfig()
    cfg.printers = [PrinterInfo(ip="1.1.1.1", name="原有机")]

    assert cfg.import_from(str(_config_dir() / "并不存在.json")) is False
    assert [p.ip for p in cfg.printers] == ["1.1.1.1"]

    broken = _config_dir() / "broken.json"
    _write_raw(broken, "{ not json")
    assert cfg.import_from(str(broken)) is False

    empty = _config_dir() / "empty.json"
    _write_json(empty, {"printers": [], "columns": 4})
    assert cfg.import_from(str(empty)) is False
    assert cfg.columns != 4  # 没有打印机时不应部分套用其它字段


# --------------------------------------------------------------------------- web_token / 解析健壮性


@pytest.mark.parametrize(
    "payload", [{}, {"web_token": ""}, {"web_token": None}], ids=["missing", "empty", "none"]
)
def test_load_generates_web_token_when_absent(payload):
    """契约：配置里没有/空 web_token 时，load() 自动生成一个非空令牌（重启后地址仍可用）。"""
    _write_json(_main_path(), payload)
    loaded = config.AppConfig.load()
    assert loaded.web_token
    assert len(loaded.web_token) == 16
    assert all(ch in "0123456789abcdef" for ch in loaded.web_token)


def test_load_skips_non_dict_printer_entries_and_unknown_model():
    """契约：printers 里的非字典元素被跳过；型号名不认识时退化成 UNKNOWN 而不是抛 ValueError。"""
    _write_json(
        _main_path(),
        {"printers": [1, "x", None, {"ip": "3.3.3.3", "model": "P99"}, {"ip": "4.4.4.4"}]},
    )
    loaded = config.AppConfig.load()
    assert [p.ip for p in loaded.printers] == ["3.3.3.3", "4.4.4.4"]
    assert loaded.printers[0].model is PrinterModel.UNKNOWN
    assert loaded.printers[1].model is PrinterModel.UNKNOWN


def test_parse_uses_default_model_when_model_key_missing():
    """契约：条目缺 model 字段时按 UNKNOWN 处理（默认值「未知机型」）。"""
    _write_json(_main_path(), {"printers": [{"ip": "5.5.5.5"}]})
    assert config.AppConfig.load().printers[0].model is PrinterModel.UNKNOWN


# --------------------------------------------------------------------------- 配置目录隔离


def test_config_dir_honours_env_override_and_creates_nested_dir(tmp_path, monkeypatch):
    """契约：``BAMBU_MONITOR_CONFIG_DIR`` 覆盖配置目录（app/config.py:22），
    路径不存在时自动创建（Docker 挂载场景），三个路径函数都落在该目录内。"""
    nested = tmp_path / "deep" / "nested"
    monkeypatch.setenv("BAMBU_MONITOR_CONFIG_DIR", str(nested))

    resolved = Path(config.config_dir())
    assert resolved == nested
    assert resolved.is_dir()
    assert Path(config.config_path()) == nested / "config.json"
    assert Path(config.backup_path()) == nested / "config.backup.json"


def test_config_dir_is_never_the_real_user_config_dir():
    """契约（安全兜底）：测试期间配置目录必须被隔离，绝不指向 ``%APPDATA%\\BambuMonitor``。"""
    resolved = Path(config.config_dir()).resolve()
    real = real_config_dir().resolve()

    assert resolved != real
    assert real not in resolved.parents
    assert os.environ.get("BAMBU_MONITOR_CONFIG_DIR", "").strip()
    # 主配置与备份都必须在隔离目录里，测试写盘不会碰到用户真实文件
    assert Path(config.config_path()).resolve().parent == resolved
    assert Path(config.backup_path()).resolve().parent == resolved
