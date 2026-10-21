#!/usr/bin/env python3
"""Diễn tập tự động kịch bản Detect – Investigate – Resolve trên thiết bị GIẢ LẬP.

  python scripts/dir_drill.py dry_run            # bơm chạy khô (kịch bản chính)
  python scripts/dir_drill.py offline            # thiết bị mất kết nối
  python scripts/dir_drill.py mist_empty         # phun sương hết nước
  python scripts/dir_drill.py dht_fail           # cảm biến DHT11 hỏng
  python scripts/dir_drill.py no_ack             # thiết bị không xác nhận lệnh
  python scripts/dir_drill.py hose_off           # ống tưới tuột: có nước chảy nhưng đất không ẩm lên
  python scripts/dir_drill.py all

Tuỳ chọn: --base http://localhost:8000  --device sim-01  --token <ADMIN_TOKEN>
Script dùng đúng API mà web dashboard dùng, nên cũng là bài kiểm thử end-to-end:
nó kiểm tra (1) lỗi được phát hiện, (2) kiểm chứng THẤT BẠI khi lỗi chưa sửa,
(3) kiểm chứng ĐẠT và incident đóng sau khi sửa. Thoát với mã 1 nếu có bước sai.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request

SCENARIOS = {
    # fault: (incident_type, chuẩn bị, mô tả sửa lỗi)
    "dry_run": ("pump_dry_run", "pump_on", "châm nước vào bể / thông ống hút"),
    "offline": ("device_offline", None, "cấp điện lại / khôi phục Wi-Fi"),
    "mist_empty": ("mist_ineffective", "mist_on", "châm nước cho máy phun sương"),
    "dht_fail": ("dht_fault", None, "cắm lại dây DATA của DHT11"),
    "no_ack": ("command_timeout", "send_command", "khởi động lại ESP32"),
    "hose_off": ("irrigation_ineffective", "irrigate", "cắm lại ống tưới vào gốc cây"),
}

SIM_IRRIGATION = {"settle_seconds": 30, "test_seconds": 12}   # thiết bị giả lập thấm nhanh -> rút ngắn để demo


def irrigate_once(api: "Api", device: str, seconds: int, label: str) -> dict:
    """Bật bơm `seconds` giây rồi chờ backend chấm điểm lần tưới đó."""
    time.sleep(25)   # để backend đo được tốc độ đất tự khô giữa hai lần tưới (bù bay hơi)
    before = (api("GET", f"devices/{device}/irrigation?limit=1")["events"] or [{}])[0].get("_id")
    api("POST", f"devices/{device}/actuators/pump/command", {"action": "on", "duration_s": seconds})
    ev = wait(lambda: (lambda e: e if e and e["_id"] != before else None)(
        (api("GET", f"devices/{device}/irrigation?limit=1")["events"] or [None])[0]),
        seconds + SIM_IRRIGATION["settle_seconds"] + 60, every=3, label=f"chấm điểm lần tưới ({label})")
    ratio = ev.get("ratio_pct_per_l")
    log(f"   {label}: {ev['liters']:.2f} L, đất {ev['rise_pct']:+.1f}% → "
        f"{'%.1f %%/L' % ratio if ratio is not None else '–'} [{ev['status']}{', z=' + str(ev['zscore']) if ev.get('zscore') is not None else ''}]")
    return ev


def ensure_soil_not_saturated(api: "Api", device: str) -> None:
    soil = (api("GET", f"devices/{device}").get("latest") or {}).get("soil_moisture_pct")
    limit = api("GET", "settings")["irrigation"]["max_start_soil_pct"]
    if soil is not None and soil > limit - 20:
        api("POST", f"sim/{device}/fault", {"fault": "dry_soil"})
        log(f"Đất giả lập đang {soil:.0f}% (gần bão hoà, lần tưới sẽ bị loại) → đặt lại 45% (chỉ có ở simulator)")
        time.sleep(12)


def learn_baseline(api: "Api", device: str) -> None:
    need = api("GET", f"devices/{device}/irrigation?limit=50")["learning_needed"]
    if not need:
        log("Đã có mức phản hồi bình thường (baseline) – bỏ qua bước học")
        return
    log(f"Học mức phản hồi bình thường: cần thêm {need} lần tưới hợp lệ")
    for i in range(need + 6):          # vài lần có thể bị loại (vd. phun sương đang chạy)
        irrigate_once(api, device, SIM_IRRIGATION["test_seconds"], f"tưới học #{i + 1}")
        if not api("GET", f"devices/{device}/irrigation?limit=50")["learning_needed"]:
            return
    raise RuntimeError("Không học được mức phản hồi bình thường")


class Api:
    def __init__(self, base: str, token: str) -> None:
        self.base, self.token = base.rstrip("/"), token

    def __call__(self, method: str, path: str, body=None):
        req = urllib.request.Request(self.base + "/api/" + path, method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Content-Type": "application/json", "X-Admin-Token": self.token})
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read())


def wait(pred, timeout: float, every: float = 2.0, label: str = ""):
    t0 = time.time()
    while time.time() - t0 < timeout:
        v = pred()
        if v:
            return v
        time.sleep(every)
    raise TimeoutError(f"Hết thời gian chờ: {label}")


def log(msg: str) -> None:
    print(time.strftime("%H:%M:%S"), msg, flush=True)


def run(api: Api, device: str, fault: str) -> bool:
    inc_type, prep, fix = SCENARIOS[fault]
    print(f"\n=== Kịch bản: {fault} → {inc_type} trên {device} ===")
    api("POST", f"sim/{device}/fault", {"fault": "clear"})
    for inc in api("GET", f"incidents?status=active&device_id={device}"):
        if inc["type"] == inc_type:
            sys.exit(f"Đang có incident {inc_type} mở trên {device} – xử lý nó trước khi diễn tập")
    if fault == "mist_empty":
        api("PUT", "settings", {"mist": {**api("GET", "settings")["mist"], "check_minutes": 1}})
        log("Giảm check_minutes của phun sương xuống 1 phút để demo nhanh")
    if fault == "hose_off":
        api("PUT", "settings", {"irrigation": SIM_IRRIGATION})
        api("PUT", f"devices/{device}/actuators/mist/mode", {"mode": "OFF"})   # phun sương làm nhiễu phép đo
        api("PUT", f"devices/{device}/actuators/pump/mode", {"mode": "MANUAL"})
        log(f"Thời gian chờ thấm {SIM_IRRIGATION['settle_seconds']} giây, tưới thử {SIM_IRRIGATION['test_seconds']} giây (giả lập)")
        ensure_soil_not_saturated(api, device)
        learn_baseline(api, device)

    # 1. Gây lỗi
    api("POST", f"sim/{device}/fault", {"fault": fault})
    log(f"[GÂY LỖI] tiêm lỗi '{fault}'")
    if prep == "pump_on":
        api("POST", f"devices/{device}/actuators/pump/command", {"action": "on", "duration_s": 300})
        log("Bật bơm 5 phút (thủ công)")
    elif prep == "mist_on":
        api("POST", f"devices/{device}/actuators/mist/command", {"action": "on", "duration_s": 600})
        log("Bật phun sương 10 phút (thủ công)")
    elif prep == "irrigate":
        log("Tưới một lần khi ống đã tuột – YF-S201 vẫn đếm nước nhưng đất không ẩm lên")
        irrigate_once(api, device, SIM_IRRIGATION["test_seconds"], "tưới khi ống tuột")
    elif prep == "send_command":
        time.sleep(1)
        api("POST", f"devices/{device}/actuators/mist/command", {"action": "off"})
        log("Gửi lệnh tắt phun sương – thiết bị sẽ không ack")

    # 2. DETECT
    t_fault = time.time()
    inc = wait(lambda: next((i for i in api("GET", f"incidents?status=active&device_id={device}")
                             if i["type"] == inc_type), None), 240, label="phát hiện sự cố")
    log(f"[DETECT] {inc['_id']} '{inc['title']}' sau {time.time() - t_fault:.0f}s "
        f"(phát hiện bởi {', '.join(inc['detected_by'])}, MTTD {inc['mttd_s']}s)")

    # 3. INVESTIGATE
    dev = api("GET", f"devices/{device}")
    log(f"[INVESTIGATE] latest={json.dumps(dev.get('latest'), ensure_ascii=False)}")
    for t in inc["timeline"]:
        log(f"   timeline: {t['actor']} {t['action']} – {t['note']}")
    api("POST", f"incidents/{inc['_id']}/acknowledge", {"actor": "drill", "note": "Đang điều tra"})

    # 4a. RESOLVE khi CHƯA sửa (trừ offline: chưa sửa thì không thể có dữ liệu) -> phải thất bại
    ok = True
    if fault in ("dry_run", "mist_empty", "dht_fail", "hose_off"):
        api("POST", f"incidents/{inc['_id']}/resolve", {"actor": "drill", "note": "Báo đã xử lý (nhưng chưa sửa thật)"})
        res = wait(lambda: (lambda i: i if i["status"] != "verifying" else None)(api("GET", f"incidents/{inc['_id']}")),
                   300, label="kiểm chứng (chưa sửa)")
        failed = res["status"] == "acknowledged"
        log(f"[RESOLVE – chưa sửa] kiểm chứng {'THẤT BẠI đúng như mong đợi' if failed else 'ĐẠT (SAI!)'}: "
            f"{res['timeline'][-1]['note']}")
        ok &= failed

    # 4b. Sửa lỗi rồi RESOLVE -> phải đạt
    api("POST", f"sim/{device}/fault", {"fault": "clear"})
    log(f"[SỬA] {fix} (xoá lỗi trên thiết bị giả lập)")
    time.sleep(6)
    api("POST", f"incidents/{inc['_id']}/resolve", {"actor": "drill", "note": fix})
    res = wait(lambda: (lambda i: i if i["status"] != "verifying" else None)(api("GET", f"incidents/{inc['_id']}")),
               300, label="kiểm chứng (đã sửa)")
    passed = res["status"] == "resolved"
    log(f"[RESOLVE] {'ĐÃ ĐÓNG' if passed else 'CHƯA ĐÓNG'} – {res['timeline'][-1]['note']} (MTTR {res.get('mttr_s')}s)")
    for act in ("pump", "mist"):
        api("PUT", f"devices/{device}/actuators/{act}/mode", {"mode": "AUTO"})
    return ok and passed


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("scenario", choices=[*SCENARIOS, "all"])
    p.add_argument("--base", default="http://localhost:8000")
    p.add_argument("--device", default="sim-01")
    p.add_argument("--token", default="")
    a = p.parse_args()
    api = Api(a.base, a.token)
    names = list(SCENARIOS) if a.scenario == "all" else [a.scenario]
    results = {n: run(api, a.device, n) for n in names}
    print("\nKết quả:", ", ".join(f"{k}={'PASS' if v else 'FAIL'}" for k, v in results.items()))
    sys.exit(0 if all(results.values()) else 1)


if __name__ == "__main__":
    main()
