"""Versioned, additive schema migrations (enhancement programme).

Rules: every migration is idempotent (IF NOT EXISTS), additive only (no DROP / no type change of existing columns),
and has a matching DOWN list used only by `python -m scripts.migrate --down <id>` for rollback.
Applied migrations are recorded in public.schema_migrations. init_db() calls run_migrations() on every start.
"""
from __future__ import annotations

from sqlalchemy import text

MIGRATIONS: list[tuple[str, list[str], list[str]]] = [
    # ------------------------------------------------------------------ foundation
    ("2026_11_12_001_foundation", [
        """CREATE TABLE IF NOT EXISTS public.feature_flags (
             key VARCHAR(60) PRIMARY KEY, enabled BOOLEAN NOT NULL DEFAULT FALSE, phase VARCHAR(10),
             description TEXT, updated_by VARCHAR(100), updated_at TIMESTAMPTZ DEFAULT now())""",
        # LLM call log: prompt version + input hash + full input/output for audit (F5/F6)
        "ALTER TABLE public.ai_usage ADD COLUMN IF NOT EXISTS prompt_version VARCHAR(40)",
        "ALTER TABLE public.ai_usage ADD COLUMN IF NOT EXISTS input_hash VARCHAR(16)",
        "ALTER TABLE public.ai_usage ADD COLUMN IF NOT EXISTS username VARCHAR(60)",
        "ALTER TABLE public.ai_usage ADD COLUMN IF NOT EXISTS input_text TEXT",
        "ALTER TABLE public.ai_usage ADD COLUMN IF NOT EXISTS output_text TEXT",
        # Prediction log (used from Phase 3; created now so every prediction is logged from day one)
        """CREATE TABLE IF NOT EXISTS public.predictions (
             id BIGSERIAL PRIMARY KEY, model VARCHAR(60) NOT NULL, model_version VARCHAR(20) NOT NULL,
             supplier_id VARCHAR(20), horizon_days INTEGER, target VARCHAR(40), probability DOUBLE PRECISION,
             confidence VARCHAR(20), features JSONB, inputs_hash VARCHAR(16), run_id VARCHAR(36),
             created_by VARCHAR(60), created_at TIMESTAMPTZ DEFAULT now())""",
    ], [
        "DROP TABLE IF EXISTS public.predictions", "DROP TABLE IF EXISTS public.feature_flags",
        "ALTER TABLE public.ai_usage DROP COLUMN IF EXISTS prompt_version", "ALTER TABLE public.ai_usage DROP COLUMN IF EXISTS input_hash",
        "ALTER TABLE public.ai_usage DROP COLUMN IF EXISTS username", "ALTER TABLE public.ai_usage DROP COLUMN IF EXISTS input_text",
        "ALTER TABLE public.ai_usage DROP COLUMN IF EXISTS output_text",
    ]),
    # ------------------------------------------------------------------ phase 1
    ("2026_11_12_002_phase1", [
        "ALTER TABLE public.messages ADD COLUMN IF NOT EXISTS artifacts JSONB",
        """CREATE TABLE IF NOT EXISTS public.alert_events (
             id BIGSERIAL PRIMARY KEY, alert_key VARCHAR(200) UNIQUE NOT NULL, type VARCHAR(40), severity VARCHAR(10),
             supplier_id VARCHAR(20), title TEXT, detail TEXT, recommended_action TEXT, owner_role VARCHAR(20),
             status VARCHAR(20) NOT NULL DEFAULT 'OPEN', assignee VARCHAR(60), active BOOLEAN NOT NULL DEFAULT TRUE,
             first_seen TIMESTAMPTZ DEFAULT now(), last_seen TIMESTAMPTZ DEFAULT now(), history JSONB DEFAULT '[]'::jsonb)""",
        "CREATE INDEX IF NOT EXISTS alert_events_status_idx ON public.alert_events (status, active)",
        """CREATE TABLE IF NOT EXISTS public.report_exports (
             id BIGSERIAL PRIMARY KEY, supplier_id VARCHAR(20), fmt VARCHAR(6), period_start DATE, period_end DATE,
             run_id VARCHAR(36), config_version INTEGER, bytes INTEGER, created_by VARCHAR(60), created_at TIMESTAMPTZ DEFAULT now())""",
    ], [
        "DROP TABLE IF EXISTS public.report_exports", "DROP TABLE IF EXISTS public.alert_events",
        "ALTER TABLE public.messages DROP COLUMN IF EXISTS artifacts",
    ]),
]

MIGRATIONS.append(
    # ------------------------------------------------------------------ network tracker T1
    ("2026_11_13_003_network_sites", [
        """CREATE TABLE IF NOT EXISTS public.network_sites (
             node_id VARCHAR(30) PRIMARY KEY, kind VARCHAR(10) NOT NULL, name VARCHAR(120) NOT NULL,
             lat DOUBLE PRECISION NOT NULL, lon DOUBLE PRECISION NOT NULL, country VARCHAR(40),
             origin_port VARCHAR(30), entry_port VARCHAR(30), assumed BOOLEAN NOT NULL DEFAULT TRUE,
             source TEXT, updated_by VARCHAR(60) DEFAULT 'seed', updated_at TIMESTAMPTZ DEFAULT now())""",
    ], ["DROP TABLE IF EXISTS public.network_sites"]),
)

MIGRATIONS.append(
    # ------------------------------------------------------------------ network twin T2
    ("2026_11_13_004_route_cache", [
        """CREATE TABLE IF NOT EXISTS public.route_cache (
             key VARCHAR(80) PRIMARY KEY, mode VARCHAR(6) NOT NULL, provider VARCHAR(40) NOT NULL, geometry JSONB NOT NULL,
             distance_km DOUBLE PRECISION, duration_h DOUBLE PRECISION, fetched_at TIMESTAMPTZ DEFAULT now())""",
        "ALTER TABLE IF EXISTS sp.fact_inbound_milestone ADD COLUMN IF NOT EXISTS source TEXT",
    ], ["DROP TABLE IF EXISTS public.route_cache"]),
)

