import unittest
from datetime import date
from unittest.mock import patch

from mcp import Client
from mcp.types import TextContent

import mcp_server
import nordpool_ee


def complete_report(delivery_date):
    records = [
        {
            "timestamp": timestamp,
            "price": 11.99 if index == 0 else 20,
        }
        for index, timestamp in enumerate(
            nordpool_ee.expected_interval_timestamps(delivery_date)
        )
    ]
    return nordpool_ee.normalize_payload(
        {"success": True, "data": {"ee": records}},
        delivery_date,
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


class MCPProtocolTests(unittest.IsolatedAsyncioTestCase):
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

        self.assertEqual(tool.input_schema["properties"], {})
        self.assertIn("delivery_date", tool.output_schema["properties"])
        self.assertFalse(result.is_error)
        self.assertEqual(result.structured_content["area"], "EE")
        self.assertEqual(result.structured_content["interval_count"], 96)
        self.assertEqual(
            len(result.structured_content["intervals"]),
            96,
        )

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
