"""Authentication and role-based access (D2/D3/D4).

- Passwords: PBKDF2-HMAC-SHA256 (200k iterations, per-user salt).
- Tokens: stateless HMAC-signed bearer tokens (username, role, supplier scope, expiry). Secret from AUTH_SECRET or a
  generated file backend/.auth_secret (gitignored).
- Roles: ADMIN | PROCUREMENT | QUALITY | VIEWER | SUPPLIER (supplier users only see their own supplier via /api/portal).
- One central POLICY table decides who may call which endpoint; endpoints also read the actor from the token,
  never from the request body.
"""
from __future__ import annotations

import base64
import contextvars
import hashlib
import hmac
import json
import os
import re
import secrets
import time
from dataclasses import dataclass

from .config import BACKEND_DIR

ROLES = ("ADMIN", "PROCUREMENT", "QUALITY", "VIEWER", "SUPPLIER")
TOKEN_TTL_S = 12 * 3600
_SECRET_FILE = BACKEND_DIR / ".auth_secret"
CRED_FILE = BACKEND_DIR / ".credentials.txt"


def _secret() -> bytes:
    env = os.environ.get("AUTH_SECRET")
    if env:
        return env.encode()
    if not _SECRET_FILE.exists():
        _SECRET_FILE.write_text(secrets.token_hex(32), encoding="utf-8")
    return _SECRET_FILE.read_text(encoding="utf-8").strip().encode()


# ---------------------------------------------------------------- passwords
def hash_password(pw: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, 200_000)
    return f"pbkdf2$200000${salt.hex()}${dk.hex()}"


def verify_password(pw: str, stored: str) -> bool:
    try:
        _, it, salt, dk = stored.split("$")
        calc = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), int(it))
        return hmac.compare_digest(calc.hex(), dk)
    except Exception:
        return False


# ---------------------------------------------------------------- tokens
@dataclass
class UserCtx:
    username: str
    role: str
    supplier_id: str | None = None
    display_name: str = ""

    def as_dict(self) -> dict:
        return {"username": self.username, "role": self.role, "supplier_id": self.supplier_id, "display_name": self.display_name}


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def issue_token(u: UserCtx) -> str:
    body = _b64(json.dumps({**u.as_dict(), "exp": int(time.time()) + TOKEN_TTL_S}).encode())
    sig = _b64(hmac.new(_secret(), body.encode(), hashlib.sha256).digest())
    return f"{body}.{sig}"


