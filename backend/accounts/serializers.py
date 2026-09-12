"""Serializers for identity endpoints (design §6, requirements B1, B4)."""

from __future__ import annotations

from django.contrib.auth.signals import user_logged_in, user_login_failed
from rest_framework import serializers
from rest_framework_simplejwt.tokens import RefreshToken

from accounts.authentication_backends import find_user_by_identifier
from accounts.models import Role, User
from accounts.permissions_registry import PERMISSIONS_BY_CODENAME
from accounts.services import resolve_permissions
from core.tenancy import get_current_organization_id


class LoginSerializer(serializers.Serializer):
    """Log in with an email address or a phone number (B1).

    One ``identifier`` field rather than separate email and phone fields: the
    person logging in should not have to tell the system which kind of thing
    they just typed.
    """

    identifier = serializers.CharField(
        help_text="Email address or phone number.", trim_whitespace=True
    )
    password = serializers.CharField(write_only=True, style={"input_type": "password"})

    default_error_messages = {
        "invalid_credentials": "Those credentials are not correct.",
        "inactive": "This account has been deactivated. Please contact your administrator.",
    }

    def validate(self, attrs):  # type: ignore[no-untyped-def]
        request = self.context.get("request")
        organization_id = get_current_organization_id()

        user = find_user_by_identifier(attrs["identifier"], organization_id=organization_id)

        if user is None or not user.check_password(attrs["password"]):
            # B6: the failure is recorded, with the attempted identifier and
            # never the password.
            user_login_failed.send(
                sender=self.__class__,
                credentials={"username": attrs["identifier"]},
                request=request,
            )
            # The same message whether the user does not exist or the password
            # is wrong, so the endpoint cannot be used to enumerate staff.
            raise serializers.ValidationError(
                self.error_messages["invalid_credentials"], code="invalid_credentials"
            )

        if not user.is_active:
            user_login_failed.send(
                sender=self.__class__,
                credentials={"username": attrs["identifier"]},
                request=request,
            )
            raise serializers.ValidationError(
                self.error_messages["inactive"], code="inactive"
            )

        attrs["user"] = user
        return attrs

    def create(self, validated_data):  # type: ignore[no-untyped-def]
        user = validated_data["user"]
        refresh = RefreshToken.for_user(user)  # type: ignore[arg-type]

        user_logged_in.send(
            sender=user.__class__, request=self.context.get("request"), user=user
        )

        return {
            "access": str(refresh.access_token),
            "refresh": str(refresh),
            "user": MeSerializer(user, context=self.context).data,
        }


class LogoutSerializer(serializers.Serializer):
    """Revoke a refresh token (B1: refresh tokens are revocable)."""

    refresh = serializers.CharField()

    def validate_refresh(self, value: str) -> str:
        try:
            self.token = RefreshToken(value)  # type: ignore[arg-type]
        except Exception as exc:  # simplejwt raises TokenError subclasses
            raise serializers.ValidationError("That refresh token is not valid.") from exc
        return value

    def save(self, **kwargs):  # type: ignore[no-untyped-def]
        # Blacklisting is what makes logout mean something: without it the
        # refresh token would keep working until it expired.
        self.token.blacklist()
        return self.token


class RoleSummarySerializer(serializers.ModelSerializer):
    class Meta:
        model = Role
        fields = ("id", "name", "is_system")


