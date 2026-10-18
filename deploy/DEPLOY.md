# Triển khai AgriGuard: GitHub → VPS Ubuntu → https://synsen.net/agriguard

Toàn bộ hệ thống chạy trên VPS. Tên miền `synsen.net` trỏ thẳng về VPS, **không qua Vercel**.

```
Trình duyệt ──https──► synsen.net ──► Caddy (HTTPS tự động) ──► backend + web
ESP32 ───────MQTT 1883 (user/pass)──► synsen.net : Mosquitto
Datadog ─────webhook──► https://synsen.net/api/webhooks/datadog?token=...
VPS ─────────Datadog Agent──► Datadog (metrics, logs, APM)
```

## Bước 1 – Đưa code lên GitHub (trên laptop)
```powershell
cd "<thư mục dự án>"
git add . ; git commit -m "AgriGuard" ; git push
```
`.gitignore` đã chặn `.env` và `secrets.h`, nên không có secret nào bị đẩy lên GitHub.

## Bước 2 – Trỏ tên miền synsen.net về VPS
1. **Gỡ synsen.net khỏi project cũ đang chạy nó.** Nếu là Vercel: vào Project → Settings → Domains → Remove. Bỏ qua bước này thì Vercel vẫn giữ tên miền.
2. Vào nơi quản lý DNS của synsen.net (Vercel DNS, Cloudflare hoặc nhà cung cấp tên miền) và tạo:

   | Loại | Tên | Giá trị |
   |---|---|---|
   | A | `@` (synsen.net) | IP của VPS |
   | A | `www` | IP của VPS (không bắt buộc) |

   Nếu DNS quản lý bằng **Cloudflare**, chọn **DNS only** (đám mây màu xám) cho bản ghi `@`. Chế độ proxy của Cloudflare chặn cổng MQTT 1883 và làm Caddy không xin được chứng chỉ.
3. Kiểm tra trên laptop: `nslookup synsen.net` phải trả về IP của VPS. DNS có thể mất từ 5 phút đến vài giờ mới cập nhật xong.

## Bước 3 – Cài trên VPS
```bash
ssh root@<IP-VPS>
git clone https://github.com/<tài-khoản>/<repo>.git agriguard   # repo private: password = GitHub token
cd agriguard
sudo bash deploy/setup-vps.sh
```
Khi script hỏi:
- **Tên miền:** nhập `synsen.net`. Nếu DNS chưa trỏ xong, cứ giữ mặc định `*.sslip.io` để chạy trước. Khi DNS xong, chạy lại script và nhập `synsen.net`.
- **MongoDB URI:** dán chuỗi `mongodb+srv://…` của Atlas.
- **Datadog:** site, API key, Application key.

Script tự làm các việc sau:
1. Cài Docker.
2. Sinh mật khẩu và token.
3. Mở tường lửa ufw.
4. Chạy các container: Caddy, backend, Mosquitto, simulator, Datadog Agent.
5. Tạo monitors và dashboard trên Datadog.
6. In ra cấu hình cho ESP32.

## Bước 4 – Việc làm tay
- **MongoDB Atlas → Network Access:** thêm IP của VPS.
- **Tường lửa của nhà cung cấp VPS** (nếu có): mở TCP **80, 443, 1883**.
- **ESP32:** sửa `firmware/agriguard_esp32/secrets.h` theo đúng cấu hình script đã in ra, ví dụ:
  ```cpp
  #define MQTT_HOST     "synsen.net"
  #define MQTT_PORT     1883
  #define MQTT_USER     "agriguard"
  #define MQTT_PASSWORD "<script in ra>"
  ```
  Sau đó nạp lại firmware. Thiết bị `esp32-01` sẽ hiện trên web với nhãn "thật".

## Bước 5 – Kiểm tra
| Kiểm tra | Kết quả đúng |
|---|---|
| Mở https://synsen.net/agriguard/ | Dashboard có `sim-01`, `sim-02` (và `esp32-01` nếu đã nạp firmware) |
| https://synsen.net/api/health | `"mongo":"ok"`, `"mqtt":"connected"`, `"datadog_metrics":true` |
| Datadog → Dashboard AgriGuard | Có dữ liệu, monitor **Backend API** và **Broker MQTT** OK |
| Diễn tập từ laptop | `python scripts/dir_drill.py all --base https://synsen.net/agriguard --token <ADMIN_TOKEN>` → PASS |

## Vận hành
| Việc | Lệnh trên VPS (trong thư mục `agriguard`) |
|---|---|
| Cập nhật sau khi `git push` | `sudo bash deploy/update.sh` |
| Log backend / Caddy | `docker compose logs -f backend` · `docker compose logs caddy --tail 50` |
| Trạng thái | `docker compose ps` |
| Đổi tài khoản Datadog (26/10) | sửa `DD_SITE`, `DD_API_KEY`, `DD_APP_KEY` trong `.env` → `sudo bash deploy/update.sh` → `python3 datadog/bootstrap.py apply` |

## Sự cố thường gặp
- **Trình duyệt báo lỗi chứng chỉ, hoặc `docker compose logs caddy` có dòng "challenge failed":** DNS chưa trỏ về VPS, hoặc cổng 80 đang bị chặn. Sửa xong, chạy `docker compose restart caddy`.
- **VPS đã có nginx hoặc apache chiếm cổng 80/443:** cách đơn giản nhất là tắt chúng đi (`sudo systemctl disable --now nginx`). Nếu cần giữ nginx cho site khác, xoá service `caddy` khỏi cách chạy (dùng `docker compose up -d` không kèm file `docker-compose.prod.yml`, đặt `BACKEND_BIND=127.0.0.1`), rồi thêm vào nginx:
  ```nginx
  server {
      server_name synsen.net;
      location = / { return 302 /agriguard/; }
      location /agriguard/ { proxy_pass http://127.0.0.1:8000/; proxy_set_header Host $host; proxy_set_header X-Forwarded-Proto $scheme; }
      location /api/ { proxy_pass http://127.0.0.1:8000; proxy_set_header Host $host; }
      # chứng chỉ: sudo certbot --nginx -d synsen.net
  }
  ```
- **ESP32 không kết nối được MQTT:** kiểm tra cổng 1883 đã mở chưa (`nc -vz synsen.net 1883` từ laptop), và user/password trong `secrets.h` có khớp với `.env` trên VPS không.
