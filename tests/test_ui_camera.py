"""契约：多路画面的「切换看哪一路」与「刷新画面」在两处界面上都要有入口。

## 为什么需要

服务端已经补齐了多路画面能力（``app/web/host.py::WebCameraAction`` → 见
``tests/test_web_camera.py``）：

* ``GET /api/printers`` 每台设备多了 ``cameras``（``[{index, name, location,
  available, detail, url, stream_url}…]``）与 ``camera``（当前显示的是哪一路）；
* ``POST /api/camera {"action":"select","camera":N}`` 换一路并写进配置；
* ``POST /api/camera {"action":"refresh"}`` 重新发现摄像头 + 丢掉缓存帧；
* ``GET /api/frame/<i>?cam=N`` 取指定那一路。

但界面侧过去**一个入口都没有**：多摄像头机器（Voron 2.4 实测是喷嘴 + 舱内两路）
在网页/桌面上都换不了路；摄像头刚插上、或之前 502 的那一路刚起来时，用户也没有
任何办法让它重新试一次（桌面版只有"整台设备重连"这种粗办法）。

本文件锁住两侧的界面契约：

* **网页/安卓**（``app/web/page.py``）：卡片上多一个「摄像头」下拉（只有多路时才
  显示）与「🔄 刷新画面」按钮，POST 的字段、失败原因提示、以及"刷新后立刻换一个
  地址再取一帧"都在这里静态守住；
* **桌面**（``app/ui/tile.py``）：右键菜单多一个「摄像头」子菜单（当前那一路打勾）
  与「🔄 刷新画面」，并用假会话真实调用一遍菜单项。

## 边界

全部离线：网页侧只做静态文本契约（不启动浏览器、不启动服务），桌面侧用假会话
（不连设备、不发请求）。真实浏览器 / 安卓 WebView 的观感（原生下拉在触屏上的手感、
切换后多久出新画面）与真机上「切到喷嘴那一路」是否真的换了图，只能人工看 ——
这是这项改动的已知盲区。
"""

from __future__ import annotations

import importlib.util
import os
import re
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402

pytestmark = pytest.mark.usefixtures("no_network")

#: Voron 2.4 真机（crowsnest 两路）实测的清单形状：一路正常、一路取不到画面。
#: 第二路特意做成 ``available=False`` 且带着 detail —— 「坏掉的那一路仍然能选，
#: 但要点明原因」正是这次要验的边界之一。
_CAMERAS = [
    {
        "index": 0,
        "name": "Printer",
        "location": "printer",
        "available": True,
        "detail": "",
        "url": "http://192.168.1.50/webcam/?action=snapshot",
        "stream_url": "",
    },
    {
        "index": 1,
        "name": "nozzle",
        "location": "nozzle",
        "available": False,
        "detail": "HTTP 502（crowsnest 里这一路没起来）",
        "url": "http://192.168.1.50/webcam2/?action=snapshot",
        "stream_url": "",
    },
]

#: 两路画面用**不同的字节**，这样才能断言"取到的确实是第二路"
JPEG_A = b"\xff\xd8\xff\xe0" + b"A" * 64 + b"\xff\xd9"
JPEG_B = b"\xff\xd8\xff\xe0" + b"B" * 64 + b"\xff\xd9"


# ======================================================================= 桌面侧
# `CameraTile` 需要 PySide6，而 CI 刻意不装它（体积上百 MB）。模块级的
# `from PySide6... import` 在无 Qt 环境里是**收集期错误**（整个文件报错，不是跳过），
# 所以这里的导入一律放到夹具/函数体内，用 importorskip 干净地跳过。


