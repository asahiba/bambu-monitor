"""``app/bambu/models.py`` 纯逻辑契约测试。

覆盖：机型表（视频通道 / RTSPS / 腔温）、``detect_model`` 识别优先级、
``PrinterStatus.apply_report`` 的增量合并语义、余量 / 信号 / 状态文案、
AMS 槽位与外挂料盘的解析、``active_tray`` 与 ``stale``、HMS 短码。
"""

from __future__ import annotations

import time

import pytest

from app.bambu.models import (
    AmsTray,
    HmsItem,
    PrinterInfo,
    PrinterModel,
    PrinterStatus,
    _normalize_model_text,
    _parse_ams,
    _parse_hms,
    _parse_tray,
    detect_model,
)

#: 本文件是纯逻辑层测试：禁止任何 socket 连接/监听（不连真机、不占端口）
pytestmark = pytest.mark.usefixtures("no_network")

# --------------------------------------------------------------------------- 机型表


@pytest.mark.parametrize(
    "model, expected",
    [
        (PrinterModel.A1, "tcp6000"),
        (PrinterModel.A1MINI, "tcp6000"),
        # A2L 是实测机型：8883 + 6000 开放、322 不可达（docs/FIELD_NOTES.md），
        # 所以固定走 6000，不应该像未知机型那样先去白试一次 RTSPS
        (PrinterModel.A2L, "tcp6000"),
        (PrinterModel.P1P, "tcp6000"),
        (PrinterModel.P1S, "tcp6000"),
        (PrinterModel.X2D, "rtsp"),
        (PrinterModel.H2D, "rtsp"),
        (PrinterModel.H2S, "rtsp"),
        (PrinterModel.P2S, "rtsp"),
        (PrinterModel.X1, "auto"),
        (PrinterModel.X1C, "auto"),
        (PrinterModel.X1E, "auto"),
        (PrinterModel.UNKNOWN, "auto"),
    ],
)
def test_video_channel_mapping(model, expected):
    """契约：A1/P1/A2L 走 6000 端口，X2D/H2/P2S 走 RTSPS，X1 系列与未知机型为 auto。"""
    assert model.video_channel == expected


@pytest.mark.parametrize(
    "model, expected",
    [
        (PrinterModel.X1, True),
        (PrinterModel.X1C, True),
        (PrinterModel.X1E, True),
        (PrinterModel.X2D, True),
        (PrinterModel.H2D, True),
        (PrinterModel.H2S, True),
        (PrinterModel.P2S, True),
        (PrinterModel.A1, False),
        (PrinterModel.A1MINI, False),
        (PrinterModel.A2L, False),  # 实测 322 端口不可达
        (PrinterModel.P1P, False),
        (PrinterModel.P1S, False),
        (PrinterModel.UNKNOWN, False),
    ],
)
def test_supports_rtsp_mapping(model, expected):
    """契约：只有 X1/X2D/H2/P2S 系列开放本地 RTSPS(322)，A1/P1/A2L 与未知机型没有。"""
    assert model.supports_rtsp is expected


@pytest.mark.parametrize(
    "model, expected",
    [
        (PrinterModel.P1S, False),
        (PrinterModel.P1P, False),
        (PrinterModel.A1, False),
        (PrinterModel.A1MINI, False),
        # 保守假设：A2L 的腔温传感器尚未实测（docs/FIELD_NOTES.md）。
        # 若日后确认它有腔温，把 A2L 加进 has_chamber_sensor 集合并改这条断言即可。
        (PrinterModel.A2L, False),
        (PrinterModel.X2D, True),
        (PrinterModel.X1C, True),
        (PrinterModel.X1, True),
        (PrinterModel.X1E, True),
        (PrinterModel.H2D, True),
        (PrinterModel.H2S, True),
        (PrinterModel.P2S, True),
    ],
)
def test_has_chamber_sensor_mapping(model, expected):
    """契约：A1/P1 系列固件报的腔温是无效值（实测 P1S 常年 5℃），必须不显示；X1/H2/X2D/P2S 显示。"""
    assert model.has_chamber_sensor is expected


@pytest.mark.parametrize(
    "model, expected",
    [
        (PrinterModel.A1, True),
        (PrinterModel.A1MINI, True),
        (PrinterModel.A2L, True),
        (PrinterModel.P1P, True),
        (PrinterModel.P1S, True),
        (PrinterModel.P2S, True),
        (PrinterModel.H2D, True),
        (PrinterModel.H2S, True),
        (PrinterModel.X1, True),
        (PrinterModel.X1C, True),
        (PrinterModel.X1E, True),
        (PrinterModel.X2D, True),
        (PrinterModel.UNKNOWN, False),
    ],
)
def test_is_known_mapping(model, expected):
    """契约：``is_known`` 取代散落各处的 ``model.value != "未知机型"`` 魔术字符串比较。

    它被用于「发现结果合并时不要用未知机型覆盖已知机型」等逻辑
    （``discovery`` / ``headless`` / ``main_window`` / ``discover_dialog``）。
    """
    assert model.is_known is expected


