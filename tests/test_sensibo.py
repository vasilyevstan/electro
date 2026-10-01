import copy
import gzip
import io
import json
import os
import stat
import subprocess
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

import sensibo as api


NOW = datetime(2026, 9, 1, 12, tzinfo=api.UTC)
DEVICE = "example-device-1"
MAC = "02:11:22:33:44:50"
TEST_KEY = "synthetic-sensibo-test-key"


def pod(device_id=DEVICE, mac=MAC):
    common = {
        "temperatures": {
            "C": {"values": [18, 19, 20, 21, 22, 23, 24], "isNative": True},
            "F": {"values": [64, 66, 68, 70, 72, 73, 75], "isNative": False},
        },
        "fanLevels": ["low", "high", "auto"],
        "swing": ["stopped", "rangeFull"],
        "horizontalSwing": ["stopped", "rangeFull"],
    }
    dry = copy.deepcopy(common)
    del dry["fanLevels"]
    return {
        "id": device_id, "macAddress": mac, "productModel": "skyv2",
        "room": {"name": "Synthetic room"},
        "connectionStatus": {"isAlive": True, "lastSeen": {"time": NOW.isoformat()}},
        "homekitSupported": False,
        "measurements": {
            "time": {"time": (NOW - timedelta(seconds=30)).isoformat(), "secondsAgo": 30},
            "temperature": 20.5, "humidity": 45, "feelsLike": 20.1, "rssi": -60,
        },
        "acState": {
            "on": True, "mode": "heat", "targetTemperature": 21, "temperatureUnit": "C",
            "fanLevel": "auto", "swing": "stopped", "horizontalSwing": "stopped",
            "timestamp": {"time": NOW.isoformat()},
        },
        "remoteCapabilities": {"modes": {"heat": common, "cool": copy.deepcopy(common), "dry": dry}},
        "blockModeChange": False, "restrictedMode": False,
        "minimumCoolingTemperature": None, "maximumHeatingTemperature": None,
    }


class Response(io.BytesIO):
    def __init__(self, result=None, *, content=None, compressed=False):
        if content is None:
            content = json.dumps({"status": "success", "result": result}).encode()
        super().__init__(gzip.compress(content) if compressed else content)
        self.headers = {"Content-Encoding": "gzip"} if compressed else {}
        self.status = 200


class Opener:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def open(self, request, **kwargs):
        self.requests.append(request)
        if not self.responses:
            raise AssertionError("Unexpected request")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class ClientCase(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="electro-sensibo-test-")
        self.addCleanup(self.temporary.cleanup)
        self.registry = api.Registry(Path(self.temporary.name) / "private" / "devices.json")
        self.registry.save([api.KnownDevice(DEVICE, MAC, "skyv2", "Synthetic room", NOW.isoformat())])
        self.clock = Mock(return_value=NOW)
        self.sleep = Mock()

    def client(self, *responses):
        self.opener = Opener(responses)
        return api.SensiboClient(
            self.registry, credentials_provider=lambda: api.Credentials(TEST_KEY),
            opener=self.opener, now=self.clock, sleep=self.sleep,
        )


