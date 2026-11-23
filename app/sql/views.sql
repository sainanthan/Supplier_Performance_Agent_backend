-- Supplier Performance: KPI views over the raw tables in schema sp.
-- Re-runnable. Executed by scripts/load_data.py (and POST /api/admin/rebuild-views).

-- ================================================================ helpers
CREATE TABLE IF NOT EXISTS sp.meta (key TEXT PRIMARY KEY, value TEXT);

-- Reference ("as-of") date. Default = the data cut-off (latest inspection / NCR event), NOT today,
-- so ageing and past-due logic do not inflate when the extract is older than today.
-- Override:  INSERT INTO sp.meta VALUES ('as_of_date','2026-10-31') ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value;
CREATE OR REPLACE FUNCTION sp.as_of_date() RETURNS date LANGUAGE sql STABLE SECURITY DEFINER SET search_path = sp, public AS $$
  SELECT COALESCE((SELECT value::date FROM sp.meta WHERE key = 'as_of_date'),
                  (SELECT GREATEST((SELECT MAX(inspection_ts)::date FROM sp.fact_quality_inspection),
                                   (SELECT MAX(opened_ts)::date FROM sp.fact_ncr_capa))),
                  CURRENT_DATE)
$$;

-- Numeric KPI definition from the approved config (public.app_config 'scorecard').
CREATE OR REPLACE FUNCTION sp.cfg_num(k text, dflt numeric) RETURNS numeric LANGUAGE sql STABLE SECURITY DEFINER SET search_path = sp, public AS $$
  SELECT COALESCE((SELECT (value::jsonb ->> k)::numeric FROM public.app_config WHERE key = 'scorecard'), dflt)
$$;

-- ---------------------------------------------------------------- working-day calendar (G1 / B2)
CREATE TABLE IF NOT EXISTS sp.dim_shift_calendar (calendar_date DATE, shift_id TEXT, shift_start TIMESTAMP, shift_end TIMESTAMP,
  planned_minutes INTEGER, break_minutes INTEGER, is_working_day BOOLEAN);
DROP TABLE IF EXISTS sp.work_calendar CASCADE;
CREATE TABLE sp.work_calendar AS
SELECT g::date AS d,
       COALESCE(c.w, NOT (EXTRACT(DOW FROM g)::int = ANY (ARRAY(
         SELECT jsonb_array_elements_text(COALESCE((SELECT value::jsonb -> 'non_working_weekdays' FROM public.app_config WHERE key = 'scorecard'), '[0]'::jsonb))::int)))) AS working,
       (c.w IS NOT NULL) AS from_shift_calendar
FROM generate_series(DATE '2025-01-01', DATE '2027-12-31', INTERVAL '1 day') g
LEFT JOIN (SELECT calendar_date, bool_or(is_working_day) AS w FROM sp.dim_shift_calendar GROUP BY calendar_date) c ON c.calendar_date = g::date;
ALTER TABLE sp.work_calendar ADD PRIMARY KEY (d);

-- Working days in (a, b]; negative when b < a.
CREATE OR REPLACE FUNCTION sp.working_days(a date, b date) RETURNS integer LANGUAGE sql STABLE AS $$
  SELECT CASE WHEN a IS NULL OR b IS NULL THEN NULL
              WHEN b >= a THEN (SELECT COUNT(*)::int FROM sp.work_calendar WHERE d > a AND d <= b AND working)
              ELSE -(SELECT COUNT(*)::int FROM sp.work_calendar WHERE d > b AND d <= a AND working) END
$$;

-- ---------------------------------------------------------------- documented, editable mapping assumptions
CREATE TABLE IF NOT EXISTS sp.defect_material_map (
  defect_category TEXT NOT NULL, material_id TEXT NOT NULL, rationale TEXT, PRIMARY KEY (defect_category, material_id));
