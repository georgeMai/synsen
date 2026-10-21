"""Luật phát hiện sự cố (hàm thuần, dễ kiểm thử).

Đầu vào: trạng thái chạy của một thiết bị + cài đặt + thời điểm hiện tại.
Đầu ra: danh sách Finding. Backend biến Finding thành incident trong MongoDB
và phát metric `agriguard.fault.active` để Datadog vẽ/cảnh báo.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from .irrigation import describe
from .state import DeviceRuntime


@dataclass(frozen=True)
class IncidentType:
    severity: str
    title: str
    blocks: tuple[str, ...] = ()   # thiết bị chấp hành bị khoá điều khiển tự động khi sự cố mở


INCIDENT_TYPES: dict[str, IncidentType] = {
    "device_offline": IncidentType("critical", "Thiết bị mất kết nối", ("pump", "mist")),
    "pump_dry_run": IncidentType("critical", "Bơm chạy khô / tắc ống", ("pump",)),
    "flow_without_pump": IncidentType("critical", "Có dòng chảy khi bơm tắt (relay kẹt / rò nước)", ("pump",)),
    "pump_overrun": IncidentType("warning", "Bơm chạy quá thời gian cho phép", ("pump",)),
    "mist_overrun": IncidentType("warning", "Phun sương chạy quá thời gian cho phép", ("mist",)),
    "mist_ineffective": IncidentType("warning", "Phun sương không làm tăng độ ẩm (hết nước?)", ("mist",)),
    "dht_fault": IncidentType("warning", "Cảm biến nhiệt độ/độ ẩm DHT11 lỗi", ("mist",)),
    "soil_sensor_fault": IncidentType("warning", "Cảm biến độ ẩm đất lỗi / tuột dây", ("pump",)),
    "soil_sensor_stuck": IncidentType("warning", "Cảm biến độ ẩm đất bị đơ (giá trị không đổi)", ("pump",)),
    "command_timeout": IncidentType("warning", "Thiết bị không xác nhận lệnh điều khiển"),
    "power_brownout": IncidentType("warning", "ESP32 khởi động lại do sụt áp nguồn"),
    "ingest_latency": IncidentType("warning", "Đường ống dữ liệu bị chậm"),
    "irrigation_ineffective": IncidentType("warning", "Nước tưới không tới gốc (đất không phản hồi)", ("pump",)),
    "soil_response_high": IncidentType("warning", "Đất ẩm lên bất thường so với lượng nước (cảm biến đọng nước?)"),
}

GENERIC = IncidentType("warning", "Sự cố khác")

PUMP_FLOW_GRACE_S = 10  # thời gian mồi nước sau khi bật bơm


def incident_type(name: str) -> IncidentType:
    return INCIDENT_TYPES.get(name, GENERIC)


@dataclass
class Finding:
    type: str
    started_at: datetime
    message: str
    context: dict[str, Any] = field(default_factory=dict)


def _seconds(a: datetime, b: datetime) -> float:
    return (a - b).total_seconds()


def evaluate(rt: DeviceRuntime, cfg: dict, now: datetime) -> list[Finding]:
    h, p, m = cfg["health"], cfg["pump"], cfg["mist"]
    findings: list[Finding] = []
    if rt.last_seen is None:
        return findings

    silent_for = _seconds(now, rt.last_seen)
    if not rt.online or silent_for > h["offline_after_s"]:
        msg = (f"{rt.device_id} báo mất kết nối MQTT (LWT)" if not rt.online
               else f"Không nhận dữ liệu từ {rt.device_id} trong {int(silent_for)} giây")
        findings.append(Finding("device_offline", rt.last_seen, msg,
                                {"silent_for_s": int(silent_for), "lwt": not rt.online}))
        return findings  # các luật khác không còn tin cậy khi thiết bị im lặng

    latest = rt.latest
    if latest is None:
        return findings
    readings = list(rt.readings)

    # --- Cảm biến ---
    n = int(h["sensor_fail_count"])
    tail = readings[-n:]
    if len(tail) >= n and all(not r.dht_ok for r in tail):
        findings.append(Finding("dht_fault", tail[0].ts, "DHT11 trả về giá trị lỗi liên tiếp",
                                {"consecutive_errors": len(tail)}))
    if len(tail) >= n and all(not r.soil_ok for r in tail):
        findings.append(Finding("soil_sensor_fault", tail[0].ts,
                                f"Giá trị ADC cảm biến đất bất thường ({latest.soil_raw})",
                                {"soil_raw": latest.soil_raw}))
    stuck_s = h["stuck_minutes"] * 60
    win = rt.window(stuck_s, now)
    if (len(win) >= 10 and _seconds(now, win[0].ts) >= stuck_s * 0.9
            and all(r.soil_ok for r in win)
            and len({r.soil_raw for r in win}) == 1):
        findings.append(Finding("soil_sensor_stuck", win[0].ts,
                                f"Giá trị cảm biến đất không đổi ({latest.soil_raw}) trong {h['stuck_minutes']} phút",
                                {"soil_raw": latest.soil_raw}))

    # --- Bơm & lưu lượng ---
    if latest.pump_lockout:
        findings.append(Finding("pump_dry_run", rt.pump_on_since or latest.ts,
                                "Thiết bị đã tự cắt và khoá bơm vì không có dòng chảy",
                                {"flow_lpm": latest.flow_lpm, "reported_by": "device"}))
    elif latest.pump_on and rt.pump_on_since:
        on_for = _seconds(now, rt.pump_on_since)
        if on_for >= PUMP_FLOW_GRACE_S + p["dry_run_seconds"]:
            w = [r for r in rt.window(p["dry_run_seconds"], now) if r.pump_on]
            if w and all(r.flow_lpm < p["min_flow_lpm"] for r in w):
                findings.append(Finding(
                    "pump_dry_run", rt.pump_on_since + timedelta(seconds=PUMP_FLOW_GRACE_S),
                    f"Bơm bật {int(on_for)} giây nhưng lưu lượng < {p['min_flow_lpm']} L/phút",
                    {"flow_lpm": latest.flow_lpm, "pump_on_for_s": int(on_for)}))
        if on_for > p["max_minutes"] * 60 + 60:
            findings.append(Finding("pump_overrun", rt.pump_on_since,
                                    f"Bơm đã chạy {int(on_for // 60)} phút (giới hạn {p['max_minutes']})",
                                    {"pump_on_for_s": int(on_for)}))

    if not latest.pump_on:
        w = rt.window(p["leak_seconds"], now)
        if (len(w) >= 2 and _seconds(now, w[0].ts) >= p["leak_seconds"] * 0.6
                and all((not r.pump_on) and r.flow_lpm >= p["min_flow_lpm"] for r in w)):
            findings.append(Finding("flow_without_pump", w[0].ts,
                                    f"Bơm đang tắt nhưng lưu lượng {latest.flow_lpm:.2f} L/phút",
                                    {"flow_lpm": latest.flow_lpm}))

    # --- Phun sương ---
    if latest.mist_on and rt.mist_on_since:
        on_for = _seconds(now, rt.mist_on_since)
        if on_for > m["max_minutes"] * 60 + 60:
            findings.append(Finding("mist_overrun", rt.mist_on_since,
                                    f"Phun sương đã chạy {int(on_for // 60)} phút (giới hạn {m['max_minutes']})",
                                    {"mist_on_for_s": int(on_for)}))
        check_s = m["check_minutes"] * 60
        if on_for >= check_s:
            # So sánh độ ẩm 30 giây gần nhất với 30 giây ở đầu cửa sổ check_minutes (cửa sổ trượt,
            # để độ ẩm môi trường thay đổi theo ngày/đêm không che mất lỗi).
            base_start = now - timedelta(seconds=check_s)
            baseline = [r.air_humidity_pct for r in rt.readings
                        if base_start <= r.ts < base_start + timedelta(seconds=30) and r.air_humidity_pct is not None]
            recent = [r.air_humidity_pct for r in rt.window(30, now) if r.air_humidity_pct is not None]
            if baseline and recent:
                base_avg = sum(baseline) / len(baseline)
                recent_avg = sum(recent) / len(recent)
                rise = recent_avg - base_avg
                if rise < m["min_rise_pct"] and recent_avg < m["air_max_pct"]:
                    findings.append(Finding(
                        "mist_ineffective", rt.mist_on_since,
                        f"Phun sương nhưng độ ẩm chỉ thay đổi {rise:+.1f}% trong {m['check_minutes']:g} phút "
                        f"(hiện {recent_avg:.0f}%, cần tăng ≥ {m['min_rise_pct']}% hoặc đạt {m['air_max_pct']}%)",
                        {"humidity_rise_pct": round(rise, 1), "humidity_pct": round(recent_avg, 1)}))

    # --- Phản hồi đất trên mỗi lít nước (lần tưới gần nhất đã chấm điểm) ---
    ev = rt.last_assessed
    if ev and ev["status"] in ("low_response", "high_response"):
        ctx = {k: ev.get(k) for k in ("liters", "rise_pct", "ratio_pct_per_l", "baseline_pct_per_l", "baseline_n",
                                     "zscore", "irrigation_id")}
        findings.append(Finding("irrigation_ineffective" if ev["status"] == "low_response" else "soil_response_high",
                                ev["started_at"], describe(ev), ctx))
    return findings


def sensor_health(rt: DeviceRuntime, findings: list[Finding]) -> dict[str, str]:
    types = {f.type for f in findings}
    latest = rt.latest
    return {
        "dht11": "fault" if "dht_fault" in types or (latest and not latest.dht_ok) else "ok",
        "soil": "fault" if types & {"soil_sensor_fault", "soil_sensor_stuck", "soil_response_high"} else "ok",
        "flow": "fault" if types & {"flow_without_pump"} else "ok",
        "pump": "fault" if types & {"pump_dry_run", "pump_overrun", "irrigation_ineffective"} else "ok",
        "mist": "fault" if types & {"mist_ineffective", "mist_overrun"} else "ok",
    }
