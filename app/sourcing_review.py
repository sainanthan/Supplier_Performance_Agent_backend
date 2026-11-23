"""Sourcing review (AUTO2): when a supplier keeps failing on a material, the agent builds the case for moving that material (or part
of its volume) to a better qualified supplier, for PROCUREMENT TO DECIDE.

For every supplier x material it checks three triggers on the governed data:
  quality   repeat defects traced to this supplier's lots of the material (repeat-defect alert, or defects well above the material average)
  delivery  poor OTIF on this material, or several order lines already past due
  budget    paying above the budgeted (standard) price, and what the next quarter will cost over budget at the current run rate
  (+ supporting: the supplier's HIGH risk tier)
When a case is raised it scores every other supplier of the same material (delivery 35 %, quality 30 %, price vs budget 20 %,
lead time 10 %, risk 5 %), keeps only those that are technically compliant and have no rejected quote, and picks the best one that
is also better on the dimension(s) that triggered the case. It then writes the reason to override, a side-by-side comparison, the
alternative's advantages, the honest trade-offs, the expected impact and a trial split.
Decision support only: nothing changes in purchasing until a buyer approves the proposal (corrective-action workflow).
"""
from __future__ import annotations

import re
import time

from . import analytics as A

TTL_S = 600
_CACHE: dict = {"t": 0.0, "v": None}
W = {"delivery": .35, "quality": .30, "price": .20, "lead": .10, "risk": .05}
RISK_SCORE = {"LOW": 100, "MEDIUM": 60, "HIGH": 20}


def _inr(v) -> str:
    if v is None:
        return "–"
    v = float(v)
    if abs(v) >= 1e7:
        return f"₹{v / 1e7:,.2f} Cr"
    if abs(v) >= 1e5:
        return f"₹{v / 1e5:,.1f} L"
    return f"₹{v:,.0f}"


def _f(v, d=1):
    return None if v is None else round(float(v), d)


def _rows() -> list[dict]:
    S = A.S
    return A._run(f"""
      WITH d AS (
        SELECT material_id, material_name, material_category, supplier_id, supplier_name, risk_tier,
               COUNT(*) FILTER (WHERE measurable) AS lines,
               100.0 * AVG(otif::int) FILTER (WHERE measurable) AS otif_pct,
               COUNT(*) FILTER (WHERE delivery_status = 'PAST_DUE' AND NOT excluded) AS past_due,
               AVG(unit_price_inr) AS avg_price, MAX(standard_price_inr) AS budget_price,
               AVG(actual_lead_time_days) FILTER (WHERE delivery_status = 'RECEIVED') AS lead_time,
               SUM(received_qty) FILTER (WHERE delivery_status = 'RECEIVED' AND NOT excluded) AS received_qty,
               SUM(GREATEST(ordered_qty - COALESCE(received_qty, 0), 0)) FILTER (WHERE delivery_status <> 'RECEIVED' AND NOT excluded) AS open_qty
        FROM {S}.v_po_delivery GROUP BY 1, 2, 3, 4, 5, 6),
      q AS (SELECT material_id, supplier_id, SUM(attribution_weight) AS defects, MODE() WITHIN GROUP (ORDER BY defect_name) AS top_defect
            FROM {S}.v_defect_attribution GROUP BY 1, 2),
      r AS (SELECT DISTINCT ON (material_id, supplier_id) material_id, supplier_id, technical_compliance, approval_status,
                   quoted_unit_price_inr, lead_time_days AS quoted_lead_time
            FROM {S}.fact_rfq_quote ORDER BY material_id, supplier_id, request_date DESC),
      sh AS (SELECT material_id, supplier_id, share_pct FROM {S}.v_material_supplier_share)
      SELECT d.*, COALESCE(q.defects, 0) AS defects, q.top_defect, r.technical_compliance, r.approval_status,
             r.quoted_unit_price_inr, r.quoted_lead_time, sh.share_pct
      FROM d LEFT JOIN q USING (material_id, supplier_id) LEFT JOIN r USING (material_id, supplier_id)
             LEFT JOIN sh USING (material_id, supplier_id)""", {})


