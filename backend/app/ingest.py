"""Xử lý thông điệp MQTT: lưu MongoDB, cập nhật trạng thái chạy, phát metric Datadog."""
from __future__ import annotations

import asyncio
import logging
import math
import time
from collections import deque
from datetime import datetime, timedelta, timezone

from .commands import commands
from .config import settings
from .db import db, utcnow
from .incidents import incidents
from .diagnose import CAUSE_TEXT, diagnose_pump_run, diagnose_reconnect
from .irrigation import BASELINE_STATUSES, ASSESSED, IrrigationTracker, assess, describe
from .metrics import device_tags, metrics
from .state import Reading, runtime

log = logging.getLogger(__name__)

# Độ trễ ingest gần đây (ms) – dùng để kiểm chứng sự cố đường ống dữ liệu
recent_latencies: deque[tuple[datetime, float]] = deque(maxlen=2000)

TELEMETRY_FIELDS = ("air_temp_c", "air_humidity_pct", "soil_moisture_pct", "flow_lpm", "flow_liters_total")


def _num(value, lo: float | None = None, hi: float | None = None) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        x = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(x) or math.isinf(x):
        return None
    if (lo is not None and x < lo) or (hi is not None and x > hi):
        return None
    return x


def _device_ts(data: dict, received: datetime) -> tuple[datetime, float | None]:
    """Thời điểm đo theo đồng hồ thiết bị (NTP). Không hợp lệ -> dùng thời điểm nhận."""
    raw = data.get("ts")
    ms = _num(raw)
    if not ms:
        return received, None
    ts = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    if abs((received - ts).total_seconds()) > 3600:
        return received, None
    return ts, max(0.0, (received - ts).total_seconds() * 1000)


async def handle_message(site: str, device_id: str, kind: str, data: dict, received: datetime) -> None:
    if kind == "telemetry":
        await _telemetry(site, device_id, data, received)
    elif kind == "status":
        await _status(site, device_id, data, received)
    elif kind == "ack":
        await commands.on_ack(device_id, data, received)
    elif kind == "event":
        await _event(site, device_id, data, received)


