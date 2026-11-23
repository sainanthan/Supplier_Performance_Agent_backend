"""Field mapping: source columns -> canonical model (DI Phase 3).

A mapping says, for one connection and one canonical entity (= one existing sp.* table):
  source        a table / view of the source, or a validated SELECT (joins / aggregations, e.g. EKKO + EKPO + EKET)
  field_map     {canonical_field: {"source": column | None, "transform": name, "arg": value}}
  record ids    source columns that identify the source record (stored as _source_record_id for lineage)
  watermark     source column for incremental loads (>= last watermark; loads are idempotent)
  column_policy {source_column: keep | hash | exclude} - REQUIRED for every mapped sensitive column; applied BEFORE anything is stored.
                Unmapped source columns are never stored anywhere.
  mode          STAGING (validate + stage + reconcile only; never touches KPI tables) or PUBLISH (upsert into the sp table).
                PUBLISH changes KPI input data, so it always needs a second ADMIN's approval (four-eyes); editing what is loaded
                resets the approval.
Transforms are a fixed, safe list (no expressions / eval). Values are finally cast to the REAL target column type; anything that
does not fit goes to quarantine with a plain-language reason.
"""
from __future__ import annotations

import datetime as dt
import difflib
import hashlib
import hmac
import json
import re

from sqlalchemy import text

from .dbx import engine
from . import registry as REG
from .canonical import CANONICAL, LINEAGE_COLS

MODES = ("STAGING", "PUBLISH")
POLICIES = ("keep", "hash", "exclude")
TRANSFORMS = {
    "none": "Use the value as it is",
    "trim": "Remove leading / trailing spaces",
    "upper": "UPPER CASE", "lower": "lower case",
    "strip_zeros": "Remove leading zeros (SAP ALPHA, e.g. 0000100001 -> 100001)",
    "to_date": "Date from YYYY-MM-DD, YYYYMMDD (SAP), DD.MM.YYYY or a timestamp",
    "to_number": "Number (accepts 1,234.50)", "to_integer": "Whole number",
    "divide": "Divide by a number (e.g. SAP item 10 -> 1 with 10)", "multiply": "Multiply by a number (e.g. currency rate)",
    "constant": "Fixed value (no source column)", "map": "Translate values with a lookup table, e.g. {\"X\": \"CLOSED\"}",
    "hash": "Pseudonymise (keyed hash) - joinable but not readable",
}

# SAP / Oracle field names that usually mean a canonical field (per entity first, then generic)
SYN_ENTITY = {
    "po_line": {"po_id": ["ebeln", "segment1", "po_number", "po_header_id"], "po_line": ["ebelp", "line_num", "po_item"],
                "supplier_id": ["lifnr", "vendor_id", "vendor_num"], "material_id": ["matnr", "item_id", "item_number"],
                "plant_id": ["werks", "werk", "ship_to_organization_id"], "po_date": ["bedat", "creation_date", "order_date"],
                "promised_date": ["eindt", "promised_date", "need_by_date", "slfdt"], "receipt_date": ["gr_date", "last_gr", "budat", "last_receipt_date"],
                "ordered_qty": ["menge", "quantity", "order_qty"], "received_qty": ["wemng", "quantity_received", "gr_qty"],
                "unit_price_inr": ["netpr", "unit_price", "price"], "currency": ["waers", "currency_code"],
                "po_status": ["zzpostat", "po_status", "closed_code", "status"], "incoterm": ["inco1", "incoterm", "fob_lookup_code"]},
    "grn": {"grn_id": ["mblnr", "belnr", "receipt_num"], "received_qty": ["menge", "erfmg", "quantity"], "receipt_date": ["budat", "budat_mkpf", "transaction_date"],
            "po_id": ["ebeln"], "po_line": ["ebelp"], "plant_id": ["werks"]},
    "incoming_inspection": {"inspection_lot_id": ["prueflos"], "inspected_qty": ["losmenge"], "accepted_qty": ["lmenge01"],
                            "rejected_qty": ["lmenge04", "lmenge02"], "usage_decision": ["vcode"], "inspection_date": ["vdatum", "enstehdat"],
                            "supplier_id": ["lifnr"], "material_id": ["matnr"], "plant_id": ["werk", "werks"], "po_id": ["ebeln"], "po_line": ["ebelp"],
                            "grn_id": ["mblnr"]},
    "schedule_line": {"agreement_id": ["ebeln"], "schedule_line": ["etenr"], "delivery_date": ["eindt"], "scheduled_qty": ["menge"],
                      "received_qty": ["wemng"]},
    "supplier": {"supplier_id": ["lifnr", "vendor_id"], "supplier_name": ["name1", "vendor_name"], "country": ["land1", "country"]},
    "material": {"material_id": ["matnr", "item_id"], "material_name": ["maktx", "description"], "material_category": ["matkl", "category"],
                 "standard_price_inr": ["stprs", "verpr", "item_cost"], "lead_time_days": ["plifz", "lead_time"]},
}
WATERMARK_HINTS = re.compile(r"^(aedat|cpudt\w*|changed_at|updated_at|last_update_date|last_updated|modified\w*|udate|timestamp|ts|_loaded_at)$", re.I)


