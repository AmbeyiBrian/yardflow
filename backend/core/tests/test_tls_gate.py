"""The certificate gate the reverse proxy asks (§12.2).

The DNS record for a tenant domain is a **wildcard**, so every name under it
reaches the box whether or not a tenant of that name exists. Caddy issues
certificates on demand, and without a gate it would try to obtain one for each
of those names — a few thousand requests to random subdomains would exhaust the
certificate authority's rate limit for the whole domain, and the next real
tenant would be unable to get HTTPS at all.

So the proxy asks first, and these tests pin what it is told.
"""

import pytest
from django.urls import reverse

from core.provisioning import provision_tenant


@pytest.fixture
def tenant(db, settings):
    settings.TENANT_BASE_DOMAIN = "yardflow.buniva.co.ke"
    return provision_tenant(
        name="Silvertech Networks Limited",
        slug="silvertech",
        owner_email="owner@silvertech.co.ke",
    )["organization"]


def ask(client, domain):
    return client.get(reverse("tls-allowed"), {"domain": domain})


@pytest.mark.django_db
class TestWhatGetsACertificate:
    def test_a_real_tenant_does(self, client, tenant):
        assert ask(client, "silvertech.yardflow.buniva.co.ke").status_code == 200

    def test_the_base_domain_does(self, client, tenant):
        """It serves the redirect for somebody who typed the bare address."""
        assert ask(client, "yardflow.buniva.co.ke").status_code == 200

    def test_a_tenant_that_does_not_exist_does_not(self, client, tenant):
        """The one that matters: this is the whole point of asking."""
        assert ask(client, "nonsense.yardflow.buniva.co.ke").status_code == 403

    def test_a_host_outside_the_domain_does_not(self, client, tenant):
        """Somebody else pointing their DNS at this box does not get a
        certificate out of it."""
        assert ask(client, "yardflow.example.com").status_code == 403

    def test_no_domain_at_all_is_refused(self, client, tenant):
        assert client.get(reverse("tls-allowed")).status_code == 403

    def test_a_suspended_tenant_still_does(self, client, tenant):
        """A2 leaves a suspended tenant able to log in and read. Refusing the
        certificate would lock them out of records they are still entitled to
        see, which suspension is not supposed to do."""
        from core.models import Organization

        Organization.objects.filter(pk=tenant.pk).update(
            status=Organization.Status.SUSPENDED
        )

        assert ask(client, "silvertech.yardflow.buniva.co.ke").status_code == 200

    def test_the_case_of_the_host_does_not_matter(self, client, tenant):
        """Host headers arrive in whatever case the client sent."""
        assert ask(client, "SilverTech.YardFlow.Buniva.Co.Ke").status_code == 200


@pytest.mark.django_db
class TestItAnswersOverPlainHttp:
    """The failure that made every certificate impossible.

    Caddy asks this question over HTTP on the container network, because it is
    asked *during* a TLS handshake — there is no certificate for the hostname
    yet, so there is nothing to ask over. Production redirects everything to
    HTTPS, Caddy refuses to follow redirects on this call, and the result was
    that no hostname could ever be issued a certificate: the gate protecting
    certificate issuance was blocking all of it.
    """

    def test_it_is_not_redirected_to_https(self, client, tenant, settings):
        settings.SECURE_SSL_REDIRECT = True

        response = ask(client, "silvertech.yardflow.buniva.co.ke")

        assert response.status_code == 200, (
            "the proxy asks over plain HTTP and will not follow a redirect"
        )

    def test_the_health_probe_is_not_redirected_either(self, client, settings):
        """Probed inside the container network, where there is no TLS."""
        settings.SECURE_SSL_REDIRECT = True

        assert client.get(reverse("health")).status_code == 200

    def test_an_ordinary_endpoint_still_is(self, client, settings):
        """The exemption is two paths, not a hole in the TLS policy."""
        settings.SECURE_SSL_REDIRECT = True

        response = client.get("/api/v1/projects")

        assert response.status_code in (301, 302)
        assert response["Location"].startswith("https://")


@pytest.mark.django_db
class TestItCostsNothingToAsk:
    def test_it_needs_no_session(self, client, tenant):
        """It is asked *during* the handshake, so there is no session to have.

        What it discloses — whether a slug is in use — the login page at that
        address discloses anyway.
        """
        response = ask(client, "silvertech.yardflow.buniva.co.ke")

        assert response.status_code == 200
        assert "Set-Cookie" not in response
