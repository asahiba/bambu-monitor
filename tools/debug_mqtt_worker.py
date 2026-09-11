"""开发用：单独调试 MqttWorker（连接内置模拟器）。"""

from __future__ import annotations

import logging
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(level=logging.DEBUG, format="%(levelname)s %(name)s %(message)s")

from app.bambu.mqtt_worker import MqttWorker  # noqa: E402
from app.sim.simulator import start_simulator, stop_simulator  # noqa: E402

CODE = "12345678"
printers, responder = start_simulator(1, CODE)
target = printers[0]
print(f"target={target.ip} serial={target.serial} code={CODE}", flush=True)
time.sleep(1.0)

reports: list[dict] = []


def on_report(payload):
    reports.append(payload)
    print(f"REPORT #{len(reports)}: {list(payload.keys())}", flush=True)


def on_state(state, detail):
    print(f"STATE -> {state} ({detail})", flush=True)


worker = MqttWorker(
    host=target.ip,
    serial=target.serial,
    access_code=CODE,
    on_report=on_report,
    on_state=on_state,
    name="debug",
)
worker.start()

for _ in range(20):
    time.sleep(1.0)
    print(f"  t={_:>2}s state={worker.state} detail={worker.detail} reports={len(reports)}", flush=True)
    if reports:
        break

worker.stop()
stop_simulator(printers, responder)
print("RESULT:", "OK" if reports else "FAILED", flush=True)
sys.exit(0 if reports else 1)