class CredentialTests(unittest.TestCase):
    def test_environment_precedes_keychain_and_repr_hides_key(self):
        with patch.dict(os.environ, {"SENSIBO_API_KEY": TEST_KEY}), patch("sensibo.subprocess.run") as run:
            credentials = api.load_credentials()
        self.assertEqual(credentials.api_key, TEST_KEY)
        self.assertNotIn(TEST_KEY, repr(credentials))
        run.assert_not_called()

    def test_invalid_explicit_key_never_falls_back(self):
        for value in ("", " ", "key\nextra", " key"):
            with self.subTest(value=value), patch.dict(os.environ, {"SENSIBO_API_KEY": value}), patch("sensibo.subprocess.run") as run:
                with self.assertRaises(api.SensiboError):
                    api.load_credentials()
                run.assert_not_called()

    def test_keychain_success_failure_and_unavailable_platform(self):
        with patch.dict(os.environ, {}, clear=True), patch("sensibo.sys.platform", "darwin"):
            with patch("sensibo.subprocess.run", return_value=subprocess.CompletedProcess([], 0, (TEST_KEY + "\n").encode())) as run:
                self.assertEqual(api.load_credentials().api_key, TEST_KEY)
                self.assertNotIn(TEST_KEY, str(run.call_args))
                self.assertEqual(run.call_args.kwargs["stderr"], subprocess.DEVNULL)
            for result in (subprocess.CompletedProcess([], 44, b""), subprocess.CompletedProcess([], 0, b"\xff")):
                with patch("sensibo.subprocess.run", return_value=result), self.assertRaises(api.SensiboError):
                    api.load_credentials()
            with patch("sensibo.subprocess.run", side_effect=subprocess.TimeoutExpired("security", 10)), self.assertRaises(api.SensiboError):
                api.load_credentials()
        with patch.dict(os.environ, {}, clear=True), patch("sensibo.sys.platform", "linux"), self.assertRaisesRegex(api.SensiboError, "environment"):
            api.load_credentials()


class RegistryTests(ClientCase):
    def test_private_atomic_inventory_survives_new_instance(self):
        self.assertEqual(api.Registry(self.registry.path).load()[0].mac, MAC)
        self.assertEqual(stat.S_IMODE(self.registry.path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.registry.path.parent.stat().st_mode), 0o700)
        self.assertEqual(list(self.registry.path.parent.glob("*.tmp")), [])

    def test_enrollment_count_identity_conflicts_and_unknown_models_fail_closed(self):
        before = self.registry.path.read_bytes()
        wrong = pod(mac="02:11:22:33:44:51")
        unknown = pod()
        unknown["productModel"] = "unknown-model"
        for devices, count in (([pod()], 4), ([wrong], 1), ([unknown], 1), ([pod(), pod("example-device-2")], 2)):
            with self.subTest(count=count, models=[item["productModel"] for item in devices]):
                with self.assertRaises(api.SensiboError):
                    self.client(Response(devices)).enroll_account(count)
                self.assertEqual(self.registry.path.read_bytes(), before)

    def test_enrollment_preserves_missing_identities(self):
        new = pod("example-device-2", "02:11:22:33:44:51")
        records = self.client(Response([new])).enroll_account(1)
        self.assertEqual({item.device_id for item in records}, {DEVICE, "example-device-2"})
        self.assertEqual(len(self.registry.load()), 2)

    def test_insecure_corrupt_and_symlink_files_are_rejected(self):
        original = self.registry.path.read_bytes()
        self.registry.path.chmod(0o644)
        with self.assertRaises(api.SensiboError):
            self.registry.load()
        self.registry.path.chmod(0o600)
        self.registry.path.write_text("not JSON", encoding="utf-8")
        with self.assertRaises(api.SensiboError):
            self.registry.load()
        self.registry.path.write_bytes(original)
        link = self.registry.path.parent / "link.json"
        link.symlink_to(self.registry.path)
        with self.assertRaises(api.SensiboError):
            api.Registry(link).load()
        with self.assertRaises(api.SensiboError):
            api.Registry(link).save(self.registry.load())

    def test_mac_normalization_and_invalid_addresses(self):
        for value in ("021122334450", "02-11-22-33-44-50", MAC):
            self.assertEqual(api.normalize_mac(value), MAC)
        for value in (None, "", "00:00:00:00:00:00", "01:11:22:33:44:50", "02:11-22:33:44:50"):
            with self.subTest(value=value), self.assertRaises(api.SensiboError):
                api.normalize_mac(value)


