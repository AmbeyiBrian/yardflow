"""T4.18–T4.20 — documents, QR lookup and deep links (§11, §9.2; G4, G5, F4)."""

from decimal import Decimal

import pytest
from django.core import signing
from django.db import transaction
from django.urls import reverse
from django.utils import timezone

from accounts.factories import RoleFactory, UserFactory, UserRoleFactory
from accounts.permissions_registry import PERM
from approvals.models import ApprovalRule
from approvals.views import DEEP_LINK_MAX_AGE, resolve_deep_link
from catalogue.factories import ItemCategoryFactory, ItemTypeFactory
from catalogue.models import Criticality, TrackingMode
from core.provisioning import provision_tenant
from core.tenancy import tenant_context
from dispatch.documents import (
    gate_pass_context,
    pdf_available,
    qr_png_data_uri,
    qr_token,
    render_gate_pass,
    resolve_qr_token,
)
from dispatch.models import GateOut, GateOutLine, GateOutPurpose
from dispatch.services import approve_gate_out, submit_gate_out
from locations.factories import YardFactory
from locations.nodes import external_node
from network.factories import ClientFactory, SiteFactory
from stock.models import MovementType, OwnerType
from stock.services import MovementRequest, post_movement


@pytest.fixture
def yard(tenant):
    return YardFactory(name="Main yard")


@pytest.fixture
def storekeeper(tenant):
    return UserFactory(organization=tenant, full_name="Sara Storekeeper")


def a_released_pass(tenant, yard, storekeeper, *, client_owned=False):
    """An approved pass with a mixed set of lines, as a real one would be."""
    # A name the starter catalogue does not already contain: these tests run
    # against provisioned tenants, which arrive with the seeded telecom list
    # (T2.4), and "RRU 2x40W" is in it.
    item = ItemTypeFactory(
        name="RRU 2x40W (test unit)",
        category=ItemCategoryFactory(name="Active equipment", criticality=Criticality.HIGH),
        default_tracking_mode=TrackingMode.BULK,
    )
    owner_client = ClientFactory(name="Safaricom") if client_owned else None

    with transaction.atomic():
        post_movement(
            MovementRequest(
                item_type=item,
                quantity=Decimal("50"),
                from_node=external_node(tenant.pk, client=owner_client),
                to_node=yard.node,
                movement_type=MovementType.RECEIPT,
                owner_type=OwnerType.CLIENT if owner_client else OwnerType.OWN,
                owner_client=owner_client,
            )
        )

    gate_out = GateOut.objects.create(
        organization=tenant,
        from_location=yard,
        site=SiteFactory(internal_ref="SLV-1001", name="Kileleshwa"),
        custody_holder=storekeeper,
        requested_by=storekeeper,
        purpose_type=GateOutPurpose.INSTALLATION,
    )
    GateOutLine.objects.create(
        organization=tenant,
        gate_out=gate_out,
        item_type=item,
        tracking_mode=TrackingMode.BULK,
        requested_qty=Decimal("3"),
        uom=item.uom,
        owner_type=OwnerType.CLIENT if owner_client else OwnerType.OWN,
        owner_client=owner_client,
    )
    submit_gate_out(gate_out, submitted_by=storekeeper)
    return gate_out


