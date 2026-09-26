"""内置网页服务：把监控墙搬到浏览器里，方便手机/其它电脑远程查看。

* `GET /`                监控墙页面
* `GET /api/printers`    各台打印机的状态 JSON（进度 / 喷嘴 / 热床 …）
* `GET /api/frame/<i>`   单张快照 JPEG
* `GET /stream/<i>`      MJPEG 持续推流（浏览器 `<img>` 直接可播）

为控制开销，网页端使用的 JPEG 由后台线程**按需**生成：只有真的有客户端在看某一路
时才转码，且多客户端共享同一份缓存，不会重复编码。
"""

from __future__ import annotations

import base64
import json
import logging
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Optional
from urllib.parse import parse_qs, urlparse

from ..bambu.discovery import local_interfaces
from ..bambu.printer import PrinterSession
from ..core.device import camera_frame, camera_status_text, cameras_of, display_status
from ..core.registry import (
    credential_label,
    default_port,
    display_model,
    has_credential,
    resolve_family,
)
from .icons import icon_bytes
from .page import INDEX_HTML
from .service_worker import SERVICE_WORKER_JS

LOGGER = logging.getLogger("bambu-monitor.web")

BOUNDARY = "bmframe"
#: 多路复用流：每条记录的帧头（魔数 2 字节 + 类型 1 + 画面序号 2 + 长度 4）
RECORD_MAGIC = b"BM"
RECORD_HEADER = 9
KIND_FRAME = 1
KIND_STATUS = 2
#: H.264 记录：负载首字节区分「初始化参数」与「访问单元」，见 _push_h264
KIND_H264 = 3
STATUS_INDEX = 0xFFFF
#: 小于这个体积的 JPEG 直接复用，不再解码重编码
PASSTHROUGH_BYTES = 90_000
#: 超过这么久没有客户端观看就停止为该路转码
CLIENT_TTL = 6.0


def _shrink_with_qt(jpeg: bytes, max_width: int) -> Optional[bytes]:
    """桌面版路径：用 Qt 解码/缩放/重编码。"""
    from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QSize, Qt
    from PySide6.QtGui import QImage

    image = QImage.fromData(jpeg, "JPG")
    if image.isNull():
        return None
    if image.width() > max_width:
        image = image.scaled(
            QSize(max_width, max_width), Qt.KeepAspectRatio, Qt.SmoothTransformation
        )
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.WriteOnly)
    image.save(buffer, "JPG", 72)
    buffer.close()
    return bytes(data.data())


def _shrink_with_cv2(jpeg: bytes, max_width: int) -> Optional[bytes]:
    """服务端路径（Linux / Docker / NAS，没有 Qt）：用 OpenCV 缩放。

    ``requirements-server.txt`` 本来就装了 ``opencv-python-headless``（RTSPS 通道
    需要它），所以这里不会是新的依赖。
    """
    import cv2
    import numpy as np

    buffer = np.frombuffer(jpeg, dtype=np.uint8)
    image = cv2.imdecode(buffer, cv2.IMREAD_COLOR)
    if image is None:
        return None
    height, width = image.shape[:2]
    if width > max_width:
        scale = max_width / float(width)
        # INTER_AREA 缩小时质量最好（INTER_LINEAR 会明显发糊）
        image = cv2.resize(
            image, (max_width, max(1, int(round(height * scale)))), interpolation=cv2.INTER_AREA
        )
    ok, encoded = cv2.imencode(".jpg", image, [int(cv2.IMWRITE_JPEG_QUALITY), 72])
    return bytes(encoded.tobytes()) if ok else None


def _shrink_jpeg(jpeg: bytes, max_width: int) -> Optional[bytes]:
    """把大图缩到网页端合适的尺寸（在后台线程调用）。

    ## 为什么要有两条路径

    桌面版有 Qt（``QImage``），服务端（``requirements-server.txt``）**故意不装
    Qt**。原来这里只试 Qt，没有 Qt 时 ``import`` 就抛异常 -> 返回 None ->
    调用方「转码失败就用原图」-> 纯服务端部署下**每一帧都是未缩放的原始大图**
    （>90KB 的 1080p 画面），手机流量与内存开销成倍上涨，而且完全静默。

    现在补 OpenCV 兜底，并且把「两条路都不行」的情况记一次日志（不刷屏）。
    """
    try:
        return _shrink_with_qt(jpeg, max_width)
    except Exception:  # noqa: BLE001 - 没有 Qt 或解码失败，换下一条路径
        pass
    try:
        return _shrink_with_cv2(jpeg, max_width)
    except Exception:  # noqa: BLE001 - 没有 OpenCV 或解码失败
        _warn_shrink_unavailable()
        return None


_shrink_warned = False


def _warn_shrink_unavailable() -> None:
    """Qt 与 OpenCV 都不可用时只提示一次（每帧都打日志会把日志刷爆）。"""
    global _shrink_warned
    if _shrink_warned:
        return
    _shrink_warned = True
    LOGGER.warning(
        "既没有 Qt 也没有 OpenCV，超过 %d 字节的画面将按原图推送（网页流量会明显变大）。"
        "服务端部署请安装 requirements-server.txt",
        PASSTHROUGH_BYTES,
    )


def _as_port(value: object) -> int:
    """请求体里的端口：非法/缺失一律当 0（= 用该族的默认端口）。"""
    try:
        port = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0
    return port if 0 <= port <= 65535 else 0


def _details_of(session: PrinterSession) -> list[dict]:
    """会话提供的额外读数（没有这个方法的老会话返回空列表，不影响前端）。"""
    getter = getattr(session, "details", None)
    if not callable(getter):
        return []
    try:
        rows = getter()
    except Exception:  # noqa: BLE001 - 读数拿不到不能把整个状态接口带崩
        LOGGER.debug("读取额外读数失败", exc_info=True)
        return []
    if not isinstance(rows, list):
        return []
    clean = []
    for row in rows:
        if isinstance(row, dict) and row.get("label"):
            clean.append({"label": str(row["label"]), "value": str(row.get("value", ""))})
    return clean


