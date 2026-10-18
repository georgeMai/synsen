"""AgriGuard – bộ giả lập thiết bị (DỮ LIỆU GIẢ LẬP, không phải dữ liệu thật).

Mô phỏng N bo ESP32 giống hệt hợp đồng MQTT của firmware thật:
  publish  agri/{site}/{device}/telemetry | status (retained + LWT) | ack | event
  subscribe agri/{site}/{device}/cmd
Có mô hình vật lý đơn giản (ngày/đêm, bay hơi đất, tưới, phun sương) và có thể tiêm lỗi
qua topic agri/{site}/{device}/sim/fault, ví dụ: {"fault": "dry_run", "on": true}

Biến môi trường: MQTT_HOST, MQTT_PORT, MQTT_USER, MQTT_PASSWORD, AGRI_SITE,
SIM_DEVICES (mặc định 2), SIM_DAY_MINUTES (độ dài 1 "ngày" mô phỏng, mặc định 30),
SIM_EVAP_FACTOR (tốc độ khô đất, mặc định 1.0), SIM_PREFIX (mặc định "sim").
"""
from __future__ import annotations

import json
import logging
import math
import os
import random
import secrets
import signal
import threading
import time

import paho.mqtt.client as mqtt

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"),
                    format='{"timestamp":"%(asctime)s","status":"%(levelname)s","logger":"simulator","message":"%(message)s"}')
log = logging.getLogger("simulator")

MQTT_HOST = os.getenv("MQTT_HOST", "localhost")
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))
MQTT_USER = os.getenv("MQTT_USER", "")
MQTT_PASSWORD = os.getenv("MQTT_PASSWORD", "")
ROOT = os.getenv("MQTT_TOPIC_ROOT", "agri")
SITE = os.getenv("AGRI_SITE", "farm-a")
N_DEVICES = int(os.getenv("SIM_DEVICES", "2"))
PREFIX = os.getenv("SIM_PREFIX", "sim")
DAY_SECONDS = float(os.getenv("SIM_DAY_MINUTES", "30")) * 60
EVAP = float(os.getenv("SIM_EVAP_FACTOR", "1.0"))

TELEMETRY_S, STATUS_S = 5, 15
SOIL_DRY_RAW, SOIL_WET_RAW = 3500, 1400
PUMP_MAX_S, MIST_MAX_S = 600, 900
PUMP_GRACE_S, PUMP_DRYRUN_S, MIN_FLOW = 8, 60, 0.3
COMM_LOSS_OFF_S = 120
FAULTS = {"dry_run", "leak", "dht_fail", "soil_disconnect", "soil_stuck", "offline", "mist_empty",
          "no_ack", "slow_network"}


def now_ms() -> int:
    return int(time.time() * 1000)


