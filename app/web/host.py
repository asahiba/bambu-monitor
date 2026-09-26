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
import os
import threading
from typing import Any, Callable, Optional

from ..core.device import cameras_of

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
        family: str = "",
        port: int = 0,
        api_key: str = "",
        camera_url: str = "",
    ) -> dict:
        """添加或更新一台设备：写配置 + 落盘 + **立即建会话**。

        「立即建会话」很关键：否则用户加完设备要等服务重启才能看到画面，
        会以为没加上。

        ⚠️ ``serial`` 必须尽量传：**遥测依赖序列号订阅 `device/<序列号>/report`**。
        没有序列号时订阅会退化成通配 `device/#`，在真机密集的网络里容易收错报文，
        而且首次拿到报文前的状态一直是"离线"。网页端的「自动搜索」会把序列号带过来，
        手动添加时也可以从序列号栏填入。

        ``family`` 非空时按该族处理（默认拓竹）：以该族的 ``CredentialPolicy``
        决定凭据填在哪个字段（拓竹 ``access_code``、Moonraker ``api_key``），
        并校验是否必填 —— 界面上的标签与必填性都来自注册表，不写死。
        """
        from ..bambu.models import PrinterInfo, detect_model
        from ..core import create_session, is_registered, resolve_family
        from ..core.registry import FAMILY_BAMBU, get

        ip = (ip or "").strip()
        if not ip:
            return {"ok": False, "detail": "IP 地址不能为空"}
        family = (family or "").strip()
        if family and not is_registered(family):
            return {"ok": False, "detail": f"不支持的设备族：{family}"}
        descriptor = get(family or FAMILY_BAMBU)
        assert descriptor is not None  # is_registered 已经查过一次
        policy = descriptor.credential
        credential = api_key if policy.key == "api_key" else access_code
        if policy.required and not credential:
            return {"ok": False, "detail": f"{policy.label}不能为空"}
        self.stats["add"] += 1

        def apply_credential(target: PrinterInfo, value: str) -> None:
            """把凭据写进该族对应的字段（用户在网页上只填一个输入框）。"""
            if not value:
                return
            setattr(target, policy.key, value)

        with self._lock:
            existing_info = next((item for item in self.config.printers if item.ip == ip), None)
            # 记住改之前的族：换了族就必须换会话实现（拓竹 ↔ Moonraker），
            # restart() 只能在同一个实现里重连
            previous_family = resolve_family(existing_info).family if existing_info else ""
            if existing_info is not None:
                if name:
                    existing_info.name = name
                # 凭据写在**该族对应的字段**上（拓竹 access_code / 第三方 api_key）
                apply_credential(existing_info, credential)
                if model_label:
                    existing_info.model = detect_model("", model_label)
                if serial:
                    existing_info.serial = serial
                if family:
                    existing_info.family = family if family != FAMILY_BAMBU else ""
                if port > 0:
                    existing_info.port = int(port)
                if camera_url:
                    existing_info.camera_url = camera_url
                info = existing_info
            else:
                info = PrinterInfo(
                    ip=ip,
                    name=name,
                    serial=serial,
                    model=detect_model(serial, model_label) if model_label else detect_model(serial),
                    family=family if family and family != FAMILY_BAMBU else "",
                    port=int(port or 0),
                    camera_url=camera_url or "",
                )
                apply_credential(info, credential)
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
            if opened is not None and previous_family != descriptor.family:
                # 族换了：会话类型也跟着换，不能只 restart
                opened.stop()
                sessions.remove(opened)
                opened = None
            if opened is not None:
                setattr(opened.info, policy.key, getattr(info, policy.key, ""))
                opened.info.name = info.name
                if serial:
                    opened.info.serial = serial
                opened.restart()
                detail = "已更新并重连"
            else:
                session = create_session(info)
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

    # ------------------------------------------------------------------ 配置备份（跨版本）
    def export_config_text(self, passphrase: str = "") -> dict:
        """把当前配置导出成 JSON 文本（网页端「导出配置」用）。

        网页/安卓端以前**没有**任何导出入口（只有桌面版有菜单项），于是用户
        在平板上没法备份配置、也没法把电脑上的配置带过来。这里补上。

        :param passphrase: 非空时用口令保护访问代码 —— 这样的文件**在任何版本、
            任何平台**都能导入（Windows / Linux / Docker / 安卓）。
            留空则按本机方式加密，换机器/换用户后访问代码需要重填。
        """
        self.stats["export"] = self.stats.get("export", 0) + 1
        try:
            text = self.config.to_json(passphrase)
        except ValueError as exc:
            return {"ok": False, "detail": f"导出失败：{exc}"}
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("导出配置失败", exc_info=True)
            return {"ok": False, "detail": f"导出失败：{type(exc).__name__}: {exc}"}
        LOGGER.info("网页端导出配置（%d 台，%s）", len(self.config.printers), "带口令" if passphrase else "本机加密")
        return {
            "ok": True,
            "json": text,
            "portable": bool(passphrase),
            "printers": len(self.config.printers),
            "detail": "已生成配置内容（带口令，可在任何版本导入）" if passphrase
            else "已生成配置内容（未设口令：只有本机能恢复访问代码）",
        }

    def import_config_text(self, text: str, passphrase: str = "") -> dict:
        """用一段配置 JSON 覆盖当前配置，并**立刻重建会话**。

        `index` 顺序要与新配置一致，所以这里是「停掉全部旧会话 → 按新配置重建」，
        而不是增量合并 —— 导入的语义就是「用这份配置替换」。
        """
        import json
        import tempfile

        self.stats["import"] = self.stats.get("import", 0) + 1
        if not (text or "").strip():
            return {"ok": False, "detail": "没有配置内容"}
        # 先落成临时文件再走 import_from：那条路已经被测试与桌面版共用，
        # 支持口令加密、本机加密、明文三种文件，没必要在这里再写一遍解析
        handle = tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False, encoding="utf-8"
        )
        try:
            handle.write(text)
            handle.close()
            from ..config import AppConfig

            probe = AppConfig()
            if not probe.needs_passphrase(handle.name) and passphrase:
                # 文件本身不需要口令：忽略多传的口令，不要因此报错
                passphrase = ""
            if not probe.import_from(handle.name, passphrase):
                return {
                    "ok": False,
                    "detail": probe.last_error or "配置文件无法解析，或里面没有打印机",
                }
            warning = probe.warnings
            new_printers = list(probe.printers)
        except (OSError, json.JSONDecodeError) as exc:
            return {"ok": False, "detail": f"配置内容无法解析：{exc}"}
        finally:
            try:
                os.unlink(handle.name)
            except OSError:
                pass

        from ..core import create_session

        with self._lock:
            sessions = self._sessions()
            for session in list(sessions):
                try:
                    session.stop()
                except Exception:  # noqa: BLE001 - 停不掉也要继续（下面会清空列表）
                    LOGGER.debug("导入时停止会话失败", exc_info=True)
            sessions.clear()

            # 把导入的设置应用到当前 config 对象上（桌面版与网页版共用同一个实例）
            self.config.printers = new_printers
            for key in (
                "columns",
                "show_timestamp",
                "auto_connect",
                "last_timeout",
                "max_fps",
                "refresh_ms",
                "web_enabled",
                "web_port",
                "web_fps",
                "web_max_width",
            ):
                setattr(self.config, key, getattr(probe, key))
            self.config.save()
            if self.config.last_error:
                return {"ok": False, "detail": self.config.last_error}
            if not warning:
                warning = self.config.warnings or ""

            for info in self.config.printers:
                session = create_session(info)
                session.set_max_fps(float(getattr(self.config, "max_fps", 10.0) or 10.0))
                session.start()
                sessions.append(session)

        LOGGER.info("网页端导入配置：%d 台设备", len(sessions))
        self._notify_change()
        result = {"ok": True, "detail": f"已导入 {len(sessions)} 台设备", "printers": len(sessions)}
        if warning:
            result["warning"] = warning
        return result

    # ------------------------------------------------------------------ 画面
    def camera_action(self, index: int = -1, action: str = "refresh", camera: int = 0) -> dict:
        """画面的「刷新」与「切换哪一路」。

        桌面版右键菜单一直有「重新连接」，而网页端过去连"刷新一下画面"都做不到：
        摄像头刚插上、或者之前 502 的那一路刚起来时，用户没有任何办法让它重新试。

        * ``refresh`` —— 让会话重新发现摄像头并丢掉缓存帧（不重建会话，立刻生效）；
        * ``select``  —— 改看第 ``camera`` 路（多摄像头机器，例如 Voron 的喷嘴 + 舱内），
          写进配置，重启后还记得。

        ⚠️ 只碰画面，不下发任何会动机器的指令。
        """
        sessions = self._sessions()
        if not 0 <= index < len(sessions):
            return {"ok": False, "detail": f"没有第 {index + 1} 台设备（当前共 {len(sessions)} 台）"}
        session = sessions[index]
        if action == "select":
            info = getattr(session, "info", None)
            if info is None:
                return {"ok": False, "detail": "这台设备没有可切换的画面"}
            info.camera_index = max(0, int(camera))
            for item in self.config.printers:
                if item.ip == info.ip:
                    item.camera_index = info.camera_index
                    break
            self.config.save()
            self._notify_change()
            return {
                "ok": True,
                "detail": f"已切换到第 {info.camera_index + 1} 路画面",
                "cameras": cameras_of(session),
                "camera": info.camera_index,
            }
        if action == "refresh":
            refresher = getattr(session, "refresh_cameras", None)
            if callable(refresher):
                try:
                    refresher()
                except Exception as exc:  # noqa: BLE001 - 刷新失败要说清原因
                    LOGGER.warning("刷新摄像头失败：%s", exc)
                    return {"ok": False, "detail": f"刷新失败：{exc}"}
            self.stats["camera"] = self.stats.get("camera", 0) + 1
            self._notify_change()
            cameras = cameras_of(session)
            return {
                "ok": True,
                "detail": f"已重新识别画面通道（{len(cameras)} 路）" if cameras else "已请求重新取帧",
                "cameras": cameras,
                "camera": int(getattr(session.info, "camera_index", 0) or 0),
            }
        return {"ok": False, "detail": f"未知操作：{action}"}

    # ------------------------------------------------------------------ 整体连接/顺序
    def sessions_action(self, action: str = "") -> dict:
        """全部连接 / 全部断开（桌面工具栏那两个按钮）。

        网页端过去只能一台一台点重连 —— 设备一多就很烦，而"全部断开"在排查
        网络问题、或者不想让程序一直连着一堆机器时是有用的。

        ⚠️ 只增删监控连接本身，**不下发任何控制指令**（打印机照常打印）。
        """
        sessions = self._sessions()
        if action == "connect_all":
            for session in sessions:
                try:
                    session.start()
                except Exception:  # noqa: BLE001 - 一台起不来不该影响其它台
                    LOGGER.debug("启动会话失败：%s", getattr(session.info, "ip", "?"), exc_info=True)
            self._notify_change()
            return {"ok": True, "detail": f"已连接 {len(sessions)} 台"}
        if action == "disconnect_all":
            for session in sessions:
                try:
                    session.stop()
                except Exception:  # noqa: BLE001
                    LOGGER.debug("停止会话失败：%s", getattr(session.info, "ip", "?"), exc_info=True)
            self._notify_change()
            return {"ok": True, "detail": f"已断开 {len(sessions)} 台（打印机本身不受影响）"}
        return {"ok": False, "detail": f"未知操作：{action}"}

    def reorder(self, index: int = -1, direction: str = "") -> dict:
        """调整某台设备在监控墙上的位置（上移 / 下移 / 移到最前 / 移到最后）。

        桌面版右键菜单里有这四项，网页端以前只能改大小。顺序要**同时**改两处：
        当前会话列表（界面立刻变）与配置里的设备顺序（重启后还是这个顺序）。
        """
        sessions = self._sessions()
        if not 0 <= index < len(sessions):
            return {"ok": False, "detail": f"没有第 {index + 1} 台设备（当前共 {len(sessions)} 台）"}
        names = {"up": "上移", "down": "下移", "top": "移到最前", "bottom": "移到最后"}
        if direction not in names:
            return {"ok": False, "detail": f"未知方向：{direction}"}
        order = list(sessions)
        session = order.pop(index)
        target = {
            "up": max(0, index - 1),
            "down": min(len(order), index + 1),
            "top": 0,
            "bottom": len(order),
        }[direction]
        order.insert(target, session)
        # 就地改会话列表：宿主（headless / 桌面版）传给服务的都是同一个列表对象
        sessions[:] = order
        # 配置里的顺序同步过去（只按会话顺序排，配置里有而没会话的留在后面）。
        # ⚠️ 排序键里**不能**再调 `list.index()`：`list.sort()` 执行期间列表内部是
        # 空的，`.index()` 会抛 "is not in list"（真踩过）。所以先把位置算好。
        original_position = {id(item): position for position, item in enumerate(self.config.printers)}
        rank = {id(item.info): position for position, item in enumerate(order)}
        self.config.printers.sort(
            key=lambda item: (rank.get(id(item), len(rank)), original_position.get(id(item), 0))
        )
        self.config.save()
        self._notify_change()
        return {
            "ok": True,
            "detail": f"「{getattr(session.info, 'display_name', lambda: '?')()}」已{names[direction]}",
            "order": [getattr(item.info, "ip", "") for item in order],
        }

    # ------------------------------------------------------------------ 通道诊断
    def diagnose(self, index: int = -1) -> dict:
        """对某台设备跑一遍只读诊断，返回可显示的报告行。

        桌面端有「通道诊断」对话框，而**网页/安卓端原来没有** —— 平板上遇到
        「画面出不来」时只能干瞪眼（`tools/diagnose.py` 是电脑上跑的脚本）。
        这里复用 `app/bambu/diagnostics.py`（与 CLI、桌面对话框同一份实现），
        所以三处结论必然一致。

        这是**同步**调用：一轮约 10~20 秒（三个端口 + 取帧 + 遥测）。
        前端会给一个"诊断中"的提示；不做流式推送是因为报告行加起来才几十行，
        为它再加一条通道不值得。
        """
        sessions = self._sessions()
        if not 0 <= index < len(sessions):
            return {"ok": False, "detail": f"没有第 {index + 1} 台设备（当前共 {len(sessions)} 台）"}
        session = sessions[index]
        info = getattr(session, "info", None)
        ip = getattr(info, "ip", "")
        code = getattr(info, "access_code", "")
        if not ip:
            return {"ok": False, "detail": "这台设备没有记录 IP" }
        if not code:
            return {"ok": False, "detail": "这台设备没有访问代码：请先在「编辑」里填写"}

        from ..bambu import diagnostics

        self.stats["diagnose"] = self.stats.get("diagnose", 0) + 1
        report: list[dict] = []
        try:
            for event in diagnostics.run(
                ip,
                code,
                serial=getattr(info, "serial", "") or "",
                with_rtsp_frame=False,
            ):
                if isinstance(event, diagnostics.SectionStart):
                    report.append({"title": event.title, "lines": []})
                else:
                    # 正文事件总是跟在标题之后；万一没有标题就单起一节
                    if not report:
                        report.append({"title": "", "lines": []})
                    report[-1]["lines"].extend(event.lines)
        except Exception as exc:  # noqa: BLE001 - 诊断失败也要给用户一个交代
            LOGGER.warning("网页端诊断失败：%s", exc, exc_info=True)
            return {"ok": False, "detail": f"诊断失败：{type(exc).__name__}: {exc}"}

        # 结论行：与桌面版的收尾提示保持一致
        tail = [
            "若画面失败但 322 端口提示 401 Unauthorized，说明 RTSP 服务是活的，"
            "请确认打印机已开启「局域网实时画面」。"
        ]
        return {
            "ok": True,
            "name": getattr(info, "display_name", lambda: ip)(),
            "ip": ip,
            "sections": report,
            "tail": tail,
            "detail": f"已完成 {getattr(info, 'display_name', lambda: ip)()} 的通道诊断",
        }

    # ------------------------------------------------------------------ 画面布局
    def set_tile_span(self, index: int = -1, span: int = 1) -> dict:
        """设置某路画面占几格（1 = 普通，2 = 重点画面 2×2）。

        桌面端有「画面顺序与大小」菜单，网页端原来只能看不能改 ——
        平板上没法把关键那台放大。
        """
        sessions = self._sessions()
        if not 0 <= index < len(sessions):
            return {"ok": False, "detail": f"没有第 {index + 1} 台设备（当前共 {len(sessions)} 台）"}
        try:
            span = max(1, min(3, int(span)))
        except (TypeError, ValueError):
            return {"ok": False, "detail": "画面大小取值不合法"}
        session = sessions[index]
        info = getattr(session, "info", None)
        if info is None:
            return {"ok": False, "detail": "这台设备没有可修改的信息"}
        info.tile_span = span
        # 同步回配置里那一份（配置对象是发信源，重启后要保住）
        for item in self.config.printers:
            if item.ip == getattr(info, "ip", ""):
                item.tile_span = span
                break
        self.config.save()
        if self.config.last_error:
            return {"ok": False, "detail": self.config.last_error}
        self.stats["layout"] = self.stats.get("layout", 0) + 1
        self._notify_change()
        label = "重点画面（2×2）" if span > 1 else "普通大小"
        return {"ok": True, "detail": f"{getattr(info, 'display_name', lambda: '设备')()} 已设为{label}"}

    # ------------------------------------------------------------------ 管理
    def manage_printer(
        self,
        index: int = -1,
        action: str = "",
        name: str = "",
        access_code: str = "",
        api_key: str = "",
        port: int = 0,
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
            from ..core import resolve_family

            # 凭据字段按族定：拓竹填访问代码，第三方族填 API Key
            policy_key = resolve_family(session.info).credential.key
            credential = api_key if policy_key == "api_key" else access_code
            if name:
                session.info.name = name
            if credential:
                setattr(session.info, policy_key, credential)
            if port > 0:
                session.info.port = int(port)
            # 同步回配置里那一份（发信源是配置对象，界面读的是会话上的 info）
            for item in self.config.printers:
                if item.ip == ip:
                    item.name = session.info.name
                    if credential:
                        setattr(item, policy_key, credential)
                    if port > 0:
                        item.port = int(port)
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

    # ------------------------------------------------------------------ 连接信息
    def info(self, port: int = 0) -> dict:
        """给网页端「在其它设备上打开」用的连接信息。

        ## 为什么必须有这个

        安卓版的令牌**只在打开页面的那一刻出现在 URL 里**，随后就被
        前端 `history.replaceState` 抹掉了（免得用户截图/分享时带出去）。
        而安卓没有终端，启动时那行「带令牌的地址」用户根本看不到 ——
        结果就是**在平板本机能用，却拿不到令牌、没法在电脑上打开**。

        桌面版有「网页信息」对话框（`app/ui/web_dialog.py`），安卓版没有桌面
        界面，所以这份信息必须由网页自己提供。这里把地址算法交给
        `build_share_urls()`（与 `WebServer.urls()` 同一份实现，不会出现
        界面给的地址打不开、终端那条却是对的）。
        """
        from .server import build_share_urls

        self.stats["info"] = self.stats.get("info", 0) + 1
        token = str(getattr(self.config, "web_token", "") or "")
        port = int(port or 0)
        # 地址算不出来（网卡枚举失败等）**不能影响令牌**：令牌才是用户真正
        # 拿不到的东西，地址他不给也能自己看 IP。所以这里降级为空列表，
        # 让前端显示"没找到局域网地址"，而不是整个接口 500。
        try:
            urls = build_share_urls(port, token, self._sessions())
        except Exception:  # noqa: BLE001
            LOGGER.warning("枚举本机地址失败，只提供令牌", exc_info=True)
            urls = [f"http://127.0.0.1:{port}/{f'?token={token}' if token else ''}"]
        try:
            from .. import __version__ as version
        except Exception:  # noqa: BLE001
            version = ""
        return {
            "supported": True,
            "token": token,
            "port": port,
            # 第 0 条是 127.0.0.1，其余是局域网地址；分开给前端，便于分区展示
            "local_urls": [u for u in urls if "127.0.0.1" in u],
            "lan_urls": [u for u in urls if "127.0.0.1" not in u],
            "urls": urls,
            "version": version,
        }

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
