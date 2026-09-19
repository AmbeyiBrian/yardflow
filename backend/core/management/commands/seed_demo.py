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
    # O1: the seventh seeded role. Without someone holding it, every Epic O
    # screen is unreachable in the demo tenant.
    ("pm@demo.local", "0700000006", "Pippa Manager", "Project manager"),
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

                # C5, C6: somewhere to send material. Here rather than in the
                # provisioning seeders, because a real customer builds their own
                # site register and demo data has no business in one.
                from network.seeding import seed_demo_network

                seed_demo_network(organization)

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
        tenant_models = [
            model
            for model in apps.get_models()
            if any(field.name == "organization" for field in model._meta.local_fields)
        ]
        tables = [model._meta.db_table for model in tenant_models]

        # ...**and the join tables hanging off them**, which have no
        # organization column of their own and so never appeared in the list
        # above. With FK enforcement switched off below, their rows simply
        # outlived their parents: `network_workorder_sites` was carrying rows
        # pointing at work orders deleted by an earlier reset, and nothing
        # noticed until a migration tried to rebuild the constraint.
        #
        # Deleted first, because a join row is meaningless once either end is
        # gone and the parent delete is what would orphan it.
        def rows_for_this_tenant(table: str, model) -> str:
            """A join table is scoped through the model that owns it."""
            return (
                f'DELETE FROM "{table}" WHERE {model._meta.model_name}_id IN '
                f'(SELECT id FROM "{model._meta.db_table}" WHERE organization_id = %s)'
            )

        join_deletes = []
        for model in tenant_models:
            for field in model._meta.local_many_to_many:
                through = field.remote_field.through
                # `through` is only ever None on an unresolved lazy reference,
                # which cannot happen once the app registry is ready.
                if through is not None and through._meta.auto_created:
                    join_deletes.append(
                        rows_for_this_tenant(through._meta.db_table, model)
                    )

        with psycopg.connect(owner_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SET session_replication_role = 'replica'")
                try:
                    for statement in join_deletes:
                        cursor.execute(statement, [organization_id])
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
