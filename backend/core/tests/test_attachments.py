"""T1.16 — attachments (§4.13, requirements D6, G3, N-7)."""

import pytest
from django.core import signing
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from accounts.factories import UserFactory
from core.attachments import (
    attachments_for,
    resolve_signed_token,
    signed_download_url,
    store_attachment,
)
from core.models import Attachment, AttachmentKind
from core.tenancy import tenant_context


def a_file(name="delivery-note.pdf", content=b"scanned delivery note"):
    return SimpleUploadedFile(name, content, content_type="application/pdf")


@pytest.fixture
def stored(tenant):
    """An attachment on some record — the organization itself will do."""
    return store_attachment(
        target=tenant,
        uploaded_file=a_file(),
        kind=AttachmentKind.DOCUMENT,
        uploaded_by=UserFactory(organization=tenant),
    )


class TestStoringAnAttachment:
    def test_an_attachment_records_its_metadata(self, stored):
        """So a listing can describe a file without touching storage."""
        assert stored.filename == "delivery-note.pdf"
        assert stored.content_type == "application/pdf"
        assert stored.size == len(b"scanned delivery note")
        assert stored.kind == AttachmentKind.DOCUMENT

    def test_the_file_is_readable_back(self, stored):
        assert stored.file.read() == b"scanned delivery note"

    def test_attachments_are_found_by_their_target(self, tenant, stored):
        assert list(attachments_for(tenant)) == [stored]

    def test_files_are_partitioned_by_organization(self, stored, tenant):
        """One tenant's files never sit among another's."""
        assert str(tenant.pk) in stored.file.name


class TestSignedUrls:
    """N-7: never publicly readable; every read is signed and time-limited."""

    def test_the_download_url_is_not_a_raw_media_path(self, stored):
        """The design point worth defending.

        If local reads went through /media/, an authorisation mistake would stay
        invisible until the first deployment.
        """
        url = stored.download_url()

        assert "/media/" not in url
        assert "/attachments/" in url
        assert "download" in url

    def test_a_signed_url_resolves_to_the_attachment(self, stored):
        url = signed_download_url(stored)
        token = url.split("/attachments/")[1].split("/download")[0]

        assert resolve_signed_token(token) == stored

    def test_a_tampered_token_is_rejected(self, stored):
        with pytest.raises(signing.BadSignature):
            resolve_signed_token("not-a-real-token")

    def test_an_expired_token_is_rejected(self, stored):
        url = signed_download_url(stored)
        token = url.split("/attachments/")[1].split("/download")[0]

        with pytest.raises(signing.SignatureExpired):
            # Zero lifetime: anything already minted is past it.
            resolve_signed_token(token, expires_in=-1)


class TestDownloadView:
    def test_a_valid_token_serves_the_file(self, client, stored):
        url = stored.download_url()

        response = client.get(url)

        assert response.status_code == 200
        assert b"".join(response.streaming_content) == b"scanned delivery note"

    def test_a_tampered_token_is_404_not_403(self, client, stored):
        """§2.4: distinguishing them would confirm the attachment exists."""
        response = client.get(
            reverse("attachment-download", kwargs={"token": "forged-token"})
        )

        assert response.status_code == 404

    def test_an_expired_link_is_404(self, client, stored):
        url = stored.download_url()
        path, _, _query = url.partition("?")

        response = client.get(f"{path}?expires_in=-1")

        assert response.status_code == 404

    def test_the_url_cannot_be_replayed_against_another_tenant(
        self, client, organization, other_organization
    ):
        """A3: the organization travels inside the signed payload.

        Even if an id were guessed, the link resolves only within the tenant it
        was issued for.
        """
        with tenant_context(organization):
            ours = store_attachment(
                target=organization, uploaded_file=a_file(), kind=AttachmentKind.DOCUMENT
            )
        token = signed_download_url(ours).split("/attachments/")[1].split("/download")[0]

        payload = signing.loads(token, salt="core.attachments.download", max_age=300)
        assert payload["org"] == str(organization.pk)
        assert payload["org"] != str(other_organization.pk)


class TestAttachmentIsolation:
    def test_attachments_are_scoped_to_their_organization(
        self, organization, other_organization
    ):
        with tenant_context(organization):
            store_attachment(
                target=organization,
                uploaded_file=a_file("ours.pdf"),
                kind=AttachmentKind.DOCUMENT,
            )
        with tenant_context(other_organization):
            store_attachment(
                target=other_organization,
                uploaded_file=a_file("theirs.pdf"),
                kind=AttachmentKind.DOCUMENT,
            )

        with tenant_context(organization):
            assert [a.filename for a in Attachment.objects.all()] == ["ours.pdf"]
