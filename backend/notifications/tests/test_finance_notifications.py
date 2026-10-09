"""T15.5 — money-out notifications (R4, §4.17.9).

Who is told at each step of an expense or an allowance request, and what the
message says. The approver group is the part worth pinning: it must follow the
open level, and it must never be the person who recorded the entry.
"""

from datetime import date
from decimal import Decimal

import pytest
from django.core import mail

from accounts.factories import RoleFactory, UserFactory, UserRoleFactory
from accounts.permissions_registry import PERM
from commercials import finance
from commercials.models import ExpenseCategory, ExpenseKind
from network.factories import ProjectFactory, SiteFactory
from notifications.events import _resolve_group, render_body
from notifications.matrix import (
    DEFAULT_MATRIX,
    Channel,
    Event,
    Recipient,
    channels_for,
    default_channels_config,
    default_matrix_config,
)
from notifications.models import NotificationDelivery, NotificationEvent
from notifications.views import resource_for

D = Decimal
DAY = date(2026, 10, 5)


@pytest.fixture(autouse=True)
def seeded_matrix(tenant):
    settings = tenant.settings
    settings.notification_matrix = default_matrix_config()
    settings.notification_channels = default_channels_config()
    settings.save()
    return settings


@pytest.fixture
def pm(tenant):
    return UserFactory(organization=tenant, full_name="Pippa Manager", email="pippa@x.co.ke")


@pytest.fixture
def tech(tenant):
    return UserFactory(organization=tenant, full_name="John Tech", email="john@x.co.ke")


@pytest.fixture
def finance_role(tenant):
    return RoleFactory(name="Finance", codenames=[PERM.FINANCE_APPROVE])


@pytest.fixture
def fiona(tenant, finance_role):
    user = UserFactory(organization=tenant, full_name="Fiona Finance", email="fiona@x.co.ke")
    UserRoleFactory(user=user, role=finance_role)
    return user


@pytest.fixture
def site(tenant, pm):
    project = ProjectFactory(reference="WO-1", manager=pm)
    site = SiteFactory(name="Ruiru")
    project.sites.add(site)
    return site


@pytest.fixture
def fuel(tenant):
    return ExpenseCategory.objects.create(
        organization=tenant, name="Fuel", kind=ExpenseKind.FUEL
    )


def spend(tech, fuel, site, amount="1200"):
    return finance.record_expense(
        actor=tech,
        category=fuel,
        amount=D(amount),
        incurred_on=DAY,
        site=site,
        vehicle_reg="KDA 123A",
        litres=D("10"),
    )


def deliveries(event_key, channel=None):
    found = NotificationDelivery.objects.filter(event__event_key=event_key)
    if channel:
        found = found.filter(channel=channel)
    return found


def who(event_key, channel):
    return {d.recipient.pk for d in deliveries(event_key, channel)}


class TestTheMatrixEntries:
    def test_defaults_follow_the_design(self):
        by_key = {spec.key: spec for spec in DEFAULT_MATRIX}

        waiting = by_key[Event.FINANCE_AWAITING_APPROVAL]
        assert waiting.recipients == (Recipient.LEVEL_APPROVERS,)
        assert set(waiting.channels) == {Channel.IN_APP, Channel.EMAIL}
        assert waiting.carries_a_control
        assert by_key[Event.FINANCE_APPROVED].recipients == (Recipient.REQUESTER,)
        assert by_key[Event.FINANCE_APPROVED].channels == (Channel.IN_APP,)
        assert set(by_key[Event.FINANCE_REJECTED].channels) == {Channel.IN_APP, Channel.EMAIL}
        assert by_key[Event.FINANCE_PAID].channels == (Channel.IN_APP,)

    def test_no_sms_by_default(self, tenant):
        for key in (
            Event.FINANCE_AWAITING_APPROVAL,
            Event.FINANCE_APPROVED,
            Event.FINANCE_REJECTED,
            Event.FINANCE_PAID,
        ):
            assert Channel.SMS not in channels_for(tenant, key)

    def test_links_go_to_the_entry_or_to_approvals(self):
        assert resource_for("commercials.ProjectExpense", "7") == "/money/expenses/7"
        assert resource_for("commercials.AllowanceRequest", "9") == "/money/requests/9"
        assert (
            resource_for("commercials.ProjectExpense", "7", Event.FINANCE_AWAITING_APPROVAL)
            == "/approvals"
        )


