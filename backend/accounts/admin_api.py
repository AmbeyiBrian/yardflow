"""User, role, delegation and settings administration (design §6, §4.2; B3–B5, C8).

Kept apart from ``accounts.views``, which holds the unauthenticated flows —
login, refresh, password reset. Everything here needs ``users.manage`` or
``settings.manage``, and B4's warning applies to all of it: the permission to
manage users is the permission to grant permissions, so it is owner-level in
practice.

Two refusals here are the substance of B3, and both are actions rather than field
edits so they can carry their reason and their guard:

* deactivating the last owner-level user
* deactivating somebody still holding material in custody
"""

from __future__ import annotations

from django.db import transaction
from drf_spectacular.utils import extend_schema, inline_serializer
from rest_framework import serializers, status
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.api_permissions import HasPermission
from accounts.models import Delegation, Role, User, UserRole
from accounts.permissions_registry import ALL_PERMISSIONS, PERM, validate_codename
from accounts.services import (
    LastOwnerProtected,
    can_be_deleted,
    deactivate_user,
    resolve_permissions,
    revoke_role,
)
from core.api import TenantScopedViewSet
from core.api_permissions import OrganizationIsActive
from core.exceptions import DomainError
from core.models import AuditAction, Organization, OrganizationSettings
from core.pagination import JoinedCursorPagination


def _same_number(left: str | None, right: str | None) -> bool:
    """Two phone numbers that reach the same handset.

    Compared on the last nine digits — the Kenyan subscriber number — so
    `0797259698`, `+254797259698` and `254797259698` are recognised as one
    person. Deliberately a comparison rather than a rewrite of what is stored:
    changing the stored form would touch every existing row and the login path,
    which is a bigger decision than refusing a duplicate.
    """
    if not left or not right:
        return False
    left_digits = "".join(character for character in left if character.isdigit())
    right_digits = "".join(character for character in right if character.isdigit())
    if not left_digits or not right_digits:
        return False
    return left_digits[-9:] == right_digits[-9:]


class UserInactive(DomainError):
    """Inviting somebody who cannot sign in would be a link that fails at the
    end, with nothing to explain why."""

    code = "USER_INACTIVE"
    default_message = "This account is deactivated. Reactivate it first."


class RoleSerializer(serializers.ModelSerializer):
    """B4: roles are data. A tenant may invent any role it likes."""

    codenames = serializers.ListField(
        child=serializers.CharField(), required=False, help_text="Permissions granted."
    )
    user_count = serializers.SerializerMethodField()

    class Meta:
        model = Role
        fields = (
            "id",
            "name",
            "description",
            "is_system",
            "codenames",
            "user_count",
            "created_at",
        )
        read_only_fields = ("is_system",)

    def get_user_count(self, role) -> int:
        return role.user_roles.count()

    def to_representation(self, instance):  # type: ignore[no-untyped-def]
        data = super().to_representation(instance)
        data["codenames"] = sorted(instance.codenames)
        return data

    def create(self, validated_data):  # type: ignore[no-untyped-def]
        codenames = validated_data.pop("codenames", [])
        role = Role.objects.create(**validated_data)
        role.set_permissions(codenames)
        return role

    def update(self, instance, validated_data):  # type: ignore[no-untyped-def]
        codenames = validated_data.pop("codenames", None)
        role = super().update(instance, validated_data)
        if codenames is not None:
            role.set_permissions(codenames)
        return role

    def validate_codenames(self, value):  # type: ignore[no-untyped-def]
        """A codename no code checks would look like protection and be none."""
        invalid = []
        for codename in value:
            try:
                validate_codename(codename)
            except Exception:
                invalid.append(codename)
        if invalid:
            raise serializers.ValidationError(
                f"Not permissions this software checks: {', '.join(sorted(invalid))}."
            )
        return value


