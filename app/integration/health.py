"""Source health, failed / late-load alerts, retention, quarantine work-queue and end-to-end lineage (DI Phase 5).

Health per active mapping
  HEALTHY    last run succeeded and data is fresh
  STALE      no successful load within the expected window (max_staleness_hours, else from the schedule: every N min -> 3 x N,
             hourly -> 3 h, daily -> 26 h; manual mappings are never "stale")
  FAILING    the latest run failed
  NEVER_RUN  no run yet
Alerts: an ADMIN notification (in-app + e-mail / Teams when configured) when a mapping BECOMES failing or stale, and when it
recovers - once per state change, not every tick (integration.health_state).
Retention: uploaded file content after `file_retention_days` (default 90); closed quarantine rows after
`quarantine_retention_days` (default 180). Both are settings an ADMIN can change; every purge is audited.
Lineage: KPI row -> lineage stamp -> run -> mapping (field map + column policy) -> connection -> source record id.
"""
from __future__ import annotations

import datetime as dt


from sqlalchemy import text

from . import mapping as MP
from .canonical import CANONICAL, LINEAGE_COLS
from .dbx import engine

DEFAULTS = {"file_retention_days": 90, "quarantine_retention_days": 180}
KPI_VIEWS = {"po_line": ("v_po_delivery", ["po_id", "po_line"]), "production_inspection": ("v_defect_attribution", ["inspection_id"])}


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def expected_hours(m: dict) -> float | None:
    if m.get("max_staleness_hours"):
        return float(m["max_staleness_hours"])
    kind, arg = MP.parse_schedule(m.get("schedule") or "manual")
    if kind == "every":
        return 3 * int(arg) / 60
    if kind == "daily":
        return 26.0
    return None


# ------------------------------------------------------------------ health
def health() -> dict:
    with engine.connect() as c:
        maps = [dict(r._mapping) for r in c.execute(text("""SELECT m.*, c.name AS connection_name, c.connector_key, c.environment, c.status AS connection_status,
                                                            c.last_test_ok FROM integration.mappings m LEFT JOIN integration.connections c ON c.id = m.connection_id
                                                            WHERE m.active ORDER BY m.id"""))]
        runs = [dict(r._mapping) for r in c.execute(text("""SELECT * FROM (SELECT r.mapping_id, r.id, r.status, r.started_at, r.finished_at, r.dry_run, r.extracted,
                   r.inserted, r.updated, r.quarantined, r.dq_summary, row_number() OVER (PARTITION BY r.mapping_id ORDER BY r.started_at DESC) rn
                   FROM integration.runs r JOIN integration.mappings m ON m.id = r.mapping_id AND m.active) x WHERE rn <= 14"""))]
        qopen = {r[0]: r[1] for r in c.execute(text("""SELECT q.mapping_id, count(*) FROM integration.quarantine q JOIN integration.mappings m
                                                       ON m.id = q.mapping_id AND m.active WHERE q.status = 'OPEN' GROUP BY 1"""))}
        fails7 = {r[0]: r[1] for r in c.execute(text("""SELECT mapping_id, count(*) FROM integration.runs WHERE status = 'FAILED'
                                                        AND started_at > now() - interval '7 days' GROUP BY 1"""))}
        last_ok = {r[0]: r[1] for r in c.execute(text("""SELECT mapping_id, max(finished_at) FROM integration.runs WHERE status IN ('SUCCESS', 'PARTIAL')
                                                         GROUP BY 1"""))}
    by = {}
    for r in runs:
        by.setdefault(r["mapping_id"], []).append(r)
    out, now = [], _now()
    for m in maps:
        rs = by.get(m["id"], [])
        last = rs[0] if rs else None
        ok_at = last_ok.get(m["id"])
        fresh_h = round((now - ok_at).total_seconds() / 3600, 1) if ok_at else None
        exp = expected_hours(m)
        consec = 0
        for r in rs:
            if r["status"] != "FAILED":
                break
            consec += 1
        if not last:
            status, why = "NEVER_RUN", "No run yet."
        elif last["status"] == "FAILED":
            status, why = "FAILING", f"Latest run failed ({consec} in a row)."
        elif exp is not None and (fresh_h is None or fresh_h > exp):
            status, why = "STALE", f"No successful load for {fresh_h} h (expected within {exp:g} h)."
        else:
            status, why = "HEALTHY", "Latest run succeeded" + (f"; data {fresh_h} h old." if fresh_h is not None else ".")
        warn = sum(v.get("failed", 0) for v in (last.get("dq_summary") or {}).values() if v.get("severity") == "WARN") if last else 0
        out.append({"mapping_id": m["id"], "name": m["name"], "entity": m["entity"], "target": f"sp.{m['target_table']}", "mode": m["mode"],
                    "schedule": m["schedule"], "connection_id": m["connection_id"], "connection_name": m["connection_name"],
                    "connector_key": m["connector_key"], "environment": m["environment"], "connection_ok": m["last_test_ok"],
                    "status": status, "reason": why, "last_run": {k: last[k] for k in ("id", "status", "started_at", "dry_run")} if last else None,
                    "last_success_at": ok_at, "freshness_hours": fresh_h, "expected_hours": exp, "failures_7d": int(fails7.get(m["id"], 0)),
                    "consecutive_failures": consec, "open_quarantine": int(qopen.get(m["id"], 0)), "dq_warnings_last_run": warn,
                    "trend": [{"at": r["started_at"], "status": r["status"], "extracted": r["extracted"], "inserted": r["inserted"],
                               "updated": r["updated"], "quarantined": r["quarantined"]} for r in reversed(rs)]})
    counts = {s: sum(1 for x in out if x["status"] == s) for s in ("HEALTHY", "STALE", "FAILING", "NEVER_RUN")}
    return {"generated_at": now, "counts": counts, "mappings": out}


