"""Top up seeded roles after a permission is added to the registry (§4.2, B4).

The data migration handles the tenants that exist at deploy time. This is for
afterwards: add a permission to `DEFAULT_ROLES`, run this, and every tenant's
seeded roles pick it up. Without it the next permission repeats the bug this
was written to fix — silently, on exactly the screens that check for it.
"""

from django.core.management.base import BaseCommand

from accounts.role_sync import sync_seeded_roles
from core.models import Organization


class Command(BaseCommand):
    help = "Grant seeded roles any default permission they are missing."

    def add_arguments(self, parser):  # type: ignore[no-untyped-def]
        parser.add_argument(
            "--tenant",
            default="",
            help="Subdomain of one tenant. Omitted, every tenant is synced.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would be granted, change nothing.",
        )

    def handle(self, *args, **options):  # type: ignore[no-untyped-def]
        from django.db import transaction

        organizations = Organization.objects.all()
        if options["tenant"]:
            organizations = organizations.filter(slug=options["tenant"])
            if not organizations.exists():
                self.stderr.write(f"No tenant with subdomain '{options['tenant']}'.")
                return

        total = 0
        for organization in organizations:
            with transaction.atomic():
                granted = sync_seeded_roles(organization)
                if options["dry_run"]:
                    transaction.set_rollback(True)

            if not granted:
                continue
            total += sum(len(codenames) for codenames in granted.values())
            self.stdout.write(f"{organization.slug}:")
            for role_name, codenames in sorted(granted.items()):
                self.stdout.write(f"    {role_name:<16} + {', '.join(codenames)}")

        if total == 0:
            self.stdout.write(self.style.SUCCESS("Every seeded role is current."))
        else:
            verb = "would be granted" if options["dry_run"] else "granted"
            self.stdout.write(self.style.SUCCESS(f"\n{total} permission(s) {verb}."))
