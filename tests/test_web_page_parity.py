"""网页端补齐「桌面版还有、网页端没有」的那几项：静态契约（`app/web/page.py`）。

## 为什么需要

用户的要求是「网页要拥有本地端的全部功能」。桌面版工具栏有「⟳ 轮巡」「▶ 全部连接」
「■ 全部断开」，右键菜单有「单画面」「抓拍」「上移 / 下移 / 移到最前 / 移到最后」，
而网页端过去一样都没有 —— 安卓版没有桌面界面，网页就是唯一入口。

这一轮补上的是：

* 顶部工具栏的**轮巡**开关（默认关闭）、**全部连接 / 全部断开**；
* **单画面区域**：轮巡在里面切台，另有显式的**全屏按钮**（Fullscreen API，失败降级成 CSS 层）
  与「✕ 退出」；
* 卡片上的 **📷 抓拍**：抓**当前选中的那一路**并触发下载（文件名带设备名与时间戳）；
* 卡片菜单里的**画面顺序**四项（`POST /api/layout {"action": …}`）；
* 添加 / 编辑表单的**开灯 / 关灯 G-code** 两个输入框。

## 这个文件锁什么

网页端是**单文件内嵌 HTML+JS**，没有构建、没有类型检查：少读一个字段、把设备数据拼进
innerHTML、或者定时器忘了关，都不会有任何报错 —— 只是功能静默失效。本文件因此只锁
**前端代码的形状**（控件 id、函数名、请求体里带了哪些键、渲染是否只走 textContent），
不启动浏览器：真正的渲染效果只能人工看。

⚠️ 灯光那两个输入框这里只锁**前端**（控件 id、请求体里的字段名）。服务端是否真的把这两个键
接住（`WebServer._add_printer` / `_manage_printer` → 宿主的 `add_printer` / `manage_printer`）
属于接口契约，本文件不断言 —— 那两个函数目前**并没有**转发 `light_on_gcode` /
`light_off_gcode`，写在这里的断言会直接红。等它们补上之后，应当再加一条
「前端提交的键，服务端确实接住了」的用例（本文件的断言只到请求体为止）。

**如果这些测试失败**：多半是 `page.py` 又被改了。确认改动是有意的之后，同步更新本文件里的
控件 id 与断言即可；若是无意的，说明刚补上的桌面版能力又缺了一块（而且不会有任何报错）。
"""

from __future__ import annotations

import importlib.util
import re
import sys

import pytest

# --------------------------------------------------------------------------- 工具


def _page_html() -> str:
    from app.web.page import INDEX_HTML

    return INDEX_HTML


def _script() -> str:
    """取出内嵌的 JS（页面里只有一个 `<script>`，且没有 src）。"""
    html = _page_html()
    return html.split("<script>", 1)[-1].rsplit("</script>", 1)[0]


def _block(start: str, end: str) -> str:
    """截取脚本里 [start, end) 之间的片段，把断言限定在某个函数内。

    内嵌 JS 里同一个字符串（例如 `/api/layout`）会出现在多处，不断言范围
    很容易"测到了别的地方"却以为过了。
    """
    script = _script()
    begin = script.index(start)
    return script[begin : script.index(end, begin)]


def _code_only(text: str) -> str:
    """去掉 JS 注释后的代码。

    「不得出现 innerHTML」这类断言必须只看**代码**：注释里写"拼 innerHTML 等于把设备数据
    当代码渲染"是在解释为什么，不是用法（本项目大量使用这种"为什么"注释）。
    """
    without_block = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return re.sub(r"(?m)//.*$", "", without_block)


def _header() -> str:
    """顶部工具栏那一段 HTML（`<header>` 里所有按钮都在这里）。"""
    html = _page_html()
    return html[html.index("<header>") : html.index("</header>")]


def _payload(call: str, block: str) -> str:
    """截出某次 `postJson(...)` 请求体对象字面量的文本（从 `{` 到配对的 `});`）。"""
    start = block.index(call) + len(call)
    start = block.index("{", start)
    return block[start : block.index("});", start)]


def _state_block() -> str:
    """`state` 字面量：轮巡 / 单画面的**初值**都在这里（默认值就是契约）。"""
    return _block("const state = {", "const wall = document.getElementById('wall');")


def _add_block() -> str:
    return _block("async function openAdd(", "async function openManage(")


def _edit_block() -> str:
    return _block("async function openEdit(", "async function openSettings(")


