"""Shared pytest fixtures (design §14).

The default test client is always tenant-scoped. Cross-tenant access has to be
written out explicitly, so it is obvious when a test is doing it.
"""

from collections.abc import Iterator
from typing import cast

import pytest

from core.factories import OrganizationFactory
from core.models import Organization
from core.tenancy import tenant_context


@pytest.fixture
def organization(db) -> Organization:
    """The tenant under test. Silvertech, in the language of the requirements."""
    return cast(Organization, OrganizationFactory(name="Silvertech", slug="silvertech"))


@pytest.fixture
def other_organization(db) -> Organization:
    """A second, unrelated tenant.

    Every isolation assertion needs one of these: proving that tenant A cannot
    see tenant B is the whole of requirement A3.
    """
    return cast(Organization, OrganizationFactory(name="Rival Contractors", slug="rival"))


@pytest.fixture
def tenant(organization: Organization) -> Iterator[Organization]:
    """Run the test with ``organization`` active, as a request would."""
    with tenant_context(organization):
        yield organization
