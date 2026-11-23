"""Predictive layer: forecasts problems before they show up in the KPIs.

1. Late-delivery risk  - for every open PO line: probability it arrives after its promise date, expected delay (days),
                         predicted arrival date and the three main reasons, in plain words.
                         Model: L2-regularised logistic regression (fitted by Newton's method) + ridge regression for the delay.
                         Chosen on purpose: small data, every prediction explainable to a buyer, no heavy ML dependency.
2. Supplier forecast   - expected on-time rate of each supplier's lines due in the next 30 days vs its last 60 days.
3. Quality early warning - weekly rejection rate per supplier x material against its own control limits (p-chart, 3 sigma):
                         an unusual jump is flagged before it moves the monthly PPM.

Governance: features use only what was known when the order was placed (no leakage); time-based validation (train on earlier
orders, test on later ones) and a forward check on orders due after the data cut-off; every model version and every prediction
is stored (public.ml_models / public.ml_predictions) so it can be audited later. Predictions are advisory: they never change a
KPI, a ranking or a sourcing decision.
"""
from __future__ import annotations

import datetime as dt
import json
import math

import numpy as np
from sqlalchemy import text

from . import analytics as A
from .db import engine, now, read_engine

S = A.S
KIND = "late_delivery"
WINDOW_DAYS = 60            # trailing window for "recent behaviour" features
PRIOR_K = 8                 # smoothing: a supplier with few recent lines is pulled towards the portfolio average
LAM_LOGIT, LAM_RIDGE = 15.0, 6.0
# Common-sense direction each behaviour / order feature must have (+1 = higher value -> more likely late). A feature whose fitted
# weight points the other way is dropped and the model refitted, so no prediction is ever explained by a backwards reason.
EXPECT = {"sup_ontime": -1, "mat_ontime": -1, "sup_delay": +1, "promised_lt": -1, "tightness": -1, "sup_open": +1, "promise_changed": +1}
BANDS = [(0.7, "HIGH"), (0.4, "MEDIUM"), (0.0, "LOW")]
CATS = ["supplier_id", "material_category", "plant_id", "incoterm", "risk_tier"]
NUMS = ["promised_lt", "tightness", "log_qty", "log_value", "promise_changed", "sup_ontime", "sup_delay", "mat_ontime", "sup_open"]
FAMILY = {"supplier_id": "supplier", "material_category": "material", "plant_id": "plant", "incoterm": "incoterm", "risk_tier": "risk_tier",
          "promised_lt": "lead_time", "tightness": "lead_time", "log_qty": "size", "log_value": "size", "promise_changed": "promise_change",
          "sup_ontime": "sup_recent", "sup_delay": "sup_recent", "mat_ontime": "mat_recent", "sup_open": "workload"}


def _d(v) -> dt.date:
    return v if isinstance(v, dt.date) else dt.date.fromisoformat(str(v)[:10])


# ---------------------------------------------------------------- data + features
def _lines() -> list[dict]:
    return A.query(f"""
      SELECT po_id, po_line, supplier_id, supplier_name, material_id, material_name, material_category, plant_id, po_date, promised_date,
             receipt_date, delivery_status, incoterm, ordered_qty, unit_price_inr, promise_changed, risk_tier,
             COALESCE(standard_lead_time_days, promised_lead_time_days) AS std_lt, delay_days, on_time
      FROM {S}.v_po_delivery WHERE NOT excluded ORDER BY po_date, po_id, po_line""")


def _known(rows: list[dict], as_of: dt.date) -> list[dict]:
    """Lines whose on-time outcome is known at the cut-off, with the day it became known.
    Received by the cut-off -> known at receipt. Not received and the promise date has passed -> late, known the day after the
    promise. Using only received lines would drop exactly the late ones still outstanding (right-censoring) and make recent
    months look far more punctual than they were."""
    out = []
    for r in rows:
        prom = _d(r["promised_date"]); rec = _d(r["receipt_date"]) if r["receipt_date"] else None
        if r["delivery_status"] == "RECEIVED" and rec and rec <= as_of:
            late = not r["on_time"]
            out.append({**r, "_late": late, "_known_at": min(rec, prom + dt.timedelta(days=1)) if late else rec,
                        "_delay": float(max(-5, min(30, r["delay_days"] or 0)))})
        elif prom < as_of:
            out.append({**r, "_late": True, "_known_at": prom + dt.timedelta(days=1), "_delay": None})
    return out


