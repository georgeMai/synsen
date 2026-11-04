"""Gửi cảnh báo của Datadog tới Zalo / WhatsApp (Datadog không có tích hợp sẵn cho hai kênh này).

Luồng: monitor Datadog báo động → webhook `@webhook-agriguard-notify` → backend (/api/webhooks/datadog/notify)
→ thêm chẩn đoán nguyên nhân của AgriGuard → Zalo Bot API và/hoặc WhatsApp (CallMeBot).

Cấu hình trong .env (để trống = không gửi kênh đó):
  ZALO_BOT_TOKEN, ZALO_CHAT_ID          tạo bot tại bot.zaloplatforms.com; lấy chat_id: nhắn bot 1 tin rồi chạy
                                        `docker compose exec backend python -m app.notify zalo-chat-id`
  CALLMEBOT_PHONE, CALLMEBOT_APIKEY     WhatsApp: nhắn "I allow callmebot to send me messages" tới số bot CallMeBot
Thử: `docker compose exec backend python -m app.notify test "Xin chào"`
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
import urllib.parse
import urllib.request

from .config import settings

log = logging.getLogger(__name__)
ZALO_API = "https://bot-api.zaloplatforms.com/bot{token}/{method}"
CALLMEBOT_API = "https://api.callmebot.com/whatsapp.php"
DEDUPE_S = 300
_last_sent: dict[str, float] = {}


def channels() -> list[str]:
    out = []
    if settings.zalo_bot_token and settings.zalo_chat_id:
        out.append("zalo")
    if settings.callmebot_phone and settings.callmebot_apikey:
        out.append("whatsapp")
    return out


def _zalo(method: str, body: dict) -> dict:
    req = urllib.request.Request(ZALO_API.format(token=settings.zalo_bot_token, method=method),
                                 data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read() or b"{}")


def _whatsapp(text: str) -> None:
    q = urllib.parse.urlencode({"phone": settings.callmebot_phone, "text": text, "apikey": settings.callmebot_apikey})
    with urllib.request.urlopen(f"{CALLMEBOT_API}?{q}", timeout=15) as r:
        r.read()


def _send_sync(text: str) -> dict:
    result = {}
    for ch in channels():
        try:
            if ch == "zalo":
                _zalo("sendMessage", {"chat_id": settings.zalo_chat_id, "text": text[:2000]})
            else:
                _whatsapp(text[:1000])
            result[ch] = "ok"
        except Exception as exc:  # không để lỗi kênh nhắn tin làm hỏng webhook
            result[ch] = f"error: {str(exc)[:120]}"
            log.warning("Notify %s failed", ch, extra={"error": str(exc)[:200]})
    return result


async def send(text: str, key: str | None = None) -> dict:
    """Gửi tới mọi kênh đã cấu hình. `key`: chống gửi trùng trong 5 phút (monitor nhấp nháy)."""
    if not channels():
        return {"skipped": "no_channels"}
    now = time.time()
    if key and now - _last_sent.get(key, 0) < DEDUPE_S:
        return {"skipped": "duplicate"}
    if key:
        _last_sent[key] = now
    return await asyncio.to_thread(_send_sync, text)


def _cli() -> None:
    args = sys.argv[1:]
    if args[:1] == ["zalo-chat-id"]:
        if not settings.zalo_bot_token:
            raise SystemExit("Chưa đặt ZALO_BOT_TOKEN trong .env")
        res = _zalo("getUpdates", {"timeout": 5})
        items = res.get("result") or []
        items = items if isinstance(items, list) else [items]
        found = {(u.get("message") or {}).get("chat", {}).get("id") for u in items}
        found.discard(None)
        print("chat_id tìm thấy:", ", ".join(map(str, found)) or "(chưa có – nhắn 1 tin cho bot rồi chạy lại)")
    elif args[:1] == ["test"]:
        print("Kênh đã cấu hình:", channels() or "(chưa có)")
        print(_send_sync(" ".join(args[1:]) or "AgriGuard: thử gửi cảnh báo"))
    else:
        print(__doc__)


if __name__ == "__main__":
    _cli()
