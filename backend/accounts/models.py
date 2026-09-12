"""Identity models (design §4.2).

Requirement B1: log in with **either** an email address or a phone number plus a
password, so that field staff without email can still use the system. Both
identifiers are unique *within a tenant*, not globally — two different companies
may legitimately employ the same person, or reuse an info@ address.
"""

from __future__ import annotations

import re
from typing import cast

from django.conf import settings
from django.contrib.auth.base_user import AbstractBaseUser, BaseUserManager
from django.contrib.auth.models import PermissionsMixin
from django.db import models
from django.db.models import Q
from django.utils import timezone

from core.models import TimeStampedModel
from core.tenancy import TenantModel


def normalise_phone(phone: str | None) -> str | None:
    """Reduce a phone number to one canonical shape: ``+254722123456``.

    Field staff type numbers inconsistently — ``0722 123 456``,
    ``+254722123456``, ``0722-123-456``, ``254722123456``. Stripping the
    punctuation was never enough: ``0722123456`` and ``+254722123456`` are the
    same phone and were stored as two different strings, so the per-tenant
    uniqueness constraint missed the duplicate and somebody invited on one form
    could not sign in with the other. A person who is told "your number is your
    username" and then cannot log in with their own number has no way to work
    out why.

    The national form is expanded using ``DEFAULT_COUNTRY_CALLING_CODE``, which
    is ``254`` here because the yard is in Kenya. A number already in
    international form is left alone, so this does not mangle a foreign
    supplier's number.
    """
    if not phone:
        return None
    cleaned = re.sub(r"[^\d+]", "", phone.strip())
    if not cleaned:
        return None
    # A ``+`` is only meaningful at the front.
    cleaned = cleaned[0] + cleaned[1:].replace("+", "")

    code = str(getattr(settings, "DEFAULT_COUNTRY_CALLING_CODE", "") or "")
    if not code:
        return cleaned
    if cleaned.startswith("+"):
        return cleaned
    if cleaned.startswith("00"):
        # The other international prefix, dialled from a landline habit.
        return "+" + cleaned[2:]
    if cleaned.startswith(code):
        return "+" + cleaned
    if cleaned.startswith("0"):
        # The national form: one leading zero stands in for the country code.
        return "+" + code + cleaned[1:]
    return cleaned


class UserManager(BaseUserManager):
    """Creates users identified by email, phone, or both (B1)."""

    use_in_migrations = True

    def _create_user(
        self,
        email: str | None,
        phone: str | None,
        password: str | None,
        **extra_fields,
    ):
        email = self.normalize_email(email).lower() if email else None
        phone = normalise_phone(phone)

        if not email and not phone:
            raise ValueError("A user must have an email address or a phone number.")

        # BaseUserManager is generic in the stubs, so `self.model(...)` widens to
        # the type variable; at runtime it is always a User.
        user = cast("User", self.model(email=email, phone=phone, **extra_fields))
        # Field staff are created by an admin and invited to set their own
        # password; an unusable password is the correct starting state.
        if password:
            user.set_password(password)
        else:
            user.set_unusable_password()
        user.save(using=self._db)
        return user

    def create_user(self, email=None, phone=None, password=None, **extra_fields):
        extra_fields.setdefault("is_staff", False)
        extra_fields.setdefault("is_superuser", False)
        return self._create_user(email, phone, password, **extra_fields)

    def create_superuser(self, email=None, phone=None, password=None, **extra_fields):
        """Create a platform admin (§3: "Platform admin — us. Cross-tenant").

        Platform admins have no organization, which is what makes them
        cross-tenant.
        """
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)
        extra_fields.setdefault("organization", None)

        if extra_fields.get("is_staff") is not True:
            raise ValueError("A superuser must have is_staff=True.")
        if extra_fields.get("is_superuser") is not True:
            raise ValueError("A superuser must have is_superuser=True.")

        return self._create_user(email, phone, password, **extra_fields)