def _features(rows: list[dict], history: list[dict], ref_of) -> list[dict]:
    """Feature rows. ref_of(row) = the date the prediction is made: order date for training, data cut-off for open lines.
    Behaviour features only use outcomes known before that date (see _known)."""
    h_rec = np.array([_d(r["_known_at"]).toordinal() for r in history]) if history else np.zeros(0)
    h_sup = np.array([r["supplier_id"] for r in history]); h_mat = np.array([r["material_id"] for r in history])
    h_on = np.array([0.0 if r["_late"] else 1.0 for r in history])
    h_del = np.array([r["_delay"] if r["_delay"] is not None else 10.0 for r in history])          # still outstanding: at least late
    all_po = np.array([_d(r["po_date"]).toordinal() for r in rows]); all_sup = np.array([r["supplier_id"] for r in rows])
    all_rec = np.array([_d(r["receipt_date"]).toordinal() if r["receipt_date"] else 10 ** 7 for r in rows])
    g_on = float(h_on.mean()) if len(h_on) else 0.5; g_del = float(h_del.mean()) if len(h_del) else 0.0
    out = []
    for r in rows:
        ref = _d(ref_of(r)).toordinal()
        win = (h_rec < ref) & (h_rec >= ref - WINDOW_DAYS)
        past = h_rec < ref
        g_on = float(h_on[past].mean()) if past.any() else 0.5; g_del = float(h_del[past].mean()) if past.any() else 0.0
        ms = win & (h_sup == r["supplier_id"]); mm = win & (h_mat == r["material_id"])
        n_s, n_m = int(ms.sum()), int(mm.sum())
        sup_on = (h_on[ms].sum() + PRIOR_K * g_on) / (n_s + PRIOR_K)
        sup_del = (h_del[ms].sum() + PRIOR_K * g_del) / (n_s + PRIOR_K)
        mat_on = (h_on[mm].sum() + PRIOR_K * g_on) / (n_m + PRIOR_K)
        # workload: this supplier's other lines ordered but not yet received on the reference date
        open_n = int(((all_sup == r["supplier_id"]) & (all_po <= ref) & (all_rec > ref)).sum()) - 1
        plt = (_d(r["promised_date"]) - _d(r["po_date"])).days
        out.append({**{c: str(r.get(c) or "NA") for c in CATS},
                    "promised_lt": float(plt), "tightness": plt / max(1.0, float(r["std_lt"] or plt or 1)),
                    "log_qty": math.log1p(float(r["ordered_qty"] or 0)), "log_value": math.log1p(float(r["ordered_qty"] or 0) * float(r["unit_price_inr"] or 0)),
                    "promise_changed": 1.0 if r.get("promise_changed") else 0.0, "sup_ontime": float(sup_on), "sup_delay": float(sup_del),
                    "mat_ontime": float(mat_on), "sup_open": float(max(0, open_n)), "_n_sup": n_s})
    return out


class Design:
    """Turns feature dicts into a standardised matrix; remembers categories, numeric features, means and stds (stored with the model)."""
    def __init__(self, feats: list[dict] | None = None, spec: dict | None = None, nums: list[str] | None = None):
        if spec:
            self.levels, self.mu, self.sd = spec["levels"], np.array(spec["mu"]), np.array(spec["sd"])
            self.nums = spec.get("nums", list(NUMS))
            return
        self.nums = list(nums if nums is not None else NUMS)
        self.levels = {c: sorted({f[c] for f in feats}) for c in CATS}
        X = self._raw(feats); self.mu = X.mean(0); self.sd = X.std(0); self.sd[self.sd < 1e-9] = 1.0

    @property
    def names(self) -> list[str]:
        return [f"{c}={v}" for c in CATS for v in self.levels[c]] + self.nums

    def _raw(self, feats):
        return np.array([[1.0 if f[c] == v else 0.0 for c in CATS for v in self.levels[c]] + [f[n] for n in self.nums] for f in feats], dtype=float)

    def X(self, feats):
        return (self._raw(feats) - self.mu) / self.sd

    def spec(self):
        return {"levels": self.levels, "mu": self.mu.tolist(), "sd": self.sd.tolist(), "nums": self.nums}


