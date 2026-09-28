import copy
import json
import unittest
from datetime import datetime, timedelta
from decimal import Decimal
from email.message import Message
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError

from nordpool_ee import UTC
import wattcast_forecast as wattcast


NOW = datetime(2026, 9, 28, 20, 45, tzinfo=UTC)


def forecast_payload():
    known_start = datetime(2026, 9, 28, 21, tzinfo=UTC)
    forecast_start = known_start + timedelta(hours=1)
    issued_at = NOW - timedelta(minutes=20)
    return {
        "zone": "EE",
        "resolution": "hour",
        "currency": "EUR",
        "unit": "EUR/MWh",
        "madeAt": int(issued_at.timestamp()),
        "madeAtIso": issued_at.isoformat(),
        "modelTrainedAt": "2026-09-28T01:33:08Z",
        "level": None,
        "adjustments": None,
        "attribution": "Wattcast; Elering (Nord Pool); Open-Meteo.com (CC BY 4.0)",
        "known": [
            {"ts": int(known_start.timestamp()), "startsAt": known_start.isoformat(), "eurMwh": 10.47}
        ],
        "forecast": [
            {"ts": int(forecast_start.timestamp()), "startsAt": forecast_start.isoformat(),
             "k": 1, "p10": -10.01, "p50": 0, "p90": 20.01}
        ],
    }


def accuracy_payload():
    return {
        "zone": "EE",
        "unit": "EUR/MWh",
        "modelTrainedAt": "2026-09-28T01:33:08Z",
        "liveDays": 14,
        "band": {"target_coverage": 80, "method": "cqr"},
        "backtest": {
            "1": {"cv_mae": 27.8, "naive_week_mae": 50.98,
                  "naive_lastknown_mae": 41.65, "n_cv": 4320, "cv_from": "2026-04-01"}
        },
        "live": {"1": {"mae": 33.51, "coverage80": 79.2, "n": 336}},
        "liveAdjusted": {"1": {"mae": 99, "n": 336}},
    }