def _score(group: list[dict]) -> None:
    """Score each supplier of one material 0-100 on the five dimensions, relative to the others."""
    ppms = [g["ppm"] for g in group]
    lts = [g["lead_time"] for g in group if g["lead_time"] is not None]
    mx_ppm, lo_lt, hi_lt = max(ppms + [0]), min(lts or [0]), max(lts or [0])
    for g in group:
        q = 100.0 if mx_ppm <= 0 else 100.0 * (1 - g["ppm"] / mx_ppm)
        ratio = (g["avg_price"] or 0) / (g["budget_price"] or g["avg_price"] or 1)
        price = max(0.0, min(100.0, 100 - max(0.0, ratio - 0.9) * 250))          # 90 % of budget = 100, 130 % = 0
        lead = 100.0 if hi_lt == lo_lt or g["lead_time"] is None else 100.0 * (1 - (g["lead_time"] - lo_lt) / (hi_lt - lo_lt))
        g["scores"] = {"delivery": g["otif_pct"] or 0, "quality": q, "price": price, "lead": lead, "risk": RISK_SCORE.get(g["risk_tier"], 60)}
        g["total"] = round(sum(W[k] * v for k, v in g["scores"].items()), 1)


def _view(g: dict) -> dict:
    keep = ("supplier_id", "supplier_name", "risk_tier", "lines", "past_due", "received_qty", "open_qty", "top_defect", "technical_compliance",
            "approval_status", "quoted_unit_price_inr", "quoted_lead_time")
    return {**{k: g.get(k) for k in keep}, "otif_pct": _f(g["otif_pct"]), "ppm": _f(g["ppm"], 0), "defects": _f(g["defects"]),
            "avg_price": _f(g["avg_price"], 2), "budget_price": _f(g["budget_price"], 2), "lead_time": _f(g["lead_time"]),
            "share_pct": _f(g["share_pct"]), "price_vs_budget_pct": _f(100 * ((g["avg_price"] or 0) / g["budget_price"] - 1)) if g["budget_price"] else None,
            "score": g["total"], "scores": {k: round(v) for k, v in g["scores"].items()}}


def build(force: bool = False) -> list[dict]:
    if not force and _CACHE["v"] is not None and time.time() - _CACHE["t"] < TTL_S:
        return _CACHE["v"]
    rows = _rows()
    repeat = set()
    for a in A.risk_alerts():
        if a["type"] == "REPEATED_DEFECT":
            for m in re.findall(r"MAT-[A-Z]+-\d+", a.get("detail") or ""):
                repeat.add((a["supplier_id"], m))
    by_mat: dict[str, list[dict]] = {}
    for r in rows:
        r["otif_pct"] = float(r["otif_pct"]) if r["otif_pct"] is not None else None
        r["avg_price"] = float(r["avg_price"]) if r["avg_price"] is not None else None
        r["budget_price"] = float(r["budget_price"]) if r["budget_price"] is not None else None
        r["lead_time"] = float(r["lead_time"]) if r["lead_time"] is not None else None
        r["defects"] = float(r["defects"] or 0)
        r["received_qty"] = float(r["received_qty"] or 0)
        r["open_qty"] = float(r["open_qty"] or 0)
        r["ppm"] = 1e6 * r["defects"] / r["received_qty"] if r["received_qty"] else 0.0
        by_mat.setdefault(r["material_id"], []).append(r)
    cases = []
    for mid, group in by_mat.items():
        _score(group)
        avg_ppm = sum(g["ppm"] for g in group) / len(group)
        for inc in group:
            if (inc["share_pct"] or 0) < 8 or (inc["lines"] or 0) < 3:
                continue
            trig = []
            rep, high = (inc["supplier_id"], mid) in repeat, inc["defects"] >= 2 and inc["ppm"] >= 1.5 * max(avg_ppm, 1)
            if rep or high:
                parts = []
                if rep:
                    parts.append("the same defect keeps returning on this material (repeat-defect alert)"
                                 + (f", mostly {inc['top_defect'].lower()}" if inc.get("top_defect") else ""))
                parts.append(f"{inc['defects']:.1f} defects traced to its {inc['material_name']} lots"
                             + (f", {inc['ppm']:.0f} PPM against a material average of {avg_ppm:.0f}" if high else ""))
                trig.append({"kind": "quality", "title": "Repeat defects", "detail": "; ".join(parts)})
            if (inc["lines"] >= 4 and (inc["otif_pct"] or 0) <= 30) or inc["past_due"] >= 2:
                trig.append({"kind": "delivery", "title": "Unreliable delivery",
                             "detail": f"OTIF {inc['otif_pct'] or 0:.0f}% on this material over {inc['lines']} order lines" + (f"; {inc['past_due']} line{'s' if inc['past_due'] != 1 else ''} already past due" if inc["past_due"] else "")})
            fwd = max(inc["received_qty"], inc["open_qty"])
            over = (inc["avg_price"] - inc["budget_price"]) * fwd if inc["avg_price"] and inc["budget_price"] and inc["avg_price"] > inc["budget_price"] * 1.03 else 0
            if over >= 2e5:
                trig.append({"kind": "budget", "title": "Over budget",
                             "detail": f"paying {_inr(inc['avg_price'])} a unit against a budget of {_inr(inc['budget_price'])} (+{100 * (inc['avg_price'] / inc['budget_price'] - 1):.1f}%); "
                                       f"the next quarter at the current run rate (~{fwd:,.0f} units) would cost about {_inr(over)} over budget"})
            if inc["risk_tier"] == "HIGH" and trig:
                trig.append({"kind": "risk", "title": "High-risk supplier", "detail": "the supplier is rated HIGH risk in the supplier master"})
            kinds = {t["kind"] for t in trig}
            if not ("quality" in kinds or len(kinds - {"risk"}) >= 2):
                continue
            # the best qualified alternative that also fixes what triggered the case
            cand = []
            for g in group:
                if g is inc or (g["lines"] or 0) < 3 or g.get("technical_compliance") == "NON_COMPLIANT" or g.get("approval_status") == "REJECTED":
                    continue
                if "quality" in kinds and g["ppm"] >= inc["ppm"]:
                    continue
                if "delivery" in kinds and (g["otif_pct"] or 0) < (inc["otif_pct"] or 0) + 10:
                    continue
                if "budget" in kinds and (g["avg_price"] or 1e18) >= inc["avg_price"]:
                    continue
                if g["total"] < inc["total"] + 8:
                    continue
                cand.append(g)
            cand.sort(key=lambda g: -g["total"])
            alt = cand[0] if cand else None
            cases.append(_case(inc, alt, cand[1:5], trig, fwd, over, group))
    cases.sort(key=lambda c: (-c["priority_score"]))
    _CACHE.update(t=time.time(), v=cases)
    return cases