def check_and_notify() -> dict:
    """Alert ADMINs when a mapping becomes FAILING / STALE, and when it recovers - once per state change."""
    from ..db import Notification, SessionLocal
    from ..jobs import deliver
    h = health()["mappings"]
    sent = []
    with engine.begin() as c:
        prev = {r[0]: r[1] for r in c.execute(text("SELECT mapping_id, status FROM integration.health_state"))}
        for m in h:
            st, before = m["status"], prev.get(m["mapping_id"])
            if st == before:
                continue
            c.execute(text("""INSERT INTO integration.health_state (mapping_id, status, since) VALUES (:m, :s, now())
                              ON CONFLICT (mapping_id) DO UPDATE SET status = :s, since = now()"""), {"m": m["mapping_id"], "s": st})
            if st in ("FAILING", "STALE") or (before in ("FAILING", "STALE") and st == "HEALTHY"):
                sev = "HIGH" if st == "FAILING" else "MEDIUM" if st == "STALE" else "LOW"
                subj = (f"Data load {m['mapping_id']} is {st.lower()}" if st != "HEALTHY" else f"Data load {m['mapping_id']} recovered")
                body = f"{m['name']} -> {m['target']}: {m['reason']}"
                sent.append({"mapping_id": m["mapping_id"], "status": st, "subject": subj, "body": body, "severity": sev})
                c.execute(text("UPDATE integration.health_state SET notified_at = now() WHERE mapping_id = :m"), {"m": m["mapping_id"]})
    if sent:
        with SessionLocal() as s:
            for x in sent:
                s.add(Notification(recipient_role="ADMIN", severity=x["severity"], subject=x["subject"], body=x["body"]))
            s.commit()
        for x in sent:
            try:
                deliver(x["subject"], x["body"], "ADMIN")
            except Exception:  # noqa: BLE001
                pass
    return {"notified": [{k: x[k] for k in ("mapping_id", "status")} for x in sent]}


