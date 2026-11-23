"""LangChain tools available to the Supplier Performance Agent.

Every tool returns a string (JSON where structured). The docstring is what the model sees.
Controls: the agent can PROPOSE corrective actions and send notifications, but it cannot
approve actions, change sourcing, or write to business data.
"""
import datetime as dt
import json
import re

import httpx
from langchain_core.tools import tool
from sqlalchemy import inspect, text

from . import analytics as A
from . import auth as AU
from . import metrics as MX
from .config import settings
from .features import is_on
from .db import CorrectiveAction, Notification, SessionLocal, now, ro_engine
from .domo_llm import domo_headers, domo_url

MAX_OUT = 6000
FORBIDDEN = re.compile(
    r"\b(insert|update|delete|drop|alter|truncate|create|grant|revoke|copy|call|do|vacuum|"
    r"merge|reindex|cluster|comment|lock|listen|notify|set|reset|pg_sleep|dblink)\b", re.I)


def _dump(obj, limit: int = MAX_OUT) -> str:
    s = json.dumps(obj, default=str, separators=(",", ":"))
    return s if len(s) <= limit else s[:limit] + f'..."(truncated, {len(s)} chars total)"'


class AmbiguousSupplier(ValueError):
    pass


def resolve_supplier(ref: str) -> str | None:
    """Master-data resolution (G2): supplier ID -> alias table -> exact name -> unique partial name.
    Ambiguous or unknown references raise instead of silently picking one supplier."""
    ref = (ref or "").strip()
    if not ref:
        return None
    if re.fullmatch(r"(?i)sup-\d{3}", ref):
        sid = ref.upper()
        if not A._run(f"SELECT 1 FROM {A.S}.dim_supplier WHERE supplier_id = :s", {"s": sid}):
            raise ValueError(f"Unknown supplier ID {sid}")
        return sid
    hit = A._run("SELECT supplier_id FROM public.supplier_aliases WHERE alias = :a", {"a": ref.lower()})
    if hit:
        return hit[0]["supplier_id"]
    exact = A._run(f"SELECT supplier_id FROM {A.S}.dim_supplier WHERE lower(supplier_name) = lower(:n)", {"n": ref})
    if len(exact) == 1:
        return exact[0]["supplier_id"]
    rows = A._run(f"SELECT supplier_id, supplier_name FROM {A.S}.dim_supplier WHERE supplier_name ILIKE :n ORDER BY supplier_id",
                  {"n": f"%{ref}%"})
    if len(rows) == 1:
        return rows[0]["supplier_id"]
    if len(rows) > 1:
        raise AmbiguousSupplier(f"'{ref}' matches several suppliers: "
                                + "; ".join(f"{r['supplier_id']} {r['supplier_name']}" for r in rows) + ". Ask the user which one.")
    raise ValueError(f"No supplier matches '{ref}'. Use get_supplier_scorecard() with no supplier to list them.")


_resolve_supplier = resolve_supplier


def _require(*roles: str) -> None:
    if not AU.has_role(*roles):
        u = AU.current_user()
        raise PermissionError(f"Role {u.role if u else 'anonymous'} may not use this tool (needs {' or '.join(roles)}).")


def _resolve_material(ref: str) -> str | None:
    ref = (ref or "").strip()
    if re.fullmatch(r"(?i)mat-[a-z]+-\d{3}", ref):
        return ref.upper()
    rows = A.query(f"SELECT material_id FROM {A.S}.dim_material WHERE material_name ILIKE :n "
                   f"OR material_category ILIKE :n LIMIT 1", n=f"%{ref}%")
    return rows[0]["material_id"] if rows else ref


