"""`app/bambu/diagnostics.py` 的契约测试（全程离线，不碰任何真机）。

## 为什么单独立一条

通道诊断原来有**两份实现**：`tools/diagnose.py`（命令行）与
`app/ui/diagnose_dialog.py`（界面）。它们连 RTSPS 的候选路径都不一样 ——
命令行会依次试 `/streaming/live/1`、`/streaming/live/2`、`/live/1`，
界面只试一个，于是出现过「命令行说通了、界面上说不行」这种自相矛盾的结论。

现在流程只有一份。本文件守住三件事：

1. 探测函数**不抛异常**（网络故障是被诊断的对象，不是错误）；
2. 渲染是纯函数、结论可读（两个入口拿到的文本必然一致）；
3. `run()` 的编排：按顺序给步骤、`should_stop` 能中断、编号不跳号。
"""

from __future__ import annotations

import pytest

from app.bambu import diagnostics

#: 本文件刻意**不**用 no_network 夹具：要测的正是「网络探测失败时如何呈现」，
#: 而那个夹具会把 socket 直接禁掉（抛 RuntimeError），测不出真实行为。
#: 目标地址全部选成「绝不可能连上真实设备」的：
#: * 127.0.0.1 的 9 号端口（discard，本机不会有人监听）
#: * 192.0.2.1（RFC 5737 的 TEST-NET-1，不可路由）
DEAD_HOST = "192.0.2.1"


# --------------------------------------------------------------------------- 探测：不抛异常


def test_端口探测在无人监听时给出失败而不是异常():
    probes = diagnostics.check_ports("127.0.0.1", ports=((9, "discard"),), timeout=0.5)
    assert len(probes) == 1
    assert probes[0].ok is False
    assert probes[0].error, "失败时必须带上原因，否则用户不知道是拒绝还是超时"


def test_tls探测在连不上时给出失败而不是异常():
    # 用本机被拒的端口，而不是不可路由地址：后者每次要等满 TCP 超时（实测 20 秒+），
    # 会让整个测试文件慢到没人愿意跑
    probes = diagnostics.check_tls("127.0.0.1", ports=(9,), timeout=0.5)
    assert len(probes) == 1
    assert probes[0].ok is False
    assert "Error" in probes[0].error or "error" in probes[0].error


def test_取帧在连不上时给出失败而不是异常():
    probe = diagnostics.grab_camera_frame("127.0.0.1", "12345678", timeout=0.5)
    assert probe.ok is False
    assert probe.seconds >= 0
    assert probe.detail, "失败时要给出通道自己的说明（例如连接失败的原因）"


def test_RTSPS_DESCRIBE在连不上时给出失败而不是异常():
    probe = diagnostics.describe_rtsp("127.0.0.1", "12345678", tls_timeout=0.5)
    assert probe.connected is False
    assert probe.error
    assert probe.attempts == [], "连 TLS 都没建立时不该报告任何路径尝试"


# --------------------------------------------------------------------------- 渲染：纯函数


def test_渲染端口结果():
    probes = [
        diagnostics.PortProbe(port=8883, label="MQTT 遥测", ok=True),
        diagnostics.PortProbe(port=6000, label="JPEG 画面", ok=False, error="ConnectionRefusedError"),
    ]
    lines = diagnostics.render_ports(probes)
    assert "8883" in lines[0] and "可连接" in lines[0]
    assert "6000" in lines[1] and "不可达" in lines[1]
    assert "ConnectionRefusedError" in lines[1]


def test_渲染TLS结果区分证书链是否校验():
    ok = diagnostics.render_tls([diagnostics.TlsProbe(port=8883, ok=True, verified=True)])
    assert "已校验" in ok[0]
    bad = diagnostics.render_tls([diagnostics.TlsProbe(port=8883, ok=False, error="ssl.SSLError")])
    assert "握手失败" in bad[0]


def test_渲染取帧结果():
    ok = diagnostics.render_camera(
        diagnostics.CameraProbe(ok=True, frame_bytes=32 * 1024, seconds=0.4, state="streaming")
    )
    assert "32 KB" in ok[0] and "0.4s" in ok[0]

    failed = diagnostics.render_camera(
        diagnostics.CameraProbe(ok=False, seconds=12.0, state="retrying", detail="连接失败")
    )
    assert "失败" in failed[0] and "连接失败" in failed[0]


