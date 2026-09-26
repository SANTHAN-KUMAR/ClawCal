"""Tamper evidence for devices: the chained log and its anchor (spec §10.4).

Every tool call, model call, gate result and policy check on a client appends

    hash = SHA-256( prev_hash ‖ 0x1F ‖ canonical(entry) )
    sig  = Ed25519(device key, hash)

On every sync the node checks the uploaded segment against the chain head it
recorded last time (the *anchor*), stores the entries, and advances the anchor.
A segment that forks from the anchor, skips a sequence number, carries a hash
that does not recompute or a signature the device key did not make is a
**tamper event**: the device is quarantined and may not re-attach until an
operator clears it.

A device that never rejoins produces no evidence. That absence is itself
logged: a device silent past its lease is reported, once.

The honest limit (RQ7): on a grade-C device an attacker with the device key can
forge a *consistent* new chain offline. What they cannot do is make it agree
with the anchor the node already holds — so rewriting history that was synced
is caught, and history that was never synced was never evidence.
"""
from __future__ import annotations

import hashlib
import time
from typing import Any

from .. import audit, db, signing
from . import decisions, devices
from .devices import DeviceError
from .identity import Principal

GENESIS = "0" * 64
MAX_SEGMENT = 5000


def entry_hash(prev_hash: str, entry: dict[str, Any]) -> str:
    """The chain step. Shared with the client, which computes the same bytes."""
    core = {"seq": int(entry["seq"]), "ts": round(float(entry["ts"]), 6),
            "kind": str(entry["kind"]), "data": entry.get("data")}
    return hashlib.sha256(prev_hash.encode("ascii") + b"\x1f"
                          + signing.canonical(core)).hexdigest()


def _tamper(device_id: str, kind: str, detail: str, *, blocking: bool = True,
            by: str = "system") -> dict[str, Any]:
    eid = db.new_id("tamper")
    db.insert("tamper_events", {"id": eid, "device_id": device_id, "ts": time.time(),
                                "kind": kind, "detail": detail[:2000],
                                "blocking": int(blocking), "state": "OPEN"})
    if blocking:
        db.update("devices", "id", device_id, {
            "state": "QUARANTINED",
            "state_reason": f"tamper event {eid} ({kind}): {detail[:300]}"})
        db.execute("UPDATE leases SET state='REVOKED' WHERE device_id=? AND "
                   "state='ACTIVE'", (device_id,))
    decisions.record("trust", f"TAMPER_{kind.upper()}", detail[:1500],
                     subject_kind="device", subject_id=device_id, principal=by,
                     basis={"event_id": eid, "blocking": blocking})
    audit.record("trust", "tamper_event", outcome="TAMPER" if blocking else "NOTICE",
                 actor=by, detail={"device_id": device_id, "event_id": eid,
                                   "kind": kind, "detail": detail[:300]})
    audit.bus.publish({"type": "tamper", "device_id": device_id, "kind": kind,
                       "event_id": eid})
    return {"ok": False, "event_id": eid, "kind": kind, "reason": detail}


