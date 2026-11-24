"""Rolling demo data: keep the dataset current so the app can always work "as of today".

The real extracts stop at a fixed date. For a demo that should look live, this fills every missing day up to today by copying
the records of the same weekday four weeks earlier (28 days) and moving all their dates forward by 28 days:

  * every copied row gets a NEW id, and references between tables follow (purchase order -> goods receipt, incoming inspection,
    invoice, delay events, milestones; customer order -> production order; quality inspection -> NCR);
  * every copied row is labelled _source_system = 'ROLLING:<original source>' with _loaded_at = now(), so it is always told apart
    from real data (Settings > Data health counts it as demo) and can be removed with  python -m scripts.roll_forward --clear ;
  * receipts / deliveries copied into the future simply count as "not received yet" until their date arrives, because every view
    judges the data as of sp.as_of_date() (today);
  * idempotent and safe with two schedulers (local + hosted) on one database: a transaction-scoped advisory lock plus the
    sp.meta key 'rolled_through' mean each day is filled exactly once.

Behind the feature flag rolling_demo_data. Switch it OFF as soon as real feeds are connected: copied days are not real records.
"""
from __future__ import annotations

import datetime as dt

from sqlalchemy import text

SHIFT = dt.timedelta(days=28)            # four weeks: keeps the weekday pattern (shifts, weekends) intact
LOCK_KEY = 724_411                        # pg_advisory_xact_lock key for this job
SKIP_DATES = {"_loaded_at", "created_at"}


def _cols(c, table: str) -> list[tuple[str, str]]:
    return [(r[0], r[1]) for r in c.execute(text(
        "SELECT column_name, data_type FROM information_schema.columns WHERE table_schema='sp' AND table_name=:t ORDER BY ordinal_position"), {"t": table})]


def _clone(c, table: str, where: str, params: dict, *, ids: dict | None = None, remap: dict | None = None,
           drop: tuple = (), label: bool = True) -> list[dict]:
    """Copy the rows matching `where`, dates +28 days, new ids (ids = {col: fn(row, k) -> new id}), references remapped
    (remap = {col: {old: new}}). Returns the inserted rows with an '__old' map of their original ids."""
    cols = _cols(c, table)
    names = [n for n, _ in cols]
    dates = [n for n, t in cols if (t == "date" or t.startswith("timestamp")) and n not in SKIP_DATES]
    rows = [dict(r) for r in c.execute(text(f"SELECT * FROM sp.{table} WHERE {where}"), params).mappings()]
    out = []
    for k, r in enumerate(rows):
        n = dict(r)
        old = {col: r[col] for col in (ids or {})}
        for col in dates:
            if n.get(col) is not None:
                n[col] = n[col] + SHIFT
        for col, fn in (ids or {}).items():
            n[col] = fn(r, k)
        for col, m in (remap or {}).items():
            if n.get(col) in m:
                n[col] = m[n[col]]
        if label and "_source_system" in names:
            n["_source_system"] = f"ROLLING:{r.get('_source_system') or table}"
            for col in ("_run_id", "_connection_id", "_source_record_id"):
                if col in n:
                    n[col] = None
        if "_loaded_at" in names:
            n["_loaded_at"] = None              # set to now() in the INSERT
        if "created_at" in names:
            n["created_at"] = None
        for col in drop:
            n.pop(col, None)
        n["__old"] = old
        out.append(n)
    if out:
        keep = [x for x in names if x not in drop]
        vals = ", ".join("now()" if x in ("_loaded_at", "created_at") else f":{x}" for x in keep)
        c.execute(text(f"INSERT INTO sp.{table} ({', '.join(keep)}) VALUES ({vals})"),
                  [{x: r[x] for x in keep if x not in ("_loaded_at", "created_at")} for r in out])
    return out


