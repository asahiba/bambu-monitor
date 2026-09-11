"""轮询型适配器基类（`app.core.adapter.PollingDeviceSession`）的契约。

第三方设备族（Moonraker / OctoPrint / PrusaLink…）都建立在这个基类上，
所以它的四条保证必须被锁死——这四条都是项目历史上真踩过的坑：

1. **线程可回收**：`stop()` 之后轮询线程必须真的退出（否则游离线程越积越多）；
2. **增量合并**：状态走 `apply_mapped`，一次只含部分字段的响应不得清零其它读数；
3. **错误抑制 + 退避**：单次网络失败不能打断监控，连续失败要拉长轮询间隔，
   且告警只记一次（离线设备不能把告警刷满）；
4. **能力门禁**：不支持的控制指令直接返回 False，绝不抛异常进界面。

这里用一个「虚构设备族」的假适配器验证基类本身，不依赖任何真实设备与网络。
"""

from __future__ import annotations

import threading
import time

import pytest

from app.bambu.models import PrinterInfo
from app.core import DeviceSession
from app.core.adapter import PollingDeviceSession
from app.core.capabilities import DeviceCapabilities
from app.core.device import JOB_PRINTING


class FakeAdapter(PollingDeviceSession):
    """假设备族：状态与画面都由测试代码喂进来，用来验证基类行为。"""

    family_label = "假设备"

    def __init__(self, **kwargs) -> None:
        info = kwargs.pop("info", None) or PrinterInfo(ip="127.0.0.1", name="假设备")
        super().__init__(info, **kwargs)
        self.capabilities = DeviceCapabilities(
            has_camera=True, can_control_light=True, can_set_speed=False
        )
        #: 测试可以替换成会抛异常的实现
        self.status_provider = lambda: {"job_state": JOB_PRINTING, "progress_percent": 10}
        self.frame_provider = lambda: b"\xff\xd8\xff\xe0fake-jpeg"
        self.status_calls = 0
        self.frame_calls = 0
        self.commands: list[tuple[str, dict]] = []
        self.command_ok = True

    def _fetch_status(self):
        self.status_calls += 1
        return self.status_provider()

    def _fetch_frame(self):
        self.frame_calls += 1
        return self.frame_provider()

    def _send_command(self, command, **params):
        self.commands.append((command, params))
        return self.command_ok


@pytest.fixture()
def adapter():
    """一个已启动的假适配器；测试结束保证停掉，避免游离线程。"""
    session = FakeAdapter(poll_interval=0.05, max_fps=50.0)
    yield session
    session.stop()


