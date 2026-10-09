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
