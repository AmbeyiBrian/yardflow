"""URL configuration (design §6).

Conventions:

* everything under ``/api/v1/``
* collections are nouns; state changes are explicit sub-resource ``POST``
  actions, never a ``PATCH`` on ``status``
* the OpenAPI schema is generated, committed, and CI-checked for drift (N-10)
"""

from django.conf import settings
from django.contrib import admin
from django.http import JsonResponse
from django.urls import include, path
from drf_spectacular.utils import extend_schema
from drf_spectacular.views import (
    SpectacularAPIView,
    SpectacularSwaggerView,
)
from rest_framework.routers import DefaultRouter

from accounts.admin_api import (
    DelegationViewSet,
    OrganizationSettingsView,
    PermissionGroupsView,
    RoleViewSet,
    UserViewSet,
)
from accounts.views import (
    LoginView,
    LogoutView,
    MeView,
    PasswordResetConfirmView,
    PasswordResetRequestView,
    PermissionCatalogueView,
    RefreshView,
)
from accounts.webauthn_views import (
    AssertBeginView,
    CredentialsView,
    RegisterBeginView,
    RegisterCompleteView,
)
from approvals.views import ApprovalRequestViewSet, ApprovalRuleViewSet
from catalogue.views import (
    CategoryCustomFieldViewSet,
    ItemCategoryViewSet,
    ItemTypeViewSet,
)
from core.attachment_api import AttachmentTargetsView, AttachmentViewSet
from core.organization_api import OrganizationProfileView
from core.views import attachment_download
from custody.views import (
    CustodyExpectationViewSet,
    CustodyHoldingsView,
    CustodyTransferViewSet,
    OverdueCustodyView,
)
from dispatch.views import GateOutViewSet, QrScanView, ReleaseVarianceViewSet
from disposition.views import (
    ClientPositionView as ClientReturnPositionView,
)
from disposition.views import (
    ClientReturnAckViewSet,
    DisposalViewSet,
    DispositionViewSet,
    QuarantineView,
)
from jobs.views import (
    ExceptionsView,
    JobCloseoutViewSet,
    JobViewSet,
    ReconciliationView,
    VarianceViewSet,
)
from locations.views import LocationViewSet, StockNodeViewSet
from network.views import (
    ClientViewSet,
    SiteReferenceViewSet,
    SiteViewSet,
    WorkOrderViewSet,
)
from notifications.views import NotificationPreferencesView, NotificationViewSet
from receiving.views import GateInViewSet
from reporting.views import (
    ReportCatalogueView,
    ReportExportView,
    ReportView,
    RetentionReviewView,
)
from stock.views import (
    ClientPositionView,
    CustodyStockView,
    InstalledBaseView,
    LowStockView,
    MovementViewSet,
    ReelHistoryView,
    ReelViewSet,
    SerialHistoryView,
    SerialUnitViewSet,
    StockCountViewSet,
    StockLookupView,
    StockOnHandView,
    TransferView,
)
from sync.views import (
    OfflineBundleView,
    SyncExceptionViewSet,
    SyncSubmissionsView,
    SyncSubmissionViewSet,
)


class ScopedSchemaView(SpectacularAPIView):
    """Serve the schema with the context filter introspection needs (N-10).

    The endpoint may be reached without a tenant — on the platform console
    subdomain, or by a client generator — and django-filter touches model
    managers while resolving lookups. See `core.schema`.
    """

    # Overriding `get` loses SpectacularAPIView's own exclusion decorator, so
    # the schema endpoint would otherwise appear inside the schema it serves.
    @extend_schema(exclude=True)
    def get(self, request, *args, **kwargs):  # type: ignore[no-untyped-def]
        from core.schema import schema_generation_context

        with schema_generation_context():
            return super().get(request, *args, **kwargs)


def health(request):
    """Liveness probe for the ALB (§12.1).

    Deliberately touches no database, so a probe stays cheap and a database
    blip does not take healthy tasks out of service.
    """
    return JsonResponse({"status": "ok"})


# §5.3, D9: WebAuthn is a step-up on the approval action, not a login. The
# assertion is bound to one ApprovalRequest so it cannot be replayed (T8.9).
webauthn_patterns = [
    path("register/begin", RegisterBeginView.as_view(), name="webauthn-register-begin"),
    path(
        "register/complete",
        RegisterCompleteView.as_view(),
        name="webauthn-register-complete",
    ),
    path("assert/begin", AssertBeginView.as_view(), name="webauthn-assert-begin"),
    # `assert/complete` is deliberately absent: completing an assertion *is*
    # approving, so it happens on `POST /gate-outs/{id}/approve` with the
    # assertion attached. A separate endpoint would mean a verified assertion
    # existing for a moment with nothing bound to it.
    path("credentials", CredentialsView.as_view(), name="webauthn-credentials"),
]

