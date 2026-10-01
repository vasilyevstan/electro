import asyncio
import json
import os
import stat
import subprocess
import tempfile
import unittest
from dataclasses import asdict, replace
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import tapo_plugs as api
from nordpool_ee import UTC

try:
    import kasa
except ModuleNotFoundError as error:
    if error.name != "kasa":
        raise
    kasa = None


MAC = "02:00:00:00:00:01"
OTHER_MAC = "02:00:00:00:00:02"
IP = "192.168.50.2"
NEW_IP = "192.168.50.3"
NOW = datetime(2026, 10, 1, 12, tzinfo=UTC)
SECRET = "synthetic-tapo-password"


def record(mac=MAC, ip=IP):
    return api.KnownPlug(mac, ip, "P110(EU)", None, "KLAP", True, NOW.isoformat())


def packet(mac=MAC, ip=IP, protocol="KLAP"):
    return {
        "meta": {"ip": ip, "port": 20002},
        "discovery_response": {"result": {
            "device_type": api.FAMILY, "device_model": "P110(EU)", "mac": mac,
            "ip": "198.51.100.99",
            "mgt_encrypt_schm": {"encrypt_type": protocol},
        }},
    }


def feature(value, unit=None, kind=None):
    return SimpleNamespace(value=value, unit=unit, type=kind or kasa.Feature.Type.Sensor)


def device(mac=MAC, ip=IP, on=True, power=12.345):
    from kasa.smart import SmartDevice

    result = MagicMock(spec=SmartDevice)
    result.mac = mac
    result.host = ip
    result.model = "P110(EU)"
    result.alias = "Example socket"
    result.device_type = kasa.DeviceType.Plug
    result.sys_info = {
        "mac": mac, "device_on": on, "fw_ver": "synthetic-firmware",
        "hw_ver": "synthetic-hardware",
    }
    result.update = AsyncMock()
    result.disconnect = AsyncMock()
    result.protocol = MagicMock()
    result.protocol.query = AsyncMock(return_value={"set_device_info": {}})
    result.modules = {kasa.Module.Energy: SimpleNamespace(data={"today_energy": 125})}
    result.features = {
        "current_consumption": feature(power, "W"),
        "consumption_today": feature(0.125, "kWh"),
        "consumption_this_month": feature(2.5, "kWh"),
        "voltage": feature(231.5, "V"),
        "current": feature(0.042, "A"),
        "rssi": feature(-49, "dBm"),
        "overheated": feature(False, kind=kasa.Feature.Type.BinarySensor),
        "device_time": feature(NOW),
        "ssid": feature("synthetic-private-network"),
        "device_id": feature("synthetic-private-device-id"),
        "state": feature(on, kind=kasa.Feature.Type.Switch),
        "reboot": feature("<Action>", kind=kasa.Feature.Type.Action),
    }
    return result


def discovery(replies, supported_ips):
    clients = {host: SimpleNamespace(disconnect=AsyncMock()) for host in supported_ips}

    async def discover(**kwargs):
        for reply in replies:
            kwargs["on_discovered_raw"](reply)
        return clients

    return discover


