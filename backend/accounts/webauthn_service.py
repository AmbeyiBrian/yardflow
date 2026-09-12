"""WebAuthn enrolment and approval step-up (design §5.3, §4.8; B5, F4, M3).

D9 keeps WebAuthn to one job in v1: **proving who authorised a gate-out**. It is
not a login replacement. The fingerprint is the signature an auditor asks about
(M3), so what matters is that the assertion cannot be lifted from one approval
and used on another.

That is T8.9's criterion, and it is the whole design of this module:

> Assertion challenge **bound to the `ApprovalRequest` id** so it cannot be
> replayed against another document.

A challenge is stored against ``(user, approval_request)`` and consumed when it
is used. So an assertion captured on the wire — or a screen that kept one around
— cannot authorise a different pass, and cannot authorise the same one twice.

Two further decisions:

**Several credentials per user** (B5's edge case: "losing a device must not lock
the user out"). One credential per user turns a lost phone into a lockout, and a
lockout on the approval step means material stops leaving the yard.

**Revoked, not deleted.** An approval signed with a credential stays explicable
after the credential is gone — which is the point of recording it at all (M3).
"""

from __future__ import annotations

import base64
import logging
import secrets
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from core.exceptions import DomainError

logger = logging.getLogger(__name__)

#: How long a challenge is good for. Short: a step-up happens in the seconds
#: after the button is tapped, and a challenge lying around is a replay waiting
#: for an opportunity.
CHALLENGE_TTL = timedelta(minutes=5)


class WebAuthnError(DomainError):
    code = "WEBAUTHN_FAILED"
    status_code = 400
    default_message = "That security key or fingerprint could not be verified."


class ChallengeMismatch(WebAuthnError):
    """T8.9: the assertion was for a different approval, or an expired challenge.

    Its own code because this is the control rather than a glitch — a client
    seeing it has either replayed something or waited too long, and those need
    different words on screen.
    """

    code = "WEBAUTHN_CHALLENGE_MISMATCH"
    status_code = 409
    default_message = "That fingerprint was for a different approval. Ask again on this one."


def relying_party() -> tuple[str, str]:
    """The RP id and name.

    The id is the registrable domain, **not** the tenant subdomain: a credential
    enrolled on ``silvertech.yardflow.co.ke`` has to keep working if the tenant
    is ever addressed differently, and WebAuthn scopes credentials to the id.
    """
    return (
        getattr(settings, "WEBAUTHN_RP_ID", None) or settings.TENANT_BASE_DOMAIN,
        getattr(settings, "WEBAUTHN_RP_NAME", "YardFlow"),
    )


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


# --------------------------------------------------------------------------
# T8.8 — enrolment
# --------------------------------------------------------------------------


def begin_registration(user, *, device_label: str = "") -> dict:
    """Options for enrolling a new authenticator (B5).

    Existing credentials are excluded, so a user who taps enrol twice on the same
    phone gets told it is already enrolled rather than silently creating a
    duplicate they cannot tell apart later.
    """
    from webauthn import generate_registration_options, options_to_json
    from webauthn.helpers.structs import (
        AuthenticatorSelectionCriteria,
        PublicKeyCredentialDescriptor,
        ResidentKeyRequirement,
        UserVerificationRequirement,
    )

    rp_id, rp_name = relying_party()

    existing = [
        PublicKeyCredentialDescriptor(id=_decode(credential.credential_id))
        for credential in user.webauthn_credentials.filter(is_active=True)
    ]

    options = generate_registration_options(
        rp_id=rp_id,
        rp_name=rp_name,
        user_id=str(user.pk).encode(),
        user_name=user.email or user.phone or str(user.pk),
        user_display_name=user.full_name or user.email or "",
        exclude_credentials=existing,
        authenticator_selection=AuthenticatorSelectionCriteria(
            # A platform authenticator: the fingerprint sensor on the phone the
            # owner already carries. D9's step-up is meant to be one tap, and a
            # roaming key would mean carrying something extra.
            resident_key=ResidentKeyRequirement.PREFERRED,
            user_verification=UserVerificationRequirement.REQUIRED,
        ),
    )

    _store_challenge(user, options.challenge, purpose="register")
    payload = options_to_json(options)
    return {"options": payload, "device_label": device_label}


def complete_registration(user, *, credential: dict, device_label: str = ""):
    """Verify the attestation and store the credential (B5)."""
    from webauthn import verify_registration_response

    challenge = _take_challenge(user, purpose="register")
    rp_id, _rp_name = relying_party()

    try:
        verification = verify_registration_response(
            credential=credential,
            expected_challenge=challenge,
            expected_rp_id=rp_id,
            expected_origin=_expected_origins(),
        )
    except Exception as failure:
        raise WebAuthnError(
            "That authenticator could not be verified. Try enrolling again."
        ) from failure

    from accounts.models import WebAuthnCredential

    return WebAuthnCredential.objects.create(
        organization_id=user.organization_id,
        user=user,
        credential_id=_b64(verification.credential_id),
        public_key=_b64(verification.credential_public_key),
        sign_count=verification.sign_count or 0,
        aaguid=verification.aaguid or "",
        device_label=device_label or "This device",
    )


