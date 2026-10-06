#!/usr/bin/env python3
"""Check packaged IANA data in a fresh build Python process, without host data."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import sys


def verify_timezone_data() -> dict:
    import tzdata
    import zoneinfo

    # This script runs separately from the app. Force the same package fallback
    # Windows needs, even when the build host has a working system tz database.
    zoneinfo.reset_tzpath(())
    zoneinfo.ZoneInfo.clear_cache()
    zone = zoneinfo.ZoneInfo("America/New_York")
    durations = {}
    for label, month, day, hours, before, after in (
        ("spring", 3, 8, 23, -5, -4),
        ("fall", 11, 1, 25, -4, -5),
    ):
        start = datetime(2026, month, day, tzinfo=zone)
        end = start + timedelta(days=1)
        actual = (end.astimezone(timezone.utc) - start.astimezone(timezone.utc)).total_seconds() / 3600
        if (actual != hours or start.utcoffset() != timedelta(hours=before)
                or end.utcoffset() != timedelta(hours=after)):
            raise RuntimeError(f"Invalid {label} DST data for America/New_York: {actual} hours")
        durations[label] = hours
    return {"verified": True, "source": "tzdata", "tzdata_version": tzdata.__version__,
            "zone": zone.key, "dst_day_hours": durations}


def main() -> int:
    try:
        result = verify_timezone_data()
    except Exception as error:
        print(f"TIMEZONE_DATA_CHECK=FAIL: {type(error).__name__}: {error}", file=sys.stderr)
        print(f'Install the declared dependencies with the build interpreter: "{sys.executable}" '
              '-I -m pip install -r backend/requirements.txt', file=sys.stderr)
        return 1
    print("TIMEZONE_DATA_CHECK=PASS " + json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