def _families_payload() -> list[dict]:
    """已注册的设备族（供网页端渲染「设备族」下拉与凭据字段标签）。

    网页与安卓端的添加表单以前只认拓竹：标签写死「访问代码」、也没有端口，
    于是加了 Moonraker 设备后根本没有入口能填对。这里把注册表里的信息
    原样交给前端，**前端不再自己写死任何族特有的文案**。
    """
    from ..core.registry import all_families

    items = []
    for descriptor in all_families():
        policy = descriptor.credential
        items.append(
            {
                "family": descriptor.family,
                "label": descriptor.label,
                "credential": {
                    "key": policy.key,
                    "label": policy.label,
                    "required": policy.required,
                    "secret": policy.secret,
                    "hint": policy.hint,
                },
                "default_port": descriptor.default_port,
                "candidate_ports": list(descriptor.candidate_ports),
                "discoveries": list(descriptor.discoveries),
            }
        )
    return items


def _video_mode_of(session: PrinterSession) -> str:
    """这台设备的画面怎么交给前端：``jpeg`` 还是 ``h264``。

    默认 ``jpeg``（会话里有现成 JPEG 帧，直接塞进 ``<img>``）。
    只有走上「纯 Python 取流 + 网页端解码」那条路时才返回 ``h264`` ——
    典型场景是安卓版没有 OpenCV 时对 RTSPS-only 机型的处理。
    """
    mode = getattr(session, "video_mode", "jpeg")
    return "h264" if mode == "h264" else "jpeg"


def _selected_camera(session: PrinterSession) -> int:
    """这台设备当前显示哪一路画面（多摄像头机器才有意义，默认 0）。"""
    try:
        return max(0, int(getattr(session.info, "camera_index", 0) or 0))
    except (TypeError, ValueError):
        return 0


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
        #: 键是 ``(设备序号, 画面序号)``：多摄像头机器上每台设备可能同时被看几路
        self._frames: dict[tuple[int, int], tuple[int, bytes]] = {}
        self._source_seq: dict[tuple[int, int], int] = {}
        self._active: dict[int, float] = {}
        self._live_clients = 0
        # ⚠️ 必须叫 _stop_event，不能叫 _stop：threading.Thread 自己有一个
        # 内部方法 _stop()，Python 3.10 的 Thread.join() 会在收尾时调用它
        # （_wait_for_tstate_lock -> self._stop()）。用 Event 覆盖掉这个名字后，
        # join() 会抛 "TypeError: 'Event' object is not callable"，
        # 表现为「线程明明跑完了却 join 失败」。3.13 改掉了这段实现，
        # 所以这个坑只在 3.10/3.11 上暴露（安卓版内嵌的正是 3.10）。
        self._stop_event = threading.Event()
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

    def frame(self, index: int, camera: int = 0) -> tuple[int, bytes]:
        with self._lock:
            return self._frames.get((index, camera), (0, b""))

    def stop(self) -> None:
        self._stop_event.set()

    def set_fps(self, fps: float, max_width: Optional[int] = None) -> None:
        with self._lock:
            self._fps = max(0.5, float(fps))
            if max_width:
                self._max_width = max(240, int(max_width))

    # ------------------------------------------------------------------ 内部
    def run(self) -> None:
        interval = 1.0 / self._fps
        while not self._stop_event.is_set():
            started = time.time()
            sessions = list(self._get_sessions())
            now = time.time()
            for index, session in enumerate(sessions):
                with self._lock:
                    last_seen = self._active.get(index, 0.0)
                    watching = self._live_clients > 0
                if not watching and now - last_seen > CLIENT_TTL:
                    continue
                # 每台设备可能有多路画面（Voron 的喷嘴 + 舱内）：缓存里那一路是
                # 用户**当前选中的**那一路（`info.camera_index`），界面切换后
                # 下一轮就会换成新的一路。
                camera = _selected_camera(session)
                seq, jpeg = camera_frame(session, camera)
                if not jpeg:
                    continue
                key = (index, camera)
                if self._source_seq.get(key) == seq:
                    continue
                self._source_seq[key] = seq
                payload = jpeg
                if len(jpeg) > PASSTHROUGH_BYTES:
                    shrunk = _shrink_jpeg(jpeg, self._max_width)
                    if shrunk:
                        payload = shrunk
                with self._lock:
                    self._frames[key] = (seq, payload)
                self.encoded += 1
            elapsed = time.time() - started
            self._stop_event.wait(max(0.02, interval - elapsed))


