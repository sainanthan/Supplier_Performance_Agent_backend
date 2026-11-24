"""FastAPI app for the Supplier Performance Agent."""
import datetime as dt
import json
import os
import re
import threading
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager

from fastapi import FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import text

from . import alert_events as AE
from . import analytics as A
from . import auth as AU
from . import dashboards as DB
from . import exports as EX
from . import metrics as MX
from .features import all_flags, is_on, list_flags, require, set_flag
from . import jobs as J
from . import twin as T
from . import tracker as TR
from .agent import generate_narrative, iter_agent, run_agent
from .config import settings
from .db import ReadSession, read_engine
from .db import (AppConfig, ConfigVersion, Conversation, CorrectiveAction, KpiExclusion, Message, NcrResponse, Notification,
                 PromiseChange, SessionLocal, SupplierAlias, TwinScenario, User, config_hash, engine, get_scorecard_config,
                 init_db, now)


# ---- short-lived response cache for the slow read-only endpoints Home and Risk Radar open with. Any write request
# (POST/PUT/PATCH/DELETE) clears it, so a change is always visible on the next read.
_RESP: dict = {}
_RESP_LOCK = threading.Lock()


_REFRESHING: set = set()


def _resp(key: str, ttl: float, fn):
    """Fresh copy within `ttl`; an older copy (up to 20 x ttl) is returned at once while it refreshes in the background,
    so no page waits on Neon. A write request empties the cache, so the next read after a change is always recomputed."""
    hit = _RESP.get(key)
    age = time.time() - hit[0] if hit else None
    if hit and age < ttl:
        return hit[1]
    if hit and age < ttl * 20:
        with _RESP_LOCK:
            start = key not in _REFRESHING
            _REFRESHING.add(key)
        if start:
            def refresh():
                try:
                    v = fn()
                    with _RESP_LOCK:
                        _RESP[key] = (time.time(), v)
                except Exception:
                    pass
                finally:
                    _REFRESHING.discard(key)
            threading.Thread(target=refresh, daemon=True).start()
        return hit[1]
    val = fn()
    with _RESP_LOCK:
        _RESP[key] = (time.time(), val)
    return val


def _warm_fast():
    """Pre-fill the response cache at start-up so the first Home visit does not wait on Neon."""
    for f in (lambda: data_readiness(), lambda: followups_list(), lambda: jobs(), lambda: di_summary(), lambda: pred_model(),
              lambda: pred_late(None, None, None)):
        try:
            f()
        except Exception:
            pass


def _warm_all():
    threading.Thread(target=_warm_fast, daemon=True).start()
    A.warm_cache()
    try:
        T.network_cached(); T.fidelity_cached(); TR.tracker()
    except Exception:
        pass
    if is_on("network_twin_v2"):                 # T2: fill any missing route geometry in the background
        try:
            TR.ensure_routes(trigger="startup")
        except Exception:
            pass
    if is_on("role_dashboards"):                 # 1.2: pre-compute the three role dashboards
        for k in DB.ROLES_FOR:
            try:
                DB.build(k)
            except Exception:
                pass


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    AU.seed_users()
    threading.Thread(target=_warm_all, daemon=True).start()
    J.start_scheduler()
    from .integration.pipeline import start_ticker
    start_ticker()                                  # DI3 load schedules (1-minute tick; DISABLE_SCHEDULER=1 turns it off)
    yield


app = FastAPI(title="Supplier Performance Agent API", version="2.0.0", lifespan=lifespan)


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    """Bearer-token authentication + central role policy for every /api route (D2/D3)."""
    path = request.url.path
    if request.method == "OPTIONS" or not path.startswith("/api"):
        return await call_next(request)
    hdr = request.headers.get("authorization", "")
    user = AU.read_token(hdr[7:]) if hdr.lower().startswith("bearer ") else None
    if not AU.allowed(request.method, path, user.role if user else None):
        if user is None:
            return JSONResponse({"detail": "Sign in required"}, status_code=401)
        return JSONResponse({"detail": f"Role {user.role} may not {request.method} {path}"}, status_code=403)
    request.state.user = user
    tok = AU.CURRENT_USER.set(user)
    write = request.method not in ("GET", "HEAD")
    if write:
        _RESP.clear()
    try:
        return await call_next(request)
    finally:
        AU.CURRENT_USER.reset(tok)
        if write:
            _RESP.clear()
app.add_middleware(GZipMiddleware, minimum_size=1000)   # JSON payloads shrink ~5-8x
cors_origins = list(dict.fromkeys([
    *(o.strip() for o in settings.cors_origins.split(",") if o.strip()),
    "https://supplier-performance-agent-frontend-sable.vercel.app",
]))
app.add_middleware(CORSMiddleware, allow_origins=cors_origins,
                   allow_methods=["*"], allow_headers=["*"])


# ================================================================ auth
class LoginIn(BaseModel):
    username: str
    password: str


@app.post("/api/auth/login")
def login(body: LoginIn):
    with ReadSession() as s:
        u = s.get(User, body.username.strip().lower())
        if not u or not u.active or not AU.verify_password(body.password, u.password_hash):
            time.sleep(0.4)
            raise HTTPException(401, "Invalid username or password")
        ctx = AU.UserCtx(u.username, u.role, u.supplier_id, u.display_name)
    return {"token": AU.issue_token(ctx), "user": ctx.as_dict()}


@app.get("/api/auth/me")
def me(request: Request):
    return request.state.user.as_dict()


class PasswordIn(BaseModel):
    username: str
    password: str = Field(min_length=8)


@app.get("/api/users")
def users():
    with SessionLocal() as s:
        return [{"username": u.username, "display_name": u.display_name, "role": u.role, "supplier_id": u.supplier_id,
                 "active": u.active} for u in s.query(User).order_by(User.role, User.username)]


@app.put("/api/users/password")
def set_password(body: PasswordIn):
    with SessionLocal() as s:
        u = s.get(User, body.username)
        if not u:
            raise HTTPException(404, "User not found")
        u.password_hash = AU.hash_password(body.password)
        s.commit()
    return {"ok": True}


# ================================================================ health
@app.get("/api/health")
def health():
    info = {"llm_configured": settings.llm_configured, "domo_instance": f"{settings.domo_subdomain}.domo.com",
            "model": settings.domo_model}
    try:
        with read_engine.connect() as c:
            c.execute(text("SELECT 1"))
        info["database"] = "ok"
        info["coverage"] = A.data_coverage()
    except Exception as e:
        info["database"] = f"error: {str(e).splitlines()[0]}"
    return info


# ================================================================ chat
class ChatIn(BaseModel):
    conversation_id: str | None = None
    message: str = Field(min_length=1, max_length=4000)


def _start_turn(body: ChatIn) -> tuple[str, list[dict]]:
    with SessionLocal() as s:
        conv = s.get(Conversation, body.conversation_id) if body.conversation_id else None
        if not conv:
            conv = Conversation(title=body.message[:60])
            s.add(conv)
            s.flush()
        history = [{"role": m.role, "content": m.content} for m in conv.messages]
        s.add(Message(conversation_id=conv.id, role="user", content=body.message))
        s.commit()
        return conv.id, history


def _save_answer(cid: str, answer: str, steps: list, artifacts: list | None = None) -> None:
    with SessionLocal() as s:
        s.add(Message(conversation_id=cid, role="assistant", content=answer, steps=steps, artifacts=artifacts or None))
        s.commit()


@app.get("/api/conversations")
def list_convs():
    with SessionLocal() as s:
        rows = s.query(Conversation).order_by(Conversation.created_at.desc()).limit(100).all()
        return [{"id": c.id, "title": c.title, "created_at": c.created_at} for c in rows]


@app.get("/api/conversations/{cid}/messages")
def get_msgs(cid: str):
    with SessionLocal() as s:
        q = s.query(Message).filter_by(conversation_id=cid).order_by(Message.id)
        return [{"role": m.role, "content": m.content, "steps": m.steps, "artifacts": m.artifacts, "created_at": m.created_at} for m in q]


@app.delete("/api/conversations/{cid}")
def delete_conv(cid: str):
    with SessionLocal() as s:
        conv = s.get(Conversation, cid)
        if conv:
            s.delete(conv)
            s.commit()
    return {"ok": True}


_HITS: dict[str, deque] = defaultdict(deque)
CHAT_LIMIT_PER_MIN = 12


def _rate_limit(request: Request) -> None:
    """Per-client rate limit to protect Domo AI quotas (guide §11 'Rate limits')."""
    key = request.client.host if request.client else "anon"
    q, t = _HITS[key], time.monotonic()
    while q and t - q[0] > 60:
        q.popleft()
    if len(q) >= CHAT_LIMIT_PER_MIN:
        raise HTTPException(429, "Too many agent requests. Please wait a minute and try again.")
    q.append(t)


@app.post("/api/chat")
def chat(body: ChatIn, request: Request):
    _rate_limit(request)
    cid, history = _start_turn(body)
    try:
        answer, steps = run_agent(body.message, history, session_id=cid)
    except Exception as e:
        raise HTTPException(502, f"Agent error: {e}")
    _save_answer(cid, answer, steps)
    return {"conversation_id": cid, "answer": answer, "steps": steps}


@app.post("/api/chat/stream")
def chat_stream(body: ChatIn, request: Request):
    """Server-Sent Events: meta, thinking, tool_start, tool_end, final | error."""
    _rate_limit(request)
    cid, history = _start_turn(body)

    user = request.state.user

    def gen():
        AU.CURRENT_USER.set(user)          # generator runs in a worker thread
        yield f"data: {json.dumps({'type': 'meta', 'conversation_id': cid})}\n\n"
        try:
            for ev in iter_agent(body.message, history, session_id=cid):
                if ev["type"] == "final":
                    _save_answer(cid, ev["answer"], ev["steps"], ev.get("artifacts"))
                elif ev["type"] == "error":
                    _save_answer(cid, f"⚠️ {ev['message']}", ev.get("steps") or [])
                yield f"data: {json.dumps(ev, default=str)}\n\n"
        except Exception as e:  # never leave the stream hanging
            yield f"data: {json.dumps({'type': 'error', 'message': str(e)})}\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ================================================================ analytics
@app.get("/api/overview")
def overview(start: str | None = None, end: str | None = None, plant_id: str | None = None, suppliers: str | None = None):
    """suppliers = comma-separated supplier ids: KPIs for that set only (the page's filters)."""
    return A.overview(start, end, plant_id, suppliers or None)


@app.get("/api/scorecard")
def scorecard(start: str | None = None, end: str | None = None, plant_id: str | None = None):
    return A.scorecard(start, end, plant_id)


@app.get("/api/trends")
def trends(supplier_id: str | None = None, grain: str = "week", plant_id: str | None = None):
    return A.trends(supplier_id, grain, plant_id)


@app.get("/api/alerts")
def alerts(supplier_id: str | None = None):
    rows = A.risk_alerts()
    return [a for a in rows if not supplier_id or a["supplier_id"] == supplier_id]


@app.get("/api/defects")
def defects(supplier_id: str | None = None):
    return A.defect_breakdown(supplier_id)


@app.get("/api/plants")
def plants():
    return A.query(f"SELECT * FROM {A.S}.dim_plant ORDER BY plant_id")


@app.get("/api/materials")
def materials():
    return A.query(f"SELECT * FROM {A.S}.dim_material ORDER BY material_id")


@app.get("/api/materials/{material_id}/sourcing")
def sourcing(material_id: str):
    return A.material_sourcing(material_id)


@app.get("/api/concentration")
def concentration():
    return A.concentration_matrix()


@app.get("/api/concentration/trends")
def concentration_trends(plant_id: str | None = None, supplier_id: str | None = None, months: int = 12):
    return A.material_spend_trends(plant_id, supplier_id, months)


@app.post("/api/cache/clear")
def cache_clear():
    """Used by the UI refresh button and after reloading data."""
    A.clear_cache()
    threading.Thread(target=_warm_all, daemon=True).start()
    return {"ok": True}


@app.get("/api/pulse")
def pulse(plant_id: str | None = None, suppliers: str | None = None):
    """Weekly sparklines and 4-week deltas for the headline KPI tiles (optionally for a supplier set)."""
    return A.kpi_pulse(plant_id, 12, suppliers or None)


class SimulateIn(BaseModel):
    weights: dict[str, float]
    start: str | None = None
    end: str | None = None
    plant_id: str | None = None


@app.post("/api/scorecard/simulate")
def simulate(body: SimulateIn):
    """What-if: re-rank suppliers with trial weights. Nothing is saved."""
    keys = {"delivery", "quality", "responsiveness", "commercial"}
    total = sum(body.weights.values())
    if set(body.weights) != keys or total <= 0:
        raise HTTPException(422, "weights must contain delivery, quality, responsiveness, commercial")
    weights = {k: v / total for k, v in body.weights.items()}
    return A.simulate(weights, body.start, body.end, body.plant_id)


@app.get("/api/compare")
def compare(ids: str = Query(..., description="Comma-separated supplier IDs, 2 to 4")):
    sids = [i.strip() for i in ids.split(",") if i.strip()][:4]
    cards = {c["supplier_id"]: c for c in A.scorecard()}
    return [{"scorecard": cards[s], "trends": A.trends(s, "month")} for s in sids if s in cards]


# ================================================================ governance: evidence, runs, DQ, jobs, recon
@app.get("/api/evidence/{supplier_id}/{metric}")
def evidence(supplier_id: str, metric: str, limit: int = Query(500, le=5000)):
    """Drill from a score to the exact source rows, with definition and config version (F4)."""
    try:
        return A.evidence(supplier_id, metric, limit)
    except KeyError:
        raise HTTPException(404, f"Unknown metric. Use one of: {', '.join(A.EVIDENCE)}")


@app.get("/api/runs")
def runs(limit: int = Query(50, le=500)):
    return A.runs(limit)


@app.get("/api/runs/{run_id}/snapshot")
def run_snapshot(run_id: str, supplier_id: str | None = None):
    return A.snapshot(run_id, supplier_id)


@app.get("/api/dq")
def dq():
    return A.dq_results()


@app.get("/api/jobs")
def jobs():
    return _resp("jobs", 20, lambda: {"schedule_minutes": get_scorecard_config().get("schedule_minutes"), "runs": J.recent_runs(),
            "channels": J.channels()})