def _tile_block() -> str:
    """卡片模板与事件绑定。"""
    return _block("function ensureTile(", "/* ---------------------------------------------------------------- 指令 */")


def _single_block() -> str:
    """单画面区域：切台、退出、轮巡的一步。"""
    return _block("单画面 / 全屏 / 轮巡", "通道诊断 / 画面大小")


def _fullscreen_block() -> str:
    """全屏的公共实现（原生 API + CSS 兜底）。"""
    return _block("全屏（原生 + CSS 兜底）", "通道诊断 / 画面大小")


def _patrol_block() -> str:
    """轮巡开关那一支。"""
    return _block("function setPatrol(", "function patrolNext(")


def _shot_block() -> str:
    """抓拍：文件名 + 请求 + 触发下载。"""
    return _block("async function snapshotTile(", "function bindTileMenu(")


def _filename_block() -> str:
    return _block("function snapshotFilename(", "async function snapshotTile(")


def _menu_block() -> str:
    return _block("function tileMenu(", "单画面 / 全屏 / 轮巡")


def _light_block() -> str:
    """灯光那两个输入框：示例文案常量 + 渲染函数（两者一起锁）。"""
    return _block("const LIGHT_GCODE_HINT", "/* ---------------------------------------------------------------- 设备管理 */")


def _sessions_block() -> str:
    return _block("async function sessionsAction(", "/* ---------------------------------------------------------------- 入口绑定 */")


# --------------------------------------------------------------- 工具栏：全局操作


def test_顶部工具栏有轮巡与全部连接断开三个按钮():
    """契约：这三个按钮必须是**工具栏**上的可见控件（桌面版工具栏上就是它们）。

    按钮 id 是 JS 里 `getElementById` 的选择器，改名会静默失效 ——
    点下去什么都不发生，控制台也不会报错。
    """
    header = _header()
    for marker in ('id="btn-patrol"', 'id="btn-connect-all"', 'id="btn-disconnect-all"'):
        assert marker in header, f"顶部工具栏缺少控件：{marker}"
    # 轮巡是"开关"，初始状态要能被读出来（辅助功能 / 自动化都靠它）
    assert 'id="btn-patrol" aria-pressed="false"' in header, (
        "轮巡按钮没有初始的 aria-pressed=false（它就是「默认关闭」这个状态的声明）"
    )
    # 三处都接到了处理函数上
    script = _script()
    assert "getElementById('btn-patrol').addEventListener('click'" in script, "轮巡按钮没有接上"
    assert "getElementById('btn-connect-all').addEventListener('click'" in script
    assert "getElementById('btn-disconnect-all').addEventListener('click'" in script


def test_全部连接与断开走sessions接口():
    """契约：两个按钮都必须走 `POST /api/sessions`，动作名与服务端约定一致。

    服务端（`WebServer._sessions_action`）只认 `connect_all` / `disconnect_all`：
    动作名写错时它回 400，而网页上只会看到一句"操作失败"。
    另外：**这两个动作只动监控连接，不下发任何控制指令** —— 打印机照常打印。
    """
    block = _sessions_block()
    assert "postJson('/api/sessions'" in block, "没有走 /api/sessions"
    body = _payload("postJson('/api/sessions'", block)
    assert re.search(r"action:\s*action", body), "请求体里没有 action"
    script = _script()
    for action in ("connect_all", "disconnect_all"):
        assert f"sessionsAction('{action}')" in script, f"没有调用 {action}"
    # 失败原因要显示出来（例如该运行方式不支持：服务端回 501）
    assert "toast(result.ok ?" in block and "detail" in block, "失败时没有把原因说出来"


# ------------------------------------------------------------------- 轮巡


def test_轮巡默认关闭():
    """契约：轮巡的初值必须是 `false`。

    **为什么默认关闭**：它会自己切画面。用户没开就不该动 —— 一进页面就自动切台，
    会让人以为画面被别人抢了（而且首屏本来还在加载）。
    """
    state = _state_block()
    assert re.search(r"patrol:\s*false", state), "轮巡的初值不是 false（默认必须是关闭）"
    # 定时器初值为空：没有定时器在跑才叫"没开"
    assert re.search(r"patrolTimer:\s*null", state), "轮巡定时器初值不是 null"
    script = _script()
    # 除了 setPatrol() 里那一处，任何地方都不许启动轮巡定时器
    assert script.count("setInterval(patrolNext") == 1, (
        "轮巡定时器只在 setPatrol() 里启动一次；多出来的那处等于绕过开关自动轮巡"
    )


