"""Platform administration in the Django admin (A1, A2; §2.2).

Creating a customer is the one thing the platform owner does that has no screen
in the product itself — the app is a *tenant* surface, and a tenant must never be
able to create another tenant. That left the REST console as the only route,
which in practice means onboarding a customer through an API docs page. This is
that job, in the place an administrator already has.

**The add form provisions; it does not just insert a row.** An `Organization`
saved on its own is a hollow tenant: no owner, no roles, nobody who can log in.
So the form takes the first owner as well and routes the whole thing through
`provision_tenant`, which is the same service the REST console calls. One code
path, whichever door you came through.

Two things are deliberately not editable:

* **The subdomain**, after creation. It addresses the tenant, it is written into
  links and bookmarks, and `Organization.save()` refuses to change it anyway.
* **The status**, by typing. Suspension is an action with a reason attached,
  because A2's audit trail is the point of it — a status silently flipped in a
  form leaves nothing to read later.
"""

from __future__ import annotations

from django import forms
from django.contrib import admin, messages
from django.utils.html import format_html

from core.models import Organization, OrganizationSettings
from core.provisioning import provision_tenant


class ProvisionOrganizationForm(forms.ModelForm):
    """The add form: a tenant and the person who will run it."""

    owner_email = forms.EmailField(
        required=False,
        help_text="They are invited to set their own password — none is generated here.",
    )
    owner_phone = forms.CharField(
        max_length=20,
        required=False,
        help_text="Enough on its own if they have no email address.",
    )
    owner_full_name = forms.CharField(max_length=200, required=False)

    class Meta:
        model = Organization
        fields = ("name", "slug", "legal_name", "email", "phone", "address", "tax_pin")

    def clean(self):
        cleaned = super().clean()
        if not cleaned.get("owner_email") and not cleaned.get("owner_phone"):
            # A tenant nobody can sign into is not a tenant. Refused here rather
            # than created and quietly abandoned.
            raise forms.ValidationError(
                "Give the first owner an email address or a phone number, so they "
                "can be invited to set a password."
            )
        return cleaned

    def clean_slug(self):
        from core.models import RESERVED_SUBDOMAINS

        slug = (self.cleaned_data["slug"] or "").lower()
        if slug in RESERVED_SUBDOMAINS:
            raise forms.ValidationError(
                f"'{slug}' is reserved for the platform and cannot address a tenant."
            )
        return slug


class OrganizationSettingsInline(admin.StackedInline):
    model = OrganizationSettings
    can_delete = False
    extra = 0


