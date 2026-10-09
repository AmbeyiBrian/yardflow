"""T15.3 — the allowance rules, with no database (§4.17.5; R2, R5).

Pure functions, so these run without a tenant. The service tests cover the same
rules end to end; what is pinned here is the arithmetic at the edges.
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from commercials import finance_rules as rules


@dataclass
class Earlier:
    number: str
    status: str
    from_date: date
    to_date: date


def earlier(number="AR-000001", status="PENDING_PM", start=(2026, 10, 5), end=(2026, 10, 7)):
    return Earlier(number, status, date(*start), date(*end))


class TestDays:
    def test_a_single_day_is_one_day(self):
        assert rules.days_between(date(2026, 10, 5), date(2026, 10, 5)) == 1

    def test_both_ends_count(self):
        assert rules.days_between(date(2026, 10, 5), date(2026, 10, 7)) == 3


class TestOverlap:
    def test_sharing_one_boundary_day_overlaps(self):
        """A request ending on the 7th and one starting on the 7th claim it twice."""
        assert rules.ranges_intersect(
            date(2026, 10, 7), date(2026, 10, 9), date(2026, 10, 5), date(2026, 10, 7)
        )
        assert rules.ranges_intersect(
            date(2026, 10, 3), date(2026, 10, 5), date(2026, 10, 5), date(2026, 10, 7)
        )

    def test_the_next_day_does_not(self):
        assert not rules.ranges_intersect(
            date(2026, 10, 8), date(2026, 10, 9), date(2026, 10, 5), date(2026, 10, 7)
        )
        assert not rules.ranges_intersect(
            date(2026, 10, 3), date(2026, 10, 4), date(2026, 10, 5), date(2026, 10, 7)
        )

    def test_one_range_inside_another_overlaps(self):
        assert rules.ranges_intersect(
            date(2026, 10, 6), date(2026, 10, 6), date(2026, 10, 5), date(2026, 10, 7)
        )

    def test_it_names_the_earlier_request(self):
        clash = rules.find_overlap(
            "NIGHT_OUT", date(2026, 10, 6), date(2026, 10, 6), [earlier("AR-000004")]
        )
        assert clash is not None
        assert clash.number == "AR-000004"

    def test_the_lowest_number_is_named_when_several_clash(self):
        clash = rules.find_overlap(
            "TRANSPORT",
            date(2026, 10, 6),
            date(2026, 10, 6),
            [earlier("AR-000009"), earlier("AR-000003")],
        )
        assert clash is not None
        assert clash.number == "AR-000003"

    def test_float_and_other_are_exempt(self):
        for exempt in ("FLOAT", "OTHER"):
            assert (
                rules.find_overlap(exempt, date(2026, 10, 6), date(2026, 10, 6), [earlier()])
                is None
            )

    def test_only_live_or_paid_requests_block(self):
        for status in ("PENDING_PM", "PENDING_FINANCE", "APPROVED", "PAID"):
            assert rules.find_overlap(
                "TEAM_ALLOWANCE",
                date(2026, 10, 6),
                date(2026, 10, 6),
                [earlier(status=status)],
            )
        assert (
            rules.find_overlap(
                "TEAM_ALLOWANCE",
                date(2026, 10, 6),
                date(2026, 10, 6),
                [earlier(status="REJECTED")],
            )
            is None
        )


LIMITS = {
    "TRANSPORT_WITHIN_NAIROBI": {"min": None, "max": "500"},
    "TRANSPORT_OUTSIDE_NAIROBI": {"min": None, "max": None},
    "NIGHT_OUT": {"min": "1500", "max": "10000"},
}


class TestLimitKey:
    def test_transport_is_keyed_by_scope(self):
        assert rules.limit_key("TRANSPORT", "WITHIN_NAIROBI") == "TRANSPORT_WITHIN_NAIROBI"
        assert rules.limit_key("TRANSPORT", "OUTSIDE_NAIROBI") == "TRANSPORT_OUTSIDE_NAIROBI"

    def test_other_types_are_keyed_by_type(self):
        assert rules.limit_key("NIGHT_OUT") == "NIGHT_OUT"


class TestLimits:
    def test_exactly_on_the_maximum_is_allowed(self):
        assert rules.check_limit("TRANSPORT_WITHIN_NAIROBI", Decimal("1500"), 3, LIMITS) is None

    def test_one_cent_over_is_not(self):
        breach = rules.check_limit("TRANSPORT_WITHIN_NAIROBI", Decimal("1500.01"), 3, LIMITS)
        assert breach is not None
        assert (breach.side, breach.bound) == ("max", Decimal("500"))

    def test_the_daily_figure_is_not_rounded_before_comparing(self):
        """1000.01 over 2 days is 500.005 a day: over, though it rounds to 500.01."""
        breach = rules.check_limit("TRANSPORT_WITHIN_NAIROBI", Decimal("1000.01"), 2, LIMITS)
        assert breach is not None
        assert breach.daily == Decimal("500.005")

    def test_below_the_minimum_is_refused(self):
        breach = rules.check_limit("NIGHT_OUT", Decimal("2999"), 2, LIMITS)
        assert breach is not None
        assert (breach.side, breach.bound) == ("min", Decimal("1500"))

    def test_exactly_on_the_minimum_is_allowed(self):
        assert rules.check_limit("NIGHT_OUT", Decimal("3000"), 2, LIMITS) is None

    def test_a_null_bound_is_no_bound(self):
        assert (
            rules.check_limit("TRANSPORT_OUTSIDE_NAIROBI", Decimal("999999"), 1, LIMITS) is None
        )

    def test_a_type_with_no_entry_is_unlimited(self):
        assert rules.check_limit("FLOAT", Decimal("999999"), 1, LIMITS) is None
        assert rules.check_limit("OTHER", Decimal("0.01"), 1, {}) is None


class TestFloatBalance:
    def test_amount_less_spent_less_returned(self):
        assert rules.float_balance(
            Decimal("5000"), Decimal("1200"), Decimal("300")
        ) == Decimal("3500")

    def test_overspent_goes_negative(self):
        assert rules.float_balance(Decimal("1000"), Decimal("1500"), Decimal("0")) == Decimal(
            "-500"
        )
