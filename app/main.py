"""程序入口：``python -m app``。"""

from __future__ import annotations

import argparse
import logging
import logging.handlers
import os
import sys
import threading

from . import __version__
from .bambu.ports import DEFAULT_ACCESS_CODE

LOGGER = logging.getLogger("bambu-monitor")


def setup_logging(debug: bool = False) -> None:
    """把日志写到 %APPDATA%\\BambuMonitor\\logs\\app.log，便于排查问题。"""
    from .config import config_dir

    level = logging.DEBUG if debug else logging.INFO
    logger = logging.getLogger("bambu-monitor")
    logger.setLevel(level)
    try:
        log_dir = os.path.join(config_dir(), "logs")
        os.makedirs(log_dir, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            os.path.join(log_dir, "app.log"), maxBytes=1_000_000, backupCount=3, encoding="utf-8"
        )
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s")
        )
        logger.addHandler(handler)
    except OSError:
        pass
    if sys.stderr is not None:
        stream = logging.StreamHandler(sys.stderr)
        stream.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
        logger.addHandler(stream)

    # 后台线程里的异常默认只打印到 stderr；打包成 windowed exe 后 stderr 为空，
    # 异常会被静默吞掉，这里统一记进日志文件。
    def thread_hook(args) -> None:
        logger.error(
            "后台线程异常（%s）",
            getattr(args.thread, "name", "?"),
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )

    threading.excepthook = thread_hook


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bambu-monitor",
        description="拓竹打印机监控台：局域网自动搜索 + 多画面实时预览 + 进度/温度显示",
    )
    parser.add_argument("--version", action="version", version=f"Bambu Monitor {__version__}")
    parser.add_argument("--core-test", action="store_true", help="无界面端到端自检（内置模拟器）")
    parser.add_argument("--sim", action="store_true", help="随软件启动 4 台虚拟打印机（演示/测试）")
    parser.add_argument("--sim-count", type=int, default=4, help="虚拟打印机数量")
    parser.add_argument("--code", default=DEFAULT_ACCESS_CODE, help="虚拟打印机的访问代码")
    parser.add_argument("--screenshot", metavar="PATH", help="启动后自动截图并退出（自检用）")
    parser.add_argument("--exit-after", type=float, default=10.0, help="配合 --screenshot 的等待秒数")
    parser.add_argument(
        "--web", nargs="?", const=0, type=int, metavar="PORT", help="启动时开启网页监控（可指定端口）"
    )
    parser.add_argument("--export-config", metavar="PATH", help="把当前配置导出到文件后退出（备份用）")
    parser.add_argument(
        "--import-config", metavar="PATH", help="从文件导入配置后退出（会替换当前设备列表）"
    )
    parser.add_argument(
        "--config-passphrase",
        metavar="口令",
        default="",
        help=(
            "导出/导入配置用的口令。设了它，配置文件在任何版本/平台都能导入"
            "（Windows / Linux / Docker / 安卓）；不设则访问代码只在本机可解"
        ),
    )
    parser.add_argument(
        "--ui-selftest",
        action="store_true",
        help="界面自检：打印每一路画面的进度/温度绑定结果（配合 --sim 使用）",
    )
    parser.add_argument("--debug", action="store_true", help="打印调试信息")
    return parser


