from datetime import date

from app import analytics as A
from app import auth as AU


def test_material_spend_trends_returns_monthly_material_series(monkeypatch):
    captured = {}
    rows = [{"period": date(2026, 1, 1), "material_id": "MAT-1", "material_name": "Widget", "spend_inr": 1250000}]

    def fake_query(sql, **params):
        captured["sql"] = sql
        captured["params"] = params
        return rows

    monkeypatch.setattr(A, "query", fake_query)

    result = A.material_spend_trends("PLANT-1", "SUP-001,SUP-002", months=60)

    assert result == rows
    assert "date_trunc('month', p.receipt_date)" in captured["sql"]
    assert "delivery_status = 'RECEIVED' AND NOT excluded" in captured["sql"]
    assert captured["params"] == {"plant_id": "PLANT-1", "supplier_ids": ["SUP-001", "SUP-002"], "months": 36}


def test_material_spend_trends_respects_concentration_access_policy():
    path = "/api/concentration/trends"

    for role in ("ADMIN", "PROCUREMENT", "QUALITY"):
        assert AU.allowed("GET", path, role)
    for role in ("VIEWER", "SUPPLIER"):
        assert not AU.allowed("GET", path, role)