async def _telemetry(site: str, device_id: str, data: dict, received: datetime) -> None:
    ts, latency_ms = _device_ts(data, received)
    source = data.get("source") if data.get("source") in ("real", "sim") else "real"
    zone = str(data.get("zone") or "default")
    temp = _num(data.get("air_temp_c"), -20, 70)
    hum = _num(data.get("air_humidity_pct"), 0, 100)
    soil = _num(data.get("soil_moisture_pct"), 0, 100)
    soil_raw = _num(data.get("soil_raw"), 0, 4095)
    flow = _num(data.get("flow_lpm"), 0, 60) or 0.0
    dht_ok = bool(data.get("dht_ok", temp is not None)) and temp is not None and hum is not None
    soil_ok = bool(data.get("soil_ok", soil is not None)) and soil is not None
    reading = Reading(
        ts=ts, air_temp_c=temp, air_humidity_pct=hum, soil_moisture_pct=soil,
        soil_raw=int(soil_raw) if soil_raw is not None else None, dht_ok=dht_ok, soil_ok=soil_ok,
        pump_on=bool(data.get("pump_on")), pump_lockout=bool(data.get("pump_lockout")),
        mist_on=bool(data.get("mist_on")), mist_lockout=bool(data.get("mist_lockout")),
        flow_lpm=flow, rssi_dbm=int(_num(data.get("rssi_dbm")) or 0) or None,
        flow_total=_num(data.get("flow_liters_total"), 0),
    )

    doc = {
        "ts": ts, "meta": {"device_id": device_id, "site": site, "zone": zone, "source": source},
        "received_at": received, "seq": data.get("seq"),
        "air_temp_c": temp, "air_humidity_pct": hum, "soil_moisture_pct": soil, "soil_raw": reading.soil_raw,
        "dht_ok": dht_ok, "soil_ok": soil_ok, "pump_on": reading.pump_on, "pump_lockout": reading.pump_lockout,
        "mist_on": reading.mist_on, "mist_lockout": reading.mist_lockout, "flow_lpm": flow,
        "flow_liters_total": _num(data.get("flow_liters_total")), "rssi_dbm": reading.rssi_dbm,
        "ingest_latency_ms": round(latency_ms) if latency_ms is not None else None,
    }
    t0 = time.perf_counter()
    await db.telemetry.insert_one(doc)
    write_ms = (time.perf_counter() - t0) * 1000

    rt = runtime.get(device_id)
    was_offline = rt.last_seen is not None and not rt.online
    prev = rt.latest
    rt.site, rt.zone, rt.source = site, zone, source
    rt.add(reading)
    try:
        cfg = await db.get_settings_cached()
        done = IrrigationTracker.on_reading(rt, reading, prev, reading.flow_total,
                                            prev.flow_total if prev else None, cfg)
        if done:
            await _finish_irrigation(rt, *done, cfg)
    except Exception:  # phân tích tưới không được làm hỏng luồng ingest chính
        log.exception("Irrigation analysis failed", extra={"device_id": device_id})

    latest = {k: doc[k] for k in ("air_temp_c", "air_humidity_pct", "soil_moisture_pct", "soil_raw", "dht_ok",
                                  "soil_ok", "pump_on", "pump_lockout", "mist_on", "mist_lockout", "flow_lpm",
                                  "rssi_dbm")}
    latest["ts"] = ts
    await db.devices.update_one(
        {"_id": device_id},
        {"$set": {"site": site, "zone": zone, "source": source, "last_seen": ts, "online": True, "latest": latest,
                  "actuators.pump.state": "on" if reading.pump_on else "off",
                  "actuators.pump.lockout": reading.pump_lockout,
                  "actuators.mist.state": "on" if reading.mist_on else "off",
                  "actuators.mist.lockout": reading.mist_lockout},
         "$setOnInsert": {"first_seen": ts, "actuators.pump.mode": "AUTO", "actuators.mist.mode": "AUTO"}},
        upsert=True,
    )
    if was_offline:
        log.info("Device back online", extra={"device_id": device_id})

    # ---- Datadog metrics ----
    tags = device_tags(device_id, site, zone, source)
    metrics.gauge("air.temperature", temp, tags)
    metrics.gauge("air.humidity", hum, tags)
    metrics.gauge("soil.moisture", soil, tags)
    metrics.gauge("soil.raw", reading.soil_raw, tags)
    metrics.gauge("pump.state", int(reading.pump_on), tags)
    metrics.gauge("pump.lockout", int(reading.pump_lockout), tags)
    metrics.gauge("mist.state", int(reading.mist_on), tags)
    metrics.gauge("flow.rate", flow, tags)
    metrics.gauge("device.rssi", reading.rssi_dbm, tags)
    metrics.gauge("sensor.ok", int(dht_ok), {**tags, "sensor": "dht11"})
    metrics.gauge("sensor.ok", int(soil_ok), {**tags, "sensor": "soil"})
    metrics.increment("ingest.messages", tags={"device_id": device_id, "source": source})
    metrics.histogram("mongo.write_latency_ms", write_ms, tags={"collection": "telemetry"})
    if latency_ms is not None:
        recent_latencies.append((received, latency_ms))
        metrics.histogram("ingest.latency_ms", latency_ms, tags={"device_id": device_id, "source": source})
        if latency_ms <= settings.ingest_slo_ms:
            metrics.increment("ingest.within_slo", tags={"device_id": device_id, "source": source})
    else:
        metrics.increment("ingest.within_slo", tags={"device_id": device_id, "source": source})
    if prev is not None:
        dt = min(30.0, max(0.0, (ts - prev.ts).total_seconds()))
        if reading.pump_on:
            metrics.increment("pump.runtime_seconds", round(dt), tags)
        if reading.mist_on:
            metrics.increment("mist.runtime_seconds", round(dt), tags)
        liters = flow * dt / 60
        if liters > 0:
            metrics.increment("flow.volume_ml", round(liters * 1000), tags)