INSERT INTO sp.defect_material_map VALUES
  ('SMT','MAT-PCB-004','Solder bridges / missing parts / tombstoning trace to PCB pads'),
  ('SMT','MAT-PASTE-017','Solder paste quality drives SMT joint defects'),
  ('DISPLAY','MAT-DSP-005','Dead pixels originate in the AMOLED display'),
  ('BATTERY','MAT-BAT-006','Swelling originates in the Li-ion cell'),
  ('CAMERA','MAT-CAM-007','Rear camera module focus'),
  ('CAMERA','MAT-CAM-008','Front camera module focus'),
  ('COSMETIC','MAT-GLS-010','Scratches on cover glass'),
  ('COSMETIC','MAT-HSG-009','Scratches on mid-frame'),
  ('FUNCTIONAL','MAT-CON-013','USB-C charging failure traces to the connector'),
  ('RF','MAT-ANT-012','RF sensitivity traces to the antenna flex'),
  ('AUDIO','MAT-SPK-011','Speaker distortion traces to the speaker module'),
  ('TRACEABILITY','MAT-BOX-014','IMEI label mismatch on retail packaging'),
  ('ASSEMBLY','MAT-HSG-009','Back cover gap from mid-frame tolerance'),
  ('ASSEMBLY','MAT-SCR-016','Back cover gap from screw set'),
  ('ASSEMBLY','MAT-ADH-018','Back cover gap from adhesive')
ON CONFLICT DO NOTHING;

-- Which material categories each line type consumes (E6: a NO_MATERIAL stop is blamed only on those suppliers).
CREATE TABLE IF NOT EXISTS sp.line_material_category (
  line_type TEXT NOT NULL, material_category TEXT NOT NULL, rationale TEXT, PRIMARY KEY (line_type, material_category));
INSERT INTO sp.line_material_category VALUES
  ('SMT','SEMICONDUCTOR','SoC / memory placed on SMT'), ('SMT','PCB','Bare boards loaded on SMT'), ('SMT','CONSUMABLE','Paste / adhesive'),
  ('Final Assembly','DISPLAY','Display fitted in FAL'), ('Final Assembly','BATTERY','Battery fitted in FAL'), ('Final Assembly','CAMERA','Camera modules'),
  ('Final Assembly','MECHANICAL','Frame, glass, screws'), ('Final Assembly','AUDIO','Speaker module'), ('Final Assembly','RF','Antenna flex'),
  ('Final Assembly','CONNECTOR','USB-C connector'), ('Final Assembly','PACKAGING','Box and cable')
ON CONFLICT DO NOTHING;

-- ---------------------------------------------------------------- optional source tables (loaded when the extract exists)
CREATE TABLE IF NOT EXISTS sp.fact_grn (grn_id TEXT, po_id TEXT, po_line INTEGER, receipt_date DATE, received_qty DOUBLE PRECISION,
  accepted_qty DOUBLE PRECISION, rejected_qty DOUBLE PRECISION, plant_id TEXT);
CREATE TABLE IF NOT EXISTS sp.fact_incoming_inspection (inspection_lot_id TEXT, grn_id TEXT, po_id TEXT, po_line INTEGER, supplier_id TEXT,
  material_id TEXT, material_lot_id TEXT, plant_id TEXT, inspection_date DATE, inspected_qty DOUBLE PRECISION,
  accepted_qty DOUBLE PRECISION, rejected_qty DOUBLE PRECISION, defect_code TEXT, usage_decision TEXT);
CREATE TABLE IF NOT EXISTS sp.fact_schedule_line (agreement_id TEXT, schedule_line INTEGER, supplier_id TEXT, material_id TEXT, plant_id TEXT,
  delivery_date DATE, scheduled_qty DOUBLE PRECISION, received_qty DOUBLE PRECISION, receipt_date DATE);
CREATE TABLE IF NOT EXISTS sp.fact_invoice (invoice_id TEXT, po_id TEXT, po_line INTEGER, supplier_id TEXT, invoice_date DATE,
  invoiced_qty DOUBLE PRECISION, invoice_unit_price_inr DOUBLE PRECISION, currency TEXT);
CREATE TABLE IF NOT EXISTS sp.fact_inbound_milestone (shipment_ref TEXT, po_id TEXT, po_line INTEGER, milestone TEXT,
  milestone_ts TIMESTAMP, location TEXT, lat DOUBLE PRECISION, lon DOUBLE PRECISION);
-- source = ERP / TMS / GPS / SYNTHETIC (demo generator). SYNTHETIC rows are always shown as "Simulated demo data".
ALTER TABLE sp.fact_inbound_milestone ADD COLUMN IF NOT EXISTS source TEXT;