# ---------------------------------------------------------------- data discovery / SQL
@tool
def get_schema() -> str:
    """List the supplier-performance tables and KPI views (schema sp) with their columns. Call before writing SQL."""
    insp = inspect(ro_engine)
    lines = []
    safe = is_on("safe_query_layer")
    if safe:
        lines.append("SAFE QUERY LAYER ON: prefer list_metrics / query_metric / compare_metric_periods. run_sql may read ONLY these "
                     "approved views and master tables.")
    for kind, names in (("TABLE", insp.get_table_names(schema=A.S)), ("VIEW", insp.get_view_names(schema=A.S))):
        for t in sorted(names):
            if t == "meta" or (safe and t not in SAFE_OBJECTS):
                continue
            cols = ", ".join(c["name"] + ":" + str(c["type"]).split("(")[0] for c in insp.get_columns(t, schema=A.S))
            lines.append(f"{kind} sp.{t}({cols})")
    lines.append("FUNCTION sp.as_of_date() -> reference date for ageing/past-due")
    lines.append("NOTES: v_po_delivery.measurable=true rows are used for OTIF. In v_defect_attribution and "
                 "v_ncr_supplier, SUM(attribution_weight) counts defects/NCRs per supplier. "
                 "PPM = defects / received_qty * 1e6.")
    return "\n".join(lines)


SAFE_OBJECTS = {"v_po_delivery", "v_defect_attribution", "v_incoming_quality", "v_ncr_supplier", "v_supply_disruption",
                "v_material_supplier_share", "v_schedule_delivery", "v_invoice_match", "dim_supplier", "dim_material", "dim_plant",
                "dim_defect_code", "dim_product", "dim_production_line"}


@tool
def run_sql(query: str) -> str:
    """Run ONE read-only PostgreSQL SELECT (schema sp; tables may be referenced without the sp. prefix). Returns up to 50 rows as JSON."""
    q = query.strip().rstrip(";").strip()
    q = re.sub(r"^```(sql)?|```$", "", q, flags=re.I).strip()
    if ";" in q or not re.match(r"^(select|with)\b", q, re.I) or FORBIDDEN.search(q):
        return "ERROR: only a single read-only SELECT is allowed."
    try:
        with ro_engine.connect() as conn:
            conn.execute(text("SET TRANSACTION READ ONLY"))
            conn.execute(text("SET LOCAL statement_timeout = 8000"))
            if is_on("safe_query_layer"):            # enforced by Postgres: agent_safe can read approved views only
                conn.execute(text("SET LOCAL ROLE agent_safe"))
            conn.execute(text(f"SET LOCAL search_path = {A.S}, public"))
            res = conn.execute(text(f"SELECT * FROM ({q}) AS t LIMIT 50"))
            cols = list(res.keys())
            rows = [{k: A._clean(v) for k, v in zip(cols, r)} for r in res.fetchall()]
            return _dump(rows) if rows else "No rows."
    except Exception as e:
        return f"ERROR: {str(e).splitlines()[0][:400]}"


# ---------------------------------------------------------------- analytics
SUMMARY_KEYS = ("supplier_id", "supplier_name", "material_category", "risk_tier", "grade", "composite_score",
                "performance_rank", "risk_score", "risk_rank", "otif_pct", "incoming_ppm",
                "avg_lead_time_variance_days", "overdue_capa", "open_ncr", "price_variance_pct",
                "disruption_hours", "past_due_lines", "scores")


@tool
def get_supplier_scorecard(supplier: str = "", plant_id: str = "", start_date: str = "", end_date: str = "") -> str:
    """Supplier scorecard: OTIF, on-time, in-full, lead-time adherence/variance, incoming PPM, NCR/CAPA ageing,
    price variance, RFQ score, disruption hours, weighted composite score, grade (A-D), performance and risk rank.
    supplier = ID or name (empty = all suppliers, summarised). Dates are YYYY-MM-DD (optional)."""
    sid = _resolve_supplier(supplier)
    rows = A.scorecard(start_date or None, end_date or None, plant_id or None, sid)
    if not sid:
        rows = [{k: r[k] for k in SUMMARY_KEYS} for r in rows]
    return _dump({"as_of_date": A.as_of_date(), "weights": A.get_scorecard_config()["weights"], "suppliers": rows})


@tool
def get_portfolio_overview() -> str:
    """Portfolio-level KPIs across all suppliers: spend, OTIF, PPM, lead-time variance, past-due lines, open NCR,
    overdue CAPA, disruption hours, grade mix."""
    return _dump(A.overview())


@tool
def get_supplier_trends(supplier: str = "", grain: str = "month") -> str:
    """Time series of OTIF %, average delay, lead-time variance, attributed defects and PPM. grain = week|month.
    supplier = ID or name (empty = all suppliers combined)."""
    rows = [r for r in A.trends(_resolve_supplier(supplier), grain) if r["lines"] or r["defects"]]
    return _dump(rows)