class UserSerializer(serializers.ModelSerializer):
    role_ids = serializers.ListField(
        child=serializers.IntegerField(), required=False, write_only=True
    )
    roles = serializers.SerializerMethodField()
    permissions = serializers.SerializerMethodField()
    can_be_deleted = serializers.SerializerMethodField()
    #: B1: an account is created with no password and the person is invited to
    #: set one. Until they do, they cannot sign in — and the administrator who
    #: added them has no way to know unless the screen says so.
    has_signed_in_yet = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = (
            "id",
            "email",
            "phone",
            "full_name",
            "is_active",
            "roles",
            "role_ids",
            "permissions",
            "can_be_deleted",
            "has_signed_in_yet",
            "date_joined",
            "last_login",
        )
        # B3: deactivation is an action with guards, never a field edit.
        read_only_fields = ("is_active", "date_joined", "last_login")

    def get_has_signed_in_yet(self, user) -> bool:
        """Whether they have set a password. Never the password itself."""
        return user.has_usable_password()

    def validate(self, attrs):  # type: ignore[no-untyped-def]
        """B1: an identity is an email or a phone number, and one is required.

        The manager enforces this too, but it raises ``ValueError`` — which
        reaches the client as a 500. A missing field is the caller's mistake and
        has to come back as a field error they can act on (§6.1).
        """
        email = attrs.get("email", getattr(self.instance, "email", None))
        phone = attrs.get("phone", getattr(self.instance, "phone", None))
        if not email and not phone:
            raise serializers.ValidationError(
                {
                    "email": [
                        "Give an email address or a phone number. Field "
                        "technicians often have no company email, so either "
                        "will do."
                    ]
                }
            )

        # Already somebody else's.
        #
        # Both of these are unique per organization in the database, and
        # violating either produced a 500: the person filling in the form was
        # told "The server had a problem with that", when in fact they had
        # typed a colleague's phone number. Naming who holds it is the whole
        # answer — usually it is the same person being added twice.
        self._refuse_a_duplicate("email", email, attrs)
        self._refuse_a_duplicate("phone", phone, attrs)

        return attrs

    def _refuse_a_duplicate(self, field: str, value, attrs) -> None:
        """Say who already has it, rather than that something went wrong."""
        if not value:
            return

        organization_id = getattr(self.context.get("request").user, "organization_id", None)
        if organization_id is None:
            # A platform admin belongs to no tenant and cannot add tenant staff;
            # there is nothing to compare against.
            return

        if field == "phone":
            from accounts.models import normalise_phone

            value = normalise_phone(value)
            attrs["phone"] = value

            # Compared on the subscriber number, not the stored text.
            # `normalise_phone` strips punctuation but keeps the form typed, so
            # `0797259698` and `+254797259698` are different strings and the
            # database constraint does not see them as a clash. They are one
            # person, and letting both exist means one human with two accounts,
            # two custody records, and a login that works only if they remember
            # which way they typed it.
            clash = [
                other
                for other in User.objects.filter(
                    organization_id=organization_id, phone__isnull=False
                )
                if _same_number(other.phone, value)
            ]
            if self.instance is not None:
                clash = [other for other in clash if other.pk != self.instance.pk]
            existing = clash[0] if clash else None
        else:
            value = value.lower()
            attrs["email"] = value
            matches = User.objects.filter(organization_id=organization_id, email=value)
            if self.instance is not None:
                matches = matches.exclude(pk=self.instance.pk)
            existing = matches.first()
        if existing is None:
            return

        label = "email address" if field == "email" else "phone number"
        raise serializers.ValidationError(
            {
                field: [
                    f"That {label} already belongs to "
                    f"{existing.full_name or existing}. One person, one "
                    f"{label} — it is how they sign in."
                ]
            }
        )

    def get_roles(self, user) -> list[dict]:
        return [
            {"id": assignment.role_id, "name": assignment.role.name}
            for assignment in user.user_roles.all()
        ]

    def get_permissions(self, user) -> list[str]:
        return sorted(resolve_permissions(user).codenames)

    def get_can_be_deleted(self, user) -> bool:
        return can_be_deleted(user)

    def _apply_roles(self, user, role_ids) -> None:
        wanted = set(role_ids)
        current = set(user.user_roles.values_list("role_id", flat=True))

        for role_id in current - wanted:
            # Goes through the service, so removing the last owner is refused
            # here exactly as it is anywhere else (B4).
            role = Role.objects.filter(pk=role_id).first()
            if role is not None:
                revoke_role(user, role)

        for role_id in wanted - current:
            role = Role.objects.filter(pk=role_id).first()
            if role is None:
                raise serializers.ValidationError({"role_ids": [f"No role {role_id}."]})
            UserRole.objects.create(organization_id=user.organization_id, user=user, role=role)

    @transaction.atomic
    def create(self, validated_data):  # type: ignore[no-untyped-def]
        role_ids = validated_data.pop("role_ids", [])
        organization = self.context["request"].user.organization
        user = User.objects.create_user(organization=organization, **validated_data)
        self._apply_roles(user, role_ids)
        return user

    @transaction.atomic
    def update(self, instance, validated_data):  # type: ignore[no-untyped-def]
        role_ids = validated_data.pop("role_ids", None)
        user = super().update(instance, validated_data)
        if role_ids is not None:
            self._apply_roles(user, role_ids)
        return user


