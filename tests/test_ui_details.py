"""契约：设备「能提供、但过去没显示」的读数（``session.details()``）必须显示出来。

## 为什么需要

接入 Klipper/Moonraker（真机 Voron 2.4）后，会话多了一条通路：

* ``session.details()`` 返回 ``[{"label": "风扇 fan", "value": "100%"}, …]``；
  Moonraker 适配器实现它（``app/adapters/moonraker/adapter.py::parse_details``），
  拓竹那族（``app/core/adapter.py::PollingDeviceSession.details``）是**空列表**；
* ``GET /api/printers`` 的**每台**设备上多了 ``details`` 字段（同结构）。

这些读数（风扇转速、断料/走料传感器、工具头板温度、耗材用量、主机负载…）取到了却
没有任何界面显示 —— 用户看到的就是「这软件没有这些数据」。本文件锁住两处界面的
显示契约：

* **桌面**（``app/ui/tile.py``）：信息行里并排最关键的 2-3 条，完整清单进卡片 tooltip。
  十几条读数全铺进信息行会把卡片撑高、把画面挤小，所以**条数是有上限的**；
* **网页/安卓**（``app/web/page.py``）：每张卡片一个**默认收起**的「详情」块，
  ``label: value`` 逐条列出，空列表时整块隐藏、刷新时不重置展开状态。

## 边界

全部离线，且**不打任何标记**去要求网络：

* 桌面侧用假会话（不连设备、不发请求、不起真实解码工作）；
* 网页侧只做静态文本契约（不启动浏览器、不启动服务）。

真实浏览器 / 安卓 WebView 里的观感（展开动画、窄屏折行是否好看、触屏点击是否顺手）
只能人工看，本文件测不到 —— 这是这项改动的已知盲区。
"""

from __future__ import annotations

import importlib.util
import os
import re
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402

pytestmark = pytest.mark.usefixtures("no_network")

#: Moonraker 真机（Voron 2.4）实测的读数样例：18 条，正是「会把卡片撑爆」的那一组。
#: 取自任务里给的真实数据（见 ``app/adapters/moonraker/adapter.py::parse_details``）。
SAMPLE_ROWS: list[dict[str, str]] = [
    {"label": "已打印", "value": "1小时07分"},
    {"label": "累计时长", "value": "1小时12分"},
    {"label": "耗材用量", "value": "3.86 m"},
    {"label": "文件位置", "value": "6.6 / 28.5 MB"},
    {"label": "喷嘴加热", "value": "15%"},
    {"label": "压力提前", "value": "0.037"},
    {"label": "热床加热", "value": "8%"},
    {"label": "当前位置", "value": "X 168.4  Y 133.3  Z 6.6"},
    {"label": "归零状态", "value": "XYZ 已归零"},
    {"label": "倍率", "value": "速度 100% · 流量 100%"},
    {"label": "温度 EBBCan", "value": "42.4℃"},
    {"label": "风扇 fan", "value": "100%"},
    {"label": "风扇 hotend_fan", "value": "100%"},
    {"label": "风扇 fan0", "value": "70%"},
    {"label": "风扇 fan1", "value": "70%"},
    {"label": "风扇 fan2", "value": "100%"},
    {"label": "走料检测 转堵监测", "value": "未检测到（瞬时值）"},
    {"label": "断料检测 断料监测", "value": "有料"},
]

#: 信息行里最多并排几条读数（与 ``app/ui/tile.py::_INLINE_DETAIL_LIMIT`` 同一约定）。
#: 这里写死数字是**故意**的：这个上限就是这项改动的契约 —— 它保证卡片不会被撑高。
INLINE_LIMIT = 3


# ======================================================================= 桌面侧
# `CameraTile` 需要 PySide6，而 CI 刻意不装它（体积上百 MB）。模块级的
# `from PySide6... import` 在无 Qt 环境里是**收集期错误**（整个文件报错，不是跳过），
# 所以这里的导入一律放到夹具/函数体内，用 importorskip 干净地跳过。


