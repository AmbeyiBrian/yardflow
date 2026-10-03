"""C9 / T12.1: the item picker's search order, and the archive filter."""

import pytest
from django.urls import reverse

from catalogue.factories import ItemTypeFactory
from core.provisioning import provision_tenant
from core.tenancy import tenant_context


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
    return client, token, result["organization"], owner


def auth(token):
    return {"HTTP_AUTHORIZATION": f"Bearer {token}"}


@pytest.fixture
def items(signed_in):
    _http, _token, organization, _owner = signed_in
    with tenant_context(organization):
        # The starter catalogue already holds "RRU 2x40W" and "RRU 4x40W".
        ItemTypeFactory(name="Antenna for RRU")
        ItemTypeFactory(name="Bracket", code="RRU-BRK")
        ItemTypeFactory(name="Cable", description="Fits the RRU cabinet")
        ItemTypeFactory(name="Old RRU", is_archived=True)
        ItemTypeFactory(name="Unrelated")


def names(response):
    assert response.status_code == 200, response.content
    return [row["name"] for row in response.json()["results"]]


class TestItemSearch:
    def test_a_search_lists_the_best_matches_first(self, signed_in, items):
        http, token, *_ = signed_in
        response = http.get(reverse("v1:item-type-list"), {"search": "rru"}, **auth(token))
        assert names(response) == ["RRU 2x40W", "RRU 4x40W", "Antenna for RRU", "Bracket", "Cable"]

    def test_an_explicit_ordering_wins(self, signed_in, items):
        http, token, *_ = signed_in
        response = http.get(
            reverse("v1:item-type-list"), {"search": "rru", "ordering": "-name"}, **auth(token)
        )
        assert names(response) == ["RRU 4x40W", "RRU 2x40W", "Cable", "Bracket", "Antenna for RRU"]

    def test_is_archived_can_be_filtered(self, signed_in, items):
        http, token, *_ = signed_in
        url = reverse("v1:item-type-list")
        live = names(http.get(url, {"is_archived": "false"}, **auth(token)))
        assert "Old RRU" not in live
        archived = names(http.get(url, {"is_archived": "true"}, **auth(token)))
        assert archived == ["Old RRU"]
