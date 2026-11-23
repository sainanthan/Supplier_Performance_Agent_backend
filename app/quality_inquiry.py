"""Supplier quality inquiry workflow (flag `quality_inquiry`).

When a supplier's material shows poor quality, the agent asks the supplier for the reason. The supplier answers in the app or by e-mail.

  DETECT   quality alerts (rising defects, repeated defect) auto-create a DRAFT inquiry with the evidence
           (job `quality_inquiries`). A quality user, or the agent tool `request_quality_reason`, can also draft one.
  SEND     a person reviews it and clicks Send:
           - e-mail to the supplier's quality contact, with a signed reply link (no login needed, valid 14 days);
             needs SMTP_* in .env. Without SMTP the e-mail is not sent, and the app says so and shows the link to copy;
           - an in-app request in that supplier's portal (always).
  RESPOND  the supplier answers through the portal or the e-mail link: reason / root cause, containment, corrective action,
           commitment date. The QUALITY owner is notified.
  CLOSE    quality accepts the answer and closes it, or turns it into a corrective action in the tracker.
Only figures calculated by the analytics layer are put in the message. Nothing is sent without a person clicking Send.
"""
from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import json
import os
import smtplib
import time
from email.message import EmailMessage

from sqlalchemy import text

from . import analytics as A
from .auth import _secret
from .db import Notification, SessionLocal, engine, now, read_engine

TTL_S = 14 * 24 * 3600
QUALITY_ALERTS = ("QUALITY_DETERIORATION", "REPEATED_DEFECT")
STATES = ("DRAFT", "SENT", "RESPONDED", "CLOSED")


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


# ---------------------------------------------------------------- signed reply links
def make_token(inq_id: int) -> str:
    body = _b64(json.dumps({"q": inq_id, "x": int(time.time()) + TTL_S}).encode())
    sig = _b64(hmac.new(_secret(), b"quality-reply:" + body.encode(), hashlib.sha256).digest())
    return f"{body}.{sig}"