# ------------------------------------------------------------------ settings + retention
def get_settings() -> dict:
    with engine.connect() as c:
        rows = {r[0]: r[1] for r in c.execute(text("SELECT key, value FROM integration.settings WHERE key = ANY(:k)"), {"k": list(DEFAULTS)})}
    return {k: int(rows.get(k, v)) for k, v in DEFAULTS.items()}


def set_settings(d: dict, actor: str) -> dict:
    from . import service as S
    clean = {}
    for k, v in d.items():
        if k not in DEFAULTS or v is None:
            continue
        n = int(v)
        if not 7 <= n <= 3650:
            raise ValueError(f"{k} must be between 7 and 3650 days")
        clean[k] = n
    with engine.begin() as c:
        for k, v in clean.items():
            c.execute(text("INSERT INTO integration.settings (key, value) VALUES (:k, :v) ON CONFLICT (key) DO UPDATE SET value = :v"), {"k": k, "v": str(v)})
    S.audit(actor, "settings:update", None, clean)
    return get_settings()


def housekeeping(actor: str = "system") -> dict:
    from . import service as S
    st = get_settings()
    with engine.begin() as c:
        files = c.execute(text("""UPDATE integration.files SET active = FALSE, content_enc = NULL
                                  WHERE active AND uploaded_at < now() - make_interval(days => :d)"""), {"d": st["file_retention_days"]}).rowcount
        q = c.execute(text("""DELETE FROM integration.quarantine WHERE status <> 'OPEN' AND COALESCE(resolved_at, created_at) < now() - make_interval(days => :d)"""),
                      {"d": st["quarantine_retention_days"]}).rowcount
    if files or q:
        S.audit(actor, "retention:purge", None, {"files_purged": files, "quarantine_deleted": q, **st})
    return {"files_purged": files, "quarantine_deleted": q}


def job() -> dict:
    """Scheduled every 15 minutes by the integration ticker (logged in job_runs)."""
    return {**check_and_notify(), **housekeeping()}


# ------------------------------------------------------------------ quarantine work-queue
def _reason(errs: list) -> str:
    e = (errs or ["?"])[0]
    return e.split(":")[0].strip() if ":" in e else e[:60]


def queue(mapping_id: str | None = None, status: str = "OPEN", reason: str | None = None, limit: int = 300) -> dict:
    w, p = ["m.active"], {"n": limit}
    if mapping_id:
        w.append("q.mapping_id = :m")
        p["m"] = mapping_id
    if status:
        w.append("q.status = :s")
        p["s"] = status
    with engine.connect() as c:
        rows = [dict(r._mapping) for r in c.execute(text(f"""SELECT q.*, m.name AS mapping_name, m.entity FROM integration.quarantine q
                                                             JOIN integration.mappings m ON m.id = q.mapping_id WHERE {' AND '.join(w)}
                                                             ORDER BY q.id DESC LIMIT 5000"""), p)]
    groups: dict = {}
    for r in rows:
        r["reason"] = _reason(r["errors"])
        g = groups.setdefault((r["mapping_id"], r["reason"]), {"mapping_id": r["mapping_id"], "mapping_name": r["mapping_name"], "reason": r["reason"],
                                                                "count": 0, "example": (r["errors"] or [""])[0], "latest": r["created_at"]})
        g["count"] += 1
    if reason:
        rows = [r for r in rows if r["reason"] == reason]
    return {"groups": sorted(groups.values(), key=lambda g: -g["count"]), "rows": rows[:limit], "total": sum(g["count"] for g in groups.values())}


