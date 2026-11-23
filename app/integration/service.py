"""Connections, environments, four-eyes approval and audit (DI Phase 1).

- Only ADMIN creates / edits / deletes / tests connections (enforced in auth.POLICY); everyone else can view status.
- Secrets go to the vault; the row keeps a secret_ref and the NAMES of secret fields (for masked display) only.
- PROD connections need a second ADMIN's approval (four-eyes): the approver must differ from the requester. Editing a PROD
  connection's config or secrets resets it to pending. Pending / rejected connections cannot be tested or synced.
- Every create / edit / delete / approve / test / preview is written to integration.audit_log (never secret values).
"""
from __future__ import annotations

import json
import os

from sqlalchemy import text

from .dbx import engine
from . import registry as REG
from .secrets import mask, vault

ENVIRONMENTS = ("DEV", "TEST", "PROD")


def active_environment() -> str:
    e = os.environ.get("APP_ENV", "DEV").upper()
    return e if e in ENVIRONMENTS else "DEV"


def audit(actor: str, action: str, connection_id: str | None = None, detail: dict | None = None) -> None:
    with engine.begin() as c:
        c.execute(text("INSERT INTO integration.audit_log (actor, action, connection_id, detail) VALUES (:a, :x, :c, CAST(:d AS jsonb))"),
                  {"a": actor, "x": action, "c": connection_id, "d": json.dumps(detail or {}, default=str)})


def _row(r) -> dict:
    d = dict(r._mapping) if hasattr(r, "_mapping") else dict(r)
    meta = REG.meta_for(d["connector_key"])
    d["connector_name"] = meta.name if meta else d["connector_key"]
    d["category"] = meta.category if meta else None
    d["secrets_masked"] = {k: "••••••••" for k in (d.pop("secret_fields") or [])}     # names only, never values or lengths
    d.pop("secret_ref", None)
    return d


def list_connections() -> list[dict]:
    with engine.connect() as c:
        return [_row(r) for r in c.execute(text("SELECT * FROM integration.connections WHERE active ORDER BY environment, name"))]


def get_connection(cid: str, with_secret_ref: bool = False) -> dict:
    with engine.connect() as c:
        r = c.execute(text("SELECT * FROM integration.connections WHERE id = :i AND active"), {"i": cid}).mappings().fetchone()
    if not r:
        raise LookupError("connection not found")
    return dict(r) if with_secret_ref else _row(r)


def _split(key: str, config: dict, secrets: dict) -> tuple[dict, dict]:
    meta = REG.meta_for(key)
    if not meta:
        raise ValueError(f"unknown connector '{key}'")
    secret_names = {f.name for f in meta.fields if f.secret}
    known = {f.name for f in meta.fields}
    cfg = {k: v for k, v in (config or {}).items() if k in known and k not in secret_names}
    # a secret typed into a config field is still treated as a secret; an explicit `secrets` value always wins
    sec = {k: v for k, v in {**{k: v for k, v in (config or {}).items() if k in secret_names}, **(secrets or {})}.items() if k in secret_names and v not in (None, "")}
    return cfg, sec


def create_connection(key: str, name: str, environment: str, config: dict, secrets: dict, actor: str) -> dict:
    environment = environment.upper()
    if environment not in ENVIRONMENTS:
        raise ValueError("environment must be DEV, TEST or PROD")
    meta = REG.meta_for(key)
    if not meta:
        raise ValueError(f"unknown connector '{key}'")
    if meta.availability == "coming_soon":
        raise ValueError(f"{meta.name} is coming soon - it cannot be connected yet")
    if not REG.enabled(key):
        raise ValueError(f"{meta.name} is switched off (feature flag)")
    if meta.availability == "planned" or key not in REG.CONNECTORS:
        raise ValueError(f"{meta.name} is in development (Phase {meta.phase}) - it can be connected once that phase ships")
    cfg, sec = _split(key, config, secrets)
    errs = [f"{f.label} is required" for f in meta.fields if f.required and (sec if f.secret else cfg).get(f.name) in (None, "")]
    errs += REG.CONNECTORS[key].policy_errors(cfg, sec, environment)          # TLS outside DEV, SSH pinning, ...
    if errs:
        raise ValueError("; ".join(errs))
    ref = vault().put(sec, actor) if sec else None
    prod = environment == "PROD"
    with engine.begin() as c:
        n = c.execute(text("SELECT COUNT(*) FROM integration.connections")).scalar() + 1
        cid = f"CONN-{n:04d}"
        c.execute(text("""INSERT INTO integration.connections (id, name, connector_key, environment, config, secret_ref, secret_fields, status,
                          approval_status, requested_by, created_by) VALUES (:i, :n, :k, :e, CAST(:c AS jsonb), :r, CAST(:f AS jsonb), :s, :ap, :a, :a)"""),
                  {"i": cid, "n": name.strip(), "k": key, "e": environment, "c": json.dumps(cfg), "r": ref, "f": json.dumps(sorted(sec)),
                   "s": "PENDING_APPROVAL" if prod else "NOT_CONNECTED", "ap": "PENDING" if prod else "APPROVED", "a": actor})
    audit(actor, "create", cid, {"connector": key, "environment": environment, "config_keys": sorted(cfg), "secret_fields": sorted(sec)})
    return get_connection(cid)