def test_渲染DESCRIBE的三种结论():
    # 200：能直接拉流
    good = diagnostics.RtspDescribeProbe(
        connected=True,
        verified=False,
        ok_path="/streaming/live/1",
        attempts=[("/streaming/live/1", "RTSP/1.0 200 OK")],
        sdp_lines=["m=video 0 RTP/AVP 96"],
    )
    text = "\n".join(diagnostics.render_rtsp_describe(good))
    assert "200 OK" in text and "m=video" in text and "可以直接拉流" in text

    # 401：服务活着但口令不对（这条提示是用户最需要的）
    unauth = diagnostics.RtspDescribeProbe(
        connected=True, unauthorized=True, attempts=[("/streaming/live/1", "RTSP/1.0 401 Unauthorized")]
    )
    assert "口令未被接受" in "\n".join(diagnostics.render_rtsp_describe(unauth))

    # 无响应：服务没开
    silent = diagnostics.RtspDescribeProbe(
        connected=True, attempts=[("/streaming/live/1", "（无响应）")]
    )
    assert "局域网实时画面" in "\n".join(diagnostics.render_rtsp_describe(silent))


def test_没装opencv时拉流那步是跳过而不是失败():
    lines = diagnostics.render_rtsp_frame(diagnostics.RtspFrameProbe(available=False))
    assert "跳过" in lines[0]
    assert "失败" not in lines[0], "缺依赖不该被报成设备故障"


def test_渲染遥测摘要():
    probe = diagnostics.MqttProbe(ok=True, lines=["IP：10.0.0.5", "遥测（8883）：成功"])
    lines = diagnostics.render_mqtt(probe)
    assert all(line.startswith("   ") for line in lines)
    assert "成功" in lines[1]


# --------------------------------------------------------------------------- 编排


def _stub_sections(monkeypatch, *, rtsp_frame: bool = False):
    """把每一步替换成可控的桩，只测编排逻辑（不发任何网络请求）。"""
    monkeypatch.setattr(diagnostics, "check_ports", lambda *a, **k: [])
    monkeypatch.setattr(diagnostics, "check_tls", lambda *a, **k: [])
    monkeypatch.setattr(
        diagnostics, "grab_camera_frame", lambda *a, **k: diagnostics.CameraProbe(ok=True)
    )
    monkeypatch.setattr(
        diagnostics, "describe_rtsp", lambda *a, **k: diagnostics.RtspDescribeProbe(connected=False)
    )
    monkeypatch.setattr(
        diagnostics, "grab_rtsp_frame", lambda *a, **k: diagnostics.RtspFrameProbe(ok=True)
    )
    monkeypatch.setattr(
        diagnostics, "probe_mqtt", lambda *a, **k: diagnostics.MqttProbe(ok=True, lines=["ok"])
    )


def _titles(events) -> list[str]:
    return [event.title for event in events if isinstance(event, diagnostics.SectionStart)]


def _bodies(events) -> list[list[str]]:
    return [event.lines for event in events if isinstance(event, diagnostics.SectionLines)]


def test_run按顺序给出步骤且编号连续(monkeypatch):
    _stub_sections(monkeypatch)
    events = list(diagnostics.run("10.0.0.5", "12345678"))
    assert _titles(events) == [
        "① 端口连通性",
        "② TLS 参数（证书链 + 安全级别）",
        "③ 6000 端口画面（TLS + 80 字节鉴权包 + JPEG 帧）",
        "④ RTSPS(322) DESCRIBE",
        "⑤ MQTT 遥测",
    ]
    # 每一步都必须「先标题、后内容」，否则界面上的进度行会跑到标题前面
    assert len(_bodies(events)) == len(_titles(events))


def test_标题先于探测发生(monkeypatch):
    """契约：先拿到 ``SectionStart``，之后才做那一步的探测。

    界面靠这个顺序把标题打出来再显示结果（否则「正在连接 MQTT…」会显示在
    它自己的标题上面）。
    """
    done: list[str] = []
    monkeypatch.setattr(diagnostics, "check_ports", lambda *a, **k: done.append("ports") or [])

    stream = diagnostics.run("10.0.0.5", "12345678")
    first = next(stream)
    assert isinstance(first, diagnostics.SectionStart)
    assert done == [], "标题还没吐出来就已经开始探测了"
    second = next(stream)
    assert isinstance(second, diagnostics.SectionLines)
    assert done == ["ports"]


