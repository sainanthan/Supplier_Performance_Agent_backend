"""Scheduled jobs (B5 / G4): refresh, snapshot, alert notifications, SLA checks.

A single daemon thread runs every `schedule_minutes` (config). Each job execution is logged in public.job_runs; a failed
job notifies ADMIN in-app (and by e-mail / Teams when SMTP_HOST / TEAMS_WEBHOOK_URL are set in backend/.env).
Set DISABLE_SCHEDULER=1 to turn the loop off (e.g. when several backend replicas run).
"""
from __future__ import annotations

import datetime as dt
import os
import smtplib
import threading
import time
import traceback
from email.message import EmailMessage

import httpx

from . import analytics as A
from .db import AlertNotice, CorrectiveAction, JobRun, Notification, SessionLocal, get_scorecard_config, now

_started = [False]
_lock = threading.Lock()


# ---------------------------------------------------------------- outbound channels (optional)
def channels() -> dict:
    return {"in_app": True, "email": bool(os.environ.get("SMTP_HOST")), "teams": bool(os.environ.get("TEAMS_WEBHOOK_URL"))}


def deliver(subject: str, body: str, role: str) -> None:
    """Send to e-mail / Teams when configured. In-app notifications are always written by the caller."""
    if os.environ.get("SMTP_HOST"):
        try:
            to = os.environ.get(f"NOTIFY_{role}_EMAIL") or os.environ.get("NOTIFY_DEFAULT_EMAIL")
            if to:
                msg = EmailMessage()
                msg["Subject"], msg["From"], msg["To"] = subject, os.environ.get("SMTP_FROM", "supplier-agent@localhost"), to
                msg.set_content(body)
                with smtplib.SMTP(os.environ["SMTP_HOST"], int(os.environ.get("SMTP_PORT", "587")), timeout=15) as smtp:
                    if os.environ.get("SMTP_USER"):
                        smtp.starttls()
                        smtp.login(os.environ["SMTP_USER"], os.environ.get("SMTP_PASSWORD", ""))
                    smtp.send_message(msg)
        except Exception:
            pass
    if os.environ.get("TEAMS_WEBHOOK_URL"):
        try:
            httpx.post(os.environ["TEAMS_WEBHOOK_URL"], json={"text": f"**{subject}**\n\n{body}"}, timeout=10)
        except Exception:
            pass


def deliver_rich(subject: str, body: str, role: str, actions: list[tuple[str, str]]) -> None:
    """Teams MessageCard / HTML e-mail with one-click action buttons (enhancement 1.3). No-op when no channel is set."""
    if os.environ.get("TEAMS_WEBHOOK_URL"):
        try:
            httpx.post(os.environ["TEAMS_WEBHOOK_URL"], timeout=10, json={
                "@type": "MessageCard", "@context": "https://schema.org/extensions", "summary": subject, "title": subject, "text": body,
                "potentialAction": [{"@type": "OpenUri", "name": n, "targets": [{"os": "default", "uri": u}]} for n, u in actions]})
        except Exception:
            pass
    if os.environ.get("SMTP_HOST"):
        to = os.environ.get(f"NOTIFY_{role}_EMAIL") or os.environ.get("NOTIFY_DEFAULT_EMAIL")
        if to:
            try:
                msg = EmailMessage()
                msg["Subject"], msg["From"], msg["To"] = subject, os.environ.get("SMTP_FROM", "supplier-agent@localhost"), to
                msg.set_content(body + "\n\n" + "\n".join(f"{n}: {u}" for n, u in actions))
                btn = "".join(f'<a href="{u}" style="display:inline-block;margin:4px;padding:8px 14px;background:#6c3cc1;color:#fff;'
                              f'border-radius:6px;text-decoration:none">{n}</a>' for n, u in actions)
                msg.add_alternative(f"<p><b>{subject}</b></p><p>{body}</p><p>{btn}</p>", subtype="html")
                with smtplib.SMTP(os.environ["SMTP_HOST"], int(os.environ.get("SMTP_PORT", "587")), timeout=15) as smtp:
                    if os.environ.get("SMTP_USER"):
                        smtp.starttls()
                        smtp.login(os.environ["SMTP_USER"], os.environ.get("SMTP_PASSWORD", ""))
                    smtp.send_message(msg)
            except Exception:
                pass


# ---------------------------------------------------------------- jobs
def job_refresh() -> dict:
    """Recompute the cached analytics (the data may have been reloaded)."""
    A.clear_cache()
    A.warm_cache()
    return {"ok": True}


