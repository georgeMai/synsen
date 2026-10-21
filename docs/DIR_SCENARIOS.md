# Kịch bản Detect – Investigate – Resolve

Mỗi kịch bản làm được theo hai cách: trên **phần cứng thật** (`esp32-01`), hoặc trên **thiết bị giả lập**
(`sim-01`, qua tab “Mô phỏng lỗi” trên web hay `scripts/dir_drill.py`).
Ở mọi kịch bản, bước cuối giống nhau: người vận hành bấm **“Đã xử lý – kiểm chứng”**, hệ thống **tự chạy thử và đo lại**,
và chỉ đóng sự cố khi dữ liệu xác nhận lỗi đã hết. Nếu báo “đã xử lý” mà chưa sửa thật, kiểm chứng sẽ thất bại
và sự cố quay về trạng thái “Đã tiếp nhận”.

---

## Kịch bản 1 (chính) – Bơm chạy khô / tắc ống

| | Phần cứng thật | Giả lập |
|---|---|---|
| Bối cảnh | Đất < 35% → bộ điều khiển tự **bật bơm** (lý do `soil_dry`), ESP32 ack, lưu lượng ≈ 1.5–1.8 L/phút | Tab Mô phỏng lỗi trên `sim-01` |
| Gây lỗi | **Nhấc ống hút của bơm chìm ra khỏi chậu nước** (hoặc kẹp ống) | Bấm “Bơm chạy khô”, rồi bấm Bật bơm |

**Detect**
- Sau ~60 giây không có dòng chảy (lưu lượng < 0.3 L/phút), **firmware tự cắt và khoá bơm** rồi gửi event `safety_lockout/dry_run`. Backend mở incident `pump_dry_run`, `detected_by: device`.
- Cùng lúc, luật backend thấy `pump_lockout = true` (`detected_by: rules`).
- Monitor Datadog **“Bơm chạy khô”** chuyển Triggered. Webhook gộp vào cùng incident (`detected_by: datadog`) và gắn link monitor.
- Web hiện banner đỏ “1 sự cố nghiêm trọng”. MTTD ≈ 68 giây (thời gian bảo vệ có chủ đích).

**Investigate** (trên dashboard Datadog, nhóm “Bơm, nước & phun sương”)
- `agriguard.pump.state` = 1 nhưng `agriguard.flow.rate` = 0, sau đó `pump.lockout` = 1.
- `agriguard.device.online` = 1 và RSSI bình thường, nên **không phải mất kết nối**.
- Các cảm biến khác vẫn OK, nên **không phải lỗi ESP32**.
- Log của backend có `command_id` cho thấy lệnh bật đã được **ack**, nên **không phải lỗi phần mềm/MQTT**.
- Kết luận: lỗi cơ khí phía nước.
- Trên web, mở “Dòng thời gian điều tra” để xem ai phát hiện, lúc nào.

**Resolve**
1. Bấm “Tiếp nhận”. Đặt ống hút lại vào nước.
2. Bấm “Đã xử lý – kiểm chứng”, ghi chú “Châm nước vào bể”.
3. Hệ thống gửi `clear_lockout`, **chạy thử bơm 45 giây**, đo lưu lượng trung bình rồi tắt bơm.
4. Lưu lượng ≥ 0.3 L/phút: incident **resolved**, ghi bằng chứng “lưu lượng trung bình 1.61 L/phút” và MTTR. Monitor Datadog về OK.
5. Thử báo “đã xử lý” khi ống vẫn còn ngoài nước: kiểm chứng **thất bại** (“lưu lượng chỉ 0.00 L/phút”), sự cố vẫn mở.

## Kịch bản 2 – Thiết bị mất kết nối

| Gây lỗi | Thật: **rút nguồn ESP32** hoặc tắt Wi‑Fi. Giả lập: “Mất điện / mất Wi‑Fi” |
|---|---|

- **Detect:** broker phát LWT `online:false` ngay lập tức, nên backend mở `device_offline` sau ≤ 5 giây.
  Nếu mất mạng mà không có LWT, backend phát hiện sau 30 giây im lặng. Monitor “Thiết bị mất kết nối” cũng bắn.
- **Investigate:**
  - Mọi metric của **riêng** thiết bị đó dừng cùng lúc.
  - Thiết bị khác vẫn gửi dữ liệu và `tcp.can_connect` của broker vẫn OK, nên pipeline vẫn khoẻ và lỗi nằm ở thiết bị hoặc đường mạng của nó.
  - Xem `device.rssi` trước lúc mất: RSSI yếu dần là dấu hiệu Wi‑Fi.
  - Trong lúc offline, bộ điều khiển khoá tự động (interlock). Sau 120 giây, firmware tự tắt bơm và phun sương.
- **Resolve:** cấp điện lại, rồi bấm “Đã xử lý”. Hệ thống đợi dữ liệu mới (≤ 30 giây) rồi đóng sự cố. Tự động tưới chạy lại.

## Kịch bản 3 – Phun sương hết nước

| Gây lỗi | Thật: **đổ hết nước** trong máy phun sương (đĩa siêu âm vẫn chạy). Giả lập: “Máy phun hết nước” |
|---|---|

