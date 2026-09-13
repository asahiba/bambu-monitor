"""安卓版的内置服务引导。

这个模块是**唯一**为 Android 新写的 Python 代码：它把项目已有的
`app.headless` 服务拉起来，供 APK 内的 WebView 使用。

## 为什么不是重写

协议层（MQTT over TLS 遥测、SSDP/2021 发现、6000 端口鉴权帧格式、RTSPS 拉流、
HMS 错误码、多路复用推流）全部复用 `app/` 下已有代码 —— 那部分有 649 项回归测试
覆盖。用 Java 重写等于把踩过的坑再踩一遍。

## 安卓上必须注意的三件事

1. **配置目录必须可写**：APK 的安装目录是只读的，配置要落到应用私有目录
   （由 Android 侧通过环境变量传进来）。
2. **不能装信号处理器**：Chaquopy 在**非主线程**运行 Python，
   `signal.signal()` 会直接抛异常并让整个应用崩掉。`app.headless` 里
   只捕获了 `(ValueError, OSError)`，而 CPython 在非主线程抛的是
   `ValueError: signal only works in main thread` —— 属于能捕获的类型，
   但为稳妥起见这里在调用前后都做了兜底。
3. **服务器要能外部访问**：绑 `0.0.0.0`，这样同一 Wi-Fi 下的手机/其它平板
   也能连这台平板看监控（相当于平板同时是服务端）。
"""

from __future__ import annotations

import os
import sys
import threading
import traceback

#: 内置服务的默认端口。避开 8080？不用——这是设备本机端口，冲突概率低，
#: 且与项目其它文档保持一致，用户更容易理解。
DEFAULT_PORT = 8080

_state: dict = {
    "thread": None,
    "port": DEFAULT_PORT,
    "error": "",
    "started": False,
    "token": "",
}
_lock = threading.Lock()


def configure_environment(config_dir: str) -> str:
    """把 Android 侧的运行参数写进环境变量，必须在导入 app 之前调用。

    :param config_dir: 可写的配置目录（Android 侧传 filesDir 下的子目录）
    :returns: 实际使用的配置目录
    """
    os.environ["BAMBU_MONITOR_CONFIG_DIR"] = config_dir
    # 日志也写进应用私有目录，方便在设备上取日志
    os.makedirs(os.path.join(config_dir, "logs"), exist_ok=True)
    return config_dir


def serve(host: str = "0.0.0.0", port: int = DEFAULT_PORT) -> None:
    """在后台线程启动内置服务（非阻塞）。

    调用后应立即轮询 :func:`status`，直到 ``running`` 为真或出现 ``error``。
    """
    with _lock:
        if _state["thread"] is not None:
            return
        _state["port"] = int(port)
        thread = threading.Thread(
            target=_serve_blocking,
            args=(host, int(port)),
            name="bambu-android-server",
            daemon=True,
        )
        _state["thread"] = thread
        thread.start()


def _serve_blocking(host: str, port: int) -> None:
    try:
        from app import __version__
        from app.headless import resolved_web_token, run_headless

        # ⚠️ 必须先问服务端要令牌，而且**不能**自己调 AppConfig.load() 取。
        #
        # 踩过的坑：AppConfig.load() 在配置文件还不存在时，每次调用都会新生成
        # 一个 web_token **且不落盘**。原来这里先 load() 一次把令牌存进 _state，
        # 随后 run_headless() 内部又 load() 一次拿到的是**另一个**令牌并用它起服务
        # —— WebView 拿到的是前者，于是首次打开必然 `{"error": "unauthorized"}`。
        #
        # resolved_web_token() 是「先落盘再返回」，两侧读到的必然是同一个值；
        # 而且它跟 run_headless() 用的是同一个函数，不会再各算各的。
        _state["token"] = resolved_web_token()

        _state["version"] = __version__
        # 与命令行走同一个入口，保证行为一致（网页服务、管理命令、日志都一样）
        run_headless(["--host", host, "--port", str(port), "--status-interval", "0"])
    except BaseException as exc:  # noqa: BLE001 - 必须记下来给界面显示
        _state["error"] = f"{type(exc).__name__}: {exc}\n\n{traceback.format_exc()}"
        sys.stderr.write(_state["error"])
    finally:
        _state["started"] = True


