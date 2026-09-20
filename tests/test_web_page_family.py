"""网页端「设备族」表单的静态契约（`app/web/page.py`）。

## 为什么需要

服务端已经支持多设备族：

* `GET /api/printers` 顶层多了 `families`（展示名 + 凭据策略 + 默认/候选端口），
  每台设备上还多了 `family` / `family_label` / `credential_label` / `port`
* `POST /api/add_printer` 接受 `family` / `port` / `api_key` / `camera_url`
* `POST /api/printers` 的 `action:"update"` 接受 `api_key` / `port`

而网页端是**单文件内嵌 HTML+JS**，没有构建、没有类型检查：少读一个字段、
把凭据放错字段、或者把「访问代码」写死在标签上，都不会有任何报错 ——
表单照样能提交，只是 Moonraker 这类设备永远填不对凭据（以前就是这样）。

本文件因此只锁**前端代码的形状**（控件 id、请求体里带了哪些键、标签是否来自数据），
不测业务逻辑（那是 `test_web_server.py` / `test_web_management.py` 的事），
也不启动浏览器：真正的渲染效果只能人工看。

**如果这些测试失败**：多半是 `page.py` 的表单被改动过。确认改动是有意的之后，
同步更新本文件里的控件 id 与断言即可；若是无意的，说明第三方设备族的入口又坏了。
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

    内嵌 JS 里同一个字符串（例如 `access_code:`）会出现在多处，不断言范围
    很容易"测到了别的地方"却以为过了。
    """
    script = _script()
    begin = script.index(start)
    return script[begin : script.index(end, begin)]


def _payload(call: str, block: str | None = None) -> str:
    """截出某次 `postJson(...)` 请求体对象字面量的文本（从 `{` 到配对的 `});`）。"""
    text = _script() if block is None else block
    start = text.index(call) + len(call)
    start = text.index("{", start)
    return text[start : text.index("});", start)]


def _add_block() -> str:
    return _block("async function openAdd(", "async function openManage(")


def _edit_block() -> str:
    return _block("async function openEdit(", "async function openSettings(")


# --------------------------------------------------------------------- 控件与标识


def test_添加表单有设备族下拉与凭据端口摄像头控件():
    """契约：这几个 id 是 JS 里 `getElementById` 的选择器，改名会静默失效。"""
    script = _script()
    for marker in (
        'id="add-family"',  # 设备族下拉
        'id="add-cred-label"',  # 凭据标签（文字由族数据写入）
        'id="add-cred"',  # 凭据输入框（拓竹填访问代码 / 第三方族填 API Key）
        'id="add-thirdparty"',  # 端口 + 摄像头 URL 的容器（拓竹族整块隐藏）
        'id="add-port"',  # 端口输入
        'id="add-port-hint"',
        'id="add-camera"',  # 摄像头 URL 输入
        'id="ed-cred"',  # 编辑表单的凭据
        'id="ed-port"',  # 编辑表单的端口
    ):
        assert marker in script, f"页面缺少控件：{marker}"

    # 端口是数字输入（安卓/平板会弹数字键盘）
    assert re.search(r'id="add-port"\s+type="number"', script), "端口输入应当是 type=number"
    # 关键标识：族数据、第三方族的凭据字段、摄像头地址
    for keyword in ("families", "api_key", "camera_url", "access_code"):
        assert keyword in script, f"内嵌 JS 里找不到关键标识：{keyword}"


def test_设备族下拉与凭据字段由families数据渲染():
    """契约：下拉选项与凭据字段名都从 `/api/printers` 的 `families` 来。

    前端写死族名/凭据字段名的话，服务端注册新族（app/core/registry.py）时
    网页端还要再改一次——这正是这次要修掉的东西。
    """
    script = _script()
    assert re.search(r"\bdata\.families\b", script), "没有读取 /api/printers 的 families 字段"
    assert "state.families" in script, "设备族表没有被缓存到 state 里"

    # 选项：值 = 族 id，文字 = 服务端给的展示名
    options = _block("function familyOptionsHtml(", "function portFamilyDetail(")
    assert "item.family" in options and "item.label" in options, (
        "下拉选项必须来自数据（族 id + 展示名），不能写死"
    )
    # 下拉是用这份选项填的
    assert "familyOptionsHtml(selected)" in _add_block(), "添加表单没有用数据渲染族下拉"

    # 端口提示里的默认端口/候选端口同样来自数据
    detail = _block("function portFamilyDetail(", "function itemFamilyId(")
    assert "descriptor.default_port" in detail and "descriptor.candidate_ports" in detail, (
        "端口提示必须用族数据里的 default_port / candidate_ports"
    )