- **Detect:** máy phun đang bật (vì trời nóng) mà sau `check_minutes` độ ẩm không tăng ≥ 3%,
  nên luật mở `mist_ineffective` và monitor “Phun sương không hiệu quả” bắn.
  Để demo nhanh, đặt `check_minutes` = 1 trong tab Cài đặt.
- **Investigate:** biểu đồ Độ ẩm không khí có vùng tô “phun sương đang chạy” nhưng đường độ ẩm đi ngang.
  Relay vẫn ON (`mist.state` = 1) và `sensor.ok{dht11}` = 1, nên cảm biến không hỏng mà máy phun không ra hơi nước.
- **Resolve:** châm nước, rồi bấm “Đã xử lý”. Hệ thống chạy thử phun 120 giây và yêu cầu độ ẩm tăng ≥ 3%.
  Interlock ngăn bật lại máy phun khi sự cố chưa đóng, giúp tránh đĩa siêu âm chạy khô.

## Kịch bản 4 – Nước tưới không tới gốc (phát hiện bất thường thống kê)

| Gây lỗi | Thật: **nhấc đầu ống tưới ra khỏi chậu** (nước vẫn chảy qua YF‑S201 nhưng rơi ra ngoài). Giả lập: “Ống tưới tuột khỏi gốc” |
|---|---|

- **Chuẩn bị:** hệ thống cần ≥ 5 lần tưới bình thường để học mức phản hồi của chậu/luống (ví dụ ~12 %/L).
  Script diễn tập tự tưới ngắn cho đủ; mức đã học được lưu trong MongoDB nên chỉ làm một lần.
- **Detect:** lần tưới kế tiếp: YF‑S201 đếm 0.35 L nhưng đất chỉ +0.7% → 1.9 %/L, z = −4.7 → incident
  `irrigation_ineffective`, monitor “Đất không phản hồi khi tưới” bắn. Mọi ngưỡng tĩnh đều “xanh”: bơm chạy, có lưu lượng, cảm biến hợp lệ.
- **Investigate:** nhóm “Phản hồi đất trên mỗi lít nước” trên Datadog: cột %/L tụt xa đường baseline, z-score vượt vạch −3,
  trong khi `flow.rate` vẫn bình thường. Document lần tưới trong `irrigation_events` có đủ số liệu để giải thích.
- **Resolve:** bấm “Đã xử lý” khi chưa cắm ống → hệ thống tưới thử, z vẫn −6.5 → **từ chối đóng**. Cắm lại ống → tưới thử
  8.4 %/L (z = −1.6) → đóng kèm bằng chứng.

```bash
python scripts/dir_drill.py hose_off       # thiết bị giả lập: tự học baseline, gây lỗi, kiểm chứng (PASS/FAIL)
```
Thiết bị thật: chậu nhỏ (nước thấm 1–3 phút), `settle_seconds` = 120. Cảm biến đất cần đọc ổn định và đã hiệu chuẩn.

## Kịch bản phụ (cho phần hỏi đáp)
- **DHT11 hỏng:** rút dây DATA. Sau 3 lần đọc lỗi liên tiếp, `dht_fault` mở và phun sương tự động bị khoá.
  Kiểm chứng: 3 lần đọc hợp lệ liên tiếp.
- **Thiết bị treo lệnh:** giả lập “Thiết bị treo lệnh” làm lệnh hết 10 giây không có ack, nên `command_timeout` mở.
  SLO “Lệnh điều khiển được xác nhận trong 10 giây” bị tiêu error budget.
- **Mạng chậm:** giả lập “Mạng chậm” đẩy p95 `ingest.latency_ms` vượt 5 giây. Monitor pipeline chuyển cảnh báo,
  webhook mở incident `ingest_latency` (do Datadog phát hiện) và SLO ingest giảm.
- **Brownout:** bơm khởi động làm sụt áp, ESP32 reset (`reset_reason: brownout`), nên `power_brownout` mở.
  Khắc phục bằng tụ 1000 µF / nguồn 3A.

## Gợi ý kịch bản video (4 phút)
| Thời lượng | Nội dung |
|---|---|
| 0:00–0:30 | Bài toán, phần cứng thật trên bàn (ESP32, bơm, cảm biến lưu lượng, máy phun sương, TFT) |
| 0:30–1:15 | Dashboard web: dữ liệu realtime; đất khô → bơm tự bật → lưu lượng tăng; trời nóng → phun sương |
| 1:15–1:45 | MongoDB Atlas: time series `telemetry`, document `incidents` với timeline |
| 1:45–3:15 | **Kịch bản 1**: nhấc ống hút → TFT hiện “KHOA” → banner web → monitor Datadog đỏ → điều tra trên dashboard Datadog → đặt ống lại → “Đã xử lý” → bơm chạy thử → resolved + MTTR |
| 3:15–3:45 | Kịch bản 2 nhanh (rút điện ESP32 → LWT → cắm lại → đóng) |
| 3:45–4:00 | SLO, MTTD/MTTR theo loại; kết thúc |
