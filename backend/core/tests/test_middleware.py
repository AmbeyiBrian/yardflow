"""T1.5 — TenantMiddleware and the Celery task base (§2.2, A1, A3)."""

import pytest
from celery import shared_task
from django.http import Http404, HttpResponse
from django.test import RequestFactory

from accounts.models import User
from core.factories import OrganizationFactory
from core.middleware import TenantMiddleware, extract_subdomain
from core.tasks import TenantTask
from core.tenancy import (
    TenantContextMissing,
    get_current_organization_id,
    get_database_organization,
)


class TestExtractSubdomain:
    """Host parsing, kept separate because it is pure and worth pinning down."""

    @pytest.mark.parametrize(
        ("host", "base", "expected"),
        [
            ("silvertech.localhost", "localhost", "silvertech"),
            ("silvertech.localhost:5173", "localhost", "silvertech"),
            ("silvertech.yardflow.co.ke", "yardflow.co.ke", "silvertech"),
            # Several labels: the one closest to the base domain is the tenant.
            ("staging.silvertech.yardflow.co.ke", "yardflow.co.ke", "silvertech"),
            ("admin.yardflow.co.ke", "yardflow.co.ke", "admin"),
            # No tenant label at all.
            ("localhost", "localhost", None),
            ("yardflow.co.ke", "yardflow.co.ke", None),
            ("127.0.0.1", "localhost", None),
            ("testserver", "localhost", None),
            # A host that is not under the base domain is not ours to interpret.
            ("silvertech.example.com", "yardflow.co.ke", None),
            ("SILVERTECH.LOCALHOST", "localhost", "silvertech"),
        ],
    )
    def test_subdomain_extraction(self, host, base, expected):
        assert extract_subdomain(host, base) == expected


def _run_middleware(request):
    """Run the middleware around a view that reports what it can see."""
    seen = {}

    def view(_request):
        seen["python_context"] = get_current_organization_id()
        seen["database_setting"] = get_database_organization()
        return HttpResponse("ok")

    response = TenantMiddleware(view)(request)
    return response, seen


@pytest.mark.django_db
class TestSubdomainResolution:
    """A1: a tenant is addressed by its subdomain."""

    def test_subdomain_resolves_the_organization(self, settings):
        settings.TENANT_BASE_DOMAIN = "localhost"
        organization = OrganizationFactory(slug="silvertech")
        request = RequestFactory().get("/", HTTP_HOST="silvertech.localhost")
        request.user = None

        response, seen = _run_middleware(request)

        assert response.status_code == 200
        assert request.organization == organization
        assert seen["python_context"] == organization.pk

    def test_the_database_setting_is_published_for_row_level_security(self, settings):
        """§2.3: the policies read app.current_org, so it must be set."""
        settings.TENANT_BASE_DOMAIN = "localhost"
        organization = OrganizationFactory(slug="silvertech")
        request = RequestFactory().get("/", HTTP_HOST="silvertech.localhost")
        request.user = None

        _, seen = _run_middleware(request)

        assert seen["database_setting"] == str(organization.pk)

    def test_an_unknown_subdomain_is_404(self, settings):
        """Never confirm whether a tenant could exist."""
        settings.TENANT_BASE_DOMAIN = "localhost"
        request = RequestFactory().get("/", HTTP_HOST="nobody.localhost")
        request.user = None

        with pytest.raises(Http404):
            _run_middleware(request)

    def test_no_subdomain_leaves_no_organization_active(self, settings):
        settings.TENANT_BASE_DOMAIN = "localhost"
        request = RequestFactory().get("/", HTTP_HOST="localhost")
        request.user = None

        _, seen = _run_middleware(request)

        assert seen["python_context"] is None

    def test_the_platform_console_subdomain_resolves_no_tenant(self, settings):
        """§2.2: the admin console is deliberately cross-tenant."""
        settings.TENANT_BASE_DOMAIN = "localhost"
        settings.PLATFORM_ADMIN_SUBDOMAIN = "admin"
        request = RequestFactory().get("/", HTTP_HOST="admin.localhost")
        request.user = None

        _, seen = _run_middleware(request)

        assert request.is_platform_console is True
        assert seen["python_context"] is None


