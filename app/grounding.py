"""Numeric grounding validator (F6).

Every figure in an AI-written answer must be traceable to a figure in the tool results (or the user's question).
Accepted: exact values within display rounding, unit conversions (Cr = 1e7, lakh = 1e5, K = 1e3, M = 1e6),
percent <-> fraction, and simple differences of two source figures (e.g. "down 5.2 pts").
Small integers (<= 12), years and dates are ignored (list numbering, "last 3 deliveries", "2026").
"""
from __future__ import annotations

import itertools
import json
import re

NUM_RE = re.compile(r"(?<![\w.])[-−]?\d[\d,]*(?:\.\d+)?(?:\s?(?:cr|crore|lakh|lakhs|l|k|m|mn|b|bn|%))?(?![\w])", re.I)
DATE_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2})?)?\b")
ID_RE = re.compile(r"\b[A-Z]{2,}[-_][A-Z0-9-_/]+\b")      # SUP-001, MAT-PCB-004, PO-000123/1, IN-TN-01
SCALE = {"cr": 1e7, "crore": 1e7, "lakh": 1e5, "lakhs": 1e5, "l": 1e5, "k": 1e3, "m": 1e6, "mn": 1e6, "b": 1e9, "bn": 1e9}


def _strip(text: str) -> str:
    return ID_RE.sub(" ", DATE_RE.sub(" ", text or ""))


def _parse(tok: str) -> tuple[float, int, float]:
    t = tok.strip().lower().replace("−", "-")
    m = re.match(r"(-?[\d,]+(?:\.\d+)?)\s?([a-z%]*)", t)
    num, unit = m.group(1).replace(",", ""), m.group(2)
    dec = len(num.split(".")[1]) if "." in num else 0
    return float(num), dec, SCALE.get(unit, 1.0)


def source_numbers(*texts: str) -> list[float]:
    out = set()
    for t in texts:
        for tok in NUM_RE.findall(_strip(t)):
            try:
                v, _, sc = _parse(tok)
                out.add(v * sc)
            except Exception:
                pass
    return sorted(out)


def _close(a: float, b: float, tol_abs: float) -> bool:
    return abs(a - b) <= max(tol_abs, 0.015 * abs(b)) + 1e-9


def check(answer: str, sources: list[str]) -> dict:
    src = source_numbers(*sources)
    pairs = src[:300]
    diffs = {round(abs(a - b), 6) for a, b in itertools.combinations(pairs, 2)} if len(pairs) > 1 else set()
    unsupported, checked = [], 0
    for tok in NUM_RE.findall(_strip(answer)):
        try:
            v, dec, sc = _parse(tok)
        except Exception:
            continue
        if sc == 1.0 and float(v).is_integer() and (abs(v) <= 12 or 1990 <= v <= 2100):
            continue
        checked += 1
        tol = 0.5 * 10 ** (-dec) * sc
        cands = [v * sc, abs(v) * sc, v / 100.0, v * 100.0]
        ok = any(_close(c, s, tol) for c in cands for s in src) or any(_close(abs(v) * sc, d, tol) for d in diffs)
        if not ok and sc == 1.0:
            # a bare number under a "(₹ Cr)" / "(₹ L)" table header: 4.12 means 41,176,930 rupees
            ok = any(_close(abs(v) * u, s, 0.5 * 10 ** (-dec) * u) for u in (1e7, 1e5) for s in src)
        if not ok:
            unsupported.append(tok.strip())
    return {"checked": checked, "unsupported": unsupported[:15], "grounded": not unsupported}


def grounded_generate(gen, prompt: str, facts: dict) -> tuple[str, dict]:
    """One-shot generation with one correction retry; flags any figure that is still unsupported."""
    src = json.dumps(facts, default=str)
    text = gen(prompt)
    res = check(text, [src])
    if not res["grounded"]:
        text = gen(prompt + "\n\nVALIDATION FAILED. These figures do not appear in the facts: "
                   + ", ".join(res["unsupported"]) + ". Rewrite using ONLY figures present in the facts, copied exactly.")
        res = check(text, [src])
        res["retried"] = True
    if not res["grounded"]:
        text += "\n\n> ⚠️ Unverified figures (not found in the source data): " + ", ".join(res["unsupported"])
    return text, res
