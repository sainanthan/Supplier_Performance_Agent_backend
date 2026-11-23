"""Schema migrations: status / apply / roll back (enhancement programme).

    python -m scripts.migrate                       # show applied migrations and feature flags
    python -m scripts.migrate --apply               # apply pending migrations (also done automatically on backend start)
    python -m scripts.migrate --down 2026_11_12_002_phase1   # roll back one migration (drops ONLY what it added)

Rollback order: switch the feature flags off first (Settings → Feature flags), then roll back newest-first.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sqlalchemy import text  # noqa: E402

from app.db import engine  # noqa: E402
from app.migrations import MIGRATIONS, rollback, run_migrations  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--down")
    a = ap.parse_args()
    if a.down:
        rollback(engine, a.down)
        print(f"rolled back {a.down}")
    if a.apply:
        print("applied:", run_migrations(engine) or "nothing pending")
    with engine.connect() as c:
        done = {r[0]: r[1] for r in c.execute(text("SELECT id, applied_at FROM public.schema_migrations"))}
        for mid, _u, _d in MIGRATIONS:
            print(f"  {'APPLIED' if mid in done else 'pending':8} {mid}  {done.get(mid, '')}")
        for k, e in c.execute(text("SELECT key, enabled FROM public.feature_flags ORDER BY phase, key")):
            print(f"  flag {k:<22} {'ON' if e else 'off'}")


if __name__ == "__main__":
    main()