def fit_checked(feats: list[dict], y: np.ndarray) -> tuple:
    """Fit, then drop any feature whose weight contradicts its expected direction (EXPECT) and refit. Returns (design, w, dropped)."""
    nums, dropped = list(NUMS), []
    for _ in range(len(EXPECT) + 1):
        D = Design(feats, nums=nums); w = fit_logistic(D.X(feats), y)
        wn = dict(zip(D.names, w[1:]))
        bad = [n for n in nums if n in EXPECT and wn[n] * EXPECT[n] < 0]
        if not bad:
            return D, w, dropped
        worst = max(bad, key=lambda n: abs(wn[n])); nums.remove(worst); dropped.append(worst)
    return D, w, dropped


def _sigmoid(z):
    return 1.0 / (1.0 + np.exp(-np.clip(z, -30, 30)))


def fit_logistic(X, y, lam=LAM_LOGIT, iters=60):
    Xb = np.hstack([np.ones((len(X), 1)), X]); w = np.zeros(Xb.shape[1]); R = lam * np.eye(Xb.shape[1]); R[0, 0] = 0
    for _ in range(iters):
        p = _sigmoid(Xb @ w); g = Xb.T @ (p - y) + R @ w
        H = Xb.T @ (Xb * (p * (1 - p))[:, None]) + R + 1e-6 * np.eye(len(w))
        step = np.linalg.solve(H, g); w -= step
        if np.abs(step).max() < 1e-7:
            break
    return w


def fit_ridge(X, y, lam=LAM_RIDGE):
    Xb = np.hstack([np.ones((len(X), 1)), X]); R = lam * np.eye(Xb.shape[1]); R[0, 0] = 0
    return np.linalg.solve(Xb.T @ Xb + R, Xb.T @ y)


def predict_p(w, X):
    return _sigmoid(np.hstack([np.ones((len(X), 1)), X]) @ w)


def auc(y, p) -> float | None:
    y = np.asarray(y); p = np.asarray(p); pos, neg = p[y == 1], p[y == 0]
    if not len(pos) or not len(neg):
        return None
    order = np.argsort(np.concatenate([pos, neg])); ranks = np.empty(len(order)); ranks[order] = np.arange(1, len(order) + 1)
    vals = np.concatenate([pos, neg])                  # average ranks for ties
    for v in np.unique(vals):
        m = vals == v
        if m.sum() > 1:
            ranks[m] = ranks[m].mean()
    return float((ranks[:len(pos)].sum() - len(pos) * (len(pos) + 1) / 2) / (len(pos) * len(neg)))


def _scores(y, p) -> dict:
    y = np.asarray(y, float); p = np.asarray(p, float)
    a = auc(y, p)
    cal = []
    for lo in (0, 0.2, 0.4, 0.6, 0.8):
        m = (p >= lo) & (p < lo + 0.2 + (1e-9 if lo == 0.8 else 0))
        if m.sum():
            cal.append({"bin": f"{int(lo * 100)}-{int(lo * 100 + 20)}%", "predicted": round(float(p[m].mean()), 3), "actual": round(float(y[m].mean()), 3), "n": int(m.sum())})
    return {"n": int(len(y)), "late_rate": round(float(y.mean()), 3) if len(y) else None, "auc": round(a, 3) if a is not None else None,
            "brier": round(float(((p - y) ** 2).mean()), 4) if len(y) else None,
            "accuracy": round(float(((p >= 0.5) == (y == 1)).mean()), 3) if len(y) else None, "calibration": cal}