class DelegationSerializer(serializers.ModelSerializer):
    from_user_name = serializers.CharField(source="from_user.full_name", read_only=True)
    to_user_name = serializers.CharField(source="to_user.full_name", read_only=True)
    #: What is actually being lent. Without this the screen could show a
    #: delegation as open without saying what it confers — which is how one that
    #: conferred nothing went unnoticed.
    role_name = serializers.CharField(source="role.name", read_only=True, default=None)
    is_currently_active = serializers.SerializerMethodField()

    class Meta:
        model = Delegation
        fields = (
            "id",
            "from_user",
            "from_user_name",
            "to_user",
            "to_user_name",
            "role",
            "role_name",
            "codenames",
            "starts_at",
            "ends_at",
            "reason",
            "is_revoked",
            "is_currently_active",
            "created_at",
        )
        read_only_fields = ("is_revoked",)

    def get_is_currently_active(self, delegation) -> bool:
        from django.utils import timezone

        return delegation.is_active_at(timezone.now())

    def validate(self, attrs):  # type: ignore[no-untyped-def]
        """Say no in words, before the database says no in a stack trace.

        Both of these are enforced by check constraints, which is right — the
        database is the last line and should hold whatever the application
        forgets. But an `IntegrityError` surfaces as a 500 with a page of
        traceback, and the person who typed the form learns nothing. The
        constraint stays; this is what a human reads.

        Errors are attached to the field that is wrong, so the form can highlight
        the right input (§6.1).
        """
        current = getattr(self, "instance", None)
        from_user = attrs.get("from_user") or getattr(current, "from_user", None)
        to_user = attrs.get("to_user") or getattr(current, "to_user", None)
        starts_at = attrs.get("starts_at") or getattr(current, "starts_at", None)
        ends_at = attrs.get("ends_at") or getattr(current, "ends_at", None)

        if from_user and to_user and from_user == to_user:
            raise serializers.ValidationError(
                {
                    "to_user": [
                        "Choose somebody else. Delegating to yourself changes "
                        "nothing, and would look like cover for an approval that "
                        "was really your own."
                    ]
                }
            )

        if starts_at and ends_at and ends_at <= starts_at:
            raise serializers.ValidationError({"ends_at": ["The end has to be after the start."]})

        # A delegation hands over authority, so both people must be inside this
        # tenant. `User.objects` is not tenant-scoped — it cannot be, because
        # signing in has to find somebody before their organization is known — so
        # the check belongs here rather than being assumed (A3, B4).
        organization_id = getattr(self.context.get("request").user, "organization_id", None)
        for field, person in (("from_user", from_user), ("to_user", to_user)):
            if person and person.organization_id != organization_id:
                raise serializers.ValidationError(
                    {field: ["That person is not in this organization."]}
                )

        # A delegation that delegates nothing is the worst outcome available
        # here: it shows as open, so somebody goes on leave believing their
        # approvals are covered, and nobody can act. Worse than a refusal,
        # because a refusal is visible on the day it happens.
        role = attrs.get("role", getattr(current, "role", None))
        codenames = attrs.get("codenames", getattr(current, "codenames", None)) or []

        # Nobody may lend what they do not hold.
        #
        # Without this a storekeeper could delegate the Owner role and create
        # authority that never existed — tested, and the API accepted it. A
        # delegation is somebody handing over their own keys for a while; it
        # cannot mint new ones.
        #
        # Measured against what the lender holds **directly**, not what they
        # have been lent: otherwise authority could be passed along a chain,
        # and the further it travels the less anybody can see who authorised
        # the first step.
        if from_user and (role or codenames):
            held = resolve_permissions(from_user)
            directly_held = set(held.codenames) - set(held.delegated_from)
            lending = set(role.codenames) if role else set(codenames)
            beyond = sorted(lending - directly_held)
            if beyond:
                field = "role" if role else "codenames"
                what = f"the {role.name} role" if role else f"{len(beyond)} of those permissions"
                raise serializers.ValidationError(
                    {
                        field: [
                            f"{from_user.full_name or from_user} cannot delegate "
                            f"{what}, because they do not hold it themselves. "
                            "A delegation lends somebody's own authority; it "
                            "cannot create any."
                        ]
                    }
                )

        if not role and not codenames:
            raise serializers.ValidationError(
                {
                    "role": [
                        "Say what is being delegated: either a role, or the "
                        "specific permissions to lend. A delegation that carries "
                        "neither looks active and confers nothing."
                    ]
                }
            )

        return attrs


