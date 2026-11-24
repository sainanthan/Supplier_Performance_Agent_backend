"""Keep the demo dataset current: fill every missing day up to today (see app/rollforward.py for how).

    python -m scripts.roll_forward --dry-run     # show which days would be filled
    python -m scripts.roll_forward               # fill them (also runs hourly as the 'roll_forward' job when the
                                                 #   rolling_demo_data feature flag is on)
    python -m scripts.roll_forward --status      # how far the data has been rolled
    python -m scripts.roll_forward --clear       # remove every ROLLING row again; the real extracts are untouched
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import rollforward as RF  # noqa: E402
from app.db import read_engine  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--clear", action="store_true")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--until", help="fill up to this date (YYYY-MM-DD) instead of today")
    a = ap.parse_args()
    if a.status:
        with read_engine.connect() as c:
            print(json.dumps(RF.status(c), indent=2, default=str))
        return
    if a.clear:
        print(json.dumps(RF.clear(), indent=2))
        return
    print(json.dumps(RF.run(dt.date.fromisoformat(a.until) if a.until else None, dry_run=a.dry_run), indent=2, default=str))


if __name__ == "__main__":
    main()
