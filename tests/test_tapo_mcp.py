import io
import json
import logging
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from mcp import Client

import tapo_mcp as server
import tapo_plugs as api
from nordpool_ee import UTC


MAC = "02:00:00:00:00:01"
NOW = datetime(2026, 10, 1, 12, tzinfo=UTC)


def state():
    return api.PlugState(
        MAC, "192.168.50.2", "P110(EU)", "Example socket", "synthetic-fw",
        "synthetic-hw", True, NOW.isoformat(), NOW.isoformat(),
        api.PlugMeasurements(
            api.Measurement(12.5, "W", "available", None, None),
            api.Measurement(0.25, "kWh", "available", None, "device-local day"),
            api.Measurement(1.5, "kWh", "available", None, "device-local month"),
            api.Measurement(None, "kWh", "unsupported", "Not supported.", "since device reboot"),
            api.Measurement(230, "V", "available", None, None),
            api.Measurement(0.055, "A", "available", None, None),
        ),
        [], ["relay", "current_consumption"], [],
    )


class TapoProtocolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.backend = MagicMock(spec=api.TapoClient)
        self.backend.list_plugs = AsyncMock(return_value=api.PlugInventory(
            "local_tapo", NOW.isoformat(), True, [], ["No Tapo plugs responded."],
        ))
        self.backend.get_plug = AsyncMock(return_value=state())
        self.backend.set_power = AsyncMock(return_value=api.PowerResult(
            MAC, True, False, True, True, True, state(), [],
        ))

    async def test_exact_tools_schemas_and_read_write_annotations(self):
        async with Client(server.mcp, raise_exceptions=True) as client:
            listed = await client.list_tools()
        tools = {tool.name: tool for tool in listed.tools}
        self.assertEqual(set(tools), {"list_tapo_plugs", "get_tapo_plug", "set_tapo_plug_power"})
        self.assertEqual(set(tools["list_tapo_plugs"].input_schema["properties"]), {"refresh"})
        self.assertEqual(set(tools["get_tapo_plug"].input_schema["properties"]), {"mac"})
        self.assertEqual(set(tools["set_tapo_plug_power"].input_schema["properties"]), {"mac", "on"})
        self.assertEqual(set(tools["set_tapo_plug_power"].input_schema["required"]), {"mac", "on"})
        for name, tool in tools.items():
            self.assertEqual(tool.annotations.read_only_hint, name != "set_tapo_plug_power")
            self.assertEqual(tool.annotations.destructive_hint, name == "set_tapo_plug_power")
            self.assertTrue(tool.annotations.idempotent_hint)
            self.assertTrue(tool.annotations.open_world_hint)
            self.assertNotIn("password", json.dumps(tool.input_schema))
            self.assertNotIn("username", json.dumps(tool.input_schema))

    async def test_discovery_defaults_and_cached_flag(self):
        with patch.object(server, "_client", self.backend):
            async with Client(server.mcp, raise_exceptions=True) as client:
                result = await client.call_tool("list_tapo_plugs", {})
                cached = await client.call_tool("list_tapo_plugs", {"refresh": False})
        self.assertFalse(result.is_error)
        self.assertFalse(cached.is_error)
        self.assertEqual([call.args for call in self.backend.list_plugs.await_args_list], [(True,), (False,)])
        self.assertEqual(json.loads(result.content[0].text), result.structured_content)

    async def test_get_and_power_result_include_on_state_consumption_and_units(self):
        with patch.object(server, "_client", self.backend):
            async with Client(server.mcp, raise_exceptions=True) as client:
                reading = await client.call_tool("get_tapo_plug", {"mac": MAC})
                power = await client.call_tool("set_tapo_plug_power", {"mac": MAC, "on": True})
        for result in (reading, power):
            self.assertFalse(result.is_error)
            self.assertEqual(json.loads(result.content[0].text), result.structured_content)
        data = reading.structured_content
        self.assertTrue(data["is_on"])
        self.assertEqual(data["measurements"]["power"]["value"], 12.5)
        self.assertEqual(data["measurements"]["power"]["unit"], "W")
        self.assertEqual(data["measurements"]["energy_today"]["unit"], "kWh")
        self.assertEqual(data["measurements"]["energy_total"]["status"], "unsupported")
        self.assertIsNone(data["measurements"]["energy_total"]["value"])
        self.assertEqual(power.structured_content["state"]["measurements"], data["measurements"])
        self.assertTrue(power.structured_content["verified"])
        self.backend.get_plug.assert_awaited_once_with(MAC)
        self.backend.set_power.assert_awaited_once_with(MAC, True)

    async def test_mutation_requires_an_explicit_boolean_and_mac(self):
        with patch.object(server, "_client", self.backend):
            async with Client(server.mcp, raise_exceptions=True) as client:
                for arguments in (
                    {}, {"mac": MAC}, {"on": True}, {"mac": MAC, "on": "true"},
                    {"mac": MAC, "on": "false"}, {"mac": MAC, "on": 1},
                    {"mac": MAC, "on": None},
                ):
                    with self.subTest(arguments=arguments):
                        result = await client.call_tool("set_tapo_plug_power", arguments)
                        self.assertTrue(result.is_error)
                result = await client.call_tool("list_tapo_plugs", {"refresh": "false"})
                self.assertTrue(result.is_error)
        self.backend.set_power.assert_not_awaited()
        self.backend.list_plugs.assert_not_awaited()

    async def test_expected_failures_and_uncertain_writes_are_mcp_errors(self):
        self.backend.get_plug.side_effect = api.TapoError("Tapo authentication failed.")
        self.backend.list_plugs.side_effect = api.TapoError("Local discovery timed out.")
        self.backend.set_power.side_effect = api.TapoError("The command may have taken effect; outcome unconfirmed.")
        with patch.object(server, "_client", self.backend):
            async with Client(server.mcp, raise_exceptions=True) as client:
                for name, arguments in (
                    ("get_tapo_plug", {"mac": MAC}), ("list_tapo_plugs", {}),
                    ("set_tapo_plug_power", {"mac": MAC, "on": False}),
                ):
                    result = await client.call_tool(name, arguments)
                    self.assertTrue(result.is_error)
                    self.assertIsNone(result.structured_content)

    async def test_missing_credentials_do_not_leak_supplied_half_pair(self):
        secret = "synthetic-private-tapo-secret"
        backend = api.TapoClient()
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        logging.getLogger().addHandler(handler)
        try:
            with patch.object(server, "_client", backend), patch(
                "tapo_plugs.ensure_runtime"
            ), patch.dict(os.environ, {"TAPO_PASSWORD": secret}, clear=True), patch(
                "tapo_plugs.subprocess.run"
            ) as keychain:
                async with Client(server.mcp, raise_exceptions=True) as client:
                    result = await client.call_tool("get_tapo_plug", {"mac": MAC})
        finally:
            logging.getLogger().removeHandler(handler)
        self.assertTrue(result.is_error)
        self.assertIn("both TAPO_USERNAME and TAPO_PASSWORD", result.content[0].text)
        self.assertNotIn(secret, result.content[0].text + stream.getvalue())
        keychain.assert_not_called()

    async def test_upstream_authentication_exception_is_redacted(self):
        try:
            import kasa
        except ModuleNotFoundError as error:
            if error.name != "kasa":
                raise
            self.skipTest("Install the optional tapo extra for transport redaction checks.")
        secret = "synthetic-private-upstream-secret"
        with tempfile.TemporaryDirectory() as directory:
            registry = api.Registry(Path(directory) / "devices.json")
            registry.save({MAC: api.KnownPlug(
                MAC, "192.168.50.2", "P110(EU)", None, "KLAP", True, NOW.isoformat(),
            )})
            backend = api.TapoClient(registry, lambda: api.Credentials("synthetic-user", secret))
            device = MagicMock()
            device.mac = MAC
            device.update = AsyncMock(side_effect=kasa.AuthenticationError(secret))
            device.disconnect = AsyncMock()
            stream = io.StringIO()
            handler = logging.StreamHandler(stream)
            logging.getLogger().addHandler(handler)
            try:
                with patch.object(server, "_client", backend), patch(
                    "kasa.Discover.discover_single", return_value=device
                ):
                    async with Client(server.mcp, raise_exceptions=True) as client:
                        result = await client.call_tool("get_tapo_plug", {"mac": MAC})
            finally:
                logging.getLogger().removeHandler(handler)
        self.assertTrue(result.is_error)
        self.assertIn("authentication failed", result.content[0].text)
        self.assertNotIn(secret, result.content[0].text + stream.getvalue())


class StartupTests(unittest.TestCase):
    def test_missing_optional_runtime_fails_on_stderr_before_starting(self):
        output, errors = io.StringIO(), io.StringIO()
        with patch("tapo_mcp.ensure_runtime", side_effect=api.TapoError("Install the tapo extra.")), patch.object(
            server.mcp, "run"
        ) as run, patch("sys.stdout", output), patch("sys.stderr", errors), self.assertRaises(SystemExit) as caught:
            server.main()
        self.assertEqual(caught.exception.code, 2)
        self.assertEqual(output.getvalue(), "")
        self.assertIn("Install the tapo extra", errors.getvalue())
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