# ---------------------------------------------------------------- training
def train(actor: str = "system") -> dict:
    """Fit on received lines; validate on later orders (time split) and forward-check on lines due after the cut-off."""
    rows, as_of = _lines(), _d(A.as_of_date())
    hist = _known(rows, as_of)
    if len(hist) < 80:
        raise ValueError(f"Not enough delivery history to train ({len(hist)} lines with a known outcome; need 80)")
    feats = _features(hist, hist, lambda r: r["po_date"])
    y = np.array([1.0 if r["_late"] else 0.0 for r in hist])
    has_d = np.array([r["_delay"] is not None for r in hist]); yd = np.array([r["_delay"] if r["_delay"] is not None else 0.0 for r in hist])
    # 1) time-based hold-out: earliest 75 % of orders train, latest 25 % test
    cut = int(len(hist) * 0.75)
    dz, w, _ = fit_checked(feats[:cut], y[:cut]); Xtr, Xte = dz.X(feats[:cut]), dz.X(feats[cut:])
    p_te = predict_p(w, Xte)
    tr_d, te_d = has_d[:cut], has_d[cut:]
    wr = fit_ridge(Xtr[tr_d], yd[:cut][tr_d]); d_te = (np.hstack([np.ones((len(Xte), 1)), Xte]) @ wr)[te_d]
    base = np.array([1 - f["sup_ontime"] for f in feats[cut:]])            # baseline: supplier's recent late rate
    holdout = {**_scores(y[cut:], p_te), "baseline_auc": (lambda a: round(a, 3) if a is not None else None)(auc(y[cut:], base)),
               "delay_mae_days": round(float(np.abs(d_te - yd[cut:][te_d]).mean()), 2),
               "delay_mae_baseline": round(float(np.abs(yd[:cut][tr_d].mean() - yd[cut:][te_d]).mean()), 2),
               "period": f"orders {str(hist[cut]['po_date'])[:10]} to {str(hist[-1]['po_date'])[:10]}"}
    # 2) final model on all history
    D, w, dropped = fit_checked(feats, y); X = D.X(feats); wr = fit_ridge(X[has_d], yd[has_d])
    # 3) forward check: lines open at the cut-off that have since been received (scored with cut-off knowledge only)
    fwd_rows = [r for r in rows if r["receipt_date"] and _d(r["receipt_date"]) > as_of and _d(r["promised_date"]) >= as_of]
    forward = None
    if fwd_rows:
        ff = _features(fwd_rows, hist, lambda r: as_of)
        forward = {**_scores([0.0 if r["on_time"] else 1.0 for r in fwd_rows], predict_p(w, D.X(ff))),
                   "note": "orders due after the data cut-off, predicted with data up to the cut-off, checked against their actual receipts"}
    # empirical "how much later" for lines already past due: E[delay | delay >= d], per supplier when it has enough history
    late_hist = {}
    for r in hist:
        if r["_delay"] is not None and r["_delay"] > 0:
            late_hist.setdefault(r["supplier_id"], []).append(int(r["_delay"])); late_hist.setdefault("*", []).append(int(r["_delay"]))
    importance = sorted(({"feature": n, "family": FAMILY[n.split("=")[0]], "weight": round(float(c), 3)} for n, c in zip(D.names, w[1:])),
                        key=lambda x: -abs(x["weight"]))[:15]
    model = {"design": D.spec(), "w": w.tolist(), "wr": wr.tolist(), "late_hist": late_hist, "global_on": float(1 - y.mean())}
    metrics = {"holdout": holdout, "forward": forward, "train_rows": len(hist), "as_of": as_of.isoformat(), "importance": importance,
               "features": D.nums, "dropped": [{"feature": n, "why": "its effect in the data pointed the wrong way (not plausible), so it is not used"} for n in dropped],
               "late_rate_known": round(float(y.mean()), 3),
               "method": "L2 logistic regression (late yes/no) + ridge regression (delay days); time-based validation"}
    version = f"{KIND}-{now().strftime('%Y%m%d-%H%M')}"
    with engine.begin() as c:
        c.execute(text("UPDATE public.ml_models SET active = FALSE WHERE kind = :k AND active"), {"k": KIND})
        mid = c.execute(text("""INSERT INTO public.ml_models (kind, version, trained_by, train_rows, metrics, model, active)
                                VALUES (:k, :v, :by, :n, CAST(:m AS jsonb), CAST(:mo AS jsonb), TRUE) RETURNING id"""),
                        {"k": KIND, "v": version, "by": actor, "n": len(hist), "m": json.dumps(metrics), "mo": json.dumps(model)}).scalar()
    _CACHE.clear()
    return {"id": mid, "version": version, **metrics}


