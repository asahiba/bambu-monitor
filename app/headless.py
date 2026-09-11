"""Linux / Docker 用的无界面服务模式。

只启动「协议层 + 网页服务」，完全不依赖 Qt / 桌面环境：

* 在 Linux 服务器、NAS、树莓派、Docker 容器里跑，浏览器远程观看
* 打印机配置来自配置文件（可用环境变量 `BAMBU_MONITOR_CONFIG_DIR` 指定目录）
* 提供 `--discover` / `--add-printer` / `--list` 等管理命令

用法::

    python -m app.headless --port 8080
    python -m app.headless --discover
    python -m app.headless --add-printer "X2D 192.168.31.110 12345678"
"""

from __future__ import annotations

import argparse
import os
import signal
import sys
import threading
import time

from .bambu.models import PrinterInfo, detect_model
from .bambu.ports import DEFAULT_ACCESS_CODE
from .bambu.printer import PrinterSession
from .config import AppConfig, config_path
from .util import secret


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bambu-monitor-headless",
        description="拓竹打印机监控台 · 无界面服务模式（Linux / Docker）",
    )
    parser.add_argument("--port", type=int, default=int(os.environ.get("BAMBU_WEB_PORT", "8080")))
    parser.add_argument("--token", default=os.environ.get("BAMBU_TOKEN", ""), help="网页访问令牌（留空则用配置里的）")
    parser.add_argument("--fps", type=float, default=float(os.environ.get("BAMBU_WEB_FPS", "4")), help="网页帧率")
    parser.add_argument("--width", type=int, default=720, help="网页画面最大宽度")
    parser.add_argument("--max-fps", type=float, default=float(os.environ.get("BAMBU_MAX_FPS", "10")), help="每路画面最大帧率")
    parser.add_argument("--host", default="0.0.0.0", help="监听地址")
    parser.add_argument("--discover", action="store_true", help="扫描局域网并写入配置后退出")
    parser.add_argument("--list", action="store_true", help="列出配置里的打印机后退出")
    parser.add_argument(
        "--add-printer",
        metavar="\"名称 IP 访问代码\"",
        action="append",
        default=[],
        help="添加/更新一台打印机（可重复）",
    )
    parser.add_argument("--remove-printer", metavar="IP", action="append", default=[], help="删除指定 IP 的打印机")
    parser.add_argument(
        "--control",
        choices=["pause", "resume", "stop", "light", "speed"],
        help="下发控制指令后退出（pause/resume/stop/light/speed）",
    )
    parser.add_argument("--value", default="", help="指令参数：light 用 on/off，speed 用 1-4")
    parser.add_argument("--target", default="", help="目标打印机 IP 或名称（留空表示全部）")
    parser.add_argument("--yes", action="store_true", help="stop 指令必须显式加 --yes 才会执行")
    parser.add_argument("--sim", type=int, default=0, metavar="N", help="启动 N 台模拟打印机（演示/自测）")
    parser.add_argument("--sim-code", default=DEFAULT_ACCESS_CODE, help="模拟打印机的访问代码")
    parser.add_argument("--status-interval", type=float, default=10.0, help="终端状态行输出间隔（0 表示关闭）")
    parser.add_argument("--timeout", type=float, default=20.0, help="单轮自动搜索时长")
    return parser


# --------------------------------------------------------------------------- 管理命令
def _cmd_list(config: AppConfig) -> int:
    if not config.printers:
        print("配置里还没有打印机")
        return 0
    print(f"共 {len(config.printers)} 台（{config_path()}）：")
    for info in config.printers:
        code = "有" if len(info.access_code) == 8 else "无"
        print(f"  {info.display_name():14} {info.ip:16} {info.model.label:9} 访问代码={code} 跨度={info.tile_span}")
    return 0


def _cmd_discover(config: AppConfig, timeout: float) -> int:
    from .bambu.discovery import DiscoveryService

    print(f"正在搜索局域网（{timeout:.0f} 秒）…")
    service = DiscoveryService(on_found=lambda info: print(f"  发现 {info.ip:16} {info.model.label:9} {info.serial}"), timeout=timeout)
    found = service.run_blocking()
    if not found:
        print("未发现打印机")
        return 1
    known = {info.serial or info.ip: info for info in config.printers}
    added = 0
    for info in found:
        key = info.serial or info.ip
        existing = known.get(key)
        if existing is None:
            config.printers.append(info)
            added += 1
        else:
            existing.ip = info.ip or existing.ip
            existing.name = existing.name or info.name
            existing.model = info.model if info.model.value != "未知机型" else existing.model
    config.save()
    print(f"共发现 {len(found)} 台，新增 {added} 台；已写入配置")
    print("提示：访问代码需要另外填写（--add-printer \"名称 IP 访问代码\"），否则只看得到进度、看不到画面")
    return 0


