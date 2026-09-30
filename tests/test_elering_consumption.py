import io
import json
import os
import subprocess
import unittest
from datetime import datetime, timedelta
from decimal import Decimal
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs
from urllib.request import Request

import elering_consumption as api
from nordpool_ee import TALLINN, UTC


EIC = "38Z0000000000001"
NOW = datetime(2026, 11, 2, 12, tzinfo=TALLINN)
SECRET = "synthetic-secret-never-display"
TOKEN = "synthetic-access-token-never-display"


def points_payload(eics=(EIC,)):
    return [
        {"eic": eic, "commodityType": "ELECTRICITY",
         "periods": [{"from": "2020-01-01T00:00:00Z", "to": None}]}
        for eic in eics
    ]


def meter_payload(start=None):
    start = start or datetime(2026, 9, 28, 21, tzinfo=UTC)
    return [{
        "meteringPointEic": EIC,
        "accountingIntervals": [
            {"periodStart": start.isoformat(), "consumptionKwh": 0.125, "productionKwh": 0}
        ],
    }]


def token_payload(value=TOKEN, expires=300):
    return {"access_token": value, "token_type": "Bearer", "expires_in": expires}


class CredentialTests(unittest.TestCase):
    def test_complete_environment_pair_overrides_keychain_and_repr_is_redacted(self):
        with patch.dict(os.environ, {"ELERING_CLIENT_ID": "client", "ELERING_CLIENT_SECRET": SECRET}, clear=True), patch(
            "elering_consumption.subprocess.run"
        ) as keychain:
            credentials = api.load_credentials()
        self.assertEqual(credentials.client_secret, SECRET)
        self.assertNotIn(SECRET, repr(credentials))
        self.assertNotIn("client_id", repr(credentials))
        keychain.assert_not_called()

    def test_partial_empty_or_absent_credentials_fail_explicitly(self):
        for environment in (
            {"ELERING_CLIENT_ID": "client"}, {"ELERING_CLIENT_SECRET": SECRET},
            {"ELERING_CLIENT_ID": "", "ELERING_CLIENT_SECRET": SECRET},
            {"ELERING_CLIENT_ID": "client", "ELERING_CLIENT_SECRET": " "},
        ):
            with self.subTest(environment=list(environment)), patch.dict(os.environ, environment, clear=True), patch(
                "elering_consumption.subprocess.run"
            ) as keychain:
                with self.assertRaises(api.ConsumptionError):
                    api.load_credentials()
                keychain.assert_not_called()
        with patch.dict(os.environ, {}, clear=True), patch("elering_consumption.sys.platform", "linux"):
            with self.assertRaisesRegex(api.ConsumptionError, "ELERING_CLIENT"):
                api.load_credentials()

    def test_keychain_is_read_into_memory_without_secret_valued_arguments(self):
        completed = [
            subprocess.CompletedProcess([], 0, b"client\n"),
            subprocess.CompletedProcess([], 0, SECRET.encode() + b"\n"),
        ]
        with patch.dict(os.environ, {}, clear=True), patch("elering_consumption.sys.platform", "darwin"), patch(
            "elering_consumption.subprocess.run", side_effect=completed
        ) as run:
            result = api.load_credentials()
        self.assertEqual(result.client_secret, SECRET)
        for call in run.call_args_list:
            self.assertNotIn(SECRET, " ".join(call.args[0]))
            self.assertEqual(call.kwargs["stderr"], subprocess.DEVNULL)
            self.assertEqual(call.kwargs["timeout"], 10)
            self.assertEqual(call.args[0][1], "find-generic-password")

    def test_locked_missing_and_malformed_keychain_do_not_leak_output(self):
        cases = [
            subprocess.CompletedProcess([], 44, SECRET.encode()),
            subprocess.CompletedProcess([], 1, SECRET.encode()),
            subprocess.CompletedProcess([], 0, b"\xff"),
            subprocess.CompletedProcess([], 0, b""),
            subprocess.TimeoutExpired(["security"], 10, output=SECRET.encode()),
            OSError(SECRET),
        ]
        for value in cases:
            with self.subTest(kind=type(value).__name__), patch.dict(os.environ, {}, clear=True), patch(
                "elering_consumption.sys.platform", "darwin"
            ), patch("elering_consumption.subprocess.run") as run:
                if isinstance(value, Exception):
                    run.side_effect = value
                else:
                    run.return_value = value
                with self.assertRaises(api.ConsumptionError) as error:
                    api.load_credentials()
                self.assertNotIn(SECRET, str(error.exception))


