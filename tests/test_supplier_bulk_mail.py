"""Agent layer 6 → affected suppliers: one quality e-mail draft per supplier with defective material, sent only on a human click."""
from sqlalchemy import text

from app import analytics as A
from app import auth as AU
from app import quality_inquiry as QI
from app.db import engine


def _affected(n=2):
    rows = A._run(f"""SELECT supplier_id FROM {A.S}.v_defect_attribution WHERE inspection_ts >= {A.S}.as_of_date() - 60
                      GROUP BY supplier_id ORDER BY SUM(attribution_weight) DESC LIMIT :n""", {"n": n})
    return [r["supplier_id"] for r in rows]


def _cleanup(ids):
    with engine.begin() as c:
        for i in ids:
            c.execute(text("DELETE FROM public.notifications WHERE body LIKE :b"), {"b": f"%(#{i})%"})
            c.execute(text("DELETE FROM public.quality_inquiries WHERE id = :i"), {"i": i})


def test_one_draft_per_affected_supplier_listing_its_own_material(monkeypatch):
    monkeypatch.delenv("SMTP_HOST", raising=False)
    sids = _affected()
    assert sids, "no defect data to test with"
    with engine.begin() as c:                         # remember any saved addresses so the test leaves them as they were
        before = {r[0]: (r[1], r[2]) for r in c.execute(text("SELECT supplier_id, email, contact_name FROM public.supplier_contacts WHERE supplier_id = ANY(:s)"), {"s": sids})}
    out = QI.bulk_draft(sids + [sids[0], "SUP-999"], "pytest")
    made = [d["id"] for d in out["drafts"] if not d["existing"]]
    try:
        by = {d["supplier_id"]: d for d in out["drafts"]}
        assert set(by) | {s["supplier_id"] for s in out["skipped"] if s["reason"].startswith("already asked")} >= set(sids)
        assert len(out["drafts"]) == len(by)                                            # duplicates collapsed
        assert any(s["supplier_id"] == "SUP-999" and s["reason"] == "unknown supplier" for s in out["skipped"])
        for sid, d in by.items():
            assert d["status"] == "DRAFT" and d["supplier_name"] in d["message"]
            mats = {r["material_id"] for r in QI.evidence(sid)["rows"]}
            assert any(m in d["message"] for m in mats)                                 # names its own defective material
        again = QI.bulk_draft(sids, "pytest")                                           # no second draft for the same supplier
        assert {d["id"] for d in again["drafts"]} <= {d["id"] for d in out["drafts"]}
        # send: saved address used, otherwise portal only; nothing sent twice
        target = next(d for d in out["drafts"] if not d["existing"]) if made else None
        if target:
            QI.save_contact(target["supplier_id"], "quality@example.com", None, "pytest")
            r = QI.send_many([target["id"]], "pytest")["results"][0]
            assert r["ok"] and r["to"] == "quality@example.com" and r["email_status"] == "NOT_CONFIGURED"
            assert not QI.send_many([target["id"]], "pytest")["results"][0]["ok"]
    finally:
        _cleanup(made)
        with engine.begin() as c:
            for sid in sids:
                if sid in before:
                    c.execute(text("UPDATE public.supplier_contacts SET email = :e, contact_name = :n WHERE supplier_id = :s"),
                              {"e": before[sid][0], "n": before[sid][1], "s": sid})
                else:
                    c.execute(text("DELETE FROM public.supplier_contacts WHERE supplier_id = :s"), {"s": sid})


def test_contacts_list_and_access():
    rows = QI.list_contacts()
    assert rows and {"supplier_id", "supplier_name", "email"} <= set(rows[0])
    for role in ("ADMIN", "QUALITY"):
        assert AU.allowed("POST", "/api/quality-inquiries/send-all", role)
    for role in ("PROCUREMENT", "VIEWER", "SUPPLIER"):
        assert not AU.allowed("POST", "/api/quality-inquiries/send-all", role)   # e-mails leave the company
    assert AU.allowed("POST", "/api/quality-inquiries/bulk-draft", "PROCUREMENT")  # drafting is fine
    assert AU.allowed("PUT", "/api/supplier-contacts/SUP-001", "QUALITY")
    assert not AU.allowed("PUT", "/api/supplier-contacts/SUP-001", "VIEWER")