def job_snapshot() -> dict:
    """Freeze a scheduled scorecard run (config + data cut-off recorded) for audit and trend of scores."""
    rows = A.scorecard()
    run_id = A.log_run("scorecard", {"scheduled": True}, rows, snapshot=True, trigger="scheduled")
    return {"run_id": run_id, "suppliers": len(rows)}


def job_alerts() -> dict:
    """Notify the owner of every NEW HIGH/CRITICAL alert, de-duplicated over `notify_dedupe_days`."""
    cfg = get_scorecard_config()
    sev = set(cfg.get("auto_notify_severities", ["CRITICAL", "HIGH"]))
    window = dt.timedelta(days=int(cfg.get("notify_dedupe_days", 7)))
    sent = 0
    owner = {"OTIF_DETERIORATION": "PROCUREMENT", "REPEATED_LATE_DELIVERY": "PROCUREMENT", "PAST_DUE_SUPPLY": "PROCUREMENT",
             "CONCENTRATION": "PROCUREMENT", "SUPPLY_DISRUPTION": "PROCUREMENT"}
    with SessionLocal() as s:
        for a in A.risk_alerts():
            if a["severity"] not in sev:
                continue
            key = f"{a['type']}|{a['supplier_id']}|{a['title']}"[:200]
            recent = s.query(AlertNotice).filter(AlertNotice.alert_key == key, AlertNotice.notified_at > now() - window).first()
            if recent:
                continue
            role = owner.get(a["type"], "QUALITY")
            s.add(Notification(recipient_role=role, supplier_id=a["supplier_id"], severity=a["severity"],
                               subject=f"[{a['severity']}] {a['supplier_name'] or ''}: {a['title']}",
                               body=f"{a['detail']}\nRecommended: {a['recommended_action']}"))
            s.add(AlertNotice(alert_key=key))
            deliver(f"[{a['severity']}] {a['title']}", a["detail"], role)
            sent += 1
        s.commit()
    out = {"notified": sent}
    from .features import is_on
    if is_on("actionable_alerts"):                       # 1.3: lifecycle + one-click actions on Teams / e-mail
        from . import alert_events as AE
        res = AE.sync(force=True)
        hi = [e["id"] for e in AE.list_events() if e["id"] in set(res["new"]) and e["severity"] in sev]
        out.update(alert_events=res["alerts"], new_events=len(res["new"]), actionable_sent=AE.notify_new(hi) if channels()["teams"] or channels()["email"] else 0)
    return out


def job_sla() -> dict:
    """Escalate corrective actions past due date while awaiting the supplier or verification."""
    today = dt.date.today()
    n = 0
    with SessionLocal() as s:
        for a in s.query(CorrectiveAction).filter(CorrectiveAction.status.in_(["APPROVED", "SUPPLIER_RESPONDED"])):
            if a.due_date and a.due_date < today:
                key = f"SLA|action|{a.id}"
                if s.query(AlertNotice).filter(AlertNotice.alert_key == key,
                                               AlertNotice.notified_at > now() - dt.timedelta(days=7)).first():
                    continue
                s.add(Notification(recipient_role=a.owner_role, supplier_id=a.supplier_id, action_id=a.id, severity="HIGH",
                                   subject=f"Action #{a.id} overdue ({(today - a.due_date).days} d): {a.title}",
                                   body=f"Status {a.status}; due {a.due_date}. Escalate per the CAPA SLA."))
                s.add(AlertNotice(alert_key=key))
                n += 1
        s.commit()
    return {"escalated": n}


def job_disruptions() -> dict:
    """Refresh disruption overlays from every provider (T3, flag twin_disruptions)."""
    from . import disruptions as D
    from . import tracker as TR
    from .features import is_on
    if not is_on("twin_disruptions"):
        return {"skipped": "flag off"}
    return D.refresh(TR.network(True, is_on("network_twin_v2"))["nodes"], trigger="scheduled")


def job_gps_retention() -> dict:
    """Delete GPS pings older than the approved retention period (config gps_retention_days)."""
    from sqlalchemy import text
    from .db import engine
    days = int(get_scorecard_config().get("gps_retention_days", 90))
    with engine.begin() as c:
        n = c.execute(text("DELETE FROM public.gps_pings WHERE ts < now() - make_interval(days => :d)"), {"d": days}).rowcount
    return {"deleted_pings": n, "retention_days": days}


def job_digest() -> dict:
    """Weekly supplier digest for the PROCUREMENT and QUALITY owners (flag workflow_extras)."""
    from .features import is_on
    if not is_on("workflow_extras"):
        return {"skipped": "workflow_extras is off"}
    from . import workflow_extras as WX
    sent = []
    for role in ("PROCUREMENT", "QUALITY"):
        d = WX.digest(role)
        with SessionLocal() as s:
            s.add(Notification(recipient_role=role, severity="INFO", subject=d["subject"], body=d["body"]))
            s.commit()
        deliver(d["subject"], d["body"], role)
        sent.append({"role": role, "top_risks": d["top_risks"], "to_approve": d["to_approve"], "overdue": d["overdue"]})
    return {"sent": sent}


