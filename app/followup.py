"""Automatic supplier follow-up (AUTO1) - the first step from "the agent recommends" to "the agent acts".

For every late (past-due, open, not excluded) PO line the agent, on its own and inside an approved policy:
  1. REQUESTED  asks the supplier, through the supplier portal, to confirm a new delivery date and the reason within
                `reply_hours` (one portal message per supplier listing all its late lines; procurement is told in-app)
  2. REMINDED   reminds the supplier once the reply window has passed
  3. ESCALATED  escalates to procurement (in-app + e-mail/Teams to procurement when configured) after `escalate_hours`
  4. CONFIRMED  the supplier's reply (new date + reason) is recorded and procurement is told; the new date is an expected
                arrival, it does NOT move the promise date / OTIF baseline
  5. CLOSED     when the line is received (or no longer late)
It also auto-approves small promise-date changes (<= `auto_approve_days`, requested at least a day before the original
date) - see auto_decide_promise_change(). Suppliers are never e-mailed automatically (original brief); everything is in
the audit log and every message is in-app or in the supplier's own portal.
"""
from __future__ import annotations

import datetime as dt
import json

from sqlalchemy import text

from .db import Notification, SessionLocal, engine, get_scorecard_config

DEFAULTS = {"followup_reply_hours": 24, "followup_escalate_hours": 48, "auto_approve_days": 3}
REASONS = {"CAPACITY": "Production capacity", "MATERIAL": "Raw material shortage", "QUALITY": "Quality hold / rework",
           "TRANSPORT": "Transport / carrier delay", "CUSTOMS": "Customs / port", "SHIPPED": "Already shipped", "OTHER": "Other"}


def policy() -> dict:
    cfg = get_scorecard_config()
    return {k: cfg.get(k, v) for k, v in DEFAULTS.items()}


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _audit(action: str, detail: dict) -> None:
    try:
        from .integration import service as SV
        SV.audit("agent", action, None, detail)
    except Exception:
        pass


def _hist(c, fid: int, event: str, **kw) -> None:
    c.execute(text("UPDATE public.supplier_followups SET history = history || CAST(:h AS jsonb) WHERE id = :i"),
              {"i": fid, "h": json.dumps([{"at": _now().isoformat(), "event": event, **kw}], default=str)})


