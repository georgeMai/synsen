"""AI tưới – phần thuần (không I/O), dễ kiểm thử.

1. **Mô hình tốc độ khô của đất** (Ridge regression, tự cài – không cần numpy):
       tốc độ khô (%/giờ) = w · [VPD (từ nhiệt độ + độ ẩm KK), độ ẩm đất, sin(giờ), cos(giờ)] + b
   Huấn luyện từ dữ liệu thật của chính luống (đặc trưng do MongoDB Aggregation Pipeline tạo, xem ai.py).
   Đánh giá theo thời gian: học trên 70% đầu, kiểm tra trên 30% sau, so với mốc "trung bình".
   Chọn mô hình tuyến tính vì dữ liệu chỉ vài ngày (vài trăm mẫu) – GBM/SVR dễ học thuộc.

2. **Dự báo độ ẩm đất 6 giờ tới**: mô phỏng từng bước 10 phút, thời tiết tương lai = hiện tại + chênh lệch
   theo giờ trong ngày (hồ sơ nhiệt độ/độ ẩm theo giờ, cũng tính bằng pipeline).

3. **Lập lịch tưới tối ưu** (tối ưu hoá rời rạc): thử mọi thời điểm bắt đầu trong 6 giờ tới (bước 10 phút)
   khi đất đã vào vùng cần tưới, và phương án "không tưới"; lượng nước = vừa đủ đưa đất lên ngưỡng đủ ẩm theo
   phản hồi đất đã học (%/L).
       chi phí = nước bay hơi (tưới lúc nóng) + trọng số stress × số phút đất dưới ngưỡng khô
   → chọn phương án chi phí nhỏ nhất: tưới đủ (không thừa), tránh giờ nóng, không để cây thiếu nước.

4. **Nguy cơ**: thiếu nước (đất xuống dưới ngưỡng khô mà bơm không tưới được) và stress nhiệt.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

# Nhiệt độ và độ ẩm không khí được gộp thành VPD (độ khô của không khí) – tránh hai đặc trưng tương quan mạnh
# làm hệ số khó đọc (nhiệt độ "âm" chỉ vì VPD đã "dương" thay cho nó).
FEATURES = ("vpd", "soil", "hour_sin", "hour_cos")
FEATURE_LABELS = {"temp": "nhiệt độ (°C)", "vpd": "VPD – độ khô của không khí (kPa)", "soil": "độ ẩm đất (%)",
                  "hour_sin": "giờ trong ngày (sin)", "hour_cos": "giờ trong ngày (cos)"}
LOCAL_OFFSET_H = 7


def vpd_kpa(temp_c: Optional[float], rh_pct: Optional[float]) -> Optional[float]:
    """Vapour pressure deficit – không khí càng nóng và khô thì đất/cây mất nước càng nhanh (công thức Tetens)."""
    if temp_c is None or rh_pct is None:
        return None
    es = 0.6108 * math.exp(17.27 * temp_c / (temp_c + 237.3))
    return round(es * (1 - max(0.0, min(100.0, rh_pct)) / 100), 4)


def hour_features(ts: datetime) -> tuple[float, float]:
    h = (ts.hour + LOCAL_OFFSET_H) % 24 + ts.minute / 60
    return math.sin(2 * math.pi * h / 24), math.cos(2 * math.pi * h / 24)


# ----------------------------------------------------------------------------- Ridge regression
def _solve(a: list[list[float]], b: list[float]) -> list[float]:
    n = len(b)
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    for c in range(n):
        p = max(range(c, n), key=lambda r: abs(m[r][c]))
        m[c], m[p] = m[p], m[c]
        if abs(m[c][c]) < 1e-12:
            raise ValueError("singular")
        for r in range(n):
            if r != c:
                f = m[r][c] / m[c][c]
                for k in range(c, n + 1):
                    m[r][k] -= f * m[c][k]
    return [m[i][n] / m[i][i] for i in range(n)]


@dataclass
class RidgeModel:
    weights: dict[str, float]
    intercept: float
    means: dict[str, float]
    stds: dict[str, float]

    def predict(self, x: dict[str, float]) -> float:
        return self.intercept + sum(self.weights[f] * (x[f] - self.means[f]) / self.stds[f] for f in FEATURES)

    def to_doc(self) -> dict:
        return {"weights": self.weights, "intercept": self.intercept, "means": self.means, "stds": self.stds}

    @classmethod
    def from_doc(cls, d: dict) -> "RidgeModel":
        return cls(d["weights"], d["intercept"], d["means"], d["stds"])


def fit_ridge(rows: list[dict], lam: float = 1.0) -> RidgeModel:
    n = len(rows)
    means = {f: sum(r[f] for r in rows) / n for f in FEATURES}
    stds = {f: (math.sqrt(sum((r[f] - means[f]) ** 2 for r in rows) / n) or 1.0) for f in FEATURES}
    xs = [[(r[f] - means[f]) / stds[f] for f in FEATURES] for r in rows]
    y_mean = sum(r["target"] for r in rows) / n
    ys = [r["target"] - y_mean for r in rows]
    k = len(FEATURES)
    xtx = [[sum(x[i] * x[j] for x in xs) + (lam if i == j else 0.0) for j in range(k)] for i in range(k)]
    xty = [sum(x[i] * y for x, y in zip(xs, ys)) for i in range(k)]
    w = _solve(xtx, xty)
    return RidgeModel({f: round(w[i], 5) for i, f in enumerate(FEATURES)}, round(y_mean, 5), means, stds)


def train(rows: list[dict], lam: float = 1.0, min_samples: int = 30) -> dict:
    """Huấn luyện + đánh giá theo thời gian. rows: đặc trưng đã sắp theo thời gian, có 'target' (%/giờ)."""
    rows = [r for r in rows if all(r.get(f) is not None for f in FEATURES) and r.get("target") is not None]
    out = {"n_samples": len(rows), "status": "insufficient_data", "model": None}
    if len(rows) < min_samples:
        return out
    split = int(len(rows) * 0.7)
    tr, te = rows[:split], rows[split:]
    m = fit_ridge(tr, lam)
    mae = sum(abs(m.predict(r) - r["target"]) for r in te) / len(te)
    base = sum(r["target"] for r in tr) / len(tr)
    base_mae = sum(abs(base - r["target"]) for r in te) / len(te)
    final = fit_ridge(rows, lam)          # mô hình dùng thật: học trên toàn bộ dữ liệu
    out.update(status="ok", model=final.to_doc(), n_train=len(tr), n_test=len(te),
               mae_pct_per_hour=round(mae, 3), baseline_mae_pct_per_hour=round(base_mae, 3),
               skill=round(1 - mae / base_mae, 3) if base_mae > 0 else None,
               target_mean=round(sum(r["target"] for r in rows) / len(rows), 3))
    return out


# ----------------------------------------------------------------------------- dự báo & lập lịch
@dataclass
class Context:
    now: datetime
    soil: float
    temp: Optional[float]
    hum: Optional[float]
    profile: dict[int, dict]          # giờ VN -> {"temp": TB, "hum": TB}
    model: Optional[RidgeModel]
    fallback_rate: float              # %/giờ khi chưa có mô hình (tốc độ khô đo trực tiếp)
    pct_per_liter: Optional[float]    # phản hồi đất đã học
    flow_lpm: Optional[float]         # lưu lượng bơm bình thường đã học


def _weather_at(ctx: Context, ts: datetime) -> tuple[Optional[float], Optional[float]]:
    """Thời tiết tương lai = hiện tại + (hồ sơ giờ tương lai − hồ sơ giờ hiện tại)."""
    if ctx.temp is None:
        return None, ctx.hum
    h_now = (ctx.now.hour + LOCAL_OFFSET_H) % 24
    h_fut = (ts.hour + LOCAL_OFFSET_H) % 24
    p0, p1 = ctx.profile.get(h_now), ctx.profile.get(h_fut)
    if not p0 or not p1:
        return ctx.temp, ctx.hum
    t = ctx.temp + (p1["temp"] - p0["temp"])
    h = None if ctx.hum is None else max(5.0, min(100.0, ctx.hum + (p1["hum"] - p0["hum"])))
    return round(t, 2), (round(h, 1) if h is not None else None)


def drying_rate(ctx: Context, ts: datetime, soil: float) -> float:
    temp, hum = _weather_at(ctx, ts)
    if ctx.model is None or temp is None or hum is None:
        return max(0.0, ctx.fallback_rate)
    s, c = hour_features(ts)
    x = {"temp": temp, "vpd": vpd_kpa(temp, hum), "soil": soil, "hour_sin": s, "hour_cos": c}
    return max(0.0, ctx.model.predict(x))


def simulate(ctx: Context, horizon_h: float, step_min: int, irrigate_at: Optional[int] = None,
             liters: float = 0.0) -> list[dict]:
    """Chuỗi dự báo; irrigate_at = chỉ số bước bắt đầu tưới (nước cộng vào đất theo %/L đã học)."""
    pts, soil = [], ctx.soil
    for k in range(int(horizon_h * 60 / step_min) + 1):
        ts = ctx.now + timedelta(minutes=k * step_min)
        if irrigate_at is not None and k == irrigate_at and ctx.pct_per_liter:
            soil = min(100.0, soil + liters * ctx.pct_per_liter)
        temp, hum = _weather_at(ctx, ts)
        pts.append({"t": ts, "soil": round(soil, 2), "temp": temp, "hum": hum})
        soil = max(0.0, soil - drying_rate(ctx, ts, soil) * step_min / 60)
    return pts


def _stress_minutes(pts: list[dict], dry_pct: float, step_min: int) -> int:
    return sum(step_min for p in pts if p["soil"] < dry_pct)


def _evap_loss(liters: float, temp: Optional[float], cfg_ai: dict) -> float:
    """Phần nước ước tính bay hơi trước khi tới rễ: tưới lúc nóng mất nhiều hơn."""
    t = 25.0 if temp is None else temp
    return liters * float(cfg_ai["evap_penalty"]) * max(0.0, t - 25) / 10


def plan_irrigation(ctx: Context, cfg: dict) -> dict:
    """Chọn thời điểm + lượng nước có chi phí nhỏ nhất trong 6 giờ tới.

    Chỉ xét tưới khi đất đã vào "vùng cần tưới" (≤ ngưỡng khô + plan_margin_pct) để không tưới lặt vặt.
    Lượng nước = vừa đủ đưa đất lên ngưỡng đủ ẩm (theo %/L đã học), tối đa max_seconds × lưu lượng.
    chi phí = nước bay hơi (tưới lúc nóng) + stress_weight × phút đất dưới ngưỡng khô (+ ưu tiên nhẹ tưới muộn)
    Nước tới rễ là nhu cầu thật của cây nên không tính là chi phí; thứ được tối ưu là nước mất và thời gian cây thiếu nước.
    """
    a, p = cfg["ai"], cfg["pump"]
    step, horizon = int(a["step_minutes"]), float(a["horizon_hours"])
    dry, target = p["soil_dry_pct"], p["soil_target_pct"]
    base = simulate(ctx, horizon, step)
    stress0 = _stress_minutes(base, dry, step)
    plan = {"forecast": base, "with_plan": base, "decision": "no_irrigation", "start_at": None, "liters": 0.0,
            "seconds": 0, "candidates": 0, "stress_minutes_without": stress0, "stress_minutes_with": stress0,
            "dry_at": next((pt["t"] for pt in base if pt["soil"] < dry), None)}
    if not ctx.pct_per_liter or not ctx.flow_lpm:
        plan["decision"] = "learning"       # chưa học xong %/L hoặc lưu lượng -> bộ điều khiển dùng ngưỡng
        return plan
    max_l = ctx.flow_lpm * p["max_seconds"] / 60
    w = float(a["stress_weight"])
    best = {"cost": w * stress0, "k": None}
    plan["cost_without"] = round(best["cost"], 4)
    window_top = dry + float(a["plan_margin_pct"])
    threshold_k = None
    for k, pt in enumerate(base):
        if pt["soil"] > window_top:
            continue
        if threshold_k is None and pt["soil"] < dry:
            threshold_k = k
        liters = min(max_l, max(0.0, (target - pt["soil"]) / ctx.pct_per_liter))
        if liters < cfg["irrigation"]["min_liters"]:
            continue
        plan["candidates"] += 1
        sim = simulate(ctx, horizon, step, k, liters)
        stress = _stress_minutes(sim, dry, step)
        loss = _evap_loss(liters, pt["temp"], a)
        cost = loss + w * stress + 1e-3 * (pt["soil"] - dry)      # hoà -> chọn lúc gần cần nhất
        if cost < best["cost"] - 1e-9:
            best = {"cost": cost, "k": k, "liters": liters, "sim": sim, "stress": stress, "loss": loss}
    if best["k"] is not None:
        k = best["k"]
        plan.update(decision="irrigate", start_at=base[k]["t"], liters=round(best["liters"], 3),
                    seconds=max(1, int(math.ceil(best["liters"] / ctx.flow_lpm * 60))), cost=round(best["cost"], 4),
                    with_plan=best["sim"], soil_at_start=base[k]["soil"], temp_at_start=base[k]["temp"],
                    stress_minutes_with=best["stress"], evap_loss_liters=round(best["loss"], 3))
    # So sánh với cách tưới theo ngưỡng (bật khi đất < ngưỡng khô) – để thấy AI tiết kiệm/giảm stress bao nhiêu
    if threshold_k is not None:
        pt = base[threshold_k]
        liters = min(max_l, (target - pt["soil"]) / ctx.pct_per_liter)
        sim = simulate(ctx, horizon, step, threshold_k, liters)
        plan["threshold_compare"] = {"start_at": pt["t"], "liters": round(liters, 3),
                                     "evap_loss_liters": round(_evap_loss(liters, pt["temp"], a), 3),
                                     "stress_minutes": _stress_minutes(sim, dry, step)}
    return plan


def risks(ctx: Context, plan: dict, cfg: dict, pump_available: bool) -> dict:
    """Điểm nguy cơ 0–1 và mức thấp / trung bình / cao."""
    a, p, m = cfg["ai"], cfg["pump"], cfg["mist"]
    horizon_min = float(a["horizon_hours"]) * 60
    dry_at = plan.get("dry_at")
    if dry_at is None:
        water = 0.0
    else:
        mins = max(0.0, (dry_at - ctx.now).total_seconds() / 60)
        water = round(1 - mins / horizon_min, 3)
        if not pump_available:
            water = min(1.0, water + 0.4)       # không tưới được (OFF / khoá / sự cố) -> nguy cơ cao hơn
        elif plan.get("decision") == "irrigate":
            water = round(water * 0.5, 3)      # đã có lịch tưới kịp thời
    temps = [pt["temp"] for pt in plan["forecast"] if pt["temp"] is not None]
    hot = m["hot_temp_c"]
    if temps:
        tmax = max(temps)
        hot_min = sum(int(a["step_minutes"]) for t in temps if t >= hot)
        heat = max(0.0, min(1.0, (tmax - (hot - 2)) / 6)) * 0.6 + min(1.0, hot_min / 180) * 0.4
        soil_min = min(pt["soil"] for pt in plan["with_plan"])
        if soil_min < p["soil_dry_pct"]:
            heat = min(1.0, heat + 0.2)         # nóng + thiếu nước = stress nặng hơn
    else:
        tmax, hot_min, heat = None, 0, 0.0
    level = lambda v: "high" if v >= 0.7 else "medium" if v >= 0.35 else "low"
    return {"water_shortage": {"score": round(max(0.0, min(1.0, water)), 3), "level": level(water),
                               "dry_at": dry_at, "pump_available": pump_available},
            "heat_stress": {"score": round(heat, 3), "level": level(heat), "temp_max": tmax,
                            "minutes_above_hot": hot_min, "hot_temp_c": hot}}