MIGRATIONS.append(
    # ------------------------------------------------------------------ network twin T3 (+ GPS tracking)
    ("2026_11_13_005_twin_t3", [
        """CREATE TABLE IF NOT EXISTS public.vehicles (
             vehicle_id VARCHAR(20) PRIMARY KEY, kind VARCHAR(10) NOT NULL, registration VARCHAR(60) NOT NULL, carrier VARCHAR(120),
             device_type VARCHAR(12) NOT NULL, device_key_hash VARCHAR(64) NOT NULL, active BOOLEAN NOT NULL DEFAULT TRUE,
             last_seen TIMESTAMPTZ, created_by VARCHAR(60), created_at TIMESTAMPTZ DEFAULT now())""",
        """CREATE TABLE IF NOT EXISTS public.vehicle_trips (
             trip_id BIGSERIAL PRIMARY KEY, vehicle_id VARCHAR(20) NOT NULL REFERENCES public.vehicles(vehicle_id),
             shipment_ids JSONB NOT NULL, started_at TIMESTAMPTZ NOT NULL DEFAULT now(), ended_at TIMESTAMPTZ, started_by VARCHAR(60))""",
        """CREATE TABLE IF NOT EXISTS public.gps_pings (
             id BIGSERIAL PRIMARY KEY, vehicle_id VARCHAR(20) NOT NULL, trip_id BIGINT NOT NULL, ts TIMESTAMPTZ NOT NULL,
             lat DOUBLE PRECISION NOT NULL, lon DOUBLE PRECISION NOT NULL, speed_kmh DOUBLE PRECISION, heading DOUBLE PRECISION,
             accuracy_m DOUBLE PRECISION, source VARCHAR(12) NOT NULL, received_at TIMESTAMPTZ DEFAULT now())""",
        "CREATE INDEX IF NOT EXISTS gps_pings_trip_ts ON public.gps_pings (trip_id, ts)",
        """CREATE TABLE IF NOT EXISTS public.disruption_events (
             id BIGSERIAL PRIMARY KEY, event_key VARCHAR(200) UNIQUE NOT NULL, provider VARCHAR(20) NOT NULL, type VARCHAR(40),
             severity VARCHAR(10), title TEXT, lat DOUBLE PRECISION, lon DOUBLE PRECISION, radius_km DOUBLE PRECISION,
             starts VARCHAR(40), ends VARCHAR(40), url TEXT, active BOOLEAN NOT NULL DEFAULT TRUE, fetched_at TIMESTAMPTZ DEFAULT now(),
             created_by VARCHAR(60))""",
        """CREATE TABLE IF NOT EXISTS public.saved_views (
             id BIGSERIAL PRIMARY KEY, username VARCHAR(60) NOT NULL, page VARCHAR(30) NOT NULL, name VARCHAR(80) NOT NULL,
             payload JSONB NOT NULL, shared BOOLEAN NOT NULL DEFAULT FALSE, created_at TIMESTAMPTZ DEFAULT now())""",
    ], ["DROP TABLE IF EXISTS public.saved_views", "DROP TABLE IF EXISTS public.disruption_events", "DROP TABLE IF EXISTS public.gps_pings",
        "DROP TABLE IF EXISTS public.vehicle_trips", "DROP TABLE IF EXISTS public.vehicles"]),
)

