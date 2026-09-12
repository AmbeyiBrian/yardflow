"""T1.8 — the error envelope and cross-tenant 404 (§6.1, §2.4, A3)."""

import pytest
from django.http import Http404
from rest_framework import exceptions as drf_exceptions

from core.api import exception_handler, flatten_field_errors
from core.exceptions import (
    AlreadyPosted,
    DomainError,
    OrganizationSuspended,
)
from core.tenancy import TenantContextMissing


class TestEnvelopeShape:
    """§6.1: one shape for every failure, with a stable code."""

    def test_domain_error_renders_the_documented_envelope(self):
        class InsufficientStock(DomainError):
            code = "INSUFFICIENT_STOCK"

        exc = InsufficientStock(
            "Only 340 m remaining on drum D-0007.",
            field_errors={"lines.2.length": ["Exceeds remaining length."]},
            details={"reel": "D-0007", "remaining": "340.000"},
        )

        response = exception_handler(exc, {})

        assert response.status_code == 400
        assert response.data == {
            "error": {
                "code": "INSUFFICIENT_STOCK",
                "message": "Only 340 m remaining on drum D-0007.",
                "field_errors": {"lines.2.length": ["Exceeds remaining length."]},
                "details": {"reel": "D-0007", "remaining": "340.000"},
            }
        }

    def test_optional_keys_are_omitted_when_empty(self):
        """A client should not have to distinguish {} from absent."""
        response = exception_handler(AlreadyPosted(), {})

        assert set(response.data["error"]) == {"code", "message"}

    def test_conflicts_are_409_not_400(self):
        """§13: "the world changed under you" is a conflict, not bad input."""
        response = exception_handler(AlreadyPosted(), {})

        assert response.status_code == 409
        assert response.data["error"]["code"] == "ALREADY_POSTED"

    def test_suspended_organization_is_403_with_its_own_code(self):
        """A2: reads continue, writes are refused."""
        response = exception_handler(OrganizationSuspended(), {})

        assert response.status_code == 403
        assert response.data["error"]["code"] == "ORGANIZATION_SUSPENDED"


class TestCrossTenantIsAlwaysNotFound:
    """A3, §2.4: 404 and never 403, so an id is never confirmed."""

    def test_http404_renders_the_envelope(self):
        response = exception_handler(Http404("gone"), {})

        assert response.status_code == 404
        assert response.data["error"]["code"] == "NOT_FOUND"

    def test_missing_tenant_context_becomes_404_not_500(self):
        """Reaching this through an API request means "not yours".

        A 500 would leak that something went wrong internally; a 403 would
        confirm the resource exists. 404 says neither.
        """
        response = exception_handler(TenantContextMissing("no org"), {})

        assert response.status_code == 404
        assert response.data["error"]["code"] == "NOT_FOUND"

    def test_the_message_does_not_echo_internal_detail(self):
        response = exception_handler(TenantContextMissing("organization 123 not in context"), {})

        assert "123" not in response.data["error"]["message"]


class TestFieldErrorFlattening:
    """§6.1: paths must line up with react-hook-form."""

    def test_simple_field_errors(self):
        assert flatten_field_errors({"name": ["This field is required."]}) == {
            "name": ["This field is required."]
        }

    def test_nested_list_of_objects_becomes_dotted_paths(self):
        detail = {"lines": [{}, {"length": ["Exceeds remaining length."]}]}

        assert flatten_field_errors(detail) == {
            "lines.1.length": ["Exceeds remaining length."]
        }

    def test_deeply_nested_paths(self):
        detail = {"lines": [{"serials": [{"serial_number": ["Duplicate."]}]}]}

        assert flatten_field_errors(detail) == {
            "lines.0.serials.0.serial_number": ["Duplicate."]
        }

    def test_multiple_messages_on_one_field_are_preserved(self):
        detail = {"quantity": ["Must be positive.", "Exceeds stock."]}

        assert flatten_field_errors(detail) == {
            "quantity": ["Must be positive.", "Exceeds stock."]
        }

    def test_drf_validation_error_is_rendered_with_field_errors(self):
        exc = drf_exceptions.ValidationError({"lines": [{"length": ["Too long."]}]})

        response = exception_handler(exc, {})

        assert response.status_code == 400
        assert response.data["error"]["code"] == "VALIDATION_ERROR"
        assert response.data["error"]["field_errors"] == {"lines.0.length": ["Too long."]}


class TestFrameworkExceptions:
    @pytest.mark.parametrize(
        ("exc", "expected_status", "expected_code"),
        [
            (drf_exceptions.NotAuthenticated(), 401, "NOT_AUTHENTICATED"),
            (drf_exceptions.AuthenticationFailed(), 401, "NOT_AUTHENTICATED"),
            (drf_exceptions.PermissionDenied(), 403, "PERMISSION_DENIED"),
            (drf_exceptions.NotFound(), 404, "NOT_FOUND"),
            (drf_exceptions.MethodNotAllowed("PUT"), 405, "METHOD_NOT_ALLOWED"),
            (drf_exceptions.ParseError(), 400, "PARSE_ERROR"),
        ],
    )
    def test_framework_errors_get_stable_codes(self, exc, expected_status, expected_code):
        response = exception_handler(exc, {})

        assert response.status_code == expected_status
        assert response.data["error"]["code"] == expected_code

    def test_an_unexpected_exception_is_left_to_django(self):
        """A bug must surface as a 500, not be dressed up as a domain error."""
        assert exception_handler(ZeroDivisionError("bug"), {}) is None


class TestTheMessageSaysWhatIsWrong:
    """A banner that never varies teaches people to ignore it.

    Reported from use: submitting a delegation with no role showed "The submitted
    data is not valid." — the same sentence as every other refusal — while the
    explanation sat unread in `field_errors`. With one problem, the banner should
    be the explanation.
    """

    def test_a_single_field_error_becomes_the_message(self):
        exc = drf_exceptions.ValidationError(
            {"role": ["Say what is being delegated: either a role, or permissions."]}
        )

        response = exception_handler(exc, {})

        assert response.data["error"]["message"] == (
            "Say what is being delegated: either a role, or permissions."
        )
        # And it stays on the field, so the form can still highlight the input.
        assert "role" in response.data["error"]["field_errors"]

    def test_several_errors_keep_the_general_message(self):
        """Restating six messages in a banner is worse than pointing at six
        fields, which the form already does."""
        exc = drf_exceptions.ValidationError(
            {"role": ["Choose a role."], "ends_at": ["The end has to be after the start."]}
        )

        response = exception_handler(exc, {})

        assert response.data["error"]["message"] == "The submitted data is not valid."
        assert set(response.data["error"]["field_errors"]) == {"role", "ends_at"}

    def test_an_error_belonging_to_no_field_always_wins(self):
        """It has nowhere else to be shown, so it has to reach the banner."""
        exc = drf_exceptions.ValidationError(
            {
                "non_field_errors": ["That window overlaps an existing delegation."],
                "role": ["Choose a role."],
            }
        )

        response = exception_handler(exc, {})

        assert response.data["error"]["message"] == (
            "That window overlaps an existing delegation."
        )