@tool
def get_risk_alerts(supplier: str = "", min_severity: str = "MEDIUM") -> str:
    """Detect supply risks: deteriorating OTIF, rising defects, repeated late deliveries, repeated defects,
    overdue CAPA, concentration risk, past-due supply and NO_MATERIAL line stoppages. Filter by supplier and min_severity
    (CRITICAL|HIGH|MEDIUM|LOW)."""
    rank = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}
    sid = _resolve_supplier(supplier)
    lim = rank.get(min_severity.upper(), 2)
    rows = [a for a in A.risk_alerts() if rank[a["severity"]] <= lim and (not sid or a["supplier_id"] == sid)]
    return _dump({"count": len(rows), "alerts": rows[:25]})


@tool
def get_supplier_exposure(supplier: str) -> str:
    """Quantify line/customer exposure for a supplier: failed inspections by line, past-due PO lines, open production
    orders and customer orders at risk, and spend share by material."""
    e = A.exposure(_resolve_supplier(supplier))
    e["open_production_orders_at_risk"] = e["open_production_orders_at_risk"][:15]
    return _dump(e)


@tool
def get_open_issues(supplier: str) -> str:
    """Open NCR/CAPA items attributed to a supplier with ageing and overdue flags (oldest/overdue first)."""
    rows = A.supplier_issues(_resolve_supplier(supplier))
    return _dump({"open_items": len(rows), "items": rows[:20]})


@tool
def get_defect_breakdown(supplier: str = "") -> str:
    """Attributed defects by defect code and material (optionally for one supplier)."""
    return _dump(A.defect_breakdown(_resolve_supplier(supplier))[:30])


@tool
def get_sourcing_review(material: str = "", supplier: str = "") -> str:
    """SHOULD WE MOVE A MATERIAL TO ANOTHER SUPPLIER: when a supplier keeps failing on a material (repeat defects, unreliable delivery,
    prices over budget) this returns the sourcing-review case: why to override the current supplier, the best qualified alternative,
    a side-by-side comparison, the alternative's advantages, trade-offs, expected impact and a trial split. Filter by material and/or
    supplier; empty = the top cases. Returns `brief` (ready markdown): present it as it is. It is a SUGGESTION for procurement."""
    from . import sourcing_review as SR
    cases = SR.build()
    sid = _resolve_supplier(supplier) if supplier else None
    if material:
        k = material.strip().lower()
        cases = [c for c in cases if k in c["material_name"].lower() or k == c["material_id"].lower()]
    if sid:
        cases = [c for c in cases if c["incumbent"]["supplier_id"] == sid or (c["alternative"] or {}).get("supplier_id") == sid]
    if not cases:
        return _dump({"brief": "No sourcing review is needed for that selection: no supplier is failing on it badly enough to suggest a switch."})
    top = cases[:3]
    return _dump({"cases": [{k: c[k] for k in ("material_name", "severity", "triggers", "impact", "action")} | {"current": c["incumbent"]["supplier_name"],
                  "suggested": (c["alternative"] or {}).get("supplier_name")} for c in top], "brief": SR.brief(top[0])})


@tool
def compare_suppliers_for_material(material: str) -> str:
    """Compare all suppliers of one material (ID like MAT-SOC-001 or name): OTIF, price vs standard, lead time,
    PPM, spend share and latest RFQ quote. Decision support only; sourcing decisions stay with procurement.
    Restricted to PROCUREMENT users."""
    _require("PROCUREMENT")
    mid = _resolve_material(material)
    return _dump({"material_id": mid, "suppliers": A.material_sourcing(mid)})


