"""连接探测：验证 IP + 访问代码是否能取到遥测与画面（用于「测试连接」）。"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from .camera import grab_single_frame
from .models import PrinterModel, PrinterStatus, detect_model
from .mqtt_worker import MqttWorker


@dataclass
class ProbeResult:
    ok: bool = False
    ip: str = ""
    serial: str = ""
    model: PrinterModel = PrinterModel.UNKNOWN
    firmware: str = ""
    mqtt_ok: bool = False
    mqtt_error: str = ""
    camera_ok: bool = False
    camera_error: str = ""
    status: Optional[PrinterStatus] = None
    frame: Optional[bytes] = None
    messages: list[str] = field(default_factory=list)

    def summary(self) -> str:
        lines = [f"IP：{self.ip}"]
        if self.serial:
            lines.append(f"序列号：{self.serial}")
        if self.model is not PrinterModel.UNKNOWN:
            lines.append(f"机型：{self.model.label}")
        if self.firmware:
            lines.append(f"固件：{self.firmware}")
        lines.append(f"遥测（8883）：{'成功' if self.mqtt_ok else '失败 —— ' + self.mqtt_error}")
        lines.append(f"画面（6000）：{'成功' if self.camera_ok else '失败 —— ' + self.camera_error}")
        if self.status is not None:
            lines.append(
                f"状态：{self.status.state_text} {self.status.progress}%  "
                f"喷嘴 {self.status.nozzle_temper:.0f}/{self.status.nozzle_target_temper:.0f}℃  "
                f"热床 {self.status.bed_temper:.0f}/{self.status.bed_target_temper:.0f}℃"
            )
        return "\n".join(lines)


def probe_printer(
    ip: str,
    access_code: str,
    serial: str = "",
    timeout: float = 10.0,
    check_camera: bool = True,
    on_step: Optional[Callable[[str], None]] = None,
    should_stop: Optional[Callable[[], bool]] = None,
) -> ProbeResult:
    """同步探测一台打印机（在后台线程里调用）。

    ``should_stop`` 用于外部取消：界面在对话框被关闭时置位，
    各等待循环都会尽快退出 —— 否则线程要拖到超时（10 秒 + 12 秒），
    而界面线程正在等它结束。
    """
    result = ProbeResult(ip=ip, serial=serial, model=detect_model(serial))

    def cancelled() -> bool:
        return bool(should_stop is not None and should_stop())

    def step(text: str) -> None:
        result.messages.append(text)
        if on_step is not None:
            try:
                on_step(text)
            except Exception:
                pass

    # --- 遥测 ---
    status = PrinterStatus()
    done = threading.Event()
    discovered_serial: list[str] = []

    def handle_report(payload: dict[str, Any]) -> None:
        status.apply_report(payload)
        info = payload.get("info")
        if isinstance(info, dict):
            modules = info.get("module")
            if isinstance(modules, list):
                for module in modules:
                    if isinstance(module, dict) and module.get("name") == "ota":
                        version = str(module.get("sw_ver", "") or "")
                        if version:
                            result.firmware = version
        if "print" in payload:
            done.set()

    def handle_serial(found: str) -> None:
        discovered_serial.append(found)

    step("正在连接 MQTT 8883（遥测）…")
    worker = MqttWorker(
        host=ip,
        serial=serial,
        access_code=access_code,
        on_report=handle_report,
        on_serial=handle_serial,
        name=ip,
    )
    worker.start()
    deadline = time.time() + timeout
    try:
        while time.time() < deadline:
            if done.is_set():
                break
            if cancelled():
                result.mqtt_error = "已取消"
                break
            if discovered_serial and not result.serial:
                result.serial = discovered_serial[0]
                worker.update_serial(result.serial)
            time.sleep(0.2)
        result.mqtt_ok = done.is_set()
        if result.mqtt_ok:
            result.status = status
            result.mqtt_error = ""
            if not result.serial:
                result.serial = worker.serial
            result.model = detect_model(result.serial, "")
            step("遥测连接成功")
        else:
            state = worker.state
            result.mqtt_error = worker.detail or "未收到状态报文"
            if state == MqttWorker.STATE_AUTH_ERROR:
                result.mqtt_error = "访问代码错误（MQTT 拒绝连接）"
            elif state == MqttWorker.STATE_CONNECTING:
                result.mqtt_error = "连接超时：8883 端口无响应，请确认已开启局域网模式、IP 正确、防火墙已放行"
            step(f"遥测失败：{result.mqtt_error}")
    finally:
        worker.stop()

    # --- 画面 ---
    if check_camera and not cancelled():
        step("正在读取 6000 端口画面…")
        frame = grab_single_frame(
            ip,
            access_code,
            serial=result.serial,
            timeout=min(12.0, timeout + 4),
            should_stop=should_stop,
        )
        if frame:
            result.camera_ok = True
            result.frame = frame
            step(f"画面读取成功（{len(frame) // 1024} KB）")
        else:
            result.camera_error = "未取到画面：请确认开启「局域网模式」并检查访问代码"
            step(f"画面失败：{result.camera_error}")

    result.ok = result.mqtt_ok or result.camera_ok
    return result