def run() -> dict:
    """One cycle: close resolved lines, request new ones, remind, escalate. Called by the scheduler (job 'followups')."""
    from .features import is_on
    if not is_on("auto_followup"):
        return {"skipped": "auto_followup is off"}
    from . import analytics as A
    pol, now = policy(), _now()
    late = A._run(f"""SELECT po_id, po_line, supplier_id, supplier_name, material_name, promised_date, ordered_qty
                     FROM {A.S}.v_po_delivery WHERE delivery_status = 'PAST_DUE' AND NOT excluded""", {})
    late_keys = {(r["po_id"], r["po_line"]) for r in late}
    out = {"late_lines": len(late), "requested": 0, "reminded": 0, "escalated": 0, "closed": 0}
    by_sup_new, by_sup_rem, esc = {}, {}, []
    with engine.begin() as c:
        open_rows = c.execute(text("SELECT * FROM public.supplier_followups WHERE stage <> 'CLOSED'")).mappings().all()
        have = {(r["po_id"], r["po_line"]) for r in open_rows}
        # one bulk statement per transition (a run can move ~100 lines; one round trip each to Neon was ~30 s)
        def move(ids, stage, col, event, why=None):
            if not ids:
                return
            ev_ = json.dumps([{"event": event, **({"why": why} if why else {})}])
            c.execute(text(f"""UPDATE public.supplier_followups SET stage = :st, {col} = now(),
                              history = history || jsonb_set(CAST(:h AS jsonb), '{{0,at}}', to_jsonb(now()::text))
                              WHERE id = ANY(:ids)"""), {"st": stage, "h": ev_, "ids": ids})
        info = {(r["po_id"], r["po_line"]): r for r in late}
        # 1) lines that are no longer late (received / excluded) close their follow-up
        gone = [r["id"] for r in open_rows if (r["po_id"], r["po_line"]) not in late_keys]
        move(gone, "CLOSED", "closed_at", "closed", "line received or no longer late"); out["closed"] = len(gone)
        # 2) new late lines -> ask the supplier (one batched insert)
        due = now + dt.timedelta(hours=float(pol["followup_reply_hours"]))
        new_lines = [r for r in late if (r["po_id"], r["po_line"]) not in have]
        if new_lines:
            h = json.dumps([{"at": now.isoformat(), "event": "requested", "reply_due": due.isoformat()}])
            c.execute(text("""INSERT INTO public.supplier_followups (po_id, po_line, supplier_id, reply_due, history)
                              VALUES (:p, :l, :s, :d, CAST(:h AS jsonb))"""),
                      [{"p": r["po_id"], "l": r["po_line"], "s": r["supplier_id"], "d": due, "h": h} for r in new_lines])
            for r in new_lines:
                by_sup_new.setdefault(r["supplier_id"], {"name": r["supplier_name"], "lines": []})["lines"].append(r)
        out["requested"] = len(new_lines)
        # 3) reminders and 4) escalations for unanswered requests
        rem, esc_ids = [], []
        for r in open_rows:
            ln = info.get((r["po_id"], r["po_line"]))
            if not ln:
                continue
            if r["stage"] == "REQUESTED" and r["reply_due"] < now:
                rem.append(r["id"]); by_sup_rem.setdefault(r["supplier_id"], {"name": ln["supplier_name"], "lines": []})["lines"].append(ln)
            elif r["stage"] == "REMINDED" and r["requested_at"] + dt.timedelta(hours=float(pol["followup_escalate_hours"])) < now:
                esc_ids.append(r["id"]); esc.append(ln)
        move(rem, "REMINDED", "reminded_at", "reminded"); out["reminded"] = len(rem)
        move(esc_ids, "ESCALATED", "escalated_at", "escalated"); out["escalated"] = len(esc_ids)
    lst = lambda ls: "\n".join(f"- {x['po_id']}/{x['po_line']} {x['material_name']}: promised {x['promised_date']}" for x in ls[:25]) + (f"\n... and {len(ls) - 25} more" if len(ls) > 25 else "")
    with SessionLocal() as s:
        for sid, g in by_sup_new.items():
            s.add(Notification(recipient_role="SUPPLIER", supplier_id=sid, severity="HIGH",
                               subject=f"Please confirm new delivery dates for {len(g['lines'])} late order line(s) by {due:%d %b %H:%M} UTC",
                               body="These order lines are past their promised date. In this portal, open 'Delivery date requests' and give a new date "
                                    "and the reason for each:\n" + lst(g["lines"])))
        for sid, g in by_sup_rem.items():
            s.add(Notification(recipient_role="SUPPLIER", supplier_id=sid, severity="HIGH",
                               subject=f"Reminder: {len(g['lines'])} delivery date request(s) still need your answer",
                               body="We have not received a new date for:\n" + lst(g["lines"])))
        if by_sup_new:
            s.add(Notification(recipient_role="PROCUREMENT", severity="INFO",
                               subject=f"Agent asked {len(by_sup_new)} supplier(s) to confirm new dates for {out['requested']} late line(s)",
                               body="Requests were posted to each supplier's portal. Replies, reminders and escalations appear in Risk Radar > Follow-ups."))
        if esc:
            s.add(Notification(recipient_role="PROCUREMENT", severity="HIGH",
                               subject=f"Escalation: {len(esc)} late line(s) with no supplier reply after {pol['followup_escalate_hours']} h",
                               body="No new delivery date was given for:\n" + "\n".join(f"- {x['supplier_name']}: {x['po_id']}/{x['po_line']} {x['material_name']}" for x in esc[:25])))
        s.commit()
    if esc:
        try:
            from .jobs import deliver                              # internal owners only (e-mail / Teams when configured)
            deliver(f"Supplier follow-up escalation: {len(esc)} line(s) without reply", "\n".join(f"{x['supplier_name']}: {x['po_id']}/{x['po_line']}" for x in esc[:25]), "PROCUREMENT")
        except Exception:
            pass
    _audit("followup:run", out)
    return out


