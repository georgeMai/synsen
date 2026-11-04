"""Cho hệ thống HỌC LẠI sau khi thay cảm biến lưu lượng (vd. YF-S201 -> YF-S401) hoặc đổi bơm / đổi chậu.

Mức "phản hồi đất trên mỗi lít" (%/L) và "lưu lượng bơm bình thường" được học từ các lần tưới cũ; số lít đo bằng
cảm biến cũ không còn so sánh được với cảm biến mới -> dễ báo nhầm "nước không tới gốc" hoặc "bơm yếu".
Lệnh này đánh dấu các lần tưới cũ của thiết bị là `excluded / sensor_changed` (vẫn giữ trong lịch sử và báo cáo nước),
nên hệ thống học lại từ các lần tưới mới (cần ~5 lần tưới để có mức chuẩn %/L, ~3 lần cho lưu lượng).

    docker compose exec backend python -m app.reset_learning esp32-01            # xem trước
    docker compose exec backend python -m app.reset_learning esp32-01 --apply    # thực hiện
    docker compose restart backend                                               # nạp lại trạng thái trong bộ nhớ
"""
import asyncio
import sys

from .db import db, utcnow


async def main(device_id: str, apply: bool) -> None:
    await db.connect()
    try:
        q = {"device_id": device_id, "reason": {"$ne": "sensor_changed"}}
        n = await db.irrigation_events.count_documents(q)
        print(f"{device_id}: {n} lần tưới cũ sẽ không còn dùng để học (%/L, lưu lượng bình thường)")
        if apply and n:
            async for ev in db.irrigation_events.find(q, {"_id": 1, "status": 1, "reason": 1}):
                await db.irrigation_events.update_one({"_id": ev["_id"]}, {"$set": {
                    "status_before": ev.get("status"), "reason_before": ev.get("reason"),
                    "status": "excluded", "reason": "sensor_changed", "relearn_at": utcnow()}})
            await db.devices.update_one({"_id": device_id}, {"$unset": {"diagnosis.pump": ""}})
            print("Đã đánh dấu. Khởi động lại backend: docker compose restart backend")
        elif n:
            print("Chạy lại với --apply để thực hiện.")
    finally:
        await db.close()


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        raise SystemExit(__doc__)
    asyncio.run(main(args[0], "--apply" in sys.argv))
