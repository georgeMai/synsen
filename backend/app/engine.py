"""Các vòng lặp nền: kiểm tra sức khoẻ (phát hiện sự cố), điều khiển tự động, giám sát lệnh."""
from __future__ import annotations

import asyncio
import logging

from .commands import commands
from .config import settings
from .control import decide_mist, decide_pump, decide_pump_ai, idle_reason
from .db import as_utc, db, utcnow
from .diagnose import offline_hypothesis
from .incidents import incidents
from .irrigation import ASSESSED
from .prediction import dry_at, predict
from .metrics import device_tags, metrics
from .rules import INCIDENT_TYPES, evaluate, incident_type, sensor_health
from .state import runtime

log = logging.getLogger(__name__)

# Loại sự cố mà luật backend tự phát hiện (để đánh dấu "điều kiện lỗi đã hết")
RULE_TYPES = {"device_offline", "pump_dry_run", "pump_low_flow", "flow_without_pump", "pump_overrun", "mist_overrun",
              "mist_ineffective", "dht_fault", "soil_sensor_fault", "soil_sensor_stuck",
              "irrigation_ineffective", "soil_response_high"}


async def health_tick() -> None:
    cfg = await db.get_settings()
    now = utcnow()
    active = await incidents.active()
    active_by_key = {(i["device_id"], i["type"]): i for i in active}

    for rt in runtime.all():
        findings = evaluate(rt, cfg, now)
        found = {f.type for f in findings}
        tags = device_tags(rt.device_id, rt.site, rt.zone, rt.source)

        offline = "device_offline" in found
        metrics.gauge("device.online", 0 if offline else 1, tags)
        if offline and rt.online:
            rt.online = False
        if offline:
            await db.devices.update_one({"_id": rt.device_id, "online": True},
                                        {"$set": {"online": False, "offline_at": now}})

        for fault in RULE_TYPES:
            metrics.gauge("fault.active", int(fault in found), {**tags, "fault": fault})
        ev = rt.last_assessed      # giữ chuỗi liên tục cho monitor Datadog (sự kiện tưới thì thưa)
        if ev:
            metrics.gauge("irrigation.response_zscore", ev.get("zscore"), tags)
            metrics.gauge("irrigation.baseline_pct_per_liter", ev.get("baseline_pct_per_l"), tags)
        health = sensor_health(rt, findings)
        if not offline:
            for sensor, state in health.items():
                metrics.gauge("sensor.health", int(state == "ok"), {**tags, "sensor": sensor})
            pred = predict(rt, cfg, now)
            metrics.gauge("soil.drying_pct_per_hour", pred["drying_pct_per_hour"], tags)
            metrics.gauge("soil.minutes_to_dry", pred["minutes_to_dry"], tags)
            metrics.gauge("irrigation.predicted_liters_next", pred["liters_next_irrigation"], tags)
            metrics.gauge("water.predicted_liters_per_day", pred["liters_per_day"], tags)
            pred["dry_at"] = dry_at(now, pred["minutes_to_dry"])
            pred["at"] = now
            await db.devices.update_one({"_id": rt.device_id}, {"$set": {"health": health, "prediction": pred}})

        for f in findings:
            inc, created = await incidents.open_or_update(rt.device_id, f.type, f.message, detected_by="rules",
                                                          started_at=f.started_at, context=f.context, site=rt.site)
            if created:
                await _initial_diagnosis(rt, inc, now)
        for (device_id, type_), inc in active_by_key.items():
            if (device_id == rt.device_id and type_ in RULE_TYPES and type_ not in found
                    and inc["status"] != "verifying"):
                was_active = inc.get("condition_active", True)
                await incidents.mark_condition_cleared(inc)
                if type_ == "device_offline" and was_active:
                    await incidents.close_offline_drop(inc, cfg["health"])

    # Thiết bị đã tắt cảnh báo (ALERT_MUTED_DEVICES) -> đóng các sự cố còn mở của thiết bị đó
    if settings.alert_muted_devices:
        for inc in active:
            if inc["device_id"] in settings.alert_muted_devices and inc["status"] != "verifying":
                await incidents.close_muted(inc)

    # Cảnh báo "đất ẩm lên bất thường" đã TẮT trong Cài đặt (chậu nhỏ) -> đóng các sự cố loại này còn mở
    if not cfg["irrigation"].get("detect_high_response", True):
        for inc in active:
            if inc["type"] == "soil_response_high" and inc["status"] != "verifying":
                await incidents.mark_resolved(inc["_id"], "Cảnh báo “đất ẩm lên bất thường” đã tắt trong Cài đặt ngưỡng "
                                                          "(chậu nhỏ, đất ẩm lên nhanh) – đóng sự cố")

    # Lệnh không được xác nhận
    for cmd in await commands.check_timeouts():
        if cmd.get("reason") == "verification":
            continue
        await incidents.open_or_update(
            cmd["device_id"], "command_timeout",
            f"Lệnh {cmd['actuator']} {cmd['action']} ({cmd['_id']}) không được xác nhận sau "
            f"{settings.command_ack_timeout_s} giây",
            detected_by="rules", site=cmd.get("site"),
            context={"command_id": cmd["_id"], "actuator": cmd["actuator"]},
        )

    # Số incident đang mở theo loại/mức độ
    counts: dict[tuple[str, str], int] = {(t, m.severity): 0 for t, m in INCIDENT_TYPES.items()}
    for inc in await incidents.active():
        key = (inc["type"], inc.get("severity", incident_type(inc["type"]).severity))
        counts[key] = counts.get(key, 0) + 1
    for (type_, severity), n in counts.items():
        metrics.gauge("incident.open", n, {"type": type_, "severity": severity})


