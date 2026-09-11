"""内置网页服务：把监控墙搬到浏览器里，方便手机/其它电脑远程查看。

* `GET /`                监控墙页面
* `GET /api/printers`    各台打印机的状态 JSON（进度 / 喷嘴 / 热床 …）
* `GET /api/frame/<i>`   单张快照 JPEG
* `GET /stream/<i>`      MJPEG 持续推流（浏览器 `<img>` 直接可播）

为控制开销，网页端使用的 JPEG 由后台线程**按需**生成：只有真的有客户端在看某一路
时才转码，且多客户端共享同一份缓存，不会重复编码。
"""

from __future__ import annotations

import json
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Optional
from urllib.parse import parse_qs, urlparse

from ..bambu.discovery import local_interfaces
from ..bambu.printer import PrinterSession
from .icons import icon_bytes
from .page import INDEX_HTML
from .service_worker import SERVICE_WORKER_JS

BOUNDARY = "bmframe"
#: 多路复用流：每条记录的帧头（魔数 2 字节 + 类型 1 + 画面序号 2 + 长度 4）
RECORD_MAGIC = b"BM"
RECORD_HEADER = 9
KIND_FRAME = 1
KIND_STATUS = 2
STATUS_INDEX = 0xFFFF
#: 小于这个体积的 JPEG 直接复用，不再解码重编码
PASSTHROUGH_BYTES = 90_000
#: 超过这么久没有客户端观看就停止为该路转码
CLIENT_TTL = 6.0


def _shrink_jpeg(jpeg: bytes, max_width: int) -> Optional[bytes]:
    """把大图缩到网页端合适的尺寸（在后台线程调用）。"""
    try:
        from PySide6.QtCore import QBuffer, QIODevice, QSize, Qt
        from PySide6.QtGui import QImage

        image = QImage.fromData(jpeg, "JPG")
        if image.isNull():
            return None
        if image.width() > max_width:
            image = image.scaled(
                QSize(max_width, max_width), Qt.KeepAspectRatio, Qt.SmoothTransformation
            )
        from PySide6.QtCore import QByteArray

        data = QByteArray()
        buffer = QBuffer(data)
        buffer.open(QIODevice.WriteOnly)
        image.save(buffer, "JPG", 72)
        buffer.close()
        return bytes(data.data())
    except Exception:  # noqa: BLE001 - 转码失败就退回原图
        return None


class WebFrameCache(threading.Thread):
    """为网页端准备 JPEG 帧（按需转码 + 多客户端共享）。"""

    def __init__(
        self,
        get_sessions: Callable[[], list[PrinterSession]],
        fps: float = 4.0,
        max_width: int = 720,
    ) -> None:
        super().__init__(name="web-frame-cache", daemon=True)
        self._get_sessions = get_sessions
        self._fps = max(0.5, float(fps))
        self._max_width = max(240, int(max_width))
        self._lock = threading.Lock()
        self._frames: dict[int, tuple[int, bytes]] = {}
        self._source_seq: dict[int, int] = {}
        self._active: dict[int, float] = {}
        self._live_clients = 0
        self._stop = threading.Event()
        self.encoded = 0

    # ------------------------------------------------------------------ 对外
    def touch(self, index: int) -> None:
        with self._lock:
            self._active[index] = time.time()

    def add_live_client(self) -> None:
        with self._lock:
            self._live_clients += 1

    def remove_live_client(self) -> None:
        with self._lock:
            self._live_clients = max(0, self._live_clients - 1)

    @property
    def live_clients(self) -> int:
        with self._lock:
            return self._live_clients

    def frame(self, index: int) -> tuple[int, bytes]:
        with self._lock:
            return self._frames.get(index, (0, b""))

    def stop(self) -> None:
        self._stop.set()

    def set_fps(self, fps: float, max_width: Optional[int] = None) -> None:
        with self._lock:
            self._fps = max(0.5, float(fps))
            if max_width:
                self._max_width = max(240, int(max_width))

    # ------------------------------------------------------------------ 内部
    def run(self) -> None:
        interval = 1.0 / self._fps
        while not self._stop.is_set():
            started = time.time()
            sessions = list(self._get_sessions())
            now = time.time()
            for index, session in enumerate(sessions):
                with self._lock:
                    last_seen = self._active.get(index, 0.0)
                    watching = self._live_clients > 0
                if not watching and now - last_seen > CLIENT_TTL:
                    continue
                seq, jpeg = session.latest_frame()
                if not jpeg:
                    continue
                if self._source_seq.get(index) == seq:
                    continue
                self._source_seq[index] = seq
                payload = jpeg
                if len(jpeg) > PASSTHROUGH_BYTES:
                    shrunk = _shrink_jpeg(jpeg, self._max_width)
                    if shrunk:
                        payload = shrunk
                with self._lock:
                    self._frames[index] = (seq, payload)
                self.encoded += 1
            elapsed = time.time() - started
            self._stop.wait(max(0.02, interval - elapsed))


