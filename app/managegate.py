"""The one gate in front of every /_cache route.

THE RULE: no XHC_MANAGE_TOKEN, no management API. An unset or blank token makes
the whole /_cache surface answer 404 naming the setting -- read-only routes
included, because status, repos, jobs and pins describe what this cache holds
and who asked for it. With a token set, a missing or wrong `Authorization:
Bearer` is 401, compared in constant time.

This used to be the other way round: an unset token meant OPEN, and every
deployment that never set the variable served prewarm, eviction, deletion and
GC to anyone who could reach the port.

HOW IT IS APPLIED: as the ROUTE CLASS of each /_cache router, not as a
per-route dependency. So:

  - a route added to one of those routers later is gated without anyone having
    to remember to ask for it;
  - the check runs BEFORE FastAPI parses the request body, so an anonymous
    caller gets 404/401 rather than a 422 that confirms the route exists;
  - no handler, dependency or body model runs on a refusal.

A NEW /_cache ROUTER MUST USE `route_class=ManageRoute`. tests/test_manage_gate
enumerates every /_cache route of the real app and fails on any that does not
refuse, so forgetting is caught rather than shipped.

/healthz and /metrics are not here and do not depend on this: /metrics has its
own XHC_METRICS_AUTH.
"""

from __future__ import annotations

import hmac
from collections.abc import Callable, Coroutine
from typing import Any

from fastapi import Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.routing import APIRoute

from .config import settings

# Same body style as the reserved-path refusals in hfcompat: plain text, naming
# the setting, so an operator who sees it knows which knob turns it on.
DISABLED_REASON = "the management API is disabled (XHC_MANAGE_TOKEN is unset)"


def enabled() -> bool:
    """True when a non-blank management token is configured."""
    token = settings.manage_token
    return bool(token and token.strip())


def refusal(authorization: str | None) -> Response | None:
    """The response that refuses this request, or None to let it through."""
    if not enabled():
        return PlainTextResponse(DISABLED_REASON + "\n", status_code=404)
    expected = f"Bearer {settings.manage_token}"
    # compare_digest so the comparison does not leak the token's prefix. Bytes,
    # because the str form refuses non-ASCII and would 500 on such a header.
    if authorization is None or not hmac.compare_digest(
        authorization.encode(), expected.encode()
    ):
        return JSONResponse(
            {"detail": "invalid or missing management token"},
            status_code=401,
        )
    return None


class ManageRoute(APIRoute):
    """An APIRoute that consults `refusal` before anything else runs."""

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        inner = super().get_route_handler()

        async def gated(request: Request) -> Response:
            refused = refusal(request.headers.get("authorization"))
            if refused is not None:
                return refused
            return await inner(request)

        return gated