class _FakeSession:
    """够 ``CameraTile`` 用的假会话：完全离线（不连设备、不发请求、无网络）。

    只有 ``details()`` / ``snapshot()`` 是这次要验的接口，其余成员只是让
    ``_update_status_panel()`` 能顺利走完；画面侧一律返回「没有新帧」，
    所以后台解码线程不会做任何事。
    """

    def __init__(self, rows=(), *, online: bool = True) -> None:
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
        self.warnings: list[str] = []
        self.last_camera_state = "stopped"
        self.last_camera_detail = ""
        self.mqtt_auth_error = False
        self.video_backend = "-"
        self.capabilities = DeviceCapabilities()
        self._rows = [dict(row) for row in rows]
        self._status = DeviceStatus()
        self._status.stale_seconds = 3600.0  # 假会话不会真的刷新时间戳
        if online:
            # 走真实的合并入口造状态：它顺便把 mqtt_online / last_message_ts 置好，
            # 而「没有遥测就不显示读数」正是要验的判据之一
            self._status.apply_mapped(
                {
                    "job_state": "printing",
                    "progress_percent": 42,
                    "remaining_minutes": 37,
                    "layer_current": 12,
                    "layer_total": 30,
                    "nozzle_temper": 245.0,
                    "nozzle_target_temper": 250.0,
                    "bed_temper": 60.0,
                    "bed_target_temper": 60.0,
                    "wifi_rssi_dbm": -58.0,
                }
            )

    # ------------------------------------------------------------ 会话接口
    def details(self) -> list[dict[str, str]]:
        return [dict(row) for row in self._rows]

    def snapshot(self):
        return self._status

    # -------------------------------------------------- 画面侧（不产生任何连接）
    def latest_frame(self):
        return 0, None

    def set_video_target_size(self, width: int, height: int) -> None:
        """`resizeEvent` 会调它（真会话里是给画面定制解码分辨率）。"""
        return None

    @property
    def camera_fps(self) -> float:
        return 0.0

    @property
    def can_control(self) -> bool:
        return False


def _refresh(tile) -> None:  # noqa: ANN001 - 夹具给的类型在 importorskip 之后才知道
    """跑一遍会变的那部分：信息行与卡片 tooltip 都在 ``_update_info_row`` 里刷新。"""
    from app.core.device import display_status

    tile._update_info_row(display_status(tile.session.snapshot()))


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

    def build(rows=(), *, session=None):  # noqa: ANN001, ANN202
        tile = CameraTile(session if session is not None else _FakeSession(rows))
        created.append(tile)
        return tile

    yield build
    for tile in created:
        tile.shutdown()
        tile.deleteLater()


def _inline_rows(tile) -> list[dict[str, str]]:  # noqa: ANN001
    """信息行里**实际并排**显示的那几条读数。

    判据是「label value」连在一起出现（``app/ui/tile.py`` 就是这么拼的），
    再按出现位置排序 —— 这样多显示一条、少显示一条都能被发现。
    """
    text = tile.info_label.text()
    found = []
    for row in SAMPLE_ROWS:
        needle = f"{row['label']} {row['value']}"
        position = text.find(needle)
        if position >= 0:
            found.append((position, row))
    return [row for _, row in sorted(found, key=lambda item: item[0])]


def test_桌面_tooltip_列出全部读数(tile_factory):
    """契约：完整清单进卡片 tooltip —— 信息行放不下的那些读数不能就此丢掉。"""
    tile = tile_factory(SAMPLE_ROWS)
    _refresh(tile)

    tooltip = tile.toolTip()
    assert tooltip, "有读数时 tooltip 不该是空的"
    for row in SAMPLE_ROWS:
        assert f"{row['label']}：{row['value']}" in tooltip, f"tooltip 里少了读数：{row}"
    # 18 条读数一条不少（去重逻辑不能顺手吃掉重复值：风扇那几条值都是 100%）
    assert len([line for line in tooltip.splitlines() if "：" in line]) >= len(SAMPLE_ROWS)


def test_桌面_信息行只并排最关键的几条(tile_factory):
    """契约：信息行最多并排 2-3 条读数（挑最影响判断的），其余只在 tooltip 里。

    为什么死盯这个上限：18 条读数全铺进信息行会把卡片撑高、把画面挤小 ——
    画面才是这个界面的主角。这条测试就是「卡片不会被读数撑高」的守门人。
    """
    tile = tile_factory(SAMPLE_ROWS)
    _refresh(tile)

    inline = _inline_rows(tile)
    assert inline, "信息行里一条读数都没有（读数等于没显示给用户）"
    assert len(inline) <= INLINE_LIMIT, f"信息行并排了 {len(inline)} 条读数，超过上限"

    labels = [row["label"] for row in inline]
    assert "耗材用量" in labels, "耗材用量是最该并排显示的一条"
    assert any(label.startswith("风扇") for label in labels), "风扇读数应当并排显示"

    # 其余读数不进信息行（它们只在 tooltip 里，卡片高度不变）
    text = tile.info_label.text()
    assert "压力提前" not in text
    assert "文件位置" not in text
    assert "归零状态" not in text


