"""Keeping seeded roles current as the permission registry grows (§4.2; B4).

``DEFAULT_ROLES`` is applied **when a tenant is provisioned**. That was fine
while the registry was fixed, and quietly wrong the moment a permission was
added: every tenant provisioned before it kept the role it was given, and the
new permission reached nobody.

The failure is silent, which is what makes it worth a module. An owner whose
role says it holds everything, on a screen that renders only what they may see,
gets a page with the figures missing and no error anywhere — which is exactly
how Epic O's five permissions landed on existing tenants.

**Additive, never subtractive.** A tenant may edit its own roles (B4), and this
must not undo that. It grants what a seeded role is missing from its default and
leaves everything else alone — including permissions the tenant added itself.
"""

from __future__ import annotations


def sync_seeded_roles(organization, *, role_model=None, permission_model=None) -> dict:
    """Grant seeded roles any default permission they are missing.

    ``role_model`` and ``permission_model`` are injectable so a data migration
    can pass its historical models. Left out, the live ones are used.

    Returns ``{role name: [codenames granted]}`` for the caller to report — a
    backfill that says nothing is a backfill nobody can check.
    """
    from accounts.permissions_registry import DEFAULT_ROLES
    from core.tenancy import tenant_context

    if role_model is None:
        from accounts.models import Role as role_model
    if permission_model is None:
        from accounts.models import RolePermission as permission_model

    granted: dict[str, list[str]] = {}

    # Row-level security applies to writes as much as reads (§2.3), and this is
    # inherently a per-tenant operation — so it establishes the context rather
    # than leaving every caller to remember.
    with tenant_context(organization):
        granted = _grant_missing(organization, role_model, permission_model, DEFAULT_ROLES)
    return granted


def _grant_missing(organization, role_model, permission_model, defaults) -> dict:
    granted: dict[str, list[str]] = {}
    roles = role_model.objects.filter(organization=organization, is_system=True)
    for role in roles:
        expected = defaults.get(role.name)
        if expected is None:
            # A renamed or tenant-made role. We have no opinion about what it
            # should hold, and inventing one would be worse than leaving it.
            continue

        held = set(
            permission_model.objects.filter(role=role).values_list("codename", flat=True)
        )
        missing = [codename for codename in expected if codename not in held]
        if not missing:
            continue

        permission_model.objects.bulk_create(
            [
                permission_model(
                    organization_id=organization.pk, role=role, codename=codename
                )
                for codename in missing
            ]
        )
        granted[role.name] = missing

    return granted
