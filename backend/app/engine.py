"""Các vòng lặp nền: kiểm tra sức khoẻ (phát hiện sự cố), điều khiển tự động, giám sát lệnh."""
from __future__ import annotations

import asyncio
import logging

from .commands import commands
from .config import settings
from .control import decide_mist, decide_pump
from .db import as_utc, db, utcnow
from .incidents import incidents
from .metrics import device_tags, metrics
from .rules import INCIDENT_TYPES, evaluate, incident_type, sensor_health
from .state import runtime

log = logging.getLogger(__name__)

# Loại sự cố mà luật backend tự phát hiện (để đánh dấu "điều kiện lỗi đã hết")
RULE_TYPES = {"device_offline", "pump_dry_run", "flow_without_pump", "pump_overrun", "mist_overrun",
              "mist_ineffective", "dht_fault", "soil_sensor_fault", "soil_sensor_stuck"}


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
        health = sensor_health(rt, findings)
        if not offline:
            for sensor, state in health.items():
                metrics.gauge("sensor.health", int(state == "ok"), {**tags, "sensor": sensor})
            await db.devices.update_one({"_id": rt.device_id}, {"$set": {"health": health}})

        for f in findings:
            await incidents.open_or_update(rt.device_id, f.type, f.message, detected_by="rules",
                                           started_at=f.started_at, context=f.context, site=rt.site)
        for (device_id, type_), inc in active_by_key.items():
            if (device_id == rt.device_id and type_ in RULE_TYPES and type_ not in found
                    and inc["status"] != "verifying"):
                await incidents.mark_condition_cleared(inc)

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
        if not rt.online or rt.latest is None:
            continue
        if (now - rt.latest.ts).total_seconds() > cfg["health"]["offline_after_s"]:
            continue
        actuators = (devices.get(rt.device_id) or {}).get("actuators", {})
        for actuator, decide in (("pump", decide_pump), ("mist", decide_mist)):
            mode = (actuators.get(actuator) or {}).get("mode", "AUTO")
            is_on = getattr(rt.latest, f"{actuator}_on")
            blocked, verifying = _blocking(active, rt.device_id, actuator)
            if verifying or mode == "MANUAL":
                continue
            if await commands.has_pending(rt.device_id, actuator):
                continue
            decision = None
            if mode == "OFF" or blocked:
                if is_on:
                    reason = "mode_off" if mode == "OFF" else "incident_interlock"
                    await commands.send(rt.device_id, actuator, "off", reason=reason, actor="controller", site=rt.site)
                    metrics.increment("control.decision", tags={"actuator": actuator, "action": "off", "reason": reason})
                continue
            decision = decide(rt, cfg, now)
            if decision:
                await commands.send(rt.device_id, actuator, decision.action, duration_s=decision.duration_s,
                                    reason=decision.reason, actor="controller", site=rt.site)
                metrics.increment("control.decision", tags={"actuator": actuator, "action": decision.action,
                                                             "reason": decision.reason, "device_id": rt.device_id})


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
