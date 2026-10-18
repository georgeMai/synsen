#!/usr/bin/env python3
"""Tạo / cập nhật toàn bộ cấu hình Datadog của AgriGuard bằng API (idempotent).

  python datadog/bootstrap.py apply      # webhook + monitors + SLOs + dashboard
  python datadog/bootstrap.py export     # ghi definitions ra datadog/*.json (để nộp bài / review)
  python datadog/bootstrap.py delete     # xoá monitors/SLO/dashboard có tag app:agriguard

Biến môi trường (đọc thêm từ file .env ở thư mục gốc nếu có):
  DD_API_KEY, DD_APP_KEY, DD_SITE (vd. us5.datadoghq.com),
  PUBLIC_BASE_URL (vd. https://synsen.net/agriguard), DATADOG_WEBHOOK_TOKEN
Chỉ dùng thư viện chuẩn Python – không cần cài thêm gì.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import definitions as defs  # noqa: E402


def load_env() -> None:
    env_file = HERE.parent / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


class Datadog:
    def __init__(self) -> None:
        self.api_key = os.environ.get("DD_API_KEY", "")
        self.app_key = os.environ.get("DD_APP_KEY", "")
        site = os.environ.get("DD_SITE", "datadoghq.com")
        self.base = f"https://api.{site}"
        if not self.api_key or not self.app_key:
            sys.exit("Thiếu DD_API_KEY hoặc DD_APP_KEY (Organization Settings → API Keys / Application Keys)")

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
        hook = {"name": defs.WEBHOOK_NAME, "url": f"{base_url}/api/webhooks/datadog?token={token}",
                "payload": json.dumps(defs.WEBHOOK_PAYLOAD), "encode_as": "json"}
        path = "/api/v1/integration/webhooks/configuration/webhooks"
        try:
            dd.call("POST", path, hook)
            print(f"✓ webhook @{defs.WEBHOOK_NAME} created")
        except RuntimeError:
            dd.call("PUT", f"{path}/{defs.WEBHOOK_NAME}", {k: v for k, v in hook.items() if k != "name"})
            print(f"✓ webhook @{defs.WEBHOOK_NAME} updated")
    else:
        print("! Bỏ qua webhook: cần PUBLIC_BASE_URL và DATADOG_WEBHOOK_TOKEN (Datadog phải gọi được backend từ Internet)")

    existing = {m["name"]: m["id"] for m in dd.call("GET", "/api/v1/monitor", query={"monitor_tags": defs.APP_TAG})}
    for mon in defs.MONITORS:
        if mon["name"] in existing:
            dd.call("PUT", f"/api/v1/monitor/{existing[mon['name']]}", mon)
            print(f"✓ monitor updated: {mon['name']}")
        else:
            dd.call("POST", "/api/v1/monitor", mon)
            print(f"✓ monitor created: {mon['name']}")

    slo_ids = []
    current = {s["name"]: s["id"] for s in dd.call("GET", "/api/v1/slo", query={"tags_query": defs.APP_TAG}).get("data", [])}
    for slo in defs.SLOS:
        if slo["name"] in current:
            dd.call("PUT", f"/api/v1/slo/{current[slo['name']]}", slo)
            slo_ids.append(current[slo["name"]])
            print(f"✓ SLO updated: {slo['name']}")
        else:
            res = dd.call("POST", "/api/v1/slo", slo)
            slo_ids.append(res["data"][0]["id"])
            print(f"✓ SLO created: {slo['name']}")

    board = defs.dashboard(slo_ids)
    dashboards = dd.call("GET", "/api/v1/dashboard").get("dashboards", [])
    match = next((d for d in dashboards if d["title"] == defs.DASHBOARD_TITLE), None)
    if match:
        res = dd.call("PUT", f"/api/v1/dashboard/{match['id']}", board)
    else:
        res = dd.call("POST", "/api/v1/dashboard", board)
    print(f"✓ dashboard: https://app.{os.environ.get('DD_SITE', 'datadoghq.com')}{res.get('url', '')}")


def delete(dd: Datadog) -> None:
    for m in dd.call("GET", "/api/v1/monitor", query={"monitor_tags": defs.APP_TAG}):
        dd.call("DELETE", f"/api/v1/monitor/{m['id']}")
        print("✗ monitor", m["name"])
    for s in dd.call("GET", "/api/v1/slo", query={"tags_query": defs.APP_TAG}).get("data", []):
        dd.call("DELETE", f"/api/v1/slo/{s['id']}")
        print("✗ SLO", s["name"])
    for d in dd.call("GET", "/api/v1/dashboard").get("dashboards", []):
        if d["title"] == defs.DASHBOARD_TITLE:
            dd.call("DELETE", f"/api/v1/dashboard/{d['id']}")
            print("✗ dashboard", d["title"])


def export() -> None:
    (HERE / "monitors.json").write_text(json.dumps(defs.MONITORS, ensure_ascii=False, indent=2), encoding="utf-8")
    (HERE / "slos.json").write_text(json.dumps(defs.SLOS, ensure_ascii=False, indent=2), encoding="utf-8")
    (HERE / "dashboard.json").write_text(json.dumps(defs.dashboard(), ensure_ascii=False, indent=2), encoding="utf-8")
    (HERE / "webhook_payload.json").write_text(json.dumps(defs.WEBHOOK_PAYLOAD, indent=2), encoding="utf-8")
    print("✓ wrote monitors.json, slos.json, dashboard.json, webhook_payload.json")


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