async def _finish_irrigation(rt, run, soil_after, cfg: dict) -> dict:
    """Chấm điểm một lần tưới đã thấm xong, lưu MongoDB, phát metric Datadog."""
    c = cfg["irrigation"]
    history = [d["ratio_pct_per_l"] async for d in db.irrigation_events.find(
        {"device_id": rt.device_id, "status": {"$in": list(BASELINE_STATUSES)}, "ratio_pct_per_l": {"$ne": None}},
        {"ratio_pct_per_l": 1}).sort("ended_at", -1).limit(int(c["baseline_events"]))]
    ev = assess(run, soil_after, history, cfg)
    ev["irrigation_id"] = f"IRR-{run.started_at:%y%m%d-%H%M%S}-{rt.device_id}"
    doc = {"_id": ev["irrigation_id"], "device_id": rt.device_id, "site": rt.site, "zone": rt.zone,
           "source": rt.source, **ev, "created_at": utcnow()}
    await db.irrigation_events.replace_one({"_id": doc["_id"]}, doc, upsert=True)
    rt.ai_plan_at = None                 # lần tưới đã thấm xong -> AI lập lại kế hoạch với độ ẩm mới
    if not db.change_streams:            # Atlas: Change Stream trên irrigation_events lo việc học lại (ai.py)
        from .ai import on_irrigation_recorded
        asyncio.create_task(on_irrigation_recorded(rt.device_id, "irrigation_event"))
    rt.last_irrigation = ev
    if ev["status"] in ASSESSED:
        rt.last_assessed = ev

    tags = device_tags(rt.device_id, rt.site, rt.zone, rt.source)
    metrics.increment("irrigation.events", tags={**tags, "status": ev["status"], "reason": ev["reason"] or "none"})
    metrics.gauge("irrigation.liters", ev["liters"], tags)
    metrics.gauge("irrigation.response_pct_per_liter", ev["ratio_pct_per_l"], {**tags, "status": ev["status"]})
    metrics.gauge("irrigation.response_zscore", ev["zscore"], tags)
    metrics.gauge("irrigation.baseline_pct_per_liter", ev["baseline_pct_per_l"], tags)
    level = logging.WARNING if ev["status"] in ("low_response", "high_response") else logging.INFO
    log.log(level, "Irrigation assessed: %s", describe(ev) if level == logging.WARNING else ev["status"],
            extra={"device_id": rt.device_id, "irrigation_id": ev["irrigation_id"], "irrigation_status": ev["status"],
                   "liters": ev["liters"], "ratio_pct_per_l": ev["ratio_pct_per_l"], "zscore": ev["zscore"]})
    return ev


async def _status(site: str, device_id: str, data: dict, received: datetime) -> None:
    rt = runtime.get(device_id)
    online = data.get("online", True) is not False
    tags = device_tags(device_id, site, data.get("zone"), data.get("source"))
    if not online:
        rt.online = False
        await db.devices.update_one({"_id": device_id},
                                    {"$set": {"online": False, "offline_at": received,
                                              "offline_reason": data.get("reason", "lwt")}},
                                    upsert=True)
        await db.events.insert_one({"ts": received, "device_id": device_id, "site": site, "type": "offline",
                                    "detail": data.get("reason", "lwt")})
        metrics.gauge("device.online", 0, tags)
        log.warning("Device went offline (LWT)", extra={"device_id": device_id})
        return

    uptime = int(_num(data.get("uptime_s")) or 0)
    reason = str(data.get("reset_reason") or "unknown")
    rebooted = rt.last_uptime_s is not None and uptime < rt.last_uptime_s
    fresh_boot = uptime < 60 and (rt.last_uptime_s is None or rebooted)
    rt.last_uptime_s = uptime
    rt.online = True
    if rt.last_seen is None or rt.last_seen < received - timedelta(seconds=60):
        rt.last_seen = received

    status = {k: data.get(k) for k in ("fw_version", "uptime_s", "free_heap", "rssi_dbm", "ip", "reset_reason",
                                       "wifi_reconnects", "mqtt_reconnects", "dht_errors_total")}
    await db.devices.update_one(
        {"_id": device_id},
        {"$set": {"site": site, "online": True, "status": status, "status_at": received},
         "$setOnInsert": {"first_seen": received, "actuators.pump.mode": "AUTO", "actuators.mist.mode": "AUTO"}},
        upsert=True,
    )
    metrics.gauge("device.online", 1, tags)
    metrics.gauge("device.uptime", uptime, tags)
    metrics.gauge("device.free_heap", _num(data.get("free_heap")), tags)
    metrics.gauge("device.wifi_reconnects", _num(data.get("wifi_reconnects")), tags)
    metrics.gauge("device.mqtt_reconnects", _num(data.get("mqtt_reconnects")), tags)

    if rebooted or fresh_boot:
        await db.events.insert_one({"ts": received, "device_id": device_id, "site": site, "type": "rebooted",
                                    "detail": reason, "uptime_s": uptime})
        metrics.increment("device.reboots", tags={**tags, "reset_reason": reason})
        log.warning("Device rebooted", extra={"device_id": device_id, "reset_reason": reason})
        if reason == "brownout":
            await incidents.open_or_update(
                device_id, "power_brownout", "ESP32 khởi động lại do sụt áp (thường khi bơm khởi động)",
                detected_by="device", site=site, context={"reset_reason": reason, "uptime_s": uptime},
            )


