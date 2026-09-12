"""Every collection endpoint answers a plain GET (design §6).

Written after a real failure: ``/api/v1/users`` returned 500 for every request
because the default cursor pagination orders by ``created_at`` and ``User``
records ``date_joined``. Every other test passed — the isolation suite exercises
*detail* verbs, and the endpoint's own tests created and read single objects.
Nothing had ever asked it for a list.

So this asks every registered endpoint for its first page, discovered from the
URL config rather than from a list somebody has to remember to extend. It is a
smoke test and says nothing about the contents; the point is that a new endpoint
cannot be born broken in the one way no other suite looks at.
"""

import pytest
from django.urls import reverse

from core.isolation import discover_tenant_viewsets
from core.provisioning import provision_tenant


@pytest.fixture
def signed_in(db, client, settings):
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
    return client, token


def _list_endpoints():
    return [
        pytest.param(basename, id=basename)
        for basename in sorted(discover_tenant_viewsets())
    ]


def _collection_actions():
    """Every ``detail=False`` GET action on a tenant viewset.

    Added after a second real failure of the same kind: ``/approvals/pending``
    filtered in Python and handed a *list* to cursor pagination, which orders by
    a column. Every request to it was a 500, and the plain list endpoint next to
    it was fine — so nothing above this line would ever have noticed.
    """
    cases = []
    for basename, viewset in sorted(discover_tenant_viewsets().items()):
        for name in dir(viewset):
            handler = getattr(viewset, name, None)
            mapping = getattr(handler, "mapping", None)
            if mapping is None or getattr(handler, "detail", True):
                continue
            if "get" not in mapping:
                continue
            url_path = getattr(handler, "url_path", name).replace("_", "-")
            cases.append(
                pytest.param(basename, url_path, id=f"{basename}-{url_path}")
            )
    return cases


@pytest.mark.parametrize("basename", _list_endpoints())
def test_the_collection_answers_a_get(basename, signed_in):
    """A 200 with a page envelope. Anything else is a broken endpoint.

    The owner holds every permission, so a 403 here would mean the endpoint
    demands something outside the permission registry — also worth failing on.
    """
    http, token = signed_in

    response = http.get(
        reverse(f"v1:{basename}-list"), HTTP_AUTHORIZATION=f"Bearer {token}"
    )

    assert response.status_code == 200, (
        f"GET {basename}-list returned {response.status_code}. "
        f"Body: {response.content[:400]!r}"
    )
    assert "results" in response.json()


@pytest.mark.parametrize("basename", _list_endpoints())
def test_the_collection_paginates(basename, signed_in):
    """Cursor pagination has to resolve its ordering field on this model.

    ``page_size=1`` is what actually exercises it: the ordering is only applied
    when a page boundary exists, which is why the plain list above passed on an
    empty table and production did not.
    """
    http, token = signed_in

    response = http.get(
        reverse(f"v1:{basename}-list"),
        {"page_size": 1},
        HTTP_AUTHORIZATION=f"Bearer {token}",
    )

    assert response.status_code == 200, (
        f"GET {basename}-list?page_size=1 returned {response.status_code}. "
        f"Body: {response.content[:400]!r}"
    )


@pytest.mark.parametrize(("basename", "url_path"), _collection_actions())
def test_a_collection_action_answers_a_get(basename, url_path, signed_in):
    """A custom list action has to survive pagination too."""
    http, token = signed_in

    url = f"{reverse(f'v1:{basename}-list')}/{url_path}"
    response = http.get(
        url, {"page_size": 1}, HTTP_AUTHORIZATION=f"Bearer {token}"
    )

    assert response.status_code == 200, (
        f"GET {url}?page_size=1 returned {response.status_code}. "
        f"Body: {response.content[:400]!r}"
    )


@pytest.mark.parametrize("basename", _list_endpoints())
def test_the_collection_url_has_no_trailing_slash(basename, signed_in):
    """One URL form for the whole API (§6).

    Found from the browser, not from a test: the client calls
    ``/api/v1/gate-outs``, the router answered only ``/api/v1/gate-outs/``, and
    ``APPEND_SLASH`` cannot redirect a POST without discarding its body — so
    Django raised ``RuntimeError`` and *every write from the frontend was a 500*.
    Nothing here noticed, because these tests reverse() their URLs and get
    whichever form the router happens to generate.

    So this asserts the *shape* rather than following it: the no-slash form is
    what §6 documents and what the hand-written paths use, and a router
    registered the other way would silently break the client again.
    """
    assert not reverse(f"v1:{basename}-list").endswith("/"), (
        f"{basename}-list ends in a slash. The frontend calls the no-slash form, "
        f"and APPEND_SLASH turns a POST to it into a 500 rather than a redirect."
    )


@pytest.mark.parametrize("basename", _list_endpoints())
def test_a_write_to_the_collection_url_is_not_a_server_error(basename, signed_in):
    """The failure above, reproduced directly.

    A POST of nothing is expected to be rejected — 400, 403, 405 are all fine.
    What must never happen is a 500, which is what a URL-form mismatch produces.
    """
    http, token = signed_in

    response = http.post(
        reverse(f"v1:{basename}-list"),
        {},
        content_type="application/json",
        HTTP_AUTHORIZATION=f"Bearer {token}",
    )

    assert response.status_code < 500, (
        f"POST {basename}-list returned {response.status_code}. "
        f"Body: {response.content[:300]!r}"
    )
