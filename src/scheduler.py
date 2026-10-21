"""زمان‌بند: هر کالکتور در نخ خودش، با بازه‌ی مشخص."""

from __future__ import annotations

import logging
import random
import threading
import time

from .airlines import AirlineIndex
from .collectors import build_jobs
from .health import HealthServer, HealthState
from .pipeline import Pipeline

log = logging.getLogger(__name__)


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
    return totals


def run_forever(config, db, bot) -> None:
    index = AirlineIndex(config.airlines)
    health = HealthState(int(config.get("health.stale_seconds", 120)))
    pipeline = Pipeline(config, db, bot, health=health)
    bot.start_listener()

    if config.get("health.enabled", True):
        HealthServer(health, config.health_port).start()

    jobs = build_jobs(config, db, index)
    stop = threading.Event()
    threads = []
    for job in jobs:
        thread = threading.Thread(
            target=_loop,
            args=(job, pipeline, stop, health),
            name=f"collector-{job.name}",
            daemon=True,
        )
        thread.start()
        threads.append(thread)
        log.info("کالکتور %s هر %d ثانیه اجرا می‌شود", job.name, job.interval)

    log.info("پایش آغاز شد. برای توقف Ctrl+C")
    try:
        while not stop.is_set():
            health.touch()
            time.sleep(1)
            if not any(thread.is_alive() for thread in threads):
                log.error("همه‌ی کالکتورها متوقف شدند")
                break
    except KeyboardInterrupt:
        log.info("توقف درخواست شد")
    finally:
        stop.set()
        bot.stop()


def _loop(job, pipeline: Pipeline, stop: threading.Event, health: HealthState) -> None:
    next_run = time.monotonic() + random.uniform(0, 3)
    while not stop.is_set():
        if time.monotonic() >= next_run:
            try:
                items = job.fn()
                if items:
                    summary = pipeline.process_many(items)
                    log.info("کالکتور %s → %s", job.name, summary)
            except Exception:  # noqa: BLE001
                log.exception("خطا در کالکتور %s", job.name)
            health.mark_poll(job.name)
            next_run = time.monotonic() + job.interval
        stop.wait(1)