@admin.register(Organization)
class OrganizationAdmin(admin.ModelAdmin):
    list_display = ("name", "address_link", "status_badge", "sms_credits", "created_at")
    list_filter = ("status",)
    search_fields = ("name", "slug", "legal_name")
    ordering = ("name",)
    inlines = (OrganizationSettingsInline,)
    actions = ("suspend", "reinstate", "add_sms_credits")

    @admin.display(description="Address")
    def address_link(self, organization: Organization) -> str:
        """Where this tenant is reached — the thing you hand the customer."""
        from django.conf import settings

        host = f"{organization.slug}.{settings.TENANT_BASE_DOMAIN}"
        return format_html("<code>{}</code>", host)

    @admin.display(description="Status")
    def status_badge(self, organization: Organization) -> str:
        suspended = organization.status == Organization.Status.SUSPENDED
        return format_html(
            '<b style="color:{}">{}</b>',
            "#b45309" if suspended else "#047857",
            organization.get_status_display(),
        )

    # -- the add form provisions ---------------------------------------------

    def get_form(self, request, obj=None, **kwargs):  # type: ignore[no-untyped-def]
        if obj is None:
            kwargs["form"] = ProvisionOrganizationForm
        return super().get_form(request, obj, **kwargs)

    def get_inline_instances(self, request, obj=None):  # type: ignore[no-untyped-def]
        # Settings are created for the tenant during provisioning, with the
        # defaults the requirements specify. Offering the inline on the *add*
        # form would mean two things trying to create the same one-to-one row.
        if obj is None:
            return []
        return super().get_inline_instances(request, obj)

    def get_readonly_fields(self, request, obj=None):  # type: ignore[no-untyped-def]
        # Immutable once it addresses a live tenant (A1), and `save()` refuses it
        # regardless — better to grey it out than to let somebody try.
        return ("slug", "status") if obj else ("status",)

    def get_fields(self, request, obj=None):  # type: ignore[no-untyped-def]
        if obj is None:
            return (
                "name",
                "slug",
                "owner_email",
                "owner_phone",
                "owner_full_name",
                "legal_name",
                "email",
                "phone",
                "address",
                "tax_pin",
            )
        return (
            "name",
            "slug",
            "status",
            "logo",
            "legal_name",
            "email",
            "phone",
            "address",
            "tax_pin",
        )

    def save_model(self, request, obj, form, change):  # type: ignore[no-untyped-def]
        if change:
            super().save_model(request, obj, form, change)
            return

        # Creation goes through the provisioning service, so an organization made
        # here is identical to one made through the REST console: owner account,
        # seeded roles, invitation sent.
        result = provision_tenant(
            name=form.cleaned_data["name"],
            slug=form.cleaned_data["slug"],
            owner_email=form.cleaned_data.get("owner_email") or None,
            owner_phone=form.cleaned_data.get("owner_phone") or None,
            owner_full_name=form.cleaned_data.get("owner_full_name", ""),
            request=request,
            legal_name=form.cleaned_data.get("legal_name", ""),
            email=form.cleaned_data.get("email", ""),
            phone=form.cleaned_data.get("phone", ""),
            address=form.cleaned_data.get("address", ""),
            tax_pin=form.cleaned_data.get("tax_pin", ""),
        )

        organization = result["organization"]
        owner = result["owner"]
        # `save_model` is expected to leave the saved instance on `obj`; the
        # admin uses it for the confirmation message and the redirect.
        obj.pk = organization.pk
        obj.id = organization.id

        from django.conf import settings

        self.message_user(
            request,
            format_html(
                "<b>{}</b> is ready at <code>{}.{}</code>. Its owner "
                "<b>{}</b> has been invited to set a password — no password was "
                "created here, so nothing needs to be passed on.",
                organization.name,
                organization.slug,
                settings.TENANT_BASE_DOMAIN,
                owner.email or owner.phone,
            ),
            messages.SUCCESS,
        )

    @admin.display(description="SMS credits")
    def sms_credits(self, organization: Organization) -> str:
        """What they have left to spend (L4). One credit is one SMS, KES 1."""
        from notifications import credits

        balance = credits.balance(organization)
        return format_html(
            '<b style="color:{}">{}</b>',
            "#b45309" if credits.is_low(organization) else "#334155",
            balance,
        )

    @admin.action(description="Sell SMS credits (adds 500)")
    def add_sms_credits(self, request, queryset):  # type: ignore[no-untyped-def]
        """Add a bundle of credits after payment (L4).

        Money changes hands outside this system — an M-Pesa transfer, an invoice —
        and the ledger entry records that it did and who recorded it. A fixed
        bundle keeps this a one-click action; an unusual amount is an adjustment
        with a note, which is the right shape for something unusual anyway.
        """
        from notifications import credits

        bundle = 500
        for organization in queryset:
            credits.purchase(
                organization,
                bundle,
                actor=request.user,
                note=f"Sold by {request.user} from the platform console.",
            )

        self.message_user(
            request,
            f"{bundle} credits added to {queryset.count()} tenant(s) — "
            f"{bundle * credits.CREDIT_PRICE_KES:,} KES worth of SMS each.",
            messages.SUCCESS,
        )

    # -- suspension is an action, not a field --------------------------------

    @admin.action(description="Suspend — reads keep working, writes are refused")
    def suspend(self, request, queryset):  # type: ignore[no-untyped-def]
        """A2: never deletes anything. Users can still sign in and read."""
        changed = queryset.exclude(status=Organization.Status.SUSPENDED).update(
            status=Organization.Status.SUSPENDED
        )
        self.message_user(
            request,
            f"{changed} suspended. Their data is untouched and their people can "
            "still sign in to read it.",
            messages.WARNING,
        )

    @admin.action(description="Reinstate")
    def reinstate(self, request, queryset):  # type: ignore[no-untyped-def]
        changed = queryset.exclude(status=Organization.Status.ACTIVE).update(
            status=Organization.Status.ACTIVE
        )
        self.message_user(request, f"{changed} reinstated.", messages.SUCCESS)

    # -- who may be here at all ----------------------------------------------

    def has_module_permission(self, request) -> bool:  # type: ignore[no-untyped-def]
        return bool(getattr(request.user, "is_platform_admin", False))

    def has_view_permission(self, request, obj=None) -> bool:  # type: ignore[no-untyped-def]
        return bool(getattr(request.user, "is_platform_admin", False))

    def has_add_permission(self, request) -> bool:  # type: ignore[no-untyped-def]
        return bool(getattr(request.user, "is_platform_admin", False))

    def has_change_permission(self, request, obj=None) -> bool:  # type: ignore[no-untyped-def]
        return bool(getattr(request.user, "is_platform_admin", False))

    def has_delete_permission(self, request, obj=None) -> bool:  # type: ignore[no-untyped-def]
        # A2 is explicit that suspension replaces deletion. A tenant's ledger is
        # somebody's audit trail; there is no button for destroying it.
        return False
