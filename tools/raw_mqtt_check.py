"""开发用：直接用 paho 连接打印机/模拟器的 MQTT，打印收到的状态报文。"""

from __future__ import annotations

import json
import ssl
import sys
import time

import paho.mqtt.client as mqtt

HOST = sys.argv[1] if len(sys.argv) > 1 else "127.0.0.2"
CODE = sys.argv[2] if len(sys.argv) > 2 else "12345678"
SERIAL = sys.argv[3] if len(sys.argv) > 3 else ""
SECONDS = float(sys.argv[4]) if len(sys.argv) > 4 else 8.0

received: list[str] = []


def on_connect(client, userdata, flags, reason_code, properties=None):
    print(f"on_connect reason={reason_code}", flush=True)
    client.subscribe(f"device/{SERIAL}/report" if SERIAL else "device/#")


def on_message(client, userdata, message):
    payload = message.payload.decode("utf-8", "replace")
    received.append(payload)
    print(f"[{message.topic}] {payload[:300]}", flush=True)


def on_log(client, userdata, level, buf):
    print(f"log: {buf}", flush=True)


def on_connect_fail(client, userdata):
    print("on_connect_fail", flush=True)


context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
context.check_hostname = False
context.verify_mode = ssl.CERT_NONE
context.maximum_version = ssl.TLSVersion.TLSv1_2

try:
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="raw-check", protocol=mqtt.MQTTv311)
except AttributeError:  # paho 1.x
    client = mqtt.Client(client_id="raw-check", protocol=mqtt.MQTTv311)

client.on_connect = on_connect
client.on_message = on_message
client.on_connect_fail = on_connect_fail
client.on_log = on_log
client.tls_set_context(context)
client.username_pw_set("bblp", CODE)
client.connect_async(HOST, 8883, keepalive=30)
client.loop_start()

deadline = time.time() + SECONDS
while time.time() < deadline and not received:
    time.sleep(0.2)

client.loop_stop()
client.disconnect()

if not received:
    print("FAILED: 没有收到任何报文", flush=True)
    sys.exit(1)

try:
    payload = json.loads(received[0])
    print_obj = payload.get("print", {})
    print(
        "OK: 进度 {p}% · 喷嘴 {n}/{nt}℃ · 热床 {b}/{bt}℃".format(
            p=print_obj.get("mc_percent"),
            n=print_obj.get("nozzle_temper"),
            nt=print_obj.get("nozzle_target_temper"),
            b=print_obj.get("bed_temper"),
            bt=print_obj.get("bed_target_temper"),
        ),
        flush=True,
    )
except json.JSONDecodeError:
    print("OK: 收到非 JSON 报文", flush=True)