def test_界面不拉流时编号不跳号(monkeypatch):
    """回归：界面少跑「RTSPS 真拉流」这一步，编号必须是 ①②③④⑤ 而不是 ①②③④⑥。"""
    _stub_sections(monkeypatch)
    titles = _titles(list(diagnostics.run("10.0.0.5", "12345678")))
    assert "⑥" not in "".join(titles)
    assert titles[-1] == "⑤ MQTT 遥测"


def test_打开拉流步骤时多一步(monkeypatch):
    _stub_sections(monkeypatch)
    titles = _titles(list(diagnostics.run("10.0.0.5", "12345678", with_rtsp_frame=True)))
    assert len(titles) == 6
    assert "实际拉流" in titles[4]
    assert titles[-1] == "⑥ MQTT 遥测"


def test_取消后立刻停在步骤边界(monkeypatch):
    """契约：``should_stop`` 为真时不再启动新的探测（关对话框后不该继续连设备）。"""
    calls: list[str] = []
    monkeypatch.setattr(
        diagnostics, "check_ports", lambda *a, **k: calls.append("ports") or []
    )
    monkeypatch.setattr(diagnostics, "check_tls", lambda *a, **k: calls.append("tls") or [])
    monkeypatch.setattr(
        diagnostics,
        "grab_camera_frame",
        lambda *a, **k: calls.append("camera") or diagnostics.CameraProbe(),
    )

    state = {"stop": False}

    def should_stop() -> bool:
        return state["stop"]

    stream = diagnostics.run("10.0.0.5", "12345678", should_stop=should_stop)
    first = next(stream)
    assert isinstance(first, diagnostics.SectionStart) and "端口" in first.title
    next(stream)  # 端口那一步的正文
    state["stop"] = True
    assert list(stream) == [], "取消之后不该再产出步骤"
    assert calls == ["ports"], f"取消后仍然发起了探测：{calls}"


def test_run_透传取消给耗时步骤(monkeypatch):
    """契约：取帧与遥测要把 ``should_stop`` 透传下去（否则最坏要等满超时）。"""
    seen: dict[str, object] = {}

    monkeypatch.setattr(diagnostics, "check_ports", lambda *a, **k: [])
    monkeypatch.setattr(diagnostics, "check_tls", lambda *a, **k: [])

    def fake_camera(host, code, serial="", timeout=None, should_stop=None):
        seen["camera"] = should_stop
        return diagnostics.CameraProbe()

    def fake_mqtt(host, code, serial="", timeout=None, on_step=None, should_stop=None):
        seen["mqtt"] = should_stop
        return diagnostics.MqttProbe()

    monkeypatch.setattr(diagnostics, "grab_camera_frame", fake_camera)
    monkeypatch.setattr(diagnostics, "describe_rtsp", lambda *a, **k: diagnostics.RtspDescribeProbe())
    monkeypatch.setattr(diagnostics, "probe_mqtt", fake_mqtt)

    marker = lambda: False  # noqa: E731 - 只用于身份比较（返回假 = 不取消）
    list(diagnostics.run("10.0.0.5", "12345678", should_stop=marker))
    assert seen["camera"] is marker
    assert seen["mqtt"] is marker


def test_两个入口共用同一份实现():
    """契约：命令行工具与界面都必须走 `app.bambu.diagnostics`。

    这条是「两份实现漂移」的根因防线：只要有人再在界面里手写一份探测，
    这里就会失败。
    """
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1]
    for relative in ("tools/diagnose.py", "app/ui/diagnose_dialog.py"):
        source = (root / relative).read_text(encoding="utf-8")
        assert "diagnostics" in source, f"{relative} 没有使用共用诊断模块"
        # 自己重写一套探测的痕迹：直接建 socket / 直接连 tlsutil
        assert "create_connection" not in source, f"{relative} 又自己写了一遍端口探测"
        assert "connect_tls" not in source, f"{relative} 又自己写了一遍 TLS 探测"