class _Handler(BaseHTTPRequestHandler):
    server_version = "BambuMonitorWeb/1.0"
    protocol_version = "HTTP/1.1"

    def __init__(self, *args, **kwargs) -> None:
        #: POST 请求体缓存（一次读干净，见 do_POST 的说明）
        self._raw_body: Optional[bytes] = None
        super().__init__(*args, **kwargs)

    # ------------------------------------------------------------------ 工具
    @property
    def app(self) -> "WebServer":
        return self.server.app  # type: ignore[attr-defined]

    # ------------------------------------------------------------------ 宿主回调
    # 设备管理与设置的真实实现由宿主（安卓版 / 桌面版）注入到 **WebServer** 上，
    # 而处理器实例本身没有这些属性 —— 必须经 self.app 取。
    # 这里统一做一层转发，避免在每个处理器里写 self.app.xxx（漏一处就是
    # AttributeError: '_Handler' object has no attribute ...）。
    @property
    def discover_fn(self):
        return self.app.discover_fn

    @property
    def add_printer_fn(self):
        return self.app.add_printer_fn

    @property
    def manage_printer_fn(self):
        return self.app.manage_printer_fn

    @property
    def get_settings_fn(self):
        return self.app.get_settings_fn

    @property
    def update_settings_fn(self):
        return self.app.update_settings_fn

    @property
    def export_config_fn(self):
        return self.app.export_config_fn

    @property
    def import_config_fn(self):
        return self.app.import_config_fn

    @property
    def diagnose_fn(self):
        return self.app.diagnose_fn

    @property
    def layout_fn(self):
        return self.app.layout_fn

    @property
    def camera_action_fn(self):
        return self.app.camera_action_fn

    @property
    def reorder_fn(self):
        return getattr(self.app, "reorder_fn", None)

    @property
    def sessions_action_fn(self):
        return getattr(self.app, "sessions_action_fn", None)

    @property
    def info_fn(self):
        return self.app.info_fn

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
        elif path == "/api/discover":
            self._discover()
        elif path == "/api/settings":
            # 读取设置走 GET，修改走 POST（同一个处理器按 self.command 分支）
            self._settings()
        elif path == "/api/live":
            self._live()
        elif path.startswith("/api/frame/"):
            self._single_frame(path.rsplit("/", 1)[-1])
        elif path.startswith("/stream/"):
            self._stream(path.rsplit("/", 1)[-1])
        elif path == "/manifest.webmanifest":
            self._manifest()
        elif path == "/api/info":
            self._info()
        elif path == "/api/diagnose":
            self._diagnose(query)
        elif path == "/sw.js":
            self._send_bytes(SERVICE_WORKER_JS.encode("utf-8"), "text/javascript; charset=utf-8")
        elif path in ("/favicon.ico", "/icon-192.png", "/icon-512.png",
                      "/icon-192-maskable.png", "/icon-512-maskable.png"):
            self._icon(path.rsplit("/", 1)[-1])
        else:
            self._send_bytes(b"not found", "text/plain; charset=utf-8", HTTPStatus.NOT_FOUND)

    def _info(self) -> None:
        """连接信息：令牌 + 可供其它设备访问的地址。

        前端「⚙ 设置 → 在其它设备上打开」用它显示地址与令牌。
        没有这个接口时，安卓版拿不到令牌（前端拿到后会立刻把它从 URL 上抹掉，
        免得截图/分享时泄露），于是用户在平板上能用、却没法在电脑上打开。
        """
        if self.info_fn is None:
            self._send_bytes(
                json.dumps(
                    {"supported": False, "detail": "该运行方式不提供连接信息"},
                    ensure_ascii=False,
                ).encode("utf-8"),
                "application/json; charset=utf-8",
                HTTPStatus.NOT_IMPLEMENTED,
            )
            return
        try:
            # 端口取**实际绑定**的那个：`port=0`（系统分配临时端口）时
            # WebServer.port 还是 0，而 server_address 才是真值。
            bound = getattr(self.server, "server_address", None)
            port = int(bound[1]) if bound else int(getattr(self.app, "port", 0) or 0)
            payload = self.info_fn(port)
        except Exception as exc:  # noqa: BLE001 - 要让前端看到原因
            self._send_bytes(
                json.dumps(
                    {"supported": True, "error": f"{type(exc).__name__}: {exc}"},
                    ensure_ascii=False,
                ).encode("utf-8"),
                "application/json; charset=utf-8",
                HTTPStatus.INTERNAL_SERVER_ERROR,
            )
            return
        self._send_bytes(
            json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            "application/json; charset=utf-8",
        )

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

    def _discover(self) -> None:
        """扫描局域网并把结果回给前端。

        网页端原先只能**监控**已配置的设备，不能在界面上添加打印机 ——
        在平板上尤其致命（安卓版没有桌面端的「自动搜索」对话框，
        用户会被卡在"没有设备"这一步）。这里把发现能力暴露成 API。

        ``discover_fn`` 由宿主注入：桌面/无界面版传 None（返回"不支持"），
        安卓版注入内置实现。这样既不把 Android 专有代码塞进通用服务端，
        也让测试能注入假实现。
        """
        if self.discover_fn is None:
            self._send_bytes(
                json.dumps(
                    {"supported": False, "detail": "该运行方式不支持在网页上搜索设备"},
                    ensure_ascii=False,
                ).encode("utf-8"),
                "application/json; charset=utf-8",
                HTTPStatus.NOT_IMPLEMENTED,
            )
            return
        try:
            found = self.discover_fn()
        except Exception as exc:  # noqa: BLE001 - 搜索失败要让前端看到原因
            self._send_bytes(
                json.dumps(
                    {"supported": True, "error": f"{type(exc).__name__}: {exc}"},
                    ensure_ascii=False,
                ).encode("utf-8"),
                "application/json; charset=utf-8",
                HTTPStatus.INTERNAL_SERVER_ERROR,
            )
            return
        known_ips = {session.info.ip for session in self._sessions()}
        items = []
        for item in found or []:
            entry = dict(item)
            entry["known"] = entry.get("ip") in known_ips
            items.append(entry)
        self._send_bytes(
            json.dumps({"supported": True, "total": len(items), "printers": items},
                       ensure_ascii=False).encode("utf-8"),
            "application/json; charset=utf-8",
        )

    def _status_payload(self) -> dict:
        sessions = self._sessions()
        items = []
        for session in sessions:
            # 界面视图：第三方族的状态字段名与拓竹不同（job_state/progress_percent…），
            # 这里统一翻译成界面认识的那套（详见 app/core/device.py::display_status）
            status = display_status(session.snapshot())
            info = session.info
            problems = []
            if status.print_error:
                problems.append(f"E{status.print_error}")
            mqtt = status.mqtt_online
            camera = status.camera_online
            # 判据与桌面版共用一处（app.core.camera_status_text）：返回短标签 +
            # 完整说明。以前这里把完整说明截到 14 个字符直接当标签用，中文提示
            # 会被截得看不懂；现在完整说明走 status_detail，前端做悬浮提示。
            status_text, status_detail = camera_status_text(
                camera_online=camera,
                mqtt_online=mqtt,
                camera_state=session.last_camera_state,
                camera_detail=session.last_camera_detail,
                mqtt_auth_error=session.mqtt_auth_error,
                has_access_code=has_credential(info),
                credential_label=credential_label(info),
                # 凭据不是必填的族（内网 Moonraker 免鉴权）不该显示「未配置 API Key」
                credential_required=bool(resolve_family(info).credential.required),
            )

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
            caps = session.capabilities
            items.append(
                {
                    "index": len(items),
                    "name": info.display_name(),
                    "ip": info.ip,
                    "model": display_model(info),
                    # 设备族：**逐台回传**，这样网页端不需要靠 model 猜自己看的是哪一族
                    # （猜错的后果是「编辑」表单把凭据写进错的字段，直接丢掉）
                    "family": resolve_family(info).family,
                    "family_label": resolve_family(info).label,
                    "credential_label": credential_label(info),
                    "port": int(default_port(info) or 0),
                    "span": max(1, min(3, int(info.tile_span or 1))),
                    "backend": session.video_backend,
                    "fps": round(session.camera_fps, 1),
                    # 画面交付方式：jpeg = 直接给 <img>；h264 = 码流走多路复用通道，
                    # 由网页端用 WebCodecs 解码（安卓版没有 OpenCV，只能这样）
                    "video_mode": _video_mode_of(session),
                    "status_text": status_text,
                    "status_detail": status_detail,
                    "mqtt_online": mqtt,
                    "camera_online": camera,
                    "can_control": session.can_control,
                    # 新机型固件会要求 MQTT 命令签名，未开 Developer Mode 时控制会被静默忽略；
                    # 前端据此解释「为什么按钮是灰的」（只增字段，不改已有键）
                    "controls_blocked_reason": getattr(session, "controls_blocked_reason", ""),
                    # 一句话版：窄位置（状态条）也要能说清「先局域网、再开发者」与农场管家
                    "controls_blocked_short": getattr(session, "controls_blocked_short", ""),
                    # 「这台设备在本机根本出不了画面」的说明（安卓上 RTSPS-only 机型就是这种）。
                    # 网页端会把它显示在画面区域：触屏没有悬浮提示，否则只有一个空画面。
                    "video_unavailable_reason": getattr(
                        session, "video_unavailable_reason", ""
                    ),
                    # 能力声明：让前端按「能力」而不是「机型」决定显示什么，
                    # 这样接入第三方设备族时前端不必改动（只增字段）
                    "capabilities": {
                        "chamber": caps.has_chamber_sensor,
                        "light": caps.can_control_light,
                        "wifi": caps.has_wifi_signal,
                        "camera": caps.has_camera,
                        "nozzles": caps.nozzle_count,
                        "hms": caps.has_hms,
                    },
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
                        if status.chamber_temper is not None
                        and session.capabilities.has_chamber_sensor
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
                    # 设备能提供、但不属于通用状态模型的读数（风扇 / 断料与走料传感器 /
                    # 工具头板温度 / 主机负载 / MCU 固件…）。第三方族走这条通路，
                    # 界面按族无关的方式逐条列出，见 app/core/adapter.py::details()
                    "details": _details_of(session),
                    # 这台设备可选哪几路画面 + 当前显示的是哪一路。
                    # 界面据此渲染「摄像头」切换；只有一路时不显示入口。
                    "cameras": cameras_of(session),
                    "camera": _selected_camera(session),
                    # 灯光命令：编辑表单要能回填当前值（留空 = 这台设备没有可控灯光）
                    "light_on_gcode": str(getattr(info, "light_on_gcode", "") or ""),
                    "light_off_gcode": str(getattr(info, "light_off_gcode", "") or ""),
                }
            )
        return {
            "total": len(items),
            "camera_online": sum(1 for item in items if item["camera_online"]),
            "mqtt_online": sum(1 for item in items if item["mqtt_online"]),
            "printing": sum(1 for item in items if item["printing"]),
            "web_fps": self.app.fps,
            # 已注册的设备族：网页端的添加/编辑表单据此渲染「设备族」下拉、
            # 凭据字段的标签与必填性、以及端口提示（前端不写死任何族特有文案）
            "families": _families_payload(),
            "printers": items,
        }

    # ------------------------------------------------------------------ 控制
    #: 网页动作名 -> 会话侧归一化命令名（用于查"该命令是否被固件签名要求挡住"）
    _ACTION_COMMAND = {
        "pause": "pause",
        "resume": "resume",
        "stop": "stop",
        "speed": "speed",
        "light_on": "light",
        "light_off": "light",
        "light_toggle": "light",
    }

    def _control(self, index: int, action: str, value: str = "") -> tuple[bool, str]:
        """执行一条控制指令；返回 (是否成功, 说明)。"""
        sessions = self._sessions()
        if not 0 <= index < len(sessions):
            return False, "没有这台打印机"
        session = sessions[index]
        if not session.can_control:
            return False, "遥测未连接，无法下发指令"
        # 固件要求命令签名时，**只有 print 段命令**会被忽略（灯控走 system 段，不受影响）。
        # 这里在发送前就按命令分别判断，把原因说清楚，而不是等设备静默忽略后
        # 回一句含糊的"发送失败"。
        # 用 getattr 容错：第三方设备族的适配器未必实现这个方法。
        check_blocked = getattr(session, "command_blocked", None)
        if callable(check_blocked):
            blocked = check_blocked(self._ACTION_COMMAND.get(action, action))
            if blocked:
                return False, blocked
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
                target = not bool(display_status(session.snapshot()).light_on)
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
        """一条连接同时推送所有画面的帧、H.264 码流与状态。

        浏览器的同域长连接上限是 6 条，而每一路 MJPEG 各占一条，
        因此超过 6 路时后面的画面会一直排队 —— 这里改成单连接多路复用：
        记录格式为 BM(2) + 类型(1) + 画面序号(2,LE) + 长度(4,LE) + 负载。

        三种记录：

        * ``KIND_FRAME``  —— 一帧 JPEG（直接给 ``<img>``）；
        * ``KIND_STATUS`` —— 状态 JSON；
        * ``KIND_H264``   —— 一路的 H.264「初始化参数」或「访问单元」（AVCC），
          由网页端的 WebCodecs 解码。没有 OpenCV 的环境（安卓版）靠它出画面。
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
        sent_params: set[int] = set()
        try:
            while not self.app.stopping:
                sessions = self._sessions()
                if not sessions:
                    break
                for index, session in enumerate(sessions):
                    if _video_mode_of(session) == "h264":
                        if not self._push_h264(index, session, sent_params):
                            return
                        continue
                    camera = _selected_camera(session)
                    cache.touch(index)
                    seq, jpeg = cache.frame(index, camera)
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

    def _push_h264(self, index: int, session: PrinterSession, sent_params: set[int]) -> bool:
        """把一路的 H.264 参数与访问单元推给网页端。

        参数（codec 串 + avcC description）只在开头推一次：网页端拿到后才能
        ``VideoDecoder.configure()``；之后每条记录就是一个访问单元。
        记录负载的格式：1 字节类型 + 内容 ——
        ``0x01`` = 初始化参数（JSON，``description`` 是 base64），``0x02`` = 访问单元。
        """
        fetcher = getattr(session, "latest_h264", None)
        if not callable(fetcher):
            return True
        params, units = fetcher()
        if params and index not in sent_params:
            payload = json.dumps(
                {
                    "codec": params.get("codec", ""),
                    "description": base64.b64encode(params.get("description", b"")).decode("ascii"),
                    "width": params.get("width", 0),
                    "height": params.get("height", 0),
                },
                ensure_ascii=False,
            ).encode("utf-8")
            if not self._write_record(KIND_H264, index, b"\x01" + payload):
                return False
            sent_params.add(index)
        for unit in units:
            key = b"\x03" if unit.is_keyframe else b"\x02"
            if not self._write_record(KIND_H264, index, key + unit.data):
                return False
        return True

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
        """控制与设备管理接口。

        * ``POST /api/command``       ``{"index":0,"action":"pause"}``
        * ``POST /api/add_printer``   ``{"name":"","ip":"","access_code":""}``

        ⚠️ **先读掉请求体再决定怎么回**：HTTP/1.1 默认 keep-alive，如果我们在
        没读完 body 的情况下就回错误（401 / 501 / 404），连接里会留下未读字节 ——
        客户端下一次写就会撞上「连接被主机中的软件中止」（Windows =
        ``ConnectionAbortedError: [WinError 10053]``），用户看到的是"网页请求
        随机失败"。Windows 上的 CI 就是这么炸的（Linux 表现不同，所以本地一直没暴露）。
        """
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        path = parsed.path.rstrip("/") or "/"
        self._read_raw_body()
        if not self._authorized(query):
            self._send_bytes(
                json.dumps({"error": "unauthorized"}, ensure_ascii=False).encode(),
                "application/json; charset=utf-8",
                HTTPStatus.UNAUTHORIZED,
            )
            return
        if path == "/api/add_printer":
            self._add_printer()
            return
        if path == "/api/printers":
            self._manage_printer()
            return
        if path == "/api/settings":
            self._settings()
            return
        if path == "/api/config/export":
            self._config_export()
            return
        if path == "/api/config/import":
            self._config_import()
            return
        if path == "/api/layout":
            self._layout()
            return
        if path == "/api/camera":
            self._camera()
            return
        if path == "/api/sessions":
            self._sessions_action()
            return
        if path != "/api/command":
            self._send_bytes(b"not found", "text/plain; charset=utf-8", HTTPStatus.NOT_FOUND)
            return
        try:
            body = json.loads(self._read_raw_body().decode("utf-8") or "{}")
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

    def _sessions_action(self) -> None:
        """``POST /api/sessions`` —— 全部连接 / 全部断开。

        桌面版工具栏有「▶ 全部连接」「■ 全部断开」，网页端过去只能一台一台点
        （而设备多的时候这正是最常用的操作）。``{"action": "connect_all"|"disconnect_all"}``。
        """
        if self.sessions_action_fn is None:
            self._send_bytes(
                json.dumps(
                    {"ok": False, "detail": "该运行方式不支持在网页上整体连接/断开"},
                    ensure_ascii=False,
                ).encode("utf-8"),
                "application/json; charset=utf-8",
                HTTPStatus.NOT_IMPLEMENTED,
            )
            return
        body = self._read_json_body()
        if body is None:
            self._send_bytes(
                json.dumps({"ok": False, "detail": "请求格式不正确"}, ensure_ascii=False).encode(),
                "application/json; charset=utf-8",
                HTTPStatus.BAD_REQUEST,
            )
            return
        try:
            result = self.sessions_action_fn(action=str(body.get("action", "") or ""))
        except Exception as exc:  # noqa: BLE001 - 失败原因要回到界面上
            result = {"ok": False, "detail": f"{type(exc).__name__}: {exc}"}
        ok = bool(result.get("ok"))
        self._send_bytes(
            json.dumps(result, ensure_ascii=False).encode("utf-8"),
            "application/json; charset=utf-8",
            HTTPStatus.OK if ok else HTTPStatus.BAD_REQUEST,
        )

    def _camera(self) -> None:
        """``POST /api/camera`` —— 画面的「刷新」与「切换哪一路」。

        * ``{"index": 0, "action": "refresh"}`` —— 重新发现摄像头并丢掉缓存帧。
          桌面版右键菜单一直有「重新连接」，而网页端连"刷新一下画面"都做不到；
          摄像头刚插上、或者之前 502 的那一路刚起来时，用户点一下就该看到结果。
        * ``{"index": 0, "action": "select", "camera": 1}`` —— 这台设备改看第 2 路
          （Voron 这类机器常有多路：喷嘴 + 舱内）。
        """
        if self.camera_action_fn is None:
            self._send_bytes(
                json.dumps(
                    {"ok": False, "detail": "该运行方式不支持在网页上切换画面"},
                    ensure_ascii=False,
                ).encode("utf-8"),
                "application/json; charset=utf-8",
                HTTPStatus.NOT_IMPLEMENTED,
            )
            return
        body = self._read_json_body()
        if body is None:
            self._send_bytes(
                json.dumps({"ok": False, "detail": "请求格式不正确"}, ensure_ascii=False).encode(),
                "application/json; charset=utf-8",
                HTTPStatus.BAD_REQUEST,
            )
            return
        try:
            index = int(body.get("index", -1))
        except (TypeError, ValueError):
            index = -1
        try:
            camera = int(body.get("camera", 0))
        except (TypeError, ValueError):
            camera = 0
        try:
            result = self.camera_action_fn(
                index=index,
                action=str(body.get("action", "") or ""),
                camera=max(0, camera),
            )
        except Exception as exc:  # noqa: BLE001 - 失败原因要回到界面上
            result = {"ok": False, "detail": f"{type(exc).__name__}: {exc}"}
        ok = bool(result.get("ok"))
        self._send_bytes(
            json.dumps(result, ensure_ascii=False).encode("utf-8"),
            "application/json; charset=utf-8",
            HTTPStatus.OK if ok else HTTPStatus.BAD_REQUEST,
        )

    def _add_printer(self) -> None:
        """添加/更新一台打印机，并在**不重启服务**的前提下让监控墙出现它。

        这是「平板上没有桌面端那个添加对话框」的补偿：网页里就能把设备加进来。
        实现要点：`add_printer_fn`（安卓版注入）负责落盘与构造会话；
        返回的会话由宿主直接接到当前会话列表上，因此界面立刻能看到新设备。
        """
        if self.add_printer_fn is None:
            self._send_bytes(
                json.dumps(
                    {"ok": False, "detail": "该运行方式不支持在网页上添加设备"},
                    ensure_ascii=False,
                ).encode("utf-8"),
                "application/json; charset=utf-8",
                HTTPStatus.NOT_IMPLEMENTED,
            )
            return
        body = self._read_json_body()
        if body is None:
            self._send_bytes(
                json.dumps({"ok": False, "detail": "请求格式不正确"}, ensure_ascii=False).encode(),
                "application/json; charset=utf-8",
                HTTPStatus.BAD_REQUEST,
            )
            return
        try:
            result = self.add_printer_fn(
                name=str(body.get("name", "") or ""),
                ip=str(body.get("ip", "") or ""),
                access_code=str(body.get("access_code", "") or ""),
                model_label=str(body.get("model", "") or ""),
                # 序列号决定遥测订阅主题 device/<序列号>/report；
                # 「自动搜索」的结果里带着它，前端必须一起回传。
                serial=str(body.get("serial", "") or ""),
                # 设备族（空 = 拓竹）。第三方族用 api_key + port，
                # 前端的标签与必填性来自 /api/families（与桌面版同一份注册表）。
                family=str(body.get("family", "") or ""),
                port=_as_port(body.get("port")),
                api_key=str(body.get("api_key", "") or ""),
                camera_url=str(body.get("camera_url", "") or ""),
                # 灯光控制：Klipper 机器上"舱灯"没有统一做法，让用户填命令
                light_on_gcode=str(body.get("light_on_gcode", "") or ""),
                light_off_gcode=str(body.get("light_off_gcode", "") or ""),
            )
        except Exception as exc:  # noqa: BLE001 - 添加失败要让界面看到原因
            result = {"ok": False, "detail": f"{type(exc).__name__}: {exc}"}
        ok = bool(result.get("ok"))
        self._send_bytes(
            json.dumps(result, ensure_ascii=False).encode("utf-8"),
            "application/json; charset=utf-8",
            HTTPStatus.OK if ok else HTTPStatus.BAD_REQUEST,
        )

    def _manage_printer(self) -> None:
        """设备管理：删除 / 重连 / 修改设置。``{"index":0,"action":"remove"}``

        桌面端有右键菜单能做这些事，网页端原先只能看不能管 —— 对平板用户尤其致命
        （安卓版没有桌面界面，网页就是唯一入口）。这里补齐。

        支持的动作：
        * ``remove``  删除该设备（同时从配置里移除）
        * ``reconnect`` 重连（相当于桌面端的「重新连接」）
        * ``update``  修改名称/访问代码（``name`` / ``access_code`` 可选）
        """
        if self.manage_printer_fn is None:
            self._send_bytes(
                json.dumps(
                    {"ok": False, "detail": "该运行方式不支持在网页上管理设备"},
                    ensure_ascii=False,
                ).encode("utf-8"),
                "application/json; charset=utf-8",
                HTTPStatus.NOT_IMPLEMENTED,
            )
            return
        body = self._read_json_body()
        if body is None:
            self._send_bytes(
                json.dumps({"ok": False, "detail": "请求格式不正确"}, ensure_ascii=False).encode(),
                "application/json; charset=utf-8",
                HTTPStatus.BAD_REQUEST,
            )
            return
        try:
            index = int(body.get("index", -1))
        except (TypeError, ValueError):
            index = -1
        action = str(body.get("action", "") or "")
        if action not in ("remove", "reconnect", "update"):
            self._send_bytes(
                json.dumps(
                    {"ok": False, "detail": f"未知操作：{action or '（空）'}"}, ensure_ascii=False
                ).encode("utf-8"),
                "application/json; charset=utf-8",
                HTTPStatus.BAD_REQUEST,
            )
            return
        try:
            result = self.manage_printer_fn(
                index=index,
                action=action,
                name=str(body.get("name", "") or ""),
                access_code=str(body.get("access_code", "") or ""),
                api_key=str(body.get("api_key", "") or ""),
                port=_as_port(body.get("port")),
                # 灯光命令：留空表示"不改"，所以只在传了非空值时才写
                light_on_gcode=str(body.get("light_on_gcode", "") or ""),
                light_off_gcode=str(body.get("light_off_gcode", "") or ""),
            )
        except Exception as exc:  # noqa: BLE001
            result = {"ok": False, "detail": f"{type(exc).__name__}: {exc}"}
        ok = bool(result.get("ok"))
        self._send_bytes(
            json.dumps(result, ensure_ascii=False).encode("utf-8"),
            "application/json; charset=utf-8",
            HTTPStatus.OK if ok else HTTPStatus.BAD_REQUEST,
        )

    def _settings(self) -> None:
        """读取/修改运行设置。

        * ``GET  /api/settings`` 返回当前设置（网页帧率、每路帧率、界面刷新、列数…）
        * ``POST /api/settings`` 部分更新（只改传来的字段）

        对应桌面端的「⚙ 设置」对话框。网页端原先无法调整，用户只能改配置文件再重启。
        """
        if self.get_settings_fn is None or self.update_settings_fn is None:
            self._send_bytes(
                json.dumps(
                    {"supported": False, "detail": "该运行方式不支持在网页上修改设置"},
                    ensure_ascii=False,
                ).encode("utf-8"),
                "application/json; charset=utf-8",
                HTTPStatus.NOT_IMPLEMENTED,
            )
            return
        if self.command == "GET":
            try:
                payload = self.get_settings_fn()
            except Exception as exc:  # noqa: BLE001
                payload = {"error": f"{type(exc).__name__}: {exc}"}
            self._send_bytes(
                json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                "application/json; charset=utf-8",
            )
            return
        body = self._read_json_body()
        if body is None:
            self._send_bytes(
                json.dumps({"ok": False, "detail": "请求格式不正确"}, ensure_ascii=False).encode(),
                "application/json; charset=utf-8",
                HTTPStatus.BAD_REQUEST,
            )
            return
        try:
            result = self.update_settings_fn(body)
        except Exception as exc:  # noqa: BLE001
            result = {"ok": False, "detail": f"{type(exc).__name__}: {exc}"}
        ok = bool(result.get("ok"))
        self._send_bytes(
            json.dumps(result, ensure_ascii=False).encode("utf-8"),
            "application/json; charset=utf-8",
            HTTPStatus.OK if ok else HTTPStatus.BAD_REQUEST,
        )

    def _config_export(self) -> None:
        """``POST /api/config/export`` ``{"passphrase": ""}`` → ``{ok, json, portable}``

        网页与安卓端导出配置。带口令时导出的文件**在任何版本都能导入**
        （Windows / Linux / Docker / 安卓都用同一套标准库算法，见
        `app/util/secret.py` 的 PortableCipher）。
        """
        if self.export_config_fn is None:
            self._send_bytes(
                json.dumps(
                    {"supported": False, "detail": "该运行方式不支持导出配置"},
                    ensure_ascii=False,
                ).encode("utf-8"),
                "application/json; charset=utf-8",
                HTTPStatus.NOT_IMPLEMENTED,
            )
            return
        body = self._read_json_body() or {}
        passphrase = str(body.get("passphrase", "") or "")
        try:
            result = self.export_config_fn(passphrase)
        except Exception as exc:  # noqa: BLE001
            result = {"ok": False, "detail": f"{type(exc).__name__}: {exc}"}
        ok = bool(result.get("ok"))
        self._send_bytes(
            json.dumps(result, ensure_ascii=False).encode("utf-8"),
            "application/json; charset=utf-8",
            HTTPStatus.OK if ok else HTTPStatus.BAD_REQUEST,
        )

    def _config_import(self) -> None:
        """``POST /api/config/import`` ``{"json": "...", "passphrase": ""}``

        导入会**停掉旧会话并按新配置重建**，所以界面上要提示"会重连"。
        """
        if self.import_config_fn is None:
            self._send_bytes(
                json.dumps(
                    {"supported": False, "detail": "该运行方式不支持导入配置"},
                    ensure_ascii=False,
                ).encode("utf-8"),
                "application/json; charset=utf-8",
                HTTPStatus.NOT_IMPLEMENTED,
            )
            return
        body = self._read_json_body()
        if body is None:
            self._send_bytes(
                json.dumps({"ok": False, "detail": "请求格式不正确"}, ensure_ascii=False).encode(),
                "application/json; charset=utf-8",
                HTTPStatus.BAD_REQUEST,
            )
            return
        text = body.get("json", "")
        if not isinstance(text, str):
            # 也接受直接传对象（前端有时会先把内容 parse 过）
            try:
                text = json.dumps(body.get("config", {}), ensure_ascii=False)
            except (TypeError, ValueError):
                text = ""
        passphrase = str(body.get("passphrase", "") or "")
        try:
            result = self.import_config_fn(text, passphrase)
        except Exception as exc:  # noqa: BLE001
            result = {"ok": False, "detail": f"{type(exc).__name__}: {exc}"}
        ok = bool(result.get("ok"))
        self._send_bytes(
            json.dumps(result, ensure_ascii=False).encode("utf-8"),
            "application/json; charset=utf-8",
            HTTPStatus.OK if ok else HTTPStatus.BAD_REQUEST,
        )

    def _diagnose(self, query: dict) -> None:
        """``GET /api/diagnose?index=N``：跑一遍只读诊断并返回报告。

        桌面端的「通道诊断」对话框走的是同一份实现
        （`app/bambu/diagnostics.py`），所以三处（桌面 / 网页 / 命令行）
        给出的结论必然一致。
        """
        if self.diagnose_fn is None:
            self._send_bytes(
                json.dumps(
                    {"supported": False, "detail": "该运行方式不支持在网页上做通道诊断"},
                    ensure_ascii=False,
                ).encode("utf-8"),
                "application/json; charset=utf-8",
                HTTPStatus.NOT_IMPLEMENTED,
            )
            return
        try:
            index = int((query.get("index", ["-1"])[0] or "-1"))
        except ValueError:
            self._send_bytes(
                json.dumps({"ok": False, "detail": "index 不合法"}, ensure_ascii=False).encode("utf-8"),
                "application/json; charset=utf-8",
                HTTPStatus.BAD_REQUEST,
            )
            return
        try:
            result = self.diagnose_fn(index)
        except Exception as exc:  # noqa: BLE001
            result = {"ok": False, "detail": f"{type(exc).__name__}: {exc}"}
        ok = bool(result.get("ok"))
        self._send_bytes(
            json.dumps(result, ensure_ascii=False).encode("utf-8"),
            "application/json; charset=utf-8",
            HTTPStatus.OK if ok else HTTPStatus.BAD_REQUEST,
        )

    def _layout(self) -> None:
        """``POST /api/layout``

        * ``{"index": 0, "span": 2}``：设置画面占几格（重点画面 2×2）；
        * ``{"index": 0, "action": "up"|"down"|"top"|"bottom"}``：调整画面顺序 ——
          桌面版右键菜单的「上移 / 下移 / 移到最前 / 移到最后」，网页端以前只能干看着。
        """
        if self.layout_fn is None:
            self._send_bytes(
                json.dumps(
                    {"supported": False, "detail": "该运行方式不支持在网页上调整画面"},
                    ensure_ascii=False,
                ).encode("utf-8"),
                "application/json; charset=utf-8",
                HTTPStatus.NOT_IMPLEMENTED,
            )
            return
        body = self._read_json_body()
        if body is None:
            self._send_bytes(
                json.dumps({"ok": False, "detail": "请求格式不正确"}, ensure_ascii=False).encode(),
                "application/json; charset=utf-8",
                HTTPStatus.BAD_REQUEST,
            )
            return
        try:
            index = int(body.get("index", -1))
        except (TypeError, ValueError):
            index = -1
        action = str(body.get("action", "") or "")
        try:
            if action:
                reorder = getattr(self.app, "reorder_fn", None)
                if reorder is None:
                    result = {"ok": False, "detail": "该运行方式不支持调整画面顺序"}
                else:
                    result = reorder(index=index, direction=action)
            else:
                span = int(body.get("span", 1))
                result = self.layout_fn(index, span)
        except (TypeError, ValueError):
            self._send_bytes(
                json.dumps({"ok": False, "detail": "参数不合法"}, ensure_ascii=False).encode(),
                "application/json; charset=utf-8",
                HTTPStatus.BAD_REQUEST,
            )
            return
        except Exception as exc:  # noqa: BLE001
            result = {"ok": False, "detail": f"{type(exc).__name__}: {exc}"}
        ok = bool(result.get("ok"))
        self._send_bytes(
            json.dumps(result, ensure_ascii=False).encode("utf-8"),
            "application/json; charset=utf-8",
            HTTPStatus.OK if ok else HTTPStatus.BAD_REQUEST,
        )

    def _read_raw_body(self) -> bytes:
        """把请求体读进来缓存（一次），供后续解析。

        上限 8 MB：网页端最大的请求体是「导入配置」，十来台设备也就几十 KB；
        给一个上限可以避免构造出来的畸形 Content-Length 把内存吃满。
        """
        if self._raw_body is not None:
            return self._raw_body
        try:
            length = int(self.headers.get("Content-Length", "0") or 0)
        except ValueError:
            length = 0
        length = max(0, min(length, 8 * 1024 * 1024))
        data = b""
        if length:
            try:
                data = self.rfile.read(length)
            except OSError:
                data = b""
        self._raw_body = data
        return data

    def _read_json_body(self) -> Optional[dict]:
        """读并解析请求体；格式不对返回 None（调用方回 400）。"""
        raw = self._read_raw_body()
        try:
            parsed = json.loads(raw.decode("utf-8") or "{}")
        except (json.JSONDecodeError, UnicodeDecodeError, OSError):
            return None
        return parsed if isinstance(parsed, dict) else None

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
        session = sessions[index]
        # ?cam=N 指定取哪一路（多摄像头机器）；不给就用该设备当前选中的那一路
        camera = self._camera_arg(session)
        self.app.cache.touch(index)
        _, jpeg = self.app.cache.frame(index, camera)
        if not jpeg:
            _, jpeg = camera_frame(session, camera)
            if not jpeg:
                self._send_bytes(b"no frame", "text/plain", HTTPStatus.SERVICE_UNAVAILABLE)
                return
        self._send_bytes(jpeg, "image/jpeg")

    def _camera_arg(self, session: PrinterSession) -> int:
        """读 ``?cam=N`` 并夹到**该设备真实存在**的那几路里。

        写坏的参数（字母、负数、超出范围）一律退回该设备当前选中的那一路 ——
        返回「没有画面」（503）会让人以为摄像头坏了，而实际只是前端传了个不存在的序号。
        拓竹那族没有 `cameras()`，所以任何非 0 的序号都会退回 0。
        """
        default = _selected_camera(session)
        query = parse_qs(urlparse(self.path).query)
        raw = (query.get("cam") or [""])[0]
        try:
            value = int(raw)
        except (TypeError, ValueError):
            return default
        known = {item["index"] for item in cameras_of(session)}
        if known:
            return value if value in known else default
        return 0

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
            session = self._sessions()[index]
            camera = self._camera_arg(session)
            self.app.cache.touch(index)
            seq, jpeg = self.app.cache.frame(index, camera)
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


def build_share_urls(port: int, token: str, sessions: list | None = None) -> list[str]:
    """算出"其它设备可以用哪些地址访问网页服务"（带令牌）。

    抽成模块级函数而不是 WebServer 的方法，是因为**有两个使用方**：
    `WebServer.urls()`（桌面/命令行的启动提示）与 `WebHost.info()`
    （网页里的「在其它设备上打开」）。两处算法必须一致 ——
    否则用户按界面给的地址打不开，而终端里那条却是对的。

    顺序有意义：本机回环在最前，接着是**打印机所在网段**的地址
    （手机通常和打印机连同一个 Wi-Fi，这个最可能可用），最后才是其它网卡。
    虚拟网卡/代理网卡的地址手机一般访问不到，除非它就是打印机所在网段。
    """
    query = f"?token={token}" if token else ""
    try:
        printer_prefixes = {
            str(session.info.ip).rsplit(".", 1)[0]
            for session in (sessions or [])
            if str(session.info.ip).count(".") == 3
        }
    except Exception:
        printer_prefixes = set()

    local = [f"http://127.0.0.1:{port}/{query}"]
    preferred: list[str] = []
    others: list[str] = []
    for iface in local_interfaces():
        if iface.ip.startswith("127.") or not iface.scannable:
            continue
        if not iface.sweepable and iface.prefix not in printer_prefixes:
            continue
        url = f"http://{iface.ip}:{port}/{query}"
        if iface.prefix in printer_prefixes:
            preferred.append(url)
        else:
            others.append(url)
    return local + preferred + others


class WebServer:
    """网页服务的生命周期管理。

    ``host`` 默认 ``0.0.0.0``：**监听所有网卡**（有意为之 —— 同网段的手机要能打开）。
    只想本机访问就传 ``127.0.0.1``。这条「会监听所有网卡」的说明同时写在
    `SECURITY.md`（威胁模型）与 `docs/DEPLOY.md`（部署）里，别只改一处。
    """

    def __init__(
        self,
        get_sessions: Callable[[], list[PrinterSession]],
        port: int = 8080,
        token: str = "",
        fps: float = 4.0,
        max_width: int = 720,
        host: str = "0.0.0.0",
        verbose: bool = False,
        discover_fn: Optional[Callable[[], list]] = None,
        add_printer_fn: Optional[Callable[..., dict]] = None,
        manage_printer_fn: Optional[Callable[..., dict]] = None,
        get_settings_fn: Optional[Callable[[], dict]] = None,
        update_settings_fn: Optional[Callable[[dict], dict]] = None,
        export_config_fn: Optional[Callable[..., dict]] = None,
        import_config_fn: Optional[Callable[..., dict]] = None,
        diagnose_fn: Optional[Callable[..., dict]] = None,
        layout_fn: Optional[Callable[..., dict]] = None,
        camera_action_fn: Optional[Callable[..., dict]] = None,
        reorder_fn: Optional[Callable[..., dict]] = None,
        sessions_action_fn: Optional[Callable[..., dict]] = None,
        info_fn: Optional[Callable[[int], dict]] = None,
    ) -> None:
        self.get_sessions = get_sessions
        self.port = port
        self.token = token
        self.fps = fps
        self.host = host
        self.verbose = verbose
        #: 可选的设备管理能力（各宿主注入，None 表示该运行方式不支持）。
        #: 把这些做成回调而不是写死在服务里，是为了：
        #:   1) 让服务端不依赖"谁在跑它"（安卓版与桌面版注入不同实现）；
        #:   2) 测试能注入假实现，不需要真的连打印机。
        self.discover_fn = discover_fn
        self.add_printer_fn = add_printer_fn
        self.manage_printer_fn = manage_printer_fn
        self.get_settings_fn = get_settings_fn
        self.update_settings_fn = update_settings_fn
        #: 配置备份（导出/导入）。网页与安卓端以前完全没有这个能力，
        #: 只能靠桌面版菜单 —— 平板上既备份不了、也搬不进去。
        self.export_config_fn = export_config_fn
        self.import_config_fn = import_config_fn
        #: 通道诊断（桌面端有对话框，网页/安卓端靠这两个回调补上）
        self.diagnose_fn = diagnose_fn
        #: 画面布局（重点画面 2×2）
        self.layout_fn = layout_fn
        #: 画面刷新与多路切换（桌面右键菜单有的能力，网页端也要有）
        self.camera_action_fn = camera_action_fn
        #: 画面顺序（上移/下移/置顶/置底）
        self.reorder_fn = reorder_fn
        #: 全部连接 / 全部断开
        self.sessions_action_fn = sessions_action_fn
        #: 连接信息（令牌 + 局域网地址）。参数是服务端口。
        self.info_fn = info_fn
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
        """返回可供其它设备访问的地址（带令牌）。"""
        return build_share_urls(self.port, self.token, self.get_sessions())

    def primary_url(self) -> str:
        urls = self.urls()
        return urls[1] if len(urls) > 1 else urls[0]
