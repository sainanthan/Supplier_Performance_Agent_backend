"""Workflow extras (flag `workflow_extras`):

6. 8D / CAPA request drafts: built from the alert and the supplier's own figures by a fixed template (no LLM, so every figure is the
   calculated one). The draft becomes a PROPOSED corrective action; once the owner approves it, the supplier sees it in the portal and
   their response is tracked in Risk Radar -> Actions.
7. Weekly supplier digest: one briefing per owner role (PROCUREMENT / QUALITY) with what changed, new risks, overdue actions and the
   top items to act on. In-app always; e-mail / Teams when configured. Runs once a week from the scheduler (job `digest`).
8. Dispute evidence pack: everything recorded about one PO line (promise, changes and approvals, receipts, inspections, invoices,
   exclusions, related actions) in one PDF, so a disputed transaction has its evidence in one place.
"""
from __future__ import annotations

import datetime as dt
import io

from . import analytics as A
from .db import CorrectiveAction, KpiExclusion, PromiseChange, SessionLocal, current_config_version, now

CATEGORY = {"QUALITY_DETERIORATION": "QUALITY", "REPEATED_DEFECT": "QUALITY", "OVERDUE_CAPA": "RESPONSIVENESS",
            "CONCENTRATION": "COMMERCIAL"}
OWNER = {"QUALITY": "QUALITY", "RESPONSIVENESS": "QUALITY"}


def _fmt(v, d=1, unit=""):
    if v is None:
        return "–"
    return f"{round(float(v)):,}{unit}" if d == 0 else f"{round(float(v), d):,}{unit}"


# ---------------------------------------------------------------- 6. 8D / CAPA request
def capa_draft(supplier_id: str, alert_id: str | None = None) -> dict:
    sid = supplier_id
    card = next((c for c in A.scorecard() if c["supplier_id"] == sid), None)
    if not card:
        raise KeyError(f"Unknown supplier {sid}")
    alerts = [a for a in A.risk_alerts() if a["supplier_id"] == sid]
    alert = next((a for a in alerts if a["id"] == alert_id), None) or (alerts[0] if alerts else None)
    defects = sorted(A.defect_breakdown(sid), key=lambda d: -float(d["defects"] or 0))[:3]
    issues = A.supplier_issues(sid)
    overdue = [i for i in issues if i.get("is_overdue")]
    cat = CATEGORY.get(alert["type"], "DELIVERY") if alert else "QUALITY"
    sev = alert["severity"] if alert else "MEDIUM"
    today = dt.date.today()
    containment, rootcause = today + dt.timedelta(days=2), today + dt.timedelta(days=14)
    subject = alert["title"] if alert else "Performance recovery"
    lines = [
        f"8D corrective-action request: {card['supplier_name']} ({sid})",
        f"Issued {today.isoformat()} · data as of {A.as_of_date()} · priority {sev}",
        "",
        "D0 · Why we are writing",
        f"{subject}. {alert['detail']}" if alert else "Your performance is below the agreed level on the measures below.",
        "",
        "D2 · Problem description (our data)",
        f"- OTIF: {_fmt(card.get('otif_pct'), 1, '%')} · grade {card.get('grade')} · score {_fmt(card.get('composite_score'))}",
        f"- Incoming PPM: {_fmt(card.get('incoming_ppm'), 0)} · lead-time variance: {_fmt(card.get('avg_lead_time_variance_days'), 1, ' days')}",
        f"- Past-due PO lines: {card.get('past_due_lines') or 0} · open NCRs: {_fmt(card.get('open_ncr'), 0)} · overdue CAPAs: {_fmt(card.get('overdue_capa'), 0)}",
    ]
    if defects:
        lines.append("- Main defects attributed to your material: " + "; ".join(
            f"{d['defect_name']} ({d['defect_code']}, {d['material_id']}, {_fmt(d['defects'], 1)} defects)" for d in defects))
    if overdue:
        lines.append(f"- Oldest overdue NCRs: " + ", ".join(f"{i['ncr_id']} ({i.get('open_age_days')} days)" for i in overdue[:5]))
    lines += [
        "",
        "What we ask you to do",
        f"D3 · Containment by {containment.isoformat()}: protect our line (sort or replace affected stock, confirm safe shipments).",
        f"D4-D5 · Root cause and permanent corrective action by {rootcause.isoformat()}, with evidence.",
        "D6 · Implementation and validation results.",
        "D7 · Prevention: how the fix is made permanent (process, control plan, training).",
        "D8 · Closure: we verify effectiveness on the next deliveries before closing.",
    ]
    if alert and alert.get("recommended_action"):
        lines += ["", f"Our recommendation: {alert['recommended_action']}"]
    lines += ["", "Please reply through the supplier portal. Figures are calculated from our records and the approved KPI definitions."]
    return {"supplier_id": sid, "title": f"8D request: {subject}"[:200], "category": cat, "priority": sev if sev in ("CRITICAL", "HIGH") else "MEDIUM",
            "owner_role": OWNER.get(cat, "PROCUREMENT"), "description": "\n".join(lines), "alert_id": alert["id"] if alert else None,
            "due_date": rootcause.isoformat()}


