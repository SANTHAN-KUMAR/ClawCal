"""The trust domain: its policy, and the grade of everyone who asks.

sovereign-workbench-v2.md §1: *compute and data never cross a boundary the
organisation cannot revoke.* A trust domain is one node (this appliance), zero
or more member devices, and one policy. The policy has two axes — the
requester's **grade** (§11) and a document's **data class** — and says which
grades may attach, which data classes each may retrieve, and whether a grade
may run detached.

The design never asks a laptop to be trustworthy. A grade is computed here, on
the node, from facts the device cannot set about itself:

    A   node console, or a managed device whose TPM attestation a configured
        verifier accepted
    B   a managed device attested by the organisation's MDM
    C   anything else with an active, self-reported egress policy
    D   a device reporting its egress policy inactive, or an unmanaged device
        running detached

Enforcement is on the node: retrieval is filtered by the data classes the
requester's grade is cleared for, and a lease is refused where the policy says
so. Attestation only sets the grade; the policy decides what each grade gets.

Stopping rule 3 (§18) is the shipped default: grade C may attach, may retrieve
`internal` data classes only, and may not run detached.
"""
from __future__ import annotations

import copy
import os
import time
from typing import Any

from .. import db
from ..config import settings
from . import decisions
from .identity import Principal

GRADES = ("A", "B", "C", "D")
DATA_CLASSES = ("public", "internal", "confidential", "restricted")
MODES = ("attached", "detached")

# The grade of a request that arrives at the node's own console (loopback on a
# loopback-bound node). The node *is* the grade-A enforcement point.
CONSOLE_GRADE = "A"

DEFAULT_POLICY: dict[str, Any] = {
    "grades": {
        "A": {"attach": True, "detached": True, "execute_local": True,
              "retrieve": ["public", "internal", "confidential", "restricted"]},
        "B": {"attach": True, "detached": True, "execute_local": True,
              "retrieve": ["public", "internal", "confidential"]},
        "C": {"attach": True, "detached": False, "execute_local": False,
              "retrieve": ["public", "internal"]},
        "D": {"attach": True, "detached": False, "execute_local": False,
              "retrieve": ["public"]},
    },
    # A remote principal using the web workbench with a token but no enrolled
    # device. The browser is a thin client with no egress claim of its own.
    "unenrolled_grade": "C",
    # New devices: "auto" activates an enrolment at once (it is graded, and the
    # grade decides what it gets); "admin" holds it PENDING for an admin.
    "enrolment": "auto",
    "lease_ttl_hours": {"attached": 24, "detached": 336},
    "grace_hours": 24,
    # A self-check older than this no longer counts.
    "egress_report_max_age_hours": 48,
    # RQ2: retrieve on the node, draft on the client. A research question, so
    # off until an organisation turns it on.
    "split_placement": False,
    # Hardware classes the profiler may issue a detached manifest for (rule 2).
    "shipped_classes": ["FIT-FAST", "SPLIT-PCIE", "UNIFIED"],
    # A device silent for longer than this past its lease is reported.
    "silence_report_hours": 72,
    # Grades that may carry an exported instrument slice off-site (§14).
    "slice_export_grades": ["A", "B"],
    # Attached plans with no contact for this long are closed as abandoned.
    "plan_idle_hours": 8,
    # Active devices one principal may hold at once.
    "max_devices_per_principal": 5,
}

_KEY = "trust.policy"


def domain_name() -> str:
    return os.environ.get("SOVEREIGN_DOMAIN", "").strip() or (
        settings.org_name.lower().replace(" ", "-")[:48] or "workbench")


# ------------------------------------------------------------------ policy

def policy() -> dict[str, Any]:
    """The live policy: the defaults with any admin overrides applied."""
    pol = copy.deepcopy(DEFAULT_POLICY)
    row = db.query_one("SELECT value FROM control_settings WHERE key=?", (_KEY,))
    stored = db.jload(row["value"], {}) if row else {}
    _merge(pol, stored or {})
    return pol


def _merge(base: dict[str, Any], over: dict[str, Any]) -> None:
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _merge(base[k], v)
        else:
            base[k] = v


