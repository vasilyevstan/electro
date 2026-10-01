"""Separate local MCP for MAC-addressed Tapo sockets."""

import sys
from typing import Annotated

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from tapo_plugs import PlugInventory, PlugState, PowerResult, TapoClient, TapoError, ensure_runtime


mcp = MCPServer(
    "tp-tapo-mcp",
    instructions=(
        "Discover local Tapo sockets and identify them by MAC, never by an old IP "
        "or ambiguous alias. Read relay state and supported measurements; when "
        "on, report measured consumption. Watts are instantaneous power; kWh "
        "counters are energy over their labeled device-local periods. An on "
        "socket can draw zero watts. Missing/unsupported values are not zero, "
        "and a socket not seen in discovery is not necessarily off. Show "
        "unavailable readings, cached metadata, and uncertain control outcomes "
        "explicitly. Only set power for the specific MAC and desired state "
        "requested by the user. Never toggle, perform unsolicited relay tests, "
        "or switch a group of loads. Credentials belong in local Keychain or "
        "the process environment, never in tool arguments or output."
    ),
)
_client = TapoClient()
_READ_ONLY = ToolAnnotations(
    read_only_hint=True, destructive_hint=False,
    idempotent_hint=True, open_world_hint=True,
)
_WRITE = ToolAnnotations(
    read_only_hint=False, destructive_hint=True,
    idempotent_hint=True, open_world_hint=True,
)


@mcp.tool(title="List local Tapo sockets by MAC", annotations=_READ_ONLY)
async def list_tapo_plugs(refresh: Annotated[bool, Field(strict=True)] = True) -> PlugInventory:
    """Discover Tapo sockets and retain known but unseen MACs in private inventory.

    No credentials or relay commands are used. refresh=false returns cached
    metadata only. IPs, labels, and capabilities can be historical; inspect
    last_seen and capabilities_verified_at. Unsupported protocols remain visible.
    """
    try:
        return await _client.list_plugs(refresh)
    except TapoError as error:
        raise ToolError(str(error)) from None


@mcp.tool(title="Read a Tapo socket's state and consumption", annotations=_READ_ONLY)
async def get_tapo_plug(mac: str) -> PlugState:
    """Read one exact MAC's relay state and supported electrical/operating readings.

    Resolve changing IPs and verify authenticated identity. Include measured
    consumption when on; never estimate watts from relay state. Distinguish
    unsupported/unavailable readings from real zeros. Energy counters retain
    their device-local periods even when the relay is off.
    """
    try:
        return await _client.get_plug(mac)
    except TapoError as error:
        raise ToolError(str(error)) from None


@mcp.tool(title="Set one Tapo socket's power explicitly", annotations=_WRITE)
async def set_tapo_plug_power(mac: str, on: Annotated[bool, Field(strict=True)]) -> PowerResult:
    """Set a specifically requested MAC on or off and verify the resulting state.

    This changes a physical load and can interrupt appliances. Never use for
    automatic testing, bulk switching, or an ambiguous target. Setting an
    already matching state sends no relay command. Return fresh available
    consumption with the verified state; report uncertain outcomes as errors
    rather than assuming success or automatically retrying the command.
    """
    try:
        return await _client.set_power(mac, on)
    except TapoError as error:
        raise ToolError(str(error)) from None


def main() -> None:
    try:
        ensure_runtime()
    except TapoError as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(2) from None
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
