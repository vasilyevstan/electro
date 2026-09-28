import json
import unittest
from dataclasses import asdict, replace
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from unittest.mock import patch

from mcp import Client
from mcp.types import TextContent

import mcp_server
import nordpool_ee
import wattcast_forecast as wattcast


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


def forecast_report(first_day=date(2026, 9, 29), known_hours=25):
    start, _ = nordpool_ee.market_bounds(first_day)
    _, end = nordpool_ee.market_bounds(first_day + timedelta(days=6))
    now = datetime.combine(first_day - timedelta(days=1), time(23), nordpool_ee.TALLINN)
    first_forecast_day = (start + known_hours * wattcast.HOUR).astimezone(nordpool_ee.TALLINN).date()
    hours = []
    for index in range(int((end - start) / wattcast.HOUR)):
        slot_start = start + index * wattcast.HOUR
        if index < known_hours:
            hours.append(wattcast.PublishedHour(slot_start, Decimal("10.47")))
        else:
            p50 = Decimal(index - 30)
            horizon = (slot_start.astimezone(nordpool_ee.TALLINN).date() - first_forecast_day).days + 1
            hours.append(wattcast.PredictedHour(slot_start, p50 - Decimal("10.01"), p50,
                                              p50 + Decimal("20.01"), horizon))
    return wattcast.ForecastReport(
        issued_at=now.astimezone(nordpool_ee.UTC) - timedelta(minutes=20),
        retrieved_at=now.astimezone(nordpool_ee.UTC),
        model_trained_at=now.astimezone(nordpool_ee.UTC) - timedelta(days=1),
        hours=tuple(hours),
        attribution="Wattcast; Elering (Nord Pool); Open-Meteo.com (CC BY 4.0)",
    )