def test_轮巡打开后定时切下一台():
    """契约：打开 = 进入单画面区域 + 起一个定时器，每一步切到**下一台**。

    * 间隔用常量 `PATROL_MS`（不是散落的魔法数字）：切太快会一直停在"正在取画面"；
    * 末尾要绕回第一台（`% total`），否则走到最后一台就不动了；
    * 再点一次必须**关掉定时器**（`clearInterval`），否则后台还在不停切台。
    """
    block = _patrol_block()
    assert "showSingle(singleIndex() >= 0 ? singleIndex() : 0)" in block, (
        "打开轮巡时没有进单画面区域（轮巡就是在这里面切设备）"
    )
    assert "state.patrolTimer = setInterval(patrolNext, PATROL_MS)" in block, (
        "打开后没有起定时器切下一台"
    )
    assert "clearInterval(state.patrolTimer)" in block, "关掉时没有停掉定时器"
    # 间隔是一个常量，且不能短到画面还没加载完就切走
    script = _script()
    interval = re.search(r"const PATROL_MS = (\d+);", script)
    assert interval, "没有 PATROL_MS 常量（间隔必须是显式的、能读出来的）"
    assert 3000 <= int(interval.group(1)) <= 60000, "轮巡间隔不合理（太短会一直在取画面）"

    step = _block("function patrolNext(", "全屏（原生 + CSS 兜底）")
    assert re.search(r"const current = singleIndex\(\) >= 0 \? singleIndex\(\) : 0", step), (
        "切台没有以「当前这一台」为起点"
    )
    assert re.search(r"showSingle\(\(current \+ 1\) % total\)", step), (
        "没有切到下一台并在末尾绕回第一台"
    )
    assert "if (total <= 0) return" in step, "没有设备时不该切（也不该报错）"


def test_轮巡按状态改按钮文字与开关样式():
    """契约：开关状态要看得见（文字 + 高亮），否则用户不知道轮巡到底开着没有。

    轮巡是"自己会动"的功能：状态说不清时，用户看到画面在自己切换会以为是故障。
    """
    block = _patrol_block()
    assert "aria-pressed" in block, "没有更新 aria-pressed"
    assert "classList.toggle('on', state.patrol)" in block, "按钮没有「开着」的视觉状态"
    assert "停止轮巡" in block and "🔁 轮巡" in block, "按钮文字没有跟着状态变"
    # 单画面里的「· 轮巡中」也要跟着清掉
    assert "' · 轮巡中'" in _single_block(), "单画面没有显示轮巡状态"


def test_退出单画面会顺手关掉轮巡():
    """契约：单画面关了，轮巡就必须停。

    轮巡是在单画面区域里切设备：区域都藏起来了还在后台切，等于定时器空转，
    而且按钮显示"轮巡中"、屏幕上却什么都没有 —— 状态与事实矛盾。
    """
    block = _block("function hideSingle(", "function setPatrol(")
    assert "if (state.patrol) setPatrol(false)" in block, "退出单画面没有停掉轮巡"
    # 退出时那条 MJPEG 连接必须断开：不摘 src 它会一直占着"同域最多 6 条连接"里的一条
    assert "removeAttribute('src')" in block, (
        "退出单画面没有摘掉 img 的 src（那条逐路 MJPEG 连接会一直挂着）"
    )
    assert "wall.style.display = ''" in block, "退出单画面没有把监控墙放回来"


# ------------------------------------------------------------- 单画面 / 全屏


