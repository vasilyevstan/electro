import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mcp import Client

import sensibo as api
import sensibo_mcp as server
from test_sensibo import DEVICE, MAC, NOW, TEST_KEY, Opener, Response, pod


class SensiboProtocolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="electro-sensibo-mcp-test-")
        self.addCleanup(self.temporary.cleanup)
        self.registry = api.Registry(Path(self.temporary.name) / "devices.json")
        self.registry.save([api.KnownDevice(DEVICE, MAC, "skyv2", "Synthetic room", NOW.isoformat())])

    def client(self, *responses):
        self.opener = Opener(responses)
        return api.SensiboClient(
            self.registry, lambda: api.Credentials(TEST_KEY), self.opener, now=lambda: NOW,
        )

    async def test_four_typed_tools_read_write_annotations_and_structured_read(self):
        backend = self.client(Response([pod()]))
        with patch.object(server, "_client", backend):
            async with Client(server.mcp, raise_exceptions=True) as client:
                tools = (await client.list_tools()).tools
                response = await client.call_tool("get_sensibo_device", {"device_id": DEVICE})
        self.assertEqual({tool.name for tool in tools}, {
            "list_sensibo_devices", "get_sensibo_device", "get_sensibo_measurements", "set_sensibo_ac_state",
        })
        for tool in tools:
            write = tool.name == "set_sensibo_ac_state"
            self.assertEqual(tool.annotations.read_only_hint, not write)
            self.assertEqual(tool.annotations.destructive_hint, write)
            self.assertEqual(tool.annotations.idempotent_hint, not write)
            self.assertNotIn("api_key", tool.input_schema["properties"])
            self.assertNotIn("ip", tool.input_schema["properties"])
            self.assertTrue(tool.output_schema)
        self.assertFalse(response.is_error)
        self.assertEqual(json.loads(response.content[0].text), response.structured_content)
        self.assertEqual(response.structured_content["device"]["mac"], MAC)
        self.assertEqual(response.structured_content["measurements"][0]["unit"], "C")
        self.assertNotIn(TEST_KEY, response.content[0].text)

    async def test_list_and_history_output_shapes(self):
        history = {"temperature": [{"time": NOW.isoformat(), "value": 0}]}
        backend = self.client(Response([pod()]), Response(history))
        with patch.object(server, "_client", backend):
            async with Client(server.mcp, raise_exceptions=True) as client:
                inventory = await client.call_tool("list_sensibo_devices", {})
                result = await client.call_tool("get_sensibo_measurements", {"device_id": DEVICE})
        self.assertFalse(inventory.is_error)
        self.assertFalse(result.is_error)
        self.assertEqual(len(inventory.structured_content["devices"]), 1)
        self.assertEqual(result.structured_content["series"][0]["samples"][0]["value"], 0)
        self.assertEqual(json.loads(result.content[0].text), result.structured_content)

    async def test_strict_argument_validation_prevents_network_and_control(self):
        backend = self.client()
        with patch.object(server, "_client", backend):
            async with Client(server.mcp, raise_exceptions=True) as client:
                for arguments in (
                    {"device_id": DEVICE, "on": "false"},
                    {"device_id": DEVICE, "on": 1},
                    {"device_id": DEVICE, "target_temperature": "22", "temperature_unit": "C"},
                    {"device_id": DEVICE},
                ):
                    result = await client.call_tool("set_sensibo_ac_state", arguments)
                    self.assertTrue(result.is_error)
                for days in (True, "1", 0, 8):
                    result = await client.call_tool("get_sensibo_measurements", {"device_id": DEVICE, "days": days})
                    self.assertTrue(result.is_error)
        self.assertEqual(self.opener.requests, [])

    async def test_control_result_is_not_physical_verification(self):
        after = pod()
        after["acState"]["on"] = False
        backend = self.client(Response([pod()]), Response({}), Response([after]))
        with patch.object(server, "_client", backend):
            async with Client(server.mcp, raise_exceptions=True) as client:
                result = await client.call_tool("set_sensibo_ac_state", {"device_id": DEVICE, "on": False})
        self.assertFalse(result.is_error)
        self.assertTrue(result.structured_content["cloud_state_matches"])
        self.assertFalse(result.structured_content["physical_effect_verified"])
        self.assertIs(result.structured_content["requested"]["on"], False)
        self.assertEqual(json.loads(result.content[0].text), result.structured_content)

    async def test_expected_failures_are_tool_errors_without_secrets(self):
        with patch.object(server._client, "get_device", side_effect=api.SensiboError("Sensibo denied API access.")):
            async with Client(server.mcp, raise_exceptions=True) as client:
                result = await client.call_tool("get_sensibo_device", {"device_id": DEVICE})
        self.assertTrue(result.is_error)
        self.assertIn("denied", result.content[0].text)
        self.assertNotIn(TEST_KEY, result.content[0].text)
