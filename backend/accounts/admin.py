"""People, seen from the platform (A1, B1, B3).

Registered for one job: after provisioning a tenant, the platform owner needs to
see the owner account they just created, and re-send the invitation when it is
lost — which is exactly what happened the first time a tenant was created here.
Everything else about users is the tenant's own business and belongs in their
Settings screen, where roles are assigned with an interface that understands
them.

So this is deliberately narrow. It **shows** accounts across tenants and can send
a password link. It does not create users, assign roles, or set passwords:

* **No password field.** B1's flow is an invitation the person answers
  themselves; a password typed here by an administrator is one that somebody else
  has seen.
* **No role editing.** Roles are tenant-scoped rows guarded by row-level
  security; editing them from outside any tenant context is the shape of a
  cross-tenant mistake.
* **No deletion.** A user is referenced by every movement they ever posted, and
  the ledger is append-only. Deactivation is the answer.
"""

from __future__ import annotations

from django.contrib import admin, messages
from django.utils.html import format_html

from accounts.models import User
from accounts.reset import send_password_reset


@admin.register(User)
class UserAdmin(admin.ModelAdmin):
    list_display = (
        "identifier",
        "full_name",
        "organization",
        "password_state",
        "is_active",
        "last_login",
    )
    list_filter = ("is_active", "organization", "is_superuser")
    search_fields = ("email", "phone", "full_name")
    ordering = ("organization__name", "email")
    list_select_related = ("organization",)
    actions = ("send_invitation", "deactivate", "reactivate")

    # Read-mostly on purpose: the fields that decide what somebody can do are
    # managed by the tenant, not from here.
    readonly_fields = (
        "organization",
        "last_login",
        "date_joined",
        "password_state",
    )
    fields = (
        "email",
        "phone",
        "full_name",
        "organization",
        "is_active",
        "password_state",
        "last_login",
        "date_joined",
    )

    @admin.display(description="Signs in with", ordering="email")
    def identifier(self, user: User) -> str:
        # B1: either identifies them, and a technician may have only a phone.
        return user.email or user.phone or "—"

    @admin.display(description="Password")
    def password_state(self, user: User) -> str:
        if user.has_usable_password():
            return format_html('<span style="color:#047857">set</span>')
        return format_html('<span style="color:#b45309">not set — invitation outstanding</span>')

    @admin.action(description="Send a password invitation / reset link")
    def send_invitation(self, request, queryset):  # type: ignore[no-untyped-def]
        """Re-send the link. The commonest thing to need after provisioning.

        The link is addressed to the *tenant's* subdomain, not to whatever host
        this admin page is on — the recipient has to land on their own app.
        """
        sent = 0
        for user in queryset:
            if not user.email and not user.phone:
                continue
            send_password_reset(user, request=request, is_invitation=True)
            sent += 1

        self.message_user(
            request,
            f"Sent to {sent}. In development there is no mail server, so the link "
            "is printed in the backend console.",
            messages.SUCCESS if sent else messages.WARNING,
        )

    @admin.action(description="Deactivate — they can no longer sign in")
    def deactivate(self, request, queryset):  # type: ignore[no-untyped-def]
        changed = queryset.update(is_active=False)
        self.message_user(
            request,
            f"{changed} deactivated. Everything they posted stays on the record.",
            messages.WARNING,
        )

    @admin.action(description="Reactivate")
    def reactivate(self, request, queryset):  # type: ignore[no-untyped-def]
        changed = queryset.update(is_active=True)
        self.message_user(request, f"{changed} reactivated.", messages.SUCCESS)

    # -- who may be here at all ----------------------------------------------

    def has_module_permission(self, request) -> bool:  # type: ignore[no-untyped-def]
        return bool(getattr(request.user, "is_platform_admin", False))

    def has_view_permission(self, request, obj=None) -> bool:  # type: ignore[no-untyped-def]
        return bool(getattr(request.user, "is_platform_admin", False))

    def has_change_permission(self, request, obj=None) -> bool:  # type: ignore[no-untyped-def]
        return bool(getattr(request.user, "is_platform_admin", False))

    def has_add_permission(self, request) -> bool:  # type: ignore[no-untyped-def]
        # A tenant's people are added by that tenant, with roles chosen at the
        # same time. An account created here would have none.
        return False

    def has_delete_permission(self, request, obj=None) -> bool:  # type: ignore[no-untyped-def]
        return False
