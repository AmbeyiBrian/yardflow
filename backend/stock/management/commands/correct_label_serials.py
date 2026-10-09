"""Rename serials that were saved as a whole scanned label (E9, design §7.3d; T14.9).

Before ``read_label`` understood ISO 15434 (T14.1), a Huawei 2D label scanned into a
gate-in was kept verbatim, so the unit's serial is the entire ``[)>`` envelope and a
lookup of the real serial finds nothing. This reads each such serial again and renames
the unit to the one serial inside it.

    python manage.py correct_label_serials                  # dry run, every organization
    python manage.py correct_label_serials --org <slug>
    python manage.py correct_label_serials --apply

A dry run is the default: renaming changes what people search by, so the owner reviews
the list first. The ledger is untouched, because movements point at the unit, not at
its text.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from core.audit import record_system
from core.models import AuditAction, Organization
from core.tenancy import tenant_context
from receiving.models import GateInSerial
from stock.labels import read_label
from stock.models import SerialUnit

ENVELOPE_START = "[)>"


@dataclass(frozen=True)
class Rename:
    unit: SerialUnit
    new: str


class Command(BaseCommand):
    help = "Rename serials saved as raw ISO 15434 label text to the serial inside (E9)."

    def add_arguments(self, parser):  # type: ignore[no-untyped-def]
        parser.add_argument("--org", dest="organization", default=None, help="Organization slug.")
        parser.add_argument(
            "--apply", action="store_true", help="Make the changes. Without it, only report."
        )

    def handle(self, *args, **options):  # type: ignore[no-untyped-def]
        slug = options["organization"]
        apply = options["apply"]

        organizations = Organization.objects.all()
        if slug:
            organizations = organizations.filter(slug=slug)
            if not organizations.exists():
                raise CommandError(f"No organization with slug {slug}.")

        totals: Counter[str] = Counter()
        for organization in organizations.order_by("slug"):
            # One transaction around the scan so the tenant is published to Postgres
            # (RLS) for the reads; each rename is its own savepoint inside it.
            with transaction.atomic(), tenant_context(organization):
                for rename in self._plan(organization, totals):
                    self.stdout.write(
                        f"{organization.slug}  {rename.unit.pk}  {rename.unit.item_type}  "
                        f"{rename.unit.serial_number!r}  ->  {rename.new}"
                    )
                    if apply:
                        with transaction.atomic():
                            self._rename(organization, rename)
                        totals["renamed"] += 1

        verb = "Renamed" if apply else "Would rename"
        count = totals["renamed"] if apply else totals["candidates"]
        self.stdout.write(
            f"{verb} {count} unit(s); skipped {totals['skipped']}."
            + ("" if apply else " Dry run: nothing changed. Re-run with --apply.")
        )

    def _plan(self, organization: Organization, totals: Counter[str]) -> list[Rename]:
        units = SerialUnit.objects.filter(serial_number__startswith=ENVELOPE_START).order_by(
            "serial_number", "pk"
        )
        taken = set(SerialUnit.objects.values_list("serial_number", flat=True))
        candidates: list[Rename] = []
        for unit in units:
            serials = read_label(unit.serial_number).serials
            if not serials or serials == (unit.serial_number,):
                reason = "no serial found in the label"
            elif len(serials) > 1:
                reason = f"more than one serial ({', '.join(serials)})"
            elif serials[0] in taken:
                reason = f"{serials[0]} already belongs to another unit"
            else:
                # Claimed now, so two labels holding the same serial cannot both rename.
                taken.add(serials[0])
                candidates.append(Rename(unit, serials[0]))
                totals["candidates"] += 1
                continue
            totals["skipped"] += 1
            self.stdout.write(
                self.style.WARNING(
                    f"{organization.slug}  {unit.pk}  {unit.item_type}  "
                    f"{unit.serial_number!r}  skipped: {reason}"
                )
            )
        return candidates

    def _rename(self, organization: Organization, rename: Rename) -> None:
        unit, old = rename.unit, rename.unit.serial_number
        unit.serial_number = rename.new
        unit.save(update_fields=["serial_number", "updated_at"])
        # The receipt's own copy, so the gate-in still shows what the unit is called.
        GateInSerial.objects.filter(serial_number=old).update(serial_number=rename.new)
        record_system(
            AuditAction.SERIAL_CORRECTED,
            organization=organization,
            target=unit,
            before={"serial_number": old},
            after={"serial_number": rename.new},
            note="correct_label_serials: serial was saved as raw label text (E9).",
        )