class ReadTests(ClientCase):
    def test_snapshot_units_zero_null_and_mode_capabilities(self):
        device = pod()
        device["measurements"].update(temperature=0, humidity=None, feelsLike=None)
        device["acState"].update(targetTemperature=70, temperatureUnit="F")
        state = self.client(Response([device], compressed=True)).get_device(DEVICE)
        measurements = {item.metric: item for item in state.measurements}
        self.assertEqual(measurements["temperature"].value, 0)
        self.assertEqual(measurements["temperature"].unit, "C")
        self.assertIsNone(measurements["humidity"].value)
        self.assertEqual(measurements["humidity"].status, "unavailable")
        self.assertEqual(measurements["feelsLike"].kind, "derived")
        self.assertEqual(measurements["temperature"].age_seconds, 30)
        self.assertFalse(measurements["temperature"].stale)
        self.assertEqual(state.ac_state.temperature_unit, "F")
        self.assertIsNone(state.device.capabilities["dry"].fan_levels)
        self.assertFalse(state.device.homekit_supported)

    def test_cache_retains_retrieval_time_and_recalculates_age(self):
        client = self.client(Response([pod()]), Response([pod()]))
        first = client.get_device(DEVICE)
        self.clock.return_value = NOW + timedelta(seconds=59)
        cached = client.get_device(DEVICE)
        self.assertEqual(cached.retrieved_at, first.retrieved_at)
        self.assertEqual(cached.cache_age_seconds, 59)
        self.assertEqual(cached.measurements[0].age_seconds, 89)
        self.assertEqual(len(self.opener.requests), 1)
        self.clock.return_value = NOW + timedelta(seconds=60)
        fresh = client.get_device(DEVICE)
        self.assertNotEqual(fresh.retrieved_at, first.retrieved_at)
        self.assertEqual(len(self.opener.requests), 2)

    def test_staleness_threshold_unknown_time_and_offline_state(self):
        for age, stale in ((600, False), (601, True)):
            device = pod()
            device["measurements"]["time"] = {"time": (NOW - timedelta(seconds=age)).isoformat()}
            self.assertEqual(self.client(Response([device])).get_device(DEVICE).measurements[0].stale, stale)
        device = pod()
        device["connectionStatus"]["isAlive"] = False
        device["measurements"]["time"] = None
        result = self.client(Response([device])).get_device(DEVICE)
        self.assertIsNone(result.measurements[0].stale)
        self.assertIsNone(result.measurements[0].age_seconds)
        self.assertIn("offline", " ".join(result.warnings))

    def test_unknown_targets_and_identity_changes_cannot_read_or_control(self):
        client = self.client()
        for identity in ("Synthetic room", "../pods/other", "unregistered-device"):
            with self.subTest(identity=identity), self.assertRaises(api.SensiboError):
                client.get_device(identity)
        self.assertEqual(self.opener.requests, [])
        for change in ({"macAddress": "02:11:22:33:44:51"}, {"productModel": "different-model"}):
            device = pod()
            device.update(change)
            with self.assertRaisesRegex(api.SensiboError, "identity"):
                self.client(Response([device])).set_state(DEVICE, on=False)
            self.assertEqual([request.method for request in self.opener.requests], ["GET"])

    def test_inventory_retains_missing_devices_and_does_not_enroll_new_ones(self):
        extra = pod("example-device-2", "02:11:22:33:44:51")
        inventory = self.client(Response([extra])).list_devices()
        self.assertEqual(len(inventory.devices), 1)
        self.assertFalse(inventory.devices[0].seen_in_account)
        self.assertIsNone(inventory.devices[0].online)
        self.assertEqual(inventory.unenrolled_device_count, 1)
        self.assertTrue(inventory.warnings)
        self.assertEqual(len(self.registry.load()), 1)

    def test_ip_changes_are_irrelevant_to_identity(self):
        device = pod()
        device["ip"] = "192.0.2.10"
        moved = copy.deepcopy(device)
        moved["ip"] = "192.0.2.99"
        client = self.client(Response([device]), Response([moved]))
        first = client.get_device(DEVICE)
        second = client.get_device(DEVICE, refresh=True)
        self.assertEqual(first.device.mac, second.device.mac)
        self.assertEqual(first.device.device_id, second.device.device_id)

    def test_malformed_and_nonfinite_data_are_errors_not_missing_readings(self):
        for field, value in (("measurements", []), ("measurements", False), ("connectionStatus", [])):
            device = pod()
            device[field] = value
            with self.subTest(field=field, value=value), self.assertRaises(api.SensiboError):
                self.client(Response([device])).get_device(DEVICE)
        for value in (True, "21.5", float("inf")):
            device = pod()
            device["measurements"]["temperature"] = value
            with self.subTest(value=value), self.assertRaises(api.SensiboError):
                self.client(Response([device])).get_device(DEVICE)
        device = pod()
        device["measurements"]["time"] = "2026-09-01T12:00:00"
        with self.assertRaisesRegex(api.SensiboError, "UTC offset"):
            self.client(Response([device])).get_device(DEVICE)


