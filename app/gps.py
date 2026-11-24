"""GPS tracking for trucks and vessels (T3).

Vehicles (trucks, vessels) are registered with a device key (shown once, stored as a SHA-256 hash). A vehicle runs TRIPS;
a trip carries PO-line shipments. Any GPS source - the driver phone page (/driver/), a telematics box, a fleet-provider
webhook, an AIS integrator or the labelled simulator - posts pings to POST /api/gps/pings with header X-Device-Key.
The twin then shows those shipments as LIVE at their true GPS position.

Data protection: positions are accepted only while a trip is active; pings outside a trip are rejected. Device keys
authorise exactly one vehicle. SIMULATOR devices are always flagged as demo data on the map.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import secrets

from sqlalchemy import text

from .db import engine

DEVICE_TYPES = ("PHONE", "TELEMATICS", "AIS", "SIMULATOR", "MANUAL")   # MANUAL = typed in by the supplier (portal form)
KINDS = ("TRUCK", "VESSEL")
MAX_BATCH = 500


def _h(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def create_vehicle(kind: str, registration: str, carrier: str, device_type: str, actor: str) -> dict:
    kind, device_type = kind.upper(), device_type.upper()
    if kind not in KINDS or device_type not in DEVICE_TYPES:
        raise ValueError(f"kind must be {KINDS}, device_type {DEVICE_TYPES}")
    key = secrets.token_urlsafe(24)
    with engine.begin() as c:
        n = c.execute(text("SELECT COALESCE(MAX(CAST(substring(vehicle_id FROM 5) AS INTEGER)), 0) FROM public.vehicles WHERE vehicle_id ~ '^VEH-[0-9]+$'")).scalar() + 1
        vid = f"VEH-{n:04d}"
        c.execute(text("""INSERT INTO public.vehicles (vehicle_id, kind, registration, carrier, device_type, device_key_hash, created_by)
                          VALUES (:v, :k, :r, :c, :d, :h, :a)"""),
                  {"v": vid, "k": kind, "r": registration.strip(), "c": carrier.strip(), "d": device_type, "h": _h(key), "a": actor})
    return {"vehicle_id": vid, "device_key": key, "note": "Store the device key now - it is not shown again."}


def rotate_key(vehicle_id: str) -> str:
    key = secrets.token_urlsafe(24)
    with engine.begin() as c:
        if not c.execute(text("UPDATE public.vehicles SET device_key_hash = :h WHERE vehicle_id = :v"), {"h": _h(key), "v": vehicle_id}).rowcount:
            raise LookupError("vehicle not found")
    return key


def vehicle_for_key(key: str | None) -> dict | None:
    if not key:
        return None
    with engine.connect() as c:
        r = c.execute(text("SELECT * FROM public.vehicles WHERE device_key_hash = :h AND active"), {"h": _h(key)}).mappings().fetchone()
    return dict(r) if r else None


def start_trip(vehicle_id: str, shipment_ids: list[str], actor: str) -> dict:
    """Assign PO-line shipments (ids 'PO-xxx-line') to a new active trip; any previous active trip is closed."""
    if not shipment_ids:
        raise ValueError("at least one shipment")
    with engine.begin() as c:
        if not c.execute(text("SELECT 1 FROM public.vehicles WHERE vehicle_id = :v AND active"), {"v": vehicle_id}).fetchone():
            raise LookupError("vehicle not found")
        for sid in shipment_ids:
            po, line = sid.rsplit("-", 1)
            if not c.execute(text("SELECT 1 FROM sp.fact_purchase_order_receipt WHERE po_id = :p AND po_line = :l"), {"p": po, "l": int(line)}).fetchone():
                raise ValueError(f"unknown shipment {sid}")
        c.execute(text("UPDATE public.vehicle_trips SET ended_at = now() WHERE vehicle_id = :v AND ended_at IS NULL"), {"v": vehicle_id})
        tid = c.execute(text("""INSERT INTO public.vehicle_trips (vehicle_id, shipment_ids, started_by) VALUES (:v, CAST(:s AS jsonb), :a)
                                RETURNING trip_id"""), {"v": vehicle_id, "s": __import__("json").dumps(shipment_ids), "a": actor}).scalar()
    return {"trip_id": tid, "vehicle_id": vehicle_id, "shipments": shipment_ids}


def end_trip(vehicle_id: str) -> int:
    with engine.begin() as c:
        return c.execute(text("UPDATE public.vehicle_trips SET ended_at = now() WHERE vehicle_id = :v AND ended_at IS NULL"),
                         {"v": vehicle_id}).rowcount


def active_trip(vehicle_id: str) -> dict | None:
    with engine.connect() as c:
        r = c.execute(text("""SELECT * FROM public.vehicle_trips WHERE vehicle_id = :v AND ended_at IS NULL
                              ORDER BY trip_id DESC LIMIT 1"""), {"v": vehicle_id}).mappings().fetchone()
    return dict(r) if r else None


def ingest(vehicle: dict, pings: list[dict], reported_by: str | None = None) -> dict:
    """Validate and store pings for the vehicle's ACTIVE trip. Returns accepted / rejected counts with reasons."""
    trip = active_trip(vehicle["vehicle_id"])
    if not trip:
        return {"accepted": 0, "rejected": len(pings), "reasons": ["no active trip: tracking is only accepted during a trip"]}
    now = dt.datetime.now(dt.timezone.utc)
    ok, bad, reasons = [], 0, []
    for p in pings[:MAX_BATCH]:
        try:
            lat, lon = float(p["lat"]), float(p["lon"])
            ts = dt.datetime.fromisoformat(str(p.get("ts") or now.isoformat()).replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=dt.timezone.utc)
            if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                raise ValueError("lat/lon out of range")
            if ts > now + dt.timedelta(minutes=5) or ts < trip["started_at"] - dt.timedelta(minutes=10):
                raise ValueError("timestamp outside the trip")
            spd = p.get("speed_kmh")
            if spd is not None and not (0 <= float(spd) <= 80 if vehicle["kind"] == "VESSEL" else 0 <= float(spd) <= 160):
                raise ValueError("implausible speed")
            ok.append({"v": vehicle["vehicle_id"], "t": trip["trip_id"], "ts": ts, "la": lat, "lo": lon, "sp": spd,
                       "hd": p.get("heading"), "ac": p.get("accuracy_m"), "src": vehicle["device_type"],
                       "no": (str(p["note"])[:200] if p.get("note") else None), "rb": reported_by})
        except Exception as e:  # noqa: BLE001
            bad += 1
            if len(reasons) < 5:
                reasons.append(str(e)[:80])
    bad += max(0, len(pings) - MAX_BATCH)
    if ok:
        with engine.begin() as c:
            c.execute(text("""INSERT INTO public.gps_pings (vehicle_id, trip_id, ts, lat, lon, speed_kmh, heading, accuracy_m, source, note, reported_by)
                              VALUES (:v, :t, :ts, :la, :lo, :sp, :hd, :ac, :src, :no, :rb)"""), ok)
            c.execute(text("UPDATE public.vehicles SET last_seen = now() WHERE vehicle_id = :v"), {"v": vehicle["vehicle_id"]})
    return {"accepted": len(ok), "rejected": bad, "reasons": reasons, "trip_id": trip["trip_id"]}