-- ================================================================ delivery
DROP VIEW IF EXISTS sp.v_material_supplier_share CASCADE;
DROP VIEW IF EXISTS sp.v_supply_disruption CASCADE;
DROP VIEW IF EXISTS sp.v_invoice_match CASCADE;
DROP VIEW IF EXISTS sp.v_po_delivery CASCADE;
CREATE VIEW sp.v_po_delivery AS
-- constants computed ONCE per query (the helper functions run with SECURITY DEFINER, so the planner cannot fold them;
-- called per row they made this view take 12-20 s for 1,300 lines)
WITH k AS (
  SELECT sp.as_of_date() AS asof, sp.cfg_num('in_full_tolerance_pct', 5) / 100.0 AS tol_under,
         sp.cfg_num('over_delivery_tolerance_pct', 10) / 100.0 AS tol_over, sp.cfg_num('early_window_days', NULL) AS early_days,
         sp.cfg_num('lead_time_tolerance_days', 2) AS lt_tol
), pc AS (       -- last APPROVED promise change that was approved on/before the ORIGINAL promise (A6)
  SELECT DISTINCT ON (c.po_id, c.po_line) c.po_id, c.po_line, c.new_promise, c.decided_by, c.approved_at, c.id AS change_id
  FROM public.promise_changes c
  JOIN sp.fact_purchase_order_receipt p0 ON p0.po_id = c.po_id AND p0.po_line = c.po_line
  WHERE c.status = 'APPROVED' AND c.approved_at::date <= p0.promised_date
  ORDER BY c.po_id, c.po_line, c.approved_at DESC
), base AS (
  SELECT p.*, COALESCE(pc.new_promise, p.promised_date) AS eff_promise, pc.change_id, pc.decided_by AS promise_approved_by
  FROM sp.fact_purchase_order_receipt p LEFT JOIN pc ON pc.po_id = p.po_id AND pc.po_line = p.po_line
), rcv AS (        -- split receipts (G1): several GRNs per PO line when fact_grn is loaded
  SELECT b.po_id, b.po_line, COUNT(g.po_id) AS n_grn,
         MIN(g.receipt_date) AS first_grn, MAX(g.receipt_date) AS last_grn, SUM(g.received_qty) AS grn_qty,
         SUM(g.received_qty) FILTER (WHERE g.receipt_date <= b.eff_promise) AS grn_qty_by_promise
  FROM base b LEFT JOIN sp.fact_grn g ON g.po_id = b.po_id AND g.po_line = b.po_line
  GROUP BY b.po_id, b.po_line
), x AS (
  SELECT b.*, r.n_grn,
         CASE WHEN r.n_grn > 0 THEN r.last_grn ELSE b.receipt_date END AS rcpt_last,
         CASE WHEN r.n_grn > 0 THEN r.first_grn ELSE b.receipt_date END AS rcpt_first,
         CASE WHEN r.n_grn > 0 THEN r.grn_qty ELSE b.received_qty END AS qty_rcvd,
         CASE WHEN r.n_grn > 0 THEN COALESCE(r.grn_qty_by_promise, 0)
              WHEN b.receipt_date <= b.eff_promise THEN b.received_qty ELSE 0 END AS qty_by_promise,
         k.tol_under, k.tol_over, k.early_days, k.lt_tol, k.asof
  FROM base b JOIN rcv r ON r.po_id = b.po_id AND r.po_line = b.po_line CROSS JOIN k
)
SELECT
  x.po_id, x.po_line, x.supplier_id, s.supplier_name, s.country AS supplier_country, s.risk_tier,
  x.material_id, m.material_name, m.material_category, x.plant_id,
  x.po_date, x.eff_promise AS promised_date, x.promised_date AS original_promised_date,
  (x.change_id IS NOT NULL) AS promise_changed, x.promise_approved_by,
  x.rcpt_last AS receipt_date, x.rcpt_first AS first_receipt_date, GREATEST(x.n_grn, 1) AS n_receipts,
  x.po_status, x.incoterm, x.ordered_qty, x.qty_rcvd AS received_qty, x.qty_by_promise, x.unit_price_inr, m.standard_price_inr,
  x.qty_rcvd * x.unit_price_inr                                   AS receipt_value_inr,
  (x.unit_price_inr - m.standard_price_inr) * x.qty_rcvd           AS price_variance_inr,
  CASE WHEN x.rcpt_last <= x.asof THEN 'RECEIVED'
       WHEN x.eff_promise < x.asof THEN 'PAST_DUE'
       ELSE 'NOT_DUE' END                                          AS delivery_status,
  (x.rcpt_last - x.eff_promise)                                   AS delay_days,
  sp.working_days(x.eff_promise, x.rcpt_last)                     AS delay_working_days,
  (x.rcpt_last - x.po_date)                                       AS actual_lead_time_days,
  sp.working_days(x.po_date, x.rcpt_last)                         AS actual_lead_time_wd,
  (x.eff_promise - x.po_date)                                     AS promised_lead_time_days,
  s.standard_lead_time_days,
  COALESCE(m.lead_time_days, s.standard_lead_time_days)           AS planned_lead_time_days,
  CASE WHEN m.lead_time_days IS NOT NULL THEN 'MATERIAL' ELSE 'SUPPLIER' END AS lead_time_basis,
  -- lead-time variance in WORKING days vs the material's planned lead time (fallback: supplier standard)
  sp.working_days(x.po_date, x.rcpt_last) - COALESCE(m.lead_time_days, s.standard_lead_time_days) AS lead_time_variance_days,
  (x.early_days IS NOT NULL AND x.rcpt_first < x.eff_promise - x.early_days::int)               AS early_delivery,
  (x.qty_rcvd > x.ordered_qty * (1 + x.tol_over))                                                AS over_delivery,
  (x.rcpt_last <= x.eff_promise AND NOT (x.early_days IS NOT NULL AND x.rcpt_first < x.eff_promise - x.early_days::int)) AS on_time,
  (x.qty_rcvd >= x.ordered_qty * (1 - x.tol_under) AND x.qty_rcvd <= x.ordered_qty * (1 + x.tol_over)) AS in_full,
  (x.qty_by_promise >= x.ordered_qty * (1 - x.tol_under) AND x.qty_rcvd <= x.ordered_qty * (1 + x.tol_over)
     AND NOT (x.early_days IS NOT NULL AND x.rcpt_first < x.eff_promise - x.early_days::int))     AS otif,
  (sp.working_days(x.po_date, x.rcpt_last) <= COALESCE(m.lead_time_days, s.standard_lead_time_days) + x.lt_tol) AS lead_time_adherent,
  (e.id IS NOT NULL)                                              AS excluded,
  e.reason                                                        AS exclusion_reason,
  (e.id IS NULL AND (x.rcpt_last <= x.asof OR x.eff_promise < x.asof)) AS measurable
