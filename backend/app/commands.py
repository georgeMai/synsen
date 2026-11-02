"""Gửi lệnh điều khiển qua MQTT, theo dõi ack và timeout."""
from __future__ import annotations

import logging
import secrets
from datetime import datetime, timedelta

from .config import settings
from .db import as_utc, db, utcnow
from .metrics import metrics
from .mqtt_bridge import bridge

log = logging.getLogger(__name__)

ACTUATORS = ("pump", "mist")
ACTIONS = ("on", "off", "clear_lockout")
PENDING = ("pending", "sent")


class CommandService:
    async def send(self, device_id: str, actuator: str, action: str, duration_s: int = 0,
                   reason: str = "manual", actor: str = "operator", site: str | None = None) -> dict:
        if actuator not in ACTUATORS or action not in ACTIONS:
            raise ValueError("unknown actuator/action")
        if site is None:
            device = await db.devices.find_one({"_id": device_id}, {"site": 1})
            site = (device or {}).get("site", settings.site)
        now = utcnow()
        cid = f"c-{now:%H%M%S}-{secrets.token_hex(3)}"
        doc = {
            "_id": cid, "device_id": device_id, "site": site, "actuator": actuator, "action": action,
            "duration_s": int(duration_s or 0), "reason": reason, "actor": actor,
            "status": "pending", "created_at": now,
        }
        await db.commands.insert_one(doc)
        payload = {"command_id": cid, "actuator": actuator, "action": action}
        if duration_s:
            payload["duration_s"] = int(duration_s)
        ok = bridge.publish(bridge.command_topic(site, device_id), payload)
        status = "sent" if ok else "failed"
        await db.commands.update_one({"_id": cid}, {"$set": {"status": status, "sent_at": utcnow()}})
        doc.update(status=status, sent_at=utcnow())
        tags = {"device_id": device_id, "actuator": actuator, "action": action, "reason": reason}
        metrics.increment("command.sent", tags=tags)
        if not ok:
            metrics.increment("command.publish_failed", tags=tags)
        log.info("Command %s %s -> %s", actuator, action, device_id,
                 extra={"command_id": cid, "device_id": device_id, "reason": reason, "actor": actor, "status": status})
        return doc

    async def has_pending(self, device_id: str, actuator: str) -> bool:
        since = utcnow() - timedelta(seconds=settings.command_ack_timeout_s)
        doc = await db.commands.find_one(
            {"device_id": device_id, "actuator": actuator, "status": {"$in": list(PENDING)},
             "created_at": {"$gte": since}}
        )
        return doc is not None

    async def on_ack(self, device_id: str, data: dict, received: datetime) -> None:
        cid = data.get("command_id")
        cmd = await db.commands.find_one({"_id": cid}) if cid else None
        if not cmd:
            metrics.increment("command.ack_unknown", tags={"device_id": device_id})
            return
        sent_at = as_utc(cmd.get("sent_at") or cmd["created_at"])
        latency_ms = max(0.0, (received - sent_at).total_seconds() * 1000)
        ok = bool(data.get("ok", True))
        late = cmd.get("status") == "timeout"
        update = {
            "status": "acked" if ok else "rejected", "acked_at": received, "latency_ms": round(latency_ms),
            "ack": {k: data.get(k) for k in ("state", "lockout", "reason") if k in data},
        }
        if late:
            update["late_ack"] = True
        await db.commands.update_one({"_id": cid}, {"$set": update})
        if ok and data.get("state") in ("on", "off") and cmd.get("actuator") in ("pump", "mist"):
            # Ack mang trạng thái thật của relay -> web thấy ngay, không chờ bản tin telemetry kế tiếp
            act = cmd["actuator"]
            state = {f"actuators.{act}.state": data["state"]}
            if "lockout" in data:
                state[f"actuators.{act}.lockout"] = bool(data["lockout"])
            await db.devices.update_one({"_id": device_id}, {"$set": state})
        tags = {"device_id": device_id, "actuator": cmd["actuator"], "action": cmd["action"]}
        metrics.histogram("command.ack_latency_ms", latency_ms, tags=tags)
        metrics.increment("command.acked" if ok else "command.rejected", tags=tags)
        if ok and not late and latency_ms <= settings.command_ack_timeout_s * 1000:
            metrics.increment("command.acked_in_time", tags=tags)
        log.info("Command ack", extra={"command_id": cid, "device_id": device_id, "ok": ok,
                                       "latency_ms": round(latency_ms), "ack_reason": data.get("reason")})

    async def check_timeouts(self) -> list[dict]:
        cutoff = utcnow() - timedelta(seconds=settings.command_ack_timeout_s)
        expired = await db.commands.find({"status": "sent", "sent_at": {"$lt": cutoff}}).to_list(100)
        for cmd in expired:
            await db.commands.update_one({"_id": cmd["_id"], "status": "sent"},
                                         {"$set": {"status": "timeout", "timeout_at": utcnow()}})
            metrics.increment("command.timeout", tags={"device_id": cmd["device_id"], "actuator": cmd["actuator"],
                                                       "action": cmd["action"]})
            log.warning("Command not acknowledged", extra={"command_id": cmd["_id"], "device_id": cmd["device_id"]})
        return expired


commands = CommandService()