@app.post("/api/jobs/{job}/run")
def run_job(job: str):
    if job not in J.JOBS:
        raise HTTPException(404, f"Unknown job. Use one of: {', '.join(J.JOBS)}")
    return J.run_job(job, trigger=f"manual:{AU.actor()}")


@app.get("/api/recon")
def recon():
    return A.query("""SELECT recon_id, check_name, supplier_id, source_value, app_value, diff, passed, created_at
                      FROM public.recon_results WHERE recon_id = (SELECT recon_id FROM public.recon_results
                      ORDER BY created_at DESC LIMIT 1) ORDER BY passed, check_name""")


@app.post("/api/recon/run")
def recon_run():
    from scripts.reconcile import reconcile
    return reconcile()


@app.get("/api/ai-usage")
def ai_usage(days: int = 30):
    rows = A.query("""
      SELECT date_trunc('day', created_at)::date AS day, purpose, COUNT(*) AS calls,
             SUM(input_tokens) AS input_tokens, SUM(output_tokens) AS output_tokens,
             SUM(total_tokens) AS total_tokens, AVG(latency_ms) AS avg_latency_ms
      FROM public.ai_usage WHERE created_at > now() - make_interval(days => :d)
      GROUP BY 1, 2 ORDER BY 1""", d=days)
    tot = A.query("""SELECT COUNT(*) AS calls, COALESCE(SUM(total_tokens),0) AS total_tokens,
                            AVG(latency_ms) AS avg_latency_ms, MAX(model_id) AS model_id
                     FROM public.ai_usage WHERE created_at > now() - make_interval(days => :d)""", d=days)[0]
    return {"totals": tot, "daily": rows}


@app.get("/api/suppliers/{supplier_id}")
def supplier_detail(supplier_id: str, start: str | None = None, end: str | None = None):
    # independent cached queries: run them side by side instead of one after another (cold: ~7 s -> ~2 s)
    jobs = {"card": lambda: A.scorecard(start, end, None, supplier_id), "trends": lambda: A.trends(supplier_id, "week"),
            "open_issues": lambda: A.supplier_issues(supplier_id), "exposure": lambda: A.exposure(supplier_id),
            "defects": lambda: A.defect_breakdown(supplier_id), "alerts": lambda: A.risk_alerts()}
    r = _parallel(jobs)
    if not r["card"]:
        raise HTTPException(404, "Supplier not found")
    return {
        "scorecard": r["card"][0], "trends": r["trends"], "open_issues": r["open_issues"], "exposure": r["exposure"],
        "defects": r["defects"], "alerts": [a for a in r["alerts"] if a["supplier_id"] == supplier_id],
    }


_POOL = None


def _parallel(jobs: dict) -> dict:
    """Run independent read-only calls concurrently; each keeps the caller's context (signed-in user, etc.)."""
    import contextvars
    from concurrent.futures import ThreadPoolExecutor
    global _POOL
    if _POOL is None:
        _POOL = ThreadPoolExecutor(max_workers=8, thread_name_prefix="par")
    futs = {k: _POOL.submit(contextvars.copy_context().run, f) for k, f in jobs.items()}
    return {k: f.result() for k, f in futs.items()}


def _recommendations(card: dict, alerts: list[dict]) -> list[str]:
    t = get_scorecard_config()["recommendation_thresholds"]
    rec = []
    if (card["otif_pct"] or 0) < t["otif_floor_pct"]:
        rec.append(f"Delivery: OTIF {card['otif_pct']}% is below the {t['otif_floor_pct']}% floor; agree a recovery plan with weekly "
                   f"promise-date confirmation ({card['past_due_lines']} lines past due).")
    if (card["avg_lead_time_variance_days"] or 0) > t["lead_time_var_days"]:
        rec.append(f"Lead time: actual lead time exceeds standard by {card['avg_lead_time_variance_days']} days; "
                   "re-baseline the standard lead time or secure capacity.")
    if (card["incoming_ppm"] or 0) > t["ppm"]:
        rec.append(f"Quality: incoming PPM {card['incoming_ppm']}; request 8D on top defect codes and increase "
                   "incoming inspection sampling until two clean months.")
    if (card["overdue_capa"] or 0) > 0:
        rec.append(f"Responsiveness: {card['overdue_capa']} overdue CAPA (oldest {card['max_open_age_days']} days); "
                   "escalate to supplier quality management.")
    if (card["price_variance_pct"] or 0) > t["price_var_pct"]:
        rec.append(f"Commercial: paying {card['price_variance_pct']}% above standard price; review at next negotiation.")
    if (card["disruption_hours"] or 0) >= t["disruption_hours"]:
        rec.append(f"Continuity: {card['disruption_hours']} h of line stoppage linked to late deliveries; "
                   "consider buffer/consignment stock.")
    if any(a["type"] == "CONCENTRATION" for a in alerts):
        rec.append("Sourcing: high spend concentration on at least one material; assess dual-sourcing "
                   "(decision remains with procurement).")
    return rec or ["Performance within thresholds; continue standard monitoring."]


@app.get("/api/suppliers/{supplier_id}/review-pack")
def review_pack(supplier_id: str, start: str | None = None, end: str | None = None):
    def _acts():
        with ReadSession() as s:
            return [_action_dict(a) for a in s.query(CorrectiveAction).filter_by(supplier_id=supplier_id)
                    .order_by(CorrectiveAction.id.desc())]
    d = supplier_detail(supplier_id, start, end)     # runs its own leaf queries in parallel; never nest pool jobs (deadlock risk)
    r = _parallel({"acts": _acts, "peers": lambda: A.scorecard(start, end), "monthly": lambda: A.trends(supplier_id, "month")})
    acts, peers = r["acts"], r["peers"]
    return {
        "generated_at": now().isoformat(), "as_of_date": A.as_of_date(), "period": {"start": start, "end": end},
        **d, "monthly_trends": r["monthly"], "corrective_actions": acts,
        "peer_benchmark": {
            "suppliers": len(peers),
            "avg_otif_pct": round(sum(p["otif_pct"] or 0 for p in peers) / max(1, len(peers)), 1),
            "avg_ppm": round(sum(p["incoming_ppm"] or 0 for p in peers) / max(1, len(peers)), 1),
            "avg_composite": round(sum(p["composite_score"] for p in peers) / max(1, len(peers)), 1),
        },
        "recommendations": _recommendations(d["scorecard"], d["alerts"]),
        "weights": get_scorecard_config()["weights"],
    }


@app.post("/api/suppliers/{supplier_id}/review-pack/narrative")
def review_narrative(supplier_id: str):
    if not settings.llm_configured:
        raise HTTPException(400, "Domo token not configured")
    pack = review_pack(supplier_id)
    card = {k: v for k, v in pack["scorecard"].items()}
    prompt = ("Write a concise executive summary (max 180 words, markdown, 3 short paragraphs: performance, risks, "
              "actions) for a supplier quarterly business review. Use only these facts:\n"
              + json.dumps({"scorecard": card, "peer_benchmark": pack["peer_benchmark"],
                            "alerts": [a["title"] for a in pack["alerts"]][:8],
                            "recommendations": pack["recommendations"]}, default=str))
    try:
        from .grounding import grounded_generate
        text_, check = grounded_generate(generate_narrative, prompt, {"scorecard": card, "peer_benchmark": pack["peer_benchmark"],
                                                                     "recommendations": pack["recommendations"]})
        return {"narrative": text_, "grounding": check}
    except Exception as e:
        raise HTTPException(502, str(e))


# ================================================================ corrective actions (controlled workflow)
TRANSITIONS = {
    "PROPOSED": {"APPROVED", "REJECTED"},
    "APPROVED": {"SUPPLIER_RESPONDED", "CLOSED"},
    "SUPPLIER_RESPONDED": {"VERIFIED", "APPROVED"},
    "VERIFIED": {"CLOSED"},
    "REJECTED": set(), "CLOSED": set(),
}


def _action_dict(a: CorrectiveAction) -> dict:
    return {c.name: getattr(a, c.name) for c in CorrectiveAction.__table__.columns}


class ActionIn(BaseModel):
    supplier_id: str
    title: str = Field(min_length=3, max_length=200)
    description: str = ""
    category: str = "QUALITY"
    priority: str = "MEDIUM"
    owner_role: str = "QUALITY"
    owner_name: str | None = None
    ncr_id: str | None = None
    due_date: dt.date | None = None
    actor: str | None = None            # ignored: the actor is the signed-in user


class ActionPatch(BaseModel):
    status: str | None = None
    supplier_response: str | None = None
    owner_name: str | None = None
    due_date: dt.date | None = None
    note: str | None = None
    actor: str | None = None            # ignored: the actor is the signed-in user


@app.get("/api/actions")
def list_actions(supplier_id: str | None = None, status: str | None = None):
    with ReadSession() as s:
        q = s.query(CorrectiveAction)
        if supplier_id:
            q = q.filter_by(supplier_id=supplier_id)
        if status:
            q = q.filter_by(status=status)
        return [_action_dict(a) for a in q.order_by(CorrectiveAction.id.desc())]


@app.get("/api/actions/stats")
def action_stats():
    with SessionLocal() as s:
        rows = s.query(CorrectiveAction).all()
    today = dt.date.today()
    resp = []
    for a in rows:
        appr = next((h["at"] for h in a.history or [] if h.get("status") == "APPROVED"), None)
        if appr and a.responded_at:
            resp.append((a.responded_at - dt.datetime.fromisoformat(appr)).total_seconds() / 86400)
    by_status: dict[str, int] = {}
    for a in rows:
        by_status[a.status] = by_status.get(a.status, 0) + 1
    return {
        "total": len(rows), "by_status": by_status,
        "overdue": sum(1 for a in rows if a.due_date and a.due_date < today
                       and a.status in ("APPROVED", "SUPPLIER_RESPONDED")),
        "avg_supplier_response_days": round(sum(resp) / len(resp), 1) if resp else None,
    }


@app.post("/api/actions")
def create_action(body: ActionIn):
    with SessionLocal() as s:
        a = CorrectiveAction(**body.model_dump(exclude={"actor"}), origin="USER", status="PROPOSED",
                             history=[{"at": now().isoformat(), "by": AU.actor(), "status": "PROPOSED"}])
        if not a.due_date:
            a.due_date = dt.date.today() + dt.timedelta(days=14)
        s.add(a)
        s.commit()
        return _action_dict(a)


@app.patch("/api/actions/{action_id}")
def update_action(action_id: int, body: ActionPatch):
    with SessionLocal() as s:
        a = s.get(CorrectiveAction, action_id)
        if not a:
            raise HTTPException(404, "Action not found")
        hist = list(a.history or [])
        if body.status and body.status != a.status:
            if body.status not in TRANSITIONS.get(a.status, set()):
                raise HTTPException(409, f"Cannot move from {a.status} to {body.status}")
            # approval / verification belong to the owning function (or ADMIN)
            if body.status in ("APPROVED", "REJECTED", "VERIFIED", "CLOSED") and not AU.has_role(a.owner_role):
                raise HTTPException(403, f"Only {a.owner_role} (or ADMIN) may set {body.status}")
            a.status = body.status
            hist.append({"at": now().isoformat(), "by": AU.actor(), "status": body.status, "note": body.note})
            if body.status == "APPROVED":
                s.add(Notification(recipient_role=a.owner_role, supplier_id=a.supplier_id, severity="INFO",
                                   action_id=a.id, subject=f"Corrective action #{a.id} approved: {a.title}",
                                   body=f"Due {a.due_date}. Track supplier response in the Actions tracker."))
        if body.supplier_response is not None:
            a.supplier_response = body.supplier_response
            a.responded_at = now()
        if body.owner_name is not None:
            a.owner_name = body.owner_name
        if body.due_date is not None:
            a.due_date = body.due_date
        if body.note and not body.status:
            hist.append({"at": now().isoformat(), "by": AU.actor(), "note": body.note})
        a.history = hist
        s.commit()
        return _action_dict(a)


# ================================================================ notifications
class NotificationIn(BaseModel):
    recipient_role: str
    subject: str
    body: str = ""
    supplier_id: str | None = None
    severity: str = "INFO"


def _my_notifications(q):
    """Role-based inbox: ADMIN sees every notification; other roles only those addressed to their role (or ALL)."""
    u = AU.current_user()
    if u and u.role != "ADMIN":
        q = q.filter(Notification.recipient_role.in_([u.role, "ALL"]))
    return q


@app.get("/api/notifications")
def list_notifications(unread_only: bool = False):
    with ReadSession() as s:
        q = _my_notifications(s.query(Notification))
        if unread_only:
            q = q.filter_by(is_read=False)
        return [{c.name: getattr(n, c.name) for c in Notification.__table__.columns}
                for n in q.order_by(Notification.id.desc()).limit(100)]


@app.post("/api/notifications")
def create_notification(body: NotificationIn):
    with SessionLocal() as s:
        n = Notification(**body.model_dump())
        s.add(n)
        s.commit()
        return {"id": n.id}


@app.post("/api/notifications/read-all")
def read_all():
    with SessionLocal() as s:                       # only the caller's own inbox - never other roles' notifications
        _my_notifications(s.query(Notification)).update({"is_read": True}, synchronize_session=False)
        s.commit()
    return {"ok": True}


# ================================================================ config & exclusions
class ScorecardConfigIn(BaseModel):
    weights: dict[str, float]
    in_full_tolerance_pct: float | None = Field(None, ge=0, le=50)
    over_delivery_tolerance_pct: float | None = Field(None, ge=0, le=100)
    early_window_days: int | None = Field(None, ge=0, le=60)
    lead_time_tolerance_days: float | None = Field(None, ge=0, le=60)
    non_working_weekdays: list[int] | None = None
    ppm_target: float | None = Field(None, ge=0)
    ppm_ceiling: float | None = Field(None, gt=0)
    grade_bands: dict[str, float] | None = None
    health_bands: dict[str, float] | None = None
    risk_score: dict | None = None
    recommendation_thresholds: dict[str, float] | None = None
    trend_window_days: int | None = Field(None, ge=7, le=180)
    otif_drop_alert_pts: float | None = None
    ppm_rise_alert_pct: float | None = None
    concentration_share_pct: float | None = Field(None, ge=0, le=100)
    repeat_defect_min_weight: float | None = None
    trend_min_lines: int | None = None
    trend_min_received_units: float | None = None
    trend_alpha: float | None = Field(None, gt=0, lt=1)
    auto_notify_severities: list[str] | None = None
    notify_dedupe_days: int | None = None
    schedule_minutes: int | None = Field(None, ge=5, le=1440)
    lane_at_risk_buffer_days: float | None = Field(None, ge=0, le=30)
    plant_cover_critical_days: float | None = Field(None, ge=0, le=60)
    plant_cover_watch_days: float | None = Field(None, ge=0, le=120)
    gps_retention_days: int | None = Field(None, ge=1, le=3650)
    clear_early_window: bool = False
    note: str = Field("", max_length=500)