MIGRATIONS.append(
    # ------------------------------------------------------------------ data integration DI1
    ("2026_11_13_006_integration", [
        "CREATE SCHEMA IF NOT EXISTS integration",
        """CREATE TABLE IF NOT EXISTS integration.secrets (
             ref VARCHAR(80) PRIMARY KEY, ciphertext TEXT NOT NULL, key_fingerprint VARCHAR(16) NOT NULL,
             created_by VARCHAR(60), created_at TIMESTAMPTZ DEFAULT now(), rotated_at TIMESTAMPTZ)""",
        """CREATE TABLE IF NOT EXISTS integration.connections (
             id VARCHAR(20) PRIMARY KEY, name VARCHAR(120) NOT NULL, connector_key VARCHAR(40) NOT NULL, environment VARCHAR(6) NOT NULL,
             config JSONB NOT NULL DEFAULT '{}'::jsonb, secret_ref VARCHAR(80), secret_fields JSONB NOT NULL DEFAULT '[]'::jsonb,
             status VARCHAR(20) NOT NULL DEFAULT 'NOT_CONNECTED', approval_status VARCHAR(10) NOT NULL DEFAULT 'APPROVED',
             requested_by VARCHAR(60), approved_by VARCHAR(60), approved_at TIMESTAMPTZ,
             last_test_at TIMESTAMPTZ, last_test_ok BOOLEAN, last_test_message TEXT, last_sync_at TIMESTAMPTZ,
             active BOOLEAN NOT NULL DEFAULT TRUE, created_by VARCHAR(60), created_at TIMESTAMPTZ DEFAULT now(), updated_at TIMESTAMPTZ DEFAULT now())""",
        """CREATE TABLE IF NOT EXISTS integration.audit_log (
             id BIGSERIAL PRIMARY KEY, ts TIMESTAMPTZ DEFAULT now(), actor VARCHAR(60) NOT NULL, action VARCHAR(40) NOT NULL,
             connection_id VARCHAR(20), detail JSONB)""",
        """CREATE TABLE IF NOT EXISTS integration.table_ownership (
             canonical_table VARCHAR(60) PRIMARY KEY, owner VARCHAR(40) NOT NULL, set_by VARCHAR(60), set_at TIMESTAMPTZ DEFAULT now())""",
        "ALTER TABLE IF EXISTS sp.dim_supplier ADD COLUMN IF NOT EXISTS _source_system TEXT",
        "ALTER TABLE IF EXISTS sp.dim_supplier ADD COLUMN IF NOT EXISTS _source_record_id TEXT",
        "ALTER TABLE IF EXISTS sp.dim_supplier ADD COLUMN IF NOT EXISTS _connection_id TEXT",
        "ALTER TABLE IF EXISTS sp.dim_supplier ADD COLUMN IF NOT EXISTS _run_id TEXT",
        "ALTER TABLE IF EXISTS sp.dim_supplier ADD COLUMN IF NOT EXISTS _loaded_at TIMESTAMPTZ",
        "ALTER TABLE IF EXISTS sp.dim_material ADD COLUMN IF NOT EXISTS _source_system TEXT",
        "ALTER TABLE IF EXISTS sp.dim_material ADD COLUMN IF NOT EXISTS _source_record_id TEXT",
        "ALTER TABLE IF EXISTS sp.dim_material ADD COLUMN IF NOT EXISTS _connection_id TEXT",
        "ALTER TABLE IF EXISTS sp.dim_material ADD COLUMN IF NOT EXISTS _run_id TEXT",
        "ALTER TABLE IF EXISTS sp.dim_material ADD COLUMN IF NOT EXISTS _loaded_at TIMESTAMPTZ",
        "ALTER TABLE IF EXISTS sp.dim_plant ADD COLUMN IF NOT EXISTS _source_system TEXT",
        "ALTER TABLE IF EXISTS sp.dim_plant ADD COLUMN IF NOT EXISTS _source_record_id TEXT",
        "ALTER TABLE IF EXISTS sp.dim_plant ADD COLUMN IF NOT EXISTS _connection_id TEXT",
        "ALTER TABLE IF EXISTS sp.dim_plant ADD COLUMN IF NOT EXISTS _run_id TEXT",
        "ALTER TABLE IF EXISTS sp.dim_plant ADD COLUMN IF NOT EXISTS _loaded_at TIMESTAMPTZ",
        "ALTER TABLE IF EXISTS sp.fact_purchase_order_receipt ADD COLUMN IF NOT EXISTS _source_system TEXT",
        "ALTER TABLE IF EXISTS sp.fact_purchase_order_receipt ADD COLUMN IF NOT EXISTS _source_record_id TEXT",
        "ALTER TABLE IF EXISTS sp.fact_purchase_order_receipt ADD COLUMN IF NOT EXISTS _connection_id TEXT",
        "ALTER TABLE IF EXISTS sp.fact_purchase_order_receipt ADD COLUMN IF NOT EXISTS _run_id TEXT",
        "ALTER TABLE IF EXISTS sp.fact_purchase_order_receipt ADD COLUMN IF NOT EXISTS _loaded_at TIMESTAMPTZ",
        "ALTER TABLE IF EXISTS sp.fact_schedule_line ADD COLUMN IF NOT EXISTS _source_system TEXT",
        "ALTER TABLE IF EXISTS sp.fact_schedule_line ADD COLUMN IF NOT EXISTS _source_record_id TEXT",
        "ALTER TABLE IF EXISTS sp.fact_schedule_line ADD COLUMN IF NOT EXISTS _connection_id TEXT",
        "ALTER TABLE IF EXISTS sp.fact_schedule_line ADD COLUMN IF NOT EXISTS _run_id TEXT",
        "ALTER TABLE IF EXISTS sp.fact_schedule_line ADD COLUMN IF NOT EXISTS _loaded_at TIMESTAMPTZ",
        "ALTER TABLE IF EXISTS sp.fact_grn ADD COLUMN IF NOT EXISTS _source_system TEXT",
        "ALTER TABLE IF EXISTS sp.fact_grn ADD COLUMN IF NOT EXISTS _source_record_id TEXT",
        "ALTER TABLE IF EXISTS sp.fact_grn ADD COLUMN IF NOT EXISTS _connection_id TEXT",
        "ALTER TABLE IF EXISTS sp.fact_grn ADD COLUMN IF NOT EXISTS _run_id TEXT",
        "ALTER TABLE IF EXISTS sp.fact_grn ADD COLUMN IF NOT EXISTS _loaded_at TIMESTAMPTZ",
        "ALTER TABLE IF EXISTS sp.fact_incoming_inspection ADD COLUMN IF NOT EXISTS _source_system TEXT",
        "ALTER TABLE IF EXISTS sp.fact_incoming_inspection ADD COLUMN IF NOT EXISTS _source_record_id TEXT",
        "ALTER TABLE IF EXISTS sp.fact_incoming_inspection ADD COLUMN IF NOT EXISTS _connection_id TEXT",
        "ALTER TABLE IF EXISTS sp.fact_incoming_inspection ADD COLUMN IF NOT EXISTS _run_id TEXT",
        "ALTER TABLE IF EXISTS sp.fact_incoming_inspection ADD COLUMN IF NOT EXISTS _loaded_at TIMESTAMPTZ",
        "ALTER TABLE IF EXISTS sp.fact_quality_inspection ADD COLUMN IF NOT EXISTS _source_system TEXT",
        "ALTER TABLE IF EXISTS sp.fact_quality_inspection ADD COLUMN IF NOT EXISTS _source_record_id TEXT",
        "ALTER TABLE IF EXISTS sp.fact_quality_inspection ADD COLUMN IF NOT EXISTS _connection_id TEXT",
        "ALTER TABLE IF EXISTS sp.fact_quality_inspection ADD COLUMN IF NOT EXISTS _run_id TEXT",
        "ALTER TABLE IF EXISTS sp.fact_quality_inspection ADD COLUMN IF NOT EXISTS _loaded_at TIMESTAMPTZ",
        "ALTER TABLE IF EXISTS sp.fact_invoice ADD COLUMN IF NOT EXISTS _source_system TEXT",
        "ALTER TABLE IF EXISTS sp.fact_invoice ADD COLUMN IF NOT EXISTS _source_record_id TEXT",
        "ALTER TABLE IF EXISTS sp.fact_invoice ADD COLUMN IF NOT EXISTS _connection_id TEXT",
        "ALTER TABLE IF EXISTS sp.fact_invoice ADD COLUMN IF NOT EXISTS _run_id TEXT",
        "ALTER TABLE IF EXISTS sp.fact_invoice ADD COLUMN IF NOT EXISTS _loaded_at TIMESTAMPTZ",
        "ALTER TABLE IF EXISTS sp.fact_ncr_capa ADD COLUMN IF NOT EXISTS _source_system TEXT",
        "ALTER TABLE IF EXISTS sp.fact_ncr_capa ADD COLUMN IF NOT EXISTS _source_record_id TEXT",
        "ALTER TABLE IF EXISTS sp.fact_ncr_capa ADD COLUMN IF NOT EXISTS _connection_id TEXT",
        "ALTER TABLE IF EXISTS sp.fact_ncr_capa ADD COLUMN IF NOT EXISTS _run_id TEXT",
        "ALTER TABLE IF EXISTS sp.fact_ncr_capa ADD COLUMN IF NOT EXISTS _loaded_at TIMESTAMPTZ",
        "ALTER TABLE IF EXISTS sp.fact_inbound_milestone ADD COLUMN IF NOT EXISTS _source_system TEXT",
        "ALTER TABLE IF EXISTS sp.fact_inbound_milestone ADD COLUMN IF NOT EXISTS _source_record_id TEXT",
        "ALTER TABLE IF EXISTS sp.fact_inbound_milestone ADD COLUMN IF NOT EXISTS _connection_id TEXT",
        "ALTER TABLE IF EXISTS sp.fact_inbound_milestone ADD COLUMN IF NOT EXISTS _run_id TEXT",
        "ALTER TABLE IF EXISTS sp.fact_inbound_milestone ADD COLUMN IF NOT EXISTS _loaded_at TIMESTAMPTZ",
        "ALTER TABLE IF EXISTS sp.fact_production_order ADD COLUMN IF NOT EXISTS _source_system TEXT",
        "ALTER TABLE IF EXISTS sp.fact_production_order ADD COLUMN IF NOT EXISTS _source_record_id TEXT",
        "ALTER TABLE IF EXISTS sp.fact_production_order ADD COLUMN IF NOT EXISTS _connection_id TEXT",
        "ALTER TABLE IF EXISTS sp.fact_production_order ADD COLUMN IF NOT EXISTS _run_id TEXT",
        "ALTER TABLE IF EXISTS sp.fact_production_order ADD COLUMN IF NOT EXISTS _loaded_at TIMESTAMPTZ",
        "ALTER TABLE IF EXISTS sp.dim_production_line ADD COLUMN IF NOT EXISTS _source_system TEXT",
        "ALTER TABLE IF EXISTS sp.dim_production_line ADD COLUMN IF NOT EXISTS _source_record_id TEXT",
        "ALTER TABLE IF EXISTS sp.dim_production_line ADD COLUMN IF NOT EXISTS _connection_id TEXT",
        "ALTER TABLE IF EXISTS sp.dim_production_line ADD COLUMN IF NOT EXISTS _run_id TEXT",
        "ALTER TABLE IF EXISTS sp.dim_production_line ADD COLUMN IF NOT EXISTS _loaded_at TIMESTAMPTZ",
        "ALTER TABLE IF EXISTS sp.fact_inventory_daily_snapshot ADD COLUMN IF NOT EXISTS _source_system TEXT",
        "ALTER TABLE IF EXISTS sp.fact_inventory_daily_snapshot ADD COLUMN IF NOT EXISTS _source_record_id TEXT",
        "ALTER TABLE IF EXISTS sp.fact_inventory_daily_snapshot ADD COLUMN IF NOT EXISTS _connection_id TEXT",
        "ALTER TABLE IF EXISTS sp.fact_inventory_daily_snapshot ADD COLUMN IF NOT EXISTS _run_id TEXT",
        "ALTER TABLE IF EXISTS sp.fact_inventory_daily_snapshot ADD COLUMN IF NOT EXISTS _loaded_at TIMESTAMPTZ",
        "ALTER TABLE IF EXISTS sp.fact_downtime_event ADD COLUMN IF NOT EXISTS _source_system TEXT",
        "ALTER TABLE IF EXISTS sp.fact_downtime_event ADD COLUMN IF NOT EXISTS _source_record_id TEXT",
        "ALTER TABLE IF EXISTS sp.fact_downtime_event ADD COLUMN IF NOT EXISTS _connection_id TEXT",
        "ALTER TABLE IF EXISTS sp.fact_downtime_event ADD COLUMN IF NOT EXISTS _run_id TEXT",
        "ALTER TABLE IF EXISTS sp.fact_downtime_event ADD COLUMN IF NOT EXISTS _loaded_at TIMESTAMPTZ",
    ], ["DROP SCHEMA IF EXISTS integration CASCADE"]),
)

