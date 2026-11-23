"""KPI integration tests against the loaded Neon data (skipped when the database is unreachable).
These are the acceptance checks for the KPI definitions in app/sql/views.sql.
"""
import pytest

from app import analytics as A


def _db():
    try:
        A._run("SELECT 1", {}, True)
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _db(), reason="database not reachable")


def q(sql):
    return A._run(sql, {})


def test_as_of_is_data_cutoff_not_today():
    row = q("""SELECT sp.as_of_date() AS a, GREATEST((SELECT MAX(inspection_ts)::date FROM sp.fact_quality_inspection),
                                                    (SELECT MAX(opened_ts)::date FROM sp.fact_ncr_capa)) AS m""")[0]
    meta = q("SELECT value FROM sp.meta WHERE key='as_of_date'")
    assert meta or row["a"] == row["m"]


def test_otif_implies_on_time_and_in_full():
    assert q("SELECT COUNT(*) AS n FROM sp.v_po_delivery WHERE otif AND NOT (on_time AND in_full)")[0]["n"] == 0


def test_measurable_excludes_excluded_and_not_due():
    assert q("SELECT COUNT(*) AS n FROM sp.v_po_delivery WHERE measurable AND (excluded OR delivery_status='NOT_DUE')")[0]["n"] == 0


def test_attribution_weights_sum_to_one_per_inspection():
    bad = q("""SELECT inspection_id FROM sp.v_defect_attribution GROUP BY inspection_id
               HAVING ABS(SUM(attribution_weight) - 1) > 1e-9""")
    assert not bad


def test_ncr_weights_sum_to_one():
    assert not q("SELECT ncr_id FROM sp.v_ncr_supplier GROUP BY ncr_id HAVING ABS(SUM(attribution_weight) - 1) > 1e-9")


def test_disruption_only_on_consumed_categories():
    bad = q("""SELECT d.downtime_id FROM sp.v_supply_disruption d WHERE d.supplier_id IS NOT NULL AND NOT EXISTS (
                 SELECT 1 FROM sp.v_po_delivery p JOIN sp.line_material_category lm ON lm.material_category = p.material_category
                 WHERE p.supplier_id = d.supplier_id AND lm.line_type = d.line_type)""")
    assert not bad


def test_working_days():
    assert A._run("SELECT sp.working_days(DATE '2026-08-01', DATE '2026-08-01')", {}, True) == 0
    assert A._run("SELECT sp.working_days(DATE '2026-08-10', DATE '2026-08-01')", {}, True) < 0


def test_scorecard_fields_and_bands():
    cards = A.scorecard()
    cfg = A.get_scorecard_config()
    for c in cards:
        assert 0 <= c["composite_score"] <= 100
        assert c["health"] in ("HEALTHY", "WATCH", "AT_RISK")
        hb = cfg["health_bands"]
        expect = "AT_RISK" if c["risk_score"] >= hb["risk"] else "WATCH" if c["risk_score"] >= hb["watch"] else "HEALTHY"
        assert c["health"] == expect
        assert c["ppm_method"] in ("INCOMING_INSPECTION", "GENEALOGY_ATTRIBUTED_PROXY")


def test_evidence_matches_scorecard_otif():
    c = A.scorecard()[0]
    ev = A.evidence(c["supplier_id"], "otif", 5000)
    rows = ev["rows"]
    otif = 100 * sum(1 for r in rows if r["otif"]) / len(rows)
    assert abs(otif - c["otif_pct"]) < 0.11