class _Handler(BaseHTTPRequestHandler):
    server_version = "BambuMonitorWeb/1.0"
    protocol_version = "HTTP/1.1"

    # ------------------------------------------------------------------ 工具
    @property
    def app(self) -> "WebServer":
        return self.server.app  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        if self.app.verbose:
            print(f"[web] {self.address_string()} {fmt % args}")

    def _authorized(self, query: dict) -> bool:
        token = self.app.token
        if not token:
            return True
        supplied = (query.get("token", [""])[0] or "").strip()
        if not supplied:
            cookie = self.headers.get("Cookie", "")
            for part in cookie.split(";"):
                if "=" in part:
                    key, value = part.split("=", 1)
                    if key.strip() == "bm_token":
                        supplied = value.strip()
                        break
        return supplied == token

    def _send_bytes(self, payload: bytes, content_type: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.end_headers()
        try:
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def _sessions(self) -> list[PrinterSession]:
        return list(self.app.get_sessions())

    # ------------------------------------------------------------------ 路由
    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        path = parsed.path.rstrip("/") or "/"

        # /health 不需要令牌，方便做存活检查
        if path == "/health":
            self._send_bytes(b"ok", "text/plain; charset=utf-8")
            return

        if not self._authorized(query):
            self._send_bytes(
                json.dumps({"error": "unauthorized"}, ensure_ascii=False).encode(),
                "application/json; charset=utf-8",
                HTTPStatus.UNAUTHORIZED,
            )
            return

        if path in ("/", "/index.html"):
            self._send_bytes(INDEX_HTML.encode("utf-8"), "text/html; charset=utf-8")
        elif path == "/api/printers":
            self._printers()
        elif path == "/api/live":
            self._live()
        elif path.startswith("/api/frame/"):
            self._single_frame(path.rsplit("/", 1)[-1])
        elif path.startswith("/stream/"):
            self._stream(path.rsplit("/", 1)[-1])
        elif path == "/manifest.webmanifest":
            self._manifest()
        elif path == "/sw.js":
            self._send_bytes(SERVICE_WORKER_JS.encode("utf-8"), "text/javascript; charset=utf-8")
        elif path in ("/favicon.ico", "/icon-192.png", "/icon-512.png",
                      "/icon-192-maskable.png", "/icon-512-maskable.png"):
            self._icon(path.rsplit("/", 1)[-1])
        else:
            self._send_bytes(b"not found", "text/plain; charset=utf-8", HTTPStatus.NOT_FOUND)

    def _manifest(self) -> None:
        """PWA 清单：把令牌写进 start_url，装到手机主屏后打开即已登录。"""
        token = self.app.token
        start = f"/?token={token}" if token else "/"
        manifest = {
            "name": "拓竹打印机监控台",
            "short_name": "打印机监控",
            "description": "局域网内拓竹 3D 打印机的实时画面与进度/温度监控",
            "lang": "zh-CN",
            "start_url": start,
            "scope": "/",
            "display": "standalone",
            "orientation": "any",
            "background_color": "#0d1114",
            "theme_color": "#151c21",
            "icons": [
                {"src": "/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
                {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"},
                {"src": "/icon-192-maskable.png", "sizes": "192x192", "type": "image/png", "purpose": "maskable"},
                {"src": "/icon-512-maskable.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"},
            ],
        }
        self._send_bytes(
            json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"),
            "application/manifest+json; charset=utf-8",
        )

    def _icon(self, name: str) -> None:
        payload = icon_bytes("favicon.ico" if name == "favicon.ico" else name)
        if not payload:
            self._send_bytes(b"not found", "text/plain; charset=utf-8", HTTPStatus.NOT_FOUND)
            return
        self._send_bytes(payload, "image/png")

    def _printers(self) -> None:
        self._send_bytes(
            json.dumps(self._status_payload(), ensure_ascii=False).encode("utf-8"),
            "application/json; charset=utf-8",
        )

    def _status_payload(self) -> dict:
        sessions = self._sessions()
        items = []
        for session in sessions:
            status = session.snapshot()
            info = session.info
            problems = []
            if status.print_error:
                problems.append(f"E{status.print_error}")
            mqtt = status.mqtt_online
            camera = status.camera_online
            if camera and mqtt:
                status_text = "在线"
            elif camera:
                status_text = "画面正常·遥测断开"
            elif session.mqtt_auth_error or session.last_camera_state == "auth_error":
                status_text = "访问代码错误"
            elif info.access_code:
                status_text = (session.last_camera_detail or "连接中")[:14]
            else:
                status_text = "未配置访问代码"

            def tray_payload(tray) -> dict:
                return {
                    "label": tray.label,
                    "type": tray.tray_type or "",
                    "color": tray.color_hex,
                    "remain": tray.remain,
                    # 读不到余量时为空字符串，前端直接不显示，避免误导
                    "remain_text": tray.remain_text,
                    "remain_known": tray.remain_known,
                    "remain_hint": tray.remain_hint,
                    "external": tray.external,
                }

            active = status.active_tray
            items.append(
                {
                    "index": len(items),
                    "name": info.display_name(),
                    "ip": info.ip,
                    "model": info.model.label,
                    "span": max(1, min(3, int(info.tile_span or 1))),
                    "backend": session.video_backend,
                    "fps": round(session.camera_fps, 1),
                    "status_text": status_text,
                    "mqtt_online": mqtt,
                    "camera_online": camera,
                    "can_control": session.can_control,
                    # 新机型固件会要求 MQTT 命令签名，未开 Developer Mode 时控制会被静默忽略；
                    # 前端据此解释「为什么按钮是灰的」（只增字段，不改已有键）
                    "controls_blocked_reason": getattr(session, "controls_blocked_reason", ""),
                    "state_text": status.state_text if (mqtt or status.gcode_state) else "离线",
                    "progress": max(0, min(100, status.progress)),
                    "remaining_text": status.remaining_text,
                    "finish_time": status.finish_time_text,
                    "task": status.subtask_name or "",
                    "nozzle": f"{status.nozzle_temper:.0f}",
                    "nozzle_target": f"{status.nozzle_target_temper:.0f}",
                    "bed": f"{status.bed_temper:.0f}",
                    "bed_target": f"{status.bed_target_temper:.0f}",
                    "chamber": (
                        f"{status.chamber_temper:.0f}"
                        if status.chamber_temper is not None and info.model.has_chamber_sensor
                        else ""
                    ),
                    "wifi": status.wifi_signal,
                    "wifi_level": status.wifi_level,
                    "light": (
                        "on" if status.light_on else ("off" if status.light_on is False else "")
                    ),
                    "paused": status.is_paused,
                    "printing": status.is_printing,
                    "ams": [tray_payload(tray) for tray in status.ams_trays],
                    "external": tray_payload(status.vt_tray) if status.vt_tray else None,
                    "active_tray": active.label if active else "",
                    "hms": [
                        {
                            "code": item.short_code,
                            "text": item.text or "（本地文案表里没有这条错误码）",
                            "wiki": item.wiki,
                        }
                        for item in status.hms_items
                    ],
                    "layer_text": (
                        f"层 {status.layer_num}/{status.total_layer_num}"
                        if status.total_layer_num
                        else ""
                    ),
                    "problem_text": " ".join(problems),
                }
            )
        return {
            "total": len(items),
            "camera_online": sum(1 for item in items if item["camera_online"]),
            "mqtt_online": sum(1 for item in items if item["mqtt_online"]),
            "printing": sum(1 for item in items if item["printing"]),
            "web_fps": self.app.fps,
            "printers": items,
        }

    # ------------------------------------------------------------------ 控制
    def _control(self, index: int, action: str, value: str = "") -> tuple[bool, str]:
        """执行一条控制指令；返回 (是否成功, 说明)。"""
        sessions = self._sessions()
        if not 0 <= index < len(sessions):
            return False, "没有这台打印机"
        session = sessions[index]
        if not session.can_control:
            return False, "遥测未连接，无法下发指令"
        if action == "pause":
            ok = session.pause_print()
            return ok, "已发送暂停指令" if ok else "发送失败"
        if action == "resume":
            ok = session.resume_print()
            return ok, "已发送继续指令" if ok else "发送失败"
        if action == "stop":
            ok = session.stop_print()
            return ok, "已发送停止指令" if ok else "发送失败"
        if action in ("light_on", "light_off", "light_toggle"):
            if action == "light_toggle":
                target = not bool(session.snapshot().light_on)
            else:
                target = action == "light_on"
            ok = session.set_light(target)
            return ok, ("已发送开灯指令" if target else "已发送关灯指令") if ok else "发送失败"
        if action == "speed":
            try:
                level = max(1, min(4, int(value or 2)))
            except ValueError:
                return False, "速度档位不合法"
            ok = session.set_speed(level)
            return ok, f"已发送速度档位 {level}" if ok else "发送失败"
        return False, f"未知指令：{action}"

    # ------------------------------------------------------------------ 多路复用
    def _live(self) -> None:
        """一条连接同时推送所有画面的帧与状态。

        浏览器的同域长连接上限是 6 条，而每一路 MJPEG 各占一条，
        因此超过 6 路时后面的画面会一直排队 —— 这里改成单连接多路复用：
        记录格式为 BM(2) + 类型(1) + 画面序号(2,LE) + 长度(4,LE) + 负载。
        """
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Cache-Control", "no-store, no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Connection", "close")
        self.end_headers()

        cache = self.app.cache
        cache.add_live_client()
        last_seq: dict[int, int] = {}
        last_status = 0.0
        try:
            while not self.app.stopping:
                sessions = self._sessions()
                if not sessions:
                    break
                for index in range(len(sessions)):
                    cache.touch(index)
                    seq, jpeg = cache.frame(index)
                    if not jpeg or last_seq.get(index) == seq:
                        continue
                    last_seq[index] = seq
                    if not self._write_record(KIND_FRAME, index, jpeg):
                        return
                now = time.time()
                if now - last_status > 1.0:
                    payload = json.dumps(self._status_payload(), ensure_ascii=False).encode("utf-8")
                    if not self._write_record(KIND_STATUS, STATUS_INDEX, payload):
                        return
                    last_status = now
                time.sleep(0.02)
        finally:
            cache.remove_live_client()

    def _write_record(self, kind: int, index: int, payload: bytes) -> bool:
        header = (
            RECORD_MAGIC
            + bytes([kind])
            + index.to_bytes(2, "little")
            + len(payload).to_bytes(4, "little")
        )
        try:
            self.wfile.write(header)
            self.wfile.write(payload)
            self.wfile.flush()
            return True
        except (BrokenPipeError, ConnectionResetError, OSError):
            return False

    def do_POST(self) -> None:  # noqa: N802
        """控制接口：POST /api/command  {"index":0,"action":"pause"}"""
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        path = parsed.path.rstrip("/") or "/"
        if not self._authorized(query):
            self._send_bytes(
                json.dumps({"error": "unauthorized"}, ensure_ascii=False).encode(),
                "application/json; charset=utf-8",
                HTTPStatus.UNAUTHORIZED,
            )
            return
        if path != "/api/command":
            self._send_bytes(b"not found", "text/plain; charset=utf-8", HTTPStatus.NOT_FOUND)
            return
        try:
            length = int(self.headers.get("Content-Length", "0") or 0)
        except ValueError:
            length = 0
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._send_bytes(b'{"ok":false,"detail":"bad json"}', "application/json", HTTPStatus.BAD_REQUEST)
            return
        try:
            index = int(body.get("index", -1))
        except (TypeError, ValueError):
            index = -1
        action = str(body.get("action", ""))
        value = str(body.get("value", ""))
        ok, detail = self._control(index, action, value)
        self._send_bytes(
            json.dumps({"ok": ok, "detail": detail}, ensure_ascii=False).encode("utf-8"),
            "application/json; charset=utf-8",
            HTTPStatus.OK if ok else HTTPStatus.BAD_REQUEST,
        )

    def _single_frame(self, raw_index: str) -> None:
        try:
            index = int(raw_index)
        except ValueError:
            self._send_bytes(b"bad index", "text/plain", HTTPStatus.BAD_REQUEST)
            return
        sessions = self._sessions()
        if not 0 <= index < len(sessions):
            self._send_bytes(b"no such printer", "text/plain", HTTPStatus.NOT_FOUND)
            return
        self.app.cache.touch(index)
        _, jpeg = self.app.cache.frame(index)
        if not jpeg:
            seq, jpeg = sessions[index].latest_frame()
            if not jpeg:
                self._send_bytes(b"no frame", "text/plain", HTTPStatus.SERVICE_UNAVAILABLE)
                return
        self._send_bytes(jpeg, "image/jpeg")

    def _stream(self, raw_index: str) -> None:
        try:
            index = int(raw_index)
        except ValueError:
            self._send_bytes(b"bad index", "text/plain", HTTPStatus.BAD_REQUEST)
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", f"multipart/x-mixed-replace; boundary={BOUNDARY}")
        self.send_header("Cache-Control", "no-store, no-cache")
        self.send_header("Connection", "close")
        self.end_headers()

        last_seq = -1
        while not self.app.stopping:
            if len(self._sessions()) <= index:
                break
            self.app.cache.touch(index)
            seq, jpeg = self.app.cache.frame(index)
            if jpeg and seq != last_seq:
                last_seq = seq
                header = (
                    f"--{BOUNDARY}\r\nContent-Type: image/jpeg\r\n"
                    f"Content-Length: {len(jpeg)}\r\n\r\n"
                ).encode()
                try:
                    self.wfile.write(header)
                    self.wfile.write(jpeg)
                    self.wfile.write(b"\r\n")
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    return
            time.sleep(0.05)


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class WebServer:
    """网页服务的生命周期管理。"""

    def __init__(
        self,
        get_sessions: Callable[[], list[PrinterSession]],
        port: int = 8080,
        token: str = "",
        fps: float = 4.0,
        max_width: int = 720,
        host: str = "0.0.0.0",
        verbose: bool = False,
    ) -> None:
        self.get_sessions = get_sessions
        self.port = port
        self.token = token
        self.fps = fps
        self.host = host
        self.verbose = verbose
        #: 记住画面最大宽度：start() 重建转码线程时要原样恢复（否则重启后静默退回默认值）
        self.max_width = max(240, int(max_width))
        self.cache = WebFrameCache(get_sessions, fps=fps, max_width=self.max_width)
        self.stopping = True
        self._httpd: Optional[_Server] = None
        self._thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------------ 生命周期
    @property
    def running(self) -> bool:
        return self._httpd is not None

    def start(self) -> bool:
        if self._httpd is not None:
            return True
        try:
            httpd = _Server((self.host, self.port), _Handler)
        except OSError:
            return False
        httpd.app = self  # type: ignore[attr-defined]
        self._httpd = httpd
        self.stopping = False
        # 重新开启服务时要把旧的转码线程收干净，否则反复开关会累积线程。
        # 注意：__init__ 里建的那个 cache 从未 start()，对未启动的线程 join()
        # 会抛 RuntimeError，所以必须先判断 is_alive()。
        self._stop_cache(self.cache)
        # max_width 必须一起传：漏传会让网页画面宽度静默退回默认 720，与用户设置不符
        self.cache = WebFrameCache(
            self.get_sessions, fps=self.fps, max_width=self.max_width
        )
        self.cache.start()
        self._thread = threading.Thread(target=httpd.serve_forever, name="web-server", daemon=True)
        self._thread.start()
        return True

    @staticmethod
    def _stop_cache(cache: "WebFrameCache") -> None:
        """停掉转码线程并等它退出（线程从未启动时只置标志，不 join）。"""
        cache.stop()
        if cache.is_alive():
            cache.join(timeout=2.0)

    def stop(self) -> None:
        self.stopping = True
        self._stop_cache(self.cache)
        httpd, self._httpd = self._httpd, None
        if httpd is not None:
            try:
                httpd.shutdown()
            except Exception:
                pass
            try:
                httpd.server_close()
            except Exception:
                pass
        thread, self._thread = self._thread, None
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)

    # ------------------------------------------------------------------ 信息
    def urls(self) -> list[str]:
        """返回可供其它设备访问的地址（带令牌）。

        优先给出「打印机所在网段」的地址：手机通常和打印机连同一个 Wi-Fi，
        用内网穿透/虚拟网卡的地址反而访问不到。
        """
        query = f"?token={self.token}" if self.token else ""
        try:
            printer_prefixes = {
                session.info.ip.rsplit(".", 1)[0]
                for session in self.get_sessions()
                if session.info.ip.count(".") == 3
            }
        except Exception:
            printer_prefixes = set()

        local = [f"http://127.0.0.1:{self.port}/{query}"]
        others: list[str] = []
        preferred: list[str] = []
        for iface in local_interfaces():
            if iface.ip.startswith("127.") or not iface.scannable:
                continue
            # 虚拟网卡/代理网卡的地址手机一般访问不到，除非它就是打印机所在网段
            if not iface.sweepable and iface.prefix not in printer_prefixes:
                continue
            url = f"http://{iface.ip}:{self.port}/{query}"
            if iface.prefix in printer_prefixes:
                preferred.append(url)
            else:
                others.append(url)
        return local + preferred + others

    def primary_url(self) -> str:
        urls = self.urls()
        return urls[1] if len(urls) > 1 else urls[0]