# ------------------------------------------------------------------ helpers
_TARGET_CACHE: dict = {}


def target_columns(table: str, schema: str = "sp") -> dict:
    """{column: (data_type, nullable, has_default)} from the catalogue (cached)."""
    k = (schema, table)
    if k not in _TARGET_CACHE:
        with engine.connect() as c:
            rows = c.execute(text("""SELECT column_name, data_type, is_nullable = 'YES', column_default IS NOT NULL FROM information_schema.columns
                                     WHERE table_schema = :s AND table_name = :t"""), {"s": schema, "t": table}).fetchall()
        if not rows:
            raise ValueError(f"target table {schema}.{table} does not exist")
        _TARGET_CACHE[k] = {r[0]: (r[1], r[2], r[3]) for r in rows}
    return _TARGET_CACHE[k]


def entity(name: str) -> dict:
    e = CANONICAL.get(name)
    if not e:
        raise ValueError(f"unknown canonical entity '{name}'")
    return e


_PEPPER: list = []


def _pepper() -> bytes:
    """Keyed-hash secret for pseudonymisation; created once and kept in the vault (never in config / code)."""
    if _PEPPER:
        return _PEPPER[0]
    import secrets as pysecrets
    from .secrets import vault
    with engine.begin() as c:
        ref = c.execute(text("SELECT value FROM integration.settings WHERE key = 'mask_pepper_ref'")).scalar()
        if not ref:
            ref = vault().put({"pepper": pysecrets.token_hex(32)}, "system")
            c.execute(text("INSERT INTO integration.settings (key, value) VALUES ('mask_pepper_ref', :r)"), {"r": ref})
    _PEPPER.append(bytes.fromhex(vault().get(ref)["pepper"]))
    return _PEPPER[0]


def pseudonymise(v) -> str | None:
    if v is None or v == "":
        return None
    return "h:" + hmac.new(_pepper(), str(v).encode(), hashlib.sha256).hexdigest()[:32]


# ------------------------------------------------------------------ transforms and casting
class CastError(ValueError):
    pass


def _num(v) -> float:
    if isinstance(v, bool):
        raise CastError("is a yes/no value, not a number")
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace(",", "")
    if s.endswith("-") and s[:-1].replace(".", "", 1).isdigit():          # SAP trailing minus: 12.50-
        s = "-" + s[:-1]
    try:
        return float(s)
    except ValueError:
        raise CastError(f"'{v}' is not a number") from None


_ISO_D = re.compile(r"\d{4}-\d{2}-\d{2}")
_SAP_D = re.compile(r"\d{8}")


def _date(v) -> str | None:
    if isinstance(v, (dt.date, dt.datetime)):
        return (v.date() if isinstance(v, dt.datetime) else v).isoformat()
    s = str(v).strip()
    if s in ("", "00000000", "0000-00-00"):
        return None
    try:                                                    # fast paths (C-level parsing): ISO date / timestamp, SAP DATS
        if _ISO_D.match(s):
            return dt.date.fromisoformat(s[:10]).isoformat()
        if _SAP_D.fullmatch(s):
            return dt.date(int(s[:4]), int(s[4:6]), int(s[6:])).isoformat()
    except ValueError:
        raise CastError(f"'{v}' is not a valid date") from None
    for fmt in ("%Y-%m-%d", "%Y%m%d", "%d.%m.%Y", "%d/%m/%Y"):
        try:
            return dt.datetime.strptime(s[:10] if fmt == "%Y-%m-%d" else s, fmt).date().isoformat()
        except ValueError:
            continue
    raise CastError(f"'{v}' is not a date (expected YYYY-MM-DD, YYYYMMDD or DD.MM.YYYY)")


