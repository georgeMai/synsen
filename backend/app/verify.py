"""Kiểm chứng khắc phục: sau khi người vận hành bấm "Đã xử lý", hệ thống tự kiểm tra
bằng dữ liệu thật trước khi đóng incident (bước cuối của Detect – Investigate – Resolve).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable

from .commands import commands
from .config import settings
from .db import db, utcnow
from .incidents import incidents
from .ingest import latency_p95
from .rules import PUMP_FLOW_GRACE_S
from .state import runtime

log = logging.getLogger(__name__)

Result = tuple[bool, str]


async def _wait_for(predicate: Callable[[], bool], timeout_s: float, interval: float = 1.0) -> bool:
    deadline = asyncio.get_running_loop().time() + timeout_s
    while True:
        if predicate():
            return True
        if asyncio.get_running_loop().time() >= deadline:
            return False
        await asyncio.sleep(interval)


def _fresh(device_id: str, max_age_s: float = 15) -> bool:
    rt = runtime.get(device_id)
    return bool(rt.online and rt.last_seen and (utcnow() - rt.last_seen).total_seconds() <= max_age_s)


async def _verify_online(inc: dict, cfg: dict) -> Result:
    ok = await _wait_for(lambda: _fresh(inc["device_id"]), settings.verify_generic_seconds)
    if ok:
        return True, f"Thiết bị gửi dữ liệu trở lại (lần cuối {runtime.get(inc['device_id']).last_seen:%H:%M:%S} UTC)"
    return False, "Vẫn chưa nhận được dữ liệu mới từ thiết bị"


async def _verify_pump_flow(inc: dict, cfg: dict) -> Result:
    dev = inc["device_id"]
    rt = runtime.get(dev)
    if not _fresh(dev):
        return False, "Thiết bị đang offline, không thể chạy thử bơm"
    p = cfg["pump"]
    if rt.latest and rt.latest.pump_lockout:
        await commands.send(dev, "pump", "clear_lockout", reason="verification", actor="system")
        await asyncio.sleep(2)
    test_s = settings.verify_pump_seconds
    await incidents.add_note(inc["_id"], "system", "verification_started",
                             f"Chạy thử bơm {test_s} giây và đo lưu lượng")
    started = utcnow()
    await commands.send(dev, "pump", "on", duration_s=test_s + 5, reason="verification", actor="system")
    await asyncio.sleep(test_s)
    window = [r for r in rt.readings
              if r.ts >= started and r.pump_on and (r.ts - started).total_seconds() >= PUMP_FLOW_GRACE_S]
    await commands.send(dev, "pump", "off", reason="verification", actor="system")
    if not window:
        return False, "Không có dữ liệu bơm đang chạy trong lúc kiểm tra (lệnh không thực hiện?)"
    avg = sum(r.flow_lpm for r in window) / len(window)
    if avg >= p["min_flow_lpm"] and not window[-1].pump_lockout:
        return True, f"Chạy thử bơm: lưu lượng trung bình {avg:.2f} L/phút (≥ {p['min_flow_lpm']})"
    return False, f"Chạy thử bơm: lưu lượng trung bình chỉ {avg:.2f} L/phút"


async def _verify_no_leak(inc: dict, cfg: dict) -> Result:
    dev = inc["device_id"]
    rt = runtime.get(dev)
    if rt.latest and rt.latest.pump_on:
        await commands.send(dev, "pump", "off", reason="verification", actor="system")
    await asyncio.sleep(settings.verify_generic_seconds)
    w = rt.window(settings.verify_generic_seconds * 0.6, utcnow())
    if w and all(r.flow_lpm < cfg["pump"]["min_flow_lpm"] and not r.pump_on for r in w):
        return True, "Bơm tắt và lưu lượng ≈ 0 trong suốt thời gian kiểm tra"
    return False, "Vẫn đo được dòng chảy khi bơm tắt"


def _verify_actuator_off(actuator: str) -> Callable[[dict, dict], Awaitable[Result]]:
    async def run(inc: dict, cfg: dict) -> Result:
        dev = inc["device_id"]
        rt = runtime.get(dev)
        if rt.latest and getattr(rt.latest, f"{actuator}_on"):
            await commands.send(dev, actuator, "off", reason="verification", actor="system")
        ok = await _wait_for(lambda: rt.latest is not None and not getattr(rt.latest, f"{actuator}_on"),
                             settings.verify_generic_seconds)
        return (True, f"{actuator} đã tắt, chế độ tự động hoạt động lại") if ok else (False, f"{actuator} vẫn đang chạy")
    return run


async def _verify_mist_effect(inc: dict, cfg: dict) -> Result:
    dev = inc["device_id"]
    rt = runtime.get(dev)
    if not _fresh(dev):
        return False, "Thiết bị đang offline"
    m = cfg["mist"]
    if rt.latest and rt.latest.mist_lockout:
        await commands.send(dev, "mist", "clear_lockout", reason="verification", actor="system")
    before = [r.air_humidity_pct for r in rt.window(30, utcnow()) if r.air_humidity_pct is not None]
    if not before:
        return False, "Không có số đo độ ẩm để so sánh"
    start = sum(before) / len(before)
    if start > 100 - m["min_rise_pct"] - 5:
        return True, f"Độ ẩm đã cao ({start:.0f}%), không cần chạy thử"
    test_s = settings.verify_mist_seconds
    await incidents.add_note(inc["_id"], "system", "verification_started",
                             f"Chạy thử phun sương {test_s} giây, độ ẩm ban đầu {start:.1f}%")
    await commands.send(dev, "mist", "on", duration_s=test_s + 5, reason="verification", actor="system")
    target = min(start + m["min_rise_pct"], m["air_max_pct"])

    def risen() -> bool:
        recent = [r.air_humidity_pct for r in rt.window(15, utcnow()) if r.air_humidity_pct is not None]
        return bool(recent) and sum(recent) / len(recent) >= target

    ok = await _wait_for(risen, test_s, interval=2)
    await commands.send(dev, "mist", "off", reason="verification", actor="system")
    now_h = rt.latest.air_humidity_pct if rt.latest else None
    if ok:
        return True, f"Phun sương làm độ ẩm tăng từ {start:.1f}% lên {now_h:.1f}%"
    return False, f"Sau {test_s} giây độ ẩm không tăng đủ {m['min_rise_pct']}% (hiện {now_h}%)"


def _verify_sensor(attr: str, label: str) -> Callable[[dict, dict], Awaitable[Result]]:
    async def run(inc: dict, cfg: dict) -> Result:
        rt = runtime.get(inc["device_id"])
        n = int(cfg["health"]["sensor_fail_count"])
        since = utcnow()

        def healthy() -> bool:
            fresh = [r for r in rt.readings if r.ts >= since]
            return len(fresh) >= n and all(getattr(r, attr) for r in fresh[-n:])

        ok = await _wait_for(healthy, settings.verify_generic_seconds + 5 * n)
        return (True, f"{label}: {n} lần đo liên tiếp hợp lệ") if ok else (False, f"{label} vẫn trả giá trị lỗi")
    return run


async def _verify_soil_varies(inc: dict, cfg: dict) -> Result:
    rt = runtime.get(inc["device_id"])
    since = utcnow()

    def varies() -> bool:
        raws = {r.soil_raw for r in rt.readings if r.ts >= since and r.soil_ok}
        return len(raws) >= 2

    ok = await _wait_for(varies, settings.verify_generic_seconds * 2)
    return (True, "Giá trị cảm biến đất thay đổi bình thường") if ok else (False, "Giá trị cảm biến đất vẫn không đổi")


async def _verify_command_roundtrip(inc: dict, cfg: dict) -> Result:
    dev = inc["device_id"]
    rt = runtime.get(dev)
    actuator = (inc.get("context") or {}).get("actuator") or "mist"
    state_on = bool(rt.latest and getattr(rt.latest, f"{actuator}_on"))
    cmd = await commands.send(dev, actuator, "on" if state_on else "off", reason="verification", actor="system")
    acked: dict = {}

    async def poll() -> bool:
        doc = await db.commands.find_one({"_id": cmd["_id"]})
        acked.update(doc or {})
        return (doc or {}).get("status") == "acked"

    deadline = asyncio.get_running_loop().time() + settings.command_ack_timeout_s + 5
    while asyncio.get_running_loop().time() < deadline:
        if await poll():
            return True, f"Thiết bị xác nhận lệnh kiểm tra sau {acked.get('latency_ms')} ms"
        await asyncio.sleep(1)
    return False, "Thiết bị vẫn không xác nhận lệnh"


async def _verify_latency(inc: dict, cfg: dict) -> Result:
    await asyncio.sleep(settings.verify_generic_seconds)
    p95 = latency_p95(60)
    if p95 is None:
        return False, "Chưa có số liệu độ trễ"
    if p95 <= settings.ingest_slo_ms:
        return True, f"Độ trễ ingest p95 1 phút gần nhất: {p95:.0f} ms (SLO {settings.ingest_slo_ms} ms)"
    return False, f"Độ trễ ingest p95 vẫn {p95:.0f} ms"


async def _verify_stable_power(inc: dict, cfg: dict) -> Result:
    rt = runtime.get(inc["device_id"])
    start_uptime = rt.last_uptime_s
    await asyncio.sleep(settings.verify_generic_seconds)
    if rt.last_uptime_s is not None and start_uptime is not None and rt.last_uptime_s >= start_uptime and _fresh(inc["device_id"], 30):
        return True, f"Không khởi động lại trong {settings.verify_generic_seconds} giây theo dõi (uptime {rt.last_uptime_s}s)"
    return False, "Thiết bị vẫn khởi động lại hoặc mất kết nối"


VERIFIERS: dict[str, Callable[[dict, dict], Awaitable[Result]]] = {
    "device_offline": _verify_online,
    "pump_dry_run": _verify_pump_flow,
    "flow_without_pump": _verify_no_leak,
    "pump_overrun": _verify_actuator_off("pump"),
    "mist_overrun": _verify_actuator_off("mist"),
    "mist_ineffective": _verify_mist_effect,
    "dht_fault": _verify_sensor("dht_ok", "DHT11"),
    "soil_sensor_fault": _verify_sensor("soil_ok", "Cảm biến đất"),
    "soil_sensor_stuck": _verify_soil_varies,
    "command_timeout": _verify_command_roundtrip,
    "ingest_latency": _verify_latency,
    "power_brownout": _verify_stable_power,
}

_running: set[str] = set()


async def run_verification(incident_id: str) -> None:
    if incident_id in _running:
        return
    _running.add(incident_id)
    try:
        inc = await db.incidents.find_one({"_id": incident_id})
        if not inc or inc["status"] != "verifying":
            return
        cfg = await db.get_settings()
        verifier = VERIFIERS.get(inc["type"], _verify_online)
        try:
            ok, evidence = await verifier(inc, cfg)
        except Exception as exc:  # không để task chết lặng lẽ
            log.exception("Verification crashed", extra={"incident_id": incident_id})
            ok, evidence = False, f"Lỗi khi kiểm chứng: {exc}"
        if ok:
            await incidents.mark_resolved(incident_id, evidence)
        else:
            await incidents.verification_failed(incident_id, evidence)
    finally:
        _running.discard(incident_id)


async def resume_pending() -> None:
    """Sau khi backend khởi động lại: chạy tiếp các incident đang 'verifying'."""
    pending = await db.incidents.find({"status": "verifying"}).to_list(100)
    for inc in pending:
        asyncio.create_task(run_verification(inc["_id"]))