def sync(device_id: str, envelope: dict[str, Any]) -> dict[str, Any]:
    """Accept a signed log segment. Returns the new anchor, or the tamper event."""
    dev = devices.get(device_id)
    if dev["state"] == "REVOKED":
        raise DeviceError(f"{device_id} is revoked; its log is no longer accepted")
    dev, body = devices._accept_signed(device_id, envelope, "sync")
    entries = body.get("entries") or []
    if not isinstance(entries, list):
        raise DeviceError("entries must be a list")
    if len(entries) > MAX_SEGMENT:
        raise DeviceError(f"a segment may hold at most {MAX_SEGMENT} entries; "
                          f"sync in parts")
    pub = bytes.fromhex(dev["public_key"])
    head, seq = dev["chain_head"] or GENESIS, int(dev["chain_seq"] or 0)
    rebase = bool(dev["rebase_pending"])

    # Entries the node already holds may be re-sent; they must be identical.
    fresh: list[dict[str, Any]] = []
    for e in entries:
        try:
            n = int(e["seq"])
        except (KeyError, TypeError, ValueError):
            return _tamper(device_id, "malformed", "an entry has no integer seq")
        if n <= seq and not rebase:
            held = db.query_one("SELECT hash FROM device_log WHERE device_id=? "
                                "AND seq=?", (device_id, n))
            if held and held["hash"] != e.get("hash"):
                return _tamper(device_id, "fork",
                               f"entry {n} differs from the one anchored earlier "
                               f"(held {held['hash'][:16]}…, sent "
                               f"{str(e.get('hash'))[:16]}…): history was rewritten")
            continue
        fresh.append(e)

    if dev["state"] == "QUARANTINED" and not rebase:
        raise DeviceError(f"{device_id} is quarantined; an operator must clear the "
                          f"tamper event before its log is accepted again")

    if fresh:
        first = fresh[0]
        if rebase:
            head, seq = str(first.get("prev_hash")), int(first["seq"]) - 1
        elif int(first["seq"]) != seq + 1:
            return _tamper(device_id, "gap",
                           f"the segment starts at entry {first['seq']} but the "
                           f"anchor is at {seq}: entries {seq + 1}–"
                           f"{int(first['seq']) - 1} are missing")
        elif first.get("prev_hash") != head:
            return _tamper(device_id, "not_at_anchor",
                           f"entry {first['seq']} does not chain from the anchored "
                           f"head {head[:16]}…: the log was rewritten below the "
                           f"anchor")

    rows = []
    prev, expect = head, seq + 1
    for e in fresh:
        n = int(e["seq"])
        if n != expect:
            return _tamper(device_id, "gap", f"entry {expect} is missing (found {n})")
        if e.get("prev_hash") != prev:
            return _tamper(device_id, "fork", f"entry {n} does not chain from "
                                              f"entry {n - 1}")
        try:
            h = entry_hash(prev, e)
        except (KeyError, TypeError, ValueError) as exc:
            return _tamper(device_id, "malformed", f"entry {n}: {exc}")
        if h != e.get("hash"):
            return _tamper(device_id, "bad_hash",
                           f"entry {n}'s hash does not recompute: the entry was "
                           f"edited after it was written")
        try:
            ok = signing.verify(pub, bytes.fromhex(h), bytes.fromhex(str(e.get("sig"))))
        except ValueError:
            ok = False
        if not ok:
            return _tamper(device_id, "bad_signature",
                           f"entry {n} was not signed by this device's key")
        rows.append((device_id, n, float(e["ts"]), str(e["kind"])[:60],
                     db.jdump(e.get("data")), prev, h, str(e["sig"]), time.time()))
        prev, expect = h, n + 1

    now = time.time()
    with db.tx() as c:
        if rebase:
            c.execute("DELETE FROM device_log WHERE device_id=? AND seq>?",
                      (device_id, seq))
        c.executemany("INSERT INTO device_log (device_id, seq, ts, kind, data, "
                      "prev_hash, hash, sig, received_at) VALUES (?,?,?,?,?,?,?,?,?)",
                      rows)
        c.execute("UPDATE devices SET chain_head=?, chain_seq=?, anchored_at=?, "
                  "rebase_pending=0, last_seen_at=? WHERE id=?",
                  (prev, expect - 1, now, now, device_id))
    skipped = (f"; entries {int(dev['chain_seq'] or 0) + 1}–{seq} were never "
               f"accepted as evidence" if rebase and seq > int(dev["chain_seq"] or 0)
               else "")
    decisions.record("trust", "REBASED" if rebase else "ANCHORED",
                     f"{device_id}: {len(rows)} entr{'y' if len(rows) == 1 else 'ies'} "
                     f"verified; anchor now at {expect - 1} ({prev[:16]}…)"
                     + (" — re-baselined after an operator cleared a tamper event"
                        + skipped if rebase else ""),
                     subject_kind="device", subject_id=device_id,
                     principal=dev["principal"],
                     basis={"from_seq": seq, "to_seq": expect - 1, "head": prev})
    return {"ok": True, "accepted": len(rows), "anchor": {"seq": expect - 1,
                                                          "head": prev,
                                                          "anchored_at": now}}


