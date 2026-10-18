"""Cầu nối MQTT (paho-mqtt, luồng mạng riêng) <-> asyncio.

Nhận: agri/{site}/{device}/{telemetry|status|ack|event}
Gửi:  agri/{site}/{device}/cmd  và  agri/{site}/{device}/sim/fault (chỉ thiết bị giả lập)
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from typing import Awaitable, Callable

import paho.mqtt.client as mqtt

from .config import settings
from .metrics import metrics

log = logging.getLogger(__name__)

SUB_KINDS = ("telemetry", "status", "ack", "event")
Handler = Callable[[str, str, str, dict, datetime], Awaitable[None]]


class MqttBridge:
    def __init__(self) -> None:
        self.queue: asyncio.Queue | None = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self.connected = False
        self.client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id=f"agriguard-backend-{int(time.time())}",
            clean_session=True,
        )
        if settings.mqtt_user:
            self.client.username_pw_set(settings.mqtt_user, settings.mqtt_password or None)
        self.client.reconnect_delay_set(min_delay=1, max_delay=10)
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_message

    # ---- callbacks chạy trong luồng của paho ----
    def _on_connect(self, client, userdata, flags, reason_code, properties):
        if reason_code.is_failure:
            log.error("MQTT connect failed: %s", reason_code)
            return
        self.connected = True
        root = settings.mqtt_topic_root
        for kind in SUB_KINDS:
            client.subscribe(f"{root}/+/+/{kind}", qos=1)
        log.info("MQTT connected", extra={"mqtt_host": settings.mqtt_host})
        metrics.increment("mqtt.connects")

    def _on_disconnect(self, client, userdata, flags, reason_code, properties):
        self.connected = False
        log.warning("MQTT disconnected: %s", reason_code)
        metrics.increment("mqtt.disconnects")

    def _on_message(self, client, userdata, msg):
        received = datetime.now(timezone.utc)
        if self.loop and self.queue:
            self.loop.call_soon_threadsafe(self.queue.put_nowait, (msg.topic, msg.payload, received))

    # ---- API asyncio ----
    async def start(self, handler: Handler) -> None:
        self.loop = asyncio.get_running_loop()
        self.queue = asyncio.Queue(maxsize=10000)
        self.client.connect_async(settings.mqtt_host, settings.mqtt_port, keepalive=30)
        self.client.loop_start()
        while True:
            topic, payload, received = await self.queue.get()
            parts = topic.split("/")
            if len(parts) != 4:
                continue
            _, site, device_id, kind = parts
            try:
                data = json.loads(payload)
                if not isinstance(data, dict):
                    raise ValueError("payload is not a JSON object")
            except (ValueError, UnicodeDecodeError):
                metrics.increment("ingest.invalid_payload", tags={"device_id": device_id, "kind": kind})
                log.warning("Invalid MQTT payload", extra={"topic": topic})
                continue
            metrics.increment("mqtt.messages_received", tags={"kind": kind, "site": site})
            try:
                await handler(site, device_id, kind, data, received)
            except Exception:
                metrics.increment("ingest.errors", tags={"kind": kind})
                log.exception("Failed to handle MQTT message", extra={"topic": topic, "device_id": device_id})

    def publish(self, topic: str, payload: dict, qos: int = 1, retain: bool = False) -> bool:
        info = self.client.publish(topic, json.dumps(payload), qos=qos, retain=retain)
        return info.rc == mqtt.MQTT_ERR_SUCCESS

    def command_topic(self, site: str, device_id: str) -> str:
        return f"{settings.mqtt_topic_root}/{site}/{device_id}/cmd"

    def sim_fault_topic(self, site: str, device_id: str) -> str:
        return f"{settings.mqtt_topic_root}/{site}/{device_id}/sim/fault"

    def stop(self) -> None:
        self.client.loop_stop()
        self.client.disconnect()


bridge = MqttBridge()
