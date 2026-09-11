"""通用状态模型（`app.core.device`）的契约。

这一层是「新增设备族时唯一需要写状态的入口」。第三方适配器把设备私有格式
转换成通用字段字典后调用 `apply_mapped()`，合并、时间戳、类型转换都由这里统一处理。
因此这里要锁死三件事：

1. **增量合并语义**——只覆盖本次真的提供的字段（P1 系列这类固件只推变化字段）；
2. **未知与 0 的区别**——进度未知是 -1 而不是 0，展示为 `--`；
3. **字段白名单**——适配器多传的键必须被忽略而不是污染状态对象。
"""

from __future__ import annotations

import time

import pytest

from app.core.device import (
    JOB_FAILED,
    JOB_FINISHED,
    JOB_IDLE,
    JOB_PAUSED,
    JOB_PRINTING,
    JOB_STATE_TEXT,
    JOB_UNKNOWN,
    MAPPABLE_FIELDS,
    DeviceStatus,
)


def test_初始状态是未知而不是零值():
    """契约：新建状态里进度是 -1（未知），不能是 0（那表示"刚开始打印"）。"""
    status = DeviceStatus()
    assert status.progress_percent == -1
    assert status.progress_text == "--"
    assert status.job_state == JOB_UNKNOWN
    assert status.job_state_text == JOB_STATE_TEXT[JOB_UNKNOWN]
    assert status.online is False


def test_合并只覆盖本次提供的字段():
    """契约：``apply_mapped`` 是**增量合并**——未提供的字段保持原值。

    这条源自真实约束：P1 系列为省资源只推送变化过的字段，若实现成"整体覆盖"，
    温度和进度会在收到一条只含 bed_temper 的报文后被清零。
    """
    status = DeviceStatus()
    status.apply_mapped({"progress_percent": 47, "nozzle_temper": 245.3, "job_name": "benchy"})
    status.apply_mapped({"bed_temper": 60.0})

    assert status.progress_percent == 47, "后续报文未提供进度时不得清零"
    assert status.nozzle_temper == 245.3
    assert status.job_name == "benchy"
    assert status.bed_temper == 60.0


def test_显式空值不覆盖已有值():
    """契约：值为 ``None`` 的字段跳过（表示"本次没这个信息"而不是"清空"）。"""
    status = DeviceStatus()
    status.apply_mapped({"chamber_temper": 33.0, "wifi_rssi_dbm": -55.0})
    status.apply_mapped({"chamber_temper": None, "bed_temper": 60.0})
    assert status.chamber_temper == 33.0
    assert status.wifi_rssi_dbm == -55.0
    assert status.bed_temper == 60.0


def test_表外字段被忽略且不污染状态():
    """契约：适配器传入白名单以外的键必须被忽略。

    这样各族适配器可以放心地"多传一点"（例如原样带上设备私有字段），
    而不必担心在状态对象上造出野生属性。
    """
    status = DeviceStatus()
    assert status.apply_mapped({"totally_unknown_field": 1}) is False
    assert not hasattr(status, "totally_unknown_field")
    assert status.last_message_ts == 0.0, "没有可识别字段时不应刷新时间戳"


def test_有可识别字段时刷新时间戳并置为在线():
    """契约：成功合并即视为"收到了状态"——刷新时间戳、标记在线。"""
    status = DeviceStatus()
    before = time.time()
    assert status.apply_mapped({"progress_percent": 10}) is True
    assert status.last_message_ts >= before
    assert status.online is True
    assert status.stale is False


def test_字段白名单自身可用():
    """契约：``MAPPABLE_FIELDS`` 里列出的键都必须是真实存在的字段名。

    适配器文档直接引用这个集合；若里面写了不存在的字段名，
    适配器就会往一个永远不会生效的键上写数据。
    """
    status = DeviceStatus()
    for name in MAPPABLE_FIELDS:
        assert hasattr(status, name), f"MAPPABLE_FIELDS 里的 {name} 不是 DeviceStatus 的字段"


@pytest.mark.parametrize(
    "state, is_printing, is_paused",
    [
        (JOB_PRINTING, True, False),
        (JOB_PAUSED, True, True),
        (JOB_IDLE, False, False),
        (JOB_FINISHED, False, False),
        (JOB_FAILED, False, False),
        (JOB_UNKNOWN, False, False),
    ],
)
def test_作业状态的派生属性(state, is_printing, is_paused):
    """契约：``is_printing`` 覆盖打印中与暂停（暂停也属"任务进行中"）；
    ``is_paused`` 只对暂停为真。"""
    status = DeviceStatus()
    status.apply_mapped({"job_state": state})
    assert status.is_printing is is_printing
    assert status.is_paused is is_paused


def test_状态文案覆盖全部取值():
    """契约：每个归一化状态都有中文文案（界面直接展示，缺一个就会出现空标签）。"""
    for state in (
        JOB_UNKNOWN,
        JOB_IDLE,
        JOB_PRINTING,
        JOB_PAUSED,
        JOB_FINISHED,
        JOB_FAILED,
        "offline",
    ):
        status = DeviceStatus()
        status.job_state = state
        assert status.job_state_text, f"{state} 没有文案"
        assert status.job_state_text != state, f"{state} 未映射成中文"


def test_进度文案被钳制():
    """契约：进度文案钳在 0..100，越界不显示成 150%。"""
    status = DeviceStatus()
    status.progress_percent = 150
    assert status.progress_text == "100%"
    status.progress_percent = -5
    assert status.progress_text == "--", "负数视为未知"


def test_stale_阈值可被适配器覆盖():
    """契约：轮询周期不同的设备族可以覆盖 ``stale_seconds``。

    Moonraker 用 2 秒轮询，用固定 30 秒判断"过期"会太迟钝；
    而拓竹是推送式，30 秒合适。所以阈值必须是可覆盖的。
    """
    status = DeviceStatus()
    status.apply_mapped({"progress_percent": 1})
    assert status.stale is False

    status.last_message_ts = time.time() - 10
    assert status.stale is False, "默认阈值 30 秒内不算过期"
    status.stale_seconds = 5.0
    assert status.stale is True, "覆盖为 5 秒后应判为过期"


def test_原始报文字段被保留():
    """契约：状态对象必须保留原始报文（排障生命线，见 KNOWN_ISSUES）。

    第三方适配器应当把设备原始响应放进 ``raw``，否则"字段没认出来"就无从排查。
    """
    status = DeviceStatus()
    status.raw.update({"print_stats": {"state": "printing"}})
    assert status.raw["print_stats"]["state"] == "printing"
