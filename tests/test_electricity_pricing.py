import copy
import json
import tempfile
import unittest
from dataclasses import replace
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

import electricity_pricing as pricing
from nordpool_ee import MARKET_INTERVAL, TALLINN
from pricing_test_support import component, example, prices_for, profile_with, readings, rule


D = Decimal


class ProfileTests(unittest.TestCase):
    def test_example_is_explicitly_synthetic_and_immutable(self):
        value = example()
        profile = pricing.parse_profile(value)
        value["components"][0]["rules"][0]["rate"] = "99"
        self.assertEqual(profile.components[0].rules[0].rate, D("0.0025"))
        self.assertEqual(profile.sources[0].basis, "synthetic")
        self.assertIn("SYNTHETIC", profile.notes[0])
        self.assertEqual(len(profile.sha256), 64)

    def test_file_snapshot_hash_changes_with_content(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "profile.json"
            value = example()
            path.write_text(json.dumps(value))
            first = pricing.load_profile(path)
            value["version"] = "example-2"
            path.write_text(json.dumps(value))
            second = pricing.load_profile(path)
            self.assertNotEqual(first.sha256, second.sha256)
            self.assertEqual(first.version, "example-1")

    def test_invalid_json_duplicate_fields_and_missing_files(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "profile.json"
            with self.assertRaises(pricing.PricingError):
                pricing.load_profile(path)
            for text in ("{", '{"schema_version":1,"schema_version":1}'):
                path.write_text(text)
                with self.assertRaises(pricing.PricingError):
                    pricing.load_profile(path)

    def test_profile_rejects_unknown_or_ambiguous_fields(self):
        mutations = [
            lambda value: value.update(schema_version=True),
            lambda value: value.update(schema_version=2),
            lambda value: value.update(currency="USD"),
            lambda value: value.update(timezone="UTC"),
            lambda value: value.update(until=None),
            lambda value: value.update(rounding="default"),
            lambda value: value.update(gross_rates=True),
            lambda value: value["components"][0]["rules"][0].update(rate=0.01),
            lambda value: value["components"][0]["rules"][0].update(source_ids=["missing"]),
            lambda value: value["components"][0]["rules"][0].update(vat_rate="1.01"),
            lambda value: value["components"][0]["rules"][0].update(tax_treatment="outside_vat"),
            lambda value: value["components"][0].update(kind="eval"),
            lambda value: value["components"][0].update(direction="unspecified"),
            lambda value: value["sources"][0].update(basis=[]),
            lambda value: value["sources"].append(copy.deepcopy(value["sources"][0])),
            lambda value: value["components"].append(copy.deepcopy(value["components"][0])),
            lambda value: value["netting"][0].update(mode="monthly_net"),
        ]
        for mutate in mutations:
            value = example()
            mutate(value)
            with self.subTest(mutation=mutations.index(mutate)):
                with self.assertRaises(pricing.PricingError):
                    pricing.parse_profile(value)

    def test_gaps_overlaps_and_missing_band_rates_are_rejected(self):
        for rules in (
            [rule(end="2026-04-01"), rule(start="2026-04-02")],
            [rule(), rule(start="2026-04-01")],
            [rule(start="2026-02-01")],
            [rule(end="2026-12-31")],
            [],
        ):
            value = example()
            value["components"][0]["rules"] = rules
            with self.subTest(rules=rules):
                with self.assertRaises(pricing.PricingError):
                    pricing.parse_profile(value)
        value = example()
        del value["components"][3]["rules"][0]["band_rates"]["rest_peak"]
        with self.assertRaises(pricing.PricingError):
            pricing.parse_profile(value)

    def test_numbers_cannot_be_nonfinite_boolean_or_unbounded(self):
        for value in (True, float("inf"), "NaN", "1e99999", "1e-99", "not a number", {}, None):
            with self.subTest(value=value):
                with self.assertRaises(pricing.PricingError):
                    pricing.decimal_value(value, "quantity")
        with self.assertRaises(pricing.PricingError):
            pricing.decimal_value("-1", "quantity", True)
        self.assertEqual(pricing.decimal_value(0.125, "quantity"), D("0.125"))


class QuoteTests(unittest.TestCase):
    def setUp(self):
        self.profile = pricing.parse_profile(example())

    def test_raw_netting_and_already_billable_are_equivalent(self):
        row = pricing.make_interval("2026-08-03T10:00:00+03:00", "2026-08-03T10:15:00+03:00", "2", "0.5")
        raw = pricing.quote_interval(self.profile, row, prices_for([row]))
        billed = replace(row, consumption_kwh=D("1.5"), export_kwh=D(0))
        billable = pricing.quote_interval(self.profile, billed, prices_for([row]), "billable")
        self.assertEqual(raw.billable_import_kwh, "1.5")
        self.assertEqual(raw.raw_import_kwh, "2")
        self.assertEqual(raw.raw_export_kwh, "0.5")
        self.assertEqual(raw.billable_export_kwh, "0")
        self.assertIsNone(billable.raw_import_kwh)
        self.assertEqual(raw.amounts, billable.amounts)
        self.assertEqual(raw.amounts.including_vat_eur, "0.3519")
        self.assertEqual(raw.fixed_fees, "excluded")
        self.assertNotIn("supplier_monthly", {line.component_id for line in raw.lines})

    def test_raw_flows_before_the_configured_change_are_not_netted(self):
        row = readings("2026-06-01", imported="2", exported="0.5")[0]
        result = pricing.quote_interval(self.profile, row, prices_for([row]))
        self.assertEqual(result.billable_import_kwh, "2")
        self.assertEqual(result.billable_export_kwh, "0.5")

    def test_single_direction_needs_explicit_billable_basis_after_netting(self):
        row = readings("2026-08-01")[0]
        row = replace(row, export_kwh=None)
        with self.assertRaisesRegex(pricing.PricingError, "both raw"):
            pricing.quote_interval(self.profile, row, prices_for([row]))
        result = pricing.quote_interval(self.profile, row, prices_for([row]), "billable")
        self.assertIsNone(result.billable_export_kwh)
        self.assertTrue(any("unspecified" in warning for warning in result.warnings))
        with self.assertRaises(pricing.PricingError):
            pricing.quote_interval(self.profile, replace(row, export_kwh=D(1)), prices_for([row]), "billable")

    def test_negative_export_proceeds_are_a_cost_and_not_taxed_as_a_sale(self):
        row = readings("2026-08-01", imported="0", exported="1")[0]
        result = pricing.quote_interval(self.profile, row, prices_for([row], "-10"))
        lines = {line.line_id: line for line in result.lines}
        self.assertEqual(lines["export_energy"].net_eur, "0.025")
        self.assertEqual(lines["export_energy"].tax_treatment, "outside_vat")
        self.assertEqual(result.amounts.outside_vat_eur, "0.025")
        self.assertEqual(result.amounts.vat_eur, "0.0008")
        self.assertEqual(result.amounts.including_vat_eur, "0.0298")

    def test_negative_import_prices_are_preserved(self):
        profile = profile_with(component(kind="spot", rate="0"))
        row = readings()[0]
        result = pricing.quote_interval(profile, row, prices_for([row], "-30"))
        self.assertEqual(result.amounts.excluding_vat_eur, "-0.03")
        self.assertEqual(result.amounts.including_vat_eur, "-0.036")

    def test_bad_duration_naive_offsets_and_missing_prices_are_errors(self):
        row = readings()[0]
        variants = [
            replace(row, end=row.start + timedelta(hours=1)),
            replace(row, start=row.start + timedelta(minutes=1), end=row.end + timedelta(minutes=1)),
            replace(row, start=row.start.replace(tzinfo=None)),
            replace(row, end=row.start),
        ]
        for candidate in variants:
            with self.subTest(candidate=candidate):
                with self.assertRaises(pricing.PricingError):
                    pricing.quote_interval(self.profile, candidate, prices_for([row]))
        with self.assertRaisesRegex(pricing.PricingError, "missing"):
            pricing.quote_interval(self.profile, row, {})

    def test_profile_does_not_extrapolate_rates(self):
        row = readings("2027-01-01")[0]
        with self.assertRaisesRegex(pricing.PricingError, "coverage"):
            pricing.quote_interval(self.profile, row, prices_for([row]))


class CalendarTests(unittest.TestCase):
    def test_statutory_holidays_and_not_easter_monday(self):
        holidays = pricing.estonian_holidays(2026)
        self.assertIn(date(2026, 4, 3), holidays)
        self.assertIn(date(2026, 4, 5), holidays)
        self.assertIn(date(2026, 5, 24), holidays)
        self.assertNotIn(date(2026, 4, 6), holidays)
        self.assertNotIn(date(2026, 6, 8), holidays)

    def test_day_night_and_winter_peaks(self):
        cases = [
            ("2026-01-05T06:45:00", "night"),
            ("2026-01-05T07:00:00", "day"),
            ("2026-01-05T09:00:00", "weekday_peak"),
            ("2026-01-05T12:00:00", "day"),
            ("2026-01-05T16:00:00", "weekday_peak"),
            ("2026-01-05T20:00:00", "day"),
            ("2026-01-05T22:00:00", "night"),
            ("2026-01-03T17:00:00", "rest_peak"),
            ("2026-01-01T10:00:00", "night"),
            ("2026-01-01T17:00:00", "rest_peak"),
            ("2026-04-03T10:00:00", "night"),
            ("2026-04-06T10:00:00", "day"),
            ("2026-08-20T10:00:00", "night"),
            ("2026-08-03T10:00:00", "day"),
        ]
        for text, expected in cases:
            with self.subTest(timestamp=text):
                self.assertEqual(pricing.time_band(datetime.fromisoformat(text).replace(tzinfo=TALLINN), True), expected)

    def test_dst_keeps_real_elapsed_quarters_and_repeated_hours(self):
        profile = profile_with(component(rate="0.01"))
        for day, count in (("2026-03-29", 92), ("2026-10-25", 100)):
            with self.subTest(day=day):
                rows = readings(day)
                result = pricing.price_period(profile, rows, prices_for(rows), day)
                self.assertEqual(result.interval_count, count)
                self.assertEqual(D(result.billable_import_kwh), count)
                self.assertEqual(D(result.amounts.excluding_vat_eur), D(count) / 100)
        rows = readings("2026-10-25")
        repeated = [row.start.astimezone(TALLINN).isoformat() for row in rows if row.start.astimezone(TALLINN).hour == 3]
        self.assertEqual(len(repeated), 8)
        self.assertEqual(len(set(repeated)), 8)
        local_rows = [
            replace(row, start=row.start.astimezone(TALLINN), end=row.end.astimezone(TALLINN))
            for row in reversed(rows)
        ]
        local_result = pricing.price_period(profile, local_rows, prices_for(rows), "2026-10-25")
        self.assertEqual(local_result.interval_count, 100)
        self.assertEqual(local_result.billable_import_kwh, "100")


class PeriodTests(unittest.TestCase):
    def test_each_kwh_is_weighted_by_its_own_price_without_duration_scaling(self):
        profile = profile_with(component(kind="spot", rate="0"))
        rows = readings(imported="0")
        rows[0] = replace(rows[0], consumption_kwh=D(1))
        rows[1] = replace(rows[1], consumption_kwh=D(9))
        prices = prices_for(rows, "0")
        prices[rows[0].start], prices[rows[1].start] = D(100), D(10)
        result = pricing.price_period(profile, rows, prices, "2026-01-01")
        self.assertEqual(result.amounts.excluding_vat_eur, "0.19")
        self.assertEqual(result.billable_import_kwh, "10")

    def test_missing_duplicates_overlap_nulls_and_naive_rows_are_rejected(self):
        profile = pricing.parse_profile(example())
        rows = readings()
        variants = [
            rows[:-1],
            rows + [rows[0]],
            [rows[0], rows[0], *rows[2:]],
            [replace(rows[0], end=rows[0].end + MARKET_INTERVAL), *rows[1:]],
            [replace(rows[0], export_kwh=None), *rows[1:]],
            [replace(rows[0], consumption_kwh=None), *rows[1:]],
            [replace(rows[0], start=rows[0].start.replace(tzinfo=None)), *rows[1:]],
        ]
        for candidate in variants:
            with self.subTest(count=len(candidate), first=candidate[0]):
                with self.assertRaises(pricing.PricingError):
                    pricing.price_period(profile, candidate, prices_for(rows), "2026-01-01")
        reversed_result = pricing.price_period(profile, list(reversed(rows)), prices_for(rows), "2026-01-01")
        self.assertEqual(reversed_result.interval_count, 96)

    def test_monthly_billing_rejects_incomplete_calendar_months(self):
        rows = readings()
        with self.assertRaisesRegex(pricing.PricingError, "complete calendar months"):
            pricing.price_period(pricing.parse_profile(example()), rows, prices_for(rows), "2026-01-01", mode="monthly")

    def test_zero_use_month_still_has_monthly_fees_once(self):
        profile = pricing.parse_profile(example())
        rows = readings("2026-01-01", "2026-01-31", imported="0")
        result = pricing.price_period(profile, rows, prices_for(rows), "2026-01-01", "2026-01-31", mode="monthly")
        fees = {line.component_id: line for line in result.lines if line.quantity_kwh is None}
        self.assertEqual(set(fees), {"supplier_monthly", "network_monthly"})
        self.assertEqual(fees["supplier_monthly"].net_eur, "2.35")
        self.assertEqual(fees["network_monthly"].net_eur, "12.50")
        self.assertEqual(result.billable_import_kwh, "0")

    def test_lines_are_rounded_after_accumulating_energy(self):
        profile = profile_with(component(rate="0.09"))
        rows = readings("2026-01-01", "2026-01-31", imported="0")
        rows[:96] = [replace(row, consumption_kwh=D("0.001")) for row in rows[:96]]
        result = pricing.price_period(profile, rows, prices_for(rows), "2026-01-01", "2026-01-31", mode="monthly")
        self.assertEqual(result.lines[0].unrounded_net_eur, "0.00864")
        self.assertEqual(result.lines[0].net_eur, "0.01")
        self.assertEqual(result.amounts.including_vat_eur, "0.01")

    def test_section_vat_is_not_recomputed_globally(self):
        profile = profile_with(
            component("fee_a", "monthly", "0.03", "purchase"),
            component("fee_b", "monthly", "0.03", "network"),
        )
        rows = readings("2026-01-01", "2026-01-31", imported="0")
        result = pricing.price_period(profile, rows, prices_for(rows), "2026-01-01", "2026-01-31", mode="monthly")
        self.assertEqual(result.amounts.taxable_net_eur, "0.06")
        self.assertEqual(result.amounts.vat_eur, "0.02")
        self.assertNotEqual((D("0.06") * D("0.20")).quantize(D("0.01")), D(result.amounts.vat_eur))

    def test_fee_identity_survives_source_versions_without_tie_drift(self):
        fee = component("fee", "monthly", "1.005", vat="0")
        fee["rules"] = [rule("1.005", "0", end="2026-01-16"), rule("1.005", "0", start="2026-01-16")]
        profile = profile_with(fee)
        rows = readings("2026-01-01", "2026-01-31", imported="0")
        result = pricing.price_period(profile, rows, prices_for(rows), "2026-01-01", "2026-01-31", mode="monthly")
        self.assertEqual(len(result.lines), 1)
        self.assertEqual(result.lines[0].unrounded_net_eur, "1.005")
        self.assertEqual(result.lines[0].net_eur, "1.01")
        self.assertEqual(len(result.lines[0].rule_ids), 2)

    def test_unconfigured_partial_contract_fee_changes_are_not_guessed(self):
        fee = component("fee", "monthly", "1")
        fee["rules"] = [rule("1", end="2026-01-16"), rule("2", start="2026-01-16")]
        profile = profile_with(fee)
        rows = readings("2026-01-01", "2026-01-31", imported="0")
        with self.assertRaisesRegex(pricing.PricingError, "proration"):
            pricing.price_period(profile, rows, prices_for(rows), "2026-01-01", "2026-01-31", mode="monthly")
        report = pricing.price_period(profile, rows, prices_for(rows), "2026-01-01", "2026-01-31")
        self.assertEqual(report.fixed_fees, "calendar_day_allocated")

    def test_both_tie_modes_and_negative_ties_are_explicit(self):
        rows = readings("2026-01-01", "2026-01-31", imported="0")
        for rate in ("1.005", "-1.005"):
            for mode, expected in (("ROUND_HALF_UP", "1.01"), ("ROUND_HALF_EVEN", "1.00")):
                if rate.startswith("-"):
                    expected = "-" + expected
                with self.subTest(rate=rate, mode=mode):
                    profile = profile_with(component("fee", "monthly", rate, vat="0"), rounding=mode)
                    result = pricing.price_period(profile, rows, prices_for(rows), "2026-01-01", "2026-01-31", mode="monthly")
                    self.assertEqual(result.lines[0].net_eur, expected)

    def test_daily_allocations_are_not_additive_rounded_invoices(self):
        profile = profile_with(component("fee", "monthly", "1", vat="0"))
        rows = readings("2026-01-01", "2026-01-31", imported="0")
        result = pricing.price_period(profile, rows, prices_for(rows), "2026-01-01", "2026-01-31", mode="monthly", include_daily=True)
        displayed_days = sum(D(day.amounts.including_vat_eur).quantize(D("0.01"), rounding=ROUND_HALF_UP) for day in result.daily_allocated_periods)
        self.assertEqual(displayed_days, D("0.93"))
        self.assertEqual(result.amounts.including_vat_eur, "1.00")
        self.assertEqual(len(result.daily_allocated_periods), 31)
        self.assertTrue(any("Daily amounts" in warning for warning in result.warnings))

    def test_variable_vat_change_inside_month_has_separate_tax_groups(self):
        fee = component(rate="1")
        fee["rules"] = [rule("1", "0.20", end="2026-01-16"), rule("1", "0.21", start="2026-01-16")]
        profile = profile_with(fee)
        rows = readings("2026-01-01", "2026-01-31")
        result = pricing.price_period(profile, rows, prices_for(rows), "2026-01-01", "2026-01-31", mode="monthly")
        self.assertEqual(len(result.vat_groups), 2)
        self.assertEqual(result.amounts.vat_eur, "610.56")

    def test_new_unbilled_period_retains_invoice_derived_provenance(self):
        value = example()
        value["sources"][0]["basis"] = "invoice_derived"
        value["reference_invoice_months"] = ["2026-01"]
        profile = pricing.parse_profile(value)
        rows = readings("2026-09-02")
        result = pricing.price_period(profile, rows, prices_for(rows), "2026-09-02")
        self.assertTrue(result.profile.invoice_derived_rules_used)
        self.assertEqual(result.profile.reference_invoice_months, ["2026-01"])
        self.assertTrue(any("unbilled" in warning for warning in result.warnings))
        self.assertFalse(hasattr(result, "invoice_total"))

    def test_netting_and_tariff_changes_use_delivery_dates(self):
        rows = readings("2026-06-30", "2026-07-01", imported="2", exported="1")
        result = pricing.price_period(pricing.parse_profile(example()), rows, prices_for(rows), "2026-06-30", "2026-07-01")
        self.assertEqual(result.raw_import_kwh, "384")
        self.assertEqual(result.raw_export_kwh, "192")
        self.assertEqual(result.billable_import_kwh, "288")
        self.assertEqual(result.billable_export_kwh, "96")
        self.assertEqual(len(result.monthly_periods), 2)

    def test_annual_amounts_equal_finalized_months_not_annual_rerounding(self):
        profile = profile_with(
            component("fee_a", "monthly", "0.03", "purchase"),
            component("fee_b", "monthly", "0.03", "network"),
            component(rate="0.000013"),
        )
        rows = readings("2026-01-01", "2026-12-31")
        result = pricing.price_period(profile, rows, prices_for(rows), "2026-01-01", "2026-12-31", mode="monthly")
        for field in ("taxable_net_eur", "vat_eur", "including_vat_eur", "outside_vat_eur"):
            monthly_sum = sum(D(getattr(month.amounts, field)) for month in result.monthly_periods)
            self.assertEqual(D(getattr(result.amounts, field)), monthly_sum)
        self.assertEqual(len(result.monthly_periods), 12)
        self.assertEqual(result.interval_count, 35040)


if __name__ == "__main__":
    unittest.main()
