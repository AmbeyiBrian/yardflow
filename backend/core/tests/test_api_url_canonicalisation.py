"""A trailing slash must not look like a missing endpoint (§6.1).

Written from a real report. A browser tab left open across a deployment kept an
older bundle that appended a trailing slash to every API path, and the server log
recorded the result plainly:

    "GET /api/v1/gate-ins?page_size=50"   200
    "GET /api/v1/gate-ins/?page_size=50"  404

The 404 was Django's own HTML page, so the frontend had no message to read and
printed "Request failed (404)." beside an empty list — which reads as "the yard is
empty and the app is broken" rather than "reload the tab".

Two rules come out of that, and both are here: a slash redirects rather than
refuses, and anything genuinely unmatched under `/api/` still answers in the
documented envelope.
"""

import pytest
from django.urls import reverse

from core.provisioning import provision_tenant


@pytest.fixture
def signed_in(db, client, settings):
    """A real HTTP client, on the tenant's own host, holding a token."""
    settings.TENANT_BASE_DOMAIN = "localhost"
    result = provision_tenant(
        name="Silvertech", slug="silvertech", owner_email="owner@silvertech.co.ke"
    )
    owner = result["owner"]
    owner.set_password("a good long password")
    owner.save()

    client.defaults["HTTP_HOST"] = "silvertech.localhost"
    token = client.post(
        reverse("v1:auth:login"),
        {"identifier": "owner@silvertech.co.ke", "password": "a good long password"},
        content_type="application/json",
    ).json()["access"]
    client.defaults["HTTP_AUTHORIZATION"] = f"Bearer {token}"
    return client


class TestTheTrailingSlash:
    def test_a_slashed_collection_redirects_to_the_canonical_url(self, signed_in):
        response = signed_in.get("/api/v1/gate-ins/?page_size=50")

        assert response.status_code == 307, "307 keeps the method, and is not cached"
        assert response["Location"] == "/api/v1/gate-ins?page_size=50", (
            "the query string has to survive, or the redirect loses the filter"
        )

    def test_following_the_redirect_returns_the_list(self, signed_in):
        response = signed_in.get("/api/v1/gate-ins/?page_size=50", follow=True)

        assert response.status_code == 200
        assert "results" in response.json()

    def test_a_write_keeps_its_method_and_body(self, signed_in):
        """The reason this is 307 and not 301 or 308.

        A 301 lets a client turn a POST into a GET. For `POST
        /gate-outs/{id}/release` that would drop a release on the floor and answer
        200 for the read it turned into — the worst outcome available.
        """
        response = signed_in.post(
            "/api/v1/item-categories/",
            {"name": "Redirected", "code": "RDR"},
            content_type="application/json",
        )

        assert response.status_code == 307
        assert response["Location"] == "/api/v1/item-categories"

    def test_the_redirected_write_actually_lands(self, signed_in):
        response = signed_in.post(
            "/api/v1/item-categories/",
            {"name": "Redirected", "code": "RDR"},
            content_type="application/json",
            follow=True,
        )

        assert response.status_code == 201, response.content[:300]
        assert response.json()["name"] == "Redirected"

    def test_the_redirect_is_never_cacheable(self, signed_in):
        """Found the hard way, from a browser stuck in a loop.

        The first version answered **308**, which browsers cache indefinitely. A
        browser still holding a cached 301 from the era when the router used
        trailing slashes then had two permanent redirects pointing at each other:
        308 stripping the slash, the cached 301 restoring it. It looped inside the
        HTTP cache until Chrome gave up — and because nothing left the cache, the
        server log was empty while the screen said the server could not be
        reached. A temporary redirect cannot be stored that way, and this says so
        out loud.
        """
        response = signed_in.get("/api/v1/gate-ins/?page_size=50")

        assert response.status_code == 307, "301 and 308 are cached; 307 is not"
        assert "no-store" in response["Cache-Control"]

    def test_the_canonical_url_does_not_redirect_back(self, signed_in):
        """The other half of the loop.

        `APPEND_SLASH` adds a slash to a path that does not resolve. If any API
        route were declared *with* one, the two mechanisms would push a request
        back and forth forever. So the canonical form has to be a dead end.
        """
        response = signed_in.get("/api/v1/gate-ins?page_size=50")

        assert response.status_code == 200, (
            "the slash-less form must answer, not redirect — otherwise "
            "APPEND_SLASH and this middleware fight"
        )

    def test_a_path_that_is_simply_wrong_is_not_redirected(self, signed_in):
        """Dropping the slash has to lead somewhere. Otherwise the path is wrong,
        and a redirect would only move the 404 one round trip away."""
        response = signed_in.get("/api/v1/no-such-thing/")

        assert response.status_code == 404


class TestTheShapeOfAnApiNotFound:
    def test_an_unmatched_api_path_answers_json(self, signed_in):
        """§6.1: one error shape. The caller is code; HTML tells it nothing."""
        response = signed_in.get("/api/v1/nothing-here")

        assert response.status_code == 404
        assert response["Content-Type"].startswith("application/json")
        body = response.json()
        assert body["error"]["code"] == "NOT_FOUND"
        # Something a screen can show, rather than a status code.
        assert body["error"]["message"]

    def test_a_real_endpoint_still_404s_the_way_it_did(self, signed_in):
        """The middleware must not touch DRF's own 404s.

        §2.4 answers 404 for another tenant's object deliberately, and that
        response already carries the documented shape.
        """
        response = signed_in.get("/api/v1/gate-ins/999999")

        assert response.status_code == 404
        assert response["Content-Type"].startswith("application/json")

    def test_pages_outside_the_api_are_untouched(self, db, client):
        """The admin and the printed documents are HTML, and stay HTML."""
        response = client.get("/definitely-not-a-page")

        assert response.status_code == 404
        assert "application/json" not in response.get("Content-Type", "")