def respond(supplier_id: str, fid: int, confirmed_date: dt.date, reason_code: str, reason_text: str, actor: str) -> dict:
    if reason_code not in REASONS:
        raise ValueError(f"reason must be one of {', '.join(REASONS)}")
    if confirmed_date < _now().date() - dt.timedelta(days=60):
        raise ValueError("the new date looks wrong (more than 60 days in the past)")
    with engine.begin() as c:
        r = c.execute(text("SELECT * FROM public.supplier_followups WHERE id = :i AND supplier_id = :s"), {"i": fid, "s": supplier_id}).mappings().fetchone()
        if not r:
            raise LookupError("request not found")
        if r["stage"] == "CLOSED":
            raise ValueError("this request is already closed")
        c.execute(text("""UPDATE public.supplier_followups SET stage = 'CONFIRMED', responded_at = now(), confirmed_date = :d,
                          reason_code = :rc, reason_text = :rt, responded_by = :a WHERE id = :i"""),
                  {"d": confirmed_date, "rc": reason_code, "rt": (reason_text or "")[:1000], "a": actor, "i": fid})
        _hist(c, fid, "confirmed", date=confirmed_date, reason=reason_code, by=actor)
    with SessionLocal() as s:
        s.add(Notification(recipient_role="PROCUREMENT", supplier_id=supplier_id, severity="INFO",
                           subject=f"{r['po_id']}/{r['po_line']}: supplier confirmed new date {confirmed_date:%d %b %Y}",
                           body=f"Reason: {REASONS[reason_code]}. {reason_text or ''}".strip()))
        s.commit()
    _audit("followup:confirmed", {"id": fid, "po": f"{r['po_id']}/{r['po_line']}", "date": str(confirmed_date), "reason": reason_code})
    return {"id": fid, "stage": "CONFIRMED"}


def listing(supplier_id: str | None = None, open_only: bool = False) -> list[dict]:
    from . import analytics as A
    where = (["supplier_id = :s"] if supplier_id else []) + (["stage <> 'CLOSED'"] if open_only else [])
    q = "SELECT * FROM public.supplier_followups" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY requested_at DESC, id DESC LIMIT 500"
    with engine.connect() as c:
        rows = [dict(r) for r in c.execute(text(q), {"s": supplier_id}).mappings().all()]
    if rows:
        meta = {(m["po_id"], m["po_line"]): m for m in A._run(
            f"""SELECT po_id, po_line, supplier_name, material_name, plant_id, promised_date, ordered_qty FROM {A.S}.v_po_delivery
                WHERE po_id || '/' || po_line = ANY(:k)""", {"k": [f"{r['po_id']}/{r['po_line']}" for r in rows]})}
        for r in rows:
            m = meta.get((r["po_id"], r["po_line"]), {})
            r.update({k: m.get(k) for k in ("supplier_name", "material_name", "plant_id", "promised_date", "ordered_qty")})
            r["reason_label"] = REASONS.get(r["reason_code"] or "", None)
            r.pop("history", None) if supplier_id else None
    return rows


def summary() -> dict:
    with engine.connect() as c:
        by = dict(c.execute(text("SELECT stage, COUNT(*) FROM public.supplier_followups GROUP BY 1")).fetchall())
        last = c.execute(text("SELECT started_at, status, detail FROM public.job_runs WHERE job = 'followups' ORDER BY id DESC LIMIT 1")).mappings().fetchone()
    return {"by_stage": by, "policy": policy(), "last_run": dict(last) if last else None, "reasons": REASONS}


def auto_decide_promise_change(old: dt.date, new: dt.date) -> str | None:
    """Policy auto-approval: a small shift (<= auto_approve_days), asked for at least one day BEFORE the original date."""
    from .features import is_on
    if not is_on("auto_followup"):
        return None
    days = (new - old).days
    if 0 < days <= int(policy()["auto_approve_days"]) and _now().date() <= old - dt.timedelta(days=1):
        return f"agent (policy: shift of {days} day(s) requested before the original date)"
    return None