# ---------------------------------------------------------------- actions (controlled)
@tool
def propose_corrective_action(supplier: str, title: str, description: str, category: str = "QUALITY",
                              priority: str = "MEDIUM", owner_role: str = "QUALITY", ncr_id: str = "",
                              due_in_days: int = 14) -> str:
    """Create a corrective-action PROPOSAL for a supplier in the tracker. It is NOT active until a person approves it.
    category = DELIVERY|QUALITY|RESPONSIVENESS|COMMERCIAL|CAPACITY; priority = LOW|MEDIUM|HIGH|CRITICAL;
    owner_role = PROCUREMENT|QUALITY. Only use when the user asks to raise/propose an action."""
    _require("PROCUREMENT", "QUALITY")
    sid = _resolve_supplier(supplier)
    with SessionLocal() as s:
        a = CorrectiveAction(
            supplier_id=sid, ncr_id=ncr_id or None, category=category.upper(), priority=priority.upper(),
            title=title[:200], description=description, owner_role=owner_role.upper(),
            due_date=dt.date.today() + dt.timedelta(days=int(due_in_days)), status="PROPOSED", origin="AGENT",
            history=[{"at": now().isoformat(), "by": f"agent (for {AU.actor()})", "status": "PROPOSED", "note": "Proposed by agent"}])
        s.add(a)
        s.commit()
        return _dump({"action_id": a.id, "status": a.status,
                      "note": "Proposal created. A procurement/quality owner must approve it in the Actions tracker."})


@tool
def list_corrective_actions(supplier: str = "", status: str = "") -> str:
    """List corrective actions in the tracker (optionally filter by supplier and status)."""
    with SessionLocal() as s:
        q = s.query(CorrectiveAction)
        if supplier:
            q = q.filter(CorrectiveAction.supplier_id == _resolve_supplier(supplier))
        if status:
            q = q.filter(CorrectiveAction.status == status.upper())
        rows = [{"id": a.id, "supplier_id": a.supplier_id, "title": a.title, "status": a.status,
                 "priority": a.priority, "due_date": a.due_date, "owner_role": a.owner_role}
                for a in q.order_by(CorrectiveAction.id.desc()).limit(30)]
    return _dump(rows) if rows else "No corrective actions found."


@tool
def notify_owner(recipient_role: str, subject: str, body: str, supplier: str = "", severity: str = "INFO") -> str:
    """Send an in-app notification to the PROCUREMENT or QUALITY owner (e.g. about a risk alert).
    Only use when the user asks to notify someone."""
    _require("PROCUREMENT", "QUALITY")
    with SessionLocal() as s:
        n = Notification(recipient_role=recipient_role.upper(), subject=subject[:200], body=body,
                         supplier_id=_resolve_supplier(supplier) if supplier else None, severity=severity.upper())
        s.add(n)
        s.commit()
        return _dump({"notification_id": n.id, "sent_to": n.recipient_role})


# ---------------------------------------------------------------- Domo Beast Mode (guide Appendix B)
PG_TO_DOMO = {"INTEGER": "LONG", "BIGINT": "LONG", "DOUBLE": "DOUBLE", "NUMERIC": "DECIMAL", "DATE": "DATE",
              "TIMESTAMP": "DATETIME", "BOOLEAN": "STRING"}


@tool
def generate_domo_beastmode(request: str, table: str = "v_po_delivery") -> str:
    """Generate a Domo Beast Mode formula from plain English for a dataset with the same columns as an sp table/view
    (e.g. 'OTIF percentage' on v_po_delivery)."""
    insp = inspect(ro_engine)
    t = table.replace("sp.", "")
    cols = [{"name": c["name"], "type": PG_TO_DOMO.get(str(c["type"]).split("(")[0].split(" ")[0], "STRING")}
            for c in insp.get_columns(t, schema=A.S)]
    payload = {"input": request, "dataSourceSchema": {"dataSourceName": t, "columns": cols}}
    try:
        r = httpx.post(domo_url("/api/ai/v1/text/beastmode"), json=payload, headers=domo_headers(), timeout=120)
        r.raise_for_status()
        return r.json().get("output", "")
    except Exception as e:
        return f"ERROR: {e}"


@tool
def what_if_weights(delivery: float, quality: float, responsiveness: float, commercial: float) -> str:
    """What-if scenario: re-rank suppliers with different scorecard weights (e.g. 60/20/10/10). Nothing is saved;
    returns new score, rank and rank change vs the agreed weights for every supplier."""
    w = {"delivery": delivery, "quality": quality, "responsiveness": responsiveness, "commercial": commercial}
    tot = sum(w.values()) or 1
    rows = A.simulate({k: v / tot for k, v in w.items()})
    return _dump([{k: r[k] for k in ("supplier_id", "supplier_name", "composite_score", "performance_rank",
                                     "baseline_score", "baseline_rank", "rank_change", "grade", "baseline_grade")} for r in rows])