MIGRATIONS.append(
    # ------------------------------------------------------------------ data integration DI2 (database connectors)
    ("2026_11_14_007_integration_db", [
        "ALTER TABLE integration.connections ADD COLUMN IF NOT EXISTS can_write BOOLEAN",
        "ALTER TABLE integration.connections ADD COLUMN IF NOT EXISTS tls_active BOOLEAN",
        "ALTER TABLE integration.connections ADD COLUMN IF NOT EXISTS warnings TEXT",
    ], ["ALTER TABLE integration.connections DROP COLUMN IF EXISTS can_write",
        "ALTER TABLE integration.connections DROP COLUMN IF EXISTS tls_active",
        "ALTER TABLE integration.connections DROP COLUMN IF EXISTS warnings"]),
)

MIGRATIONS.append(
    # ------------------------------------------------------------------ data integration DI3 (mapping, staging, quarantine, runs)
    ("2026_11_14_008_integration_loads", [
        """CREATE TABLE IF NOT EXISTS integration.mappings (
             id VARCHAR(20) PRIMARY KEY, name TEXT NOT NULL, connection_id VARCHAR(20) NOT NULL, entity VARCHAR(40) NOT NULL,
             target_table VARCHAR(60) NOT NULL, source_kind VARCHAR(10) NOT NULL, source_object TEXT, source_query TEXT,
             field_map JSONB NOT NULL DEFAULT '{}'::jsonb, record_id_columns JSONB NOT NULL DEFAULT '[]'::jsonb,
             watermark_column TEXT, last_watermark TEXT, column_policy JSONB NOT NULL DEFAULT '{}'::jsonb,
             mode VARCHAR(10) NOT NULL DEFAULT 'STAGING', approval_status VARCHAR(10) NOT NULL DEFAULT 'NONE',
             requested_by VARCHAR(60), approved_by VARCHAR(60), approved_at TIMESTAMPTZ,
             schedule VARCHAR(40) NOT NULL DEFAULT 'manual', enabled BOOLEAN NOT NULL DEFAULT TRUE, next_run_at TIMESTAMPTZ,
             last_run_at TIMESTAMPTZ, last_run_id VARCHAR(24), last_run_status VARCHAR(12),
             max_quarantine_pct NUMERIC NOT NULL DEFAULT 5, max_rows INTEGER NOT NULL DEFAULT 200000, version INTEGER NOT NULL DEFAULT 1,
             active BOOLEAN NOT NULL DEFAULT TRUE, created_by VARCHAR(60), created_at TIMESTAMPTZ DEFAULT now(), updated_at TIMESTAMPTZ DEFAULT now())""",
        """CREATE TABLE IF NOT EXISTS integration.runs (
             id VARCHAR(24) PRIMARY KEY, mapping_id VARCHAR(20) NOT NULL, mapping_version INTEGER, connection_id VARCHAR(20), trigger VARCHAR(20),
             actor VARCHAR(60), dry_run BOOLEAN NOT NULL DEFAULT FALSE, status VARCHAR(12) NOT NULL, started_at TIMESTAMPTZ DEFAULT now(),
             finished_at TIMESTAMPTZ, watermark_from TEXT, watermark_to TEXT, extracted INTEGER DEFAULT 0, staged INTEGER DEFAULT 0,
             quarantined INTEGER DEFAULT 0, inserted INTEGER DEFAULT 0, updated INTEGER DEFAULT 0, restamped INTEGER DEFAULT 0,
             unchanged INTEGER DEFAULT 0, field_changes JSONB, samples JSONB, message TEXT)""",
        "CREATE INDEX IF NOT EXISTS runs_mapping_idx ON integration.runs (mapping_id, started_at DESC)",
        """CREATE TABLE IF NOT EXISTS integration.staging (
             run_id VARCHAR(24) NOT NULL, seq INTEGER NOT NULL, record_key TEXT NOT NULL, source_record_id TEXT, record JSONB NOT NULL,
             PRIMARY KEY (run_id, seq))""",
        """CREATE TABLE IF NOT EXISTS integration.quarantine (
             id BIGSERIAL PRIMARY KEY, run_id VARCHAR(24) NOT NULL, mapping_id VARCHAR(20) NOT NULL, source_record_id TEXT,
             source_values JSONB, errors JSONB NOT NULL, status VARCHAR(10) NOT NULL DEFAULT 'OPEN', resolved_by VARCHAR(60),
             resolved_at TIMESTAMPTZ, created_at TIMESTAMPTZ DEFAULT now())""",
        "CREATE INDEX IF NOT EXISTS quarantine_mapping_idx ON integration.quarantine (mapping_id, status)",
        "CREATE TABLE IF NOT EXISTS integration.settings (key VARCHAR(60) PRIMARY KEY, value TEXT NOT NULL)",
    ], ["DROP TABLE IF EXISTS integration.quarantine", "DROP TABLE IF EXISTS integration.staging", "DROP TABLE IF EXISTS integration.runs",
        "DROP TABLE IF EXISTS integration.mappings", "DROP TABLE IF EXISTS integration.settings"]),
)

