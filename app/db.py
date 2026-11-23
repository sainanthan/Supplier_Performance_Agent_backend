"""Database engines and the application's own tables (chat history, actions, alerts, config).

Business data (CSV datasets) lives in the `sp` schema and is loaded by scripts/load_data.py.
Application tables live in `public` and are created automatically on startup.
"""
import datetime as dt
import uuid

from sqlalchemy import (JSON, Date, DateTime, ForeignKey, Integer, String, Text,
                        UniqueConstraint, create_engine)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker

from .config import settings

# Neon closes idle connections, so pre-ping and recycle keep the pool healthy.
# Performance: Neon is ~130 ms away, so every round-trip counts.
# - no pool_pre_ping (it costs an extra round-trip per query); stale connections are retried in analytics.query
# - a larger pool so analytics queries can run in parallel
# - TCP keepalives + recycle below Neon's idle timeout
_KEEPALIVE = {"keepalives": 1, "keepalives_idle": 30, "keepalives_interval": 10, "keepalives_count": 3}
engine = create_engine(settings.db_url, pool_pre_ping=False, pool_recycle=240, pool_size=12, max_overflow=8,
                       connect_args=_KEEPALIVE)
# Reads: same pool in autocommit mode. psycopg otherwise sends a separate BEGIN before the first query and a ROLLBACK when the
# connection goes back to the pool: 3 round-trips (~700 ms to Neon) for a one-line SELECT instead of 1 (~230 ms).
# Use it only for SELECTs; anything that writes keeps using engine.begin() (one atomic transaction).
read_engine = engine.execution_options(isolation_level="AUTOCOMMIT")
# Agent SQL tool: stays transactional on purpose - its READ ONLY / SET LOCAL statement_timeout / SET LOCAL ROLE guards need a transaction.
ro_engine = create_engine(settings.ro_db_url, pool_pre_ping=True, pool_recycle=240, pool_size=3, connect_args=_KEEPALIVE)
SessionLocal = sessionmaker(engine, expire_on_commit=False)
ReadSession = sessionmaker(read_engine, expire_on_commit=False)   # GET-only ORM reads (no BEGIN/ROLLBACK round-trips)


def now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class Base(DeclarativeBase):
    pass


class Conversation(Base):
    __tablename__ = "conversations"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    title: Mapped[str] = mapped_column(String(200), default="New chat")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=now)
    messages = relationship("Message", back_populates="conversation", order_by="Message.id",
                            cascade="all, delete-orphan")


class Message(Base):
    __tablename__ = "messages"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.id", ondelete="CASCADE"))
    role: Mapped[str] = mapped_column(String(20))                     # user | assistant
    content: Mapped[str] = mapped_column(Text)
    steps: Mapped[list | None] = mapped_column(JSON, nullable=True)   # tool trace
    artifacts: Mapped[list | None] = mapped_column(JSON, nullable=True)  # charts + source links (feature chat_charts)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=now)
    conversation = relationship("Conversation", back_populates="messages")


class CorrectiveAction(Base):
    """Corrective-action tracker. The agent may only PROPOSE; a person must approve."""
    __tablename__ = "corrective_actions"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    supplier_id: Mapped[str] = mapped_column(String(20), index=True)
    ncr_id: Mapped[str | None] = mapped_column(String(20), nullable=True)
    category: Mapped[str] = mapped_column(String(20), default="QUALITY")    # DELIVERY|QUALITY|RESPONSIVENESS|COMMERCIAL|CAPACITY
    priority: Mapped[str] = mapped_column(String(10), default="MEDIUM")     # LOW|MEDIUM|HIGH|CRITICAL
    title: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    owner_role: Mapped[str] = mapped_column(String(20), default="PROCUREMENT")  # PROCUREMENT|QUALITY
    owner_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    due_date: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    # PROPOSED -> APPROVED -> SUPPLIER_RESPONDED -> VERIFIED -> CLOSED   (or REJECTED)
    status: Mapped[str] = mapped_column(String(24), default="PROPOSED")
    origin: Mapped[str] = mapped_column(String(10), default="USER")         # AGENT|USER
    supplier_response: Mapped[str | None] = mapped_column(Text, nullable=True)
    responded_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    history: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class Notification(Base):
    """In-app notifications to procurement / quality owners."""
    __tablename__ = "notifications"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    recipient_role: Mapped[str] = mapped_column(String(20))   # PROCUREMENT|QUALITY
    supplier_id: Mapped[str | None] = mapped_column(String(20), nullable=True)
    severity: Mapped[str] = mapped_column(String(10), default="INFO")
    subject: Mapped[str] = mapped_column(String(200))
    body: Mapped[str] = mapped_column(Text, default="")
    action_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_read: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=now)