class CredentialAndIdentityTests(unittest.TestCase):
    def test_mac_normalization_and_rejection(self):
        self.assertEqual(api.normalize_mac(" 02-0a-0b-0c-0d-0e "), "02:0A:0B:0C:0D:0E")
        for invalid in ("", "socket", "02:00-00:00:00:01", "02:00:00:00:00", "00:00:00:00:00:00",
                        "FF:FF:FF:FF:FF:FF", "01:00:00:00:00:01", None, 123):
            with self.subTest(value=invalid), self.assertRaises(api.TapoError):
                api.normalize_mac(invalid)

    def test_environment_pair_has_precedence_and_no_secret_repr(self):
        with patch.dict(os.environ, {"TAPO_USERNAME": "synthetic-user", "TAPO_PASSWORD": SECRET}, clear=True), patch(
            "tapo_plugs.subprocess.run"
        ) as keychain:
            credentials = api.load_credentials()
        keychain.assert_not_called()
        self.assertEqual(credentials.password, SECRET)
        self.assertNotIn(SECRET, repr(credentials))
        self.assertNotIn("synthetic-user", repr(credentials))

    def test_partial_or_empty_environment_never_uses_keychain(self):
        for values in (
            {"TAPO_USERNAME": "synthetic-user"}, {"TAPO_PASSWORD": SECRET},
            {"TAPO_USERNAME": "", "TAPO_PASSWORD": SECRET},
            {"TAPO_USERNAME": "synthetic-user", "TAPO_PASSWORD": "   "},
        ):
            with self.subTest(values=tuple(values)), patch.dict(os.environ, values, clear=True), patch(
                "tapo_plugs.subprocess.run"
            ) as keychain, self.assertRaisesRegex(api.TapoError, "both"):
                api.load_credentials()
            keychain.assert_not_called()

    def test_keychain_reads_only_named_items_without_secret_arguments(self):
        with patch.dict(os.environ, {}, clear=True), patch("tapo_plugs.sys.platform", "darwin"), patch(
            "tapo_plugs.subprocess.run",
            side_effect=[
                SimpleNamespace(returncode=0, stdout=b"synthetic-user\n"),
                SimpleNamespace(returncode=0, stdout=(SECRET + "\n").encode()),
            ],
        ) as command:
            credentials = api.load_credentials()
        self.assertEqual(credentials.password, SECRET)
        self.assertEqual(command.call_count, 2)
        for account, call in zip(("username", "password"), command.call_args_list):
            self.assertEqual(call.args[0], [
                "/usr/bin/security", "find-generic-password", "-s", "tp-tapo-mcp", "-a", account, "-w",
            ])
            self.assertNotIn(SECRET, repr(call))
            self.assertEqual(call.kwargs["stderr"], subprocess.DEVNULL)

    def test_keychain_failures_are_safe_and_explicit(self):
        for result in (
            SimpleNamespace(returncode=44, stdout=SECRET.encode()),
            SimpleNamespace(returncode=0, stdout=b"\n"),
            SimpleNamespace(returncode=0, stdout=b"\xff"),
        ):
            with self.subTest(code=result.returncode), patch.dict(os.environ, {}, clear=True), patch(
                "tapo_plugs.sys.platform", "darwin"
            ), patch("tapo_plugs.subprocess.run", return_value=result), self.assertRaises(api.TapoError) as caught:
                api.load_credentials()
            self.assertNotIn(SECRET, str(caught.exception))
        with patch.dict(os.environ, {}, clear=True), patch("tapo_plugs.sys.platform", "linux"), self.assertRaisesRegex(
            api.TapoError, "process environment"
        ):
            api.load_credentials()

    def test_unsupported_runtime_and_missing_extra_have_actionable_errors(self):
        with patch("tapo_plugs.sys.version_info", (3, 10)), self.assertRaisesRegex(api.TapoError, "3.11"):
            api.ensure_runtime()
        with patch("tapo_plugs.importlib.util.find_spec", return_value=None), patch(
            "tapo_plugs.sys.version_info", (3, 12)
        ), self.assertRaisesRegex(api.TapoError, "--extra tapo"):
            api.ensure_runtime()


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "tapo" / "devices.json"
        self.registry = api.Registry(self.path)

    def write(self, value):
        self.path.parent.mkdir(mode=0o700, exist_ok=True)
        self.path.write_text(json.dumps(value), encoding="utf-8")
        self.path.chmod(0o600)

    def test_private_roundtrip_has_only_inventory_not_credentials_or_measurements(self):
        self.assertEqual(self.registry.load(), {})
        known = replace(record(), alias="Example socket", capabilities=["relay"], capabilities_verified_at=NOW.isoformat())
        self.registry.save({MAC: known})
        self.assertEqual(self.registry.load(), {MAC: known})
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.path.parent.stat().st_mode), 0o700)
        content = self.path.read_text()
        for forbidden in ("password", "username", "power_w", SECRET):
            self.assertNotIn(forbidden, content)

    def test_invalid_registry_fails_instead_of_resetting(self):
        rows = [asdict(record())]
        for value in (
            [], {"version": True, "devices": rows}, {"version": 2, "devices": rows},
            {"version": 1, "devices": {}}, {"version": 1, "devices": rows + rows},
            {"version": 1, "devices": [{**rows[0], "last_ip": "8.8.8.8"}]},
            {"version": 1, "devices": [{**rows[0], "last_seen": "2026-10-01T12:00:00"}]},
            {"version": 1, "devices": [{**rows[0], "capabilities": ["relay"]}]},
        ):
            with self.subTest(value=value):
                self.write(value)
                before = self.path.read_bytes()
                with self.assertRaises(api.TapoError):
                    self.registry.load()
                self.assertEqual(self.path.read_bytes(), before)
        self.path.write_text("{broken", encoding="utf-8")
        with self.assertRaisesRegex(api.TapoError, "invalid JSON"):
            self.registry.load()

    def test_public_permissions_are_rejected(self):
        self.registry.save({MAC: record()})
        self.path.chmod(0o644)
        with self.assertRaisesRegex(api.TapoError, "600"):
            self.registry.load()
        self.path.chmod(0o600)
        self.path.parent.chmod(0o755)
        with self.assertRaisesRegex(api.TapoError, "700"):
            self.registry.save({MAC: record()})

    def test_failed_atomic_write_preserves_inventory_and_cleans_temporary(self):
        self.registry.save({MAC: record()})
        with patch("tapo_plugs.os.replace", side_effect=OSError(SECRET)), self.assertRaises(api.TapoError) as caught:
            self.registry.save({MAC: record(ip=NEW_IP)})
        self.assertNotIn(SECRET, str(caught.exception))
        self.assertEqual(self.registry.load()[MAC].last_ip, IP)
        self.assertEqual(list(self.path.parent.glob(".devices-*")), [])


