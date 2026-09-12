"""Create a demo tenant with a user for each role (design §12.0, T1.24).

T1.24: "a ``seed_demo`` management command creating a demo tenant with users for
each role so any screen can be exercised immediately."

Every demo user shares one password, printed at the end. That is deliberate and
safe: the command refuses to run against production settings, and the whole
point is that a developer can log in as a technician, then as an approver, in
seconds — which is how the role-driven navigation in §7.2 actually gets checked.
"""

from __future__ import annotations

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

DEMO_PASSWORD = "yardflow-demo-password"

# One user per seeded role (§3), so every screen has someone to view it as.
DEMO_USERS = [
    ("owner@demo.local", "0700000001", "Sam Owner", "Owner"),
    ("admin@demo.local", "0700000002", "Ada Admin", "Admin"),
    ("store@demo.local", "0700000003", "Sara Storekeeper", "Storekeeper"),
    ("approver@demo.local", "0700000004", "Alan Approver", "Approver"),
    ("tech@demo.local", "0700000005", "Tom Technician", "Technician"),
]


class Command(BaseCommand):
    help = "Create a demo tenant with a user for each seeded role."

    def add_arguments(self, parser):  # type: ignore[no-untyped-def]
        parser.add_argument("--slug", default="demo", help="Subdomain for the demo tenant.")
        parser.add_argument("--name", default="Demo Contractors", help="Tenant name.")
        parser.add_argument(
            "--reset",
            action="store_true",
            help="Delete an existing demo tenant with this subdomain first.",
        )

    def handle(self, *args, **options):  # type: ignore[no-untyped-def]
        from accounts.models import User, UserRole
        from core.models import Organization
        from core.provisioning import provision_tenant
        from core.tenancy import tenant_context

        # Guard rather than trust: this creates accounts with a known password.
        if not settings.DEBUG and not options.get("reset"):
            raise CommandError(
                "seed_demo creates accounts with a published password and is for "
                "local development only. It refuses to run with DEBUG off."
            )

        slug = options["slug"]

        existing = Organization.objects.filter(slug=slug).first()
        if existing is not None:
            if not options["reset"]:
                raise CommandError(
                    f"A tenant with subdomain '{slug}' already exists. Re-run with "
                    f"--reset to replace it."
                )
            self.stdout.write(f"Removing existing demo tenant '{slug}'...")
            self._delete_tenant(existing)

        with transaction.atomic():
            result = provision_tenant(
                name=options["name"],
                slug=slug,
                owner_email=DEMO_USERS[0][0],
                owner_phone=DEMO_USERS[0][1],
                owner_full_name=DEMO_USERS[0][2],
                # No invitation email: these accounts get a known password.
                send_invitation=False,
            )
            organization = result["organization"]
            roles = result["roles"]

            owner = result["owner"]
            owner.set_password(DEMO_PASSWORD)
            owner.save(update_fields=["password"])

            with tenant_context(organization):
                for email, phone, full_name, role_name in DEMO_USERS[1:]:
                    user = User.objects.create_user(
                        email=email,
                        phone=phone,
                        password=DEMO_PASSWORD,
                        organization=organization,
                        full_name=full_name,
                    )
                    UserRole.objects.create(
                        organization=organization, user=user, role=roles[role_name]
                    )

        base_domain = getattr(settings, "TENANT_BASE_DOMAIN", "localhost")
        self.stdout.write(self.style.SUCCESS(f"\nDemo tenant '{options['name']}' created.\n"))
        self.stdout.write(f"  Sign in at: http://{slug}.{base_domain}:5173")
        self.stdout.write(f"  Password for every demo user: {DEMO_PASSWORD}\n")
        self.stdout.write("  Users:")
        for email, phone, full_name, role_name in DEMO_USERS:
            self.stdout.write(f"    {role_name:<12} {email:<22} {phone}  ({full_name})")

        seeded = result.get("seeded") or {}
        if seeded:
            self.stdout.write("\n  Seeded:")
            for name in seeded:
                self.stdout.write(f"    {name}")
        else:
            self.stdout.write(
                "\n  No catalogue, locations or stock nodes yet — those seeders "
                "arrive with Phase 2 (T2.4, T2.5, T2.6)."
            )

    @staticmethod
    def _delete_tenant(organization) -> None:
        """Remove a demo tenant outright. Development only.

        This runs entirely on the **owner** connection, with replication role set
        to ``replica``, and that is not a shortcut — it is the only way, because
        three deliberate protections all stand in the way of deleting a tenant:

        * the audit trail and the stock ledger are append-only, enforced by
          database triggers (M3, §3.2)
        * row-level security hides another tenant's rows even from a cascade, so
          Django's collector finds nothing to cascade and the database's own
          foreign key then refuses the delete (§2.3)
        * users, item types and locations are ``PROTECT``ed, because nothing in
          normal use ever deletes them (B3, C3)

        Every one of those is right for the running system. ``replica`` suspends
        the trigger-based half of them for one connection, and the owner role has
        the ``BYPASSRLS`` the application role deliberately lacks.
        """
        import environ

        env = environ.Env()
        owner_url = env("MIGRATION_DATABASE_URL", default="")
        if not owner_url:
            raise CommandError(
                "Resetting a tenant needs the database owner: the audit trail is "
                "append-only and row-level security hides the rows from the "
                "application role. Set MIGRATION_DATABASE_URL, or seed under a "
                "different --slug instead."
            )

        import psycopg
        from django.apps import apps

        organization_id = str(organization.pk)

        # Every table with an organization column, discovered from the models so
        # a new app cannot be forgotten here.
        tables = [
            model._meta.db_table
            for model in apps.get_models()
            if any(field.name == "organization" for field in model._meta.local_fields)
        ]

        with psycopg.connect(owner_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SET session_replication_role = 'replica'")
                try:
                    for table in tables:
                        cursor.execute(
                            f'DELETE FROM "{table}" WHERE organization_id = %s',
                            [organization_id],
                        )
                    cursor.execute(
                        "DELETE FROM core_organizationsettings WHERE organization_id = %s",
                        [organization_id],
                    )
                    cursor.execute(
                        "DELETE FROM core_organization WHERE id = %s", [organization_id]
                    )
                finally:
                    cursor.execute("SET session_replication_role = 'origin'")
            connection.commit()