def _digest_due() -> bool:
    """Once a week: on Mondays, if no successful digest ran in the last 6 days."""
    if dt.date.today().weekday() != 0:
        return False
    with SessionLocal() as s:
        last = s.query(JobRun).filter(JobRun.job == "digest", JobRun.status == "OK").order_by(JobRun.id.desc()).first()
    return not last or (now() - last.started_at).days >= 6


def job_quality_inquiries() -> dict:
    """Draft (never send) a supplier quality inquiry for each new quality alert (flag quality_inquiry)."""
    from .features import is_on
    if not is_on("quality_inquiry"):
        return {"skipped": "quality_inquiry is off"}
    from . import quality_inquiry as QI
    return QI.auto_draft_from_alerts()


def job_predict() -> dict:
    """Predictive layer: retrain weekly, score every open PO line, store the predictions, notify new high risks (flag predictive_layer)."""
    from .features import is_on
    if not is_on("predictive_layer"):
        return {"skipped": "predictive_layer is off"}
    from . import predictive as PR
    return PR.run("scheduler")


def job_roll_forward() -> dict:
    """Demo only: fill every missing day up to today so the app works as of today (flag rolling_demo_data)."""
    from .features import is_on
    if not is_on("rolling_demo_data"):
        return {"skipped": "rolling_demo_data is off"}
    from . import rollforward as RF
    return RF.run()


def job_followups() -> dict:
    """Automatic supplier follow-up for late PO lines: request, remind, escalate, close (flag auto_followup)."""
    from . import followup as FU
    return FU.run()


JOBS = {"roll_forward": job_roll_forward, "followups": job_followups, "refresh": job_refresh, "snapshot": job_snapshot, "alerts": job_alerts, "sla": job_sla, "disruptions": job_disruptions,
        "gps_retention": job_gps_retention, "digest": job_digest,
        "quality_inquiries": job_quality_inquiries, "predict": job_predict}


def run_job(name: str, trigger: str = "scheduled") -> dict:
    t0 = now()
    with SessionLocal() as s:
        jr = JobRun(job=name, status="RUNNING", started_at=t0, detail={"trigger": trigger})
        s.add(jr)
        s.commit()
        jid = jr.id
    try:
        out = JOBS[name]()
        status, err = "OK", None
    except Exception as e:  # noqa: BLE001
        out, status, err = {}, "FAILED", "".join(traceback.format_exception_only(type(e), e)).strip()[:2000]
    with SessionLocal() as s:
        jr = s.get(JobRun, jid)
        jr.status, jr.error, jr.finished_at = status, err, now()
        jr.detail = {"trigger": trigger, **out}
        if status == "FAILED":
            s.add(Notification(recipient_role="ADMIN", severity="HIGH", subject=f"Scheduled job '{name}' failed", body=err))
            deliver(f"Job {name} failed", err, "ADMIN")
        s.commit()
    return {"job": name, "status": status, "error": err, **out}


def recent_runs(limit: int = 40) -> list[dict]:
    with SessionLocal() as s:
        return [{"id": j.id, "job": j.job, "status": j.status, "detail": j.detail, "error": j.error,
                 "started_at": j.started_at, "finished_at": j.finished_at}
                for j in s.query(JobRun).order_by(JobRun.id.desc()).limit(limit)]


def _loop() -> None:
    time.sleep(90)                                    # let the cache warm first
    while True:
        for name in ("roll_forward", "refresh", "snapshot", "alerts", "sla", "disruptions", "gps_retention", "quality_inquiries", "predict", "followups"):
            try:
                run_job(name)
            except Exception:
                pass
        try:
            if _digest_due():
                run_job("digest")
        except Exception:
            pass
        mins = max(5, int(get_scorecard_config().get("schedule_minutes", 60) or 60))
        time.sleep(mins * 60)


def start_scheduler() -> None:
    if os.environ.get("DISABLE_SCHEDULER") == "1":
        return
    with _lock:
        if _started[0]:
            return
        _started[0] = True
    try:                                              # runs cut off by a restart would otherwise show RUNNING forever
        with SessionLocal() as s:
            s.query(JobRun).filter(JobRun.status == "RUNNING").update({"status": "STOPPED", "error": "backend restarted during the run"}, synchronize_session=False)
            s.commit()
    except Exception:
        pass
    threading.Thread(target=_loop, daemon=True, name="scheduler").start()
