"""设备族「接线」的契约测试：从配置到界面，整条链路都要能走通。

## 为什么单独立一条

`app/core/registry.py` + `app/adapters/moonraker/` 曾经是**代码就绪、线路未接**：
适配器 34 条用例全绿，但 `session_factory` 是 ``None``、全仓库只有测试在调
`all_families()`，`PrinterInfo` 也没有 `family`/`port`/`api_key` 字段 ——
配置里就算写了 `family: "moonraker"`，会话仍会按拓竹处理（去连 8883，必然失败）。

这条链路跨了五个层（models → config → registry → 会话 −→ 界面视图），
任何一处漏掉都会表现成"界面里看不到/填不对"，而且**不会报错**，
所以每一段都在这里单独钉一遍：

1. 注册表：每个登记过的族都必须有 ``session_factory``；
2. `create_session()`：按族返回不同实现，端口/凭据从配置来；
3. 配置往返：新字段（family/port/api_key/camera_url）不丢，且 **API Key 与访问代码
   一样加密落盘**（明文写盘等于把凭据放在桌面上）；
4. 跨版本：带口令导出的文件在另一台机器上导入后，第三方凭据也能解回来；
5. 界面视图 `display_status()`：通用状态（`DeviceStatus`）能被界面当成拓竹状态读；
6. 命令行：`--add-printer` 支持 ``族id@名称`` 形式（Docker/NAS 用户只有命令行）；
7. 端到端：假 Moonraker 服务器 → `create_session()` → 界面视图，一路看得到进度。
"""

from __future__ import annotations

import json
import time

import pytest

from app.adapters.moonraker import MoonrakerAdapter
from app.adapters.moonraker.fake import FakeMoonraker
from app.bambu.models import PrinterInfo, PrinterModel, PrinterStatus
from app.bambu.printer import PrinterSession
from app.config import AppConfig
from app.core import registry
from app.core.device import JOB_PRINTING, DeviceStatus, display_status
from app.core.registry import (
    FAMILY_BAMBU,
    FAMILY_MOONRAKER,
    CredentialPolicy,
    FamilyDescriptor,
    all_families,
    create_session,
    credential_label,
    credential_of,
    default_port,
    display_model,
    has_credential,
    is_registered,
    resolve_family,
)

MOONRAKER_INFO = dict(ip="127.0.0.1", name="Voron", family=FAMILY_MOONRAKER)


def moonraker_info(**overrides) -> PrinterInfo:
    data = dict(MOONRAKER_INFO)
    data.update(overrides)
    return PrinterInfo(**data)


def bambu_info(**overrides) -> PrinterInfo:
    data = dict(
        ip="192.168.31.110",
        serial="20P00A1234567",
        name="车间 X2D",
        model=PrinterModel.X2D,
        access_code="12345678",
    )
    data.update(overrides)
    return PrinterInfo(**data)