class TestGatePassDocument:
    """G4: "the driver carries a document as they do today"."""

    def test_the_pass_renders(self, tenant, yard, storekeeper):
        gate_out = a_released_pass(tenant, yard, storekeeper)

        content, content_type, filename = render_gate_pass(gate_out, as_pdf=False)

        assert gate_out.number.encode() in content
        assert content_type.startswith("text/html")
        assert filename.endswith(".html")

    def test_it_shows_what_the_guard_needs_to_check(self, tenant, yard, storekeeper):
        """G1: the physical load is verified against the approved list."""
        gate_out = a_released_pass(tenant, yard, storekeeper)

        content, _type, _name = render_gate_pass(gate_out, as_pdf=False)
        text = content.decode()

        assert "RRU 2x40W (test unit)" in text
        assert "Sara Storekeeper" in text  # who is taking custody (F1)
        assert "SLV-1001" in text  # where it is going
        assert gate_out.number in text

    def test_client_owned_material_is_distinguishable_on_paper(self, tenant, yard, storekeeper):
        """E1: "client-owned stock is visually distinct wherever it appears".

        Including the paper — a guard checking a load needs to know whose it is.
        """
        gate_out = a_released_pass(tenant, yard, storekeeper, client_owned=True)

        text = render_gate_pass(gate_out, as_pdf=False)[0].decode()

        assert "Safaricom" in text
        assert "owner-client" in text

    def test_own_stock_is_labelled_too(self, tenant, yard, storekeeper):
        gate_out = a_released_pass(tenant, yard, storekeeper)

        text = render_gate_pass(gate_out, as_pdf=False)[0].decode()

        assert "Own stock" in text

    def test_the_approval_trail_is_printed(self, tenant, yard, storekeeper):
        """An operator asking "who authorised this?" is answerable from the paper."""
        approver_role = RoleFactory(name="Owner", codenames=[PERM.GATE_OUT_APPROVE])
        approver = UserFactory(organization=tenant, full_name="Sam Owner")
        UserRoleFactory(user=approver, role=approver_role)
        ApprovalRule.objects.create(
            criticality=Criticality.HIGH, required_role=approver_role, sequence=3
        )

        gate_out = a_released_pass(tenant, yard, storekeeper)
        approve_gate_out(gate_out, actor=approver)

        text = render_gate_pass(gate_out, as_pdf=False)[0].decode()

        assert "Sam Owner" in text
        assert "Authorised by" in text

    def test_the_status_is_prominent(self, tenant, yard, storekeeper):
        """A voided or expired pass must be unmistakable on paper (M6)."""
        gate_out = a_released_pass(tenant, yard, storekeeper)

        text = render_gate_pass(gate_out, as_pdf=False)[0].decode()

        assert gate_out.get_status_display() in text

    def test_the_context_includes_the_qr_code(self, tenant, yard, storekeeper):
        gate_out = a_released_pass(tenant, yard, storekeeper)

        context = gate_pass_context(gate_out)

        assert context["qr"].startswith("data:image/png;base64,")

    def test_the_qr_is_embedded_not_linked(self, tenant, yard, storekeeper):
        """The PDF has to be readable when printed, forwarded or opened offline."""
        gate_out = a_released_pass(tenant, yard, storekeeper)

        assert qr_png_data_uri(gate_out).startswith("data:image/png;base64,")

    def test_pdf_rendering_degrades_to_html_when_unavailable(self, tenant, yard, storekeeper):
        """A missing system library must not stop a driver leaving with a document.

        WeasyPrint needs GTK, which is absent on many machines. Whether this
        returns a PDF or HTML depends on the host; either is a usable document.
        """
        gate_out = a_released_pass(tenant, yard, storekeeper)

        _content, content_type, filename = render_gate_pass(gate_out, as_pdf=True)

        assert content_type in ("application/pdf", "text/html; charset=utf-8")
        assert filename.endswith((".pdf", ".html"))

    def test_where_the_renderer_is_present_a_real_pdf_comes_out(
        self, tenant, yard, storekeeper
    ):
        """The test that was missing, and the reason nobody noticed.

        Accepting "a PDF *or* HTML" everywhere meant the suite stayed green with
        no PDF engine installed at all — which is what had happened: the
        dependency was pinned to a version that does not exist, so it was never
        installed and every gate pass in development was a web page. Skipped
        where the host genuinely cannot render, asserted where it can, so CI
        exercises the thing a driver actually carries.
        """
        if not pdf_available():
            pytest.skip("no PDF renderer on this host")

        gate_out = a_released_pass(tenant, yard, storekeeper)

        content, content_type, filename = render_gate_pass(gate_out, as_pdf=True)

        assert content.startswith(b"%PDF"), "an actual PDF, not a page pretending"
        assert content_type == "application/pdf"
        assert filename.endswith(".pdf")


class TestQrLookup:
    """G5, T4.20: "scanning a gate pass at the gate opens the right document"."""

    def test_a_token_resolves_to_its_document(self, tenant, yard, storekeeper):
        gate_out = a_released_pass(tenant, yard, storekeeper)

        assert resolve_qr_token(qr_token(gate_out)) == gate_out

    def test_a_tampered_token_is_rejected(self, tenant, yard, storekeeper):
        with pytest.raises(signing.BadSignature):
            resolve_qr_token("not-a-real-token")

    def test_a_token_carries_its_tenant(self, tenant, yard, storekeeper):
        """A3: a photographed pass from one tenant cannot resolve in another."""
        gate_out = a_released_pass(tenant, yard, storekeeper)

        payload = signing.loads(qr_token(gate_out), salt="dispatch.document.qr")

        assert payload["org"] == str(tenant.pk)
        assert payload["type"] == "dispatch.GateOut"


