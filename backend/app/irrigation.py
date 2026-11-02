"""Phản hồi của đất trên mỗi lít nước tưới (soil response per liter) – phát hiện bất thường thống kê.

Ý tưởng: mỗi lần tưới, đất ở luống đó phải ẩm lên một lượng tương đối ổn định trên mỗi lít nước.
Hệ thống học "mức bình thường" (trung vị + MAD của N lần tưới gần nhất, lưu trong MongoDB) rồi
so sánh lần tưới mới bằng robust z-score:

  ratio = (độ ẩm đất sau khi thấm − độ ẩm trước khi tưới) / số lít đã bơm        [% / L]
  z     = (ratio − trung vị) / max(1.4826·MAD, tỉ lệ tối thiểu)

  z ≤ −3  -> nước chảy (YF-S201 đếm được) nhưng không tới gốc: tuột ống, rò ngầm, cảm biến lệch
  z ≥ +3  -> đất ẩm lên quá nhanh so với lượng nước: cảm biến bị ngập/đọng nước, đất bị nén

Lỗi này KHÔNG ngưỡng tĩnh nào bắt được: bơm vẫn chạy, lưu lượng vẫn bình thường, độ ẩm vẫn trong khoảng hợp lệ.

Phần tính toán là hàm thuần (dễ kiểm thử); `IrrigationTracker` theo dõi từng lần tưới từ luồng telemetry.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from statistics import median
from typing import Optional

from .state import DeviceRuntime, Reading

log = logging.getLogger(__name__)

ASSESSED = ("normal", "low_response", "high_response")
REASONS = {   # mã lý do (dùng làm tag Datadog) -> mô tả cho người vận hành
    "insufficient_baseline": "đang học mức bình thường (chưa đủ số lần tưới)",
    "water_not_reaching_roots": "nước không tới gốc",
    "response_too_high": "đất ẩm lên quá nhanh so với lượng nước",
    "soil_sensor_invalid": "cảm biến đất lỗi trong lúc tưới",
    "too_little_water": "lượng nước quá ít để đánh giá",
    "soil_already_wet": "đất đã gần bão hoà",
    "sensor_saturated": "cảm biến đất chạm ngưỡng bão hoà",
    "mist_running": "phun sương chạy cùng lúc (nhiễu)",
}
BASELINE_STATUSES = ("normal", "learning")


@dataclass
class IrrigationRun:
    """Một lần tưới đang diễn ra hoặc đang chờ nước thấm."""
    started_at: datetime
    soil_before: Optional[float]
    counter_start: Optional[float]          # flow_liters_total của thiết bị lúc bắt đầu
    liters_integrated: float = 0.0          # tích phân flow_lpm·dt (dự phòng khi không có bộ đếm)
    ended_at: Optional[datetime] = None
    counter_end: Optional[float] = None
    pump_seconds: float = 0.0
    mist_seen: bool = False
    soil_invalid: bool = False
    readings_on: int = 0
    soil_peak: Optional[float] = None
    drying_per_s: float = 0.0               # tốc độ đất tự khô trước khi tưới (%/giây, ≥ 0)
    after_ts: Optional[datetime] = None     # thời điểm lấy số đo "sau khi tưới"
    extra: dict = field(default_factory=dict)

    @property
    def liters(self) -> float:
        if (self.counter_start is not None and self.counter_end is not None
                and self.counter_end >= self.counter_start):
            return self.counter_end - self.counter_start
        return self.liters_integrated


def robust_baseline(ratios: list[float]) -> tuple[float, float]:
    """(trung vị, độ phân tán dùng cho z-score). Phân tán có sàn để vài lần tưới giống hệt nhau không làm z bùng nổ."""
    med = median(ratios)
    mad = median(abs(x - med) for x in ratios)
    scale = max(1.4826 * mad, 0.12 * abs(med), 0.5)
    return med, scale


def assess(run: IrrigationRun, soil_after: Optional[float], history: list[float], cfg: dict) -> dict:
    """Đánh giá một lần tưới đã kết thúc. `history`: ratio của các lần tưới bình thường trước đó (mới nhất trước)."""
    c = cfg["irrigation"]
    liters = run.liters
    out = {
        "started_at": run.started_at, "ended_at": run.ended_at, "pump_seconds": round(run.pump_seconds),
        "liters": round(liters, 3), "soil_before": run.soil_before, "soil_after": soil_after,
        "rise_pct": None, "ratio_pct_per_l": None, "baseline_pct_per_l": None, "baseline_n": len(history),
        "zscore": None, "status": "excluded", "reason": None,
    }
    if run.soil_invalid or run.soil_before is None or soil_after is None:
        out["reason"] = "soil_sensor_invalid"
        return out
    # Đất vẫn tự khô trong lúc tưới + chờ thấm -> cộng bù phần bay hơi để phép so sánh công bằng
    elapsed = (run.after_ts - run.started_at).total_seconds() if run.after_ts else 0.0
    correction = run.drying_per_s * max(0.0, elapsed)
    rise = soil_after - run.soil_before + correction
    out["rise_pct"] = round(rise, 2)
    out["drying_correction_pct"] = round(correction, 2)
    if liters < c["min_liters"]:
        out["reason"] = "too_little_water"
        return out
    if run.soil_before > c["max_start_soil_pct"]:
        out["reason"] = "soil_already_wet"        # đất gần bão hoà: tưới thêm không làm tăng nữa
        return out
    if soil_after >= 97:
        out["reason"] = "sensor_saturated"
        return out
    if run.mist_seen and c["exclude_if_mist"]:
        out["reason"] = "mist_running"            # phun sương có thể làm ướt mặt đất -> nhiễu
        return out

    ratio = rise / liters
    out["ratio_pct_per_l"] = round(ratio, 2)
    usable = history[: int(c["baseline_events"])]
    if len(usable) < int(c["min_baseline_events"]):
        out["status"], out["reason"] = "learning", "insufficient_baseline"
        return out
    med, scale = robust_baseline(usable)
    z = (ratio - med) / scale
    out.update(baseline_pct_per_l=round(med, 2), baseline_n=len(usable), zscore=round(z, 2))
    if z <= -c["zscore_threshold"]:
        out["status"], out["reason"] = "low_response", "water_not_reaching_roots"
    elif z >= c["zscore_threshold"] and c.get("detect_high_response", True):
        out["status"], out["reason"] = "high_response", "response_too_high"
    elif z >= c["zscore_threshold"]:
        out["status"], out["reason"] = "normal", "high_response_ignored"   # cảnh báo đã tắt trong Cài đặt
    else:
        out["status"] = "normal"
    return out


def describe(ev: dict) -> str:
    if ev["status"] == "low_response":
        return (f"Tưới {ev['liters']:.2f} L nhưng độ ẩm đất chỉ thay đổi {ev['rise_pct']:+.1f}% "
                f"({ev['ratio_pct_per_l']:.1f} %/L, bình thường {ev['baseline_pct_per_l']:.1f} %/L, "
                f"z = {ev['zscore']:.1f}) → nước không tới gốc (tuột/rò ống, cảm biến lệch chỗ)")
    if ev["status"] == "high_response":
        return (f"Tưới {ev['liters']:.2f} L mà độ ẩm đất tăng {ev['rise_pct']:+.1f}% "
                f"({ev['ratio_pct_per_l']:.1f} %/L, bình thường {ev['baseline_pct_per_l']:.1f} %/L, "
                f"z = {ev['zscore']:.1f}) → cảm biến đọng nước hoặc đất bị nén")
    return f"{ev['status']}: {REASONS.get(ev.get('reason'), ev.get('reason') or '')}"


def drying_rate(readings: list[Reading]) -> float:
    """Độ dốc giảm của độ ẩm đất (%/giây) theo bình phương tối thiểu; chỉ tính khi đất đang khô đi."""
    pts = [((r.ts - readings[0].ts).total_seconds(), r.soil_moisture_pct) for r in readings
           if r.soil_ok and r.soil_moisture_pct is not None]
    if len(pts) < 3:
        return 0.0
    n = len(pts)
    mx = sum(x for x, _ in pts) / n
    my = sum(y for _, y in pts) / n
    den = sum((x - mx) ** 2 for x, _ in pts)
    if den <= 0:
        return 0.0
    slope = sum((x - mx) * (y - my) for x, y in pts) / den
    return min(max(0.0, -slope), 0.2)      # bỏ qua khi đất đang ẩm lên; chặn trên để nhiễu không phóng đại


def _soil_median(readings: list[Reading]) -> Optional[float]:
    vals = [r.soil_moisture_pct for r in readings if r.soil_ok and r.soil_moisture_pct is not None]
    return median(vals) if vals else None


class IrrigationTracker:
    """Gọi `on_reading` cho mỗi bản tin telemetry; trả về IrrigationRun + soil_after khi một lần tưới đã thấm xong."""

    @staticmethod
    def on_reading(rt: DeviceRuntime, r: Reading, prev: Optional[Reading], flow_total: Optional[float],
                   prev_total: Optional[float], cfg: dict) -> Optional[tuple[IrrigationRun, Optional[float]]]:
        c = cfg["irrigation"]
        run: Optional[IrrigationRun] = rt.irrigation
        finished = None
        dt = min(30.0, max(0.0, (r.ts - prev.ts).total_seconds())) if prev else 0.0

        # Lần tưới mới bắt đầu (kể cả khi lần trước chưa thấm xong -> chốt lần trước với dữ liệu hiện có)
        if r.pump_on and (prev is None or not prev.pump_on):
            if run is not None and run.ended_at is not None:
                run.after_ts = prev.ts if prev else r.ts
                rt.irrigation_settled_at = run.after_ts
                finished = (run, _soil_median([x for x in rt.readings if run.ended_at <= x.ts < r.ts][-4:]))
            before = [x for x in rt.readings if r.ts - timedelta(seconds=c["before_seconds"]) <= x.ts < r.ts]
            settled = rt.irrigation_settled_at
            drying = [x for x in before if settled is None or x.ts >= settled]   # không lẫn phần nước lần trước
            rate = None
            if len(drying) >= 4 and (drying[-1].ts - drying[0].ts).total_seconds() >= 20:
                rate = drying_rate(drying)
                rt.drying_rate_est, rt.drying_rate_at = rate, r.ts
            elif rt.drying_rate_est is not None and rt.drying_rate_at is not None \
                    and (r.ts - rt.drying_rate_at).total_seconds() <= 1800:
                rate = rt.drying_rate_est      # tưới dồn dập: dùng tốc độ khô đo được gần nhất (≤ 30 phút)
            run = IrrigationRun(started_at=r.ts, soil_before=_soil_median(before),
                                counter_start=prev_total if prev_total is not None else flow_total,
                                mist_seen=any(x.mist_on for x in before), drying_per_s=rate or 0.0)
            rt.irrigation = run
            # bản tin đầu tiên có pump_on cũng mang nước của khoảng dt trước đó
            run.liters_integrated += r.flow_lpm * dt / 60
            run.readings_on += 1
            run.counter_end = flow_total
            return finished
        if run is None:
            return None

        if run.ended_at is None:
            if r.pump_on:
                run.liters_integrated += r.flow_lpm * dt / 60
                run.pump_seconds += dt
                run.readings_on += 1
            else:
                run.ended_at = r.ts
                run.pump_seconds += dt
            run.counter_end = flow_total if flow_total is not None else run.counter_end
            run.mist_seen |= r.mist_on
            run.soil_invalid |= not r.soil_ok
            return None

        # Đang chờ nước thấm
        run.mist_seen |= r.mist_on
        run.soil_invalid |= not r.soil_ok
        if (r.ts - run.ended_at).total_seconds() >= c["settle_seconds"]:
            window = [x for x in rt.readings
                      if x.ts >= r.ts - timedelta(seconds=c["after_window_seconds"])]
            rt.irrigation = None
            run.after_ts = r.ts - timedelta(seconds=c["after_window_seconds"] / 2)
            rt.irrigation_settled_at = r.ts
            return run, _soil_median(window)
        return None