def _case(inc: dict, alt: dict | None, others: list[dict], trig: list[dict], fwd: float, over: float, group: list[dict]) -> dict:
    m = inc["material_name"]
    weight = {"quality": 3, "delivery": 2, "budget": 2, "risk": 1}
    pscore = sum(weight[t["kind"]] for t in trig) * 10 + (alt["total"] - inc["total"] if alt else 0)
    why = (f"{inc['supplier_name']} should be reviewed for {m}: " + "; ".join(f"{t['title'].lower()} ({t['detail']})" for t in trig) + ".")
    adv, trade, impact, action = [], [], {}, ""
    if alt:
        a, i = alt, inc
        if (a["otif_pct"] or 0) > (i["otif_pct"] or 0):
            adv.append(f"Delivers on time and in full {a['otif_pct']:.0f}% of the time against {i['otif_pct'] or 0:.0f}% (+{a['otif_pct'] - (i['otif_pct'] or 0):.0f} points)")
        if a["ppm"] < i["ppm"]:
            adv.append(f"Fewer defects on {m}: {a['ppm']:.0f} PPM against {i['ppm']:.0f} PPM" if a["ppm"] > 0 else f"No defects traced to its {m} lots, against {i['defects']:.1f} for {i['supplier_name']}")
        if a["avg_price"] and i["avg_price"] and a["avg_price"] < i["avg_price"]:
            adv.append(f"Cheaper: {_inr(a['avg_price'])} a unit against {_inr(i['avg_price'])} ({100 * (1 - a['avg_price'] / i['avg_price']):.1f}% less"
                       + (", within budget" if a["avg_price"] <= a["budget_price"] else "") + ")")
        if a["lead_time"] is not None and i["lead_time"] is not None and a["lead_time"] < i["lead_time"] - 0.5:
            adv.append(f"Shorter lead time: {a['lead_time']:.1f} days against {i['lead_time']:.1f}")
        if RISK_SCORE.get(a["risk_tier"], 60) > RISK_SCORE.get(i["risk_tier"], 60):
            adv.append(f"Lower supplier risk: {a['risk_tier'].lower()} against {i['risk_tier'].lower()}")
        if a.get("technical_compliance") == "COMPLIANT":
            adv.append("Technically compliant for this material" + (f", latest quote {a['approval_status'].lower()}" if a.get("approval_status") else ""))
        if a["past_due"] < i["past_due"]:
            adv.append(f"{a['past_due']} lines past due against {i['past_due']}")
        if (a["otif_pct"] or 0) < (i["otif_pct"] or 0):
            trade.append(f"Lower OTIF on this material ({a['otif_pct']:.0f}% against {i['otif_pct']:.0f}%)")
        if a["avg_price"] and i["avg_price"] and a["avg_price"] > i["avg_price"]:
            trade.append(f"Costs more: {_inr(a['avg_price'])} a unit against {_inr(i['avg_price'])}")
        if a["lead_time"] is not None and i["lead_time"] is not None and a["lead_time"] > i["lead_time"] + 0.5:
            trade.append(f"Longer lead time: {a['lead_time']:.1f} days against {i['lead_time']:.1f}")
        if (a["lines"] or 0) < (i["lines"] or 0):
            trade.append(f"Less order history on this material ({a['lines']} lines against {i['lines']})")
        trade.append("Capacity to take extra volume is not confirmed: ask before shifting more than a trial share")
        share = 30 if len(trig) < 3 else 50
        move_qty = fwd * share / 100
        saving = (i["avg_price"] - a["avg_price"]) * move_qty if i["avg_price"] and a["avg_price"] else 0
        late_avoided = (((a["otif_pct"] or 0) - (i["otif_pct"] or 0)) / 100) * (i["lines"] or 0) * share / 100
        defects_avoided = max(0.0, (i["ppm"] - a["ppm"]) * move_qty / 1e6)
        impact = {"share_pct": share, "move_qty": round(move_qty), "saving_inr": round(saving), "over_budget_inr": round(over),
                  "late_lines_avoided": round(late_avoided, 1), "defects_avoided": round(defects_avoided, 1)}
        action = (f"Move a trial {share}% of the next {m} orders (about {move_qty:,.0f} units a quarter) from {i['supplier_name']} to {a['supplier_name']}; "
                  f"keep {i['supplier_name']} on the rest while its corrective action runs; review OTIF and defects after 4 weeks and shift more only if "
                  f"{a['supplier_name']} holds its performance.")
    else:
        action = (f"No other current supplier of {m} is better on what failed. Keep {inc['supplier_name']} under a corrective action and run an RFQ "
                  f"for {m} to qualify a new source.")
    # numbers the page needs to re-plan the trial live (any share, any qualified supplier)
    impact = {**impact, "fwd_qty": round(fwd), "budget_price": _f(inc["budget_price"], 2), "over_budget_inr": round(over)}
    return {"id": f"{inc['supplier_id']}:{inc['material_id']}", "material_id": inc["material_id"], "material_name": m,
            "material_category": inc.get("material_category"), "severity": "HIGH" if len(trig) >= 3 or "quality" in {t["kind"] for t in trig} and len(trig) >= 2 else "MEDIUM",
            "priority_score": round(pscore, 1), "triggers": trig, "why_override": why,
            "incumbent": _view(inc), "alternative": _view(alt) if alt else None, "others": [_view(o) for o in others],
            "advantages": adv, "tradeoffs": trade, "impact": impact, "action": action, "suppliers_for_material": len(group),
            "note": "Suggestion for procurement to decide. Nothing changes in purchasing until a buyer approves it."}