@unittest.skipIf(kasa is None, "Install the optional tapo extra for client-adapter tests.")
class ReadingTests(unittest.TestCase):
    def test_power_energy_and_operating_readings_keep_units_and_privacy(self):
        result = api.snapshot(device(), NOW)
        self.assertTrue(result.is_on)
        self.assertEqual(result.measurements.power.value, 12.345)
        self.assertEqual(result.measurements.power.unit, "W")
        self.assertEqual(result.measurements.energy_today.value, 0.125)
        self.assertEqual(result.measurements.energy_today.unit, "kWh")
        self.assertEqual(result.measurements.energy_today.period, "device-local day")
        self.assertEqual(result.measurements.energy_this_month.value, 2.5)
        self.assertEqual(result.measurements.voltage.value, 231.5)
        self.assertEqual(result.measurements.current.value, 0.042)
        self.assertEqual(result.measurements.energy_total.status, "unsupported")
        self.assertIsNone(result.measurements.energy_total.value)
        self.assertEqual(result.device_time, NOW.isoformat())
        self.assertEqual({row.id for row in result.operating_readings}, {"rssi", "overheated", "device_time"})
        self.assertFalse(next(row.value for row in result.operating_readings if row.id == "overheated"))
        self.assertNotIn("synthetic-private", json.dumps(asdict(result)))

    def test_on_zero_and_off_energy_counters_are_not_inferred_or_reset(self):
        for on in (True, False):
            with self.subTest(on=on):
                result = api.snapshot(device(on=on, power=0), NOW)
                self.assertEqual(result.is_on, on)
                self.assertEqual(result.measurements.power.value, 0)
                self.assertEqual(result.measurements.power.status, "available")
                self.assertEqual(result.measurements.energy_today.value, 0.125)
                self.assertEqual(result.measurements.energy_this_month.value, 2.5)

    def test_missing_supported_reading_is_not_zero_or_unsupported(self):
        result = api.snapshot(device(power=None), NOW)
        self.assertEqual(result.measurements.power.status, "unavailable")
        self.assertIsNone(result.measurements.power.value)
        self.assertTrue(result.warnings)
        plug = device()
        del plug.features["voltage"]
        self.assertEqual(api.snapshot(plug, NOW).measurements.voltage.status, "unsupported")

    def test_missing_or_invalid_relay_state_is_not_assumed_off(self):
        for value in (None, 0, "false"):
            plug = device()
            plug.sys_info["device_on"] = value
            with self.subTest(value=value), self.assertRaisesRegex(api.TapoError, "relay state"):
                api.snapshot(plug, NOW)

    def test_bad_numeric_values_and_units_are_explicitly_unavailable(self):
        for value in (True, float("nan"), float("inf"), 10 ** 1000, "10"):
            with self.subTest(kind=type(value).__name__):
                reading = api.snapshot(device(power=value), NOW).measurements.power
                self.assertEqual(reading.status, "unavailable")
                self.assertIsNone(reading.value)
        plug = device()
        plug.features["current_consumption"].unit = "mW"
        self.assertEqual(api.snapshot(plug, NOW).measurements.power.status, "unavailable")

    def test_failed_energy_module_never_exposes_cached_values(self):
        class FailedEnergy:
            @property
            def data(self):
                raise kasa.KasaException(SECRET)

        plug = device()
        plug.modules[kasa.Module.Energy] = FailedEnergy()
        result = api.snapshot(plug, NOW)
        for reading in asdict(result.measurements).values():
            self.assertEqual(reading["status"], "unavailable")
            self.assertIsNone(reading["value"])
        self.assertNotIn(SECRET, json.dumps(asdict(result)))
        self.assertTrue(result.is_on)

    def test_no_energy_module_and_no_clock_are_explicit(self):
        plug = device()
        plug.modules = {}
        plug.features = {}
        result = api.snapshot(plug, NOW)
        self.assertEqual(result.measurements.power.status, "unsupported")
        self.assertIsNone(result.device_time)
        self.assertIn("device clock", " ".join(result.warnings))