@pytest.mark.django_db
class TestUserFallback:
    """§2.2: resolution falls back to the authenticated user's organization."""

    def test_user_organization_is_used_when_no_subdomain_addresses_one(self, settings):
        settings.TENANT_BASE_DOMAIN = "localhost"
        organization = OrganizationFactory(slug="silvertech")
        user = User.objects.create_user(email="store@example.com", organization=organization)
        request = RequestFactory().get("/", HTTP_HOST="localhost")
        request.user = user

        _, seen = _run_middleware(request)

        assert seen["python_context"] == organization.pk

    def test_a_platform_admin_without_an_organization_stays_unscoped(self, settings):
        settings.TENANT_BASE_DOMAIN = "localhost"
        admin = User.objects.create_superuser(email="ops@yardflow.co.ke", password="x")
        request = RequestFactory().get("/", HTTP_HOST="localhost")
        request.user = admin

        _, seen = _run_middleware(request)

        assert seen["python_context"] is None

    def test_an_anonymous_request_stays_unscoped(self, settings):
        from django.contrib.auth.models import AnonymousUser

        settings.TENANT_BASE_DOMAIN = "localhost"
        request = RequestFactory().get("/", HTTP_HOST="localhost")
        request.user = AnonymousUser()

        _, seen = _run_middleware(request)

        assert seen["python_context"] is None


@pytest.mark.django_db
class TestMismatchIsNotFound:
    """A3, §2.4: a subdomain/user disagreement is 404, never 403."""

    def test_user_from_another_tenant_gets_404(self, settings):
        settings.TENANT_BASE_DOMAIN = "localhost"
        OrganizationFactory(slug="silvertech")
        rival = OrganizationFactory(slug="rival")
        intruder = User.objects.create_user(email="them@rival.com", organization=rival)

        request = RequestFactory().get("/", HTTP_HOST="silvertech.localhost")
        request.user = intruder

        # 404 and not 403: a 403 would confirm Silvertech exists.
        with pytest.raises(Http404):
            _run_middleware(request)

    def test_matching_user_and_subdomain_is_allowed(self, settings):
        settings.TENANT_BASE_DOMAIN = "localhost"
        organization = OrganizationFactory(slug="silvertech")
        user = User.objects.create_user(email="store@example.com", organization=organization)

        request = RequestFactory().get("/", HTTP_HOST="silvertech.localhost")
        request.user = user

        response, seen = _run_middleware(request)

        assert response.status_code == 200
        assert seen["python_context"] == organization.pk

    def test_a_platform_admin_may_act_through_a_tenant_subdomain(self, settings):
        """Allowed, and the audit trail records who acted (M3)."""
        settings.TENANT_BASE_DOMAIN = "localhost"
        organization = OrganizationFactory(slug="silvertech")
        admin = User.objects.create_superuser(email="ops@yardflow.co.ke", password="x")

        request = RequestFactory().get("/", HTTP_HOST="silvertech.localhost")
        request.user = admin

        _, seen = _run_middleware(request)

        assert seen["python_context"] == organization.pk


@pytest.mark.django_db
class TestContextDoesNotLeakBetweenRequests:
    """The failure this guards against is the worst kind: intermittent."""

    def test_context_is_cleared_after_the_request(self, settings):
        settings.TENANT_BASE_DOMAIN = "localhost"
        OrganizationFactory(slug="silvertech")
        request = RequestFactory().get("/", HTTP_HOST="silvertech.localhost")
        request.user = None

        _run_middleware(request)

        assert get_current_organization_id() is None

    def test_context_is_cleared_even_when_the_view_raises(self, settings):
        settings.TENANT_BASE_DOMAIN = "localhost"
        OrganizationFactory(slug="silvertech")
        request = RequestFactory().get("/", HTTP_HOST="silvertech.localhost")
        request.user = None

        def failing_view(_request):
            raise RuntimeError("boom")

        with pytest.raises(RuntimeError):
            TenantMiddleware(failing_view)(request)

        assert get_current_organization_id() is None


@shared_task(base=TenantTask, name="tests.needs_org")
def needs_organization_task(organization_id=None):
    return {
        "python_context": get_current_organization_id(),
        "database_setting": get_database_organization(),
    }


@shared_task(base=TenantTask, name="tests.cross_tenant")
def cross_tenant_task():
    """Explicitly cross-tenant, e.g. a sweep that fans out per organization."""
    return get_current_organization_id()


cross_tenant_task.requires_organization = False


@pytest.mark.django_db
class TestTenantTask:
    """§2.2: a task carries its organization explicitly, or refuses to run."""

    def test_a_task_without_an_organization_refuses_to_run(self):
        """A task has no request, so it cannot infer a tenant. Guessing would
        mean operating on the wrong tenant's data, or on everyone's."""
        with pytest.raises(TenantContextMissing, match="explicit organization_id"):
            needs_organization_task()

    def test_a_task_given_an_organization_runs_with_it_in_context(self, organization):
        observed = needs_organization_task(organization_id=organization.pk)

        assert observed["python_context"] == organization.pk
        # Row-level security applies to task work too (§2.3).
        assert observed["database_setting"] == str(organization.pk)

    def test_context_is_cleared_after_the_task(self, organization):
        needs_organization_task(organization_id=organization.pk)

        assert get_current_organization_id() is None

    def test_an_explicitly_cross_tenant_task_may_run_unscoped(self, db):
        assert cross_tenant_task() is None