def revoke_credential(credential, *, revoked_by=None):
    """B5: "an admin can revoke enrolled credentials" — without a lockout.

    Revoked rather than deleted, so an approval signed with it stays explicable
    years later (M3). The caller checks that the user keeps at least one, because
    a user with none simply falls back to a password — which is a degradation,
    not a lockout.
    """
    credential.is_active = False
    credential.revoked_at = timezone.now()
    credential.revoked_by = revoked_by
    credential.save(update_fields=["is_active", "revoked_at", "revoked_by", "updated_at"])
    return credential


# --------------------------------------------------------------------------
# T8.9 — approval step-up
# --------------------------------------------------------------------------


def begin_assertion(user, approval_request) -> dict:
    """A challenge bound to one approval request (T8.9, F4, M3).

    The binding is the requirement. Without it, an assertion obtained for a
    trivial approval could be replayed against a large one, and the signature an
    auditor relies on would prove only that the person once touched a sensor.
    """
    from webauthn import generate_authentication_options, options_to_json
    from webauthn.helpers.structs import (
        PublicKeyCredentialDescriptor,
        UserVerificationRequirement,
    )

    credentials = list(user.webauthn_credentials.filter(is_active=True))
    if not credentials:
        raise WebAuthnError(
            "No fingerprint or security key is enrolled on this account. Enrol "
            "one in your settings, or approve with your password."
        )

    rp_id, _rp_name = relying_party()
    options = generate_authentication_options(
        rp_id=rp_id,
        allow_credentials=[
            PublicKeyCredentialDescriptor(id=_decode(credential.credential_id))
            for credential in credentials
        ],
        user_verification=UserVerificationRequirement.REQUIRED,
    )

    _store_challenge(user, options.challenge, purpose="approve", approval_request=approval_request)
    return {
        "options": options_to_json(options),
        "approval_request": approval_request.pk,
    }


def complete_assertion(user, approval_request, *, credential: dict):
    """Verify an assertion **for this approval** and return the credential used.

    Two refusals matter here, and they are different:

    * the challenge was raised for another approval — a replay (T8.9)
    * the signature counter went backwards — a cloned authenticator, which is
      what the counter exists to detect
    """
    from webauthn import verify_authentication_response

    from accounts.models import WebAuthnCredential

    challenge = _take_challenge(user, purpose="approve", approval_request=approval_request)

    raw_id = credential.get("id") or credential.get("rawId") or ""
    stored = WebAuthnCredential.objects.filter(
        user=user, credential_id=_normalise(raw_id), is_active=True
    ).first()
    if stored is None:
        raise WebAuthnError("That credential is not enrolled, or has been revoked.")

    rp_id, _rp_name = relying_party()
    try:
        verification = verify_authentication_response(
            credential=credential,
            expected_challenge=challenge,
            expected_rp_id=rp_id,
            expected_origin=_expected_origins(),
            credential_public_key=_decode(stored.public_key),
            credential_current_sign_count=stored.sign_count,
            require_user_verification=True,
        )
    except Exception as failure:
        raise WebAuthnError(
            "That fingerprint could not be verified against this approval."
        ) from failure

    stored.sign_count = verification.new_sign_count
    stored.last_used_at = timezone.now()
    stored.save(update_fields=["sign_count", "last_used_at", "updated_at"])
    return stored


# --------------------------------------------------------------------------
# Challenges
# --------------------------------------------------------------------------
#
# Kept in the cache rather than a table. A challenge lives for seconds, is used
# once, and has no audit value of its own — what the audit trail records is the
# *credential* that signed (M3). A table would need sweeping, and a stale row in
# it would be a replay window.


def _challenge_key(user, purpose: str, approval_request=None) -> str:
    suffix = f":{approval_request.pk}" if approval_request is not None else ""
    return f"webauthn:{purpose}:{user.organization_id}:{user.pk}{suffix}"


def _store_challenge(user, challenge: bytes, *, purpose: str, approval_request=None) -> None:
    from django.core.cache import cache

    cache.set(
        _challenge_key(user, purpose, approval_request),
        _b64(challenge),
        int(CHALLENGE_TTL.total_seconds()),
    )


def _take_challenge(user, *, purpose: str, approval_request=None) -> bytes:
    """Fetch and consume a challenge. Absent means expired, or replayed."""
    from django.core.cache import cache

    key = _challenge_key(user, purpose, approval_request)
    stored = cache.get(key)
    if stored is None:
        raise ChallengeMismatch(
            "There is no live challenge for this. It may have expired, or it "
            "was raised for a different approval (T8.9)."
        )
    # Consumed: one challenge authorises one action.
    cache.delete(key)
    return _decode(stored)


def _decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def _normalise(raw_id: str) -> str:
    """Credential ids arrive base64url with or without padding."""
    return raw_id.rstrip("=")


def _expected_origins() -> list[str]:
    """Every origin a tenant might be addressed from (§2.2).

    Subdomains are per tenant, so the expected origin cannot be a single string
    — and getting this wrong shows up as a verification failure nobody can
    debug from the message.
    """
    configured = getattr(settings, "WEBAUTHN_ORIGINS", None)
    if configured:
        return list(configured)

    base = settings.TENANT_BASE_DOMAIN
    scheme = "http" if base in ("localhost", "127.0.0.1") else "https"
    # The dev frontend runs on 5173, and a credential enrolled there has to work
    # there.
    if scheme == "http":
        return [f"http://{base}:5173", f"http://{base}:8000", f"http://{base}"]
    return [f"https://{base}"]


def new_challenge_bytes() -> bytes:
    """Only used by tests that need a challenge without the library."""
    return secrets.token_bytes(32)