def test_单画面区域有完整控件且切台会替代监控墙():
    """契约：单画面是一块**固定的**区域（轮巡在这里跑），控件齐全。

    为什么是"替代监控墙"而不是"让卡片自己放大"：墙上十几张卡时单张放大也只多看清一点，
    读数还是挤在一起；这块区域里只有一台设备，画面能开到最大。
    """
    html = _page_html()
    assert '<section id="single" aria-hidden="true">' in html, "页面里没有单画面区域"
    for marker in (
        'id="single-img"',       # 画面
        'id="single-name"',      # 设备名
        'id="single-info"',      # 状态 / 进度 / 温度
        'id="single-count"',     # 第几台 / 共几台
        'id="single-nosignal"',  # 取不到画面时的说明
        'id="single-prev"', 'id="single-next"',
        'id="single-shot"',      # 抓拍
        'id="single-full"',      # 全屏
        'id="single-exit"',      # 退出（回墙上）
    ):
        assert marker in html, f"单画面区域缺少控件：{marker}"

    block = _single_block()
    assert "wall.style.display = 'none'" in block, "打开单画面时没有把监控墙收起来"
    assert "box.style.display = 'flex'" in block, "打开单画面时没有把它显示出来"
    # 越界与负数都要绕回来（例如在最后一台点「下一台」）
    assert re.search(r"\(\(start % total\) \+ total\) % total", block), (
        "设备序号没有做环绕（在最后一台点「下一台」会算到不存在的设备上）"
    )
    assert "if (!printers.length)" in block, "一台设备都没有时也没有给提示"
    # 画面走 frameUrl（带 cam 与防缓存时间戳），只多开一条连接
    assert "img.src = frameUrl(index, cam)" in block, (
        "单画面没有用 frameUrl 取画面（它是 cam + 时间戳的唯一来源）"
    )


def test_单画面只用textContent渲染设备数据():
    """契约：单画面上的名字 / 状态 / 任务名都是**设备数据**，不得拼 innerHTML。

    与 `renderDetails` / `renderCameras` 是同一条硬要求：拼 innerHTML 等于把设备返回的
    字符串当代码渲染 —— 网页端没有构建、没有类型检查，错了也不会有人发现。
    """
    block = _single_block()
    assert "createElement" in _script(), "页面整体应当有 createElement 的渲染先例"
    assert "textContent" in block, "单画面应当用 textContent 写文本"
    for forbidden in ("innerHTML", "insertAdjacentHTML", "outerHTML", "document.write"):
        assert forbidden not in _code_only(block), f"单画面渲染里出现了 {forbidden}（注入风险）"


def test_单画面换台必须换画面地址():
    """契约：判断"要不要换地址"必须同时比**设备**与**那一路**。

    只看那一路的话（两台设备都用第 0 路 —— 这是常态），切到下一台会被判定成
    "地址没变"，画面就留在上一台，而顶部名字已经换了：用户看到的是「名字是 A、画面是 B」。
    反过来也不能每帧都写 src：那等于不停重开 MJPEG 连接，画面永远停在第一帧。
    """
    block = _single_block()
    assert "state.singleShown === index" in block, "没有记住画面上现在是哪一台（换台不会换画面）"
    assert "state.singleShownCam === cam" in block, "没有记住画面上现在是哪一路"
    assert block.count("img.src = frameUrl(") == 1, (
        "改 src 的地方应当只有一处（setSingleSource）：多出来的那处会在每帧重开 MJPEG 连接"
    )


def test_显式全屏按钮优先原生API并降级到CSS层():
    """契约：卡片上的「⛶ 全屏」是显式入口，走 Fullscreen API，**失败必须降级**。

    两条路缺一不可：
    * iPhone / iPad 的 Safari 不允许对非 <video> 元素调 `requestFullscreen`（直接抛错），
      只留 API 那条路的话按钮按下去毫无反应；
    * 有些内嵌 WebView 里 CSS 的 `position:fixed` 会被父容器裁剪，能用原生时就该用原生。
    """
    block = _fullscreen_block()
    assert "element.requestFullscreen || element.webkitRequestFullscreen" in block, (
        "没有试原生 Fullscreen API（含 webkit 前缀）"
    )
    # 失败降级：加一个 CSS 兜底类（样式里把元素固定到整屏）
    assert re.search(r"catch\(err\)\{\s*\n\s*element\.classList\.add\(fallbackClass\)", block), (
        "Fullscreen API 失败时没有降级成 CSS 层"
    )
    assert "document.exitFullscreen" in block, "没有退出全屏的函数（进去了出不来）"
    assert "document.fullscreenElement === element" in block, (
        "没有把「原生全屏中」算进状态判断（会导致按钮点两次才生效）"
    )
    assert "toast(" in block, "降级成 CSS 层时应当给一句说明（静默降级用户会以为样式坏了）"

    # 两处调用点：卡片用 'full'（既有样式），单画面用 'cssfull'（新样式）
    script = _script()
    assert "toggleFullscreen(tile.element, 'full')" in script, "卡片的全屏按钮没有接到全屏实现上"
    assert "toggleFullscreen(document.getElementById('single'), 'cssfull')" in script, (
        "单画面的全屏按钮没有接到全屏实现上"
    )
    html = _page_html()
    assert "#single.cssfull{position:fixed;inset:0" in html, "缺少 CSS 兜底层的样式"
    assert ".tile.full{position:fixed;inset:0" in html, "卡片原有的 CSS 全屏层样式被删了？"


