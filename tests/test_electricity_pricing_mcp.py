import io
import json
import os
import tempfile
import unittest
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from mcp import Client

import electricity_pricing as pricing
import electricity_pricing_mcp as server
import elering_consumption as consumption
import nordpool_ee as market
from pricing_test_support import example, prices_for, readings


NOW = datetime(2027, 1, 2, 12, tzinfo=market.TALLINN)
DAY = "2026-08-03"
EIC = "38Z0000000000001"


def household_report(rows, first=DAY, last=None):
    start_date, end_date, start, end = pricing.period_bounds(first, last)
    return consumption.ConsumptionReport(
        consumption.DateWindow(start_date, end_date, start, end), EIC, "15_minutes",
        tuple(consumption.Reading(row.start, row.consumption_kwh, row.export_kwh) for row in rows),
        NOW,
    )


class PricingProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def test_discovers_three_read_only_tools_and_quotes_exact_strings(self):
        profile = pricing.parse_profile(example())
        row = pricing.make_interval("2026-08-03T10:00:00+03:00", "2026-08-03T10:15:00+03:00", "2", "0.5")
        with patch.object(server, "_profile", return_value=profile), patch.object(
            server, "_market_prices", return_value=prices_for([row])
        ), patch.object(server._client, "get_consumption") as household:
            async with Client(server.mcp, raise_exceptions=True) as client:
                tools = await client.list_tools()
                response = await client.call_tool("quote_electricity_interval", {
                    "start": row.start.isoformat(), "end": row.end.isoformat(),
                    "consumption_kwh": "2", "export_kwh": "0.5",
                })
        self.assertEqual({tool.name for tool in tools.tools}, {
            "quote_electricity_interval", "calculate_electricity_period", "estimate_electricity_scenario",
        })
        for tool in tools.tools:
            self.assertTrue(tool.annotations.read_only_hint)
            self.assertFalse(tool.annotations.destructive_hint)
            for forbidden in ("profile_path", "client_secret", "client_id", "forecast"):
                self.assertNotIn(forbidden, tool.input_schema["properties"])
        self.assertFalse(response.is_error)
        result = response.structured_content
        self.assertEqual(json.loads(response.content[0].text), result)
        self.assertEqual(result["amounts"]["including_vat_eur"], "0.3519")
        self.assertEqual(result["billable_import_kwh"], "1.5")
        self.assertEqual(result["price_source"], market.API_URL)
        self.assertEqual(result["profile"]["sha256"], profile.sha256)
        household.assert_not_called()

    async def test_supplied_float_readings_and_automatic_readings_agree(self):
        profile = pricing.parse_profile(example())
        rows = readings(DAY, imported="2", exported="0.5")
        supplied = [
            {"start": row.start.isoformat(), "end": row.end.isoformat(),
             "consumption_kwh": float(row.consumption_kwh), "export_kwh": float(row.export_kwh)}
            for row in rows
        ]
        with patch.object(server, "_profile", return_value=profile), patch.object(
            server, "_market_prices", return_value=prices_for(rows)
        ), patch.object(server, "tallinn_now", return_value=NOW), patch.object(
            server._client, "get_consumption", return_value=household_report(rows)
        ) as household:
            async with Client(server.mcp, raise_exceptions=True) as client:
                manual = await client.call_tool("calculate_electricity_period", {
                    "start_date": DAY, "intervals": supplied, "include_daily": True,
                })
                household.assert_not_called()
                automatic = await client.call_tool("calculate_electricity_period", {
                    "start_date": DAY, "include_daily": True,
                })
        self.assertFalse(manual.is_error)
        self.assertFalse(automatic.is_error)
        self.assertEqual(manual.structured_content["amounts"], automatic.structured_content["amounts"])
        self.assertEqual(automatic.structured_content["billable_import_kwh"], "144")
        self.assertEqual(automatic.structured_content["billable_export_kwh"], "0")
        self.assertEqual(automatic.structured_content["input_source"], consumption.API_BASE + "/api/public/v1/metering-data")
        self.assertNotIn("metering_point_eic", automatic.structured_content)
        self.assertEqual(len(automatic.structured_content["daily_allocated_periods"]), 1)
        self.assertEqual(household.call_args.args[1], "15_minutes")

    async def test_invalid_and_incomplete_inputs_are_tool_errors_before_market_fetch(self):
        profile = pricing.parse_profile(example())
        cases = [
            ("quote_electricity_interval", {
                "start": "2026-08-03T10:00:00+03:00", "end": "2026-08-03T11:00:00+03:00",
                "consumption_kwh": "1", "export_kwh": "0",
            }),
            ("quote_electricity_interval", {
                "start": "2026-08-03T10:00:00+03:00", "end": "2026-08-03T10:15:00+03:00",
                "consumption_kwh": True, "export_kwh": "0",
            }),
            ("calculate_electricity_period", {"start_date": DAY, "intervals": []}),
            ("calculate_electricity_period", {"start_date": DAY, "mode": "monthly"}),
            ("calculate_electricity_period", {"start_date": DAY, "quantity_basis": "billable"}),
            ("calculate_electricity_period", {"start_date": "2027-01-01"}),
        ]
        with patch.object(server, "_profile", return_value=profile), patch.object(
            server, "_market_prices"
        ) as fetch, patch.object(server._client, "get_consumption") as household:
            async with Client(server.mcp) as client:
                for name, arguments in cases:
                    with self.subTest(name=name, arguments=arguments):
                        response = await client.call_tool(name, arguments)
                        self.assertTrue(response.is_error)
        fetch.assert_not_called()
        household.assert_not_called()

    async def test_unfinished_days_and_private_provider_failures_do_not_return_totals(self):
        profile = pricing.parse_profile(example())
        with patch.object(server, "_profile", return_value=profile), patch.object(
            server, "tallinn_now", return_value=datetime(2026, 8, 3, 12, tzinfo=market.TALLINN)
        ), patch.object(server._client, "get_consumption") as household:
            async with Client(server.mcp) as client:
                response = await client.call_tool("calculate_electricity_period", {"start_date": DAY})
                self.assertTrue(response.is_error)
            household.assert_not_called()
        with patch.object(server, "_profile", return_value=profile), patch.object(
            server, "tallinn_now", return_value=NOW
        ), patch.object(server._client, "get_consumption", side_effect=consumption.ConsumptionError("Multiple metering points are authorized.")):
            async with Client(server.mcp) as client:
                response = await client.call_tool("calculate_electricity_period", {"start_date": DAY})
        self.assertTrue(response.is_error)
        self.assertIn("Multiple metering", response.content[0].text)
        self.assertIsNone(response.structured_content)

    async def test_missing_or_invalid_profile_has_no_tariff_fallback(self):
        with patch.dict(os.environ, {}, clear=True):
            async with Client(server.mcp) as client:
                response = await client.call_tool("calculate_electricity_period", {"start_date": DAY})
        self.assertTrue(response.is_error)
        self.assertIn("ELECTRICITY_PRICING_PROFILE", response.content[0].text)

    async def test_profile_is_snapshotted_once_and_reloaded_on_next_call(self):
        row = readings(DAY)[0]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "profile.json"
            value = example()
            path.write_text(json.dumps(value))
            old = pricing.load_profile(path)

            def changed_profile(first, last):
                value["version"] = "example-2"
                path.write_text(json.dumps(value))
                return prices_for([row])

            with patch.dict(os.environ, {"ELECTRICITY_PRICING_PROFILE": str(path)}), patch.object(
                server, "_market_prices", side_effect=changed_profile
            ):
                async with Client(server.mcp) as client:
                    arguments = {
                        "start": row.start.isoformat(), "end": row.end.isoformat(),
                        "consumption_kwh": "1", "export_kwh": "0",
                    }
                    first = await client.call_tool("quote_electricity_interval", arguments)
                    second = await client.call_tool("quote_electricity_interval", arguments)
            self.assertEqual(first.structured_content["profile"]["sha256"], old.sha256)
            self.assertEqual(first.structured_content["profile"]["version"], "example-1")
            self.assertEqual(second.structured_content["profile"]["version"], "example-2")
            self.assertNotEqual(first.structured_content["profile"]["sha256"], second.structured_content["profile"]["sha256"])


class ScenarioProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def test_load_request_shares_one_profile_and_price_fetch_for_all_quarters(self):
        profile = pricing.parse_profile(example())
        start = "2026-08-03T10:07:30+03:00"
        load = pricing.make_load(profile, start, 60, power_kw=2)
        with patch.object(server, "_profile", return_value=profile) as snapshot, patch.object(
            server, "_market_prices", return_value=prices_for(load.intervals)
        ) as fetch, patch.object(server._client, "get_consumption") as household:
            async with Client(server.mcp, raise_exceptions=True) as client:
                response = await client.call_tool("estimate_electricity_scenario", {
                    "start": start, "duration_minutes": "60", "power_kw": "2",
                })
        result = response.structured_content
        self.assertEqual(json.loads(response.content[0].text), result)
        self.assertEqual(result["mode"], "load")
        self.assertEqual(result["start"], start)
        self.assertEqual(result["end"], "2026-08-03T11:07:30+03:00")
        self.assertEqual(result["accounting_start"], "2026-08-03T10:00:00+03:00")
        self.assertEqual(result["accounting_end"], "2026-08-03T11:15:00+03:00")
        self.assertEqual(result["amounts"]["including_vat_eur"], "0.4692")
        self.assertEqual(result["energy_kwh"], "2")
        self.assertEqual(len(result["energy_allocation"]), 5)
        self.assertIsNone(result["baseline"])
        self.assertEqual(result["fixed_fees"], "excluded")
        self.assertEqual(result["profile"]["sha256"], profile.sha256)
        self.assertEqual(result["scenario"]["price_source"], market.API_URL)
        self.assertEqual(result["prices_retrieved_at"], result["scenario"]["prices_retrieved_at"])
        snapshot.assert_called_once_with()
        fetch.assert_called_once_with(date(2026, 8, 3), date(2026, 8, 3))
        household.assert_not_called()

    async def test_comparison_snapshots_tariffs_once_and_reloads_on_next_request(self):
        before = {"start": "2026-08-03T10:00:00+03:00", "end": "2026-08-03T10:15:00+03:00",
                  "consumption_kwh": 0.0, "export_kwh": 2.0}
        after = {**before, "consumption_kwh": 1.0, "export_kwh": 0.0}
        arguments = {"mode": "comparison", "baseline_intervals": [before], "scenario_intervals": [after]}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "profile.json"
            value = example()
            path.write_text(json.dumps(value))
            old = pricing.load_profile(path)
            row = pricing.make_interval(**before)

            def change_profile(first, last):
                value["version"] = "example-2"
                value["components"][0]["rules"][0]["rate"] = "0.1025"
                path.write_text(json.dumps(value))
                return prices_for([row])

            with patch.dict(os.environ, {"ELECTRICITY_PRICING_PROFILE": str(path)}), patch.object(
                server, "_market_prices", side_effect=change_profile
            ) as fetch, patch.object(server._client, "get_consumption") as household:
                async with Client(server.mcp, raise_exceptions=True) as client:
                    first = (await client.call_tool("estimate_electricity_scenario", arguments)).structured_content
                    second = (await client.call_tool("estimate_electricity_scenario", arguments)).structured_content
        self.assertEqual(first["amounts"]["including_vat_eur"], "0.395")
        self.assertEqual(second["amounts"]["including_vat_eur"], "0.515")
        self.assertEqual(first["profile"]["sha256"], old.sha256)
        for result in (first, second):
            for side in ("baseline", "scenario"):
                self.assertEqual(result[side]["profile"], result["profile"])
                self.assertEqual(result[side]["price_source"], market.API_URL)
                self.assertEqual(result[side]["prices_retrieved_at"], result["prices_retrieved_at"])
            self.assertIsNone(result["energy_kwh"])
            self.assertEqual(result["energy_allocation"], [])
        self.assertEqual(fetch.call_count, 2)
        household.assert_not_called()

    async def test_raw_and_billable_comparison_modes_agree(self):
        row = {"start": "2026-08-03T10:00:00+03:00", "end": "2026-08-03T10:15:00+03:00",
               "consumption_kwh": "2", "export_kwh": "0.5"}
        after = {**row, "consumption_kwh": "3", "export_kwh": "0.25"}
        prices = prices_for([pricing.make_interval(**row)])
        with patch.object(server, "_profile", return_value=pricing.parse_profile(example())), patch.object(
            server, "_market_prices", return_value=prices
        ):
            async with Client(server.mcp, raise_exceptions=True) as client:
                raw = await client.call_tool("estimate_electricity_scenario", {
                    "mode": "comparison", "baseline_intervals": [row], "scenario_intervals": [after],
                })
                billable = await client.call_tool("estimate_electricity_scenario", {
                    "mode": "comparison", "quantity_basis": "billable",
                    "baseline_intervals": [{**row, "consumption_kwh": "1.5", "export_kwh": "0"}],
                    "scenario_intervals": [{**after, "consumption_kwh": "2.75", "export_kwh": "0"}],
                })
        self.assertEqual(raw.structured_content["amounts"], billable.structured_content["amounts"])
        self.assertEqual(raw.structured_content["amounts"]["including_vat_eur"], "0.29325")

    async def test_invalid_inputs_fail_before_fetching_prices_or_readings(self):
        row = {"start": "2026-08-03T10:00:00+03:00", "end": "2026-08-03T10:15:00+03:00",
               "consumption_kwh": "1", "export_kwh": "0"}
        load = {"start": row["start"], "duration_minutes": 60, "power_kw": "2"}
        comparison = {"mode": "comparison", "baseline_intervals": [row], "scenario_intervals": [row]}
        cases = [
            {}, {"mode": "unknown"}, {**load, "power_kw": None},
            {**load, "energy_kwh": "2"}, {**load, "power_kw": True},
            {**load, "duration_minutes": 0}, {**load, "quantity_basis": "raw"},
            {**load, "scenario_intervals": []},
            {**comparison, "start": row["start"]}, {**comparison, "duration_minutes": 15},
            {**comparison, "power_kw": 2}, {**comparison, "energy_kwh": 1},
            {**comparison, "baseline_intervals": None}, {**comparison, "scenario_intervals": None},
            {**comparison, "baseline_intervals": []}, {**comparison, "scenario_intervals": []},
            {**comparison, "baseline_intervals": [{**row, "export_kwh": None}]},
            {**comparison, "scenario_intervals": [{**row, "end": "2026-08-03T11:00:00+03:00"}]},
            {**comparison, "scenario_intervals": [{**row, "start": "2026-08-03T10:15:00+03:00",
                                                   "end": "2026-08-03T10:30:00+03:00"}]},
            {**load, "start": "2026-12-31T23:30:00+02:00"},
        ]
        with patch.object(server, "_profile", return_value=pricing.parse_profile(example())), patch.object(
            server, "_market_prices"
        ) as fetch, patch.object(server._client, "get_consumption") as household:
            async with Client(server.mcp) as client:
                for arguments in cases:
                    with self.subTest(arguments=arguments):
                        response = await client.call_tool("estimate_electricity_scenario", arguments)
                        self.assertTrue(response.is_error)
                        self.assertIsNone(response.structured_content)
        fetch.assert_not_called()
        household.assert_not_called()

    async def test_inclusive_price_dates_cover_only_the_requested_accounting_window(self):
        profile = pricing.parse_profile(example())
        for start, duration, last in (
            ("2026-06-30T23:00:00+03:00", 60, date(2026, 6, 30)),
            ("2026-06-30T23:50:00+03:00", 20, date(2026, 7, 1)),
        ):
            with self.subTest(start=start):
                load = pricing.make_load(profile, start, duration, energy_kwh=1)
                with patch.object(server, "_profile", return_value=profile), patch.object(
                    server, "_market_prices", return_value=prices_for(load.intervals)
                ) as fetch:
                    async with Client(server.mcp, raise_exceptions=True) as client:
                        result = await client.call_tool("estimate_electricity_scenario", {
                            "start": start, "duration_minutes": duration, "energy_kwh": 1,
                        })
                self.assertFalse(result.is_error)
                fetch.assert_called_once_with(date(2026, 6, 30), last)

    async def test_missing_prices_and_provider_errors_never_return_partial_totals(self):
        with patch.object(server, "_profile", return_value=pricing.parse_profile(example())):
            for missing in (None, market.PriceNetworkError("Published prices unavailable")):
                with self.subTest(error=missing), patch.object(
                    server, "_market_prices", side_effect=missing, return_value={}
                ):
                    async with Client(server.mcp) as client:
                        result = await client.call_tool("estimate_electricity_scenario", {
                            "start": "2026-08-03T10:00:00+03:00", "duration_minutes": 60, "power_kw": 2,
                        })
                    self.assertTrue(result.is_error)
                    self.assertIsNone(result.structured_content)