def bulk_resolve(action: str, actor: str, ids: list[int] | None = None, mapping_id: str | None = None, reason: str | None = None) -> dict:
    from . import service as S
    action = (action or "").upper()
    if action not in ("IGNORED", "RESOLVED"):
        raise ValueError("action must be IGNORED or RESOLVED")
    if not ids and not (mapping_id and reason):
        raise ValueError("give record ids, or a mapping and a reason")
    if not ids:
        ids = [r["id"] for r in queue(mapping_id, "OPEN", reason, 5000)["rows"]]
    with engine.begin() as c:
        n = c.execute(text("""UPDATE integration.quarantine SET status = :s, resolved_by = :a, resolved_at = now()
                              WHERE id = ANY(:ids) AND status = 'OPEN'"""), {"s": action, "a": actor, "ids": list(ids)}).rowcount
    S.audit(actor, f"quarantine:bulk_{action.lower()}", None, {"count": n, "mapping": mapping_id, "reason": reason})
    return {"updated": n}


# ------------------------------------------------------------------ lineage
def search_keys(entity: str, q: str, limit: int = 20) -> list[dict]:
    e = CANONICAL.get(entity)
    if not e:
        raise ValueError(f"unknown entity '{entity}'")
    k0 = e["key"][0]
    cols = ", ".join(f'"{k}"::text AS "{k}"' for k in e["key"])
    with engine.connect() as c:
        return [dict(r._mapping) for r in c.execute(text(f"""SELECT {cols}, "_source_system", "_connection_id" FROM sp."{e['table']}"
                                                             WHERE "{k0}"::text ILIKE :q ORDER BY 1 LIMIT :n"""), {"q": f"%{q.strip()}%", "n": limit})]


def lineage(entity: str, key: dict) -> dict:
    e = CANONICAL.get(entity)
    if not e:
        raise ValueError(f"unknown entity '{entity}'")
    missing = [k for k in e["key"] if key.get(k) in (None, "")]
    if missing:
        raise ValueError(f"key field(s) required: {', '.join(missing)}")
    cond = " AND ".join(f'"{k}"::text = :{k}' for k in e["key"])
    with engine.connect() as c:
        row = c.execute(text(f'SELECT * FROM sp."{e["table"]}" WHERE {cond} LIMIT 1'), {k: str(key[k]) for k in e["key"]}).fetchone()
        if not row:
            raise LookupError(f"no {e['label'].lower()} with that key")
        row = {k: (v.isoformat() if isinstance(v, (dt.date, dt.datetime)) else v) for k, v in dict(row._mapping).items()}
        lin = {c_: row.pop(c_, None) for c_, _ in LINEAGE_COLS}
        run = mapping = conn = None
        if lin["_run_id"]:
            r = c.execute(text("SELECT * FROM integration.runs WHERE id = :i"), {"i": lin["_run_id"]}).fetchone()
            run = dict(r._mapping) if r else None
            if run:
                mr = c.execute(text("""SELECT id, name, entity, target_table, source_kind, source_object, field_map, column_policy, record_id_columns,
                                       watermark_column, mode, approval_status, requested_by, approved_by, version, active FROM integration.mappings WHERE id = :i"""),
                               {"i": run["mapping_id"]}).fetchone()
                mapping = dict(mr._mapping) if mr else None
        if lin["_connection_id"]:
            cr = c.execute(text("""SELECT id, name, connector_key, environment, approval_status, approved_by, active FROM integration.connections WHERE id = :i"""),
                           {"i": lin["_connection_id"]}).fetchone()
            conn = dict(cr._mapping) if cr else None
        kpi = None
        if entity in KPI_VIEWS:
            v, ks = KPI_VIEWS[entity]
            kc = " AND ".join(f'"{k}"::text = :{k}' for k in ks if k in key)
            kr = c.execute(text(f'SELECT * FROM sp."{v}" WHERE {kc} LIMIT 1'), {k: str(key[k]) for k in ks if k in key}).fetchone()
            if kr:
                kpi = {"view": f"sp.{v}", "row": {k: (x.isoformat() if isinstance(x, (dt.date, dt.datetime)) else x) for k, x in dict(kr._mapping).items()}}
    origin = ("connector" if lin["_connection_id"] else "csv" if str(lin["_source_system"] or "").startswith("CSV:") else "unknown")
    # sensitive values in the KPI row are what the KPI layer already shows internally; the source mapping shows field -> column only
    fm = {f: {"source": s.get("source"), "transform": s.get("transform"), "policy": (mapping or {}).get("column_policy", {}).get(s.get("source"))}
          for f, s in ((mapping or {}).get("field_map") or {}).items()}
    return {"entity": entity, "label": e["label"], "table": f"sp.{e['table']}", "key": {k: key[k] for k in e["key"]}, "row": row, "lineage": lin,
            "origin": origin, "feeds": e["feeds"], "kpi": kpi,
            "run": {k: run[k] for k in ("id", "status", "trigger", "actor", "started_at", "finished_at", "dry_run", "watermark_from", "watermark_to",
                                        "mapping_version")} if run else None,
            "mapping": {**{k: mapping[k] for k in ("id", "name", "source_kind", "source_object", "mode", "approval_status", "requested_by",
                                                  "approved_by", "version", "active", "record_id_columns")}, "fields": fm} if mapping else None,
            "connection": conn}


