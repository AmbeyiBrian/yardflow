"""The permission codename registry (design §4.2, requirement B4).

**Roles are data; permissions are code.** A tenant may invent any role it likes
and tick whichever permissions it wants — that is B4. But the *set* of things a
permission can authorise is fixed by what the software actually does, so the
codenames live here as constants rather than in a table. A codename in the
database that no code checks would be a silent lie.

Codenames are grouped by resource, dotted: ``gate_out.approve``.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PermissionSpec:
    """One thing a role may be allowed to do."""

    codename: str
    label: str
    group: str
    #: Why this permission exists as its own switch rather than being implied by
    #: another. Shown as help text on the role editor (T2.15).
    rationale: str = ""


# --------------------------------------------------------------------------
# Codename constants
# --------------------------------------------------------------------------
# Referenced from code as PERM.GATE_OUT_APPROVE rather than as a bare string, so
# a typo is an AttributeError instead of a permission that silently never
# matches.


class PERM:
    """Permission codenames. §4.2 lists the v1 set."""

    # Receiving
    GATE_IN_POST = "gate_in.post"

    # Dispatch — three separate permissions on purpose (§4.2, G1, Q7)
    GATE_OUT_REQUEST = "gate_out.request"
    GATE_OUT_APPROVE = "gate_out.approve"
    GATE_OUT_RELEASE = "gate_out.release"

    # Stock
    STOCK_ADJUST = "stock.adjust"
    CUSTODY_TRANSFER = "custody.transfer"

    # Jobs
    JOB_CLOSEOUT = "job.closeout"
    JOB_CLOSE_WITH_VARIANCE = "job.close_with_variance"

    # Disposition
    DISPOSAL_APPROVE = "disposal.approve"

    # Configuration
    CATALOGUE_MANAGE = "catalogue.manage"
    SETTINGS_MANAGE = "settings.manage"
    USERS_MANAGE = "users.manage"

    # Reporting
    REPORT_VIEW_ALL = "report.view_all"


ALL_PERMISSIONS: tuple[PermissionSpec, ...] = (
    PermissionSpec(
        PERM.GATE_IN_POST,
        "Post a gate-in",
        "Receiving",
        "Posting is what affects stock; drafts do not.",
    ),
    PermissionSpec(
        PERM.GATE_OUT_REQUEST,
        "Raise a gate-out request",
        "Dispatch",
        "Held by storekeepers and technicians alike.",
    ),
    PermissionSpec(
        PERM.GATE_OUT_APPROVE,
        "Approve a gate-out",
        "Dispatch",
        "Approval is the control the system exists to provide.",
    ),
    PermissionSpec(
        PERM.GATE_OUT_RELEASE,
        "Release a gate-out at the gate",
        "Dispatch",
        "Separate from approval so a dedicated gate guard is possible without a "
        "code change (G1, Q7).",
    ),
    PermissionSpec(
        PERM.STOCK_ADJUST,
        "Adjust stock after a count",
        "Stock",
        "Adjustments to client-owned stock always require approval regardless.",
    ),
    PermissionSpec(
        PERM.CUSTODY_TRANSFER,
        "Transfer custody between people",
        "Stock",
        "Requires acknowledgement by the receiver.",
    ),
    PermissionSpec(
        PERM.JOB_CLOSEOUT,
        "Close out a job",
        "Jobs",
        "Normally the assigned technician.",
    ),
    PermissionSpec(
        PERM.JOB_CLOSE_WITH_VARIANCE,
        "Close a job with material unaccounted for",
        "Jobs",
        "The override in H5. Deliberately rare, and always audited.",
    ),
    PermissionSpec(
        PERM.DISPOSAL_APPROVE,
        "Approve a disposal",
        "Disposition",
        "Client-owned disposals always require approval, whatever the rules say.",
    ),
    PermissionSpec(
        PERM.CATALOGUE_MANAGE,
        "Manage the catalogue and master data",
        "Configuration",
        "Categories, custom fields, item types, locations, clients, sites.",
    ),
    PermissionSpec(
        PERM.SETTINGS_MANAGE,
        "Manage organization settings",
        "Configuration",
        "Includes the approval behaviour switches, so it is close to owner-level.",
    ),
    PermissionSpec(
        PERM.USERS_MANAGE,
        "Manage users, roles and delegations",
        "Configuration",
        "Grants the ability to grant, so it is owner-level in practice.",
    ),
    PermissionSpec(
        PERM.REPORT_VIEW_ALL,
        "View all reports",
        "Reporting",
        "Without it a user sees only what their own work produced.",
    ),
)

PERMISSIONS_BY_CODENAME: dict[str, PermissionSpec] = {
    spec.codename: spec for spec in ALL_PERMISSIONS
}

ALL_CODENAMES: frozenset[str] = frozenset(PERMISSIONS_BY_CODENAME)

#: B4: "at least one active user must hold owner-level permissions at all times".
#: These two together are what the system treats as owner-level — the ability to
#: grant permissions, and the ability to approve. Losing the last holder would
#: leave a tenant unable to administer itself or release any material.
OWNER_LEVEL_PERMISSIONS: frozenset[str] = frozenset(
    {PERM.USERS_MANAGE, PERM.GATE_OUT_APPROVE}
)


def permission_groups() -> dict[str, list[PermissionSpec]]:
    """Permissions grouped for the role editor's permission matrix (T2.15)."""
    grouped: dict[str, list[PermissionSpec]] = {}
    for spec in ALL_PERMISSIONS:
        grouped.setdefault(spec.group, []).append(spec)
    return grouped


def validate_codename(codename: str) -> str:
    """Reject a codename no code checks.

    A role holding an unknown permission looks like protection and provides
    none, so this is a hard error rather than a warning.
    """
    if codename not in ALL_CODENAMES:
        raise ValueError(
            f"'{codename}' is not a known permission. Known codenames: "
            f"{', '.join(sorted(ALL_CODENAMES))}"
        )
    return codename


# --------------------------------------------------------------------------
# Seeded default roles (§3)
# --------------------------------------------------------------------------
# "The six above are seeded defaults. A tenant may create, rename or delete
# roles and assign granular permissions to them." Platform admin is not a tenant
# role, so five roles are seeded per organization.

DEFAULT_ROLES: dict[str, tuple[str, ...]] = {
    "Owner": tuple(ALL_CODENAMES),
    "Admin": (
        PERM.USERS_MANAGE,
        PERM.CATALOGUE_MANAGE,
        PERM.SETTINGS_MANAGE,
        PERM.REPORT_VIEW_ALL,
    ),
    "Storekeeper": (
        PERM.GATE_IN_POST,
        PERM.GATE_OUT_REQUEST,
        # Storekeeper is also the gate guard at Silvertech, but this stays a
        # separate permission so another tenant can split the two (Q7).
        PERM.GATE_OUT_RELEASE,
        PERM.STOCK_ADJUST,
        PERM.CUSTODY_TRANSFER,
        PERM.REPORT_VIEW_ALL,
    ),
    "Approver": (
        PERM.GATE_OUT_APPROVE,
        PERM.DISPOSAL_APPROVE,
        PERM.REPORT_VIEW_ALL,
    ),
    "Technician": (
        PERM.GATE_OUT_REQUEST,
        PERM.JOB_CLOSEOUT,
    ),
}
