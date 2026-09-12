"""Making the API forgiving about the one thing clients get wrong (§6.1).

Every URL in this API has exactly one form, without a trailing slash. That is a
good convention and it is documented, but it has a sharp edge: a client that asks
for `/api/v1/gate-ins/` matches nothing, so Django answers its own 404 — an HTML
page with no error envelope. The frontend cannot read that, so it falls back to
printing the status code, and a storekeeper sees "Request failed (404)." beside an
empty list. It looks like the yard is empty and the app is broken.

It is not hypothetical. A browser tab left open across a deployment kept the older
bundle, which appended the slash. The server log tells the whole story:

    "GET /api/v1/gate-ins?page_size=50"   200
    "GET /api/v1/gate-ins/?page_size=50"  404   <- same request, one character

Two fixes, both about not punishing a client for a character:

**Redirect, do not refuse.** A trailing slash on an API path redirects to the
canonical form with **308**, which — unlike 301 or 302 — obliges the client to
repeat the method and the body. `APPEND_SLASH` cannot help here: it only ever
*adds* a slash, and it drops a POST body doing it.

**And when a path really is wrong, say so in JSON.** Anything unmatched under
`/api/` gets the same `{"error": {...}}` envelope as every other failure, so a
client always has a message to show instead of a number.
"""

from __future__ import annotations

from django.http import HttpResponseRedirect, JsonResponse
from django.urls import resolve
from django.urls.exceptions import Resolver404

API_PREFIX = "/api/"


class HttpResponseRedirectPreservingMethod(HttpResponseRedirect):
    """307: temporary, and the method survives.

    Two things had to be true, and only 307 gives both.

    **The method has to survive.** A 301 or 302 invites the client to turn a POST
    into a GET, which for `POST /gate-outs/{id}/release` would drop a release and
    answer 200 for the read it became.

    **And it must not be permanent.** The first version of this used 308, and that
    was a mistake with teeth: browsers cache permanent redirects indefinitely.
    A browser that still held a cached 301 from the era when the router *did* use
    trailing slashes then had two permanent redirects pointing at each other —
    308 stripping the slash, the cached 301 putting it back — and looped until it
    gave up, entirely inside the HTTP cache. The requests never reached the
    server, so the server log showed nothing at all while the screen said it
    could not be reached. `no-store` is belt and braces on top.
    """

    status_code = 307


class ApiUrlCanonicalisationMiddleware:
    """Redirect `/api/…/` to `/api/…`, and answer unmatched API paths in JSON."""

    def __init__(self, get_response):  # type: ignore[no-untyped-def]
        self.get_response = get_response

    def __call__(self, request):  # type: ignore[no-untyped-def]
        path = request.path

        if (
            path.startswith(API_PREFIX)
            and path.endswith("/")
            and path != API_PREFIX
            # Only when dropping the slash actually leads somewhere. Otherwise the
            # path is simply wrong, and a redirect would just move the 404.
            and _resolves(path.rstrip("/"))
        ):
            target = path.rstrip("/")
            if request.META.get("QUERY_STRING"):
                target = f"{target}?{request.META['QUERY_STRING']}"
            response = HttpResponseRedirectPreservingMethod(target)
            # Never let this be remembered. A cached redirect is how the loop
            # described above became invisible to the server.
            response["Cache-Control"] = "no-store"
            return response

        response = self.get_response(request)

        # Django's own 404 is an HTML page. Under `/api/` that is never useful:
        # the caller is code, and code needs the documented shape (§6.1).
        if (
            response.status_code == 404
            and path.startswith(API_PREFIX)
            and "application/json" not in response.get("Content-Type", "")
        ):
            return JsonResponse(
                {
                    "error": {
                        "code": "NOT_FOUND",
                        "message": "There is no endpoint at this address.",
                        "details": {"path": path},
                    }
                },
                status=404,
            )

        return response


def _resolves(path: str) -> bool:
    try:
        resolve(path)
    except Resolver404:
        return False
    return True
