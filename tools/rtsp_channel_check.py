"""只读检查一台打印机的 RTSPS(322) 通道：SDP 内容 + 首帧 + 采样到的编码参数。

用法：``python tools/rtsp_channel_check.py <IP> [访问代码|--auto]``

用途（Android 复查用）：

* 证明**打印机这一侧的 RTSPS 是好的**（能 DESCRIBE、能出帧）——把"安卓看不到画面"
  的原因定位到客户端缺解码器，而不是打印机不支持；
* 把 SDP 里的关键信息打出来（编码格式、包化模式、SPS/PPS 的 profile/level），
  这些正是自建 H.264 通路（给 WebView 的 MSE 播放）需要的参数；
* 全程**只读**：只发 DESCRIBE / SETUP / PLAY，不发任何控制指令。
"""

from __future__ import annotations

import base64
import os
import re
import socket
import ssl
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.bambu import tlsutil  # noqa: E402
from app.bambu.ports import RTSP_PORT  # noqa: E402
from tools._common import enable_utf8, resolve_code  # noqa: E402

enable_utf8()

PATHS = ("/streaming/live/1", "/streaming/live/2", "/live/1")


def rtsp_request(sock: socket.socket, method: str, url: str, cseq: int, auth: str, extra: str = "") -> str:
    lines = [f"{method} {url} RTSP/1.0", f"CSeq: {cseq}", "User-Agent: bambu-monitor-check"]
    if method == "DESCRIBE":
        lines.append("Accept: application/sdp")
    if auth:
        lines.append(f"Authorization: Basic {auth}")
    if extra:
        lines.append(extra)
    sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
    data = b""
    sock.settimeout(6.0)
    try:
        while b"\r\n\r\n" not in data:
            chunk = sock.recv(4096)
            if not chunk:
                break
            data += chunk
    except (socket.timeout, TimeoutError, OSError):
        pass
    return data.decode("utf-8", "replace")


def parse_sdp(text: str) -> dict:
    """从 SDP 里挑出与视频解码相关的关键行。"""
    info: dict[str, object] = {"raw": text, "lines": [], "sprop": [], "control": [], "codecs": []}
    info["lines"] = [line.strip() for line in text.splitlines() if line.strip()]
    info["sprop"] = re.findall(r"sprop-parameter-sets=([^;\r\n]+)", text)
    info["control"] = re.findall(r"a=control:(\S+)", text)
    info["codecs"] = re.findall(r"a=rtpmap:(\d+) (\S+)", text)
    info["packetization"] = re.findall(r"packetization-mode=(\d+)", text)
    info["profile"] = re.findall(r"profile-level-id=([0-9A-Fa-f]+)", text)
    return info


