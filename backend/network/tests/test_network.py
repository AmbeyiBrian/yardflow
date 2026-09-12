"""T2.7–T2.10 — clients, sites, references and work orders (§4.4; C5–C7, D13, D14)."""

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from core.rls import rls_bypass
from network.factories import (
    ClientFactory,
    SiteFactory,
    SiteReferenceFactory,
    WorkOrderFactory,
)
from network.models import Client, Site, SiteReference, SiteStatus, WorkOrderStatus


class TestClient:
    """C5: register clients so consignment stock is attributable."""

    def test_client_names_are_unique_within_a_tenant(self, tenant):
        ClientFactory(name="Safaricom")

        with pytest.raises(IntegrityError), transaction.atomic():
            Client.objects.create(organization=tenant, name="Safaricom")

    def test_a_client_with_no_pattern_accepts_any_site_code(self, tenant):
        """C6: optional is the important word.

        Operator formats are unpublished (D13). A client with no pattern must
        accept whatever the work order says.
        """
        client = ClientFactory(name="Towerco", site_code_pattern="")

        client.check_site_code("anything at all 123 /#")

    def test_an_invalid_pattern_is_rejected_when_it_is_set(self, tenant):
        """Otherwise every site reference for that client fails, unhelpfully."""
        with pytest.raises(ValidationError, match="not a valid pattern"):
            ClientFactory(site_code_pattern="[unclosed")

    def test_a_pattern_validates_site_codes(self, tenant):
        client = ClientFactory(name="Safaricom", site_code_pattern=r"SAF\d{5}")

        client.check_site_code("SAF12345")
        with pytest.raises(ValidationError, match="does not match"):
            client.check_site_code("SAF123")

    def test_a_pattern_must_match_the_whole_value(self, tenant):
        """A partial match would let "SAF12345-junk" through."""
        client = ClientFactory(site_code_pattern=r"SAF\d{5}")

        with pytest.raises(ValidationError):
            client.check_site_code("SAF12345-junk")


class TestSiteRegister:
    """C6: the site register, and why it has no hardcoded code format."""

    def test_a_site_has_an_internal_reference_unique_to_the_tenant(self, tenant):
        SiteFactory(internal_ref="SLV-1001")

        with pytest.raises(IntegrityError), transaction.atomic():
            SiteFactory(internal_ref="SLV-1001")

    def test_radio_identifiers_are_never_validated(self, tenant):
        """C6: "may be captured as free fields, never validated".

        These come off a work order in whatever form the operator wrote them.
        Rejecting one would stop work over a formatting opinion.
        """
        site = SiteFactory(cell_id="not a real cell id!!", enodeb_id="???")

        site.refresh_from_db()
        assert site.cell_id == "not a real cell id!!"

    def test_coordinates_are_optional(self, tenant):
        site = SiteFactory(latitude=None, longitude=None)

        assert site.latitude is None

    def test_a_decommissioned_site_stays_in_the_register(self, tenant):
        """C6, D5: recoveries originate from decommissioned sites."""
        site = SiteFactory(internal_ref="SLV-9000")
        site.status = SiteStatus.DECOMMISSIONED
        site.save()

        assert Site.objects.filter(internal_ref="SLV-9000").exists()
        assert site.is_decommissioned is True

    def test_a_site_can_never_be_deleted(self, tenant):
        """A recovery whose origin had been deleted could not be shown to the
        operator — the one report they are most likely to ask for."""
        site = SiteFactory()

        with pytest.raises(ValidationError, match="never deleted"):
            site.delete()