# ---------------------------------------------------------------- 7. weekly digest
def digest(role: str) -> dict:
    """Weekly briefing for one owner role. Figures come from analytics and the action tracker only."""
    role = role.upper()
    cards = A.scorecard()
    alerts = A.risk_alerts()
    quality_types = ("QUALITY_DETERIORATION", "REPEATED_DEFECT", "OVERDUE_CAPA")
    mine = [a for a in alerts if (a["type"] in quality_types) == (role == "QUALITY")]
    top = [a for a in mine if a["severity"] in ("CRITICAL", "HIGH")][:5]
    today = dt.date.today()
    with SessionLocal() as s:
        acts = s.query(CorrectiveAction).filter(CorrectiveAction.owner_role == role).all()
        pcs = s.query(PromiseChange).filter_by(status="PROPOSED").count() if role == "PROCUREMENT" else 0
    pending = [a for a in acts if a.status == "PROPOSED"]
    overdue = [a for a in acts if a.due_date and a.due_date < today and a.status in ("APPROVED", "SUPPLIER_RESPONDED")]
    waiting = [a for a in acts if a.status == "SUPPLIER_RESPONDED"]
    ov = A.overview()
    worst = sorted(cards, key=lambda c: c["composite_score"])[:3]
    lines = [f"Week of {today.isoformat()} · data as of {ov['as_of_date']}",
             f"Portfolio: OTIF {_fmt(ov['otif_pct'], 1, '%')} · incoming PPM {_fmt(ov['incoming_ppm'], 0)} · "
             f"{ov['past_due_lines']} past-due PO lines · {_fmt(ov['overdue_capa'], 0)} overdue CAPAs",
             "",
             f"Your queue: {len(pending)} action(s) to approve · {len(waiting)} supplier response(s) to verify · {len(overdue)} overdue"
             + (f" · {pcs} promise-date change(s) to decide" if pcs else ""),
             "",
             f"Top risks for {role.lower()} ({len(top)} critical/high):"]
    lines += [f"- [{a['severity']}] {a['supplier_name'] or 'Portfolio'}: {a['title']}" for a in top] or ["- none"]
    lines += ["", "Lowest scores: " + ", ".join(f"{c['supplier_name']} ({c['grade']}, {_fmt(c['composite_score'])})" for c in worst),
              "", "Open Risk Radar → Actions to act on these."]
    subject = f"Weekly supplier digest · {role.title()} · {len(top)} top risk(s), {len(pending) + len(overdue)} action(s) need you"
    return {"role": role, "subject": subject, "body": "\n".join(lines), "top_risks": len(top), "to_approve": len(pending),
            "overdue": len(overdue), "to_verify": len(waiting)}