class KpiExclusion(Base):
    """Documented exclusions: PO lines removed from KPIs (e.g. disputed or force-majeure)."""
    __tablename__ = "kpi_exclusions"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    entity_type: Mapped[str] = mapped_column(String(20), default="PO_LINE")   # PO_LINE | INSPECTION | NCR
    entity_id: Mapped[str | None] = mapped_column(String(40), nullable=True)  # inspection_id / ncr_id
    po_id: Mapped[str | None] = mapped_column(String(20), nullable=True)
    po_line: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reason: Mapped[str] = mapped_column(Text)
    approved_by: Mapped[str] = mapped_column(String(100))                     # authenticated user who approved
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=now)
    revoked_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    revoke_reason: Mapped[str | None] = mapped_column(Text, nullable=True)


class AiUsage(Base):
    """Domo token usage per call (guide §11 'Logging': modelProviderUsage for cost tracking)."""
    __tablename__ = "ai_usage"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    model_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    purpose: Mapped[str] = mapped_column(String(30), default="agent")
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    total_tokens: Mapped[int] = mapped_column(Integer, default=0)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    ok: Mapped[bool] = mapped_column(default=True)
    prompt_version: Mapped[str | None] = mapped_column(String(40), nullable=True)
    input_hash: Mapped[str | None] = mapped_column(String(16), nullable=True)
    username: Mapped[str | None] = mapped_column(String(60), nullable=True)
    input_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    output_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=now)


class TwinScenario(Base):
    """Saved digital-twin scenarios. Simulations are advisory: DRAFT -> SUBMITTED -> APPROVED | REJECTED."""
    __tablename__ = "twin_scenarios"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(160))
    params: Mapped[dict] = mapped_column(JSON)
    summary: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(20), default="DRAFT")
    created_by: Mapped[str] = mapped_column(String(100), default="user")
    decided_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class User(Base):
    """Application users. Roles: ADMIN | PROCUREMENT | QUALITY | VIEWER | SUPPLIER (scoped to supplier_id)."""
    __tablename__ = "app_users"
    username: Mapped[str] = mapped_column(String(60), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(120))
    role: Mapped[str] = mapped_column(String(20))
    supplier_id: Mapped[str | None] = mapped_column(String(20), nullable=True)
    password_hash: Mapped[str] = mapped_column(String(200))
    active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=now)


class ConfigVersion(Base):
    """Every approved change of KPI definitions (F1). The active config is the latest version."""
    __tablename__ = "config_versions"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    key: Mapped[str] = mapped_column(String(50), default="scorecard")
    value: Mapped[dict] = mapped_column(JSON)
    config_hash: Mapped[str] = mapped_column(String(16))
    approved_by: Mapped[str] = mapped_column(String(100))
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=now)


class CalcRun(Base):
    """Calculation run log (F5): what was calculated, with which config, data and code version."""
    __tablename__ = "calc_runs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    kind: Mapped[str] = mapped_column(String(30))
    trigger: Mapped[str] = mapped_column(String(20), default="on-demand")
    params: Mapped[dict] = mapped_column(JSON, default=dict)
    config_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    config_hash: Mapped[str | None] = mapped_column(String(16), nullable=True)
    code_version: Mapped[str] = mapped_column(String(40))
    as_of_date: Mapped[str | None] = mapped_column(String(10), nullable=True)
    data_coverage: Mapped[dict] = mapped_column(JSON, default=dict)
    row_count: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(12), default="OK")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=now)


class ScorecardSnapshot(Base):
    """Frozen scorecard results per run, so any past score can be reproduced and evidenced (F4/F5)."""
    __tablename__ = "scorecard_snapshots"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(36), index=True)
    supplier_id: Mapped[str] = mapped_column(String(20), index=True)
    payload: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=now)


class PromiseChange(Base):
    """Promise-date changes (A6). Supplier proposes (portal) or ERP loads; procurement approves."""
    __tablename__ = "promise_changes"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    po_id: Mapped[str] = mapped_column(String(20), index=True)
    po_line: Mapped[int] = mapped_column(Integer)
    supplier_id: Mapped[str] = mapped_column(String(20))
    old_promise: Mapped[dt.date] = mapped_column(Date)
    new_promise: Mapped[dt.date] = mapped_column(Date)
    reason: Mapped[str] = mapped_column(Text, default="")
    requested_by: Mapped[str] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(12), default="PROPOSED")
    decided_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    approved_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=now)


