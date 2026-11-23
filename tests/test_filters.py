"""Page filters drive every KPI: overview, pulse, defects and trends accept a supplier set."""
from app import analytics as A


def test_overview_kpis_follow_the_supplier_filter():
    full, sub = A.overview(), A.overview(None, None, None, "SUP-001,SUP-008")
    cards = {c["supplier_id"]: c for c in A.scorecard()}
    assert sub["suppliers"] == 2 and full["suppliers"] == len(cards)
    assert sub["past_due_lines"] == cards["SUP-001"]["past_due_lines"] + cards["SUP-008"]["past_due_lines"]
    assert sub["spend_inr"] < full["spend_inr"]
    assert A.overview(None, None, None, "__none__")["suppliers"] == 0           # filter matching nothing -> empty, not "all"


def test_charts_follow_the_supplier_filter():
    total = lambda rows: sum(float(r["defects"] or 0) for r in rows)              # noqa: E731
    assert total(A.defect_breakdown("SUP-001,SUP-008")) < total(A.defect_breakdown())
    pulse = A.kpi_pulse(None, 12, "SUP-008")
    assert pulse["otif_pct"]["series"] and pulse["disruption_hours"]["series"] is not None
    assert A.trends("SUP-001,SUP-008", "month") != A.trends(None, "month")