# ---------------------------------------------------------------- 8. dispute evidence pack
def evidence(po_id: str, po_line: int) -> dict:
    p = {"p": po_id, "l": po_line}
    row = A._run(f"SELECT * FROM {A.S}.v_po_delivery WHERE po_id=:p AND po_line=:l", p)
    if not row:
        raise KeyError(f"PO line {po_id}/{po_line} not found")
    po = row[0]
    q = lambda sql: A._run(sql, p)                                                                    # noqa: E731
    grn = q(f"SELECT grn_id, receipt_date, received_qty, accepted_qty, rejected_qty FROM {A.S}.fact_grn WHERE po_id=:p AND po_line=:l ORDER BY receipt_date")
    insp = q(f"SELECT inspection_lot_id, inspection_date, inspected_qty, accepted_qty, rejected_qty, defect_code, usage_decision "
             f"FROM {A.S}.fact_incoming_inspection WHERE po_id=:p AND po_line=:l ORDER BY inspection_date")
    inv = q(f"SELECT invoice_id, invoice_date, invoiced_qty, invoice_unit_price_inr, qty_match, price_match FROM {A.S}.v_invoice_match WHERE po_id=:p AND po_line=:l")
    with SessionLocal() as s:
        pcs = [{"requested": x.created_at, "by": x.requested_by, "old": x.old_promise, "new": x.new_promise, "reason": x.reason,
                "status": x.status, "decided_by": x.decided_by, "approved_at": x.approved_at}
               for x in s.query(PromiseChange).filter_by(po_id=po_id, po_line=po_line).order_by(PromiseChange.id)]
        exc = [{"reason": e.reason, "approved_by": e.approved_by, "at": e.created_at, "revoked_at": e.revoked_at, "revoked_by": e.revoked_by}
               for e in s.query(KpiExclusion).filter_by(entity_type="PO_LINE", po_id=po_id, po_line=po_line)]
        acts = [{"id": a.id, "title": a.title, "status": a.status, "history": a.history or []}
                for a in s.query(CorrectiveAction).filter(CorrectiveAction.supplier_id == po["supplier_id"],
                                                          CorrectiveAction.description.contains(po_id))]
    ver = current_config_version()[0]
    return {"po": {k: A._clean(v) for k, v in po.items()}, "promise_changes": pcs, "grn": grn, "inspections": insp, "invoices": inv,
            "exclusions": exc, "actions": acts, "config_version": ver, "as_of_date": str(A.as_of_date()),
            "generated_at": now().isoformat(timespec="seconds")}