def validate(pol: dict[str, Any]) -> None:
    for g in GRADES:
        rules = pol["grades"].get(g)
        if not isinstance(rules, dict):
            raise ValueError(f"the policy has no rules for grade {g}")
        bad = [c for c in rules.get("retrieve", []) if c not in DATA_CLASSES]
        if bad:
            raise ValueError(f"grade {g}: unknown data class(es) {bad}; "
                             f"classes are {DATA_CLASSES}")
    # A lower grade must never be cleared for more than a higher one: that
    # would make lowering a device's trust a way to gain access.
    for hi, lo in zip(GRADES, GRADES[1:]):
        extra = set(pol["grades"][lo]["retrieve"]) - set(pol["grades"][hi]["retrieve"])
        if extra:
            raise ValueError(f"grade {lo} would be cleared for {sorted(extra)}, "
                             f"which grade {hi} is not; clearance must not "
                             f"increase as trust decreases")
    if pol.get("unenrolled_grade") not in GRADES:
        raise ValueError("unenrolled_grade must be one of A, B, C, D")
    if pol.get("enrolment") not in ("auto", "admin"):
        raise ValueError("enrolment must be 'auto' or 'admin'")
    for m in MODES:
        ttl = float(pol["lease_ttl_hours"].get(m, 0))
        if not 0.05 <= ttl <= 24 * 90:
            raise ValueError(f"lease_ttl_hours.{m} must be between 3 minutes "
                             f"and 90 days")


def set_policy(changes: dict[str, Any], admin: Principal) -> dict[str, Any]:
    admin.require("admin", "changing the trust-domain policy")
    before = policy()
    after = copy.deepcopy(before)
    _merge(after, changes)
    validate(after)
    row = db.query_one("SELECT value FROM control_settings WHERE key=?", (_KEY,))
    stored = db.jload(row["value"], {}) if row else {}
    _merge(stored, changes)
    db.upsert("control_settings", {"key": _KEY, "value": db.jdump(stored),
                                   "changed_by": admin.name,
                                   "changed_at": time.time()}, key="key")
    decisions.record("trust", "POLICY_CHANGED",
                     f"{admin.name} changed the trust-domain policy",
                     subject_kind="policy", subject_id=domain_name(),
                     principal=admin.name, basis={"changes": changes})
    return after


def rules_for(grade: str) -> dict[str, Any]:
    return policy()["grades"].get(grade) or policy()["grades"]["D"]


def clearance(grade: str) -> list[str]:
    """The data classes a requester of this grade may retrieve."""
    return list(rules_for(grade).get("retrieve", []))


# ------------------------------------------------------------------ grades

def compute_grade(device: dict[str, Any], *, mode: str | None = None,
                  now: float | None = None) -> tuple[str, str]:
    """(grade, reason) for a device, from facts the device cannot assert.

    `managed` is set by an admin. An attestation counts only if a verifier on
    the node marked it verified. The egress self-check is self-reported, which
    is why on its own it can never lift a device above C.
    """
    now = now or time.time()
    mode = mode or device.get("mode") or "attached"
    platform = (device.get("platform") or "").lower()
    managed = bool(device.get("managed"))
    att = db.jload(device.get("attestation"), {}) or {}
    att_ok = bool(att.get("verified"))
    kind = att.get("kind", "self-report")
    egress = db.jload(device.get("egress_report"), {}) or {}
    max_age = float(policy().get("egress_report_max_age_hours", 48)) * 3600

    # The ceiling set by what has been verified about the hardware.
    if managed and att_ok and kind == "tpm-quote" and platform in ("linux", "windows"):
        ceiling, why = "A", (f"managed {platform} device; TPM quote verified by "
                             f"{att.get('verifier', 'the attestation verifier')}")
    elif managed and att_ok and kind == "mdm":
        ceiling, why = "B", (f"managed {platform} device; MDM attestation recorded "
                             f"by {att.get('verifier', 'an admin')}")
    elif managed:
        ceiling, why = "C", ("marked managed, but no attestation has been "
                             "verified, so the claim cannot be checked")
    else:
        ceiling, why = "C", "unmanaged device; its claims are self-reported"

    # The egress policy is what stops a prompt-injected agent sending corpus
    # text anywhere. Without a recent passing self-check, grade D.
    age = now - float(egress.get("checked_at") or 0)
    if not egress:
        return "D", why + "; no egress self-check has been reported"
    if not egress.get("active"):
        return "D", (why + "; the device reports its egress policy is NOT active: "
                     + str(egress.get("reason") or "no reason given")[:200])
    if age > max_age:
        return "D", why + (f"; the last egress self-check is {age / 3600:.0f} h "
                           f"old, older than the {max_age / 3600:.0f} h the policy "
                           f"accepts")
    if mode == "detached" and ceiling == "C":
        return "D", why + "; running detached, so evidence arrives only on rejoin"
    return ceiling, why + "; egress self-check passing"


def request_grade(device: dict[str, Any] | None, console: bool) -> tuple[str, str]:
    """The grade of one request: its device's, the console's, or the default."""
    if device:
        return device.get("grade") or "D", f"device {device['id']}"
    if console:
        return CONSOLE_GRADE, "the node's own console"
    g = policy().get("unenrolled_grade", "C")
    return g, "a remote principal without an enrolled device"


