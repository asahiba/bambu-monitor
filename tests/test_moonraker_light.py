"""第三方族的灯光控制：用用户填的 G-code（Klipper 上"舱灯"没有统一做法）。

## 为什么不让程序猜

真机（Voron 2.4）的 60 个对象里**没有** `led` / `neopixel` / `output_pin`，
也没有灯光宏；只有 5 个风扇对象。也就是说"舱灯接在哪"完全取决于用户怎么接线的：

* 宏：``LIGHT_ON`` / ``LIGHT_OFF``；
* 输出引脚：``SET_PIN PIN=caselight VALUE=1``；
* 某个风扇：``SET_FAN_SPEED FAN=chamber_light SPEED=1``。

猜名字必然踩空，而踩空的表现是"点了没反应"—— 用户会以为软件坏了。
所以按 Fluidd / Mainsail 的做法：**让用户填命令**，程序只负责发。

Moonraker 原生的 ``printer.control.led``（U1 的 ``cavity_led``）那条路保留：
设备真的有 LED 对象时才用得到。
"""

from __future__ import annotations

import time

from app.adapters.moonraker.adapter import MoonrakerAdapter
from app.adapters.moonraker.fake import FakeMoonraker
from app.bambu.models import PrinterInfo
from app.core.registry import create_session


def _adapter(fake: FakeMoonraker, **kwargs) -> MoonrakerAdapter:
    host, _, port = fake.base_url.rpartition(":")
    return MoonrakerAdapter(
        PrinterInfo(ip=host.replace("http://", ""), port=int(port), name="Voron 假机"),
        base_url=fake.base_url,
        poll_interval=0.1,
        **kwargs,
    )


def _wait_until(predicate, timeout: float = 5.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def test_填了G代码就能开关灯():
    """契约：开灯/关灯各发对应的那条 G-code。

    ⚠️ 要等第一次状态轮询成功（`can_control` 为真）才能下发 —— 设备还没连上时
    基类的能力门禁会直接挡住控制命令，这是设计如此。
    """
    with FakeMoonraker() as fake:
        adapter = _adapter(fake, light_on_gcode="LIGHT_ON", light_off_gcode="LIGHT_OFF")
        try:
            adapter.start()
            assert _wait_until(lambda: adapter.can_control), "遥测没连上"
            assert adapter.set_light(True) is True
            assert adapter.set_light(False) is True
            assert fake.gcode_scripts == ["LIGHT_ON", "LIGHT_OFF"]
        finally:
            adapter.stop()


def test_填了引脚或风扇命令也一样能用():
    """契约：填的是哪条命令就发哪条（不解析、不改写）。"""
    with FakeMoonraker() as fake:
        adapter = _adapter(
            fake,
            light_on_gcode="SET_PIN PIN=caselight VALUE=1",
            light_off_gcode="SET_FAN_SPEED FAN=chamber_light SPEED=0",
        )
        try:
            adapter.start()
            assert _wait_until(lambda: adapter.can_control)
            assert adapter.set_light(True) is True
            assert adapter.set_light(False) is True
            assert fake.gcode_scripts[0] == "SET_PIN PIN=caselight VALUE=1"
            assert "SET_FAN_SPEED" in fake.gcode_scripts[1]
        finally:
            adapter.stop()


def test_没配置灯控时不声明能力也不发请求():
    """契约：什么都没填就不该有灯按钮，更不该发一个必然失败的请求。"""
    with FakeMoonraker() as fake:
        adapter = _adapter(fake)
        try:
            adapter.start()
            assert adapter.capabilities.can_control_light is False
            assert adapter.set_light(True) is False
            assert fake.gcode_scripts == []
        finally:
            adapter.stop()


def test_配置了灯控就声明能力():
    with FakeMoonraker() as fake:
        adapter = _adapter(fake, light_on_gcode="LIGHT_ON", light_off_gcode="LIGHT_OFF")
        try:
            assert adapter.capabilities.can_control_light is True
        finally:
            adapter.stop()
        # 只填一条也算（有些机器只有一个方向的命令）
        only_on = _adapter(fake, light_on_gcode="LIGHT_ON")
        try:
            assert only_on.capabilities.can_control_light is True
        finally:
            only_on.stop()


def test_有LED对象时仍走WebSocket那条路():
    """契约：设备真的有 LED 对象（U1 的 cavity_led）时，原来那条路不能坏。"""
    with FakeMoonraker() as fake:
        adapter = _adapter(fake, led_name="cavity_led", ws_url=fake._ws.url)
        try:
            adapter.start()
            assert _wait_until(lambda: adapter.can_control)
            assert adapter.set_light(True) is True
            assert _wait_until(lambda: fake.led is True)
            assert fake.gcode_scripts == [], "该走 WS 就不该发 G-code"
        finally:
            adapter.stop()


def test_配置从配置对象一路传到适配器():
    """契约：用户在界面里填的灯控命令要能经 `create_session` 到达适配器。"""
    with FakeMoonraker() as fake:
        host, _, port = fake.base_url.rpartition(":")
        info = PrinterInfo(
            ip=host.replace("http://", ""),
            port=int(port),
            name="Voron",
            family="moonraker",
            light_on_gcode="LIGHT_ON",
            light_off_gcode="LIGHT_OFF",
        )
        adapter = create_session(info)
        try:
            assert isinstance(adapter, MoonrakerAdapter)
            assert adapter.capabilities.can_control_light is True
            adapter.start()
            assert _wait_until(lambda: adapter.can_control)
            assert adapter.set_light(True) is True
            assert fake.gcode_scripts == ["LIGHT_ON"]
        finally:
            adapter.stop()


def test_灯控命令能往返配置(isolated_config_dir):
    """契约：填的灯控命令要存得住（重启后还得在）。"""
    from app.config import AppConfig

    config = AppConfig()
    config.persist = True
    config.printers = [
        PrinterInfo(
            ip="192.168.31.55",
            name="Voron",
            family="moonraker",
            light_on_gcode="LIGHT_ON",
            light_off_gcode="SET_PIN PIN=caselight VALUE=0",
            camera_index=1,
        )
    ]
    config.save()
    assert not config.last_error
    reloaded = AppConfig.load()
    item = reloaded.printers[0]
    assert item.light_on_gcode == "LIGHT_ON"
    assert item.light_off_gcode == "SET_PIN PIN=caselight VALUE=0"
    assert item.camera_index == 1
