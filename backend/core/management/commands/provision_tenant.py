"""Create a real tenant, from the command line (A1, D7; design §12.2).

`seed_demo` is the development counterpart and refuses to run in production,
because every account it makes shares a password written in the repository. This
is the other one: it creates an organization and invites its owner, and there is
never a password to intercept — `provision_tenant` gives the owner an unusable
one and sends them a link to set their own (A1).

The platform admin console does the same thing over HTTP (D7). This exists for
the first tenant on a box, when there is no platform admin yet to log in as.

    python manage.py provision_tenant \\
        --name "Silvertech Networks Limited" \\
        --slug silvertech \\
        --owner-email owner@silvertech.co.ke

**The link is printed as well as sent.** Before SES is out of its sandbox, mail
to an unverified address is accepted and discarded — the command would report
success and the owner would never hear anything. Printing it means the operator
running this can always finish the job.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction


class Command(BaseCommand):
    help = "Create an organization and invite its owner to set a password."

    def add_arguments(self, parser):  # type: ignore[no-untyped-def]
        parser.add_argument("--name", required=True, help="The company's registered name.")
        parser.add_argument(
            "--slug",
            required=True,
            help="Subdomain. The tenant is reached at <slug>.<TENANT_BASE_DOMAIN>.",
        )
        parser.add_argument("--owner-email", default="", help="Where the invitation goes.")
        parser.add_argument(
            "--owner-phone",
            default="",
            help="Used instead of email where the owner has no mailbox (B1).",
        )
        parser.add_argument("--owner-name", default="", help="The owner's full name.")
        parser.add_argument(
            "--no-invitation",
            action="store_true",
            help=(
                "Create the owner without sending anything. The link is still "
                "printed, which is the usable half before mail is configured."
            ),
        )

    def handle(self, *args, **options):  # type: ignore[no-untyped-def]
        from django.conf import settings

        from accounts.reset import make_reset_token, reset_url
        from core.models import Organization
        from core.provisioning import provision_tenant

        slug = options["slug"].strip().lower()
        email = options["owner_email"].strip()
        phone = options["owner_phone"].strip()

        if not email and not phone:
            raise CommandError(
                "An owner needs an email address or a phone number to be invited (A1, B1)."
            )

        # Checked before rather than caught after: a half-made tenant is worse
        # than a refused one, and the slug is what the subdomain resolves on.
        if Organization.objects.filter(slug=slug).exists():
            raise CommandError(
                f"A tenant with subdomain '{slug}' already exists. Subdomains are "
                f"the tenant's address and cannot be shared."
            )

        with transaction.atomic():
            result = provision_tenant(
                name=options["name"],
                slug=slug,
                owner_email=email or None,
                owner_phone=phone or None,
                owner_full_name=options["owner_name"],
                send_invitation=not options["no_invitation"],
            )

        organization = result["organization"]
        owner = result["owner"]

        base = getattr(settings, "TENANT_BASE_DOMAIN", "localhost")
        address = f"https://{slug}.{base}"

        # Generated fresh rather than captured from the invitation: the token is
        # single-use per password change, so printing the one already sent would
        # be fine, but re-deriving it keeps this correct if the invitation was
        # skipped entirely.
        link = reset_url(owner, make_reset_token(owner))

        self.stdout.write(self.style.SUCCESS(f"\nCreated {organization.name}"))
        self.stdout.write(f"  Address:  {address}")
        self.stdout.write(f"  Owner:    {owner.email or owner.phone}")
        self.stdout.write(f"  Roles:    {len(result['roles'])} seeded\n")

        if options["no_invitation"]:
            self.stdout.write("  No invitation was sent.")
        else:
            self.stdout.write("  An invitation has been sent.")

        self.stdout.write(
            "\n  Set the owner's password with this link. It expires, and it is\n"
            "  the only thing here worth keeping secret:\n"
        )
        self.stdout.write(f"  {link}\n")