def read_token(tok: str) -> UserCtx | None:
    try:
        body, sig = tok.split(".")
        good = _b64(hmac.new(_secret(), body.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(good, sig):
            return None
        d = json.loads(_unb64(body))
        if d["exp"] < time.time() or d["role"] not in ROLES:
            return None
        return UserCtx(d["username"], d["role"], d.get("supplier_id"), d.get("display_name", ""))
    except Exception:
        return None


# Current user for code that has no request (agent tools, narrative).
CURRENT_USER: contextvars.ContextVar[UserCtx | None] = contextvars.ContextVar("current_user", default=None)


def current_user() -> UserCtx | None:
    return CURRENT_USER.get()


def has_role(*roles: str) -> bool:
    u = CURRENT_USER.get()
    return bool(u and (u.role in roles or u.role == "ADMIN"))


# ---------------------------------------------------------------- access policy
INTERNAL = ("ADMIN", "PROCUREMENT", "QUALITY", "VIEWER")
EDITORS = ("ADMIN", "PROCUREMENT", "QUALITY")
# (method, path regex, allowed roles) - first match wins. Unlisted /api paths: GET -> INTERNAL, others -> EDITORS.
POLICY: list[tuple[str, str, tuple]] = [
    ("*", r"^/api/health$", ("*",)),
    ("GET", r"^/api/alert-links/", ("*",)),
    ("*", r"^/api/quality-reply/", ("*",)),             # signed reply link in the supplier e-mail (token = authorisation)
    ("POST", r"^/api/quality-inquiries/\d+/(send|close)$", ("ADMIN", "QUALITY")),
    ("POST", r"^/api/quality-inquiries/send-all$", ("ADMIN", "QUALITY")),     # e-mails leave the company: quality owners only
    ("POST", r"^/api/predictions/retrain$", ("ADMIN",)),                       # model changes: admins only
    ("*", r"^/api/supplier-contacts", ("ADMIN", "QUALITY", "PROCUREMENT")),            # signed one-click links from Teams / e-mail (token = authorisation)
    ("PUT", r"^/api/features/", ("ADMIN",)),
    ("POST", r"^/api/tracker/routes/refresh$", ("ADMIN",)),
    # T3 GPS: devices authenticate with X-Device-Key inside the endpoint (no user session on a driver's phone)
    ("POST", r"^/api/gps/pings$", ("*",)),
    ("GET", r"^/api/gps/trip$", ("*",)),
    ("*", r"^/api/vehicles", ("ADMIN", "PROCUREMENT")),
    ("POST", r"^/api/disruptions/refresh$", ("ADMIN",)),
    ("POST", r"^/api/disruptions$", ("ADMIN", "PROCUREMENT")),
    ("DELETE", r"^/api/disruptions/", ("ADMIN", "PROCUREMENT")),
    ("*", r"^/api/saved-views", INTERNAL),
    # Data integration: everyone internal may VIEW status; only ADMIN creates / edits / tests / previews source data
    ("GET", r"^/api/integration/connections/[^/]+/(preview|schema)$", ("ADMIN",)),
    ("GET", r"^/api/integration/quarantine", ("ADMIN",)),                 # quarantined rows hold (masked) source values
    ("GET", r"^/api/integration/", INTERNAL),
    ("*", r"^/api/integration/", ("ADMIN",)),
    ("GET", r"^/api/features$", ROLES),
    ("POST", r"^/api/auth/login$", ("*",)),
    ("GET", r"^/api/auth/me$", ROLES),
    ("*", r"^/api/portal/", ("SUPPLIER", "ADMIN")),
    # sensitive commercial / sourcing data (D3)
    ("GET", r"^/api/materials/[^/]+/sourcing$", ("ADMIN", "PROCUREMENT")),
    ("GET", r"^/api/compare$", ("ADMIN", "PROCUREMENT")),
    ("GET", r"^/api/concentration$", ("ADMIN", "PROCUREMENT", "QUALITY")),
    # read-only analysis that is not persisted is open to every internal role
    ("POST", r"^/api/chat(/stream)?$", INTERNAL),
    ("POST", r"^/api/scorecard/simulate$", INTERNAL),
    ("POST", r"^/api/twin/simulate$", INTERNAL),
    ("POST", r"^/api/cache/clear$", INTERNAL),
    ("POST", r"^/api/notifications/read-all$", INTERNAL),
    ("POST", r"^/api/suppliers/[^/]+/review-pack/narrative$", INTERNAL),
    # governance (ADMIN only): changes AND the admin read-outs (calculation runs, data quality, jobs, reconciliation, AI usage)
    ("GET", r"^/api/(runs|dq|jobs|recon|ai-usage|data-readiness)(/|$)", ("ADMIN",)),
    ("PUT", r"^/api/config/", ("ADMIN",)),
    ("*", r"^/api/users", ("ADMIN",)),
    ("POST", r"^/api/jobs/", ("ADMIN",)),
    ("*", r"^/api/aliases", ("ADMIN", "PROCUREMENT")) ,
    ("POST", r"^/api/recon/run$", ("ADMIN",)),
    ("POST", r"^/api/followups/run$", ("ADMIN", "PROCUREMENT")),
    ("*", r"^/api/sourcing-reviews", ("ADMIN", "PROCUREMENT")),          # sourcing data and proposals: procurement only
    # promise changes: procurement decides
    ("PATCH", r"^/api/promise-changes/", ("ADMIN", "PROCUREMENT")),
    ("POST", r"^/api/promise-changes$", ("ADMIN", "PROCUREMENT")),
]


def allowed(method: str, path: str, role: str | None) -> bool:
    for m, rx, roles in POLICY:
        if (m == "*" or m == method) and re.search(rx, path):
            return "*" in roles or (role in roles)
    if role is None:
        return False
    if method in ("GET", "HEAD", "OPTIONS"):
        return role in INTERNAL
    return role in EDITORS


def actor() -> str:
    u = CURRENT_USER.get()
    return u.username if u else "system"


# ---------------------------------------------------------------- seeding
def seed_users() -> list[tuple[str, str, str]]:
    """Create the default users once, with random passwords written to backend/.credentials.txt (gitignored)."""
    from sqlalchemy import text

    from .db import SessionLocal, User, engine
    created = []
    with SessionLocal() as s:
        if s.query(User).first():
            return []
        people = [("admin", "Platform Admin", "ADMIN", None), ("procurement", "Procurement Manager", "PROCUREMENT", None),
                  ("quality", "Supplier Quality Engineer", "QUALITY", None), ("viewer", "Plant Leadership (read-only)", "VIEWER", None)]
        with engine.connect() as c:
            for sid, name in c.execute(text("SELECT supplier_id, supplier_name FROM sp.dim_supplier ORDER BY supplier_id")):
                people.append((f"supplier.{sid.lower().replace('-', '')}", f"{name} (supplier portal)", "SUPPLIER", sid))
        for un, dn, role, sid in people:
            pw = secrets.token_urlsafe(9)
            s.add(User(username=un, display_name=dn, role=role, supplier_id=sid, password_hash=hash_password(pw)))
            created.append((un, role, pw))
        s.commit()
    CRED_FILE.write_text("Initial credentials (change after first login; this file is gitignored)\n\n"
                         + "\n".join(f"{u:<28} {r:<12} {p}" for u, r, p in created) + "\n", encoding="utf-8")
    return created