class TestQrScanEndpoint:
    @pytest.fixture
    def signed_in(self, db, client, settings):
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

    def test_scanning_opens_the_right_document(self, signed_in):
        http, token, organization, owner = signed_in

        with tenant_context(organization):
            yard = YardFactory(name="Scan yard")
            gate_out = a_released_pass(organization, yard, owner)
            code = qr_token(gate_out)

        response = http.get(
            reverse("v1:qr-scan"), {"token": code}, HTTP_AUTHORIZATION=f"Bearer {token}"
        )

        assert response.status_code == 200
        body = response.json()
        assert body["number"] == gate_out.number
        # T4.20: the client is told what to open, so a scan at the gate lands on
        # the release screen.
        assert body["resource"] == f"/gate-out/{gate_out.pk}"

    def test_scanning_requires_authentication(self, signed_in):
        """The token identifies a document; it authorises nothing (§9.2)."""
        http, _token, organization, owner = signed_in

        with tenant_context(organization):
            yard = YardFactory(name="Scan yard 2")
            gate_out = a_released_pass(organization, yard, owner)
            code = qr_token(gate_out)

        response = http.get(reverse("v1:qr-scan"), {"token": code})

        assert response.status_code == 401

    def test_a_tampered_code_is_404_not_400(self, signed_in):
        """§2.4: a forged code must look exactly like an unknown one."""
        http, token, _organization, _owner = signed_in

        response = http.get(
            reverse("v1:qr-scan"),
            {"token": "forged"},
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )

        assert response.status_code == 404

    def test_a_code_from_another_tenant_resolves_to_nothing(self, signed_in):
        """The scan that must fail: a photographed pass from a rival's yard."""
        http, token, _organization, _owner = signed_in

        rival = provision_tenant(name="Rival", slug="rival", owner_email="owner@rival.co.ke")
        with tenant_context(rival["organization"]):
            rival_yard = YardFactory(name="Rival yard")
            rival_pass = a_released_pass(rival["organization"], rival_yard, rival["owner"])
            foreign_code = qr_token(rival_pass)

        response = http.get(
            reverse("v1:qr-scan"),
            {"token": foreign_code},
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )

        assert response.status_code == 404


class TestApprovalDeepLinks:
    """T4.18, §9.2: short-lived, signed, and authenticating nothing."""

    def test_a_deep_link_resolves_to_its_approval(self, tenant, yard, storekeeper):
        from approvals.models import ApprovalRequest

        approver_role = RoleFactory(name="Owner", codenames=[PERM.GATE_OUT_APPROVE])
        ApprovalRule.objects.create(
            criticality=Criticality.HIGH, required_role=approver_role, sequence=3
        )
        a_released_pass(tenant, yard, storekeeper)
        approval_request = ApprovalRequest.objects.get()

        token = signing.dumps(
            {"id": approval_request.pk, "org": str(tenant.pk)},
            salt="approvals.deep_link",
        )

        assert resolve_deep_link(token) == approval_request

    def test_a_tampered_link_is_rejected(self, tenant):
        with pytest.raises(signing.BadSignature):
            resolve_deep_link("forged-token")

    def test_an_expired_link_is_rejected(self, tenant, yard, storekeeper):
        """A forwarded message must be useless the next day."""
        from approvals.models import ApprovalRequest

        approver_role = RoleFactory(name="Owner", codenames=[PERM.GATE_OUT_APPROVE])
        ApprovalRule.objects.create(
            criticality=Criticality.HIGH, required_role=approver_role, sequence=3
        )
        a_released_pass(tenant, yard, storekeeper)
        approval_request = ApprovalRequest.objects.get()

        token = signing.dumps(
            {"id": approval_request.pk, "org": str(tenant.pk)},
            salt="approvals.deep_link",
        )

        with pytest.raises(signing.SignatureExpired):
            signing.loads(token, salt="approvals.deep_link", max_age=-1)

    def test_the_link_lifetime_is_short(self):
        """Long enough to read a notification and act; short enough to expire."""
        assert DEEP_LINK_MAX_AGE <= 24 * 60 * 60


