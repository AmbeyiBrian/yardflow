"""Dev-side helper for e2e/attendance.spec.ts (T16.16). Run through `manage.py shell`.

Reads E2E_TOOL (make-user | form-days) and its arguments from the environment.
make-user: a fresh technician with a known password, so every run starts with
no days. form-days: runs the hourly sweep's day formation as if it were
`E2E_DAYS_AHEAD` days from now (a day is only routed once its date is over).
"""
import os
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from accounts.models import Role, User, UserRole
from core.models import Organization
from core.tenancy import tenant_context

tool = os.environ["E2E_TOOL"]
org = Organization.objects.get(slug=os.environ.get("E2E_TENANT", "demo"))

with transaction.atomic(), tenant_context(org):
    if tool == "make-user":
        user = User.objects.create_user(
            email=os.environ["E2E_EMAIL"],
            phone=None,
            password=os.environ["E2E_PASSWORD"],
            organization=org,
            full_name=os.environ["E2E_NAME"],
        )
        role = Role.objects.get(name="Technician")
        UserRole.objects.create(organization=org, user=user, role=role)
        print(f"E2E_OK user={user.pk}")
    elif tool == "form-days":
        from attendance import sweeps

        now = timezone.now() + timedelta(days=int(os.environ.get("E2E_DAYS_AHEAD", "2")))
        closed = sweeps.close_stale_sessions(org, now=now)
        formed = sweeps.form_days(org, now=now)
        print(f"E2E_OK closed={closed} formed={formed}")
    else:
        raise SystemExit(f"unknown tool {tool}")
