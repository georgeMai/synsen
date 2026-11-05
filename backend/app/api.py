"""REST API cho web dashboard, Datadog webhook và bộ giả lập."""
from __future__ import annotations

import asyncio
import logging
import os
import secrets
from datetime import timedelta, timezone
from typing import Literal, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field

from . import ai, notify
from .analytics import water_report
from .commands import commands
from .config import settings
from .db import DEFAULT_SETTINGS, db, deep_merge, utcnow
from .incidents import ACTIVE, _parse_tags, incidents
from .irrigation import BASELINE_STATUSES, robust_baseline
from .metrics import metrics
from .mqtt_bridge import bridge
from .rules import INCIDENT_TYPES
from .verify import run_verification

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api")

LOCAL_TZ = timezone(timedelta(hours=7))  # Asia/Ho_Chi_Minh
TELEMETRY_INTERVAL_S = 5


# ---------------------------------------------------------------- auth
def require_admin(x_admin_token: Optional[str] = Header(default=None)) -> str:
    if settings.admin_token and not secrets.compare_digest(x_admin_token or "", settings.admin_token):
        raise HTTPException(status_code=401, detail="Cần ADMIN token hợp lệ (header X-Admin-Token)")
    return "operator"


async def audit(kind: str, actor: str, **data) -> None:
    await db.audit_log.insert_one({"ts": utcnow(), "actor": actor, "kind": kind, "data": data})


# ---------------------------------------------------------------- models
class CommandIn(BaseModel):
    action: Literal["on", "off", "clear_lockout"]
    duration_s: int = Field(default=0, ge=0, le=3600)


class ModeIn(BaseModel):
    mode: Literal["AUTO", "AI", "MANUAL", "OFF"]


class NoteIn(BaseModel):
    note: str = Field(default="", max_length=500)
    actor: str = Field(default="operator", max_length=60)


class PumpSettings(BaseModel):
    soil_dry_pct: float = Field(ge=5, le=90)
    soil_target_pct: float = Field(ge=10, le=100)
    hot_temp_c: float = Field(ge=15, le=50)
    max_seconds: int = Field(ge=1, le=3600)
    min_off_minutes: float = Field(ge=0, le=240)
    min_flow_lpm: float = Field(ge=0.05, le=10)
    dry_run_seconds: int = Field(ge=15, le=600)
    leak_seconds: int = Field(ge=5, le=600)
    verify_seconds: Optional[int] = Field(None, ge=3, le=120)


class MistSettings(BaseModel):
    hot_temp_c: float = Field(ge=15, le=50)
    temp_hysteresis_c: float = Field(ge=0, le=5)
    air_dry_pct: float = Field(ge=10, le=90)
    air_target_pct: float = Field(ge=20, le=95)
    air_max_pct: float = Field(ge=30, le=100)
    max_minutes: float = Field(ge=1, le=120)
    min_off_minutes: float = Field(ge=0, le=240)
    check_minutes: float = Field(ge=1, le=60)
    min_rise_pct: float = Field(ge=0.5, le=30)
    verify_rise_pct: Optional[float] = Field(None, ge=0.5, le=30)
    verify_temp_drop_c: Optional[float] = Field(None, ge=0.2, le=10)


class HealthSettings(BaseModel):
    offline_after_s: int = Field(ge=10, le=3600)
    stuck_minutes: float = Field(ge=2, le=15)
    sensor_fail_count: int = Field(ge=1, le=20)
    rssi_weak_dbm: int = Field(ge=-100, le=-40)
    low_flow_ratio: Optional[float] = Field(None, ge=0.2, le=0.95)