MIGRATIONS.append(
    # DI3 fix: run lease on the mapping row (session advisory locks are unreliable behind Neon's PgBouncer transaction pooler)
    ("2026_11_14_009_integration_run_lease", [
        "ALTER TABLE integration.mappings ADD COLUMN IF NOT EXISTS running_run_id VARCHAR(24)",
        "ALTER TABLE integration.mappings ADD COLUMN IF NOT EXISTS running_since TIMESTAMPTZ",
    ], ["ALTER TABLE integration.mappings DROP COLUMN IF EXISTS running_run_id",
        "ALTER TABLE integration.mappings DROP COLUMN IF EXISTS running_since"]),
)

MIGRATIONS.append(
    # ------------------------------------------------------------------ data integration DI4 (file uploads, encrypted at rest)
    ("2026_11_15_010_integration_files", [
        """CREATE TABLE IF NOT EXISTS integration.files (
             id BIGSERIAL PRIMARY KEY, connection_id VARCHAR(20) NOT NULL, name TEXT NOT NULL, size_bytes INTEGER NOT NULL,
             sha256 VARCHAR(64) NOT NULL, row_count INTEGER, columns JSONB, content_enc BYTEA, uploaded_by VARCHAR(60),
             uploaded_at TIMESTAMPTZ DEFAULT now(), active BOOLEAN NOT NULL DEFAULT TRUE)""",
        "CREATE INDEX IF NOT EXISTS files_conn_idx ON integration.files (connection_id, active, uploaded_at)",
    ], ["DROP TABLE IF EXISTS integration.files"]),
)

