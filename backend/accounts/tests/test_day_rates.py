"""T10.12 — day rates, and who may see them (§4.14; O15, O14).

A rate here is a **costing** figure, not pay: nothing in this system works out
what anybody is owed. But an individual rate is close enough to pay that O14
puts it behind its own permission, and the device most likely to be handed
around a yard is the one it would leak on.

The resolution order is the other half. Their own rate, then their role's, then
**nothing** — never zero. A job whose labour costs nothing reads as a job
delivered for free, which is exactly the flattery O12 exists to prevent.
"""

from decimal import Decimal

import pytest

from accounts.factories import RoleFactory, UserFactory
from accounts.permissions_registry import PERM
from accounts.services import day_rate_for


@pytest.mark.django_db
class TestResolution:
    def test_their_own_rate_wins(self, tenant):
        user = UserFactory(organization=tenant, day_rate=Decimal("3500.00"))
        user.user_roles.create(
            organization=tenant, role=RoleFactory(day_rate=Decimal("1000.00"))
        )

        assert day_rate_for(user) == (Decimal("3500.00"), "USER")

    def test_the_role_rate_is_the_fallback(self, tenant):
        user = UserFactory(organization=tenant, day_rate=None)
        user.user_roles.create(
            organization=tenant, role=RoleFactory(day_rate=Decimal("1800.00"))
        )

        assert day_rate_for(user) == (Decimal("1800.00"), "ROLE")

    def test_the_highest_role_rate_wins(self, tenant):
        """Otherwise the figure depends on the order rows happened to be made."""
        user = UserFactory(organization=tenant, day_rate=None)
        user.user_roles.create(
            organization=tenant,
            role=RoleFactory(name="Technician", day_rate=Decimal("1800.00")),
        )
        user.user_roles.create(
            organization=tenant,
            role=RoleFactory(name="Supervisor", day_rate=Decimal("2600.00")),
        )

        assert day_rate_for(user) == (Decimal("2600.00"), "ROLE")

    def test_no_rate_anywhere_is_none_not_zero(self, tenant):
        """O15: uncosted, which the report can say. Zero reads as free."""
        user = UserFactory(organization=tenant, day_rate=None)
        user.user_roles.create(organization=tenant, role=RoleFactory(day_rate=None))

        assert day_rate_for(user) == (None, "NONE")


@pytest.mark.django_db
class TestWhoMaySeeARate:
    def serialize_as(self, tenant, viewer, subject, rf):
        from accounts.admin_api import UserSerializer

        request = rf.get("/api/v1/users")
        request.user = viewer
        return UserSerializer(subject, context={"request": request}).data

    def test_a_holder_of_the_permission_sees_it(self, tenant, rf):
        subject = UserFactory(organization=tenant, day_rate=Decimal("3500.00"))
        viewer = UserFactory(organization=tenant)
        viewer.user_roles.create(
            organization=tenant,
            role=RoleFactory(codenames=[PERM.PROJECT_VIEW_RATES]),
        )

        assert self.serialize_as(tenant, viewer, subject, rf)["day_rate"] == "3500.00"

    def test_everyone_else_gets_no_field_at_all(self, tenant, rf):
        """O14: absent, not null. Null would be a claim about the rate; absent
        is a claim about the reader, and only one of those is true."""
        subject = UserFactory(organization=tenant, day_rate=Decimal("3500.00"))
        viewer = UserFactory(organization=tenant)
        viewer.user_roles.create(organization=tenant, role=RoleFactory(codenames=[]))

        assert "day_rate" not in self.serialize_as(tenant, viewer, subject, rf)