def list_vehicles() -> list[dict]:
    with engine.connect() as c:
        rows = c.execute(text("""
          SELECT v.vehicle_id, v.kind, v.registration, v.carrier, v.device_type, v.active, v.last_seen, v.created_at,
                 t.trip_id, t.shipment_ids, t.started_at,
                 (SELECT COUNT(*) FROM public.gps_pings g WHERE g.trip_id = t.trip_id) AS pings,
                 (SELECT MAX(ts) FROM public.gps_pings g WHERE g.trip_id = t.trip_id) AS last_ping
          FROM public.vehicles v
          LEFT JOIN LATERAL (SELECT * FROM public.vehicle_trips x WHERE x.vehicle_id = v.vehicle_id AND x.ended_at IS NULL
                             ORDER BY trip_id DESC LIMIT 1) t ON TRUE
          ORDER BY v.vehicle_id""")).mappings().all()
    return [dict(r) for r in rows]


def shipment_gps(shipment_ids: set | None = None) -> dict:
    """{shipment_id: {vehicle, device_type, pings: [(ts, lat, lon, speed)]}} for every trip (active or ended)."""
    with engine.connect() as c:
        rows = c.execute(text("""
          SELECT t.trip_id, t.shipment_ids, t.started_at, t.ended_at, v.vehicle_id, v.registration, v.kind, v.device_type, v.carrier
          FROM public.vehicle_trips t JOIN public.vehicles v USING (vehicle_id)""")).mappings().all()
        out = {}
        for r in rows:
            pings = c.execute(text("SELECT ts, lat, lon, speed_kmh FROM public.gps_pings WHERE trip_id = :t ORDER BY ts"),
                              {"t": r["trip_id"]}).fetchall()
            if not pings:
                continue
            for sid in r["shipment_ids"] or []:
                if shipment_ids is None or sid in shipment_ids:
                    out[sid] = {"vehicle_id": r["vehicle_id"], "registration": r["registration"], "kind": r["kind"], "carrier": r["carrier"],
                                "device_type": r["device_type"], "trip_id": r["trip_id"], "active": r["ended_at"] is None,
                                "pings": [(p[0], p[1], p[2], p[3]) for p in pings]}
    return out