class RoleViewSet(TenantScopedViewSet):
    """``/api/v1/roles`` (B4)."""

    serializer_class = RoleSerializer
    model = Role
    prefetch_related = ("permissions", "user_roles")
    search_fields = ["name", "description"]
    ordering_fields = ["name"]

    required_permission = PERM.USERS_MANAGE

    def get_permissions(self):  # type: ignore[no-untyped-def]
        """Reading the role list is not administration.

        Every screen that assigns work needs to know the roles exist — an
        approval rule routes to one (§5.1). Requiring ``users.manage`` to read
        them would mean a storekeeper could not see who to send a request to.
        """
        if self.action in ("list", "retrieve"):
            return [IsAuthenticated(), OrganizationIsActive()]
        return super().get_permissions()


class UserViewSet(TenantScopedViewSet):
    """``/api/v1/users`` (B3, B4).

    Users are not a ``TenantModel`` — a platform admin belongs to no tenant — so
    the queryset is scoped explicitly here rather than by the manager. That is
    the one place in the codebase where tenant scoping is written out, and it is
    written out because the model cannot carry it.
    """

    serializer_class = UserSerializer
    model = User
    prefetch_related = ("user_roles", "user_roles__role")
    filterset_fields = ["is_active"]
    search_fields = ["full_name", "email", "phone"]
    ordering_fields = ["full_name", "date_joined"]

    required_permission = PERM.USERS_MANAGE

    # ``User`` has no ``created_at``, so the default cursor ordering cannot be
    # used here (see the class docstring).
    pagination_class = JoinedCursorPagination

    # B3: never deleted through the API. Deactivated, with the guards below.
    http_method_names = ["get", "post", "patch", "head", "options"]

    def get_queryset(self):  # type: ignore[no-untyped-def]
        from core.tenancy import require_current_organization_id

        return (
            User.objects.filter(organization_id=require_current_organization_id())
            .prefetch_related(*self.prefetch_related)
            .order_by("full_name", "id")
        )

    def get_permissions(self):  # type: ignore[no-untyped-def]
        """Everyone needs the people list; only admins may change it.

        A gate-out names a custody holder, a job names an assignee, a handover
        names a receiver. None of those screens is administration.
        """
        if self.action in ("list", "retrieve"):
            return [IsAuthenticated(), OrganizationIsActive()]
        return super().get_permissions()

    def perform_create(self, serializer):  # type: ignore[no-untyped-def]
        """``User`` carries no ``created_by``, so the base class's stamp cannot
        apply. The audit entry records who did it instead (M3)."""
        user = serializer.save()
        self._audit(AuditAction.USER_CREATED, user, f"User {user} created.")

    def perform_update(self, serializer):  # type: ignore[no-untyped-def]
        user = serializer.save()
        self._audit(AuditAction.USER_UPDATED, user, f"User {user} updated.")

    def _audit(self, what, user, note) -> None:
        from core.audit import record

        record(
            what,
            actor=self.request.user,
            organization=user.organization_id,
            target=user,
            target_label=str(user),
            request=self.request,
            note=note,
        )

    @extend_schema(request=None, responses={200: UserSerializer})
    @action(detail=True, methods=["post"])
    def deactivate(self, request, pk=None):  # type: ignore[no-untyped-def]
        """B3: refused if they hold custody, or if they are the last owner."""
        user = self.get_object()
        try:
            deactivate_user(user)
        except LastOwnerProtected as exc:
            return Response(
                {"error": {"code": "LAST_OWNER_PROTECTED", "message": str(exc)}},
                status=status.HTTP_400_BAD_REQUEST,
            )
        user.refresh_from_db()
        self._audit(AuditAction.USER_DEACTIVATED, user, f"User {user} deactivated (B3).")
        return Response(self.get_serializer(user).data)

    @extend_schema(request=None, responses={200: UserSerializer})
    @extend_schema(request=None, responses={200: UserSerializer})
    @action(detail=True, methods=["post"], url_path="resend-invitation")
    def resend_invitation(self, request, pk=None):  # type: ignore[no-untyped-def]
        """Send the invitation again (B1).

        An account is created with no password and the person is invited to set
        one. Links get lost — a phone is wiped, an email goes to spam — and
        without this the administrator who added them had no way to try again.
        Asking the platform owner to re-send it is not tenant administration.

        No password is generated here either. The link is addressed to the
        tenant's own subdomain, whoever sends it.
        """
        from accounts.reset import send_password_reset

        user = self.get_object()

        # No "has no contact details" branch: B1 is enforced when the account is
        # created, so every user has an email address or a phone number, and a
        # guard here would be a branch nothing can reach.
        if not user.is_active:
            raise UserInactive()

        # `send_password_reset` records `PASSWORD_RESET_REQUESTED` itself, so
        # there is nothing to audit here — a second entry would only make the
        # trail say it happened twice.
        send_password_reset(user, request=request, is_invitation=True)
        return Response(UserSerializer(user, context={"request": request}).data)

    @action(detail=True, methods=["post"])
    def reactivate(self, request, pk=None):  # type: ignore[no-untyped-def]
        """B3: deactivation preserves everything, so it is reversible."""
        user = self.get_object()
        user.is_active = True
        user.save(update_fields=["is_active"])
        self._audit(AuditAction.USER_UPDATED, user, f"User {user} reactivated.")
        return Response(self.get_serializer(user).data)