@pytest.mark.django_db
class TestLevelApprovers:
    def test_a_pm_level_is_the_project_manager_alone(self, tenant, pm, tech, fiona, site, fuel):
        expense = spend(tech, fuel, site)

        assert _resolve_group(tenant, Recipient.LEVEL_APPROVERS, expense) == [pm]

    def test_an_inactive_pm_is_not_told(self, tenant, pm, tech, fiona, site, fuel):
        expense = spend(tech, fuel, site)
        pm.is_active = False
        pm.save()

        assert _resolve_group(tenant, Recipient.LEVEL_APPROVERS, expense) == []

    def test_a_permission_level_is_its_holders_through_a_role(
        self, tenant, pm, tech, fiona, finance_role, site, fuel
    ):
        second = UserFactory(organization=tenant, full_name="Femi Finance")
        UserRoleFactory(user=second, role=finance_role)
        gone = UserFactory(organization=tenant, full_name="Gone Finance", is_active=False)
        UserRoleFactory(user=gone, role=finance_role)
        expense = spend(tech, fuel, site)
        finance.decide(expense, actor=pm, approved=True)

        people = _resolve_group(tenant, Recipient.LEVEL_APPROVERS, expense)

        assert set(people) == {fiona, second}

    def test_the_recorder_is_never_their_own_approver(
        self, tenant, pm, fiona, finance_role, site, fuel
    ):
        # Fiona holds finance.approve and records: she must not be asked.
        other = UserFactory(organization=tenant, full_name="Other Finance")
        UserRoleFactory(user=other, role=finance_role)
        expense = spend(fiona, fuel, site)
        finance.decide(expense, actor=pm, approved=True)

        assert _resolve_group(tenant, Recipient.LEVEL_APPROVERS, expense) == [other]

    def test_the_lowest_open_level_decides(self, tenant, pm, tech, fiona, site, fuel):
        expense = spend(tech, fuel, site)
        assert _resolve_group(tenant, Recipient.LEVEL_APPROVERS, expense) == [pm]

        finance.decide(expense, actor=pm, approved=True)

        assert _resolve_group(tenant, Recipient.LEVEL_APPROVERS, expense) == [fiona]

    def test_with_no_document_it_resolves_to_nobody(self, tenant):
        assert _resolve_group(tenant, Recipient.LEVEL_APPROVERS, None) == []