def active_model() -> dict | None:
    with read_engine.connect() as c:
        r = c.execute(text("SELECT * FROM public.ml_models WHERE kind = :k AND active ORDER BY id DESC LIMIT 1"), {"k": KIND}).mappings().fetchone()
    return dict(r) if r else None


def model_card() -> dict | None:
    m = active_model()
    if not m:
        return None
    return {k: m[k] for k in ("id", "version", "trained_at", "trained_by", "train_rows")} | {"metrics": m["metrics"]}


# ---------------------------------------------------------------- scoring
_CACHE: dict = {}

_PLAIN = {
    "supplier": ("{sup} has a history of late deliveries", "{sup} is usually reliable"),
    "material": ("{cat} orders are often late", "{cat} orders usually arrive on time"),
    "plant": ("deliveries to {plant} run late more often", "deliveries to {plant} are usually on time"),
    "incoterm": ("{inc} shipments tend to be later", "{inc} shipments tend to be on time"),
    "risk_tier": ("supplier risk tier is {tier}", "low-risk supplier tier"),
    "lead_time": ("tight promise: {plt} days against a standard of {std}", "comfortable lead time ({plt} days)"),
    "size": ("large order ({qty} units)", "small order ({qty} units)"),
    "promise_change": ("the promise date was already changed once", "promise date unchanged"),
    "sup_recent": ("recent slide: only {son}% of its deliveries in the last 60 days were on time", "recently on time ({son}% in the last 60 days)"),
    "mat_recent": ("this material was recently late ({mon}% on time in 60 days)", "this material was recently on time ({mon}%)"),
    "workload": ("supplier is busy: {open} other open orders", "light workload at the supplier ({open} open orders)"),
}


def _drivers(x_std: np.ndarray, w: np.ndarray, names: list[str], f: dict, r: dict, n: int = 3) -> list[dict]:
    contrib = {}
    for name, xv, wv in zip(names, x_std, w[1:]):
        fam = FAMILY[name.split("=")[0]]
        if "=" in name and not (f[name.split("=")[0]] == name.split("=", 1)[1]):
            continue                                                   # only the categories this line actually has
        contrib[fam] = contrib.get(fam, 0.0) + float(xv * wv)
    top = sorted(contrib.items(), key=lambda kv: -abs(kv[1]))[:n]
    vals = {"sup": r.get("supplier_name") or r["supplier_id"], "cat": (r.get("material_category") or "").lower() or "these",
            "plant": r["plant_id"], "inc": r.get("incoterm") or "–", "tier": (r.get("risk_tier") or "").lower(),
            "plt": int(f["promised_lt"]), "std": int(r.get("std_lt") or f["promised_lt"]), "qty": f"{int(r['ordered_qty'] or 0):,}",
            "son": round(f["sup_ontime"] * 100), "mon": round(f["mat_ontime"] * 100), "open": int(f["sup_open"])}
    return [{"factor": fam, "effect": "raises" if c > 0 else "lowers", "weight": round(c, 3),
             "text": _PLAIN[fam][0 if c > 0 else 1].format(**vals)} for fam, c in top if abs(c) >= 0.03]


