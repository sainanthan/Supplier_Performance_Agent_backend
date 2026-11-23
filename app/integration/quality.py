"""Data-quality rules for loads (DI Phase 5).

Rules are CONFIGURATION, never SQL text: a fixed set of kinds with validated parameters, per canonical entity.
  not_null              {field}
  conditional_not_null  {field, when_field, when_value}         e.g. a FAILed inspection needs a defect code
  range                 {field, min?, max?}
  allowed_values        {field, values[]}
  pattern               {field, regex}                          (regex length / value length capped - no catastrophic input)
  date_order            {first, second}                         first <= second when both present
  ratio_max             {field, of, max}                        field <= max * of
  reference             {field, entity, ref_field}              value must exist in the referenced canonical (sp) table
Severity ERROR -> the row is quarantined with the rule's reason and never staged; WARN -> loaded, counted in the run's DQ summary.
Row rules run on the transformed canonical record (after column policy + casting); reference rules run set-based on the staged
rows against the KPI master tables. Every run's results are also written to public.dq_results (same table as the CSV loader's
checks), so one data-quality history covers both paths.
"""
from __future__ import annotations

import datetime as dt
import json
import re

from sqlalchemy import text

from .canonical import CANONICAL
from .dbx import engine

KINDS = {
    "not_null": ("field",), "conditional_not_null": ("field", "when_field", "when_value"), "range": ("field",),
    "allowed_values": ("field", "values"), "pattern": ("field", "regex"), "date_order": ("first", "second"),
    "ratio_max": ("field", "of", "max"), "reference": ("field", "entity", "ref_field"),
}
FIELD_PARAMS = ("field", "when_field", "first", "second", "of")


def validate_rule(entity: str, kind: str, params: dict, severity: str) -> list[str]:
    errs = []
    ent = CANONICAL.get(entity)
    if not ent:
        return [f"unknown entity '{entity}'"]
    if kind not in KINDS:
        return [f"unknown rule kind '{kind}' (allowed: {', '.join(KINDS)})"]
    if severity not in ("ERROR", "WARN"):
        errs.append("severity must be ERROR or WARN")
    for k in KINDS[kind]:
        if params.get(k) in (None, "", []):
            errs.append(f"'{k}' is required for {kind}")
    for k in FIELD_PARAMS:
        if k in params and params[k] not in ent["fields"]:
            errs.append(f"{k}: '{params[k]}' is not a field of {ent['label']}")
    if kind == "range" and params.get("min") is None and params.get("max") is None:
        errs.append("range needs min and/or max")
    for k in ("min", "max"):
        if params.get(k) is not None:
            try:
                float(params[k])
            except (TypeError, ValueError):
                errs.append(f"{k} must be a number")
    if kind == "pattern":
        rx = str(params.get("regex") or "")
        if len(rx) > 200:
            errs.append("pattern longer than 200 characters")
        try:
            re.compile(rx)
        except re.error as e:
            errs.append(f"invalid pattern: {e}")
    if kind == "allowed_values" and not isinstance(params.get("values"), list):
        errs.append("values must be a list")
    if kind == "reference":
        ref = CANONICAL.get(params.get("entity") or "")
        if not ref:
            errs.append(f"reference entity '{params.get('entity')}' does not exist")
        elif params.get("ref_field") not in ref["fields"]:
            errs.append(f"'{params.get('ref_field')}' is not a field of {ref['label']}")
    return errs


# ------------------------------------------------------------------ CRUD
def list_rules(entity: str | None = None, enabled_only: bool = False) -> list[dict]:
    q = "SELECT * FROM integration.dq_rules WHERE TRUE" + (" AND entity = :e" if entity else "") + (" AND enabled" if enabled_only else "") + " ORDER BY entity, id"
    with engine.connect() as c:
        return [dict(r._mapping) for r in c.execute(text(q), {"e": entity})]