# Thiết bị phát hiện lỗi sau một khoảng trễ -> lùi thời điểm bắt đầu lỗi để tính MTTD đúng
EVENT_DETECTION_DELAY_S = {"pump_dry_run": 68, "flow_without_pump": 10}

EVENT_INCIDENTS = {
    ("safety_lockout", "dry_run"): "pump_dry_run",
    ("max_runtime_cutoff", "pump"): "pump_overrun",
    ("max_runtime_cutoff", "mist"): "mist_overrun",
    ("flow_without_pump", None): "flow_without_pump",
}


async def _event(site: str, device_id: str, data: dict, received: datetime) -> None:
    type_ = str(data.get("type") or "unknown")
    ts, _ = _device_ts(data, received)
    doc = {"ts": ts, "device_id": device_id, "site": site, "type": type_,
           "actuator": data.get("actuator"), "detail": data.get("detail"), "flow_lpm": data.get("flow_lpm")}
    for k in ("cause", "down_s", "wifi_reason", "rssi_before", "reset_reason", "pulses", "edges", "seconds",
              "first_flow_ms", "soil_start", "soil_end", "relay_switch_ago_ms"):
        if data.get(k) is not None:
            doc[k] = data.get(k)
    await db.events.insert_one(doc)
    metrics.increment("device.events", tags={"device_id": device_id, "event_type": type_})
    if type_ == "reconnected":
        await _on_reconnected(site, device_id, data, ts)
        return
    if type_ == "pump_run":
        await _on_pump_run(site, device_id, data, ts)
        return
    log.info("Device event %s", type_, extra={"device_id": device_id, "event_type": type_,
                                               "detail": data.get("detail")})
    incident_type = (EVENT_INCIDENTS.get((type_, data.get("detail")))
                     or EVENT_INCIDENTS.get((type_, data.get("actuator")))
                     or EVENT_INCIDENTS.get((type_, None)))
    if incident_type:
        await incidents.open_or_update(
            device_id, incident_type, f"Thiết bị báo sự kiện an toàn: {type_} ({data.get('detail') or data.get('actuator')})",
            detected_by="device", site=site,
            started_at=ts - timedelta(seconds=EVENT_DETECTION_DELAY_S.get(incident_type, 0)), context={"event": type_, "flow_lpm": data.get("flow_lpm")},
        )