def ui_selftest_report(window) -> str:
    """检查每一路画面的状态条是否正确绑定了遥测数据。"""
    lines: list[str] = []
    tiles = getattr(window, "tiles", [])
    lines.append(f"tile_count={len(tiles)}")
    for index, tile in enumerate(tiles):
        session = tile.session
        status = session.snapshot()
        lines.append(
            f"[{index}] name={session.info.display_name()} ip={session.info.ip} "
            f"model={session.info.model.label} backend={session.video_backend}"
        )
        lines.append(
            f"    视频: has_frame={tile.video.has_frame()} fps={session.camera_fps:.2f} "
            f"状态={tile.video._status_text} 相机状态={session.last_camera_state}"
            f"({session.last_camera_detail}) 警告={session.warnings}"
        )
        lines.append(
            f"    状态条: 徽标={tile.badge.text()} 进度={tile.progress_label.text()} "
            f"进度条={tile.progress_bar.value()} 剩余={tile.remaining_label.text()}"
        )
        lines.append(
            f"    温度: 喷嘴={status.nozzle_temper:.0f}/{status.nozzle_target_temper:.0f}℃ "
            f"热床={status.bed_temper:.0f}/{status.bed_target_temper:.0f}℃ "
            f"任务='{tile.task_label.text()}'"
        )
        lines.append(
            f"    遥测: mqtt={status.mqtt_online} 相机={status.camera_online} "
            f"进度={status.progress}%"
        )
        trays = "  ".join(
            f"{t.label}:{t.tray_type}:{t.color_hex}:{t.remain_text or '余量不显示'}"
            for t in status.ams_trays
        )
        external = (
            f"{status.vt_tray.tray_type}:{status.vt_tray.color_hex}:"
            f"{status.vt_tray.remain_text or '余量不显示'}"
            if status.vt_tray
            else "无"
        )
        active = status.active_tray.label if status.active_tray else "无"
        lines.append(f"    耗材: AMS {len(status.ams_trays)} 槽 [{trays}] 外挂={external} 当前={active}")
        caps = session.capabilities
        chamber_text = (
            f"{status.chamber_temper:.1f}℃"
            if (status.chamber_temper is not None and caps.has_chamber_sensor)
            else ("无传感器" if not caps.has_chamber_sensor else "--")
        )
        lines.append(
            f"    环境: 仓温={chamber_text}"
            f" WiFi={status.wifi_signal or '--'}"
            f"({status.wifi_level}格) 预计完成={status.finish_time_text}"
            f" 舱灯={'开' if status.light_on else ('关' if status.light_on is False else '未知')}"
        )
        lines.append(
            f"    HMS: {len(status.hms_items)} 条"
            + ("".join(f"\n        {item.display}" for item in status.hms_items) if status.hms_items else "")
        )
        lines.append(
            f"    控件: 暂停可见={tile.pause_button.isVisible()}({tile.pause_button.text()}) "
            f"停止可见={tile.stop_button.isVisible()} 灯光={tile.light_button.text()}"
            f" 可下发={session.can_control}"
        )
        lines.append(
            f"    状态行: 信息='{tile.info_label.text()}'"
        )
        lines.append(
            f"    耗材行: 可见={tile.filament_label.isVisible()} '"
            f"{tile.filament_label.text()}'"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    # 打包成 --windowed 的 exe 时没有控制台，sys.stdout/stderr 为 None，
    # 任何 print 都会抛异常，这里先兜底成空写入器
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = sys.stdout
    # GBK 控制台无法编码 ✓/✗ 等符号，不处理会让自检以 UnicodeEncodeError 收场
    from .util import configure_stdio

    configure_stdio()

    raw = list(sys.argv[1:] if argv is None else argv)
    if "--headless" in raw:
        # 无界面服务模式（Linux / Docker）：不加载 Qt
        raw.remove("--headless")
        from .headless import run_headless

        return run_headless(raw)

    args = build_parser().parse_args(raw)
    setup_logging(args.debug)

    if args.core_test:
        from .selftest import run_core_test

        return run_core_test()

    if args.export_config:
        from .config import AppConfig

        config = AppConfig.load()
        if config.export_to(args.export_config, args.config_passphrase):
            print(f"已导出 {len(config.printers)} 台打印机配置 → {args.export_config}")
            if args.config_passphrase:
                print("（已用口令保护：任何版本都能导入，导入时要用同一个口令）")
            else:
                print("（未设口令：访问代码按本机方式加密，只有同一台机器/同一用户能恢复）")
                print("  想拿到别的设备上用，请加 --config-passphrase 重新导出")
            return 0
        print(f"导出失败：{config.last_error or f'无法写入 {args.export_config}'}")
        return 1

    if args.import_config:
        from .config import AppConfig

        config = AppConfig.load()
        if config.import_from(args.import_config, args.config_passphrase):
            # import_from 只改内存，不落盘 —— 命令行这里必须自己存一次
            config.save()
            if config.last_error:
                print(f"导入失败（写盘出错）：{config.last_error}")
                return 1
            print(f"已导入 {len(config.printers)} 台打印机（来自 {args.import_config}）")
            if config.warnings:
                print(f"提示：{config.warnings}")
            return 0
        print(f"导入失败：{config.last_error or '文件无法解析，或里面没有打印机'}")
        return 1

    try:
        from PySide6.QtCore import QTimer
        from PySide6.QtGui import QFont
        from PySide6.QtWidgets import QApplication, QMessageBox
    except ImportError as exc:
        print("缺少依赖 PySide6：请先执行\n    pip install -r requirements.txt")
        print(f"（{exc}）")
        return 2

    from .config import AppConfig
    from .ui import theme
    from .ui.main_window import MainWindow

    app = QApplication(sys.argv)
    app.setApplicationName("Bambu Monitor")
    app.setApplicationDisplayName("拓竹打印机监控台")
    app.setStyleSheet(theme.STYLESHEET)
    app.setFont(QFont(theme.FONT_FAMILY, 9))

    def handle_exception(exc_type, exc_value, exc_tb) -> None:
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_tb)
            return
        LOGGER.error("未捕获异常", exc_info=(exc_type, exc_value, exc_tb))
        # 自检/自动截图模式不能弹模态框，否则会一直卡住等点击
        if args.ui_selftest or args.screenshot:
            print(f"未捕获异常：{exc_type.__name__}: {exc_value}", file=sys.stderr)
            return
        try:
            from .config import config_dir

            log_file = os.path.join(config_dir(), "logs", "app.log")
            QMessageBox.critical(
                None,
                "程序出现异常",
                f"{exc_value}\n\n详细信息已写入日志：\n{log_file}",
            )
        except Exception:
            pass

    sys.excepthook = handle_exception

    config = AppConfig.load()
    simulator = None
    if args.sim:
        from .bambu.models import PrinterInfo, detect_model
        from .sim.simulator import start_simulator

        # 演示模式：加入虚拟打印机，且不写入真实配置
        config.persist = False
        config.printers = []
        config.auto_connect = True
        printers, responder = start_simulator(args.sim_count, args.code)
        simulator = (printers, responder)
        for printer in printers:
            config.printers.append(
                PrinterInfo(
                    ip=printer.ip,
                    serial=printer.serial,
                    name=f"{printer.name}·模拟",
                    access_code=args.code,
                    model=detect_model(printer.serial, printer.model),
                    # 模拟器只提供 6000 端口通道，这里显式指定，避免按真机规则去找 RTSPS
                    stream_mode="tcp6000",
                )
            )

    window = MainWindow(config)
    if config.window_geometry:
        try:
            from PySide6.QtCore import QByteArray

            window.restoreGeometry(QByteArray.fromBase64(config.window_geometry.encode("ascii")))
        except Exception:
            pass
    window.show()

    if args.web is not None:
        if args.web:
            config.web_port = int(args.web)
        QTimer.singleShot(800, lambda: window.start_web_server(show_dialog=False))

    if simulator is not None:
        window.statusBar().showMessage("演示模式：已启动虚拟打印机（不会保存到配置）", 8000)

    if args.screenshot or args.ui_selftest:
        def capture() -> None:
            if args.ui_selftest:
                report = ui_selftest_report(window)
                print("\n===== 界面自检报告 =====")
                print(report)
                print("========================\n")
                LOGGER.info("界面自检报告\n%s", report)
            if args.screenshot:
                try:
                    window.grab().save(args.screenshot)
                    print(f"screenshot saved: {args.screenshot}")
                except Exception as exc:  # pragma: no cover
                    print(f"screenshot failed: {exc}")
            app.quit()

        QTimer.singleShot(int(args.exit_after * 1000), capture)

    exit_code = app.exec()
    if simulator is not None:
        from .sim.simulator import stop_simulator

        stop_simulator(*simulator)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
