"""单路监控画面：视频 + 下方状态条（进度 / 喷嘴温度 / 热床温度）。"""

from __future__ import annotations

import html
import os
import time
from typing import Any, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QAction, QFont, QGuiApplication
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ..bambu.printer import PrinterSession
from ..core.device import (
    DisplayStatus,
    camera_frame,
    camera_status_text,
    cameras_of,
    display_status,
)
from ..core.registry import credential_label, has_credential, resolve_family
from . import theme
from .frame_decoder import FrameDecoder
from .video_widget import VideoWidget

#: 信息行里优先并排显示的读数（按这个顺序挑，最多 :data:`_INLINE_DETAIL_LIMIT` 条）。
#:
#: 为什么只挑几条、其余进 tooltip：Klipper/Moonraker 这类设备实测能给出十几条读数
#: （风扇 / 断料与走料传感器 / 工具头板温度 / 耗材用量…，Voron 2.4 上是 18 条），
#: 全部铺进信息行会把卡片撑高、把画面挤小 —— 而画面才是这个界面的主角。
_INLINE_DETAIL_PRIORITY: tuple[str, ...] = (
    "耗材用量",  # 还剩多少料：换料/断料判断的第一步
    "已打印",  # 这一件打了多久
    "风扇",  # 风扇停了是堵料/过热的前兆
    "断料检测",  # 断料开关
    "走料检测",  # 走料传感器（瞬时值）
)

#: 信息行里最多并排几条这样的读数
_INLINE_DETAIL_LIMIT = 3


def session_details(session: Any) -> list[dict[str, str]]:
    """读会话的额外读数（``session.details()``），拿不到就返回空列表。

    * 拓竹那族（以及没有这个方法的旧会话）就是空列表 —— 此时界面**整块不显示**：
      不给空框、不留空行，信息行与卡片提示都与改造前完全一致；
    * 读数取不到（适配器内部异常）不能把整路画面带崩，所以这里兜住异常。
    """
    getter = getattr(session, "details", None)
    if not callable(getter):
        return []
    try:
        rows = getter()
    except Exception:  # noqa: BLE001 - 读数只是锦上添花，不能影响画面刷新
        return []
    if not isinstance(rows, list):
        return []
    clean: list[dict[str, str]] = []
    for row in rows:
        if not isinstance(row, dict) or not row.get("label"):
            continue
        clean.append({"label": str(row["label"]), "value": str(row.get("value", ""))})
    return clean


