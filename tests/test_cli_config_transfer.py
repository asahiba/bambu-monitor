"""命令行导入/导出配置（无界面版与桌面入口的参数）。

## 为什么单独立一条

网页端与桌面版都有界面入口，但 **NAS / Docker / 树莓派用户只有命令行**。
他们要在换机器时搬配置，就只剩这一条路 —— 而这条路以前只有
`--export-config`（还是"仅本机可用"的加密），没有导入，也不能设口令。

现在两个入口都有 `--export-config` / `--import-config` / `--config-passphrase`，
本文件把参数解析与三条关键结果都测掉（成功 / 口令错 / 没给口令）。
"""

from __future__ import annotations

import json

import pytest

from app import config
from app.bambu.models import PrinterInfo, PrinterModel

pytestmark = pytest.mark.usefixtures("isolated_config_dir", "no_network")


def _seed() -> config.AppConfig:
    cfg = config.AppConfig()
    cfg.printers = [
        PrinterInfo(
            ip="10.9.9.9",
            serial="01P00A0000009",
            name="测试机",
            model=PrinterModel.P1S,
            access_code="11223344",
        )
    ]
    cfg.save()
    return cfg


def _headless(argv: list[str]) -> int:
    from app.headless import run_headless

    return run_headless(argv)


def test_无界面版_带口令导出后能在别处导入(tmp_path):
    """核心用法：Docker 上导出 -> 另一台机器导入，访问代码完整。"""
    _seed()
    target = tmp_path / "portable.json"
    assert _headless(["--export-config", str(target), "--config-passphrase", "跨机口令"]) == 0

    raw = json.loads(target.read_text(encoding="utf-8"))
    assert raw["portable"] is True
    assert "11223344" not in target.read_text(encoding="utf-8"), "不得出现明文访问代码"

    # 模拟另一台机器：清空内存里的设备列表，但**不动**磁盘上的密钥文件
    fresh = config.AppConfig()
    assert fresh.import_from(str(target), "跨机口令") is True
    assert fresh.printers[0].access_code == "11223344"
    assert fresh.printers[0].name == "测试机"


def test_无界面版_不带口令导出会提示怎么跨机(tmp_path, capsys):
    """不带口令时要说清"只有本机能恢复"，并指出下一步该加什么参数。"""
    _seed()
    target = tmp_path / "local.json"
    assert _headless(["--export-config", str(target)]) == 0
    out = capsys.readouterr().out
    assert "只有本机能恢复" in out
    assert "--config-passphrase" in out, "要告诉用户怎么才能跨机用"


def test_无界面版_口令错时报错并保留原配置(tmp_path):
    _seed()
    target = tmp_path / "portable.json"
    _headless(["--export-config", str(target), "--config-passphrase", "对的"])

    assert _headless(["--import-config", str(target), "--config-passphrase", "错的"]) == 1
    assert config.AppConfig.load().printers[0].access_code == "11223344", "原配置不能被破坏"


def test_无界面版_没给口令但文件加密时给出可读提示(tmp_path, capsys):
    _seed()
    target = tmp_path / "portable.json"
    _headless(["--export-config", str(target), "--config-passphrase", "口令"])

    assert _headless(["--import-config", str(target)]) == 1
    out = capsys.readouterr().out
    assert "口令" in out, "要明确说是缺口令，而不是笼统的失败"


def test_无界面版_导入本机加密的文件并给出提示(tmp_path, capsys):
    """同机导入（不带口令）应当成功；这是最常见的"备份再恢复"用法。"""
    _seed()
    target = tmp_path / "local.json"
    _headless(["--export-config", str(target)])

    # 先把配置清空，模拟"恢复备份"
    cfg = config.AppConfig()
    cfg.printers = []
    cfg.save()

    assert _headless(["--import-config", str(target)]) == 0
    assert config.AppConfig.load().printers[0].ip == "10.9.9.9"


def test_桌面入口也支持同一组参数(tmp_path):
    """契约：`python -m app --export-config ... --config-passphrase ...` 同样可用。

    打包版（`BambuMonitor-cli.exe`）走的就是这个入口，所以它必须与无界面版一致。
    """
    from app.main import build_parser

    _seed()
    parser = build_parser()
    args = parser.parse_args(["--export-config", str(tmp_path / "p.json"), "--config-passphrase", "k"])
    assert args.export_config.endswith("p.json")
    assert args.config_passphrase == "k"
    args = parser.parse_args(["--import-config", str(tmp_path / "p.json")])
    assert args.import_config.endswith("p.json")
    assert args.config_passphrase == "", "不传口令时应当是空串（= 本机加密）"


def test_参数都没给时不受影响(tmp_path):
    """回归：加了这些开关之后，正常启动路径（无这些参数）不该被改变。"""
    from app.main import build_parser

    args = build_parser().parse_args([])
    assert args.export_config is None
    assert args.import_config is None
    assert args.config_passphrase == ""
