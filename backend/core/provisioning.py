"""Tenant provisioning (design §4.1, §12; requirement A1).

A1: "create a tenant with a name, subdomain and initial owner account, so that a
new customer can start using the system." Creating a tenant seeds default roles,
a starter item catalogue, default settings and one yard.

Everything happens in **one transaction**. A half-provisioned tenant — roles but
no owner, or an owner who cannot log in — is worse than no tenant at all,
because the subdomain is then taken and immutable (A1).

Later phases need to seed more: the yard and its quarantine location and the
system stock nodes (T2.5, T2.6), and the starter catalogue (T2.4). Rather than
have this module import from apps that do not exist yet, those register
themselves through :func:`register_tenant_seeder`. Provisioning therefore stays
correct at every phase, seeding exactly what has been built.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from django.db import transaction

from core.models import Organization

logger = logging.getLogger(__name__)

#: Seeders contributed by later phases, run in registration order.
_SEEDERS: list[tuple[int, str, Callable]] = []


def register_tenant_seeder(func: Callable | None = None, *, order: int = 100, name: str = ""):
    """Register a callable run for every newly provisioned tenant.

    Called as ``register_tenant_seeder(seed_locations, order=10)`` from an app's
    ``ready()``. ``order`` sequences seeders that depend on each other — stock
    nodes need locations to exist first.
    """

    def decorate(target: Callable) -> Callable:
        label = name or getattr(target, "__name__", repr(target))
        if any(existing_name == label for _, existing_name, _ in _SEEDERS):
            return target  # idempotent: `ready()` can run more than once
        _SEEDERS.append((order, label, target))
        _SEEDERS.sort(key=lambda entry: (entry[0], entry[1]))
        return target

    return decorate if func is None else decorate(func)


def registered_seeders() -> list[str]:
    """Names of the registered seeders, for diagnostics and tests."""
    return [name for _, name, _ in _SEEDERS]


def seed_default_roles(organization: Organization) -> dict:
    """Create the seeded roles from §3 with their permissions (B4).

    Marked ``is_system`` so the UI can say where they came from. That does not
    make them permanent — B4 explicitly allows a tenant to rename or delete a
    role and define its own.
    """
    from accounts.models import Role
    from accounts.permissions_registry import DEFAULT_ROLES

    roles: dict[str, Role] = {}
    for role_name, codenames in DEFAULT_ROLES.items():
        role = Role.objects.create(
            organization=organization, name=role_name, is_system=True
        )
        role.set_permissions(codenames)
        roles[role_name] = role
    return roles


@transaction.atomic
def provision_tenant(
    *,
    name: str,
    slug: str,
    owner_email: str | None = None,
    owner_phone: str | None = None,
    owner_full_name: str = "",
    send_invitation: bool = True,
    request=None,
    **organization_fields,
) -> dict:
    """Create a usable tenant, and return what was created.

    The owner is created with an **unusable password** and invited to set one
    (A1). No temporary password is ever generated, so none can be intercepted or
    left unchanged.
    """
    from accounts.models import User, UserRole
    from accounts.reset import send_password_reset
    from core.audit import record
    from core.models import AuditAction
    from core.tenancy import tenant_context

    if not owner_email and not owner_phone:
        raise ValueError(
            "An owner needs an email address or a phone number to be invited (A1, B1)."
        )

    organization = Organization.objects.create(name=name, slug=slug, **organization_fields)
    # OrganizationSettings is created by signal, so its defaults are guaranteed
    # here (§4.1). The notification matrix is seeded from L2's default table,
    # which the tenant then edits — "so that people are not spammed" is a
    # per-company judgement.
    _seed_notification_settings(organization)

    with tenant_context(organization):
        roles = seed_default_roles(organization)

        owner = User.objects.create_user(
            email=owner_email,
            phone=owner_phone,
            password=None,  # invited, not issued a password
            organization=organization,
            full_name=owner_full_name,
        )
        UserRole.objects.create(
            organization=organization, user=owner, role=roles["Owner"]
        )

        seeded: dict[str, object] = {}
        for _order, seeder_name, seeder in _SEEDERS:
            try:
                seeded[seeder_name] = seeder(organization)
            except Exception:
                # A failed seeder must fail the whole provisioning: a tenant
                # missing its quarantine location or catalogue is not usable, and
                # the subdomain would be permanently taken (A1).
                logger.exception("tenant seeder %s failed", seeder_name)
                raise

        record(
            AuditAction.USER_CREATED,
            actor=None,
            organization=organization,
            target=owner,
            target_label=str(owner),
            request=request,
            note=f"Tenant '{name}' provisioned with owner {owner}.",
        )

    if send_invitation:
        send_password_reset(owner, request=request, is_invitation=True)

    return {
        "organization": organization,
        "settings": organization.settings,
        "owner": owner,
        "roles": roles,
        "seeded": seeded,
    }


def _seed_notification_settings(organization) -> None:
    """Seed the L2 default matrix and active channels (C8, L1, L2, Q1)."""
    from notifications.matrix import default_channels_config, default_matrix_config

    settings = organization.settings
    settings.notification_matrix = default_matrix_config()
    settings.notification_channels = default_channels_config()
    settings.save(update_fields=["notification_matrix", "notification_channels"])