def test_卡片有显式全屏按钮且按钮文字跟着状态变():
    """契约：卡片模板里要有显式的全屏按钮，进 / 出用同一个按钮，文字跟着状态走。

    卡片上原有的「点整卡全屏」保留（点击只做页面内的 CSS 层放大，误触代价小）；
    显式按钮才是"真全屏"的入口 —— 点一下不知道是进还是出，用户就不敢点。
    """
    tile = _tile_block()
    assert re.search(r'<button class="cam-full" id="cam-full-\$\{index\}"', tile), (
        "卡片模板里没有显式的全屏按钮（id 必须按设备序号唯一）"
    )
    assert "toggleTileFullscreen(index)" in tile, "全屏按钮没有接到处理函数上"
    assert re.search(
        r"camFull\.addEventListener\('click', event => \{\s*\n\s*event\.stopPropagation\(\)", tile
    ), "点全屏按钮必须拦住冒泡（否则整卡点击又会再切一次全屏）"

    sync = _block("function syncTileFullLabel(", "function syncSingleFullLabel(")
    assert "isFullscreen(tile.element, 'full')" in sync, "按钮文字没有按当前状态算"
    assert "退出全屏" in sync, "全屏中的按钮文字没有变成「退出全屏」"
    # 按 Esc 退出原生全屏后，按钮文字也要跟着回来
    assert "addEventListener('fullscreenchange'" in _script(), (
        "没有监听 fullscreenchange：按 Esc 退出后按钮还写着「退出全屏」"
    )


def test_菜单里有退出全屏单画面的入口():
    """契约：右键 / 长按菜单里要能退出全屏并回到墙上（触屏没有 Esc 键可用）。"""
    menu = _menu_block()
    assert "'退出全屏 / 单画面'" in menu and "'单画面 / 全屏'" in menu, (
        "菜单里没有「单画面 / 全屏」这个入口（文字要按当前状态写）"
    )
    assert "toggleTileFullscreen(index)" in menu, "菜单项没有接到全屏实现上"


# ------------------------------------------------------------------- 抓拍


def test_抓拍取当前选中的那一路():
    """契约：抓拍必须带 `?cam=<当前那一路>`。

    多摄像头机器（Voron 的喷嘴 + 舱内）上用户看的是舱内那一路，抓下来的却是第 0 路 ——
    这张图对他就没用了。当前那一路以服务端认下来的为准（卡片上的 `tile.camera`）。
    """
    block = _shot_block()
    assert "api('/api/frame/' + index + '?cam=' + cam)" in block, (
        "抓拍没有带当前选中的那一路（少了 ?cam=）"
    )
    assert "(tile && typeof tile.camera === 'number')" in block, (
        "没有优先用卡片上记住的那一路（服务端认下来的那个）"
    )
    assert re.search(r"parseInt\(\(item \|\| \{\}\)\.camera, 10\) \|\| 0", block), (
        "拿不到卡片状态时没有回退到逐台回传的 camera"
    )
    # 故障要说原因：503 = 服务端现在取不到帧（设备离线 / 那一路刚插上还没出图）
    assert "response.status === 503" in block, "没有单独解释 503（没有画面）"
    assert "抓拍失败：" in block, "失败时没有提示"


def test_抓拍会触发下载且文件名带设备名与时间戳():
    """契约：抓拍要真的把图片**下载**下来，文件名形如 `Voron_20260918-2210.jpg`。

    为什么不用 `toISOString()`：它给的是 UTC，文件名上的时间会和用户手表差好几个小时
    （中国 +8），事后回看根本对不上"几点拍的"。
    """
    block = _shot_block()
    assert "link.download = snapshotFilename(item, index)" in block, "没有设置下载文件名"
    assert "link.click()" in block, "没有触发下载"
    # 挂进文档再点：部分内核（含老版 WebView）对游离节点上的 click() 不触发下载
    assert "document.body.appendChild(link)" in block and "document.body.removeChild(link)" in block
    assert "URL.createObjectURL(blob)" in block and "URL.revokeObjectURL(url)" in block, (
        "blob 地址要么没建、要么没释放（后者会一直占内存）"
    )

    name = _filename_block()
    for part in ("getFullYear()", "getMonth() + 1", "getDate()", "getHours()", "getMinutes()"):
        assert part in name, f"文件名的时间戳缺了 {part}（要的是本地时间，不是 UTC）"
    assert "'.jpg'" in name, "文件名没有 .jpg 后缀"
    assert re.search(r"replace\(/\[.*\\s\]\+/g, '_'\)", name), (
        "设备名里的空格与非法字符没有换成下划线（用户随手填的名字会破坏文件名）"
    )
    assert "(item && item.name) || ('printer-' + index)" in name, (
        "没有名字时应当退回 printer-<序号>，而不是留个空名"
    )


