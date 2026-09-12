"""Cross-tenant console routes (design §6, requirements A1, A2)."""

from rest_framework.routers import DefaultRouter

from platform_admin.views import OrganizationViewSet

router = DefaultRouter()
router.register("organizations", OrganizationViewSet, basename="organization")

urlpatterns = router.urls
