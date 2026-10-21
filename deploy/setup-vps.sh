#!/usr/bin/env bash
# AgriGuard – cài đặt trên VPS Ubuntu (chạy lại nhiều lần cũng được).
#   cd agriguard && sudo bash deploy/setup-vps.sh
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$(pwd)"

say()  { printf '\n\033[1;32m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m!! %s\033[0m\n' "$*"; }

if [ "$(id -u)" -ne 0 ]; then echo "Hãy chạy bằng sudo: sudo bash deploy/setup-vps.sh"; exit 1; fi

# ------------------------------------------------------------------ 1. Docker
if ! command -v docker >/dev/null 2>&1; then
  say "Cài Docker Engine + Compose plugin"
  curl -fsSL https://get.docker.com | sh
fi
docker compose version >/dev/null || { echo "Thiếu docker compose plugin"; exit 1; }
command -v python3 >/dev/null || apt-get install -y python3
command -v openssl >/dev/null || apt-get install -y openssl

# ------------------------------------------------------------------ 2. .env
PUBLIC_IP="$(curl -fsS4 https://api.ipify.org || curl -fsS4 https://ifconfig.me || echo "")"
[ -f .env ] || { cp .env.example .env; say "Đã tạo .env từ .env.example"; }

getenv() { python3 - "$1" <<'PY'
import re, sys
key = sys.argv[1]
for line in open(".env", encoding="utf-8"):
    m = re.match(rf"^\s*{re.escape(key)}\s*=(.*)$", line)
    if m:
        v = re.split(r"\s+#", m.group(1).strip(), maxsplit=1)[0].strip().strip('"').strip("'")
        print(v); break
PY
}
setenv() { python3 - "$1" "$2" <<'PY'
import re, sys
key, value = sys.argv[1], sys.argv[2]
lines = open(".env", encoding="utf-8").read().splitlines()
for i, line in enumerate(lines):
    if re.match(rf"^\s*{re.escape(key)}\s*=", line):
        lines[i] = f"{key}={value}"; break
else:
    lines.append(f"{key}={value}")
open(".env", "w", encoding="utf-8").write("\n".join(lines) + "\n")
PY
}
ask() {  # ask KEY "câu hỏi" "mặc định"
  local cur; cur="$(getenv "$1")"; local def="${cur:-$3}"; local ans
  read -r -p "$2 [${def}]: " ans || true
  setenv "$1" "${ans:-$def}"
}
randomize() { [ -n "$(getenv "$1")" ] || setenv "$1" "$(openssl rand -hex 16)"; }

say "Cấu hình (Enter = giữ giá trị trong ngoặc)"
DEFAULT_DOMAIN=""
[ -n "$PUBLIC_IP" ] && DEFAULT_DOMAIN="${PUBLIC_IP//./-}.sslip.io"
ask API_DOMAIN "Tên miền trỏ về VPS này (vd. synsen.net; để mặc định *.sslip.io nếu chưa đổi DNS)" "$DEFAULT_DOMAIN"
ask PUBLIC_BASE_URL "URL để Datadog gọi webhook (gọi thẳng VPS, không qua Vercel)" "https://$(getenv API_DOMAIN)"
ask WEB_URL "Địa chỉ web công khai" "https://$(getenv API_DOMAIN)/agriguard"
ask MONGODB_URI "Chuỗi kết nối MongoDB Atlas (mongodb+srv://...)" ""
ask DD_SITE "Datadog site" "datadoghq.com"
ask DD_API_KEY "Datadog API key" ""
ask DD_APP_KEY "Datadog Application key (để tạo monitors/dashboard)" ""

setenv BACKEND_BIND 127.0.0.1
setenv ROOT_PATH /agriguard
setenv METRICS_ENABLED true
setenv DD_TRACE_ENABLED true
[ -n "$(getenv MQTT_USER)" ] || setenv MQTT_USER agriguard
randomize ADMIN_TOKEN
randomize MQTT_PASSWORD
randomize DATADOG_WEBHOOK_TOKEN
chmod 600 .env