@app.get("/api/config/scorecard")
def get_config():
    from .db import current_config_version
    v, h = current_config_version()
    return {**get_scorecard_config(), "_version": v, "_hash": h}


@app.get("/api/config/versions")
def config_versions():
    with SessionLocal() as s:
        return [{"id": v.id, "config_hash": v.config_hash, "approved_by": v.approved_by, "note": v.note,
                 "created_at": v.created_at, "value": v.value} for v in s.query(ConfigVersion).order_by(ConfigVersion.id.desc()).limit(50)]


@app.put("/api/config/scorecard")
def put_config(body: ScorecardConfigIn):
    """ADMIN only. Every change creates an immutable ConfigVersion (who, when, why, hash) (F1)."""
    keys = {"delivery", "quality", "responsiveness", "commercial"}
    if set(body.weights) != keys or abs(sum(body.weights.values()) - 1) > 0.001:
        raise HTTPException(422, "weights must have delivery, quality, responsiveness, commercial and sum to 1")
    if body.grade_bands and not (body.grade_bands["A"] > body.grade_bands["B"] > body.grade_bands["C"]):
        raise HTTPException(422, "grade bands must satisfy A > B > C")
    if body.health_bands and not body.health_bands["risk"] > body.health_bands["watch"]:
        raise HTTPException(422, "health band 'risk' must be above 'watch'")
    if body.plant_cover_critical_days is not None and body.plant_cover_watch_days is not None \
            and body.plant_cover_watch_days <= body.plant_cover_critical_days:
        raise HTTPException(422, "plant cover 'watch' days must be above 'critical' days")
    if body.ppm_target is not None and body.ppm_ceiling is not None and body.ppm_ceiling <= body.ppm_target:
        raise HTTPException(422, "ppm_ceiling must exceed ppm_target")
    if len(body.note.strip()) < 5:
        raise HTTPException(422, "a change note (reason, min 5 characters) is required")
    cfg = get_scorecard_config()
    cfg.update({k: v for k, v in body.model_dump(exclude={"note", "clear_early_window"}).items() if v is not None})
    if body.clear_early_window:
        cfg["early_window_days"] = None
    with SessionLocal() as s:
        row = s.get(AppConfig, "scorecard")
        row.value = cfg
        s.add(ConfigVersion(value=cfg, config_hash=config_hash(cfg), approved_by=AU.actor(), note=body.note.strip()))
        s.commit()
    _rebuild_calendar()
    A.clear_cache()
    threading.Thread(target=_warm_all, daemon=True).start()
    return cfg


def _rebuild_calendar() -> None:
    """Non-working weekdays are baked into sp.work_calendar; refresh it after a config change."""
    try:
        from scripts.load_data import plain_url
        import psycopg
        sql_ = (A._BACKEND_DIR / "app" / "sql" / "views.sql").read_text(encoding="utf-8")
        with psycopg.connect(plain_url(settings.db_url), autocommit=True) as c:
            c.execute(sql_)
    except Exception:
        pass


class ExclusionIn(BaseModel):
    entity_type: str = "PO_LINE"           # PO_LINE | INSPECTION | NCR
    po_id: str | None = None
    po_line: int | None = 1
    entity_id: str | None = None           # inspection_id / ncr_id
    reason: str = Field(min_length=5)
    approved_by: str | None = None         # ignored: the approver is the signed-in user


class RevokeIn(BaseModel):
    reason: str = Field(min_length=5)


@app.get("/api/exclusions")
def list_exclusions():
    with SessionLocal() as s:
        return [{c.name: getattr(e, c.name) for c in KpiExclusion.__table__.columns}
                for e in s.query(KpiExclusion).order_by(KpiExclusion.id.desc())]


EXCL_CHECK = {
    "PO_LINE": f"SELECT COUNT(*) FROM {A.S}.fact_purchase_order_receipt WHERE po_id=:p AND po_line=:l",
    "INSPECTION": f"SELECT COUNT(*) FROM {A.S}.fact_quality_inspection WHERE inspection_id=:e",
    "NCR": f"SELECT COUNT(*) FROM {A.S}.fact_ncr_capa WHERE ncr_id=:e",
}


@app.post("/api/exclusions")
def add_exclusion(body: ExclusionIn):
    et = body.entity_type.upper()
    if et not in EXCL_CHECK:
        raise HTTPException(422, "entity_type must be PO_LINE, INSPECTION or NCR")
    if et == "PO_LINE":
        if not body.po_id:
            raise HTTPException(422, "po_id is required")
        exists = A._run(EXCL_CHECK[et], {"p": body.po_id, "l": body.po_line}, True)
    else:
        if not body.entity_id:
            raise HTTPException(422, "entity_id is required")
        exists = A._run(EXCL_CHECK[et], {"e": body.entity_id}, True)
    if not exists:
        raise HTTPException(404, f"{et} not found")
    with SessionLocal() as s:
        q = s.query(KpiExclusion).filter(KpiExclusion.entity_type == et, KpiExclusion.revoked_at.is_(None))
        q = q.filter_by(po_id=body.po_id, po_line=body.po_line) if et == "PO_LINE" else q.filter_by(entity_id=body.entity_id)
        if q.first():
            raise HTTPException(409, "Already excluded")
        e = KpiExclusion(entity_type=et, entity_id=body.entity_id if et != "PO_LINE" else f"{body.po_id}/{body.po_line}",
                         po_id=body.po_id if et == "PO_LINE" else None, po_line=body.po_line if et == "PO_LINE" else None,
                         reason=body.reason, approved_by=AU.actor())
        s.add(e)
        s.commit()
        eid = e.id
    A.clear_cache()
    return {"id": eid}


@app.post("/api/exclusions/{exclusion_id}/revoke")
def revoke_exclusion(exclusion_id: int, body: RevokeIn):
    """Exclusions are never deleted: revoking keeps the audit trail (who/when/why) and restores the KPI."""
    with SessionLocal() as s:
        e = s.get(KpiExclusion, exclusion_id)
        if not e:
            raise HTTPException(404, "Exclusion not found")
        if e.revoked_at:
            raise HTTPException(409, "Already revoked")
        e.revoked_at, e.revoked_by, e.revoke_reason = now(), AU.actor(), body.reason
        s.commit()
    A.clear_cache()
    return {"ok": True}


@app.delete("/api/exclusions/{exclusion_id}")
def delete_exclusion(exclusion_id: int):
    return revoke_exclusion(exclusion_id, RevokeIn(reason="Removed from the Settings page"))


@app.get("/api/po-lines")
def po_lines(supplier_id: str | None = None, status: str | None = None, limit: int = Query(200, le=1000)):
    cond, p = A._where(None, supplier_id=supplier_id,
                       extra="delivery_status = :st" if status else None)
    if status:
        p["st"] = status
    return A.query(f"""SELECT po_id, po_line, supplier_id, material_id, plant_id, po_date, promised_date, receipt_date,
                              ordered_qty, received_qty, delivery_status, delay_days, on_time, in_full, otif, excluded,
                              exclusion_reason
                       FROM {A.S}.v_po_delivery WHERE {cond} ORDER BY promised_date DESC LIMIT {int(limit)}""", **p)


# ================================================================ fast bootstrap
@app.get("/api/bootstrap")
def bootstrap(plant_id: str | None = None):
    """Everything the Command Center needs in ONE request (each part computed in parallel and cached)."""
    from concurrent.futures import ThreadPoolExecutor
    jobs = {"overview": lambda: A.overview(None, None, plant_id), "scorecard": lambda: A.scorecard(None, None, plant_id),
            "alerts": A.risk_alerts, "pulse": lambda: A.kpi_pulse(plant_id), "coverage": A.data_coverage}
    with ThreadPoolExecutor(max_workers=len(jobs)) as ex:
        futs = {k: ex.submit(f) for k, f in jobs.items()}
        return {k: f.result() for k, f in futs.items()}


# ================================================================ digital twin
class TwinIn(BaseModel):
    scope: str | None = None            # supplier id | OVERSEAS | ALL | NONE
    delay_days: int = Field(0, ge=0, le=60)
    capacity_pct: int = Field(100, ge=0, le=100)
    outage_days: int = Field(0, ge=0, le=60)
    lot_reject_pct: int = Field(0, ge=0, le=100)
    demand_pct: int = Field(0, ge=-50, le=100)
    horizon_days: int = Field(60, ge=14, le=120)
    dual_source_pct: int = Field(0, ge=0, le=100)
    safety_boost_days: int = Field(0, ge=0, le=30)
    expedite_days: int = Field(0, ge=0, le=14)
    response_days: int = Field(5, ge=0, le=30)
    runs: int = Field(200, ge=50, le=500)
    plant_id: str | None = None


@app.get("/api/twin/network")
def twin_network():
    return T.network_cached()


@app.get("/api/twin/fidelity")
def twin_fidelity():
    return T.fidelity_cached()


@app.get("/api/twin/state/{supplier_id}")
def twin_state(supplier_id: str):
    return T.supplier_state_cached(supplier_id)


@app.get("/api/twin/presets")
def twin_presets():
    return T.PRESETS


@app.post("/api/twin/simulate")
def twin_simulate(body: TwinIn):
    return T.simulate(body.model_dump())


class TwinSaveIn(BaseModel):
    name: str = Field(min_length=3, max_length=160)
    params: dict
    summary: dict
    actor: str | None = None


class TwinDecisionIn(BaseModel):
    status: str
    actor: str | None = None
    note: str | None = None


def _scn(x: TwinScenario) -> dict:
    return {c.name: getattr(x, c.name) for c in TwinScenario.__table__.columns}


@app.get("/api/twin/scenarios")
def twin_scenarios():
    with SessionLocal() as s:
        return [_scn(x) for x in s.query(TwinScenario).order_by(TwinScenario.id.desc()).limit(50)]


@app.post("/api/twin/scenarios")
def twin_save(body: TwinSaveIn):
    with SessionLocal() as s:
        x = TwinScenario(name=body.name, params=body.params, summary=body.summary, created_by=AU.actor())
        s.add(x)
        s.commit()
        return _scn(x)


TWIN_FLOW = {"DRAFT": {"SUBMITTED"}, "SUBMITTED": {"APPROVED", "REJECTED"}, "APPROVED": set(), "REJECTED": {"DRAFT"}}


@app.patch("/api/twin/scenarios/{sid}")
def twin_decide(sid: int, body: TwinDecisionIn):
    with SessionLocal() as s:
        x = s.get(TwinScenario, sid)
        if not x:
            raise HTTPException(404, "Scenario not found")
        if body.status not in TWIN_FLOW.get(x.status, set()):
            raise HTTPException(409, f"Cannot move from {x.status} to {body.status}")
        if body.status in ("APPROVED", "REJECTED") and not AU.has_role("PROCUREMENT"):
            raise HTTPException(403, "Only PROCUREMENT (or ADMIN) may approve or reject a scenario")
        x.status, x.note = body.status, body.note
        if body.status in ("APPROVED", "REJECTED"):
            x.decided_by = AU.actor()
        if body.status == "SUBMITTED":
            s.add(Notification(recipient_role="PROCUREMENT", severity="INFO", subject=f"Twin scenario for approval: {x.name}",
                               body=f"Revenue at risk ₹{(x.summary or {}).get('revenue_at_risk_inr', 0):,.0f}. Review in Digital Twin."))
        s.commit()
        return _scn(x)


# ================================================================ network tracker (map)
@app.get("/api/tracker/suppliers/{sid}/twin")
def tracker_supplier_twin(sid: str):
    """3D supplier view: profile and owners, KPIs, materials supplied and delivery history."""
    require("tracker_3d_twins")
    try:
        return TR.supplier_twin(sid)
    except KeyError as e:
        raise HTTPException(404, str(e))


@app.get("/api/tracker")
def tracker(scope: str | None = None, delay_days: int = Query(0, ge=0, le=60), outage_days: int = Query(0, ge=0, le=60)):
    """Supplier sites, ports, plants, sea/road lanes and every PO line as a time-stamped shipment."""
    return TR.tracker(scope, delay_days, outage_days)


@app.get("/api/tracker/routes")
def tracker_routes():
    """Route cache status + the last route-feed refreshes (T2)."""
    from . import routing as R
    rows = A._run("""SELECT key, mode, provider, distance_km, duration_h, fetched_at, jsonb_array_length(geometry) AS points
                     FROM public.route_cache ORDER BY mode, key""", {})
    runs = A._run("""SELECT id, status, detail, started_at, finished_at FROM public.job_runs WHERE job = 'feed:routes'
                     ORDER BY id DESC LIMIT 10""", {})
    return {"road_provider": "openrouteservice" if R.ORS_KEY else f"osrm ({R.OSRM_URL})", "sea_provider": "searoute (offline)",
            "routes": rows, "refreshes": runs}


# ================================================================ T3: GPS tracking
def _device(request: Request) -> dict:
    from . import gps as G
    require("gps_tracking")
    v = G.vehicle_for_key(request.headers.get("x-device-key"))
    if not v:
        raise HTTPException(401, "Unknown or inactive device key")
    return v


_GPS_HITS: dict[str, deque] = defaultdict(deque)


@app.post("/api/gps/pings")
async def gps_pings(request: Request):
    """GPS ingestion for phones, telematics boxes, AIS integrators and the simulator. Auth: header X-Device-Key.
    Body: {"pings": [{"ts": ISO-8601, "lat": .., "lon": .., "speed_kmh": .., "heading": .., "accuracy_m": ..}, ...]}"""
    from . import gps as G
    v = _device(request)
    q, t = _GPS_HITS[v["vehicle_id"]], time.monotonic()
    while q and t - q[0] > 60:
        q.popleft()
    if len(q) >= 120:
        raise HTTPException(429, "Too many requests from this device")
    q.append(t)
    body = await request.json()
    pings = body.get("pings") if isinstance(body, dict) else None
    if not isinstance(pings, list):
        raise HTTPException(422, "body must be {pings: [...]}")
    return G.ingest(v, pings)