def _ts(v) -> str | None:
    if isinstance(v, dt.datetime):
        return v.isoformat()
    s = str(v).strip()
    if not s:
        return None
    try:
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).isoformat()
    except ValueError:
        d = _date(s)
        return f"{d}T00:00:00" if d else None


def apply_transform(v, spec: dict):
    t, arg = (spec.get("transform") or "none"), spec.get("arg")
    if t == "constant":
        return arg
    if v is None:
        return None
    if t == "none":
        return v
    if t == "trim":
        return str(v).strip()
    if t == "upper":
        return str(v).upper()
    if t == "lower":
        return str(v).lower()
    if t == "strip_zeros":
        s = str(v).strip()
        return (s.lstrip("0") or "0") if s.isdigit() else s
    if t == "to_date":
        return _date(v)
    if t == "to_number":
        return _num(v)
    if t == "to_integer":
        n = _num(v)
        if n != int(n):
            raise CastError(f"{v} is not a whole number")
        return int(n)
    if t in ("divide", "multiply"):
        k = _num(arg)
        if t == "divide" and k == 0:
            raise CastError("divide by zero")
        return _num(v) / k if t == "divide" else _num(v) * k
    if t == "map":
        table = arg if isinstance(arg, dict) else json.loads(arg or "{}")
        return table.get(str(v), v)
    if t == "hash":
        return pseudonymise(v)
    raise CastError(f"unknown transform '{t}'")


INT_T = {"integer", "bigint", "smallint"}
NUM_T = {"numeric", "double precision", "real"}
TS_T = {"timestamp without time zone", "timestamp with time zone"}
TEXT_T = {"text", "character varying", "character"}
SQL_TYPE = {"integer": "integer", "bigint": "bigint", "smallint": "smallint", "numeric": "numeric", "double precision": "double precision",
            "real": "real", "date": "date", "timestamp without time zone": "timestamp", "timestamp with time zone": "timestamptz",
            "text": "text", "character varying": "varchar", "character": "text", "boolean": "boolean"}


def cast(v, data_type: str):
    """Cast to the REAL target column type (JSON-safe result)."""
    if v is None or (isinstance(v, str) and v.strip() == "" and data_type not in TEXT_T):
        return None
    if data_type in INT_T:
        n = _num(v)
        if n != int(n):
            raise CastError(f"{v} is not a whole number")
        return int(n)
    if data_type in NUM_T:
        return _num(v)
    if data_type == "date":
        return _date(v)
    if data_type in TS_T:
        return _ts(v)
    if data_type == "boolean":
        s = str(v).strip().upper()
        if s in ("TRUE", "T", "1", "X", "Y", "YES"):
            return True
        if s in ("FALSE", "F", "0", "", "N", "NO"):
            return False
        raise CastError(f"'{v}' is not yes/no")
    return str(v)


# ------------------------------------------------------------------ source metadata (approved connection only)
def source_columns(con, kind: str, obj: str | None, query: str | None) -> list[dict]:
    """[{name, type, sensitive}] - metadata only, no data rows. For queries, sensitivity follows derivation (aliases)."""
    if kind == "object":
        o = next((x for x in con.discover_schema() if x.name == obj), None)
        if not o:
            raise ValueError(f"{obj} was not found in the source (or this account cannot read it)")
        return [{"name": f.name, "type": f.type, "sensitive": bool(f.sensitive), "key": bool(f.key)} for f in o.fields]
    if kind == "query":
        if not hasattr(con, "query_columns"):
            raise ValueError("query sources are only supported for database connectors")
        from .sqlsafe import tainted_outputs, validate_select
        q = validate_select(query, con.key)
        taint = tainted_outputs(q, con.key, con.is_sensitive)
        cols = con.query_columns(q)
        return [{"name": c, "type": "", "sensitive": con.is_sensitive(c) or c.lower() in taint or f"#{i}" in taint, "key": False}
                for i, c in enumerate(cols)]
    raise ValueError("source kind must be 'object' or 'query'")