FROM x
JOIN sp.dim_supplier s ON s.supplier_id = x.supplier_id
JOIN sp.dim_material m ON m.material_id = x.material_id
LEFT JOIN public.kpi_exclusions e ON e.entity_type = 'PO_LINE' AND e.revoked_at IS NULL AND e.po_id = x.po_id AND e.po_line = x.po_line;

-- Schedule-agreement delivery lines (B1), measured like PO lines when the extract is loaded.
DROP VIEW IF EXISTS sp.v_schedule_delivery;
CREATE VIEW sp.v_schedule_delivery AS
SELECT sl.*, (sl.receipt_date <= sl.delivery_date) AS on_time,
       (sl.received_qty >= sl.scheduled_qty * (1 - sp.cfg_num('in_full_tolerance_pct', 5) / 100.0)) AS in_full,
       (sl.receipt_date <= sl.delivery_date AND sl.received_qty >= sl.scheduled_qty * (1 - sp.cfg_num('in_full_tolerance_pct', 5) / 100.0)) AS otif
FROM sp.fact_schedule_line sl WHERE sl.receipt_date IS NOT NULL OR sl.delivery_date < sp.as_of_date();

-- Three-way match: PO vs receipt vs invoice (B1).
CREATE VIEW sp.v_invoice_match AS
SELECT i.*, d.ordered_qty, d.received_qty, d.unit_price_inr AS po_price_inr,
       (ABS(i.invoiced_qty - d.received_qty) <= 0.005 * d.ordered_qty)          AS qty_match,
       (ABS(i.invoice_unit_price_inr - d.unit_price_inr) <= 0.005 * d.unit_price_inr) AS price_match,
       (i.invoice_unit_price_inr - d.standard_price_inr) * i.invoiced_qty         AS invoice_price_variance_inr