def test_凭据标签占位与必填标记都来自族数据():
    """契约：凭据输入框的标签 / 占位提示 / 是否必填跟着所选族变。

    以前标签写死「访问代码」，Moonraker 设备会看到「请填访问代码」——
    那台设备根本没有这个概念（见 app/core/registry.py::credential_label 的注释）。
    """
    add = _add_block()
    # 模板里只留中性文案，真正的文字由 applyAddFamily() 按族写进去
    assert '<label id="add-cred-label">凭据</label>' in add, (
        "凭据标签的位置必须是中性占位，族特有文案由数据写入"
    )
    assert "访问代码" not in add, "添加表单里不得写死「访问代码」这类族特有文案"
    assert "API Key" not in add
    # 老表单里写死的那对 id 必须消失，否则说明回退了
    assert 'id="add-code"' not in _script(), "旧的写死凭据输入框（add-code）还在"

    # 标签 = credential.label（+ 必填标记），占位 = credential.hint
    apply = _block("function applyAddFamily(", "/* ---------------------------------------------------------------- 手动添加 */")
    assert re.search(
        r"getElementById\('add-cred-label'\)\.textContent\s*=\s*\n?\s*policy\.label", apply
    ), "凭据标签没有来自 credential.label"
    assert re.search(r"getElementById\('add-cred'\)\.placeholder\s*=\s*policy\.hint", apply), (
        "凭据占位提示没有来自 credential.hint"
    )
    assert "policy.required" in apply, "必填标记没有来自 credential.required"


def test_编辑表单的凭据标签也来自族数据():
    """契约：编辑对话框的凭据标签同样按该设备的族来写（拓竹访问代码 / 第三方 API Key）。

    标签优先取服务端**逐台**回传的 `credential_label`，拿不到时才用 `families` 表里的
    `credential.label` 兜底 —— 两处都由服务端注册表派生，前端不写死族特有文案。
    """
    edit = _edit_block()
    assert re.search(r"getElementById\('ed-cred-label'\)\.textContent\s*=\s*\n?\s*itemCredentialLabel\(", edit), (
        "编辑表单的凭据标签没有走 itemCredentialLabel()"
    )
    assert "'（留空表示不改）'" in edit, "编辑表单应当说明「留空表示不改」"
    label = _block("function itemCredentialLabel(", "/* 切换族时凭据")
    assert "item.credential_label" in label, "没有优先用逐台回传的 credential_label"
    assert "credentialPolicy(family).label" in label, "没有用 families 表里的标签兜底"
    assert "访问代码" not in label and "API Key" not in label, "标签位置不得写死族特有文案"


def test_编辑按逐台family判定族与端口():
    """契约：编辑表单用逐台回传的 `family` / `port`，不再靠 `model` 反查族。

    靠 model 反查是上一版的妥协：拓竹机型名一旦撞上某个族的展示名就会判错族，
    而判错族的后果是凭据写进错的字段、被服务端当成「没填」直接丢掉。
    """
    edit = _edit_block()
    assert "itemFamilyId(item)" in edit, "编辑表单没有用 itemFamilyId(item) 判定族"
    fam = _block("function itemFamilyId(", "function itemCredentialLabel(")
    assert "item.family" in fam, "itemFamilyId 没有读逐台的 family 字段"
    # 旧的「按展示名反查」必须彻底消失
    assert "guessFamilyId" not in _script(), "旧的 model 反查族函数还在"
    assert "item.model" not in edit, "编辑表单不该再拿 model 当族判据"
    # 端口用逐台回传的 port（服务端已经算好「实际端口，没有则族默认」）
    assert re.search(r"parseInt\(item\.port", edit), "编辑表单没有用逐台回传的 port"


# ------------------------------------------------------------------- 请求体契约


