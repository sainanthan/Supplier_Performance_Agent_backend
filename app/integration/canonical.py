"""Canonical data model (DI Phase 1).

Every connector maps into these entities. They ARE the existing sp.* tables that the KPI views read - no parallel model -
so integration only has to fill them correctly. Each entry documents the business key (used for idempotent upserts), the
required fields with types, typical source fields (SAP / Oracle) and which KPIs / pages consume it (lineage).
Lineage columns (_source_system, _source_record_id, _connection_id, _run_id, _loaded_at) are added to every table so a
score can be traced score -> PO / GRN -> source system record.
"""
from __future__ import annotations

LINEAGE_COLS = [("_source_system", "TEXT"), ("_source_record_id", "TEXT"), ("_connection_id", "TEXT"),
                ("_run_id", "TEXT"), ("_loaded_at", "TIMESTAMPTZ")]

# entity -> table, key, fields {name: (type, required, description)}, sources, feeds (views / KPIs / pages)
CANONICAL: dict[str, dict] = {
    "supplier": {
        "table": "dim_supplier", "label": "Supplier", "key": ["supplier_id"],
        "fields": {"supplier_id": ("text", True, "Supplier number"), "supplier_name": ("text", True, "Legal / trading name"),
                   "country": ("text", True, "Country"), "material_category": ("text", False, "Main commodity"),
                   "risk_tier": ("text", False, "HIGH / MEDIUM / LOW"), "quality_certification": ("text", False, "e.g. IATF 16949"),
                   "standard_lead_time_days": ("integer", False, "Standard lead time (days)")},
        "sources": {"SAP": "LFA1-LIFNR, NAME1, LAND1; LFM1", "Oracle": "AP_SUPPLIERS / POZ_SUPPLIERS"},
        "feeds": ["All scorecards", "Supplier drawer", "Network Tracker nodes"],
    },
    "material": {
        "table": "dim_material", "label": "Material", "key": ["material_id"],
        "fields": {"material_id": ("text", True, "Material number"), "material_name": ("text", True, "Description"),
                   "material_category": ("text", True, "Commodity / group"), "standard_price_inr": ("numeric", True, "Standard price (INR)"),
                   "lead_time_days": ("integer", False, "Planned delivery time (days)")},
        "sources": {"SAP": "MARA-MATNR, MAKT-MAKTX, MARC-PLIFZ, MBEW-STPRS", "Oracle": "MTL_SYSTEM_ITEMS_B"},
        "feeds": ["Price variance", "Lead-time variance", "Plant twin"],
    },
    "plant": {"table": "dim_plant", "label": "Plant", "key": ["plant_id"],
              "fields": {"plant_id": ("text", True, "Plant code"), "plant_name": ("text", True, "Name")},
              "sources": {"SAP": "T001W-WERKS, NAME1"}, "feeds": ["Filters", "Plant twin"]},
    "po_line": {
        "table": "fact_purchase_order_receipt", "label": "Purchase-order line", "key": ["po_id", "po_line"],
        "fields": {"po_id": ("text", True, "PO number"), "po_line": ("integer", True, "Item"), "supplier_id": ("text", True, "Supplier"),
                   "material_id": ("text", True, "Material"), "plant_id": ("text", True, "Plant"), "po_date": ("date", True, "PO date"),
                   "promised_date": ("date", True, "Promised / confirmed delivery date"), "receipt_date": ("date", False, "Last goods receipt (derived from GRNs)"),
                   "ordered_qty": ("numeric", True, "Ordered quantity"), "received_qty": ("numeric", False, "Received quantity (sum of GRNs)"),
                   "unit_price_inr": ("numeric", True, "Net price per unit (INR)"), "currency": ("text", False, "Currency"),
                   "po_status": ("text", False, "Status"), "incoterm": ("text", False, "Incoterm")},
        "sources": {"SAP": "EKKO (BEDAT, LIFNR), EKPO (EBELN, EBELP, MATNR, WERKS, MENGE, NETPR), EKET-EINDT", "Oracle": "PO_HEADERS_ALL / PO_LINES_ALL / PO_LINE_LOCATIONS_ALL"},
        "feeds": ["OTIF", "On-time", "In-full", "Lead-time variance", "Price variance", "Network Tracker shipments"],
    },
    "schedule_line": {
        "table": "fact_schedule_line", "label": "Schedule-agreement line", "key": ["agreement_id", "schedule_line"],
        "fields": {"agreement_id": ("text", True, "Agreement"), "schedule_line": ("integer", True, "Line"), "supplier_id": ("text", True, "Supplier"),
                   "material_id": ("text", True, "Material"), "plant_id": ("text", True, "Plant"), "delivery_date": ("date", True, "Scheduled date"),
                   "scheduled_qty": ("numeric", True, "Scheduled quantity"), "received_qty": ("numeric", False, "Received"), "receipt_date": ("date", False, "Receipt date")},
        "sources": {"SAP": "EKET (EBELN, EBELP, ETENR, EINDT, MENGE, WEMNG)"}, "feeds": ["Schedule-line OTIF (v_schedule_delivery)"],
    },
    "grn": {
        "table": "fact_grn", "label": "Goods receipt", "key": ["grn_id"],
        "fields": {"grn_id": ("text", True, "Material document + item"), "po_id": ("text", True, "PO"), "po_line": ("integer", True, "PO item"),
                   "receipt_date": ("date", True, "Posting date"), "received_qty": ("numeric", True, "Quantity"), "accepted_qty": ("numeric", False, "Accepted"),
                   "rejected_qty": ("numeric", False, "Rejected"), "plant_id": ("text", False, "Plant")},
        "sources": {"SAP": "EKBE (VGABE=1) / MSEG (BWART 101/102)", "Oracle": "RCV_TRANSACTIONS"}, "feeds": ["Split receipts in OTIF", "Source-record trace"],
    },
    "incoming_inspection": {
        "table": "fact_incoming_inspection", "label": "Incoming inspection", "key": ["inspection_lot_id"],
        "fields": {"inspection_lot_id": ("text", True, "Inspection lot"), "grn_id": ("text", False, "GRN"), "po_id": ("text", False, "PO"),
                   "po_line": ("integer", False, "PO item"), "supplier_id": ("text", True, "Supplier"), "material_id": ("text", True, "Material"),
                   "plant_id": ("text", True, "Plant"), "inspection_date": ("date", True, "Date"), "inspected_qty": ("numeric", True, "Inspected"),
                   "accepted_qty": ("numeric", False, "Accepted"), "rejected_qty": ("numeric", True, "Rejected"), "defect_code": ("text", False, "Defect"),
                   "usage_decision": ("text", False, "Usage decision")},
        "sources": {"SAP": "QALS (PRUEFLOS, LIFNR, MATNR), QAVE (VCODE)"}, "feeds": ["Incoming PPM (preferred source)"],
    },
    "production_inspection": {
        "table": "fact_quality_inspection", "label": "Production inspection", "key": ["inspection_id"],
        "fields": {"inspection_id": ("text", True, "Inspection"), "inspection_ts": ("timestamp", True, "Time"), "plant_id": ("text", True, "Plant"),
                   "line_id": ("text", True, "Line"), "production_order_id": ("text", True, "Production order"), "defect_code": ("text", False, "Defect"),
                   "result": ("text", True, "PASS / FAIL"), "severity": ("text", False, "Severity")},
        "sources": {"MES": "inspection results", "QMS": "test records"}, "feeds": ["Attributed defects / PPM", "Repeat-defect alerts"],
    },
    "invoice": {
        "table": "fact_invoice", "label": "Supplier invoice", "key": ["invoice_id"],
        "fields": {"invoice_id": ("text", True, "Invoice"), "po_id": ("text", True, "PO"), "po_line": ("integer", True, "PO item"),
                   "supplier_id": ("text", True, "Supplier"), "invoice_date": ("date", True, "Date"), "invoiced_qty": ("numeric", True, "Quantity"),
                   "invoice_unit_price_inr": ("numeric", True, "Unit price"), "currency": ("text", False, "Currency")},
        "sources": {"SAP": "RBKP / RSEG", "Oracle": "AP_INVOICES_ALL / AP_INVOICE_LINES_ALL"}, "feeds": ["Three-way match (v_invoice_match)"],
    },
    "supplier_issue": {
        "table": "fact_ncr_capa", "label": "Supplier issue (NCR / CAPA)", "key": ["ncr_id"],
        "fields": {"ncr_id": ("text", True, "NCR"), "inspection_id": ("text", True, "Inspection"), "opened_ts": ("timestamp", True, "Opened"),
                   "defect_code": ("text", True, "Defect"), "root_cause_category": ("text", False, "Root cause"), "capa_due_date": ("date", True, "CAPA due"),
                   "capa_status": ("text", True, "OPEN / IN_PROGRESS / CLOSED / VERIFIED")},
        "sources": {"SAP": "QMEL / QMFE", "QMS": "ETQ, MasterControl NCR"}, "feeds": ["Open NCR", "Overdue CAPA", "Response time", "Alerts"],
    },
    "shipment_milestone": {
        "table": "fact_inbound_milestone", "label": "Shipment milestone", "key": ["shipment_ref", "milestone", "milestone_ts"],
        "fields": {"shipment_ref": ("text", True, "Shipment / ASN"), "po_id": ("text", True, "PO"), "po_line": ("integer", True, "PO item"),
                   "milestone": ("text", True, "DEPARTED_SUPPLIER / VESSEL_DEPARTED / ... / DELIVERED"), "milestone_ts": ("timestamp", True, "Time"),
                   "location": ("text", False, "Location"), "lat": ("numeric", False, "Latitude"), "lon": ("numeric", False, "Longitude"),
                   "source": ("text", False, "ERP / TMS / GPS")},
        "sources": {"EDI": "856 ASN", "Visibility": "project44, FourKites", "SAP": "VTTK/VTTS"}, "feeds": ["Network Tracker positions (Milestone)"],
    },
    "production_order": {
        "table": "fact_production_order", "label": "Production order", "key": ["production_order_id"],
        "fields": {"production_order_id": ("text", True, "Order"), "plant_id": ("text", True, "Plant"), "product_id": ("text", True, "Product"),
                   "customer_order_id": ("text", False, "Customer order"), "order_qty": ("numeric", True, "Quantity"), "due_date": ("date", True, "Due"),
                   "order_status": ("text", True, "Status")},
        "sources": {"SAP": "AUFK / AFKO / AFPO", "MES": "work orders"}, "feeds": ["Exposure", "Plant twin line exposure"],
    },
    "production_line": {"table": "dim_production_line", "label": "Production line", "key": ["line_id"],
                        "fields": {"line_id": ("text", True, "Line"), "plant_id": ("text", True, "Plant"), "line_type": ("text", True, "Type"),
                                   "nominal_capacity_per_hour": ("numeric", False, "Capacity / h")},
                        "sources": {"SAP": "CRHD (work centres)", "MES": "line master"}, "feeds": ["Plant twin", "Disruption hours"]},
    "inventory_snapshot": {
        "table": "fact_inventory_daily_snapshot", "label": "Inventory snapshot", "key": ["snapshot_date", "plant_id", "material_id", "warehouse_id", "lot_id"],
        "fields": {"snapshot_date": ("date", True, "Date"), "plant_id": ("text", True, "Plant"), "material_id": ("text", True, "Material"),
                   "warehouse_id": ("text", True, "Storage location"), "lot_id": ("text", True, "Batch"), "on_hand_qty": ("numeric", True, "On hand"),
                   "quality_hold_qty": ("numeric", False, "Quality hold"), "safety_stock_qty": ("numeric", False, "Safety stock")},
        "sources": {"SAP": "MARD / MCHB (LABST, INSME, SPEME)", "WMS": "stock by location"}, "feeds": ["Plant twin stock cover", "Risk Radar what-if"],
    },
    "downtime": {"table": "fact_downtime_event", "label": "Downtime event", "key": ["downtime_id"],
                 "fields": {"downtime_id": ("text", True, "Event"), "plant_id": ("text", True, "Plant"), "line_id": ("text", True, "Line"),
                            "start_ts": ("timestamp", True, "Start"), "duration_min": ("numeric", True, "Minutes"), "reason_code": ("text", True, "NO_MATERIAL / ...")},
                 "sources": {"MES": "machine states / downtime log", "Historian": "OSIsoft PI events"}, "feeds": ["Disruption hours", "Plant twin"]},
}


def canonical_tables() -> list[str]:
    return [e["table"] for e in CANONICAL.values()]


def ensure_lineage(conn_or_cur, table: str, schema: str = "sp", source_system: str | None = None) -> None:
    """Add the lineage columns (idempotent). With source_system, stamp rows that have none (e.g. after a CSV load)."""
    exe = conn_or_cur.execute
    for col, typ in LINEAGE_COLS:
        exe(f'ALTER TABLE IF EXISTS {schema}."{table}" ADD COLUMN IF NOT EXISTS "{col}" {typ}')
    if source_system:
        exe(f'UPDATE {schema}."{table}" SET "_source_system" = %s, "_loaded_at" = now() WHERE "_source_system" IS NULL', (source_system,))