def create_rule(d: dict, actor: str) -> dict:
    from . import service as S
    errs = validate_rule(d.get("entity"), d.get("kind"), d.get("params") or {}, (d.get("severity") or "ERROR").upper())
    name = re.sub(r"[^a-z0-9_]+", "_", (d.get("name") or "").lower()).strip("_")[:60]
    if not name:
        errs.append("name is required")
    if errs:
        raise ValueError("; ".join(errs))
    with engine.begin() as c:
        if c.execute(text("SELECT 1 FROM integration.dq_rules WHERE entity = :e AND name = :n"), {"e": d["entity"], "n": name}).fetchone():
            raise ValueError(f"a rule '{name}' already exists for {d['entity']}")
        rid = c.execute(text("""INSERT INTO integration.dq_rules (entity, name, kind, params, severity, description, created_by)
                                VALUES (:e, :n, :k, CAST(:p AS jsonb), :s, :d, :a) RETURNING id"""),
                        {"e": d["entity"], "n": name, "k": d["kind"], "p": json.dumps(d.get("params") or {}), "s": (d.get("severity") or "ERROR").upper(),
                         "d": d.get("description"), "a": actor}).scalar()
    S.audit(actor, "dq_rule:create", None, {"rule": rid, "entity": d["entity"], "name": name, "kind": d["kind"], "severity": d.get("severity")})
    return get_rule(rid)


def get_rule(rid: int) -> dict:
    with engine.connect() as c:
        r = c.execute(text("SELECT * FROM integration.dq_rules WHERE id = :i"), {"i": rid}).fetchone()
    if not r:
        raise LookupError("rule not found")
    return dict(r._mapping)


def update_rule(rid: int, d: dict, actor: str) -> dict:
    from . import service as S
    cur = get_rule(rid)
    params = d.get("params") if d.get("params") is not None else cur["params"]
    sev = (d.get("severity") or cur["severity"]).upper()
    errs = validate_rule(cur["entity"], cur["kind"], params, sev)
    if errs:
        raise ValueError("; ".join(errs))
    with engine.begin() as c:
        c.execute(text("""UPDATE integration.dq_rules SET params = CAST(:p AS jsonb), severity = :s, enabled = :en, description = COALESCE(:d, description),
                          updated_at = now() WHERE id = :i"""),
                  {"p": json.dumps(params), "s": sev, "en": cur["enabled"] if d.get("enabled") is None else bool(d["enabled"]), "d": d.get("description"), "i": rid})
    S.audit(actor, "dq_rule:update", None, {"rule": rid, "before": {"severity": cur["severity"], "enabled": cur["enabled"], "params": cur["params"]},
                                             "after": {"severity": sev, "enabled": d.get("enabled", cur["enabled"]), "params": params}})
    return get_rule(rid)


def delete_rule(rid: int, actor: str) -> None:
    from . import service as S
    cur = get_rule(rid)
    with engine.begin() as c:
        c.execute(text("DELETE FROM integration.dq_rules WHERE id = :i"), {"i": rid})
    S.audit(actor, "dq_rule:delete", None, {"rule": rid, "entity": cur["entity"], "name": cur["name"]})


# ------------------------------------------------------------------ evaluation
def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _cmp_date(v):
    if v is None:
        return None
    s = str(v)
    try:
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).replace(tzinfo=None) if "T" in s else dt.datetime.fromisoformat(s[:10])
    except ValueError:
        return None


def check_row(rule: dict, rec: dict) -> str | None:
    """None when the record passes, else a plain reason."""
    k, p = rule["kind"], rule["params"]
    f = p.get("field")
    v = rec.get(f) if f else None
    if k == "not_null":
        return None if v not in (None, "") else f"{f} is empty"
    if k == "conditional_not_null":
        return f"{f} is empty while {p['when_field']} = {p['when_value']}" if str(rec.get(p["when_field"])) == str(p["when_value"]) and v in (None, "") else None
    if v is None and k in ("range", "allowed_values", "pattern", "ratio_max"):
        return None
    if k == "range":
        n = _num(v)
        if n is None:
            return f"{f} = {v} is not a number"
        if p.get("min") is not None and n < float(p["min"]):
            return f"{f} = {v} is below {p['min']}"
        if p.get("max") is not None and n > float(p["max"]):
            return f"{f} = {v} is above {p['max']}"
        return None
    if k == "allowed_values":
        return None if str(v) in {str(x) for x in p["values"]} else f"{f} = '{v}' is not one of {', '.join(map(str, p['values']))}"
    if k == "pattern":
        return None if re.fullmatch(p["regex"], str(v)[:200]) else f"{f} = '{str(v)[:40]}' does not match the expected format"
    if k == "date_order":
        ra, rb = rec.get(p["first"]), rec.get(p["second"])
        a, b = _cmp_date(ra), _cmp_date(rb)
        if a and b and (len(str(ra)) <= 10 or len(str(rb)) <= 10):        # a plain date on either side: compare whole days
            a, b = a.date(), b.date()
        return f"{p['second']} ({rb}) is before {p['first']} ({ra})" if a and b and b < a else None
    if k == "ratio_max":
        a, b = _num(v), _num(rec.get(p["of"]))
        return f"{f} = {v} is more than {p['max']} x {p['of']} ({b})" if a is not None and b and a > float(p["max"]) * b else None
    return None