FROM sp.fact_invoice i JOIN sp.v_po_delivery d ON d.po_id = i.po_id AND d.po_line = i.po_line;

-- ================================================================ quality
-- Failed (production) inspections attributed to the suppliers whose lots of the implicated material went into the same
-- production order. method + confidence make the probabilistic nature explicit (A7). Excluded inspections are dropped.
DROP VIEW IF EXISTS sp.v_defect_attribution;
CREATE VIEW sp.v_defect_attribution AS
WITH fails AS (
  SELECT q.inspection_id, q.inspection_ts, q.plant_id, q.line_id, q.station_id, q.product_id,
         q.production_order_id, q.inspection_stage, q.defect_code, d.defect_name, d.defect_category, q.severity, q.disposition
  FROM sp.fact_quality_inspection q
  JOIN sp.dim_defect_code d ON d.defect_code = q.defect_code
  WHERE q.result = 'FAIL'
    AND NOT EXISTS (SELECT 1 FROM public.kpi_exclusions e WHERE e.entity_type = 'INSPECTION' AND e.revoked_at IS NULL AND e.entity_id = q.inspection_id)
), cand AS (
  SELECT f.inspection_id, g.supplier_id, g.material_id, array_agg(DISTINCT g.material_lot_id) AS material_lots
  FROM fails f
  JOIN sp.defect_material_map mm ON mm.defect_category = f.defect_category
  JOIN sp.fact_product_genealogy_event g ON g.production_order_id = f.production_order_id AND g.material_id = mm.material_id
  GROUP BY f.inspection_id, g.supplier_id, g.material_id
)
SELECT f.*, c.supplier_id, c.material_id, c.material_lots,
       COUNT(*) OVER (PARTITION BY f.inspection_id)            AS n_candidates,
       1.0 / COUNT(*) OVER (PARTITION BY f.inspection_id)      AS attribution_weight,
       1.0 / COUNT(*) OVER (PARTITION BY f.inspection_id)      AS confidence,
       'GENEALOGY_PRODUCTION_ORDER'::text                       AS attribution_method
FROM fails f JOIN cand c ON c.inspection_id = f.inspection_id;

-- Incoming (GRN) inspection: the direct supplier rejection rate when that extract is loaded (A3 / E2).
DROP VIEW IF EXISTS sp.v_incoming_quality;
CREATE VIEW sp.v_incoming_quality AS
SELECT ii.*, 'INCOMING_INSPECTION'::text AS method FROM sp.fact_incoming_inspection ii
WHERE NOT EXISTS (SELECT 1 FROM public.kpi_exclusions e WHERE e.entity_type = 'INSPECTION' AND e.revoked_at IS NULL AND e.entity_id = ii.inspection_lot_id);

-- NCR / CAPA attributed to suppliers, with ageing and supplier response time (A4 / A5 / E4 / E5).
DROP VIEW IF EXISTS sp.v_ncr_supplier;
CREATE VIEW sp.v_ncr_supplier AS
WITH base AS (
  SELECT n.ncr_id, n.inspection_id, n.opened_ts, n.defect_code, d.defect_name, d.defect_category,
         d.severity AS defect_severity, n.root_cause_category, n.containment_action, n.owner_id,
         n.capa_due_date, n.capa_status, n.effectiveness_check,
         q.plant_id, q.line_id, q.product_id, q.production_order_id,
         (n.capa_status IN ('OPEN','IN_PROGRESS'))                                        AS is_open,
         (n.capa_status IN ('OPEN','IN_PROGRESS') AND n.capa_due_date < sp.as_of_date())  AS is_overdue,
         CASE WHEN n.capa_status IN ('OPEN','IN_PROGRESS') THEN sp.as_of_date() - n.opened_ts::date END AS open_age_days,
         CASE WHEN n.capa_status IN ('OPEN','IN_PROGRESS') AND n.capa_due_date < sp.as_of_date()
              THEN sp.as_of_date() - n.capa_due_date END                                   AS days_overdue,
         (n.root_cause_category = 'MATERIAL' OR n.containment_action = 'SUPPLIER_ALERT')   AS supplier_related
  FROM sp.fact_ncr_capa n
  JOIN sp.fact_quality_inspection q ON q.inspection_id = n.inspection_id
  JOIN sp.dim_defect_code d ON d.defect_code = n.defect_code
  WHERE NOT EXISTS (SELECT 1 FROM public.kpi_exclusions e WHERE e.entity_type = 'NCR' AND e.revoked_at IS NULL AND e.entity_id = n.ncr_id)
), cand AS (
  SELECT DISTINCT b.ncr_id, g.supplier_id, g.material_id
  FROM base b
  JOIN sp.defect_material_map mm ON mm.defect_category = b.defect_category
  JOIN sp.fact_product_genealogy_event g ON g.production_order_id = b.production_order_id AND g.material_id = mm.material_id
), resp AS (
  SELECT ncr_id, supplier_id, MIN(responded_at) AS first_response_at FROM public.ncr_responses GROUP BY ncr_id, supplier_id
)
SELECT b.*, c.supplier_id, c.material_id,
       1.0 / COUNT(*) OVER (PARTITION BY b.ncr_id) AS attribution_weight,
       r.first_response_at,
       EXTRACT(EPOCH FROM (r.first_response_at - b.opened_ts)) / 86400.0 AS response_days