JOB_NAME = "integration_health"


def connection_details(cid: str) -> dict:
    """Everything one connection feeds: its mappings (source object -> canonical entity -> target table), run totals,
    rows currently in each target that came from this connection (lineage stamp _connection_id), health and recent runs."""
    from . import service as DS
    conn = DS.get_connection(cid)                                   # raises LookupError when unknown
    hl = {h["mapping_id"]: h for h in health().get("mappings", []) if h.get("connection_id") == cid}
    with engine.connect() as c:
        maps = [dict(r._mapping) for r in c.execute(text("""
            SELECT m.id, m.name, m.entity, m.target_table, m.source_kind, m.source_object, m.source_query, m.mode, m.schedule, m.enabled,
                   m.approval_status, m.last_run_at, m.last_run_status, m.watermark_column, m.last_watermark,
                   jsonb_array_length(CASE WHEN jsonb_typeof(m.field_map) = 'array' THEN m.field_map ELSE '[]'::jsonb END) AS mapped_fields,
                   (SELECT count(*) FROM jsonb_object_keys(CASE WHEN jsonb_typeof(m.field_map) = 'object' THEN m.field_map ELSE '{}'::jsonb END)) AS mapped_keys,
                   COALESCE(r.runs, 0) AS runs, COALESCE(r.extracted, 0) AS extracted, COALESCE(r.inserted, 0) AS inserted,
                   COALESCE(r.updated, 0) AS updated, COALESCE(r.quarantined, 0) AS quarantined, r.first_run, r.failed,
                   COALESCE(p.previews, 0) AS previews, COALESCE(p.previewed, 0) AS previewed
            FROM integration.mappings m
            LEFT JOIN (SELECT mapping_id, count(*) runs, sum(extracted) extracted, sum(inserted) inserted, sum(updated) updated,
                              sum(quarantined) quarantined, min(started_at) first_run, count(*) FILTER (WHERE status = 'FAILED') failed
                       FROM integration.runs WHERE NOT dry_run GROUP BY mapping_id) r ON r.mapping_id = m.id
            LEFT JOIN (SELECT mapping_id, count(*) previews, sum(extracted) previewed FROM integration.runs WHERE dry_run GROUP BY mapping_id) p
                   ON p.mapping_id = m.id
            WHERE m.connection_id = :c AND m.active ORDER BY m.id"""), {"c": cid})]
        loaded, origin = {}, {}
        for t in {m["target_table"] for m in maps if m.get("target_table")}:
            schema, _, name = t.rpartition(".")
            try:
                rows = c.execute(text(f'SELECT _connection_id, _source_system, count(*) FROM "{schema or "sp"}"."{name}" GROUP BY 1, 2')).fetchall()
                loaded[t] = sum(n for conn_id, _s, n in rows if conn_id == cid)
                origin[t] = [{"connection_id": conn_id, "source": src or ("this connection" if conn_id == cid else "initial CSV extract"), "rows": n}
                             for conn_id, src, n in sorted(rows, key=lambda r: -r[2])]
            except Exception:
                c.rollback()
                loaded[t], origin[t] = None, []
        recent = [dict(r._mapping) for r in c.execute(text("""
            SELECT r.id, r.mapping_id, r.status, r.dry_run, r.trigger, r.started_at, r.finished_at, r.extracted, r.inserted, r.updated, r.quarantined, r.message
            FROM integration.runs r JOIN integration.mappings m ON m.id = r.mapping_id AND m.active
            WHERE r.connection_id = :c ORDER BY r.started_at DESC LIMIT 8"""), {"c": cid})]
        retired_runs = c.execute(text("""SELECT count(*) FROM integration.runs r JOIN integration.mappings m ON m.id = r.mapping_id AND NOT m.active
                                         WHERE r.connection_id = :c"""), {"c": cid}).scalar()
        audit = [dict(r._mapping) for r in c.execute(text("""
            SELECT ts AS at, actor, action FROM integration.audit_log WHERE connection_id = :c ORDER BY ts DESC LIMIT 6"""), {"c": cid})] \
            if c.execute(text("SELECT to_regclass('integration.audit_log')")).scalar() else []
    for m in maps:
        m["fields"] = m.pop("mapped_fields") or m.pop("mapped_keys", 0)
        m.pop("mapped_keys", None)
        m["rows_in_target"] = loaded.get(m["target_table"])
        m["target_origin"] = origin.get(m["target_table"], [])
        m["target_rows_total"] = sum(o["rows"] for o in m["target_origin"])
        m["source_tables"] = _tables_in(m.pop("source_query", None)) if m["source_kind"] == "query" else [m["source_object"]] if m["source_object"] else []
        h = hl.get(m["id"]) or {}
        m["health"], m["health_reason"] = h.get("status"), h.get("reason")
        m["target_table"] = m["target_table"] if "." in (m["target_table"] or "") else f"sp.{m['target_table']}"
    totals = {"datasets": len(maps), "source_tables": len({t for m in maps for t in m["source_tables"]}), "rows_in_targets": sum(v for v in loaded.values() if v),
              "previewed": sum(int(m["previewed"] or 0) for m in maps), "previews": sum(int(m["previews"] or 0) for m in maps),
              "extracted": sum(int(m["extracted"] or 0) for m in maps), "loaded": sum(int(m["inserted"] or 0) + int(m["updated"] or 0) for m in maps),
              "quarantined": sum(int(m["quarantined"] or 0) for m in maps), "runs": sum(int(m["runs"] or 0) for m in maps)}
    return {"connection": conn, "mappings": maps, "totals": totals, "recent_runs": recent, "retired_runs": retired_runs, "audit": audit}