def _wait_until(predicate, timeout: float = 5.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


# ------------------------------------------------------------------ 1. 注册表
def test_每个登记过的设备族都必须有会话工厂():
    """契约：登记了族却建不出会话＝界面里出现一个选了也没用的选项。

    `app/adapters/__init__.py` 的既有约定是"实现完再登记"，这条测试把它变成
    可执行的判据：``session_factory`` 为空就红。
    """
    families = all_families()
    assert families, "一个设备族都没有？注册表可能没被初始化"
    for descriptor in families:
        assert descriptor.session_factory is not None, (
            f"{descriptor.family}（{descriptor.label}）登记了却没有 session_factory："
            "用户能选中它，但永远连不上"
        )
    assert {item.family for item in families} >= {FAMILY_BAMBU, FAMILY_MOONRAKER}


def test_未登记的族不出现():
    """契约：OctoPrint 只有常量、没有适配器，就不能出现在可选列表里。"""
    assert FAMILY_MOONRAKER in {item.family for item in all_families()}
    assert not is_registered(registry.FAMILY_OCTOPRINT), "适配器未落地就不该登记"


# --------------------------------------------------------------- 2. create_session
def test_create_session按族返回不同实现():
    """契约：同一个入口，按配置里的 family 给出不同会话实现。"""
    assert isinstance(create_session(bambu_info()), PrinterSession)
    adapter = create_session(moonraker_info(port=7125))
    try:
        assert isinstance(adapter, MoonrakerAdapter)
        # 端口与地址来自配置：填了就用填的，没填用族默认端口
        assert adapter.base_url == "http://127.0.0.1:7125"
    finally:
        adapter.stop()


def test_没填端口时用族的默认端口():
    """契约：没填端口就用族的默认端口（Moonraker 官方默认 7125，不是拓竹的 8883）。

    ⚠️ Snapmaker U1 出厂配置前面挂了 nginx，**80 端口也能访问**（见 registry 的
    ``candidate_ports``）。这里刻意不自动回退：会话创建发生在界面线程上，
    多试一个端口就是多等一次超时；界面上把候选端口作为提示显示，由用户填。
    """
    assert default_port(moonraker_info()) == 7125
    assert default_port(bambu_info()) == 8883
    assert default_port(moonraker_info(port=80)) == 80
    adapter = create_session(moonraker_info())
    try:
        assert adapter.base_url == "http://127.0.0.1:7125"
    finally:
        adapter.stop()


def test_APIKey从配置传进会话():
    """契约：``info.api_key`` 必须传给适配器（否则需要鉴权的设备连不上）。"""
    adapter = create_session(moonraker_info(api_key="k-123", camera_url="http://x/s.jpg"))
    try:
        assert adapter.api_key == "k-123"
        assert adapter.capabilities.has_camera is True
    finally:
        adapter.stop()


def test_没有会话实现的族给出明确错误(monkeypatch):
    """契约：只登记了探测能力的族要**当场报清楚**，而不是静默按拓竹连。

    （静默降级正是这次要修的老毛病：配置写着 moonraker，程序去连 8883。）
    """
    descriptor = FamilyDescriptor(
        family="future",
        label="未来的族",
        credential=CredentialPolicy(key="token", label="令牌", required=False),
    )
    monkeypatch.setitem(registry._REGISTRY, "future", descriptor)  # noqa: SLF001
    info = PrinterInfo(ip="1.2.3.4", family="future")
    with pytest.raises(RuntimeError) as info_error:
        create_session(info)
    assert "未来的族" in str(info_error.value)


# ------------------------------------------------------------ 3. 凭据与显示名
def test_凭据字段与显示名按族取值():
    """契约：凭据按族的 ``CredentialPolicy.key`` 取，显示名不写死拓竹机型。"""
    bambu = bambu_info()
    assert credential_of(bambu) == "12345678"
    assert credential_label(bambu) == "访问代码"
    assert has_credential(bambu) is True
    assert display_model(bambu) == PrinterModel.X2D.label
    assert resolve_family(bambu).family == FAMILY_BAMBU

    moon = moonraker_info(api_key="abc")
    assert credential_of(moon) == "abc"
    assert credential_label(moon) == "API Key"
    assert has_credential(moon) is True
    assert display_model(moon) == "Klipper / Moonraker（含 Snapmaker U1）"
    assert resolve_family(moon).family == FAMILY_MOONRAKER

    # 内网 Moonraker 免鉴权：没填凭据也不算"配置不全"
    empty = moonraker_info()
    assert has_credential(empty) is False
    assert resolve_family(empty).credential.required is False


# ------------------------------------------------------------------ 4. 配置往返
def test_旧配置没有族字段时仍按拓竹处理(isolated_config_dir):
    """契约：**老配置必须免迁移** —— 没有 family 字段就是拓竹。"""
    path = isolated_config_dir / "config.json"
    path.write_text(
        json.dumps(
            {
                "printers": [
                    {"ip": "192.168.1.50", "serial": "01P", "name": "老机器", "model": "P1S"}
                ]
            }
        ),
        encoding="utf-8",
    )
    config = AppConfig.load()
    assert len(config.printers) == 1
    info = config.printers[0]
    assert info.family == ""
    assert resolve_family(info).family == FAMILY_BAMBU
    assert isinstance(create_session(info), PrinterSession)


def test_第三方设备字段能往返且凭据不明文落盘(isolated_config_dir):
    """契约：family/port/api_key/camera_url 存得住；**API Key 必须加密**。"""
    config = AppConfig()
    config.printers.append(
        moonraker_info(
            port=7125,
            api_key="super-secret-key",
            camera_url="http://127.0.0.1/webcam/?action=snapshot",
        )
    )
    config.persist = True
    config.save()
    assert not config.last_error, config.last_error

    raw = (isolated_config_dir / "config.json").read_text(encoding="utf-8")
    assert "super-secret-key" not in raw, "API Key 明文写盘了（和访问代码一样必须加密）"
    assert "api_key" in raw

    reloaded = AppConfig.load()
    info = reloaded.printers[0]
    assert info.family == FAMILY_MOONRAKER
    assert info.port == 7125
    assert info.api_key == "super-secret-key"
    assert info.camera_url.endswith("action=snapshot")


def test_带口令导出后第三方凭据也能跨机导入(isolated_config_dir, tmp_path):
    """契约：带口令的导出文件在**别的机器**上导入，API Key 也要解得回来。

    这正是"在电脑上导出、在平板上导入"的用法：第三方族的凭据必须和访问代码
    走同一套 ``bmp1:`` 口令加密，否则换了机器就只剩一个空 API Key。
    """
    source = AppConfig()
    source.printers.append(moonraker_info(api_key="portable-secret", port=7125))
    target_file = tmp_path / "export.json"
    assert source.export_to(str(target_file), "口令123") is True

    text = target_file.read_text(encoding="utf-8")
    assert "portable-secret" not in text
    assert AppConfig.needs_passphrase(str(target_file)) is True

    other = AppConfig()
    assert other.import_from(str(target_file), "口令123") is True
    assert other.printers[0].api_key == "portable-secret"
    assert other.printers[0].family == FAMILY_MOONRAKER
    assert other.printers[0].port == 7125


def test_口令不对时第三方凭据不会被偷偷导入成空(isolated_config_dir, tmp_path):
    """契约：口令错＝导入失败（不能"成功"地导进一堆空凭据）。"""
    source = AppConfig()
    source.printers.append(moonraker_info(api_key="portable-secret"))
    target_file = tmp_path / "export.json"
    assert source.export_to(str(target_file), "口令123") is True

    other = AppConfig()
    assert other.import_from(str(target_file), "错口令") is False
    assert "口令" in other.last_error


# ------------------------------------------------------------------ 5. 界面视图
def test_界面视图对拓竹状态是无损的():
    """契约：拓竹对象过 `display_status()` 后**逐字段等同原对象**。

    这是"接第三方族不回归拓竹界面"的保证：视图对拓竹只做透传。
    """
    status = PrinterStatus(
        mqtt_online=True,
        camera_online=True,
        gcode_state="RUNNING",
        progress=47,
        remaining_minutes=95,
        subtask_name="benchy.gcode.3mf",
        layer_num=12,
        total_layer_num=240,
        nozzle_temper=245.3,
        bed_temper=60.0,
        wifi_signal="-52dBm",
        lights={"chamber_light": "on"},
        print_error=0,
    )
    view = display_status(status)
    for name in (
        "state_text",
        "progress",
        "remaining_text",
        "finish_time_text",
        "subtask_name",
        "layer_num",
        "total_layer_num",
        "gcode_state",
        "nozzle_temper",
        "bed_temper",
        "wifi_signal",
        "wifi_level",
        "light_on",
        "is_printing",
        "is_paused",
        "mqtt_online",
        "camera_online",
    ):
        assert getattr(view, name) == getattr(status, name), name
    assert view.ams_trays == []
    assert view.hms_items == []
    assert view.vt_tray is None
    assert view.lights == {"chamber_light": "on"}


def test_界面视图把通用状态翻译成界面字段():
    """契约：`DeviceStatus` 的通用字段名要能喂给现有界面（不用改界面代码）。"""
    status = DeviceStatus()
    status.apply_mapped(
        {
            "job_state": JOB_PRINTING,
            "progress_percent": 42,
            "job_name": "benchy.gcode",
            "layer_current": 12,
            "layer_total": 240,
            "remaining_minutes": 95,
            "nozzle_temper": 245.3,
            "bed_temper": 60.0,
            "wifi_rssi_dbm": -52.0,
            "light_on": True,
        }
    )
    view = display_status(status)
    assert view.state_text == "打印中"
    assert view.progress == 42
    assert view.subtask_name == "benchy.gcode"
    assert view.layer_num == 12 and view.total_layer_num == 240
    assert view.remaining_text == "1小时35分"
    assert view.gcode_state == JOB_PRINTING
    assert view.wifi_signal == "-52dBm" and view.wifi_level == 4
    assert view.light_on is True
    assert view.is_printing is True and view.is_paused is False
    assert view.finish_time_text != ""


def test_界面视图在字段缺失时给安全默认值():
    """契约：第三方族没上报的字段不能让界面崩（`AttributeError`＝整个画面白屏）。"""
    view = display_status(DeviceStatus())
    assert view.state_text == "未知"
    assert view.progress == 0, "未知进度（-1）按 0 显示，与拓竹侧行为一致"
    assert view.remaining_text == "--"
    assert view.layer_num == 0 and view.total_layer_num == 0
    assert view.wifi_signal == "" and view.wifi_level == 0
    assert view.light_on is None
    assert view.ams_trays == [] and view.hms_items == [] and view.lights == {}
    assert view.subtask_name == ""


def test_进度未知时不会显示成52或负数():
    """契约：``progress_percent = -1``（未知）夹到 0，不能出现负数进度。"""
    status = DeviceStatus()
    status.progress_percent = -1
    assert display_status(status).progress == 0
    status.progress_percent = 250
    assert display_status(status).progress == 100


def test_重复包装不叠加():
    """契约：`display_status()` 幂等（界面多处调用，包两层不能出问题）。"""
    view = display_status(PrinterStatus(gcode_state="IDLE"))
    assert display_status(view) is view


# ------------------------------------------------------------------ 6. 命令行
def test_命令行规格解析支持第三方族():
    """契约：``--add-printer`` 能加第三方设备（Docker/NAS 用户只有命令行）。

    格式：``"名称 IP 凭据 [端口]"``，族用 ``族id@名称`` 指定。
    """
    from app.headless import _parse_add_spec

    info, error = _parse_add_spec("X2D 192.168.31.110 12345678")
    assert error == "" and info.family == "" and info.access_code == "12345678"

    info, error = _parse_add_spec("moonraker@Voron 192.168.1.90 abc123 7125")
    assert error == ""
    assert info.family == FAMILY_MOONRAKER
    assert info.name == "Voron" and info.api_key == "abc123" and info.port == 7125
    assert info.access_code == "", "第三方族的凭据绝不该写进 access_code"

    # 免鉴权族可以不填凭据
    info, error = _parse_add_spec("moonraker@U1 192.168.1.91")
    assert error == "" and info.api_key == "" and info.port == 0


@pytest.mark.parametrize(
    "spec, keyword",
    [
        ("只有名字", "格式不正确"),
        ("moonraker@Voron 192.168.1.90 abc 70000", "端口超出范围"),
        ("moonraker@Voron 192.168.1.90 abc http", "端口不是数字"),
        ("Voron 192.168.1.90", "访问代码"),
        ("nosuch@Voron 192.168.1.90", "不认识的设备族"),
        ("Voron 192.168.1.90 12345678 80 extra", "字段太多"),
    ],
)
def test_命令行规格的错误要说得清(spec, keyword):
    """契约：写错规格要报**人能看懂**的原因（并返回退出码 2）。"""
    from app.headless import _parse_add_spec

    info, error = _parse_add_spec(spec)
    assert info is None
    assert keyword in error, error


# ------------------------------------------------------------------ 7. 端到端
def test_假Moonraker经过create_session后界面看得到进度():
    """端到端：配置 → `create_session()` → 界面视图，一路读到进度/状态/温度。

    这条覆盖的是"接线"本身：适配器单独能用（`tests/test_moonraker.py`）不等于
    产品路径能用 —— 中间还隔着注册表、端口解析与界面视图。
    """
    with FakeMoonraker() as fake:
        host, _, port = fake.base_url.rpartition(":")
        info = moonraker_info(ip=host.replace("http://", ""), port=int(port))
        session = create_session(info)
        try:
            session.start()
            assert _wait_until(lambda: session.snapshot().online), "端到端没有拿到状态"
            view = display_status(session.snapshot())
            assert view.state_text == "打印中"
            assert view.progress == 42
            assert view.subtask_name == "benchy.gcode"
            assert view.layer_num == 12 and view.total_layer_num == 240
            assert f"{view.nozzle_temper:.0f}" == "245"
            assert view.lights == {} and view.hms_items == []
        finally:
            session.stop()


def test_网页接口把设备族表交给前端():
    """契约：`/api/printers` 的 `families` 与注册表一致（前端据此渲染表单）。

    前端**不写死**任何族特有文案：标签/必填性/端口都来自这里。
    """
    from app.web.server import _families_payload

    payload = _families_payload()
    by_family = {item["family"]: item for item in payload}
    assert set(by_family) == {item.family for item in all_families()}
    moon = by_family[FAMILY_MOONRAKER]
    assert moon["credential"]["key"] == "api_key"
    assert moon["credential"]["label"] == "API Key"
    assert moon["credential"]["required"] is False
    assert moon["candidate_ports"] == [80, 7125]
    bambu = by_family[FAMILY_BAMBU]
    assert bambu["credential"]["key"] == "access_code"
    assert bambu["credential"]["required"] is True
