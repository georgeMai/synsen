"""Điểm vào FastAPI: khởi động MongoDB, MQTT, các vòng lặp nền và phục vụ web dashboard."""
from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from .api import router
from .config import settings
from .db import db
from .engine import bootstrap_runtime, control_tick, health_tick, run_forever
from .ingest import handle_message
from .logging_setup import setup_logging
from .mqtt_bridge import bridge
from .verify import resume_pending

setup_logging(os.getenv("LOG_LEVEL", "INFO"))
log = logging.getLogger("agriguard")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.connect()
    await bootstrap_runtime()
    tasks = [
        asyncio.create_task(bridge.start(handle_message), name="mqtt"),
        asyncio.create_task(run_forever("health", health_tick, settings.health_interval_s), name="health"),
        asyncio.create_task(run_forever("control", control_tick, settings.control_interval_s), name="control"),
    ]
    await resume_pending()
    log.info("AgriGuard backend started", extra={"site": settings.site, "env": settings.env})
    try:
        yield
    finally:
        for t in tasks:
            t.cancel()
        bridge.stop()
        await db.close()


app = FastAPI(title="AgriGuard API", version="1.0.0", lifespan=lifespan, root_path=settings.root_path,
              description="Giám sát IoT nông nghiệp thời gian thực – MongoDB + Datadog")
app.include_router(router)

web_dir = Path(settings.web_dir) if settings.web_dir else Path(__file__).resolve().parents[2] / "web"
if web_dir.is_dir():
    app.mount("/", StaticFiles(directory=web_dir, html=True), name="web")
else:
    log.warning("Web directory not found: %s", web_dir)
