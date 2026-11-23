"""Predictive layer: no look-ahead, censoring handled, plausible drivers only, valid predictions for every open line."""
import datetime as dt

import numpy as np

from app import analytics as A
from app import auth as AU
from app import predictive as P


def test_auc_and_scores():
    assert P.auc([0, 0, 1, 1], [0.1, 0.2, 0.8, 0.9]) == 1.0
    assert P.auc([0, 0, 1, 1], [0.9, 0.8, 0.2, 0.1]) == 0.0
    assert abs(P.auc([0, 1, 0, 1], [0.5, 0.5, 0.5, 0.5]) - 0.5) < 1e-9          # ties -> coin toss
    s = P._scores([0, 1, 1, 0], [0.2, 0.7, 0.9, 0.1])
    assert s["accuracy"] == 1.0 and s["auc"] == 1.0 and s["calibration"]


def test_known_outcomes_include_past_due_and_no_lookahead():
    rows, as_of = P._lines(), P._d(A.as_of_date())
    known = P._known(rows, as_of)
    keys = {(r["po_id"], r["po_line"]) for r in known}
    past_due = [r for r in rows if r["delivery_status"] == "PAST_DUE"]
    assert past_due and all((r["po_id"], r["po_line"]) in keys for r in past_due)       # censored lines are kept, as late
    assert all(r["_late"] for r in known if r["delivery_status"] == "PAST_DUE")
    assert all(P._d(r["_known_at"]) <= as_of + dt.timedelta(days=1) for r in known)     # nothing known after the cut-off
    not_due = [r for r in rows if not (r["delivery_status"] == "RECEIVED" and r["receipt_date"] and P._d(r["receipt_date"]) <= as_of)
               and P._d(r["promised_date"]) >= as_of]
    assert not_due and not any((r["po_id"], r["po_line"]) in keys for r in not_due)    # open lines are never training labels
    # behaviour features of a training row only use outcomes known before its order date
    r0 = known[len(known) // 2]
    f_all = P._features([r0], known, lambda r: r["po_date"])[0]
    f_past = P._features([r0], [h for h in known if P._d(h["_known_at"]) < P._d(r0["po_date"])], lambda r: r["po_date"])[0]
    assert abs(f_all["sup_ontime"] - f_past["sup_ontime"]) < 1e-9


def test_direction_check_keeps_only_plausible_effects():
    rows, as_of = P._lines(), P._d(A.as_of_date())
    known = P._known(rows, as_of)
    feats = P._features(known, known, lambda r: r["po_date"])
    y = np.array([1.0 if r["_late"] else 0.0 for r in known])
    D, w, dropped = P.fit_checked(feats, y)
    wn = dict(zip(D.names, w[1:]))
    for n, sign in P.EXPECT.items():
        if n in D.nums:
            assert wn[n] * sign >= 0, (n, wn[n])                                      # every kept effect points the plausible way
    assert set(dropped).isdisjoint(D.nums)


def test_every_open_line_gets_a_valid_prediction():
    if not P.active_model():
        P.train("pytest")
    res = P.score(force=True)
    assert res["items"]
    for i in res["items"]:
        assert 0 <= i["p_late"] <= 1 and i["risk_band"] in ("LOW", "MEDIUM", "HIGH", "LATE")
        if i["status"] == "PAST_DUE":
            assert i["p_late"] == 1.0 and i["risk_band"] == "LATE" and i["expected_delay_days"] > 0
        assert i["drivers"] and all(d["text"] for d in i["drivers"])
    card = P.model_card()
    assert card["metrics"]["holdout"]["auc"] is not None and "dropped" in card["metrics"]
    fc = P.supplier_forecast()
    assert fc and all(0 <= f["predicted_on_time_pct"] <= 100 for f in fc)
    q = P.quality_watch()
    assert {"anomalies", "trend"} <= set(q)


def test_access_rules():
    assert AU.allowed("POST", "/api/predictions/retrain", "ADMIN")
    for role in ("PROCUREMENT", "QUALITY", "VIEWER", "SUPPLIER"):
        assert not AU.allowed("POST", "/api/predictions/retrain", role)
    assert AU.allowed("GET", "/api/predictions/late", "PROCUREMENT")
    assert not AU.allowed("GET", "/api/predictions/late", "SUPPLIER")
