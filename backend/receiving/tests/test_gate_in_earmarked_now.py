"""Q2 — a delivery's lines show where their material is earmarked now."""

from decimal import Decimal

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from core.provisioning import provision_tenant
from core.tenancy import tenant_context
from locations.factories import YardFactory
from network.factories import SiteFactory
from receiving.services import post_gate_in
from receiving.tests.test_gate_in import add_bulk_line, draft
from receiving.tests.test_gate_in_earmarks import add_reel_line, add_serialized_line
from stock.models import BulkEarmark, Reel, SerialUnit


@pytest.fixture
def signed_in(db, client, settings):
    settings.TENANT_BASE_DOMAIN = "localhost"
    result = provision_tenant(name="Earmarkco", slug="earmarkco", owner_email="o@earmarkco.co.ke")
    owner = result["owner"]
    owner.set_password("a good long password")
    owner.save()
    client.defaults["HTTP_HOST"] = "earmarkco.localhost"
    token = client.post(
        reverse("v1:auth:login"),
        {"identifier": "o@earmarkco.co.ke", "password": "a good long password"},
        content_type="application/json",
    ).json()["access"]
    return client, {"HTTP_AUTHORIZATION": f"Bearer {token}"}, result["organization"]


def test_later_earmarks_show_on_each_line(signed_in):
    http, auth, org = signed_in
    with tenant_context(org):
        yard = YardFactory(name="Y")
        site_a, site_b = SiteFactory(name="Atlantis"), SiteFactory(name="Baobab")
        gate_in = draft(org, yard)
        serial = add_serialized_line(gate_in, ["RRU-1", "RRU-2"])
        reel = add_reel_line(gate_in)
        bulk = add_bulk_line(gate_in, quantity=25)
        unclaimed = add_bulk_line(gate_in, quantity=5)
        url = reverse("v1:gate-in-detail", args=[gate_in.pk])

        # A draft has no movements and nothing earmarked.
        body = http.get(url, **auth).json()
        assert all(line["earmarked_now"] == [] for line in body["lines"])

        post_gate_in(gate_in)
        SerialUnit.objects.filter(serial_number="RRU-1").update(earmark_site=site_b)
        SerialUnit.objects.filter(serial_number="RRU-2").update(earmark_site=site_a)
        Reel.objects.update(earmark_site=site_a)
        BulkEarmark.objects.create(
            organization=org,
            site=site_a,
            node=yard.node,
            item_type=bulk.item_type,
            condition=bulk.condition,
            quantity=Decimal("10"),
        )

        with CaptureQueriesContext(connection) as ctx:
            response = http.get(url, **auth)
        lines = {line["id"]: line for line in response.json()["lines"]}
        a = {"site": site_a.pk, "name": "Atlantis"}
        assert lines[serial.pk]["earmarked_now"] == [a, {"site": site_b.pk, "name": "Baobab"}]
        assert lines[reel.pk]["earmarked_now"] == [a]
        assert lines[bulk.pk]["earmarked_now"] == [a]
        assert lines[unclaimed.pk]["earmarked_now"] == []
        earmark_queries = [q for q in ctx.captured_queries if "earmark" in q["sql"].lower()]
        assert len(earmark_queries) <= 4
        print(f"detail total queries: {len(ctx)}")

        listing = http.get(reverse("v1:gate-in-list"), **auth).json()
        rows = listing["results"] if isinstance(listing, dict) else listing
        assert all(line["earmarked_now"] == [] for row in rows for line in row["lines"])