def test_添加请求体带上family端口与摄像头地址():
    """契约：`/api/add_printer` 的请求体必须带上服务端新接受的这几个字段。"""
    add = _add_block()
    assert "'/api/add_printer'" in add, "添加设备的端点名是前端契约"
    body = _payload("'/api/add_printer'", add)
    for key in ("family:", "port:", "camera_url:", "access_code:", "api_key:"):
        assert key in body, f"add_printer 请求体缺少 {key}"

    # family 是下拉的当前值（空串在服务端即「拓竹」）
    assert re.search(r"family:\s*family\b", body), "family 没有跟着下拉走"
    # 端口 0/非法值 = 用该族默认端口；这里把非法值显式归零
    assert re.search(r"port:\s*thirdParty && port > 0 \? port : 0", body), (
        "端口没有按「第三方族才生效、0 = 用默认」处理"
    )


def test_凭据按所选族放进正确字段():
    """契约：拓竹 → `access_code`，第三方族 → `api_key`（服务端就是这么分工的）。

    凭据放错字段时服务端会认为「没填凭据」，Moonraker 上表现为静默连不上。
    """
    body = _payload("'/api/add_printer'", _add_block())
    assert re.search(r"access_code:\s*policy\.key === 'access_code' \? credential : ''", body), (
        "access_code 没有按 credential.key 判断"
    )
    assert re.search(r"api_key:\s*policy\.key === 'api_key' \? credential : ''", body), (
        "api_key 没有按 credential.key 判断"
    )
    # 字段名取自族数据（credential.key），不是前端写死的分支
    policy = _block("function credentialPolicy(", "async function ensureFamilies(")
    assert "descriptor.credential" in policy, "凭据策略必须来自 families 数据"


def test_编辑请求体带上api_key与port():
    """契约：`action:"update"` 也要能改第三方族的 API Key 与端口。

    而且凭据只放进**该设备所属族对应的那一个字段**，另一个传空串：
    服务端 `manage_printer` 是按设备实际的族挑字段的（另一个键原样忽略），
    所以传空串既安全、又不会同一次请求把两个字段都写上值。
    """
    edit = _edit_block()
    assert re.search(r"action:\s*'update'", edit), "编辑走的是 update 动作"
    body = _payload("'/api/printers'", edit)
    for key in ("access_code:", "api_key:", "port:"):
        assert key in body, f"update 请求体缺少 {key}"
    assert re.search(r"access_code:\s*policy\.key === 'access_code' \? credential : ''", body), (
        "update 的 access_code 没有按该设备的族判断（非该族的字段应当传空串）"
    )
    assert re.search(r"api_key:\s*policy\.key === 'api_key' \? credential : ''", body), (
        "update 的 api_key 没有按该设备的族判断（非该族的字段应当传空串）"
    )
    # 选字段用的 policy 来自这台设备实际的族（family → families 表里的 credential.key）
    assert re.search(r"const family = itemFamilyId\(item\)", edit), (
        "凭据字段名不是按 item.family 定的"
    )
    assert re.search(r"port:\s*port > 0 \? port : 0", body), (
        "0/留空应当表示「不改端口」（服务端只在 port > 0 时写入）"
    )


def test_必填族的凭据为空时在页面上拦住():
    """契约：`required` 为真的族（拓竹）缺凭据时给页面提示，不白发一次必然失败的请求。"""
    add = _add_block()
    guard = re.search(r"if \(policy\.required && !credential\)\{\s*\n\s*status\.textContent = '请填写'", add)
    assert guard, "没有在提交前拦住「必填但没填」的凭据"
    assert "policy.label" in add[guard.start() : guard.start() + 200], (
        "提示里应当带上该族的凭据标签（而不是写死「访问代码」）"
    )
    # 拦住发生在发请求之前
    assert add.index("policy.required && !credential") < add.index("'/api/add_printer'"), (
        "校验必须在提交之前"
    )


def test_端口与摄像头URL只对第三方族生效():
    """契约：拓竹族的端口与画面地址由服务端自动发现，表单整块隐藏、提交时传 0/空。

    隐藏而不是禁用：默认族就是拓竹，隐藏后表单长度与改造前一致；安卓/平板窄屏上
    两个永远不能填的灰框会白占两行（详见 page.py::applyAddFamily 的注释）。
    """
    script = _script()
    assert re.search(r"const FAMILY_BAMBU = 'bambu'", script), "默认族常量（服务端约定空 = 拓竹）"
    assert "isThirdPartyFamily" in script, "缺少「是不是第三方族」的判定"
    # 可见性跟着族走
    assert re.search(
        r"getElementById\('add-thirdparty'\)\.style\.display\s*=\s*\n?\s*isThirdPartyFamily\(family\)",
        script,
    ), "端口/摄像头那一块没有按族显示或隐藏"
    # 提交时拓竹族一律归零，免得隐藏输入框里的残留值被当成用户意图
    body = _payload("'/api/add_printer'", _add_block())
    assert re.search(r"camera_url:\s*thirdParty \? .* : ''", body), (
        "拓竹族不该把隐藏输入框里的摄像头地址提交上去"
    )


