"""Isolation fixture for the attachment endpoint (T1.20, A3, N-7).

A cross-tenant read of an attachment is worse than most: the object carries a
signed download URL, so a 200 here would hand out a working link to another
organization's evidence, not merely confirm that a row exists.
"""

from django.core.files.uploadedfile import SimpleUploadedFile

from core.isolation import register_isolation_fixture


def register() -> None:
    from accounts.models import User
    from core.attachments import store_attachment
    from core.models import AttachmentKind
    from jobs.models import Job
    from network.models import Client, Site

    def make_attachment(organization):
        # Hung off a job, because that is one of the targets the API accepts —
        # a fixture attached to something the endpoint would refuse would test
        # a shape that cannot occur.
        client = Client.objects.filter(organization=organization).first() or (
            Client.objects.create(organization=organization, name="Isolation client")
        )
        site = Site.objects.filter(organization=organization).first() or (
            Site.objects.create(
                organization=organization,
                client=client,
                name="Isolation site",
                internal_ref="ISO-ATT-1",
            )
        )
        job = Job.objects.filter(organization=organization).first() or (
            Job.objects.create(
                organization=organization,
                reference="ISO-ATT-JOB",
                client=client,
                site=site,
                # A job is always somebody's (H1), and provisioning has already
                # created the owner.
                assignee=User.objects.filter(organization=organization).first(),
            )
        )
        return store_attachment(
            target=job,
            uploaded_file=SimpleUploadedFile(
                "isolation.jpg", b"pretend jpeg", content_type="image/jpeg"
            ),
            kind=AttachmentKind.PHOTO,
            organization=organization,
        )

    # No payload: the endpoint accepts no PUT or PATCH, so there is nothing to
    # send. Evidence is added and removed, never edited (see the viewset).
    register_isolation_fixture("attachment", make_attachment)
