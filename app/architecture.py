"""Agent architecture: a static description of how a question flows through the system, plus a catalogue that maps
every agent tool to the specialist role it plays, the backend function it calls, the datasets it reads and the page
that shows the same data (used by the Agent Architecture page and for drill-through links from the chat).

Datasets were taken from the SQL each backend function runs (app/analytics.py, app/metrics.py, app/twin.py); keep them
in step when a function starts reading a new view. tests/test_architecture.py checks every agent tool is catalogued.
"""
from __future__ import annotations

import json
import re

DELIVERY = ["sp.v_po_delivery"]
QUALITY = ["sp.v_defect_attribution", "sp.fact_quality_inspection"]

AGENTS = {
    "orchestrator": {"name": "Orchestrator", "does": "Reads the question, plans which specialists to call, and writes the final answer (Domo LLM via LangChain)."},
    "performance": {"name": "Performance analyst", "does": "Scorecards, KPIs, grades and ranks."},
    "trend": {"name": "Trend analyst", "does": "Month-by-month and week-by-week movement of OTIF, PPM and lead time."},
    "risk": {"name": "Risk analyst", "does": "Risk signals, exposure of production lines and customer orders."},
    "quality": {"name": "Quality analyst", "does": "Defects, NCRs and CAPAs attributed to suppliers."},
    "sourcing": {"name": "Sourcing advisor", "does": "Compares suppliers of one material and, when a supplier keeps failing, builds the case for moving volume to a better one, for procurement to decide."},
    "forecast": {"name": "Forecasting analyst", "does": "Predicts late deliveries, supplier on-time outlook and quality early warnings (explainable model)."},
    "logistics": {"name": "Delay investigator", "does": "Explains why a delivery is late: where the time was lost, root cause, evidence, impact, actions."},
    "simulation": {"name": "Simulation agent", "does": "What-if weights and digital-twin Monte Carlo scenarios."},
    "query": {"name": "Safe query agent", "does": "Approved metrics and read-only SQL over approved views."},
    "action": {"name": "Action agent", "does": "Proposes corrective actions and notifications; a person approves them."},
    "validator": {"name": "Grounding validator", "does": "Checks every figure in the answer against the tool results."},
}

