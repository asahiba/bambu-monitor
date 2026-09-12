"""网页服务的「宿主能力」适配层。

网页端现在能管理设备与改设置（见 `page.py` 里的对话框），而**真实实现**由宿主提供：
服务端只暴露回调（`discover_fn` / `add_printer_fn` / ...），谁在跑它谁负责接线。

这样做的好处：
* `app/web/server.py` 不必知道"谁在跑它"，也不必导入配置与协议层；
* 桌面版与无界面版可以共用这一份实现；
* 测试能注入假实现，不需要真打印机。

## 为什么网页端需要这些能力

桌面端有「自动搜索 / 添加打印机 / 画面布局 / ⚙ 设置」这些对话框。
而 **安卓版没有桌面界面，网页就是唯一入口** —— 没有这些能力，
平板用户根本没法把打印机加进来，装上也用不了。
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable, Optional

LOGGER = logging.getLogger("bambu-monitor.webhost")

#: 可在网页上修改的设置项及其取值范围（与 AppConfig 的钳制保持一致）
SETTINGS_SPEC: dict[str, tuple[type, float, float]] = {
    "max_fps": (float, 0.0, 30.0),
    "refresh_ms": (int, 50, 1000),
    "web_fps": (float, 0.5, 15.0),
    "web_max_width": (int, 240, 1920),
}

#: 这些设置改了要作用到现有会话（而不是只写进配置）
_SESSION_SETTINGS = {"max_fps"}


class WebHost:
    """把配置、协议层与网页服务接起来。

    :param config: `AppConfig` 实例
    :param sessions: 返回**当前会话列表**的可调用对象（列表本身可变，宿主直接往里加/删）
    :param config_path: 配置文件路径（用于界面提示）
    :param on_change: 设置变更后的回调（例如让宿主写盘），可选
    """

    def __init__(
        self,
        config: Any,
        sessions: Callable[[], list],
        *,
        on_change: Optional[Callable[[], None]] = None,
    ) -> None:
        self.config = config
        self._sessions = sessions
        self._on_change = on_change
        self._lock = threading.Lock()
        #: 清点使用过的次数，便于排查"网页上到底点没点"（日志里能看出来）
        self.stats = {"discover": 0, "add": 0, "manage": 0, "settings": 0}

    # ------------------------------------------------------------------ 发现
    def discover(self) -> list:
        """扫描局域网。用与桌面端相同的实现（SSDP + 2021 广播）。

        返回前再按「序列号或 IP」去一次重。`discovery.discover()` 自己已经去过重，
        这里再做一遍是**兜底**：网页端是安卓版唯一的入口，列表里出现重复项会让
        用户以为搜到了两台机器（改动前就是这个症状），代价只是几行判断。
        """
        from ..bambu.discovery import discover as _discover
        from ..bambu.discovery import merge_devices

        self.stats["discover"] += 1
        timeout = float(getattr(self.config, "last_timeout", 20.0) or 20.0)
        try:
            unique = merge_devices([], list(_discover(timeout=timeout)))
        except Exception:
            LOGGER.exception("自动搜索失败")
            raise
        known_ips = {getattr(item, "ip", "") for item in self._sessions()}
        found = []
        for info in unique:
            found.append(
                {
                    "ip": info.ip,
                    "serial": info.serial,
                    "name": info.name,
                    "model": info.model.label,
                    "firmware": info.firmware,
                    "known": info.ip in known_ips,
                }
            )
        LOGGER.info("网页端自动搜索：发现 %d 台", len(found))
        return found

    # ------------------------------------------------------------------ 添加
    def add_printer(
        self,
        name: str = "",
        ip: str = "",
        access_code: str = "",
        model_label: str = "",
        serial: str = "",
    ) -> dict:
        """添加或更新一台打印机：写配置 + 落盘 + **立即建会话**。

        「立即建会话」很关键：否则用户加完设备要等服务重启才能看到画面，
        会以为没加上。

        ⚠️ ``serial`` 必须尽量传：**遥测依赖序列号订阅 `device/<序列号>/report`**。
        没有序列号时订阅会退化成通配 `device/#`，在真机密集的网络里容易收错报文，
        而且首次拿到报文前的状态一直是"离线"。网页端的「自动搜索」会把序列号带过来，
        手动添加时也可以从序列号栏填入。
        """
        from ..bambu.models import PrinterInfo, detect_model
        from ..bambu.printer import PrinterSession

        ip = (ip or "").strip()
        if not ip:
            return {"ok": False, "detail": "IP 地址不能为空"}
        self.stats["add"] += 1

        with self._lock:
            existing_info = next((item for item in self.config.printers if item.ip == ip), None)
            if existing_info is not None:
                if name:
                    existing_info.name = name
                if access_code:
                    existing_info.access_code = access_code
                if model_label:
                    existing_info.model = detect_model("", model_label)
                if serial:
                    existing_info.serial = serial
                info = existing_info
            else:
                info = PrinterInfo(
                    ip=ip,
                    name=name,
                    serial=serial,
                    access_code=access_code,
                    model=detect_model(serial, model_label) if model_label else detect_model(serial),
                )
                self.config.printers.append(info)

            self.config.save()
            # ⚠️ 只有 `last_error`（写盘真的失败）才算失败。
            # `warnings` 是「凭据只能明文存」这类提示 —— 在安卓/Linux 上必然出现，
            # 和设备有没有加上毫无关系。曾经把 warnings 也当失败，于是安卓上
            # 设备已经加进去了、界面却报「添加失败当前系统没有 DPAPI」。
            if self.config.last_error:
                return {"ok": False, "detail": self.config.last_error}
            warning = getattr(self.config, "warnings", "") or ""

            # 已有会话就重启它（换了访问代码必须重连），没有就新建
            sessions = self._sessions()
            opened = next((s for s in sessions if getattr(s.info, "ip", "") == ip), None)
            if opened is not None:
                opened.info.access_code = info.access_code
                opened.info.name = info.name
                if serial:
                    opened.info.serial = serial
                opened.restart()
                detail = "已更新并重连"
            else:
                session = PrinterSession(info)
                session.set_max_fps(float(getattr(self.config, "max_fps", 10.0) or 10.0))
                session.start()
                sessions.append(session)
                detail = "已添加"

        LOGGER.info("网页端添加设备：%s（%s）", ip, detail)
        self._notify_change()
        total = len(self._sessions())
        result = {"ok": True, "detail": f"{detail}（共 {total} 台）"}
        if warning:
            # 明确告诉用户"加上了，但有件事要知道"，不要让提示看起来像失败
            result["warning"] = warning
        return result

    # ------------------------------------------------------------------ 管理
    def manage_printer(
        self, index: int = -1, action: str = "", name: str = "", access_code: str = ""
    ) -> dict:
        """重连 / 编辑 / 删除。索引基于当前会话列表（与网页端看到的一致）。"""
        sessions = self._sessions()
        if not 0 <= index < len(sessions):
            return {"ok": False, "detail": f"没有第 {index + 1} 台设备（当前共 {len(sessions)} 台）"}
        self.stats["manage"] += 1
        session = sessions[index]
        ip = getattr(session.info, "ip", "")
        LOGGER.info("网页端管理设备：%s action=%s", ip, action)

        if action == "reconnect":
            session.restart()
            return {"ok": True, "detail": "正在重连…"}

        if action == "update":
            if name:
                session.info.name = name
            if access_code:
                session.info.access_code = access_code
            # 同步回配置里那一份（发信源是配置对象，界面读的是会话上的 info）
            for item in self.config.printers:
                if item.ip == ip:
                    item.name = session.info.name
                    if access_code:
                        item.access_code = access_code
                    break
            self.config.save()
            # 同 add_printer：只认 last_error，warnings 不阻断
            if self.config.last_error:
                return {"ok": False, "detail": self.config.last_error}
            # 改了访问代码要重连才生效
            session.restart()
            self._notify_change()
            result = {"ok": True, "detail": "已保存并重连"}
            warning = getattr(self.config, "warnings", "") or ""
            if warning:
                result["warning"] = warning
            return result

        if action == "remove":
            session.stop()
            sessions.pop(index)
            self.config.printers = [item for item in self.config.printers if item.ip != ip]
            self.config.save()
            self._notify_change()
            return {"ok": True, "detail": "已删除"}

        return {"ok": False, "detail": f"未知操作：{action}"}

    # ------------------------------------------------------------------ 设置
    def get_settings(self) -> dict:
        """当前设置（网页端「⚙ 设置」对话框据此渲染）。"""
        self.stats["settings"] += 1
        return {
            "supported": True,
            "max_fps": float(getattr(self.config, "max_fps", 10.0) or 0.0),
            "refresh_ms": int(getattr(self.config, "refresh_ms", 150) or 150),
            "web_fps": float(getattr(self.config, "web_fps", 4.0) or 4.0),
            "web_max_width": int(getattr(self.config, "web_max_width", 720) or 720),
            "columns": int(getattr(self.config, "columns", 0) or 0),
        }

    def update_settings(self, changes: dict) -> dict:
        """部分更新设置：只改传来的字段，并按 SLEPTINGS_SPEC 钳制范围。

        改完立即生效：``max_fps`` 作用到每个会话，``web_fps`` / ``web_max_width``
        作用到网页转码缓存（由宿主通过 on_change 通知网页服务刷新）。
        """
        if not isinstance(changes, dict):
            return {"ok": False, "detail": "参数格式不正确"}
        applied = []
        for key, value in changes.items():
            spec = SETTINGS_SPEC.get(key)
            if spec is None:
                continue
            caster, low, high = spec
            try:
                number = caster(value)
            except (TypeError, ValueError):
                return {"ok": False, "detail": f"{key} 的值不合法：{value!r}"}
            number = max(low, min(high, number))
            setattr(self.config, key, number)
            applied.append(key)
        if not applied:
            return {"ok": False, "detail": "没有可修改的字段"}

        if _SESSION_SETTINGS & set(applied):
            fps = float(getattr(self.config, "max_fps", 10.0) or 0.0)
            for session in self._sessions():
                try:
                    session.set_max_fps(fps)
                except Exception:  # noqa: BLE001 - 某一路失败不影响其它路
                    LOGGER.debug("给会话应用 max_fps 失败", exc_info=True)

        self.config.save()
        if self.config.last_error:
            return {"ok": False, "detail": self.config.last_error}
        LOGGER.info("网页端修改设置：%s", ", ".join(applied))
        self.stats["settings"] += 1
        self._notify_change()
        return {"ok": True, "detail": "设置已保存（" + "、".join(applied) + "）"}

    # ------------------------------------------------------------------ 内部
    def _notify_change(self) -> None:
        if self._on_change is None:
            return
        try:
            self._on_change()
        except Exception:  # noqa: BLE001 - 通知失败不能影响接口返回
            LOGGER.debug("设置变更回调失败", exc_info=True)