def test_卡片与单画面上都有抓拍按钮():
    """契约：两个入口都要有，且都拦住冒泡（否则点抓拍先触发整卡放大）。"""
    tile = _tile_block()
    assert re.search(r'<button class="cam-shot" id="cam-shot-\$\{index\}"', tile), (
        "卡片模板里没有「📷 抓拍」按钮"
    )
    assert "📷 抓拍" in tile, "按钮文字必须能让人一眼看出是干什么的"
    assert "snapshotTile(index, printerOf(index))" in tile, "抓拍按钮没有接到抓拍实现上"
    assert re.search(
        r"camShot\.addEventListener\('click', event => \{\s*\n\s*event\.stopPropagation\(\)", tile
    ), "点抓拍按钮必须拦住冒泡"
    bindings = _block("document.getElementById('single-shot')", "document.addEventListener('fullscreenchange'")
    assert "snapshotTile(index, printerOf(index))" in bindings, "单画面上的抓拍按钮没有接上"


# --------------------------------------------------------------- 画面顺序


def test_画面顺序四个入口在卡片菜单里():
    """契约：上移 / 下移 / 移到最前 / 移到最后必须都能点到，走 `POST /api/layout`。

    **为什么放在卡片菜单而不是顶部工具栏**：顺序是**逐台**的（服务端 `/api/layout`
    必须带 `index`），工具栏上没有"当前是哪台"这个概念 —— 放那儿还得让用户先选一次设备。
    工具栏只放"全局"操作：轮巡、全部连接 / 全部断开。
    """
    menu = _menu_block()
    for direction in ("'up'", "'down'", "'top'", "'bottom'"):
        assert f"reorderTile(index, {direction})" in menu, f"菜单里缺少顺序操作：{direction}"
    for label in ("画面上移", "画面下移", "移到最前", "移到最后"):
        assert label in menu, f"菜单里缺少入口文字：{label}"
        # 逐台操作不放工具栏（理由见上）
        assert label not in _header(), f"「{label}」不该出现在顶部工具栏里"
    # 已经在头 / 尾的那两项置灰：点了也不会变，按钮却看着能用会让人怀疑没生效
    assert re.search(r"add\('画面上移', \(\) => reorderTile\(index, 'up'\), '', index <= 0\)", menu), (
        "「画面上移」在第一台时没有置灰"
    )
    assert re.search(r"index >= total - 1\)", menu), "「画面下移 / 移到最后」在最后一台时没有置灰"


def test_顺序调整走layout接口并同步墙面顺序():
    """契约：服务端改了顺序之后，墙面上的卡片位置必须跟着改。

    `applyStatus()` 是按 index 找卡片的，而卡片本身不会自己换位置 —— 不同步的话会变成
    "卡片没动、内容却换成了另一台设备"（用户看到的是画面串台）。
    """
    block = _block("async function reorderTile(", "function resyncWallOrder(")
    assert "postJson('/api/layout', {index: index, action: direction})" in block, (
        "顺序调整没有走 /api/layout 的 action 形式（那是服务端约定的入口）"
    )
    assert "toast(result.ok ?" in block, "失败时没有把原因说出来"
    assert "resyncWallOrder()" in block, "调整顺序后没有同步墙面上的卡片顺序"
    assert "pollStatus()" in block, "调整顺序后没有刷新状态"

    sync = _block("function resyncWallOrder(", "function snapshotFilename(")
    assert "sort((a, b) => a - b)" in sync, "重排没有按序号升序"
    assert "wall.appendChild(state.tiles.get(index).element)" in sync, (
        "没有用 appendChild 移动已有节点（重建节点会让画面重连、闪一下）"
    )