async def _attach_note(device_id: str, types: tuple[str, ...], note: str, context: dict, within_min: int = 30) -> None:
    """Ghi chẩn đoán vào sự cố đang mở (hoặc vừa đóng trong `within_min` phút) cùng loại của thiết bị."""
    inc = await db.incidents.find_one({"device_id": device_id, "type": {"$in": list(types)},
                                       "status": {"$ne": "resolved"}}, sort=[("opened_at", -1)])
    if not inc:
        inc = await db.incidents.find_one(
            {"device_id": device_id, "type": {"$in": list(types)}, "status": "resolved",
             "resolved_at": {"$gte": utcnow() - timedelta(minutes=within_min)}}, sort=[("resolved_at", -1)])
    if inc:
        await incidents.add_note(inc["_id"], "device", "diagnosis", note)
        await db.incidents.update_one({"_id": inc["_id"]}, {"$set": {"context.diagnosis": context}})


async def _on_reconnected(site: str, device_id: str, data: dict, ts: datetime) -> None:
    rt = runtime.get(device_id)
    tags = device_tags(device_id, site, data.get("zone"), data.get("source"))
    code, text = diagnose_reconnect(data)
    down = data.get("down_s")
    diag = {"cause": code, "text": text, "down_s": down, "rssi_before": data.get("rssi_before"),
            "wifi_reason": data.get("wifi_reason"), "reported_cause": data.get("cause"), "at": ts}
    rt.last_conn_diag = diag
    metrics.increment("device.reconnects", tags={**tags, "cause": code})
    if isinstance(down, (int, float)):
        metrics.histogram("device.disconnect_seconds", down, tags={**tags, "cause": code})
    if isinstance(data.get("rssi_before"), (int, float)):
        metrics.gauge("device.rssi_before_drop", data["rssi_before"], tags)
    await db.devices.update_one({"_id": device_id}, {"$set": {"diagnosis.connection": diag}})
    level = logging.INFO if code in ("power_loss",) and data.get("cause") == "boot" else logging.WARNING
    log.log(level, "Device reconnected: %s", text,
            extra={"device_id": device_id, "cause": code, "down_s": down, "rssi_before": data.get("rssi_before"),
                   "wifi_reason": data.get("wifi_reason"), "reset_reason": data.get("reset_reason")})
    if data.get("cause") != "boot" or code != "power_loss":
        metrics.event(f"[AgriGuard] {device_id} nối lại – {CAUSE_TEXT.get(code, code)}", text,
                      alert_type="warning", tags=[f"device_id:{device_id}", f"cause:{code}"])
    await _attach_note(device_id, ("device_offline", "power_brownout"), f"Chẩn đoán từ thiết bị: {text}", diag)


async def _on_pump_run(site: str, device_id: str, data: dict, ts: datetime) -> None:
    rt = runtime.get(device_id)
    tags = device_tags(device_id, site, data.get("zone"), data.get("source"))
    first = data.get("first_flow_ms")
    if isinstance(first, (int, float)) and first >= 0:
        metrics.histogram("pump.prime_ms", first, tags)
    metrics.gauge("pump.run_pulses", data.get("pulses"), tags)
    cfg = await db.get_settings_cached()
    res = diagnose_pump_run(data, cfg["pump"]["min_flow_lpm"])
    if not res:
        return
    code, text = res
    diag = {"cause": code, "text": text, "at": ts, "seconds": data.get("seconds"), "pulses": data.get("pulses"),
            "soil_start": data.get("soil_start"), "soil_end": data.get("soil_end")}
    rt.last_pump_diag = diag
    metrics.increment("pump.dry_run_diagnosis", tags={**tags, "cause": code})
    await db.devices.update_one({"_id": device_id}, {"$set": {"diagnosis.pump": diag}})
    log.warning("Pump run diagnosis: %s", text, extra={"device_id": device_id, "cause": code,
                                                       "pulses": data.get("pulses"), "seconds": data.get("seconds")})
    await _attach_note(device_id, ("pump_dry_run", "pump_low_flow"), f"Chẩn đoán lần bơm: {text}", diag)


def latency_p95(seconds: int = 60) -> float | None:
    cutoff = utcnow() - timedelta(seconds=seconds)
    values = sorted(v for t, v in recent_latencies if t >= cutoff)
    if not values:
        return None
    return values[min(len(values) - 1, int(len(values) * 0.95))]