TOOLS = {
    "get_portfolio_overview": {"agent": "performance", "backend": "analytics.overview()", "page": "command",
                               "datasets": ["sp.v_po_delivery", "sp.fact_quality_inspection", "sp.v_incoming_quality", "sp.v_ncr_supplier", "sp.v_supply_disruption"],
                               "label": "Portfolio KPIs"},
    "get_supplier_scorecard": {"agent": "performance", "backend": "analytics.scorecard()", "page": "command",
                               "datasets": ["sp.v_po_delivery", "sp.v_incoming_quality", "sp.v_defect_attribution", "sp.v_ncr_supplier",
                                            "sp.v_supply_disruption", "sp.fact_rfq_quote", "sp.dim_supplier"], "label": "Supplier scorecard"},
    "get_supplier_trends": {"agent": "trend", "backend": "analytics.trends()", "page": "trends",
                            "datasets": ["sp.v_po_delivery", "sp.v_defect_attribution"], "label": "OTIF / PPM trend"},
    "get_risk_alerts": {"agent": "risk", "backend": "analytics.risk_alerts()", "page": "risk",
                        "datasets": ["sp.v_po_delivery", "sp.v_defect_attribution", "sp.v_ncr_supplier", "sp.v_material_supplier_share",
                                     "sp.v_supply_disruption", "sp.fact_quality_inspection"], "label": "Risk signals"},
    "get_supplier_exposure": {"agent": "risk", "backend": "analytics.exposure()", "page": "tracker",
                              "datasets": ["sp.v_po_delivery", "sp.v_defect_attribution", "sp.v_material_supplier_share",
                                           "sp.fact_production_order", "sp.fact_bom_component"], "label": "Line and customer exposure"},
    "get_open_issues": {"agent": "quality", "backend": "analytics.supplier_issues()", "page": "risk",
                        "datasets": ["sp.v_ncr_supplier"], "label": "Open NCR / CAPA"},
    "get_defect_breakdown": {"agent": "quality", "backend": "analytics.defect_breakdown()", "page": "command",
                             "datasets": ["sp.v_defect_attribution"], "label": "Defects by code"},
    "get_sourcing_review": {"agent": "sourcing", "backend": "sourcing_review.build()", "page": "risk",
                            "datasets": ["sp.v_po_delivery", "sp.v_defect_attribution", "sp.v_material_supplier_share", "sp.fact_rfq_quote"],
                            "label": "Sourcing review (switch suggestion)"},
    "compare_suppliers_for_material": {"agent": "sourcing", "backend": "analytics.material_sourcing()", "page": None,
                                       "datasets": ["sp.v_po_delivery", "sp.v_defect_attribution", "sp.v_material_supplier_share", "sp.fact_rfq_quote"],
                                       "label": "Supplier comparison"},
    "what_if_weights": {"agent": "simulation", "backend": "analytics.simulate()", "page": "command",
                        "datasets": ["sp.v_po_delivery", "sp.v_incoming_quality", "sp.v_defect_attribution", "sp.v_ncr_supplier"], "label": "What-if weights"},
    "get_supplier_twin": {"agent": "simulation", "backend": "twin.supplier_state_cached()", "page": "tracker",
                          "datasets": ["sp.v_po_delivery", "sp.dim_material", "sp.fact_inventory_daily_snapshot"], "label": "Supplier twin"},
    "run_twin_scenario": {"agent": "simulation", "backend": "twin.simulate()", "page": "tracker",
                          "datasets": ["sp.v_po_delivery", "sp.dim_material", "sp.dim_production_line", "sp.fact_customer_order",
                                       "sp.fact_inventory_daily_snapshot"], "label": "Twin scenario"},
    "list_metrics": {"agent": "query", "backend": "metrics.catalog()", "page": "settings", "datasets": ["public.app_config (metric catalogue)"],
                     "label": "Approved metrics"},
    "query_metric": {"agent": "query", "backend": "metrics.query()", "page": "trends", "datasets": DELIVERY + ["sp.v_defect_attribution", "sp.v_ncr_supplier"],
                     "label": "Approved metric"},
    "compare_metric_periods": {"agent": "query", "backend": "metrics.compare_periods()", "page": "trends",
                               "datasets": DELIVERY + ["sp.v_defect_attribution"], "label": "Period comparison"},
    "get_schema": {"agent": "query", "backend": "tools.get_schema()", "page": None, "datasets": ["sp (catalogue only)"], "label": "Schema"},
    "run_sql": {"agent": "query", "backend": "tools.run_sql() · read-only role", "page": None, "datasets": ["sp (approved views only)"],
                "label": "Read-only SQL"},
    "propose_corrective_action": {"agent": "action", "backend": "public.corrective_actions (PROPOSED)", "page": "risk",
                                  "datasets": ["public.corrective_actions"], "label": "Corrective action proposal", "writes": True},
    "draft_capa_request": {"agent": "action", "backend": "workflow_extras.capa_draft()", "page": "risk",
                           "datasets": ["sp.v_po_delivery", "sp.v_defect_attribution", "sp.v_ncr_supplier"], "label": "8D / CAPA request draft"},
    "explain_late_delivery": {"agent": "logistics", "backend": "delay_reasoning.explain()", "page": "risk",
                              "datasets": ["sp.fact_delay_event", "sp.v_po_delivery", "public.disruption_events", "sp.v_supply_disruption"], "label": "Why is this late?"},
    "get_delay_causes": {"agent": "logistics", "backend": "delay_reasoning.causes()", "page": "risk",
                         "datasets": ["sp.fact_delay_event", "sp.v_po_delivery"], "label": "Delay-cause Pareto"},
    "predict_late_deliveries": {"agent": "forecast", "backend": "predictive.late_risk()", "page": "risk",
                                "datasets": ["sp.v_po_delivery", "public.ml_models", "public.ml_predictions"], "label": "Late-delivery prediction"},
    "get_delivery_forecast": {"agent": "forecast", "backend": "predictive.supplier_forecast()", "page": "risk",
                              "datasets": ["sp.v_po_delivery", "public.ml_models"], "label": "30-day on-time forecast"},
    "get_quality_early_warnings": {"agent": "forecast", "backend": "predictive.quality_watch()", "page": "risk",
                                   "datasets": QUALITY, "label": "Quality early warning"},
    "request_quality_reason": {"agent": "action", "backend": "quality_inquiry.draft()", "page": "risk",
                               "datasets": ["sp.v_defect_attribution", "public.quality_inquiries"], "label": "Supplier quality inquiry draft", "writes": True},
    "list_corrective_actions": {"agent": "action", "backend": "public.corrective_actions", "page": "risk",
                                "datasets": ["public.corrective_actions"], "label": "Action tracker"},
    "notify_owner": {"agent": "action", "backend": "public.notifications", "page": None, "datasets": ["public.notifications"],
                     "label": "Notification", "writes": True},
    "generate_domo_beastmode": {"agent": "query", "backend": "Domo AI Beast Mode API (column names only)", "page": None,
                                "datasets": ["sp view columns"], "label": "Beast Mode formula"},
}