def _tables_in(sql: str | None) -> list[str]:
    """Source tables a mapping's SELECT reads (parsed, never executed)."""
    if not sql:
        return []
    try:
        import sqlglot
        from sqlglot import exp
        return sorted({".".join(p for p in (t.db, t.name) if p) for t in sqlglot.parse_one(sql, read="postgres").find_all(exp.Table)})
    except Exception:
        return []


# tables the analytics / twin / tracker code reads directly (not through a view); from the SQL in app/analytics.py, twin.py, tracker.py
CODE_USES = {
    "fact_rfq_quote": ["Scorecard (commercial score)", "Sourcing comparison"], "fact_production_order": ["Line & customer exposure"],
    "fact_bom_component": ["Line & customer exposure"], "fact_customer_order": ["Customer exposure", "Digital twin"],
    "fact_inventory_daily_snapshot": ["Digital twin (stock cover)", "Plant twin"], "fact_digital_twin_scenario": ["Digital twin (factory link)"],
    "fact_quality_inspection": ["Quality acceptance %", "Defect attribution"], "fact_downtime_event": ["Disruption hours", "KPI pulse"],
    "dim_plant": ["Plant filter", "Network tracker"], "dim_product": ["Genealogy / review pack"], "dim_production_line": ["Digital twin"],
    "dim_shift_calendar": ["Working-day lead times"], "work_calendar": ["Working-day lead times"], "meta": ["As-of date"],
    "fact_inbound_milestone": ["Network tracker positions"], "dim_supplier": ["Every supplier page"], "dim_material": ["Every material page"],
}
BUILTIN_REF = {"defect_material_map", "line_material_category", "work_calendar", "meta"}