class DelegationViewSet(TenantScopedViewSet):
    """``/api/v1/delegations`` (F5)."""

    serializer_class = DelegationSerializer
    model = Delegation
    select_related = ("from_user", "to_user", "role")
    filterset_fields = ["from_user", "to_user", "is_revoked"]
    ordering_fields = ["starts_at", "ends_at"]

    required_permission = PERM.USERS_MANAGE

    http_method_names = ["get", "post", "patch", "head", "options"]

    @extend_schema(request=None, responses={200: DelegationSerializer})
    @action(detail=True, methods=["post"])
    def revoke(self, request, pk=None):  # type: ignore[no-untyped-def]
        """Someone came back early. Revoked rather than deleted, so the window
        it covered stays visible next to the approvals made under it (§4.2)."""
        delegation = self.get_object()
        delegation.is_revoked = True
        delegation.save(update_fields=["is_revoked", "updated_at"])
        return Response(self.get_serializer(delegation).data)


SETTINGS_FIELDS = (
    "money_tracking_enabled",
    "min_stock_enabled",
    "qr_labels_enabled",
    "asset_tag_enabled",
    "asset_tag_prefix_format",
    "client_waybill_enabled",
    "attachments_enabled",
    "attachments_required_gate_in",
    "attachments_required_gate_out",
    "signature_required_on_release",
    "gate_pass_expiry_hours",
    "allow_self_approval",
    "approval_escalation_hours",
    "allow_document_amendment",
    "retention_months",
    "timezone",
    "currency",
    # L2: the tenant decides who hears what, and through which channels. Editable
    # behind `settings.manage` like everything else on this endpoint — the first
    # build locked these two, which took the decision away from the Owner and
    # Admin roles that are accountable for it.
    "notification_channels",
    "notification_matrix",
)


