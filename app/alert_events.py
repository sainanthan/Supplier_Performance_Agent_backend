"""Actionable alerts (enhancement 1.3).

Risk alerts are recomputed by analytics.risk_alerts(); this module gives each one a STABLE key and a lifecycle
(OPEN -> ACKNOWLEDGED -> ASSIGNED -> RESPONSE_REQUESTED -> CLOSED) in public.alert_events, with a full history.
Teams / e-mail messages carry signed one-click links (acknowledge, request supplier response). A link is an HMAC token
(event id, action, recipient role, expiry) - it can only perform that one action on that one alert.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time

from sqlalchemy import text

from . import analytics as A
from .auth import _secret
from .db import Notification, SessionLocal, engine, now

OWNER = {"OTIF_DETERIORATION": "PROCUREMENT", "REPEATED_LATE_DELIVERY": "PROCUREMENT", "PAST_DUE_SUPPLY": "PROCUREMENT",
         "CONCENTRATION": "PROCUREMENT", "SUPPLY_DISRUPTION": "PROCUREMENT"}
FLOW = {"ack": ({"OPEN"}, "ACKNOWLEDGED"),
        "assign": ({"OPEN", "ACKNOWLEDGED", "ASSIGNED", "RESPONSE_REQUESTED"}, "ASSIGNED"),
        "request_response": ({"OPEN", "ACKNOWLEDGED", "ASSIGNED"}, "RESPONSE_REQUESTED"),
        "close": ({"OPEN", "ACKNOWLEDGED", "ASSIGNED", "RESPONSE_REQUESTED"}, "CLOSED"),
        "reopen": ({"CLOSED"}, "OPEN")}
LINK_ACTIONS = {"ack", "request_response"}
LINK_TTL_S = 7 * 86400


def alert_key(a: dict) -> str:
    m = a.get("metric") or {}
    return f"{a['type']}|{a.get('supplier_id') or 'ALL'}|{m.get('defect_code') or m.get('material_id') or ''}"[:200]


_LAST_SYNC = {"t": 0.0, "out": None}
SYNC_EVERY_S = 60


def sync(force: bool = False) -> dict:
    """Upsert current alerts; alerts no longer detected become inactive (history kept).
    One statement for all alerts (was one round-trip per alert, ~14 s to Neon), and skipped if it ran in the last minute:
    the alerts themselves come from the analytics cache, so re-syncing on every page view changes nothing."""
    if not force and _LAST_SYNC["out"] is not None and time.time() - _LAST_SYNC["t"] < SYNC_EVERY_S:
        return _LAST_SYNC["out"]
    alerts = A.risk_alerts()
    hist = json.dumps([{"at": now().isoformat(), "by": "system", "status": "OPEN", "note": "detected"}])
    rows, seen = [], set()
    for a in alerts:
        k = alert_key(a)
        if k in seen:                                   # one row per key (ON CONFLICT cannot touch a row twice)
            continue
        seen.add(k)
        rows.append({"k": k, "t": a["type"], "s": a["severity"], "sid": a.get("supplier_id"), "ti": a["title"], "d": a["detail"],
                     "ra": a["recommended_action"], "o": OWNER.get(a["type"], "QUALITY")})
    new = []
    with engine.begin() as c:
        if rows:
            new = [r.id for r in c.execute(text("""
              INSERT INTO public.alert_events (alert_key, type, severity, supplier_id, title, detail, recommended_action, owner_role, history)
              SELECT x.k, x.t, x.s, x.sid, x.ti, x.d, x.ra, x.o, CAST(:h AS jsonb)
              FROM jsonb_to_recordset(CAST(:rows AS jsonb)) AS x(k text, t text, s text, sid text, ti text, d text, ra text, o text)
              ON CONFLICT (alert_key) DO UPDATE SET severity = EXCLUDED.severity, title = EXCLUDED.title, detail = EXCLUDED.detail,
                 recommended_action = EXCLUDED.recommended_action, last_seen = now(), active = TRUE
              RETURNING id, (xmax = 0) AS inserted"""), {"rows": json.dumps(rows, default=str), "h": hist}) if r.inserted]
            c.execute(text("UPDATE public.alert_events SET active = FALSE WHERE NOT (alert_key = ANY(:k)) AND active"), {"k": [r["k"] for r in rows]})
    out = {"alerts": len(alerts), "new": new}
    _LAST_SYNC.update(t=time.time(), out=out)
    return out


def list_events(status: str | None = None, active_only: bool = True) -> list[dict]:
    cond = ["TRUE"]
    if active_only:
        cond.append("e.active")
    if status:
        cond.append("e.status = :st")
    return A._run(f"""SELECT e.*, s.supplier_name FROM public.alert_events e LEFT JOIN sp.dim_supplier s ON s.supplier_id = e.supplier_id
                      WHERE {' AND '.join(cond)}
                      ORDER BY CASE e.severity WHEN 'CRITICAL' THEN 0 WHEN 'HIGH' THEN 1 WHEN 'MEDIUM' THEN 2 ELSE 3 END, e.last_seen DESC""",
                  {"st": status} if status else {})


def act(event_id: int, action: str, actor: str, assignee: str | None = None, note: str | None = None) -> dict:
    if action not in FLOW:
        raise ValueError(f"action must be one of {', '.join(FLOW)}")
    allowed_from, to = FLOW[action]
    with engine.begin() as c:
        e = c.execute(text("SELECT * FROM public.alert_events WHERE id = :i FOR UPDATE"), {"i": event_id}).mappings().fetchone()
        if not e:
            raise LookupError("Alert not found")
        if e["status"] not in allowed_from:
            raise PermissionError(f"Cannot {action} an alert that is {e['status']}")
        if action == "assign":
            if not assignee or not c.execute(text("SELECT 1 FROM public.app_users WHERE username = :u AND role <> 'SUPPLIER' AND active"),
                                             {"u": assignee}).fetchone():
                raise ValueError("assignee must be an active internal user")
        hist = list(e["history"] or []) + [{"at": now().isoformat(), "by": actor, "status": to, "action": action,
                                            "assignee": assignee, "note": note}]
        c.execute(text("UPDATE public.alert_events SET status = :s, assignee = COALESCE(:a, assignee), history = CAST(:h AS jsonb) WHERE id = :i"),
                  {"s": to, "a": assignee, "h": json.dumps(hist), "i": event_id})
    with SessionLocal() as s:
        if action == "request_response" and e["supplier_id"]:
            s.add(Notification(recipient_role="SUPPLIER", supplier_id=e["supplier_id"], severity=e["severity"],
                               subject=f"Response requested: {e['title']}",
                               body=f"{e['detail']}\n\nPlease respond with containment and root cause. (alert #{event_id})"))
        if action == "assign":
            s.add(Notification(recipient_role=e["owner_role"], supplier_id=e["supplier_id"], severity=e["severity"],
                               subject=f"Alert assigned to {assignee}: {e['title']}", body=note or e["detail"]))
        s.commit()
    return {"id": event_id, "status": to}


# ---------------------------------------------------------------- signed one-click links
def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def make_link(event_id: int, action: str, role: str) -> str:
    body = _b64(json.dumps({"e": event_id, "a": action, "r": role, "x": int(time.time()) + LINK_TTL_S}).encode())
    sig = _b64(hmac.new(_secret(), b"alert-link:" + body.encode(), hashlib.sha256).digest())
    base = os.environ.get("PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/")
    return f"{base}/api/alert-links/{body}.{sig}"


def read_link(token: str) -> dict | None:
    try:
        body, sig = token.split(".")
        good = _b64(hmac.new(_secret(), b"alert-link:" + body.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(good, sig):
            return None
        d = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        if d["x"] < time.time() or d["a"] not in LINK_ACTIONS:
            return None
        return d
    except Exception:
        return None


def notify_new(event_ids: list[int]) -> int:
    """Send new HIGH/CRITICAL events to Teams / e-mail with action buttons (in-app notification is written by jobs)."""
    from .jobs import deliver_rich
    sent = 0
    for e in [x for x in list_events() if x["id"] in set(event_ids)]:
        links = {a: make_link(e["id"], a, e["owner_role"]) for a in LINK_ACTIONS}
        app = os.environ.get("PUBLIC_APP_URL", "http://localhost:5173").rstrip("/") + "/#risk"
        deliver_rich(f"[{e['severity']}] {e['supplier_name'] or ''}: {e['title']}", e["detail"], e["owner_role"],
                     [("Acknowledge", links["ack"]), ("Request supplier response", links["request_response"]), ("Assign in app", app)])
        sent += 1
    return sent
