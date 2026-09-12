"""Isolation fixtures for the approval endpoints (T1.20, A3)."""

from core.isolation import register_isolation_fixture


def register() -> None:
    from accounts.models import Role
    from approvals.models import ApprovalRequest, ApprovalRule
    from catalogue.models import Criticality

    def make_rule(organization):
        role = Role.objects.filter(organization=organization).first() or Role.objects.create(
            organization=organization, name="Isolation approver"
        )
        return ApprovalRule.objects.create(
            organization=organization,
            criticality=Criticality.HIGH,
            required_role=role,
            sequence=9,
        )

    def make_request(organization):
        role = Role.objects.filter(organization=organization).first() or Role.objects.create(
            organization=organization, name="Isolation approver"
        )
        return ApprovalRequest.objects.create(
            organization=organization,
            document_type="dispatch.GateOut",
            document_id="0",
            document_number="ISO-1",
            level=1,
            required_role=role,
        )

    register_isolation_fixture(
        "approval-rule", make_rule, payload={"criticality": "HIGH", "sequence": 9}
    )
    register_isolation_fixture("approval", make_request)