def grade_for_task(task_id: str | None) -> str:
    """A task's grade, read live: a device that drops a grade loses clearance
    at its next tool call, not at its next task."""
    if not task_id:
        return CONSOLE_GRADE
    row = db.query_one(
        "SELECT t.grade, t.device_id, d.grade AS dgrade, d.state AS dstate "
        "FROM tasks t LEFT JOIN devices d ON d.id = t.device_id WHERE t.id=?",
        (task_id,))
    if not row:
        # Unit tests and ad-hoc tool calls run without a task row.
        return CONSOLE_GRADE
    if row["device_id"]:
        if row["dstate"] != "ACTIVE":
            return "D"
        return row["dgrade"] or "D"
    return row["grade"] or CONSOLE_GRADE


def clearance_for_task(task_id: str | None) -> list[str]:
    return clearance(grade_for_task(task_id))


def document_cleared(doc_id: str, task_id: str | None) -> tuple[bool, str]:
    """May this task see this document? (allowed, reason if not)"""
    row = db.query_one("SELECT title, data_class FROM documents WHERE id=?",
                       (doc_id,))
    if not row:
        return True, ""
    grade = grade_for_task(task_id)
    cls = row["data_class"] or "internal"
    if cls in clearance(grade):
        return True, ""
    return False, (f"{row['title']} is classified {cls!r}; this session runs at "
                   f"grade {grade}, which the trust-domain policy clears for "
                   f"{', '.join(clearance(grade)) or 'nothing'} only")


def set_data_class(doc_id: str, data_class: str, admin: Principal) -> None:
    admin.require("approver", "classifying a document")
    if data_class not in DATA_CLASSES:
        raise ValueError(f"unknown data class {data_class!r}; classes are "
                         f"{DATA_CLASSES}")
    row = db.query_one("SELECT data_class FROM documents WHERE id=?", (doc_id,))
    if not row:
        raise ValueError(f"no document {doc_id!r}")
    db.update("documents", "id", doc_id, {"data_class": data_class})
    decisions.record("trust", "CLASSIFIED",
                     f"{admin.name} classified {doc_id} as {data_class} "
                     f"(was {row['data_class']})",
                     subject_kind="document", subject_id=doc_id,
                     principal=admin.name,
                     basis={"from": row["data_class"], "to": data_class})


def health() -> dict[str, Any]:
    """Is the trust domain ready to serve devices? Each item says what is wrong
    and what to do, so the Security page and `trustctl health` read the same."""
    from ..bundles import repo
    from . import attestation
    items: list[dict[str, Any]] = []

    def item(name: str, ok: bool | None, detail: str) -> None:
        items.append({"check": name, "ok": ok, "detail": detail})

    now = time.time()
    ts = (repo.load("timestamp.json") or {}).get("signed", {})
    item("bundle store", bool(ts),
         f"timestamp v{ts.get('version')} valid for "
         f"{(float(ts.get('expires', 0)) - now) / 3600:.1f} h more (re-signed as it "
         f"ages)" if ts else "not initialised: publish the client package")
    root_key = repo.KEY_DIR / "root.ed25519"
    item("offline root key", not root_key.exists(),
         "not on the node — as it should be" if not root_key.exists() else
         f"{root_key} is still on the node; move it to offline storage (it is "
         f"needed only to rotate the root)")
    targets = repo.targets() if ts else {}
    item("client package", any(t.startswith("client/") for t in targets),
         "published" if any(t.startswith("client/") for t in targets) else
         "not published: trustctl publish-client")
    item("harness build", any(t.startswith("harness/") for t in targets),
         "the organisation's opencode build is published"
         if any(t.startswith("harness/") for t in targets) else
         "no harness build registered: devices would use whatever opencode is "
         "installed (bundle/opencode/build.sh, then trustctl add-harness)")
    ok, why = attestation.available()
    item("TPM attestation", ok, why)
    tamper = db.query_one("SELECT COUNT(*) AS n FROM tamper_events t JOIN devices d "
                          "ON d.id = t.device_id WHERE t.state='OPEN' AND t.blocking=1 "
                          "AND d.state != 'REVOKED'")["n"]
    item("tamper events", tamper == 0,
         "none open" if not tamper else f"{tamper} open: review under Security")
    stale = db.query_one("SELECT COUNT(*) AS n FROM devices d WHERE state='ACTIVE' "
                         "AND NOT EXISTS (SELECT 1 FROM leases l WHERE l.device_id=d.id "
                         "AND l.state='ACTIVE' AND l.grace_until > ?)", (now,))["n"]
    item("device leases", stale == 0 if stale is not None else None,
         "every active device holds a live lease" if not stale else
         f"{stale} active device(s) past their lease grace: they have sealed; "
         f"they must reach the node to renew")
    return {"ok": all(i["ok"] is not False for i in items), "items": items,
            "domain": domain_name(), "checked_at": now}