def suggest(ent: str, cols: list[dict]) -> dict:
    """Rule-based suggestions (SAP / Oracle dictionary + name similarity). No data and no external service involved.
    Every suggestion must be confirmed by a person before it is saved."""
    e = entity(ent)
    names = {c["name"].lower(): c["name"] for c in cols}
    syn = SYN_ENTITY.get(ent, {})
    tcols = target_columns(e["table"])
    fmap, used = {}, set()
    for f, (ftype, req, desc) in e["fields"].items():
        pick, why, conf = None, "", 0.0
        for s in syn.get(f, []):
            if s in names and names[s] not in used:
                pick, why, conf = names[s], f"standard field name ({s.upper()})", 0.95
                break
        if not pick and f in names and names[f] not in used:
            pick, why, conf = names[f], "same name", 0.9
        if not pick:
            best = max(((difflib.SequenceMatcher(None, f, n).ratio(), n) for n in names if names[n] not in used), default=(0, None))
            if best[0] >= 0.75:
                pick, why, conf = names[best[1]], f"similar name ({best[0]:.0%})", round(best[0] * 0.8, 2)
        if not pick:
            continue
        used.add(pick)
        dtype = tcols.get(f, ("text",))[0]
        tr, arg = "none", None
        if pick.lower() == "ebelp" and dtype in INT_T:
            tr, arg, why = "divide", 10, why + "; SAP items step by 10 (10, 20 ...) - check against your data"
        elif dtype == "date":
            tr = "to_date"
        elif dtype in INT_T:
            tr = "to_integer"
        elif dtype in NUM_T:
            tr = "to_number"
        fmap[f] = {"source": pick, "transform": tr, "arg": arg, "confidence": conf, "reason": why}
    key_src = [fmap[k]["source"] for k in e["key"] if k in fmap]
    wm = [c["name"] for c in cols if WATERMARK_HINTS.match(c["name"])]
    sens = [c["name"] for c in cols if c.get("sensitive")]
    return {"field_map": fmap, "record_id_columns": key_src, "watermark_candidates": wm, "sensitive_columns": sens,
            "unmapped_required": [f for f, (_, req, _) in e["fields"].items() if req and f not in fmap],
            "note": "Suggestions only - review every row. Nothing is saved until you confirm."}


# ------------------------------------------------------------------ validation
def validate(m: dict, cols: list[dict]) -> tuple[list[str], list[str]]:
    """(errors, warnings) for a mapping against the source columns and the real target table."""
    errs, warns = [], []
    e = entity(m["entity"])
    tcols = target_columns(e["table"])
    by = {c["name"]: c for c in cols}
    fmap, pol = m.get("field_map") or {}, m.get("column_policy") or {}
    for f, spec in fmap.items():
        if f not in e["fields"]:
            errs.append(f"'{f}' is not a field of {e['label']}")
            continue
        if f not in tcols:
            errs.append(f"'{f}' does not exist in sp.{e['table']}")
        t = spec.get("transform") or "none"
        if t not in TRANSFORMS:
            errs.append(f"{f}: unknown transform '{t}'")
        if t == "constant":
            continue
        src = spec.get("source")
        if not src:
            errs.append(f"{f}: choose a source column (or use a constant)")
        elif src not in by:
            errs.append(f"{f}: source column '{src}' does not exist")
        elif by[src]["sensitive"] and pol.get(src) not in POLICIES:
            errs.append(f"{src} looks sensitive (price / personal data) and is mapped to {f}: choose Keep, Hash or Exclude before it can be stored")
        if t in ("divide", "multiply"):
            try:
                float(spec.get("arg"))
            except (TypeError, ValueError):
                errs.append(f"{f}: '{t}' needs a number")
    for f, (_, req, _) in e["fields"].items():
        spec = fmap.get(f)
        if req and not spec:
            errs.append(f"required field '{f}' ({e['fields'][f][2]}) is not mapped")
        elif req and spec and spec.get("transform") != "constant" and pol.get(spec.get("source")) == "exclude":
            errs.append(f"required field '{f}' is mapped from an EXCLUDED column")
    for k in e["key"]:
        if k not in fmap:
            errs.append(f"key field '{k}' must be mapped (it identifies the record for idempotent loads)")
    for col, (dtype, nullable, has_default) in tcols.items():
        if not nullable and not has_default and col not in fmap and not col.startswith("_"):
            errs.append(f"sp.{e['table']}.{col} cannot be empty and is not mapped")
    for c in m.get("record_id_columns") or []:
        if c not in by:
            errs.append(f"record-id column '{c}' does not exist")
    if m.get("watermark_column") and m["watermark_column"] not in by:
        errs.append(f"watermark column '{m['watermark_column']}' does not exist")
    if m.get("mode") not in MODES:
        errs.append("mode must be STAGING or PUBLISH")
    for c, p in pol.items():
        if p not in POLICIES:
            errs.append(f"{c}: policy must be keep, hash or exclude")
        elif p == "hash" and any(s.get("source") == c and tcols.get(f, ("text",))[0] not in TEXT_T for f, s in fmap.items()):
            errs.append(f"{c}: only text fields can hold a pseudonymised (hashed) value")
    if not m.get("watermark_column"):
        warns.append("No watermark column: every run reads the whole source (fine for small tables).")
    if not m.get("record_id_columns"):
        warns.append("No record-id columns: lineage will use the canonical key.")
    for c in cols:
        if c["sensitive"] and pol.get(c["name"]) == "keep":
            warns.append(f"{c['name']} (sensitive) will be stored as-is - confirmed by policy 'keep'.")
    try:
        parse_schedule(m.get("schedule") or "manual")
    except ValueError as ex:
        errs.append(str(ex))
    return errs, warns