def forecast_accuracy(report):
    return wattcast.AccuracyReport(
        retrieved_at=report.retrieved_at,
        model_trained_at=report.model_trained_at,
        live_days=14,
        nominal_coverage_percent=Decimal(80),
        band_method="cqr",
        horizons=tuple(
            wattcast.HorizonAccuracy(
                horizon_days=k,
                backtest=wattcast.BacktestScore(
                    Decimal("27.8"), Decimal("50.98"), Decimal("41.65"), 4320, date(2026, 4, 1)
                ),
                live=wattcast.LiveScore(
                    Decimal("33.51") if k == 1 else Decimal("44.5"),
                    Decimal("79.2") if k == 1 else Decimal("71.3"),
                    336 if k == 1 else 216,
                ),
            ) for k in range(1, 8)
        ),
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


class ForecastResultTests(unittest.TestCase):
    def test_complete_window_separates_published_prices_from_forecast_quantiles(self):
        report = forecast_report()
        result = mcp_server.build_forecast_result(
            report, report.retrieved_at, forecast_accuracy(report)
        )
        self.assertEqual(result.window_start, "2026-09-29T00:00:00+03:00")
        self.assertEqual(result.window_end, "2026-10-06T00:00:00+03:00")
        self.assertEqual(result.expected_hours, 168)
        self.assertEqual(result.available_hours, 168)
        self.assertEqual(result.published_hours, 25)
        self.assertEqual(result.forecast_hours, 143)
        self.assertEqual(result.missing_hours, [])
        self.assertTrue(result.complete)
        self.assertFalse(result.stale)
        self.assertEqual(result.model_mode, "raw")
        self.assertTrue(result.advisory_only)
        self.assertEqual(result.source, wattcast.FORECAST_URL)
        self.assertEqual(result.vat_rate_percent, 24)
        self.assertNotIn("VAT", result.excluded_costs)
        known = result.hourly_prices[24]
        self.assertEqual(known.basis, "published")
        self.assertEqual(known.start, "2026-09-30T00:00:00+03:00")
        self.assertIsNone(known.prediction)
        self.assertEqual(known.published_price.excluding_vat.cents_per_kwh, 1.047)
        self.assertEqual(known.published_price.including_vat.cents_per_kwh, 1.29828)
        predicted = result.hourly_prices[25]
        self.assertEqual(predicted.start, "2026-09-30T01:00:00+03:00")
        self.assertEqual(predicted.basis, "forecast")
        self.assertIsNone(predicted.published_price)
        self.assertEqual(predicted.prediction.p50.excluding_vat.eur_per_mwh, -5)
        self.assertEqual(predicted.prediction.p50.including_vat.cents_per_kwh, -0.62)
        self.assertEqual(predicted.prediction.p10.including_vat.cents_per_kwh, -1.86124)
        self.assertEqual(predicted.prediction.p90.including_vat.cents_per_kwh, 1.86124)
        self.assertEqual(result.hourly_prices[30].prediction.p50.including_vat.cents_per_kwh, 0)

    def test_daily_summaries_average_points_not_probability_bounds(self):
        report = forecast_report()
        result = mcp_server.build_forecast_result(report, report.retrieved_at)
        self.assertEqual(len(result.daily_summaries), 7)
        published, mixed = result.daily_summaries[:2]
        self.assertEqual(published.basis, "published")
        self.assertEqual(published.average_available_hourly_price.including_vat.cents_per_kwh, 1.29828)
        self.assertEqual(mixed.basis, "mixed")
        self.assertEqual(mixed.published_hours, 1)
        self.assertEqual(mixed.forecast_hours, 23)
        self.assertEqual(mixed.average_available_hourly_price.excluding_vat.eur_per_mwh, 6.18625)
        self.assertEqual(mixed.average_available_hourly_price.including_vat.eur_per_mwh, 7.67095)
        self.assertEqual(mixed.minimum_hour.start, "2026-09-30T01:00:00+03:00")
        self.assertEqual(mixed.maximum_hour.start, "2026-09-30T23:00:00+03:00")
        self.assertNotIn("p10", asdict(mixed))
        self.assertNotIn("p90", asdict(mixed))

    def test_dst_windows_preserve_each_real_hour(self):
        for first_day, expected, changed_day, day_count in [
            (date(2026, 3, 25), 167, "2026-03-29", 23),
            (date(2026, 9, 29), 168, "2026-09-29", 24),
            (date(2026, 10, 21), 169, "2026-10-25", 25),
        ]:
            with self.subTest(first_day=first_day):
                report = forecast_report(first_day, known_hours=0)
                result = mcp_server.build_forecast_result(report, report.retrieved_at)
                self.assertEqual(result.expected_hours, expected)
                self.assertEqual(result.available_hours, expected)
                self.assertEqual(len({hour.start for hour in result.hourly_prices}), expected)
                self.assertTrue(result.complete)
                day = next(day for day in result.daily_summaries if day.delivery_date == changed_day)
                self.assertEqual(day.available_hours, day_count)
                self.assertEqual(day.expected_hours, day_count)
                for hour in result.hourly_prices:
                    start = datetime.fromisoformat(hour.start).astimezone(nordpool_ee.UTC)
                    end = datetime.fromisoformat(hour.end).astimezone(nordpool_ee.UTC)
                    self.assertEqual(end - start, timedelta(hours=1))
                repeated = [hour.start for hour in result.hourly_prices
                            if hour.start.startswith("2026-10-25T03:")]
                if day_count == 25:
                    self.assertEqual(repeated, [
                        "2026-10-25T03:00:00+03:00", "2026-10-25T03:00:00+02:00"
                    ])

    def test_partial_results_report_individual_and_whole_day_gaps(self):
        report = forecast_report()
        kept = tuple(slot for index, slot in enumerate(report.hours)
                     if index not in {3, 167} and not 48 <= index < 72)
        result = mcp_server.build_forecast_result(
            replace(report, hours=kept), report.retrieved_at
        )
        self.assertFalse(result.complete)
        self.assertEqual(result.expected_hours, 168)
        self.assertEqual(result.available_hours, 142)
        self.assertEqual(len(result.missing_hours), 26)
        self.assertIn("2026-09-29T03:00:00+03:00", result.missing_hours)
        self.assertEqual(result.available_end, "2026-10-05T23:00:00+03:00")
        self.assertEqual(result.window_end, "2026-10-06T00:00:00+03:00")
        first, empty = result.daily_summaries[0], result.daily_summaries[2]
        self.assertFalse(first.complete)
        self.assertEqual(first.available_hours, 23)
        self.assertEqual(first.average_available_hourly_price.excluding_vat.eur_per_mwh, 10.47)
        self.assertEqual(empty.delivery_date, "2026-10-01")
        self.assertEqual(empty.basis, "unavailable")
        self.assertEqual(empty.available_hours, 0)
        self.assertIsNone(empty.average_available_hourly_price)
        self.assertIsNone(empty.minimum_hour)
        self.assertIsNone(empty.maximum_hour)
        self.assertIn("Partial forecast", " ".join(result.warnings))

    def test_staleness_uses_issuance_not_retrieval_and_has_explicit_warning(self):
        report = forecast_report()
        for seconds, stale in [(10800, False), (10801, True)]:
            with self.subTest(seconds=seconds):
                changed = replace(report, issued_at=report.retrieved_at - timedelta(seconds=seconds))
                result = mcp_server.build_forecast_result(changed, report.retrieved_at)
                self.assertEqual(result.age_seconds, seconds)
                self.assertEqual(result.stale, stale)
                self.assertEqual("Stale forecast" in " ".join(result.warnings), stale)
                self.assertEqual(result.retrieved_at, report.retrieved_at.astimezone(nordpool_ee.TALLINN).isoformat())

    def test_cached_data_recalculates_window_and_freshness_across_local_midnight(self):
        report = forecast_report()
        original = mcp_server.build_forecast_result(report, report.retrieved_at)
        later = mcp_server.build_forecast_result(
            report, report.retrieved_at + timedelta(hours=2)
        )
        self.assertEqual(later.window_start, "2026-09-30T00:00:00+03:00")
        self.assertEqual(later.window_end, "2026-10-07T00:00:00+03:00")
        self.assertEqual(later.issued_at, original.issued_at)
        self.assertEqual(later.retrieved_at, original.retrieved_at)
        self.assertEqual(later.age_seconds, original.age_seconds + 7200)
        self.assertFalse(later.complete)
        self.assertEqual(len(later.missing_hours), 24)

    def test_no_forecast_in_the_window_is_an_error_not_a_published_only_result(self):
        report = forecast_report()
        with self.assertRaisesRegex(wattcast.ForecastError, "No usable forecast hours"):
            mcp_server.build_forecast_result(replace(report, hours=report.hours[:25]), report.retrieved_at)
        with self.assertRaises(wattcast.ForecastError):
            mcp_server.build_forecast_result(report, report.retrieved_at + timedelta(days=14))

    def test_accuracy_distinguishes_live_backtest_and_nominal_coverage(self):
        report = forecast_report()
        result = mcp_server.build_forecast_result(report, report.retrieved_at, forecast_accuracy(report))
        accuracy = result.accuracy
        self.assertEqual(accuracy.status, "available")
        self.assertEqual(accuracy.nominal_interval_coverage_percent, 80)
        self.assertEqual(accuracy.live_window_days, 14)
        self.assertIn("not independently verified", accuracy.evidence)
        self.assertIn("not elapsed", accuracy.horizon_definition)
        first = accuracy.horizons[0]
        self.assertEqual(first.live.mae.excluding_vat.eur_per_mwh, 33.51)
        self.assertEqual(first.live.mae.including_vat.cents_per_kwh, 4.15524)
        self.assertEqual(first.live.interval_coverage_percent, 79.2)
        self.assertEqual(first.live.sample_count, 336)
        self.assertEqual(first.backtest.mae.excluding_vat.eur_per_mwh, 27.8)
        self.assertEqual(first.backtest.naive_week_mae.excluding_vat.eur_per_mwh, 50.98)
        self.assertEqual(accuracy.horizons[6].live.mae.including_vat.cents_per_kwh, 5.518)
        self.assertIn("below the nominal 80%", " ".join(result.warnings))

    def test_missing_accuracy_and_model_mismatch_are_not_silent(self):
        report = forecast_report()
        result = mcp_server.build_forecast_result(report, report.retrieved_at, accuracy_error="offline")
        self.assertTrue(result.complete)
        self.assertEqual(result.accuracy.status, "unavailable")
        self.assertEqual(result.accuracy.unavailable_reason, "offline")
        self.assertEqual(result.accuracy.horizons, [])
        self.assertIn("Accuracy unavailable: offline", result.warnings)
        accuracy = forecast_accuracy(report)
        accuracy = replace(accuracy, model_trained_at=report.model_trained_at - timedelta(days=7))
        result = mcp_server.build_forecast_result(report, report.retrieved_at, accuracy)
        self.assertIn("model timestamps differ", " ".join(result.warnings))

    def test_unrepresentable_accuracy_is_unavailable_without_hiding_forecasts(self):
        report = forecast_report()
        accuracy = forecast_accuracy(report)
        bad_live = replace(accuracy.horizons[0].live, mae=Decimal("1.7e308"))
        bad_horizon = replace(accuracy.horizons[0], live=bad_live)
        accuracy = replace(accuracy, horizons=(bad_horizon,) + accuracy.horizons[1:])
        result = mcp_server.build_forecast_result(report, report.retrieved_at, accuracy)
        self.assertTrue(result.complete)
        self.assertEqual(result.accuracy.status, "unavailable")
        self.assertIn("numeric range", result.accuracy.unavailable_reason)


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
                "get_estonia_price_forecast",
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

    async def test_forecast_tool_has_typed_output_and_matching_text(self):
        report = forecast_report()
        report = replace(report, issued_at=report.retrieved_at - timedelta(hours=4), hours=report.hours[:-1])
        with patch("mcp_server.tallinn_now", return_value=report.retrieved_at), patch(
            "mcp_server.fetch_forecast", return_value=report
        ), patch("mcp_server.fetch_accuracy", return_value=forecast_accuracy(report)):
            async with Client(mcp_server.mcp, raise_exceptions=True) as client:
                listed = await client.list_tools()
                tool = next(tool for tool in listed.tools if tool.name == "get_estonia_price_forecast")
                result = await client.call_tool("get_estonia_price_forecast", {})
        self.assertEqual(tool.input_schema["properties"], {})
        self.assertIn("hourly_prices", tool.output_schema["properties"])
        self.assertIn("accuracy", tool.output_schema["properties"])
        self.assertFalse(result.is_error)
        data = result.structured_content
        self.assertEqual(json.loads(result.content[0].text), data)
        self.assertEqual(data["model_mode"], "raw")
        self.assertFalse(data["complete"])
        self.assertTrue(data["stale"])
        self.assertEqual(data["available_hours"], 167)
        self.assertEqual(data["hourly_prices"][25]["prediction"]["p50"]["including_vat"]["cents_per_kwh"], -0.62)
        self.assertIn("Partial forecast", " ".join(data["warnings"]))
        self.assertIn("Stale forecast", " ".join(data["warnings"]))

    async def test_forecast_accuracy_outage_does_not_hide_usable_predictions(self):
        report = forecast_report()
        with patch("mcp_server.tallinn_now", return_value=report.retrieved_at), patch(
            "mcp_server.fetch_forecast", return_value=report
        ), patch("mcp_server.fetch_accuracy", side_effect=wattcast.ForecastError("metrics offline")):
            async with Client(mcp_server.mcp, raise_exceptions=True) as client:
                result = await client.call_tool("get_estonia_price_forecast", {})
        self.assertFalse(result.is_error)
        self.assertEqual(result.structured_content["accuracy"]["status"], "unavailable")
        self.assertIn("metrics offline", " ".join(result.structured_content["warnings"]))

    async def test_forecast_transport_failure_is_an_mcp_error(self):
        with patch("mcp_server.fetch_forecast", side_effect=wattcast.ForecastError("provider offline")), patch(
            "mcp_server.fetch_accuracy"
        ) as accuracy:
            async with Client(mcp_server.mcp, raise_exceptions=True) as client:
                result = await client.call_tool("get_estonia_price_forecast", {})
        self.assertTrue(result.is_error)
        self.assertIsNone(result.structured_content)
        self.assertIn("provider offline", result.content[0].text)
        accuracy.assert_not_called()


if __name__ == "__main__":
    unittest.main()