def _cmd_add(config: AppConfig, specs: list[str]) -> int:
    for spec in specs:
        parts = spec.split()
        if len(parts) < 3:
            print(f"格式不正确：{spec!r}（应为 \"名称 IP 访问代码\"）")
            return 2
        name, ip, code = parts[0], parts[1], parts[2]
        existing = next((item for item in config.printers if item.ip == ip), None)
        if existing is None:
            info = PrinterInfo(
                ip=ip,
                name=name,
                access_code=code,
                model=detect_model("", name),
            )
            config.printers.append(info)
            print(f"已添加 {name}（{ip}）")
        else:
            existing.name = name
            existing.access_code = code
            print(f"已更新 {name}（{ip}）")
    config.save()
    return 0


def _cmd_control(config: AppConfig, args: argparse.Namespace) -> int:
    """下发一条控制指令（命令行用法，便于脚本自动化）。"""
    targets = [
        item
        for item in config.printers
        if not args.target or args.target in (item.ip, item.display_name(), item.serial)
    ]
    if not targets:
        print(f"没有找到目标打印机：{args.target or '（配置为空）'}")
        return 2
    if args.control == "stop" and not args.yes:
        print("停止打印不可恢复：确认请加 --yes（例如 --control stop --yes --target 192.168.1.50）")
        return 2

    sessions = [PrinterSession(item) for item in targets]
    for session in sessions:
        session.set_max_fps(0)
        session.start()
    results: list[tuple[str, bool, str]] = []
    try:
        # 等遥测连上（控制指令走 MQTT）
        deadline = time.time() + 15
        pending = list(sessions)
        while pending and time.time() < deadline:
            pending = [session for session in pending if not session.status.mqtt_online]
            if pending:
                time.sleep(0.3)
        for session in sessions:
            name = session.info.display_name()
            if not session.status.mqtt_online:
                results.append((name, False, "遥测未连接，未下发指令"))
                continue
            if args.control == "pause":
                ok = session.pause_print()
            elif args.control == "resume":
                ok = session.resume_print()
            elif args.control == "stop":
                ok = session.stop_print()
            elif args.control == "speed":
                try:
                    level = max(1, min(4, int(args.value or 2)))
                except ValueError:
                    results.append((name, False, f"速度档位不合法：{args.value}"))
                    continue
                ok = session.set_speed(level)
            else:  # light
                want_on = (args.value or "on").lower() in ("on", "1", "true", "开")
                ok = session.set_light(want_on)
            results.append((name, bool(ok), "指令已发送" if ok else "发送失败"))
        # 给打印机一点时间反应，便于观察状态变化
        if args.control in ("pause", "resume", "stop"):
            time.sleep(2.0)
            for session in sessions:
                status = session.snapshot()
                print(
                    f"   {session.info.display_name():14} 状态={status.state_text} "
                    f"进度={status.progress}% 舱灯={status.light_on}"
                )
    finally:
        for session in sessions:
            session.stop()

    print(f"\n控制指令 {args.control}（{args.value or '-'}）执行结果：")
    failed = 0
    for name, success, detail in results:
        print(f"  {'✓' if success else '✗'} {name:14} {detail}")
        if not success:
            failed += 1
    return 1 if failed else 0


def _cmd_remove(config: AppConfig, ips: list[str]) -> int:
    for ip in ips:
        before = len(config.printers)
        config.printers = [item for item in config.printers if item.ip != ip]
        print(f"已删除 {ip}" if len(config.printers) < before else f"配置里没有 {ip}")
    config.save()
    return 0