# --------------------------------------------------------------------------- 机型识别


@pytest.mark.parametrize(
    "serial, expected",
    [
        ("094ABC123", PrinterModel.H2D),
        ("20PABC123", PrinterModel.X2D),
        ("01PABC123", PrinterModel.P1S),
        ("030ABC123", PrinterModel.A1MINI),
        ("093ABC123", PrinterModel.H2S),
        ("039ABC123", PrinterModel.A1),
        ("00MABC123", PrinterModel.X1C),
        ("00WABC123", PrinterModel.X1),
        ("03WABC123", PrinterModel.X1E),
        ("01SABC123", PrinterModel.P1P),
        ("22EABC123", PrinterModel.P2S),
        # 实测：A2L 的序列号是 26A00A000000000000（18 位），前缀 26A
        ("26A00A000000000000", PrinterModel.A2L),
        ("26AABC123", PrinterModel.A2L),
        # 官方 Wiki 的序列号前缀表（https://wiki.bambulab.com/en/general/find-sn）
        ("31B00A123456789", PrinterModel.H2C),
        ("23900A123456789", PrinterModel.H2D_PRO),
    ],
)
def test_detect_model_by_serial_prefix(serial, expected):
    """契约：型号名为空时按序列号前 3 位识别机型（SERIAL_PREFIX_MODEL 表）。"""
    assert detect_model(serial, "") is expected
    assert detect_model(serial) is expected


@pytest.mark.parametrize(
    "dev_model, expected",
    [
        # 新机型用代号上报 devmodel，代号与型号名毫无字面关系，必须查专用表
        ("C11", PrinterModel.P1P),
        ("C12", PrinterModel.P1S),
        ("N9", PrinterModel.A2L),
        ("O1E", PrinterModel.H2D_PRO),  # H2D Pro 代号来源不一致，两个都收
        ("O2D", PrinterModel.H2D_PRO),
        ("c12", PrinterModel.P1S),  # 代号匹配不区分大小写
    ],
)
def test_detect_model_by_dev_model_code(dev_model, expected):
    """契约：`devmodel` 代号表（``DEV_MODEL_CODES``）能识别代号形式的机型。

    P1S 报 ``C12``、A2L 报 ``N9`` —— 若只靠型号名关键字匹配，这些会全部落空，
    退化成「未知机型」并走 auto 通道，新机型可能因此被错误地先去试 6000 端口。
    """
    assert detect_model("", "", dev_model) is expected
    assert detect_model("01P00A123456789", "", dev_model) is expected, (
        "代号应优先于序列号前缀（官方说明换主板后序列号会变）"
    )


def test_h2c_dev_model_codes_both_recognized():
    """契约：H2C 有**两个** devmodel 代号，`O1C`（单喷嘴）与 `O1C2`（双喷嘴）都识别为 H2C。

    漏掉 ``O1C2`` 会让程序把它当未知机型、误用 6000 端口并陷入重连循环
    （社区已记录该故障）。顺序上 ``o1c2`` 必须排在 ``o1c`` 之前，否则会命中后者
    ——两者现在都映射到 H2C，所以顺序不再影响结果，但改动该表时仍要注意。
    """
    assert detect_model("", "", "O1C") is PrinterModel.H2C
    assert detect_model("", "", "O1C2") is PrinterModel.H2C
    assert PrinterModel.H2C.video_channel == "rtsp", "H2C 走 RTSPS(322)"
    assert PrinterModel.H2C.supports_rtsp is True
    assert PrinterModel.H2C.has_chamber_sensor is True


def test_h2d_pro_capabilities_match_h2_family():
    """契约：H2D Pro 与 H2 系同族：RTSPS + 有腔温传感器。"""
    assert PrinterModel.H2D_PRO.video_channel == "rtsp"
    assert PrinterModel.H2D_PRO.supports_rtsp is True
    assert PrinterModel.H2D_PRO.has_chamber_sensor is True


# --------------------------------------------------------------------------- Developer Mode


def test_fun_field_from_real_a2l_report_sets_signature_required():
    """契约：实测 A2L 的 `fun` = "100d122002fbd"（**十六进制字符串**）解析后要求签名。

    这条直接决定「控制按钮能不能用」：该机型未开 Developer Mode 时，
    第三方下发的暂停/停止/开灯会被固件静默忽略。
    """
    status = PrinterStatus()
    assert status.needs_mqtt_signature is None, "没收到 fun 时不得擅自判断"
    assert status.developer_mode is None

    status.apply_report({"print": {"fun": "100d122002fbd"}})
    assert status.fun_bits == 0x100D122002FBD
    assert status.needs_mqtt_signature is True
    assert status.developer_mode is False


