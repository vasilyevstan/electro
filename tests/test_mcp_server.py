import json
import unittest
from datetime import date, datetime
from unittest.mock import patch

from mcp import Client
from mcp.types import TextContent

import mcp_server
import nordpool_ee


def complete_report(delivery_date, price_factory=None):
    records = [
        {
            "timestamp": timestamp,
            "price": (
                price_factory(index)
                if price_factory is not None
                else (11.99 if index == 0 else 20)
            ),
        }
        for index, timestamp in enumerate(
            nordpool_ee.expected_interval_timestamps(delivery_date)
        )
    ]
    return nordpool_ee.normalize_payload(
        {"success": True, "data": {"ee": records}},
        delivery_date,
    )


def evening_report():
    prices = [20.01, 8.77, 7.05, 6.05]
    return complete_report(
        date(2026, 9, 28),
        price_factory=lambda index: prices[index - 92] if index >= 92 else 20,
    )


class MCPResultTests(unittest.TestCase):
    def test_builds_typed_structured_result(self):
        result = mcp_server.build_price_result(
            complete_report(date(2026, 9, 29))
        )

        self.assertEqual(result.area, "EE")
        self.assertEqual(result.delivery_date, "2026-09-29")
        self.assertEqual(result.timezone, "Europe/Tallinn")
        self.assertEqual(result.interval_minutes, 15)
        self.assertEqual(result.interval_count, 96)
        self.assertTrue(result.wholesale_only)
        self.assertEqual(result.intervals[0].eur_per_mwh, 11.99)
        self.assertEqual(result.intervals[0].cents_per_kwh, 1.199)
        self.assertEqual(result.summary.minimum.start, "2026-09-29T00:00:00+03:00")
        self.assertIsNone(result.current_interval)
        self.assertIsNone(result.current_hour)
        self.assertEqual(result.vat_rate_percent, 24)
        self.assertNotIn("VAT", result.excluded_costs)
        self.assertIn("network charges", result.excluded_costs)
        self.assertEqual(result.intervals[0].excluding_vat.eur_per_mwh, 11.99)
        self.assertEqual(result.intervals[0].including_vat.eur_per_mwh, 14.8676)
        self.assertEqual(result.intervals[0].including_vat.cents_per_kwh, 1.48676)
        self.assertEqual(result.summary.average_eur_per_mwh, 19.9165625)
        self.assertEqual(
            result.summary.average_excluding_vat.eur_per_mwh,
            result.summary.average_eur_per_mwh,
        )
        self.assertEqual(
            result.summary.average_including_vat.eur_per_mwh, 24.6965375
        )
        self.assertEqual(result.summary.minimum.including_vat.eur_per_mwh, 14.8676)
        self.assertEqual(result.summary.maximum.including_vat.eur_per_mwh, 24.8)

    def test_identifies_current_interval(self):
        result = mcp_server.build_price_result(
            complete_report(date(2026, 9, 29)),
            current_time=datetime(
                2026,
                9,
                29,
                8,
                7,
                tzinfo=nordpool_ee.TALLINN,
            ),
        )

        self.assertIsNotNone(result.current_interval)
        self.assertEqual(
            result.current_interval.start,
            "2026-09-29T08:00:00+03:00",
        )
        self.assertEqual(result.current_hour.start, "2026-09-29T08:00:00+03:00")
        self.assertEqual(result.current_hour.end, "2026-09-29T09:00:00+03:00")

    def test_builds_specific_hour_result(self):
        result = mcp_server.build_hour_price_result(
            complete_report(date(2026, 9, 29)),
            hour=8,
        )

        self.assertEqual(result.delivery_date, "2026-09-29")
        self.assertEqual(result.hour, 8)
        self.assertEqual(result.interval_count, 4)
        self.assertEqual(result.intervals[0].start, "2026-09-29T08:00:00+03:00")
        self.assertEqual(result.intervals[-1].start, "2026-09-29T08:45:00+03:00")
        self.assertEqual(result.summary.average_eur_per_mwh, 20.0)
        self.assertEqual(result.summary.average_including_vat.eur_per_mwh, 24.8)
        self.assertEqual(len(result.hourly_averages), 1)

    def test_hour_average_matches_1_30_cents_with_vat(self):
        result = mcp_server.build_hour_price_result(evening_report(), hour=23)

        self.assertEqual(result.summary.average_eur_per_mwh, 10.47)
        self.assertEqual(result.summary.average_cents_per_kwh, 1.047)
        self.assertEqual(result.summary.average_excluding_vat.cents_per_kwh, 1.047)
        self.assertEqual(result.summary.average_including_vat.cents_per_kwh, 1.29828)
        self.assertEqual(result.summary.average_including_vat.eur_per_mwh, 12.9828)
        self.assertEqual(
            result.hourly_averages[0].average_including_vat,
            result.summary.average_including_vat,
        )
        self.assertEqual(result.hourly_averages[0].start, "2026-09-28T23:00:00+03:00")
        self.assertEqual(result.hourly_averages[0].end, "2026-09-29T00:00:00+03:00")

    def test_current_quarter_hour_is_distinct_from_current_hour_average(self):
        result = mcp_server.build_price_result(
            evening_report(),
            current_time=datetime(2026, 9, 28, 23, 9, tzinfo=nordpool_ee.TALLINN),
        )

        self.assertEqual(result.current_interval.excluding_vat.cents_per_kwh, 2.001)
        self.assertEqual(result.current_interval.including_vat.cents_per_kwh, 2.48124)
        self.assertEqual(result.current_hour.average_excluding_vat.cents_per_kwh, 1.047)
        self.assertEqual(result.current_hour.average_including_vat.cents_per_kwh, 1.29828)
        self.assertEqual(result.current_hour, result.hourly_averages[-1])

    def test_negative_and_zero_prices_are_not_clamped_or_rounded(self):
        result = mcp_server.build_hour_price_result(
            complete_report(
                date(2026, 9, 28),
                price_factory=lambda index: [0, -50.05, 10, 20][index % 4],
            ),
            hour=23,
        )

        self.assertEqual(result.intervals[0].including_vat.eur_per_mwh, 0)
        self.assertEqual(result.intervals[1].including_vat.eur_per_mwh, -62.062)
        self.assertEqual(result.intervals[1].including_vat.cents_per_kwh, -6.2062)
        self.assertEqual(result.summary.average_excluding_vat.cents_per_kwh, -0.50125)
        self.assertEqual(result.summary.average_including_vat.cents_per_kwh, -0.62155)

    def test_day_hour_counts_follow_dst_and_preserve_all_intervals(self):
        for delivery_date, expected in [
            (date(2026, 3, 29), 23),
            (date(2026, 9, 29), 24),
            (date(2026, 10, 25), 25),
        ]:
            with self.subTest(delivery_date=delivery_date):
                result = mcp_server.build_price_result(complete_report(delivery_date))
                hours = result.hourly_averages
                self.assertEqual(len(hours), expected)
                self.assertTrue(all(hour.interval_count == 4 for hour in hours))
                self.assertEqual(sum(hour.interval_count for hour in hours), result.interval_count)
                self.assertEqual(len({hour.start for hour in hours}), expected)
                self.assertAlmostEqual(
                    sum(hour.average_including_vat.eur_per_mwh for hour in hours) / expected,
                    result.summary.average_including_vat.eur_per_mwh,
                )
                for hour in hours:
                    start = datetime.fromisoformat(hour.start).astimezone(nordpool_ee.UTC)
                    end = datetime.fromisoformat(hour.end).astimezone(nordpool_ee.UTC)
                    self.assertEqual((end - start).total_seconds(), 3600)

    def test_repeated_hours_have_separate_averages_and_correct_current_hour(self):
        report = complete_report(
            date(2026, 10, 25),
            price_factory=lambda index: 10 if index < 16 else 30,
        )
        result = mcp_server.build_price_result(
            report,
            current_time=datetime(
                2026, 10, 25, 3, 7, fold=1, tzinfo=nordpool_ee.TALLINN
            ),
        )
        repeated = [hour for hour in result.hourly_averages if hour.hour == 3]

        self.assertEqual(len(repeated), 2)
        self.assertEqual(repeated[0].start, "2026-10-25T03:00:00+03:00")
        self.assertEqual(repeated[1].start, "2026-10-25T03:00:00+02:00")
        self.assertEqual(repeated[0].average_including_vat.eur_per_mwh, 12.4)
        self.assertEqual(repeated[1].average_including_vat.eur_per_mwh, 37.2)
        self.assertEqual(result.current_hour, repeated[1])
        selected_hour = mcp_server.build_hour_price_result(report, 3)
        self.assertEqual(selected_hour.hourly_averages, repeated)
        self.assertEqual(selected_hour.summary.average_eur_per_mwh, 20)
        self.assertEqual(selected_hour.summary.average_including_vat.eur_per_mwh, 24.8)

    def test_current_hour_changes_at_boundary(self):
        report = complete_report(date(2026, 9, 29))
        for minute, expected_start in [(59, "08:00"), (60, "09:00")]:
            with self.subTest(minute=minute):
                now = datetime(
                    2026, 9, 29, 8 + minute // 60, minute % 60,
                    tzinfo=nordpool_ee.TALLINN,
                )
                result = mcp_server.build_price_result(report, current_time=now)
                self.assertEqual(result.current_hour.start[11:16], expected_start)

    def test_specific_hour_rejects_skipped_dst_hour(self):
        report = complete_report(date(2026, 3, 29))

        with self.assertRaisesRegex(mcp_server.ToolError, "daylight-saving"):
            mcp_server.build_hour_price_result(report, hour=3)

    def test_specific_hour_includes_repeated_dst_hour(self):
        result = mcp_server.build_hour_price_result(
            complete_report(date(2026, 10, 25)),
            hour=3,
        )

        self.assertEqual(result.interval_count, 8)
        self.assertEqual(
            {interval.start[-6:] for interval in result.intervals},
            {"+02:00", "+03:00"},
        )