if [ -z "$(getenv MONGODB_URI)" ] || [[ "$(getenv MONGODB_URI)" == mongodb://mongo:* ]]; then
  PROFILES="--profile datadog --profile local-mongo"
  setenv MONGODB_URI mongodb://mongo:27017
  warn "Chưa có chuỗi Atlas -> dùng MongoDB chạy trong Docker trên VPS"
else
  PROFILES="--profile datadog"
fi
[ -n "$(getenv DD_API_KEY)" ] || { PROFILES="${PROFILES/--profile datadog/}"; setenv METRICS_ENABLED false; setenv DD_TRACE_ENABLED false; warn "Chưa có DD_API_KEY -> chạy không có Datadog Agent"; }

# ------------------------------------------------------------------ 3. Tường lửa
if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then
  say "Mở cổng tường lửa ufw: 22, 80, 443, 1883"
  ufw allow 22/tcp >/dev/null; ufw allow 80/tcp >/dev/null; ufw allow 443/tcp >/dev/null; ufw allow 1883/tcp >/dev/null
fi

# ------------------------------------------------------------------ 3b. Cổng 80/443 có bị chiếm?
if command -v ss >/dev/null && ss -ltnp 2>/dev/null | grep -E ':(80|443)\s' | grep -vq docker; then
  warn "Cổng 80/443 đang bị chương trình khác dùng (nginx/apache?). Caddy sẽ không khởi động được."
  ss -ltnp | grep -E ':(80|443)\s' || true
  warn "Tắt chương trình đó (vd. sudo systemctl disable --now nginx) hoặc xem mục 'VPS đã có nginx' trong deploy/DEPLOY.md"
fi

# ------------------------------------------------------------------ 4. Chạy
say "Build và khởi động container"
# shellcheck disable=SC2086
docker compose -f docker-compose.yml -f docker-compose.prod.yml $PROFILES up -d --build --remove-orphans
echo "COMPOSE_PROFILES_USED=\"$PROFILES\"" > deploy/.profiles

say "Chờ backend sẵn sàng"
for i in $(seq 1 40); do
  if curl -fsS http://127.0.0.1:8000/api/health >/dev/null 2>&1; then break; fi
  sleep 3
done
if curl -fsS http://127.0.0.1:8000/api/health; then echo; else
  warn "Backend chưa trả lời. Xem log: docker compose logs backend --tail 80"
fi

# ------------------------------------------------------------------ 5. Datadog
if [ -n "$(getenv DD_APP_KEY)" ]; then
  say "Tạo/cập nhật webhook, monitors, SLO, dashboard trên Datadog"
  python3 datadog/bootstrap.py apply || warn "bootstrap.py lỗi – xem thông báo ở trên"
fi

# ------------------------------------------------------------------ 6. Tóm tắt
API_DOMAIN="$(getenv API_DOMAIN)"
cat <<EOF

================ AgriGuard đã chạy ================
Web:                    $(getenv WEB_URL)/
ADMIN_TOKEN (nút 🔑 trên web, --token của dir_drill.py):  $(getenv ADMIN_TOKEN)

ESP32 – nhập trên trang 192.168.4.1 (Wi-Fi AgriGuard-esp32-01) hoặc điền vào secrets.h:
  #define MQTT_HOST     "${API_DOMAIN}"     // hoặc IP ${PUBLIC_IP}
  #define MQTT_PORT     1883
  #define MQTT_USER     "$(getenv MQTT_USER)"
  #define MQTT_PASSWORD "$(getenv MQTT_PASSWORD)"

Việc còn lại:
  - MongoDB Atlas → Network Access: thêm IP ${PUBLIC_IP}
  - Tường lửa của nhà cung cấp VPS: mở 80, 443, 1883 (TCP)
  - Cập nhật code sau này: sudo bash deploy/update.sh
====================================================
EOF