def test_fun_field_without_signature_bit_means_developer_mode_on():
    """契约：`fun` 里 bit 0x20000000 为 0 时表示不要求签名（已开 Developer Mode）。

    用实测值清掉该位得到 ``100d102002fbd``（对照实测值 ``100d122002fbd``）。
    """
    status = PrinterStatus()
    status.apply_report({"print": {"fun": "100d102002fbd"}})
    assert status.fun_bits == 0x100D102002FBD
    assert status.needs_mqtt_signature is False
    assert status.developer_mode is True


@pytest.mark.parametrize("raw", ["0x100d122002fbd", "100D122002FBD", 0x100D122002FBD])
def test_fun_field_accepts_hex_prefix_case_and_int(raw):
    """契约：`fun` 的解析要容忍 0x 前缀、大小写，以及少数固件直接给整数。"""
    status = PrinterStatus()
    status.apply_report({"print": {"fun": raw}})
    assert status.fun_bits == 0x100D122002FBD
    assert status.needs_mqtt_signature is True


@pytest.mark.parametrize("raw", ["", "   ", None, "not-hex", {}, []])
def test_fun_field_unparsable_is_unknown_not_false(raw):
    """契约：`fun` 无法解析时必须保持未知（None），**不能**退化成「需要签名」。

    否则老机型（压根没有这个字段）会被误判成需要 Developer Mode 而禁用控制按钮。
    """
    status = PrinterStatus()
    status.apply_report({"print": {"fun": raw}})
    assert status.needs_mqtt_signature is None


def test_fun_field_is_merged_incrementally():
    """契约：P1 系列只推送变化字段，`fun` 一旦收到应被保留（增量合并不清零）。"""
    status = PrinterStatus()
    status.apply_report({"print": {"fun": "100d122002fbd"}})
    status.apply_report({"print": {"mc_percent": 50}})
    assert status.fun_bits == 0x100D122002FBD, "后续报文不带 fun 时不应丢掉已知的功能位"


def test_model_name_wins_over_dev_model_code():
    """契约：完整型号名优先于代号。

    老机型的 ``devmodel`` 直接报完整型号名（例如 ``P1S``），它比两个字符的代号
    更不容易误伤，因此先试型号名关键字。
    """
    assert detect_model("", "P1S", "P1S") is PrinterModel.P1S
    assert detect_model("", "Bambu Lab X1 Carbon", "C12") is PrinterModel.X1C


@pytest.mark.parametrize("serial", ["", "0", "09", "xxx", "999ABC", "   "])
def test_detect_model_unknown_falls_back(serial):
    """契约：序列号不足 3 位、前缀不在表里或为空时返回 UNKNOWN（未知机型）。"""
    assert detect_model(serial, "") is PrinterModel.UNKNOWN


@pytest.mark.parametrize(
    "serial, model_name, expected",
    [
        ("01PABC123", "X1C", PrinterModel.X1C),
        ("094ABC123", "P1S", PrinterModel.P1S),
        ("030ABC123", "H2D", PrinterModel.H2D),
    ],
)
def test_detect_model_model_name_wins_over_serial(serial, model_name, expected):
    """契约：SSDP 型号名关键字优先于序列号前缀（型号名更可靠）。"""
    assert detect_model(serial, model_name) is expected


@pytest.mark.parametrize(
    "model_name, expected",
    [
        ("X1C", PrinterModel.X1C),
        ("x1c", PrinterModel.X1C),
        ("X1-Carbon", PrinterModel.X1C),  # "-" 被去掉 -> x1carbon
        ("X1_Carbon", PrinterModel.X1C),  # "_" 被去掉 -> x1carbon
        ("X1 Carbon", PrinterModel.X1C),  # 空格被去掉 -> x1carbon
        ("Bambu Lab X1 Carbon", PrinterModel.X1C),  # 真机 SSDP devmodel 的全称
        ("A1-MINI", PrinterModel.A1MINI),  # "-" 被去掉 -> a1mini
        ("a1_mini", PrinterModel.A1MINI),  # "_" 被去掉 -> a1mini
        ("  P2S  ", PrinterModel.P2S),
        ("H2D", PrinterModel.H2D),
        ("Bambu Lab X1C", PrinterModel.X1C),
        # A2L：真机 SSDP 的 devname 就是 "A2L"（devmodel 为空），见 docs/FIELD_NOTES.md
        ("A2L", PrinterModel.A2L),
        ("a2l", PrinterModel.A2L),
        ("Bambu Lab A2L", PrinterModel.A2L),
    ],
)
def test_detect_model_normalizes_case_and_separators(model_name, expected):
    """契约：型号名关键字匹配忽略大小写、空格与 ``-`` / ``_``（``_normalize_model_text``）。

    关键字表顺序保证更具体的型号优先：``x1carbon`` / ``x1c`` 必须先于 ``x1`` 命中，
    否则 X1C 会被误认成 X1；同理 ``a2l`` 在 ``a1`` 之前。
    """
    assert detect_model("", model_name) is expected


