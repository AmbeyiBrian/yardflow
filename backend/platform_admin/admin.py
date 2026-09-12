"""The platform owner's view of every tenant (§2.4, A1, A2, B6).

Support questions arrive as "nothing is happening", "the SMS never came", "the
stock is wrong". Answering them meant opening a database client. This is the
same information, in the place the platform owner already signs into.

Three rules hold everywhere in this module, and they are what make a cross-tenant
console defensible:

**Read-only. All of it.** No add, no change, no delete, on anything registered
here. The Django admin has no tenant context, so a form saved from it would write
a row nobody scoped — and the stock ledger and the audit trail are append-only by
design, which a change form silently contradicts. Support needs to *see*; the
tenant's own app is where things are done.

**One tenant at a time — because the database insists.** Policies are created
with `FORCE ROW LEVEL SECURITY`, so Postgres applies them to the table owner as
well; there is no role in this system that can read two tenants' rows in one
query, and that is the guarantee the whole design rests on (§2.3). `all_objects`
bypasses Django's manager, not the database. So the console *selects* a tenant,
publishes it for the request the way an ordinary request does, and shows that
tenant's rows. Choose from the Organization filter; the choice is remembered as
you move between screens.

That is not a workaround grudgingly accepted. Support is always about one named
company, and a screen that cannot mix two companies' rows cannot answer a
question about the wrong one.
"""

from __future__ import annotations

from django.contrib import admin, messages
from django.template.response import TemplateResponse

from accounts.models import Role, UserRole
from approvals.models import ApprovalAction, ApprovalRequest
from catalogue.models import ItemType
from core.models import AuditLog
from custody.models import CustodyExpectation
from dispatch.models import GateOut, ReleaseVariance
from disposition.models import Disposal
from jobs.models import Job
from locations.models import Location
from notifications.models import NotificationDelivery, SmsCreditEntry
from receiving.models import GateIn
from stock.models import StockBalance, StockMovement
from sync.models import SyncException, SyncSubmission

#: Where the chosen tenant is remembered between screens.
SESSION_KEY = "platform_admin.organization_id"


class PlatformReadOnly(admin.ModelAdmin):
    """Look, do not touch — and at one tenant at a time."""

    # A list rather than a tuple: `ModelAdmin` declares this as a class
    # variable, and subclasses extend it to pull in the rows their own columns
    # show. Annotating it as an instance variable is what mypy objects to, so
    # this matches the base class's own shape.
    list_select_related = ["organization"]
    list_per_page = 50

    def get_queryset(self, request):  # type: ignore[no-untyped-def]
        # `all_objects` rather than `objects`: the scoped manager raises when no
        # tenant is in context, and an empty list is a better answer than a 500.
        # Row-level security is what actually restricts this to the chosen
        # tenant, which is why it is safe for the manager to be permissive here.
        return self.model.all_objects.select_related("organization")

    # -- choosing a tenant ---------------------------------------------------

    def _selected_organization_id(self, request):  # type: ignore[no-untyped-def]
        """The tenant being looked at: from the filter, else the last one."""
        chosen = request.GET.get("organization__id__exact")
        if chosen:
            request.session[SESSION_KEY] = chosen
            return chosen
        return request.session.get(SESSION_KEY)

    def _within_tenant(self, request, render):  # type: ignore[no-untyped-def]
        """Run a view with the chosen tenant published to Postgres.

        The response has to be **rendered inside** the context. A
        `TemplateResponse` is lazy — the querysets in it are evaluated when the
        middleware renders it, by which time the context would be gone and every
        list would be empty.
        """
        from core.models import Organization
        from core.tenancy import tenant_context

        organization_id = self._selected_organization_id(request)
        if not organization_id:
            messages.info(
                request,
                "Choose a tenant from the Organization filter on the right. "
                "The database refuses to read two tenants in one query, by "
                "design, so this list stays empty until you pick one.",
            )
            return render()

        if not Organization.objects.filter(pk=organization_id).exists():
            request.session.pop(SESSION_KEY, None)
            messages.warning(request, "That tenant no longer exists.")
            return render()

        with tenant_context(organization_id):
            response = render()
            if isinstance(response, TemplateResponse):
                response.render()
            return response

    def changelist_view(self, request, extra_context=None):  # type: ignore[no-untyped-def]
        return self._within_tenant(
            request, lambda: super(PlatformReadOnly, self).changelist_view(
                request, extra_context
            )
        )

    def change_view(self, request, object_id, form_url="", extra_context=None):  # type: ignore[no-untyped-def]
        return self._within_tenant(
            request,
            lambda: super(PlatformReadOnly, self).change_view(
                request, object_id, form_url, extra_context
            ),
        )

    def has_add_permission(self, request) -> bool:  # type: ignore[no-untyped-def]
        return False

    def has_change_permission(self, request, obj=None) -> bool:  # type: ignore[no-untyped-def]
        return False

    def has_delete_permission(self, request, obj=None) -> bool:  # type: ignore[no-untyped-def]
        return False

    def has_view_permission(self, request, obj=None) -> bool:  # type: ignore[no-untyped-def]
        return bool(getattr(request.user, "is_platform_admin", False))

    def has_module_permission(self, request) -> bool:  # type: ignore[no-untyped-def]
        return bool(getattr(request.user, "is_platform_admin", False))