@tool
def get_supplier_twin(supplier: str) -> str:
    """Digital-twin mirror of one supplier: calibrated lead-time distribution (mean/p50/p90), on-time probability,
    fill rate, open pipeline, share of each plant/material flow, and backtest fidelity (predicted vs actual on-time)."""
    from . import twin as T
    st = T.supplier_state_cached(_resolve_supplier(supplier))
    st.get("calibrated", {}).pop("lead_time_histogram", None)
    st.get("calibrated", {}).pop("lead_time_bins", None)
    return _dump(st)


@tool
def run_twin_scenario(supplier: str = "", delay_days: int = 0, capacity_pct: int = 100, outage_days: int = 0,
                      lot_reject_pct: int = 0, demand_pct: int = 0, dual_source_pct: int = 0,
                      safety_boost_days: int = 0, expedite_days: int = 0, horizon_days: int = 60) -> str:
    """Digital-twin Monte Carlo simulation of the inbound supply network vs baseline. supplier = ID/name, 'OVERSEAS'
    (all non-India suppliers), 'ALL', or '' for demand-only scenarios. Disruptions: delay_days, capacity_pct (remaining),
    outage_days, lot_reject_pct, demand_pct. Mitigations: dual_source_pct, safety_boost_days, expedite_days.
    Returns stockout probabilities, units/revenue/line-hours at risk, backlog at risk and the most impacted materials."""
    from . import twin as T
    scope = supplier.strip().upper() if supplier.strip().upper() in ("OVERSEAS", "ALL") else (
        _resolve_supplier(supplier) if supplier else "NONE")
    r = T.simulate({"scope": scope, "delay_days": delay_days, "capacity_pct": capacity_pct, "outage_days": outage_days,
                    "lot_reject_pct": lot_reject_pct, "demand_pct": demand_pct, "dual_source_pct": dual_source_pct,
                    "safety_boost_days": safety_boost_days, "expedite_days": expedite_days, "horizon_days": horizon_days})
    sm = r["summary"]
    cr = lambda v: f"₹{(v or 0) / 1e7:,.2f} Cr"       # pre-formatted so the model never has to convert units
    sm["readable"] = {"revenue_at_risk": cr(sm["revenue_at_risk_inr"]), "baseline_revenue_at_risk": cr(sm["baseline_revenue_at_risk_inr"]),
                      "incremental_revenue_at_risk": cr(sm["incremental_revenue_at_risk_inr"]),
                      "backlog_at_risk": cr(sm["backlog_at_risk_inr"])}
    return _dump({"summary": sm,
                  "plants": [{k: v for k, v in p.items() if k != "factory_twin_lines"} for p in r["plants"]],
                  "top_materials": r["materials"][:8], "note": "Simulation for decision support; save and approve in the Digital Twin page."})


# ---------------------------------------------------------------- safe query layer (enhancement 1.1)
@tool
def list_metrics() -> str:
    """List the approved metrics (id, definition, allowed dimensions) usable with query_metric / compare_metric_periods."""
    return _dump(MX.catalog())


@tool
def query_metric(metric: str, group_by: str = "", supplier: str = "", plant_id: str = "", material_id: str = "",
                 start_date: str = "", end_date: str = "") -> str:
    """Compute an APPROVED metric (see list_metrics), optionally grouped by up to one dimension (supplier | plant | material |
    category | defect) and/or a time grain (week | month), comma-separated, e.g. group_by='month,supplier'. Filters: supplier
    (ID or name), plant_id, material_id, start_date/end_date (YYYY-MM-DD). Returns rows, a chart and a source reference."""
    f = {"supplier_id": _resolve_supplier(supplier) if supplier else None, "plant_id": plant_id or None,
         "material_id": material_id or None, "start": start_date or None, "end": end_date or None}
    try:
        r = MX.query(metric, [g for g in group_by.split(",") if g.strip()], f)
    except MX.MetricError as e:
        return f"ERROR: {e}"
    r["rows"] = r["rows"][:60]
    return json.dumps(r, default=str, separators=(",", ":"))


