import pytest

from accounts.factories import RoleFactory, UserFactory, UserRoleFactory
from accounts.permissions_registry import PERM


@pytest.fixture
def finance_user(tenant):
    """Someone holding ``finance.approve``: the second level, and the person who
    makes ``record_*`` possible at all (it refuses when nobody else could approve)."""
    role = RoleFactory(name="Finance", codenames=[PERM.FINANCE_APPROVE])
    user = UserFactory(organization=tenant, full_name="Fiona Finance")
    UserRoleFactory(user=user, role=role)
    return user
