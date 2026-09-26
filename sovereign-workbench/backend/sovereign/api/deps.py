"""Request identity, shared by every router: who is asking, from which device.

`principal` resolves the human (token, or the loopback owner on a loopback
node). `caller` adds the device: a request that carries the device and lease
headers is authenticated as that device, and one that claims a device but
cannot prove it is refused — never silently served as something else.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from fastapi import Depends, Request

from .. import control
from ..config import settings
from ..control.identity import Principal

LOOPBACK = ("127.0.0.1", "::1", "localhost", "testclient")
SESSION_HEADER = "X-ClawCal-Session"


def _forwarded(request: Request) -> bool:
    return any(h in request.headers for h in
               ("x-forwarded-for", "x-real-ip", "forwarded"))


def principal(request: Request) -> Principal:
    token_q = request.query_params.get("token")      # EventSource cannot set headers
    auth = request.headers.get("authorization") or (f"Bearer {token_q}"
                                                     if token_q else None)
    client = request.client.host if request.client else None
    return control.identity.authenticate(auth, client, forwarded=_forwarded(request))


def role(required: str):
    def dep(p: Principal = Depends(principal)) -> Principal:
        p.require(required)
        return p
    return dep


viewer, engineer, approver, admin = (role("viewer"), role("engineer"),
                                     role("approver"), role("admin"))


@dataclass
class Caller:
    principal: Principal
    device: Any                    # control.devices.DeviceContext | None
    console: bool                  # the node's own console: loopback, not proxied
    node_url: str
    session_id: str | None

    @property
    def grade(self) -> str:
        if self.device is not None:
            return self.device.grade
        return control.trust.request_grade(None, self.console)[0]


def node_url(request: Request) -> str:
    return settings.public_url or str(request.base_url).rstrip("/")


def caller(request: Request, p: Principal = Depends(principal)) -> Caller:
    dev = control.devices.authenticate(
        request.headers.get(control.devices.DEVICE_HEADER),
        request.headers.get(control.devices.LEASE_HEADER), p)
    host = request.client.host if request.client else None
    console = dev is None and host in LOOPBACK and not _forwarded(request)
    return Caller(principal=p, device=dev, console=console,
                  node_url=node_url(request),
                  session_id=request.headers.get(SESSION_HEADER) or None)
