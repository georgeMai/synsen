"""Vòng đời sự cố: open -> acknowledged -> verifying -> resolved.

- Phát hiện (Detect): luật trong backend, sự kiện an toàn từ ESP32, hoặc monitor Datadog (webhook).
  Cùng một lỗi trên cùng thiết bị chỉ có MỘT incident đang mở; các nguồn phát hiện được gộp lại.
- Điều tra (Investigate): timeline + context + liên kết monitor Datadog.
- Khắc phục (Resolve): người vận hành bấm "Đã xử lý" -> hệ thống tự kiểm chứng bằng dữ liệu
  (xem verify.py) rồi mới đóng. Kiểm chứng thất bại -> incident quay lại trạng thái acknowledged.
"""
from __future__ import annotations

import logging
import secrets
from datetime import datetime
from typing import Any

from .db import as_utc, db, utcnow
from .metrics import metrics
from .rules import incident_type

log = logging.getLogger(__name__)

ACTIVE = ("open", "acknowledged", "verifying")


def _entry(actor: str, action: str, note: str = "", **extra) -> dict:
    return {"at": utcnow(), "actor": actor, "action": action, "note": note, **extra}


class IncidentService:
    async def find_active(self, device_id: str, type_: str) -> dict | None:
        return await db.incidents.find_one({"device_id": device_id, "type": type_, "status": {"$in": list(ACTIVE)}})

    async def active(self) -> list[dict]:
        return await db.incidents.find({"status": {"$in": list(ACTIVE)}}).sort("opened_at", -1).to_list(500)

    async def open_or_update(self, device_id: str, type_: str, message: str, *, detected_by: str,
                             started_at: datetime | None = None, context: dict | None = None,
                             site: str | None = None, datadog: dict | None = None) -> tuple[dict, bool]:
        now = utcnow()
        existing = await self.find_active(device_id, type_)
        if existing:
            update: dict[str, Any] = {"$set": {"last_detected_at": now, "condition_active": True}}
            pushes = []
            if detected_by not in existing.get("detected_by", []):
                update["$addToSet"] = {"detected_by": detected_by}
                pushes.append(_entry(detected_by, "detected", message))
            elif not existing.get("condition_active", True):
                pushes.append(_entry("system", "condition_returned", message))
            if datadog:
                update["$set"]["datadog"] = {**existing.get("datadog", {}), **datadog}
            if pushes:
                update["$push"] = {"timeline": {"$each": pushes}}
            await db.incidents.update_one({"_id": existing["_id"]}, update)
            return existing, False

        meta = incident_type(type_)
        started = as_utc(started_at) or now
        doc = {
            "_id": f"INC-{now:%y%m%d-%H%M%S}-{secrets.token_hex(2)}",
            "device_id": device_id, "site": site, "type": type_, "title": meta.title,
            "severity": meta.severity, "status": "open", "detected_by": [detected_by],
            "message": message, "context": context or {}, "condition_active": True,
            "fault_started_at": started, "opened_at": now, "last_detected_at": now,
            "mttd_s": max(0, round((now - started).total_seconds())),
            "timeline": [_entry(detected_by, "opened", message)],
        }
        if datadog:
            doc["datadog"] = datadog
        await db.incidents.insert_one(doc)
        tags = {"device_id": device_id, "type": type_, "severity": meta.severity, "detected_by": detected_by}
        metrics.increment("incident.opened", tags=tags)
        metrics.histogram("incident.mttd_seconds", doc["mttd_s"], tags=tags)
        metrics.event(f"[AgriGuard] {meta.title} – {device_id}", message,
                      alert_type="error" if meta.severity == "critical" else "warning",
                      tags=[f"device_id:{device_id}", f"incident_type:{type_}", f"incident_id:{doc['_id']}"])
        log.warning("Incident opened: %s", meta.title,
                    extra={"incident_id": doc["_id"], "device_id": device_id, "incident_type": type_,
                           "detected_by": detected_by, "mttd_s": doc["mttd_s"]})
        return doc, True

    async def mark_condition_cleared(self, incident: dict) -> None:
        if incident.get("condition_active", True):
            await db.incidents.update_one(
                {"_id": incident["_id"]},
                {"$set": {"condition_active": False, "condition_cleared_at": utcnow()},
                 "$push": {"timeline": _entry("system", "condition_cleared",
                                              "Dữ liệu không còn cho thấy lỗi – chờ người vận hành xác nhận")}},
            )

    async def acknowledge(self, incident_id: str, actor: str, note: str = "") -> dict | None:
        inc = await db.incidents.find_one({"_id": incident_id})
        if not inc or inc["status"] not in ("open",):
            return inc
        now = utcnow()
        await db.incidents.update_one(
            {"_id": incident_id},
            {"$set": {"status": "acknowledged", "acknowledged_at": now, "acknowledged_by": actor},
             "$push": {"timeline": _entry(actor, "acknowledged", note)}},
        )
        metrics.histogram("incident.time_to_ack_seconds", (now - as_utc(inc["opened_at"])).total_seconds(),
                          tags={"type": inc["type"]})
        return await db.incidents.find_one({"_id": incident_id})

    async def request_resolve(self, incident_id: str, actor: str, note: str = "") -> dict | None:
        inc = await db.incidents.find_one({"_id": incident_id})
        if not inc or inc["status"] not in ("open", "acknowledged"):
            return None
        sets = {"status": "verifying", "resolve_requested_at": utcnow(), "resolve_requested_by": actor,
                "resolution_note": note}
        if inc["status"] == "open":
            sets.update(acknowledged_at=utcnow(), acknowledged_by=actor)
        await db.incidents.update_one(
            {"_id": incident_id},
            {"$set": sets, "$push": {"timeline": _entry(actor, "resolve_requested", note)}},
        )
        return await db.incidents.find_one({"_id": incident_id})

    async def add_note(self, incident_id: str, actor: str, action: str, note: str, **extra) -> None:
        await db.incidents.update_one({"_id": incident_id}, {"$push": {"timeline": _entry(actor, action, note, **extra)}})

    async def mark_resolved(self, incident_id: str, evidence: str) -> None:
        inc = await db.incidents.find_one({"_id": incident_id})
        if not inc:
            return
        now = utcnow()
        mttr = round((now - as_utc(inc["opened_at"])).total_seconds())
        await db.incidents.update_one(
            {"_id": incident_id},
            {"$set": {"status": "resolved", "resolved_at": now, "mttr_s": mttr, "verification": evidence,
                      "condition_active": False},
             "$push": {"timeline": _entry("system", "verified", evidence)}},
        )
        tags = {"device_id": inc["device_id"], "type": inc["type"], "severity": inc["severity"]}
        metrics.increment("incident.resolved", tags=tags)
        metrics.histogram("incident.mttr_seconds", mttr, tags=tags)
        metrics.event(f"[AgriGuard] Đã khắc phục: {inc['title']} – {inc['device_id']}",
                      f"{evidence}. MTTR {mttr}s", alert_type="success",
                      tags=[f"device_id:{inc['device_id']}", f"incident_type:{inc['type']}", f"incident_id:{incident_id}"])
        log.info("Incident resolved", extra={"incident_id": incident_id, "device_id": inc["device_id"],
                                             "incident_type": inc["type"], "mttr_s": mttr})

    async def verification_failed(self, incident_id: str, reason: str) -> None:
        await db.incidents.update_one(
            {"_id": incident_id},
            {"$set": {"status": "acknowledged"},
             "$inc": {"verification_failures": 1},
             "$push": {"timeline": _entry("system", "verification_failed", reason)}},
        )
        inc = await db.incidents.find_one({"_id": incident_id}, {"type": 1, "device_id": 1})
        metrics.increment("incident.verification_failed", tags={"type": (inc or {}).get("type")})
        log.warning("Incident verification failed", extra={"incident_id": incident_id, "reason": reason})

    async def on_datadog_webhook(self, payload: dict) -> dict:
        """Payload do webhook Datadog gửi (định nghĩa trong datadog/bootstrap.py)."""
        tags = _parse_tags(payload.get("tags", "")) | _parse_tags(payload.get("scope", ""))
        type_ = tags.get("incident_type") or "datadog_alert"
        device_id = tags.get("device_id") or "pipeline"
        transition = (payload.get("transition") or "").lower()
        dd = {k: payload.get(k) for k in ("monitor_id", "link", "title") if payload.get(k)}
        dd["last_transition"] = transition
        if transition in ("triggered", "re-triggered", "warn", "no data"):
            inc, created = await self.open_or_update(
                device_id, type_, payload.get("title") or "Datadog monitor triggered",
                detected_by="datadog", datadog=dd, context={"scope": payload.get("scope")},
            )
            return {"incident_id": inc["_id"], "created": created}
        if transition in ("recovered", "no data recovered"):
            inc = await self.find_active(device_id, type_)
            if inc:
                await db.incidents.update_one({"_id": inc["_id"]}, {"$set": {"datadog": {**inc.get("datadog", {}), **dd}}})
                await self.add_note(inc["_id"], "datadog", "monitor_recovered",
                                    "Monitor Datadog đã trở về OK – chờ người vận hành xác nhận")
                return {"incident_id": inc["_id"], "created": False}
        return {"ignored": transition}


def _parse_tags(raw: str | list) -> dict[str, str]:
    items = raw if isinstance(raw, list) else str(raw).replace(" ", "").split(",")
    out = {}
    for item in items:
        if ":" in item:
            k, v = item.split(":", 1)
            out[k] = v
    return out


incidents = IncidentService()