@pytest.mark.django_db
class TestEachEmitPoint:
    def test_recording_tells_the_pm_in_app_and_by_email(
        self, tenant, pm, tech, fiona, site, fuel, django_capture_on_commit_callbacks
    ):
        with django_capture_on_commit_callbacks(execute=True):
            spend(tech, fuel, site)

        key = Event.FINANCE_AWAITING_APPROVAL
        assert who(key, Channel.IN_APP) == {pm.pk}
        assert who(key, Channel.EMAIL) == {pm.pk}
        assert not deliveries(key, Channel.SMS).exists()
        assert [m.to for m in mail.outbox] == [["pippa@x.co.ke"]]

    def test_the_email_names_the_amount_the_site_and_the_recorder(
        self, tenant, pm, tech, fiona, site, fuel, django_capture_on_commit_callbacks
    ):
        with django_capture_on_commit_callbacks(execute=True):
            spend(tech, fuel, site)

        message = mail.outbox[0]
        assert message.subject == (
            "Expense waiting for your approval: KES 1,200 fuel at Ruiru by John Tech"
        )
        assert "KES 1,200" in message.body
        assert "Ruiru" in message.body
        assert "No receipt has been attached." in message.body

    def test_the_payload_has_what_an_approver_decides_on(
        self, tenant, pm, tech, fiona, site, fuel, django_capture_on_commit_callbacks
    ):
        with django_capture_on_commit_callbacks(execute=True):
            spend(tech, fuel, site)

        payload = NotificationEvent.objects.get().payload
        assert payload["amount"] == "1200.00"
        assert payload["category"] == "Fuel"
        assert payload["site"] == "Ruiru"
        assert payload["project"]
        assert payload["recorded_by"] == "John Tech"
        assert payload["evidence_state"] == "NONE"
        assert payload["label"] == "Expense KES 1,200 · Fuel"

    def test_a_request_carries_its_number_and_the_open_float_warning(
        self, tenant, pm, tech, fiona, site, django_capture_on_commit_callbacks
    ):
        float_request = finance.request_allowance(
            actor=tech, type="FLOAT", amount=D("5000"), from_date=DAY, to_date=DAY, site=site
        )
        finance.decide(float_request, actor=pm, approved=True)
        finance.decide(float_request, actor=fiona, approved=True)
        finance.mark_paid(float_request, actor=fiona, reference="MPESA-1")

        with django_capture_on_commit_callbacks(execute=True):
            second = finance.request_allowance(
                actor=tech,
                type="FLOAT",
                amount=D("3000"),
                from_date=DAY,
                to_date=DAY,
                site=site,
            )

        event = NotificationEvent.objects.get(
            event_key=Event.FINANCE_AWAITING_APPROVAL, target_id=str(second.pk)
        )
        assert event.payload["number"] == second.number
        assert event.payload["float_warning"]["number"] == float_request.number
        body = render_body(event)
        assert second.number in body
        assert float_request.number in body

    def test_the_pm_approving_tells_finance_and_not_the_recorder(
        self, tenant, pm, tech, fiona, site, fuel, django_capture_on_commit_callbacks
    ):
        expense = spend(tech, fuel, site)
        NotificationEvent.objects.all().delete()

        with django_capture_on_commit_callbacks(execute=True):
            finance.decide(expense, actor=pm, approved=True)

        key = Event.FINANCE_AWAITING_APPROVAL
        assert who(key, Channel.IN_APP) == {fiona.pk}
        assert who(key, Channel.EMAIL) == {fiona.pk}
        assert not deliveries(Event.FINANCE_APPROVED).exists()

    def test_final_approval_tells_the_recorder_in_app_only(
        self, tenant, pm, tech, fiona, site, fuel, django_capture_on_commit_callbacks
    ):
        expense = spend(tech, fuel, site)
        finance.decide(expense, actor=pm, approved=True)
        NotificationEvent.objects.all().delete()
        mail.outbox.clear()

        with django_capture_on_commit_callbacks(execute=True):
            finance.decide(expense, actor=fiona, approved=True)

        assert who(Event.FINANCE_APPROVED, Channel.IN_APP) == {tech.pk}
        assert not deliveries(Event.FINANCE_APPROVED, Channel.EMAIL).exists()
        assert mail.outbox == []

    def test_a_rejection_carries_the_reason_in_app_and_by_email(
        self, tenant, pm, tech, fiona, site, fuel, django_capture_on_commit_callbacks
    ):
        expense = spend(tech, fuel, site)
        NotificationEvent.objects.all().delete()
        mail.outbox.clear()

        with django_capture_on_commit_callbacks(execute=True):
            finance.decide(expense, actor=pm, approved=False, reason="Receipt is unreadable.")

        key = Event.FINANCE_REJECTED
        assert who(key, Channel.IN_APP) == {tech.pk}
        assert who(key, Channel.EMAIL) == {tech.pk}
        assert len(mail.outbox) == 1
        assert "Receipt is unreadable." in mail.outbox[0].body
        assert "KES 1,200" in mail.outbox[0].body
        assert mail.outbox[0].subject.startswith("Expense rejected:")

    def test_sending_again_tells_the_first_open_level(
        self, tenant, pm, tech, fiona, site, fuel, django_capture_on_commit_callbacks
    ):
        expense = spend(tech, fuel, site)
        finance.decide(expense, actor=pm, approved=False, reason="Wrong site.")
        NotificationEvent.objects.all().delete()

        with django_capture_on_commit_callbacks(execute=True):
            finance.resubmit(expense, actor=tech)

        assert who(Event.FINANCE_AWAITING_APPROVAL, Channel.IN_APP) == {pm.pk}

    def test_paying_tells_the_recorder_with_the_reference(
        self, tenant, pm, tech, fiona, site, fuel, django_capture_on_commit_callbacks
    ):
        expense = spend(tech, fuel, site)
        finance.decide(expense, actor=pm, approved=True)
        finance.decide(expense, actor=fiona, approved=True)
        NotificationEvent.objects.all().delete()

        with django_capture_on_commit_callbacks(execute=True):
            finance.mark_paid(expense, actor=fiona, reference="MPESA-QX1")

        assert who(Event.FINANCE_PAID, Channel.IN_APP) == {tech.pk}
        delivery = deliveries(Event.FINANCE_PAID, Channel.IN_APP).get()
        assert "MPESA-QX1" in delivery.body
        assert not deliveries(Event.FINANCE_PAID, Channel.EMAIL).exists()

    def test_a_pm_who_records_skips_to_finance(
        self, tenant, pm, fiona, site, fuel, django_capture_on_commit_callbacks
    ):
        with django_capture_on_commit_callbacks(execute=True):
            spend(pm, fuel, site)

        assert who(Event.FINANCE_AWAITING_APPROVAL, Channel.IN_APP) == {fiona.pk}

    def test_a_rolled_back_record_sends_nothing(
        self, tenant, pm, tech, site, fuel, django_capture_on_commit_callbacks
    ):
        # Nobody but the recorder could approve: refused, and nothing is said.
        with django_capture_on_commit_callbacks(execute=True):
            with pytest.raises(finance.FinanceNoOtherApprover):
                spend(tech, fuel, site)

        assert not NotificationEvent.objects.exists()