@app.get("/api/gps/trip")
def gps_trip(request: Request):
    """What the driver page shows: vehicle and active trip (destinations and PO references only - no prices)."""
    from . import gps as G
    v = _device(request)
    trip = G.active_trip(v["vehicle_id"])
    out = {"vehicle_id": v["vehicle_id"], "kind": v["kind"], "registration": v["registration"], "trip": None}
    if trip:
        rows = A._run(f"""SELECT po_id || '-' || po_line AS id, plant_id, material_name, promised_date FROM {A.S}.v_po_delivery
                          WHERE po_id || '-' || po_line = ANY(:ids)""", {"ids": list(trip["shipment_ids"])})
        out["trip"] = {"trip_id": trip["trip_id"], "started_at": trip["started_at"], "shipments": rows}
    return out


class VehicleIn(BaseModel):
    kind: str = "TRUCK"
    registration: str = Field(min_length=3, max_length=60)
    carrier: str = ""
    device_type: str = "PHONE"


class TripIn(BaseModel):
    shipment_ids: list[str] = Field(min_length=1, max_length=200)


@app.get("/api/vehicles")
def vehicles():
    from . import gps as G
    require("gps_tracking")
    return G.list_vehicles()


@app.get("/api/vehicles/setup")
def vehicles_setup():
    """GPS page: will driver links work on phones? (browsers share location only from an https page)"""
    require("gps_tracking")
    url = os.environ.get("PUBLIC_APP_URL", "").strip().rstrip("/")
    return {"public_url": url or None, "https": url.startswith("https://"),
            "retention_days": get_scorecard_config().get("gps_retention_days", 90)}


@app.get("/api/vehicles/{vehicle_id}/track")
def vehicle_track(vehicle_id: str):
    from . import gps as G
    require("gps_tracking")
    try:
        out = G.vehicle_track(vehicle_id)
    except LookupError as e:
        raise HTTPException(404, str(e))
    out["route"] = None                       # the planned road route of the trip's lane (supplier -> plant), for the map
    ln = (out.get("lines") or [None])[0]
    if ln:
        try:
            net = TR.tracker()
            lane = next((l for l in net["lanes"] if l["id"] == f"{ln['supplier_id']}>{ln['plant_id']}"), None)
            nodes = {n["id"]: n for n in net["nodes"]}
            if lane and lane.get("path"):
                o, d = nodes.get(ln["supplier_id"]), nodes.get(ln["plant_id"])
                out["route"] = {"lane": lane["id"], "mode": lane.get("mode"), "path": lane["path"],
                                "origin": {"name": o["name"], "lat": o["lat"], "lon": o["lon"]} if o else None,
                                "dest": {"name": d["name"], "lat": d["lat"], "lon": d["lon"]} if d else None}
        except Exception:  # noqa: BLE001
            pass
    return out


@app.post("/api/vehicles")
def vehicle_create(body: VehicleIn):
    from . import gps as G
    require("gps_tracking")
    try:
        out = G.create_vehicle(body.kind, body.registration, body.carrier, body.device_type, AU.actor())
    except ValueError as e:
        raise HTTPException(422, str(e))
    out["driver_link"] = f"{os.environ.get('PUBLIC_APP_URL', 'http://localhost:5173').rstrip('/')}/driver/#k={out['device_key']}"
    return out


@app.post("/api/vehicles/{vehicle_id}/trip")
def vehicle_trip(vehicle_id: str, body: TripIn):
    from . import gps as G
    require("gps_tracking")
    try:
        r = G.start_trip(vehicle_id, body.shipment_ids, AU.actor())
    except LookupError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(422, str(e))
    A.clear_cache()
    return r


@app.post("/api/vehicles/{vehicle_id}/end-trip")
def vehicle_end_trip(vehicle_id: str):
    from . import gps as G
    require("gps_tracking")
    return {"ended": G.end_trip(vehicle_id)}


@app.post("/api/vehicles/{vehicle_id}/rotate-key")
def vehicle_rotate(vehicle_id: str):
    from . import gps as G
    require("gps_tracking")
    try:
        key = G.rotate_key(vehicle_id)
    except LookupError as e:
        raise HTTPException(404, str(e))
    return {"vehicle_id": vehicle_id, "device_key": key,
            "driver_link": f"{os.environ.get('PUBLIC_APP_URL', 'http://localhost:5173').rstrip('/')}/driver/#k={key}"}


# ================================================================ T3: disruption overlays
def _net_nodes_lanes():
    from .features import is_on as _on
    n = TR.network(True, _on("network_twin_v2"))
    return n["nodes"], n["lanes"]


@app.get("/api/disruptions")
def disruptions():
    from . import disruptions as D
    require("twin_disruptions")
    nodes, lanes = _net_nodes_lanes()
    return D.active(lanes, nodes)


@app.post("/api/disruptions/refresh")
def disruptions_refresh():
    from . import disruptions as D
    require("twin_disruptions")
    nodes, _ = _net_nodes_lanes()
    return D.refresh(nodes, trigger=f"manual:{AU.actor()}")


class DisruptionIn(BaseModel):
    type: str = Field(min_length=3, max_length=40)            # Strike, Port congestion, Road closure, ...
    severity: str = "MEDIUM"
    title: str = Field(min_length=5, max_length=300)
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    radius_km: float = Field(50, gt=0, le=2000)
    ends: str | None = None


@app.post("/api/disruptions")
def disruption_add(body: DisruptionIn):
    from . import disruptions as D
    require("twin_disruptions")
    try:
        return {"id": D.add_manual(body.type, body.severity.upper(), body.title, body.lat, body.lon, body.radius_km, body.ends, AU.actor())}
    except ValueError as e:
        raise HTTPException(422, str(e))


@app.delete("/api/disruptions/{event_id}")
def disruption_remove(event_id: int):
    from . import disruptions as D
    require("twin_disruptions")
    D.deactivate(event_id)
    return {"ok": True}


# ================================================================ T3: source records behind the twin (PO / GRN trace)
@app.get("/api/tracker/shipments/{po_id}/{po_line}/source")
def shipment_source(po_id: str, po_line: int):
    """The PO line exactly as the KPI views see it, its goods receipts, invoices, milestones and GPS trip."""
    require("twin_side_panel")
    p = {"p": po_id, "l": po_line}
    po = A._run(f"SELECT * FROM {A.S}.v_po_delivery WHERE po_id = :p AND po_line = :l", p)
    if not po:
        raise HTTPException(404, "PO line not found")
    raw = A._run(f"SELECT * FROM {A.S}.fact_purchase_order_receipt WHERE po_id = :p AND po_line = :l", p)
    grn = A._run(f"SELECT * FROM {A.S}.fact_grn WHERE po_id = :p AND po_line = :l ORDER BY receipt_date", p)
    inv = A._run(f"SELECT * FROM {A.S}.fact_invoice WHERE po_id = :p AND po_line = :l ORDER BY invoice_date", p)
    ms = A._run(f"""SELECT milestone, milestone_ts, location, lat, lon, COALESCE(source, 'ERP') AS source FROM {A.S}.fact_inbound_milestone
                    WHERE po_id = :p AND po_line = :l ORDER BY milestone_ts""", p)
    gps = A._run("""SELECT t.trip_id, t.vehicle_id, v.registration, v.device_type, t.started_at, t.ended_at,
                           (SELECT COUNT(*) FROM public.gps_pings g WHERE g.trip_id = t.trip_id) AS pings,
                           (SELECT MAX(ts) FROM public.gps_pings g WHERE g.trip_id = t.trip_id) AS last_ping
                    FROM public.vehicle_trips t JOIN public.vehicles v USING (vehicle_id)
                    WHERE t.shipment_ids ? :sid ORDER BY t.trip_id DESC""", {"sid": f"{po_id}-{po_line}"})
    return {"po_line": po[0], "erp_record": raw[0] if raw else None, "grn": grn,
            "grn_note": None if grn else "No goods-receipt extract is loaded: the receipt date / quantity come from the PO receipt record.",
            "invoices": inv, "milestones": ms, "gps_trips": gps, "source_tables": [f"{A.S}.v_po_delivery", f"{A.S}.fact_purchase_order_receipt",
                                                                                     f"{A.S}.fact_grn", f"{A.S}.fact_invoice", f"{A.S}.fact_inbound_milestone", "public.gps_pings"]}


@app.get("/api/tracker/lanes/{supplier_id}/{plant_id}/records")
def lane_records(supplier_id: str, plant_id: str):
    """Every PO line on a lane - the records behind the lane's on-time % and counts."""
    require("twin_side_panel")
    rows = A._run(f"""SELECT po_id, po_line, material_id, po_date, promised_date, receipt_date, ordered_qty, received_qty,
                             delivery_status, on_time, in_full, otif, delay_days, excluded
                      FROM {A.S}.v_po_delivery WHERE supplier_id = :s AND plant_id = :p ORDER BY promised_date DESC""",
                  {"s": supplier_id, "p": plant_id})
    meas = [r for r in rows if r["delivery_status"] == "RECEIVED" and not r["excluded"]]
    return {"supplier_id": supplier_id, "plant_id": plant_id, "rows": rows, "row_count": len(rows),
            "on_time_pct": round(100 * sum(1 for r in meas if r["on_time"]) / len(meas), 1) if meas else None,
            "definition": "On-time % = received PO lines (not excluded) received by the effective promise date / received PO lines."}


# ================================================================ DI1: data integration
class ConnectionIn(BaseModel):
    connector_key: str
    name: str = Field(min_length=3, max_length=120)
    environment: str = "DEV"
    config: dict = {}
    secrets: dict = {}


class ConnectionPatch(BaseModel):
    name: str | None = Field(None, min_length=3, max_length=120)
    config: dict | None = None
    secrets: dict | None = None


class ApprovalIn(BaseModel):
    decision: str
    note: str | None = Field(None, max_length=500)


def _di(fn, *a, **k):
    from .integration import service as DS
    require("data_integration")
    try:
        return getattr(DS, fn)(*a, **k)
    except LookupError as e:
        raise HTTPException(404, str(e))
    except PermissionError as e:
        raise HTTPException(409, str(e))
    except NotImplementedError as e:
        raise HTTPException(501, str(e))
    except ValueError as e:
        raise HTTPException(422, str(e))
    except Exception as e:  # noqa: BLE001 - source unreachable etc.: plain-language hint, never a stack trace
        from .integration.db_connectors import SourceError
        if isinstance(e, SourceError):
            raise HTTPException(502, str(e))
        raise


@app.get("/api/integration/summary")
def di_summary():
    from .integration import service as DS
    from .integration.secrets import vault
    require("data_integration")
    def build():
        conns = DS.list_connections()
        return {"environment": DS.active_environment(), "vault": vault().describe(), "connections": len(conns),
                "by_status": {s: sum(c["status"] == s for c in conns) for s in {c["status"] for c in conns}},
                "pending_approvals": sum(c["approval_status"] == "PENDING" for c in conns), "table_ownership": DS.table_ownership()}
    return _resp("di_summary", 60, build)


@app.get("/api/integration/catalog")
def di_catalog():
    return _di("catalogue_with_status")


@app.get("/api/integration/canonical")
def di_canonical():
    from .integration.canonical import CANONICAL, LINEAGE_COLS
    require("data_integration")
    return {"entities": [{"entity": k, **{**v, "fields": [{"name": f, "type": t[0], "required": t[1], "description": t[2]} for f, t in v["fields"].items()]}}
                         for k, v in CANONICAL.items()], "lineage_columns": [c for c, _ in LINEAGE_COLS]}


@app.get("/api/integration/connections")
def di_connections():
    return _di("list_connections")


@app.post("/api/integration/connections")
def di_create(body: ConnectionIn):
    return _di("create_connection", body.connector_key, body.name, body.environment, body.config, body.secrets, AU.actor())


@app.patch("/api/integration/connections/{cid}")
def di_update(cid: str, body: ConnectionPatch):
    return _di("update_connection", cid, AU.actor(), body.name, body.config, body.secrets)


@app.delete("/api/integration/connections/{cid}")
def di_delete(cid: str):
    _di("delete_connection", cid, AU.actor())
    return {"ok": True}


@app.post("/api/integration/connections/{cid}/approve")
def di_approve(cid: str, body: ApprovalIn):
    return _di("decide", cid, body.decision, AU.actor(), body.note)


@app.post("/api/integration/connections/{cid}/test")
def di_test(cid: str):
    return _di("test", cid, AU.actor())


@app.get("/api/integration/primary")
def di_primary():
    """Built-in primary connection: the app's own Neon PostgreSQL warehouse (no credentials shown)."""
    require("data_integration")
    from .integration import health as DH
    return DH.primary_details()


@app.get("/api/integration/connections/{cid}/details")
def di_connection_details(cid: str):
    """Datasets this connection feeds (mapping -> canonical entity -> target table), rows extracted / loaded / in target, health, runs."""
    require("data_integration")
    from .integration import health as DH
    try:
        return DH.connection_details(cid)
    except LookupError as e:
        raise HTTPException(404, str(e))


@app.get("/api/integration/connections/{cid}/schema")
def di_schema(cid: str):
    return _di("schema", cid, AU.actor())


@app.get("/api/integration/connections/{cid}/preview")
def di_preview(cid: str, object: str = Query(..., min_length=1, max_length=120)):
    return _di("preview", cid, object, AU.actor())


def _dim(fn, *a, mod="mapping", **k):
    """DI3 endpoints: page flag + mapping/loads flag, same error mapping as _di."""
    import importlib
    require("di_mapping_loads")
    M = importlib.import_module(f".integration.{mod}", __package__)
    return _di_call(lambda: getattr(M, fn)(*a, **k))


def _di_call(f):
    require("data_integration")
    try:
        return f()
    except LookupError as e:
        raise HTTPException(404, str(e))
    except PermissionError as e:
        raise HTTPException(409, str(e))
    except NotImplementedError as e:
        raise HTTPException(501, str(e))
    except ValueError as e:
        raise HTTPException(422, str(e))
    except Exception as e:  # noqa: BLE001
        from .integration.db_connectors import SourceError
        if isinstance(e, SourceError):
            raise HTTPException(502, str(e))
        raise