def score(force: bool = False) -> dict:
    """Predictions for every line still open at the data cut-off (computed once per model version)."""
    m = active_model()
    if not m:
        return {"model": None, "items": []}
    key = (m["id"], A.as_of_date())
    if not force and key in _CACHE:
        return _CACHE[key]
    mo = m["model"]; D = Design(spec=mo["design"]); w = np.array(mo["w"]); wr = np.array(mo["wr"])
    rows, as_of = _lines(), _d(A.as_of_date())
    hist = _known(rows, as_of)
    open_rows = [r for r in rows if not (r["delivery_status"] == "RECEIVED" and r["receipt_date"] and _d(r["receipt_date"]) <= as_of)]
    feats = _features(open_rows, hist, lambda r: as_of)
    X = D.X(feats); P = predict_p(w, X); DLY = np.hstack([np.ones((len(X), 1)), X]) @ wr
    items = []
    for r, f, x, p, dly in zip(open_rows, feats, X, P, DLY):
        prom = _d(r["promised_date"]); overdue = (as_of - prom).days
        if overdue > 0:                                                  # already past its promise and not received
            lh = mo["late_hist"].get(r["supplier_id"]) if len(mo["late_hist"].get(r["supplier_id"], [])) >= 5 else mo["late_hist"].get("*", [])
            beyond = [d for d in lh if d >= overdue]
            exp_delay = float(np.mean(beyond)) if beyond else overdue + 3.0
            p_late, band = 1.0, "LATE"
        else:
            p_late = float(p); exp_delay = max(0.0, float(dly)) if p_late >= 0.5 else max(0.0, float(dly)) * p_late
            band = next(b for t, b in BANDS if p_late >= t)
        value = float(r["ordered_qty"] or 0) * float(r["unit_price_inr"] or 0)
        items.append({"po_id": r["po_id"], "po_line": r["po_line"], "supplier_id": r["supplier_id"], "supplier_name": r["supplier_name"],
                      "material_id": r["material_id"], "material_name": r["material_name"], "plant_id": r["plant_id"],
                      "promised_date": prom.isoformat(), "days_to_due": -overdue, "status": "PAST_DUE" if overdue > 0 else "OPEN",
                      "p_late": round(p_late, 3), "risk_band": band, "expected_delay_days": round(exp_delay, 1),
                      "predicted_arrival": (prom + dt.timedelta(days=round(exp_delay))).isoformat() if p_late >= 0.5 else prom.isoformat(),
                      "value_inr": round(value), "value_at_risk_inr": round(value * p_late),
                      "drivers": _drivers(x, w, D.names, f, r) if overdue <= 0 else
                                 [{"factor": "overdue", "effect": "raises", "weight": 1.0, "text": f"already {overdue} days past the promise date and not received"}]})
    items.sort(key=lambda i: (i["status"] != "OPEN", -i["p_late"], i["promised_date"]))   # predictions first, then lines already late
    out = {"model": {k: m[k] for k in ("id", "version", "trained_at")} | {"auc": m["metrics"]["holdout"].get("auc")}, "as_of": as_of.isoformat(), "items": items}
    _CACHE.clear(); _CACHE[key] = out
    return out


def save_predictions(res: dict) -> int:
    """Keep what we predicted, and when (audit + later accuracy tracking)."""
    if not res.get("model") or not res["items"]:
        return 0
    rows = [{"po": i["po_id"], "ln": i["po_line"], "s": i["supplier_id"], "p": i["p_late"], "b": i["risk_band"], "d": i["expected_delay_days"],
             "a": i["predicted_arrival"], "dr": json.dumps(i["drivers"])} for i in res["items"]]
    with engine.begin() as c:
        c.execute(text("DELETE FROM public.ml_predictions WHERE model_id = :m AND as_of = :ao"), {"m": res["model"]["id"], "ao": res["as_of"]})
        c.execute(text("""INSERT INTO public.ml_predictions (model_id, as_of, po_id, po_line, supplier_id, p_late, risk_band, expected_delay_days, predicted_arrival, drivers)
                          SELECT :m, CAST(:ao AS date), x.po, x.ln, x.s, x.p, x.b, x.d, CAST(x.a AS date), CAST(x.dr AS jsonb)
                          FROM jsonb_to_recordset(CAST(:rows AS jsonb)) AS x(po text, ln int, s text, p float, b text, d float, a text, dr text)"""),
                  {"m": res["model"]["id"], "ao": res["as_of"], "rows": json.dumps(rows)})
    return len(rows)