class ConsumptionValidationTests(unittest.TestCase):
    def test_strict_dates_inclusive_range_and_future_bounds(self):
        window = api.make_window("2026-09-01", "2026-09-30", NOW)
        self.assertEqual(window.start, datetime(2026, 8, 31, 21, tzinfo=UTC))
        self.assertEqual(window.end, datetime(2026, 9, 30, 21, tzinfo=UTC))
        for start, end in [
            ("20260901", None), ("01-09-2026", None), ("2026-02-30", None),
            ("2026-10-02", "2026-10-01"), ("2026-09-01", "2026-10-02"),
            ("2026-11-03", None), ("2026-11-01", "2026-11-03"),
        ]:
            with self.subTest(start=start, end=end):
                with self.assertRaises(api.ConsumptionError):
                    api.make_window(start, end, NOW)

    def test_dst_window_lengths(self):
        for date, hours in [("2026-03-29", 23), ("2026-09-29", 24), ("2026-10-25", 25)]:
            with self.subTest(date=date):
                window = api.make_window(date, None, NOW)
                self.assertEqual((window.end - window.start).total_seconds(), hours * 3600)

    def test_point_types_periods_and_duplicates(self):
        payload = points_payload()
        payload.append({"commodityType": "NATURAL_GAS"})
        result = api.normalize_points(payload, NOW)
        self.assertEqual(len(result.points), 1)
        self.assertEqual(result.points[0].eic, EIC)
        self.assertIsNone(result.points[0].periods[0].end)
        for mutated in ({}, [{"commodityType": "OTHER"}], points_payload((EIC, EIC))):
            with self.subTest(kind=type(mutated).__name__):
                with self.assertRaises(api.ConsumptionError):
                    api.normalize_points(mutated, NOW)
        payload = points_payload()
        payload[0]["periods"][0]["to"] = "2019-01-01T00:00:00Z"
        with self.assertRaises(api.ConsumptionError):
            api.normalize_points(payload, NOW)

    def test_energy_null_zero_decimals_sorting_and_exclusive_boundary(self):
        window = api.make_window("2026-09-29", None, NOW)
        payload = meter_payload(window.start)
        rows = payload[0]["accountingIntervals"]
        rows.append({"periodStart": (window.start + timedelta(hours=1)).isoformat(),
                     "consumptionKwh": None, "productionKwh": 0.025})
        rows.append({"periodStart": window.end.isoformat(), "consumptionKwh": 999, "productionKwh": 999})
        rows.reverse()
        parsed = api.normalize_readings(payload, EIC, window.start, window.end, "hourly")
        self.assertEqual(len(parsed), 2)
        self.assertEqual(parsed[0].consumption_kwh, Decimal("0.125"))
        self.assertEqual(parsed[0].export_kwh, 0)
        self.assertIsNone(parsed[1].consumption_kwh)
        self.assertEqual(parsed[1].export_kwh, Decimal("0.025"))

    def test_invalid_energy_alignment_and_duplicate_intervals_are_errors(self):
        window = api.make_window("2026-09-29", None, NOW)
        for field, value in [
            ("consumptionKwh", True), ("consumptionKwh", "0.1"),
            ("productionKwh", float("nan")), ("consumptionKwh", Decimal("Infinity")),
            ("consumptionKwh", Decimal("1e1000")),
            ("periodStart", "2026-09-28T21:00:00"),
            ("periodStart", "2026-09-28T21:15:00Z"),
        ]:
            with self.subTest(field=field):
                payload = meter_payload()
                payload[0]["accountingIntervals"][0][field] = value
                with self.assertRaises(api.ConsumptionError):
                    api.normalize_readings(payload, EIC, window.start, window.end, "hourly")
        payload = meter_payload()
        payload[0]["accountingIntervals"] *= 2
        with self.assertRaisesRegex(api.ConsumptionError, "duplicate"):
            api.normalize_readings(payload, EIC, window.start, window.end, "hourly")

    def test_per_meter_errors_wrong_eic_and_wrong_shapes_are_not_silent(self):
        window = api.make_window("2026-09-29", None, NOW)
        error_payload = meter_payload()
        error_payload[0]["error"] = {"message": SECRET}
        wrong_meter = meter_payload()
        wrong_meter[0]["meteringPointEic"] = "private-unexpected-id"
        for value in (error_payload, wrong_meter, {}, [None], meter_payload() * 2):
            with self.subTest(kind=type(value).__name__):
                with self.assertRaises(api.ConsumptionError) as error:
                    api.normalize_readings(value, EIC, window.start, window.end, "hourly")
                self.assertNotIn(SECRET, str(error.exception))
                self.assertNotIn("private-unexpected-id", str(error.exception))
        self.assertEqual(api.normalize_readings([], EIC, window.start, window.end, "hourly"), ())


