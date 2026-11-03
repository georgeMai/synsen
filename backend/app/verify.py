"""Kiểm chứng khắc phục: sau khi người vận hành bấm "Đã xử lý", hệ thống tự kiểm tra
bằng dữ liệu thật trước khi đóng incident (bước cuối của Detect – Investigate – Resolve).
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import nullcontext
from typing import Awaitable, Callable, Optional

try:                                  # có khi chạy bằng ddtrace-run (Docker); không bắt buộc khi chạy thử
    from ddtrace import tracer
except ImportError:                   # pragma: no cover
    tracer = None

from .commands import commands
from .config import settings
from .db import db, utcnow
from .incidents import incidents
from .irrigation import REASONS, describe
from .ingest import latency_p95
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


DRIFT_WINDOW_S = 180   # xu hướng nhiệt độ / độ ẩm tự nhiên trước khi chạy thử phun sương
MIN_EFFECT_S = 20      # chờ tối thiểu trước khi kết luận (tránh nhiễu tức thời)


def _fresh(device_id: str, max_age_s: float = 15) -> bool:
    rt = runtime.get(device_id)
    return bool(rt.online and rt.last_seen and (utcnow() - rt.last_seen).total_seconds() <= max_age_s)


async def _verify_online(inc: dict, cfg: dict) -> Result:
    ok = await _wait_for(lambda: _fresh(inc["device_id"]), settings.verify_generic_seconds)
    if ok:
        return True, f"Thiết bị gửi dữ liệu trở lại (lần cuối {runtime.get(inc['device_id']).last_seen:%H:%M:%S} UTC)"
    return False, "Vẫn chưa nhận được dữ liệu mới từ thiết bị"


async def _verify_pump_flow(inc: dict, cfg: dict) -> Result:
    """Chạy thử bơm pump.verify_seconds giây (mặc định 5) rồi đo lưu lượng.

    Lần chạy ngắn chỉ có 1–2 bản tin telemetry (5 giây/lần) nên lưu lượng được tính từ BỘ ĐẾM LÍT của thiết bị
    (flow_liters_total, YF-S201): lít đếm được ÷ thời gian chạy. Không có bộ đếm thì lấy trung bình flow_lpm.
    """
    dev = inc["device_id"]
    rt = runtime.get(dev)
    if not _fresh(dev):
        return False, "Thiết bị đang offline, không thể chạy thử bơm"
    p = cfg["pump"]
    if rt.latest and rt.latest.pump_lockout:
        await commands.send(dev, "pump", "clear_lockout", reason="verification", actor="system")
        await asyncio.sleep(2)
    test_s = int(p.get("verify_seconds", 5))
    await incidents.add_note(inc["_id"], "system", "verification_started",
                             f"Chạy thử bơm {test_s} giây và đo lưu lượng (bộ đếm lít của cảm biến)")
    total0 = rt.latest.flow_total if rt.latest else None
    started = utcnow()
    await commands.send(dev, "pump", "on", duration_s=test_s, reason="verification", actor="system")

    def finished() -> bool:
        ons = [r for r in rt.readings if r.ts >= started and r.pump_on]
        return bool(ons) and any(r.ts > ons[0].ts and not r.pump_on for r in rt.readings)

    await _wait_for(finished, test_s + 15, interval=0.5)
    await asyncio.sleep(1.5)                       # bản tin cuối (bơm tắt) mang bộ đếm lít sau cùng
    await commands.send(dev, "pump", "off", reason="verification", actor="system")
    ons = [r for r in rt.readings if r.ts >= started and r.pump_on]
    if not ons:
        return False, "Không thấy bơm chạy trong lúc kiểm tra (lệnh không thực hiện?)"
    if any(r.pump_lockout for r in rt.readings if r.ts >= started):
        return False, "Thiết bị lại tự khoá bơm vì không có dòng chảy"
    total1 = rt.latest.flow_total if rt.latest else None
    if total0 is not None and total1 is not None and total1 >= total0:
        liters = total1 - total0
        avg = liters / test_s * 60
        how = f"{liters:.3f} L trong {test_s} giây = {avg:.2f} L/phút"
    else:
        avg = sum(r.flow_lpm for r in ons) / len(ons)
        how = f"lưu lượng trung bình {avg:.2f} L/phút"
    if inc["type"] == "pump_low_flow" and rt.normal_flow_lpm:
        need = float(cfg["health"].get("low_flow_ratio", 0.6)) * rt.normal_flow_lpm
        if avg >= need:
            return True, f"Chạy thử bơm: {how}, đạt ≥ {need:.2f} (mức bình thường {rt.normal_flow_lpm:.2f})"
        return False, f"Chạy thử bơm: {how}, cần ≥ {need:.2f} L/phút"
    if avg >= p["min_flow_lpm"]:
        return True, f"Chạy thử bơm: {how} (≥ {p['min_flow_lpm']})"
    return False, f"Chạy thử bơm: {how} – vẫn không có nước"


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
    rise_need = float(m.get("verify_rise_pct", 1))
    drop_need = float(m.get("verify_temp_drop_c", 1))

    def series(attr: str, seconds: float) -> list[tuple[float, float]]:
        now = utcnow()
        return [((r.ts - now).total_seconds(), getattr(r, attr)) for r in rt.window(seconds, now)
                if getattr(r, attr) is not None]

    def avg(attr: str, seconds: float) -> Optional[float]:
        vals = [v for _, v in series(attr, seconds)]
        return sum(vals) / len(vals) if vals else None

    def drift(attr: str) -> float:
        """Xu hướng tự nhiên (đơn vị/giây) trong 3 phút trước khi chạy thử – hồi quy tuyến tính.
        Trời tự mát đi hay ẩm lên thì không được tính là công của máy phun sương."""
        pts = series(attr, DRIFT_WINDOW_S)
        if len(pts) < 6 or pts[-1][0] - pts[0][0] < 60:
            return 0.0
        mx = sum(x for x, _ in pts) / len(pts)
        my = sum(y for _, y in pts) / len(pts)
        den = sum((x - mx) ** 2 for x, _ in pts)
        return sum((x - mx) * (y - my) for x, y in pts) / den if den else 0.0

    h0, t0 = avg("air_humidity_pct", 30), avg("air_temp_c", 30)
    if h0 is None and t0 is None:
        return False, "Không có số đo nhiệt độ / độ ẩm để so sánh"
    # chỉ trừ phần trời tự thay đổi CÙNG chiều với tác dụng phun sương (ẩm lên / mát đi)
    h_drift, t_drift = max(0.0, drift("air_humidity_pct")), min(0.0, drift("air_temp_c"))
    test_s = settings.verify_mist_seconds
    await incidents.add_note(inc["_id"], "system", "verification_started",
                             f"Chạy thử phun sương {test_s} giây: đạt khi độ ẩm tăng ≥ {rise_need:g}% "
                             f"hoặc nhiệt độ giảm ≥ {drop_need:g}°C so với xu hướng tự nhiên (ban đầu "
                             f"{'–' if h0 is None else f'{h0:.1f}%'}, {'–' if t0 is None else f'{t0:.1f}°C'})")
    started = utcnow()
    await commands.send(dev, "mist", "on", duration_s=test_s + 5, reason="verification", actor="system")
    result: dict = {}

    def effective() -> bool:
        el = (utcnow() - started).total_seconds()
        if el < MIN_EFFECT_S:
            return False
        h, t = avg("air_humidity_pct", 15), avg("air_temp_c", 15)
        # hiệu quả = thay đổi đo được − phần trời tự thay đổi (ngoại suy xu hướng trước đó)
        dh = h - h0 - h_drift * el if h is not None and h0 is not None else None
        dt = t0 - t + t_drift * el if t is not None and t0 is not None else None
        result.update(h=h, t=t, dh=dh, dt=dt, el=el)
        if dh is not None and dh >= rise_need:
            result["by"] = "humidity"
            return True
        if dt is not None and dt >= drop_need:
            result["by"] = "temperature"
            return True
        return False

    ok = await _wait_for(effective, test_s, interval=2)
    await commands.send(dev, "mist", "off", reason="verification", actor="system")
    el = result.get("el", test_s)

    def describe(label: str, v0, v, nat: float, unit: str) -> str:
        if v0 is None or v is None:
            return f"{label} không đo được"
        txt = f"{label} {v0:.1f}{unit} → {v:.1f}{unit} ({v - v0:+.1f}{unit}"
        return txt + (f", trời tự thay đổi {nat * el:+.1f}{unit})" if abs(nat * el) >= 0.05 else ")")

    sh = describe("độ ẩm", h0, result.get("h"), h_drift, "%")
    st = describe("nhiệt độ", t0, result.get("t"), t_drift, "°C")
    if ok:
        return True, f"Phun sương có hiệu quả: {sh if result['by'] == 'humidity' else st}"
    return False, (f"Sau {test_s} giây phun sương chưa có hiệu quả: {sh}, {st}. "
                   f"Cần độ ẩm tăng ≥ {rise_need:g}% hoặc nhiệt độ giảm ≥ {drop_need:g}°C so với tự nhiên")


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


async def _verify_irrigation_response(inc: dict, cfg: dict) -> Result:
    """Tưới thử một lượng nhỏ, chờ nước thấm rồi so phản hồi của đất với mức bình thường đã học."""
    dev = inc["device_id"]
    rt = runtime.get(dev)
    if not _fresh(dev):
        return False, "Thiết bị đang offline, không thể tưới thử"
    c = cfg["irrigation"]
    if rt.latest and rt.latest.pump_lockout:
        await commands.send(dev, "pump", "clear_lockout", reason="verification", actor="system")
        await asyncio.sleep(2)
    soil = rt.latest.soil_moisture_pct if rt.latest else None
    if soil is not None and soil > c["max_start_soil_pct"]:
        return False, (f"Đất đang quá ẩm ({soil:.0f}% > {c['max_start_soil_pct']}%) nên chưa tưới thử được – "
                       "chờ đất khô bớt rồi bấm lại “Đã xử lý”")
    test_s = int(c["test_seconds"])
    await incidents.add_note(inc["_id"], "system", "verification_started",
                             f"Tưới thử {test_s} giây, chờ {int(c['settle_seconds'])} giây cho nước thấm "
                             "rồi so sánh %/L với mức bình thường")
    since = utcnow()
    await commands.send(dev, "pump", "on", duration_s=test_s, reason="verification", actor="system")

    def assessed() -> bool:
        ev = rt.last_irrigation
        return bool(ev and ev.get("started_at") and (ev["started_at"] - since).total_seconds() >= -10)

    ok = await _wait_for(assessed, test_s + c["settle_seconds"] + 60, interval=2)
    if not ok:
        return False, "Không ghi nhận được lần tưới thử (lệnh không thực hiện hoặc thiếu dữ liệu)"
    ev = rt.last_irrigation
    if ev["status"] == "normal":
        return True, (f"Tưới thử {ev['liters']:.2f} L: đất ẩm lên {ev['rise_pct']:+.1f}% = "
                      f"{ev['ratio_pct_per_l']:.1f} %/L (bình thường {ev['baseline_pct_per_l']:.1f} %/L, z = {ev['zscore']:.1f})")
    if ev["status"] == "excluded":
        return False, f"Lần tưới thử không đánh giá được ({REASONS.get(ev['reason'], ev['reason'])}) – thử lại sau"
    return False, "Tưới thử vẫn bất thường: " + describe(ev)


VERIFIERS: dict[str, Callable[[dict, dict], Awaitable[Result]]] = {
    "device_offline": _verify_online,
    "pump_dry_run": _verify_pump_flow,
    "pump_low_flow": _verify_pump_flow,
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
    "irrigation_ineffective": _verify_irrigation_response,
    "soil_response_high": _verify_irrigation_response,
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
        traced = tracer is not None and getattr(tracer, "enabled", False)
        # Một span APM bao trọn quá trình kiểm chứng: lệnh chạy thử, truy vấn MongoDB, kết luận
        with (tracer.trace("agriguard.verify_incident", service=settings.service, resource=inc["type"])
              if traced else nullcontext()) as span:
            if span is not None:
                span.set_tag("incident_id", incident_id)
                span.set_tag("device_id", inc["device_id"])
                span.set_tag("incident_type", inc["type"])
                trace_id = str(span.trace_id & 0xFFFFFFFFFFFFFFFF)   # 64 bit thấp – dạng UI Datadog dùng
                await incidents.add_note(incident_id, "datadog", "datadog_trace",
                                         "APM trace \"agriguard.verify_incident\": ghi lại lệnh chạy thử, "
                                         "các truy vấn MongoDB và kết luận", trace_id=trace_id)
            try:
                ok, evidence = await verifier(inc, cfg)
            except Exception as exc:  # không để task chết lặng lẽ
                log.exception("Verification crashed", extra={"incident_id": incident_id})
                ok, evidence = False, f"Lỗi khi kiểm chứng: {exc}"
            if span is not None:
                span.set_tag("verification.ok", ok)
                span.set_tag("verification.evidence", evidence)
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