class DiMappingIn(BaseModel):
    name: str | None = Field(None, max_length=120)
    connection_id: str | None = Field(None, max_length=20)
    entity: str | None = Field(None, max_length=40)
    source_kind: str | None = Field(None, pattern="^(object|query)$")
    source_object: str | None = Field(None, max_length=200)
    source_query: str | None = Field(None, max_length=20000)
    field_map: dict | None = None
    record_id_columns: list[str] | None = None
    watermark_column: str | None = Field(None, max_length=120)
    column_policy: dict | None = None
    mode: str | None = Field(None, pattern="^(STAGING|PUBLISH)$")
    schedule: str | None = Field(None, max_length=40)
    enabled: bool | None = None
    max_quarantine_pct: float | None = Field(None, ge=0, le=100)
    max_rows: int | None = Field(None, ge=1, le=5_000_000)


class DiRunIn(BaseModel):
    dry_run: bool = False
    full: bool = False


class DiDecisionIn(BaseModel):
    decision: str
    note: str | None = Field(None, max_length=500)


@app.get("/api/integration/mappings")
def di_mappings():
    return _dim("list_mappings")


@app.post("/api/integration/mappings/suggest")
def di_mapping_suggest(body: DiMappingIn):
    """Rule-based suggestions from source METADATA only (column names / types) - no rows, no external service."""
    def go():
        from .integration import mapping as MP, service as SV
        _, con = SV.connector_for(body.connection_id)
        cols = MP.source_columns(con, body.source_kind or "object", body.source_object, body.source_query)
        SV.audit(AU.actor(), "mapping:suggest", body.connection_id, {"entity": body.entity, "source": body.source_object or "query"})
        return {**MP.suggest(body.entity, cols), "columns": cols}
    require("di_mapping_loads")
    return _di_call(go)


@app.post("/api/integration/mappings/preview")
def di_mapping_preview(body: DiMappingIn):
    """Validate + transform the first rows of an unsaved mapping. Nothing is stored."""
    return _dim("preview", body.model_dump(), mod="pipeline")


@app.post("/api/integration/mappings")
def di_mapping_create(body: DiMappingIn):
    return _dim("create_mapping", body.model_dump(), AU.actor())


@app.get("/api/integration/mappings/{mid}")
def di_mapping_get(mid: str):
    return _dim("get_mapping", mid)


@app.patch("/api/integration/mappings/{mid}")
def di_mapping_update(mid: str, body: DiMappingIn):
    return _dim("update_mapping", mid, body.model_dump(exclude_none=True), AU.actor())


@app.delete("/api/integration/mappings/{mid}")
def di_mapping_delete(mid: str):
    _dim("delete_mapping", mid, AU.actor())
    return {"ok": True}


@app.post("/api/integration/mappings/{mid}/approve")
def di_mapping_approve(mid: str, body: DiDecisionIn):
    return _dim("decide_mapping", mid, body.decision, AU.actor(), body.note)


@app.post("/api/integration/mappings/{mid}/run")
def di_mapping_run(mid: str, body: DiRunIn):
    return _dim("run", mid, "manual", AU.actor(), body.dry_run, body.full, mod="pipeline")


@app.get("/api/integration/runs")
def di_runs(mapping_id: str | None = None, limit: int = Query(100, le=500)):
    return _dim("list_runs", mapping_id, limit, mod="pipeline")


@app.get("/api/integration/runs/{rid}")
def di_run(rid: str):
    return _dim("get_run", rid, mod="pipeline")


@app.get("/api/integration/quarantine")
def di_quarantine(mapping_id: str | None = None, run_id: str | None = None, status: str | None = "OPEN", limit: int = Query(200, le=1000)):
    return _dim("quarantine", mapping_id, run_id, status or None, limit, mod="pipeline")


@app.post("/api/integration/quarantine/{qid}")
def di_quarantine_resolve(qid: int, body: DiDecisionIn):
    return _dim("resolve_quarantine", qid, body.decision, AU.actor(), mod="pipeline")


@app.post("/api/integration/ownership/{table}/release")
def di_release(table: str):
    return _dim("release_ownership", table, AU.actor(), mod="pipeline")


@app.get("/api/integration/connections/{cid}/files")
def di_files(cid: str):
    """Uploaded files of a CSV / Excel connection (names, sizes, row counts - never content)."""
    def go():
        from .integration import file_connectors as F, service as SV
        SV.get_connection(cid)
        return F.list_files(cid)
    require("di_files_apis")
    return _di_call(go)


@app.post("/api/integration/connections/{cid}/files")
async def di_upload(cid: str, file: UploadFile = File(...)):
    """ADMIN: upload a CSV / XLSX (max 50 MB). Parsed once to validate, then stored ENCRYPTED; loads happen through a mapping."""
    data = await file.read(50 * 1024 * 1024 + 1)

    def go():
        from .integration import file_connectors as F, service as SV
        c = SV.get_connection(cid)
        if c["connector_key"] != "csv_excel":
            raise ValueError("files can only be uploaded to a CSV / Excel connection")
        if c["approval_status"] != "APPROVED":
            raise PermissionError("connection is not approved yet")
        out = F.store_upload(cid, file.filename or "upload.csv", data, AU.actor())
        SV.audit(AU.actor(), "file:upload", cid, {k: out[k] for k in ("id", "name", "size_bytes", "rows", "sha256")})
        return out
    require("di_files_apis")
    return _di_call(go)


@app.delete("/api/integration/connections/{cid}/files/{fid}")
def di_file_delete(cid: str, fid: int):
    def go():
        from .integration import file_connectors as F, service as SV
        F.delete_file(cid, fid)
        SV.audit(AU.actor(), "file:delete", cid, {"id": fid})
        return {"ok": True}
    require("di_files_apis")
    return _di_call(go)


def _dih(fn, *a, mod="health", **k):
    """DI5 endpoints: page flag + quality/health flag."""
    import importlib
    require("di_quality_health")
    M = importlib.import_module(f".integration.{mod}", __package__)
    return _di_call(lambda: getattr(M, fn)(*a, **k))


class DqRuleIn(BaseModel):
    entity: str | None = Field(None, max_length=40)
    name: str | None = Field(None, max_length=60)
    kind: str | None = Field(None, max_length=30)
    params: dict | None = None
    severity: str | None = Field(None, pattern="^(ERROR|WARN)$")
    description: str | None = Field(None, max_length=300)
    enabled: bool | None = None


class BulkQuarantineIn(BaseModel):
    action: str
    ids: list[int] | None = None
    mapping_id: str | None = Field(None, max_length=20)
    reason: str | None = Field(None, max_length=200)


class DiSettingsIn(BaseModel):
    file_retention_days: int | None = None
    quarantine_retention_days: int | None = None


@app.get("/api/integration/dq-rules")
def di_dq_rules(entity: str | None = None):
    return _dih("list_rules", entity, mod="quality")


@app.post("/api/integration/dq-rules")
def di_dq_rule_create(body: DqRuleIn):
    return _dih("create_rule", body.model_dump(), AU.actor(), mod="quality")


@app.patch("/api/integration/dq-rules/{rid}")
def di_dq_rule_update(rid: int, body: DqRuleIn):
    return _dih("update_rule", rid, body.model_dump(exclude_none=True), AU.actor(), mod="quality")


@app.delete("/api/integration/dq-rules/{rid}")
def di_dq_rule_delete(rid: int):
    _dih("delete_rule", rid, AU.actor(), mod="quality")
    return {"ok": True}


@app.get("/api/integration/health")
def di_health():
    return _dih("health")


@app.post("/api/integration/health/check")
def di_health_check():
    """ADMIN: run the health / alert / retention job now (also runs every 15 minutes)."""
    def go():
        from .jobs import JOBS, run_job
        from .integration import health as HL
        JOBS.setdefault("integration_health", HL.job)
        return run_job("integration_health", "manual")
    require("di_quality_health")
    return _di_call(go)


@app.get("/api/integration/quarantine-queue")
def di_quarantine_queue(mapping_id: str | None = None, status: str | None = "OPEN", reason: str | None = None, limit: int = Query(300, le=2000)):
    return _dih("queue", mapping_id, status or "", reason, limit)


@app.post("/api/integration/quarantine-bulk")
def di_quarantine_bulk(body: BulkQuarantineIn):
    return _dih("bulk_resolve", body.action, AU.actor(), body.ids, body.mapping_id, body.reason)


@app.get("/api/integration/lineage/search")
def di_lineage_search(entity: str, q: str = Query("", max_length=60)):
    return _dih("search_keys", entity, q)


@app.get("/api/integration/lineage")
def di_lineage(entity: str, key: str = Query(..., max_length=500)):
    """key = JSON object of the canonical key, e.g. {"po_id": "POC-000001", "po_line": "1"}."""
    try:
        k = json.loads(key)
    except ValueError:
        raise HTTPException(422, "key must be a JSON object")
    return _dih("lineage", entity, k if isinstance(k, dict) else {})


@app.get("/api/integration/settings")
def di_settings():
    return _dih("get_settings")


@app.put("/api/integration/settings")
def di_settings_put(body: DiSettingsIn):
    return _dih("set_settings", body.model_dump(exclude_none=True), AU.actor())


class DiQueryIn(BaseModel):
    sql: str = Field(..., min_length=1, max_length=20000)
    limit: int = Field(100, ge=1, le=1000)


@app.post("/api/integration/connections/{cid}/query")
def di_query(cid: str, body: DiQueryIn):
    """ADMIN only: one SELECT against an approved source (parsed SELECT-only guard, read-only session, timeout, row cap, masked)."""
    return _di("query", cid, body.sql, body.limit, AU.actor())


@app.get("/api/integration/audit")
def di_audit(connection_id: str | None = None, limit: int = Query(200, le=1000)):
    return _di("audit_log", limit, connection_id)


# ================================================================ T4: plant digital twin
@app.get("/api/plants/{plant_id}/twin")
def plant_twin(plant_id: str, date: str | None = None):
    """Docks, quality hold, warehouses, lines, days of cover and exposure for one plant at a date of the timeline."""
    from . import plant as PL
    require("plant_twin")
    try:
        return PL.view(plant_id, date)
    except LookupError as e:
        raise HTTPException(404, str(e))


@app.get("/api/plants/{plant_id}/twin/records")
def plant_twin_records(plant_id: str, kind: str, date: str | None = None, material_id: str | None = None, line_id: str | None = None):
    """Source rows behind a plant-twin figure: stock | usage | orders | downtime."""
    from . import plant as PL
    require("plant_twin")
    try:
        return PL.records(plant_id, kind, date, material_id, line_id)
    except KeyError:
        raise HTTPException(422, "kind must be stock, usage, orders or downtime")


# ================================================================ T3: saved views
class SavedViewIn(BaseModel):
    page: str = Field("tracker", max_length=30)
    name: str = Field(min_length=2, max_length=80)
    payload: dict
    shared: bool = False


@app.get("/api/saved-views")
def saved_views(page: str = "tracker"):
    require("twin_filters")
    return A._run("""SELECT id, username, name, payload, shared, created_at FROM public.saved_views
                     WHERE page = :p AND (username = :u OR shared) ORDER BY shared, name""", {"p": page, "u": AU.actor()})


@app.post("/api/saved-views")
def saved_view_add(body: SavedViewIn):
    require("twin_filters")
    with engine.begin() as c:
        vid = c.execute(text("""INSERT INTO public.saved_views (username, page, name, payload, shared)
                                VALUES (:u, :p, :n, CAST(:j AS jsonb), :s) RETURNING id"""),
                        {"u": AU.actor(), "p": body.page, "n": body.name, "j": json.dumps(body.payload), "s": body.shared}).scalar()
    return {"id": vid}


@app.delete("/api/saved-views/{view_id}")
def saved_view_delete(view_id: int):
    require("twin_filters")
    with engine.begin() as c:
        n = c.execute(text("DELETE FROM public.saved_views WHERE id = :i AND (username = :u OR :admin)"),
                      {"i": view_id, "u": AU.actor(), "admin": AU.has_role("ADMIN")}).rowcount
    if not n:
        raise HTTPException(404, "View not found or not yours")
    return {"ok": True}


@app.post("/api/tracker/routes/refresh")
def tracker_routes_refresh(force: bool = False):
    """ADMIN: (re)compute route geometry in the background; only coordinates are sent to the routing provider."""
    TR.ensure_routes(force=force, trigger=f"manual:{AU.actor()}")
    return {"status": "started", "force": force}


# ================================================================ enhancement programme: feature flags
@app.get("/api/features")
def features():
    return {"flags": all_flags(force=True), "detail": list_flags() if AU.has_role("ADMIN") else None}


class FlagIn(BaseModel):
    enabled: bool


@app.put("/api/features/{key}")
def put_feature(key: str, body: FlagIn):
    set_flag(key, body.enabled, AU.actor())
    return {"key": key, "enabled": body.enabled}


# ================================================================ 1.1 safe query layer (approved metrics)
def _metrics_on() -> None:
    if not (is_on("safe_query_layer") or is_on("chat_charts") or is_on("role_dashboards")):
        require("safe_query_layer")


def _mfilters(supplier_id, plant_id, material_id, start, end) -> dict:
    return {"supplier_id": supplier_id, "plant_id": plant_id, "material_id": material_id, "start": start, "end": end}


@app.get("/api/metrics")
def metrics_catalog():
    _metrics_on()
    return MX.catalog()


@app.get("/api/metrics/{metric}/query")
def metrics_query(metric: str, group_by: str = "", supplier_id: str | None = None, plant_id: str | None = None,
                  material_id: str | None = None, start: str | None = None, end: str | None = None):
    _metrics_on()
    try:
        return MX.query(metric, [g for g in group_by.split(",") if g], _mfilters(supplier_id, plant_id, material_id, start, end))
    except MX.MetricError as e:
        raise HTTPException(422, str(e))


@app.get("/api/metrics/{metric}/compare")
def metrics_compare(metric: str, grain: str = "month", dimension: str = "supplier", plant_id: str | None = None):
    _metrics_on()
    try:
        return MX.compare_periods(metric, grain, dimension, {"plant_id": plant_id})
    except MX.MetricError as e:
        raise HTTPException(422, str(e))


@app.get("/api/metrics/{metric}/records")
def metrics_records(metric: str, supplier_id: str | None = None, plant_id: str | None = None, material_id: str | None = None,
                    start: str | None = None, end: str | None = None, limit: int = Query(500, le=5000)):
    """'Show source records': the exact rows behind a metric figure or chart (audit trail)."""
    _metrics_on()
    try:
        return MX.records(metric, _mfilters(supplier_id, plant_id, material_id, start, end), limit)
    except MX.MetricError as e:
        raise HTTPException(422, str(e))


