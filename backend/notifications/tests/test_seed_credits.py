"""What a new tenant can send on its first day (L4, B2).

Metering granted an opening balance to every organization that predated it, so
nobody's messages stopped without warning. Tenants provisioned afterwards got
nothing, so the first thing a new organization does — invite its owner — could
not go by SMS. That is a dead end for a technician who has a phone and no email.
"""

from __future__ import annotations

import pytest

from core.provisioning import provision_tenant, registered_seeders
from notifications import credits
from notifications.seed import OPENING_BALANCE

pytestmark = pytest.mark.django_db


def test_a_new_tenant_can_send_a_message(db, settings):
    settings.TENANT_BASE_DOMAIN = "localhost"
    result = provision_tenant(
        name="Silvertech", slug="credits-silvertech", owner_email="owner@silvertech.co.ke"
    )

    assert credits.balance(result["organization"]) == OPENING_BALANCE


def test_the_grant_is_on_the_ledger_not_just_the_cache(db, settings):
    """The balance is a cache; the entries are the truth. A number with no
    entry behind it is a number nobody can explain later."""
    settings.TENANT_BASE_DOMAIN = "localhost"
    result = provision_tenant(
        name="Silvertech", slug="credits-ledger", owner_email="owner@silvertech.co.ke"
    )

    assert credits.ledger_balance(result["organization"]) == OPENING_BALANCE


def test_the_seeder_is_registered():
    assert "opening sms credits" in registered_seeders()
