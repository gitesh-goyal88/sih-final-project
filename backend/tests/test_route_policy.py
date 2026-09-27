"""SEC-AZ-01: deny by default — every route declares `policy(...)` or `public()`; CI fails otherwise."""

from __future__ import annotations

from typing import Any

from fastapi.routing import APIRoute, APIWebSocketRoute


def _deps(dependant: Any) -> list[Any]:
    out = []
    for d in dependant.dependencies:
        out.append(d.call)
        out.extend(_deps(d))
    return out


def _routes(app: Any) -> list[Any]:
    found: list[Any] = []

    def walk(routes: list[Any]) -> None:
        for r in routes:
            if isinstance(r, (APIRoute, APIWebSocketRoute)):
                found.append(r)
            inner = getattr(r, "routes", None) or getattr(getattr(r, "original_router", None), "routes", None)
            if inner:
                walk(inner)

    walk(app.router.routes)
    return found


def test_every_route_declares_a_policy() -> None:
    from app.main import app

    missing = []
    routes = _routes(app)
    assert len(routes) > 90
    for r in routes:
        if isinstance(r, APIWebSocketRoute) or r.path.endswith("/ws") or "/dev/" in r.path or r.path.endswith("jwks.json"):
            continue  # WS authenticates by ticket; /dev only exists in local/demo
        if r.path.endswith(("/openapi.json", "/docs", "/docs/oauth2-redirect")):
            continue
        if not any(hasattr(c, "__am_policy__") for c in _deps(r.dependant)):
            missing.append(f"{sorted(r.methods)} {r.path}")
    assert not missing, "routes without @policy/public():\n" + "\n".join(missing)