class OrganizationSettingsSerializer(serializers.ModelSerializer):
    class Meta:
        model = OrganizationSettings
        fields = SETTINGS_FIELDS

    def validate_notification_channels(self, value):  # type: ignore[no-untyped-def]
        """Known channels, boolean values. Nothing else.

        A typo here is silent otherwise: an unknown key would simply never match,
        and the tenant would believe they had turned something on.
        """
        from notifications.matrix import default_channels_config

        if not isinstance(value, dict):
            raise serializers.ValidationError("Expected an object of channel switches.")

        known = set(default_channels_config())
        unknown = set(value) - known
        if unknown:
            raise serializers.ValidationError(
                f"Unknown channel(s): {', '.join(sorted(unknown))}. "
                f"Known channels are {', '.join(sorted(known))}."
            )
        for channel, enabled in value.items():
            if not isinstance(enabled, bool):
                raise serializers.ValidationError(f"'{channel}' must be true or false.")
        return value

    def validate_notification_matrix(self, value):  # type: ignore[no-untyped-def]
        """Known events, known channels, known recipients.

        Recipients are validated too, because a role name that does not resolve
        produces an event with nobody to tell — which looks exactly like a
        notification that was never sent.
        """
        from notifications.matrix import DEFAULT_MATRIX, Channel, Recipient

        if not isinstance(value, dict):
            raise serializers.ValidationError("Expected an object keyed by event.")

        known_events = {spec.key for spec in DEFAULT_MATRIX}
        known_channels = {getattr(Channel, name) for name in dir(Channel) if name.isupper()}
        known_recipients = {getattr(Recipient, name) for name in dir(Recipient) if name.isupper()}

        unknown = set(value) - known_events
        if unknown:
            raise serializers.ValidationError(f"Unknown event(s): {', '.join(sorted(unknown))}.")

        for event, entry in value.items():
            if not isinstance(entry, dict):
                raise serializers.ValidationError(f"'{event}' must be an object.")

            if "enabled" in entry and not isinstance(entry["enabled"], bool):
                raise serializers.ValidationError(f"'{event}.enabled' must be true or false.")

            for field, allowed in (
                ("channels", known_channels),
                ("recipients", known_recipients),
            ):
                if field not in entry:
                    continue
                if not isinstance(entry[field], list):
                    raise serializers.ValidationError(f"'{event}.{field}' must be a list.")
                strays = set(entry[field]) - allowed
                if strays:
                    raise serializers.ValidationError(
                        f"'{event}.{field}' contains unknown value(s): {', '.join(sorted(strays))}."
                    )

        return value


class OrganizationSettingsView(APIView):
    """``/api/v1/settings`` (C8).

    One object per tenant, so it is a singleton endpoint rather than a
    collection. Reading is open to any member — half the screens change shape
    according to ``money_tracking_enabled`` and its neighbours, and a screen that
    could not read its own switches would have to guess.
    """

    permission_classes = [IsAuthenticated, OrganizationIsActive, HasPermission]
    required_permissions = {"patch": PERM.SETTINGS_MANAGE}

    def get_settings(self, request):  # type: ignore[no-untyped-def]
        organization = Organization.objects.get(pk=request.user.organization_id)
        return organization.settings

    @extend_schema(responses={200: OrganizationSettingsSerializer})
    def get(self, request):  # type: ignore[no-untyped-def]
        return Response(OrganizationSettingsSerializer(self.get_settings(request)).data)

    @extend_schema(
        request=OrganizationSettingsSerializer, responses={200: OrganizationSettingsSerializer}
    )
    def patch(self, request):  # type: ignore[no-untyped-def]
        settings_object = self.get_settings(request)
        serializer = OrganizationSettingsSerializer(
            settings_object, data=request.data, partial=True
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()

        from core.audit import record

        record(
            AuditAction.SETTINGS_CHANGED,
            actor=request.user,
            organization=request.user.organization_id,
            target=settings_object,
            target_label="Organization settings",
            request=request,
            after=serializer.validated_data,
            note="Organization settings changed.",
        )
        return Response(serializer.data)


class PermissionGroupsView(APIView):
    """``/api/v1/permission-groups`` — the registry, grouped, for the role editor.

    Includes each permission's rationale (§4.2): the role editor shows *why* a
    permission is its own switch, so an administrator ticking boxes is making an
    informed decision rather than guessing from a codename.
    """

    permission_classes = [IsAuthenticated, OrganizationIsActive]

    @extend_schema(
        responses={
            200: inline_serializer(
                "PermissionGroups",
                {"groups": serializers.ListField(child=serializers.DictField())},
            )
        }
    )
    def get(self, request):  # type: ignore[no-untyped-def]
        groups: dict[str, list[dict]] = {}
        for spec in ALL_PERMISSIONS:
            groups.setdefault(spec.group, []).append(
                {
                    "codename": spec.codename,
                    "label": spec.label,
                    "rationale": spec.rationale,
                }
            )
        return Response(
            {
                "groups": [
                    {"group": group, "permissions": permissions}
                    for group, permissions in groups.items()
                ]
            }
        )
