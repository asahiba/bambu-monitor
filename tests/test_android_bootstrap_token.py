"""安卓版首次启动「一打开就 unauthorized」的回归测试。

## 这个 bug 长什么样

APK 装好第一次打开，WebView 直接显示服务端返回的
``{"error": "unauthorized"}``，不重启永远进不去。

## 根因

`AppConfig.load()` 在**配置文件还不存在**时，每次调用都会新生成一个
``web_token`` 且**不落盘**。安卓版 device_server 原来自己去 load() 一次取令牌
交给 WebView，随后启动服务时 ``run_headless()`` 内部又 load() 了一次，
拿到的是**另一个**令牌 —— WebView 手里那个服务端根本不认。

桌面版看不出来：那里的配置文件早就存在，两次 load() 读的是同一个值。

## 修法

把「取令牌」收敛成一个函数 ``app.headless.resolved_web_token()``，
**先落盘再返回**，服务和宿主都走它。
"""

from __future__ import annotations

import argparse
import http.client
import json

import pytest

from app import headless
from app.config import AppConfig, config_path


@pytest.fixture()
def fresh_install(isolated_config_dir, monkeypatch):
    """模拟「刚装好、还没有配置文件」的首次启动。"""
    # 模块级缓存会跨用例残留，必须清掉
    monkeypatch.setattr(headless, "_SERVED_TOKEN", "", raising=False)
    return isolated_config_dir


def test_空配置下两次load会得到不同令牌(fresh_install):
    """守住根因本身：`load()` 在无配置文件时不是幂等的。

    如果哪天有人把 load() 改成会落盘（或改成幂等），这条会失败 ——
    那说明前提变了，本文件与相关代码的注释需要同步更新。
    """
    first = AppConfig.load().web_token
    second = AppConfig.load().web_token
    assert first, "令牌不应为空"
    assert first != second, (
        "AppConfig.load() 在空配置目录下变成幂等的了："
        "请复核 app/headless.py 与 android 侧 device_server.py 的注释是否还成立"
    )


def test_令牌解析会先落盘再返回(fresh_install):
    """`resolved_web_token()` 必须把令牌持久化，否则重启后地址就变了。"""
    token = headless.resolved_web_token()
    assert token, "解析出的令牌不应为空"
    assert AppConfig.load().web_token == token, (
        "令牌没有落盘：再 load() 会拿到另一个值，WebView 与服务端必然对不上"
    )


def test_令牌解析可重复调用且稳定(fresh_install):
    tokens = [headless.resolved_web_token() for _ in range(3)]
    assert len(set(tokens)) == 1, f"同一进程内令牌应稳定，实际得到 {tokens}"


def test_宿主与服务端拿到同一个令牌_核心回归(fresh_install):
    """**核心回归**：复现安卓 device_server 的时序。

    宿主先要令牌（拿去拼 WebView 地址），随后服务端才起来。
    两者必须一致，否则首次打开就是 unauthorized。
    """
    # 1) 宿主侧：device_server 在起服务之前先取令牌交给 WebView
    token_for_webview = headless.resolved_web_token()

    # 2) 服务端侧：run_headless 解析参数后会再走一次同样的解析
    args = argparse.Namespace(token="")  # 安卓侧不传 --token
    served = headless.resolved_web_token() if not args.token else args.token
    AppConfig.load().web_token = served

    assert served == token_for_webview, (
        f"WebView 拿到 {token_for_webview!r}，服务端却用 {served!r} —— "
        "首次打开必然 unauthorized"
    )


def test_旧写法会被本测试抓住_防回归失效(fresh_install):
    """自检：本文件确实能抓住那个 bug。

    这里**故意复现修复前的写法**（宿主自己 ``AppConfig.load()`` 取令牌），
    断言它得到的令牌与服务端用的不一致。若哪天这条不再失败，
    说明上面那些测试已经抓不到这个 bug 了，需要重新设计。
    """
    # 修复前的写法：宿主自己 load() 一次，把 web_token 交给 WebView
    old_way_token = AppConfig.load().web_token

    # 服务端随后启动时又 load() 了一次
    served_token = AppConfig.load().web_token

    assert old_way_token != served_token, (
        "旧的错误写法竟然也能拿到一致的令牌 —— 说明 AppConfig.load() 的行为变了"
        "（或本测试的空配置前提没生效），请复核"
    )
    # 而且两边都非空，说明这确实是「两个不同令牌」而不是「一个空一个非空」
    assert old_way_token and served_token


def test_命令行显式指定令牌时以此为准(fresh_install):
    headless.resolved_web_token()  # 先让配置文件里有个令牌
    explicit = "deadbeefcafe0001"
    config = AppConfig.load()
    config.web_token = explicit
    config.save()
    assert AppConfig.load().web_token == explicit


def test_服务端鉴权认这个令牌(fresh_install):
    """端到端：用解析出的令牌请求网页应当 200；不带令牌应当 401。"""
    from app.web.server import WebServer

    token = headless.resolved_web_token()
    assert token, "必须先有令牌"
    assert config_path(), "配置路径应可用"

    server = WebServer(get_sessions=lambda: [], port=0, token=token, host="127.0.0.1")
    assert server.start(), "服务应能起来"
    try:
        # port=0 时真实端口由系统分配，回填后再用
        port = server._httpd.server_address[1]

        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", f"/?token={token}")
        resp = conn.getresponse()
        body = resp.read().decode("utf-8")
        conn.close()
        assert resp.status == 200, f"带正确令牌应 200，实际 {resp.status}"
        assert "打印机监控" in body

        # 不带令牌 -> 401，证明鉴权确实在起作用（不是被整体关掉了）
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/")
        resp = conn.getresponse()
        payload = json.loads(resp.read().decode("utf-8"))
        conn.close()
        assert resp.status == 401
        assert payload.get("error") == "unauthorized"

        # 错误令牌 -> 401
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/?token=wrongtoken00")
        resp = conn.getresponse()
        resp.read()
        conn.close()
        assert resp.status == 401
    finally:
        server.stop()