class User(AbstractBaseUser, PermissionsMixin):
    """A platform or tenant user (§4.2).

    Not a :class:`~core.tenancy.TenantModel`: ``organization`` is nullable here,
    because a platform admin belongs to no tenant, and ``TenantModel`` requires
    one.

    Authorisation for tenant users runs on this project's own ``Role`` and
    ``Permission`` models (T1.10, B4) — roles are data, not code. The
    ``PermissionsMixin`` groups and permissions inherited here serve only the
    Django admin site, which §1.1 keeps as a free internal tool.
    """

    # Null for platform admins; set for everyone else (§4.2).
    organization = models.ForeignKey(
        "core.Organization",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="users",
        help_text="Null only for platform admins, who are deliberately cross-tenant.",
    )

    # Either identifier may be absent, but not both — see the check constraint.
    # `null=True` on a text field is normally discouraged, and is right
    # here: the partial unique constraints below key on NULL to mean "no
    # identifier". Empty strings would collide, so two users without an
    # email address could not coexist in one tenant.
    email = models.EmailField(null=True, blank=True)  # noqa: DJ001
    phone = models.CharField(  # noqa: DJ001
        max_length=20,
        null=True,
        blank=True,
        help_text="Normalised to digits with an optional leading '+'.",
    )

    full_name = models.CharField(max_length=200, blank=True)

    # B3: deactivating a user preserves all their historical records. A user who
    # has posted movements can never be deleted, only deactivated.
    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(
        default=False, help_text="Access to the Django admin site, not to tenant data."
    )
    date_joined = models.DateTimeField(default=timezone.now)

    objects = UserManager()

    # Login accepts email *or* phone, resolved by a custom authentication
    # backend (T1.12). This field is not globally unique — uniqueness is per
    # organization — so Django's auth.E003 check is silenced in settings with
    # that reasoning recorded.
    USERNAME_FIELD = "email"  # type: ignore[misc]
    REQUIRED_FIELDS: list[str] = []  # type: ignore[misc]

    class Meta:
        constraints = [
            # B1: at least one identifier, or the user could never log in.
            models.CheckConstraint(
                condition=Q(email__isnull=False) | Q(phone__isnull=False),
                name="user_has_email_or_phone",
            ),
            # B1: unique *within* a tenant. Two organizations may each have a
            # user with the same email address.
            models.UniqueConstraint(
                fields=["organization", "email"],
                condition=Q(email__isnull=False),
                name="uniq_user_email_per_organization",
            ),
            models.UniqueConstraint(
                fields=["organization", "phone"],
                condition=Q(phone__isnull=False),
                name="uniq_user_phone_per_organization",
            ),
            # Postgres treats NULLs as distinct in a unique constraint, so the
            # two constraints above do not constrain platform admins at all
            # (their organization is NULL). These cover that gap.
            models.UniqueConstraint(
                fields=["email"],
                condition=Q(organization__isnull=True, email__isnull=False),
                name="uniq_platform_admin_email",
            ),
            models.UniqueConstraint(
                fields=["phone"],
                condition=Q(organization__isnull=True, phone__isnull=False),
                name="uniq_platform_admin_phone",
            ),
        ]
        indexes = [
            models.Index(fields=["organization", "is_active"]),
        ]

    def __str__(self) -> str:
        return self.full_name or self.email or self.phone or f"user {self.pk}"

    @property
    def is_platform_admin(self) -> bool:
        """Platform admins are exactly those with no organization (§3)."""
        return self.organization_id is None

    def clean(self) -> None:
        super().clean()
        self.email = self.email.lower() if self.email else None
        self.phone = normalise_phone(self.phone)

    def save(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        # Normalise on every path, not only through the manager, so that admin
        # edits and fixtures cannot slip past the uniqueness constraints.
        self.email = self.email.lower() if self.email else None
        self.phone = normalise_phone(self.phone)
        return super().save(*args, **kwargs)

    def get_full_name(self) -> str:
        return self.full_name

    def get_short_name(self) -> str:
        return self.full_name.split(" ")[0] if self.full_name else str(self)


# --------------------------------------------------------------------------
# Roles and permissions (§4.2, B3, B4)
# --------------------------------------------------------------------------
# "Roles are data, not code" (§3). A tenant may create, rename or delete roles
# and tick the permissions each one holds. The permission *codenames* are code
# (accounts/permissions_registry.py), because they must correspond to something
# the software actually checks.


class Role(TenantModel, TimeStampedModel):
    """A named bundle of permissions, defined per tenant (B4)."""

    name = models.CharField(max_length=100)
    description = models.CharField(max_length=255, blank=True)

    # Seeded roles are marked so the UI can explain where they came from. It does
    # not make them undeletable — B4 explicitly allows deleting a role.
    is_system = models.BooleanField(
        default=False, help_text="Seeded when the tenant was created, rather than hand-made."
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "name"], name="uniq_role_name_per_organization"
            )
        ]
        ordering = ("name",)

    def __str__(self) -> str:
        return self.name

    @property
    def codenames(self) -> set[str]:
        return {rp.codename for rp in self.permissions.all()}

    def set_permissions(self, codenames) -> None:
        """Replace this role's permissions with ``codenames``.

        Validates every codename, so a typo cannot create a permission that
        looks like protection but is never checked.
        """
        from accounts.permissions_registry import validate_codename

        wanted = {validate_codename(codename) for codename in codenames}
        current = self.codenames

        self.permissions.filter(codename__in=current - wanted).delete()
        RolePermission.objects.bulk_create(
            [
                RolePermission(organization_id=self.organization_id, role=self, codename=codename)
                for codename in sorted(wanted - current)
            ]
        )