class AiSettings(BaseModel):
    horizon_hours: Optional[float] = Field(None, ge=1, le=24)
    step_minutes: Optional[int] = Field(None, ge=5, le=30)
    train_days: Optional[float] = Field(None, ge=1, le=30)
    min_samples: Optional[int] = Field(None, ge=10, le=2000)
    ridge_lambda: Optional[float] = Field(None, ge=0, le=100)
    retrain_hours: Optional[float] = Field(None, ge=0.5, le=168)
    plan_interval_s: Optional[int] = Field(None, ge=60, le=3600)
    plan_margin_pct: Optional[float] = Field(None, ge=0, le=30)
    stress_weight: Optional[float] = Field(None, ge=0, le=10)
    evap_penalty: Optional[float] = Field(None, ge=0, le=2)
    critical_margin_pct: Optional[float] = Field(None, ge=0, le=30)


class IrrigationSettings(BaseModel):
    settle_seconds: Optional[float] = Field(None, ge=10, le=1800)
    after_window_seconds: Optional[float] = Field(None, ge=5, le=300)
    before_seconds: Optional[float] = Field(None, ge=10, le=600)
    min_liters: Optional[float] = Field(None, ge=0.01, le=50)
    max_start_soil_pct: Optional[float] = Field(None, ge=30, le=100)
    exclude_if_mist: Optional[bool] = None
    baseline_events: Optional[int] = Field(None, ge=3, le=200)
    min_baseline_events: Optional[int] = Field(None, ge=2, le=100)
    zscore_threshold: Optional[float] = Field(None, ge=1, le=10)
    detect_high_response: Optional[bool] = None
    test_seconds: Optional[int] = Field(None, ge=2, le=300)   # chậu demo nhỏ: 3–4 giây


class SettingsIn(BaseModel):
    pump: Optional[PumpSettings] = None
    mist: Optional[MistSettings] = None
    health: Optional[HealthSettings] = None
    irrigation: Optional[IrrigationSettings] = None
    ai: Optional[AiSettings] = None


class FaultIn(BaseModel):
    fault: Literal["dry_run", "leak", "dht_fail", "soil_disconnect", "soil_stuck", "offline", "mist_empty",
                   "no_ack", "brownout", "slow_network", "hose_off", "clog", "flow_sensor_broken", "dry_soil", "clear"]
    on: bool = True


# ---------------------------------------------------------------- health
@router.get("/health")
async def health():
    try:
        await db.db.command("ping")
        mongo = "ok"
    except Exception as exc:  # pragma: no cover
        mongo = f"error: {exc}"
    return {"status": "ok" if mongo == "ok" else "degraded", "mongo": mongo, "timeseries": db.timeseries,
            "mqtt": "connected" if bridge.connected else "disconnected",
            "datadog_metrics": metrics.enabled, "time": utcnow(), "env": settings.env}


@router.get("/meta")
async def meta():
    return {"site": settings.site, "incident_types": {k: v.__dict__ for k, v in INCIDENT_TYPES.items()},
            "admin_required": bool(settings.admin_token), "sim_api": settings.enable_sim_api,
            "command_ack_timeout_s": settings.command_ack_timeout_s,
            "datadog_app": _dd_app_host() if metrics.enabled else None, "rum": rum_config()}


def rum_config() -> dict | None:
    """Cấu hình Datadog RUM cho web; None khi chưa đặt DD_RUM_APPLICATION_ID / DD_RUM_CLIENT_TOKEN."""
    if not (settings.rum_application_id and settings.rum_client_token):
        return None
    return {"applicationId": settings.rum_application_id, "clientToken": settings.rum_client_token,
            "site": (settings.dd_site or "datadoghq.com").strip().lower(), "service": "agriguard-web",
            "env": settings.env, "version": os.environ.get("DD_VERSION", "1.0.0"),
            "sessionSampleRate": max(0, min(100, settings.rum_session_sample)),
            "sessionReplaySampleRate": max(0, min(100, settings.rum_replay_sample)),
            "backendService": settings.service}


def _dd_app_host() -> str:
    site = (settings.dd_site or "datadoghq.com").strip().lower()
    return {"datadoghq.com": "app.datadoghq.com", "datadoghq.eu": "app.datadoghq.eu"}.get(site, site)