class _FakeCameraSession:
    """够 ``CameraTile`` 用的假会话：多路画面、完全离线。

    只有 ``cameras()`` / ``latest_frame(camera)`` / ``refresh_cameras()`` 是这次要验的
    接口，其余成员只是让 ``_update_status_panel()`` 能顺利走完。
    ``latest_frame()`` 返回**不同的字节**：这样"切到第二路"这件事能被断言出来
    （解码线程拿到的是哪一路的帧，一眼可辨）。
    """

    def __init__(self, cameras=None) -> None:
        from app.bambu.models import PrinterInfo, PrinterModel
        from app.core.capabilities import DeviceCapabilities
        from app.core.device import DeviceStatus

        self.info = PrinterInfo(
            ip="127.0.0.1",  # 保留地址，且这里根本不会去连
            name="假机 Voron",
            serial="FAKE000001",
            model=PrinterModel.P1S,
            access_code="00000000",
        )
        self.info.camera_index = 0
        self.warnings: list[str] = []
        self.last_camera_state = "stopped"
        self.last_camera_detail = ""
        self.mqtt_auth_error = False
        self.video_backend = "-"
        self.capabilities = DeviceCapabilities()
        self._cameras = [dict(item) for item in (_CAMERAS if cameras is None else cameras)]
        self.refresh_count = 0
        self._status = DeviceStatus()
        self._status.stale_seconds = 3600.0  # 假会话不会真的刷新时间戳
        self._status.apply_mapped(
            {
                "job_state": "printing",
                "progress_percent": 42,
                "remaining_minutes": 37,
                "nozzle_temper": 245.0,
                "nozzle_target_temper": 250.0,
                "bed_temper": 60.0,
                "bed_target_temper": 60.0,
            }
        )

    # ------------------------------------------------------------ 会话接口
    def cameras(self) -> list[dict]:
        return [dict(item) for item in self._cameras]

    def refresh_cameras(self) -> list[dict]:
        self.refresh_count += 1
        return self.cameras()

    def latest_frame(self, camera: int = 0):
        return (int(camera) + 1, JPEG_B if int(camera) == 1 else JPEG_A)

    def snapshot(self):
        return self._status

    def set_video_target_size(self, width: int, height: int) -> None:
        """`resizeEvent` 会调它（真会话里是给画面定制解码分辨率）。"""
        return None

    @property
    def camera_fps(self) -> float:
        return 0.0

    @property
    def can_control(self) -> bool:
        return False


@pytest.fixture()
def tile_factory():
    """造 ``CameraTile``；用完停掉解码线程。

    ⚠️ 必须 ``shutdown()``：解码线程会在之后继续碰 ``QImage``，若它还在跑而 Qt
    已经开始拆对象，进程会直接 fail-fast 消失（见 ``app/ui/tile.py::shutdown``）。
    """
    pytest.importorskip("PySide6.QtWidgets", reason="界面测试需要 PySide6（CI 不装它）")
    from PySide6.QtWidgets import QApplication

    from app.ui.tile import CameraTile

    QApplication.instance() or QApplication([])
    created: list = []

    def build(session=None):  # noqa: ANN001, ANN202
        tile = CameraTile(session if session is not None else _FakeCameraSession())
        created.append(tile)
        return tile

    yield build
    for tile in created:
        tile.shutdown()
        tile.deleteLater()


def _camera_submenu(menu):  # noqa: ANN001 - Qt 类型在 importorskip 之后才知道
    """从右键菜单里找出「摄像头」子菜单（没有就返回 None）。"""
    for action in menu.actions():
        submenu = action.menu()
        if submenu is not None and action.text() == "摄像头":
            return submenu
    return None


def _action_by_text(menu, text: str):  # noqa: ANN001
    """按文字找菜单项（找不到直接失败，顺带守住文案）。"""
    for action in menu.actions():
        if action.text() == text:
            return action
    raise AssertionError(f"菜单里没有「{text}」，实际有：{[a.text() for a in menu.actions()]}")


def _notices(tile) -> list[str]:  # noqa: ANN001
    """把 ``notify`` 的提示收下来（界面上是右上角气泡，测里只关心文字）。"""
    messages: list[str] = []
    tile.notify.connect(messages.append)
    return messages


def test_桌面_多路画面才有摄像头子菜单并勾选当前那一路(tile_factory):
    """契约：只有多路时才建「摄像头」子菜单，当前显示的那一路打勾。"""
    tile = tile_factory()
    menu = tile._build_context_menu()
    submenu = _camera_submenu(menu)
    assert submenu is not None, "多路画面的设备在右键菜单里必须能切换"

    actions = submenu.actions()
    assert len(actions) == 2, "两路画面就该有两项"
    # 名字来自设备数据（不写死"摄像头 1/2"）
    assert actions[0].text().startswith("Printer"), actions[0].text()
    # 取不到画面的那一路**仍然列出来**（用户正是要看原因），但必须标注
    assert "nozzle" in actions[1].text() and "不可用" in actions[1].text(), actions[1].text()

    assert all(action.isCheckable() for action in actions), "每一项都要能打勾（当前那一路）"
    assert actions[0].isChecked(), "默认看第 0 路 → 第一项应当打勾"
    assert not actions[1].isChecked()


