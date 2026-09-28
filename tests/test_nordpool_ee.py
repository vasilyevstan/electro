import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from io import StringIO
from unittest.mock import patch
from urllib.error import URLError

import nordpool_ee


class FakeResponse:
    def __init__(self, body, status=200):
        self.body = body
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def read(self):
        return self.body

    def getcode(self):
        return self.status


def complete_payload(delivery_date, price_factory=None):
    records = []
    for index, timestamp in enumerate(
        nordpool_ee.expected_interval_timestamps(delivery_date)
    ):
        price = price_factory(index) if price_factory else 10 + index / 100
        records.append({"timestamp": timestamp, "price": price})
    return {"success": True, "data": {"ee": records}}


class TimeWindowTests(unittest.TestCase):
    def test_current_delivery_date_uses_tallinn_not_machine_timezone(self):
        now_utc = datetime(2026, 9, 28, 21, 30, tzinfo=timezone.utc)

        self.assertEqual(
            nordpool_ee.current_delivery_date(now_utc),
            date(2026, 9, 29),
        )

    def test_next_delivery_date_uses_tallinn_not_machine_timezone(self):
        now_utc = datetime(2026, 9, 28, 21, 30, tzinfo=timezone.utc)

        self.assertEqual(
            nordpool_ee.next_delivery_date(now_utc),
            date(2026, 9, 30),
        )

    def test_next_delivery_date_rejects_naive_datetime(self):
        with self.assertRaisesRegex(ValueError, "timezone"):
            nordpool_ee.next_delivery_date(datetime(2026, 9, 28, 12, 0))

    def test_current_delivery_date_rejects_naive_datetime(self):
        with self.assertRaisesRegex(ValueError, "timezone"):
            nordpool_ee.current_delivery_date(datetime(2026, 9, 28, 12, 0))

    def test_regular_day_has_expected_utc_bounds_and_96_intervals(self):
        delivery_date = date(2026, 9, 29)

        start_utc, end_utc = nordpool_ee.market_bounds(delivery_date)

        self.assertEqual(
            start_utc,
            datetime(2026, 9, 28, 21, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(
            end_utc,
            datetime(2026, 9, 29, 21, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(
            len(nordpool_ee.expected_interval_timestamps(delivery_date)),
            96,
        )

    def test_dst_start_day_has_92_intervals(self):
        delivery_date = date(2026, 3, 29)
        start_utc, end_utc = nordpool_ee.market_bounds(delivery_date)

        self.assertEqual(end_utc - start_utc, timedelta(hours=23))
        self.assertEqual(
            len(nordpool_ee.expected_interval_timestamps(delivery_date)),
            92,
        )

    def test_dst_end_day_has_100_intervals(self):
        delivery_date = date(2026, 10, 25)
        start_utc, end_utc = nordpool_ee.market_bounds(delivery_date)

        self.assertEqual(end_utc - start_utc, timedelta(hours=25))
        self.assertEqual(
            len(nordpool_ee.expected_interval_timestamps(delivery_date)),
            100,
        )

    def test_api_url_uses_inclusive_millisecond_end(self):
        url = nordpool_ee.build_api_url(date(2026, 9, 29))

        self.assertIn("start=2026-09-28T21%3A00%3A00.000Z", url)
        self.assertIn("end=2026-09-29T20%3A59%3A59.999Z", url)


class PayloadTests(unittest.TestCase):
    def setUp(self):
        self.delivery_date = date(2026, 9, 29)

    def test_normalizes_complete_estonia_market_day(self):
        report = nordpool_ee.normalize_payload(
            complete_payload(self.delivery_date),
            self.delivery_date,
        )

        self.assertEqual(report.delivery_date, self.delivery_date)
        self.assertEqual(len(report.intervals), 96)
        self.assertEqual(
            report.intervals[0].start.isoformat(),
            "2026-09-29T00:00:00+03:00",
        )
        self.assertEqual(report.intervals[0].price_eur_mwh, Decimal("10.0"))
        self.assertEqual(
            report.intervals[-1].start.isoformat(),
            "2026-09-29T23:45:00+03:00",
        )

    def test_empty_estonia_series_means_not_published(self):
        payload = {"success": True, "data": {"ee": []}}

        with self.assertRaises(nordpool_ee.PricesNotPublishedError):
            nordpool_ee.normalize_payload(payload, self.delivery_date)

    def test_missing_interval_is_rejected(self):
        payload = complete_payload(self.delivery_date)
        payload["data"]["ee"].pop()

        with self.assertRaisesRegex(nordpool_ee.PriceDataError, "1 missing"):
            nordpool_ee.normalize_payload(payload, self.delivery_date)

    def test_duplicate_timestamp_is_rejected(self):
        payload = complete_payload(self.delivery_date)
        payload["data"]["ee"].append(dict(payload["data"]["ee"][0]))

        with self.assertRaisesRegex(nordpool_ee.PriceDataError, "duplicate"):
            nordpool_ee.normalize_payload(payload, self.delivery_date)

    def test_missing_estonia_key_is_rejected(self):
        payload = {"success": True, "data": {}}

        with self.assertRaisesRegex(nordpool_ee.PriceDataError, "data.ee"):
            nordpool_ee.normalize_payload(payload, self.delivery_date)

    def test_invalid_price_is_rejected(self):
        payload = complete_payload(self.delivery_date)
        payload["data"]["ee"][0]["price"] = "11.99"

        with self.assertRaisesRegex(nordpool_ee.PriceDataError, "invalid price"):
            nordpool_ee.normalize_payload(payload, self.delivery_date)

    def test_records_outside_target_day_do_not_substitute_for_target(self):
        other_date = date(2026, 9, 28)

        with self.assertRaises(nordpool_ee.PricesNotPublishedError):
            nordpool_ee.normalize_payload(
                complete_payload(other_date),
                self.delivery_date,
            )


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.delivery_date = date(2026, 9, 29)

    def test_fetch_payload_decodes_json_and_passes_timeout(self):
        body = json.dumps(complete_payload(self.delivery_date)).encode("utf-8")
        observed = {}

        def opener(request, timeout):
            observed["url"] = request.full_url
            observed["timeout"] = timeout
            return FakeResponse(body)

        payload = nordpool_ee.fetch_payload(
            self.delivery_date,
            timeout=7,
            opener=opener,
        )

        self.assertTrue(payload["success"])
        self.assertIn("start=", observed["url"])
        self.assertEqual(observed["timeout"], 7)
        self.assertIsInstance(payload["data"]["ee"][0]["price"], Decimal)

    def test_network_failure_is_explicit(self):
        def opener(request, timeout):
            raise URLError("offline")

        with self.assertRaisesRegex(nordpool_ee.PriceNetworkError, "offline"):
            nordpool_ee.fetch_payload(self.delivery_date, opener=opener)

    def test_non_success_status_is_network_error(self):
        def opener(request, timeout):
            return FakeResponse(b"{}", status=503)

        with self.assertRaisesRegex(nordpool_ee.PriceNetworkError, "503"):
            nordpool_ee.fetch_payload(self.delivery_date, opener=opener)

    def test_non_json_response_is_rejected(self):
        def opener(request, timeout):
            return FakeResponse(b"not json")

        with self.assertRaisesRegex(nordpool_ee.PriceDataError, "invalid JSON"):
            nordpool_ee.fetch_payload(self.delivery_date, opener=opener)


class OutputTests(unittest.TestCase):
    def setUp(self):
        self.delivery_date = date(2026, 9, 29)

    def test_unit_conversion(self):
        self.assertEqual(
            nordpool_ee.eur_mwh_to_cents_kwh(Decimal("11.99")),
            Decimal("1.199"),
        )

    def test_report_contains_intervals_units_and_summary(self):
        report = nordpool_ee.normalize_payload(
            complete_payload(
                self.delivery_date,
                price_factory=lambda index: 11.99 if index == 0 else 20,
            ),
            self.delivery_date,
        )

        rendered = nordpool_ee.render_report(report)

        self.assertIn("2026-09-29", rendered)
        self.assertIn("00:00 EEST", rendered)
        self.assertIn("11.99", rendered)
        self.assertIn("1.199", rendered)
        self.assertIn("Intervals: 96", rendered)
        self.assertIn("Duration-weighted average:", rendered)
        self.assertIn(nordpool_ee.API_URL, rendered)

    def test_cli_returns_unavailable_exit_code(self):
        stdout = StringIO()
        stderr = StringIO()

        with patch(
            "nordpool_ee.fetch_prices",
            side_effect=nordpool_ee.PricesNotPublishedError("not published"),
        ):
            with redirect_stdout(stdout), redirect_stderr(stderr):
                exit_code = nordpool_ee.main([])

        self.assertEqual(exit_code, nordpool_ee.EXIT_UNAVAILABLE)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("Price data unavailable", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
