"""T17.9 — the vehicle expiry sweep (§4.20.4; R14).

The 30-day edge, one alert only, catch-up after a missed day, renewal re-arm,
closed assets skipped, and one failing step not stopping the others.
"""

from datetime import timedelta

import pytest
from django.utils import timezone

from assets.models import Asset, AssetCloseReason, AssetStatus, AssetType
from core import sweeps
from core.tenancy import tenant_context
from notifications.matrix import Event
from notifications.models import NotificationEvent

pytestmark = pytest.mark.django_db


def today():
    return timezone.localdate()


def vehicle(tenant, **overrides):
    values = {
        "organization": tenant,
        "type": AssetType.VEHICLE,
        "name": "Hilux",
        "tag": "KDA 123A",
    }
    values.update(overrides)
    return Asset.objects.create(**values)


def alerts():
    return NotificationEvent.objects.filter(event_key=Event.ASSET_EXPIRY_DUE)


def run(tenant):
    return sweeps._sweep_asset_expiries(tenant.pk)


def test_alerts_on_the_thirtieth_day_and_not_the_thirty_first(tenant):
    due = vehicle(tenant, insurance_expires_on=today() + timedelta(days=30))
    vehicle(tenant, name="Later", tag="KDB 1", insurance_expires_on=today() + timedelta(days=31))

    assert run(tenant) == 1
    [event] = alerts()
    assert event.target_id == str(due.pk)
    assert event.payload["document"] == "Insurance"
    assert event.payload["days_left"] == 30
    assert event.payload["expired"] is False
    due.refresh_from_db()
    assert due.insurance_alerted_for == due.insurance_expires_on


def test_the_two_documents_alert_separately(tenant):
    vehicle(
        tenant,
        insurance_expires_on=today() + timedelta(days=10),
        inspection_expires_on=today() + timedelta(days=5),
    )

    assert run(tenant) == 2
    assert {e.payload["document"] for e in alerts()} == {"Insurance", "Inspection"}


def test_it_alerts_once_however_often_it_runs(tenant):
    vehicle(tenant, insurance_expires_on=today() + timedelta(days=20))

    assert run(tenant) == 1
    assert run(tenant) == 0
    assert run(tenant) == 0
    assert alerts().count() == 1


def test_a_missed_run_catches_up_and_a_lapsed_date_alerts_once_as_expired(tenant):
    # The beat never ran on day 30, 29 ... or the date has already passed.
    asset = vehicle(tenant, insurance_expires_on=today() - timedelta(days=3))

    assert run(tenant) == 1
    [event] = alerts()
    assert event.payload["expired"] is True
    assert event.payload["days_left"] == 0
    assert run(tenant) == 0
    asset.refresh_from_db()
    assert asset.insurance_alerted_for == today() - timedelta(days=3)


def test_a_renewal_re_arms_it(tenant):
    asset = vehicle(tenant, insurance_expires_on=today() + timedelta(days=10))
    run(tenant)

    asset.insurance_expires_on = today() + timedelta(days=370)
    asset.save()
    assert run(tenant) == 0  # renewed, nothing due

    asset.insurance_expires_on = today() + timedelta(days=25)
    asset.save()
    assert run(tenant) == 1  # the new date alerts in its turn
    assert alerts().count() == 2


def test_a_closed_vehicle_is_ignored(tenant):
    vehicle(
        tenant,
        insurance_expires_on=today() + timedelta(days=5),
        status=AssetStatus.CLOSED,
        closed_on=today(),
        closed_reason=AssetCloseReason.SOLD,
    )

    assert run(tenant) == 0
    assert not alerts().exists()


def test_other_types_and_undated_vehicles_are_ignored(tenant):
    vehicle(tenant)
    Asset.objects.create(
        organization=tenant, type=AssetType.GENERATOR, name="Gen", tag="G-1"
    )

    assert run(tenant) == 0


def test_it_does_not_cross_tenants(tenant, other_organization):
    with tenant_context(other_organization):
        theirs = vehicle(
            other_organization, insurance_expires_on=today() + timedelta(days=3)
        )
    vehicle(tenant)

    assert run(tenant) == 0
    with tenant_context(other_organization):
        assert Asset.objects.get(pk=theirs.pk).insurance_alerted_for is None
        assert not alerts().exists()


def test_sweep_tenant_runs_it_and_a_failing_step_does_not_stop_the_others(tenant, monkeypatch):
    vehicle(tenant, insurance_expires_on=today() + timedelta(days=2))

    def boom(organization_id):
        raise RuntimeError("beat blew up")

    monkeypatch.setattr(sweeps, "_sweep_custody_overdue", boom)

    result = sweeps.sweep_tenant(organization_id=str(tenant.pk))

    assert result["custody_overdue"] == "failed"
    assert result["asset_expiries"] == 1
    assert result["expired_gate_passes"] != "failed"


def test_a_failing_expiry_step_does_not_stop_the_other_sweeps(tenant, monkeypatch):
    def boom(organization_id):
        raise RuntimeError("expiry blew up")

    monkeypatch.setattr(sweeps, "_sweep_asset_expiries", boom)

    result = sweeps.sweep_tenant(organization_id=str(tenant.pk))

    assert result["asset_expiries"] == "failed"
    assert result["unacknowledged_returns"] != "failed"
    assert result["custody_overdue"] != "failed"