def test_桌面_只有一路或没有这个能力时不建子菜单(tile_factory):
    """契约：单路设备（拓竹那族根本没有 ``cameras()``）不出现「摄像头」菜单。

    永远只有一个选项的菜单只是噪音，还会让用户以为点错了什么；
    更重要的是：**拓竹那族的界面必须与改造前完全一致**。
    """
    single = tile_factory(_FakeCameraSession(cameras=_CAMERAS[:1]))
    assert _camera_submenu(single._build_context_menu()) is None, "只有一路时不该有这个子菜单"

    bambu_like = tile_factory()
    bambu_like.session.cameras = None  # type: ignore[method-assign]  # 拓竹那族没有这个方法
    menu = bambu_like._build_context_menu()
    assert _camera_submenu(menu) is None, "没有 cameras() 的会话不该出现这个子菜单"
    # 「刷新画面」仍然在（重新识别通道对任何设备都有意义）
    assert _action_by_text(menu, "🔄 刷新画面") is not None


def test_桌面_点第二路会改选中序号并真的去取那一路的帧(tile_factory):
    """契约：点一下菜单项 → ``info.camera_index`` 改掉，且**解码线程拿到的是新那一路**。

    ⚠️ 后半句才是重点：``FrameDecoder`` 调的是无参的 ``latest_frame()``，
    多路会话的无参调用永远是第 0 路。只改 ``info.camera_index`` 而不把序号
    转发到取帧那一侧的话，画面根本不会变（"切了等于没切"）。
    """
    tile = tile_factory()
    notices = _notices(tile)
    submenu = _camera_submenu(tile._build_context_menu())
    submenu.actions()[1].trigger()

    assert tile.session.info.camera_index == 1, "选中的画面序号没有改"
    # 解码线程的取帧源必须跟着走（它就是画面上显示的那一路）
    seq, frame = tile.decoder.session.latest_frame()
    assert frame == JPEG_B, f"解码线程仍然在取第 0 路（seq={seq}）"

    # 坏掉的那一路切过去不是"失败"，但要把设备给的原因说出来，否则用户只看到一片黑
    assert notices, "切换后没有任何提示"
    assert "nozzle" in notices[-1] and "502" in notices[-1], notices[-1]


def test_桌面_刷新画面调用一次refresh_cameras并清掉旧帧(tile_factory):
    """契约：「🔄 刷新画面」→ 调 ``session.refresh_cameras()`` 一次 + 清掉当前帧。

    清帧是必须的：旧画面继续停着，用户会以为按钮没生效。
    """
    tile = tile_factory()
    notices = _notices(tile)
    tile.video.set_image(_jpeg_image())
    assert tile.video.has_frame()

    _action_by_text(tile._build_context_menu(), "🔄 刷新画面").trigger()

    assert tile.session.refresh_count == 1, "refresh_cameras 应当正好被调用一次"
    assert not tile.video.has_frame(), "刷新后应当先清掉旧帧，等新的一帧"
    assert notices and "2 路" in notices[-1], notices


def test_桌面_没有refresh_cameras的会话点刷新不报错(tile_factory):
    """契约：拓竹那族没有 ``refresh_cameras()`` → 跳过并提示，不得抛异常。"""
    tile = tile_factory()
    tile.session.refresh_cameras = None  # type: ignore[method-assign]
    notices = _notices(tile)

    _action_by_text(tile._build_context_menu(), "🔄 刷新画面").trigger()
    assert notices, "跳过时也要说一句，否则用户以为按钮坏了"


def _jpeg_image():  # noqa: ANN202
    """一张 2×2 的假画面（只用于断言"有没有帧"，不解码真实 JPEG）。"""
    from PySide6.QtGui import QImage

    return QImage(2, 2, QImage.Format_RGB32)


# ======================================================================= 网页侧
# 网页端是**单文件内嵌 HTML+JS**，没有构建、没有类型检查：少读一个字段、把设备数据
# 拼进 innerHTML、或者下拉永远显示着，都不会有任何报错。本文件因此只锁前端代码的
# 形状，不启动浏览器。


def _page_html() -> str:
    from app.web.page import INDEX_HTML

    return INDEX_HTML


def _script() -> str:
    """取出内嵌的 JS（页面里只有一个 `<script>`，且没有 src）。"""
    html = _page_html()
    return html.split("<script>", 1)[-1].rsplit("</script>", 1)[0]


def _block(start: str, end: str) -> str:
    """截取脚本里 [start, end) 之间的片段，把断言限定在某个函数内。

    内嵌 JS 里同一个字符串会出现在多处（例如 ``img.src``），不断言范围很容易
    「测到了别的地方」却以为过了。
    """
    script = _script()
    begin = script.index(start)
    return script[begin : script.index(end, begin)]