@pytest.mark.parametrize(
    "model_name, expected",
    [
        # 型号名带空格/连字符全称时，优先级依然压过序列号前缀（X1C 不能被降级成 X1）
        ("Bambu Lab X1 Carbon", PrinterModel.X1C),
        ("X1 Carbon", PrinterModel.X1C),
        ("Bambu Lab X1-Carbon", PrinterModel.X1C),
    ],
)
def test_detect_model_x1_carbon_full_name_beats_x1_keyword(model_name, expected):
    """契约（回归）：SSDP 全称 "Bambu Lab X1 Carbon" 必须识别为 X1C，而不是退化成 X1。"""
    assert detect_model("00MABC123", model_name) is expected


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("Bambu Lab X1 Carbon", "bambulabx1carbon"),
        ("X1-Carbon", "x1carbon"),
        ("X1_Carbon", "x1carbon"),
        ("  A1  mini ", "a1mini"),
        ("", ""),
        ("   ", ""),
    ],
)
def test_normalize_model_text(raw, expected):
    """契约：``_normalize_model_text`` 去掉空白与 ``-``/``_`` 并转小写（子串匹配前的归一化）。"""
    assert _normalize_model_text(raw) == expected


@pytest.mark.parametrize(
    "serial, expected",
    [
        # 源码只对 model_name 做 replace("-","")，序列号只做 strip+upper；
        # 好在序列号前缀只看前 3 位，"-" 出现在第 4 位之后不影响识别。
        ("094-ABC123", PrinterModel.H2D),
        ("094_ABC123", PrinterModel.H2D),
        # 前缀位置就是 "-"，则无法识别：这是真实实现的行为（此处如实固定下来）
        ("-094ABC123", PrinterModel.UNKNOWN),
        # 小写序列号会被 upper 处理
        ("094abc123", PrinterModel.H2D),
    ],
)
def test_detect_model_serial_normalization_is_case_only(serial, expected):
    """契约（如实记录真实实现）：序列号只做 strip+upper，``-`` / ``_`` 的剥离仅作用于型号名。"""
    assert detect_model(serial) is expected


# --------------------------------------------------------------------------- 报文合并


def test_apply_report_merges_incrementally():
    """契约（重点）：P1 系列只推变化字段，``apply_report`` 必须增量合并而不是覆盖。

    第一条报文给 mc_percent / nozzle_temper，第二条只给 bed_temper；
    合并后 progress 与 nozzle_temper 必须仍是 47 / 245.3。
    """
    status = PrinterStatus()
    assert status.apply_report({"print": {"mc_percent": 47, "nozzle_temper": 245.3}}) is True
    assert status.progress == 47
    assert status.nozzle_temper == pytest.approx(245.3)

    assert status.apply_report({"print": {"bed_temper": 60}}) is True
    assert status.bed_temper == pytest.approx(60.0)
    # 增量语义：未被推送的字段保持旧值
    assert status.progress == 47
    assert status.nozzle_temper == pytest.approx(245.3)


def test_apply_report_merges_three_messages_and_raw():
    """契约：连续多条局部报文累积成完整状态，且原始字段都留在 ``raw`` 里。"""
    status = PrinterStatus()
    status.apply_report({"print": {"gcode_state": "RUNNING", "mc_percent": 10}})
    status.apply_report({"print": {"mc_remaining_time": 90}})
    status.apply_report({"print": {"layer_num": 5, "total_layer_num": 200}})

    assert status.gcode_state == "RUNNING"
    assert status.progress == 10
    assert status.remaining_minutes == 90
    assert (status.layer_num, status.total_layer_num) == (5, 200)
    assert status.raw["mc_percent"] == 10
    assert status.raw["total_layer_num"] == 200


@pytest.mark.parametrize(
    "payload",
    [
        {"print": 5},
        {"print": "RUNNING"},
        {"print": []},
        {"print": None},
        {},
        {"other": {"mc_percent": 99}},
    ],
    ids=["print-int", "print-str", "print-list", "print-none", "empty-dict", "no-print-key"],
)
def test_apply_report_rejects_invalid_payload_without_touching_state(payload):
    """契约：``print`` 不是字典（或报文里没有 print）时返回 False，且不得改动任何状态。"""
    status = PrinterStatus()
    status.apply_report({"print": {"mc_percent": 47, "nozzle_temper": 245.3, "bed_temper": 60}})
    before = (
        status.progress,
        status.nozzle_temper,
        status.bed_temper,
        status.gcode_state,
        status.last_message_ts,
        status.last_error,
    )

    assert status.apply_report(payload) is False
    assert (
        status.progress,
        status.nozzle_temper,
        status.bed_temper,
        status.gcode_state,
        status.last_message_ts,
        status.last_error,
    ) == before