# ================================================================ UX3 supplier quality inquiry
class QiDraftIn(BaseModel):
    supplier_id: str
    material_id: str | None = None


class QiSendIn(BaseModel):
    email: str | None = Field(default=None, max_length=200)
    save_contact: bool = True
    subject: str | None = Field(default=None, max_length=200)
    message: str | None = Field(default=None, max_length=8000)


class QiCloseIn(BaseModel):
    note: str | None = Field(default=None, max_length=1000)


class QiReplyIn(BaseModel):
    reason: str = Field(min_length=5, max_length=4000)
    containment: str = Field(default="", max_length=4000)
    corrective_action: str = Field(default="", max_length=4000)
    commit_date: dt.date | None = None


def _delay():
    require("delay_reasoning")
    from . import delay_reasoning as DR
    return DR


@app.get("/api/delays/causes")
def delay_causes(supplier_id: str | None = None, days: int = Query(120, ge=14, le=365)):
    return _delay().causes(supplier_id, days)


@app.get("/api/delays/{po_id}/{po_line}")
def delay_explain(po_id: str, po_line: int):
    """Why is this PO line late: journey plan vs actual, root cause, contributing factors, impact, actions (+ markdown brief)."""
    try:
        return _delay().explain(po_id, po_line)
    except KeyError as e:
        raise HTTPException(404, str(e))


def _pred():
    require("predictive_layer")
    from . import predictive as PR
    return PR


@app.get("/api/predictions/model")
def pred_model():
    """Model card: method, validation on later orders, forward check, features used / dropped, main drivers."""
    return _resp("pred_model", 300, lambda: _pred().model_card() or {"version": None})


@app.get("/api/predictions/late")
def pred_late(supplier_id: str | None = None, min_band: str | None = Query(None, pattern="^(LOW|MEDIUM|HIGH|LATE)$"),
              horizon_days: int | None = Query(None, ge=0, le=365)):
    PR = _pred()
    try:
        return _resp(f"pred_late:{supplier_id}:{min_band}:{horizon_days}", 120, lambda: PR.late_risk(supplier_id, min_band, horizon_days))
    except ValueError as e:
        raise HTTPException(409, str(e))


@app.get("/api/predictions/suppliers")
def pred_suppliers(horizon_days: int = Query(30, ge=7, le=120)):
    return _pred().supplier_forecast(horizon_days)


@app.get("/api/predictions/quality")
def pred_quality(weeks: int = Query(12, ge=6, le=52)):
    return _pred().quality_watch(weeks)


@app.post("/api/predictions/retrain")
def pred_retrain():
    PR = _pred()
    try:
        out = PR.train(AU.actor())
    except ValueError as e:
        raise HTTPException(409, str(e))
    PR.save_predictions(PR.score(force=True))
    return {k: out[k] for k in ("id", "version", "train_rows", "holdout", "forward", "features", "dropped")}


def _qi():
    require("quality_inquiry")
    from . import quality_inquiry as QI
    return QI


@app.get("/api/quality-inquiries")
def qi_list(status: str | None = None, supplier_id: str | None = None):
    QI = _qi()
    return {"items": QI.list_all(status, supplier_id), "smtp": QI.smtp_ready()}


@app.post("/api/quality-inquiries")
def qi_draft(body: QiDraftIn):
    QI = _qi()
    try:
        return QI.draft(body.supplier_id, body.material_id, AU.actor())
    except KeyError as e:
        raise HTTPException(404, str(e))


@app.post("/api/quality-inquiries/{inq_id}/send")
def qi_send(inq_id: int, body: QiSendIn):
    QI = _qi()
    email = (body.email or "").strip() or None
    if email and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        raise HTTPException(422, "That e-mail address does not look valid")
    q = QI.get(inq_id)
    if not q:
        raise HTTPException(404, "Inquiry not found")
    if email and body.save_contact:
        QI.save_contact(q["supplier_id"], email, None, AU.actor())
    try:
        return QI.send(inq_id, email, AU.actor(), body.subject, body.message)
    except ValueError as e:
        raise HTTPException(409, str(e))


@app.post("/api/quality-inquiries/{inq_id}/close")
def qi_close(inq_id: int, body: QiCloseIn | None = None):
    QI = _qi()
    try:
        return QI.close(inq_id, AU.actor(), body.note if body else None)
    except KeyError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(409, str(e))


@app.get("/api/quality-inquiries/{inq_id}/link")
def qi_link(inq_id: int):
    """The supplier reply link, so it can be pasted into a manual e-mail when SMTP is not configured."""
    QI = _qi()
    if not QI.get(inq_id):
        raise HTTPException(404, "Inquiry not found")
    return {"link": QI.reply_url(inq_id)}


class QiBulkIn(BaseModel):
    supplier_ids: list[str] = Field(min_length=1, max_length=50)


class QiSendAllIn(BaseModel):
    ids: list[int] = Field(min_length=1, max_length=50)


class ContactIn(BaseModel):
    email: str = Field(min_length=3, max_length=200)
    contact_name: str | None = Field(default=None, max_length=120)


@app.post("/api/quality-inquiries/bulk-draft")
def qi_bulk_draft(body: QiBulkIn):
    """One draft per affected supplier (from an agent answer). Drafts only: sending needs a separate human click."""
    QI = _qi()
    return QI.bulk_draft(body.supplier_ids, AU.actor())       # each draft's history records who drafted it


@app.post("/api/quality-inquiries/send-all")
def qi_send_all(body: QiSendAllIn):
    QI = _qi()
    return QI.send_many(body.ids, AU.actor())


@app.get("/api/supplier-contacts")
def contacts_list():
    from . import quality_inquiry as QI
    return QI.list_contacts()


@app.put("/api/supplier-contacts/{sid}")
def contact_put(sid: str, body: ContactIn):
    from . import quality_inquiry as QI
    email = body.email.strip()
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        raise HTTPException(422, "That e-mail address does not look valid")
    QI.save_contact(sid.upper(), email, body.contact_name, AU.actor())
    return QI.contact(sid.upper())


@app.get("/api/supplier-contacts/{sid}")
def contact_get(sid: str):
    from . import quality_inquiry as QI
    return QI.contact(sid) or {"supplier_id": sid, "email": None}


@app.get("/api/portal/quality-inquiries")
def portal_qi(as_supplier: str | None = None):
    QI = _qi()
    sid = _portal_sid(as_supplier)
    keep = ("id", "subject", "message", "question", "status", "due_date", "sent_at", "reason", "containment", "corrective_action",
            "commit_date", "responded_at", "responded_via", "material_id", "defect_name")
    return [{k: r[k] for k in keep} for r in QI.list_all(None, sid) if r["status"] in ("SENT", "RESPONDED", "CLOSED")]


@app.post("/api/portal/quality-inquiries/{inq_id}/respond")
def portal_qi_respond(inq_id: int, body: QiReplyIn, as_supplier: str | None = None):
    QI = _qi()
    sid = _portal_sid(as_supplier)
    q = QI.get(inq_id)
    if not q or q["supplier_id"] != sid:
        raise HTTPException(404, "Inquiry not found")
    try:
        return QI.respond(inq_id, body.reason, body.containment, body.corrective_action,
                          body.commit_date.isoformat() if body.commit_date else None, "PORTAL", AU.actor())
    except ValueError as e:
        raise HTTPException(409, str(e))


def _qi_page(title: str, inner: str, ok: bool = True, code: int = 200) -> HTMLResponse:
    import html as H
    return HTMLResponse(
        "<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
        "<title>Quality inquiry</title><body style='font-family:Inter,Arial,sans-serif;background:#f5f7fa;margin:0;padding:24px'>"
        "<div style='max-width:680px;margin:auto;background:#fff;border:1px solid #e3e6ee;border-radius:14px;padding:24px'>"
        f"<h2 style='margin-top:0;color:{'#6c3cc1' if ok else '#c0392b'}'>{H.escape(title)}</h2>{inner}</div></body>", status_code=code)


@app.get("/api/quality-reply/{token}", response_class=HTMLResponse)
def qi_reply_form(token: str):
    """Public reply page opened from the supplier e-mail. The signed token authorises answering exactly this one inquiry."""
    import html as H
    from . import quality_inquiry as QI
    if not is_on("quality_inquiry"):
        return _qi_page("Not available", "<p>This feature is switched off.</p>", False, 400)
    qid = QI.read_token(token)
    q = QI.get(qid) if qid else None
    if not q:
        return _qi_page("Link invalid or expired", "<p>Please answer through the supplier portal.</p>", False, 400)
    msg = "<pre style='white-space:pre-wrap;font-family:inherit;background:#f7f7fb;padding:12px;border-radius:10px'>" + H.escape(q["message"]) + "</pre>"
    if q["status"] not in ("SENT", "RESPONDED"):
        return _qi_page(q["subject"], msg + "<p><b>This inquiry is closed.</b> Thank you.</p>")
    field = lambda n, label, val, rows=4: (f"<label style='display:block;margin:12px 0 4px;font-weight:600'>{label}</label>"
        f"<textarea name='{n}' rows='{rows}' style='width:100%;box-sizing:border-box;padding:8px;border:1px solid #cfd4e0;border-radius:8px'"
        f"{' required minlength=5' if n == 'reason' else ''}>{H.escape(val or '')}</textarea>")
    done = "<p style='color:#0e9c86'><b>You already replied on " + str(q["responded_at"])[:16] + ".</b> You can update your answer below.</p>" if q["status"] == "RESPONDED" else ""
    form = (f"<form method='post'>{field('reason', '1. Reason / root cause *', q['reason'])}{field('containment', '2. Containment', q['containment'], 3)}"
            f"{field('corrective_action', '3. Corrective action', q['corrective_action'], 3)}"
            f"<label style='display:block;margin:12px 0 4px;font-weight:600'>Effective from</label>"
            f"<input type='date' name='commit_date' value='{q['commit_date'] or ''}' style='padding:8px;border:1px solid #cfd4e0;border-radius:8px'>"
            "<p><button type='submit' style='background:#6c3cc1;color:#fff;border:0;border-radius:8px;padding:10px 18px;font-weight:700'>Send reply</button></p></form>")
    return _qi_page(q["subject"], msg + done + form)


@app.post("/api/quality-reply/{token}", response_class=HTMLResponse)
async def qi_reply_submit(token: str, request: Request):
    from urllib.parse import parse_qs
    from . import quality_inquiry as QI
    if not is_on("quality_inquiry"):
        return _qi_page("Not available", "<p>This feature is switched off.</p>", False, 400)
    qid = QI.read_token(token)
    q = QI.get(qid) if qid else None
    if not q:
        return _qi_page("Link invalid or expired", "<p>Please answer through the supplier portal.</p>", False, 400)
    raw = (await request.body())[:20000].decode("utf-8", "replace")
    f = {k: v[0].strip() for k, v in parse_qs(raw).items()}
    if len(f.get("reason", "")) < 5:
        return _qi_page("Reason required", "<p>Please describe the reason (at least a few words). Use your browser's Back button.</p>", False, 422)
    cd = f.get("commit_date") or None
    try:
        if cd:
            dt.date.fromisoformat(cd)
        QI.respond(qid, f["reason"][:4000], f.get("containment", "")[:4000], f.get("corrective_action", "")[:4000], cd, "EMAIL_LINK",
                   f"{q['supplier_id']} (e-mail link)")
    except ValueError as e:
        return _qi_page("Not saved", f"<p>{str(e)}</p>", False, 409)
    return _qi_page("Thank you, your reply was received", "<p>Our supplier quality team has been notified. You can also follow this inquiry in the supplier portal.</p>")


# ================================================================ workflow extras (8D request, weekly digest, evidence pack)
@app.get("/api/suppliers/{sid}/capa-draft")
def capa_draft(sid: str, alert_id: str | None = None):
    """Template-built 8D / CAPA request for a supplier (figures from analytics; no LLM). Saved via POST /api/actions."""
    require("workflow_extras")
    from . import workflow_extras as WX
    try:
        return WX.capa_draft(sid, alert_id)
    except KeyError as e:
        raise HTTPException(404, str(e))


@app.get("/api/digest/{role}")
def digest_preview(role: str):
    require("workflow_extras")
    if role.upper() not in ("PROCUREMENT", "QUALITY"):
        raise HTTPException(404, "role must be procurement or quality")
    from . import workflow_extras as WX
    return WX.digest(role)


@app.get("/api/po-evidence/{po_id}/{po_line}")
def evidence(po_id: str, po_line: int):
    require("workflow_extras")
    from . import workflow_extras as WX
    try:
        return WX.evidence(po_id, po_line)
    except KeyError as e:
        raise HTTPException(404, str(e))


