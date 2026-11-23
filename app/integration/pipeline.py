"""Load pipeline: extract -> policy (exclude / hash) -> transform + type-check -> staging / quarantine -> diff -> publish (DI Phase 3).

Guarantees
- Nothing reaches a KPI table unless the mapping is in PUBLISH mode AND approved by a second ADMIN AND the run is not a dry run AND
  quarantine stays under the mapping's threshold (default 5 %) - otherwise the run stops after staging.
- Idempotent: rows are matched on the canonical business key; only rows whose values actually differ are updated, new keys are
  inserted, unchanged rows are not rewritten (a second identical run changes nothing). Source record IDs and lineage
  (_source_system, _source_record_id, _connection_id, _run_id, _loaded_at) are stamped on every row written.
- Incremental: >= the last watermark (boundary rows re-read; harmless because loads are idempotent). The watermark only advances
  after a successful publish. Deletions in the source are NOT propagated (SAP uses deletion flags - map them to a status field).
- One table, one owner: a published table belongs to that connection; the CSV reloader then skips it and another connection
  cannot publish into it until ownership is released (rollback).
- Every run is recorded in integration.runs with counts, per-field change counts and examples (reconciliation), plus the audit log.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import secrets as pysecrets
import threading
import time

from sqlalchemy import text

from .dbx import engine
from . import mapping as MP
from . import service as S

STAGING_KEEP_RUNS = 3
LEASE_HOURS = 2
SAMPLE_CHANGES = 8
_SCHEMA_RE = re.compile(r"^[a-z_][a-z0-9_]{0,40}$")


def _q(ident: str) -> str:
    return '"' + ident.replace('"', '""') + '"'


def _new_run_id() -> str:
    return "RUN-" + dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d%H%M%S") + "-" + pysecrets.token_hex(2)


def _extract(con, m: dict, since, batch: int = 5000):
    if m["source_kind"] == "query":
        return con.extract_query(m["source_query"], m.get("watermark_column"), since, batch)
    return con.extract(m["source_object"], m.get("watermark_column"), since, batch)


# ------------------------------------------------------------------ preview (unsaved mapping, no storage)
def preview(d: dict, limit: int = 50) -> dict:
    """Validate + transform the first rows of an (unsaved) mapping. Nothing is stored. Sensitive values are shown masked."""
    from . import service as SV
    d, cols, errs, warns = MP.check(d)
    _, con = SV.connector_for(d["connection_id"])
    out = {"columns": cols, "errors": errs, "warnings": warns, "rows": [], "quarantine": [], "sampled": 0}
    if errs:
        return out
    gen = _extract(con, d, None, max(1, min(int(limit), 100)))
    try:
        rows = next(gen, [])
    finally:
        gen.close()
    good, bad = MP.transform_rows(d, rows, cols)
    sens_fields = {f for f, s in d["field_map"].items() if s.get("source") in {c["name"] for c in cols if c["sensitive"]}
                   and d["column_policy"].get(s["source"]) == "keep"}
    out["rows"] = [{k: ("•••" if k in sens_fields and v is not None else v) for k, v in r.items()} for _, _, r in good]
    out["quarantine"] = [{"source_record_id": s, "values": v, "errors": e} for s, v, e in bad]
    out["sampled"] = len(rows)
    return out


# ------------------------------------------------------------------ diff + publish (set-based SQL over the staging table)
def _exprs(m: dict, schema: str):
    tcols = MP.target_columns(m["target_table"], schema)
    ent = MP.entity(m["entity"])
    fields = list(m["field_map"])
    for f in fields + ent["key"]:
        if tcols[f][0] not in MP.SQL_TYPE:
            raise ValueError(f"unsupported target type {tcols[f][0]} for {f}")
    ex = {f: f"(s.record->>'{f}')::{MP.SQL_TYPE[tcols[f][0]]}" for f in set(fields) | set(ent["key"])}
    match = " AND ".join(f"t.{_q(k)} = {ex[k]}" for k in ent["key"])
    distinct = " OR ".join(f"t.{_q(f)} IS DISTINCT FROM {ex[f]}" for f in fields if f not in ent["key"]) or "FALSE"
    return fields, ex, match, distinct, f"{_q(schema)}.{_q(m['target_table'])}"


def diff(c, m: dict, run_id: str, schema: str = "sp") -> dict:
    fields, ex, match, distinct, tgt = _exprs(m, schema)
    p = {"r": run_id}
    ins = c.execute(text(f"SELECT count(*) FROM integration.staging s WHERE s.run_id = :r AND NOT EXISTS (SELECT 1 FROM {tgt} t WHERE {match})"), p).scalar()
    per = {}
    nonkey = [f for f in fields if f not in MP.entity(m["entity"])["key"]]
    if nonkey:
        sel = ", ".join(f"count(*) FILTER (WHERE t.{_q(f)} IS DISTINCT FROM {ex[f]})" for f in nonkey)
        row = c.execute(text(f"SELECT count(*) FILTER (WHERE {distinct}), {sel} FROM integration.staging s JOIN {tgt} t ON {match} WHERE s.run_id = :r"), p).fetchone()
        upd, per = row[0], {f: int(n) for f, n in zip(nonkey, row[1:]) if n}
    else:
        upd = 0
    matched = c.execute(text(f"SELECT count(*) FROM integration.staging s JOIN {tgt} t ON {match} WHERE s.run_id = :r"), p).scalar()
    samples = []
    for f in list(per)[:SAMPLE_CHANGES]:
        r = c.execute(text(f"""SELECT s.record_key, t.{_q(f)}::text, {ex[f]}::text FROM integration.staging s JOIN {tgt} t ON {match}
                               WHERE s.run_id = :r AND t.{_q(f)} IS DISTINCT FROM {ex[f]} LIMIT 1"""), p).fetchone()
        if r:
            samples.append({"key": r[0], "field": f, "current": r[1], "source": r[2]})
    return {"inserted": int(ins), "updated": int(upd), "unchanged": int(matched - upd), "field_changes": per, "samples": samples}


def publish(c, m: dict, run_id: str, conn: dict, schema: str = "sp") -> dict:
    fields, ex, match, distinct, tgt = _exprs(m, schema)
    ent = MP.entity(m["entity"])
    src_sys = f"{conn['connector_key']}:{m['source_object'] or 'query'}"
    lin = {"ss": src_sys, "cid": m["connection_id"], "r": run_id}
    nonkey = [f for f in fields if f not in ent["key"]]
    sets = ", ".join([f"{_q(f)} = {ex[f]}" for f in nonkey] + ['"_source_system" = :ss', '"_source_record_id" = s.source_record_id',
                                                             '"_connection_id" = :cid', '"_run_id" = :r', '"_loaded_at" = now()'])
    upd = c.execute(text(f"UPDATE {tgt} t SET {sets} FROM integration.staging s WHERE s.run_id = :r AND {match} AND ({distinct})"), lin).rowcount
    # first takeover: data identical, only lineage changes (not counted as a data update)
    rest = c.execute(text(f"""UPDATE {tgt} t SET "_source_system" = :ss, "_source_record_id" = s.source_record_id, "_connection_id" = :cid,
                              "_run_id" = :r, "_loaded_at" = now() FROM integration.staging s
                              WHERE s.run_id = :r AND {match} AND t."_connection_id" IS DISTINCT FROM :cid"""), lin).rowcount
    allf = list(dict.fromkeys(ent["key"] + fields))
    cols = ", ".join(_q(f) for f in allf) + ', "_source_system", "_source_record_id", "_connection_id", "_run_id", "_loaded_at"'
    vals = ", ".join(ex[f] for f in allf) + ", :ss, s.source_record_id, :cid, :r, now()"
    ins = c.execute(text(f"INSERT INTO {tgt} ({cols}) SELECT {vals} FROM integration.staging s WHERE s.run_id = :r "
                         f"AND NOT EXISTS (SELECT 1 FROM {tgt} t WHERE {match})"), lin).rowcount
    return {"inserted": ins, "updated": upd, "restamped": rest}


# ------------------------------------------------------------------ run
def run(mid: str, trigger: str, actor: str, dry_run: bool = False, full: bool = False, target_schema: str = "sp") -> dict:
    if not _SCHEMA_RE.match(target_schema):
        raise ValueError("invalid target schema")
    m = MP.get_mapping(mid)
    conn, con = S.connector_for(m["connection_id"])
    if not m["enabled"] and trigger == "schedule":
        raise PermissionError("mapping is disabled")
    publishing = m["mode"] == "PUBLISH" and not dry_run
    if publishing and m["approval_status"] != "APPROVED":
        raise PermissionError("publishing into KPI tables needs a second ADMIN's approval of this mapping (four-eyes)")
    if publishing and target_schema == "sp":
        owner = S.table_ownership().get(m["target_table"])
        if owner and owner != m["connection_id"]:
            raise PermissionError(f"sp.{m['target_table']} is owned by {owner}; release it first (one table, one source)")
    rid = _new_run_id()
    # Run lease on the mapping row: one atomic UPDATE, released by run id. Works behind transaction poolers (PgBouncer / Neon
    # pooled endpoint), where session advisory locks can be taken and released on different server sessions. A crashed run's
    # lease expires after LEASE_HOURS so a mapping can never stay blocked.
    with engine.begin() as c:
        got = c.execute(text(f"""UPDATE integration.mappings SET running_run_id = :r, running_since = now() WHERE id = :i
                                 AND (running_run_id IS NULL OR running_since < now() - interval '{LEASE_HOURS} hours') RETURNING id"""),
                        {"r": rid, "i": mid}).fetchone()
    if not got:
        raise PermissionError("this mapping is already running")
    since = None if full else m["last_watermark"]
    stats = {"extracted": 0, "staged": 0, "quarantined": 0, "inserted": 0, "updated": 0, "restamped": 0, "unchanged": 0}
    status, msg, wm_to, dres, dq_sum = "FAILED", None, None, {}, {}
    try:
        with engine.begin() as c:
            c.execute(text("""INSERT INTO integration.runs (id, mapping_id, mapping_version, connection_id, trigger, actor, dry_run, status, watermark_from)
                              VALUES (:i, :m, :v, :c, :t, :a, :d, 'RUNNING', :w)"""),
                      {"i": rid, "m": mid, "v": m["version"], "c": m["connection_id"], "t": trigger, "a": actor, "d": not publishing, "w": since})
        cols = MP.source_columns(con, m["source_kind"], m["source_object"], m["source_query"])
        errs, _ = MP.validate(m, cols)
        if errs:
            raise ValueError("mapping no longer matches the source: " + "; ".join(errs[:5]))
        seq, seen = 0, set()
        wcol = m.get("watermark_column")
        # DI5 data-quality rules (flag di_quality_health): ERROR rows are quarantined before staging; WARN rows are counted
        from ..features import is_on
        from . import quality as QL
        rules = QL.list_rules(m["entity"], enabled_only=True) if is_on("di_quality_health") else []
        sens_src = {c["name"] for c in cols if c["sensitive"]}
        masked_fields = [f for f, sp_ in m["field_map"].items() if sp_.get("source") in sens_src]
        for batch in _extract(con, m, since):
            stats["extracted"] += len(batch)
            if stats["extracted"] > m["max_rows"]:
                raise ValueError(f"more than {m['max_rows']} rows - narrow the source (watermark / filter) or raise the mapping's row limit")
            if wcol:
                vals = [str(r[wcol]) for r in batch if r.get(wcol) is not None]
                if vals:
                    wm_to = max([wm_to, *vals]) if wm_to else max(vals)
            good, bad = MP.transform_rows(m, batch, cols)
            if rules:
                good, dq_bad, ssum = QL.apply_row_rules(rules, good)
                for name, v in ssum.items():
                    dq_sum.setdefault(name, {**v, "failed": 0})["failed"] += v["failed"]
                bad += [(srid, {k: ("•••" if k in masked_fields and v is not None else v) for k, v in rec.items()}, e) for srid, rec, e in dq_bad]
            st, qu = [], []
            for key, srid, rec in good:
                if key in seen:
                    qu.append({"r": rid, "m": mid, "s": srid, "v": json.dumps({}), "e": json.dumps([f"duplicate key {key} in this extract (first one kept)"])})
                    continue
                seen.add(key)
                seq += 1
                st.append({"r": rid, "q": seq, "k": key, "s": srid, "j": json.dumps(rec, default=str)})
            qu += [{"r": rid, "m": mid, "s": s, "v": json.dumps(v, default=str), "e": json.dumps(e)} for s, v, e in bad]
            with engine.begin() as c:
                if st:
                    _copy_staging(c, st)
                if qu:
                    c.execute(text("INSERT INTO integration.quarantine (run_id, mapping_id, source_record_id, source_values, errors) "
                                   "VALUES (:r, :m, :s, CAST(:v AS jsonb), CAST(:e AS jsonb))"), qu)
            stats["staged"] += len(st)
            stats["quarantined"] += len(qu)
        with engine.begin() as c:
            c.execute(text("SET LOCAL lock_timeout = '30s'"))          # never queue for minutes behind a stale session
            c.execute(text("SET LOCAL statement_timeout = '600s'"))
            if rules:
                for r in rules:
                    dq_sum.setdefault(r["name"], {"severity": r["severity"], "failed": 0, "kind": r["kind"]})
                moved = QL.apply_reference_rules(c, rules, rid, mid, dq_sum, masked_fields)
                stats["staged"] -= moved
                stats["quarantined"] += moved
            pct = 100.0 * stats["quarantined"] / stats["extracted"] if stats["extracted"] else 0.0
            dres = diff(c, m, rid, target_schema)
            stats["inserted"], stats["updated"], stats["unchanged"] = dres["inserted"], dres["updated"], dres["unchanged"]
            if publishing and pct > float(m["max_quarantine_pct"]):
                status = "FAILED"
                msg = f"{pct:.1f}% of rows quarantined (limit {float(m['max_quarantine_pct']):g}%) - nothing published. Fix the source or the mapping."
            elif publishing:
                res = publish(c, m, rid, conn, target_schema)
                stats.update(res)
                if target_schema == "sp":
                    c.execute(text("""INSERT INTO integration.table_ownership (canonical_table, owner, set_by) VALUES (:t, :o, :a)
                                      ON CONFLICT (canonical_table) DO UPDATE SET owner = :o, set_by = :a, set_at = now()"""),
                              {"t": m["target_table"], "o": m["connection_id"], "a": actor})
                c.execute(text("UPDATE integration.mappings SET last_watermark = COALESCE(:w, last_watermark) WHERE id = :i"), {"w": wm_to, "i": mid})
                status = "PARTIAL" if stats["quarantined"] else "SUCCESS"
                msg = (f"Published: {res['inserted']} new, {res['updated']} changed, {stats['unchanged']} unchanged"
                       + (f" ({res['restamped']} re-stamped with lineage)" if res["restamped"] else "") + ".")
            else:
                status = "PARTIAL" if stats["quarantined"] else "SUCCESS"
                msg = (f"{'Dry run' if m['mode'] == 'PUBLISH' else 'Staged'} - nothing written to KPI tables. Would insert {stats['inserted']}, "
                       f"update {stats['updated']}, leave {stats['unchanged']} unchanged.")
    except Exception as e:  # noqa: BLE001
        status, msg = "FAILED", str(e)[:800]
    finally:
        try:
            with engine.begin() as c:                       # upsert: the run is recorded even if the start insert failed
                c.execute(text("""INSERT INTO integration.runs (id, mapping_id, mapping_version, connection_id, trigger, actor, dry_run, status,
                                  watermark_from, finished_at, watermark_to, extracted, staged, quarantined, inserted, updated, restamped, unchanged,
                                  field_changes, samples, message)
                                  VALUES (:i, :m, :v, :c, :t, :a, :d, :st, :wf, now(), :w, :ex, :sg, :qu, :ins, :up, :rs, :un,
                                  CAST(:fc AS jsonb), CAST(:sm AS jsonb), :msg)
                                  ON CONFLICT (id) DO UPDATE SET status = EXCLUDED.status, finished_at = now(), watermark_to = EXCLUDED.watermark_to,
                                  extracted = EXCLUDED.extracted, staged = EXCLUDED.staged, quarantined = EXCLUDED.quarantined,
                                  inserted = EXCLUDED.inserted, updated = EXCLUDED.updated, restamped = EXCLUDED.restamped,
                                  unchanged = EXCLUDED.unchanged, field_changes = EXCLUDED.field_changes, samples = EXCLUDED.samples,
                                  message = EXCLUDED.message"""),
                          {"i": rid, "m": mid, "v": m["version"], "c": m["connection_id"], "t": trigger, "a": actor, "d": not publishing,
                           "wf": since, "st": status, "w": wm_to, "ex": stats["extracted"], "sg": stats["staged"], "qu": stats["quarantined"],
                           "ins": stats["inserted"], "up": stats["updated"], "rs": stats["restamped"], "un": stats["unchanged"],
                           "fc": json.dumps(dres.get("field_changes") or {}), "sm": json.dumps(dres.get("samples") or [], default=str), "msg": msg})
                c.execute(text("""UPDATE integration.mappings SET last_run_at = now(), last_run_id = :r, last_run_status = :st, next_run_at = :nr
                                  WHERE id = :i"""), {"r": rid, "st": status, "nr": MP.next_run(m["schedule"]), "i": mid})
                c.execute(text("UPDATE integration.runs SET dq_summary = CAST(:d AS jsonb) WHERE id = :i"), {"d": json.dumps(dq_sum), "i": rid})
                if status != "FAILED":                       # a record that now loads cleanly closes its earlier quarantine entries
                    c.execute(text("""UPDATE integration.quarantine q SET status = 'RESOLVED', resolved_by = :by, resolved_at = now()
                                      WHERE q.mapping_id = :m AND q.status = 'OPEN' AND q.run_id <> :r AND q.source_record_id IN
                                      (SELECT source_record_id FROM integration.staging WHERE run_id = :r)"""),
                              {"by": f"system:{rid}", "m": mid, "r": rid})
                c.execute(text("""DELETE FROM integration.staging WHERE run_id IN (SELECT id FROM integration.runs WHERE mapping_id = :m
                                  ORDER BY started_at DESC OFFSET :k)"""), {"m": mid, "k": STAGING_KEEP_RUNS})
                if publishing and status != "FAILED":
                    c.execute(text("UPDATE integration.connections SET last_sync_at = now() WHERE id = :c"), {"c": m["connection_id"]})
        finally:
            _release(mid, rid)
        try:
            if dq_sum:
                from . import quality as QL2
                QL2.record_results(rid, m["entity"], dq_sum, stats["extracted"])
        except Exception:  # noqa: BLE001 - reporting must not fail the run
            pass
    S.audit(actor, "run" if publishing else "run:dry", m["connection_id"], {"mapping": mid, "run": rid, "status": status, **stats})
    if status == "FAILED" and trigger == "schedule":
        _notify_failure(m, rid, msg)
    if publishing and status != "FAILED" and target_schema == "sp" and (stats["inserted"] or stats["updated"]):
        _refresh_kpis()
    return get_run(rid)


def _release(mid: str, rid: str) -> None:
    for _ in range(3):                                   # retried: a lease left behind would block the mapping until it expires
        try:
            with engine.begin() as c:
                c.execute(text("UPDATE integration.mappings SET running_run_id = NULL, running_since = NULL WHERE id = :i AND running_run_id = :r"),
                          {"i": mid, "r": rid})
            return
        except Exception:  # noqa: BLE001
            time.sleep(2)


def _copy_staging(c, rows: list[dict]) -> None:
    """Bulk-load staged rows with COPY (one round-trip per batch instead of one per row - ~10x faster over a WAN link)."""
    cur = c.connection.driver_connection.cursor()
    with cur.copy("COPY integration.staging (run_id, seq, record_key, source_record_id, record) FROM STDIN") as cp:
        for x in rows:
            cp.write_row((x["r"], x["q"], x["k"], x["s"], x["j"]))


def _notify_failure(m: dict, rid: str, msg: str | None) -> None:
    try:
        from ..db import Notification, SessionLocal
        from ..jobs import deliver
        with SessionLocal() as s:
            s.add(Notification(recipient_role="ADMIN", severity="HIGH", subject=f"Data load {m['id']} ({m['name']}) failed", body=f"Run {rid}: {msg}"))
            s.commit()
        deliver(f"Data load {m['id']} failed", f"Run {rid}: {msg}", "ADMIN")
    except Exception:  # noqa: BLE001
        pass


def _refresh_kpis() -> None:
    """KPI inputs changed: recompute the cached analytics in the background (logged as a job run)."""
    from ..jobs import run_job
    threading.Thread(target=run_job, args=("refresh", "integration"), daemon=True).start()


# ------------------------------------------------------------------ runs, quarantine, ownership
def list_runs(mapping_id: str | None = None, limit: int = 100) -> list[dict]:
    with engine.connect() as c:
        q = ("SELECT r.*, m.name AS mapping_name, m.entity, m.target_table FROM integration.runs r LEFT JOIN integration.mappings m ON m.id = r.mapping_id"
             + (" WHERE r.mapping_id = :m" if mapping_id else "") + " ORDER BY r.started_at DESC LIMIT :n")
        return [dict(r._mapping) for r in c.execute(text(q), {"m": mapping_id, "n": limit})]


def get_run(rid: str) -> dict:
    with engine.connect() as c:
        r = c.execute(text("""SELECT r.*, m.name AS mapping_name, m.entity, m.target_table FROM integration.runs r
                              LEFT JOIN integration.mappings m ON m.id = r.mapping_id WHERE r.id = :i"""), {"i": rid}).fetchone()
    if not r:
        raise LookupError("run not found")
    return dict(r._mapping)


def quarantine(mapping_id: str | None = None, run_id: str | None = None, status: str | None = "OPEN", limit: int = 200) -> list[dict]:
    w, p = [], {"n": limit}
    for col, v in (("mapping_id", mapping_id), ("run_id", run_id), ("status", status)):
        if v:
            w.append(f"{col} = :{col}")
            p[col] = v
    with engine.connect() as c:
        return [dict(r._mapping) for r in c.execute(text("SELECT * FROM integration.quarantine" + (" WHERE " + " AND ".join(w) if w else "")
                                                         + " ORDER BY id DESC LIMIT :n"), p)]


def resolve_quarantine(qid: int, action: str, actor: str) -> dict:
    action = (action or "").upper()
    if action not in ("IGNORED", "RESOLVED"):
        raise ValueError("action must be IGNORED or RESOLVED (fix the source, then re-run the mapping)")
    with engine.begin() as c:
        r = c.execute(text("""UPDATE integration.quarantine SET status = :s, resolved_by = :a, resolved_at = now() WHERE id = :i AND status = 'OPEN'
                              RETURNING mapping_id"""), {"s": action, "a": actor, "i": qid}).fetchone()
    if not r:
        raise LookupError("open quarantine record not found")
    S.audit(actor, f"quarantine:{action.lower()}", None, {"quarantine_id": qid, "mapping": r[0]})
    return {"id": qid, "status": action}


def release_ownership(table: str, actor: str) -> dict:
    """Rollback lever: hand a KPI table back to the CSV loader (data stays; lineage shows where each row came from)."""
    with engine.begin() as c:
        r = c.execute(text("DELETE FROM integration.table_ownership WHERE canonical_table = :t RETURNING owner"), {"t": table}).fetchone()
    if not r:
        raise LookupError(f"sp.{table} is not owned by a connection")
    S.audit(actor, "ownership:release", r[0], {"table": table})
    return {"table": table, "released_from": r[0]}


# ------------------------------------------------------------------ scheduler (1-minute tick, own thread)
def due_mappings() -> list[str]:
    with engine.connect() as c:
        return [r[0] for r in c.execute(text("""SELECT id FROM integration.mappings WHERE active AND enabled AND schedule <> 'manual'
                                                AND next_run_at IS NOT NULL AND next_run_at <= now() ORDER BY next_run_at"""))]


def sync_due() -> dict:
    out = {}
    for mid in due_mappings():
        try:
            out[mid] = run(mid, "schedule", "scheduler")["status"]
        except Exception as e:  # noqa: BLE001
            out[mid] = f"ERROR: {str(e)[:200]}"
            with engine.begin() as c:                                           # do not retry every minute
                m = MP.get_mapping(mid)
                c.execute(text("UPDATE integration.mappings SET next_run_at = :n, last_run_status = 'FAILED' WHERE id = :i"),
                          {"n": MP.next_run(m["schedule"]), "i": mid})
            _notify_failure(MP.get_mapping(mid), "-", str(e)[:300])
    return {"runs": out}


_started = [False]


def start_ticker() -> None:
    import os
    if os.environ.get("DISABLE_SCHEDULER") == "1" or _started[0]:
        return
    _started[0] = True

    def loop():
        from ..features import is_on
        from ..jobs import JOBS, run_job
        from . import health as HL
        JOBS.setdefault("integration_sync", sync_due)
        JOBS.setdefault("integration_health", HL.job)
        time.sleep(60)
        last_health = 0.0
        while True:
            try:
                if is_on("data_integration") and is_on("di_mapping_loads") and due_mappings():
                    run_job("integration_sync", "schedule")
                if is_on("data_integration") and is_on("di_quality_health") and time.time() - last_health >= 900:
                    last_health = time.time()
                    run_job("integration_health", "schedule")          # failed / late-load alerts + retention, every 15 min
            except Exception:  # noqa: BLE001
                pass
            time.sleep(60)

    threading.Thread(target=loop, daemon=True, name="integration-scheduler").start()
