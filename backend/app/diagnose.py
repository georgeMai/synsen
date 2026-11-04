"""Chẩn đoán nguyên nhân cho 2 loại sự cố hay gặp nhất: mất kết nối và bơm chạy khô / tắc ống.

Nguồn dữ liệu (firmware 0.5.4 / simulator):
  * event "reconnected": ESP32 tự báo vì sao vừa mất kết nối (boot + reset_reason, mất Wi-Fi + mã lý do, RSSI
    trước khi rớt, có vừa đóng/ngắt relay không, đã mở trang cấu hình chưa, mất bao lâu).
  * event "pump_run": kết quả mỗi lần bơm (số xung YF-S401, cạnh nhiễu, thời gian mồi nước, độ ẩm đất trước/sau).
  * dữ liệu backend đang có (telemetry trước khi mất, RSSI, bơm/phun vừa bật).

Kết quả: mã nguyên nhân (tag `cause` trên metric Datadog) + câu giải thích ghi vào dòng thời gian sự cố.
Hàm thuần, dễ kiểm thử.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

WEAK_RSSI = -78
# Mã lý do ngắt Wi-Fi của ESP-IDF (wifi_err_reason_t)
WIFI_REASONS = {
    2: "hết hạn xác thực", 4: "hết hạn liên kết", 8: "ESP32 chủ động rời mạng", 15: "bắt tay WPA thất bại (sai mật khẩu?)",
    200: "mất tín hiệu router (beacon timeout)", 201: "không thấy router (router tắt / ngoài vùng phủ)",
    202: "xác thực thất bại (sai mật khẩu Wi-Fi)", 203: "router từ chối liên kết (đầy client?)",
    204: "bắt tay Wi-Fi quá hạn", 205: "kết nối Wi-Fi thất bại",
}
CAUSE_TEXT = {
    "power_brownout": "ESP32 khởi động lại do SỤT ÁP",
    "watchdog": "ESP32 bị treo và watchdog khởi động lại",
    "panic": "ESP32 lỗi phần mềm (panic) và tự khởi động lại",
    "power_loss": "ESP32 mất điện / được cắm lại nguồn",
    "reboot_other": "ESP32 khởi động lại",
    "relay_interference": "mất kết nối ngay sau khi relay bơm/phun đóng ngắt (nhiễu điện hoặc sụt áp)",
    "wifi_weak": "Wi-Fi yếu",
    "router_unreachable": "không thấy router Wi-Fi",
    "wifi_auth": "Wi-Fi từ chối kết nối (mật khẩu / router)",
    "wifi_lost": "mất Wi-Fi",
    "network_path": "Wi-Fi vẫn kết nối nhưng không tới được broker (Internet / nhà mạng / VPS)",
}
PUMP_CAUSE_TEXT = {
    "no_water": "bơm chạy nhưng không có nước qua cảm biến và đất không ẩm lên: hết nước trong bể, tắc ống hút, "
                "bơm hỏng hoặc mất nguồn bơm",
    "flow_sensor_suspect": "đất VẪN ẩm lên nhưng YF-S401 không đếm được xung: nghi cảm biến lưu lượng hỏng / tuột dây "
                           "tín hiệu (nước vẫn tới cây)",
    "flow_signal_noise": "chân tín hiệu YF-S401 có nhiều cạnh nhiễu nhưng không có xung hợp lệ: dây tín hiệu nhiễu / "
                         "thiếu điện trở kéo lên",
    "slow_priming": "xung lưu lượng đầu tiên đến muộn: mồi nước chậm (mực nước thấp, khí trong ống hút)",
}


def diagnose_reconnect(ev: dict, pump_switched_before_drop: bool = False) -> tuple[str, str]:
    """(mã nguyên nhân, câu giải thích) từ event "reconnected" của thiết bị."""
    cause = ev.get("cause")
    down = ev.get("down_s")
    rssi = ev.get("rssi_before")
    took = f", mất kết nối {int(down)} giây" if isinstance(down, (int, float)) else ""
    if cause == "boot":
        reason = ev.get("reset_reason") or ev.get("detail")
        code = {"brownout": "power_brownout", "watchdog": "watchdog", "panic": "panic",
                "power_on": "power_loss"}.get(reason, "reboot_other")
        text = CAUSE_TEXT[code]
        if code == "power_brownout":
            text += " – thường do bơm / phun sương khởi động kéo sụt nguồn: dùng nguồn riêng cho bơm, thêm tụ"
            if pump_switched_before_drop:
                text += " (bơm/phun vừa bật ngay trước khi mất kết nối)"
        return code, f"{text} (reset_reason={reason})"
    relay_ago = ev.get("relay_switch_ago_ms")
    relay_recent = isinstance(relay_ago, (int, float)) and relay_ago <= 3000 and (
        ev.get("pump_on_at_drop") or ev.get("mist_on_at_drop") or relay_ago <= 1500)
    portal = " – đã tự mở trang cấu hình Wi-Fi" if cause == "portal" else ""
    if relay_recent:
        return "relay_interference", (f"{CAUSE_TEXT['relay_interference']}: relay đổi trạng thái {int(relay_ago)} ms "
                                      f"trước khi rớt{took}{portal}")
    if cause in ("wifi_lost", "portal"):
        wr = ev.get("wifi_reason")
        why = WIFI_REASONS.get(wr, f"mã {wr}") if wr else "không rõ mã"
        if isinstance(rssi, (int, float)) and rssi <= WEAK_RSSI:
            return "wifi_weak", (f"{CAUSE_TEXT['wifi_weak']}: RSSI {int(rssi)} dBm trước khi rớt ({why}){took}{portal}"
                                 " – đưa ESP32 lại gần router / thêm bộ kích sóng")
        if wr == 201:
            return "router_unreachable", f"{CAUSE_TEXT['router_unreachable']} ({why}){took}{portal}"
        if wr in (15, 202, 203, 204):
            return "wifi_auth", f"{CAUSE_TEXT['wifi_auth']}: {why}{took}{portal}"
        return "wifi_lost", (f"{CAUSE_TEXT['wifi_lost']} ({why}, RSSI trước đó "
                             f"{int(rssi) if isinstance(rssi, (int, float)) else '–'} dBm){took}{portal}")
    return "network_path", (f"{CAUSE_TEXT['network_path']}{took}, RSSI "
                            f"{int(rssi) if isinstance(rssi, (int, float)) else '–'} dBm, trạng thái MQTT "
                            f"{ev.get('mqtt_state')}")


def diagnose_pump_run(ev: dict, min_flow_lpm: float = 0.3, slow_prime_ms: int = 4000) -> Optional[tuple[str, str]]:
    """Chẩn đoán một lần bơm; None nếu bình thường."""
    secs = float(ev.get("seconds") or 0)
    if secs < 2:
        return None
    pulses = float(ev.get("pulses") or 0)
    edges = float(ev.get("edges") or 0)
    liters = ev.get("liters")
    # lít do firmware tính theo FLOW_PULSES_PER_LITER của cảm biến đang lắp (YF-S401: 5880 xung/L, YF-S401: 450)
    lpm = (float(liters) if isinstance(liters, (int, float)) else pulses / 450) / secs * 60
    s0, s1 = ev.get("soil_start"), ev.get("soil_end")
    rise = (s1 - s0) if isinstance(s0, (int, float)) and isinstance(s1, (int, float)) else None
    if lpm < min_flow_lpm:
        detail = f"{secs:.0f} giây, {int(pulses)} xung ({lpm:.2f} L/phút)"
        if rise is not None and rise >= 2:
            return "flow_sensor_suspect", f"{PUMP_CAUSE_TEXT['flow_sensor_suspect']} – {detail}, đất +{rise:.1f}%"
        if edges >= 20 and edges > 3 * pulses:
            return "flow_signal_noise", f"{PUMP_CAUSE_TEXT['flow_signal_noise']} – {detail}, {int(edges)} cạnh"
        return "no_water", (f"{PUMP_CAUSE_TEXT['no_water']} – {detail}"
                            + (f", đất {rise:+.1f}%" if rise is not None else ""))
    first = ev.get("first_flow_ms")
    if isinstance(first, (int, float)) and first > slow_prime_ms:
        return "slow_priming", f"{PUMP_CAUSE_TEXT['slow_priming']} – xung đầu tiên sau {first / 1000:.1f} giây"
    return None


def offline_hypothesis(readings: list, now: Optional[datetime] = None) -> str:
    """Nghi vấn ban đầu khi vừa mất kết nối (trước khi ESP32 nối lại và tự báo)."""
    if not readings:
        return "Chưa có dữ liệu trước khi mất kết nối – chờ ESP32 nối lại và tự báo nguyên nhân"
    last = readings[-1]
    hints = []
    recent = [r for r in readings if (last.ts - r.ts).total_seconds() <= 20]
    switched = any(a.pump_on != b.pump_on or a.mist_on != b.mist_on for a, b in zip(recent, recent[1:]))
    if switched or last.pump_on or last.mist_on:
        hints.append("bơm/phun vừa bật hoặc đang chạy ngay trước khi mất – nghi sụt áp / nhiễu relay")
    rssi = [r.rssi_dbm for r in readings[-6:] if r.rssi_dbm]
    if rssi and min(rssi) <= WEAK_RSSI:
        hints.append(f"Wi-Fi yếu ({min(rssi)} dBm)")
    if not hints:
        return "Không thấy dấu hiệu sụt áp hay Wi-Fi yếu trước khi mất – chờ ESP32 nối lại và tự báo nguyên nhân"
    return "Nghi vấn ban đầu: " + "; ".join(hints) + " – sẽ xác nhận khi ESP32 nối lại"
