"""Searching the registers (§6; M1, I3, J2).

The API has searched every ordinary collection since phase 2. These three are
assembled in Python — the exceptions register from four sources, the overdue
report from grouped rows, quarantine from balances — so none of them inherited
it, and the screens that read them had no box.

A register nobody can find anything in is the failure J2 names outright:
quarantine becomes a graveyard because nobody is looking at it.
"""

import pytest
from django.urls import reverse

from core.provisioning import provision_tenant

PASSWORD = "a good long password"


@pytest.fixture
def signed_in(db, client, settings):
    settings.TENANT_BASE_DOMAIN = "localhost"
    result = provision_tenant(
        name="Silvertech", slug="silvertech", owner_email="owner@silvertech.co.ke"
    )
    owner = result["owner"]
    owner.set_password(PASSWORD)
    owner.save()
    client.defaults["HTTP_HOST"] = "silvertech.localhost"
    token = client.post(
        reverse("v1:auth:login"),
        {"identifier": "owner@silvertech.co.ke", "password": PASSWORD},
        content_type="application/json",
    ).json()["access"]
    return client, token, result["organization"]


def auth(token):
    return {"HTTP_AUTHORIZATION": f"Bearer {token}"}


@pytest.mark.django_db
class TestTheRegistersAcceptASearch:
    """Empty registers still have to answer, or the screens cannot render."""

    @pytest.mark.parametrize(
        "route", ["v1:quarantine", "v1:exceptions", "v1:custody-overdue"]
    )
    def test_searching_an_empty_register_is_not_an_error(self, signed_in, route):
        http, token, _ = signed_in

        response = http.get(f"{reverse(route)}?search=nothing", **auth(token))

        assert response.status_code == 200, response.content

    @pytest.mark.parametrize(
        "route", ["v1:quarantine", "v1:exceptions", "v1:custody-overdue"]
    )
    def test_an_empty_search_behaves_as_no_search(self, signed_in, route):
        """A blank box must not filter everything away."""
        http, token, _ = signed_in

        with_blank = http.get(f"{reverse(route)}?search=", **auth(token)).json()
        without = http.get(reverse(route), **auth(token)).json()

        assert with_blank == without


@pytest.mark.django_db
class TestQuarantineSearch:
    def test_it_narrows_to_the_item_searched_for(self, signed_in):
        from catalogue.factories import ItemTypeFactory
        from core.tenancy import tenant_context
        from locations.factories import YardFactory
        from locations.models import LocationType
        from locations.nodes import node_for_location
        from stock.models import Condition, StockBalance

        http, token, organization = signed_in
        with tenant_context(organization):
            yard = YardFactory(name="Main yard")
            bay = yard.children.filter(type=LocationType.QUARANTINE).first()
            assert bay is not None, "provisioning seeds a quarantine bay per yard"
            node = node_for_location(bay)
            wanted = ItemTypeFactory(name="Faulty RRU 2x40W")
            other = ItemTypeFactory(name="Jumper cable")
            for item in (wanted, other):
                StockBalance.objects.create(
                    organization=organization,
                    node=node,
                    item_type=item,
                    condition=Condition.FAULTY,
                    quantity=1,
                    uom=item.uom,
                )

        everything = http.get(reverse("v1:quarantine"), **auth(token)).json()
        narrowed = http.get(
            f"{reverse('v1:quarantine')}?search=RRU", **auth(token)
        ).json()

        assert everything["count"] == 2
        assert narrowed["count"] == 1
        assert "RRU" in narrowed["items"][0]["item_type"]

    def test_the_count_matches_what_is_returned(self, signed_in):
        """Filtered in the query, so the count and the rows cannot disagree."""
        http, token, _ = signed_in

        body = http.get(f"{reverse('v1:quarantine')}?search=zzz", **auth(token)).json()

        assert body["count"] == len(body["items"]) == 0