auth_patterns = [
    path("login", LoginView.as_view(), name="login"),
    path("refresh", RefreshView.as_view(), name="token-refresh"),
    path("logout", LogoutView.as_view(), name="logout"),
    path("password-reset", PasswordResetRequestView.as_view(), name="password-reset"),
    path(
        "password-reset/confirm",
        PasswordResetConfirmView.as_view(),
        name="password-reset-confirm",
    ),
    path("webauthn/", include((webauthn_patterns, "webauthn"))),
]

# Master data (Phase 2). Registered on a router so T1.20's isolation suite
# discovers each endpoint automatically rather than being told about it.
# `trailing_slash=False`, so every URL in this project has exactly one form.
#
# Found from the browser: the frontend calls `/api/v1/gate-outs`, the router only
# answered `/api/v1/gate-outs/`, and `APPEND_SLASH` cannot redirect a POST
# without losing its body — so Django raised, and **every write from the client
# was a 500**. Nothing caught it because the backend tests reverse() their URLs
# and the earlier HTTP walk-throughs happened to type the slash.
#
# The no-slash form is the one §6 documents (`POST /gate-outs/{id}/submit`) and
# the one the hand-written paths below already use, so this is the router being
# brought into line with the rest of the API rather than a new convention.
router = DefaultRouter(trailing_slash=False)
# Administration (B3, B4, C8). Registered before the master data so the
# isolation suite sees them in the same sweep.
router.register("users", UserViewSet, basename="user")
router.register("roles", RoleViewSet, basename="role")
router.register("delegations", DelegationViewSet, basename="delegation")

router.register("item-categories", ItemCategoryViewSet, basename="item-category")
router.register(
    "category-custom-fields", CategoryCustomFieldViewSet, basename="category-custom-field"
)
router.register("item-types", ItemTypeViewSet, basename="item-type")
router.register("clients", ClientViewSet, basename="client")
router.register("sites", SiteViewSet, basename="site")
router.register("site-references", SiteReferenceViewSet, basename="site-reference")
router.register("work-orders", WorkOrderViewSet, basename="work-order")
router.register("locations", LocationViewSet, basename="location")
router.register("stock-nodes", StockNodeViewSet, basename="stock-node")

# Dispatch and approvals (Phase 4). §6: state changes are POST sub-resources on
# the document, never a PATCH on status.
# Receiving and stock (Phase 3). D8: a gate-in is created as a draft and posted
# by an action, because posting is what moves stock.
router.register("gate-ins", GateInViewSet, basename="gate-in")
router.register("movements", MovementViewSet, basename="movement")
router.register("serials", SerialUnitViewSet, basename="serial")
router.register("drums", ReelViewSet, basename="drum")
router.register("stock-counts", StockCountViewSet, basename="stock-count")

router.register("gate-outs", GateOutViewSet, basename="gate-out")
router.register("release-variances", ReleaseVarianceViewSet, basename="release-variance")
router.register("approval-rules", ApprovalRuleViewSet, basename="approval-rule")
router.register("approvals", ApprovalRequestViewSet, basename="approval")

# L1: in-app is the channel that is always on, so it has an endpoint.
router.register("notifications", NotificationViewSet, basename="notification")

# Jobs and custody (Phase 5). §6: closing a job and submitting a closeout are
# POST actions on the document, so `status` is never writable.
router.register("jobs", JobViewSet, basename="job")
router.register("job-closeouts", JobCloseoutViewSet, basename="job-closeout")
router.register("variances", VarianceViewSet, basename="variance")
router.register(
    "custody-expectations", CustodyExpectationViewSet, basename="custody-expectation"
)
router.register("custody-transfers", CustodyTransferViewSet, basename="custody-transfer")

# §4.13, N-7: evidence. Listing is always by target — see the viewset for why an
# unfiltered list of a tenant's files is not something to offer.
router.register("attachments", AttachmentViewSet, basename="attachment")

# Disposition, disposal and client returns (Phase 6). §4.11: deciding that
# something is scrap and destroying it are two documents, because they are two
# decisions taken at different times by different people.
router.register("dispositions", DispositionViewSet, basename="disposition")
router.register("disposals", DisposalViewSet, basename="disposal")
router.register(
    "client-return-acks", ClientReturnAckViewSet, basename="client-return-ack"
)

# N1, N3: what a device has sent, and what could not be applied (§8.2, §8.4).
router.register("sync-submissions", SyncSubmissionViewSet, basename="sync-submission")
router.register("sync-exceptions", SyncExceptionViewSet, basename="sync-exception")