# --------------------------------------------------------------------------- 状态输出
def _print_status(sessions: list[PrinterSession]) -> None:
    online_camera = sum(1 for s in sessions if s.snapshot().camera_online)
    online_mqtt = sum(1 for s in sessions if s.snapshot().mqtt_online)
    printing = sum(1 for s in sessions if s.snapshot().is_printing)
    print(
        f"[{time.strftime('%H:%M:%S')}] 共 {len(sessions)} 台 · 画面在线 {online_camera} · "
        f"遥测在线 {online_mqtt} · 打印中 {printing}"
    )
    for session in sessions:
        status = session.snapshot()
        if not (status.camera_online or status.mqtt_online):
            continue
        print(
            f"   {session.info.display_name():14} {status.state_text:6} {status.progress:>3}% "
            f"喷嘴 {status.nozzle_temper:.0f}/{status.nozzle_target_temper:.0f}℃ "
            f"热床 {status.bed_temper:.0f}/{status.bed_target_temper:.0f}℃ "
            f"{session.video_backend:8} {session.camera_fps:.1f}fps"
        )


# --------------------------------------------------------------------------- 主流程
def run_headless(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = AppConfig.load()
    if args.token:
        config.web_token = args.token
    elif not config.web_token:
        config.web_token = secret.token_hex(8)
    # 令牌落盘：否则每次重启都会换一个访问地址，装到手机主屏的 PWA 就失效了
    config.save()

    if args.list:
        return _cmd_list(config)
    handled = False
    if args.add_printer:
        if _cmd_add(config, args.add_printer):
            return 2
        handled = True
    if args.remove_printer:
        _cmd_remove(config, args.remove_printer)
        handled = True
    if args.discover:
        return _cmd_discover(config, args.timeout)
    if args.control:
        return _cmd_control(config, args)
    if handled:
        # 管理命令执行完就退出，不要顺手把服务也起来
        return _cmd_list(config)

    sessions: list[PrinterSession] = []
    simulator = None
    if args.sim:
        from .sim.simulator import start_simulator

        printers, responder = start_simulator(args.sim, args.sim_code)
        simulator = (printers, responder)
        for printer in printers:
            config.printers.append(
                PrinterInfo(
                    ip=printer.ip,
                    serial=printer.serial,
                    name=f"{printer.name}·模拟",
                    access_code=args.sim_code,
                    model=detect_model(printer.serial, printer.model),
                    stream_mode="tcp6000",
                )
            )
        print(f"已启动 {len(printers)} 台模拟打印机（访问代码 {args.sim_code}）")

    if not config.printers:
        print("配置里还没有打印机：先执行 `--discover` 或 `--add-printer \"名称 IP 访问代码\"`")
        if not args.sim:
            return 1

    for info in config.printers:
        session = PrinterSession(info)
        session.set_max_fps(args.max_fps)
        sessions.append(session)
    for session in sessions:
        session.start()

    from .web.server import WebServer

    server = WebServer(
        get_sessions=lambda: list(sessions),
        port=args.port,
        token=config.web_token,
        fps=args.fps,
        max_width=args.width,
        host=args.host,
    )
    if not server.start():
        print(f"✗ 无法监听 {args.host}:{args.port}（端口被占用？）")
        return 1

    print("=" * 68)
    print("拓竹打印机监控台 · 无界面服务模式已启动")
    for url in server.urls():
        print(f"  网页监控：{url}")
    print(f"  配置文件：{config_path()}")
    print(f"  打印机：{len(sessions)} 台（带访问代码的 {sum(1 for s in sessions if s.info.access_code)} 台）")
    print("=" * 68)
    print("按 Ctrl+C 退出。\n")

    stop = threading.Event()

    def handle_signal(signum, frame) -> None:  # noqa: ARG001
        print(f"\n收到信号 {signum}，正在退出…")
        stop.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, handle_signal)
        except (ValueError, OSError):  # pragma: no cover - 非主线程/不支持的平台
            pass

    try:
        while not stop.wait(1.0):
            if args.status_interval > 0:
                now = time.time()
                if now - getattr(run_headless, "_last_status", 0) >= args.status_interval:
                    run_headless._last_status = now  # type: ignore[attr-defined]
                    _print_status(sessions)
    finally:
        server.stop()
        for session in sessions:
            session.stop()
        if simulator is not None:
            from .sim.simulator import stop_simulator

            stop_simulator(*simulator)
        print("已退出")
    return 0


def main(argv: list[str] | None = None) -> int:
    if sys.stdout is None:  # pragma: no cover - 极端情况
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    # 无界面模式大量使用中文与 ✓/✗ 符号，统一走 UTF-8 输出
    from .util import configure_stdio

    configure_stdio()
    return run_headless(argv)


if __name__ == "__main__":
    sys.exit(main())