FROM base b JOIN cand c ON c.ncr_id = b.ncr_id
LEFT JOIN resp r ON r.ncr_id = b.ncr_id AND r.supplier_id = c.supplier_id;

-- ================================================================ disruption
-- NO_MATERIAL downtime blamed only on suppliers that were LATE for a material the stopped line actually consumes (E6).
CREATE VIEW sp.v_supply_disruption AS
WITH ev AS (
  SELECT d.downtime_id, d.plant_id, d.line_id, l.line_type, d.start_ts, d.duration_min, d.impact_units
  FROM sp.fact_downtime_event d JOIN sp.dim_production_line l ON l.line_id = d.line_id
  WHERE d.reason_code = 'NO_MATERIAL'
), late AS (
  SELECT DISTINCT ev.downtime_id, p.supplier_id
  FROM ev
  JOIN sp.line_material_category lm ON lm.line_type = ev.line_type
  JOIN sp.v_po_delivery p ON p.plant_id = ev.plant_id AND p.material_category = lm.material_category AND NOT p.excluded
   AND ev.start_ts::date > p.promised_date AND ev.start_ts::date <= p.receipt_date
)
SELECT ev.*, l.supplier_id,
       CASE WHEN l.supplier_id IS NULL THEN 1.0 ELSE 1.0 / COUNT(*) OVER (PARTITION BY ev.downtime_id) END AS attribution_weight
FROM ev LEFT JOIN late l ON l.downtime_id = ev.downtime_id;

-- ================================================================ concentration
CREATE VIEW sp.v_material_supplier_share AS
SELECT material_id, material_name, supplier_id, supplier_name, risk_tier, supplier_country,
       SUM(receipt_value_inr) AS spend_inr,
       100.0 * SUM(receipt_value_inr) / NULLIF(SUM(SUM(receipt_value_inr)) OVER (PARTITION BY material_id), 0) AS share_pct
FROM sp.v_po_delivery WHERE NOT excluded
GROUP BY material_id, material_name, supplier_id, supplier_name, risk_tier, supplier_country;

COMMENT ON VIEW sp.v_po_delivery IS 'One row per PO line: effective (approved) promise, split receipts, OTIF with early/over-delivery rules, working-day lead time. Use measurable=true.';
COMMENT ON VIEW sp.v_defect_attribution IS 'Failed production inspections attributed to supplier/material/lot via genealogy; SUM(attribution_weight) = defects; confidence = 1/candidates.';
COMMENT ON VIEW sp.v_incoming_quality IS 'Incoming (GRN) inspection lots: direct supplier rejection rate (preferred PPM source when loaded).';
COMMENT ON VIEW sp.v_ncr_supplier IS 'NCR/CAPA per implicated supplier: open/overdue, ageing, days overdue, first supplier response time.';
COMMENT ON VIEW sp.v_supply_disruption IS 'NO_MATERIAL downtime blamed on suppliers late for materials the stopped line consumes.';
COMMENT ON VIEW sp.v_material_supplier_share IS 'Spend share by material and supplier (concentration risk).';
