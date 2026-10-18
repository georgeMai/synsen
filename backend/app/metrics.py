"""Gửi custom metrics tới Datadog Agent qua DogStatsD.

Tất cả metric có tiền tố `agriguard.` và tag chung env/service.
Khi METRICS_ENABLED=false (chưa có Agent) mọi lời gọi là no-op, nhưng vẫn được
đếm trong bộ nhớ để /api/health hiển thị.
"""
from __future__ import annotations

import logging
from collections import Counter
from typing import Iterable

from .config import settings

log = logging.getLogger(__name__)

PREFIX = "agriguard."


class Metrics:
    def __init__(self) -> None:
        self.local_counts: Counter[str] = Counter()
        self._statsd = None
        self._base_tags = [f"env:{settings.env}", f"service:{settings.service}"]
        if settings.metrics_enabled:
            try:
                from datadog.dogstatsd import DogStatsd

                self._statsd = DogStatsd(
                    host=settings.statsd_host,
                    port=settings.statsd_port,
                    constant_tags=self._base_tags,
                    disable_telemetry=True,
                )
                log.info("DogStatsD enabled", extra={"statsd_host": settings.statsd_host})
            except Exception:  # pragma: no cover - chỉ xảy ra khi thiếu thư viện
                log.exception("Cannot initialise DogStatsD, metrics disabled")

    @staticmethod
    def _tags(tags: dict | Iterable[str] | None) -> list[str]:
        if not tags:
            return []
        if isinstance(tags, dict):
            return [f"{k}:{v}" for k, v in tags.items() if v is not None]
        return list(tags)

    def gauge(self, name: str, value: float | int | bool | None, tags=None) -> None:
        if value is None:
            return
        self.local_counts[f"gauge:{name}"] += 1
        if self._statsd:
            self._statsd.gauge(PREFIX + name, float(value), tags=self._tags(tags))

    def increment(self, name: str, value: int = 1, tags=None) -> None:
        self.local_counts[name] += value
        if self._statsd:
            self._statsd.increment(PREFIX + name, value, tags=self._tags(tags))

    def histogram(self, name: str, value: float, tags=None) -> None:
        self.local_counts[f"hist:{name}"] += 1
        if self._statsd:
            self._statsd.histogram(PREFIX + name, value, tags=self._tags(tags))

    def distribution(self, name: str, value: float, tags=None) -> None:
        self.local_counts[f"dist:{name}"] += 1
        if self._statsd:
            self._statsd.distribution(PREFIX + name, value, tags=self._tags(tags))

    def event(self, title: str, text: str, alert_type: str = "info", tags=None) -> None:
        if self._statsd:
            self._statsd.event(title, text, alert_type=alert_type, tags=self._tags(tags) + self._base_tags)

    @property
    def enabled(self) -> bool:
        return self._statsd is not None


metrics = Metrics()


def device_tags(device_id: str, site: str | None = None, zone: str | None = None, source: str | None = None) -> dict:
    return {"device_id": device_id, "site": site, "zone": zone, "source": source}
