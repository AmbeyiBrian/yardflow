"""The attachment endpoint (§6, §4.13; D6, G3, H2, N-7).

T5.10 needs a technician to close out a job "including two site photos", which
is the first thing in the product that uploads a file. The storage layer was
already tested (`test_attachments.py`); these are the tests for the way in, and
most of them are refusals — an upload endpoint's interesting behaviour is what it
declines.
"""

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from core.provisioning import provision_tenant
from core.tenancy import tenant_context


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
    return client, token, result["organization"], owner


def auth(token):
    return {"HTTP_AUTHORIZATION": f"Bearer {token}"}


def a_photo(name="site.jpg"):
    return SimpleUploadedFile(name, b"\xff\xd8\xff\xd9 pretend jpeg", content_type="image/jpeg")


def a_job(organization, assignee, ref="JOB-ATT-1"):
    from jobs.models import Job
    from network.factories import SiteFactory

    with tenant_context(organization):
        site = SiteFactory(internal_ref=f"ATT-{ref}", name="Attachment site")
        return Job.objects.create(
            organization=organization,
            reference=ref,
            client=site.client,
            site=site,
            assignee=assignee,
        )


class TestUploading:
    def test_a_photo_is_stored_against_its_record(self, signed_in):
        """H2: the evidence a job was actually done."""
        http, token, organization, owner = signed_in
        job = a_job(organization, owner)

        response = http.post(
            reverse("v1:attachment-list"),
            {"target_type": "jobs.Job", "target_id": str(job.pk), "file": a_photo()},
            **auth(token),
        )

        assert response.status_code == 201, response.content
        body = response.json()
        assert body["filename"] == "site.jpg"
        assert body["kind"] == "PHOTO"
        assert body["uploaded_by"] == owner.pk
        # N-7: what comes back is a signed link, never a public path.
        assert body["download_url"]
        assert "/media/" not in body["download_url"]

    def test_the_listing_is_by_target(self, signed_in):
        http, token, organization, owner = signed_in
        job = a_job(organization, owner, ref="JOB-ATT-2")
        other = a_job(organization, owner, ref="JOB-ATT-3")

        http.post(
            reverse("v1:attachment-list"),
            {"target_type": "jobs.Job", "target_id": str(job.pk), "file": a_photo("one.jpg")},
            **auth(token),
        )
        http.post(
            reverse("v1:attachment-list"),
            {
                "target_type": "jobs.Job",
                "target_id": str(other.pk),
                "file": a_photo("two.jpg"),
            },
            **auth(token),
        )

        response = http.get(
            reverse("v1:attachment-list"),
            {"target_type": "jobs.Job", "target_id": str(job.pk)},
            **auth(token),
        )

        assert response.status_code == 200
        assert [row["filename"] for row in response.json()["results"]] == ["one.jpg"]

    def test_an_unfiltered_list_returns_nothing(self, signed_in):
        """Not an error: a screen with no document yet asks with no id.

        But it must not become "every file in the organization" either — one
        request should never be able to enumerate a tenant's whole evidence
        trail.
        """
        http, token, organization, owner = signed_in
        job = a_job(organization, owner, ref="JOB-ATT-4")
        http.post(
            reverse("v1:attachment-list"),
            {"target_type": "jobs.Job", "target_id": str(job.pk), "file": a_photo()},
            **auth(token),
        )

        response = http.get(reverse("v1:attachment-list"), **auth(token))

        assert response.status_code == 200
        assert response.json()["results"] == []

    def test_a_fresh_signed_url_can_be_asked_for(self, signed_in):
        """§6: links expire, so a long-open screen asks again."""
        http, token, organization, owner = signed_in
        job = a_job(organization, owner, ref="JOB-ATT-5")
        created = http.post(
            reverse("v1:attachment-list"),
            {"target_type": "jobs.Job", "target_id": str(job.pk), "file": a_photo()},
            **auth(token),
        ).json()

        response = http.get(
            reverse("v1:attachment-url", kwargs={"pk": created["id"]}), **auth(token)
        )

        assert response.status_code == 200
        assert response.json()["url"]
        assert response.json()["expires_in"] > 0