# ---------------------------------------------------------------- devices & telemetry
@router.get("/devices")
async def list_devices():
    return await db.devices.find({}).sort("_id", 1).to_list(200)


@router.get("/devices/{device_id}")
async def get_device(device_id: str):
    device = await db.devices.find_one({"_id": device_id})
    if not device:
        raise HTTPException(404, "Không tìm thấy thiết bị")
    return device


@router.get("/devices/{device_id}/telemetry")
async def telemetry(device_id: str, minutes: int = Query(60, ge=5, le=7 * 24 * 60)):
    start = utcnow() - timedelta(minutes=minutes)
    bucket_s = max(TELEMETRY_INTERVAL_S, (minutes * 60) // 240)
    match = {"meta.device_id": device_id, "ts": {"$gte": start}}
    fields = ("air_temp_c", "air_humidity_pct", "soil_moisture_pct", "flow_lpm")
    if bucket_s <= TELEMETRY_INTERVAL_S:
        rows = await db.telemetry.find(match, {"_id": 0, "ts": 1, "pump_on": 1, "mist_on": 1,
                                               **{f: 1 for f in fields}}).sort("ts", 1).to_list(5000)
        points = [{"t": int(r["ts"].timestamp() * 1000), **{f: r.get(f) for f in fields},
                   "pump_on": int(bool(r.get("pump_on"))), "mist_on": int(bool(r.get("mist_on")))} for r in rows]
    elif settings.mongodb_uri.startswith("memory://"):
        # mongomock không hỗ trợ $toLong với Date -> gom nhóm bằng Python (chỉ chế độ chạy thử)
        rows = await db.telemetry.find(match, {"_id": 0}).sort("ts", 1).to_list(200000)
        points = _bucket_in_python(rows, bucket_s, fields)
    else:
        bucket_ms = bucket_s * 1000
        ts_ms = {"$toLong": "$ts"}
        pipeline = [
            {"$match": match},
            {"$group": {
                "_id": {"$subtract": [ts_ms, {"$mod": [ts_ms, bucket_ms]}]},
                **{f: {"$avg": f"${f}"} for f in fields},
                "pump_on": {"$max": {"$cond": ["$pump_on", 1, 0]}},
                "mist_on": {"$max": {"$cond": ["$mist_on", 1, 0]}},
            }},
            {"$sort": {"_id": 1}},
        ]
        rows = await (await db.telemetry.aggregate(pipeline)).to_list(5000)
        points = [{"t": r["_id"], **{f: (round(r[f], 2) if r.get(f) is not None else None) for f in fields},
                   "pump_on": r["pump_on"], "mist_on": r["mist_on"]} for r in rows]
    return {"device_id": device_id, "minutes": minutes, "bucket_s": bucket_s, "points": points}


def _bucket_in_python(rows: list[dict], bucket_s: int, fields: tuple[str, ...]) -> list[dict]:
    buckets: dict[int, list[dict]] = {}
    for r in rows:
        t = int(r["ts"].timestamp() * 1000)
        buckets.setdefault(t - t % (bucket_s * 1000), []).append(r)
    out = []
    for t, items in sorted(buckets.items()):
        point = {"t": t, "pump_on": int(any(i.get("pump_on") for i in items)),
                 "mist_on": int(any(i.get("mist_on") for i in items))}
        for f in fields:
            vals = [i[f] for i in items if i.get(f) is not None]
            point[f] = round(sum(vals) / len(vals), 2) if vals else None
        out.append(point)
    return out


@router.get("/devices/{device_id}/stats/today")
async def stats_today(device_id: str):
    local_now = utcnow().astimezone(LOCAL_TZ)
    start = local_now.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
    pipeline = [
        {"$match": {"meta.device_id": device_id, "ts": {"$gte": start}}},
        {"$group": {"_id": None, "n": {"$sum": 1},
                    "pump_samples": {"$sum": {"$cond": ["$pump_on", 1, 0]}},
                    "mist_samples": {"$sum": {"$cond": ["$mist_on", 1, 0]}},
                    "flow_sum": {"$sum": "$flow_lpm"},
                    "temp_max": {"$max": "$air_temp_c"}, "temp_min": {"$min": "$air_temp_c"},
                    "soil_min": {"$min": "$soil_moisture_pct"}}},
    ]
    rows = await (await db.telemetry.aggregate(pipeline)).to_list(1)
    r = rows[0] if rows else {}
    pump_cmds = await db.commands.count_documents({"device_id": device_id, "actuator": "pump", "action": "on",
                                                    "created_at": {"$gte": start}})
    return {
        "since": start, "samples": r.get("n", 0),
        "water_liters": round((r.get("flow_sum") or 0) * TELEMETRY_INTERVAL_S / 60, 2),
        "pump_minutes": round(r.get("pump_samples", 0) * TELEMETRY_INTERVAL_S / 60, 1),
        "mist_minutes": round(r.get("mist_samples", 0) * TELEMETRY_INTERVAL_S / 60, 1),
        "temp_max": r.get("temp_max"), "temp_min": r.get("temp_min"), "soil_min": r.get("soil_min"),
        "pump_starts": pump_cmds,
    }


@router.get("/devices/{device_id}/irrigation")
async def device_irrigation(device_id: str, limit: int = Query(30, le=200)):
    """Các lần tưới gần nhất + mức phản hồi bình thường đã học (%/L)."""
    cfg = await db.get_settings()
    events = await db.irrigation_events.find({"device_id": device_id}).sort("ended_at", -1).limit(limit).to_list(limit)
    ratios = [e["ratio_pct_per_l"] for e in events
              if e.get("status") in BASELINE_STATUSES and e.get("ratio_pct_per_l") is not None]
    ratios = ratios[: int(cfg["irrigation"]["baseline_events"])]
    baseline = None
    if len(ratios) >= int(cfg["irrigation"]["min_baseline_events"]):
        med, scale = robust_baseline(ratios)
        baseline = {"median_pct_per_l": round(med, 2), "scale": round(scale, 2), "n": len(ratios)}
    return {"device_id": device_id, "baseline": baseline, "learning_needed": max(
        0, int(cfg["irrigation"]["min_baseline_events"]) - len(ratios)), "events": events}


@router.get("/devices/{device_id}/water/daily")
async def water_daily(device_id: str, days: int = Query(14, ge=1, le=90),
                      tank_liters: Optional[float] = Query(None, gt=0, le=1_000_000)):
    """Báo cáo nước theo ngày (MongoDB Aggregation Pipeline): lít, số lần tưới, nước lãng phí, tốc độ khô,
    trung bình 7 ngày, dự báo lít/ngày và số ngày bể trữ còn đủ. Trả kèm 2 pipeline đã chạy."""
    return await water_report(device_id, days, tank_liters)


@router.get("/devices/{device_id}/ai")
async def device_ai(device_id: str):
    """AI tưới: dự báo độ ẩm đất 6 giờ, lịch tưới tối ưu, nguy cơ, mô hình và độ tin cậy (sai số thực tế)."""
    dev = await db.devices.find_one({"_id": device_id}, {"ai": 1, "actuators": 1})
    if not dev:
        raise HTTPException(404, "Không tìm thấy thiết bị")
    last = await db.forecasts.find({"device_id": device_id}).sort("created_at", -1).limit(1).to_list(1)
    since = utcnow() - timedelta(hours=24)
    errors = await db.forecasts.find({"device_id": device_id, "created_at": {"$gte": since}, "error_1h": {"$ne": None}},
                                     {"created_at": 1, "soil_1h": 1, "actual_1h": 1, "error_1h": 1, "method": 1}
                                     ).sort("created_at", 1).to_list(500)
    models = await db.models.find({"device_id": device_id}, {"model": 0}).sort("trained_at", -1).limit(8).to_list(8)
    hist = await db.telemetry.find({"meta.device_id": device_id, "ts": {"$gte": utcnow() - timedelta(hours=2)},
                                    "soil_ok": True}, {"_id": 0, "ts": 1, "soil_moisture_pct": 1, "pump_on": 1}
                                   ).sort("ts", 1).to_list(5000)
    step = max(1, len(hist) // 120)
    end = utcnow()
    return {"device_id": device_id, "ai": dev.get("ai") or {}, "mode": (dev.get("actuators") or {}).get("pump", {}).get("mode"),
            "forecast": last[0] if last else None, "errors": errors, "models": models,
            "history": [{"t": h["ts"], "soil": h["soil_moisture_pct"], "pump_on": h.get("pump_on")} for h in hist[::step]],
            "change_streams": db.change_streams,
            "pipelines": {"features": ai.feature_pipeline(device_id, end - timedelta(days=7), end),
                          "weather_profile": ai.profile_pipeline(device_id, end - timedelta(days=7))}}


@router.post("/devices/{device_id}/ai/train")
async def device_ai_train(device_id: str, actor: str = Depends(require_admin)):
    """Học lại mô hình ngay, chạy nền theo từng bước (pipeline MongoDB → đặc trưng → Ridge → lưu → dự báo lại).
    Theo dõi tiến độ bằng GET /devices/{id}/ai/train/{job_id}."""
    if not await db.devices.find_one({"_id": device_id}, {"_id": 1}):
        raise HTTPException(404, "Không tìm thấy thiết bị")
    job = ai.start_train_job(device_id)
    await audit("ai_train", actor, device_id=device_id, job_id=job["job_id"])
    return job


@router.get("/devices/{device_id}/ai/train/{job_id}")
async def device_ai_train_status(device_id: str, job_id: str):
    job = ai.TRAIN_JOBS.get(job_id)
    if not job or job["device_id"] != device_id:
        raise HTTPException(404, "Không tìm thấy phiên học lại (backend có thể vừa khởi động lại)")
    return job


@router.get("/devices/{device_id}/events")
async def device_events(device_id: str, limit: int = Query(50, le=500)):
    return await db.events.find({"device_id": device_id}, {"_id": 0}).sort("ts", -1).limit(limit).to_list(limit)


# ---------------------------------------------------------------- control
@router.post("/devices/{device_id}/actuators/{actuator}/command")
async def send_command(device_id: str, actuator: Literal["pump", "mist"], body: CommandIn,
                       actor: str = Depends(require_admin)):
    device = await db.devices.find_one({"_id": device_id})
    if not device:
        raise HTTPException(404, "Không tìm thấy thiết bị")
    # Bật/tắt bằng tay -> chuyển sang MANUAL để bộ điều khiển tự động không ghi đè
    if body.action in ("on", "off") and (device.get("actuators", {}).get(actuator, {}).get("mode") in ("AUTO", "AI")):
        await db.devices.update_one({"_id": device_id}, {"$set": {f"actuators.{actuator}.mode": "MANUAL"}})
    duration = body.duration_s
    if actuator == "pump" and body.action == "on":
        # Bật tay cũng tuân theo "chạy tối đa mỗi lần" (vd. chậu demo 3 giây)
        limit = int((await db.get_settings())["pump"]["max_seconds"])
        duration = min(duration, limit) if duration else limit
    cmd = await commands.send(device_id, actuator, body.action, duration_s=duration,
                              reason="manual", actor=actor, site=device.get("site"))
    await audit("command", actor, device_id=device_id, actuator=actuator, action=body.action, command_id=cmd["_id"])
    return cmd


@router.put("/devices/{device_id}/actuators/{actuator}/mode")
async def set_mode(device_id: str, actuator: Literal["pump", "mist"], body: ModeIn,
                   actor: str = Depends(require_admin)):
    if body.mode == "AI" and actuator != "pump":
        raise HTTPException(400, "Chế độ AI chỉ áp dụng cho máy bơm")
    res = await db.devices.update_one({"_id": device_id}, {"$set": {f"actuators.{actuator}.mode": body.mode}})
    if not res.matched_count:
        raise HTTPException(404, "Không tìm thấy thiết bị")
    await audit("mode", actor, device_id=device_id, actuator=actuator, mode=body.mode)
    return {"device_id": device_id, "actuator": actuator, "mode": body.mode}


@router.get("/commands")
async def list_commands(device_id: Optional[str] = None, limit: int = Query(30, le=200)):
    query = {"device_id": device_id} if device_id else {}
    return await db.commands.find(query).sort("created_at", -1).limit(limit).to_list(limit)


@router.get("/settings")
async def get_settings():
    return await db.get_settings()


@router.put("/settings")
async def put_settings(body: SettingsIn, actor: str = Depends(require_admin)):
    current = await db.get_settings()
    patch = body.model_dump(exclude_none=True)
    merged = deep_merge(current, patch)
    if merged["pump"]["soil_dry_pct"] >= merged["pump"]["soil_target_pct"]:
        raise HTTPException(422, "Ngưỡng đất khô phải nhỏ hơn ngưỡng tưới đủ")
    if not merged["mist"]["air_dry_pct"] < merged["mist"]["air_target_pct"] <= merged["mist"]["air_max_pct"]:
        raise HTTPException(422, "Cần: độ ẩm KK khô < mục tiêu ≤ tối đa")
    await db.settings.update_one({"_id": settings.site},
                                 {"$set": {**{k: merged[k] for k in DEFAULT_SETTINGS},
                                           "updated_by": actor, "updated_at": utcnow()}}, upsert=True)
    await audit("settings", actor, patch=patch)
    log.info("Settings updated", extra={"actor": actor})
    return merged


# ---------------------------------------------------------------- incidents
@router.get("/incidents")
async def list_incidents(status: Literal["active", "resolved", "all"] = "active", device_id: Optional[str] = None,
                         limit: int = Query(100, le=500)):
    query: dict = {}
    if status == "active":
        query["status"] = {"$in": list(ACTIVE)}
    elif status == "resolved":
        query["status"] = "resolved"
    if device_id:
        query["device_id"] = device_id
    sort_key = "resolved_at" if status == "resolved" else "opened_at"
    return await db.incidents.find(query).sort(sort_key, -1).limit(limit).to_list(limit)


@router.get("/incidents/stats")
async def incident_stats():
    pipeline = [
        {"$match": {"status": "resolved"}},
        {"$group": {"_id": "$type", "count": {"$sum": 1}, "avg_mttd_s": {"$avg": "$mttd_s"},
                    "avg_mttr_s": {"$avg": "$mttr_s"}, "max_mttr_s": {"$max": "$mttr_s"}}},
        {"$sort": {"count": -1}},
    ]
    rows = await (await db.incidents.aggregate(pipeline)).to_list(100)
    active = await db.incidents.count_documents({"status": {"$in": list(ACTIVE)}})
    return {"active": active, "by_type": [{"type": r["_id"], **{k: v for k, v in r.items() if k != "_id"}}
                                          for r in rows]}


@router.get("/incidents/{incident_id}")
async def get_incident(incident_id: str):
    inc = await db.incidents.find_one({"_id": incident_id})
    if not inc:
        raise HTTPException(404, "Không tìm thấy sự cố")
    return inc


@router.post("/incidents/{incident_id}/acknowledge")
async def acknowledge(incident_id: str, body: NoteIn, _: str = Depends(require_admin)):
    inc = await incidents.acknowledge(incident_id, body.actor, body.note)
    if not inc:
        raise HTTPException(404, "Không tìm thấy sự cố")
    await audit("incident_ack", body.actor, incident_id=incident_id)
    return inc


@router.post("/incidents/{incident_id}/resolve")
async def resolve(incident_id: str, body: NoteIn, _: str = Depends(require_admin)):
    inc = await incidents.request_resolve(incident_id, body.actor, body.note)
    if not inc:
        raise HTTPException(409, "Sự cố không ở trạng thái có thể xử lý")
    asyncio.create_task(run_verification(incident_id))
    await audit("incident_resolve", body.actor, incident_id=incident_id, note=body.note)
    return inc


@router.post("/incidents/{incident_id}/note")
async def add_note(incident_id: str, body: NoteIn, _: str = Depends(require_admin)):
    await incidents.add_note(incident_id, body.actor, "note", body.note)
    return await get_incident(incident_id)


# ---------------------------------------------------------------- Datadog webhook
@router.post("/webhooks/datadog")
async def datadog_webhook(payload: dict, token: str = Query("")):
    if not settings.webhook_token or not secrets.compare_digest(token, settings.webhook_token):
        raise HTTPException(401, "Sai token webhook")
    result = await incidents.on_datadog_webhook(payload)
    metrics.increment("webhook.datadog", tags={"transition": str(payload.get("transition", "")).lower()})
    log.info("Datadog webhook", extra={"transition": payload.get("transition"), "scope": payload.get("scope"),
                                       "monitor_id": payload.get("monitor_id"), "result": str(result)})
    return result


@router.post("/webhooks/datadog/notify")
async def datadog_notify(payload: dict, token: str = Query("")):
    """Webhook @webhook-agriguard-notify: chuyển cảnh báo Datadog sang Zalo / WhatsApp, kèm chẩn đoán nguyên nhân."""
    if not settings.webhook_token or not secrets.compare_digest(token, settings.webhook_token):
        raise HTTPException(401, "Sai token webhook")
    tags = _parse_tags(payload.get("tags", "")) | _parse_tags(payload.get("scope", ""))
    device_id = tags.get("device_id", "")
    transition = (payload.get("transition") or "").lower()
    icon = "✅" if "recover" in transition else "⚠️" if transition in ("warn", "no data") else "🚨"
    lines = [f"{icon} AgriGuard: {payload.get('title') or 'Datadog monitor'}"]
    if device_id:
        lines.append(f"Thiết bị: {device_id}")
        dev = await db.devices.find_one({"_id": device_id}, {"diagnosis": 1}) or {}
        diag = dev.get("diagnosis") or {}
        kind = tags.get("incident_type", "")
        d = diag.get("pump") if kind.startswith("pump") or "pump" in kind else diag.get("connection")
        if d and "recover" not in transition:
            lines.append(f"Chẩn đoán: {d.get('text')}")
    if payload.get("link"):
        lines.append(f"Datadog: {payload['link']}")
    if settings.public_base_url:
        lines.append(f"Web: {settings.public_base_url.rstrip('/')}/")
    result = await notify.send("\n".join(lines), key=f"{payload.get('monitor_id')}:{device_id}:{transition}")
    metrics.increment("notify.sent", tags={"transition": transition or "unknown", "channels": ",".join(notify.channels()) or "none"})
    log.info("Datadog notify", extra={"transition": transition, "device_id": device_id, "result": str(result)})
    return result


# ---------------------------------------------------------------- simulator fault injection
@router.post("/sim/{device_id}/fault")
async def sim_fault(device_id: str, body: FaultIn, actor: str = Depends(require_admin)):
    if not settings.enable_sim_api:
        raise HTTPException(403, "API giả lập đang tắt")
    device = await db.devices.find_one({"_id": device_id})
    if not device or device.get("source") != "sim":
        raise HTTPException(400, "Chỉ tiêm lỗi được vào thiết bị giả lập (source=sim)")
    bridge.publish(bridge.sim_fault_topic(device.get("site", settings.site), device_id),
                   {"fault": body.fault, "on": body.on})
    await audit("sim_fault", actor, device_id=device_id, fault=body.fault, on=body.on)
    log.info("Simulator fault injected", extra={"device_id": device_id, "fault": body.fault, "on": body.on})
    return {"device_id": device_id, "fault": body.fault, "on": body.on, "at": utcnow()}