def evidence_pdf(ev: dict, by: str) -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    po = ev["po"]
    st = getSampleStyleSheet()
    purple = colors.HexColor("#6c3cc1")
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=15 * mm, rightMargin=15 * mm, topMargin=15 * mm, bottomMargin=16 * mm,
                            title=f"Evidence pack {po['po_id']}/{po['po_line']}")

    def tbl(rows, widths=None):
        return Table(rows, colWidths=widths, repeatRows=1, style=TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), purple), ("TEXTCOLOR", (0, 0), (-1, 0), colors.white), ("FONTSIZE", (0, 0), (-1, -1), 8),
            ("GRID", (0, 0), (-1, -1), 0.25, colors.lightgrey), ("VALIGN", (0, 0), (-1, -1), "TOP")]))

    def section(title, head, rows, empty):
        out = [Spacer(1, 6), Paragraph(title, st["Heading3"])]
        out.append(tbl([head] + [[str(c if c is not None else "–") for c in r] for r in rows]) if rows else Paragraph(empty, st["Normal"]))
        return out

    yes = lambda b: "yes" if b else "no"                                                               # noqa: E731
    el = [Paragraph(f"Dispute evidence pack · {po['po_id']} line {po['po_line']}", st["Title"]),
          Paragraph(f"{po['supplier_name']} ({po['supplier_id']}) · {po['material_name']} ({po['material_id']}) · plant {po['plant_id']}", st["Normal"]),
          Spacer(1, 6),
          tbl([["Fact", "Value"],
               ["PO date / incoterm", f"{po['po_date']} · {po['incoterm']}"],
               ["Original promise date", str(po["original_promised_date"])],
               ["Promise date used for KPIs", f"{po['promised_date']}" + (f" (approved change by {po['promise_approved_by']})" if po["promise_changed"] else " (no approved change)")],
               ["Receipt date (last / first)", f"{po['receipt_date']} / {po['first_receipt_date']} · {po['n_receipts']} receipt(s)"],
               ["Ordered / received / by promise", f"{po['ordered_qty']:,} / {po['received_qty'] or 0:,} / {po['qty_by_promise'] or 0:,}"],
               ["Delay", f"{po['delay_days']} days ({po['delay_working_days']} working days)" if po["delay_days"] is not None else "–"],
               ["KPI result", f"on time {yes(po['on_time'])} · in full {yes(po['in_full'])} · OTIF {yes(po['otif'])} · measurable {yes(po['measurable'])}"],
               ["Excluded from KPIs", f"{yes(po['excluded'])}" + (f" · {po['exclusion_reason']}" if po["excluded"] else "")],
               ["Price", f"PO {po['unit_price_inr']:,.2f} INR vs standard {po['standard_price_inr']:,.2f} INR"]], [55 * mm, 125 * mm])]
    el += section("Promise-date changes", ["Requested", "By", "Old", "New", "Reason", "Status", "Decided by", "Approved at"],
                  [[str(x["requested"])[:16], x["by"], x["old"], x["new"], x["reason"], x["status"], x["decided_by"], str(x["approved_at"] or "")[:16]] for x in ev["promise_changes"]],
                  "No promise-date change was requested for this line.")
    el += section("Goods receipts (GRN)", ["GRN", "Date", "Received", "Accepted", "Rejected"],
                  [[g["grn_id"], g["receipt_date"], g["received_qty"], g["accepted_qty"], g["rejected_qty"]] for g in ev["grn"]],
                  "No GRN extract loaded; the receipt date above comes from the PO receipt record.")
    el += section("Incoming inspection", ["Lot", "Date", "Inspected", "Accepted", "Rejected", "Defect", "Decision"],
                  [[i["inspection_lot_id"], i["inspection_date"], i["inspected_qty"], i["accepted_qty"], i["rejected_qty"], i["defect_code"], i["usage_decision"]] for i in ev["inspections"]],
                  "No incoming-inspection extract loaded for this line.")
    el += section("Invoices (three-way match)", ["Invoice", "Date", "Qty", "Unit price", "Qty match", "Price match"],
                  [[i["invoice_id"], i["invoice_date"], i["invoiced_qty"], i["invoice_unit_price_inr"], yes(i["qty_match"]), yes(i["price_match"])] for i in ev["invoices"]],
                  "No invoice extract loaded for this line.")
    el += section("KPI exclusions", ["Reason (evidence)", "Approved by", "At", "Revoked"],
                  [[e["reason"], e["approved_by"], str(e["at"])[:16], f"{str(e['revoked_at'])[:16]} by {e['revoked_by']}" if e["revoked_at"] else "active"] for e in ev["exclusions"]],
                  "This line has never been excluded from the KPIs.")
    el += section("Related corrective actions", ["#", "Title", "Status", "History"],
                  [[a["id"], a["title"], a["status"], "; ".join(f"{h.get('at', '')[:10]} {h.get('status', '')} {h.get('by', '')}" for h in a["history"])] for a in ev["actions"]],
                  "No corrective action references this PO.")
    el += [Spacer(1, 10), Paragraph(f"Generated {ev['generated_at']} by {by} · data as of {ev['as_of_date']} · KPI definitions "
                                    f"v{ev['config_version']} · read-only extract of recorded data.", st["Italic"])]
    doc.build(el)
    return buf.getvalue()