class ForecastParsingTests(unittest.TestCase):
    def test_parses_raw_model_and_retains_negative_zero_and_decimal_prices(self):
        report = wattcast.normalize_forecast(forecast_payload(), NOW)
        self.assertEqual(report.issued_at, NOW - timedelta(minutes=20))
        self.assertEqual(report.retrieved_at, NOW)
        self.assertIsInstance(report.hours[0], wattcast.PublishedHour)
        self.assertEqual(report.hours[0].price_eur_mwh, Decimal("10.47"))
        prediction = report.hours[1]
        self.assertIsInstance(prediction, wattcast.PredictedHour)
        self.assertEqual(prediction.p10, Decimal("-10.01"))
        self.assertEqual(prediction.price_eur_mwh, 0)
        self.assertEqual(prediction.p90, Decimal("20.01"))

    def test_rejects_invalid_metadata_and_adjustments(self):
        for key, value in [
            ("zone", "FI"), ("currency", "USD"), ("unit", "ct/kWh"),
            ("resolution", "15min"), ("level", {}), ("adjustments", {}),
            ("madeAt", True), ("madeAt", 1.5), ("madeAt", 10**30),
            ("madeAtIso", "2026-09-28T20:25:00"),
            ("madeAtIso", "2026-09-28T20:26:00Z"),
            ("modelTrainedAt", "not-a-date"), ("attribution", ""),
            ("known", {}), ("forecast", []),
        ]:
            with self.subTest(key=key, value=value):
                payload = forecast_payload()
                payload[key] = value
                with self.assertRaises(wattcast.ForecastError):
                    wattcast.normalize_forecast(payload, NOW)

    def test_rejects_malformed_forecast_records(self):
        for key, value in [
            ("ts", True), ("ts", 1.5), ("ts", float("inf")),
            ("p50", "20"), ("p50", None), ("p50", True),
            ("p50", float("nan")), ("p50", Decimal("Infinity")),
            ("p50", 100), ("p10", 10), ("p90", -10),
            ("k", 0), ("k", 8), ("k", 1.5),
            ("startsAt", "2026-09-28T22:01:00Z"),
        ]:
            with self.subTest(key=key, value=value):
                payload = forecast_payload()
                payload["forecast"][0][key] = value
                with self.assertRaises(wattcast.ForecastError):
                    wattcast.normalize_forecast(payload, NOW)
        payload = forecast_payload()
        payload["forecast"] = [None]
        with self.assertRaises(wattcast.ForecastError):
            wattcast.normalize_forecast(payload, NOW)

    def test_rejects_duplicates_and_conflicting_classifications(self):
        for collection in ("known", "forecast"):
            with self.subTest(collection=collection):
                payload = forecast_payload()
                payload[collection].append(copy.deepcopy(payload[collection][0]))
                with self.assertRaisesRegex(wattcast.ForecastError, "duplicate"):
                    wattcast.normalize_forecast(payload, NOW)
        payload = forecast_payload()
        payload["forecast"][0].update(
            ts=payload["known"][0]["ts"], startsAt=payload["known"][0]["startsAt"]
        )
        with self.assertRaisesRegex(wattcast.ForecastError, "conflicting"):
            wattcast.normalize_forecast(payload, NOW)

    def test_rejects_subhourly_and_future_issuance_times(self):
        payload = forecast_payload()
        row = payload["forecast"][0]
        start = datetime.fromisoformat(row["startsAt"]) + timedelta(minutes=15)
        row.update(ts=int(start.timestamp()), startsAt=start.isoformat())
        with self.assertRaisesRegex(wattcast.ForecastError, "aligned"):
            wattcast.normalize_forecast(payload, NOW)
        payload = forecast_payload()
        future = NOW + timedelta(minutes=6)
        payload.update(madeAt=int(future.timestamp()), madeAtIso=future.isoformat())
        with self.assertRaisesRegex(wattcast.ForecastError, "future"):
            wattcast.normalize_forecast(payload, NOW)

    def test_gaps_are_retained_not_filled(self):
        payload = forecast_payload()
        later = copy.deepcopy(payload["forecast"][0])
        start = datetime.fromisoformat(later["startsAt"]) + timedelta(hours=2)
        later.update(ts=int(start.timestamp()), startsAt=start.isoformat())
        payload["forecast"].insert(0, later)
        report = wattcast.normalize_forecast(payload, NOW)
        self.assertEqual(len(report.hours), 3)
        self.assertEqual(report.hours[-1].start - report.hours[-2].start, timedelta(hours=2))

    def test_accuracy_uses_raw_live_branch_and_explicit_missing_horizons(self):
        report = wattcast.normalize_accuracy(accuracy_payload(), NOW)
        self.assertEqual(report.horizons[0].live.mae, Decimal("33.51"))
        self.assertEqual(report.horizons[0].live.coverage_percent, Decimal("79.2"))
        self.assertEqual(report.horizons[0].backtest.sample_count, 4320)
        self.assertEqual(report.nominal_coverage_percent, 80)
        self.assertEqual(report.live_days, 14)
        self.assertIsNone(report.horizons[1].live)
        self.assertIsNone(report.horizons[1].backtest)

    def test_rejects_invalid_accuracy(self):
        for key, value in [
            ("zone", "FI"), ("unit", "cents/kWh"), ("liveDays", 30),
            ("band", {"target_coverage": 0.8}), ("live", []),
        ]:
            with self.subTest(key=key):
                payload = accuracy_payload()
                payload[key] = value
                with self.assertRaises(wattcast.ForecastError):
                    wattcast.normalize_accuracy(payload, NOW)
        for key, value in [("mae", -1), ("coverage80", 101), ("n", 0), ("n", 0.5)]:
            with self.subTest(key=key):
                payload = accuracy_payload()
                payload["live"]["1"][key] = value
                with self.assertRaises(wattcast.ForecastError):
                    wattcast.normalize_accuracy(payload, NOW)


