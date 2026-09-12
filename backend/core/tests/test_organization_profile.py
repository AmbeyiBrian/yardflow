"""The tenant's own company profile and logo (A4).

The logo field and the templates that print it both existed from the start, and
nothing let a customer upload one — so their branding depended on the platform
owner opening the Django admin for them. That is a support ticket for a picture.

What is tested here is mostly refusal, because the failure this prevents is
slow: a logo that is accepted today and prints as a grey smudge on a gate pass
three weeks later, in front of a client.
"""

from __future__ import annotations

import io

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test.client import BOUNDARY, MULTIPART_CONTENT, encode_multipart
from django.urls import reverse

from accounts.models import Role, User, UserRole
from core.provisioning import provision_tenant
from core.tenancy import tenant_context

pytestmark = pytest.mark.django_db


def png(width: int, height: int) -> bytes:
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGBA", (width, height), (15, 23, 42, 255)).save(buffer, format="PNG")
    return buffer.getvalue()


@pytest.fixture
def signed_in(db, client, settings):
    settings.TENANT_BASE_DOMAIN = "localhost"
    result = provision_tenant(
        name="Silvertech", slug="silvertech", owner_email="owner@silvertech.co.ke"
    )
    owner = result["owner"]
    owner.set_password("a good long password")
    owner.save()
    client.defaults["HTTP_HOST"] = "silvertech.localhost"
    token = client.post(
        reverse("v1:auth:login"),
        {"identifier": "owner@silvertech.co.ke", "password": "a good long password"},
        content_type="application/json",
    ).json()["access"]
    return client, token, result["organization"]


def auth(token):
    return {"HTTP_AUTHORIZATION": f"Bearer {token}"}


def upload(http, token, file):
    """A PATCH carrying a file.

    Django's test client only encodes multipart for POST; left alone a PATCH
    goes out as octet-stream and the endpoint rightly refuses it. A browser
    sending FormData does the right thing on its own.
    """
    return http.patch(
        reverse("v1:organization-profile"),
        data=encode_multipart(BOUNDARY, {"logo": file}),
        content_type=MULTIPART_CONTENT,
        **auth(token),
    )


class TestTheCompanyDetails:
    def test_an_owner_can_change_what_prints_on_documents(self, signed_in):
        http, token, organization = signed_in

        response = http.patch(
            reverse("v1:organization-profile"),
            {"legal_name": "Silvertech Limited", "tax_pin": "P051234567X"},
            content_type="application/json",
            **auth(token),
        )

        assert response.status_code == 200, response.content
        organization.refresh_from_db()
        assert organization.legal_name == "Silvertech Limited"

    def test_anybody_may_read_them(self, signed_in):
        """A screen previewing a document has to know what it will say."""
        http, token, _organization = signed_in

        response = http.get(reverse("v1:organization-profile"), **auth(token))

        assert response.status_code == 200
        assert response.json()["name"] == "Silvertech"

    def test_the_guidance_comes_from_the_server(self, signed_in):
        """So the screen and the print template cannot drift apart about what
        fits."""
        http, token, _organization = signed_in

        guidance = http.get(reverse("v1:organization-profile"), **auth(token)).json()[
            "logo_guidance"
        ]

        assert guidance["print_height_mm"] == 18
        assert guidance["print_width_mm"] == 45
        assert "PNG" in guidance["accepted"]


class TestTheLogo:
    def test_a_good_logo_is_accepted(self, signed_in):
        http, token, organization = signed_in

        response = upload(http, token, SimpleUploadedFile("logo.png", png(900, 360), "image/png"))

        assert response.status_code == 200, response.content
        assert response.json()["logo_url"]
        organization.refresh_from_db()
        assert organization.logo

    def test_something_too_small_to_print_is_refused(self, signed_in):
        """At 18 mm a 64 px image is a grey smudge, and nobody finds out until a
        client is holding the paper."""
        http, token, _organization = signed_in

        response = upload(http, token, SimpleUploadedFile("tiny.png", png(64, 64), "image/png"))

        assert response.status_code == 400
        assert "300" in str(response.json()["error"])

    def test_something_that_is_not_an_image_is_refused(self, signed_in):
        http, token, _organization = signed_in

        response = upload(
            http,
            token,
            SimpleUploadedFile("accounts.pdf", b"%PDF-1.4", "application/pdf"),
        )

        assert response.status_code == 400

    def test_it_can_be_removed(self, signed_in):
        http, token, organization = signed_in
        upload(http, token, SimpleUploadedFile("logo.png", png(900, 360), "image/png"))

        response = http.delete(reverse("v1:organization-profile"), **auth(token))

        assert response.status_code == 204
        organization.refresh_from_db()
        assert not organization.logo, "documents fall back to the company name"


class TestWhoMayChangeIt:
    def test_a_storekeeper_cannot(self, signed_in):
        http, _owner_token, organization = signed_in
        with tenant_context(organization):
            store = User.objects.create_user(
                email="store@silvertech.co.ke",
                password="a good long password",
                organization=organization,
            )
            UserRole.objects.create(
                organization=organization,
                user=store,
                role=Role.objects.get(name="Storekeeper"),
            )
        token = http.post(
            reverse("v1:auth:login"),
            {"identifier": "store@silvertech.co.ke", "password": "a good long password"},
            content_type="application/json",
        ).json()["access"]

        response = http.patch(
            reverse("v1:organization-profile"),
            {"legal_name": "Not Theirs Limited"},
            content_type="application/json",
            **auth(token),
        )

        assert response.status_code == 403


class TestTheUrlTheBrowserGets:
    def test_it_is_absolute(self, signed_in):
        """Development serves media from port 8000 while the app runs on 5173,
        so a bare ``/media/...`` path renders as a broken image. Production
        returns a pre-signed S3 link, which is absolute already."""
        http, token, _organization = signed_in
        upload(http, token, SimpleUploadedFile("logo.png", png(900, 360), "image/png"))

        url = http.get(reverse("v1:organization-profile"), **auth(token)).json()["logo_url"]

        assert url.startswith("http://"), url
