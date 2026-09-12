"""Recompute balances from the ledger and report drift (design §3.3, §13; T3.6).

Run nightly by Celery beat. **Reports; never corrects.** If the cache and the
ledger disagree, something wrote a balance outside ``post_movement`` — rewriting
the cache would hide that and let the same cause corrupt the next figure too.

    python manage.py verify_ledger              # every organization
    python manage.py verify_ledger --org <uuid>
"""

from django.core.management.base import BaseCommand, CommandError

from core.models import Organization
from core.tenancy import tenant_context
from stock.verification import verify_ledger


class Command(BaseCommand):
    help = "Verify cached stock balances against the ledger. Reports drift, never corrects it."

    def add_arguments(self, parser):  # type: ignore[no-untyped-def]
        parser.add_argument("--org", dest="organization", default=None)

    def handle(self, *args, **options):  # type: ignore[no-untyped-def]
        target = options["organization"]

        if target:
            organizations = list(Organization.objects.filter(pk=target))
            if not organizations:
                raise CommandError(f"No organization with id {target}.")
        else:
            organizations = list(Organization.objects.all())

        total_drifts = 0

        for organization in organizations:
            with tenant_context(organization):
                result = verify_ledger(organization.pk)

            total_drifts += len(result.drifts)
            headline = f"{organization.name}: {result.summary()}"

            if result.ok:
                self.stdout.write(self.style.SUCCESS(headline))
                continue

            self.stdout.write(self.style.ERROR(headline))
            for drift in result.drifts:
                self.stdout.write(f"    {drift}")

        if total_drifts:
            # A non-zero exit, so a scheduler or CI treats drift as a failure
            # rather than a line in a log nobody reads.
            raise CommandError(
                f"{total_drifts} disagreement(s) between the balance cache and the "
                f"ledger. Investigate before correcting anything: the ledger is the "
                f"source of truth, and the cause matters more than the symptom."
            )