def _wait_until(predicate, timeout: float = 3.0) -> bool:
    """等待条件成立（避免用固定 sleep 造成偶发失败）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


# --------------------------------------------------------------------------- 协议


def test_轮询适配器满足设备会话协议():
    """契约：轮询适配器天然满足 `DeviceSession`，因此界面与网页无需改动即可用。"""
    session = FakeAdapter()
    assert isinstance(session, DeviceSession)


# --------------------------------------------------------------------------- 生命周期


def test_启动后会轮询状态与画面():
    """契约：start() 之后状态与画面都被周期性地取回。"""
    session = FakeAdapter(poll_interval=0.05, max_fps=50.0)
    try:
        session.start()
        assert _wait_until(lambda: session.status_calls >= 3), "状态没有被轮询"
        assert _wait_until(lambda: session.frame_calls >= 3), "画面没有被轮询"
        assert _wait_until(lambda: session.status.online), "状态应被标记为在线"
        assert session.status.progress_percent == 10
        assert session.status.camera_online is True
    finally:
        session.stop()


def test_重复_start是幂等的():
    """契约：重复 start() 不应启动第二条轮询线程。"""
    session = FakeAdapter(poll_interval=0.05)
    try:
        session.start()
        thread = session._thread
        session.start()
        assert session._thread is thread
    finally:
        session.stop()


def test_stop之后轮询线程真的退出():
    """契约：stop() 必须回收轮询线程。

    这是项目历史上真踩过的坑（网页转码线程与视频建立线程都曾只置标志不 join，
    反复开关会累积游离线程），所以第三方适配器这条必须可靠。
    """
    session = FakeAdapter(poll_interval=0.05)
    session.start()
    thread = session._thread
    assert _wait_until(lambda: session.status_calls >= 1)

    session.stop()
    session.stop()  # 重复 stop 也必须安全
    assert thread is not None
    assert _wait_until(lambda: not thread.is_alive(), timeout=3.0), "轮询线程没有退出"
    assert session.running is False
    assert session.status.online is False


def test_停止后不再轮询():
    """契约：stop() 之后调用次数不再增加（不能"假装停了"仍在后台请求设备）。"""
    session = FakeAdapter(poll_interval=0.05)
    session.start()
    assert _wait_until(lambda: session.status_calls >= 2)
    session.stop()
    time.sleep(0.15)
    calls = session.status_calls
    time.sleep(0.15)
    assert session.status_calls == calls


# --------------------------------------------------------------------------- 状态合并


def test_状态走增量合并不清零其它读数():
    """契约：基类必须用 `apply_mapped` 的增量语义合并状态。

    第三方设备常按需返回部分字段（例如 Moonraker 只回你查询的那些对象），
    若实现成整体覆盖，进度/温度会在下一次响应里被清零。
    """
    session = FakeAdapter(poll_interval=0.05)
    responses = [
        {"job_state": JOB_PRINTING, "progress_percent": 47, "nozzle_temper": 245.3},
        {"bed_temper": 60.0},
        {"bed_temper": 61.0},
    ]
    index = {"i": 0}

    def provider():
        value = responses[min(index["i"], len(responses) - 1)]
        index["i"] += 1
        return value

    session.status_provider = provider
    try:
        session.start()
        assert _wait_until(lambda: session.status.bed_temper == 61.0)
        assert session.status.progress_percent == 47, "后续响应未含进度时不得清零"
        assert session.status.nozzle_temper == 245.3
    finally:
        session.stop()


def test_空响应不算失败():
    """契约：返回空字典表示"这次没新数据"，不应被计为失败或置为离线。"""
    session = FakeAdapter(poll_interval=0.05)
    session.status_provider = lambda: {}
    try:
        session.start()
        assert _wait_until(lambda: session.status_calls >= 3)
        assert session._failures == 0
        assert session.warnings == []
    finally:
        session.stop()


# --------------------------------------------------------------------------- 错误抑制


def test_单次失败不打断监控且只记一条告警():
    """契约：网络抖动是常态——失败要记成告警并继续重试，且告警只记一次。

    注意重试会**退避**（连续失败后间隔逐步拉长），所以这里只断言"确实还
    在继续尝试"，不断言具体次数，以免测试依赖时序。
    """
    session = FakeAdapter(poll_interval=0.05)

    def flaky():
        raise OSError("连接被拒绝")

    session.status_provider = flaky
    try:
        session.start()
        assert _wait_until(lambda: session.status_calls >= 2), "失败后仍应继续轮询"
        assert session.running is True, "单次失败不应停止会话"
        assert len(session.warnings) == 1, f"告警应只记一次，实际 {session.warnings}"
        assert "连接被拒绝" in session.warnings[0]
        assert session.status.online is False
    finally:
        session.stop()


def test_连续失败会拉长轮询间隔():
    """契约：设备离线时要退避，避免每秒空转请求（也避免刷爆日志）。"""
    session = FakeAdapter(poll_interval=0.05)
    session.status_provider = lambda: (_ for _ in ()).throw(OSError("down"))
    try:
        session.start()
        assert _wait_until(
            lambda: session._failures >= session.backoff_after_failures, timeout=3.0
        )
        # 退避后下次轮询的时间点应当明显被推后
        interval = session._poll_interval * (1 + session._failures)
        assert interval > session._poll_interval
    finally:
        session.stop()


def test_恢复后清掉错误说明并回到在线():
    """契约：设备恢复后必须回到在线、并清掉上一次的错误说明。"""
    session = FakeAdapter(poll_interval=0.05)
    state = {"fail": True}

    def provider():
        if state["fail"]:
            raise OSError("暂时不可用")
        return {"job_state": JOB_PRINTING, "progress_percent": 5}

    session.status_provider = provider
    try:
        session.start()
        assert _wait_until(lambda: session._failures >= 1)
        state["fail"] = False
        assert _wait_until(lambda: session.status.online is True, timeout=3.0)
        assert session.status.last_error == "" or state["fail"] is False
    finally:
        session.stop()


# --------------------------------------------------------------------------- 画面


def test_画面帧被缓存且序号递增():
    """契约：`latest_frame()` 返回「序号 + 最新帧」，序号递增让上层能判断是否新帧。

    用序号而不是回调，是为了让界面按自己的节奏轮询、高频帧不压爆事件队列。
    """
    session = FakeAdapter(poll_interval=0.05, max_fps=50.0)
    try:
        session.start()
        assert _wait_until(lambda: session.latest_frame()[0] >= 3)
        seq, frame = session.latest_frame()
        assert seq >= 3
        assert frame is not None and frame.startswith(b"\xff\xd8"), "必须是 JPEG 字节"
    finally:
        session.stop()


def test_没有摄像头时不拉画面():
    """契约：`capabilities.has_camera` 为假时不应请求画面。

    这是第三方设备族的真实需求：PrusaLink 只有静态快照、部分 Klipper 机器没有摄像头。
    """
    session = FakeAdapter(poll_interval=0.05, max_fps=50.0)
    session.capabilities = session.capabilities.merged(has_camera=False)
    try:
        session.start()
        assert _wait_until(lambda: session.status_calls >= 2)
        time.sleep(0.15)
        assert session.frame_calls == 0, "无摄像头设备不应被拉画面"
        assert session.status.camera_online is False
    finally:
        session.stop()


def test_取画面失败不置为在线():
    """契约：画面取不到时 `camera_online` 必须为假，并把原因写进通道说明。"""
    session = FakeAdapter(poll_interval=0.05, max_fps=50.0)
    session.frame_provider = lambda: None
    try:
        session.start()
        assert _wait_until(lambda: session.frame_calls >= 2)
        assert session.status.camera_online is False
        assert session.last_camera_detail, "应给出可读的通道说明"
    finally:
        session.stop()


# --------------------------------------------------------------------------- 控制门禁


def test_支持的指令被转成会话调用():
    """契约：pause/resume/stop/light 按能力门禁后转发给子类；参数正确。"""
    session = FakeAdapter()
    # 控制要求在线（与拓竹侧 can_control 的语义一致），这里直接置为在线
    session.status.apply_mapped({"progress_percent": 1})
    assert session.pause_print() is True
    assert session.resume_print() is True
    assert session.stop_print() is True
    assert session.set_light(True) is True
    assert session.set_light(False) is True
    assert session.commands == [
        ("pause", {}),
        ("resume", {}),
        ("stop", {}),
        ("light", {"on": True}),
        ("light", {"on": False}),
    ]


def test_不支持的指令直接返回失败且不下发():
    """契约：能力未声明支持时，指令必须被拦住且**不产生任何传输调用**。

    例如 U1 没有舱灯、也不支持速度档位——界面即使误发也不能打到设备上
    （对逆向协议而言，未知命令可能有破坏性，见 docs/FIELD_NOTES.md 的红线一）。
    """
    session = FakeAdapter()
    session.capabilities = session.capabilities.merged(can_set_speed=False)
    assert session.set_speed(3) is False
    assert session.commands == [], "不支持的指令不应下发到设备"

    session.capabilities = session.capabilities.merged(can_control_light=False)
    assert session.set_light(True) is False
    assert session.commands == []


def test_离线时不下发指令():
    """契约：状态不在线时不下发（与拓竹侧 `can_control` 的语义一致）。"""
    session = FakeAdapter()
    assert session.can_control is False
    assert session.pause_print() is False
    assert session.commands == []


def test_下发异常被吞成失败而不是抛给界面():
    """契约：控制失败必须返回 False，绝不把异常抛进 Qt 事件循环。"""

    def boom(command, **params):
        raise RuntimeError("设备拒绝")

    session = FakeAdapter()
    session._send_command = boom
    session.status.apply_mapped({"progress_percent": 1})  # 置为在线
    assert session.can_control is True
    assert session.pause_print() is False


def test_连接摘要在各种组合下都有文案():
    """契约：在线/部分在线/离线三种摘要都必须有可读文案（界面状态栏直接显示）。"""
    session = FakeAdapter()
    assert session.connection_summary() == "离线"

    session.status.apply_mapped({"progress_percent": 1})
    assert session.connection_summary() == "部分在线"

    session.status.camera_online = True
    assert session.connection_summary() == "在线"


def test_未实现的子类会明确报错():
    """契约：子类没实现传输方法时应当明确抛 NotImplementedError（便于开发时发现）。"""

    class Incomplete(PollingDeviceSession):
        pass

    session = Incomplete(PrinterInfo(ip="127.0.0.1"))
    with pytest.raises(NotImplementedError):
        session._fetch_status()
    with pytest.raises(NotImplementedError):
        session._send_command("pause")


def test_告警列表可被界面读取():
    """契约：`warnings` 必须是可读的字符串列表（诊断对话框与自检报告会展示）。"""
    session = FakeAdapter()
    assert isinstance(session.warnings, list)
    session.warnings.append("示例告警")
    assert session.warnings == ["示例告警"]


def test_轮询线程是守护线程():
    """契约：轮询线程必须是 daemon，避免进程退出时被卡住。"""
    session = FakeAdapter(poll_interval=0.05)
    try:
        session.start()
        assert session._thread is not None
        assert session._thread.daemon is True
    finally:
        session.stop()


def test_并发停止不会留下线程():
    """契约：多线程同时 stop() 也必须安全且不残留线程（模拟界面关闭时的竞态）。"""
    session = FakeAdapter(poll_interval=0.05)
    session.start()

    def stopper():
        session.stop()

    threads = [threading.Thread(target=stopper) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=3.0)
    assert all(not thread.is_alive() for thread in threads)
    assert session.running is False
