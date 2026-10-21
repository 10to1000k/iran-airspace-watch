"""سرور سلامت سبک برای probeهای OpenShift/Kubernetes.

بدون وابستگی خارجی، روی http.server استاندارد.
هدف: اگر حلقه‌ی اصلی کد hang کرد، k8s خودش pod را ری‌استارت کند.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

log = logging.getLogger(__name__)


class HealthState:
    def __init__(self, stale_seconds: int = 120):
        self.stale_seconds = stale_seconds
        self.started = time.time()
        self._last_touch = time.monotonic()
        self._polls: dict[str, float] = {}
        self._published = 0
        self._lock = threading.Lock()

    # ── به‌روزرسانی ──────────────────────────────────────────
    def touch(self) -> None:
        with self._lock:
            self._last_touch = time.monotonic()

    def mark_poll(self, name: str) -> None:
        with self._lock:
            self._last_touch = time.monotonic()
            self._polls[name] = time.time()

    def mark_publish(self) -> None:
        with self._lock:
            self._published += 1
            self._last_touch = time.monotonic()

    # ── وضعیت ────────────────────────────────────────────────
    def age(self) -> float:
        return time.monotonic() - self._last_touch

    def healthy(self) -> bool:
        return self.age() < self.stale_seconds

    def snapshot(self) -> dict:
        with self._lock:
            age = time.monotonic() - self._last_touch
            return {
                "status": "ok" if age < self.stale_seconds else "stale",
                "uptime_seconds": round(time.time() - self.started),
                "seconds_since_heartbeat": round(age, 1),
                "published": self._published,
                "last_poll_utc": {
                    name: time.strftime("%H:%M:%S", time.gmtime(value))
                    for name, value in self._polls.items()
                },
            }


class _Handler(BaseHTTPRequestHandler):
    state: HealthState  # در زیرکلاس مقدار می‌گیرد

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path not in ("/healthz", "/readyz"):
            self.send_response(404)
            self.end_headers()
            return
        healthy = self.state.healthy()
        body = json.dumps(self.state.snapshot(), ensure_ascii=False).encode("utf-8")
        self.send_response(200 if healthy else 503)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        log.debug("health: " + fmt, *args)


class HealthServer:
    def __init__(self, state: HealthState, port: int = 8080, host: str = "0.0.0.0"):
        self.state = state
        self.port = port
        self.host = host
        self._server: ThreadingHTTPServer | None = None

    def start(self) -> None:
        handler = type("Handler", (_Handler,), {"state": self.state})
        try:
            self._server = ThreadingHTTPServer((self.host, self.port), handler)
        except OSError as exc:
            log.error("سرور سلامت روی پورت %s بالا نیامد: %s", self.port, exc)
            return
        self._server.daemon_threads = True
        self.port = self._server.server_address[1]
        threading.Thread(
            target=self._server.serve_forever, name="health-server", daemon=True
        ).start()
        log.info("سرور سلامت روی %s:%s فعال شد", self.host, self.port)

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()