class MCPProtocolTests(unittest.IsolatedAsyncioTestCase):
    def assert_vat_response(self, result):
        self.assertFalse(result.is_error)
        data = result.structured_content
        self.assertEqual(data["vat_rate_percent"], 24)
        self.assertNotIn("VAT", data["excluded_costs"])
        self.assertEqual(
            data["intervals"][0]["excluding_vat"]["eur_per_mwh"],
            data["intervals"][0]["eur_per_mwh"],
        )
        self.assertEqual(
            set(data["summary"]["average_including_vat"]),
            {"eur_per_mwh", "cents_per_kwh"},
        )
        self.assertTrue(data["hourly_averages"])
        self.assertIsInstance(result.content[0], TextContent)
        self.assertEqual(json.loads(result.content[0].text), data)

    async def test_lists_and_calls_price_tool_in_memory(self):
        delivery_date = date(2026, 9, 29)
        report = complete_report(delivery_date)

        with patch(
            "mcp_server.next_delivery_date", return_value=delivery_date
        ), patch("mcp_server.fetch_prices", return_value=report):
            async with Client(mcp_server.mcp, raise_exceptions=True) as client:
                listed = await client.list_tools()
                tool = next(
                    tool
                    for tool in listed.tools
                    if tool.name == "get_estonia_next_day_prices"
                )
                result = await client.call_tool(
                    "get_estonia_next_day_prices",
                    {},
                )

        self.assertEqual(
            {tool.name for tool in listed.tools},
            {
                "get_estonia_current_day_prices",
                "get_estonia_next_day_prices",
                "get_estonia_prices_for_hour",
            },
        )
        self.assertEqual(tool.input_schema["properties"], {})
        self.assertIn("delivery_date", tool.output_schema["properties"])
        self.assertIn("vat_rate_percent", tool.output_schema["properties"])
        self.assertIn("hourly_averages", tool.output_schema["properties"])
        self.assertIn("current_hour", tool.output_schema["properties"])
        self.assert_vat_response(result)
        self.assertIsNone(result.structured_content["current_hour"])
        self.assertFalse(result.is_error)
        self.assertEqual(result.structured_content["area"], "EE")
        self.assertEqual(result.structured_content["interval_count"], 96)
        self.assertEqual(
            len(result.structured_content["intervals"]),
            96,
        )

    async def test_calls_current_day_price_tool_in_memory(self):
        delivery_date = date(2026, 9, 29)
        report = complete_report(delivery_date)
        current_time = datetime(
            2026,
            9,
            29,
            8,
            7,
            tzinfo=nordpool_ee.TALLINN,
        )

        with patch(
            "mcp_server.tallinn_now", return_value=current_time
        ), patch("mcp_server.fetch_prices", return_value=report):
            async with Client(mcp_server.mcp, raise_exceptions=True) as client:
                result = await client.call_tool(
                    "get_estonia_current_day_prices",
                    {},
                )

        self.assertFalse(result.is_error)
        self.assertEqual(result.structured_content["delivery_date"], "2026-09-29")
        self.assertEqual(
            result.structured_content["current_interval"]["start"],
            "2026-09-29T08:00:00+03:00",
        )
        self.assert_vat_response(result)
        self.assertEqual(
            result.structured_content["current_hour"]["average_including_vat"]["cents_per_kwh"],
            2.48,
        )

    async def test_calls_specific_hour_price_tool_in_memory(self):
        delivery_date = date(2026, 9, 29)
        report = complete_report(delivery_date)

        with patch("mcp_server.fetch_prices", return_value=report):
            async with Client(mcp_server.mcp, raise_exceptions=True) as client:
                result = await client.call_tool(
                    "get_estonia_prices_for_hour",
                    {
                        "delivery_date": "2026-09-29",
                        "hour": 8,
                    },
                )

        self.assertFalse(result.is_error)
        self.assertEqual(result.structured_content["hour"], 8)
        self.assertEqual(result.structured_content["interval_count"], 4)
        self.assertEqual(
            result.structured_content["intervals"][0]["start"],
            "2026-09-29T08:00:00+03:00",
        )
        self.assert_vat_response(result)

    async def test_hour_tool_returns_exact_vat_example_in_both_response_channels(self):
        with patch("mcp_server.fetch_prices", return_value=evening_report()):
            async with Client(mcp_server.mcp, raise_exceptions=True) as client:
                result = await client.call_tool(
                    "get_estonia_prices_for_hour",
                    {"delivery_date": "2026-09-28", "hour": 23},
                )

        self.assert_vat_response(result)
        self.assertEqual(
            result.structured_content["summary"]["average_including_vat"]["cents_per_kwh"],
            1.29828,
        )

    async def test_specific_hour_rejects_invalid_date(self):
        async with Client(mcp_server.mcp, raise_exceptions=True) as client:
            result = await client.call_tool(
                "get_estonia_prices_for_hour",
                {
                    "delivery_date": "29-09-2026",
                    "hour": 8,
                },
            )

        self.assertTrue(result.is_error)
        self.assertIsInstance(result.content[0], TextContent)
        self.assertIn("YYYY-MM-DD", result.content[0].text)

    async def test_expected_price_failure_is_a_visible_tool_error(self):
        delivery_date = date(2026, 9, 29)

        with patch(
            "mcp_server.next_delivery_date", return_value=delivery_date
        ), patch(
            "mcp_server.fetch_prices",
            side_effect=nordpool_ee.PriceNetworkError("offline"),
        ):
            async with Client(mcp_server.mcp, raise_exceptions=True) as client:
                result = await client.call_tool(
                    "get_estonia_next_day_prices",
                    {},
                )

        self.assertTrue(result.is_error)
        self.assertIsNone(result.structured_content)
        self.assertIsInstance(result.content[0], TextContent)
        self.assertIn("offline", result.content[0].text)


if __name__ == "__main__":
    unittest.main()
