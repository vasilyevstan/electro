"""Separate stdio MCP for enrolled Sensibo Sky devices."""

from typing import Annotated, Optional

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from sensibo import (
    ControlResult,
    DeviceInventory,
    DeviceState,
    MeasurementHistory,
    SensiboClient,
    SensiboError,
)


mcp = MCPServer(
    "sensibo-mcp",
    instructions=(
        "Read enrolled Sensibo Sky controllers through the official cloud API. "
        "Identify devices by their exact enrolled device ID and verified MAC, not IP "
        "addresses or ambiguous room names. Sensors are polled on demand; report "
        "their timestamps, age, cache age, offline status, and missing values. "
        "Temperature sensors use Celsius; the HVAC target has its own explicit unit. "
        "Feels-like temperature is derived, not a separate physical sensor. "
        "Only set the specific device and desired state requested by the user. "
        "Never toggle, bulk switch, test physical controls without authorization, "
        "or automatically retry an uncertain command. Commands can emit infrared "
        "even if cloud state already matches. Acknowledgement and matching cloud "
        "state do not verify physical HVAC or compressor operation. "
        "Credentials and enrollment are private setup, never tool arguments."
    ),
)
_client = SensiboClient()
_READ_ONLY = ToolAnnotations(
    read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=True,
)
_WRITE = ToolAnnotations(
    read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=True,
)


@mcp.tool(title="List enrolled Sensibo devices and capabilities", annotations=_READ_ONLY)
def list_sensibo_devices() -> DeviceInventory:
    """List registered IDs/MACs, connectivity, and mode-specific capabilities.

    Keep unavailable identities visible. New account devices are not silently
    enrolled. Cloud reads share a 60-second cache; inspect cache age.
    """
    try:
        return _client.list_devices()
    except SensiboError as error:
        raise ToolError(str(error)) from None


@mcp.tool(title="Read Sensibo climate state and current sensors", annotations=_READ_ONLY)
def get_sensibo_device(device_id: str) -> DeviceState:
    """Read one enrolled device's cloud state and timestamped Sky sensors.

    Return measured temperature/humidity, derived feels-like temperature, and
    Wi-Fi RSSI. Missing readings are not zero. Cloud state is not physical HVAC
    feedback; samples older than 10 minutes are explicitly marked stale.
    """
    try:
        return _client.get_device(device_id)
    except SensiboError as error:
        raise ToolError(str(error)) from None


@mcp.tool(title="Read Sensibo sensor history", annotations=_READ_ONLY)
def get_sensibo_measurements(
    device_id: str, days: Annotated[int, Field(strict=True, ge=1, le=7)] = 1,
) -> MeasurementHistory:
    """Read 1-7 days of provider-available samples, with UTC times and coverage.

    Preserve nulls and gaps without interpolation or completeness claims.
    History is not a background local recording or an event subscription.
    Temperature measurements use Celsius, independently of AC setpoint units.
    """
    try:
        return _client.get_measurements(device_id, days)
    except SensiboError as error:
        raise ToolError(str(error)) from None


@mcp.tool(title="Set one Sensibo HVAC device's explicit state", annotations=_WRITE)
def set_sensibo_ac_state(
    device_id: str,
    on: Annotated[Optional[bool], Field(strict=True)] = None,
    mode: Optional[str] = None,
    target_temperature: Annotated[Optional[float], Field(strict=True)] = None,
    temperature_unit: Optional[str] = None,
    fan_level: Optional[str] = None,
    swing: Optional[str] = None,
    horizontal_swing: Optional[str] = None,
) -> ControlResult:
    """Send one explicit state update after fresh identity/capability checks.

    Changes affect a physical HVAC load. Use only for a specifically requested
    device and desired settings, never a control test or bulk action. Supply
    target_temperature and temperature_unit (C/F) together. Other options must
    match the selected mode's capabilities; unspecified settings are not sent.
    Even repeated desired states can emit IR. Never replay an uncertain write.
    """
    try:
        return _client.set_state(
            device_id, on=on, mode=mode, target_temperature=target_temperature,
            temperature_unit=temperature_unit, fan_level=fan_level,
            swing=swing, horizontal_swing=horizontal_swing,
        )
    except SensiboError as error:
        raise ToolError(str(error)) from None


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
