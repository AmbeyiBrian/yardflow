"""T14.9 — correcting serials saved as raw ISO 15434 label text (§7.3d; E9)."""

from io import StringIO

import pytest
from django.core.management import call_command

from catalogue.factories import ItemTypeFactory
from catalogue.models import TrackingMode
from core.factories import OrganizationFactory
from core.models import AuditAction, AuditLog
from core.tenancy import tenant_context
from locations.factories import YardFactory
from receiving.models import GateInSerial
from receiving.tests import test_gate_in as base
from receiving.tests.test_gate_in_boxes import add_serialized_line
from stock.factories import SerialUnitFactory
from stock.models import SerialUnit

RS, GS, EOT = "\x1e", "\x1d", "\x04"


def label(*fields: str) -> str:
    return f"[)>{RS}06{GS}" + GS.join(fields) + RS + EOT


def run(*args: str) -> str:
    out = StringIO()
    call_command("correct_label_serials", *args, stdout=out)
    return out.getvalue()


@pytest.fixture
def yard(tenant):
    return YardFactory(name="Main yard")


@pytest.fixture
def unit(tenant, yard):
    """A unit and its gate-in serial, both holding the raw label."""
    raw = label("P02312CRW", "S2641098217", "1P12345")
    item = ItemTypeFactory(default_tracking_mode=TrackingMode.SERIALIZED)
    unit = SerialUnitFactory(item_type=item, serial_number=raw, current_node=yard.node)
    line = add_serialized_line(base.draft(tenant, yard), item, [])
    GateInSerial.objects.create(organization=tenant, line=line, serial_number=raw)
    return unit


def test_dry_run_prints_old_and_new_and_changes_nothing(tenant, unit):
    raw = unit.serial_number

    output = run()

    assert repr(raw) in output
    assert "2641098217" in output
    assert "Dry run" in output
    with tenant_context(tenant):
        unit.refresh_from_db()
        assert unit.serial_number == raw
        assert GateInSerial.objects.filter(serial_number=raw).count() == 1
        assert not AuditLog.objects.filter(action=AuditAction.SERIAL_CORRECTED).exists()


def test_apply_renames_the_unit_its_gate_in_serial_and_audits(tenant, unit):
    raw = unit.serial_number

    output = run("--apply")

    assert "Renamed 1 unit(s)" in output
    with tenant_context(tenant):
        unit.refresh_from_db()
        assert unit.serial_number == "2641098217"
        assert GateInSerial.objects.get().serial_number == "2641098217"
        entry = AuditLog.objects.get(action=AuditAction.SERIAL_CORRECTED)
        assert entry.target_id == str(unit.pk)
        assert entry.before == {"serial_number": raw}
        assert entry.after == {"serial_number": "2641098217"}
        assert "E9" in entry.note


def test_a_second_apply_is_a_no_op(tenant, unit):
    run("--apply")

    output = run("--apply")

    assert "Renamed 0 unit(s)" in output
    with tenant_context(tenant):
        assert AuditLog.objects.filter(action=AuditAction.SERIAL_CORRECTED).count() == 1


def test_a_collision_is_skipped_and_reported(tenant, yard):
    existing = SerialUnitFactory(serial_number="2641098217", current_node=yard.node)
    raw = label("S2641098217")
    clashing = SerialUnitFactory(serial_number=raw, current_node=yard.node)

    output = run("--apply")

    assert "already belongs to another unit" in output
    with tenant_context(tenant):
        clashing.refresh_from_db()
        existing.refresh_from_db()
        assert clashing.serial_number == raw
        assert existing.serial_number == "2641098217"


def test_two_labels_for_one_serial_rename_only_the_first(tenant, yard):
    SerialUnitFactory(serial_number=label("S77", "1P1"), current_node=yard.node)
    SerialUnitFactory(serial_number=label("S77", "1P2"), current_node=yard.node)

    output = run("--apply")

    assert "Renamed 1 unit(s); skipped 1" in output
    with tenant_context(tenant):
        assert SerialUnit.objects.filter(serial_number="77").count() == 1


def test_more_than_one_serial_and_no_serial_are_skipped(tenant, yard):
    two = SerialUnitFactory(serial_number=label("SA1", "SB2"), current_node=yard.node)
    none = SerialUnitFactory(serial_number=label("P02312CRW", "1P12345"), current_node=yard.node)

    output = run("--apply")

    assert "more than one serial" in output
    assert "skipped: no serial found" in output
    assert "Renamed 0 unit(s); skipped 2" in output
    with tenant_context(tenant):
        for unit in (two, none):
            raw = unit.serial_number
            unit.refresh_from_db()
            assert unit.serial_number == raw


def test_org_limits_the_run_to_one_organization(tenant, yard):
    mine = SerialUnitFactory(serial_number=label("S111"), current_node=yard.node)
    other = OrganizationFactory(name="Rival Contractors", slug="rival")
    with tenant_context(other):
        rival_yard = YardFactory(name="Rival yard")
        theirs = SerialUnitFactory(serial_number=label("S222"), current_node=rival_yard.node)

    run("--apply", "--org", tenant.slug)

    with tenant_context(tenant):
        mine.refresh_from_db()
        assert mine.serial_number == "111"
    with tenant_context(other):
        theirs.refresh_from_db()
        assert theirs.serial_number == label("S222")
