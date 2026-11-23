"""Stamp lineage on rows that have none (e.g. loaded from CSV before Data Integration existed).

    python -m scripts.stamp_lineage
Sets _source_system = 'CSV:<table>.csv', _source_record_id = business key (from the canonical model), _loaded_at = now().
Idempotent: only rows with _source_system IS NULL are touched.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sqlalchemy import text  # noqa: E402

from app.db import engine, init_db  # noqa: E402
from app.integration.canonical import CANONICAL  # noqa: E402


def main() -> None:
    init_db()
    with engine.begin() as c:
        for ent in CANONICAL.values():
            t, key = ent["table"], ent["key"]
            expr = " || '|' || ".join(f"COALESCE(CAST({k} AS text), '')" for k in key)
            n = c.execute(text(f"""UPDATE sp.{t} SET _source_system = 'CSV:{t}.csv', _source_record_id = {expr}, _loaded_at = now()
                                   WHERE _source_system IS NULL""")).rowcount
            print(f"  {t:<32} stamped {n}")


if __name__ == "__main__":
    main()
