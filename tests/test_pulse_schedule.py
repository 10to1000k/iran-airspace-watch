"""Pulse-mode scheduling: due-only collection and one-shot Telegram commands."""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import util
from src.config import load_config


class PulseDueTests(unittest.TestCase):
    def _db(self, tmp):
        from src.db import Database

        return Database(Path(tmp) / "test.db")

    def test_job_becomes_due_only_after_its_interval(self):
        from src.scheduler import job_due, mark_job_run

        base = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
        job = SimpleNamespace(name="social_hashtags", interval=3600)
        with tempfile.TemporaryDirectory() as tmp:
            db = self._db(tmp)
            try:
                self.assertTrue(job_due(db, job, now=base))
                mark_job_run(db, job, now=base)
                self.assertFalse(job_due(db, job, now=base + timedelta(minutes=59)))
                self.assertTrue(job_due(db, job, now=base + timedelta(seconds=3600)))
            finally:
                db.close()

    def test_due_only_run_skips_a_job_that_is_not_due(self):
        from src import scheduler

        config = load_config(dry_run=True)
        due_job = SimpleNamespace(
            name="rss", interval=75, fn=mock.Mock(return_value=[])
        )
        sleeping_job = SimpleNamespace(
            name="social_hashtags", interval=3600, fn=mock.Mock(return_value=[])
        )
        with tempfile.TemporaryDirectory() as tmp:
            db = self._db(tmp)
            try:
                scheduler.mark_job_run(
                    db,
                    sleeping_job,
                    now=util.now_utc(),
                )
                with (
                    mock.patch.object(
                        scheduler, "build_jobs", return_value=[due_job, sleeping_job]
                    ),
                    mock.patch.object(scheduler, "Pipeline") as pipeline,
                ):
                    pipeline.return_value.process_many.return_value = {}
                    scheduler.run_once(config, db, bot=None, due_only=True)

                due_job.fn.assert_called_once_with()
                sleeping_job.fn.assert_not_called()
                self.assertIsNotNone(db.kv_get("pulse_last:rss"))
            finally:
                db.close()


class PulseCliTests(unittest.TestCase):
    def test_cli_accepts_due_with_once(self):
        from run import build_parser

        args = build_parser().parse_args(["--once", "--due"])
        self.assertTrue(args.once)
        self.assertTrue(args.due)


class OneShotTelegramTests(unittest.TestCase):
    class FakeDb:
        def __init__(self):
            self.values = {}

        def kv_get(self, key, default=None):
            return self.values.get(key, default)

        def kv_set(self, key, value):
            self.values[key] = value

    def test_poll_updates_once_handles_updates_and_advances_offset(self):
        from src.telegram_bot import TelegramBot

        db = self.FakeDb()
        bot = TelegramBot(load_config(dry_run=True), db)
        bot.token = "test-token"
        bot.dry_run = False
        updates = [{"update_id": 41, "message": {"chat": {"id": 7}, "text": "/start"}}]
        with (
            mock.patch.object(bot, "_call", return_value=updates) as call,
            mock.patch.object(bot, "_handle_update") as handle,
        ):
            handled = bot.poll_updates_once()

        self.assertEqual(handled, 1)
        handle.assert_called_once_with(updates[0])
        self.assertEqual(db.values["tg_offset"], "42")
        self.assertEqual(call.call_args.args[0], "getUpdates")
        self.assertEqual(call.call_args.args[1]["offset"], 0)
        self.assertEqual(call.call_args.args[1]["timeout"], 0)

    def test_run_once_polls_telegram_commands_once(self):
        from src import scheduler

        config = load_config(dry_run=True)
        bot = mock.Mock()
        bot.poll_updates_once.return_value = 0
        with (
            mock.patch.object(scheduler, "Pipeline") as pipeline,
            mock.patch.object(scheduler, "build_jobs", return_value=[]),
        ):
            scheduler.run_once(config, db=mock.Mock(), bot=bot)

        bot.poll_updates_once.assert_called_once_with()
        pipeline.return_value.flush_unsent.assert_called_once_with()


if __name__ == "__main__":
    unittest.main(verbosity=2)