def _cameras_block() -> str:
    """卡片上的摄像头下拉 + 原因提示的渲染。"""
    return _block("function renderCameras(", "function applyStatus(")


def _frame_block() -> str:
    """静态帧地址与"刷新后立刻重取一帧"。"""
    return _block("function frameUrl(", "function ensureTile(")


def _action_block() -> str:
    """`POST /api/camera` 的调用处。"""
    return _block("async function sendCameraAction(", "function showHms(")


def _tile_block() -> str:
    """卡片模板与事件绑定。"""
    return _block("function ensureTile(", "/* ---------------------------------------------------------------- 指令 */")


def test_网页卡片有摄像头下拉与刷新按钮的id():
    """契约：两个控件的 id 是唯一的（每张卡片一份），改名会静默失效。"""
    script = _script()
    assert re.search(r'<select class="cam-select" id="cam-select-\$\{index\}"', script), (
        "卡片模板里没有「摄像头」下拉（id 必须按设备序号唯一）"
    )
    assert re.search(r'<button class="cam-refresh" id="cam-refresh-\$\{index\}"', script), (
        "卡片模板里没有「🔄 刷新画面」按钮"
    )
    assert "🔄 刷新画面" in script, "按钮文字必须能让人一眼看出是干什么的"
    # 默认隐藏：只有真有多路画面的设备才显示这个下拉（见下一条用例）
    assert re.search(r'id="cam-select-\$\{index\}"[^>]*style="display:none"', script, re.S), (
        "下拉默认必须是隐藏的"
    )


def test_网页只有一路时不显示摄像头下拉():
    """契约：``cameras`` 只有一路（拓竹那族是空列表）时**不显示**下拉。

    永远只有一个选项的下拉既不解决问题，又要占掉状态条上宝贵的一行。
    """
    block = _cameras_block()
    assert "Array.isArray(info.cameras)" in block, "老服务端没有 cameras 键时要当空处理"
    assert re.search(r"if \(cameras\.length <= 1\)\{\s*\n\s*select\.style\.display = 'none'", block), (
        "只有一路时必须把下拉藏起来，不留一个没用的控件"
    )
    assert re.search(r"select\.style\.display = ''", block), "多路时必须把下拉显示出来"


def test_网页不可用的那一路仍可选并标注原因():
    """契约：``available`` 为假的那一路**仍然能选到**，但名字里要标注、画面上要写原因。

    为什么不能把坏掉的那一路从下拉里剔除：用户正是要在它坏掉时切过去看原因
    （也是确认"是这一路坏了，不是软件坏了"的唯一办法）。
    """
    block = _cameras_block()
    assert "available === false" in block, "没有处理「这一路取不到画面」的情况"
    assert "（不可用）" in block, "坏掉的那一路没有在名字里标注"
    assert "note.textContent" in block, "没有把 detail 里的原因写到画面上"
    assert "selected.detail" in block, "原因必须来自设备回传的 detail"
    # 原因提示也要有 DOM 位置（卡片模板里那块盖在画面上的说明）
    assert re.search(r'<div class="camnote" style="display:none"></div>', _script()), (
        "卡片模板里没有放原因提示的位置"
    )


def test_网页摄像头名字只用textContent与createElement渲染():
    """契约：画面名与 detail 都是设备/配置数据，**不得**拼 innerHTML。

    与 `renderDetails` 是同一条硬要求（那里已有先例）：拼 innerHTML 等于把设备
    返回的字符串当代码渲染 —— 网页端没有构建、没有类型检查，错了也不会有人发现。
    """
    block = _cameras_block()
    assert "createElement('option')" in block, "下拉选项应当用 createElement 建"
    assert "option.textContent" in block, "选项文字应当用 textContent 写"
    assert "select.appendChild(option)" in block
    for forbidden in ("innerHTML", "insertAdjacentHTML", "outerHTML", "document.write"):
        assert forbidden not in block, f"摄像头渲染里出现了 {forbidden}（注入风险）"