class ConsumptionClientTests(unittest.TestCase):
    def setUp(self):
        self.client = api.EstfeedClient(lambda: api.Credentials("client", SECRET))
        patcher = patch("elering_consumption.time.sleep")
        self.sleep = patcher.start()
        self.addCleanup(patcher.stop)

    def test_token_cached_and_refreshed_before_expiry(self):
        responses = [
            api.HttpResult(200, token_payload(), None),
            api.HttpResult(200, token_payload("second-token"), None),
        ]
        with patch.object(self.client, "_request", side_effect=responses) as request, patch(
            "elering_consumption.time.monotonic", return_value=100
        ) as clock:
            self.assertEqual(self.client._token(), TOKEN)
            clock.return_value = 369
            self.assertEqual(self.client._token(), TOKEN)
            clock.return_value = 370
            self.assertEqual(self.client._token(), "second-token")
        self.assertEqual(request.call_count, 2)
        first = request.call_args_list[0].args[0]
        self.assertEqual(first.full_url, api.TOKEN_URL)
        self.assertEqual(first.method, "POST")
        self.assertEqual(parse_qs(first.data.decode())["grant_type"], ["client_credentials"])
        self.assertEqual(parse_qs(first.data.decode())["client_secret"], [SECRET])

    def test_invalid_tokens_are_not_cached_or_printed(self):
        for key, value in [
            ("access_token", ""), ("access_token", "token\nbad"),
            ("token_type", "Other"), ("expires_in", None), ("expires_in", 0),
            ("expires_in", True), ("expires_in", -1),
        ]:
            with self.subTest(key=key, value=value):
                payload = token_payload()
                payload[key] = value
                with patch.object(self.client, "_request", return_value=api.HttpResult(200, payload, None)):
                    with self.assertRaises(api.ConsumptionError) as error:
                        self.client._token()
                    self.assertNotIn(TOKEN, str(error.exception))
                    self.assertIsNone(self.client._access_token)

    def test_401_refreshes_once_and_403_is_not_retried(self):
        for statuses, message, expected in [
            ([401, 200], None, 4), ([401, 401], "denied", 4), ([403], "denied", 2),
        ]:
            with self.subTest(statuses=statuses):
                client = api.EstfeedClient(lambda: api.Credentials("client", SECRET))
                responses = [api.HttpResult(200, token_payload(), None)]
                for index, status in enumerate(statuses):
                    if index:
                        responses.append(api.HttpResult(200, token_payload("renewed"), None))
                    responses.append(api.HttpResult(status, points_payload() if status == 200 else None, None))
                with patch.object(client, "_request", side_effect=responses) as request:
                    if message:
                        with self.assertRaisesRegex(api.ConsumptionError, message):
                            client._get("/api/public/v1/metering-point-eics", {})
                    else:
                        self.assertEqual(client._get("/api/public/v1/metering-point-eics", {}), points_payload())
                    self.assertEqual(request.call_count, expected)

    def test_rate_limit_retry_is_bounded_and_honors_retry_after(self):
        request = Request(api.API_BASE)
        with patch.object(self.client, "_request", side_effect=[
            api.HttpResult(429, None, "7"), api.HttpResult(200, {}, None),
        ]) as network:
            self.assertEqual(self.client._request_with_rate_retry(request).status, 200)
            self.assertEqual(network.call_count, 2)
            self.sleep.assert_called_with(7)
        for value in ("31", "-1", "NaN", "not a date"):
            with self.subTest(value=value), patch.object(self.client, "_request", return_value=api.HttpResult(429, None, value)) as network:
                with self.assertRaisesRegex(api.ConsumptionError, "rate"):
                    self.client._request_with_rate_retry(request)
                self.assertEqual(network.call_count, 1)
        with patch.object(self.client, "_request", return_value=api.HttpResult(429, None, "0")) as network:
            with self.assertRaisesRegex(api.ConsumptionError, "persists"):
                self.client._request_with_rate_retry(request)
            self.assertEqual(network.call_count, 2)

    def test_http_timeout_json_errors_and_error_bodies_do_not_leak(self):
        for failure in (URLError(SECRET), TimeoutError(SECRET), OSError(SECRET)):
            with self.subTest(kind=type(failure).__name__), patch.object(
                self.client._opener, "open", side_effect=failure
            ):
                with self.assertRaises(api.ConsumptionError) as error:
                    self.client._request(Request(api.TOKEN_URL))
                self.assertNotIn(SECRET, str(error.exception))
        for body in (b"not JSON " + SECRET.encode(), b'{"n":NaN}', b"\xff"):
            response = MagicMock()
            response.__enter__.return_value.status = 200
            response.__enter__.return_value.headers = {}
            response.__enter__.return_value.read.return_value = body
            with self.subTest(body_kind=body[:1]), patch.object(self.client._opener, "open", return_value=response) as opener:
                with self.assertRaisesRegex(api.ConsumptionError, "invalid JSON"):
                    self.client._request(Request(api.TOKEN_URL))
                self.assertEqual(opener.call_args.kwargs["timeout"], 30)
        failure = HTTPError(api.TOKEN_URL, 403, SECRET, {}, io.BytesIO(SECRET.encode()))
        with patch.object(self.client._opener, "open", side_effect=failure):
            result = self.client._request(Request(api.TOKEN_URL))
        self.assertEqual(result.status, 403)
        self.assertIsNone(result.payload)

    def test_request_spacing_and_redirect_refusal(self):
        response = MagicMock()
        response.__enter__.return_value.status = 200
        response.__enter__.return_value.headers = {}
        response.__enter__.return_value.read.return_value = b"{}"
        with patch("elering_consumption.time.monotonic", return_value=100), patch.object(
            self.client._opener, "open", return_value=response
        ):
            self.client._request(Request(api.API_BASE))
            self.client._request(Request(api.API_BASE))
        self.sleep.assert_called_with(5)
        with self.assertRaisesRegex(api.ConsumptionError, "not forwarded"):
            api._NoRedirect().redirect_request(Request(api.TOKEN_URL), None, 302, "", {}, "https://example.com")

    def test_points_cache_and_unique_household_requirement(self):
        window = api.make_window("2026-09-29", None, NOW)
        with patch.object(self.client, "_get", return_value=points_payload()) as get, patch(
            "elering_consumption.time.monotonic", return_value=100
        ) as clock:
            first = self.client.list_metering_points(window)
            clock.return_value = 399
            self.assertIs(self.client.list_metering_points(window), first)
            clock.return_value = 400
            self.client.list_metering_points(window)
            self.assertEqual(get.call_count, 2)
        for points in ([], points_payload((EIC, "38Z0000000000002"))):
            client = api.EstfeedClient()
            with patch.object(client, "_get", return_value=points) as get:
                with self.assertRaises(api.ConsumptionError):
                    client.get_consumption(window, "hourly")
                self.assertEqual(get.call_count, 1)

    def test_31_local_days_split_at_dst_for_both_endpoints(self):
        window = api.make_window("2026-10-01", "2026-10-31", NOW)
        self.assertEqual((window.end - window.start).total_seconds() / 3600, 745)
        def source(path, parameters):
            if path.endswith("metering-point-eics"):
                return points_payload()
            self.assertEqual(parameters["meteringPointEics"], EIC)
            self.assertEqual(parameters["resolution"], "fifteen_minutes")
            return meter_payload(datetime.fromisoformat(parameters["startDateTime"].replace("Z", "+00:00")))
        with patch.object(self.client, "_get", side_effect=source) as get:
            report = self.client.get_consumption(window, "15_minutes")
        self.assertEqual(len(report.readings), 2)
        self.assertEqual(get.call_count, 4)
        for call in get.call_args_list:
            params = call.args[1]
            start = datetime.fromisoformat(params["startDateTime"].replace("Z", "+00:00"))
            end = datetime.fromisoformat(params["endDateTime"].replace("Z", "+00:00"))
            self.assertLessEqual(end - start, timedelta(days=31))
        calls = get.call_args_list
        self.assertEqual(calls[2].args[1]["endDateTime"], calls[3].args[1]["startDateTime"])


if __name__ == "__main__":
    unittest.main()