# --------------------------------------------------------------------------
# "Who did that, and when?" — B6
# --------------------------------------------------------------------------


@admin.register(AuditLog)
class AuditLogAdmin(PlatformReadOnly):
    """The first place to look for almost any question about a past action."""

    list_display = (
        "occurred_at",
        "organization",
        "action",
        "actor_identifier",
        "target_label",
    )
    list_filter = ("action", "organization", "occurred_at")
    search_fields = ("actor_identifier", "target_label", "target_id")
    date_hierarchy = "occurred_at"


# --------------------------------------------------------------------------
# "The message never arrived" — L1, L3, L4
# --------------------------------------------------------------------------


@admin.register(NotificationDelivery)
class NotificationDeliveryAdmin(PlatformReadOnly):
    """Answers the commonest support question there is.

    `status` and `last_error` together say whether it was sent, refused, retried
    or never attempted — and for SMS, whether the tenant had run out of credit.
    """

    list_display = (
        "created_at",
        "organization",
        "channel",
        "status",
        "recipient",
        "attempts",
    )
    list_filter = ("channel", "status", "organization", "created_at")
    search_fields = ("destination", "subject", "last_error")
    date_hierarchy = "created_at"


@admin.register(SmsCreditEntry)
class SmsCreditEntryAdmin(PlatformReadOnly):
    """The billing ledger: what a tenant bought, and what it went on."""

    list_display = (
        "created_at",
        "organization",
        "kind",
        "quantity",
        "note",
        "created_by",
    )
    list_filter = ("kind", "organization", "created_at")
    search_fields = ("note",)
    date_hierarchy = "created_at"


# --------------------------------------------------------------------------
# "They cannot do X" — B3, B4
# --------------------------------------------------------------------------


@admin.register(Role)
class RoleAdmin(PlatformReadOnly):
    list_display = ("name", "organization", "is_system", "created_at")
    list_filter = ("is_system", "organization")
    search_fields = ("name",)


@admin.register(UserRole)
class UserRoleAdmin(PlatformReadOnly):
    """Which people hold which role — the answer to most permission questions."""

    list_display = ("user", "role", "organization", "created_at")
    # Only `organization`, deliberately. A filter on `role` builds its dropdown
    # with the model's *default* manager, which is tenant-scoped and raises
    # `TenantContextMissing` in a console that belongs to no tenant. Filter by
    # tenant and search by person; the role is on the row either way.
    list_filter = ("organization",)
    search_fields = ("user__email", "user__phone", "user__full_name", "role__name")
    list_select_related = ["organization", "user", "role"]


# --------------------------------------------------------------------------
# "The stock is wrong" — D8, M1
# --------------------------------------------------------------------------


@admin.register(StockMovement)
class StockMovementAdmin(PlatformReadOnly):
    """The ledger. Append-only in the database, and read-only here to match."""

    list_display = (
        "occurred_at",
        "organization",
        "movement_type",
        "item_type",
        "quantity",
        "from_node",
        "to_node",
        "posted_by",
    )
    list_filter = ("movement_type", "organization", "occurred_at")
    search_fields = ("item_type__name", "item_type__code")
    date_hierarchy = "occurred_at"


@admin.register(StockBalance)
class StockBalanceAdmin(PlatformReadOnly):
    """The cache the screens read. When it disagrees with the ledger, the ledger
    is right — `manage.py verify_ledger` is what settles it."""

    list_display = (
        "organization",
        "node",
        "item_type",
        "condition",
        "quantity",
        "owner_client",
    )
    list_filter = ("organization", "condition")
    search_fields = ("item_type__name",)


# --------------------------------------------------------------------------
# "The gate pass is stuck" — F1, F5, G1
# --------------------------------------------------------------------------