def decode_sps(sps_b64: str) -> str:
    """把 SPS 的 profile/level 解出来（不解码整段，只看头几个字节）。"""
    try:
        raw = base64.b64decode(sps_b64.strip())
    except Exception as exc:  # noqa: BLE001
        return f"（SPS 解不开：{exc}）"
    if len(raw) < 4:
        return "（SPS 过短）"
    profile = raw[1]
    constraints = raw[2]
    level = raw[3]
    # 1080p / 720p 之类的分辨率要从 SPS 里继续解析，这里只报 profile/level
    return (
        f"profile_idc={profile} (0x{profile:02X}) constraints=0x{constraints:02X} "
        f"level_idc={level} ({level / 10:.1f})"
    )


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    host = argv[0]
    code = resolve_code(host, argv[1] if len(argv) > 1 else "--auto")
    if not code:
        print("没有可用的访问代码（可传 --auto 从配置读取）")
        return 2
    auth = base64.b64encode(f"bblp:{code}".encode()).decode()

    print(f"== {host}:{RTSP_PORT}（RTSPS，只读）==")
    print("\n[1] TLS 连接 + DESCRIBE")
    try:
        tls, verified = tlsutil.connect_tls(host, RTSP_PORT, timeout=6.0, server_hostname=host)
    except Exception as exc:  # noqa: BLE001
        print(f"  ✗ TLS 连接失败：{type(exc).__name__}: {exc}")
        print("  → 322 端口不可用（服务未开启 / 机型不支持）")
        return 1
    print(f"  TLS 握手成功（证书链{'已校验' if verified else '未校验'}）")

    sdp = ""
    chosen = ""
    challenge = ""
    try:
        for index, path in enumerate(PATHS, start=1):
            response = rtsp_request(tls, "DESCRIBE", f"rtsps://{host}:{RTSP_PORT}{path}", index, auth)
            head = response.splitlines()[0] if response else "（无响应）"
            print(f"  DESCRIBE {path} -> {head}")
            if " 200" in head or head.startswith("RTSP/1.0 200"):
                sdp, chosen = response, path
                break
            if " 401" in head or " 403" in head:
                challenge = next(
                    (line.strip() for line in response.splitlines() if line.lower().startswith("www-authenticate")),
                    "",
                )
                print(f"  鉴权质询：{challenge or '（响应里没有 WWW-Authenticate）'}")
                break
    finally:
        pass

    # 打印机用的是 Digest 鉴权：拿到质询后按 RFC 2617 重发一次
    if not sdp and challenge.lower().startswith("www-authenticate: digest"):
        print("\n[1b] 用 Digest 鉴权重发 DESCRIBE")
        fields = {k: (v or w) for (k, v, w) in re.findall(r'(\w+)=(?:"([^"]*)"|([^,\s]+))', challenge)}
        realm = fields.get("realm", "")
        nonce = fields.get("nonce", "")
        opaque = fields.get("opaque", "")
        uri = f"rtsps://{host}:{RTSP_PORT}{PATHS[0]}"
        import hashlib

        def md5(text: str) -> str:
            return hashlib.md5(text.encode()).hexdigest()

        ha1 = md5(f"bblp:{realm}:{code}")
        ha2 = md5(f"DESCRIBE:{uri}")
        digest = md5(f"{ha1}:{nonce}:{ha2}")
        parts = [
            'username="bblp"',
            f'realm="{realm}"',
            f'nonce="{nonce}"',
            f'uri="{uri}"',
            f'response="{digest}"',
        ]
        if opaque:
            parts.append(f'opaque="{opaque}"')
        digest_auth = "Authorization: Digest " + ", ".join(parts)
        for index, path in enumerate(PATHS, start=1):
            response = rtsp_request(
                tls, "DESCRIBE", f"rtsps://{host}:{RTSP_PORT}{path}", 100 + index, digest_auth
            )
            head = response.splitlines()[0] if response else "（无响应）"
            print(f"  DESCRIBE {path}（Digest）-> {head}")
            if " 200" in head:
                sdp, chosen = response, path
                break

    if not sdp:
        tls.close()
        print("  ✗ 没有拿到 SDP（服务未开启或鉴权失败）")
        return 1

    info = parse_sdp(sdp)
    print(f"  可用路径：{chosen}")
    print(f"  编码：{info['codecs']}  包化模式：{info['packetization']}  "
          f"profile-level-id：{info['profile']}")
    for sprop in info["sprop"]:
        params = sprop.split(",")
        print(f"  sprop-parameter-sets：{len(params)} 段（SPS/PPS）")
        for param in params:
            print(f"     {param[:28]}…  {decode_sps(param)}")

    print("\n[2] SETUP（TCP interleaved）+ PLAY，收 3 秒 RTP")
    try:
        setup = rtsp_request(
            tls,
            "SETUP",
            f"rtsps://{host}:{RTSP_PORT}{chosen}/video",
            20,
            auth,
            extra="Transport: RTP/AVP/TCP;unicast;interleaved=0-1",
        )
        print("  " + (setup.splitlines()[0] if setup else "（无响应）"))
        play = rtsp_request(tls, "PLAY", f"rtsps://{host}:{RTSP_PORT}{chosen}", 21, auth)
        print("  " + (play.splitlines()[0] if play else "（无响应）"))
        # 读 3 秒：RTP over TCP 是 "$\x00\x00<len><rtp>"
        tls.settimeout(3.0)
        total = 0
        rtp = 0
        payload = 0
        try:
            while True:
                header = tls.recv(4)
                if not header or len(header) < 4:
                    break
                if header[0] != 0x24:  # '$'：可能是 RTSP 响应，跳过这一批
                    continue
                length = int.from_bytes(header[2:4], "big")
                body = b""
                while len(body) < length:
                    chunk = tls.recv(length - len(body))
                    if not chunk:
                        break
                    body += chunk
                total += length
                rtp += 1
                if len(body) > 12:
                    payload += len(body) - 12
        except (socket.timeout, TimeoutError):
            pass
        print(f"  收到 {rtp} 个 RTP 包 / 负载 {payload / 1024:.0f} KB —— "
              f"{'通道正常，能出数据流' if rtp else '没有收到任何数据'}")
    finally:
        tls.close()

    print("\n[3] 客户端侧：本机有没有 OpenCV（决定安卓/桌面能不能解码）")
    import importlib.util

    has_cv2 = importlib.util.find_spec("cv2") is not None
    print(f"  本机 cv2：{'有' if has_cv2 else '没有'}")
    from app.bambu.rtsp import RtspStream

    print(f"  RtspStream.available() = {RtspStream.available()}")
    if not has_cv2:
        print("  → 在这种情况下 RtspStream.run() 会直接停在「未安装 opencv-python，无法使用 RTSPS」，"
              "连 322 都不会连（这就是安卓版的处境）")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