MIGRATIONS.append(
    # ------------------------------------------------------------------ data integration DI5 (data quality, health, lineage, retention)
    ("2026_11_15_011_integration_quality", [
        """CREATE TABLE IF NOT EXISTS integration.dq_rules (
             id SERIAL PRIMARY KEY, entity VARCHAR(40) NOT NULL, name VARCHAR(60) NOT NULL, kind VARCHAR(30) NOT NULL,
             params JSONB NOT NULL, severity VARCHAR(5) NOT NULL DEFAULT 'ERROR', description TEXT, enabled BOOLEAN NOT NULL DEFAULT TRUE,
             created_by VARCHAR(60), created_at TIMESTAMPTZ DEFAULT now(), updated_at TIMESTAMPTZ DEFAULT now(), UNIQUE (entity, name))""",
        """INSERT INTO integration.dq_rules (entity, name, kind, params, severity, description, created_by) VALUES
        ('po_line', 'promise_not_null', 'not_null', CAST('{"field": "promised_date"}' AS jsonb), 'ERROR', 'Promised date must be present (OTIF needs it)', 'system'),
        ('po_line', 'ordered_qty_positive', 'range', CAST('{"field": "ordered_qty", "min": 1e-06}' AS jsonb), 'ERROR', 'Ordered quantity must be > 0', 'system'),
        ('po_line', 'received_not_negative', 'range', CAST('{"field": "received_qty", "min": 0}' AS jsonb), 'ERROR', 'Received quantity cannot be negative', 'system'),
        ('po_line', 'promise_after_po_date', 'date_order', CAST('{"first": "po_date", "second": "promised_date"}' AS jsonb), 'ERROR', 'Promise cannot be before the PO date', 'system'),
        ('po_line', 'receipt_after_po_date', 'date_order', CAST('{"first": "po_date", "second": "receipt_date"}' AS jsonb), 'ERROR', 'Receipt cannot be before the PO date', 'system'),
        ('po_line', 'over_receipt_50pct', 'ratio_max', CAST('{"field": "received_qty", "of": "ordered_qty", "max": 1.5}' AS jsonb), 'WARN', 'Received more than 150 % of ordered', 'system'),
        ('po_line', 'supplier_known', 'reference', CAST('{"field": "supplier_id", "entity": "supplier", "ref_field": "supplier_id"}' AS jsonb), 'ERROR', 'Supplier must exist in the supplier master', 'system'),
        ('po_line', 'material_known', 'reference', CAST('{"field": "material_id", "entity": "material", "ref_field": "material_id"}' AS jsonb), 'ERROR', 'Material must exist in the material master', 'system'),
        ('po_line', 'status_valid', 'allowed_values', CAST('{"field": "po_status", "values": ["OPEN", "PARTIAL", "CLOSED", "CANCELLED"]}' AS jsonb), 'WARN', 'Unexpected PO status', 'system'),
        ('supplier', 'supplier_name_present', 'not_null', CAST('{"field": "supplier_name"}' AS jsonb), 'ERROR', 'Supplier name must be present', 'system'),
        ('material', 'lead_time_present', 'not_null', CAST('{"field": "lead_time_days"}' AS jsonb), 'WARN', 'Lead time missing (lead-time KPIs use it)', 'system'),
        ('supplier_issue', 'capa_due_after_open', 'date_order', CAST('{"first": "opened_ts", "second": "capa_due_date"}' AS jsonb), 'WARN', 'CAPA due before the NCR was opened', 'system'),
        ('production_inspection', 'fail_has_defect', 'conditional_not_null', CAST('{"field": "defect_code", "when_field": "result", "when_value": "FAIL"}' AS jsonb), 'WARN', 'Failed inspection without a defect code', 'system')
        ON CONFLICT (entity, name) DO NOTHING""",
        "ALTER TABLE integration.runs ADD COLUMN IF NOT EXISTS dq_summary JSONB",
        "ALTER TABLE integration.mappings ADD COLUMN IF NOT EXISTS max_staleness_hours NUMERIC",
        """CREATE TABLE IF NOT EXISTS integration.health_state (
             mapping_id VARCHAR(20) PRIMARY KEY, status VARCHAR(12) NOT NULL, since TIMESTAMPTZ DEFAULT now(), notified_at TIMESTAMPTZ)""",
    ], ["DROP TABLE IF EXISTS integration.health_state", "ALTER TABLE integration.mappings DROP COLUMN IF EXISTS max_staleness_hours",
        "ALTER TABLE integration.runs DROP COLUMN IF EXISTS dq_summary", "DROP TABLE IF EXISTS integration.dq_rules"]),
)

# Feature flags: (key, phase, description). All start DISABLED; an ADMIN switches them on in Settings.
MIGRATIONS.append(
    # ------------------------------------------------------------------ UX3 supplier quality inquiry (ask the supplier for the reason)
    ("2026_11_16_012_quality_inquiry", [
        """CREATE TABLE IF NOT EXISTS public.supplier_contacts (
             supplier_id VARCHAR(20) PRIMARY KEY, contact_name VARCHAR(120), email VARCHAR(200) NOT NULL,
             updated_by VARCHAR(60), updated_at TIMESTAMPTZ DEFAULT now())""",
        """CREATE TABLE IF NOT EXISTS public.quality_inquiries (
             id SERIAL PRIMARY KEY, supplier_id VARCHAR(20) NOT NULL, material_id VARCHAR(40), defect_code VARCHAR(20), defect_name VARCHAR(120),
             subject VARCHAR(200) NOT NULL, message TEXT NOT NULL, question TEXT, evidence JSONB,
             status VARCHAR(12) NOT NULL DEFAULT 'DRAFT', origin VARCHAR(10) DEFAULT 'USER', alert_key VARCHAR(80), due_date DATE,
             created_by VARCHAR(60), created_at TIMESTAMPTZ DEFAULT now(),
             sent_at TIMESTAMPTZ, sent_by VARCHAR(60), sent_to VARCHAR(200), email_status VARCHAR(16), email_error TEXT,
             reason TEXT, containment TEXT, corrective_action TEXT, commit_date DATE,
             responded_at TIMESTAMPTZ, responded_via VARCHAR(12), responded_by VARCHAR(120),
             closed_at TIMESTAMPTZ, closed_by VARCHAR(60), close_note TEXT, history JSONB DEFAULT '[]'::jsonb)""",
        "CREATE INDEX IF NOT EXISTS ix_quality_inquiries_supplier ON public.quality_inquiries (supplier_id, status)",
    ], ["DROP TABLE IF EXISTS public.quality_inquiries", "DROP TABLE IF EXISTS public.supplier_contacts"]),
)
MIGRATIONS.append(
    ("2026_11_17_013_predictive_layer", [
        """CREATE TABLE IF NOT EXISTS public.ml_models (
             id SERIAL PRIMARY KEY, kind VARCHAR(40) NOT NULL, version VARCHAR(60) NOT NULL, trained_at TIMESTAMPTZ DEFAULT now(),
             trained_by VARCHAR(60), train_rows INTEGER, metrics JSONB, model JSONB, active BOOLEAN DEFAULT FALSE)""",
        "CREATE INDEX IF NOT EXISTS ix_ml_models_kind ON public.ml_models (kind, active)",
        """CREATE TABLE IF NOT EXISTS public.ml_predictions (
             id BIGSERIAL PRIMARY KEY, model_id INTEGER REFERENCES public.ml_models(id), as_of DATE NOT NULL, predicted_at TIMESTAMPTZ DEFAULT now(),
             po_id VARCHAR(40) NOT NULL, po_line INTEGER NOT NULL, supplier_id VARCHAR(20), p_late DOUBLE PRECISION, risk_band VARCHAR(10),
             expected_delay_days DOUBLE PRECISION, predicted_arrival DATE, drivers JSONB)""",
        "CREATE INDEX IF NOT EXISTS ix_ml_predictions_po ON public.ml_predictions (po_id, po_line)",
        "CREATE INDEX IF NOT EXISTS ix_ml_predictions_model ON public.ml_predictions (model_id, as_of)",
    ], ["DROP TABLE IF EXISTS public.ml_predictions", "DROP TABLE IF EXISTS public.ml_models"]),
)
MIGRATIONS.append(
    ("2026_11_18_014_delay_events", [
        # stage-by-stage plan vs actual of each PO line's journey, with the reason for any slip. Filled from carrier / TMS /
        # supplier feeds when connected; until then scripts/gen_delay_events.py writes rows tagged source='SYNTHETIC' (demo).
        """CREATE TABLE IF NOT EXISTS sp.fact_delay_event (
             id BIGSERIAL PRIMARY KEY, po_id VARCHAR(40) NOT NULL, po_line INTEGER NOT NULL, supplier_id VARCHAR(20),
             stage_seq INTEGER NOT NULL, stage VARCHAR(40) NOT NULL, location VARCHAR(120),
             planned_ts DATE, actual_ts DATE, added_delay_days NUMERIC(6, 1) DEFAULT 0,
             reason_code VARCHAR(40), reason_text TEXT, reported_by VARCHAR(20),
             source VARCHAR(20) DEFAULT 'SYNTHETIC', created_at TIMESTAMPTZ DEFAULT now())""",
        "CREATE INDEX IF NOT EXISTS ix_delay_event_po ON sp.fact_delay_event (po_id, po_line)",
        "CREATE INDEX IF NOT EXISTS ix_delay_event_sup ON sp.fact_delay_event (supplier_id)",
    ], ["DROP TABLE IF EXISTS sp.fact_delay_event"]),
)

