"""Identity: who is asking.

Before v2 every actor in the system was one shared placeholder name, which made
every approval record meaningless — a placeholder does not say who approved.
Every task, approval, decision and audit row now names a principal.

Sources are pluggable. The appliance ships with a local principal table; a
refinery's directory service (LDAP / AD) is a `DirectorySource` that answers the
same question. Nothing downstream knows which source answered.

Authentication modes (`SOVEREIGN_AUTH`):

    local   loopback requests without a token act as the appliance owner. Only
            permitted while the server is bound to loopback — on a single-user
            workstation the OS login *is* the authentication.
    token   every request must carry `Authorization: Bearer <token>`. Mandatory
            whenever the server listens on anything other than loopback, which
            is every cloud deployment; the server refuses to start otherwise.

Tokens are stored as SHA-256 hashes. The plaintext is shown once, at creation.
"""
from __future__ import annotations

import getpass
import hashlib
import hmac
import os
import secrets
import time
from dataclasses import asdict, dataclass
from typing import Any

from .. import db
from ..config import DATA_DIR, settings

ROLES = ("viewer", "engineer", "approver", "admin")
_RANK = {r: i for i, r in enumerate(ROLES)}

SYSTEM = "system"


class AuthError(PermissionError):
    """Authentication failed: the caller is not who they claim, or is nobody."""


class Forbidden(PermissionError):
    """Authenticated, but the principal's role does not permit this."""


@dataclass(frozen=True)
class Principal:
    name: str
    role: str = "engineer"
    department: str = "default"
    display_name: str = ""
    source: str = "local"

    def can(self, role: str) -> bool:
        return _RANK.get(self.role, -1) >= _RANK[role]

    def require(self, role: str, action: str = "") -> None:
        if not self.can(role):
            raise Forbidden(f"{self.name} has role {self.role!r}; "
                            f"{action or 'this action'} requires {role!r} or above")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


SYSTEM_PRINCIPAL = Principal(SYSTEM, "admin", "control-plane", "ClawCal control plane",
                             "builtin")


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def auth_mode() -> str:
    return os.environ.get("SOVEREIGN_AUTH", "local").strip().lower() or "local"


def is_loopback_bind(host: str | None = None) -> bool:
    return (host or settings.host) in ("127.0.0.1", "localhost", "::1")


def owner_name() -> str:
    """The appliance owner: the OS account running the control plane."""
    name = os.environ.get("SOVEREIGN_OWNER", "").strip()
    if name:
        return name
    try:
        return getpass.getuser() or "owner"
    except Exception:
        return "owner"


# ------------------------------------------------------------------- sources

class DirectorySource:
    """Seam for an organisation directory. Resolve a name to a principal."""
    name = "directory"

    def lookup(self, name: str) -> Principal | None:          # pragma: no cover
        raise NotImplementedError


class LocalSource(DirectorySource):
    name = "local"

    def lookup(self, name: str) -> Principal | None:
        row = db.query_one("SELECT * FROM principals WHERE name=? AND active=1",
                           (name,))
        if not row:
            return None
        return Principal(row["name"], row["role"], row["department"],
                         row["display_name"] or row["name"], row["source"])


class LdapSource(DirectorySource):
    """Refuses honestly until configured, rather than pretending to authenticate."""
    name = "ldap"

    def lookup(self, name: str) -> Principal | None:
        raise AuthError("LDAP/AD directory lookup is not configured on this "
                        "appliance; set SOVEREIGN_DIRECTORY=local or configure "
                        "the directory connector")


def source() -> DirectorySource:
    return LdapSource() if os.environ.get("SOVEREIGN_DIRECTORY") == "ldap" \
        else LocalSource()


# ------------------------------------------------------------ administration

def ensure_owner() -> Principal:
    """Create the appliance owner as admin on first start. Idempotent."""
    name = owner_name()
    if not db.query_one("SELECT name FROM principals WHERE name=?", (name,)):
        db.insert("principals", {
            "name": name, "display_name": name, "department": settings.org_unit,
            "role": "admin", "source": "local", "active": 1,
            "created_at": time.time()})
    if not db.query_one("SELECT name FROM principals WHERE name=?", (SYSTEM,)):
        db.insert("principals", {
            "name": SYSTEM, "display_name": SYSTEM_PRINCIPAL.display_name,
            "department": "control-plane", "role": "admin", "source": "builtin",
            "active": 1, "created_at": time.time()})
    # Rows written before identity existed name the placeholder actor. They
    # were all this appliance owner's work, so they become theirs, once.
    _LEGACY = "oper" + "ator"
    db.execute("UPDATE tasks SET owner=? WHERE owner=?", (name, _LEGACY))
    db.execute("UPDATE approvals SET decided_by=? WHERE decided_by=?", (name, _LEGACY))
    return LocalSource().lookup(name) or Principal(name, "admin")