@tool
def compare_metric_periods(metric: str, grain: str = "month", dimension: str = "supplier", plant_id: str = "") -> str:
    """Change of an APPROVED metric between the current period (containing the as-of date) and the previous one, per
    supplier (or plant / material / category). Answers 'which suppliers got worse this month?'. direction = worse | better |
    flat using the metric's good direction. This is performance monitoring, not a sourcing ranking."""
    try:
        r = MX.compare_periods(metric, grain, dimension, {"plant_id": plant_id or None})
    except MX.MetricError as e:
        return f"ERROR: {e}"
    return json.dumps(r, default=str, separators=(",", ":"))


@tool
def draft_capa_request(supplier: str) -> str:
    """Draft an 8D / corrective-action request letter for a supplier from its own figures (OTIF, PPM, top defects, overdue NCRs,
    strongest risk alert). Returns the draft only: nothing is sent. To file it, call propose_corrective_action with the draft
    as the description (a person must approve it before the supplier sees it)."""
    from . import workflow_extras as WX
    if not is_on("workflow_extras"):
        return "ERROR: workflow_extras is switched off."
    try:
        return _dump(WX.capa_draft(_resolve_supplier(supplier)))
    except KeyError as e:
        return f"ERROR: {e}"


@tool
def request_quality_reason(supplier: str, material: str = "") -> str:
    """When a supplier's material shows poor quality, draft a request asking the supplier for the reason (root cause, containment,
    corrective action) with the defect evidence (defects, inspections, production orders, lots). Creates a DRAFT only: a quality user
    reviews and sends it by e-mail and to the supplier portal from Risk Radar -> Actions. Only use when the user asks."""
    _require("QUALITY", "PROCUREMENT")
    if not is_on("quality_inquiry"):
        return "ERROR: quality_inquiry is switched off."
    from . import quality_inquiry as QI
    try:
        d = QI.draft(_resolve_supplier(supplier), _resolve_material(material) if material else None, f"agent (for {AU.actor()})", origin="AGENT")
    except KeyError as e:
        return f"ERROR: {e}"
    return _dump({"inquiry_id": d["id"], "status": d["status"], "subject": d["subject"], "due_date": d["due_date"],
                  "note": "Draft created. A quality user must review and click Send in Risk Radar -> Actions."})


@tool
def predict_late_deliveries(supplier: str = "", min_risk: str = "MEDIUM", horizon_days: int = 30) -> str:
    """PREDICTIVE: which open PO lines are likely to arrive LATE, before it happens. For each line: probability of being late,
    expected delay in days, predicted arrival date and the main reasons. min_risk: LOW | MEDIUM | HIGH | LATE (already past due).
    Use for 'what will be late', 'which deliveries are at risk next month', 'forecast late shipments'. Predictions are advisory."""
    if not is_on("predictive_layer"):
        return "ERROR: predictive_layer is switched off."
    from . import predictive as PR
    sid = _resolve_supplier(supplier) if supplier else None
    try:
        r = PR.late_risk(sid, (min_risk or "MEDIUM").upper(), horizon_days)
    except ValueError as e:
        return f"ERROR: {e}"
    keep = ("po_id", "po_line", "supplier_name", "material_name", "promised_date", "p_late", "risk_band", "expected_delay_days", "predicted_arrival", "value_inr")
    items = [{**{k: i[k] for k in keep}, "why": "; ".join(d["text"] for d in i["drivers"])} for i in r["items"][:25]]
    return _dump({"model": r["model"], "summary": r["summary"], "lines": items, "note": "probability = model estimate (advisory); validated on later orders"})


@tool
def get_delivery_forecast(supplier: str = "", horizon_days: int = 30) -> str:
    """PREDICTIVE: expected on-time rate of each supplier's deliveries due in the next N days, compared with its last 60 days,
    with value at risk and an outlook (WORSENING / STABLE / IMPROVING). Use for 'which suppliers will get worse', 'forecast OTIF'."""
    if not is_on("predictive_layer"):
        return "ERROR: predictive_layer is switched off."
    from . import predictive as PR
    rows = PR.supplier_forecast(horizon_days)
    if supplier:
        sid = _resolve_supplier(supplier); rows = [r for r in rows if r["supplier_id"] == sid]
    return _dump(rows)