def late_risk(supplier_id: str | None = None, min_band: str | None = None, horizon_days: int | None = None) -> dict:
    res = score()
    order = {"LATE": 3, "HIGH": 2, "MEDIUM": 1, "LOW": 0}
    items = [i for i in res["items"] if (not supplier_id or i["supplier_id"] == supplier_id)
             and (not min_band or order[i["risk_band"]] >= order[min_band])
             and (horizon_days is None or i["days_to_due"] <= horizon_days)]
    open_items = [i for i in items if i["status"] == "OPEN"]
    summary = {"lines": len(items), "past_due": sum(i["status"] == "PAST_DUE" for i in items),
               "high_risk_open": sum(i["risk_band"] == "HIGH" for i in open_items), "medium_risk_open": sum(i["risk_band"] == "MEDIUM" for i in open_items),
               "value_at_risk_inr": sum(i["value_at_risk_inr"] for i in items),
               "expected_late_open": round(sum(i["p_late"] for i in open_items), 1)}
    return {**res, "items": items, "summary": summary}


def supplier_forecast(horizon_days: int = 30) -> list[dict]:
    """Expected on-time rate of each supplier's lines due in the next N days vs its last 60 days of actual deliveries."""
    res = score(); as_of = _d(A.as_of_date())
    rec = {}                                   # last 60 days of KNOWN outcomes (incl. lines already past due: not only received ones)
    for r in _known(_lines(), as_of):
        if as_of - dt.timedelta(days=60) < _d(r["_known_at"]) <= as_of:
            b = rec.setdefault(r["supplier_id"], {"n": 0, "on": 0}); b["n"] += 1; b["on"] += 0 if r["_late"] else 1
    rec = {k: {"ontime": v["on"] / v["n"]} for k, v in rec.items() if v["n"]}
    by = {}
    for i in res["items"]:
        if i["days_to_due"] > horizon_days:
            continue
        b = by.setdefault(i["supplier_id"], {"supplier_id": i["supplier_id"], "supplier_name": i["supplier_name"], "lines": 0, "exp_on": 0.0,
                                             "value_at_risk_inr": 0, "past_due": 0, "high": 0})
        b["lines"] += 1; b["exp_on"] += 1 - i["p_late"]; b["value_at_risk_inr"] += i["value_at_risk_inr"]
        b["past_due"] += i["status"] == "PAST_DUE"; b["high"] += i["risk_band"] == "HIGH"
    out = []
    for sid, b in by.items():
        pred = 100 * b["exp_on"] / b["lines"]
        last = rec.get(sid); last_on = round(100 * float(last["ontime"]), 1) if last and last["ontime"] is not None else None
        delta = None if last_on is None else round(pred - last_on, 1)
        out.append({**{k: v for k, v in b.items() if k != "exp_on"}, "predicted_on_time_pct": round(pred, 1), "last_60d_on_time_pct": last_on,
                    "change_pts": delta, "outlook": "WORSENING" if delta is not None and delta <= -5 else "IMPROVING" if delta is not None and delta >= 5 else "STABLE"})
    return sorted(out, key=lambda x: x["predicted_on_time_pct"])


