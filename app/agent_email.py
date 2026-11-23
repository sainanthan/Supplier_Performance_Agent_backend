"""Agent email trigger (Agent Architecture, layer 6).

After a run, the verified answer is e-mailed to the chosen internal owner(s) and always written to their in-app notifications.
Recipients come from configuration only (NOTIFY_<ROLE>_EMAIL / NOTIFY_DEFAULT_EMAIL in backend/.env), never from the model.
Without SMTP_* the e-mail is not sent and the status says so; the in-app notification is still delivered.
The answer contains only figures already checked by the grounding validator. No LLM is called here.
"""
from __future__ import annotations

import html
import os
import re
import smtplib
from email.message import EmailMessage

from .db import Notification, SessionLocal, now

ROLES = {"PROCUREMENT": ["PROCUREMENT"], "QUALITY": ["QUALITY"], "BOTH": ["PROCUREMENT", "QUALITY"]}


def _inline(t: str) -> str:
    t = html.escape(t)
    t = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", t)
    t = re.sub(r"`(.+?)`", r"<code>\1</code>", t)
    return t


def md_to_html(md: str) -> str:
    """Small Markdown subset (headings, bold, bullets, numbered lists, tables) for the e-mail body."""
    out, lines, i = [], (md or "").splitlines(), 0
    while i < len(lines):
        ln = lines[i].rstrip()
        if ln.startswith("|") and i + 1 < len(lines) and re.match(r"^\|?\s*:?-{3,}", lines[i + 1]):
            head = [c.strip() for c in ln.strip("|").split("|")]
            i += 2
            rows = []
            while i < len(lines) and lines[i].startswith("|"):
                rows.append([c.strip() for c in lines[i].strip().strip("|").split("|")]); i += 1
            th = "".join(f'<th style="background:#6f3dba;color:#fff;padding:6px 10px;text-align:left">{_inline(h)}</th>' for h in head)
            trs = "".join("<tr>" + "".join(f'<td style="border-bottom:1px solid #e6e8ef;padding:6px 10px">{_inline(c)}</td>' for c in r) + "</tr>" for r in rows)
            out.append(f'<table style="border-collapse:collapse;font-size:13px;margin:8px 0"><tr>{th}</tr>{trs}</table>')
            continue
        m = re.match(r"^(#{1,4})\s+(.*)", ln)
        if m:
            out.append(f'<h{min(4, len(m.group(1)) + 1)} style="color:#1f2f59;margin:14px 0 6px">{_inline(m.group(2))}</h{min(4, len(m.group(1)) + 1)}>')
        elif re.match(r"^\s*[-*]\s+", ln):
            items = []
            while i < len(lines) and re.match(r"^\s*[-*]\s+", lines[i]):
                items.append(f"<li>{_inline(re.sub(r'^\s*[-*]\s+', '', lines[i]))}</li>"); i += 1
            out.append(f'<ul style="margin:6px 0 6px 18px;padding:0">{"".join(items)}</ul>')
            continue
        elif re.match(r"^\s*\d+\.\s+", ln):
            items = []
            while i < len(lines) and re.match(r"^\s*\d+\.\s+", lines[i]):
                items.append(f"<li>{_inline(re.sub(r'^\s*\d+\.\s+', '', lines[i]))}</li>"); i += 1
            out.append(f'<ol style="margin:6px 0 6px 18px;padding:0">{"".join(items)}</ol>')
            continue
        elif ln.strip():
            out.append(f'<p style="margin:6px 0">{_inline(ln)}</p>')
        i += 1
    return "\n".join(out)


def recipients(role_key: str) -> list[dict]:
    out = []
    for r in ROLES.get(role_key.upper(), ["PROCUREMENT"]):
        out.append({"role": r, "email": os.environ.get(f"NOTIFY_{r}_EMAIL") or os.environ.get("NOTIFY_DEFAULT_EMAIL")})
    return out


def compose(question: str, answer: str, actor: str, grounding: dict | None) -> dict:
    first = next((l.strip("# ").strip() for l in (answer or "").splitlines() if l.strip()), "Agent answer")
    subject = f"Supplier Performance Agent · {first[:110]}"
    verified = f"{grounding.get('checked')} figure(s) verified against the data" if grounding and grounding.get("grounded") else "figures checked by the grounding validator"
    body_html = (
        '<div style="font-family:Inter,Arial,sans-serif;max-width:720px;color:#1f2f59">'
        '<div style="background:linear-gradient(90deg,#17112e,#2a1d5c);color:#fff;padding:14px 18px;border-radius:10px 10px 0 0">'
        '<b style="font-size:15px">Supplier Performance Agent</b><br><span style="font-size:12px;opacity:.85">Automated insight · triggered by an agent run</span></div>'
        '<div style="border:1px solid #e3e6ee;border-top:0;padding:14px 18px;border-radius:0 0 10px 10px">'
        f'<p style="font-size:12px;color:#5a6782;margin:0 0 8px">Question asked by {html.escape(actor)}: <i>{html.escape(question)}</i></p>'
        f'{md_to_html(answer)}'
        f'<p style="font-size:11.5px;color:#0e9c86;margin-top:14px">✓ {html.escape(verified)}</p>'
        '<p style="font-size:11px;color:#8a93a6">Open the Supplier Performance Agent for the source records and drill-through.</p></div></div>')
    body_text = f"Question ({actor}): {question}\n\n{answer}\n\n{verified}\n"
    return {"subject": subject, "html": body_html, "text": body_text}


def send(question: str, answer: str, role_key: str, actor: str, grounding: dict | None = None) -> dict:
    msg = compose(question, answer, actor, grounding)
    to = recipients(role_key)
    smtp = bool(os.environ.get("SMTP_HOST"))
    results = []
    for r in to:
        if not smtp:
            status, err = "NOT_CONFIGURED", "SMTP_HOST not set in backend/.env"
        elif not r["email"]:
            status, err = "NO_ADDRESS", f"NOTIFY_{r['role']}_EMAIL not set"
        else:
            try:
                m = EmailMessage()
                m["Subject"], m["From"], m["To"] = msg["subject"], os.environ.get("SMTP_FROM", "supplier-agent@localhost"), r["email"]
                m.set_content(msg["text"]); m.add_alternative(msg["html"], subtype="html")
                with smtplib.SMTP(os.environ["SMTP_HOST"], int(os.environ.get("SMTP_PORT", "587")), timeout=20) as s:
                    if os.environ.get("SMTP_USER"):
                        s.starttls(); s.login(os.environ["SMTP_USER"], os.environ.get("SMTP_PASSWORD", ""))
                    s.send_message(m)
                status, err = "SENT_VIA_SMTP", None
            except Exception as e:  # noqa: BLE001
                status, err = "FAILED", f"{type(e).__name__}: {str(e)[:160]}"
        with SessionLocal() as s:                        # always delivered in-app
            n = Notification(recipient_role=r["role"], severity="INFO", subject=msg["subject"][:200], body=msg["text"][:4000])
            s.add(n); s.commit(); nid = n.id
        results.append({**r, "status": status, "error": err, "notification_id": nid})
    return {"subject": msg["subject"], "html": msg["html"], "sent_at": now().isoformat(timespec="seconds"),
            "from": os.environ.get("SMTP_FROM", "supplier-agent@localhost"), "smtp": smtp, "recipients": results}