MIGRATIONS.append(
    # supplier portal: a supplier registers its own vehicles and reports dispatch + GPS positions by form (device_type MANUAL)
    ("2026_11_18_015_supplier_fleet", [
        "ALTER TABLE public.vehicles ADD COLUMN IF NOT EXISTS supplier_id VARCHAR(20)",
        "ALTER TABLE public.vehicles ADD COLUMN IF NOT EXISTS driver_name VARCHAR(80)",
        "ALTER TABLE public.vehicles ADD COLUMN IF NOT EXISTS driver_phone VARCHAR(30)",
        "ALTER TABLE public.gps_pings ADD COLUMN IF NOT EXISTS note VARCHAR(200)",
        "ALTER TABLE public.gps_pings ADD COLUMN IF NOT EXISTS reported_by VARCHAR(60)",
        "CREATE INDEX IF NOT EXISTS ix_vehicles_supplier ON public.vehicles (supplier_id)",
    ], ["DROP INDEX IF EXISTS public.ix_vehicles_supplier", "ALTER TABLE public.gps_pings DROP COLUMN IF EXISTS reported_by",
        "ALTER TABLE public.gps_pings DROP COLUMN IF EXISTS note", "ALTER TABLE public.vehicles DROP COLUMN IF EXISTS driver_phone",
        "ALTER TABLE public.vehicles DROP COLUMN IF EXISTS driver_name", "ALTER TABLE public.vehicles DROP COLUMN IF EXISTS supplier_id"]),
)

MIGRATIONS.append(
    # automatic supplier follow-up (AUTO1): the agent asks suppliers to confirm a new date for every late PO line,
    # reminds, escalates and records the reply - through the supplier portal and in-app, never by e-mail to suppliers
    ("2026_11_19_016_supplier_followups", [
        """CREATE TABLE IF NOT EXISTS public.supplier_followups (
             id BIGSERIAL PRIMARY KEY, po_id VARCHAR(40) NOT NULL, po_line INTEGER NOT NULL, supplier_id VARCHAR(20) NOT NULL,
             kind VARCHAR(20) NOT NULL DEFAULT 'DATE_CONFIRM', stage VARCHAR(16) NOT NULL DEFAULT 'REQUESTED',
             requested_at TIMESTAMPTZ NOT NULL DEFAULT now(), reply_due TIMESTAMPTZ NOT NULL,
             reminded_at TIMESTAMPTZ, escalated_at TIMESTAMPTZ, responded_at TIMESTAMPTZ, closed_at TIMESTAMPTZ,
             confirmed_date DATE, reason_code VARCHAR(40), reason_text TEXT, responded_by VARCHAR(60),
             history JSONB NOT NULL DEFAULT '[]'::jsonb)""",
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_followup_open ON public.supplier_followups (po_id, po_line) WHERE stage <> 'CLOSED'",
        "CREATE INDEX IF NOT EXISTS ix_followup_supplier ON public.supplier_followups (supplier_id, stage)",
    ], ["DROP TABLE IF EXISTS public.supplier_followups"]),
)

MIGRATIONS.append(
    # UAT performance: the optional extract tables had no indexes; v_po_delivery joins fact_grn per PO line
    ("2026_11_23_017_extract_indexes", [
        "CREATE INDEX IF NOT EXISTS ix_grn_po ON sp.fact_grn (po_id, po_line)",
        "CREATE INDEX IF NOT EXISTS ix_incoming_insp_po ON sp.fact_incoming_inspection (po_id, po_line)",
        "CREATE INDEX IF NOT EXISTS ix_incoming_insp_sup ON sp.fact_incoming_inspection (supplier_id)",
        "CREATE INDEX IF NOT EXISTS ix_invoice_po ON sp.fact_invoice (po_id, po_line)",
        "CREATE INDEX IF NOT EXISTS ix_schedule_sup ON sp.fact_schedule_line (supplier_id)",
        "CREATE INDEX IF NOT EXISTS ix_milestone_po ON sp.fact_inbound_milestone (po_id, po_line)",
        "ANALYZE sp.fact_grn", "ANALYZE sp.fact_incoming_inspection", "ANALYZE sp.fact_invoice",
    ], ["DROP INDEX IF EXISTS sp.ix_grn_po", "DROP INDEX IF EXISTS sp.ix_incoming_insp_po", "DROP INDEX IF EXISTS sp.ix_incoming_insp_sup",
        "DROP INDEX IF EXISTS sp.ix_invoice_po", "DROP INDEX IF EXISTS sp.ix_schedule_sup", "DROP INDEX IF EXISTS sp.ix_milestone_po"]),
)

