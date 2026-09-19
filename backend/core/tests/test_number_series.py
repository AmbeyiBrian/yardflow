"""Number series a tenant can set (§4.13, §6; M6, O1).

Two guarantees pull against each other here, and both tests exist to hold them
apart.

M6 wants numbers that are sequential, gap-free and never reused — that is what
lets an auditor say nothing was removed. A tenant wants their own prefix, and
often wants to carry on from a sequence they already ran on paper.

Both are satisfiable: the prefix and the padding are theirs, the counter may
move **forward** to meet an existing sequence, and it may not move back onto
numbers already issued. The last of those is the one that would quietly produce
two documents with the same number.
"""

import pytest
from django.db import transaction
from django.urls import reverse

from core.numbering import (
    DocumentSequence,
    DocumentType,
    allocate_number,
    format_number,
    peek_next_number,
)
from core.provisioning import provision_tenant
from core.tenancy import tenant_context

PASSWORD = "a good long password"


@pytest.fixture
def signed_in(db, client, settings):
    settings.TENANT_BASE_DOMAIN = "localhost"
    result = provision_tenant(
        name="Silvertech", slug="silvertech", owner_email="owner@silvertech.co.ke"
    )
    owner = result["owner"]
    owner.set_password(PASSWORD)
    owner.save()
    client.defaults["HTTP_HOST"] = "silvertech.localhost"
    token = client.post(
        reverse("v1:auth:login"),
        {"identifier": "owner@silvertech.co.ke", "password": PASSWORD},
        content_type="application/json",
    ).json()["access"]
    return client, token, result["organization"]


def auth(token):
    return {"HTTP_AUTHORIZATION": f"Bearer {token}"}


@pytest.mark.django_db
class TestDefaults:
    def test_a_type_never_used_still_appears(self, signed_in):
        """A screen showing only what you have already done is no use for
        deciding what the next one should look like."""
        http, token, _ = signed_in

        rows = http.get(reverse("v1:number-series"), **auth(token)).json()
        by_type = {row["document_type"]: row for row in rows}

        assert set(by_type) == {value for value, _ in DocumentType.choices}
        assert by_type["PROJECT"]["prefix"] == "PRJ"
        assert by_type["PROJECT"]["example"] == "PRJ-000001"

    def test_the_example_shows_what_the_next_one_will_be(self, signed_in):
        http, token, organization = signed_in
        with tenant_context(organization), transaction.atomic():
            allocate_number(DocumentType.GATE_OUT)

        rows = http.get(reverse("v1:number-series"), **auth(token)).json()
        gate_out = next(row for row in rows if row["document_type"] == "GATE_OUT")

        assert gate_out["example"] == "GP-000002"
        assert gate_out["highest_issued"] == 1


@pytest.mark.django_db
class TestChangingASeries:
    def patch(self, http, token, **data):
        return http.patch(
            reverse("v1:number-series"), data, content_type="application/json", **auth(token)
        )

    def test_a_tenant_may_set_their_own_prefix_and_width(self, signed_in):
        http, token, organization = signed_in

        response = self.patch(
            http, token, document_type="PROJECT", prefix="SLV", width=4, next_number=1
        )

        assert response.status_code == 200, response.content
        assert response.json()["example"] == "SLV-0001"

        with tenant_context(organization), transaction.atomic():
            assert allocate_number(DocumentType.PROJECT) == "SLV-0001"

    def test_the_counter_may_move_forward(self, signed_in):
        """The real case: a contractor carrying on from a paper sequence."""
        http, token, organization = signed_in

        response = self.patch(
            http, token, document_type="GATE_OUT", prefix="GP", width=6, next_number=4313
        )

        assert response.status_code == 200, response.content
        with tenant_context(organization), transaction.atomic():
            assert allocate_number(DocumentType.GATE_OUT) == "GP-004313"

    def test_it_may_not_move_back_onto_numbers_already_issued(self, signed_in):
        """The one that would put the same number on two documents."""
        http, token, organization = signed_in
        with tenant_context(organization), transaction.atomic():
            allocate_number(DocumentType.GATE_OUT)
            allocate_number(DocumentType.GATE_OUT)

        response = self.patch(
            http, token, document_type="GATE_OUT", prefix="GP", width=6, next_number=1
        )

        assert response.status_code == 400
        assert "same number" in str(response.content)

    def test_an_unknown_type_is_refused(self, signed_in):
        http, token, _ = signed_in

        response = self.patch(
            http, token, document_type="NONSENSE", prefix="X", width=4, next_number=1
        )

        assert response.status_code == 400


@pytest.mark.django_db
class TestOldNumbersAreLeftAlone:
    def test_changing_the_prefix_does_not_rewrite_what_was_issued(self, signed_in):
        """A gate pass already filed keeps the number printed on it. The screen
        warns about the two eras; it does not pretend they did not happen."""
        _, _, organization = signed_in
        with tenant_context(organization), transaction.atomic():
            first = allocate_number(DocumentType.GATE_OUT)

        with tenant_context(organization):
            sequence = DocumentSequence.objects.get(
                organization=organization, document_type=DocumentType.GATE_OUT
            )
            sequence.prefix = "PASS"
            sequence.save()

        with tenant_context(organization), transaction.atomic():
            second = allocate_number(DocumentType.GATE_OUT)

        assert first == "GP-000001"
        assert second == "PASS-000002"


@pytest.mark.django_db
class TestAutoNumbering:
    def test_a_project_numbers_itself(self, signed_in):
        """O1: the reference is not typed. One somebody chooses is one somebody
        can collide with, and the client's own name for the work is `po_number`."""
        from network.factories import ClientFactory
        from network.models import Project

        _, _, organization = signed_in
        with tenant_context(organization):
            project = Project.objects.create(
                organization=organization, client=ClientFactory()
            )

        assert project.reference == "PRJ-000001"

    def test_a_job_numbers_itself(self, signed_in):
        from accounts.factories import UserFactory
        from jobs.models import Job
        from network.factories import SiteFactory

        _, _, organization = signed_in
        with tenant_context(organization):
            site = SiteFactory()
            job = Job.objects.create(
                organization=organization,
                client=site.client,
                site=site,
                assignee=UserFactory(organization=organization),
            )

        assert job.reference == "JOB-000001"

    def test_they_keep_counting(self, signed_in):
        from network.factories import ClientFactory
        from network.models import Project

        _, _, organization = signed_in
        with tenant_context(organization):
            client = ClientFactory()
            first = Project.objects.create(organization=organization, client=client)
            second = Project.objects.create(organization=organization, client=client)

        assert (first.reference, second.reference) == ("PRJ-000001", "PRJ-000002")

    def test_peeking_does_not_consume(self, signed_in):
        _, _, organization = signed_in
        with tenant_context(organization):
            assert peek_next_number(DocumentType.PROJECT) == "PRJ-000001"
            assert peek_next_number(DocumentType.PROJECT) == "PRJ-000001"


@pytest.mark.django_db
class TestFormatting:
    def test_defaults_apply_when_no_series_says_otherwise(self):
        assert format_number(DocumentType.GATE_OUT, 42) == "GP-000042"

    def test_a_series_overrides_them(self):
        assert format_number(DocumentType.GATE_OUT, 42, prefix="PASS", width=3) == "PASS-042"
