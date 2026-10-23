"""زمان‌بند: هر کالکتور در نخ خودش، با بازه‌ی مشخص و نظارت واقعی سلامت."""

from __future__ import annotations

import logging
import random
import signal
import threading
import time

from .airlines import AirlineIndex
from .collectors import build_jobs
from .health import HealthServer, HealthState
from .pipeline import Pipeline

log = logging.getLogger(__name__)

PRUNE_INTERVAL_SECONDS = 24 * 3600


def run_once(config, db, bot) -> dict[str, int]:
    index = AirlineIndex(config.airlines)
    pipeline = Pipeline(config, db, bot)
    totals: dict[str, int] = {}
    for job in build_jobs(config, db, index):
        try:
            items = job.fn()
        except Exception as exc:  # noqa: BLE001
            log.error("کالکتور %s خطا داد: %s", job.name, exc)
            continue
        if items:
            log.info("کالکتور %s: %d آیتم", job.name, len(items))
        for key, value in pipeline.process_many(items).items():
            totals[key] = totals.get(key, 0) + value
    pipeline.flush_unsent()
    return totals


def _install_signal_handlers(stop: threading.Event) -> None:
    if threading.current_thread() is not threading.main_thread():
        return

    def _on_signal(signum, _frame) -> None:
        log.info("سیگنال توقف دریافت شد (%s) — خروج ایمن آغاز شد", signum)
        stop.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _on_signal)
        except (ValueError, OSError):
            pass


def run_forever(config, db, bot) -> None:
    index = AirlineIndex(config.airlines)
    stale_seconds = int(config.get("health.stale_seconds", 300))
    retention_days = int(config.get("app.retention_days", 30))

    health = HealthState(stale_seconds=stale_seconds)
    pipeline = Pipeline(config, db, bot, health=health)
    bot.bind_health(health)
    bot.on_recipient_ready(pipeline.flush_unsent)
    bot.start_listener()

    if bot.listener_enabled:
        health.add_check("telegram_listener", lambda: bot.listener_alive)
        health.expect_poll("telegram_bot", max(120, stale_seconds))

    server: HealthServer | None = None
    if config.get("health.enabled", True):
        server = HealthServer(health, config.health_port)
        server.start()

    stop = threading.Event()
    _install_signal_handlers(stop)

    jobs = build_jobs(config, db, index)
    threads: list[threading.Thread] = []
    for job in jobs:
        thread = threading.Thread(
            target=_loop,
            args=(job, pipeline, stop, health),
            name=f"collector-{job.name}",
            daemon=True,
        )
        health.expect_poll(job.name, max(job.interval * 4, stale_seconds))
        health.add_check(f"collector:{job.name}", thread.is_alive)
        thread.start()
        threads.append(thread)
        log.info("کالکتور %s هر %d ثانیه اجرا می‌شود", job.name, job.interval)

    last_prune = 0.0
    log.info("پایش آغاز شد. برای توقف Ctrl+C")
    try:
        while not stop.is_set():
            health.touch()
            now_mono = time.monotonic()
            if now_mono - last_prune >= PRUNE_INTERVAL_SECONDS:
                try:
                    pruned = db.prune(keep_days=retention_days)
                    if pruned:
                        log.info("پاک‌سازی دیتابیس: %d آیتم قدیمی حذف شد", pruned)
                except Exception as exc:  # noqa: BLE001
                    log.warning("پاک‌سازی دیتابیس ناموفق بود: %s", exc)
                last_prune = now_mono

            stop.wait(1)
            if threads and not any(thread.is_alive() for thread in threads):
                log.error("همه‌ی کالکتورها متوقف شدند")
                break
    except KeyboardInterrupt:
        log.info("توقف درخواست شد")
    finally:
        stop.set()
        bot.stop()
        if server:
            server.stop()
        for thread in threads:
            thread.join(timeout=3)
        try:
            db.close()
        except Exception:  # noqa: BLE001
            pass


def _loop(job, pipeline: Pipeline, stop: threading.Event, health: HealthState) -> None:
    next_run = time.monotonic() + random.uniform(0, 2)
    while not stop.is_set():
        if time.monotonic() >= next_run:
            try:
                items = job.fn()
                if items:
                    summary = pipeline.process_many(items)
                    log.info("کالکتور %s → %s", job.name, summary)
                pipeline.flush_unsent()
            except Exception:  # noqa: BLE001
                log.exception("خطا در کالکتور %s", job.name)
            health.mark_poll(job.name)
            next_run = time.monotonic() + job.interval
        stop.wait(1)