class TestTheDocumentsAreActuallyServed:
    """Over HTTP, which nothing above this did (§11, G4, D1, A4).

    Both of these were latent failures. The HTML fallback took its choice from a
    ``format`` query parameter, which DRF reserves for content negotiation — so
    ``?format=html`` 404'd looking for a renderer of that name, and the fallback
    that exists for hosts without WeasyPrint never worked. The GRN endpoint did
    not exist at all: T4.19 built the template and the renderer, and nothing
    served them.
    """

    @pytest.fixture
    def signed_in(self, db, client, settings):
        from core.provisioning import provision_tenant

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

    def test_a_gate_pass_can_be_fetched_as_html(self, signed_in):
        """The fallback a host without GTK libraries depends on."""
        from core.tenancy import tenant_context
        from dispatch.models import GateOut, GateOutLine, GateOutPurpose

        http, token, organization, owner = signed_in

        with tenant_context(organization):
            from catalogue.factories import ItemTypeFactory
            from locations.factories import YardFactory
            from network.factories import SiteFactory

            yard = YardFactory(name="Served yard")
            site = SiteFactory(internal_ref="SRV-1", name="Served site")
            item = ItemTypeFactory(name="Served clamp", uom="ea")
            gate_out = GateOut.objects.create(
                organization=organization,
                from_location=yard,
                site=site,
                custody_holder=owner,
                requested_by=owner,
                purpose_type=GateOutPurpose.INSTALLATION,
            )
            GateOutLine.objects.create(
                organization=organization,
                gate_out=gate_out,
                item_type=item,
                tracking_mode="BULK",
                requested_qty=Decimal("1"),
                uom="ea",
            )

        response = http.get(
            reverse("v1:gate-out-pdf", args=[gate_out.pk]),
            {"output": "html"},
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )

        assert response.status_code == 200, response.content[:300]
        assert b"Gate pass" in response.content

    def test_a_grn_can_be_fetched(self, signed_in):
        """M3 replaces the GRN book, which needs the GRN to be printable."""
        from core.tenancy import tenant_context
        from receiving.models import GateIn, GateInLine, GateInSource

        http, token, organization, _owner = signed_in

        with tenant_context(organization):
            from catalogue.factories import ItemTypeFactory
            from locations.factories import YardFactory

            yard = YardFactory(name="GRN yard")
            item = ItemTypeFactory(name="GRN clamp", uom="ea")
            gate_in = GateIn.objects.create(
                organization=organization,
                source_type=GateInSource.PURCHASE,
                supplier_name="Huawei Kenya",
                to_location=yard,
                received_at=timezone.now(),
            )
            GateInLine.objects.create(
                organization=organization,
                gate_in=gate_in,
                item_type=item,
                tracking_mode="BULK",
                quantity=Decimal("10"),
                uom="ea",
            )

        response = http.get(
            reverse("v1:gate-in-grn", args=[gate_in.pk]),
            {"output": "html"},
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )

        assert response.status_code == 200, response.content[:300]
        assert b"Huawei Kenya" in response.content


class TestTheCompanysOwnLogo:
    """A4: the letterhead is the customer's, and it has to survive the trip.

    The context used to pass ``organization.logo.url`` — a relative path. The
    gate pass is rendered by handing WeasyPrint a bare string, with no base URL
    to resolve against, so that path pointed nowhere and the logo was simply
    absent from every PDF. Nothing failed; the document just came out plain,
    which is the kind of defect you only discover from a client.
    """

    def test_it_reaches_the_printed_pass(self, tenant, yard, storekeeper):
        import io

        from django.core.files.uploadedfile import SimpleUploadedFile
        from PIL import Image

        buffer = io.BytesIO()
        Image.new("RGBA", (900, 360), (15, 23, 42, 255)).save(buffer, format="PNG")
        tenant.logo = SimpleUploadedFile("logo.png", buffer.getvalue(), "image/png")
        tenant.save(update_fields=["logo"])
        gate_out = a_released_pass(tenant, yard, storekeeper)

        text = render_gate_pass(gate_out, as_pdf=False)[0].decode()

        assert "data:image/png;base64," in text, "embedded, so it prints"
        assert "/media/" not in text, "a link would resolve to nothing in the PDF"

    def test_without_one_the_document_still_renders(self, tenant, yard, storekeeper):
        """Most tenants never upload a logo. The company name carries the header."""
        gate_out = a_released_pass(tenant, yard, storekeeper)

        text = render_gate_pass(gate_out, as_pdf=False)[0].decode()

        assert tenant.name in text