FLAGS = [
    ("safe_query_layer", "1.1", "Agent queries only approved metrics/views; run_sql restricted to approved views (no raw tables)"),
    ("chat_charts", "1.1", "Agent answers include a chart and a 'show source records' link"),
    ("role_dashboards", "1.2", "Procurement / Quality / Management dashboards"),
    ("actionable_alerts", "1.3", "Alert inbox with acknowledge / assign / request supplier response, and action links in Teams/e-mail"),
    ("review_pack_export", "1.4", "Server-generated PDF / PPTX supplier review packs"),
    ("tracker_cleanup", "T1", "Network Tracker clean-up: per-origin sea lanes via Indian entry ports, consolidated vessels/trucks "
                              "with counts, one ring per meaning, legend, label de-cluttering, estimated-position labelling"),
    ("network_twin_v2", "T2", "Network twin map (MapLibre + deck.gl): real road routes (OSRM, coordinates only, cached), real sea "
                              "lanes (searoute, offline), positions from milestones/GPS when available, Live / Milestone / Estimated labels"),
    ("twin_risk_encoding", "T3", "Twin map: supplier colour = health band (approved risk thresholds), size = disruption hours; lane & vehicle colour "
                                 "= on time / at risk / delayed vs promise date; legend shows the thresholds"),
    ("twin_disruptions", "T3", "Twin map: disruption overlays from pluggable providers (GDACS, Open-Meteo, manual) with source and refresh time"),
    ("twin_side_panel", "T3", "Twin side panel: scorecard, open POs, in-transit, alerts, actions; every number links to PO / GRN source records"),
    ("twin_filters", "T3", "Twin filters (supplier, plant, material group, risk, mode, date range) and saved views"),
    ("gps_tracking", "T3", "GPS tracking: vehicle registry, trips, GPS ingestion API (phone / telematics / AIS / simulator), driver phone page; "
                           "assigned shipments shown LIVE at their GPS position"),
    ("di_quality_health", "DI5", "Data Integration: data-quality rules (quarantine on ERROR), quarantine work-queue, source health "
                                  "dashboard with failed / late-load alerts, end-to-end lineage, retention of files and closed quarantine"),
    ("di_files_apis", "DI4", "Data Integration: SAP S/4 OData, REST, MES REST, CSV / Excel upload (encrypted at rest), SFTP and OPC UA "
                              "(read-only) connectors with SSRF protection"),
    ("di_mapping_loads", "DI3", "Data Integration: field mapping to the canonical model, column exclusion / masking before storage, staging, "
                                 "quarantine, idempotent incremental loads with lineage, schedules, run history, reconciliation"),
    ("di_database_connectors", "DI2", "Data Integration: PostgreSQL, Oracle and SQL Server connectors (TLS, SSH tunnel, write-privilege warning, "
                                       "schema discovery, masked preview, SELECT-only custom queries)"),
    ("data_integration", "DI1", "Data Integration page: connector gallery, connections with vault-stored secrets, four-eyes approval "
                                "for production sources, canonical model, audit log"),
    ("tracker_3d_twins", "UX4", "Network map: click a ship or truck for a 3D digital twin of the materials on board; click a supplier for a 3D "
                                "twin of its site with profile, owners, materials supplied and delivery history"),
    ("quality_inquiry", "UX3", "Supplier quality inquiry: when material shows poor quality the agent drafts a request asking the supplier "
                               "for the reason; a person sends it by e-mail (signed reply link) and to the supplier portal; the reply comes back "
                               "into Risk Radar → Actions"),
    ("workflow_extras", "UX2", "Workflow extras: template-built 8D / CAPA request drafts (to the supplier portal after approval), weekly "
                               "supplier digest for procurement and quality owners, and a one-click dispute evidence pack (PDF) per PO line"),
    ("agent_architecture", "UX1", "Agent Architecture page: animated 3D workflow of the agent, driven live by real runs (specialist, "
                                  "datasets, validation, answer); drill-through links in chat answers"),
    ("delay_reasoning", "AI3", "Why is this late? Stage-by-stage plan vs actual of each PO line's journey, where the time was lost, root "
                               "cause, contributing factors (tight promise, route disruptions, repeat pattern, supplier's stated reason), "
                               "impact and recommended actions; delay-cause Pareto per supplier (demo logistics data until feeds are connected)"),
    ("predictive_layer", "AI2", "Predictive layer: late-delivery probability, expected delay and predicted arrival for every open PO line "
                                "(explainable model, validated on later orders), 30-day supplier on-time forecast and quality early warnings "
                                "(control limits); predictions are stored for audit and are advisory only"),
    ("auto_followup", "AUTO1", "Automatic supplier follow-up: for every late PO line the agent asks the supplier (portal) to confirm a new "
                               "date within the reply window, reminds, escalates to procurement, records the reply; auto-approves small "
                               "promise-date changes requested before the original date (policy). Never e-mails suppliers"),
    ("plant_twin", "T4", "Plant digital twin: click a plant for docks, quality hold, warehouse stock, lines, days of cover per material, "
                         "arrivals in 24/72 h and lines exposed if a shipment is late"),
]


def run_migrations(engine) -> list[str]:
    applied_now = []
    with engine.begin() as c:
        c.execute(text("CREATE TABLE IF NOT EXISTS public.schema_migrations (id VARCHAR(80) PRIMARY KEY, applied_at TIMESTAMPTZ DEFAULT now())"))
        done = {r[0] for r in c.execute(text("SELECT id FROM public.schema_migrations"))}
        for mid, up, _down in MIGRATIONS:
            if mid in done:
                continue
            for stmt in up:
                c.execute(text(stmt))
            c.execute(text("INSERT INTO public.schema_migrations (id) VALUES (:i)"), {"i": mid})
            applied_now.append(mid)
        for key, phase, desc in FLAGS:
            c.execute(text("""INSERT INTO public.feature_flags (key, enabled, phase, description, updated_by)
                              VALUES (:k, FALSE, :p, :d, 'migration') ON CONFLICT (key) DO UPDATE SET description = EXCLUDED.description,
                              phase = EXCLUDED.phase"""), {"k": key, "p": phase, "d": desc})
    return applied_now


def rollback(engine, mid: str) -> None:
    down = next((d for m, _u, d in MIGRATIONS if m == mid), None)
    if down is None:
        raise SystemExit(f"unknown migration {mid}")
    with engine.begin() as c:
        for stmt in down:
            c.execute(text(stmt))
        c.execute(text("DELETE FROM public.schema_migrations WHERE id = :i"), {"i": mid})
