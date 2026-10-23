"""سرور سلامت واقعی برای probeهای OpenShift/Kubernetes.

بدون وابستگی خارجی، روی http.server استاندارد.
علاوه بر ضربان حلقه‌ی اصلی، زنده‌بودن نخ‌های حیاتی (کالکتور RSS و شنونده‌ی تلگرام)
و عقب نیفتادن واقعی پایش‌ها را بررسی می‌کند تا در صورت قفل‌شدن هر بخش،
OpenShift خودکار کانتینر را ری‌استارت کند.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

log = logging.getLogger(__name__)


class HealthState:
    def __init__(self, stale_seconds: int = 300):
        self.stale_seconds = stale_seconds
        self.started = time.time()
        self._last_touch = time.monotonic()
        self._polls: dict[str, float] = {}
        self._poll_mono: dict[str, float] = {}
        self._max_silence: dict[str, float] = {}
        self._checks: dict[str, Callable[[], bool]] = {}
        self._published = 0
        self._lock = threading.Lock()

    # ── ثبت سرویس‌های حیاتی ───────────────────────────────────
    def expect_poll(self, name: str, max_silence_seconds: float) -> None:
        with self._lock:
            self._max_silence[name] = float(max_silence_seconds)
            self._poll_mono.setdefault(name, time.monotonic())

    def add_check(self, name: str, check: Callable[[], bool]) -> None:
        with self._lock:
            self._checks[name] = check

    # ── به‌روزرسانی ──────────────────────────────────────────
    def touch(self) -> None:
        with self._lock:
            self._last_touch = time.monotonic()

    def mark_poll(self, name: str) -> None:
        now_mono = time.monotonic()
        now_wall = time.time()
        with self._lock:
            self._last_touch = now_mono
            self._poll_mono[name] = now_mono
            self._polls[name] = now_wall

    def mark_publish(self) -> None:
        with self._lock:
            self._published += 1
            self._last_touch = time.monotonic()

    # ── وضعیت ────────────────────────────────────────────────
    def age(self) -> float:
        with self._lock:
            return time.monotonic() - self._last_touch

    def _evaluate(self) -> tuple[bool, list[str], float, dict[str, float]]:
        now_mono = time.monotonic()
        with self._lock:
            age = now_mono - self._last_touch
            polls_copy = dict(self._polls)
            poll_mono = dict(self._poll_mono)
            max_silence = dict(self._max_silence)
            checks = dict(self._checks)

        reasons: list[str] = []
        if age >= self.stale_seconds:
            reasons.append(f"main-loop-stale:{round(age, 1)}s")

        for name, limit in max_silence.items():
            since = now_mono - poll_mono.get(name, now_mono)
            if since >= limit:
                reasons.append(f"poll-stale:{name}:{round(since, 1)}s")

        for name, check in checks.items():
            try:
                ok = bool(check())
            except Exception:  # noqa: BLE001
                ok = False
            if not ok:
                reasons.append(f"check-failed:{name}")

        return (len(reasons) == 0), reasons, age, polls_copy

    def healthy(self) -> bool:
        ok, _, _, _ = self._evaluate()
        return ok

    def snapshot(self) -> dict:
        ok, reasons, age, polls_copy = self._evaluate()
        with self._lock:
            published = self._published
        return {
            "status": "ok" if ok else "stale",
            "reasons": reasons,
            "uptime_seconds": round(time.time() - self.started),
            "seconds_since_heartbeat": round(age, 1),
            "published": published,
            "last_poll_utc": {
                name: time.strftime("%H:%M:%S", time.gmtime(value))
                for name, value in polls_copy.items()
            },
            "last_poll_epoch": polls_copy,
        }


class _Handler(BaseHTTPRequestHandler):
    state: HealthState  # در زیرکلاس مقدار می‌گیرد

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path not in ("/healthz", "/readyz"):
            self.send_response(404)
            self.end_headers()
            return
        snap = self.state.snapshot()
        healthy = snap["status"] == "ok"
        body = json.dumps(snap, ensure_ascii=False).encode("utf-8")
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