class SimDevice:
    def __init__(self, idx: int) -> None:
        self.device_id = f"{PREFIX}-{idx:02d}"
        self.zone = f"bed-{idx + 1}"
        self.lock = threading.RLock()
        rnd = random.Random(idx)
        self.temp_offset = rnd.uniform(-0.8, 0.8)
        self.phase_offset = rnd.uniform(0, 0.08)
        self.temp = 27.0
        self.hum = 70.0
        self.soil = rnd.uniform(38, 55)
        self.flow = 0.0
        self.liters = 0.0
        self.pump = {"on": False, "since": 0.0, "auto_off": 0.0, "lockout": False, "reason": ""}
        self.mist = {"on": False, "since": 0.0, "auto_off": 0.0, "lockout": False, "reason": ""}
        self.dry_run_s = 0
        self.leak_s = 0
        self.faults: set[str] = set()
        self.stuck_raw: int | None = None
        self.boot = time.time()
        self.reset_reason = "power_on"
        self.seq = 0
        self.offline_since = 0.0
        self.client: mqtt.Client | None = None
        self.base = f"{ROOT}/{SITE}/{self.device_id}/"

    # ------------------------------------------------------------- MQTT
    def connect(self) -> None:
        c = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
                        client_id=f"{self.device_id}-{secrets.token_hex(3)}", clean_session=True)
        if MQTT_USER:
            c.username_pw_set(MQTT_USER, MQTT_PASSWORD or None)
        c.will_set(self.base + "status",
                   json.dumps({"device_id": self.device_id, "site": SITE, "online": False, "reason": "lwt",
                               "source": "sim"}), qos=1, retain=True)
        c.on_connect = lambda cl, u, f, rc, p: (cl.subscribe(self.base + "cmd", qos=1), self.publish_status())
        c.on_message = self.on_command
        c.reconnect_delay_set(1, 10)
        self.client = c
        c.connect_async(MQTT_HOST, MQTT_PORT, keepalive=15)
        c.loop_start()

    def disconnect_like_power_loss(self) -> None:
        if self.client:
            # Broker sẽ phát LWT khi socket đóng đột ngột; gửi thêm bản sao để chắc chắn khi demo
            self.client.publish(self.base + "status",
                                json.dumps({"device_id": self.device_id, "site": SITE, "online": False,
                                            "reason": "lwt", "source": "sim"}), qos=1, retain=True)
            time.sleep(0.2)
            self.client.loop_stop()
            self.client.disconnect()
            self.client = None

    def pub(self, kind: str, payload: dict, retain: bool = False) -> None:
        if self.client is None or "offline" in self.faults:
            return
        base = {"device_id": self.device_id, "site": SITE, "zone": self.zone, "source": "sim", "ts": now_ms()}
        if kind == "telemetry" and "slow_network" in self.faults:
            base["ts"] -= random.randint(6000, 9000)  # gói tin đến trễ 6–9 giây
        base.update(payload)
        self.client.publish(self.base + kind, json.dumps(base), qos=1 if kind != "telemetry" else 0, retain=retain)

    def publish_status(self) -> None:
        self.pub("status", {
            "online": True, "fw_version": "sim-1.0", "uptime_s": int(time.time() - self.boot),
            "free_heap": 180000 + random.randint(-3000, 3000), "rssi_dbm": random.randint(-70, -55),
            "ip": "10.0.0.%d" % (10 + int(self.device_id[-2:])), "reset_reason": self.reset_reason,
            "wifi_reconnects": 0, "mqtt_reconnects": 1, "dht_errors_total": 0,
        }, retain=True)

    def event(self, type_: str, actuator: str | None = None, detail: str | None = None) -> None:
        self.pub("event", {"type": type_, "actuator": actuator, "detail": detail, "flow_lpm": round(self.flow, 2)})
        log.info("%s event %s %s %s", self.device_id, type_, actuator or "", detail or "")

    def on_command(self, client, userdata, msg) -> None:
        if "no_ack" in self.faults:
            return  # thiết bị "treo": không thực hiện, không xác nhận
        try:
            cmd = json.loads(msg.payload)
        except ValueError:
            self.event("bad_command", detail="invalid_json")
            return
        cid = cmd.get("command_id", "unknown")
        name, action = cmd.get("actuator"), cmd.get("action")
        with self.lock:
            act = self.pump if name == "pump" else self.mist if name == "mist" else None
            if act is None:
                self.ack(cid, None, False, "unknown_actuator")
                return
            if action == "on":
                if act["lockout"]:
                    self.ack(cid, name, False, act["reason"])
                    return
                self.set_actuator(name, True, int(cmd.get("duration_s") or 0))
                self.ack(cid, name, True)
            elif action == "off":
                self.set_actuator(name, False)
                self.ack(cid, name, True)
            elif action == "clear_lockout":
                act["lockout"], act["reason"] = False, ""
                self.dry_run_s = 0
                self.ack(cid, name, True, "lockout_cleared")
            else:
                self.ack(cid, name, False, "unknown_action")

    def ack(self, cid: str, name: str | None, ok: bool, reason: str | None = None) -> None:
        payload = {"command_id": cid, "ok": ok}
        if name:
            act = self.pump if name == "pump" else self.mist
            payload.update(actuator=name, state="on" if act["on"] else "off", lockout=act["lockout"])
        if reason:
            payload["reason"] = reason
        time.sleep(random.uniform(0.05, 0.3))  # độ trễ xử lý giống ESP32
        self.pub("ack", payload)

    # ------------------------------------------------------------- actuators
    def set_actuator(self, name: str, on: bool, duration_s: int = 0) -> None:
        act = self.pump if name == "pump" else self.mist
        limit = PUMP_MAX_S if name == "pump" else MIST_MAX_S
        if on:
            if not act["on"]:
                act["since"] = time.time()
            act["auto_off"] = time.time() + min(duration_s, limit) if duration_s else 0.0
            if name == "pump":
                self.dry_run_s = 0
        else:
            act["auto_off"] = 0.0
        act["on"] = on

    # ------------------------------------------------------------- fault injection
    def apply_fault(self, fault: str, on: bool) -> None:
        with self.lock:
            if fault == "clear":
                was_offline = "offline" in self.faults
                self.faults.clear()
                self.stuck_raw = None
                if was_offline:
                    self.connect()
                log.info("%s all faults cleared", self.device_id)
                return
            if fault == "brownout":
                self.boot = time.time()
                self.reset_reason = "brownout"
                for n in ("pump", "mist"):
                    self.set_actuator(n, False)
                    (self.pump if n == "pump" else self.mist).update(lockout=False, reason="")
                self.publish_status()
                log.info("%s simulated brownout reboot", self.device_id)
                return
            if fault not in FAULTS:
                return
            if on:
                self.faults.add(fault)
                if fault == "soil_stuck":
                    self.stuck_raw = self.soil_raw()
                if fault == "offline":
                    self.offline_since = time.time()
                    self.disconnect_like_power_loss()
            else:
                self.faults.discard(fault)
                if fault == "offline":
                    self.connect()
            log.info("%s fault %s=%s", self.device_id, fault, on)

    # ------------------------------------------------------------- physics
    def ambient(self, t: float) -> tuple[float, float]:
        day = (t / DAY_SECONDS + self.phase_offset) % 1.0
        temp = 28.0 + 5.5 * math.sin(2 * math.pi * (day - 0.25)) + self.temp_offset
        hum = 82.0 - 2.2 * (temp - 22.5)
        return temp, hum

    def soil_raw(self) -> int:
        return int(SOIL_DRY_RAW - self.soil / 100 * (SOIL_DRY_RAW - SOIL_WET_RAW) + random.randint(-6, 6))

    def step(self, dt: float) -> None:
        with self.lock:
            t = time.time()
            amb_t, amb_h = self.ambient(t)
            misting = self.mist["on"] and "mist_empty" not in self.faults
            if misting:
                self.hum += (96 - self.hum) * 0.025 * dt
                self.temp += ((amb_t - 2.5) - self.temp) * 0.02 * dt
            else:
                self.hum += (amb_h - self.hum) * (1 - math.exp(-dt / 60))
                self.temp += (amb_t - self.temp) * (1 - math.exp(-dt / 45))

            water_ok = "dry_run" not in self.faults
            if self.pump["on"] and water_ok:
                self.flow = max(0.0, random.gauss(1.6, 0.05))
            elif not self.pump["on"] and "leak" in self.faults:
                self.flow = max(0.0, random.gauss(0.6, 0.05))
            else:
                self.flow = 0.0
            self.liters += self.flow * dt / 60
            evaporation = (0.012 + 0.004 * max(0.0, self.temp - 25)) * EVAP * dt
            self.soil = min(98.0, max(5.0, self.soil - evaporation + self.flow * 0.45 * dt))

            # an toàn cục bộ giống firmware
            for name, act, limit in (("pump", self.pump, PUMP_MAX_S), ("mist", self.mist, MIST_MAX_S)):
                if not act["on"]:
                    continue
                if act["auto_off"] and t >= act["auto_off"]:
                    self.set_actuator(name, False)
                    self.event("auto_off", name, "duration_elapsed")
                elif t - act["since"] > limit:
                    self.set_actuator(name, False)
                    self.event("max_runtime_cutoff", name)
            if self.pump["on"] and t - self.pump["since"] > PUMP_GRACE_S:
                self.dry_run_s = 0 if self.flow >= MIN_FLOW else self.dry_run_s + dt
                if self.dry_run_s >= PUMP_DRYRUN_S:
                    self.set_actuator("pump", False)
                    self.pump["lockout"], self.pump["reason"] = True, "dry_run"
                    self.event("safety_lockout", "pump", "dry_run")
            if not self.pump["on"]:
                self.leak_s = self.leak_s + dt if self.flow >= MIN_FLOW else 0
                if 10 <= self.leak_s < 10 + dt:
                    self.event("flow_without_pump", "pump")
            if "offline" in self.faults and t - self.offline_since > COMM_LOSS_OFF_S:
                self.set_actuator("pump", False)
                self.set_actuator("mist", False)

    def telemetry(self) -> None:
        with self.lock:
            self.seq += 1
            dht_ok = "dht_fail" not in self.faults
            if "soil_disconnect" in self.faults:
                raw, soil_ok = 4095, False
            elif self.stuck_raw is not None:
                raw, soil_ok = self.stuck_raw, True
            else:
                raw, soil_ok = self.soil_raw(), True
            soil_pct = max(0.0, min(100.0, (SOIL_DRY_RAW - raw) * 100 / (SOIL_DRY_RAW - SOIL_WET_RAW)))
            self.pub("telemetry", {
                "seq": self.seq,
                "air_temp_c": round(self.temp + random.gauss(0, 0.1), 1) if dht_ok else None,
                "air_humidity_pct": round(self.hum + random.gauss(0, 0.4), 1) if dht_ok else None,
                "dht_ok": dht_ok, "soil_raw": raw,
                "soil_moisture_pct": round(soil_pct, 1) if soil_ok else None, "soil_ok": soil_ok,
                "pump_on": self.pump["on"], "pump_lockout": self.pump["lockout"],
                "mist_on": self.mist["on"], "mist_lockout": self.mist["lockout"],
                "flow_lpm": round(self.flow, 2), "flow_liters_total": round(self.liters, 3),
                "rssi_dbm": random.randint(-70, -55),
            })