def read_token(token: str) -> int | None:
    try:
        body, sig = token.split(".")
        good = _b64(hmac.new(_secret(), b"quality-reply:" + body.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(good, sig):
            return None
        d = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        return int(d["q"]) if d["x"] >= time.time() else None
    except Exception:
        return None


def reply_url(inq_id: int) -> str:
    base = os.environ.get("PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/")
    return f"{base}/api/quality-reply/{make_token(inq_id)}"


# ---------------------------------------------------------------- data access
def _row(c, inq_id: int) -> dict | None:
    r = c.execute(text("SELECT * FROM public.quality_inquiries WHERE id = :i"), {"i": inq_id}).mappings().fetchone()
    return dict(r) if r else None


def get(inq_id: int) -> dict | None:
    with read_engine.connect() as c:
        return _row(c, inq_id)


def list_all(status: str | None = None, supplier_id: str | None = None) -> list[dict]:
    q, p = "SELECT * FROM public.quality_inquiries WHERE 1=1", {}
    if status:
        q += " AND status = :st"; p["st"] = status
    if supplier_id:
        q += " AND supplier_id = :s"; p["s"] = supplier_id
    with read_engine.connect() as c:
        rows = [dict(r) for r in c.execute(text(q + " ORDER BY id DESC LIMIT 200"), p).mappings()]
        contacts = {r[0]: r[1] for r in c.execute(text("SELECT supplier_id, email FROM public.supplier_contacts"))}
    for r in rows:
        r["contact_email"] = contacts.get(r["supplier_id"])
    return rows


def contact(supplier_id: str) -> dict | None:
    with read_engine.connect() as c:
        r = c.execute(text("SELECT * FROM public.supplier_contacts WHERE supplier_id = :s"), {"s": supplier_id}).mappings().fetchone()
        return dict(r) if r else None


def save_contact(supplier_id: str, email: str, name: str | None, actor: str) -> None:
    with engine.begin() as c:
        c.execute(text("""INSERT INTO public.supplier_contacts (supplier_id, contact_name, email, updated_by, updated_at)
                          VALUES (:s, :n, :e, :a, now())
                          ON CONFLICT (supplier_id) DO UPDATE SET email = EXCLUDED.email,
                            contact_name = COALESCE(EXCLUDED.contact_name, public.supplier_contacts.contact_name),
                            updated_by = EXCLUDED.updated_by, updated_at = now()"""),
                  {"s": supplier_id, "n": name, "e": email.strip(), "a": actor})


def _hist(c, inq_id: int, entry: dict) -> None:
    c.execute(text("UPDATE public.quality_inquiries SET history = COALESCE(history, '[]'::jsonb) || CAST(:h AS jsonb) WHERE id = :i"),
              {"i": inq_id, "h": json.dumps([{"at": now().isoformat(), **entry}], default=str)})


# ---------------------------------------------------------------- evidence + draft
def evidence(supplier_id: str, material_id: str | None = None, days: int = 60) -> dict:
    """Failed inspections traced to this supplier's material in the last `days` (genealogy attribution)."""
    p = {"s": supplier_id, "d": days}
    mat = ""
    if material_id:
        mat, p["m"] = " AND material_id = :m", material_id
    rows = A._run(f"""
      SELECT material_id, defect_code, defect_name, ROUND(SUM(attribution_weight)::numeric, 1) AS defects,
             COUNT(DISTINCT inspection_id) AS inspections, COUNT(DISTINCT production_order_id) AS orders,
             MIN(inspection_ts)::date AS first_seen, MAX(inspection_ts)::date AS last_seen,
             (SELECT STRING_AGG(DISTINCT l, ', ') FROM (SELECT unnest(material_lots) AS l FROM {A.S}.v_defect_attribution d2
               WHERE d2.supplier_id = :s AND d2.material_id = v.material_id AND d2.defect_code = v.defect_code
                 AND d2.inspection_ts >= {A.S}.as_of_date() - :d LIMIT 6) z) AS lots
      FROM {A.S}.v_defect_attribution v
      WHERE supplier_id = :s AND inspection_ts >= {A.S}.as_of_date() - :d {mat}
      GROUP BY material_id, defect_code, defect_name ORDER BY SUM(attribution_weight) DESC LIMIT 5""", p)
    card = next((c for c in A.scorecard() if c["supplier_id"] == supplier_id), None)
    if not card:
        raise KeyError(f"Unknown supplier {supplier_id}")
    return {"rows": rows, "card": card, "days": days}


def _message(sup_name: str, ev: dict, material_id: str | None, due: dt.date) -> tuple[str, str, str]:
    rows, card = ev["rows"], ev["card"]
    top = rows[0] if rows else None
    focus = (material_id or (top and top["material_id"]) or card.get("material_category") or "your material")
    subject = f"Quality concern on {focus}: please explain the cause" + (f" ({top['defect_name']})" if top else "")
    lines = [f"Dear {sup_name} quality team,", "",
             f"Our incoming-quality records show a problem with material you supplied ({focus}) over the last {ev['days']} days:"]
    for r in rows:
        lines.append(f"- {r['defect_name']} ({r['defect_code']}) on {r['material_id']}: {r['defects']} defects in {r['inspections']} "
                     f"inspection(s) across {r['orders']} production order(s), {r['first_seen']} to {r['last_seen']}"
                     + (f"; lots {r['lots']}" if r.get("lots") else ""))
    if not rows:
        lines.append("- No individual defect records in this window; the concern is your overall quality trend.")
    ppm = card.get("incoming_ppm")
    lines += [f"- Your incoming PPM is {round(ppm) if ppm is not None else '–'} (grade {card.get('grade')}).", "",
              f"Please reply by {due.isoformat()} with:",
              "1. The reason / root cause of these defects.",
              "2. The containment you have put in place (stock checked, lots blocked or sorted).",
              "3. Your corrective action and the date it will be effective.", "",
              "You can answer through the supplier portal or with the reply link in this e-mail.", "",
              "Thank you,", "Supplier Quality"]
    question = "Please explain the reason for these defects, your containment and your corrective action."
    return subject, "\n".join(lines), question


def draft(supplier_id: str, material_id: str | None = None, actor: str = "system", alert_key: str | None = None,
          origin: str = "USER") -> dict:
    sup = A._run(f"SELECT supplier_name FROM {A.S}.dim_supplier WHERE supplier_id = :s", {"s": supplier_id})
    if not sup:
        raise KeyError(f"Unknown supplier {supplier_id}")
    ev = evidence(supplier_id, material_id)
    top = ev["rows"][0] if ev["rows"] else {}
    due = dt.date.today() + dt.timedelta(days=5)
    subject, message, question = _message(sup[0]["supplier_name"], ev, material_id, due)
    with engine.begin() as c:
        new_id = c.execute(text("""INSERT INTO public.quality_inquiries
            (supplier_id, material_id, defect_code, defect_name, subject, message, question, status, due_date, created_by, origin, alert_key, evidence)
            VALUES (:s, :m, :dc, :dn, :sub, :msg, :q, 'DRAFT', :due, :by, :o, :ak, CAST(:ev AS jsonb)) RETURNING id"""),
            {"s": supplier_id, "m": material_id or top.get("material_id"), "dc": top.get("defect_code"), "dn": top.get("defect_name"),
             "sub": subject, "msg": message, "q": question, "due": due, "by": actor, "o": origin, "ak": alert_key,
             "ev": json.dumps(ev["rows"], default=str)}).scalar()
        _hist(c, new_id, {"status": "DRAFT", "by": actor, "note": f"Drafted ({origin.lower()})"})
        return _row(c, new_id)


def bulk_draft(supplier_ids: list[str], actor: str, origin: str = "AGENT") -> dict:
    """One DRAFT per affected supplier, each listing that supplier's own defective materials.
    Skips suppliers with no defect records in the window and suppliers that already have an open (draft/sent) inquiry,
    whose existing inquiry is returned instead so it can be sent from the same screen. Nothing is sent here."""
    drafts, skipped, seen = [], [], set()
    with read_engine.connect() as c:
        open_q = {r["supplier_id"]: dict(r) for r in c.execute(text(
            "SELECT * FROM public.quality_inquiries WHERE status IN ('DRAFT','SENT') ORDER BY id")).mappings()}
        contacts = {r[0]: r[1] for r in c.execute(text("SELECT supplier_id, email FROM public.supplier_contacts"))}
        names = {r[0]: r[1] for r in c.execute(text(f"SELECT supplier_id, supplier_name FROM {A.S}.dim_supplier"))}
    for sid in supplier_ids:
        sid = (sid or "").strip().upper()
        if not sid or sid in seen:
            continue
        seen.add(sid)
        if sid not in names:
            skipped.append({"supplier_id": sid, "reason": "unknown supplier"}); continue
        if sid in open_q:
            q = open_q[sid]
            if q["status"] == "DRAFT":
                drafts.append({**q, "contact_email": contacts.get(sid), "supplier_name": names[sid], "existing": True})
            else:
                skipped.append({"supplier_id": sid, "supplier_name": names[sid], "reason": f"already asked on {str(q['sent_at'])[:10]} (#{q['id']}), awaiting reply"})
            continue
        if not evidence(sid)["rows"]:
            skipped.append({"supplier_id": sid, "supplier_name": names[sid], "reason": "no poor-quality / damaged material in the last 60 days"}); continue
        d = draft(sid, None, actor, None, origin=origin)
        drafts.append({**d, "contact_email": contacts.get(sid), "supplier_name": names[sid], "existing": False})
    return {"drafts": drafts, "skipped": skipped, "smtp": smtp_ready()}


def send_many(ids: list[int], actor: str) -> dict:
    """Send several drafts, each to its supplier's saved quality e-mail (portal-only when no address is saved)."""
    with read_engine.connect() as c:
        contacts = {r[0]: r[1] for r in c.execute(text("SELECT supplier_id, email FROM public.supplier_contacts"))}
    results = []
    for i in ids:
        q = get(i)
        if not q:
            results.append({"id": i, "ok": False, "error": "not found"}); continue
        email = contacts.get(q["supplier_id"])
        try:
            r = send(i, email, actor)
            results.append({"id": i, "ok": True, "supplier_id": q["supplier_id"], "to": email, "email_status": r["email_status"],
                            "email_error": r.get("email_error"), "reply_link": r["reply_link"]})
        except ValueError as e:
            results.append({"id": i, "ok": False, "supplier_id": q["supplier_id"], "error": str(e)})
    return {"results": results, "smtp": smtp_ready()}


def list_contacts() -> list[dict]:
    with read_engine.connect() as c:
        return [dict(r) for r in c.execute(text(f"""
            SELECT s.supplier_id, s.supplier_name, c.contact_name, c.email, c.updated_by, c.updated_at
            FROM {A.S}.dim_supplier s LEFT JOIN public.supplier_contacts c ON c.supplier_id = s.supplier_id
            ORDER BY s.supplier_id""")).mappings()]


def auto_draft_from_alerts() -> dict:
    """Job: one DRAFT per new quality alert that has no open inquiry yet (by alert key)."""
    made = []
    with read_engine.connect() as c:
        open_keys = {r[0] for r in c.execute(text("SELECT alert_key FROM public.quality_inquiries WHERE alert_key IS NOT NULL AND status <> 'CLOSED'"))}
    for a in A.risk_alerts():
        if a["type"] not in QUALITY_ALERTS or not a.get("supplier_id"):
            continue
        key = f"{a['type']}|{a['supplier_id']}"
        if key in open_keys:
            continue
        d = draft(a["supplier_id"], None, "agent", key, origin="AGENT")
        made.append(d["id"])
        open_keys.add(key)
    if made:
        with SessionLocal() as s:
            s.add(Notification(recipient_role="QUALITY", severity="MEDIUM", subject=f"{len(made)} supplier quality inquiry draft(s) ready to send",
                               body="The agent drafted requests asking suppliers for the reason behind poor-quality material. "
                                    "Review and send them in Risk Radar → Actions."))
            s.commit()
    return {"drafted": made}


# ---------------------------------------------------------------- send / respond / close
def smtp_ready() -> bool:
    return bool(os.environ.get("SMTP_HOST"))


def _send_email(to: str, subject: str, body: str) -> tuple[str, str | None]:
    if not smtp_ready():
        return "NOT_CONFIGURED", "SMTP is not configured (SMTP_HOST in backend/.env); the request is in the supplier portal."
    try:
        msg = EmailMessage()
        msg["Subject"], msg["From"], msg["To"] = subject, os.environ.get("SMTP_FROM", "supplier-quality@localhost"), to
        msg.set_content(body)
        with smtplib.SMTP(os.environ["SMTP_HOST"], int(os.environ.get("SMTP_PORT", "587")), timeout=20) as smtp:
            if os.environ.get("SMTP_USER"):
                smtp.starttls()
                smtp.login(os.environ["SMTP_USER"], os.environ.get("SMTP_PASSWORD", ""))
            smtp.send_message(msg)
        return "SENT", None
    except Exception as e:  # noqa: BLE001
        return "FAILED", f"{type(e).__name__}: {str(e)[:200]}"


def send(inq_id: int, email: str | None, actor: str, subject: str | None = None, message: str | None = None) -> dict:
    with engine.begin() as c:
        q = _row(c, inq_id)
        if not q:
            raise KeyError("Inquiry not found")
        if q["status"] != "DRAFT":
            raise ValueError(f"Already {q['status'].lower()}")
        subject = (subject or q["subject"]).strip()[:200]
        message = (message or q["message"]).strip()
        link = reply_url(inq_id)
        body = f"{message}\n\nReply here (no login needed, valid 14 days):\n{link}\n"
        status, err = ("NO_EMAIL", "No supplier e-mail address; the request is in the supplier portal.") if not email else _send_email(email, subject, body)
        c.execute(text("""UPDATE public.quality_inquiries SET status = 'SENT', subject = :sub, message = :msg, sent_at = now(), sent_by = :by,
                          sent_to = :to, email_status = :es, email_error = :ee WHERE id = :i"""),
                  {"sub": subject, "msg": message, "by": actor, "to": email, "es": status, "ee": err, "i": inq_id})
        _hist(c, inq_id, {"status": "SENT", "by": actor, "note": f"e-mail {status.lower().replace('_', ' ')}" + (f" to {email}" if email else "")})
    with SessionLocal() as s:                                   # in-app request in the supplier's portal (always)
        s.add(Notification(recipient_role="SUPPLIER", supplier_id=q["supplier_id"], severity="HIGH", subject=subject,
                           body=f"{message}\n\nAnswer in the portal under Quality inquiries (#{inq_id})."))
        s.commit()
    out = get(inq_id)
    out["reply_link"] = link
    return out


def respond(inq_id: int, reason: str, containment: str, corrective_action: str, commit_date: str | None, via: str, actor: str) -> dict:
    with engine.begin() as c:
        q = _row(c, inq_id)
        if not q:
            raise KeyError("Inquiry not found")
        if q["status"] not in ("SENT", "RESPONDED"):
            raise ValueError("This inquiry is not open for a reply")
        c.execute(text("""UPDATE public.quality_inquiries SET status = 'RESPONDED', reason = :r, containment = :ct, corrective_action = :ca,
                          commit_date = :cd, responded_at = now(), responded_via = :via, responded_by = :by WHERE id = :i"""),
                  {"r": reason, "ct": containment, "ca": corrective_action, "cd": commit_date or None, "via": via, "by": actor, "i": inq_id})
        _hist(c, inq_id, {"status": "RESPONDED", "by": actor, "note": f"via {via.lower()}"})
    with SessionLocal() as s:
        s.add(Notification(recipient_role="QUALITY", supplier_id=q["supplier_id"], severity="MEDIUM",
                           subject=f"Supplier replied: {q['subject'][:150]}",
                           body=f"{q['supplier_id']} answered inquiry #{inq_id} via {via.lower()}.\nReason: {reason[:500]}"))
        s.commit()
    return get(inq_id)


def close(inq_id: int, actor: str, note: str | None = None) -> dict:
    with engine.begin() as c:
        q = _row(c, inq_id)
        if not q:
            raise KeyError("Inquiry not found")
        if q["status"] == "CLOSED":
            raise ValueError("Already closed")
        c.execute(text("UPDATE public.quality_inquiries SET status = 'CLOSED', closed_at = now(), closed_by = :by, close_note = :n WHERE id = :i"),
                  {"by": actor, "n": note, "i": inq_id})
        _hist(c, inq_id, {"status": "CLOSED", "by": actor, "note": note or ""})
    return get(inq_id)