v1_patterns = [
    path("auth/", include((auth_patterns, "auth"))),
    path("me", MeView.as_view(), name="me"),
    path("permissions", PermissionCatalogueView.as_view(), name="permission-catalogue"),
    # C8: one settings object per tenant, so a singleton rather than a collection.
    path("settings", OrganizationSettingsView.as_view(), name="organization-settings"),
    # A4: the company's own details and logo, managed by the company.
    path(
        "organization",
        OrganizationProfileView.as_view(),
        name="organization-profile",
    ),
    path(
        "permission-groups", PermissionGroupsView.as_view(), name="permission-groups"
    ),
    # G5: scanning a gate pass at the gate opens the right document.
    path("qr/scan", QrScanView.as_view(), name="qr-scan"),
    # E1: one endpoint for "do we have it, where is it, whose is it".
    path("stock", StockOnHandView.as_view(), name="stock-on-hand"),
    # D7, E2: a scanned identifier, whatever kind it turns out to be.
    path("stock/lookup", StockLookupView.as_view(), name="stock-lookup"),
    path(
        "stock/serials/<str:serial_number>/history",
        SerialHistoryView.as_view(),
        name="serial-history",
    ),
    path(
        "stock/drums/<str:drum_number>/history",
        ReelHistoryView.as_view(),
        name="reel-history",
    ),
    path("stock/low", LowStockView.as_view(), name="stock-low"),
    path("stock/client-position", ClientPositionView.as_view(), name="client-position"),
    path("stock/installed", InstalledBaseView.as_view(), name="installed-base"),
    path("stock/custody", CustodyStockView.as_view(), name="stock-custody"),
    path("stock/transfers", TransferView.as_view(), name="stock-transfer"),
    # H4: the operator's question, answered for a site or a work order.
    path("reconciliation", ReconciliationView.as_view(), name="reconciliation"),
    # L2: what a user will be told, and through which channel.
    path(
        "notification-preferences",
        NotificationPreferencesView.as_view(),
        name="notification-preferences",
    ),
    # M1, M2: the day-one report set. One catalogue endpoint and two per-report
    # endpoints, for every report there will ever be (§10, T7.1).
    path("reports", ReportCatalogueView.as_view(), name="report-catalogue"),
    path("reports/<str:slug>", ReportView.as_view(), name="report"),
    path(
        "reports/<str:slug>/export",
        ReportExportView.as_view(),
        name="report-export",
    ),
    # M5: expiry produces a review list and nothing else. There is deliberately
    # no endpoint here that deletes.
    path(
        "retention-review",
        RetentionReviewView.as_view(),
        name="retention-review",
    ),
    # §8.1, §8.2: the queue drains here, and a device fills its cache from the
    # bundle before it loses signal.
    path("sync/submissions", SyncSubmissionsView.as_view(), name="sync-submissions"),
    path("sync/bundle", OfflineBundleView.as_view(), name="sync-bundle"),
    # M1: one register, so there is one place to look rather than three.
    path("exceptions", ExceptionsView.as_view(), name="exceptions"),
    # J1, J2: what is in quarantine and how long it has been there. The age is
    # the point — quarantine becomes a graveyard when nobody is looking.
    path("quarantine", QuarantineView.as_view(), name="quarantine"),
    # K1, K3: held, in transit, acknowledged. Three states, because one number
    # is not a defensible answer to an operator audit.
    path(
        "client-position",
        ClientReturnPositionView.as_view(),
        name="client-return-position",
    ),
    path("custody/holdings", CustodyHoldingsView.as_view(), name="custody-holdings"),
    path("custody/overdue", OverdueCustodyView.as_view(), name="custody-overdue"),
    # D6, G3, H2: so a screen knows whether to offer a camera button before the
    # user taps it, from the same map the upload enforces.
    path(
        "attachment-targets",
        AttachmentTargetsView.as_view(),
        name="attachment-targets",
    ),
    *router.urls,
]

urlpatterns = [
    path("health", health, name="health"),
    # N-7: attachments are never publicly readable. This is the local
    # counterpart of an S3 pre-signed GET, so the contract is the same in
    # development and production.
    path(
        "attachments/<str:token>/download",
        attachment_download,
        name="attachment-download",
    ),
    path("api/v1/", include((v1_patterns, "v1"))),
    # D7: no self-serve signup in v1. Tenants are onboarded through this
    # cross-tenant console, which is restricted to platform admins (A1, A2).
    path("admin-api/", include(("platform_admin.urls", "platform-admin"))),
    # N-10: OpenAPI, so future integrations are straightforward (D19).
    path("api/v1/schema", ScopedSchemaView.as_view(), name="schema"),
    path(
        "api/v1/docs",
        SpectacularSwaggerView.as_view(url_name="schema"),
        name="swagger-ui",
    ),
    # §1.1: the Django admin site is a free internal tool. It is not a tenant
    # surface — tenant users reach nothing through it.
    path("django-admin/", admin.site.urls),
]

if settings.DEBUG:
    from django.conf.urls.static import static

    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