@app.get("/api/po-evidence/{po_id}/{po_line}/pdf")
def evidence_pdf(po_id: str, po_line: int):
    require("workflow_extras")
    from . import workflow_extras as WX
    try:
        ev = WX.evidence(po_id, po_line)
    except KeyError as e:
        raise HTTPException(404, str(e))
    data = WX.evidence_pdf(ev, AU.actor())
    return Response(content=data, media_type="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="evidence_{po_id}_{po_line}.pdf"'})


# ================================================================ requirement data readiness
READINESS = [  # (requirement source, table, what it unlocks, template file in data/templates)
    ("SAP/ERP purchasing: PO lines", "sp.fact_purchase_order_receipt", "OTIF, lead time, price variance", None),
    ("GRN (goods receipts)", "sp.fact_grn", "Split deliveries, receipt-level OTIF", "fact_grn.csv"),
    ("Incoming inspection", "sp.fact_incoming_inspection", "True rejection rate / PPM at receipt", "fact_incoming_inspection.csv"),
    ("Schedule agreements", "sp.fact_schedule_line", "OTIF for scheduling-agreement lines", "fact_schedule_line.csv"),
    ("Supplier invoices", "sp.fact_invoice", "Three-way match (PO / GRN / invoice)", "fact_invoice.csv"),
    ("QMS: production inspections", "sp.fact_quality_inspection", "Defects traced to supplier, lot and plant", None),
    ("QMS: NCR / CAPA", "sp.fact_ncr_capa", "Responsiveness, open-issue ageing, overdue CAPA", None),
    ("Logistics milestones (ASN / TMS / GPS)", "sp.fact_inbound_milestone", "Real shipment positions on the tracker", "fact_inbound_milestone.csv"),
    ("MES: downtime events", "sp.fact_downtime_event", "Line stops traced to suppliers", None),
    ("Working calendar", "sp.dim_shift_calendar", "Lead-time adherence in working days", None),
]


@app.get("/api/data-readiness")
def data_readiness():
    """Row counts for every source the agent requirement names, so missing extracts are visible (Settings → Data health)."""
    return _resp("readiness", 300, _data_readiness)


def _readiness_one_query() -> dict | None:
    parts = []
    for i, (_n, table, _u, _t) in enumerate(READINESS):
        demo_col = "source" if table.endswith("inbound_milestone") else "_source_system"
        demo = (f"count(*) FILTER (WHERE COALESCE({demo_col}, '') LIKE 'SYNTHETIC%' OR COALESCE(_source_system, '') LIKE 'ROLLING%')"
                if table.startswith("sp.fact_") else "0")
        last = "MAX(_loaded_at)" if table.startswith("sp.fact_") else "NULL::timestamptz"   # when the table was last loaded (lineage stamp)
        parts.append(f"SELECT {i} AS i, count(*) AS n, {demo} AS d, {last} AS l FROM {table}")
    try:
        with read_engine.connect() as c:
            return {r[0]: (r[1], r[2], r[3]) for r in c.execute(text(" UNION ALL ".join(parts))).fetchall()}
    except Exception:
        return None


def _data_readiness():
    got = _readiness_one_query()
    if got is not None:
        out = [{"source": name, "table": table, "rows": got[i][0], "demo_rows": got[i][1], "demo": bool(got[i][0]) and got[i][1] == got[i][0],
                "unlocks": unlocks, "template": tmpl, "ready": bool(got[i][0]),
                "last_loaded": got[i][2].isoformat() if got[i][2] else None} for i, (name, table, unlocks, tmpl) in enumerate(READINESS)]
        return {"sources": out, "ready": sum(1 for x in out if x["ready"]), "total": len(out)}
    out = []
    with read_engine.connect() as c:
        for name, table, unlocks, tmpl in READINESS:
            # SYNTHETIC demo rows (scripts/load_synthetic.py, gen_milestones.py) are counted but reported apart, so demo data
            # is never shown as real data
            demo_col = "source" if table.endswith("inbound_milestone") else "_source_system"
            try:
                n = c.execute(text(f"SELECT count(*) FROM {table}")).scalar()
                try:
                    demo = c.execute(text(f"SELECT count(*) FROM {table} WHERE COALESCE({demo_col}, '') LIKE 'SYNTHETIC%'")).scalar()
                except Exception:
                    c.rollback(); demo = 0
            except Exception:
                c.rollback(); n, demo = None, 0
            out.append({"source": name, "table": table, "rows": n, "demo_rows": demo, "demo": bool(n) and demo == n,
                        "unlocks": unlocks, "template": tmpl, "ready": bool(n)})
    return {"sources": out, "ready": sum(1 for x in out if x["ready"]), "total": len(out)}


# ================================================================ UX1 agent architecture
class AgentEmailIn(BaseModel):
    question: str = Field(min_length=1, max_length=4000)
    answer: str = Field(min_length=1, max_length=20000)
    to_role: str = Field(default="PROCUREMENT", pattern="^(PROCUREMENT|QUALITY|BOTH)$")
    grounding: dict | None = None


@app.post("/api/agent/email")
def agent_email(body: AgentEmailIn):
    """Layer 6 email trigger: e-mail the verified answer to the chosen owner(s); always delivered in-app too."""
    require("agent_architecture")
    from . import agent_email as AE2
    return AE2.send(body.question, body.answer, body.to_role, AU.actor(), body.grounding)


@app.get("/api/architecture")
def architecture():
    """How a question flows through the agent, and which datasets / backend function / page each tool uses."""
    require("agent_architecture")
    from . import architecture as ARCH
    return ARCH.describe()


# ================================================================ 1.2 role-based dashboards
@app.get("/api/dashboards/{kind}")
def dashboard(kind: str, plant_id: str | None = None, start: str | None = None, end: str | None = None):
    require("role_dashboards")
    if kind not in DB.ROLES_FOR:
        raise HTTPException(404, "Dashboard must be procurement, quality or management")
    if not AU.has_role(*DB.ROLES_FOR[kind]):
        raise HTTPException(403, f"Your role cannot open the {kind} dashboard")
    return DB.build(kind, plant_id, start, end)


# ================================================================ 1.3 actionable alerts
@app.get("/api/alert-events")
def alert_events(status: str | None = None, include_inactive: bool = False, sync: bool = True):
    require("actionable_alerts")
    if sync:
        AE.sync()
    return AE.list_events(status, not include_inactive)


class AlertActionIn(BaseModel):
    action: str                         # ack | assign | request_response | close | reopen
    assignee: str | None = None
    note: str | None = Field(None, max_length=1000)


@app.post("/api/alert-events/{event_id}/action")
def alert_event_action(event_id: int, body: AlertActionIn):
    require("actionable_alerts")
    try:
        return AE.act(event_id, body.action, AU.actor(), body.assignee, body.note)
    except LookupError as e:
        raise HTTPException(404, str(e))
    except PermissionError as e:
        raise HTTPException(409, str(e))
    except ValueError as e:
        raise HTTPException(422, str(e))


@app.get("/api/alert-events/users")
def alert_assignees():
    require("actionable_alerts")
    return A._run("SELECT username, display_name, role FROM public.app_users WHERE role <> 'SUPPLIER' AND active ORDER BY role, username", {})


@app.get("/api/alert-links/{token}", response_class=HTMLResponse)
def alert_link(token: str):
    """One-click action from Teams / e-mail. The signed token authorises exactly one action on one alert."""
    import html
    page = lambda title, msg, ok=True: HTMLResponse(
        f"<!doctype html><meta name=viewport content='width=device-width'><body style='font-family:Montserrat,Arial;padding:24px;"
        f"max-width:560px;margin:auto'><h2 style='color:{'#6c3cc1' if ok else '#c0392b'}'>{html.escape(title)}</h2><p>{html.escape(msg)}</p>"
        f"<p><a href='{os.environ.get('PUBLIC_APP_URL', 'http://localhost:5173')}/#risk'>Open the Supplier Performance Agent</a></p></body>",
        status_code=200 if ok else 400)
    if not is_on("actionable_alerts"):
        return page("Feature switched off", "Actionable alerts are currently disabled.", False)
    d = AE.read_link(token)
    if not d:
        return page("Link invalid or expired", "Open the app to act on this alert.", False)
    try:
        r = AE.act(d["e"], d["a"], f"{d['r']} (via link)")
        return page("Done", f"Alert #{r['id']} is now {r['status'].replace('_', ' ').lower()}.")
    except Exception as e:  # noqa: BLE001
        return page("Not applied", str(e), False)


# ================================================================ 1.4 export-ready review packs
@app.get("/api/suppliers/{supplier_id}/review-pack/export")
def review_pack_export(supplier_id: str, fmt: str = "pdf", start: str | None = None, end: str | None = None, narrative: bool = False):
    require("review_pack_export")
    if fmt not in ("pdf", "pptx"):
        raise HTTPException(422, "fmt must be pdf or pptx")
    pack = review_pack(supplier_id, start, end)
    text_ = None
    if narrative and settings.llm_configured:
        try:
            text_ = review_narrative(supplier_id)
            text_ = text_["narrative"] if text_.get("grounding", {}).get("grounded") else None   # only verified narratives go in a file
        except Exception:
            text_ = None
    data = EX.pdf(pack, text_) if fmt == "pdf" else EX.pptx(pack, text_)
    m = EX.meta()
    run_id = A.log_run("review_pack_export", {"supplier_id": supplier_id, "fmt": fmt, "start": start, "end": end,
                                             "narrative": bool(text_)}, [pack["scorecard"]])
    with engine.begin() as c:
        c.execute(text("""INSERT INTO public.report_exports (supplier_id, fmt, period_start, period_end, run_id, config_version, bytes, created_by)
                          VALUES (:s, :f, CAST(:a AS date), CAST(:b AS date), :r, :v, :n, :u)"""),
                  {"s": supplier_id, "f": fmt, "a": start, "b": end, "r": run_id, "v": m["config_version"], "n": len(data), "u": AU.actor()})
    name = f"{supplier_id}_review_{start or 'all'}_{end or pack['as_of_date']}.{fmt}"
    mime = "application/pdf" if fmt == "pdf" else "application/vnd.openxmlformats-officedocument.presentationml.presentation"
    return Response(data, media_type=mime, headers={"Content-Disposition": f'attachment; filename="{name}"', "X-Calc-Run": str(run_id)})


# ================================================================ promise-date changes (A6)
class PromiseChangeIn(BaseModel):
    po_id: str
    po_line: int = 1
    new_promise: dt.date
    reason: str = Field(min_length=5)


class DecisionIn(BaseModel):
    status: str                        # APPROVED | REJECTED
    note: str | None = None


def _pc(x: PromiseChange) -> dict:
    return {c.name: getattr(x, c.name) for c in PromiseChange.__table__.columns}


def _create_promise_change(body: PromiseChangeIn, supplier_scope: str | None) -> dict:
    row = A._run(f"SELECT supplier_id, promised_date, receipt_date FROM {A.S}.fact_purchase_order_receipt WHERE po_id=:p AND po_line=:l",
                 {"p": body.po_id, "l": body.po_line})
    if not row:
        raise HTTPException(404, "PO line not found")
    r = row[0]
    if supplier_scope and r["supplier_id"] != supplier_scope:
        raise HTTPException(403, "PO line belongs to another supplier")
    with SessionLocal() as s:
        from . import followup as FU
        old = dt.date.fromisoformat(str(r["promised_date"]))
        x = PromiseChange(po_id=body.po_id, po_line=body.po_line, supplier_id=r["supplier_id"],
                          old_promise=old, new_promise=body.new_promise, reason=body.reason, requested_by=AU.actor())
        auto = FU.auto_decide_promise_change(old, body.new_promise)      # small shift asked in time: approved by policy
        if auto:
            x.status, x.decided_by, x.approved_at = "APPROVED", auto, dt.datetime.now(dt.timezone.utc)
        s.add(x)
        s.add(Notification(recipient_role="PROCUREMENT", supplier_id=r["supplier_id"], severity="INFO",
                           subject=f"Promise-date change {'auto-approved' if auto else 'requested'}: {body.po_id}/{body.po_line}",
                           body=f"{r['promised_date']} -> {body.new_promise}. Reason: {body.reason}" + (f" Approved by {auto}." if auto else "")))
        s.commit()
        return _pc(x)


# ================================================================ AUTO2 sourcing review (switch suggestion, procurement decides)
class SourcingProposeIn(BaseModel):
    case_id: str = Field(min_length=5, max_length=60)
    alt_supplier_id: str | None = Field(None, max_length=20)      # the supplier the buyer chose (default: the suggested one)
    share_pct: int | None = Field(None, ge=5, le=100)             # the trial share the buyer chose


@app.get("/api/sourcing-reviews")
def sourcing_reviews(refresh: bool = False):
    from . import sourcing_review as SR
    cs = SR.build(force=refresh)
    return {"cases": cs, "count": len(cs), "high": sum(1 for c in cs if c["severity"] == "HIGH"),
            "note": "Suggestions for procurement to decide. Nothing changes in purchasing until a buyer approves."}


@app.post("/api/sourcing-reviews/propose")
def sourcing_propose(body: SourcingProposeIn):
    """Turn a case into a PROPOSED corrective action (category COMMERCIAL) that procurement approves or rejects."""
    from . import sourcing_review as SR
    c = next((x for x in SR.build() if x["id"] == body.case_id), None)
    if not c:
        raise HTTPException(404, "case not found (it may no longer apply)")
    i, a = c["incumbent"], c["alternative"]
    if body.alt_supplier_id:
        a = next((x for x in [c["alternative"], *c.get("others", [])] if x and x["supplier_id"] == body.alt_supplier_id), None)
        if not a:
            raise HTTPException(422, "that supplier is not a qualified alternative for this case")
    share = body.share_pct or c["impact"].get("share_pct", 30)
    title = (f"Sourcing review: move {share}% of {c['material_name']} from {i['supplier_name']} to {a['supplier_name']}" if a
             else f"Sourcing review: RFQ for a new {c['material_name']} source ({i['supplier_name']} failing)")[:200]
    with SessionLocal() as s:
        x = CorrectiveAction(supplier_id=i["supplier_id"], category="COMMERCIAL", priority="HIGH" if c["severity"] == "HIGH" else "MEDIUM",
                             title=title, owner_role="PROCUREMENT", origin="AGENT", status="PROPOSED",
                             description=SR.brief(c) + (f"\n\n#### Buyer's plan\nMove {share}% of the next {c['material_name']} orders "
                                                       f"(about {c['impact'].get('fwd_qty', 0) * share / 100:,.0f} units a quarter) to {a['supplier_name']}." if a else ""),
                             due_date=dt.date.today() + dt.timedelta(days=14),
                             history=[{"at": dt.datetime.now(dt.timezone.utc).isoformat(), "by": AU.actor(), "event": "proposed from sourcing review", "case": c["id"]}])
        s.add(x)
        s.add(Notification(recipient_role="PROCUREMENT", supplier_id=i["supplier_id"], severity="HIGH" if c["severity"] == "HIGH" else "INFO",
                           subject=title, body="A sourcing review was proposed for your approval in Risk Radar > Actions."))
        s.commit()
        aid = x.id
    try:
        from .integration import service as SV
        SV.audit(AU.actor(), "sourcing:propose", None, {"case": c["id"], "action_id": aid})
    except Exception:
        pass
    return {"action_id": aid, "title": title}


# ================================================================ AUTO1 automatic supplier follow-up
class FollowupReplyIn(BaseModel):
    confirmed_date: dt.date
    reason_code: str = Field(min_length=3, max_length=40)
    reason_text: str = Field("", max_length=1000)


@app.get("/api/followups")
def followups_list():
    from . import followup as FU
    return _resp("followups", 30, lambda: {**FU.summary(), "rows": FU.listing()})


@app.post("/api/followups/run")
def followups_run():
    return J.run_job("followups", trigger="manual")       # follow-ups change no KPI: no cache clear (that forces a full recompute)


@app.get("/api/portal/followups")
def portal_followups(as_supplier: str | None = None):
    from . import followup as FU
    return {"rows": FU.listing(_portal_sid(as_supplier), open_only=True), "reasons": FU.REASONS}


@app.post("/api/portal/followups/{fid}/respond")
def portal_followup_respond(fid: int, body: FollowupReplyIn, as_supplier: str | None = None):
    from . import followup as FU
    try:
        return FU.respond(_portal_sid(as_supplier), fid, body.confirmed_date, body.reason_code, body.reason_text, AU.actor())
    except LookupError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(422, str(e))


@app.get("/api/promise-changes")
def list_promise_changes(status: str | None = None):
    with ReadSession() as s:
        q = s.query(PromiseChange)
        if status:
            q = q.filter_by(status=status)
        return [_pc(x) for x in q.order_by(PromiseChange.id.desc()).limit(200)]


@app.post("/api/promise-changes")
def create_promise_change(body: PromiseChangeIn):
    return _create_promise_change(body, None)


@app.patch("/api/promise-changes/{cid}")
def decide_promise_change(cid: int, body: DecisionIn):
    """Only an APPROVED change, approved on/before the ORIGINAL promise date, moves the OTIF baseline (see views.sql)."""
    if body.status not in ("APPROVED", "REJECTED"):
        raise HTTPException(422, "status must be APPROVED or REJECTED")
    with SessionLocal() as s:
        x = s.get(PromiseChange, cid)
        if not x:
            raise HTTPException(404, "Change not found")
        if x.status != "PROPOSED":
            raise HTTPException(409, f"Already {x.status}")
        x.status, x.decided_by = body.status, AU.actor()
        if body.status == "APPROVED":
            x.approved_at = now()
        s.commit()
        out = _pc(x)
    A.clear_cache()
    out["counts_for_otif"] = body.status == "APPROVED" and now().date() <= out["old_promise"]
    return out


# ================================================================ master-data aliases (G2)
class AliasIn(BaseModel):
    alias: str = Field(min_length=2, max_length=200)
    supplier_id: str
    source_system: str = "MANUAL"


@app.get("/api/aliases")
def list_aliases():
    with SessionLocal() as s:
        return [{c.name: getattr(a, c.name) for c in SupplierAlias.__table__.columns} for a in s.query(SupplierAlias)]


@app.post("/api/aliases")
def add_alias(body: AliasIn):
    if not A._run(f"SELECT 1 FROM {A.S}.dim_supplier WHERE supplier_id=:s", {"s": body.supplier_id}):
        raise HTTPException(404, "Supplier not found")
    with SessionLocal() as s:
        s.add(SupplierAlias(alias=body.alias.strip().lower(), supplier_id=body.supplier_id, source_system=body.source_system,
                            created_by=AU.actor()))
        try:
            s.commit()
        except Exception:
            raise HTTPException(409, "Alias already mapped")
    return {"ok": True}


# ================================================================ supplier portal (C4 / A4)
def _scope() -> str:
    u = AU.current_user()
    if u.role == "SUPPLIER":
        return u.supplier_id
    raise HTTPException(400, "Portal endpoints are for supplier users (ADMIN: use ?as_supplier=)")


def _portal_sid(as_supplier: str | None) -> str:
    u = AU.current_user()
    if u.role == "ADMIN" and as_supplier:
        return as_supplier
    return _scope()


@app.get("/api/portal/me")
def portal_me(as_supplier: str | None = None):
    sid = _portal_sid(as_supplier)
    card = A.scorecard(None, None, None, sid)
    keep = ("supplier_id", "supplier_name", "grade", "composite_score", "scores", "otif_pct", "on_time_pct", "in_full_pct",
            "lead_time_adherence_pct", "avg_lead_time_variance_days", "incoming_ppm", "ppm_method", "open_ncr", "overdue_capa",
            "avg_response_days", "past_due_lines", "health")
    with SessionLocal() as s:
        acts = [_action_dict(a) for a in s.query(CorrectiveAction).filter_by(supplier_id=sid)
                .filter(CorrectiveAction.status.in_(["APPROVED", "SUPPLIER_RESPONDED", "VERIFIED", "CLOSED"]))
                .order_by(CorrectiveAction.id.desc())]
        pcs = [_pc(x) for x in s.query(PromiseChange).filter_by(supplier_id=sid).order_by(PromiseChange.id.desc()).limit(50)]
    ncrs = A._run(f"""SELECT ncr_id, MIN(opened_ts) AS opened_ts, MIN(defect_code) AS defect_code, MIN(defect_name) AS defect_name,
                             MIN(capa_status) AS capa_status, MIN(capa_due_date) AS capa_due_date, bool_or(is_overdue) AS is_overdue,
                             MAX(open_age_days) AS open_age_days, STRING_AGG(DISTINCT material_id, ', ') AS material_id,
                             MIN(first_response_at) AS first_response_at, ROUND(MIN(response_days)::numeric,1) AS response_days
                      FROM {A.S}.v_ncr_supplier WHERE supplier_id=:s AND is_open GROUP BY ncr_id ORDER BY MIN(opened_ts) DESC""", {"s": sid})
    open_pos = A._run(f"""SELECT po_id, po_line, material_id, material_name, plant_id, promised_date, original_promised_date,
                                 ordered_qty, received_qty, delivery_status, promise_changed
                          FROM {A.S}.v_po_delivery WHERE supplier_id=:s AND delivery_status <> 'RECEIVED'
                          ORDER BY promised_date""", {"s": sid})
    with SessionLocal() as s:
        reqs = [{"id": n.id, "subject": n.subject, "body": n.body, "severity": n.severity, "created_at": n.created_at, "is_read": n.is_read}
                for n in s.query(Notification).filter_by(recipient_role="SUPPLIER", supplier_id=sid).order_by(Notification.id.desc()).limit(30)]
    return {"supplier_id": sid, "scorecard": {k: card[0].get(k) for k in keep} if card else None, "requests": reqs,
            "open_ncrs": ncrs, "open_pos": open_pos, "actions": acts, "promise_changes": pcs,
            "as_of_date": A.as_of_date()}


class NcrRespondIn(BaseModel):
    response: str = Field(min_length=5, max_length=4000)
    containment: str | None = None


@app.post("/api/portal/ncr/{ncr_id}/respond")
def portal_ncr_respond(ncr_id: str, body: NcrRespondIn, as_supplier: str | None = None):
    sid = _portal_sid(as_supplier)
    if not A._run(f"SELECT 1 FROM {A.S}.v_ncr_supplier WHERE ncr_id=:n AND supplier_id=:s", {"n": ncr_id, "s": sid}):
        raise HTTPException(404, "NCR not found for your company")
    with SessionLocal() as s:
        s.add(NcrResponse(ncr_id=ncr_id, supplier_id=sid, response=body.response, containment=body.containment,
                          responded_by=AU.actor()))
        s.add(Notification(recipient_role="QUALITY", supplier_id=sid, severity="INFO", subject=f"Supplier responded to {ncr_id}",
                           body=body.response[:400]))
        s.commit()
    A.clear_cache()
    return {"ok": True}


class PortalActionIn(BaseModel):
    response: str = Field(min_length=5, max_length=4000)


@app.post("/api/portal/actions/{action_id}/respond")
def portal_action_respond(action_id: int, body: PortalActionIn, as_supplier: str | None = None):
    sid = _portal_sid(as_supplier)
    with SessionLocal() as s:
        a = s.get(CorrectiveAction, action_id)
        if not a or a.supplier_id != sid:
            raise HTTPException(404, "Action not found for your company")
        if a.status != "APPROVED":
            raise HTTPException(409, f"Action is {a.status}; only APPROVED actions await your response")
        a.status, a.supplier_response, a.responded_at = "SUPPLIER_RESPONDED", body.response, now()
        a.history = list(a.history or []) + [{"at": now().isoformat(), "by": AU.actor(), "status": "SUPPLIER_RESPONDED"}]
        s.add(Notification(recipient_role=a.owner_role, supplier_id=sid, action_id=a.id, severity="INFO",
                           subject=f"Supplier responded to action #{a.id}", body=body.response[:400]))
        s.commit()
        return _action_dict(a)


@app.post("/api/portal/promise-change")
def portal_promise_change(body: PromiseChangeIn, as_supplier: str | None = None):
    return _create_promise_change(body, _portal_sid(as_supplier))


# ---------------------------------------------------------------- supplier portal: vehicles + manual GPS (device_type MANUAL)
class FleetVehicleIn(BaseModel):
    kind: str = Field("TRUCK", pattern="^(TRUCK|VESSEL)$")
    registration: str = Field(min_length=3, max_length=60)
    carrier: str = Field("", max_length=120)
    driver_name: str = Field("", max_length=80)
    driver_phone: str = Field("", max_length=30, pattern=r"^[0-9 +()-]*$")


class FleetPositionIn(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    ts: dt.datetime | None = None
    speed_kmh: float | None = Field(None, ge=0, le=160)
    note: str | None = Field(None, max_length=200)


class FleetDispatchIn(BaseModel):
    vehicle_id: str | None = None                     # an existing vehicle, or `vehicle` to register one in the same step
    vehicle: FleetVehicleIn | None = None
    shipment_ids: list[str] = Field(min_length=1, max_length=50)
    departed_at: dt.datetime | None = None
    position: FleetPositionIn | None = None           # optional first position (e.g. the supplier's gate)


class FleetPositionsIn(BaseModel):
    positions: list[FleetPositionIn] = Field(min_length=1, max_length=200)


def _fleet_err(fn):
    try:
        return fn()
    except LookupError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(422, str(e))


def _fleet_audit(action: str, sid: str, detail: dict) -> None:
    try:
        from .integration import service as SV
        SV.audit(AU.actor(), action, None, {"supplier_id": sid, **detail})
    except Exception:
        pass


def _pos_dicts(ps: list[FleetPositionIn]) -> list[dict]:
    return [{"lat": p.lat, "lon": p.lon, "ts": p.ts.isoformat() if p.ts else None, "speed_kmh": p.speed_kmh, "note": p.note} for p in ps]


@app.get("/api/portal/fleet")
def portal_fleet(as_supplier: str | None = None):
    """The supplier's vehicles with their current trip and last reported position, the open PO lines it can dispatch,
    and quick-pick places (its own site, ports, our plants) for the position form."""
    from . import gps as G
    require("gps_tracking")
    sid = _portal_sid(as_supplier)
    fleet = G.supplier_fleet(sid)
    on_trip = {s: v["vehicle_id"] for v in fleet for s in (v["shipment_ids"] or [])}
    lines = A._run(f"""SELECT po_id || '-' || po_line AS id, po_id, po_line, material_name, plant_id, promised_date, ordered_qty,
                              delivery_status FROM {A.S}.v_po_delivery
                       WHERE supplier_id = :s AND delivery_status <> 'RECEIVED' AND NOT excluded ORDER BY promised_date""", {"s": sid})
    for ln in lines:
        ln["vehicle_id"] = on_trip.get(ln["id"])
    places = []
    if sid in TR.SUPPLIER_SITES:
        name, lat, lon, port = TR.SUPPLIER_SITES[sid]
        places.append({"name": f"Our site · {name}", "lat": lat, "lon": lon})
        if port and port in TR.PORTS:
            places.append({"name": TR.PORTS[port][0], "lat": TR.PORTS[port][1], "lon": TR.PORTS[port][2]})
    places += [{"name": n, "lat": la, "lon": lo} for n, la, lo in TR.INDIA_PORTS.values()]
    places += [{"name": n, "lat": la, "lon": lo} for n, la, lo in TR.PLANTS.values()]
    return {"supplier_id": sid, "vehicles": fleet, "open_lines": lines, "places": places}


@app.post("/api/portal/fleet/vehicles")
def portal_fleet_vehicle(body: FleetVehicleIn, as_supplier: str | None = None):
    from . import gps as G
    require("gps_tracking")
    sid = _portal_sid(as_supplier)
    vid = _fleet_err(lambda: G.supplier_add_vehicle(sid, body.kind, body.registration, body.carrier, body.driver_name, body.driver_phone, AU.actor()))
    _fleet_audit("fleet:vehicle", sid, {"vehicle_id": vid, "registration": body.registration})
    return {"vehicle_id": vid}


@app.post("/api/portal/fleet/dispatch")
def portal_fleet_dispatch(body: FleetDispatchIn, as_supplier: str | None = None):
    """One form: (register the vehicle) -> load the PO lines on it -> optionally its first position."""
    from . import gps as G
    require("gps_tracking")
    sid = _portal_sid(as_supplier)
    vid = body.vehicle_id
    if not vid:
        if not body.vehicle:
            raise HTTPException(422, "choose a vehicle or enter a new one")
        v = body.vehicle
        vid = _fleet_err(lambda: G.supplier_add_vehicle(sid, v.kind, v.registration, v.carrier, v.driver_name, v.driver_phone, AU.actor()))
    trip = _fleet_err(lambda: G.supplier_dispatch(sid, vid, body.shipment_ids, body.departed_at, AU.actor()))
    pos = _fleet_err(lambda: G.supplier_report_positions(sid, vid, _pos_dicts([body.position]), AU.actor())) if body.position else None
    _fleet_audit("fleet:dispatch", sid, {"vehicle_id": vid, "trip_id": trip["trip_id"], "shipments": body.shipment_ids})
    A.clear_cache()
    return {**trip, "position": pos}


@app.post("/api/portal/fleet/{vehicle_id}/positions")
def portal_fleet_positions(vehicle_id: str, body: FleetPositionsIn, as_supplier: str | None = None):
    from . import gps as G
    require("gps_tracking")
    sid = _portal_sid(as_supplier)
    res = _fleet_err(lambda: G.supplier_report_positions(sid, vehicle_id, _pos_dicts(body.positions), AU.actor()))
    _fleet_audit("fleet:position", sid, {"vehicle_id": vehicle_id, "accepted": res["accepted"], "rejected": res["rejected"]})
    return res


@app.post("/api/portal/fleet/{vehicle_id}/arrived")
def portal_fleet_arrived(vehicle_id: str, as_supplier: str | None = None):
    from . import gps as G
    require("gps_tracking")
    sid = _portal_sid(as_supplier)
    n = _fleet_err(lambda: G.supplier_arrived(sid, vehicle_id))
    _fleet_audit("fleet:arrived", sid, {"vehicle_id": vehicle_id})
    A.clear_cache()
    return {"ended": n}