class NcrResponse(Base):
    """Supplier responses to NCRs (A4/E4 response time). Written from the supplier portal."""
    __tablename__ = "ncr_responses"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    ncr_id: Mapped[str] = mapped_column(String(20), index=True)
    supplier_id: Mapped[str] = mapped_column(String(20))
    response: Mapped[str] = mapped_column(Text)
    containment: Mapped[str | None] = mapped_column(Text, nullable=True)
    responded_by: Mapped[str] = mapped_column(String(100))
    responded_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=now)


class SupplierAlias(Base):
    """Master-data mapping (G2): alternate names / source-system codes -> one supplier_id."""
    __tablename__ = "supplier_aliases"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    alias: Mapped[str] = mapped_column(String(200), unique=True)
    source_system: Mapped[str] = mapped_column(String(30), default="MANUAL")
    supplier_id: Mapped[str] = mapped_column(String(20))
    created_by: Mapped[str] = mapped_column(String(100), default="system")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=now)


class DqResult(Base):
    """Data-quality check results per load (G3)."""
    __tablename__ = "dq_results"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    load_id: Mapped[str] = mapped_column(String(36), index=True)
    table_name: Mapped[str] = mapped_column(String(60))
    rule: Mapped[str] = mapped_column(String(160))
    severity: Mapped[str] = mapped_column(String(10))
    failed_rows: Mapped[int] = mapped_column(Integer, default=0)
    total_rows: Mapped[int] = mapped_column(Integer, default=0)
    sample: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=now)


class JobRun(Base):
    """Scheduled job executions (G4)."""
    __tablename__ = "job_runs"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    job: Mapped[str] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(10))
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=now)
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AlertNotice(Base):
    """De-duplication of automatic alert notifications (B5)."""
    __tablename__ = "alert_notices"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    alert_key: Mapped[str] = mapped_column(String(200), index=True)
    notified_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=now)


class ReconResult(Base):
    """Reconciliation of app KPIs against independently recomputed source figures (G5)."""
    __tablename__ = "recon_results"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    recon_id: Mapped[str] = mapped_column(String(36), index=True)
    check_name: Mapped[str] = mapped_column(String(120))
    supplier_id: Mapped[str | None] = mapped_column(String(20), nullable=True)
    source_value: Mapped[float | None] = mapped_column(nullable=True)
    app_value: Mapped[float | None] = mapped_column(nullable=True)
    diff: Mapped[float | None] = mapped_column(nullable=True)
    passed: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=now)


class AppConfig(Base):
    """Key/value config, e.g. the agreed scorecard weights and thresholds."""
    __tablename__ = "app_config"
    key: Mapped[str] = mapped_column(String(50), primary_key=True)
    value: Mapped[dict] = mapped_column(JSON)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


DEFAULT_SCORECARD_CONFIG = {
    "weights": {"delivery": 0.35, "quality": 0.35, "responsiveness": 0.15, "commercial": 0.15},
    "in_full_tolerance_pct": 5.0,        # received >= ordered * (1 - tol) counts as in-full
    "lead_time_tolerance_days": 2,       # actual lead time <= standard + tol counts as adherent
    "ppm_target": 25.0,                  # PPM at or below -> quality score 100
    "ppm_ceiling": 150.0,               # PPM at or above -> quality score 0
    "grade_bands": {"A": 85, "B": 70, "C": 55},
    "trend_window_days": 30,
    "otif_drop_alert_pts": 5.0,
    "ppm_rise_alert_pct": 25.0,
    "concentration_share_pct": 25.0,
    "repeat_defect_min_weight": 2.5,
    # --- delivery edge cases (G1)
    "early_window_days": None,           # receipt earlier than promise - N days = not on time (None = no early penalty)
    "over_delivery_tolerance_pct": 10.0, # received > ordered * (1 + tol) = not in full
    "non_working_weekdays": [0],         # outside the shift calendar: 0 = Sunday
    # --- risk score, health bands, recommendations (formerly hard-coded)
    "risk_score": {"composite_factor": 0.6, "tier_points": {"HIGH": 20, "MEDIUM": 10, "LOW": 0},
                   "capa_points_each": 2, "capa_points_cap": 10, "disruption_points_cap": 10},
    "health_bands": {"watch": 45, "risk": 55},
    "recommendation_thresholds": {"otif_floor_pct": 80, "lead_time_var_days": 2, "ppm": 50, "price_var_pct": 2, "disruption_hours": 2},
    # --- trend detection robustness (A8)
    "trend_min_lines": 5, "trend_min_received_units": 20000, "trend_alpha": 0.10,
    # --- automation (B5 / G4)
    "auto_notify_severities": ["CRITICAL", "HIGH"], "notify_dedupe_days": 7, "schedule_minutes": 60,
    # --- network twin (T3): a shipment is AT RISK when its ETA is within this many days of the promise date (or later)
    "lane_at_risk_buffer_days": 2,
    # --- plant twin (T4): days of stock cover below which a material is critical / on watch (or below safety stock)
    "plant_cover_critical_days": 2, "plant_cover_watch_days": 5,
    # --- GPS tracking (T3): driver / vehicle positions are deleted after this many days (data minimisation)
    "gps_retention_days": 90,
}