async def _initial_diagnosis(rt, inc: dict, now) -> None:
    """Ghi chẩn đoán ban đầu vào sự cố vừa mở (mất kết nối, bơm chạy khô)."""
    if inc["type"] == "device_offline":
        note = offline_hypothesis(list(rt.readings), now)
        await incidents.add_note(inc["_id"], "system", "diagnosis", note)
    elif inc["type"] in ("pump_dry_run", "pump_low_flow"):
        d = rt.last_pump_diag
        if d and (now - d["at"]).total_seconds() <= 300:
            await incidents.add_note(inc["_id"], "device", "diagnosis", f"Chẩn đoán lần bơm: {d['text']}")
            await db.incidents.update_one({"_id": inc["_id"]}, {"$set": {"context.diagnosis": d}})


def _blocking(active: list[dict], device_id: str, actuator: str) -> tuple[bool, bool]:
    """(bị khoá bởi incident đang mở, đang trong quá trình kiểm chứng)."""
    blocked = verifying = False
    for inc in active:
        if inc["device_id"] != device_id or actuator not in incident_type(inc["type"]).blocks:
            continue
        if inc["status"] == "verifying":
            verifying = True
        else:
            blocked = True
    return blocked, verifying


async def control_tick() -> None:
    cfg = await db.get_settings()
    now = utcnow()
    active = await incidents.active()
    devices = {d["_id"]: d async for d in db.devices.find({}, {"actuators": 1, "online": 1})}

    for rt in runtime.all():
        status: dict[str, dict] = {}
        if not rt.online or rt.latest is None:
            status = {a: {"reason": "offline"} for a in ("pump", "mist")}
        elif (now - rt.latest.ts).total_seconds() > cfg["health"]["offline_after_s"]:
            status = {a: {"reason": "stale_data"} for a in ("pump", "mist")}
        else:
            actuators = (devices.get(rt.device_id) or {}).get("actuators", {})
            for actuator, decide in (("pump", decide_pump), ("mist", decide_mist)):
                status[actuator] = await _control_one(rt, cfg, now, active, actuators, actuator, decide)
        # Lưu lý do để web hiển thị "vì sao chưa bật" (chỉ ghi khi đổi, tránh ghi MongoDB mỗi giây)
        snapshot = {a: (v.get("reason"), str(v.get("start_at")), v.get("liters")) for a, v in status.items()}
        if snapshot != getattr(rt, "_control_snapshot", None):
            rt._control_snapshot = snapshot
            await db.devices.update_one({"_id": rt.device_id},
                                        {"$set": {f"control.{a}": {**v, "at": now} for a, v in status.items()}})


async def _control_one(rt, cfg, now, active, actuators, actuator, decide) -> dict:
    mode = (actuators.get(actuator) or {}).get("mode", "AUTO")
    is_on = getattr(rt.latest, f"{actuator}_on")
    blocked, verifying = _blocking(active, rt.device_id, actuator)
    if verifying:
        return {"reason": "verifying"}
    if mode == "MANUAL":
        return {"reason": "mode_manual"}
    if await commands.has_pending(rt.device_id, actuator):
        return {"reason": "pending_command"}
    if mode == "OFF" or blocked:
        if is_on:
            reason = "mode_off" if mode == "OFF" else "incident_interlock"
            await commands.send(rt.device_id, actuator, "off", reason=reason, actor="controller", site=rt.site)
            metrics.increment("control.decision", tags={"actuator": actuator, "action": "off", "reason": reason})
        if mode == "OFF":
            return {"reason": "mode_off"}
        types = sorted({i["type"] for i in active if i["device_id"] == rt.device_id
                        and actuator in incident_type(i["type"]).blocks})
        return {"reason": "incident_block", "incidents": types}
    extra: dict = {}
    if mode == "AI" and actuator == "pump":
        decision, extra = decide_pump_ai(rt, cfg, now)
    else:
        decision = decide(rt, cfg, now)
    if decision:
        await commands.send(rt.device_id, actuator, decision.action, duration_s=decision.duration_s,
                            reason=decision.reason, actor="controller", site=rt.site)
        metrics.increment("control.decision", tags={"actuator": actuator, "action": decision.action,
                                                     "reason": decision.reason, "device_id": rt.device_id})
        return {"reason": f"decided_{decision.action}", "decision": decision.reason}
    if extra:
        return extra
    return {"reason": idle_reason(actuator, rt, cfg, now)}


async def run_forever(name: str, tick, interval_s: float) -> None:
    while True:
        try:
            await tick()
        except asyncio.CancelledError:
            raise
        except Exception:
            metrics.increment("engine.errors", tags={"loop": name})
            log.exception("Background loop %s failed", name)
        await asyncio.sleep(interval_s)


async def bootstrap_runtime() -> None:
    """Nạp lại trạng thái online/uptime sau khi backend khởi động lại."""
    async for d in db.devices.find({}):
        rt = runtime.get(d["_id"])
        rt.site, rt.zone, rt.source = d.get("site", ""), d.get("zone", ""), d.get("source", "real")
        rt.last_seen = as_utc(d.get("last_seen"))
        rt.online = bool(d.get("online"))  # dữ liệu cũ hơn offline_after_s sẽ bị luật coi là offline
        last = await db.irrigation_events.find({"device_id": d["_id"]}).sort("ended_at", -1).limit(1).to_list(1)
        assessed = await db.irrigation_events.find(
            {"device_id": d["_id"], "status": {"$in": list(ASSESSED)}}).sort("ended_at", -1).limit(1).to_list(1)
        rt.last_irrigation = last[0] if last else None
        rt.last_assessed = assessed[0] if assessed else None
        for ev in filter(None, (rt.last_irrigation, rt.last_assessed)):
            for k in ("started_at", "ended_at"):
                ev[k] = as_utc(ev.get(k))