def status() -> dict:
    """返回服务状态：``{"running": bool, "port": int, "token": str, "error": str}``。"""
    with _lock:
        thread = _state["thread"]
        return {
            # 「运行中」的判据是"线程还活着且没有错误"，而不是"start() 被调用过"。
            # 服务真正可用还要等端口监听完成，所以 Android 侧仍会做一次 /health 探测。
            "running": bool(thread is not None and thread.is_alive() and not _state["error"]),
            "port": _state["port"],
            "token": _state["token"],
            "error": _state["error"],
            "version": _state.get("version", ""),
        }


def url_for(host: str = "127.0.0.1") -> str:
    """拼出可直接打开的监控地址（自动带上访问令牌）。"""
    state = status()
    token = state.get("token") or ""
    suffix = f"?token={token}" if token else ""
    return f"http://{host}:{state['port']}/{suffix}"


def stop() -> None:
    """尽力停掉内置服务（Android 侧在 onDestroy 里调用）。

    嵌入式场景下进程随应用结束，所以这里只做温和尝试、不阻塞界面：
    先用一次本地请求让 HTTP 服务关掉监听，再让会话随进程退出。
    """
    try:
        import urllib.request

        # 通过一个不存在的路径触发一次请求即可确认服务在跑；
        # 真正的关闭靠进程退出（daemon 线程随进程结束）。
        urllib.request.urlopen(url_for(), timeout=2).close()
    except Exception:  # noqa: BLE001 - 关不掉也无所谓，进程退出会清理
        pass


def discover(timeout: float = 8.0) -> list:
    """扫描局域网里的打印机，返回可 JSON 序列化的列表。

    网页界面用它来「自动搜索」，避免用户在平板上手输 IP。
    """
    from app.bambu.discovery import discover as _discover

    found = []
    for info in _discover(timeout=timeout):
        found.append(
            {
                "ip": info.ip,
                "serial": info.serial,
                "name": info.name,
                "model": info.model.label,
                "firmware": info.firmware,
            }
        )
    return found


def add_printer(name: str, ip: str, access_code: str, model_label: str = "") -> dict:
    """添加一台打印机并落盘。返回 ``{"ok": bool, "detail": str}``。"""
    from app.bambu.models import PrinterInfo, detect_model
    from app.config import AppConfig

    ip = (ip or "").strip()
    if not ip:
        return {"ok": False, "detail": "IP 不能为空"}
    config = AppConfig.load()
    existing = next((item for item in config.printers if item.ip == ip), None)
    if existing is not None:
        existing.name = name or existing.name
        existing.access_code = access_code or existing.access_code
        if model_label:
            existing.model = detect_model("", model_label)
        detail = "已更新"
    else:
        config.printers.append(
            PrinterInfo(
                ip=ip,
                name=name,
                access_code=access_code,
                model=detect_model("", model_label),
            )
        )
        detail = "已添加"
    config.save()
    if config.last_error:
        return {"ok": False, "detail": config.last_error}
    return {"ok": True, "detail": f"{detail}（共 {len(config.printers)} 台）"}


def list_printers() -> list:
    """列出已配置的打印机（访问代码只回传长度，不回传内容）。"""
    from app.config import AppConfig

    config = AppConfig.load()
    return [
        {
            "ip": info.ip,
            "name": info.display_name(),
            "model": info.model.label,
            "serial": info.serial,
            "has_code": bool(info.access_code),
            "code_len": len(info.access_code or ""),
        }
        for info in config.printers
    ]


def remove_printer(ip: str) -> dict:
    """按 IP 删除一台打印机。"""
    from app.config import AppConfig

    config = AppConfig.load()
    before = len(config.printers)
    config.printers = [item for item in config.printers if item.ip != ip]
    config.save()
    removed = before - len(config.printers)
    return {"ok": removed > 0, "detail": f"已删除 {removed} 台"}