# --------------------------------------------------------------- 灯光 G-code


def test_灯光输入框由数据渲染且带示例与留空说明():
    """契约：`开灯 G-code` / `关灯 G-code` 两个输入框存在，示例与"留空"的含义都说清楚。

    为什么让用户自己填命令：Klipper 机器上"舱灯"没有统一做法 —— 可能是宏（`LIGHT_ON`）、
    输出引脚（`SET_PIN PIN=caselight VALUE=1`），也可能是某个风扇
    （`SET_FAN_SPEED FAN=chamber_light SPEED=1`）。软件猜不出来，只能让用户填自己机器上
    真正有效的那条。留空 = 这台设备没有可控灯光（卡片上就不出现「💡 灯」按钮）。
    """
    block = _light_block()
    assert "createElement('label')" in block and "createElement('input')" in block, (
        "输入框应当用 createElement 建（值来自配置/服务端，不能拼进 HTML 串）"
    )
    assert "label.textContent = row.label" in block, "标签文字应当用 textContent 写"
    assert "input.placeholder = row.placeholder" in block, "示例（placeholder）没有写进输入框"
    for text in ("开灯 G-code", "关灯 G-code"):
        assert text in block, f"缺少输入框：{text}"
    # 示例覆盖三种常见做法里的两种，第三种在说明文字里
    assert "LIGHT_ON" in block and "SET_PIN PIN=caselight VALUE=1" in block, (
        "placeholder 没有给出可照抄的示例"
    )
    assert "SET_FAN_SPEED FAN=chamber_light SPEED=1" in block, (
        "说明里没有提到「某个风扇」这类做法"
    )
    assert "note.textContent = hint + LIGHT_GCODE_HINT" in block, (
        "两个表单各自的「留空」含义没有接上共用的说明（漏了就会少半句话）"
    )
    # 「留空 = 没有可控灯光」是**添加表单**的说法（新设备没有历史值可保留）；
    # 编辑表单里"留空"的含义是"不改"（见下一条用例）
    assert "没有可控灯光" in _add_block(), "添加表单没有说明「留空 = 该设备没有可控灯光」"
    # 这两条命令只对第三方设备有效（拓竹的灯由设备本身控制）—— 不说清就是给了个没用的框
    assert "只对第三方设备" in block, "没有说明这两条命令只对第三方设备（Klipper / Moonraker）有效"
    # 控件的 id 必须与提交时读的一致 —— 那种不一致不会有任何报错
    assert "prefix + '-light-on'" in block and "prefix + '-light-off'" in block, (
        "输入框 id 的后缀变了（提交时读的 <prefix>-light-on/off 会对不上）"
    )


def test_添加表单带灯光字段并随请求提交():
    """契约：添加表单要有这两个框，提交时带上 `light_on_gcode` / `light_off_gcode`。"""
    add = _add_block()
    assert "'<div id=\"add-light\"></div>'" in add, "添加表单里没有放灯光输入框的容器"
    assert "lightFields('add'" in add, "添加表单没有渲染灯光输入框"
    body = _payload("'/api/add_printer'", add)
    assert re.search(
        r"light_on_gcode:\s*document\.getElementById\('add-light-on'\)\.value\.trim\(\)", body
    ), "add_printer 请求体缺少 light_on_gcode（或读的控件 id 与渲染时不一致）"
    assert re.search(
        r"light_off_gcode:\s*document\.getElementById\('add-light-off'\)\.value\.trim\(\)", body
    ), "add_printer 请求体缺少 light_off_gcode"


def test_编辑表单带灯光字段并走update动作():
    """契约：编辑表单要有这两个框，`action:"update"` 时带上这两个键。

    留空 = 不改（与凭据、端口同一条约定）：编辑一台设备时不该因为"我没填灯光"
    就把人家原来的灯控命令清掉。
    """
    edit = _edit_block()
    assert "'<div id=\"ed-light\"></div>'" in edit, "编辑表单里没有放灯光输入框的容器"
    assert "lightFields('ed'" in edit, "编辑表单没有渲染灯光输入框"
    assert re.search(r"action:\s*'update'", edit), "编辑走的是 update 动作"
    body = _payload("'/api/printers'", edit)
    assert re.search(
        r"light_on_gcode:\s*document\.getElementById\('ed-light-on'\)\.value\.trim\(\)", body
    ), "update 请求体缺少 light_on_gcode"
    assert re.search(
        r"light_off_gcode:\s*document\.getElementById\('ed-light-off'\)\.value\.trim\(\)", body
    ), "update 请求体缺少 light_off_gcode"
    # 两个表单的"留空"含义不同，说明文字必须分开写
    assert "留空表示不改" in edit, "编辑表单没有说明「留空表示不改」"