def main() -> None:
    devices = {d.device_id: d for d in (SimDevice(i + 1) for i in range(N_DEVICES))}
    for d in devices.values():
        d.connect()

    ctl = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
                      client_id=f"sim-control-{secrets.token_hex(3)}")
    if MQTT_USER:
        ctl.username_pw_set(MQTT_USER, MQTT_PASSWORD or None)

    def on_fault(client, userdata, msg):
        device_id = msg.topic.split("/")[2]
        dev = devices.get(device_id)
        try:
            body = json.loads(msg.payload)
        except ValueError:
            return
        if dev:
            dev.apply_fault(str(body.get("fault")), bool(body.get("on", True)))

    ctl.on_connect = lambda c, u, f, rc, p: c.subscribe(f"{ROOT}/{SITE}/+/sim/fault", qos=1)
    ctl.on_message = on_fault
    ctl.connect_async(MQTT_HOST, MQTT_PORT, keepalive=30)
    ctl.loop_start()
    log.info("Simulator started: %s devices on %s:%s", len(devices), MQTT_HOST, MQTT_PORT)

    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    last = time.time()
    tick = 0
    while not stop.is_set():
        time.sleep(1)
        now = time.time()
        dt, last = now - last, now
        tick += 1
        for d in devices.values():
            d.step(dt)
            if tick % TELEMETRY_S == 0:
                d.telemetry()
            if tick % STATUS_S == 0:
                d.publish_status()
    for d in devices.values():
        if d.client:
            d.client.loop_stop()
            d.client.disconnect()
    ctl.loop_stop()


if __name__ == "__main__":
    main()