def test_桌面_信息行读数做纯文本渲染(tile_factory):
    """契约：读数来自设备，进 RichText 标签前必须转义（值里的 ``<`` 不能当标签解析）。

    这个标签是 ``Qt.RichText``：不转义的话，设备返回的 ``<img src=…>`` 会被
    当成 HTML 渲染 —— 与网页端「不拼 innerHTML」是同一个要求。
    """
    tile = tile_factory([{"label": "风扇 注入", "value": "<img src=x onerror=alert(1)>"}])
    _refresh(tile)

    text = tile.info_label.text()
    assert "<img" not in text, "设备返回的读数被当成 HTML 渲染了"
    assert "&lt;img" in text, "读数应当被转义后再进 RichText 标签"
    # 转义不影响 tooltip（那是纯文本）
    assert "<img src=x onerror=alert(1)>" in tile.toolTip()


def test_桌面_details_为空时整块不显示(tile_factory):
    """契约：``details()`` 返回空列表（拓竹那族）时**整块不显示**，不留空框、不留空行。

    判据是「与改造前逐字节一致」：tooltip 里只有原本就有的通道提示，
    信息行里也一条读数都不多。
    """
    tile = tile_factory([])
    tile.session.warnings.append("画面长时间无更新，正在切换到 RTSPS 通道重试")
    _refresh(tile)

    assert tile.toolTip() == "画面长时间无更新，正在切换到 RTSPS 通道重试", (
        "空读数时 tooltip 应当与改造前完全一致（不留空行、不留空框）"
    )
    text = tile.info_label.text()
    for row in SAMPLE_ROWS:
        assert row["label"] not in text, f"空读数时信息行多出了：{row['label']}"


def test_桌面_没有遥测时不显示读数(tile_factory):
    """契约：离线（没有遥测）时不显示读数。

    Moonraker 适配器的 ``details()`` 是**上一次轮询留下的快照**，跟「离线」角标
    放在一起会误导（设备都没连上，界面却报着风扇转速）。所以与信息行同一判据。
    """
    session = _FakeSession(SAMPLE_ROWS, online=False)
    tile = tile_factory(session=session)
    _refresh(tile)

    assert tile.info_label.text() == "", "离线时信息行应当是空的"
    assert "风扇 fan" not in tile.toolTip()


def test_桌面_读数接口缺失或抛异常时不崩(tile_factory):
    """契约：读数是「锦上添花」，取不到不能把整路画面带崩。

    两种真实情形：老会话根本没有 ``details()``（第三方路径以外的一切）；
    适配器解析设备返回时抛异常（字段格式变了）。
    """
    tile = tile_factory(SAMPLE_ROWS)

    tile.session.details = None  # type: ignore[method-assign]  # 老会话：没有这个方法
    _refresh(tile)
    assert "风扇 fan" not in tile.toolTip()
    assert "风扇 fan" not in tile.info_label.text()

    def boom() -> list[dict[str, str]]:
        raise RuntimeError("设备返回的读数格式变了")

    tile.session.details = boom  # type: ignore[method-assign]
    _refresh(tile)  # 不得抛异常
    assert "风扇 fan" not in tile.toolTip()


# ======================================================================= 网页侧
# 网页端是**单文件内嵌 HTML+JS**，没有构建、没有类型检查：少读一个字段、把设备数据
# 拼进 innerHTML，都不会有任何报错。本文件因此只锁前端代码的形状，不启动浏览器。


def _page_html() -> str:
    from app.web.page import INDEX_HTML

    return INDEX_HTML


def _script() -> str:
    """取出内嵌的 JS（页面里只有一个 `<script>`，且没有 src）。"""
    html = _page_html()
    return html.split("<script>", 1)[-1].rsplit("</script>", 1)[0]


def _block(start: str, end: str) -> str:
    """截取脚本里 [start, end) 之间的片段，把断言限定在某个函数内。

    内嵌 JS 里同一个字符串会出现在多处（例如 ``display``），不断言范围很容易
    「测到了别的地方」却以为过了。
    """
    script = _script()
    begin = script.index(start)
    return script[begin : script.index(end, begin)]