def _fill_day(c, day: dt.date, maps: dict) -> dict:
    """Create `day` from day - 28. Returns rows added per table."""
    src, tag, added = day - SHIFT, day.strftime("%y%m%d"), {}
    p = {"d": src}

    co = _clone(c, "fact_customer_order", "order_date = :d", p, ids={"customer_order_id": lambda r, k: f"CO-R{tag}-{k:03d}"})
    maps["co"].update({r["__old"]["customer_order_id"]: r["customer_order_id"] for r in co})
    added["fact_customer_order"] = len(co)
    added["fact_production_order"] = len(_clone(c, "fact_production_order", "planned_start::date = :d", p,
                                                ids={"production_order_id": lambda r, k: f"PO-R{tag}-{k:03d}"},
                                                remap={"customer_order_id": maps["co"]}))

    po = _clone(c, "fact_purchase_order_receipt", "po_date = :d", p, ids={"po_id": lambda r, k: f"POC-R{tag}-{k:03d}"})
    pmap = {r["__old"]["po_id"]: r["po_id"] for r in po}
    added["fact_purchase_order_receipt"] = len(po)
    if pmap:
        q = {"ids": list(pmap)}
        grn = _clone(c, "fact_grn", "po_id = ANY(:ids)", q, ids={"grn_id": lambda r, k: f"{r['grn_id']}-R{tag}"}, remap={"po_id": pmap})
        gmap = {r["__old"]["grn_id"]: r["grn_id"] for r in grn}
        added["fact_grn"] = len(grn)
        added["fact_incoming_inspection"] = len(_clone(c, "fact_incoming_inspection", "po_id = ANY(:ids)", q,
                                                       ids={"inspection_lot_id": lambda r, k: f"{r['inspection_lot_id']}-R{tag}"},
                                                       remap={"po_id": pmap, "grn_id": gmap}))
        added["fact_invoice"] = len(_clone(c, "fact_invoice", "po_id = ANY(:ids)", q,
                                           ids={"invoice_id": lambda r, k: f"{r['invoice_id']}-R{tag}"}, remap={"po_id": pmap}))
        added["fact_delay_event"] = len(_clone(c, "fact_delay_event", "po_id = ANY(:ids)", q, remap={"po_id": pmap}, drop=("id",)))
        added["fact_inbound_milestone"] = len(_clone(c, "fact_inbound_milestone", "po_id = ANY(:ids)", q,
                                                     ids={"shipment_ref": lambda r, k: f"{r['shipment_ref']}-R{tag}"}, remap={"po_id": pmap}))

    added["fact_schedule_line"] = len(_clone(c, "fact_schedule_line", "delivery_date = :d", p,
                                             ids={"agreement_id": lambda r, k: f"{r['agreement_id']}-R{tag}"}))
    # quality inspections keep their production order and serial number, so defects still trace to supplier lots (genealogy)
    qi = _clone(c, "fact_quality_inspection", "inspection_ts::date = :d", p, ids={"inspection_id": lambda r, k: f"QI-R{tag}-{k:05d}"})
    maps["qi"].update({r["__old"]["inspection_id"]: r["inspection_id"] for r in qi})
    added["fact_quality_inspection"] = len(qi)
    added["fact_ncr_capa"] = len(_clone(c, "fact_ncr_capa", "opened_ts::date = :d", p, ids={"ncr_id": lambda r, k: f"NCR-R{tag}-{k:03d}"},
                                        remap={"inspection_id": maps["qi"]}))
    added["fact_downtime_event"] = len(_clone(c, "fact_downtime_event", "start_ts::date = :d", p,
                                              ids={"downtime_id": lambda r, k: f"DT-R{tag}-{k:03d}"}))
    added["fact_inventory_daily_snapshot"] = len(_clone(c, "fact_inventory_daily_snapshot", "snapshot_date = :d", p))
    added["fact_rfq_quote"] = len(_clone(c, "fact_rfq_quote", "request_date = :d", p, ids={"rfq_id": lambda r, k: f"RFQ-R{tag}-{k:02d}"}))
    return added


def _extend_calendar(c, until: dt.date) -> int:
    """The shift calendar drives working-day lead times; keep it 60 days ahead of today (same 28-day copy)."""
    last = c.execute(text("SELECT MAX(calendar_date) FROM sp.dim_shift_calendar")).scalar()
    n = 0
    while last and last < until:
        last += dt.timedelta(days=1)
        n += len(_clone(c, "dim_shift_calendar", "calendar_date = :d", {"d": last - SHIFT}, label=False))
    return n


def status(c) -> dict:
    through = c.execute(text("SELECT value FROM sp.meta WHERE key = 'rolled_through'")).scalar()
    rows = c.execute(text("SELECT count(*) FROM sp.fact_purchase_order_receipt WHERE _source_system LIKE 'ROLLING%'")).scalar()
    return {"rolled_through": through, "rolling_po_lines": rows}


def run(until: dt.date | None = None, dry_run: bool = False) -> dict:
    from .db import engine
    with engine.begin() as c:
        c.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": LOCK_KEY})
        today = until or c.execute(text("SELECT CURRENT_DATE")).scalar()
        through = c.execute(text("SELECT value FROM sp.meta WHERE key = 'rolled_through'")).scalar()
        if through:
            through = dt.date.fromisoformat(through)
        else:                                   # first run: the real data ends at its latest inspection / NCR date
            through = c.execute(text("""SELECT GREATEST((SELECT MAX(inspection_ts)::date FROM sp.fact_quality_inspection),
                                                        (SELECT MAX(opened_ts)::date FROM sp.fact_ncr_capa))""")).scalar()
        days = [through + dt.timedelta(days=i) for i in range(1, (today - through).days + 1)]
        if dry_run or not days:
            return {"from": str(through), "to": str(today), "days": len(days), "dry_run": dry_run}
        maps, totals = {"co": {}, "qi": {}}, {}
        for d in days:
            for t, n in _fill_day(c, d, maps).items():
                totals[t] = totals.get(t, 0) + n
        cal = _extend_calendar(c, today + dt.timedelta(days=60))
        c.execute(text("""INSERT INTO sp.meta (key, value) VALUES ('rolled_through', :v)
                          ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value"""), {"v": today.isoformat()})
    _clear_caches()
    return {"filled": f"{days[0]} .. {days[-1]}", "days": len(days), "rows": totals, "calendar_rows": cal}


def clear() -> dict:
    """Remove every ROLLING row (the real data is untouched) and forget the marker."""
    from .db import engine
    out = {}
    with engine.begin() as c:
        c.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": LOCK_KEY})
        for t in ("fact_customer_order", "fact_production_order", "fact_purchase_order_receipt", "fact_grn", "fact_incoming_inspection",
                  "fact_invoice", "fact_inbound_milestone", "fact_schedule_line", "fact_quality_inspection", "fact_ncr_capa",
                  "fact_downtime_event", "fact_inventory_daily_snapshot", "fact_rfq_quote"):
            out[t] = c.execute(text(f"DELETE FROM sp.{t} WHERE _source_system LIKE 'ROLLING%'")).rowcount
        out["fact_delay_event"] = c.execute(text("DELETE FROM sp.fact_delay_event WHERE po_id LIKE 'POC-R%'")).rowcount
        c.execute(text("DELETE FROM sp.meta WHERE key = 'rolled_through'"))
    _clear_caches()
    return out


def _clear_caches() -> None:
    import sys
    try:
        from . import analytics as A
        A.clear_cache()
    except Exception:  # noqa: BLE001
        pass
    m = sys.modules.get("app.main")
    if m is not None and hasattr(m, "_RESP"):
        m._RESP.clear()
