import io
import json
import logging
import unittest
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal
from unittest.mock import patch
from urllib.error import HTTPError

from mcp import Client

import elering_consumption as api
import elering_consumption_mcp as server
from nordpool_ee import TALLINN, UTC


EIC = "38Z0000000000001"
NOW = datetime(2026, 11, 2, 12, tzinfo=TALLINN)


def complete_report(start="2026-09-29", end=None, resolution="hourly"):
    window = api.make_window(start, end, NOW)
    step = api.interval_length(resolution)
    return api.ConsumptionReport(
        window, EIC, resolution,
        tuple(
            api.Reading(window.start + index * step, Decimal("0.125"), Decimal("0.025"))
            for index in range(int((window.end - window.start) / step))
        ),
        NOW,
    )


class HouseholdResultTests(unittest.TestCase):
    def test_known_energy_is_summed_not_price_weighted_and_has_no_vat(self):
        report = complete_report()
        result = server.build_consumption_result(report, NOW)
        self.assertEqual(result.unit, "kWh")
        self.assertEqual(result.commodity, "ELECTRICITY")
        self.assertEqual(result.timezone, "Europe/Tallinn")
        self.assertEqual(result.window_start, "2026-09-29T00:00:00+03:00")
        self.assertEqual(result.window_end, "2026-09-30T00:00:00+03:00")
        self.assertEqual(result.interval_minutes, 60)
        self.assertEqual(result.summary.consumption.total_kwh, 3)
        self.assertEqual(result.summary.export.total_kwh, 0.6)
        self.assertTrue(result.summary.consumption.complete)
        self.assertTrue(result.summary.export.complete)
        self.assertEqual(result.summary.expected_intervals, 24)
        self.assertEqual(result.summary.pending_intervals, 0)
        self.assertEqual(result.intervals[0].consumption_kwh, 0.125)
        self.assertEqual(result.latest_returned_reading_start, "2026-09-29T23:00:00+03:00")
        self.assertEqual(result.finality, "not_reported_by_supported_api")
        self.assertFalse(hasattr(result, "vat_rate_percent"))
        self.assertNotIn("stale", " ".join(result.warnings).lower())

    def test_dst_hours_and_quarters_are_real_elapsed_intervals(self):
        for delivery_date, hours in [("2026-03-29", 23), ("2026-09-29", 24), ("2026-10-25", 25)]:
            for resolution, multiplier in [("hourly", 1), ("15_minutes", 4)]:
                with self.subTest(date=delivery_date, resolution=resolution):
                    report = complete_report(delivery_date, resolution=resolution)
                    result = server.build_consumption_result(report, NOW)
                    self.assertEqual(result.summary.expected_intervals, hours * multiplier)
                    self.assertEqual(len(result.intervals), hours * multiplier)
                    self.assertEqual(len({row.start for row in result.intervals}), hours * multiplier)
                    self.assertEqual(result.summary.consumption.total_kwh, float(Decimal("0.125") * hours * multiplier))
                    self.assertTrue(result.summary.consumption.complete)
                    for row in result.intervals:
                        start = datetime.fromisoformat(row.start).astimezone(UTC)
                        end = datetime.fromisoformat(row.end).astimezone(UTC)
                        self.assertEqual(end - start, api.interval_length(resolution))
                    if hours == 25 and resolution == "hourly":
                        repeated = [row.start for row in result.intervals if "T03:00" in row.start]
                        self.assertEqual(repeated, [
                            "2026-10-25T03:00:00+03:00", "2026-10-25T03:00:00+02:00",
                        ])

    def test_import_export_nulls_zero_and_gaps_are_separate(self):
        report = complete_report()
        rows = list(report.readings)
        rows[0] = replace(rows[0], consumption_kwh=Decimal(0), export_kwh=None)
        rows[1] = replace(rows[1], consumption_kwh=None, export_kwh=Decimal(0))
        del rows[2]
        result = server.build_consumption_result(replace(report, readings=tuple(rows)), NOW)
        self.assertEqual(result.intervals[0].consumption_kwh, 0)
        self.assertIsNone(result.intervals[0].export_kwh)
        self.assertIsNone(result.intervals[1].consumption_kwh)
        self.assertEqual(result.summary.consumption.total_kwh, 2.625)
        self.assertEqual(result.summary.export.total_kwh, 0.525)
        self.assertEqual(result.summary.consumption.known_intervals, 22)
        self.assertEqual(result.summary.consumption.missing_elapsed_intervals, 2)
        self.assertEqual(result.summary.export.missing_elapsed_intervals, 2)
        self.assertFalse(result.summary.consumption.complete)
        self.assertFalse(result.summary.export.elapsed_complete)
        self.assertEqual(result.missing_intervals, ["2026-09-29T02:00:00+03:00"])
        self.assertIn("not zero", " ".join(result.warnings))

    def test_absent_export_is_not_zero(self):
        report = complete_report()
        rows = tuple(replace(row, export_kwh=None) for row in report.readings)
        result = server.build_consumption_result(replace(report, readings=rows), NOW)
        self.assertTrue(result.summary.consumption.complete)
        self.assertIsNone(result.summary.export.total_kwh)
        self.assertFalse(result.summary.export.complete)
        self.assertEqual(result.summary.export.missing_elapsed_intervals, 24)

    def test_empty_or_all_null_data_is_unavailable_not_zero_use(self):
        report = complete_report()
        for rows in ((), tuple(replace(row, consumption_kwh=None, export_kwh=None) for row in report.readings)):
            with self.subTest(count=len(rows)):
                with self.assertRaisesRegex(api.ConsumptionError, "No measurements"):
                    server.build_consumption_result(replace(report, readings=rows), NOW)

    def test_today_excludes_active_and_future_intervals_without_calling_them_missing(self):
        report = complete_report()
        now = datetime(2026, 9, 29, 12, 15, tzinfo=TALLINN)
        result = server.build_consumption_result(report, now)
        self.assertEqual(result.summary.elapsed_intervals, 12)
        self.assertEqual(result.summary.pending_intervals, 12)
        self.assertEqual(len(result.intervals), 12)
        self.assertEqual(result.summary.consumption.total_kwh, 1.5)
        self.assertEqual(result.summary.consumption.missing_elapsed_intervals, 0)
        self.assertTrue(result.summary.consumption.elapsed_complete)
        self.assertFalse(result.summary.consumption.complete)
        self.assertEqual(result.pending_intervals[0], "2026-09-29T12:00:00+03:00")
        self.assertEqual(result.missing_intervals, [])
        self.assertEqual(result.latest_returned_reading_start, "2026-09-29T11:00:00+03:00")

    def test_partial_and_full_calendar_months_are_labeled(self):
        partial = complete_report("2026-09-29", "2026-10-02")
        result = server.build_consumption_result(partial, NOW)
        self.assertEqual(len(result.daily_summaries), 4)
        self.assertEqual([item.period for item in result.monthly_summaries], ["2026-09", "2026-10"])
        self.assertTrue(all(not item.covers_full_calendar_period for item in result.monthly_summaries))
        self.assertEqual(result.monthly_summaries[0].summary.consumption.total_kwh, 6)
        self.assertEqual(result.monthly_summaries[1].summary.consumption.total_kwh, 6)
        self.assertEqual(result.summary.consumption.total_kwh, 12)
        complete = server.build_consumption_result(complete_report("2026-09-01", "2026-09-30"), NOW)
        self.assertTrue(complete.monthly_summaries[0].covers_full_calendar_period)
        self.assertEqual(complete.monthly_summaries[0].summary.consumption.total_kwh, 90)

    def test_empty_days_keep_null_summaries(self):
        report = complete_report("2026-09-29", "2026-09-30")
        rows = tuple(row for row in report.readings if row.start.astimezone(TALLINN).day == 29)
        result = server.build_consumption_result(replace(report, readings=rows), NOW)
        empty = result.daily_summaries[1]
        self.assertEqual(empty.period, "2026-09-30")
        self.assertIsNone(empty.summary.consumption.total_kwh)
        self.assertEqual(empty.summary.consumption.missing_elapsed_intervals, 24)
        self.assertFalse(result.summary.consumption.complete)

    def test_unrepresentable_sum_is_an_explicit_error(self):
        report = complete_report()
        rows = tuple(replace(row, consumption_kwh=Decimal("1.7e308")) for row in report.readings)
        with self.assertRaisesRegex(api.ConsumptionError, "numeric range"):
            server.build_consumption_result(replace(report, readings=rows), NOW)


class HouseholdProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def test_discovers_only_two_read_only_tools_and_calls_consumption(self):
        report = complete_report()
        with patch("elering_consumption_mcp.tallinn_now", return_value=NOW), patch.object(
            server._client, "get_consumption", return_value=report
        ) as fetch:
            async with Client(server.mcp, raise_exceptions=True) as client:
                listed = await client.list_tools()
                response = await client.call_tool("get_household_consumption", {"start_date": "2026-09-29"})
        self.assertEqual({tool.name for tool in listed.tools}, {
            "list_household_metering_points", "get_household_consumption",
        })
        for tool in listed.tools:
            self.assertTrue(tool.annotations.read_only_hint)
            self.assertFalse(tool.annotations.destructive_hint)
            self.assertNotIn("client_secret", tool.input_schema["properties"])
        self.assertFalse(response.is_error)
        self.assertEqual(json.loads(response.content[0].text), response.structured_content)
        self.assertEqual(response.structured_content["summary"]["consumption"]["total_kwh"], 3)
        self.assertEqual(fetch.call_args.args[1], "hourly")

    async def test_metering_discovery_defaults_and_access_periods(self):
        points = api.PointReport((api.MeteringPoint(EIC, (api.AccessPeriod(datetime(2020, 1, 1, tzinfo=UTC), None),)),), NOW)
        with patch("elering_consumption_mcp.tallinn_now", return_value=NOW), patch.object(
            server._client, "list_metering_points", return_value=points
        ) as fetch:
            async with Client(server.mcp, raise_exceptions=True) as client:
                response = await client.call_tool("list_household_metering_points", {})
        self.assertFalse(response.is_error)
        data = response.structured_content
        self.assertEqual(data["metering_points"][0]["eic"], EIC)
        self.assertIsNone(data["metering_points"][0]["access_periods"][0]["end"])
        window = fetch.call_args.args[0]
        self.assertEqual((window.end_date - window.start_date).days, 30)
        self.assertEqual(window.end_date, NOW.date())
        self.assertEqual(json.loads(response.content[0].text), data)

    async def test_validation_errors_never_fetch_measurements(self):
        with patch("elering_consumption_mcp.tallinn_now", return_value=NOW), patch.object(
            server._client, "get_consumption"
        ) as fetch:
            async with Client(server.mcp, raise_exceptions=True) as client:
                for arguments in (
                    {"start_date": "bad"},
                    {"start_date": "2026-09-01", "end_date": "2026-10-31"},
                    {"start_date": "2026-11-03"},
                    {"start_date": "2026-09-29", "resolution": "one_month"},
                ):
                    response = await client.call_tool("get_household_consumption", arguments)
                    self.assertTrue(response.is_error)
        fetch.assert_not_called()

    async def test_private_transport_errors_are_redacted_in_mcp_and_logs(self):
        secret = "sentinel-private-client-secret"
        token = "sentinel-private-access-token"
        source = api.EstfeedClient(lambda: api.Credentials("synthetic-client", secret))
        failure = HTTPError(
            api.TOKEN_URL, 401, secret, {},
            io.BytesIO(json.dumps({"error": secret, "access_token": token}).encode()),
        )
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        logging.getLogger().addHandler(handler)
        try:
            with patch.object(source._opener, "open", side_effect=failure), patch.object(
                server, "_client", source
            ), patch("elering_consumption_mcp.tallinn_now", return_value=NOW):
                async with Client(server.mcp, raise_exceptions=True) as client:
                    response = await client.call_tool("get_household_consumption", {"start_date": "2026-09-29"})
        finally:
            logging.getLogger().removeHandler(handler)
        self.assertTrue(response.is_error)
        self.assertIsNone(response.structured_content)
        rendered = response.content[0].text + stream.getvalue()
        self.assertIn("authentication failed", rendered)
        self.assertNotIn(secret, rendered)
        self.assertNotIn(token, rendered)


if __name__ == "__main__":
    unittest.main()