class MarketRangeTests(unittest.TestCase):
    def test_one_request_preserves_strict_day_validation_and_dst(self):
        first, last = date(2026, 3, 28), date(2026, 3, 30)
        records = [
            {"timestamp": timestamp, "price": 10}
            for offset in range(3)
            for timestamp in market.expected_interval_timestamps(first + timedelta(days=offset))
        ]
        body = json.dumps({"success": True, "data": {"ee": records}}).encode()

        class Response(io.BytesIO):
            status = 200

        calls = []

        def opener(request, timeout):
            calls.append(request.full_url)
            return Response(body)

        reports = market.fetch_prices_range(first, last, opener=opener)
        self.assertEqual(len(calls), 1)
        self.assertEqual([len(report.intervals) for report in reports], [96, 92, 96])
        query = parse_qs(urlsplit(calls[0]).query)
        self.assertEqual(query["start"], ["2026-03-27T22:00:00.000Z"])
        self.assertEqual(query["end"], ["2026-03-30T20:59:59.999Z"])
        broken = json.dumps({"success": True, "data": {"ee": records[1:]}}).encode()
        with self.assertRaises(market.PriceDataError):
            market.fetch_prices_range(first, last, opener=lambda *args, **kwargs: Response(broken))

    def test_range_bounds_and_network_errors_remain_explicit(self):
        for first, last in ((date(2026, 1, 1), date(2026, 2, 1)), (date(2026, 2, 1), date(2026, 1, 1))):
            with self.assertRaises(market.PriceDataError):
                market.fetch_prices_range(first, last)
        with patch.object(market, "_fetch_payload_url", side_effect=market.PriceNetworkError("Unavailable")):
            with self.assertRaises(market.PriceNetworkError):
                market.fetch_prices_range(date(2026, 1, 1), date(2026, 1, 1))

    def test_year_is_fetched_in_bounded_calendar_batches(self):
        calls = []

        def fetch(first, last):
            self.assertLessEqual((last - first).days, 30)
            calls.append((first, last))
            reports = []
            current = first
            while current <= last:
                start, end = market.market_bounds(current)
                intervals = tuple(
                    market.PriceInterval(datetime.fromtimestamp(timestamp, market.UTC), Decimal(10))
                    for timestamp in market.expected_interval_timestamps(current)
                )
                reports.append(market.PriceReport(current, start, end, intervals))
                current += timedelta(days=1)
            return tuple(reports)

        with patch.object(server, "fetch_prices_range", side_effect=fetch):
            result = server._market_prices(date(2026, 1, 1), date(2026, 12, 31))
        self.assertEqual(len(result), 35040)
        self.assertEqual(len(calls), 12)
        self.assertEqual(calls[0][0], date(2026, 1, 1))
        self.assertEqual(calls[-1][1], date(2026, 12, 31))
        for previous, following in zip(calls, calls[1:]):
            self.assertEqual(previous[1] + timedelta(days=1), following[0])

        first, last, start, end = pricing.period_bounds("2026-01-01", "2026-12-31")
        upstream = consumption.EstfeedClient._ranges(consumption.DateWindow(first, last, start, end))
        self.assertEqual(upstream[0][0], start)
        self.assertEqual(upstream[-1][1], end)
        self.assertTrue(all(stop - begin <= timedelta(days=31) for begin, stop in upstream))


if __name__ == "__main__":
    unittest.main()
