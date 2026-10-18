#!/usr/bin/env python3
"""Diễn tập tự động kịch bản Detect – Investigate – Resolve trên thiết bị GIẢ LẬP.

  python scripts/dir_drill.py dry_run            # bơm chạy khô (kịch bản chính)
  python scripts/dir_drill.py offline            # thiết bị mất kết nối
  python scripts/dir_drill.py mist_empty         # phun sương hết nước
  python scripts/dir_drill.py dht_fail           # cảm biến DHT11 hỏng
  python scripts/dir_drill.py no_ack             # thiết bị không xác nhận lệnh
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
}


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

    # 1. Gây lỗi
    api("POST", f"sim/{device}/fault", {"fault": fault})
    log(f"[GÂY LỖI] tiêm lỗi '{fault}'")
    if prep == "pump_on":
        api("POST", f"devices/{device}/actuators/pump/command", {"action": "on", "duration_s": 300})
        log("Bật bơm 5 phút (thủ công)")
    elif prep == "mist_on":
        api("POST", f"devices/{device}/actuators/mist/command", {"action": "on", "duration_s": 600})
        log("Bật phun sương 10 phút (thủ công)")
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
    if fault in ("dry_run", "mist_empty", "dht_fail"):
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
