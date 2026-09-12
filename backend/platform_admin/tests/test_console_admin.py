"""The platform console: cross-tenant, and read-only (§2.4, A1, B6).

Support questions arrive as "nothing happened" and "the SMS never came", and
answering them used to mean a database client. These screens exist so the
platform owner can look without being able to touch.

"Read-only" is a promise worth testing rather than commenting: a `ModelAdmin`
that forgets one of the three permission methods gets a working edit form, over
an append-only ledger, with no tenant scoping. So this walks every registered
model and asserts the promise for all of them — a model registered next year is
covered without anybody remembering to add a test.
"""

from __future__ import annotations

import pytest
from django.contrib import admin
from django.urls import reverse

from accounts.models import Role, User
from core.provisioning import provision_tenant
from core.tenancy import tenant_context
from platform_admin.admin import PlatformReadOnly

pytestmark = pytest.mark.django_db


def read_only_models():
    """Every model registered through the read-only base."""
    return [
        pytest.param(model, id=f"{model._meta.app_label}.{model.__name__}")
        for model, model_admin in admin.site._registry.items()
        if isinstance(model_admin, PlatformReadOnly)
    ]


@pytest.fixture
def platform_admin(db):
    return User.objects.create_superuser(
        email="platform@yardflow.co.ke", password="a good long password"
    )


@pytest.fixture
def console(client, platform_admin):
    client.force_login(platform_admin)
    return client


@pytest.fixture
def two_tenants(db):
    first = provision_tenant(
        name="Silvertech", slug="silvertech", owner_email="owner@silvertech.co.ke"
    )
    second = provision_tenant(
        name="Global Connect", slug="globalconnect", owner_email="owner@globalconnect.co.ke"
    )
    return first["organization"], second["organization"]


class TestItIsReadOnly:
    @pytest.mark.parametrize("model", read_only_models())
    def test_nothing_can_be_added_changed_or_deleted(self, model, console, platform_admin):
        """All three, for every model. Forgetting one leaves a working edit form
        over data that is supposed to be immutable."""
        model_admin = admin.site._registry[model]
        request = console.get(reverse("admin:index")).wsgi_request

        assert model_admin.has_add_permission(request) is False
        assert model_admin.has_change_permission(request) is False
        assert model_admin.has_delete_permission(request) is False

    @pytest.mark.parametrize("model", read_only_models())
    def test_the_changelist_opens(self, model, console):
        """Cheap, and it catches the ordinary mistake: a `list_display` naming a
        field that does not exist only fails when somebody opens the page."""
        url = reverse(
            f"admin:{model._meta.app_label}_{model._meta.model_name}_changelist"
        )

        response = console.get(url)

        assert response.status_code == 200, response.content[:300]

    @pytest.mark.parametrize("model", read_only_models())
    def test_the_add_page_is_refused(self, model, console):
        url = reverse(f"admin:{model._meta.app_label}_{model._meta.model_name}_add")

        response = console.get(url)

        assert response.status_code in (403, 302)