class RolePermission(TenantModel):
    """One permission granted to one role (§4.2).

    Stores the codename as text rather than a foreign key to a permissions
    table: the registry is code, so a row here is a reference into it.
    """

    role = models.ForeignKey(Role, on_delete=models.CASCADE, related_name="permissions")
    codename = models.CharField(max_length=100)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["role", "codename"], name="uniq_permission_per_role")
        ]

    def __str__(self) -> str:
        return f"{self.role.name}: {self.codename}"


class UserRole(TenantModel, TimeStampedModel):
    """Assignment of a role to a user. A user may hold several (B3)."""

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="user_roles")
    role = models.ForeignKey(Role, on_delete=models.PROTECT, related_name="user_roles")

    class Meta:
        constraints = [models.UniqueConstraint(fields=["user", "role"], name="uniq_role_per_user")]

    def __str__(self) -> str:
        return f"{self.user} as {self.role.name}"


class Delegation(TenantModel, TimeStampedModel):
    """Temporary transfer of permissions from one user to another (F5).

    F5: "configure delegation, so that approvals continue when the owner is
    away." The critical rule is attribution: a delegated approval is recorded as
    **"X on behalf of Y", never as Y** (§4.2). Recording it as Y would forge the
    principal's signature on an approval they never gave — the opposite of what
    the audit trail is for.
    """

    from_user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="delegations_given",
        help_text="The principal, whose authority is being lent.",
    )
    to_user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="delegations_received",
        help_text="The delegate, who will act on the principal's behalf.",
    )

    # Either a whole role, or a specific set of codenames. A role is the common
    # case ("cover for me while I am away"); codenames allow lending only the
    # authority actually needed.
    role = models.ForeignKey(
        Role, on_delete=models.CASCADE, null=True, blank=True, related_name="delegations"
    )
    codenames = models.JSONField(
        default=list,
        blank=True,
        help_text="Specific permissions to delegate. Used when no role is given.",
    )

    starts_at = models.DateTimeField()
    ends_at = models.DateTimeField()
    reason = models.CharField(max_length=255, blank=True)

    # Revocable before it expires — someone may come back early.
    is_revoked = models.BooleanField(default=False)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(ends_at__gt=models.F("starts_at")),
                name="delegation_ends_after_it_starts",
            ),
            # A delegation that lends neither a role nor any permission is an
            # inert record that reads, on the screen, exactly like cover being
            # in place. Somebody goes on leave believing approvals will
            # continue, and they do not. The serializer refuses it; this is the
            # backstop for every other way a row can be written.
            models.CheckConstraint(
                condition=Q(role__isnull=False) | ~Q(codenames=[]),
                name="delegation_delegates_something",
            ),
            # Delegating to yourself would be a no-op that looks like a control.
            models.CheckConstraint(
                condition=~Q(from_user=models.F("to_user")),
                name="delegation_is_not_self",
            ),
        ]
        indexes = [
            models.Index(fields=["organization", "to_user", "starts_at", "ends_at"]),
        ]

    def __str__(self) -> str:
        window = f"{self.starts_at:%Y-%m-%d} to {self.ends_at:%Y-%m-%d}"
        return f"{self.from_user} -> {self.to_user} ({window})"

    def is_active_at(self, moment) -> bool:
        return not self.is_revoked and self.starts_at <= moment <= self.ends_at

    def delegated_codenames(self) -> set[str]:
        """The permissions this delegation confers."""
        if self.role_id and self.role is not None:
            return self.role.codenames
        return set(self.codenames or [])


class WebAuthnCredential(TenantModel, TimeStampedModel):
    """A platform authenticator enrolled by a user (§4.2; B5, D9).

    D9 limits WebAuthn to a **step-up on the approval action only** in v1: the
    fingerprint proves who authorised a gate-out, which is the signature an
    auditor cares about (M3). It is not a replacement for the password login.

    B5's edge case is why several credentials are allowed per user: "losing a
    device must not lock the user out; an admin can revoke enrolled
    credentials." One credential per user would turn a lost phone into a lockout.

    The model exists from Phase 4 so ``ApprovalAction`` can record which
    credential signed an approval. The enrolment and assertion flows are T8.8
    and T8.9.
    """

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="webauthn_credentials")

    credential_id = models.CharField(max_length=400, db_index=True)
    public_key = models.TextField()
    sign_count = models.PositiveBigIntegerField(default=0)
    aaguid = models.CharField(max_length=64, blank=True)

    # So a user revoking a credential can tell which phone it was.
    device_label = models.CharField(max_length=100, blank=True)
    last_used_at = models.DateTimeField(null=True, blank=True)

    # B5: an admin can revoke a credential. Revoked rather than deleted, so an
    # approval already signed with it stays explicable.
    is_active = models.BooleanField(default=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    revoked_by = models.ForeignKey(
        User, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "credential_id"],
                name="uniq_webauthn_credential_per_org",
            )
        ]
        ordering = ("-created_at",)

    def __str__(self) -> str:
        return self.device_label or f"Credential {self.credential_id[:12]}"