class MeSerializer(serializers.ModelSerializer):
    """``GET /me`` — the payload the frontend drives navigation from (§7.2).

    Returns *resolved* permissions rather than roles alone, so the client never
    has to reimplement the resolution rules and then disagree with the server.
    The client gate is UX; the server re-checks every call.
    """

    roles = serializers.SerializerMethodField()
    permissions = serializers.SerializerMethodField()
    organization = serializers.SerializerMethodField()
    is_platform_admin = serializers.BooleanField(read_only=True)

    class Meta:
        model = User
        fields = (
            "id",
            "email",
            "phone",
            "full_name",
            "is_active",
            "is_platform_admin",
            "organization",
            "roles",
            "permissions",
        )
        read_only_fields = fields

    def get_roles(self, user: User) -> list[dict]:
        """A platform admin holds no tenant roles.

        Asking for them would raise ``TenantContextMissing`` — they have no
        organization in context — and the handler would turn that into a
        404, making ``/me`` look like a missing endpoint (§2.1, §3).
        """
        if user.organization_id is None:
            return []
        roles = [assignment.role for assignment in user.user_roles.all()]
        return RoleSummarySerializer(roles, many=True).data

    def get_permissions(self, user: User) -> list[str]:
        return sorted(resolve_permissions(user).codenames)

    def get_organization(self, user: User) -> dict | None:
        organization = user.organization
        if organization is None:
            return None
        settings = getattr(organization, "settings", None)
        return {
            "id": str(organization.pk),
            "name": organization.name,
            "slug": organization.slug,
            "status": organization.status,
            # C8: the client needs these to decide what to render at all —
            # whether to show cost fields, whether attachments are mandatory.
            "settings": {
                "money_tracking_enabled": getattr(settings, "money_tracking_enabled", False),
                "min_stock_enabled": getattr(settings, "min_stock_enabled", False),
                "qr_labels_enabled": getattr(settings, "qr_labels_enabled", False),
                "attachments_enabled": getattr(settings, "attachments_enabled", True),
                "attachments_required_gate_in": getattr(
                    settings, "attachments_required_gate_in", False
                ),
                "attachments_required_gate_out": getattr(
                    settings, "attachments_required_gate_out", False
                ),
                "signature_required_on_release": getattr(
                    settings, "signature_required_on_release", False
                ),
                "client_waybill_enabled": getattr(settings, "client_waybill_enabled", False),
                "timezone": getattr(settings, "timezone", "Africa/Nairobi"),
                "currency": getattr(settings, "currency", "KES"),
            }
            if settings is not None
            else None,
        }


class PermissionCatalogueSerializer(serializers.Serializer):
    """The permission registry, for building the role editor (T2.15, B4)."""

    codename = serializers.CharField()
    label = serializers.CharField()
    group = serializers.CharField()
    rationale = serializers.CharField()

    @staticmethod
    def catalogue() -> list[dict]:
        return [
            {
                "codename": spec.codename,
                "label": spec.label,
                "group": spec.group,
                "rationale": spec.rationale,
            }
            for spec in PERMISSIONS_BY_CODENAME.values()
        ]


# --------------------------------------------------------------------------
# Password reset and invitation (B2, A1)
# --------------------------------------------------------------------------


class PasswordResetRequestSerializer(serializers.Serializer):
    """B2: "reset my password by SMS or email, so that I can recover access"."""

    identifier = serializers.CharField(help_text="Email address or phone number.")

    def save(self, **kwargs):  # type: ignore[no-untyped-def]
        from accounts.reset import send_password_reset

        user = find_user_by_identifier(
            self.validated_data["identifier"],
            organization_id=get_current_organization_id(),
        )

        # Always report the same thing. Saying "no such user" would turn this
        # endpoint into a way to discover who works at a company.
        if user is not None and user.is_active:
            send_password_reset(user, request=self.context.get("request"))
        return {"detail": "If that account exists, a reset link has been sent."}


class PasswordResetConfirmSerializer(serializers.Serializer):
    """Set a new password from a reset or invitation token."""

    uid = serializers.CharField()
    token = serializers.CharField()
    password = serializers.CharField(write_only=True, style={"input_type": "password"})

    def validate(self, attrs):  # type: ignore[no-untyped-def]
        from django.contrib.auth.password_validation import validate_password
        from django.core.exceptions import ValidationError as DjangoValidationError

        from accounts.reset import check_reset_token, decode_uid

        user = decode_uid(attrs["uid"])
        if user is None or not check_reset_token(user, attrs["token"]):
            raise serializers.ValidationError(
                {"token": ["This link is not valid or has expired. Request a new one."]}
            )

        # Django's configured validators, so a reset cannot be used to set a
        # weaker password than the rules otherwise allow.
        try:
            validate_password(attrs["password"], user)
        except DjangoValidationError as exc:
            raise serializers.ValidationError({"password": list(exc.messages)}) from exc

        attrs["user"] = user
        return attrs

    def save(self, **kwargs):  # type: ignore[no-untyped-def]
        from core.audit import record
        from core.models import AuditAction

        user = self.validated_data["user"]
        user.set_password(self.validated_data["password"])
        user.save(update_fields=["password"])

        record(
            AuditAction.PASSWORD_CHANGED,
            actor=user,
            organization=user.organization_id,
            target=user,
            request=self.context.get("request"),
        )
        return user