@pytest.mark.parametrize("bad_value", [None, "四十七", "", {}, []])
def test_apply_report_keeps_previous_value_on_unparsable_number(bad_value):
    """契约：字段值无法转成数字时沿用旧值（``_as_int`` / ``_as_float`` 的 default 语义），不得清零。"""
    status = PrinterStatus()
    status.apply_report({"print": {"mc_percent": 47, "nozzle_temper": 245.3}})
    status.apply_report({"print": {"mc_percent": bad_value, "nozzle_temper": bad_value}})
    assert status.progress == 47
    assert status.nozzle_temper == pytest.approx(245.3)


def test_apply_report_ams_none_clears_trays():
    """契约：显式推送 ``"ams": null`` 表示 AMS 已断开，应清空槽位并置 ams_exist=False。"""
    status = PrinterStatus()
    status.apply_report(_ams_report())
    assert status.ams_trays

    status.apply_report({"print": {"ams": None}})
    assert status.ams_trays == []
    assert status.ams_exist is False


def test_apply_report_updates_last_message_ts():
    """契约：成功合并一条报文后 ``last_message_ts`` 更新，``stale`` 立即变为 False。"""
    status = PrinterStatus()
    assert status.stale is True
    status.apply_report({"print": {"mc_percent": 1}})
    assert status.last_message_ts > 0
    assert status.stale is False


# --------------------------------------------------------------------------- 文案与信号


@pytest.mark.parametrize(
    "minutes, expected",
    [
        (0, "--"),
        (-5, "--"),
        (1, "1分钟"),
        (45, "45分钟"),
        (59, "59分钟"),
        (60, "1小时00分"),
        (90, "1小时30分"),
        (125, "2小时05分"),
    ],
)
def test_remaining_text(minutes, expected):
    """契约：剩余时间为 0 或负数显示 ``--``；不足 1 小时显示 N 分钟，超过则显示 N小时MM分。"""
    status = PrinterStatus(remaining_minutes=minutes)
    assert status.remaining_text == expected


@pytest.mark.parametrize(
    "signal, expected",
    [
        ("-50", 4),
        ("-55", 4),
        ("-60", 3),
        ("-65", 3),
        ("-70", 2),
        ("-75", 2),
        ("-80", 1),
        ("-85", 1),
        ("-95", 0),
        ("-45dBm", 4),  # 真实报文字段形如 "-45dBm"
        ("", 0),
        ("abc", 0),
        ("--", 0),
    ],
)
def test_wifi_level(signal, expected):
    """契约：把 dBm 字符串换算成 0-4 格信号；空串或非数字一律 0 格（不得抛异常）。"""
    assert PrinterStatus(wifi_signal=signal).wifi_level == expected


@pytest.mark.parametrize(
    "state, expected",
    [
        ("RUNNING", "打印中"),
        ("PAUSE", "已暂停"),
        ("PAUSED", "已暂停"),
        ("IDLE", "空闲"),
        ("FINISH", "打印完成"),
        ("FAILED", "打印失败"),
        ("running", "打印中"),  # 大小写不敏感
        ("", "未知"),
    ],
)
def test_state_text(state, expected):
    """契约：gcode_state 映射成中文；空值显示「未知」，大小写不敏感。"""
    assert PrinterStatus(gcode_state=state).state_text == expected


def test_state_text_unknown_state_falls_back_to_raw():
    """契约：表里没有的状态原样返回（而不是变成「未知」，避免掩盖固件新状态）。"""
    assert PrinterStatus(gcode_state="WEIRD_STATE").state_text == "WEIRD_STATE"


# --------------------------------------------------------------------------- 余量契约（重点）


@pytest.mark.parametrize(
    "kwargs, known, text",
    [
        # 官方料卷（有 RFID）+ 有效余量 -> 显示
        ({"has_rfid": True, "remain": 92}, True, "92%"),
        ({"has_rfid": True, "remain": 0}, True, "0%"),
        # 第三方料卷（无 RFID）：固件报 100 也是假值 -> 不显示
        ({"has_rfid": False, "remain": 100}, False, ""),
        ({"has_rfid": False, "remain": 0}, False, ""),
        # 有 RFID 但余量未知（-1）-> 不显示
        ({"has_rfid": True, "remain": -1}, False, ""),
        # 外挂料盘：没有余量检测机制 -> 不显示
        ({"has_rfid": True, "remain": 50, "external": True}, False, ""),
        ({"has_rfid": False, "remain": 100, "external": True}, False, ""),
        # 空槽位 -> 显示「空」
        ({"has_rfid": True, "remain": 80, "empty": True}, False, "空"),
        ({"has_rfid": False, "remain": -1, "empty": True}, False, "空"),
    ],
    ids=[
        "official-92",
        "official-0",
        "third-party-100",
        "third-party-0",
        "official-unknown-remain",
        "external-with-rfid",
        "external-no-rfid",
        "empty-with-rfid",
        "empty-no-rfid",
    ],
)
def test_ams_tray_remain_contract(kwargs, known, text):
    """契约（重点）：只有「有 RFID + 非外挂 + 非空 + remain>=0」才显示余量百分比。

    第三方料卷（无 RFID）即便固件报 100 也必须不显示；外挂料盘不显示；空槽位显示「空」。
    """
    tray = AmsTray(**kwargs)
    assert tray.remain_known is known
    assert tray.remain_text == text