# ------------------------------------------------------------------ row transformation
def transform_rows(m: dict, rows: list[dict], cols: list[dict]) -> tuple[list[tuple], list[tuple]]:
    """-> good [(record_key, source_record_id, record)], bad [(source_record_id, masked mapped source values, errors)].
    Column policy is applied FIRST (exclude -> dropped, hash -> pseudonymised); unmapped columns are never kept."""
    e = entity(m["entity"])
    tcols = target_columns(e["table"])
    pol, fmap = m.get("column_policy") or {}, m.get("field_map") or {}
    sens = {c["name"] for c in cols if c["sensitive"]}
    mapped_src = {s["source"] for s in fmap.values() if s.get("source")}
    rid_cols = m.get("record_id_columns") or []
    good, bad = [], []
    for r in rows:
        src = {}
        for c in mapped_src | set(rid_cols):
            v = r.get(c)
            p = pol.get(c)
            src[c] = None if p == "exclude" else pseudonymise(v) if p == "hash" else v
        rec, errs = {}, []
        for f, spec in fmap.items():
            try:
                v = apply_transform(src.get(spec.get("source")) if spec.get("transform") != "constant" else None, spec)
                rec[f] = cast(v, tcols[f][0])
            except CastError as ex:
                errs.append(f"{f}: {ex}")
        for f, (_, req, _) in e["fields"].items():
            if req and f in fmap and rec.get(f) is None and not any(x.startswith(f + ":") for x in errs):
                errs.append(f"{f}: required value is empty")
        srid = "|".join(str(src.get(c)) for c in rid_cols) if rid_cols else "|".join(str(rec.get(k)) for k in e["key"])
        if errs:
            shown = {c: ("•••" if c in sens and v is not None else v) for c, v in src.items()}
            bad.append((srid, shown, errs))
            continue
        good.append((json.dumps([rec.get(k) for k in e["key"]], default=str), srid, rec))
    return good, bad


# ------------------------------------------------------------------ schedules
def parse_schedule(s: str) -> tuple[str, int | str | None]:
    s = (s or "manual").strip().lower()
    if s == "manual":
        return "manual", None
    if s == "hourly":
        return "every", 60
    m = re.fullmatch(r"every:(\d{1,4})", s)
    if m:
        n = int(m.group(1))
        if n < 5:
            raise ValueError("schedule: at most every 5 minutes")
        return "every", n
    m = re.fullmatch(r"daily:([01]\d|2[0-3]):([0-5]\d)", s)
    if m:
        return "daily", f"{m.group(1)}:{m.group(2)}"
    raise ValueError("schedule must be manual, hourly, every:<minutes> or daily:HH:MM")


def next_run(schedule: str, after: dt.datetime | None = None) -> dt.datetime | None:
    kind, arg = parse_schedule(schedule)
    now = after or dt.datetime.now(dt.timezone.utc)
    if kind == "manual":
        return None
    if kind == "every":
        return now + dt.timedelta(minutes=int(arg))
    import zoneinfo
    tz = zoneinfo.ZoneInfo("Asia/Kolkata")                          # plant time (IST)
    h, mi = map(int, str(arg).split(":"))
    local = now.astimezone(tz)
    nxt = local.replace(hour=h, minute=mi, second=0, microsecond=0)
    if nxt <= local:
        nxt += dt.timedelta(days=1)
    return nxt.astimezone(dt.timezone.utc)