def _details_block() -> str:
    return _block("function renderDetails(", "function applyStatus(")


def test_网页卡片有默认收起的详情块():
    """契约：卡片上必须有这个块，且**默认收起、默认隐藏**。

    默认收起是这次的核心要求：18 条读数全铺开会把一屏占满，用户反而看不到画面。
    """
    script = _script()
    assert re.search(r'<details class="details" style="display:none">', script), (
        "卡片模板里缺少「默认收起且隐藏」的详情块"
    )
    assert "<summary>详情</summary>" in script, "缺少「详情」这个可点的标题"
    assert "<tbody></tbody>" in script, "详情内容应当是一个（可两列的）表格"
    # 详情放在控制按钮**之后**：展开时上面的按钮不跳动
    assert script.index('class="details"') > script.index('<div class="row4">')

    # 样式：窄屏（手机一行两列）也要能看，不横向滚动
    html = _page_html()
    for marker in (".details", "table-layout:fixed", "word-break:break-word"):
        assert marker in html, f"缺少样式：{marker}"


def test_网页详情读每台设备的details字段():
    """契约：渲染的数据来自 ``/api/printers`` 逐台回传的 ``details``，空列表整块隐藏。"""
    block = _details_block()
    assert "info.details" in block, "详情没有读逐台回传的 details 字段"
    assert "Array.isArray(info.details)" in block, "老服务端没有 details 键时要当空处理"
    assert re.search(r"if \(!rows\.length\)\{\s*\n\s*box\.style\.display = 'none'", block), (
        "空列表（拓竹那族）时必须整块隐藏，不留空框"
    )
    # applyStatus() 每帧都走，必须真的把详情渲染进去
    assert "renderDetails(element, info)" in _script(), "applyStatus 里没有调用 renderDetails"


def test_网页详情只用textContent与createElement渲染():
    """契约：读数是设备数据，**不得**拼 innerHTML（等于把设备返回的字符串当代码跑）。"""
    block = _details_block()
    assert "createElement" in block, "详情应当用 createElement 建节点"
    assert "textContent" in block, "详情应当用 textContent 写文本"
    for forbidden in ("innerHTML", "insertAdjacentHTML", "outerHTML", "document.write"):
        assert forbidden not in block, f"详情渲染里出现了 {forbidden}（注入风险）"


def test_网页详情刷新不重置展开状态():
    """契约：``applyStatus()`` 每帧都会调用渲染，用户展开的详情不能被收起、也不能闪。

    * 展开状态是 ``<details>`` 的 DOM 属性：刷新时**绝不写 ``.open``**；
    * 内容没变就不重建表格（否则每一帧都在闪，用户根本看不清）。
    """
    block = _details_block()
    assert not re.search(r"box\.open\s*=", block), "刷新时改写了展开状态（用户展开的详情会被收起）"
    assert not re.search(r"\.open\s*=", block), "渲染里不该出现任何 .open 赋值"
    assert not re.search(r"box\.(remove|replaceWith)\(", block), "刷新时不该整个换掉详情块"
    assert "box.dataset.signature !== signature" in block, "没有按内容签名跳过重建（每帧闪烁）"
    assert "box.style.display = ''" in block, "有读数时没有把详情块显示出来"


def test_网页点详情不会触发整卡全屏():
    """契约：卡片上的点击是「切换全屏」，点「详情」必须拦住这次冒泡。

    否则用户想看读数，画面却直接全屏了 —— 这是个真实的手感事故。
    """
    block = _block("function ensureTile(", "/* ---------------------------------------------------------------- 指令 */")
    assert "querySelector('.details').addEventListener('click'" in block, "详情块没有拦点击冒泡"
    assert "stopPropagation" in block.split("querySelector('.details')")[1][:200], (
        "点详情的处理里没有 stopPropagation"
    )


def test_内嵌JS语法可被esprima解析():
    """契约：内嵌 JS 必须语法正确（HTML 里的特殊字符最容易把脚本写坏）。"""
    if importlib.util.find_spec("esprima") is None:  # pragma: no cover
        pytest.skip("未安装 esprima，跳过内嵌 JS 语法解析")

    import esprima

    esprima.parseScript(_script())  # 语法错会直接抛


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-v"]))