def test_third_party_spool_hides_percentage():
    """契约（README:124-126）：第三方料卷只显示材料类型，绝不出现百分比。"""
    tray = AmsTray(tray_type="PETG", color="00FF00FF", remain=100, has_rfid=False)
    assert tray.remain_text == ""
    assert "%" not in tray.remain_text
    assert "%" not in tray.summary
    assert tray.summary == "PETG #00FF00"
    assert "非官方料卷" in tray.remain_hint


def test_external_spool_hides_percentage_and_hints_why():
    """契约：外挂料盘的余量文案为空，提示语说明「外挂料盘没有余量检测」。"""
    tray = AmsTray(tray_type="PLA", color="FF0000FF", remain=77, has_rfid=True, external=True)
    assert tray.remain_known is False
    assert tray.remain_text == ""
    assert "外挂料盘" in tray.remain_hint
    assert tray.label == "外挂"


def test_empty_tray_shows_empty_text():
    """契约：空槽位 remain_text == "空"，提示语为「槽位为空」，且不显示百分比。"""
    tray = AmsTray(ams_id=0, tray_id=2, remain=0, empty=True)
    assert tray.remain_text == "空"
    assert tray.remain_hint == "槽位为空"
    assert tray.label == "AMS1-3"


def test_official_spool_shows_percentage_everywhere():
    """契约：官方料卷（RFID）的余量在文案、提示与摘要里都可见。"""
    tray = AmsTray(ams_id=0, tray_id=0, tray_type="PLA", color="FF0000FF", remain=92, has_rfid=True)
    assert tray.remain_known is True
    assert tray.remain_text == "92%"
    assert tray.remain_hint == "余量 92%"
    assert tray.summary == "PLA #FF0000 92%"


@pytest.mark.parametrize(
    "color, expected",
    [
        ("FF0000FF", "#FF0000"),
        ("ff00ff", "#FF00FF"),
        ("#12345678", "#123456"),
        ("ABCDEF", "#ABCDEF"),
        ("", ""),
        ("12345", ""),  # 不足 6 位 -> 空
    ],
)
def test_ams_tray_color_hex(color, expected):
    """契约：``tray_color``（RRGGBBAA）截取前 6 位并转成 "#RRGGBB" 大写，不足 6 位返回空串。"""
    assert AmsTray(color=color).color_hex == expected


@pytest.mark.parametrize(
    "ams_id, tray_id, external, expected",
    [
        (0, 0, False, "AMS1-1"),
        (0, 3, False, "AMS1-4"),
        (1, 0, False, "AMS2-1"),
        (255, 254, True, "外挂"),
    ],
)
def test_ams_tray_label(ams_id, tray_id, external, expected):
    """契约：AMS 内槽位显示 "AMS{ams_id+1}-{tray_id+1}"，外挂料盘显示「外挂」。"""
    assert AmsTray(ams_id=ams_id, tray_id=tray_id, external=external).label == expected


# --------------------------------------------------------------------------- AMS 报文解析


def _ams_report() -> dict:
    """构造一份贴近真机的 AMS + 外挂料盘报文（两个 AMS 单元，共 5 个槽位）。"""
    return {
        "print": {
            "ams": {
                "ams": [
                    {
                        "id": 0,
                        "tray": [
                            {  # 官方 PLA，有 RFID，余量可信
                                "id": 0,
                                "tray_type": "PLA",
                                "tray_color": "FF0000FF",
                                "remain": 92,
                                "tag_uid": "1234",
                            },
                            {  # 第三方 PETG：tag_uid 全 0，固件报 100 属于假值
                                "id": 1,
                                "tray_type": "PETG",
                                "tray_color": "00FF00FF",
                                "remain": 100,
                                "tag_uid": "00000000",
                            },
                            {  # 空槽位
                                "id": 2,
                                "tray_type": "",
                                "tray_color": "00000000",
                                "remain": 0,
                            },
                            {  # 靠 tray_uuid 判定为有 RFID
                                "id": 3,
                                "tray_type": "ABS",
                                "tray_color": "0000FFFF",
                                "remain": 30,
                                "tray_uuid": "ABCD1234",
                            },
                        ],
                    },
                    {
                        "id": 1,
                        "tray": [
                            {
                                "id": 0,
                                "tray_type": "TPU",
                                "tray_color": "FFFFFFFF",
                                "remain": 55,
                                "tag_uid": "5678",
                            }
                        ],
                    },
                ],
                "tray_now": "1",  # 真机是字符串
                "tray_tar": "0",
                "ams_exist_bits": "1",
            },
            "vt_tray": {
                "id": 254,
                "tray_type": "PLA",
                "tray_color": "111111FF",
                "remain": 77,
                "tag_uid": "9999",
            },
        }
    }


