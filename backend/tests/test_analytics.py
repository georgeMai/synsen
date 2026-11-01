"""Kiểm thử báo cáo nước theo ngày (analytics.py) – phần Python tương đương pipeline, không cần MongoDB."""
from datetime import datetime, timedelta, timezone

from app.analytics import (_py_irrigation_daily, _py_soil_daily, day_bounds, irrigation_daily_pipeline,
                           soil_daily_pipeline)

NOW = datetime(2026, 10, 30, 5, 0, tzinfo=timezone.utc)        # 12:00 giờ VN ngày 30/10
START, END = day_bounds(5, NOW)                                 # 26/10 .. 30/10 (giờ VN)


def ev(day_offset, hour_utc, liters, status="normal", ratio=10.0):
    t = START + timedelta(days=day_offset, hours=hour_utc)
    return {"ended_at": t, "liters": liters, "status": status, "pump_seconds": 30, "ratio_pct_per_l": ratio}


def test_day_bounds_are_vietnam_midnight():
    assert START == datetime(2026, 10, 25, 17, 0, tzinfo=timezone.utc)   # 0h 26/10 giờ VN
    assert END - START == timedelta(days=5)


def test_daily_rows_fill_missing_days_and_count_waste():
    events = [ev(0, 2, 1.0), ev(0, 9, 1.0), ev(2, 3, 2.0), ev(4, 1, 0.5, "low_response", -1.0)]
    rows = _py_irrigation_daily(events, START, END)
    assert [r["runs"] for r in rows] == [2, 0, 1, 0, 1]             # ngày không tưới vẫn có dòng = 0
    assert rows[0]["liters"] == 2.0 and rows[0]["ok_runs"] == 2
    assert rows[4]["wasted_liters"] == 0.5 and rows[4]["bad_runs"] == 1
    assert rows[4]["avg_pct_per_l"] is None                         # %/L bất thường không tính vào trung bình
    assert rows[0]["liters_avg7"] is None                           # chưa có ngày trước
    assert rows[3]["liters_avg7"] == (2.0 + 0 + 2.0) / 3            # trung bình các ngày TRƯỚC
    assert rows[-1]["liters_cumulative"] == 4.5


def test_event_late_evening_utc_counts_for_next_local_day():
    # 18:00 UTC ngày 26/10 = 01:00 giờ VN ngày 27/10
    e = {"ended_at": datetime(2026, 10, 26, 18, 0, tzinfo=timezone.utc), "liters": 1.0, "status": "normal"}
    rows = _py_irrigation_daily([e], START, END)
    assert [r["runs"] for r in rows] == [0, 1, 0, 0, 0]


def tele(t, soil, pump=False, mist=False):
    return {"ts": t, "soil_moisture_pct": soil, "soil_ok": True, "air_temp_c": 30.0,
            "pump_on": pump, "mist_on": mist, "flow_lpm": 1.0 if pump else 0.0}


def test_drying_rate_ignores_watering_hour():
    t0 = START + timedelta(hours=2)
    rows = []
    for i in range(36):                                             # 6 giờ, mỗi 10 phút
        t = t0 + timedelta(minutes=10 * i)
        rows.append(tele(t, 60 - i * 0.5))                          # khô đều 3 %/giờ
    # một lần tưới giữa chừng: độ ẩm nhảy lên, 60 phút sau đó không được tính
    rows[18] = tele(rows[18]["ts"], 80, pump=True)
    out = _py_soil_daily(rows)
    assert len(out) == 1
    assert abs(-out[0]["slope_avg"] - 3.0) < 0.01
    assert out[0]["temp_avg"] == 30.0


def test_pipelines_use_window_functions():
    p = irrigation_daily_pipeline("sim-01", START, END)
    stages = [next(iter(s)) for s in p]
    assert stages == ["$match", "$group", "$densify", "$fill", "$setWindowFields", "$sort"]
    q = soil_daily_pipeline("sim-01", START, END)
    assert "$derivative" in q[2]["$setWindowFields"]["output"]["slope"]