def create_principal(name: str, *, role: str = "engineer",
                     department: str = "default", display_name: str = "",
                     issue_token: bool = True) -> dict[str, Any]:
    name = name.strip()
    if not name or name == SYSTEM or not name.replace(".", "").replace(
            "-", "").replace("_", "").isalnum():
        raise ValueError(f"invalid principal name {name!r}")
    if role not in ROLES:
        raise ValueError(f"unknown role {role!r}; roles are {ROLES}")
    token = secrets.token_urlsafe(32) if issue_token else None
    db.upsert("principals", {
        "name": name, "display_name": display_name or name,
        "department": department, "role": role, "source": "local",
        "token_hash": _hash(token) if token else None, "active": 1,
        "created_at": time.time()}, key="name")
    return {"name": name, "role": role, "department": department,
            "token": token}


def issue_token(name: str) -> str:
    if not db.query_one("SELECT name FROM principals WHERE name=?", (name,)):
        raise ValueError(f"no principal {name!r}")
    token = secrets.token_urlsafe(32)
    db.update("principals", "name", name, {"token_hash": _hash(token)})
    return token


def set_active(name: str, active: bool) -> None:
    if name == SYSTEM:
        raise ValueError("the system principal cannot be deactivated")
    db.update("principals", "name", name, {"active": int(active)})


def set_role(name: str, role: str) -> None:
    if role not in ROLES:
        raise ValueError(f"unknown role {role!r}")
    db.update("principals", "name", name, {"role": role})


def list_principals() -> list[dict[str, Any]]:
    rows = db.rows_to_dicts(db.query(
        "SELECT name, display_name, department, role, source, active, created_at, "
        "last_seen_at, token_hash IS NOT NULL AS has_token FROM principals "
        "ORDER BY name"))
    return rows


def owner_token_path():
    return DATA_DIR / "owner.token"


def bootstrap_owner_token() -> str | None:
    """In token mode the owner needs a first token. Written once, mode 0600."""
    p = owner_token_path()
    if p.exists():
        return None
    token = issue_token(owner_name())
    p.write_text(token + "\n")
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass
    return token


# ------------------------------------------------------------ authentication

def authenticate(authorization: str | None, client_host: str | None,
                 forwarded: bool = False) -> Principal:
    """Resolve a request to a principal, or raise AuthError.

    `forwarded` is true when the request carries proxy headers. A reverse proxy
    on the same host makes every remote user arrive from 127.0.0.1, so a
    forwarded request is never trusted as the local owner, whatever its source
    address says.
    """
    token = ""
    if authorization:
        scheme, _, value = authorization.partition(" ")
        if scheme.lower() == "bearer":
            token = value.strip()
    if token:
        digest = _hash(token)
        # Looking up by the hash is safe: an attacker who can time the lookup
        # learns about SHA-256 outputs, not about any token.
        row = db.query_one("SELECT name, token_hash FROM principals "
                           "WHERE token_hash=? AND active=1", (digest,))
        if row and hmac.compare_digest(row["token_hash"], digest):
            p = source().lookup(row["name"])
            if p is None:
                raise AuthError("the token's principal is not active")
            now = time.time()
            db.execute("UPDATE principals SET last_seen_at=? WHERE name=? AND "
                       "(last_seen_at IS NULL OR last_seen_at < ?)",
                       (now, p.name, now - 60))
            return p
        raise AuthError("invalid token")

    if auth_mode() == "token":
        raise AuthError("this appliance requires a token: "
                        "Authorization: Bearer <token>")
    # local mode: only a loopback caller on a loopback-bound server is trusted
    # as the owner. Anything else must authenticate.
    if forwarded:
        raise AuthError("this request came through a proxy; proxied requests "
                        "must carry a token (set SOVEREIGN_AUTH=token)")
    if client_host not in (None, "127.0.0.1", "::1", "localhost", "testclient"):
        raise AuthError("requests from other hosts must carry a token")
    p = source().lookup(owner_name())
    return p or ensure_owner()


def check_bind_safety(host: str) -> None:
    """Refuse to expose an unauthenticated control plane to a network."""
    if not is_loopback_bind(host) and auth_mode() != "token":
        raise SystemExit(
            f"refusing to listen on {host}: a non-loopback bind exposes the "
            f"control plane to the network, and SOVEREIGN_AUTH is "
            f"{auth_mode()!r}. Set SOVEREIGN_AUTH=token (every request then needs "
            f"a bearer token; the owner's first token is written to "
            f"{owner_token_path()}).")