def test_切换设备族时凭据按族各自保留():
    """契约：切族时先把当前输入存回旧族，再取新族的草稿（切回去不丢）。"""
    script = _script()
    assert "const addCredDrafts = {}" in script, "没有按族存凭据草稿"
    change = _block("getElementById('add-family').addEventListener('change'", "getElementById('add-cancel')")
    assert re.search(r"addCredDrafts\[addCredFamilyId\] = document\.getElementById\('add-cred'\)\.value", change), (
        "切族前没有把当前输入存回旧族"
    )
    assert re.search(r"addCredFamilyId = event\.target\.value", change), "切族后没有更新当前族"
    assert re.search(r"\.value = addCredDrafts\[addCredFamilyId\] \|\| ''", change), (
        "没有把新族的草稿取回来"
    )


def test_编辑端口用逐台回传的port做占位与提示():
    """契约：`#ed-port` 的占位与提示用服务端逐台回的 `port`（实际端口或族默认端口），
    这样用户改之前就知道这台设备现在连的是哪个端口。"""
    edit = _edit_block()
    # 占位符来自 item.port（算不出来才退化成 0）
    assert re.search(r"placeholder=\"'\s*\+\s*\n?\s*\(currentPort \? String\(currentPort\) : '0'\)", edit), (
        "端口占位符没有用逐台回传的 port"
    )
    # 提示里给出「当前端口」与该族的候选端口（都来自数据）
    assert re.search(r"candidates = \(familyOf\(family\) \|\| \{\}\)\.candidate_ports", edit), (
        "候选端口没有取自 families 数据"
    )
    hint = edit[edit.index("'ed-port-hint'") : edit.index("'ed-back'")]
    assert "currentPort" in hint, "端口提示里没有当前端口"
    assert "candidates.join" in hint, "端口提示里没有该族的候选端口"
    assert "留空或 0 = 不改端口" in hint, "端口提示要说清「留空 = 不改」"


def test_页面不引入任何外部脚本依赖():
    """契约：页面必须能离线 / 内网直接用（没有构建步骤，也不能依赖 CDN）。"""
    html = _page_html()
    assert not re.search(r"<script[^>]*\bsrc=", html), "内嵌页面不得引用外部脚本"
    assert not re.search(r"<link[^>]+href=\"https?://", html), "不得引用外部样式/字体"
    assert "cdn." not in html.lower(), "不得引用 CDN"


def test_前端读的字段确实在families载荷里():
    """契约：JS 读的键必须真在服务端载荷里（键名不一致时只会静默显示兜底文案）。"""
    from app.web import server as web_server

    payload_fn = getattr(web_server, "_families_payload", None)
    assert payload_fn is not None, (
        "服务端不再提供 _families_payload：families 载荷的来源变了，请同步本测试"
    )
    items = payload_fn()
    assert items, "服务端没有登记任何设备族"
    for item in items:
        for key in ("family", "label", "credential", "default_port", "candidate_ports"):
            assert key in item, f"families 载荷缺少 {key}"
        for key in ("key", "label", "required", "hint"):
            assert key in item["credential"], f"credential 里缺少 {key}"
    # 前端实际读的就是这些键（缺一个就只会退化成中性兜底文案）
    script = _script()
    assert "descriptor.credential" in script, "前端没有从载荷里取 credential"
    for key in ("policy.key", "policy.label", "policy.hint", "policy.required"):
        assert key in script, f"前端没有读 {key}"


def test_内嵌JS语法可被esprima解析():
    """契约：内嵌 JS 必须语法正确（HTML 里的特殊字符最容易把脚本写坏）。"""
    if importlib.util.find_spec("esprima") is None:  # pragma: no cover
        pytest.skip("未安装 esprima，跳过内嵌 JS 语法解析")

    import esprima

    esprima.parseScript(_script())  # 语法错会直接抛


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-v"]))