# ------------------------------------------------------------------ storage (CRUD + four-eyes)
LOAD_KEYS = ("connection_id", "entity", "source_kind", "source_object", "source_query", "field_map", "record_id_columns",
             "watermark_column", "column_policy", "mode")


def _row(r) -> dict:
    d = dict(r._mapping) if hasattr(r, "_mapping") else dict(r)
    d["target"] = f"sp.{d['target_table']}"
    return d


def list_mappings() -> list[dict]:
    with engine.connect() as c:
        return [_row(r) for r in c.execute(text("SELECT * FROM integration.mappings WHERE active ORDER BY id"))]


def get_mapping(mid: str) -> dict:
    with engine.connect() as c:
        r = c.execute(text("SELECT * FROM integration.mappings WHERE id = :i AND active"), {"i": mid}).fetchone()
    if not r:
        raise LookupError("mapping not found")
    return _row(r)


def _clean_input(d: dict) -> dict:
    fm = {}
    for f, s in (d.get("field_map") or {}).items():
        if not s or (not s.get("source") and s.get("transform") != "constant"):
            continue
        fm[f] = {"source": s.get("source") or None, "transform": s.get("transform") or "none", "arg": s.get("arg")}
    return {**d, "field_map": fm, "column_policy": {k: v for k, v in (d.get("column_policy") or {}).items() if v},
            "record_id_columns": list(d.get("record_id_columns") or []), "mode": (d.get("mode") or "STAGING").upper(),
            "schedule": (d.get("schedule") or "manual").strip().lower(), "source_kind": d.get("source_kind") or "object"}


def check(d: dict) -> tuple[dict, list[dict], list[str], list[str]]:
    """Resolve the (approved) connection, read source METADATA and validate. -> (clean mapping, columns, errors, warnings)."""
    from . import service as S
    d = _clean_input(d)
    _, con = S.connector_for(d["connection_id"])
    cols = source_columns(con, d["source_kind"], d.get("source_object"), d.get("source_query"))
    errs, warns = validate(d, cols)
    return d, cols, errs, warns


def create_mapping(d: dict, actor: str) -> dict:
    from . import service as S
    d, _, errs, _ = check(d)
    if errs:
        raise ValueError("; ".join(errs))
    e = entity(d["entity"])
    pub = d["mode"] == "PUBLISH"
    with engine.begin() as c:
        n = c.execute(text("SELECT COUNT(*) FROM integration.mappings")).scalar() + 1
        mid = f"MAP-{n:04d}"
        c.execute(text("""INSERT INTO integration.mappings (id, name, connection_id, entity, target_table, source_kind, source_object, source_query,
                          field_map, record_id_columns, watermark_column, column_policy, mode, approval_status, requested_by, schedule, enabled,
                          next_run_at, max_quarantine_pct, max_rows, created_by)
                          VALUES (:i, :n, :c, :e, :t, :k, :o, :q, CAST(:fm AS jsonb), CAST(:rid AS jsonb), :w, CAST(:p AS jsonb), :m, :ap, :a, :s, :en,
                          :nr, :mq, :mr, :a)"""),
                  {"i": mid, "n": (d.get("name") or f"{e['label']} from {d['connection_id']}").strip(), "c": d["connection_id"], "e": d["entity"],
                   "t": e["table"], "k": d["source_kind"], "o": d.get("source_object"), "q": d.get("source_query"), "fm": json.dumps(d["field_map"]),
                   "rid": json.dumps(d["record_id_columns"]), "w": d.get("watermark_column") or None, "p": json.dumps(d["column_policy"]),
                   "m": d["mode"], "ap": "PENDING" if pub else "NONE", "a": actor, "s": d["schedule"], "en": bool(d.get("enabled", True)),
                   "nr": next_run(d["schedule"]), "mq": float(d.get("max_quarantine_pct") or 5), "mr": int(d.get("max_rows") or 200000)})
    S.audit(actor, "mapping:create", d["connection_id"], {"mapping": mid, "entity": d["entity"], "mode": d["mode"], "fields": sorted(d["field_map"]),
                                                          "column_policy": d["column_policy"]})
    return get_mapping(mid)


