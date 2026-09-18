"""``app.core.camera_status_text`` 契约测试。

## 为什么单独立一条

同一个判据以前在**两处**各写了一遍（桌面版 ``app/ui/tile.py`` 的角标、网页版
``app/web/server.py`` 的状态行），而且两处都把完整说明截断后直接当标签用
（12 / 14 个字符）——「RTSPS(322) 未取到画面…」被截成「RTSPS(322) 未取」，
用户看不懂。现在统一成一个函数：短标签 + 完整说明，两条界面各自决定怎么放。

这里锁住的关键点是**优先级顺序**，因为两处原实现顺序不同：

1. 画面与遥测都通 -> 「在线」（哪怕说明里还留着上一轮的告警）；
2. 鉴权失败 -> 「访问代码错误」（比「遥测断开」更贴近真实原因）；
3. 只有画面通 -> 「画面正常·遥测断开」；
4. 没填访问代码 -> 「未配置访问代码」；
5. 其余 -> 按 ``last_camera_state`` 给短标签，**完整说明原样返回、不截断**。
"""

from __future__ import annotations

import pytest

from app.core.device import CAMERA_STATE_TEXT, camera_status_text

pytestmark = pytest.mark.usefixtures("no_network")


def test_两路都通显示在线():
    assert camera_status_text(camera_online=True, mqtt_online=True) == ("在线", "")


def test_鉴权失败优先于遥测断开():
    """画面断了 + 代码错：应该报「访问代码错误」，而不是笼统的「连接中」。"""
    label, _ = camera_status_text(
        camera_online=False,
        mqtt_online=False,
        camera_state="auth_error",
        has_access_code=True,
    )
    assert label == "访问代码错误"

    # 遥测侧报的鉴权错同样成立（MQTT 被拒时画面可能还没轮询到）
    label, _ = camera_status_text(
        camera_online=False,
        mqtt_online=False,
        mqtt_auth_error=True,
        has_access_code=True,
    )
    assert label == "访问代码错误"


def test_只有画面通时说明遥测断开():
    label, _ = camera_status_text(camera_online=True, mqtt_online=False)
    assert label == "画面正常·遥测断开"


def test_没有访问代码时给出专门提示():
    label, _ = camera_status_text(
        camera_online=False,
        mqtt_online=False,
        camera_state="stopped",
        has_access_code=False,
    )
    assert label == "未配置访问代码"


@pytest.mark.parametrize("state", sorted(CAMERA_STATE_TEXT))
def test_各通道状态都有短标签(state: str):
    label, _ = camera_status_text(
        camera_online=False, mqtt_online=False, camera_state=state, has_access_code=True
    )
    assert label == CAMERA_STATE_TEXT[state]


def test_未知状态回落到连接中():
    label, detail = camera_status_text(
        camera_online=False,
        mqtt_online=False,
        camera_state="某个适配器自定义的状态",
        has_access_code=True,
    )
    assert label == "连接中"
    assert detail == ""


def test_完整说明不被截断():
    """回归：状态行以前是 ``detail[:14]``，中文提示被截成半句话。"""
    detail = "RTSPS(322) 未取到画面，正在回退到 6000 端口重试"
    label, returned = camera_status_text(
        camera_online=False,
        mqtt_online=False,
        camera_state="retrying",
        camera_detail=detail,
        has_access_code=True,
    )
    assert label == "画面重连中"
    assert returned == detail
    assert len(returned) > 14


def test_短标签长度可控():
    """契约：标签要能塞进很窄的状态行，所以必须是固定短词而不是拼接出来的句子。"""
    for state in sorted(CAMERA_STATE_TEXT):
        label, _ = camera_status_text(
            camera_online=False, mqtt_online=False, camera_state=state, has_access_code=True
        )
        assert len(label) <= 10, label
