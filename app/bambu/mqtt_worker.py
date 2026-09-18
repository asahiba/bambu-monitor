"""MQTT 遥测客户端：订阅打印机状态报文（进度、喷嘴温度、热床温度等）。

本地 MQTT 服务：`mqtts://{IP}:8883`
  * TLS：是（证书由 Bambu Lab CA 签发，CN 为序列号，需要 SNI）
  * 用户名：`bblp`
  * 密码：局域网访问代码
  * 上报主题：`device/{序列号}/report`
  * 下发主题：`device/{序列号}/request`
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any, Callable, Optional

from . import tlsutil
from .ports import MQTT_PORT
from .timeouts import MQTT_CONNECT_TIMEOUT, MQTT_THREAD_JOIN

LOGGER = logging.getLogger("bambu-monitor.mqtt")

try:  # pragma: no cover - 依赖缺失时给出可读提示
    import paho.mqtt.client as mqtt
except ImportError as exc:  # pragma: no cover
    mqtt = None  # type: ignore[assignment]
    _IMPORT_ERROR = exc
else:
    _IMPORT_ERROR = None


def _make_client(client_id: str, serial: str):
    """创建 paho 客户端，兼容 paho-mqtt 1.x / 2.x。"""
    callback_version = getattr(mqtt, "CallbackAPIVersion", None)
    if callback_version is not None:
        client = mqtt.Client(
            callback_version.VERSION2, client_id=client_id, protocol=mqtt.MQTTv311
        )
    else:  # pragma: no cover - 老版本 paho
        client = mqtt.Client(client_id=client_id, protocol=mqtt.MQTTv311)

    # 打印机的证书 CN 是序列号，用它做 SNI 才能通过证书链校验
    if serial:
        try:
            base_class = type(client)

            class _SniClient(base_class):  # type: ignore[misc, valid-type]
                def _ssl_wrap_socket(self, tcp_sock):  # type: ignore[override]
                    original_host = self._host
                    try:
                        self._host = serial
                        return super()._ssl_wrap_socket(tcp_sock)
                    finally:
                        self._host = original_host

            client.__class__ = _SniClient
        except Exception:
            pass
    return client


class MqttWorker:
    """每台打印机一个 MQTT 连接。"""

    STATE_CONNECTING = "connecting"
    STATE_ONLINE = "online"
    STATE_OFFLINE = "offline"
    STATE_AUTH_ERROR = "auth_error"

    def __init__(
        self,
        host: str,
        serial: str,
        access_code: str,
        on_report: Optional[Callable[[dict[str, Any]], None]] = None,
        on_state: Optional[Callable[[str, str], None]] = None,
        on_serial: Optional[Callable[[str], None]] = None,
        name: str = "",
    ) -> None:
        self.host = host
        self.serial = serial
        self.access_code = access_code
        self._on_report = on_report
        self._on_state = on_state
        self._on_serial = on_serial
        self._client = None
        self.state = self.STATE_OFFLINE
        self.detail = ""
        self.last_message_ts = 0.0
        self._last_pushall = 0.0
        self._sequence = 0
        self._lock = threading.Lock()
        self.client_id = f"bambu-monitor-{serial[-4:] if serial else host}".replace(".", "")
        self.topic_report = f"device/{serial}/report" if serial else "device/#"
        self.topic_request = f"device/{serial}/request" if serial else ""
        self.tls_verified = True
        self._stop_requested = False
        self._thread: Optional[threading.Thread] = None
        #: 统计用：连接成功过几次。宿主（PrinterSession）的看门狗靠"这个数字
        #: 一直不涨"来判断 paho 的重连是不是已经卡死，从而强制重建连接。
        self.connect_count = 0
        #: 上一次进入 STATE_ONLINE 的时刻；0 表示从未连上
        self.last_connected_at = 0.0
        #: 第一次发起连接尝试的时刻（用于"一直没连上"的超时判断）
        self.first_attempt_at = 0.0

    def update_serial(self, serial: str) -> None:
        """在未知序列号（通配订阅）场景下，从报文中补全序列号。"""
        if not serial or self.serial == serial:
            return
        self.serial = serial
        self.topic_report = f"device/{serial}/report"
        self.topic_request = f"device/{serial}/request"
        client = self._client
        if client is not None:
            try:
                client.unsubscribe("device/#")
                client.subscribe(self.topic_report, qos=0)
                self.request_pushall(force=True)
            except Exception:
                pass

    # ------------------------------------------------------------------ 对外接口
    def start(self) -> None:
        if mqtt is None:  # pragma: no cover
            self._set_state(self.STATE_OFFLINE, f"缺少依赖 paho-mqtt：{_IMPORT_ERROR}")
            return
        self._stop_requested = False
        if not self.first_attempt_at:
            self.first_attempt_at = time.time()
        # 证书探测 + 建连放到后台线程，避免阻塞界面
        self._thread = threading.Thread(
            target=self._start_blocking, name=f"mqtt-{self.host}", daemon=True
        )
        self._thread.start()

    def restart(self) -> None:
        """丢掉现有连接并重新建一条。

        宿主看门狗在"疑似卡死"时调用它。与 paho 自身的重连（``loop_start`` +
        ``reconnect_delay_set``）是**互补**关系，不是替代：

        * 打印机主动断开、网络抖动 -> paho 自己退避重连，够用；
        * 但 paho 的重连也会卡死（初始 connect 一直失败、或 TLS 握手在某种
          网络状态下坏了），此时它会一直停在断连状态不再成功 —— 表现为
          「画面正常·遥测断开」**时不时出现且不再自愈**。
          这里整条重建（新 client + 重做 TLS 探测）是唯一可靠的恢复手段。
        """
        LOGGER.info("重启 MQTT 连接（%s）", self.host)
        self.stop()
        # stop() 已经把状态置为 offline，这里再起一条新连接
        self.start()

    def _start_blocking(self) -> None:
        """后台线程入口：做一次连接尝试，并把任何异常变成可诊断的状态。

        ⚠️ 这里必须兜住**所有**异常。以前 TLS 探测之后的部分没有保护，
        一旦抛异常（TLS 探测超时、网络切换导致 socket 报错、paho 版本差异…），
        后台线程会**静默退出且永不重试** —— 遥测就永久停在断连状态，
        界面上一直是「画面正常·遥测断开」，只有手动重连才能恢复。

        现在兜住之后，状态会如实变成 offline 并带上原因，由宿主看门狗
        （`PrinterSession._video_watchdog`）负责再试，直到连上为止。
        """
        self._set_state(self.STATE_CONNECTING, f"正在连接 MQTT {MQTT_PORT}")
        try:
            self._connect_once()
        except Exception as exc:  # noqa: BLE001 - 兜住一切，见上面的说明
            LOGGER.warning("MQTT 连接过程出错（%s）：%s", self.host, exc, exc_info=True)
            self._set_state(self.STATE_OFFLINE, f"MQTT 连接出错：{exc}")

    def _connect_once(self) -> None:
        """探测 TLS 参数并建立一条连接（可能抛异常，由调用方兜住）。"""
        # 先探测该打印机可用的 TLS 参数（证书链 + 安全级别），再交给 paho
        context, verified = tlsutil.select_context(
            self.host, MQTT_PORT, self.serial or None, timeout=MQTT_CONNECT_TIMEOUT
        )
        if self._stop_requested:
            return
        self.tls_verified = verified
        if not verified:
            LOGGER.info("打印机 %s 的 MQTT 证书链未能校验，改用不校验证书模式", self.host)
        client = _make_client(self.client_id, self.serial)
        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.on_message = self._on_message
        client.username_pw_set("bblp", self.access_code)
        client.tls_set_context(context)
        client.reconnect_delay_set(min_delay=1, max_delay=30)
        self._client = client
        if self._stop_requested:
            # 竞态：stop() 发生在 TLS 探测之后、这里之前。此时若照常
            # loop_start()，就再也没人能回收这条连接（僵尸 MQTT）。
            self._client = None
            try:
                client.disconnect()
            except Exception:
                pass
            return
        try:
            client.connect_async(self.host, MQTT_PORT, keepalive=60)
            client.loop_start()
        except (OSError, ValueError) as exc:
            self._set_state(self.STATE_OFFLINE, f"MQTT 连接失败：{exc}")
        except Exception as exc:  # noqa: BLE001 - paho 在异常网络下会抛各种异常
            LOGGER.warning("MQTT 启动失败：%s", exc, exc_info=True)
            self._set_state(self.STATE_OFFLINE, f"MQTT 启动失败：{exc}")

    def stop(self) -> None:
        self._stop_requested = True
        client = self._client
        self._client = None
        if client is not None:
            try:
                client.disconnect()
            except Exception:
                pass
            try:
                client.loop_stop()
            except Exception:
                pass
        thread, self._thread = self._thread, None
        if thread is not None and thread.is_alive():
            thread.join(timeout=MQTT_THREAD_JOIN)
        self._set_state(self.STATE_OFFLINE, "已断开")

    def request_pushall(self, force: bool = False) -> None:
        """请求完整状态。

        注意：P1P 硬件性能有限，官方建议间隔不要小于 5 分钟。
        """
        client = self._client
        if client is None:
            return
        now = time.time()
        if not force and (now - self._last_pushall) < 300:
            return
        self._last_pushall = now
        with self._lock:
            self._sequence += 1
            sequence = str(self._sequence)
        payload = {
            "pushing": {
                "sequence_id": sequence,
                "command": "pushall",
                "version": 1,
                "push_target": 1,
            }
        }
        try:
            client.publish(self.topic_request, json.dumps(payload))
        except Exception:
            pass

    def request_version(self) -> None:
        client = self._client
        if client is None:
            return
        with self._lock:
            self._sequence += 1
            sequence = str(self._sequence)
        payload = {"info": {"sequence_id": sequence, "command": "get_version"}}
        try:
            client.publish(self.topic_request, json.dumps(payload))
        except Exception:
            pass

    def publish_command(self, section: str, command: str, **fields: Any) -> bool:
        """下发一条控制命令（自动补 sequence_id）。

        ``section`` 通常是 ``print``（暂停/继续/停止/速度）或 ``system``（灯光）。
        返回 False 表示当前没有可用的 MQTT 连接。
        """
        client = self._client
        if client is None or not self.topic_request:
            return False
        with self._lock:
            self._sequence += 1
            sequence = str(self._sequence)
        body: dict[str, Any] = {"sequence_id": sequence, "command": command}
        body.update(fields)
        payload = {section: body}
        try:
            info = client.publish(self.topic_request, json.dumps(payload))
            return info is not None
        except Exception:
            LOGGER.warning("控制命令发送失败：%s.%s", section, command, exc_info=True)
            return False

    @property
    def stale(self) -> bool:
        return (time.time() - self.last_message_ts) > 30 if self.last_message_ts else True

    # ------------------------------------------------------------------ 回调
    def _set_state(self, state: str, detail: str = "") -> None:
        self.state = state
        self.detail = detail
        if self._on_state is not None:
            try:
                self._on_state(state, detail)
            except Exception:
                pass

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):  # noqa: ANN001
        code = getattr(reason_code, "value", reason_code)
        if code == 0:
            self.connect_count += 1
            self.last_connected_at = time.time()
            self._set_state(self.STATE_ONLINE, "遥测已连接")
            try:
                client.subscribe(self.topic_report, qos=0)
            except Exception:
                pass
            if self.serial:
                self.request_pushall(force=True)
                self.request_version()
        elif code in (4, 5, 134, 135):
            self._set_state(self.STATE_AUTH_ERROR, "访问代码错误或未被授权（MQTT 拒绝连接）")
        else:
            self._set_state(self.STATE_OFFLINE, f"MQTT 连接被拒绝（错误码 {code}）")

    def _on_disconnect(self, client, userdata, *args):  # noqa: ANN001
        if self._client is None:
            return
        self._set_state(self.STATE_OFFLINE, "遥测连接断开，正在重连")

    def _on_message(self, client, userdata, message):  # noqa: ANN001
        self.last_message_ts = time.time()
        topic = getattr(message, "topic", "") or ""
        # 未填写序列号时使用通配订阅，从主题里反推序列号
        if not self.serial and topic.startswith("device/") and topic.endswith("/report"):
            parts = topic.split("/")
            if len(parts) >= 2 and parts[1]:
                self.update_serial(parts[1])
                if self._on_serial is not None:
                    try:
                        self._on_serial(parts[1])
                    except Exception:
                        pass
        try:
            payload = json.loads(message.payload.decode("utf-8", errors="replace"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return
        if not isinstance(payload, dict):
            return
        if "print" in payload:
            self._set_state(self.STATE_ONLINE, "遥测正常")
        if self._on_report is not None:
            try:
                self._on_report(payload)
            except Exception:
                pass
