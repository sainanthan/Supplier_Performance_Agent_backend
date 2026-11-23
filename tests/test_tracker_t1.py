"""Network Tracker T1 (clean-up) tests against the loaded data (skipped without a database)."""
import pytest

from app import tracker as TR


def _db():
    try:
        from app import analytics as A
        A._run("SELECT 1", {}, True)
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _db(), reason="database not reachable")


@pytest.fixture(scope="module")
def net():
    from app.db import init_db
    init_db()
    return TR._network(True)


def test_v1_payload_unchanged():
    n = TR._network(False)
    assert n["version"] == 1 and "movements" not in n
    assert all("sea_end" not in l for l in n["lanes"])


def test_movements_cover_every_shipment_once(net):
    mv = TR.movements(net)
    ids = [i for m in mv for i in m["shipment_ids"]]
    assert len(ids) == len(set(ids)) == len(net["shipments"])
    assert sum(m["count"] for m in mv) == len(net["shipments"])


def test_movement_grouping_rules(net):
    ships = {s["id"]: s for s in net["shipments"]}
    for m in TR.movements(net):
        mem = [ships[i] for i in m["shipment_ids"]]
        assert {x["lane"] for x in mem} == {m["lane"]}
        if m["mode"] == "ROAD":
            assert len({round(x["t_eta"]) for x in mem}) == 1          # one truck run per arrival day
        else:
            assert len({int(x["t_eta"] // 7) for x in mem}) == 1       # one vessel per arrival week
        assert m["t_depart"] == min(x["t_depart"] for x in mem) and m["t_eta"] == max(x["t_eta"] for x in mem)
        assert m["position"] == "ESTIMATED"


def test_sea_lanes_use_own_entry_port_and_shared_corridor(net):
    sea = [l for l in net["lanes"] if l["mode"] == "SEA"]
    assert sea
    for l in sea:
        lat, lon = TR.INDIA_PORTS[l["entry_port"]][1:]
        assert l["path"][l["sea_end"]] == [lat, lon]                    # sea ends at the lane's Indian entry port
        assert l["path"][l["shared_from"]] == list(TR.MALACCA_EXIT[0])  # shared corridor starts at the Malacca exit
        assert l["sea_start"] < l["shared_from"] < l["sea_end"] < len(l["path"]) - 1
        assert l["corridor"] == f"MALACCA>{l['entry_port']}"
    assert len({l["entry_port"] for l in sea}) > 1                      # no longer every lane into one port


def test_only_used_ports_are_drawn(net):
    used = {l.get("origin_port") for l in net["lanes"]} | {l.get("entry_port") for l in net["lanes"]}
    assert {n["id"] for n in net["nodes"] if n["kind"] == "port"} == used - {None}


def test_sites_are_editable_and_flagged_assumed():
    sites = TR._sites()
    sup = [s for s in sites.values() if s["kind"] == "supplier"]
    assert len(sup) == len(TR.SUPPLIER_SITES) and all(s["assumed"] for s in sup)


def test_scenario_marks_movements_disrupted():
    from app.features import is_on
    net = TR.tracker("SUP-001", delay_days=7)
    if not is_on("tracker_cleanup"):
        pytest.skip("tracker_cleanup flag is off")
    hit = [m for m in net["movements"] if m["supplier_id"] == "SUP-001" and m["disrupted"]]
    assert hit and all(m["supplier_id"] == "SUP-001" for m in net["movements"] if m["disrupted"])
