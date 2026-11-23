"""Feature flags (public.feature_flags). Every enhancement is switched on/off here; all start disabled.

    from .features import is_on, require
    if is_on("chat_charts"): ...
    require("role_dashboards")          # raises 404 when the feature is off
"""
from __future__ import annotations

import threading
import time

from fastapi import HTTPException
from sqlalchemy import text

_CACHE: dict = {"t": 0.0, "v": {}}
_LOCK = threading.Lock()
TTL = 15


_REFRESHING = [False]


def all_flags(force: bool = False) -> dict[str, bool]:
    from .db import read_engine
    with _LOCK:
        age, have = time.time() - _CACHE["t"], bool(_CACHE["v"])
        if not force and age < TTL:
            return dict(_CACHE["v"])
        if not force and have:                       # stale: answer now, refresh in the background (no request waits on Neon)
            if not _REFRESHING[0]:
                _REFRESHING[0] = True
                threading.Thread(target=_refresh, daemon=True).start()
            return dict(_CACHE["v"])
    return _load(read_engine)


def _refresh() -> None:
    from .db import read_engine
    try:
        _load(read_engine)
    finally:
        _REFRESHING[0] = False


def _load(read_engine) -> dict[str, bool]:
    try:
        with read_engine.connect() as c:
            v = {k: bool(e) for k, e in c.execute(text("SELECT key, enabled FROM public.feature_flags"))}
    except Exception:                                 # a network blip must not switch every feature off: keep the last good set
        with _LOCK:
            return dict(_CACHE["v"])
    with _LOCK:
        _CACHE.update(t=time.time(), v=v)
    return dict(v)


def is_on(key: str) -> bool:
    return all_flags().get(key, False)


def require(key: str) -> None:
    if not is_on(key):
        raise HTTPException(404, f"Feature '{key}' is switched off (Settings → Feature flags)")


def set_flag(key: str, enabled: bool, actor: str) -> None:
    from .db import engine
    with engine.begin() as c:
        n = c.execute(text("UPDATE public.feature_flags SET enabled=:e, updated_by=:a, updated_at=now() WHERE key=:k"),
                      {"e": enabled, "a": actor, "k": key}).rowcount
    if not n:
        raise HTTPException(404, "Unknown feature flag")
    all_flags(force=True)


def list_flags() -> list[dict]:
    from .db import read_engine
    with read_engine.connect() as c:
        return [dict(r._mapping) for r in c.execute(text(
            "SELECT key, enabled, phase, description, updated_by, updated_at FROM public.feature_flags ORDER BY phase, key"))]