def inline_details(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    """从读数里挑出并排进信息行的几条（完整清单仍然在 tooltip 里）。"""
    picked: list[dict[str, str]] = []
    for keyword in _INLINE_DETAIL_PRIORITY:
        for row in rows:
            if row in picked or keyword not in row["label"]:
                continue
            picked.append(row)
            break
        if len(picked) >= _INLINE_DETAIL_LIMIT:
            break
    return picked


def selected_camera_index(session: Any) -> int:
    """这台设备当前选中的画面序号（``info.camera_index``），坏值一律退回 0。

    序号来自配置（用户可以手改），所以这里不假设它是合法整数：0 等于"第一路"，
    任何解析不出来的值都退回它 —— 与 ``app/web/server.py::_camera_arg`` 同一判据。
    """
    try:
        return max(0, int(getattr(session.info, "camera_index", 0) or 0))
    except (TypeError, ValueError):
        return 0


def camera_label(camera: dict) -> str:
    """菜单里那一路画面的文字：名字（+ 位置）+ 取不到画面时的标注。

    取不到画面（``available`` 为假）的那一路**仍然列出来**：用户就是要在它坏掉时
    切过去看原因，把入口藏掉等于让人没法排查。标注与网页端下拉的写法一致。
    """
    name = str(camera.get("name") or f"摄像头 {int(camera.get('index', 0)) + 1}")
    location = str(camera.get("location") or "")
    # 位置只在**确实多一层信息**时才附上：Voron 上 name/location 常见是
    # "Printer/printer" 这种同词大小写不同，拼出来是「Printer（printer）」——只是噪音
    text = f"{name}（{location}）" if location and location.lower() not in name.lower() else name
    return text if camera.get("available", True) else f"{text}（不可用）"


class _CameraAwareSource:
    """把「当前选中的那一路」喂给后台解码线程的会话外壳。

    为什么需要这一层：``FrameDecoder`` 调的是无参的 ``session.latest_frame()``，
    而多路画面的会话（Moonraker）**只有带序号调用**时才返回第 N 路 ——
    无参调用永远是第 0 路。于是「右键切换摄像头」若只改 ``info.camera_index``，
    画面不会有任何变化（切了等于没切）。这里把带序号的取帧转发进去。

    取帧时**每次重新读** ``info.camera_index``，所以切换后不必重建解码线程，
    下一帧就是新那一路（这正是"点一下立刻见效"的关键）。

    为什么不改 ``frame_decoder.py``：那边只该关心"把 JPEG 解码成 QImage"，
    不该知道这台设备有几路画面；而且拓竹那族的会话没有这个能力 ——
    ``camera_frame()`` 已经把这些差异兜住了（序号参数没人接受就退回无参调用）。
    """

    def __init__(self, session: Any) -> None:
        self._session = session

    @property
    def info(self) -> Any:
        # FrameDecoder 只用它给线程命名（decode-<ip>）
        return self._session.info

    def latest_frame(self) -> tuple[int, bytes]:
        return camera_frame(self._session, selected_camera_index(self._session))


class _Badge(QLabel):
    """状态徽标（打印中/空闲/离线…）。"""

    COLORS = {
        "打印中": theme.PRINTING,
        "准备中": theme.PRINTING,
        "已暂停": theme.WARN,
        "打印完成": theme.OK,
        "打印失败": theme.ERROR,
        "空闲": theme.TEXT_DIM,
        "离线": theme.ERROR,
        "未知": theme.TEXT_DIM,
    }

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setAlignment(Qt.AlignCenter)
        self.setMinimumWidth(58)
        self._text = ""

    def set_state(self, text: str) -> None:
        if text == self._text:
            return
        self._text = text
        color = self.COLORS.get(text, theme.TEXT_DIM)
        self.setText(text)
        self.setStyleSheet(
            f"color: {color}; border: 1px solid {color}; border-radius: 3px;"
            f" padding: 1px 6px; font-weight: bold; background: rgba(0,0,0,60);"
        )


class _ValueLabel(QLabel):
    """带标题的数值显示，例如 喷嘴 245.3/250.0 ℃。"""

    def __init__(self, caption: str, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._caption = caption
        self._current = "--"
        self._target = "--"
        self._text_cache = ""
        font = QFont(theme.FONT_FAMILY, 9)
        self.setFont(font)

    def set_values(self, current: Optional[float], target: Optional[float]) -> None:
        cur = "--" if current is None else f"{current:.0f}"
        tar = "--" if target is None else f"{target:.0f}"
        if cur == self._current and tar == self._target:
            return
        self._current, self._target = cur, tar
        html = (
            f"<span style='color:{theme.TEXT_DIM}'>{self._caption}</span>&nbsp;"
            f"<span style='color:{theme.ACCENT};font-weight:bold'>{cur}</span>"
            f"<span style='color:{theme.TEXT_DIM}'>/{tar}℃</span>"
        )
        self.setText(html)

    def clear(self) -> None:
        self.set_values(None, None)


class CameraTile(QFrame):
    """一路监控画面（视频 + 状态条）。"""

    request_single_view = Signal(object)
    request_edit = Signal(object)
    request_remove = Signal(object)
    request_reconnect = Signal(object)
    request_diagnose = Signal(object)
    request_layout = Signal(object, str)
    notify = Signal(str)

    def __init__(self, session: PrinterSession, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.session = session
        # ⚠️ 这两个字典必须在 _build_status_panel() **之前**建好：
        # 那里会把控制按钮的原始样式/提示记进来（见 _apply_control_block_hint）。
        self._control_button_style: dict = {}
        self._control_button_tip: dict = {}
        self.setFrameShape(QFrame.NoFrame)
        self.setStyleSheet(
            f"CameraTile {{ background: {theme.PANEL}; border: 1px solid {theme.BORDER};"
            f" border-radius: 6px; }}"
        )
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(1, 1, 1, 1)
        layout.setSpacing(0)

        self.video = VideoWidget(self)
        self.video.double_clicked.connect(lambda: self.request_single_view.emit(self))
        layout.addWidget(self.video, 1)

        layout.addWidget(self._build_status_panel())

        # 解码在后台线程完成（12 路 1080p 时这是性能关键）。
        # 注意传进去的是"按当前选中画面取帧"的外壳，不是会话本身 ——
        # 否则右键菜单切换到第 2 路时，画面还会一直显示第 1 路（见 _CameraAwareSource）。
        self.decoder = FrameDecoder(_CameraAwareSource(session), 640, 360)
        self.decoder.start()
        self._last_seq = -1
        self._last_state_text = ""
        self._text_cache: dict = {}
        self._progress_accent = ""
        self._control_block_reason: Optional[str] = None
        self._tooltip = ""
        self._filament_signature: object = None
        self._snapshot_dir = os.path.join(_pictures_dir(), "BambuMonitor")

    def shutdown(self) -> None:
        """从监控墙移除时停止后台解码线程。

        必须等一下线程真正退出：解码线程会在之后继续碰 ``QImage``，
        若它还在跑而 Qt 已经开始拆对象，进程会直接 fail-fast 消失
        （和诊断对话框那个崩溃是同一类问题，见 `app/ui/qt_threads.py`）。
        """
        self.decoder.stop()
        self.decoder.join(timeout=1.0)

    # ------------------------------------------------------------------ 状态条
    def _build_status_panel(self) -> QWidget:
        panel = QWidget(self)
        panel.setStyleSheet(
            f"background: {theme.PANEL_ALT}; border-bottom-left-radius: 5px;"
            f" border-bottom-right-radius: 5px;"
        )
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(9, 6, 9, 7)
        layout.setSpacing(4)

        # 第一行：状态 / 进度百分比 / 剩余时间
        top = QHBoxLayout()
        top.setSpacing(8)
        self.badge = _Badge()
        top.addWidget(self.badge)

        self.progress_label = QLabel("--%")
        progress_font = QFont(theme.FONT_FAMILY, 11)
        progress_font.setBold(True)
        self.progress_label.setFont(progress_font)
        self.progress_label.setStyleSheet(f"color: {theme.ACCENT};")
        top.addWidget(self.progress_label)

        self.task_label = QLabel("")
        self.task_label.setStyleSheet(f"color: {theme.TEXT_DIM};")
        self.task_label.setTextInteractionFlags(Qt.NoTextInteraction)
        top.addWidget(self.task_label, 1)

        self.hms_badge = QLabel("")
        self.hms_badge.setStyleSheet(
            f"color: {theme.ERROR}; border: 1px solid {theme.ERROR}; border-radius: 3px;"
            f" padding: 0 5px; font-weight: bold;"
        )
        self.hms_badge.setCursor(Qt.PointingHandCursor)
        self.hms_badge.setToolTip("点击查看 HMS 提示详情")
        self.hms_badge.mousePressEvent = self._open_hms  # type: ignore[assignment]
        self.hms_badge.setVisible(False)
        top.addWidget(self.hms_badge)

        self.remaining_label = QLabel("剩余 --")
        self.remaining_label.setStyleSheet(f"color: {theme.TEXT_DIM};")
        top.addWidget(self.remaining_label, 0, Qt.AlignRight)
        layout.addLayout(top)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setFixedHeight(10)
        layout.addWidget(self.progress_bar)

        # 第二行：喷嘴/热床/仓温/层数/WiFi/预计完成
        # 用**单个可换行的富文本标签**：一排控件的最小宽度是各项文本宽度之和，
        # 13 路时会把整个网格撑到窗口外（「画面跑到屏幕外」的根因）。
        self.info_label = QLabel("")
        self.info_label.setWordWrap(True)
        self.info_label.setTextFormat(Qt.RichText)
        self.info_label.setStyleSheet(f"color: {theme.TEXT_DIM};")
        self.info_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        layout.addWidget(self.info_label)

        # 第三行：耗材（AMS 各槽位 + 外挂料盘），同样是单个可换行标签
        self.filament_label = QLabel("")
        self.filament_label.setWordWrap(True)
        self.filament_label.setTextFormat(Qt.RichText)
        self.filament_label.setStyleSheet(f"color: {theme.TEXT_DIM};")
        self.filament_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.filament_label.setVisible(False)
        layout.addWidget(self.filament_label)

        # 第四行：控制按钮（窄画面只显示图标，宽画面自动带文字）
        controls = QHBoxLayout()
        controls.setSpacing(4)
        controls.addStretch(1)
        self.pause_button = QPushButton("⏸")
        self.pause_button.setToolTip("暂停打印（可恢复）")
        self.pause_button.clicked.connect(self._toggle_pause)
        self.stop_button = QPushButton("⏹")
        self.stop_button.setToolTip("停止打印（不可恢复，会二次确认）")
        self.stop_button.clicked.connect(self._stop_print)
        self.light_button = QPushButton("💡")
        self.light_button.setToolTip("开/关舱灯")
        self.light_button.clicked.connect(self._toggle_light)
        for button in (self.pause_button, self.stop_button, self.light_button):
            style = (
                f"QPushButton {{ background: {theme.PANEL}; border: 1px solid {theme.BORDER};"
                f" border-radius: 3px; padding: 1px 8px; font-size: 11px; }}"
                f"QPushButton:hover {{ border-color: {theme.ACCENT}; }}"
                f"QPushButton:disabled {{ color: #5a6a72; }}"
            )
            button.setStyleSheet(style)
            button.setCursor(Qt.PointingHandCursor)
            button.setMinimumWidth(34)
            # 记下"原始样式与提示"：被固件挡住时要在这基础上叠加，而不是整体替换。
            # （以前直接 setStyleSheet("color: …") 会把上面这套样式冲掉，
            #  第一次刷新后按钮就变回 Qt 默认外观了。）
            self._control_button_style[button] = style
            self._control_button_tip[button] = button.toolTip()
            controls.addWidget(button)
        layout.addLayout(controls)
        return panel

    # ------------------------------------------------------------------ 控制
    def _open_hms(self, event=None) -> None:  # noqa: ANN001
        status = display_status(self.session.snapshot())
        if not status.hms_items:
            return
        from .hms_dialog import HmsDialog

        HmsDialog(status.hms_items, self.session.info.display_name(), self).exec()

    def _toggle_pause(self) -> None:
        if self._show_blocked_reason("pause", "无法暂停 / 继续"):
            return
        status = display_status(self.session.snapshot())
        name = self.session.info.display_name()
        if status.is_paused:
            ok = self.session.resume_print()
            self.notify.emit(f"{name}：{'已发送继续指令' if ok else '发送失败（遥测未连接）'}")
        else:
            ok = self.session.pause_print()
            self.notify.emit(f"{name}：{'已发送暂停指令' if ok else '发送失败（遥测未连接）'}")

    def _stop_print(self) -> None:
        if self._show_blocked_reason("stop", "无法停止"):
            return
        name = self.session.info.display_name()
        answer = QMessageBox.question(
            self,
            "停止打印",
            f"确定要停止「{name}」当前的打印任务吗？\n\n停止后打印无法继续，需要重新开始打印。",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        ok = self.session.stop_print()
        self.notify.emit(f"{name}：{'已发送停止指令' if ok else '发送失败（遥测未连接）'}")

    def _toggle_light(self) -> None:
        if self._show_blocked_reason("light", "无法控制灯光"):
            return
        status = display_status(self.session.snapshot())
        name = self.session.info.display_name()
        target = not bool(status.light_on)
        ok = self.session.set_light(target)
        action = "开灯" if target else "关灯"
        self.notify.emit(f"{name}：{'已发送' + action + '指令' if ok else '发送失败（遥测未连接）'}")

    def _show_blocked_reason(self, command: str, title: str) -> bool:
        """命令被固件挡住时，把完整原因弹给用户；返回 True 表示已处理（调用方应直接返回）。

        ⚠️ 为什么不能只靠 tooltip + 置灰：**Qt 的禁用控件不接收鼠标事件，
        tooltip 根本不会弹出来**。于是用户看到一个灰色的「暂停」按钮，
        鼠标悬停没有任何提示、点也没有反应 —— 而这段文案正是"唯一的线索"。
        所以被挡住时按钮保持可点，点下去弹说明（里面有"先局域网、再开发者"
        的完整步骤与农场管家这条捷径）。
        """
        checker = getattr(self.session, "command_blocked", None)
        reason = checker(command) if callable(checker) else ""
        if not reason:
            return False
        name = self.session.info.display_name()
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Information)
        box.setWindowTitle(f"{title} · {name}")
        box.setText(f"「{name}」的这条命令被打印机固件拒绝了。")
        box.setInformativeText(reason)
        box.setStandardButtons(QMessageBox.Ok)
        box.exec()
        return True

    # ------------------------------------------------------------------ 刷新
    def refresh(self) -> None:
        seq, image = self.decoder.latest()
        if image is not None and seq != self._last_seq:
            self._last_seq = seq
            self.video.set_image(image)
            self.video.set_fps(self.session.camera_fps)

        info = self.session.info
        status = display_status(self.session.snapshot())
        self.video.set_title(f"{info.display_name()}", f"{info.ip} · {info.model.label}")

        state_text, color = self._connection_text(status)
        self.video.set_status(state_text, color)
        self._update_status_panel(status)

    def _connection_text(self, status: DisplayStatus) -> tuple[str, str]:
        """角标文案：与网页版共用 `app.core.camera_status_text` 的判据。

        完整说明（例如「RTSPS(322) 未取到画面…」）不再截断后塞进角标，而是
        走 `setToolTip`（见 `_update_status_panel`），角标只放稳定的短标签。
        """
        label, _detail = camera_status_text(
            camera_online=status.camera_online,
            mqtt_online=status.mqtt_online,
            camera_state=self.session.last_camera_state,
            camera_detail=self.session.last_camera_detail,
            mqtt_auth_error=self.session.mqtt_auth_error,
            # 凭据怎么叫、要不要必填都按设备族来：Moonraker 内网免鉴权，
            # 没填 API Key 是正常状态，不该显示成「未配置访问代码」
            has_access_code=has_credential(self.session.info),
            credential_label=credential_label(self.session.info),
            credential_required=bool(resolve_family(self.session.info).credential.required),
        )
        color = {
            "在线": theme.OK,
            "画面正常·遥测断开": theme.WARN,
            "访问代码错误": theme.ERROR,
            "未配置访问代码": theme.ERROR,
        }.get(label, theme.WARN)
        return label, color

    def _set_text(self, widget: QLabel, value: str) -> None:
        """只在文本变化时 setText：避免每 150ms 触发一次布局重算。"""
        if self._text_cache.get(widget) != value:
            self._text_cache[widget] = value
            widget.setText(value)

    def _update_status_panel(self, status: DisplayStatus) -> None:
        state = status.state_text if (status.mqtt_online or status.gcode_state) else "离线"
        if state != self._last_state_text:
            self._last_state_text = state
            self.badge.set_state(state)

        percent = max(0, min(100, status.progress))
        self._set_text(self.progress_label, f"{percent}%")
        if self.progress_bar.value() != percent:
            self.progress_bar.setValue(percent)

        accent = theme.PRINTING if status.is_printing else theme.TEXT_DIM
        if accent != self._progress_accent:
            self._progress_accent = accent
            self.progress_bar.setStyleSheet(
                f"QProgressBar {{ background: #101a1f; border: 1px solid {theme.BORDER};"
                f" border-radius: 5px; }}"
                f"QProgressBar::chunk {{ background: {accent}; border-radius: 4px; }}"
            )

        if status.mqtt_online or status.last_message_ts:
            self._set_text(self.remaining_label, f"剩余 {status.remaining_text}")
        else:
            self._set_text(self.remaining_label, "剩余 --")

        task = status.subtask_name or ""
        if task:
            task = os.path.splitext(os.path.basename(task))[0]
        self._set_text(self.task_label, task)

        self._update_info_row(status)
        self._update_filament_row(status)
        self._update_controls(status)

        if status.hms_items:
            self.hms_badge.setText(f"⚠ HMS×{len(status.hms_items)}")
            self.hms_badge.setVisible(True)
        else:
            self.hms_badge.setVisible(False)

        # 把通道提示（例如 RTSPS 回退、缺少依赖）与设备读数放到鼠标提示里
        self._refresh_tooltip(status)

    def _refresh_tooltip(self, status: DisplayStatus) -> None:
        """拼卡片 tooltip：通道告警 + 「设备能提供但不属于通用状态模型」的完整读数清单。

        ⚠️ 这个方法同时被 `_update_status_panel()` 与 `_update_info_row()` 调用：
        信息行是这两处唯一会变的东西，读数的并排显示在信息行里、完整清单在这里，
        两边必须同进同出（只刷新信息行却不刷新 tooltip，读数就会漏在 tooltip 外面）。
        重复调用是安全的：`self._tooltip` 做了缓存比较，内容没变就不会重设。
        """
        tips = list(self.session.warnings)
        if status.last_error:
            tips.append(status.last_error)
        if self.session.last_camera_detail:
            tips.append(f"视频通道：{self.session.video_backend}（{self.session.last_camera_detail}）")
        # 设备读数（风扇 / 传感器 / 耗材用量…）：十几条在信息行里放不下，逐条列在这里。
        # 空列表时**一行都不加**，tooltip 内容与改造前完全一致（拓竹那族就是这样）。
        for row in self._detail_rows(status):
            tips.append(f"{row['label']}：{row['value']}")
        tooltip = "\n".join(dict.fromkeys(tips)) if tips else ""
        if tooltip != self._tooltip:
            self._tooltip = tooltip
            self.setToolTip(tooltip)

    def _detail_rows(self, status: DisplayStatus) -> list[dict[str, str]]:
        """本设备可展示的额外读数；遥测没上来时返回空列表。

        离线时设备读数多半是上一次轮询留下的旧值，跟「离线」角标放在一起会误导，
        所以与信息行同一个判据：没有遥测就整块不显示。
        """
        if not (status.mqtt_online or status.last_message_ts):
            return []
        return session_details(self.session)

    def _update_info_row(self, status: DisplayStatus) -> None:
        """喷嘴/热床/仓温/层数/WiFi/预计完成/错误码，渲染成一个可换行的富文本标签。"""
        if not (status.mqtt_online or status.last_message_ts):
            self._set_text(self.info_label, "")
            self._refresh_tooltip(status)
            return
        accent = theme.ACCENT
        dim = theme.TEXT_DIM
        parts = [
            f"喷嘴 <b style='color:{accent}'>{status.nozzle_temper:.0f}</b>"
            f"<span style='color:{dim}'>/{status.nozzle_target_temper:.0f}℃</span>",
            f"热床 <b style='color:{accent}'>{status.bed_temper:.0f}</b>"
            f"<span style='color:{dim}'>/{status.bed_target_temper:.0f}℃</span>",
        ]
        if status.chamber_temper is not None and self.session.capabilities.has_chamber_sensor:
            parts.append(f"仓温 {status.chamber_temper:.0f}℃")
        if status.total_layer_num > 0:
            parts.append(f"层 {status.layer_num}/{status.total_layer_num}")
        if status.wifi_signal:
            bars = status.wifi_level
            parts.append(f"WiFi {'▮' * bars}{'▯' * (4 - bars)} {status.wifi_signal}")
        if status.finish_time_text != "--":
            parts.append(f"预计完成 {status.finish_time_text}")
        if status.print_error:
            label = f"E{status.print_error}"
            if status.print_error_text:
                label = f"{label} {status.print_error_text}"
            parts.append(f"<b style='color:{theme.ERROR}'>{label}</b>")
        # 该设备「能看但不进通用状态模型」的读数：只把最关键的 2-3 条并在这里
        # （完整清单进 tooltip，见 _refresh_tooltip）—— 十几条全铺开会把卡片撑高。
        for row in inline_details(self._detail_rows(status)):
            # 这个标签是 RichText：读数来自设备，必须转义，否则值里的 `<` 会被当标签解析
            parts.append(f"{html.escape(row['label'], quote=False)} {html.escape(row['value'], quote=False)}")
        self._set_text(self.info_label, "&nbsp;&nbsp;".join(parts))
        # tooltip 里带着这些读数的完整清单，信息行刷新时一并刷新（见 _refresh_tooltip 的说明）
        self._refresh_tooltip(status)

    def _update_filament_row(self, status: DisplayStatus) -> None:
        """耗材行：AMS 各槽位色块 + 类型 + 余量，以及外挂料盘（单个可换行标签）。"""
        entries = list(status.ams_trays)
        if status.vt_tray is not None and not status.vt_tray.empty:
            entries.append(status.vt_tray)
        if not entries:
            if self.filament_label.isVisible():
                self.filament_label.setVisible(False)
            self._set_text(self.filament_label, "")
            return

        active = status.active_tray
        chips = []
        for tray in entries:
            color = tray.color_hex or "#3a3a3a"
            is_active = active is not None and tray is active
            # 余量读不到时（非官方料卷 / 外挂）就不显示，避免误导
            text = f"{tray.label} {tray.tray_type or '--'}"
            remain = tray.remain_text
            if remain:
                text = f"{text} {remain}"
            square = f"<span style='color:{color}'>■</span>"
            if is_active:
                chips.append(f"<b style='color:{theme.ACCENT}'>{square} {text}</b>")
            else:
                chips.append(f"{square} {text}")
        self._set_text(self.filament_label, "耗材&nbsp;&nbsp;" + "&nbsp;&nbsp;".join(chips))
        if not self.filament_label.isVisible():
            self.filament_label.setVisible(True)
        self.filament_label.setToolTip(
            "\n".join(
                f"{tray.label}：{tray.tray_type or '未知'} {tray.color_hex or ''}，{tray.remain_hint}"
                f"（喷嘴 {tray.nozzle_temp_min}-{tray.nozzle_temp_max}℃）"
                for tray in entries
            )
        )

    def _apply_button_labels(self, paused: bool) -> None:
        """宽画面显示文字，窄画面只显示图标（避免把画面挤出屏幕）。"""
        wide = self.width() >= 380
        if wide:
            self.pause_button.setText("▶ 继续" if paused else "⏸ 暂停")
            self.stop_button.setText("⏹ 停止")
            self.light_button.setText("💡 关灯" if display_status(self.session.snapshot()).light_on else "💡 开灯")
        else:
            self.pause_button.setText("▶" if paused else "⏸")
            self.stop_button.setText("⏹")
            self.light_button.setText("💡")

    def _update_controls(self, status: DisplayStatus) -> None:
        """控制按钮的可用状态与文案。

        ⚠️ 按钮可用性要**按命令分别判断**：固件要求 MQTT 命令签名时，
        只有 `print` 段命令（暂停/停止/速度）会被忽略，而灯控走 `system` 段、
        不受影响 —— 实测确认 A2L 在需签名的状态下灯控仍然生效。
        以前用一刀切的 `can_control` 会把灯按钮也置灰，等于把能用的功能锁住。
        """
        online = self.session.can_control
        printing = status.is_printing
        paused = status.is_paused
        gcode = (status.gcode_state or "").upper()
        caps = self.session.capabilities

        def blocked(command: str) -> str:
            checker = getattr(self.session, "command_blocked", None)
            return checker(command) if callable(checker) else ""

        pause_block, stop_block, light_block = blocked("pause"), blocked("stop"), blocked("light")

        self.pause_button.setVisible(printing)
        # 被固件挡住时**保持可点**：Qt 的禁用控件不接收鼠标事件，tooltip 弹不出来，
        # 用户就永远看不到「怎么放行」的说明（点下去会弹完整原因，见 _show_blocked_reason）。
        # 视觉上仍然按"挡住的样式"呈现（见 _apply_control_block_hint）。
        self.pause_button.setEnabled(online and printing)
        self.stop_button.setVisible(printing and gcode != "FINISH")
        self.stop_button.setEnabled(online and printing)
        # 灯按钮按**设备是否真的上报了灯**决定显隐，而不是按机型猜。
        # 教训：A2L 是开放式机型，我据此推断它没有舱灯，实测却上报了
        # chamber_light —— 结果界面把按钮藏了，用户没法开关灯。
        # 遥测还没上来时（light_on 为 None）才退回机型能力作为兜底。
        has_light = status.light_on is not None or caps.can_control_light
        self.light_button.setVisible(has_light)
        self.light_button.setEnabled(has_light and online)
        self._apply_control_block_hint(pause_block or stop_block, light_block)
        self._apply_button_labels(paused)

    def _apply_control_block_hint(self, print_reason: str, light_reason: str) -> None:
        """控制被「固件要求命令签名」挡住时，把原因挂到对应按钮上。

        实测：新机型（H2C / H2S / X2D / P2S / A2L）的 `fun` 字段会置位命令签名要求，
        未开开发者模式时 `print` 段命令（暂停/停止/速度）会被固件静默忽略；
        **灯控不受影响**。按钮置灰却不说明原因会让人以为软件坏了，所以把原因挂上。

        注意这里是**叠加**而不是替换样式/提示：直接 ``setStyleSheet("color: …")``
        会把构造函数里那套按钮样式冲掉（第一次刷新后按钮就变回 Qt 默认外观）。
        提示里只放**一句话版**（``controls_blocked_short``），点下去弹完整步骤 ——
        完整文案有十几行，塞进 tooltip 反而看不清。
        """
        reason = print_reason or light_reason
        if reason == self._control_block_reason:
            return
        self._control_block_reason = reason
        short = getattr(self.session, "controls_blocked_short", "") or ""
        for button, blocked_reason in (
            (self.pause_button, print_reason),
            (self.stop_button, print_reason),
            (self.light_button, light_reason),
        ):
            base_style = self._control_button_style.get(button, "")
            base_tip = self._control_button_tip.get(button, "")
            if blocked_reason:
                button.setToolTip(f"{base_tip}\n\n⚠ {short or blocked_reason}\n（点一下看完整做法）")
                button.setStyleSheet(base_style + f"QPushButton {{ color: {theme.TEXT_DIM}; }}")
            else:
                button.setToolTip(base_tip)
                button.setStyleSheet(base_style)

    # ------------------------------------------------------------------ 交互
    def _build_context_menu(self) -> QMenu:
        """构造卡片右键菜单。

        抽成独立方法（而不是写在 ``contextMenuEvent`` 里）是为了能被直接调用：
        模拟一次真实的右键事件要构造 QContextMenuEvent，而菜单内容才是要验的东西。
        """
        menu = QMenu(self)
        menu.addAction("单画面 / 退出单画面", lambda: self.request_single_view.emit(self))
        menu.addAction("重新连接", lambda: self.request_reconnect.emit(self))
        menu.addAction("画面通道诊断…", lambda: self.request_diagnose.emit(self))
        menu.addAction("抓拍保存图片", self.save_snapshot)
        self._add_camera_actions(menu)
        menu.addSeparator()

        layout_menu = menu.addMenu("画面顺序与大小")
        focused = self.session.info.tile_span >= 2
        layout_menu.addAction(
            "取消重点画面" if focused else "设为重点画面（占 2×2）",
            lambda: self.request_layout.emit(self, "focus_off" if focused else "focus_on"),
        )
        layout_menu.addSeparator()
        layout_menu.addAction("上移", lambda: self.request_layout.emit(self, "up"))
        layout_menu.addAction("下移", lambda: self.request_layout.emit(self, "down"))
        layout_menu.addAction("移到最前", lambda: self.request_layout.emit(self, "top"))
        layout_menu.addAction("移到最后", lambda: self.request_layout.emit(self, "bottom"))
        layout_menu.addSeparator()
        layout_menu.addAction("打开布局窗口…", lambda: self.request_layout.emit(self, "dialog"))

        menu.addSeparator()
        menu.addAction("编辑打印机…", lambda: self.request_edit.emit(self))
        copy_action = QAction("复制 IP 地址", menu)
        copy_action.triggered.connect(lambda: self._copy_ip())
        menu.addAction(copy_action)
        menu.addSeparator()
        menu.addAction("从监控墙移除", lambda: self.request_remove.emit(self))
        return menu

    def _add_camera_actions(self, menu: QMenu) -> None:
        """画面相关的两条：「摄像头」子菜单 + 「🔄 刷新画面」。

        为什么只有一路时不建这个子菜单：永远只有一个选项的菜单只是噪音
        （与网页端"只有一路不显示下拉"同一判据），顺便保证拓竹那族
        （``cameras()`` 返回空列表、根本没有这个方法）的界面与改造前一致。
        """
        cameras = cameras_of(self.session)
        if len(cameras) > 1:
            current = selected_camera_index(self.session)
            submenu = menu.addMenu("摄像头")
            for camera in cameras:
                action = QAction(camera_label(camera), submenu)
                action.setCheckable(True)
                action.setChecked(int(camera["index"]) == current)
                # ⚠️ 必须用默认参数把序号绑进闭包：循环变量在 lambda 里是**延迟读取**的，
                # 不绑的话每一项点下去都会切到最后一那路（经典坑）。
                action.triggered.connect(
                    lambda _checked=False, target=int(camera["index"]): self._apply_camera(target)
                )
                submenu.addAction(action)
        # 「刷新画面」始终在：重新发现摄像头与切换哪一路是**两件事**，
        # 摄像头刚插上、或之前 502 的那一路刚起来时，任何设备都用得上。
        menu.addAction("🔄 刷新画面", self.refresh_camera_stream)

    def contextMenuEvent(self, event) -> None:  # noqa: N802
        self._build_context_menu().exec(event.globalPos())

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        size = self.video.size()
        if size.width() > 16 and size.height() > 16:
            self.decoder.set_target_size(size.width(), size.height())
            self.session.set_video_target_size(size.width(), size.height())
        self._apply_button_labels(display_status(self.session.snapshot()).is_paused)

    def _clear_frame(self) -> None:
        """丢掉当前这一帧（切换画面 / 刷新画面之后调用）。

        为什么必须清：旧的那一路会继续停在画面上，用户看到画面没变，
        只会以为"点了没反应"（新的那一路要等下一帧才到，通常几百毫秒）。
        ⚠️ 这里**不能**重置 ``self._last_seq``：``refresh()`` 会拿解码线程里
        **缓存的那张旧图**再贴一次，等于刚清掉又画回来。保持序号不动，
        新的帧一到（序号变了）自然就会贴上去。
        """
        self.video.clear_frame()

    def _apply_camera(self, index: int) -> None:
        """切到第 ``index`` 路画面（右下角这一路显示的内容）。

        为什么不发信号给主窗口：主窗口没有"换画面"这类可接的信号，而这次改动
        只碰本文件。这里只改内存里的选中值 + 立刻重取画面；**配置的落盘由既有的
        定时保存负责**（``info.camera_index`` 是配置里的字段，会被一并写走）。

        取帧那一侧不用管：解码线程拿的是 ``_CameraAwareSource``，
        它每次取帧都重新读 ``info.camera_index``，所以改完就是新那一路。
        """
        cameras = cameras_of(self.session)
        target = next((item for item in cameras if int(item["index"]) == int(index)), None)
        if target is None:
            # 这一路已经不存在了（例如摄像头被拔掉后重新识别过）：什么都不做
            return
        self.session.info.camera_index = int(target["index"])
        self._clear_frame()
        self.refresh()

        name = self.session.info.display_name()
        if target.get("available", True):
            self.notify.emit(f"{name}：已切换到「{camera_label(target)}」")
        else:
            # 切到坏掉的那一路**不是错误操作**（用户正是要看原因），所以别说成失败，
            # 但必须把设备给的原因直接说出来 —— 否则用户只看到一片黑
            detail = str(target.get("detail") or "设备没有给出原因")
            self.notify.emit(f"{name}：「{camera_label(target)}」现在取不到画面：{detail}")

    def refresh_camera_stream(self) -> None:
        """「🔄 刷新画面」：重新发现摄像头 → 丢掉旧帧 → 立刻重取一帧。

        场景：摄像头刚插上 / 刚在 crowsnest 里加了一路 / 之前 502 的那一路
        现在起来了。这类情况以前在界面里没有任何办法，只能整台设备重连。

        没有 ``refresh_cameras()`` 的会话（拓竹那族）直接跳过，不报错、不弹窗 ——
        提示走既有的 ``notify``（右上角气泡），不用模态对话框：
        刷新画面是随手一点的小操作，弹个必须点"确定"的框比不提示还烦。
        """
        name = self.session.info.display_name()
        refresher = getattr(self.session, "refresh_cameras", None)
        if not callable(refresher):
            self.notify.emit(f"{name}：这台设备不需要重新识别画面通道")
            return
        try:
            found = refresher()
        except Exception as exc:  # noqa: BLE001 - 刷新失败要说清原因，不能静默
            self.notify.emit(f"{name}：刷新画面失败（{exc}）")
            return
        self._clear_frame()
        self.refresh()
        count = len(found) if isinstance(found, list) else len(cameras_of(self.session))
        if count:
            self.notify.emit(f"{name}：已重新识别画面通道（{count} 路）")
        else:
            self.notify.emit(f"{name}：已请求重新取帧")

    def _copy_ip(self) -> None:
        clipboard = QGuiApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(self.session.info.ip)
            self.notify.emit(f"已复制 {self.session.info.ip}")

    def save_snapshot(self) -> None:
        # 抓拍的必须是**画面上显示的那一路**：多摄像头机器上抓错路，
        # 用户拿去对比时会以为机器出了别的毛病（网页端同一个接口也是这个判据）
        _, frame = camera_frame(self.session, selected_camera_index(self.session))
        # ⚠️ 判据是"没有字节"而不是"不是 None"：camera_frame() 把 None 归一成了 b""
        # （拓竹那族没有画面时就是这样），用 `is None` 判断会存下一个 0 字节的"照片"
        if not frame:
            self.notify.emit("当前没有画面可抓拍")
            return
        try:
            os.makedirs(self._snapshot_dir, exist_ok=True)
            stamp = time.strftime("%Y%m%d_%H%M%S")
            name = self.session.info.display_name().replace(" ", "_")
            path = os.path.join(self._snapshot_dir, f"{name}_{stamp}.jpg")
            with open(path, "wb") as handle:
                handle.write(frame)
            self.notify.emit(f"已保存抓拍：{path}")
        except OSError as exc:
            self.notify.emit(f"抓拍保存失败：{exc}")


def _pictures_dir() -> str:
    from PySide6.QtCore import QStandardPaths

    location = QStandardPaths.writableLocation(QStandardPaths.PicturesLocation)
    return location or os.path.expanduser("~")
