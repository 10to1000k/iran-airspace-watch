"""ساخت لیست زنده‌ی ایرلاین‌ها از داده‌ی واقعی پرواز (OpenSky).

چرا: لیست دستیِ ایرلاین‌ها در این حوزه سریع کهنه می‌شود.
اینجا از پروازهای واقعیِ ۳۰ روز گذشته‌ی OIIE و OIII استفاده می‌کنیم.
"""

from __future__ import annotations

import json
import logging
import time
from collections import Counter
from datetime import datetime, timedelta, timezone

import requests

from . import util

log = logging.getLogger(__name__)

AIRPORTS = ("OIIE", "OIII")
BASE = "https://opensky-network.org/api/flights"


def build_watchlist(config, days: int = 30) -> dict:
    icao_map = {}
    for entry in config.airlines:
        if entry.get("icao"):
            icao_map[entry["icao"].upper()] = entry["name"]

    auth = None
    user = config.env_get("OPENSKY_USER")
    password = config.env_get("OPENSKY_PASS")
    if user and password:
        auth = (user, password)

    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    observed: Counter[str] = Counter()
    errors = 0

    window_start = start
    while window_start < end:
        window_end = min(window_start + timedelta(days=2), end)
        for airport in AIRPORTS:
            for kind in ("arrival", "departure"):
                params = {
                    "airport": airport,
                    "begin": int(window_start.timestamp()),
                    "end": int(window_end.timestamp()),
                }
                try:
                    response = requests.get(
                        f"{BASE}/{kind}", params=params, auth=auth, timeout=60
                    )
                    if response.status_code == 404:
                        continue
                    response.raise_for_status()
                    flights = response.json() or []
                except Exception as exc:  # noqa: BLE001
                    errors += 1
                    log.warning("OpenSky %s/%s ناموفق: %s", airport, kind, exc)
                    time.sleep(1)
                    continue
                for flight in flights:
                    callsign = (flight.get("callsign") or "").strip().upper()
                    if len(callsign) >= 3:
                        observed[callsign[:3]] += 1
                time.sleep(0.6)
        window_start = window_end

    matched, unknown = [], []
    for prefix, count in observed.most_common():
        if prefix in icao_map:
            matched.append({"icao": prefix, "name": icao_map[prefix], "flights": count})
        else:
            unknown.append({"icao": prefix, "flights": count})

    report = {
        "generated_at": util.iso(util.now_utc()),
        "days": days,
        "airports": list(AIRPORTS),
        "errors": errors,
        "observed_airlines": matched,
        "unknown_prefixes": unknown[:40],
        "note": "airlines.yaml را با این گزارش تطبیق دهید؛ status و icao را به‌روز کنید.",
    }
    out = config.db_path.parent / "watchlist_report.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def print_report(report: dict) -> None:
    print(f"\nگزارش لیست زنده — {report['generated_at']} ({report['days']} روز)")
    print("=" * 78)
    if report["observed_airlines"]:
        print("\n✅ ایرلاین‌های شناسایی‌شده با پرواز واقعی:")
        for row in report["observed_airlines"]:
            print(f"   {row['icao']:<4} {row['name']:<40} {row['flights']:>4} پرواز")
    if report["unknown_prefixes"]:
        print("\n❓ کدهایی که در airlines.yaml نیستند (کاندید افزودن):")
        for row in report["unknown_prefixes"][:20]:
            print(f"   {row['icao']:<4} {row['flights']:>4} پرواز")
    if report["errors"]:
        print(f"\n⚠️ {report['errors']} درخواست ناموفق (احتمالاً محدودیت نرخ OpenSky)")
    print("\nگزارش کامل: data/watchlist_report.json")