class TransportTests(ClientCase):
    def test_authentication_private_body_and_credential_urls_are_redacted(self):
        for status in (301, 401, 403, 404, 429, 500):
            error = HTTPError(api.API_BASE + "?apiKey=" + TEST_KEY, status, "private upstream text", {}, io.BytesIO(b"private upstream body"))
            responses = [error, HTTPError(api.API_BASE, 429, "private", {}, None)] if status == 429 else [error]
            with self.subTest(status=status), self.assertRaises(api.SensiboError) as caught:
                self.client(*responses).list_devices()
            message = str(caught.exception)
            self.assertNotIn(TEST_KEY, message)
            self.assertNotIn("private upstream", message)
            self.assertIn(str(status), message)
        self.assertIsNone(api._NoRedirect().redirect_request(None, None, 302, "", {}, "https://example.invalid"))

    def test_retry_after_is_bounded_and_reads_only(self):
        error = HTTPError(api.API_BASE, 429, "limited", {"Retry-After": "2"}, None)
        client = self.client(error, Response([pod()]))
        client.list_devices()
        self.sleep.assert_called_once_with(2)
        self.assertEqual(len(self.opener.requests), 2)
        for value in ("31", "-1", "nan", "bad date"):
            with self.subTest(value=value), self.assertRaises(api.SensiboError):
                self.client(HTTPError(api.API_BASE, 429, "limited", {"Retry-After": value}, None)).list_devices()

    def test_invalid_json_compression_and_network_failures_are_explicit(self):
        for response in (
            Response(content=b"not-json"),
            Response(content=b'{"status":"error","error":"private upstream text"}'),
            URLError("private URL " + TEST_KEY),
            TimeoutError("private URL " + TEST_KEY),
        ):
            with self.subTest(kind=type(response).__name__), self.assertRaises(api.SensiboError) as caught:
                self.client(response).list_devices()
            self.assertNotIn(TEST_KEY, str(caught.exception))
            self.assertNotIn("private upstream text", str(caught.exception))
        broken = Response(content=b"not gzip")
        broken.headers["Content-Encoding"] = "gzip"
        with self.assertRaises(api.SensiboError):
            self.client(broken).list_devices()


