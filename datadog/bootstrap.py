#!/usr/bin/env python3
"""Tạo / cập nhật toàn bộ cấu hình Datadog của AgriGuard bằng API (idempotent).

  python datadog/bootstrap.py apply      # webhook + monitors + SLOs + dashboard
  python datadog/bootstrap.py export     # ghi definitions ra datadog/*.json (để nộp bài / review)
  python datadog/bootstrap.py delete     # xoá monitors/SLO/dashboard có tag app:agriguard

Biến môi trường (đọc thêm từ file .env ở thư mục gốc nếu có):
  DD_API_KEY, DD_APP_KEY, DD_SITE (vd. us5.datadoghq.com),
  PUBLIC_BASE_URL (vd. https://synsen.net/agriguard), DATADOG_WEBHOOK_TOKEN,
  DD_NOTIFY_HANDLES (tuỳ chọn, vd. "@ban@gmail.com" – email nhận cảnh báo của các monitor gắn tag notify:yes)
Chỉ dùng thư viện chuẩn Python – không cần cài thêm gì.
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import definitions as defs  # noqa: E402


def _clean(value: str) -> str:
    value = value.strip()
    if value[:1] in "\"'" and value[-1:] == value[:1]:
        return value[1:-1]
    return re.split(r"\s+#", value, maxsplit=1)[0].strip()   # bỏ chú thích cuối dòng


def load_env() -> None:
    env_file = HERE.parent / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), _clean(v))


def normalize_site(site: str) -> str:
    """Chấp nhận cả 'app.datadoghq.com/', 'https://us5.datadoghq.com' ... -> 'datadoghq.com', 'us5.datadoghq.com'."""
    site = re.sub(r"^https?://", "", site.strip()).strip("/")
    site = re.sub(r"^(app|api)\.", "", site)
    return site or "datadoghq.com"


class Datadog:
    def __init__(self) -> None:
        self.api_key = os.environ.get("DD_API_KEY", "").strip()
        self.app_key = os.environ.get("DD_APP_KEY", "").strip()
        self.site = normalize_site(os.environ.get("DD_SITE", "datadoghq.com"))
        self.base = f"https://api.{self.site}"
        if not self.api_key or not self.app_key:
            sys.exit("Thiếu DD_API_KEY hoặc DD_APP_KEY (Organization Settings → API Keys / Application Keys)")
        print(f"Site: {self.site} | API key ...{self.api_key[-4:]} ({len(self.api_key)} ký tự, cần 32) | "
              f"App key ...{self.app_key[-4:]} ({len(self.app_key)} ký tự, cần 40)")
        self.preflight()

    def preflight(self) -> None:
        """Tách bạch lỗi API key và lỗi Application key trước khi tạo cấu hình."""
        req = urllib.request.Request(self.base + "/api/v1/validate", headers={"DD-API-KEY": self.api_key})
        try:
            urllib.request.urlopen(req, timeout=20).read()
            print("✓ API key hợp lệ với site", self.site)
        except urllib.error.URLError as e:
            if not isinstance(e, urllib.error.HTTPError):
                sys.exit(f"✗ Không kết nối được {self.base}: {e.reason}. Kiểm tra DD_SITE, mạng/VPN/diệt virus.")
            sys.exit(f"✗ API key KHÔNG hợp lệ với site {self.site} (HTTP {e.code}). "
                     "Kiểm tra DD_SITE (vd. datadoghq.com, us5.datadoghq.com) và chép lại API key.")
        try:
            self.call("GET", "/api/v1/monitor", query={"page_size": 1})
            print("✓ Application key hợp lệ")
        except RuntimeError as e:
            if "HTTP 401" in str(e) or "HTTP 403" in str(e):
                sys.exit("✗ Application key bị từ chối. Cách sửa:\n"
                         "  1. Organization Settings → Application Keys → New Key, chép NGAY giá trị key "
                         "(chuỗi 40 ký tự, KHÔNG phải 'Key ID').\n"
                         "  2. Key phải tạo trong CÙNG tổ chức/tài khoản với API key.\n"
                         "  3. Nếu key có 'Scopes', cấp: monitors_read, monitors_write, dashboards_read, "
                         "dashboards_write, slos_read, slos_write (hoặc để không giới hạn scope).\n"
                         f"  Chi tiết: {e}")
            raise

    def call(self, method: str, path: str, body=None, query: dict | None = None):
        url = self.base + path + ("?" + urllib.parse.urlencode(query) if query else "")
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers={
            "DD-API-KEY": self.api_key, "DD-APPLICATION-KEY": self.app_key,
            "Content-Type": "application/json", "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as res:
                raw = res.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")
            raise RuntimeError(f"{method} {path} -> HTTP {e.code}: {detail}") from None


def apply(dd: Datadog) -> None:
    base_url = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
    token = os.environ.get("DATADOG_WEBHOOK_TOKEN", "")
    if base_url and token:
        path = "/api/v1/integration/webhooks/configuration/webhooks"
        for name, route in ((defs.WEBHOOK_NAME, "/api/webhooks/datadog"),
                            (defs.NOTIFY_WEBHOOK_NAME, "/api/webhooks/datadog/notify")):
            hook = {"name": name, "url": f"{base_url}{route}?token={token}",
                    "payload": json.dumps(defs.WEBHOOK_PAYLOAD), "encode_as": "json"}
            try:
                dd.call("POST", path, hook)
                print(f"✓ webhook @{name} created")
            except RuntimeError:
                dd.call("PUT", f"{path}/{name}", {k: v for k, v in hook.items() if k != "name"})
                print(f"✓ webhook @{name} updated")
    else:
        print("! Bỏ qua webhook: cần PUBLIC_BASE_URL và DATADOG_WEBHOOK_TOKEN (Datadog phải gọi được backend từ Internet)")

    existing = {m["name"]: m["id"] for m in dd.call("GET", "/api/v1/monitor", query={"monitor_tags": defs.APP_TAG})}
    failed = []
    # Monitor gắn tag notify:yes: thêm kênh nhận cảnh báo (webhook Zalo/WhatsApp qua backend + email trong DD_NOTIFY_HANDLES)
    handles = os.environ.get("DD_NOTIFY_HANDLES", "").strip()
    extra = " ".join(x for x in ([f"@webhook-{defs.NOTIFY_WEBHOOK_NAME}"] if base_url and token else []) + [handles] if x)
    for mon in defs.MONITORS:
        if extra and defs.NOTIFY_TAG in mon.get("tags", []):
            mon = {**mon, "message": f"{mon['message']}\n\n{extra}"}
        try:
            if mon["name"] in existing:
                dd.call("PUT", f"/api/v1/monitor/{existing[mon['name']]}", mon)
                print(f"✓ monitor updated: {mon['name']}")
            else:
                dd.call("POST", "/api/v1/monitor", mon)
                print(f"✓ monitor created: {mon['name']}")
        except RuntimeError as e:   # một monitor lỗi không chặn phần còn lại
            failed.append(mon["name"])
            print(f"✗ monitor FAILED: {mon['name']}\n    {e}")

    slo_ids = []
    current = {s["name"]: s["id"] for s in dd.call("GET", "/api/v1/slo", query={"tags_query": defs.APP_TAG}).get("data", [])}
    for slo in defs.SLOS:
        try:
            if slo["name"] in current:
                dd.call("PUT", f"/api/v1/slo/{current[slo['name']]}", slo)
                slo_ids.append(current[slo["name"]])
                print(f"✓ SLO updated: {slo['name']}")
            else:
                res = dd.call("POST", "/api/v1/slo", slo)
                slo_ids.append(res["data"][0]["id"])
                print(f"✓ SLO created: {slo['name']}")
        except RuntimeError as e:
            failed.append(slo["name"])
            print(f"✗ SLO FAILED: {slo['name']}\n    {e}")

    dashboards = dd.call("GET", "/api/v1/dashboard").get("dashboards", [])
    for board in (defs.dashboard(slo_ids), defs.dir_dashboard(slo_ids)):
        match = next((d for d in dashboards if d["title"] == board["title"]), None)
        try:
            if match:
                res = dd.call("PUT", f"/api/v1/dashboard/{match['id']}", board)
            else:
                res = dd.call("POST", "/api/v1/dashboard", board)
            print(f"✓ dashboard \"{board['title']}\" (id {res.get('id')}): https://app.{dd.site}{res.get('url', '')}")
        except RuntimeError as e:
            failed.append(board["title"])
            print(f"✗ dashboard FAILED: {board['title']}\n    {e}")
    if failed:
        print(f"\n! {len(failed)} mục lỗi: {', '.join(failed)} – gửi phần thông báo lỗi ở trên để sửa, rồi chạy lại apply.")
    else:
        print(f"\n✓ Hoàn tất: {len(defs.MONITORS)} monitors, {len(defs.SLOS)} SLO, 2 dashboard.")


def delete(dd: Datadog) -> None:
    for m in dd.call("GET", "/api/v1/monitor", query={"monitor_tags": defs.APP_TAG}):
        dd.call("DELETE", f"/api/v1/monitor/{m['id']}")
        print("✗ monitor", m["name"])
    for s in dd.call("GET", "/api/v1/slo", query={"tags_query": defs.APP_TAG}).get("data", []):
        dd.call("DELETE", f"/api/v1/slo/{s['id']}")
        print("✗ SLO", s["name"])
    for d in dd.call("GET", "/api/v1/dashboard").get("dashboards", []):
        if d["title"] in (defs.DASHBOARD_TITLE, defs.DIR_DASHBOARD_TITLE):
            dd.call("DELETE", f"/api/v1/dashboard/{d['id']}")
            print("✗ dashboard", d["title"])


def export() -> None:
    (HERE / "monitors.json").write_text(json.dumps(defs.MONITORS, ensure_ascii=False, indent=2), encoding="utf-8")
    (HERE / "slos.json").write_text(json.dumps(defs.SLOS, ensure_ascii=False, indent=2), encoding="utf-8")
    (HERE / "dashboard.json").write_text(json.dumps(defs.dashboard(), ensure_ascii=False, indent=2), encoding="utf-8")
    (HERE / "dashboard_dir.json").write_text(json.dumps(defs.dir_dashboard(), ensure_ascii=False, indent=2),
                                             encoding="utf-8")
    (HERE / "webhook_payload.json").write_text(json.dumps(defs.WEBHOOK_PAYLOAD, indent=2), encoding="utf-8")
    print("✓ wrote monitors.json, slos.json, dashboard.json, dashboard_dir.json, webhook_payload.json")


if __name__ == "__main__":
    load_env()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "apply"
    if cmd == "export":
        export()
    elif cmd == "apply":
        apply(Datadog())
    elif cmd == "delete":
        delete(Datadog())
    else:
        sys.exit(__doc__)