@unittest.skipIf(kasa is None, "Install the optional tapo extra for client-adapter tests.")
class ClientTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.registry = api.Registry(Path(self.temporary.name) / "devices.json")
        self.credentials = MagicMock(return_value=api.Credentials("synthetic-user", SECRET))
        self.client = api.TapoClient(self.registry, self.credentials, lambda: NOW)

    def known(self):
        self.registry.save({MAC: record()})

    async def test_discovery_needs_no_credentials_and_keeps_unsupported_and_unseen(self):
        unseen = "02:00:00:00:00:03"
        self.registry.save({unseen: record(unseen, "192.168.50.8")})
        replies = [packet(), packet(), packet(OTHER_MAC, NEW_IP, "TPAP")]
        with patch("kasa.Discover.discover", side_effect=discovery(replies, [IP])) as scan:
            result = await self.client.list_plugs()
        self.credentials.assert_not_called()
        self.assertNotIn("credentials", scan.call_args.kwargs)
        self.assertEqual(len(result.plugs), 3)
        indexed = {row.mac: row for row in result.plugs}
        self.assertEqual(indexed[MAC].last_ip, IP)
        self.assertEqual(indexed[MAC].discovery_status, "seen")
        self.assertEqual(indexed[OTHER_MAC].access_status, "unsupported_protocol")
        self.assertEqual(indexed[unseen].discovery_status, "not_seen")
        self.assertFalse(hasattr(indexed[unseen], "is_on"))
        self.assertEqual(len(self.registry.load()), 3)
        self.assertTrue(result.warnings)

    async def test_cached_inventory_and_empty_discovery_do_not_claim_reachability(self):
        self.known()
        with patch("kasa.Discover.discover", side_effect=discovery([], [])) as scan:
            cached = await self.client.list_plugs(False)
            scan.assert_not_called()
            fresh = await self.client.list_plugs(True)
        self.assertEqual(cached.plugs[0].discovery_status, "not_checked")
        self.assertEqual(fresh.plugs[0].discovery_status, "not_seen")
        self.assertEqual(len(self.registry.load()), 1)
        self.assertIn("No Tapo plugs", " ".join(fresh.warnings))

    async def test_conflicting_identity_does_not_change_registry(self):
        self.known()
        for replies in ([packet(), packet(ip=NEW_IP)], [packet(), packet(OTHER_MAC)]):
            with self.subTest(replies=replies), patch(
                "kasa.Discover.discover", side_effect=discovery(replies, [IP, NEW_IP])
            ), self.assertRaisesRegex(api.TapoError, "conflicting"):
                await self.client.list_plugs()
            self.assertEqual(self.registry.load(), {MAC: record()})

    async def test_changed_ip_never_authenticates_or_controls_wrong_old_host(self):
        self.known()
        wrong, right = device(OTHER_MAC), device(ip=NEW_IP)
        with patch("kasa.Discover.discover_single", side_effect=[wrong, right]) as single, patch(
            "kasa.Discover.discover", side_effect=discovery([packet(ip=NEW_IP)], [NEW_IP])
        ) as scan:
            result = await self.client.get_plug(MAC)
        self.assertEqual(result.mac, MAC)
        self.assertEqual(result.ip, NEW_IP)
        self.assertEqual([call.args[0] for call in single.call_args_list], [IP, NEW_IP])
        scan.assert_awaited_once()
        wrong.update.assert_not_awaited()
        wrong.protocol.query.assert_not_awaited()
        wrong.disconnect.assert_awaited_once()
        right.disconnect.assert_awaited_once()
        self.assertEqual(self.registry.load()[MAC].last_ip, NEW_IP)

    async def test_authenticated_identity_mismatch_never_sends_command(self):
        self.known()
        impostor = device()
        impostor.sys_info["mac"] = OTHER_MAC
        with patch("kasa.Discover.discover_single", return_value=impostor), patch(
            "kasa.Discover.discover", side_effect=discovery([packet()], [IP])
        ) as scan, self.assertRaisesRegex(api.TapoError, "verified MAC"):
            await self.client.set_power(MAC, False)
        self.assertEqual(impostor.update.await_count, 2)
        impostor.protocol.query.assert_not_awaited()
        scan.assert_awaited_once()

    async def test_authentication_failure_is_redacted_without_retrying_credentials(self):
        self.known()
        plug = device()
        plug.update.side_effect = kasa.AuthenticationError(SECRET)
        with patch("kasa.Discover.discover_single", return_value=plug), patch(
            "kasa.Discover.discover"
        ) as scan, self.assertRaisesRegex(api.TapoError, "authentication failed") as caught:
            await self.client.get_plug(MAC)
        self.assertNotIn(SECRET, str(caught.exception))
        scan.assert_not_called()
        plug.disconnect.assert_awaited_once()

    async def test_unknown_mac_is_bounded_and_unsupported_mac_is_explicit(self):
        with patch("kasa.Discover.discover", side_effect=discovery([], [])) as scan, patch(
            "kasa.Discover.discover_single"
        ) as single, self.assertRaisesRegex(api.TapoError, "not found"):
            await self.client.get_plug(MAC)
        scan.assert_awaited_once()
        single.assert_not_called()
        with patch("kasa.Discover.discover", side_effect=discovery([packet(protocol="TPAP")], [])), self.assertRaisesRegex(
            api.TapoError, "unsupported protocol"
        ):
            await self.client.get_plug(MAC)

    async def test_invalid_inputs_and_corrupt_registry_never_reach_devices(self):
        with patch("kasa.Discover.discover_single") as single, patch("kasa.Discover.discover") as scan:
            for mac, on in (("alias", True), (MAC, "false"), (MAC, 1)):
                with self.subTest(mac=mac, on=on), self.assertRaises(api.TapoError):
                    await self.client.set_power(mac, on)
            self.credentials.assert_not_called()
            self.known()
            self.registry.path.write_text("{broken", encoding="utf-8")
            with self.assertRaises(api.TapoError):
                await self.client.set_power(MAC, True)
        single.assert_not_called()
        scan.assert_not_called()

    async def test_matching_desired_state_sends_no_command_and_includes_consumption(self):
        self.known()
        plug = device(on=True, power=7.5)
        with patch("kasa.Discover.discover_single", return_value=plug):
            result = await self.client.set_power(MAC, True)
        self.assertTrue(result.verified)
        self.assertFalse(result.command_sent)
        self.assertIsNone(result.acknowledged)
        self.assertEqual(result.state.measurements.power.value, 7.5)
        plug.protocol.query.assert_not_awaited()
        plug.disconnect.assert_awaited_once()

    async def test_one_no_retry_command_and_fresh_readback_consumption(self):
        self.known()
        before, after = device(on=False, power=0), device(on=True, power=18.25)
        with patch("kasa.Discover.discover_single", side_effect=[before, after]) as single:
            result = await self.client.set_power(MAC, True)
        before.protocol.query.assert_awaited_once_with({"set_device_info": {"device_on": True}}, retry_count=0)
        self.assertEqual(single.await_count, 2)
        self.assertTrue(result.command_sent)
        self.assertTrue(result.acknowledged)
        self.assertTrue(result.verified)
        self.assertFalse(result.before_on)
        self.assertEqual(result.state.measurements.power.value, 18.25)
        self.assertEqual(result.state.measurements.energy_today.value, 0.125)
        for plug in (before, after):
            plug.update.assert_awaited_once()
            plug.disconnect.assert_awaited_once()

    async def test_lost_acknowledgement_can_be_resolved_by_readback_not_a_retry(self):
        self.known()
        before, after = device(on=False), device(on=True)
        before.protocol.query.side_effect = kasa.TimeoutError(SECRET)
        with patch("kasa.Discover.discover_single", side_effect=[before, after]):
            result = await self.client.set_power(MAC, True)
        self.assertFalse(result.acknowledged)
        self.assertTrue(result.verified)
        self.assertTrue(result.warnings)
        self.assertNotIn(SECRET, json.dumps(asdict(result)))
        before.protocol.query.assert_awaited_once()

    async def test_readback_failure_and_wrong_state_are_not_success(self):
        self.known()
        before = device(on=False)
        with patch("kasa.Discover.discover_single", return_value=before), patch.object(
            self.client, "_read", side_effect=api.TapoError("unreachable")
        ), self.assertRaisesRegex(api.TapoError, "may have taken effect"):
            await self.client.set_power(MAC, True)
        before.protocol.query.assert_awaited_once()
        before, after = device(on=False), device(on=False)
        with patch("kasa.Discover.discover_single", side_effect=[before, after]), self.assertRaisesRegex(
            api.TapoError, "not confirmed"
        ):
            await self.client.set_power(MAC, True)
        before.protocol.query.assert_awaited_once()

    async def test_registry_failure_after_verified_write_reports_physical_outcome(self):
        self.known()
        before, after = device(on=False), device(on=True)
        with patch("kasa.Discover.discover_single", side_effect=[before, after]), patch.object(
            self.registry, "save", side_effect=api.TapoError("registry unavailable")
        ), self.assertRaisesRegex(api.TapoError, "relay state was confirmed"):
            await self.client.set_power(MAC, True)
        before.protocol.query.assert_awaited_once()

    async def test_disconnect_failure_after_write_is_explicitly_uncertain(self):
        self.known()
        before = device(on=False)
        before.disconnect.side_effect = kasa.KasaException(SECRET)
        with patch("kasa.Discover.discover_single", return_value=before), self.assertRaisesRegex(
            api.TapoError, "may have taken effect"
        ) as caught:
            await self.client.set_power(MAC, True)
        self.assertNotIn(SECRET, str(caught.exception))
        before.protocol.query.assert_awaited_once()

    async def test_discovery_close_failure_still_closes_other_clients(self):
        first, second = device(), device(OTHER_MAC, NEW_IP)
        first.disconnect.side_effect = kasa.KasaException(SECRET)

        async def discover(**kwargs):
            kwargs["on_discovered_raw"](packet())
            kwargs["on_discovered_raw"](packet(OTHER_MAC, NEW_IP))
            return {IP: first, NEW_IP: second}

        with patch("kasa.Discover.discover", side_effect=discover), self.assertRaises(api.TapoError) as caught:
            await self.client.list_plugs()
        self.assertNotIn(SECRET, str(caught.exception))
        first.disconnect.assert_awaited_once()
        second.disconnect.assert_awaited_once()
        self.assertEqual(self.registry.load(), {})

    async def test_concurrent_desired_state_requests_are_serialized(self):
        self.known()
        entered, release = asyncio.Event(), asyncio.Event()
        before, after, already_on = device(on=False), device(on=True), device(on=True)

        async def write(*args, **kwargs):
            entered.set()
            await release.wait()
            return {}

        before.protocol.query.side_effect = write
        with patch("kasa.Discover.discover_single", side_effect=[before, after, already_on]) as single:
            first = asyncio.create_task(self.client.set_power(MAC, True))
            second = None
            try:
                await asyncio.wait_for(entered.wait(), timeout=5)
                second = asyncio.create_task(self.client.set_power(MAC, True))
                await asyncio.sleep(0)
                self.assertEqual(single.await_count, 1)
            finally:
                release.set()
                results = await asyncio.wait_for(asyncio.gather(first, *([second] if second else [])), timeout=5)
        self.assertEqual([result.command_sent for result in results], [True, False])
        before.protocol.query.assert_awaited_once()
        already_on.protocol.query.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
