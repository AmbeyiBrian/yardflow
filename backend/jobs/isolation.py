"""Isolation fixtures for the jobs endpoints (T1.20, A3)."""

from decimal import Decimal

from core.isolation import register_isolation_fixture


def register() -> None:
    from accounts.models import User
    from catalogue.models import ItemCategory, ItemType
    from jobs.models import (
        CloseoutAction,
        Job,
        JobCloseout,
        JobCloseoutLine,
        Variance,
        VarianceType,
    )
    from network.models import Client, Site

    def _prerequisites(organization):
        assignee = User.objects.filter(organization=organization).first() or (
            User.objects.create_user(
                email="iso-assignee@example.com", organization=organization
            )
        )
        client = Client.objects.filter(organization=organization).first() or (
            Client.objects.create(organization=organization, name="Isolation client")
        )
        site = Site.objects.filter(organization=organization).first() or Site.objects.create(
            organization=organization,
            client=client,
            internal_ref="ISO-JOB-1",
            name="Isolation site",
        )
        return assignee, client, site

    def _item(organization):
        category = ItemCategory.objects.filter(organization=organization).first() or (
            ItemCategory.objects.create(organization=organization, name="Isolation")
        )
        return ItemType.objects.filter(organization=organization).first() or (
            ItemType.objects.create(
                organization=organization, category=category, name="Isolation item"
            )
        )

    def make_job(organization):
        assignee, client, site = _prerequisites(organization)
        return Job.objects.create(
            organization=organization,
            client=client,
            site=site,
            assignee=assignee,
            description="Isolation fixture",
        )

    def make_closeout(organization):
        job = make_job(organization)
        closeout = JobCloseout.objects.create(
            organization=organization, job=job, submitted_by=job.assignee
        )
        item = _item(organization)
        JobCloseoutLine.objects.create(
            organization=organization,
            closeout=closeout,
            action=CloseoutAction.INSTALLED,
            item_type=item,
            quantity=Decimal("1"),
            uom=item.uom,
        )
        return closeout

    def make_variance(organization):
        return Variance.objects.create(
            organization=organization,
            type=VarianceType.RETURN,
            item_type=_item(organization),
            expected=Decimal("3"),
            actual=Decimal("2"),
            reason="Isolation fixture",
        )

    register_isolation_fixture("job", make_job, payload={"description": "renamed"})
    register_isolation_fixture("job-closeout", make_closeout, payload={"notes": "renamed"})
    register_isolation_fixture("variance", make_variance)
