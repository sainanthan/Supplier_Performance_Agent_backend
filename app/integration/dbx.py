"""Database engine for Data Integration writes (control tables, staging, quarantine, publish).

Unlike the analytics engine (tuned for read latency, no pre-ping), integration writes must survive Neon closing idle pooled
connections, so this pool pre-pings every checkout (one extra round-trip - negligible next to a load run) and keeps TCP
keepalives on. Statements that take locks on KPI tables run with a lock_timeout so they can never hang behind a stale session.
"""
from sqlalchemy import create_engine

from ..config import settings

engine = create_engine(settings.db_url, pool_pre_ping=True, pool_recycle=240, pool_size=5, max_overflow=5,
                       connect_args={"keepalives": 1, "keepalives_idle": 30, "keepalives_interval": 10, "keepalives_count": 3})