class TestWhatItRefuses:
    def test_an_arbitrary_model_cannot_be_attached_to(self, signed_in):
        """The owner as a target: allowed by the schema, refused by the list.

        Without the allow-list, a file could be hung off any row in the tenant —
        a user, an audit entry — and served back by that record's listing.
        """
        http, token, _organization, owner = signed_in

        response = http.post(
            reverse("v1:attachment-list"),
            {
                "target_type": "accounts.User",
                "target_id": str(owner.pk),
                "file": a_photo(),
            },
            **auth(token),
        )

        assert response.status_code == 400
        assert "target_type" in response.json()["error"]["field_errors"]

    def test_another_tenants_record_is_a_404(self, signed_in):
        """A3: ids are sequential, so guessing one must reach nothing."""
        http, token, _organization, _owner = signed_in
        theirs = provision_tenant(
            name="Rival", slug="rival", owner_email="owner@rival.co.ke"
        )
        their_job = a_job(theirs["organization"], theirs["owner"], ref="JOB-THEIRS")

        response = http.post(
            reverse("v1:attachment-list"),
            {
                "target_type": "jobs.Job",
                "target_id": str(their_job.pk),
                "file": a_photo(),
            },
            **auth(token),
        )

        assert response.status_code == 404

    def test_a_file_of_the_wrong_type_is_refused(self, signed_in):
        http, token, organization, owner = signed_in
        job = a_job(organization, owner, ref="JOB-ATT-6")

        response = http.post(
            reverse("v1:attachment-list"),
            {
                "target_type": "jobs.Job",
                "target_id": str(job.pk),
                "file": SimpleUploadedFile(
                    "payload.exe", b"MZ...", content_type="application/x-msdownload"
                ),
            },
            **auth(token),
        )

        assert response.status_code == 400

    def test_a_file_over_the_limit_is_refused(self, signed_in, settings):
        http, token, organization, owner = signed_in
        settings.ATTACHMENT_MAX_BYTES = 10
        job = a_job(organization, owner, ref="JOB-ATT-7")

        response = http.post(
            reverse("v1:attachment-list"),
            {
                "target_type": "jobs.Job",
                "target_id": str(job.pk),
                "file": a_photo(),
            },
            **auth(token),
        )

        assert response.status_code == 400

    def test_attaching_needs_the_permission_that_makes_the_document(self, signed_in):
        """Reading a document is not licence to add evidence to it.

        D6 and G3 exist to make a record trustworthy; if anyone who can see a
        gate-in could photograph it, that is exactly backwards.
        """
        from accounts.factories import UserFactory

        http, _token, organization, owner = signed_in
        job = a_job(organization, owner, ref="JOB-ATT-8")

        with tenant_context(organization):
            # No roles: authenticated, sees the tenant, holds nothing.
            onlooker = UserFactory(organization=organization, email="nobody@silvertech.co.ke")
            onlooker.set_password("a good long password")
            onlooker.save()

        their_token = http.post(
            reverse("v1:auth:login"),
            {"identifier": "nobody@silvertech.co.ke", "password": "a good long password"},
            content_type="application/json",
        ).json()["access"]

        response = http.post(
            reverse("v1:attachment-list"),
            {"target_type": "jobs.Job", "target_id": str(job.pk), "file": a_photo()},
            **auth(their_token),
        )

        assert response.status_code == 403

    def test_only_the_uploader_can_remove_a_file(self, signed_in):
        """Otherwise the evidence is deniable, which is the point of having it."""
        from accounts.factories import UserFactory
        from accounts.models import UserRole

        http, token, organization, owner = signed_in
        job = a_job(organization, owner, ref="JOB-ATT-9")
        created = http.post(
            reverse("v1:attachment-list"),
            {"target_type": "jobs.Job", "target_id": str(job.pk), "file": a_photo()},
            **auth(token),
        ).json()

        with tenant_context(organization):
            other = UserFactory(organization=organization, email="tech@silvertech.co.ke")
            other.set_password("a good long password")
            other.save()
            for role in owner.user_roles.all():
                UserRole.objects.create(
                    organization=organization, user=other, role=role.role
                )

        their_token = http.post(
            reverse("v1:auth:login"),
            {"identifier": "tech@silvertech.co.ke", "password": "a good long password"},
            content_type="application/json",
        ).json()["access"]

        response = http.delete(
            reverse("v1:attachment-detail", kwargs={"pk": created["id"]}),
            **auth(their_token),
        )

        assert response.status_code == 403
        # And the uploader still can.
        assert (
            http.delete(
                reverse("v1:attachment-detail", kwargs={"pk": created["id"]}),
                **auth(token),
            ).status_code
            == 204
        )


class TestTargetsEndpoint:
    def test_it_says_what_this_user_may_attach_to(self, signed_in):
        """So a camera button appears exactly when the upload would succeed."""
        http, token, _organization, _owner = signed_in

        response = http.get(reverse("v1:attachment-targets"), **auth(token))

        assert response.status_code == 200
        body = response.json()
        assert body["targets"]["jobs.JobCloseout"] is True
        assert body["max_bytes"] > 0
        assert "image/jpeg" in body["content_types"]