class TestSiteReferences:
    """C6: several labelled references per site — the heart of the requirement."""

    def test_one_site_can_carry_several_differently_labelled_references(self, tenant):
        """C6's exact scenario: an operator code, a towerco code, and our own."""
        site = SiteFactory(internal_ref="SLV-1001")

        SiteReferenceFactory(site=site, label="Safaricom site ID", value="SAF12345")
        SiteReferenceFactory(site=site, label="Towerco ref", value="ATC-0099")
        SiteReferenceFactory(site=site, label="Legacy ref", value="OLD-77")

        assert site.references.count() == 3

    def test_searching_any_reference_finds_the_same_site(self, tenant):
        """T2.9's definition of done, stated exactly."""
        site = SiteFactory(internal_ref="SLV-1001")
        SiteReferenceFactory(site=site, label="Safaricom site ID", value="SAF12345")
        SiteReferenceFactory(site=site, label="Towerco ref", value="ATC-0099")

        for search in ("SAF12345", "ATC-0099"):
            found = Site.objects.filter(references__value=search).distinct()
            assert list(found) == [site], f"searching {search} did not resolve the site"

        # And the internal reference resolves it too.
        assert list(Site.objects.filter(internal_ref="SLV-1001")) == [site]

    def test_a_label_is_unique_per_site(self, tenant):
        """Two "Safaricom site ID" values on one site would be ambiguous."""
        site = SiteFactory()
        SiteReferenceFactory(site=site, label="Safaricom site ID", value="SAF1")

        # `save()` validates constraints, so this surfaces as a readable
        # ValidationError rather than a raw IntegrityError.
        with pytest.raises(ValidationError, match="already exists"):
            SiteReference.objects.create(
                organization=site.organization,
                site=site,
                label="Safaricom site ID",
                value="SAF2",
            )

    def test_the_same_label_may_be_used_on_different_sites(self, tenant):
        first = SiteFactory()
        second = SiteFactory()

        SiteReferenceFactory(site=first, label="Safaricom site ID", value="SAF1")
        SiteReferenceFactory(site=second, label="Safaricom site ID", value="SAF2")

        assert SiteReference.objects.filter(label="Safaricom site ID").count() == 2

    def test_a_reference_is_checked_against_the_clients_pattern(self, tenant):
        """T2.7's definition of done."""
        client = ClientFactory(name="Safaricom", site_code_pattern=r"SAF\d{5}")
        site = SiteFactory(client=client)

        SiteReferenceFactory(site=site, label="Safaricom site ID", value="SAF12345")

        with pytest.raises(ValidationError, match="does not match"):
            SiteReferenceFactory(site=site, label="Another", value="nonsense")

    def test_a_reference_for_a_client_without_a_pattern_is_accepted(self, tenant):
        client = ClientFactory(name="Towerco", site_code_pattern="")
        site = SiteFactory(client=client)

        SiteReferenceFactory(site=site, label="Towerco ref", value="whatever/they-use")


class TestWorkOrders:
    """C7, D14: optional throughout."""

    def test_a_work_order_groups_sites_under_a_client(self, tenant):
        client = ClientFactory()
        work_order = WorkOrderFactory(client=client, reference="WO-2001")
        work_order.sites.add(SiteFactory(client=client), SiteFactory(client=client))

        assert work_order.sites.count() == 2

    def test_a_work_order_may_have_no_sites(self, tenant):
        """D14: optional means it must not require anything to be usable."""
        assert WorkOrderFactory().sites.count() == 0

    def test_references_are_unique_within_a_tenant(self, tenant):
        WorkOrderFactory(reference="WO-2001")

        with pytest.raises(IntegrityError), transaction.atomic():
            WorkOrderFactory(reference="WO-2001")

    def test_a_work_order_opens_in_the_open_state(self, tenant):
        work_order = WorkOrderFactory()

        assert work_order.status == WorkOrderStatus.OPEN
        assert work_order.closed_at is None

    def test_a_closed_work_order_must_record_when(self, tenant):
        """Otherwise "closed" carries no information an auditor can use."""
        work_order = WorkOrderFactory()

        work_order.status = WorkOrderStatus.CLOSED
        with pytest.raises(IntegrityError), transaction.atomic():
            work_order.save()

    def test_closing_with_a_timestamp_is_accepted(self, tenant):
        from django.utils import timezone

        work_order = WorkOrderFactory()
        work_order.status = WorkOrderStatus.CLOSED
        work_order.closed_at = timezone.now()
        work_order.save()

        work_order.refresh_from_db()
        assert work_order.status == WorkOrderStatus.CLOSED

    def test_a_work_order_with_no_material_reconciles(self, tenant):
        """C7: closing warns only when material is actually unreconciled.

        A zero here is now an answer rather than a placeholder — T5.7 built the
        reconciliation, so nothing issued genuinely means nothing outstanding.
        """
        summary = WorkOrderFactory().unreconciled_summary()

        assert summary["available"] is True
        assert summary["unreconciled"] is False
        assert summary["items"] == []


class TestNetworkIsolation:
    def test_sites_are_scoped_to_their_organization(self, organization, other_organization):
        from core.tenancy import tenant_context

        with tenant_context(organization):
            SiteFactory(internal_ref="OURS-1")
        with tenant_context(other_organization):
            SiteFactory(internal_ref="THEIRS-1")

        with tenant_context(organization):
            refs = set(Site.objects.values_list("internal_ref", flat=True))
            assert refs == {"OURS-1"}

    def test_the_same_internal_reference_may_exist_in_two_tenants(
        self, organization, other_organization
    ):
        from core.tenancy import tenant_context

        with tenant_context(organization):
            SiteFactory(internal_ref="SLV-1001")
        with tenant_context(other_organization):
            SiteFactory(internal_ref="SLV-1001")

        # `all_objects` lifts the manager's filter; row-level security is a
        # separate barrier, so crossing tenants needs `rls_bypass` as well (§2.3).
        with rls_bypass():
            assert Site.all_objects.filter(internal_ref="SLV-1001").count() == 2