APP_TABLES = {"conversations", "messages", "corrective_actions", "notifications",
              "kpi_exclusions", "app_config", "ai_usage", "twin_scenarios", "app_users", "config_versions", "calc_runs",
              "scorecard_snapshots", "promise_changes", "ncr_responses", "supplier_aliases", "dq_results", "job_runs",
              "alert_notices", "recon_results"}


def config_hash(cfg: dict) -> str:
    import hashlib
    import json
    return hashlib.sha256(json.dumps(cfg, sort_keys=True, default=str).encode()).hexdigest()[:16]


def init_db() -> None:
    from sqlalchemy import text as _t
    Base.metadata.create_all(engine)
    with engine.begin() as c:                     # upgrade tables created by earlier versions
        for stmt in ["ALTER TABLE kpi_exclusions ADD COLUMN IF NOT EXISTS entity_type VARCHAR(20) DEFAULT 'PO_LINE'",
                     "ALTER TABLE kpi_exclusions ADD COLUMN IF NOT EXISTS entity_id VARCHAR(40)",
                     "ALTER TABLE kpi_exclusions ADD COLUMN IF NOT EXISTS revoked_at TIMESTAMPTZ",
                     "ALTER TABLE kpi_exclusions ADD COLUMN IF NOT EXISTS revoked_by VARCHAR(100)",
                     "ALTER TABLE kpi_exclusions ADD COLUMN IF NOT EXISTS revoke_reason TEXT",
                     "ALTER TABLE kpi_exclusions ALTER COLUMN po_id DROP NOT NULL",
                     "ALTER TABLE kpi_exclusions ALTER COLUMN po_line DROP NOT NULL",
                     "ALTER TABLE kpi_exclusions DROP CONSTRAINT IF EXISTS kpi_exclusions_po_id_po_line_key",
                     "UPDATE kpi_exclusions SET entity_type = 'PO_LINE' WHERE entity_type IS NULL"]:
            c.execute(_t(stmt))
    with SessionLocal() as s:
        row = s.get(AppConfig, "scorecard")
        if not row:
            s.add(AppConfig(key="scorecard", value=DEFAULT_SCORECARD_CONFIG))
        else:                                     # add newly introduced definition keys without touching approved values
            merged = {**DEFAULT_SCORECARD_CONFIG, **(row.value or {})}
            if merged != row.value:
                row.value = merged
        s.commit()
        if not s.query(ConfigVersion).first():
            cfg = s.get(AppConfig, "scorecard").value
            s.add(ConfigVersion(value=cfg, config_hash=config_hash(cfg), approved_by="system (initial definitions)",
                                note="Initial approved KPI definitions"))
            s.commit()
    from .migrations import run_migrations
    run_migrations(engine)


def current_config_version() -> tuple:
    with SessionLocal() as s:
        v = s.query(ConfigVersion).order_by(ConfigVersion.id.desc()).first()
        return (v.id, v.config_hash) if v else (None, None)


_CFG_CACHE: dict = {"t": 0.0, "v": None}
CFG_TTL_S = 30


def get_scorecard_config() -> dict:
    """KPI configuration. Read on almost every request (~0.75 s to Neon each time), so it is cached for CFG_TTL_S;
    saving a new configuration clears it at once (analytics.clear_cache -> invalidate_scorecard_config)."""
    import copy
    import time
    hit = _CFG_CACHE["v"]
    if hit is not None and time.time() - _CFG_CACHE["t"] < CFG_TTL_S:
        return copy.deepcopy(hit)
    with ReadSession() as s:
        row = s.get(AppConfig, "scorecard")
        cfg = dict(DEFAULT_SCORECARD_CONFIG)
        if row:
            cfg.update(row.value or {})
    _CFG_CACHE.update(t=time.time(), v=copy.deepcopy(cfg))
    return cfg


def invalidate_scorecard_config() -> None:
    _CFG_CACHE.update(t=0.0, v=None)