def test_网页切换与刷新POST的action与body字段():
    """契约：两个控件都必须走 ``POST /api/camera``，字段与服务端约定一致。

    * ``{"index": <设备序号>, "action": "refresh"}``
    * ``{"index": <设备序号>, "action": "select", "camera": <画面序号>}``
    """
    block = _action_block()
    assert "postJson('/api/camera', payload)" in block, "没有走 /api/camera"
    assert re.search(r"const payload = \{index: index, action: action\}", block), (
        "请求体里必须带设备序号与 action"
    )
    assert re.search(r"if \(action === 'select'\) payload\.camera = camera", block), (
        "select 必须带 camera 字段；refresh 不带（服务端按当前选中的那一路取帧）"
    )
    # 失败原因必须显示出来：那是用户手上唯一的线索（例如那一路返回 502）
    assert "toast(data.detail ||" in block, "失败时也要把 detail 说出来"

    # 两个控件真的接上了（下拉用 change，按钮用 click）
    tile_block = _tile_block()
    assert "sendCameraAction(index, 'select', parseInt(camSelect.value, 10) || 0)" in tile_block, (
        "下拉的 change 没有接到切画面上"
    )
    assert "sendCameraAction(index, 'refresh')" in tile_block, "刷新按钮没有接上"


def test_网页刷新后立刻换一个地址再取一帧():
    """契约：切换/刷新之后 `<img>` 要**立刻**带新的 cam + 时间戳重新取一帧。

    地址一模一样时浏览器认为源没变、继续显示旧画面 —— 用户就会以为按钮没生效，
    所以时间戳是必须的（不是优化，是功能）。
    """
    block = _frame_block()
    assert "'?cam=' + cam" in block, "静态帧地址没有带上要看哪一路"
    assert "'&t=' + frameStamp" in block and "Date.now()" in block, (
        "静态帧地址没有防缓存的时间戳"
    )
    # 时间戳只取 Date.now() 不够：同一毫秒内连点两下会得到同一个值，这次刷新就被吞了
    assert "frameStamp = Math.max(Date.now(), frameStamp + 1)" in block, (
        "时间戳必须是只增不减的，否则同一毫秒内的第二次刷新不会重新取帧"
    )
    assert "tile.img.src = frameUrl(index, tile.camera)" in block, (
        "刷新后没有用它重新取一帧"
    )
    # 以服务端认下来的那一路为准：静态帧地址才不会和实际显示的对不上
    assert "tile.camera = data.camera" in _action_block(), "没有用服务端回传的 camera 校准"

    script = _script()
    # 兼容模式（逐路 MJPEG）启动时、以及画面出错重试时，同样要带上当前那一路，
    # 否则多路机器会悄悄跳回第 0 路
    assert "tile.img.src = frameUrl(index, tile.camera)" in script
    assert "img.src = frameUrl(index, cam)" in script


def test_网页实时模式不去改img源():
    """契约：实时模式（``/api/live`` 单连接多路复用）下画面由服务端推，别去改 `<img src>`。

    改 src 等于又另开一条 MJPEG 长连接，把"同域最多 6 条连接"的坑重新踩回来；
    切换后服务端下一轮就推新那一路，这里只需要把旧画面盖住。
    """
    block = _frame_block()
    live_branch = re.search(r"if \(state\.live && !state\.fallback\)\{(.*?)\n  \}", block, re.S)
    assert live_branch, "没有区分实时模式与兼容模式"
    assert ".src" not in live_branch.group(1), "实时模式下不该改 <img src>"
    assert "nosignal" in live_branch.group(1), "实时模式下应当先把旧画面盖住"


def test_网页点摄像头控件不会触发整卡全屏():
    """契约：卡片上的点击是「切换全屏」，点下拉/刷新按钮必须拦住这次冒泡。

    否则用户一拉下拉，整张卡就全屏了（与「详情」那块是同一个手感事故）。
    """
    block = _tile_block()
    for marker in ("camSelect.addEventListener('click'", "camRefresh.addEventListener('click'"):
        assert marker in block, f"缺少 {marker} 的冒泡拦截"
        tail = block.split(marker, 1)[1][:160]
        assert "stopPropagation" in tail, f"{marker} 的处理里没有 stopPropagation"


def test_内嵌JS语法可被esprima解析():
    """契约：内嵌 JS 必须语法正确（HTML 里的特殊字符最容易把脚本写坏）。

    ⚠️ 新加的控件用到了模板字符串 ``${index}``：写错一处，整个页面的脚本都不会执行
    （画面全黑、状态全是"--"），而 Python 这边一点报错都没有。
    """
    if importlib.util.find_spec("esprima") is None:  # pragma: no cover
        pytest.skip("未安装 esprima，跳过内嵌 JS 语法解析")

    import esprima

    esprima.parseScript(_script())  # 语法错会直接抛


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-v"]))
