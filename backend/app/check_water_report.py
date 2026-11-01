"""Kiểm tra 2 pipeline báo cáo nước trên MongoDB THẬT (Atlas) – so với cách tính Python tương đương.

    docker compose exec backend python -m app.check_water_report

Tạo CSDL tạm `<MONGODB_DB>_pipeline_check`, sinh 14 ngày dữ liệu giả lập (telemetry time series +
irrigation_events), chạy pipeline trong MongoDB, so từng con số với `_py_*`, in bảng kết quả rồi XOÁ CSDL tạm.
Không đọc/ghi dữ liệu vận hành thật.

    docker compose exec backend python -m app.check_water_report --explain sim-01
In kế hoạch truy vấn (explain) của pipeline irrigation_events trên dữ liệu thật để xem có dùng index không.
"""
import math
import random
import sys
from datetime import timedelta

from pymongo import MongoClient

from .analytics import (_py_irrigation_daily, _py_soil_daily, day_bounds, irrigation_daily_pipeline,
                        soil_daily_pipeline)
from .config import settings
from .db import utcnow

DAYS = 14
DEV = "check-01"


def seed(db, start):
    db.create_collection("telemetry", timeseries={"timeField": "ts", "metaField": "meta", "granularity": "seconds"})
    rnd = random.Random(7)
    tele, events = [], []
    soil, t = 70.0, start
    while t < start + timedelta(days=DAYS):
        hour = (t.hour + 7) % 24
        temp = 27 + 7 * max(0.0, math.sin((hour - 6) / 12 * math.pi))
        soil -= (0.6 + 0.12 * (temp - 27)) * (5 / 60) + rnd.uniform(-0.05, 0.05)   # khô nhanh hơn khi nóng
        pump = False
        if soil < 35:                                            # tưới
            liters = round(rnd.uniform(0.8, 1.2), 3)
            bad = rnd.random() < 0.1                             # 10% lần tưới ống tuột
            soil += 0 if bad else liters * 10
            pump = True
            events.append({"device_id": DEV, "ended_at": t, "liters": liters, "pump_seconds": 40,
                           "status": "low_response" if bad else "normal",
                           "ratio_pct_per_l": -0.5 if bad else round(10 + rnd.uniform(-0.5, 0.5), 2)})
        tele.append({"ts": t, "meta": {"device_id": DEV}, "soil_moisture_pct": round(soil, 2), "soil_ok": True,
                     "air_temp_c": round(temp, 1), "pump_on": pump, "mist_on": False, "flow_lpm": 1.5 if pump else 0.0})
        t += timedelta(minutes=5)
    db.telemetry.insert_many(tele)
    db.irrigation_events.insert_many(events)
    return len(tele), len(events)


def close(a, b):
    if a is None or b is None:
        return a is None and b is None
    return abs(a - b) <= 1e-6 * max(1.0, abs(a), abs(b))


def main():
    client = MongoClient(settings.mongodb_uri)
    if len(sys.argv) == 3 and sys.argv[1] == "--explain":
        start, end = day_bounds(DAYS)
        plan = client[settings.mongodb_db].command(
            "explain", {"aggregate": "irrigation_events", "pipeline": irrigation_daily_pipeline(sys.argv[2], start, end),
                        "cursor": {}}, verbosity="queryPlanner")
        text = str(plan)
        print("Dùng index (IXSCAN):", "IXSCAN" in text, "| quét toàn bộ (COLLSCAN):", "COLLSCAN" in text)
        return
    print("MongoDB server", client.server_info()["version"])
    name = f"{settings.mongodb_db}_pipeline_check"
    client.drop_database(name)
    db = client[name]
    try:
        start, end = day_bounds(DAYS, utcnow() - timedelta(days=0))
        n_t, n_e = seed(db, start)
        print(f"Đã sinh {n_t} bản tin telemetry, {n_e} lần tưới trong {DAYS} ngày")
        irr = list(db.irrigation_events.aggregate(irrigation_daily_pipeline(DEV, start, end)))
        soil = list(db.telemetry.aggregate(soil_daily_pipeline(DEV, start, end)))
        py_irr = _py_irrigation_daily(list(db.irrigation_events.find({}, {"_id": 0})), start, end)
        py_soil = _py_soil_daily(list(db.telemetry.find({}, {"_id": 0}).sort("ts", 1)))
        bad = 0
        if len(irr) != len(py_irr) or len(soil) != len(py_soil):
            print(f"SỐ DÒNG KHÁC: irrigation {len(irr)} vs {len(py_irr)}, soil {len(soil)} vs {len(py_soil)}")
            bad += 1
        keys = ("runs", "ok_runs", "bad_runs", "liters", "wasted_liters", "pump_seconds", "avg_pct_per_l",
                "liters_avg7", "liters_trend_per_day", "liters_cumulative")
        print(f"{'ngày':<11}{'lần':>4}{'lít':>8}{'lãng phí':>9}{'TB7':>8}{'khô %/h':>9}{'nhiệt':>7}")
        for a, b, s, ps in zip(irr, py_irr, soil, py_soil):
            for k in keys:
                if not close(a.get(k), b.get(k)):
                    print("  KHÁC", a["_id"], k, a.get(k), b.get(k)); bad += 1
            for k in ("slope_avg", "dry_buckets", "temp_avg", "temp_max", "soil_min"):
                if not close(s.get(k), ps.get(k)):
                    print("  KHÁC", s["_id"], k, s.get(k), ps.get(k)); bad += 1
            day = (a["_id"] + timedelta(hours=7)).strftime("%Y-%m-%d")
            f = lambda v, n=2: "–" if v is None else f"{v:.{n}f}"
            print(f"{day:<11}{a['runs']:>4}{f(a['liters']):>8}{f(a['wasted_liters']):>9}{f(a.get('liters_avg7')):>8}"
                  f"{f(-s['slope_avg'] if s.get('slope_avg') is not None else None):>9}{f(s.get('temp_avg'), 1):>7}")
        print("KẾT QUẢ:", "PIPELINE KHỚP PYTHON ✔" if not bad else f"{bad} chỗ khác ✘")
    finally:
        client.drop_database(name)


if __name__ == "__main__":
    main()
