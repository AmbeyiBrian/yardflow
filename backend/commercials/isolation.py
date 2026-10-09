"""Isolation fixtures for the expense endpoints (T1.20, A3)."""

from core.isolation import register_isolation_fixture


def register() -> None:
    from datetime import date
    from decimal import Decimal

    from accounts.models import User
    from commercials.models import ExpenseCategory, ProjectExpense
    from network.models import Client, Project

    def make_expense_category(organization):
        return ExpenseCategory.objects.create(
            organization=organization, name="Isolation category"
        )

    def make_project_expense(organization):
        client = Client.objects.create(
            organization=organization, name="Isolation Operator"
        )
        manager = User.objects.filter(organization=organization).first()
        project = Project.objects.create(
            organization=organization,
            client=client,
            reference="ISO-EXP-WO",
            po_number="ISO-EXP-PO",
            manager=manager,
            contract_value=Decimal("1.00"),
            cost_budget=Decimal("1.00"),
        )
        return ProjectExpense.objects.create(
            organization=organization,
            project=project,
            category=make_expense_category(organization),
            amount=Decimal("1.00"),
            incurred_on=date(2026, 1, 1),
            recorded_by=manager,
        )

    def make_casual(organization):
        from commercials.models import Casual

        return Casual.objects.create(
            organization=organization,
            name="Isolation Casual",
            id_number="ISO-12345678",
            registered_by=User.objects.filter(organization=organization).first(),
        )

    def make_allowance_request(organization):
        from commercials.models import AllowanceRequest, AllowanceType

        project = make_project_expense(organization).project
        return AllowanceRequest.objects.create(
            organization=organization,
            number="AR-ISO-1",
            type=AllowanceType.OTHER,
            amount=Decimal("1.00"),
            from_date=date(2026, 1, 1),
            to_date=date(2026, 1, 1),
            project=project,
            recorded_by=User.objects.filter(organization=organization).first(),
        )

    def make_site_purchase(organization):
        from commercials.models import SitePurchase
        from network.models import Site

        project = make_project_expense(organization).project
        user = User.objects.filter(organization=organization).first()
        site = Site.objects.create(
            organization=organization,
            client=project.client,
            internal_ref="ISO-SP-1",
            name="Isolation Purchase Site",
        )
        return SitePurchase.objects.create(
            organization=organization,
            number="SP-ISO-1",
            project=project,
            site=site,
            purchase_date=date(2026, 1, 1),
            amount=Decimal("1.00"),
            recorded_by=user,
        )

    register_isolation_fixture(
        "expense-category", make_expense_category, payload={"name": "Renamed"}
    )
    register_isolation_fixture(
        "project-expense",
        make_project_expense,
        payload={"description": "Renamed"},
    )
    # No PATCH body for allowance requests: the viewset offers none (§4.17.6).
    register_isolation_fixture("allowance-request", make_allowance_request)
    register_isolation_fixture(
        "casual", make_casual, payload={"name": "Renamed"}
    )

    def make_milestone(organization):
        from commercials.models import MilestoneCondition, MilestoneShare, ProjectMilestone

        project = make_project_expense(organization).project
        return ProjectMilestone.objects.create(
            organization=organization,
            project=project,
            sequence=1,
            name="Isolation milestone",
            share_type=MilestoneShare.PERCENT,
            share_value=Decimal("50"),
            condition=MilestoneCondition.NONE,
        )

    def make_milestone_invoice(organization):
        from commercials.models import MilestoneInvoice

        return MilestoneInvoice.objects.create(
            organization=organization,
            milestone=make_milestone(organization),
            invoice_number="ISO-INV-1",
            invoice_date=date(2026, 1, 1),
            amount=Decimal("1.00"),
            recorded_by=User.objects.filter(organization=organization).first(),
        )

    def make_milestone_receipt(organization):
        from commercials.models import MilestoneReceipt

        return MilestoneReceipt.objects.create(
            organization=organization,
            milestone=make_milestone(organization),
            received_on=date(2026, 1, 1),
            amount=Decimal("1.00"),
            recorded_by=User.objects.filter(organization=organization).first(),
        )

    # Read-only collections: Finance changes them through named actions.
    register_isolation_fixture("milestone", make_milestone)
    register_isolation_fixture("milestone-invoice", make_milestone_invoice)
    register_isolation_fixture("milestone-receipt", make_milestone_receipt)

    def make_subcontract(organization):
        from commercials.models import Subcontract
        from network.models import Subcontractor

        project = make_project_expense(organization).project
        return Subcontract.objects.create(
            organization=organization,
            number="SC-ISO-1",
            project=project,
            subcontractor=Subcontractor.objects.create(
                organization=organization, name="Isolation Subcontractor"
            ),
            contract_value=Decimal("1.00"),
            created_by=User.objects.filter(organization=organization).first(),
        )

    def make_subcontract_payment(organization):
        from commercials.models import SubcontractPayment

        return SubcontractPayment.objects.create(
            organization=organization,
            subcontract=make_subcontract(organization),
            amount=Decimal("1.00"),
            paid_on=date(2026, 1, 1),
            reference="ISO-PAY-1",
            recorded_by=User.objects.filter(organization=organization).first(),
        )

    register_isolation_fixture(
        "subcontract", make_subcontract, payload={"payment_terms": "Renamed"}
    )
    # No PATCH for payments: a wrong one is rejected or reversed (O16).
    register_isolation_fixture("subcontract-payment", make_subcontract_payment)

    # PATCH takes the photo count only, and only from the recorder (§4.19.10).
    register_isolation_fixture("site-purchase", make_site_purchase)
