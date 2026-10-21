#!/usr/bin/env python
"""نقطه‌ی ورود.

نمونه‌ها:
    python run.py                      # اجرای دائمی
    python run.py --once --dry-run     # یک چرخه، بدون ارسال به تلگرام
    python run.py --check-sources      # تست فیدهای RSS
    python run.py --decode-notam "..." # دیکود یک NOTAM
    python run.py --test-message       # ارسال پیام نمونه
    python run.py --build-watchlist    # ساخت لیست زنده‌ی ایرلاین‌ها
"""

from __future__ import annotations

import argparse
import logging
import sys

from src import util
from src.config import load_config
from src.db import Database
from src.formatter import format_event
from src.notam import NotamCollector, decode_notam, parse_notam
from src.telegram_bot import TelegramBot

# کنسول ویندوز پیش‌فرض cp1252 است و فارسی را چاپ نمی‌کند
for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="replace")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="iran-airspace-watch",
        description="پایش اخبار حریم هوایی ایران و ارسال فوری به تلگرام",
    )
    parser.add_argument("--once", action="store_true", help="یک چرخه اجرا کن و خارج شو")
    parser.add_argument("--dry-run", action="store_true", help="بدون تماس با تلگرام، چاپ در کنسول")
    parser.add_argument("--check-sources", action="store_true", help="تست سلامت فیدهای RSS")
    parser.add_argument("--decode-notam", metavar="TEXT", help="دیکود یک NOTAM خام")
    parser.add_argument("--build-watchlist", action="store_true", help="ساخت گزارش لیست ایرلاین‌ها")
    parser.add_argument("--days", type=int, default=30, help="بازه‌ی گزارش OpenSky (پیش‌فرض ۳۰)")
    parser.add_argument("--test-message", action="store_true", help="ارسال پیام نمونه")
    parser.add_argument("--stats", action="store_true", help="آمار دیتابیس")
    parser.add_argument("--verbose", "-v", action="store_true", help="لاگ کامل")
    return parser


def setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)-18s %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def sample_event() -> dict:
    return {
        "id": 0,
        "severity": "critical",
        "category": "airport",
        "title_fa": "بسته شدن موقت باند ۲۹L در فرودگاه امام خمینی",
        "body_fa": (
            "بر اساس اطلاعیه‌ی هوانوردی، باند ۲۹L فرودگاه بین‌المللی امام خمینی (IKA/OIIE) "
            "از ساعت ۰۹:۱۰ تا ۱۲:۳۰ UTC به‌دلیل عملیات عمرانی بسته است. پروازهای ورودی "
            "به باند جایگزین هدایت می‌شوند."
        ),
        "confidence": "multi",
        "numbers_ok": 1,
        "primary_source": "NOTAM — فرودگاه امام خمینی (IKA/OIIE)",
        "primary_url": "https://www.notams.faa.gov/",
        "first_seen": util.iso(util.now_utc()),
        "airlines": "Emirates,Turkish Airlines",
        "countries": "IR,AE,TR",
    }


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(args.verbose)
    config = load_config(dry_run=args.dry_run)

    if args.decode_notam:
        notam = parse_notam(args.decode_notam)
        if not notam:
            print("NOTAM قابل تجزیه نبود. مطمئن شو شماره و ساختار استاندارد دارد.")
            return 2
        print(f"شماره: {notam.number}")
        print(f"نوع: {notam.kind}")
        print(f"FIR: {notam.fir}   Q-code: {notam.qcode}")
        print(f"محل: {notam.location}")
        print(f"شروع: {notam.start}")
        print(f"پایان: {notam.end}")
        print("-" * 70)
        print(decode_notam(notam, config.glossary))
        return 0

    db = Database(config.db_path)
    bot = TelegramBot(config, db)

    if args.stats:
        print(db.stats())
        return 0

    if args.check_sources:
        from src.collectors import check_sources

        failures = check_sources(config, db)
        print(f"\n{'همه‌ی فیدها سالم‌اند.' if not failures else f'{failures} فید مشکل دارد.'}")
        notam = NotamCollector(config)
        print(f"NOTAM: {'فعال' if notam.enabled else 'غیرفعال (کلید FAA تنظیم نشده)'}")
        return 0 if not failures else 1

    if args.build_watchlist:
        from src.watchlist_builder import build_watchlist, print_report

        print_report(build_watchlist(config, days=args.days))
        return 0

    if args.test_message:
        text = format_event(sample_event(), config)
        message_id = bot.send(text, keyboard=None)
        print("پیام آزمایشی ارسال شد." if message_id else "ارسال نشد (dry-run یا بدون گیرنده).")
        return 0

    if args.once:
        from src.scheduler import run_once

        totals = run_once(config, db, bot)
        print(f"نتیجه: {totals or 'هیچ آیتم تازه‌ای نبود'}")
        return 0

    from src.scheduler import run_forever

    run_forever(config, db, bot)
    return 0


if __name__ == "__main__":
    sys.exit(main())
