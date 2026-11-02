"""Ghi lại cài đặt mặc định trong code (DEFAULT_SETTINGS) vào MongoDB.

Cài đặt được lưu trong MongoDB ngay lần chạy đầu tiên; sau đó sửa DEFAULT_SETTINGS trong code
KHÔNG ảnh hưởng tới CSDL đã có. Chạy lệnh này để áp giá trị mới:

    docker compose exec backend python -m app.reset_settings              # chỉ nhóm irrigation
    docker compose exec backend python -m app.reset_settings pump mist     # các nhóm khác
    docker compose exec backend python -m app.reset_settings all
"""
import asyncio
import sys

from .config import settings
from .db import DEFAULT_SETTINGS, db, utcnow


async def main(groups: list[str]) -> None:
    if not groups:
        groups = ["irrigation"]
    if groups == ["all"]:
        groups = list(DEFAULT_SETTINGS)
    unknown = [g for g in groups if g not in DEFAULT_SETTINGS]
    if unknown:
        raise SystemExit(f"Nhóm không hợp lệ: {unknown}. Có: {list(DEFAULT_SETTINGS)} hoặc all")
    await db.connect()
    try:
        update = {g: DEFAULT_SETTINGS[g] for g in groups}
        await db.settings.update_one({"_id": settings.site},
                                     {"$set": {**update, "updated_by": "reset_settings", "updated_at": utcnow()}},
                                     upsert=True)
        for g in groups:
            print(f"[{g}]", DEFAULT_SETTINGS[g])
        print("Đã ghi vào MongoDB. Backend tự nhận giá trị mới trong vòng 5 giây.")
    finally:
        await db.close()


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:]))