# ---------------------------------------------------------------- supplier portal: manual fleet + GPS reporting
# A supplier user registers its own trucks / vessels, dispatches its open PO lines on them and types in positions
# (or uses the browser's location). Rows go to the same vehicles / vehicle_trips / gps_pings tables with device_type
# MANUAL, so the network map shows them as LIVE positions labelled "reported by supplier". Every query is scoped to the
# supplier: a supplier can never see or touch another supplier's vehicles or PO lines.
def _own_vehicle(c, supplier_id: str, vehicle_id: str) -> dict:
    r = c.execute(text("SELECT * FROM public.vehicles WHERE vehicle_id = :v AND supplier_id = :s AND active"),
                  {"v": vehicle_id, "s": supplier_id}).mappings().fetchone()
    if not r:
        raise LookupError("vehicle not found")
    return dict(r)


def supplier_fleet(supplier_id: str) -> list[dict]:
    with engine.connect() as c:
        rows = c.execute(text("""
          SELECT v.vehicle_id, v.kind, v.registration, v.carrier, v.driver_name, v.driver_phone, v.device_type, v.last_seen,
                 t.trip_id, t.shipment_ids, t.started_at,
                 (SELECT COUNT(*) FROM public.gps_pings g WHERE g.trip_id = t.trip_id) AS pings,
                 (SELECT json_build_object('ts', g.ts, 'lat', g.lat, 'lon', g.lon, 'speed_kmh', g.speed_kmh, 'note', g.note)
                    FROM public.gps_pings g WHERE g.trip_id = t.trip_id ORDER BY g.ts DESC LIMIT 1) AS last_position
          FROM public.vehicles v
          LEFT JOIN LATERAL (SELECT * FROM public.vehicle_trips x WHERE x.vehicle_id = v.vehicle_id AND x.ended_at IS NULL
                             ORDER BY trip_id DESC LIMIT 1) t ON TRUE
          WHERE v.supplier_id = :s AND v.active ORDER BY v.vehicle_id"""), {"s": supplier_id}).mappings().all()
    return [dict(r) for r in rows]


def supplier_add_vehicle(supplier_id: str, kind: str, registration: str, carrier: str, driver_name: str, driver_phone: str,
                         actor: str) -> str:
    reg = registration.strip().upper()
    with engine.begin() as c:
        old = c.execute(text("SELECT vehicle_id FROM public.vehicles WHERE supplier_id = :s AND upper(registration) = :r AND active"),
                        {"s": supplier_id, "r": reg}).scalar()
        if old:                                            # same registration again: update the details, keep the id
            c.execute(text("UPDATE public.vehicles SET kind = :k, carrier = :c, driver_name = :dn, driver_phone = :dp WHERE vehicle_id = :v"),
                      {"k": kind, "c": carrier.strip(), "dn": driver_name.strip(), "dp": driver_phone.strip(), "v": old})
            return old
    vid = create_vehicle(kind, reg, carrier, "MANUAL", actor)["vehicle_id"]   # the device key is never shown: no device posts for MANUAL
    with engine.begin() as c:
        c.execute(text("UPDATE public.vehicles SET supplier_id = :s, driver_name = :dn, driver_phone = :dp WHERE vehicle_id = :v"),
                  {"s": supplier_id, "dn": driver_name.strip(), "dp": driver_phone.strip(), "v": vid})
    return vid