def primary_details() -> dict:
    """The app's own Neon PostgreSQL warehouse (schema sp): the built-in primary connection every KPI reads.
    Exact row counts in one round trip, which views use each table, and where each table's rows came from (lineage stamp)."""
    from urllib.parse import urlparse
    from ..config import settings
    u = urlparse(settings.database_url)
    with engine.connect() as c:
        tabs = [dict(r._mapping) for r in c.execute(text("""
            SELECT t.table_name, pg_total_relation_size(format('sp.%I', t.table_name)) AS bytes,
                   EXISTS (SELECT 1 FROM information_schema.columns k WHERE k.table_schema = 'sp' AND k.table_name = t.table_name
                           AND k.column_name = '_source_system') AS has_lineage
            FROM information_schema.tables t WHERE t.table_schema = 'sp' AND t.table_type = 'BASE TABLE' ORDER BY t.table_name"""))]
        parts = [f"SELECT '{t['table_name']}' AS t, count(*) AS n, "
                 + (f"min(_source_system) AS src" if t["has_lineage"] else "NULL::text AS src")
                 + f' FROM sp."{t["table_name"]}"' for t in tabs]
        counts = {r[0]: (r[1], r[2]) for r in c.execute(text(" UNION ALL ".join(parts)))} if parts else {}
        uses = {}
        for v, tname in c.execute(text("""SELECT view_name, table_name FROM information_schema.view_table_usage
                                          WHERE view_schema = 'sp' AND table_schema = 'sp'""")):
            uses.setdefault(tname, set()).add(v)
        views = [r[0] for r in c.execute(text("SELECT table_name FROM information_schema.views WHERE table_schema = 'sp' ORDER BY 1"))]
        ver = c.execute(text("SHOW server_version")).scalar()
        size = c.execute(text("SELECT pg_database_size(current_database())")).scalar()
        try:                                         # client side: the Neon pooler hides TLS from pg_stat_ssl
            tls = bool(c.connection.driver_connection.pgconn.ssl_in_use)
        except Exception:
            tls = None
    import os
    csvs = set(os.listdir(settings.data_dir)) if os.path.isdir(settings.data_dir) else set()
    out = []
    for t in tabs:
        n, src = counts.get(t["table_name"], (None, None))
        if not src:
            src = (f"CSV:{t['table_name']}.csv" if f"{t['table_name']}.csv" in csvs else
                   "built-in reference (views.sql)" if t["table_name"] in BUILTIN_REF else "awaiting extract" if not n else "loaded (no stamp)")
        kind = "master data" if t["table_name"].startswith("dim_") else "transactions" if t["table_name"].startswith("fact_") else "reference"
        out.append({"table": f"sp.{t['table_name']}", "kind": kind, "rows": n, "bytes": t["bytes"],
                    "source": src, "used_by": sorted(uses.get(t["table_name"], [])), "features": CODE_USES.get(t["table_name"], [])})
    used = [x for x in out if (x["used_by"] or x["features"]) and x["rows"]]      # only tables the agent uses that hold records
    return {"name": "Neon DB · primary warehouse", "connector_key": "neon", "host": u.hostname, "database": u.path.lstrip("/").split("?")[0],
            "schema": "sp", "server_version": ver, "tls": bool(tls), "database_bytes": size, "views": views, "tables": used, "hidden_tables": len(out) - len(used),
            "totals": {"tables": len(used), "views": len(views), "rows": sum(x["rows"] for x in used)}}