def update_connection(cid: str, actor: str, name: str | None = None, config: dict | None = None, secrets: dict | None = None) -> dict:
    cur = get_connection(cid, with_secret_ref=True)
    cfg, sec = _split(cur["connector_key"], config or {}, secrets or {})
    new_cfg = {**(cur["config"] or {}), **cfg}
    cls = REG.CONNECTORS.get(cur["connector_key"])
    if cls and (cfg or sec):
        have = {**(vault().get(cur["secret_ref"]) if cur["secret_ref"] else {}), **sec}
        errs = cls.policy_errors(new_cfg, have, cur["environment"])
        if errs:
            raise ValueError("; ".join(errs))
    ref, fields = cur["secret_ref"], list(cur["secret_fields"] or [])
    if sec:
        merged = {**(vault().get(ref) if ref else {}), **sec}
        if ref:
            vault().replace(ref, merged, actor)
        else:
            ref = vault().put(merged, actor)
        fields = sorted(merged)
    changed = bool(cfg or sec)
    reset = changed and cur["environment"] == "PROD"
    with engine.begin() as c:
        c.execute(text("""UPDATE integration.connections SET name = COALESCE(:n, name), config = CAST(:c AS jsonb), secret_ref = :r,
                          secret_fields = CAST(:f AS jsonb), updated_at = now(),
                          approval_status = CASE WHEN :reset THEN 'PENDING' ELSE approval_status END,
                          status = CASE WHEN :reset THEN 'PENDING_APPROVAL' ELSE status END,
                          approved_by = CASE WHEN :reset THEN NULL ELSE approved_by END, requested_by = CASE WHEN :reset THEN :a ELSE requested_by END
                          WHERE id = :i"""),
                  {"n": name, "c": json.dumps(new_cfg), "r": ref, "f": json.dumps(fields), "reset": reset, "a": actor, "i": cid})
    audit(actor, "update", cid, {"config_keys": sorted(cfg), "secret_fields_changed": sorted(sec), "approval_reset": reset})
    return get_connection(cid)


def delete_connection(cid: str, actor: str) -> None:
    cur = get_connection(cid, with_secret_ref=True)
    if cur["secret_ref"]:
        vault().delete(cur["secret_ref"])
    with engine.begin() as c:
        c.execute(text("UPDATE integration.connections SET active = FALSE, secret_ref = NULL, status = 'DELETED', updated_at = now() WHERE id = :i"), {"i": cid})
    audit(actor, "delete", cid, {"connector": cur["connector_key"]})


def decide(cid: str, decision: str, actor: str, note: str | None = None) -> dict:
    """Four-eyes approval of a PROD connection: approver must be a different ADMIN than the requester."""
    cur = get_connection(cid, with_secret_ref=True)
    decision = decision.upper()
    if decision not in ("APPROVED", "REJECTED"):
        raise ValueError("decision must be APPROVED or REJECTED")
    if cur["approval_status"] != "PENDING":
        raise PermissionError(f"connection is {cur['approval_status']}, nothing to approve")
    if actor == cur["requested_by"]:
        raise PermissionError("four-eyes rule: the requester cannot approve their own production connection")
    with engine.begin() as c:
        c.execute(text("""UPDATE integration.connections SET approval_status = CAST(:d AS varchar), approved_by = :a, approved_at = now(),
                          status = CASE WHEN CAST(:d AS varchar) = 'APPROVED' THEN 'NOT_CONNECTED' ELSE 'REJECTED' END, updated_at = now() WHERE id = :i"""),
                  {"d": decision, "a": actor, "i": cid})
    audit(actor, f"approval:{decision.lower()}", cid, {"note": note, "requested_by": cur["requested_by"]})
    return get_connection(cid)