def anchor(device_id: str) -> dict[str, Any]:
    d = devices.get(device_id)
    return {"seq": int(d["chain_seq"] or 0), "head": d["chain_head"] or GENESIS,
            "anchored_at": d["anchored_at"],
            # An operator cleared a tamper event and chose to re-baseline: the
            # device's next segment, however it starts, becomes the new anchor.
            "rebase_pending": bool(d["rebase_pending"])}


def clear(event_id: str, admin: Principal, reason: str, *,
          rebase: bool = False) -> dict[str, Any]:
    """An operator clears a tamper event. With `rebase`, the device's next
    segment becomes the new baseline — the operator has decided what the
    device's history is, and the decision is recorded with their name."""
    admin.require("admin", "clearing a tamper event")
    if not (reason or "").strip():
        raise DeviceError("clearing a tamper event needs a reason on the record")
    ev = db.query_one("SELECT * FROM tamper_events WHERE id=?", (event_id,))
    if not ev:
        raise DeviceError(f"no tamper event {event_id!r}")
    if ev["state"] != "OPEN":
        raise DeviceError(f"{event_id} is already {ev['state']}")
    db.update("tamper_events", "id", event_id, {
        "state": "CLEARED", "cleared_by": admin.name, "cleared_at": time.time(),
        "clear_reason": reason[:1000]})
    left = db.query_one("SELECT COUNT(*) AS n FROM tamper_events WHERE device_id=? "
                        "AND state='OPEN' AND blocking=1", (ev["device_id"],))["n"]
    dev = devices.get(ev["device_id"])
    changes: dict[str, Any] = {}
    if rebase:
        changes["rebase_pending"] = 1
    if not left and dev["state"] == "QUARANTINED":
        changes.update({"state": "ACTIVE",
                        "state_reason": f"tamper event cleared by {admin.name}: "
                                        f"{reason[:200]}"})
    if changes:
        db.update("devices", "id", ev["device_id"], changes)
    decisions.record("trust", "TAMPER_CLEARED",
                     f"{admin.name} cleared {event_id} ({ev['kind']}): {reason}"
                     + (" — next segment re-baselines the chain" if rebase else ""),
                     subject_kind="device", subject_id=ev["device_id"],
                     principal=admin.name, basis={"event_id": event_id,
                                                  "rebase": rebase})
    return devices.describe(ev["device_id"])


def sweep_silent(now: float | None = None) -> int:
    """Report, once, each device silent past its lease plus the policy margin."""
    from . import trust
    now = now or time.time()
    margin = float(trust.policy().get("silence_report_hours", 72)) * 3600
    n = 0
    for r in db.query(
            "SELECT d.id, d.anchored_at, MAX(l.grace_until) AS grace "
            "FROM devices d JOIN leases l ON l.device_id = d.id "
            "WHERE d.state IN ('ACTIVE','QUARANTINED') GROUP BY d.id"):
        if not r["grace"] or now < r["grace"] + margin:
            continue
        if (r["anchored_at"] or 0) > r["grace"]:
            continue
        if db.query_one("SELECT 1 FROM tamper_events WHERE device_id=? AND "
                        "kind='silent' AND ts > ?", (r["id"], r["grace"])):
            continue
        _tamper(r["id"], "silent",
                f"no log segment since the lease ran out "
                f"{(now - r['grace']) / 3600:.0f} h ago; whatever this device did "
                f"since its last anchor is not evidence", blocking=False)
        n += 1
    return n


def log_for(device_id: str, limit: int = 200) -> list[dict[str, Any]]:
    rows = db.rows_to_dicts(db.query(
        "SELECT seq, ts, kind, data, hash, received_at FROM device_log "
        "WHERE device_id=? ORDER BY seq DESC LIMIT ?", (device_id, limit)))
    for r in rows:
        r["data"] = db.jload(r["data"], None)
    return rows