def supplier_dispatch(supplier_id: str, vehicle_id: str, shipment_ids: list[str], departed_at: dt.datetime | None, actor: str) -> dict:
    """Start a trip for the supplier's own open PO lines. departed_at lets the supplier report a dispatch that already happened."""
    with engine.begin() as c:
        _own_vehicle(c, supplier_id, vehicle_id)
        for sid in shipment_ids:
            po, line = sid.rsplit("-", 1)
            ok = c.execute(text("""SELECT 1 FROM sp.v_po_delivery WHERE po_id = :p AND po_line = :l AND supplier_id = :s
                                   AND delivery_status <> 'RECEIVED'"""), {"p": po, "l": int(line), "s": supplier_id}).fetchone()
            if not ok:
                raise ValueError(f"{sid} is not one of your open PO lines")
        c.execute(text("UPDATE public.vehicle_trips SET ended_at = now() WHERE vehicle_id = :v AND ended_at IS NULL"), {"v": vehicle_id})
        now = dt.datetime.now(dt.timezone.utc)
        start = min(departed_at, now) if departed_at else now
        tid = c.execute(text("""INSERT INTO public.vehicle_trips (vehicle_id, shipment_ids, started_at, started_by)
                                VALUES (:v, CAST(:s AS jsonb), :t, :a) RETURNING trip_id"""),
                        {"v": vehicle_id, "s": __import__("json").dumps(shipment_ids), "t": start, "a": actor}).scalar()
    return {"trip_id": tid, "vehicle_id": vehicle_id, "shipments": shipment_ids, "started_at": start}


def supplier_report_positions(supplier_id: str, vehicle_id: str, positions: list[dict], actor: str) -> dict:
    with engine.connect() as c:
        v = _own_vehicle(c, supplier_id, vehicle_id)
    return ingest(v, positions, reported_by=actor)


def supplier_arrived(supplier_id: str, vehicle_id: str) -> int:
    with engine.begin() as c:
        _own_vehicle(c, supplier_id, vehicle_id)
        return c.execute(text("UPDATE public.vehicle_trips SET ended_at = now() WHERE vehicle_id = :v AND ended_at IS NULL"),
                         {"v": vehicle_id}).rowcount


def vehicle_track(vehicle_id: str, limit: int = 300) -> dict:
    """GPS page detail: the vehicle, its current (or last) trip, the PO lines it carries and its latest positions."""
    with engine.connect() as c:
        v = c.execute(text("""SELECT vehicle_id, kind, registration, carrier, device_type, active, last_seen, created_at
                              FROM public.vehicles WHERE vehicle_id = :v"""), {"v": vehicle_id}).mappings().fetchone()
        if not v:
            raise LookupError(f"Unknown vehicle {vehicle_id}")
        trip = c.execute(text("""SELECT trip_id, shipment_ids, started_at, ended_at FROM public.vehicle_trips
                                 WHERE vehicle_id = :v ORDER BY (ended_at IS NULL) DESC, trip_id DESC LIMIT 1"""), {"v": vehicle_id}).mappings().fetchone()
        pings, lines = [], []
        if trip:
            pings = [dict(r) for r in c.execute(text("""SELECT ts, lat, lon, speed_kmh, accuracy_m, source FROM public.gps_pings
                                                        WHERE trip_id = :t ORDER BY ts DESC LIMIT :n"""), {"t": trip["trip_id"], "n": limit}).mappings()][::-1]
            ids = list(trip["shipment_ids"] or [])
            if ids:
                lines = [dict(r) for r in c.execute(text("""SELECT po_id, po_line, supplier_id, supplier_name, material_name, plant_id, promised_date, ordered_qty
                                                            FROM sp.v_po_delivery WHERE po_id || '-' || po_line = ANY(:k)"""), {"k": ids}).mappings()]
    return {"vehicle": dict(v), "trip": dict(trip) if trip else None, "pings": pings, "lines": lines}