def test_parse_ams_slots_labels_and_colors():
    """契约：``_parse_ams`` 按 AMS 单元展开槽位，数量、label、color_hex、材料类型都正确。"""
    status = PrinterStatus()
    status.apply_report(_ams_report())

    assert len(status.ams_trays) == 5
    assert [tray.label for tray in status.ams_trays] == [
        "AMS1-1",
        "AMS1-2",
        "AMS1-3",
        "AMS1-4",
        "AMS2-1",
    ]
    assert status.ams_trays[0].color_hex == "#FF0000"
    assert status.ams_trays[0].tray_type == "PLA"
    assert status.ams_trays[0].remain == 92
    assert status.ams_trays[0].ams_id == 0
    assert status.ams_trays[4].ams_id == 1
    assert status.ams_exist is True


def test_parse_ams_rfid_and_empty_flags():
    """契约：RFID 由 ``tag_uid``/``tray_uuid`` 去 0 后判定；空槽位由材料名与颜色共同判定。"""
    status = PrinterStatus()
    status.apply_report(_ams_report())
    trays = status.ams_trays

    assert (trays[0].has_rfid, trays[0].empty, trays[0].remain_text) == (True, False, "92%")
    # 第三方料卷：报 100 也不显示
    assert (trays[1].has_rfid, trays[1].empty, trays[1].remain_text) == (False, False, "")
    # 空槽位
    assert (trays[2].has_rfid, trays[2].empty, trays[2].remain_text) == (False, True, "空")
    # tray_uuid 有效即算有 RFID
    assert (trays[3].has_rfid, trays[3].empty, trays[3].remain_text) == (True, False, "30%")
    assert trays[4].remain_text == "55%"


def test_parse_ams_unit_grouping():
    """契约：``ams_by_unit`` 按 ams_id 分组并按 tray_id 升序排列。"""
    status = PrinterStatus()
    status.apply_report(_ams_report())
    units = status.ams_by_unit
    assert list(units.keys()) == [0, 1]
    assert [tray.tray_id for tray in units[0]] == [0, 1, 2, 3]
    assert [tray.tray_id for tray in units[1]] == [0]


@pytest.mark.parametrize(
    "ams_obj",
    [{"ams": "x"}, {}, {"ams": None}, {"ams": [None, 5, "tray"]}, {"ams": [{"id": 0}]}],
    ids=["ams-str", "no-ams-key", "ams-none", "bad-units", "unit-without-tray"],
)
def test_parse_ams_ignores_malformed_units(ams_obj):
    """契约：AMS 结构异常（非列表 / 元素非字典 / 缺 tray）时安全返回空列表，不抛异常。"""
    assert _parse_ams(ams_obj) == []


def test_parse_tray_negative_remain_becomes_unknown():
    """契约：``remain`` 为负数（缺字段或 -1）时归一化成 -1（未知），不会显示为百分比。"""
    tray = _parse_tray({"tray_type": "PLA", "tray_color": "FF0000FF"}, ams_id=0)
    assert tray.remain == -1
    assert tray.remain_known is False
    assert tray.remain_text == ""


def test_vt_tray_parsed_as_external():
    """契约：``vt_tray`` 解析成 external=True 的料盘，标签为「外挂」且不显示余量。"""
    status = PrinterStatus()
    status.apply_report(_ams_report())

    assert status.vt_tray is not None
    assert status.vt_tray.external is True
    assert status.vt_tray.ams_id == 255
    assert status.vt_tray.label == "外挂"
    assert status.vt_tray.tray_type == "PLA"
    assert status.vt_tray.remain_text == ""  # 外挂不显示余量


# --------------------------------------------------------------------------- active_tray / stale


def test_active_tray_none_when_tray_now_is_255():
    """契约：``tray_now=255``（未使用任何料卷）时 active_tray 为 None。"""
    status = PrinterStatus()
    status.apply_report(_ams_report())
    status.tray_now = 255
    assert status.active_tray is None


def test_active_tray_returns_vt_tray_when_tray_now_is_254():
    """契约：``tray_now=254`` 表示正在用外挂料盘，返回 vt_tray 本体。"""
    status = PrinterStatus()
    status.apply_report(_ams_report())
    status.tray_now = 254
    assert status.active_tray is status.vt_tray


