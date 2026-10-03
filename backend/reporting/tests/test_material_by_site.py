"""T13.6 — the "Material by site" report (§4.16.8; Q5)."""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from accounts.factories import UserFactory
from catalogue.factories import ItemTypeFactory
from catalogue.models import TrackingMode
from jobs.models import Job
from locations.factories import YardFactory
from network.factories import ClientFactory, ProjectFactory, SiteFactory
from reporting.exports import to_excel, to_pdf
from reporting.framework import all_reports, get_report
from stock.factories import ReelFactory, SerialUnitFactory
from stock.models import BulkEarmark, Condition, EarmarkAction, EarmarkEvent

D = Decimal


@pytest.fixture
def scenario(tenant):
    yard = YardFactory(name="Material yard")
    client_x, client_y = ClientFactory(name="Xco"), ClientFactory(name="Yco")
    site_a = SiteFactory(name="Alpha", client=client_x)
    site_b = SiteFactory(name="Bravo", client=client_y)
    clamp = ItemTypeFactory(name="Clamp", uom="ea", default_tracking_mode=TrackingMode.BULK)
    radio = ItemTypeFactory(name="Radio", uom="ea", default_tracking_mode=TrackingMode.SERIALIZED)
    cable = ItemTypeFactory(name="Cable", uom="m", default_tracking_mode=TrackingMode.REEL)

    project = ProjectFactory(client=client_x)
    Job.objects.create(
        organization=tenant,
        reference="MBS-1",
        client=client_x,
        site=site_a,
        project=project,
        assignee=UserFactory(),
    )

    def event(action, site=None, item=clamp, qty=None, to_site=None, **extra):
        return EarmarkEvent.objects.create(
            action=action, site=site, to_site=to_site, item_type=item, quantity=qty, **extra
        )

    unit1 = SerialUnitFactory(item_type=radio, current_node=yard.node, earmark_site=site_a)
    unit2 = SerialUnitFactory(item_type=radio, current_node=yard.node, earmark_site=site_a)
    for unit in (unit1, unit2):
        event(EarmarkAction.EARMARKED, site_a, radio, serial_unit=unit)
    SerialUnitFactory(item_type=radio, current_node=yard.node, earmark_site=site_b)

    event(EarmarkAction.EARMARKED, site_a, clamp, D("10"))
    event(EarmarkAction.EARMARKED, site_b, clamp, D("5"))
    event(EarmarkAction.DELIVERED, site_a, clamp, D("4"))
    event(EarmarkAction.DIVERTED, site_a, clamp, D("2"), to_site=site_b)
    event(EarmarkAction.CHANGED, site_a, clamp, D("3"), to_site=site_b)
    # Long ago: outside any recent period.
    event(
        EarmarkAction.EARMARKED,
        site_b,
        clamp,
        D("100"),
        occurred_at=timezone.now() - timedelta(days=90),
    )

    for site, qty in ((site_a, "4"), (site_b, "8")):
        BulkEarmark.objects.create(
            site=site, node=yard.node, item_type=clamp, condition=Condition.NEW, quantity=D(qty)
        )
    ReelFactory(item_type=cable, current_node=yard.node, earmark_site=site_b,
                remaining_length=D("120.500"))
    return {"a": site_a, "b": site_b, "x": client_x, "project": project}


def run(**params):
    return list(get_report("material-by-site").rows(params))


def by_key(rows):
    return {(r["site"], r["item"]): r for r in rows}


def figures(row):
    return (row["received"], row["sent"], row["waiting"], row["diverted"])


class TestMaterialBySite:
    def test_each_column_per_site_and_item(self, scenario):
        rows = run(from_date=(timezone.now() - timedelta(days=7)).date())
        got = by_key(rows)

        assert figures(got["Alpha", "Clamp"]) == (D("10"), D("4"), D("4"), D("2"))
        assert figures(got["Alpha", "Radio"]) == (2, 0, 2, 0)
        # 5 earmarked + 3 changed to it; the 90-day-old 100 is outside the period.
        assert figures(got["Bravo", "Clamp"]) == (D("8"), 0, D("8"), 0)
        assert figures(got["Bravo", "Radio"]) == (0, 0, 1, 0)
        assert figures(got["Bravo", "Cable"]) == (0, 0, D("120.500"), 0)
        assert [(r["site"], r["item"]) for r in rows] == sorted(
            (r["site"], r["item"]) for r in rows
        )

    def test_the_period_bounds_the_ledger_but_not_what_is_waiting(self, scenario):
        rows = by_key(run())
        assert rows["Bravo", "Clamp"]["received"] == D("108")

        old = by_key(run(to_date=(timezone.now() - timedelta(days=30)).date()))
        assert figures(old["Bravo", "Clamp"]) == (D("100"), 0, D("8"), 0)

    def test_rows_with_nothing_in_any_column_are_left_out(self, scenario):
        # Alpha has no cable; neither does anything else for Alpha.
        assert ("Alpha", "Cable") not in by_key(run())

    def test_client_filter_narrows_the_sites(self, scenario):
        rows = run(client=scenario["x"].pk)
        assert {r["site"] for r in rows} == {"Alpha"}

    def test_project_filter_means_the_sites_of_its_jobs(self, scenario):
        rows = run(project=scenario["project"].pk)
        assert {r["site"] for r in rows} == {"Alpha"}

    def test_site_filter(self, scenario):
        assert {r["site"] for r in run(site=scenario["b"].pk)} == {"Bravo"}

    def test_units_print_as_whole_numbers_and_the_rest_by_quantity(self, scenario):
        rendered = get_report("material-by-site").render({})["rows"]
        radio = next(r for r in rendered if r["site"] == "Alpha" and r["item"] == "Radio")
        clamp = next(r for r in rendered if r["site"] == "Alpha" and r["item"] == "Clamp")
        assert radio["received"] == "2"
        assert clamp["received"] == "10.000"

    def test_a_handful_of_queries_whatever_the_number_of_sites(
        self, scenario, django_assert_max_num_queries
    ):
        for n in range(6):
            site = SiteFactory(name=f"Extra {n}")
            BulkEarmark.objects.create(
                site=site,
                node=BulkEarmark.objects.first().node,
                item_type=BulkEarmark.objects.first().item_type,
                condition=Condition.NEW,
                quantity=D("1"),
            )
        with django_assert_max_num_queries(14):
            assert len(run()) >= 8


class TestInTheCatalogue:
    def test_it_is_listed_under_stock(self, tenant):
        report = next(r for r in all_reports() if r.slug == "material-by-site")
        info = report.as_dict()
        assert info["category"] == "Stock"
        assert info["title"] == "Material by site"
        assert [f["key"] for f in info["filters"]] == [
            "from_date",
            "to_date",
            "client",
            "project",
            "site",
        ]
        assert [c["label"] for c in info["columns"]][3:] == [
            "Received for",
            "Sent to",
            "Still in the yard",
            "Diverted away",
        ]

    def test_it_exports_to_excel_and_pdf(self, tenant, scenario):
        report = get_report("material-by-site")
        assert to_excel(report, {})[:2] == b"PK"
        content, _type, filename = to_pdf(report, {}, organization=tenant)
        assert content
        assert filename.startswith("material-by-site")
