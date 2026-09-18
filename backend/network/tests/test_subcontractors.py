"""T10.4 — the subcontractor register (§4.14; O4).

O4's distinction is the point: a **supplier** sells goods and is named once, in
free text, on a receipt. A **subcontractor** does work, is paid an agreed price
per job, and their cost has to roll up by party — which is impossible if the
party is whatever somebody typed that morning.
"""

import pytest
from django.db import IntegrityError, transaction

from core.rls import rls_bypass
from core.tenancy import tenant_context
from network.models import Subcontractor


@pytest.mark.django_db
class TestTheRegister:
    def test_a_name_is_unique_within_a_tenant(self, tenant):
        Subcontractor.objects.create(organization=tenant, name="Rigging Co")

        with pytest.raises(IntegrityError), transaction.atomic():
            Subcontractor.objects.create(organization=tenant, name="Rigging Co")

    def test_the_same_name_may_exist_in_two_tenants(
        self, organization, other_organization
    ):
        for org in (organization, other_organization):
            with tenant_context(org):
                Subcontractor.objects.create(organization=org, name="Rigging Co")

        with rls_bypass():
            assert Subcontractor.all_objects.filter(name="Rigging Co").count() == 2

    def test_one_arrives_active(self, tenant):
        assert Subcontractor.objects.create(organization=tenant, name="A").is_active

    def test_it_leaves_the_list_by_being_deactivated(self, tenant):
        """O4: never deleted — last year's cost still names them."""
        contractor = Subcontractor.objects.create(organization=tenant, name="A")
        contractor.is_active = False
        contractor.save()

        assert Subcontractor.objects.filter(is_active=True).count() == 0
        assert Subcontractor.objects.count() == 1
