"""Xoá link Datadog CŨ trong các sự cố (sau khi chuyển sang tài khoản Datadog mới).

Link "Mở trong Datadog", "mở monitor" và "mở trace" trong dòng thời gian được lưu vào MongoDB lúc webhook của tài khoản cũ
gọi về, nên vẫn trỏ tới monitor của tài khoản cũ. Lệnh này xoá các link đó (không xoá sự cố, không xoá ghi chú).
Hàng link "Điều tra trên Datadog" (Monitor / Logs / Sự kiện / Metric) không bị ảnh hưởng: web tự tạo theo DD_SITE.

    docker compose exec backend python -m app.clear_datadog_links            # xem trước, không sửa gì
    docker compose exec backend python -m app.clear_datadog_links --apply    # xoá thật
"""
import asyncio
import sys

from .db import db


async def main(apply: bool) -> None:
    await db.connect()
    try:
        n_inc = n_tl = 0
        async for inc in db.incidents.find({}):
            dd = inc.get("datadog") or {}
            has_link = bool(dd.get("link") or dd.get("monitor_id"))
            timeline = inc.get("timeline") or []
            new_tl, removed = [], 0
            for t in timeline:
                if t.get("link") or t.get("trace_id"):     # monitor / trace của tài khoản cũ
                    t = {k: v for k, v in t.items() if k not in ("link", "trace_id")}
                    removed += 1
                new_tl.append(t)
            if not has_link and not removed:
                continue
            n_inc += 1
            n_tl += removed
            print(f"{inc['_id']} [{inc.get('status')}] {inc.get('title')}: link sự cố {'có' if has_link else '–'}, "
                  f"{removed} link trong dòng thời gian")
            if apply:
                update = {"$set": {"timeline": new_tl}}
                if has_link:
                    update["$unset"] = {"datadog.link": "", "datadog.monitor_id": ""}
                await db.incidents.update_one({"_id": inc["_id"]}, update)
        verb = "Đã xoá" if apply else "Sẽ xoá"
        print(f"{verb} link Datadog cũ ở {n_inc} sự cố ({n_tl} link trong dòng thời gian).")
        if not apply and n_inc:
            print("Chạy lại với --apply để xoá thật.")
    finally:
        await db.close()


if __name__ == "__main__":
    asyncio.run(main("--apply" in sys.argv[1:]))