def connector_for(cid: str):
    cur = get_connection(cid, with_secret_ref=True)
    if not REG.enabled(cur["connector_key"]):
        raise PermissionError("this connector type is switched off (feature flag)")
    if cur["approval_status"] != "APPROVED":
        raise PermissionError("connection is not approved yet (production sources need a second ADMIN's approval)")
    secrets = vault().get(cur["secret_ref"]) if cur["secret_ref"] else {}
    return cur, REG.instance(cur["connector_key"], cur["config"] or {}, secrets, cur["environment"], cur["id"])


def test(cid: str, actor: str) -> dict:
    cur, con = connector_for(cid)
    try:
        r = con.test_connection()
        res = {"ok": r.ok, "message": r.message, "hint": r.hint, "details": r.details}
    except Exception as e:  # noqa: BLE001 - surface as a plain-language failure
        res = {"ok": False, "message": "The connection test failed.", "hint": str(e)[:300], "details": {}}
    d = res["details"] or {}
    priv = d.get("privileges") or {}
    warning = "; ".join(d.get("warnings") or []) or None
    with engine.begin() as c:
        c.execute(text("""UPDATE integration.connections SET status = :s, last_test_at = now(), last_test_ok = :ok, last_test_message = :m,
                          can_write = :w, tls_active = :t, warnings = :warn, updated_at = now() WHERE id = :i"""),
                  {"s": "CONNECTED" if res["ok"] else "ERROR", "ok": res["ok"], "m": res["message"], "i": cid,
                   "w": priv.get("can_write") if res["ok"] else None, "t": d.get("tls") if res["ok"] else None, "warn": warning if res["ok"] else None})
    audit(actor, "test", cid, {"ok": res["ok"], "message": res["message"], "can_write": priv.get("can_write"), "tls": d.get("tls"),
                               "error_category": d.get("category"), "warnings": d.get("warnings")})
    return res


def schema(cid: str, actor: str) -> list[dict]:
    from dataclasses import asdict
    _, con = connector_for(cid)
    out = [asdict(o) for o in con.discover_schema()]
    audit(actor, "discover_schema", cid, {"objects": len(out)})
    return out


def preview(cid: str, obj: str, actor: str) -> dict:
    _, con = connector_for(cid)
    out = con.preview_data(obj)
    audit(actor, "preview", cid, {"object": obj, "rows": out["row_count"], "masked": out["masked_columns"]})
    return out


def query(cid: str, sql: str, limit: int, actor: str) -> dict:
    """Custom SELECT against a source: parsed SELECT-only guard, read-only session, timeout, row cap, masked output.
    The query text is audited (it is admin-written SQL, never source data); results are not stored."""
    import hashlib
    from .sqlsafe import UnsafeQuery
    cur, con = connector_for(cid)
    h = hashlib.sha256((sql or "").encode()).hexdigest()[:16]
    try:
        out = con.run_query(sql, limit)
    except UnsafeQuery as e:
        audit(actor, "query:blocked", cid, {"sql_sha256": h, "sql": (sql or "")[:500], "reason": str(e)})
        raise ValueError(f"Query blocked: {e}") from None
    audit(actor, "query", cid, {"sql_sha256": h, "sql": sql[:500], "rows": out["row_count"], "masked": out["masked_columns"], "elapsed_ms": out["elapsed_ms"]})
    return out


def catalogue_with_status() -> list[dict]:
    conns = list_connections()
    out = []
    for m in REG.catalogue():
        mine = [c for c in conns if c["connector_key"] == m["key"]]
        st = "NOT_CONNECTED"
        for s in ("ERROR", "SYNCING", "PENDING_APPROVAL", "CONNECTED"):
            if any(c["status"] == s for c in mine):
                st = s
                break
        if not REG.enabled(m["key"]):
            m = {**m, "availability": "planned", "flag_off": True}
        out.append({**m, "connections": len(mine), "status": st})
    return out


def audit_log(limit: int = 200, connection_id: str | None = None) -> list[dict]:
    with engine.connect() as c:
        q = "SELECT * FROM integration.audit_log" + (" WHERE connection_id = :c" if connection_id else "") + " ORDER BY id DESC LIMIT :n"
        return [dict(r._mapping) for r in c.execute(text(q), {"c": connection_id, "n": limit})]


def table_ownership() -> dict:
    with engine.connect() as c:
        return {r[0]: r[1] for r in c.execute(text("SELECT canonical_table, owner FROM integration.table_ownership"))}