class HistoryTests(ClientCase):
    def test_history_has_real_utc_times_nulls_zeros_gaps_and_coverage(self):
        first = NOW - timedelta(hours=3)
        second = NOW - timedelta(hours=1)
        history = {
            "temperature": [
                {"time": second.isoformat(), "value": None},
                {"time": first.isoformat(), "value": 0},
                {"time": (NOW - timedelta(days=2)).isoformat(), "value": 18},
            ],
            "humidity": [{"time": first.isoformat(), "value": 40}],
        }
        result = self.client(Response([pod()]), Response(history)).get_measurements(DEVICE)
        self.assertEqual(result.timezone, "UTC")
        self.assertEqual(result.series[0].unit, "C")
        self.assertEqual(len(result.series[0].samples), 2)
        self.assertEqual(result.series[0].samples[0].value, 0)
        self.assertIsNone(result.series[0].samples[1].value)
        self.assertEqual(result.series[0].available_start, first.isoformat())
        self.assertEqual(result.series[0].available_end, second.isoformat())
        self.assertIn("not guaranteed", " ".join(result.warnings))
        self.assertIn("null", " ".join(result.warnings))

    def test_history_cache_keeps_window_and_retrieval_time(self):
        history = {"temperature": [{"time": (NOW - timedelta(minutes=1)).isoformat(), "value": 21}]}
        client = self.client(Response([pod()]), Response(history))
        first = client.get_measurements(DEVICE, 7)
        self.clock.return_value = NOW + timedelta(seconds=30)
        cached = client.get_measurements(DEVICE, 7)
        self.assertEqual(first.window_end, cached.window_end)
        self.assertEqual(first.retrieved_at, cached.retrieved_at)
        self.assertEqual(cached.cache_age_seconds, 30)
        self.assertEqual(len(self.opener.requests), 2)

    def test_bad_windows_empty_history_and_conflicts_fail(self):
        for days in (0, 8, True, 1.5, "1"):
            with self.subTest(days=days), self.assertRaises(api.SensiboError):
                self.client().get_measurements(DEVICE, days)
            self.assertEqual(self.opener.requests, [])
        for history in (
            {}, {"temperature": []},
            {"temperature": [{"time": NOW.isoformat(), "value": None}]},
            {"temperature": [{"value": 21}]},
            {"temperature": [{"time": NOW.isoformat(), "value": 21}, {"time": NOW.isoformat(), "value": 22}]},
        ):
            with self.subTest(history=history), self.assertRaises(api.SensiboError):
                self.client(Response([pod()]), Response(history)).get_measurements(DEVICE)