class ForecastTransportTests(unittest.TestCase):
    def setUp(self):
        patcher = patch.multiple(wattcast, _forecast_cache=None, _accuracy_cache=None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_json_uses_decimal_and_a_finite_timeout(self):
        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps(forecast_payload()).encode()
        with patch("wattcast_forecast.urlopen", return_value=response) as opener:
            payload, retrieved_at = wattcast._fetch_json(wattcast.FORECAST_URL)
        self.assertEqual(payload["known"][0]["eurMwh"], Decimal("10.47"))
        self.assertIsNotNone(retrieved_at.utcoffset())
        request = opener.call_args.args[0]
        self.assertEqual(request.full_url, wattcast.FORECAST_URL)
        self.assertIn("adjusted=false", request.full_url)
        self.assertIn("resolution=hour", request.full_url)
        self.assertEqual(opener.call_args.kwargs["timeout"], 10)

    def test_transport_and_json_errors_are_explicit(self):
        headers = Message()
        headers["Retry-After"] = "60"
        for error, message in [
            (HTTPError(wattcast.FORECAST_URL, 429, "limited", headers, None), "Retry-After: 60"),
            (HTTPError(wattcast.FORECAST_URL, 503, "offline", None, None), "503"),
            (URLError("offline"), "offline"),
            (TimeoutError("timed out"), "timed out"),
        ]:
            with self.subTest(error=error), patch("wattcast_forecast.urlopen", side_effect=error):
                with self.assertRaisesRegex(wattcast.ForecastError, message):
                    wattcast.fetch_forecast()
        for body in (b"bad", b"\xff", b'{"price":NaN}', b'{"price":Infinity}', b"[]"):
            response = MagicMock()
            response.__enter__.return_value.read.return_value = body
            with self.subTest(body=body), patch("wattcast_forecast.urlopen", return_value=response):
                with self.assertRaises(wattcast.ForecastError):
                    wattcast.fetch_forecast()

    def test_caches_each_valid_response_for_one_hour(self):
        for fetch, payload, url in (
            (wattcast.fetch_forecast, forecast_payload(), wattcast.FORECAST_URL),
            (wattcast.fetch_accuracy, accuracy_payload(), wattcast.ACCURACY_URL),
        ):
            with self.subTest(url=url), patch(
                "wattcast_forecast._fetch_json",
                side_effect=[(payload, NOW), (payload, NOW + timedelta(hours=1))],
            ) as request, patch("wattcast_forecast.monotonic", return_value=100) as clock:
                first = fetch()
                clock.return_value = 3699
                second = fetch()
                self.assertIs(first, second)
                self.assertEqual(second.retrieved_at, NOW)
                self.assertEqual(request.call_count, 1)
                clock.return_value = 3700
                third = fetch()
                self.assertEqual(request.call_count, 2)
                self.assertEqual(third.retrieved_at, NOW + timedelta(hours=1))
                request.assert_called_with(url)

    def test_invalid_data_is_not_cached_and_expired_cache_is_not_a_fallback(self):
        bad = forecast_payload()
        bad["unit"] = "wrong"
        with patch(
            "wattcast_forecast._fetch_json",
            side_effect=[(bad, NOW), (forecast_payload(), NOW), wattcast.ForecastError("offline")],
        ) as request, patch("wattcast_forecast.monotonic", return_value=0) as clock:
            with self.assertRaises(wattcast.ForecastError):
                wattcast.fetch_forecast()
            wattcast.fetch_forecast()
            clock.return_value = 3600
            with self.assertRaisesRegex(wattcast.ForecastError, "offline"):
                wattcast.fetch_forecast()
            self.assertEqual(request.call_count, 3)


if __name__ == "__main__":
    unittest.main()