# The path every question takes. `component` is the real module/endpoint so engineers can follow along.
PIPELINE = [
    {"id": "receive", "title": "Question received", "layer": "API", "component": "POST /api/chat/stream (FastAPI)",
     "detail": "Sign-in checked, role-based policy applied, rate limit (12 per minute) enforced, conversation saved."},
    {"id": "plan", "title": "Orchestrator plans", "layer": "Agent", "component": "agent.iter_agent → Domo LLM (LangChain)",
     "detail": "The model sees the question, chat history, the as-of date and the tool catalogue, then picks the next tool."},
    {"id": "tools", "title": "Specialists run", "layer": "Tools", "component": "app/tools.py → app/analytics.py, metrics.py, twin.py",
     "detail": "Each tool runs governed SQL on read-only connections to the KPI views in Neon Postgres."},
    {"id": "ground", "title": "Figures verified", "layer": "Validator", "component": "grounding.grounding_check",
     "detail": "Every number in the answer must appear in a tool result; otherwise the agent is sent back once to correct it."},
    {"id": "answer", "title": "Answer delivered", "layer": "Output", "component": "public.messages + chat panel",
     "detail": "Answer, charts and the full tool trace are stored with the conversation and streamed to the screen."},
]

LAYERS = [
    {"id": "ui", "name": "Experience", "items": ["React / Vite app", "Chat panel", "Overview, risk, network map"]},
    {"id": "api", "name": "API", "items": ["FastAPI", "Role-based policy (auth.py)", "Feature flags", "Audit log"]},
    {"id": "agent", "name": "Agent", "items": ["Orchestrator (Domo LLM via LangChain)", "Specialist tools", "Grounding validator"]},
    {"id": "logic", "name": "Analytics", "items": ["Scorecard engine", "Approved metric catalogue", "Risk rules", "Digital twin"]},
    {"id": "data", "name": "Data", "items": ["Neon Postgres · schema sp (KPI views)", "schema public (outputs)", "schema integration (connectors)"]},
    {"id": "src", "name": "Sources", "items": ["SAP / ERP", "MES / OPC UA", "Files, REST, SFTP"]},
]


def summarize(output: str) -> str:
    """One-line description of a tool result for the checklist ('25 rows', '12 fields', 'error')."""
    o = (output or "").strip()
    if o.startswith("ERROR"):
        return "error: " + o[6:120].strip()
    if o in ("No rows.", ""):
        return "no rows"
    try:
        v = json.loads(o)
    except ValueError:
        if o[:1] in "[{" and "(truncated," in o[-60:]:          # tools.py _dump cuts long results for the model
            m = re.search(r'"count":(\d+)', o)
            return (f"{m.group(1)} records" if m else "large result") + " (shortened to 6,000 characters for the model)"
        return f"{len(o):,} characters of text"
    if isinstance(v, list):
        return f"{len(v)} rows"
    if isinstance(v, dict):
        for k in ("rows", "suppliers", "alerts", "items", "open_items"):
            if isinstance(v.get(k), list):
                return f"{len(v[k])} {k.replace('_', ' ')}"
        return f"{len(v)} fields"
    return "1 value"


def describe() -> dict:
    return {"pipeline": PIPELINE, "layers": LAYERS, "agents": AGENTS, "tools": TOOLS}