class ControlTests(ClientCase):
    def test_explicit_update_preserves_unspecified_fields_and_reports_only_cloud_confirmation(self):
        before = pod()
        after = pod()
        after["acState"]["on"] = False
        client = self.client(Response([before]), Response({"status": "Success"}), Response([after]))
        result = client.set_state(DEVICE, on=False)
        self.assertEqual(json.loads(self.opener.requests[1].data), {"acState": {"on": False}})
        self.assertEqual([request.method for request in self.opener.requests], ["GET", "POST", "GET"])
        self.assertTrue(result.api_acknowledged)
        self.assertTrue(result.cloud_state_matches)
        self.assertFalse(result.physical_effect_verified)
        self.assertEqual(result.state.ac_state.target_temperature, 21)

    def test_control_bypasses_read_cache_and_uses_one_combined_command(self):
        initial = pod()
        updated = pod()
        updated["acState"].update(mode="cool", targetTemperature=22, fanLevel="low", swing="rangeFull")
        client = self.client(Response([initial]), Response([initial]), Response({}), Response([updated]))
        client.get_device(DEVICE)
        result = client.set_state(DEVICE, mode="cool", target_temperature=22, temperature_unit="C", fan_level="low", swing="rangeFull")
        self.assertTrue(result.cloud_state_matches)
        self.assertEqual([request.method for request in self.opener.requests], ["GET", "GET", "POST", "GET"])
        sent = json.loads(self.opener.requests[2].data)["acState"]
        self.assertNotIn("on", sent)
        self.assertNotIn("horizontalSwing", sent)

    def test_empty_invalid_types_units_and_numbers_send_no_request(self):
        for arguments in (
            {}, {"on": 1}, {"on": "false"}, {"target_temperature": True, "temperature_unit": "C"},
            {"target_temperature": float("nan"), "temperature_unit": "C"}, {"target_temperature": 22},
            {"temperature_unit": "F"}, {"target_temperature": 22, "temperature_unit": "K"},
            {"mode": ""}, {"fan_level": 2},
        ):
            client = self.client()
            with self.subTest(arguments=arguments), self.assertRaises(api.SensiboError):
                client.set_state(DEVICE, **arguments)
            self.assertEqual(self.opener.requests, [])

    def test_capabilities_policy_offline_and_missing_state_block_post(self):
        scenarios = []
        for update in ({"mode": "unsupported"}, {"target_temperature": 99, "temperature_unit": "C"}, {"fan_level": "unsupported"}, {"mode": "dry", "fan_level": "auto"}):
            scenarios.append((pod(), update))
        for field, value in (("restrictedMode", True), ("blockModeChange", True), ("maximumHeatingTemperature", 20)):
            device = pod()
            device[field] = value
            arguments = {"mode": "cool"} if field == "blockModeChange" else {"on": True}
            scenarios.append((device, arguments))
        device = pod()
        device["connectionStatus"]["isAlive"] = False
        scenarios.append((device, {"on": False}))
        device = pod()
        device["acState"] = None
        scenarios.append((device, {"on": False}))
        for device, arguments in scenarios:
            with self.subTest(arguments=arguments), self.assertRaises(api.SensiboError):
                self.client(Response([device])).set_state(DEVICE, **arguments)
            self.assertEqual([request.method for request in self.opener.requests], ["GET"])

    def test_mode_change_does_not_choose_a_new_fan_setting(self):
        before = pod()
        before["remoteCapabilities"]["modes"]["cool"]["fanLevels"] = ["low", "high"]
        with self.assertRaisesRegex(api.SensiboError, "explicit compatible"):
            self.client(Response([before])).set_state(DEVICE, mode="cool")
        after = pod()
        after["acState"]["mode"] = "dry"
        client = self.client(Response([pod()]), Response({}), Response([after]))
        client.set_state(DEVICE, mode="dry")
        self.assertEqual(json.loads(self.opener.requests[1].data), {"acState": {"mode": "dry"}})

    def test_timeout_rate_limit_rejection_or_bad_readback_never_replays_write(self):
        for result in (
            TimeoutError("private " + TEST_KEY),
            HTTPError(api.API_BASE, 429, "limited", {"Retry-After": "0"}, None),
            Response({"status": "Failure"}),
            Response({}),
        ):
            responses = [Response([pod()]), result]
            if isinstance(result, Response):
                responses.append(Response([pod()]))
            client = self.client(*responses)
            with self.subTest(kind=type(result).__name__), self.assertRaisesRegex(api.SensiboError, "uncertain") as caught:
                client.set_state(DEVICE, on=False)
            self.assertNotIn(TEST_KEY, str(caught.exception))
            self.assertEqual(sum(request.method == "POST" for request in self.opener.requests), 1)
            self.assertIsNone(client._snapshot)

    def test_concurrent_control_requests_are_serialized(self):
        client = self.client()
        gate = threading.Barrier(2)
        active = []
        calls = []
        device = pod()

        def request(method, path, parameters=None, body=None):
            calls.append((threading.get_ident(), method))
            if method == "GET":
                return [copy.deepcopy(device)]
            active.append(threading.get_ident())
            self.assertEqual(len(active), 1)
            device["acState"].update(body["acState"])
            active.pop()
            return {}

        def change(on):
            gate.wait(timeout=5)
            return client.set_state(DEVICE, on=on)

        with patch.object(client, "_request", side_effect=request), ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(change, False)
            second = pool.submit(change, True)
            self.assertTrue(first.result(timeout=5).cloud_state_matches)
            self.assertTrue(second.result(timeout=5).cloud_state_matches)
        self.assertEqual([method for _, method in calls], ["GET", "POST", "GET", "GET", "POST", "GET"])
        self.assertEqual(len({thread for thread, _ in calls[:3]}), 1)
        self.assertEqual(len({thread for thread, _ in calls[3:]}), 1)