def test_灯光字段回填当前值():
    """契约：编辑时要用逐台回传的当前值回填（拿不到就是空框 = 不改）。"""
    block = _light_block()
    assert re.search(r"const current = data\[row\.key\]", block), "没有按字段名取当前值"
    assert "input.value = current == null ? '' : String(current)" in block, (
        "没有把当前值写进输入框（编辑时用户看不到现在配的是什么）"
    )
    assert "light_on_gcode" in block and "light_off_gcode" in block, (
        "回填用的字段名与服务端（PrinterInfo）不一致"
    )


# --------------------------------------------------------------- 实时模式


def test_实时模式多路复用没有被改坏():
    """契约：新增的这几项**不能**动到实时模式（`/api/live` 单连接多路复用）。

    实时模式下画面是服务端在同一条连接上推过来的：谁去改 `<img src>`，就等于又开一条
    MJPEG 长连接，把"同域最多 6 条连接"的坑重新踩回来（第 7 路画面就一直排队）。
    """
    script = _script()
    assert "/api/live" in script, "多路复用端点名是前端契约"
    assert "function startLive(" in script and "function processBuffer(" in script
    assert "KIND_FRAME" in script and "KIND_H264" in script, "多路复用的记录类型被删了？"

    frame_block = _block("function frameUrl(", "function ensureTile(")
    live_branch = re.search(r"if \(state\.live && !state\.fallback\)\{(.*?)\n  \}", frame_block, re.S)
    assert live_branch, "没有区分实时模式与兼容模式"
    assert ".src" not in live_branch.group(1), "实时模式下不该改 <img src>"

    # 单画面是**另一条**通道：它走 frameUrl（/stream/<i>?cam=N），只多开一条连接，
    # 与墙上的多路复用互不干扰 —— 也绝不去改卡片自己那条 img.src
    assert "img.src = frameUrl(index, cam)" in _single_block()
    assert "tile.img.src" not in _single_block(), "单画面不该去改卡片上那条 img 的源"


# --------------------------------------------------------------- 通用要求


def test_新增渲染只用textContent与createElement():
    """契约：所有设备数据（名字、detail、命令示例等）都不得拼进 innerHTML。

    这几块都是本轮新加的渲染代码，直接静态守住：出现 innerHTML / insertAdjacentHTML /
    outerHTML / document.write 就等于把设备与配置里的字符串当代码渲染。
    """
    blocks = {
        "灯光输入框": _light_block(),
        "单画面": _single_block(),
        "画面顺序同步": _block("function resyncWallOrder(", "function snapshotFilename("),
    }
    for name, block in blocks.items():
        for forbidden in ("innerHTML", "insertAdjacentHTML", "outerHTML", "document.write"):
            assert forbidden not in _code_only(block), (
                f"{name}的渲染里出现了 {forbidden}（注入风险）"
            )
    # 正面要求：文本一律走 textContent / value，节点一律走 createElement
    assert "textContent" in blocks["单画面"], "单画面应当用 textContent 写文本"
    assert "createElement" in blocks["灯光输入框"], "灯光输入框应当用 createElement 建节点"


def test_页面不引入任何外部脚本依赖():
    """契约：新增功能必须仍然离线可用（没有构建步骤，也不能依赖 CDN）。"""
    html = _page_html()
    assert not re.search(r"<script[^>]*\bsrc=", html), "内嵌页面不得引用外部脚本"
    assert "cdn." not in html.lower(), "不得引用 CDN"


def test_内嵌JS语法可被esprima解析():
    """契约：内嵌 JS 必须语法正确（HTML 里的特殊字符最容易把脚本写坏）。

    ⚠️ 本轮新加的代码用了模板字符串、正则字面量与 async/await：写错一处，
    整个页面的脚本都不会执行（画面全黑、状态全是"--"），而 Python 这边一点报错都没有。
    """
    if importlib.util.find_spec("esprima") is None:  # pragma: no cover
        pytest.skip("未安装 esprima，跳过内嵌 JS 语法解析")

    import esprima

    esprima.parseScript(_script())  # 语法错会直接抛


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-v"]))