def test_active_tray_maps_tray_now_to_ams_slot():
    """契约：``tray_now`` 按 ``divmod(tray_now, 4)`` 映射到 AMS 单元与槽位。

    报文里的 "1"（字符串）应解析成 1 -> AMS1 的第 2 个槽位（PETG）。
    """
    status = PrinterStatus()
    status.apply_report(_ams_report())
    assert status.tray_now == 1

    active = status.active_tray
    assert active is not None
    assert (active.ams_id, active.tray_id) == (0, 1)
    assert active.tray_type == "PETG"
    assert active is status.ams_trays[1]

    status.tray_now = 3
    assert status.active_tray is status.ams_trays[3]


def test_active_tray_none_for_unknown_slot():
    """契约：``tray_now`` 指向不存在的槽位时返回 None，而不是抛异常。"""
    status = PrinterStatus()
    status.apply_report(_ams_report())
    status.tray_now = 99
    assert status.active_tray is None


@pytest.mark.parametrize(
    "last_message_ts, expected",
    [(0, True), (-1, True), (-1000, True), (None, True)],
    ids=["zero", "negative", "very-negative", "none"],
)
def test_stale_true_when_never_received_message(last_message_ts, expected):
    """契约：``last_message_ts<=0``（从未收到报文）时 ``stale`` 为 True。"""
    status = PrinterStatus()
    if last_message_ts is not None:
        status.last_message_ts = last_message_ts
    assert status.stale is expected


def test_stale_threshold_is_30_seconds():
    """契约：报文时间戳超过 30 秒即过期；30 秒以内不算过期。"""
    fresh = PrinterStatus(last_message_ts=time.time() - 10)
    assert fresh.stale is False
    assert PrinterStatus(last_message_ts=time.time() - 29).stale is False
    assert PrinterStatus(last_message_ts=time.time() - 31).stale is True


# --------------------------------------------------------------------------- HMS


def test_parse_hms_short_code_and_display():
    """契约：HMS 项生成 HMS_xxxx_xxxx_xxxx_xxxx 短码，display 可安全调用且包含短码。"""
    items = _parse_hms([{"attr": 0x03000100, "code": 0x00010001}])
    assert len(items) == 1
    item = items[0]
    assert item.short_code == "HMS_0300_0100_0001_0001"
    assert item.short_code  # 非空
    assert item.display  # 不抛异常且非空（具体文案依赖内置表，不在此断言）
    assert item.short_code in item.display


def test_parse_hms_skips_non_dict_items():
    """契约：hms 数组里的非字典元素被跳过，不会让整条报文解析失败。"""
    items = _parse_hms([None, "x", 5, {"attr": 1, "code": 2}])
    assert len(items) == 1
    assert items[0].short_code == "HMS_0000_0001_0000_0002"


def test_apply_report_updates_hms_list_and_count():
    """契约：报文带 hms 数组时更新 hms_items 与 hms_count（数量一致）。"""
    status = PrinterStatus()
    status.apply_report(
        {"print": {"hms": [{"attr": 0x03000100, "code": 0x00010001}, {"attr": 1, "code": 2}]}}
    )
    assert len(status.hms_items) == 2
    assert status.hms_count == 2
    assert all(isinstance(item, HmsItem) for item in status.hms_items)


def test_hms_item_display_without_short_code():
    """契约：``display`` 在短码为空时退回 8 位十六进制 attr+code，绝不返回空串。"""
    assert HmsItem(attr=0x0300, code=0x0100).display == "0000030000000100"


# --------------------------------------------------------------------------- 展示辅助


@pytest.mark.parametrize(
    "info, expected",
    [
        (PrinterInfo(ip="1.1.1.1", serial="01PABC123", name="客厅"), "客厅"),
        (PrinterInfo(ip="1.1.1.1", serial="01PABC123"), "ABC123"),
        (PrinterInfo(ip="1.1.1.1"), "1.1.1.1"),
        (PrinterInfo(), "未命名"),
    ],
    ids=["name", "serial-tail", "ip", "empty"],
)
def test_printer_info_display_name(info, expected):
    """契约：显示名优先用名称，其次序列号后 6 位，再其次 IP，全空则为「未命名」。"""
    assert info.display_name() == expected


def test_printer_model_is_string_enum_with_label():
    """契约：PrinterModel 是 str 枚举（可直接当字符串用），label 等于 value。"""
    assert isinstance(PrinterModel.P1S, str)
    assert PrinterModel.P1S == "P1S"
    assert PrinterModel.P1S.label == "P1S"
    assert str(PrinterModel.A1MINI) == "A1 mini"
    assert PrinterModel("未知机型") is PrinterModel.UNKNOWN
