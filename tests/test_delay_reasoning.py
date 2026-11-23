"""Why is this late?: plan vs actual per stage, no look-ahead past the data cut-off, clean brief, causes Pareto."""
import datetime as dt
import json

import pytest

from app import analytics as A
from app import delay_reasoning as D
from app import tools as T


def _first(status_sql: str) -> str:
    rows = A.query(f"SELECT po_id FROM {A.S}.v_po_delivery WHERE {status_sql} AND NOT excluded ORDER BY promised_date LIMIT 1")
    if not rows:
        pytest.skip("no such line in the data")
    return rows[0]["po_id"]


def test_overdue_line_never_shows_events_after_the_cutoff():
    x = D.explain(_first("delivery_status = 'PAST_DUE'"))
    as_of = dt.date.fromisoformat(A.as_of_date()[:10])
    assert x["status"] == "OVERDUE" and x["days_late"] > 0 and x["received"] is None
    for t in x["timeline"]:
        if t["actual"]:
            assert dt.date.fromisoformat(t["actual"]) <= as_of               # nothing known after the cut-off
    if x["root_cause"] and x["root_cause"]["ongoing"]:
        assert "so far" in x["brief"] and "held the shipment" not in x["root_cause"]["evidence"]   # present tense, no final duration


def test_delivered_late_line_explains_the_full_delay():
    x = D.explain(_first("delivery_status = 'RECEIVED' AND delay_days > 3 AND receipt_date <= sp.as_of_date()"))
    assert x["status"] == "DELIVERED_LATE" and x["root_cause"]
    assert sum(w["days"] for w in x["where_lost"]) == x["days_late"]        # the stages account for every day lost
    for h in ("**Verdict:**", "#### Where the time was lost", "#### Why it happened", "#### Impact", "#### What to do"):
        assert h in x["brief"]
    assert x["actions"]


def test_causes_pareto_adds_up():
    c = D.causes()
    assert c["causes"] and c["late_lines"] > 0
    assert abs(sum(i["share_pct"] for i in c["causes"]) - 100) <= len(c["causes"])       # rounding only
    assert c["causes"] == sorted(c["causes"], key=lambda i: -i["delay_days"])
    assert "#### Causes, largest first" in c["brief"]


def test_agent_tools():
    assert T._po_ref("why is POC-000920/1 late?") == ("POC-000920", 1)
    out = json.loads(T.explain_late_delivery.invoke({"po": _first("delivery_status = 'PAST_DUE'")}))
    assert out["brief"].startswith("**PO ")
    assert T.explain_late_delivery.invoke({"po": "nonsense"}).startswith("ERROR")
    out = json.loads(T.get_delay_causes.invoke({}))
    assert out["top_causes"] and "brief" in out