def apply_row_rules(rules: list[dict], good: list[tuple]) -> tuple[list[tuple], list[tuple], dict]:
    """-> (still good, quarantined [(srid, values, errors)], summary {rule: {severity, failed}})."""
    row_rules = [r for r in rules if r["kind"] != "reference"]
    summary = {r["name"]: {"severity": r["severity"], "failed": 0, "kind": r["kind"]} for r in rules}
    keep, bad = [], []
    for key, srid, rec in good:
        errs = []
        for r in row_rules:
            why = check_row(r, rec)
            if why:
                summary[r["name"]]["failed"] += 1
                if r["severity"] == "ERROR":
                    errs.append(f"DQ {r['name']}: {why}")
        if errs:
            bad.append((srid, rec, errs))
        else:
            keep.append((key, srid, rec))
    return keep, bad, summary


def apply_reference_rules(c, rules: list[dict], run_id: str, mapping_id: str, summary: dict, masked_fields: list[str] | None = None) -> int:
    """Set-based: staged rows whose reference does not exist in the KPI master table. ERROR -> moved to quarantine."""
    moved = 0
    for r in [x for x in rules if x["kind"] == "reference"]:
        p = r["params"]
        ref = CANONICAL[p["entity"]]
        f, rf = p["field"], p["ref_field"]
        miss = f"""s.run_id = :r AND (s.record->>'{f}') IS NOT NULL AND NOT EXISTS
                   (SELECT 1 FROM sp."{ref['table']}" d WHERE d."{rf}"::text = s.record->>'{f}')"""
        n = c.execute(text(f"SELECT count(*) FROM integration.staging s WHERE {miss}"), {"r": run_id}).scalar()
        summary[r["name"]]["failed"] = int(n)
        if n and r["severity"] == "ERROR":
            c.execute(text(f"""INSERT INTO integration.quarantine (run_id, mapping_id, source_record_id, source_values, errors)
                               SELECT CAST(:r AS varchar), CAST(:m AS varchar), s.source_record_id, s.record - CAST(:sens AS text[]),
                                      jsonb_build_array('DQ ' || CAST(:rn AS text) || ': ' || CAST(:f AS text) || ' = ' || (s.record->>'{f}')
                                                        || ' does not exist in ' || CAST(:lbl AS text))
                               FROM integration.staging s WHERE {miss}"""),
                      {"r": run_id, "m": mapping_id, "rn": r["name"], "f": f, "lbl": ref["label"].lower() + " master", "sens": list(masked_fields or [])})
            moved += c.execute(text(f"DELETE FROM integration.staging s WHERE {miss}"), {"r": run_id}).rowcount
    return moved


def record_results(run_id: str, entity: str, summary: dict, total: int) -> None:
    """Mirror into public.dq_results (the history the CSV loader's checks already use)."""
    if not summary:
        return
    with engine.begin() as c:
        c.execute(text("""INSERT INTO public.dq_results (load_id, table_name, rule, severity, failed_rows, total_rows, sample, created_at)
                          VALUES (:l, :t, :r, :s, :f, :n, CAST('[]' AS json), now())"""),
                  [{"l": run_id[:36], "t": f"di:{entity}", "r": name, "s": "PASS" if v["failed"] == 0 else v["severity"], "f": v["failed"], "n": total}
                   for name, v in summary.items()])