def update_mapping(mid: str, d: dict, actor: str) -> dict:
    from . import service as S
    cur = get_mapping(mid)
    merged = {**{k: cur[k] for k in (*LOAD_KEYS, "name", "schedule", "enabled", "max_quarantine_pct", "max_rows")}, **{k: v for k, v in d.items() if v is not None}}
    merged, _, errs, _ = check(merged)
    if errs:
        raise ValueError("; ".join(errs))
    changed = [k for k in LOAD_KEYS if json.dumps(merged.get(k), sort_keys=True, default=str) != json.dumps(cur.get(k), sort_keys=True, default=str)]
    reset = merged["mode"] == "PUBLISH" and bool(changed)
    approval = ("PENDING" if reset else cur["approval_status"]) if merged["mode"] == "PUBLISH" else "NONE"
    with engine.begin() as c:
        c.execute(text("""UPDATE integration.mappings SET name = :n, source_kind = :k, source_object = :o, source_query = :q, field_map = CAST(:fm AS jsonb),
                          record_id_columns = CAST(:rid AS jsonb), watermark_column = :w, column_policy = CAST(:p AS jsonb), mode = :m,
                          approval_status = CAST(:ap AS varchar), requested_by = CASE WHEN :reset THEN CAST(:a AS varchar) ELSE requested_by END,
                          approved_by = CASE WHEN :reset OR CAST(:ap AS varchar) = 'NONE' THEN NULL ELSE approved_by END,
                          schedule = :s, enabled = :en, next_run_at = :nr, max_quarantine_pct = :mq, max_rows = :mr,
                          version = version + CASE WHEN :chg THEN 1 ELSE 0 END, updated_at = now() WHERE id = :i"""),
                  {"n": merged.get("name") or cur["name"], "k": merged["source_kind"], "o": merged.get("source_object"), "q": merged.get("source_query"),
                   "fm": json.dumps(merged["field_map"]), "rid": json.dumps(merged["record_id_columns"]), "w": merged.get("watermark_column") or None,
                   "p": json.dumps(merged["column_policy"]), "m": merged["mode"], "ap": approval, "reset": reset, "a": actor, "s": merged["schedule"],
                   "en": bool(merged.get("enabled", True)), "nr": next_run(merged["schedule"]), "mq": float(merged.get("max_quarantine_pct") or 5),
                   "mr": int(merged.get("max_rows") or 200000), "chg": bool(changed), "i": mid})
    S.audit(actor, "mapping:update", cur["connection_id"], {"mapping": mid, "changed": changed, "approval_reset": reset})
    return get_mapping(mid)


def delete_mapping(mid: str, actor: str) -> None:
    from . import service as S
    cur = get_mapping(mid)
    with engine.begin() as c:
        c.execute(text("UPDATE integration.mappings SET active = FALSE, enabled = FALSE, updated_at = now() WHERE id = :i"), {"i": mid})
    S.audit(actor, "mapping:delete", cur["connection_id"], {"mapping": mid})


def decide_mapping(mid: str, decision: str, actor: str, note: str | None = None) -> dict:
    """Four-eyes: publishing into KPI tables needs a second ADMIN (approver != requester)."""
    from . import service as S
    cur = get_mapping(mid)
    decision = (decision or "").upper()
    if decision not in ("APPROVED", "REJECTED"):
        raise ValueError("decision must be APPROVED or REJECTED")
    if cur["approval_status"] != "PENDING":
        raise PermissionError(f"mapping is {cur['approval_status']}, nothing to approve")
    if actor == cur["requested_by"]:
        raise PermissionError("four-eyes rule: the requester cannot approve their own publish mapping")
    with engine.begin() as c:
        c.execute(text("""UPDATE integration.mappings SET approval_status = CAST(:d AS varchar), approved_by = :a, approved_at = now(), updated_at = now()
                          WHERE id = :i"""), {"d": decision, "a": actor, "i": mid})
    S.audit(actor, f"mapping:{decision.lower()}", cur["connection_id"], {"mapping": mid, "requested_by": cur["requested_by"], "note": note,
                                                                         "version": cur["version"]})
    return get_mapping(mid)


LINEAGE = [c for c, _ in LINEAGE_COLS]