class TestItLooksAtOneTenant:
    """The database decides this, not the interface.

    Policies are created with `FORCE ROW LEVEL SECURITY`, so Postgres applies
    them to the table owner too: no role in this system can read two tenants'
    rows in one query. `all_objects` bypasses Django's manager, not the database
    — which is easy to assume and wrong, and was assumed here first. So the
    console publishes a chosen tenant for the request, exactly as an ordinary
    request does.
    """

    def test_nothing_is_listed_until_a_tenant_is_chosen(self, console, two_tenants):
        response = console.get(reverse("admin:accounts_role_changelist"))

        assert response.status_code == 200
        assert response.context["cl"].result_count == 0
        # And it says why, rather than looking broken.
        assert any(
            "Choose a tenant" in str(message)
            for message in response.context["messages"]
        )

    def test_choosing_one_shows_that_tenants_rows(self, console, two_tenants):
        silvertech, _globalconnect = two_tenants
        with tenant_context(silvertech):
            expected = Role.objects.count()
        assert expected > 0

        response = console.get(
            reverse("admin:accounts_role_changelist"),
            {"organization__id__exact": str(silvertech.pk)},
        )

        assert response.context["cl"].result_count == expected

    def test_the_other_tenants_rows_are_not_there(self, console, two_tenants):
        """Row-level security, not a filter: the rows are unreadable, not hidden."""
        silvertech, globalconnect = two_tenants

        response = console.get(
            reverse("admin:accounts_role_changelist"),
            {"organization__id__exact": str(silvertech.pk)},
        )

        organizations = {
            role.organization_id for role in response.context["cl"].result_list
        }
        assert organizations == {silvertech.pk}
        assert globalconnect.pk not in organizations

    def test_the_choice_is_remembered_between_screens(self, console, two_tenants):
        """Support moves from deliveries to the audit log to the ledger. Picking
        the tenant again each time would be the main thing you did."""
        silvertech, _globalconnect = two_tenants
        console.get(
            reverse("admin:accounts_role_changelist"),
            {"organization__id__exact": str(silvertech.pk)},
        )

        response = console.get(reverse("admin:core_auditlog_changelist"))

        assert response.context["cl"].result_count > 0

    def test_the_tenant_is_named_on_every_list(self, console, two_tenants):
        """A cross-tenant screen that does not say whose row you are reading is
        how a support answer ends up about the wrong company."""
        for model, model_admin in admin.site._registry.items():
            if not isinstance(model_admin, PlatformReadOnly):
                continue
            assert "organization" in model_admin.list_display, (
                f"{model.__name__} does not show which tenant a row belongs to"
            )

    def test_it_can_be_narrowed_to_one_tenant(self, console, two_tenants):
        silvertech, _globalconnect = two_tenants
        with tenant_context(silvertech):
            role_count = Role.objects.count()
        assert role_count > 0

        response = console.get(
            reverse("admin:accounts_role_changelist"),
            {"organization__id__exact": str(silvertech.pk)},
        )

        assert response.status_code == 200
        # The *results*, not the page: the filter sidebar names every tenant by
        # design, so searching the whole body for the other name proves nothing.
        assert response.context["cl"].result_count == role_count


class TestWhoMayLook:
    def test_a_tenant_user_sees_none_of_it(self, client, two_tenants):
        """Even one made staff. Belonging to a tenant is what disqualifies
        them — otherwise the console would be a way around §2.4."""
        silvertech, _other = two_tenants
        with tenant_context(silvertech):
            member = User.objects.create_user(
                email="store@silvertech.co.ke",
                password="a good long password",
                organization=silvertech,
            )
        member.is_staff = True
        member.save(update_fields=["is_staff"])
        client.force_login(member)

        response = client.get(reverse("admin:stock_stockmovement_changelist"))

        assert response.status_code in (302, 403)

    def test_the_platform_owner_does(self, console):
        response = console.get(reverse("admin:core_auditlog_changelist"))

        assert response.status_code == 200


class TestWhatItAnswers:
    """The questions this was built for, each in one query."""

    def test_why_did_the_sms_not_arrive(self, console, two_tenants):
        """Status and the provider's error, side by side."""
        from notifications.models import NotificationDelivery

        model_admin = admin.site._registry[NotificationDelivery]

        assert "status" in model_admin.list_display
        assert "last_error" in model_admin.search_fields

    def test_what_did_this_tenant_buy(self, console):
        from notifications.models import SmsCreditEntry

        model_admin = admin.site._registry[SmsCreditEntry]

        assert "quantity" in model_admin.list_display
        assert "kind" in model_admin.list_filter

    def test_who_changed_that(self, console):
        from core.models import AuditLog

        model_admin = admin.site._registry[AuditLog]

        assert "actor_identifier" in model_admin.list_display
        assert model_admin.date_hierarchy == "occurred_at"