def brief(case: dict) -> str:
    i, a = case["incumbent"], case["alternative"]
    lines = [f"**Sourcing review: {case['material_name']}.** " + (f"Suggest moving part of the volume from {i['supplier_name']} to **{a['supplier_name']}** (for procurement to decide)." if a
             else f"{i['supplier_name']} is failing on this material and no current supplier is clearly better; suggest an RFQ."), "",
             "#### Why review the current supplier", *[f"- **{t['title']}:** {t['detail']}" for t in case["triggers"]]]
    if a:
        lines += ["", "#### Current vs suggested", "| Measure | " + i["supplier_name"] + " | " + a["supplier_name"] + " |", "|---|---|---|",
                  f"| OTIF on this material (%) | {i['otif_pct']} | {a['otif_pct']} |", f"| Defect PPM | {i['ppm']} | {a['ppm']} |",
                  f"| Unit price (₹) | {i['avg_price']} | {a['avg_price']} |", f"| Lead time (days) | {i['lead_time']} | {a['lead_time']} |",
                  f"| Risk tier | {i['risk_tier']} | {a['risk_tier']} |", f"| Score (0-100) | {i['score']} | {a['score']} |",
                  "", "#### Advantages of " + a["supplier_name"], *[f"- {x}" for x in case["advantages"]],
                  "", "#### Trade-offs", *[f"- {x}" for x in case["tradeoffs"]]]
    lines += ["", "#### What to do", f"1. {case['action']}", "2. Procurement approves or rejects this proposal; the agent never changes a sourcing decision itself."]
    return "\n".join(lines)
