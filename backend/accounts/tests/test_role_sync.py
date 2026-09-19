"""Seeded roles keep up with the registry (§4.2; B4).

The bug this exists for was invisible. `DEFAULT_ROLES` is applied when a tenant
is provisioned, so a permission added later reached only tenants created after
it — and because the screens render what a user may see, an owner missing a
permission got a page with the figures gone and no error anywhere. Epic O's five
permissions landed on two live tenants exactly that way.

The last test is the one that matters most: a tenant that edited its own roles
must keep those edits. A backfill that trampled them would trade a silent bug
for a loud one.
"""

import pytest

from accounts.models import Role, RolePermission
from accounts.permissions_registry import ALL_CODENAMES, DEFAULT_ROLES, PERM
from accounts.role_sync import sync_seeded_roles
from core.provisioning import provision_tenant


@pytest.fixture
def tenancy(db):
    """A provisioned tenant, with its context held open.

    Row-level security applies to the test's own reads and writes too (§2.3),
    so the assertions below need the same context the application runs in.
    """
    from core.tenancy import tenant_context

    result = provision_tenant(
        name="Silvertech", slug="silvertech", owner_email="owner@silvertech.co.ke"
    )
    with tenant_context(result["organization"]):
        yield result["organization"], result["roles"]


def codenames(role) -> set[str]:
    return set(RolePermission.objects.filter(role=role).values_list("codename", flat=True))


@pytest.mark.django_db
class TestABackfillGrantsWhatIsMissing:
    def test_an_owner_missing_a_new_permission_gets_it(self, tenancy):
        """The exact shape of the live failure: a role provisioned before a
        permission existed, so the owner held everything *except* it."""
        organization, roles = tenancy
        owner = roles["Owner"]
        RolePermission.objects.filter(
            role=owner, codename=PERM.PROJECT_VIEW_MARGIN
        ).delete()
        assert PERM.PROJECT_VIEW_MARGIN not in codenames(owner)

        granted = sync_seeded_roles(organization)

        assert PERM.PROJECT_VIEW_MARGIN in codenames(owner)
        assert PERM.PROJECT_VIEW_MARGIN in granted["Owner"]

    def test_the_owner_ends_up_holding_everything(self, tenancy):
        organization, roles = tenancy
        RolePermission.objects.filter(role=roles["Owner"]).delete()

        sync_seeded_roles(organization)

        assert codenames(roles["Owner"]) == set(ALL_CODENAMES)

    def test_every_seeded_role_is_topped_up(self, tenancy):
        organization, roles = tenancy
        for role in roles.values():
            RolePermission.objects.filter(role=role).delete()

        sync_seeded_roles(organization)

        for name, expected in DEFAULT_ROLES.items():
            assert codenames(roles[name]) == set(expected), name

    def test_running_it_twice_grants_nothing_the_second_time(self, tenancy):
        organization, _ = tenancy
        sync_seeded_roles(organization)

        assert sync_seeded_roles(organization) == {}


@pytest.mark.django_db
class TestItDoesNotUndoWhatATenantChose:
    def test_a_permission_the_tenant_added_survives(self, tenancy):
        """B4 lets a tenant edit its roles. A backfill that reset them would
        trade a silent bug for a loud one."""
        organization, roles = tenancy
        technician = roles["Technician"]
        RolePermission.objects.create(
            organization=organization,
            role=technician,
            codename=PERM.STOCK_ADJUST,
        )

        sync_seeded_roles(organization)

        assert PERM.STOCK_ADJUST in codenames(technician)

    def test_a_role_the_tenant_made_is_left_alone(self, tenancy):
        """We have no default for it, and inventing one would be worse than
        leaving it as the tenant built it."""
        organization, _ = tenancy
        theirs = Role.objects.create(
            organization=organization, name="Yard supervisor", is_system=False
        )
        RolePermission.objects.create(
            organization=organization, role=theirs, codename=PERM.GATE_IN_POST
        )

        sync_seeded_roles(organization)

        assert codenames(theirs) == {PERM.GATE_IN_POST}

    def test_a_renamed_seeded_role_is_left_alone(self, tenancy):
        organization, roles = tenancy
        owner = roles["Owner"]
        owner.name = "Principal"
        owner.save()
        RolePermission.objects.filter(role=owner).delete()

        sync_seeded_roles(organization)

        assert codenames(owner) == set()