@admin.register(GateOut)
class GateOutAdmin(PlatformReadOnly):
    list_display = (
        "number",
        "organization",
        "status",
        "purpose_type",
        "custody_holder",
        "created_at",
    )
    list_filter = ("status", "purpose_type", "organization")
    search_fields = ("number",)
    date_hierarchy = "created_at"


@admin.register(ApprovalRequest)
class ApprovalRequestAdmin(PlatformReadOnly):
    """Why something is waiting: which level, which role, and since when."""

    list_display = (
        "document_number",
        "organization",
        "status",
        "level",
        "required_role",
        "due_at",
        "escalated_at",
    )
    list_filter = ("status", "organization", "document_type")
    search_fields = ("document_number",)


@admin.register(ApprovalAction)
class ApprovalActionAdmin(PlatformReadOnly):
    """Who decided, on whose behalf, and with what authentication — the record a
    dispute turns on."""

    list_display = (
        "decided_at",
        "organization",
        "decision",
        "actor",
        "on_behalf_of",
        "auth_method",
    )
    list_filter = ("decision", "auth_method", "organization")
    date_hierarchy = "decided_at"


@admin.register(ReleaseVariance)
class ReleaseVarianceAdmin(PlatformReadOnly):
    list_display = (
        "created_at",
        "organization",
        "approved_qty",
        "released_qty",
        "reason",
        "recorded_by",
    )
    list_filter = ("organization", "created_at")


# --------------------------------------------------------------------------
# "The delivery was not received" — D8
# --------------------------------------------------------------------------


@admin.register(GateIn)
class GateInAdmin(PlatformReadOnly):
    list_display = (
        "number",
        "organization",
        "status",
        "source_type",
        "supplier_name",
        "received_at",
    )
    list_filter = ("status", "source_type", "organization")
    search_fields = ("number", "supplier_name")
    date_hierarchy = "created_at"


# --------------------------------------------------------------------------
# "Material is unaccounted for" — H4, I3, J3
# --------------------------------------------------------------------------


@admin.register(Job)
class JobAdmin(PlatformReadOnly):
    list_display = ("reference", "organization", "status", "assignee", "site", "closed_at")
    list_filter = ("status", "organization")
    search_fields = ("reference",)


@admin.register(CustodyExpectation)
class CustodyExpectationAdmin(PlatformReadOnly):
    """Who is holding what, and whether it is late."""

    list_display = (
        "organization",
        "holder",
        "item_type",
        "quantity",
        "returned_quantity",
        "expected_return_date",
        "status",
    )
    list_filter = ("status", "organization")
    search_fields = ("holder__full_name", "holder__email", "item_type__name")


@admin.register(Disposal)
class DisposalAdmin(PlatformReadOnly):
    list_display = ("number", "organization", "status", "method", "handler_name", "approved_at")
    list_filter = ("status", "method", "organization")
    search_fields = ("number", "handler_name")


# --------------------------------------------------------------------------
# "It was captured on the phone and never appeared" — N1, N3
# --------------------------------------------------------------------------


@admin.register(SyncSubmission)
class SyncSubmissionAdmin(PlatformReadOnly):
    """An offline capture and what became of it. `client_uuid` is what makes a
    resend idempotent, so it is the field to search when somebody says they
    submitted twice."""

    list_display = (
        "captured_at",
        "organization",
        "operation",
        "status",
        "document_number",
        "submitted_by",
    )
    list_filter = ("status", "operation", "organization")
    search_fields = ("client_uuid", "document_number")
    date_hierarchy = "captured_at"


@admin.register(SyncException)
class SyncExceptionAdmin(PlatformReadOnly):
    """What the yard refused, and why."""

    list_display = ("created_at", "organization", "code", "status", "reason", "resolved_by")
    list_filter = ("status", "code", "organization")
    search_fields = ("reason",)


# --------------------------------------------------------------------------
# "Why can they not find the item / the location?" — C1–C7
# --------------------------------------------------------------------------


@admin.register(ItemType)
class ItemTypeAdmin(PlatformReadOnly):
    list_display = (
        "name",
        "organization",
        "code",
        "category",
        "default_tracking_mode",
        "uom",
    )
    list_filter = ("default_tracking_mode", "organization")
    search_fields = ("name", "code")


@admin.register(Location)
class LocationAdmin(PlatformReadOnly):
    list_display = ("name", "organization", "code", "type", "is_active", "is_system")
    list_filter = ("type", "is_active", "organization")
    search_fields = ("name", "code")