def quality_watch(weeks: int = 12) -> dict:
    """p-chart per supplier x material: this week's rejection rate vs the expected rate from earlier weeks (3-sigma limit)."""
    as_of = _d(A.as_of_date())
    rows = A.query(f"""SELECT supplier_id, material_id, date_trunc('week', inspection_ts)::date wk, SUM(attribution_weight) AS defects
                       FROM {S}.v_defect_attribution WHERE inspection_ts::date <= CAST(:a AS date) AND inspection_ts::date > CAST(:a AS date) - :d
                       GROUP BY 1, 2, 3 ORDER BY 1, 2, 3""", a=as_of.isoformat(), d=weeks * 7)
    names = {r["supplier_id"]: r["supplier_name"] for r in A.query(f"SELECT supplier_id, supplier_name FROM {S}.dim_supplier")}
    mats = {r["material_id"]: r["material_name"] for r in A.query(f"SELECT material_id, material_name FROM {S}.dim_material")}
    all_weeks = sorted({str(r["wk"])[:10] for r in rows})
    if len(all_weeks) < 5:
        return {"anomalies": [], "trend": [], "weeks": weeks, "as_of": as_of.isoformat(), "method": "not enough weeks of defect data"}
    recent = set(all_weeks[-2:]); base_weeks = all_weeks[:-2]
    grid = {}
    for r in rows:
        grid.setdefault((r["supplier_id"], r["material_id"]), {})[str(r["wk"])[:10]] = float(r["defects"] or 0)
    alerts = []
    for (sid, mid), ws in grid.items():
        base = [ws.get(w, 0.0) for w in base_weeks]
        lam = max(sum(base) / len(base), 0.5)                    # expected defects per week (floor: rare events still vary)
        for w in sorted(recent):
            k = ws.get(w, 0.0); z = (k - lam) / math.sqrt(lam)
            if z >= 3 and k >= 3:
                alerts.append({"supplier_id": sid, "supplier_name": names.get(sid, sid), "material_id": mid, "material_name": mats.get(mid, mid),
                               "week": w, "defects": round(k, 1), "expected": round(lam, 1), "upper_limit": round(lam + 3 * math.sqrt(lam), 1),
                               "z": round(z, 1), "severity": "HIGH" if z >= 5 else "MEDIUM"})
    # supplier trend: least-squares slope of weekly defects, projected 4 weeks ahead
    trend = []
    for sid in sorted({k[0] for k in grid}):
        y = np.array([sum(ws.get(w, 0.0) for (s2, _), ws in grid.items() if s2 == sid) for w in all_weeks], float)
        x = np.arange(len(y), dtype=float); slope, icpt = np.polyfit(x, y, 1)
        cur = float(y[-4:].mean()); proj = max(0.0, float(icpt + slope * (x[-1] + 4)))
        trend.append({"supplier_id": sid, "supplier_name": names.get(sid, sid), "current_per_week": round(cur, 1), "projected_per_week_4w": round(proj, 1),
                      "slope_per_week": round(float(slope), 2),
                      "outlook": "WORSENING" if proj > cur * 1.15 and slope > 0 else "IMPROVING" if proj < cur * 0.85 and slope < 0 else "STABLE"})
    alerts.sort(key=lambda a: -a["z"])
    return {"anomalies": alerts, "trend": sorted(trend, key=lambda t: -t["projected_per_week_4w"]), "weeks": weeks, "as_of": as_of.isoformat(),
            "method": "defects traced to each supplier's material per week vs its own expected weekly level (Poisson control limit, 3 sigma)"}


# ---------------------------------------------------------------- scheduled job
def run(actor: str = "scheduler", retrain_days: int = 7) -> dict:
    """Retrain weekly (or when there is no model), score open lines, store the predictions, notify owners of new high risks."""
    m = active_model(); trained = None
    if not m or (now() - m["trained_at"]).days >= retrain_days:
        trained = train(actor)
    res = score(force=True); n = save_predictions(res)
    high = [i for i in res["items"] if i["risk_band"] == "HIGH" and 0 <= i["days_to_due"] <= 14]
    notified = 0
    if high:
        from .db import Notification, SessionLocal
        with read_engine.connect() as c:
            seen = {r[0] for r in c.execute(text("""SELECT subject FROM public.notifications WHERE created_at > now() - interval '7 days'
                                                     AND subject LIKE 'Predicted late:%'"""))}
        with SessionLocal() as s:
            for i in high[:20]:
                subj = f"Predicted late: {i['po_id']}/{i['po_line']} {i['supplier_name']} ({round(i['p_late'] * 100)}%)"[:200]
                if subj in seen:
                    continue
                s.add(Notification(recipient_role="PROCUREMENT", severity="MEDIUM", subject=subj, supplier_id=i["supplier_id"],
                                   body=f"{i['material_name']} due {i['promised_date']} is predicted to arrive about {i['expected_delay_days']} days late "
                                        f"(value {round(i['value_inr']):,} INR). Why: " + "; ".join(d["text"] for d in i["drivers"]) +
                                        ". Suggested: confirm the promise date with the supplier now. (Prediction - advisory only.)"))
                notified += 1
            s.commit()
    return {"trained": trained["version"] if trained else None, "scored": n, "high_risk_due_14d": len(high), "notified": notified}