@tool
def get_quality_early_warnings(supplier: str = "") -> str:
    """PREDICTIVE: quality early warnings - supplier x material combinations whose defects this week jumped above their own
    statistical control limit, plus each supplier's defect trend projected 4 weeks ahead. Use for 'unusual quality problems',
    'which supplier's quality is getting worse'."""
    if not is_on("predictive_layer"):
        return "ERROR: predictive_layer is switched off."
    from . import predictive as PR
    q = PR.quality_watch()
    if supplier:
        sid = _resolve_supplier(supplier)
        q = {**q, "anomalies": [a for a in q["anomalies"] if a["supplier_id"] == sid], "trend": [t for t in q["trend"] if t["supplier_id"] == sid]}
    return _dump(q)


def _po_ref(ref: str) -> tuple[str, int | None]:
    m = re.search(r"(?i)\b(POC?-?\d{3,})(?:\s*[/-]\s*(\d{1,3}))?\b", ref or "")
    if not m:
        raise ValueError("Give a PO number such as POC-000920 or POC-000920/1")
    po = m.group(1).upper()
    if not po.startswith("POC-") and po.startswith("POC"):
        po = "POC-" + po[3:].lstrip("-")
    return po, int(m.group(2)) if m.group(2) else None


@tool
def explain_late_delivery(po: str = "", supplier: str = "") -> str:
    """WHY IS THIS LATE: for one PO line (e.g. "POC-000920" or "POC-000920/1") explains where the time was lost (journey stages
    planned vs actual), the root cause and contributing factors with evidence, the impact on our lines and what to do. With only
    `supplier`, explains that supplier's most overdue open line. Returns `brief` (ready markdown): present it as it is."""
    if not is_on("delay_reasoning"):
        return "ERROR: delay_reasoning is switched off."
    from . import delay_reasoning as DR
    try:
        if po:
            p, ln = _po_ref(po)
        elif supplier:
            sid = _resolve_supplier(supplier)
            rows = A._run(f"""SELECT po_id, po_line FROM {A.S}.v_po_delivery WHERE supplier_id = :s AND delivery_status = 'PAST_DUE' AND NOT excluded
                             ORDER BY promised_date LIMIT 1""", {"s": sid})
            if not rows:
                return _dump({"brief": "This supplier has no overdue PO lines right now."})
            p, ln = rows[0]["po_id"], rows[0]["po_line"]
        else:
            return "ERROR: give a PO number or a supplier."
        x = DR.explain(p, ln)
    except (KeyError, ValueError) as e:
        return f"ERROR: {e}"
    keep = ("po_id", "po_line", "supplier_name", "material_name", "plant", "status", "promised", "received", "days_late", "root_cause", "impact")
    return _dump({**{k: x[k] for k in keep}, "brief": x["brief"]})


@tool
def get_delay_causes(supplier: str = "", days: int = 120) -> str:
    """WHY DELIVERIES ARE LATE in general: Pareto of root causes of late PO lines (capacity, raw material, quality hold, missed
    vessel, port congestion, customs, trucks, weather...) for one supplier or all suppliers, with who owns the delay
    (supplier / logistics / customs / external). Returns `brief` (ready markdown): present it as it is."""
    if not is_on("delay_reasoning"):
        return "ERROR: delay_reasoning is switched off."
    from . import delay_reasoning as DR
    sid = _resolve_supplier(supplier) if supplier else None
    c = DR.causes(sid, days)
    return _dump({"scope": c["scope"], "late_lines": c["late_lines"], "delay_days": c["delay_days"], "top_causes": c["causes"][:5],
                  "by_category": c["by_category"], "brief": c["brief"]})


ARTIFACT_TOOLS = {"query_metric", "compare_metric_periods"}

TOOLS = [list_metrics, query_metric, compare_metric_periods, get_portfolio_overview, what_if_weights, get_supplier_twin, run_twin_scenario, get_supplier_scorecard, get_supplier_trends, get_risk_alerts,
         get_supplier_exposure, get_open_issues, get_defect_breakdown, compare_suppliers_for_material,
         get_sourcing_review, predict_late_deliveries, get_delivery_forecast, get_quality_early_warnings, explain_late_delivery, get_delay_causes,
         get_schema, run_sql, propose_corrective_action, draft_capa_request, request_quality_reason, list_corrective_actions, notify_owner,
         generate_domo_beastmode]
