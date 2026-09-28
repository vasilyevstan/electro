# Estonia next-day electricity prices CLI and MCP server

`nordpool_ee.py` is a small, dependency-free command-line tool that prints
Estonia's Nord Pool day-ahead wholesale electricity prices for the next
`Europe/Tallinn` calendar day. `mcp_server.py` exposes the same validated data
as a typed Model Context Protocol tool over stdio.

Repository: <https://github.com/vasilyevstan/electro>

## Requirements

- Python 3.10 or newer
- Internet access to `dashboard.elering.ee`
- [`uv`](https://docs.astral.sh/uv/)

## Setup

Clone the repository and install its locked dependencies:

```bash
git clone https://github.com/vasilyevstan/electro.git
cd electro
uv sync --locked
```

## CLI usage

From a checkout:

```bash
uv run nordpool-ee
```

Without a checkout:

```bash
uvx --from "git+https://github.com/vasilyevstan/electro.git@main" nordpool-ee
```

The output contains every published market interval in Tallinn local time,
followed by the minimum, maximum, and duration-weighted average. Each price is
shown in:

- `EUR/MWh`, the unit returned by Elering
- euro `cents/kWh`, calculated as `EUR/MWh / 10`

These are wholesale energy prices only. VAT, electricity supplier margin,
network charges, excise, and other consumer costs are not included.

Day-ahead prices are normally published during the afternoon before delivery.
If tomorrow's prices are not available yet, the command reports that explicitly
and exits with a non-zero status instead of showing today's or partial data.

Exit statuses:

- `0`: complete next-day data was printed
- `2`: next-day prices have not been published
- `3`: Elering could not be reached successfully
- `4`: Elering returned invalid or incomplete data

## MCP server

Start the local stdio server from a checkout:

```bash
uv run nordpool-ee-mcp
```

Or launch it directly from GitHub:

```bash
uvx --from "git+https://github.com/vasilyevstan/electro.git@main" \
  nordpool-ee-mcp
```

It exposes one tool:

```text
get_estonia_next_day_prices
```

The tool takes no arguments and returns typed structured content containing:

- the Estonia bidding area and delivery date
- timezone, currency, source, and interval metadata
- every complete 15-minute interval in EUR/MWh and cents/kWh
- minimum, maximum, and duration-weighted average prices
- an explicit wholesale-only flag and excluded consumer costs

Expected retrieval failures are returned as MCP tool errors, not successful
responses containing error text.

### GitHub Copilot CLI

Register the server in Copilot CLI's user-level configuration so it is
available in every new session:

```bash
copilot mcp add \
  --transport stdio \
  --tools get_estonia_next_day_prices \
  electro -- \
  uvx --from "git+https://github.com/vasilyevstan/electro.git@main" \
  nordpool-ee-mcp
```

For an immutable installation, replace `@main` with `@<commit-sha>`.

The equivalent `~/.copilot/mcp-config.json` entry is:

```json
{
  "mcpServers": {
    "electro": {
      "type": "stdio",
      "command": "uvx",
      "args": [
        "--from",
        "git+https://github.com/vasilyevstan/electro.git@main",
        "nordpool-ee-mcp"
      ],
      "tools": ["get_estonia_next_day_prices"]
    }
  }
}
```

Copilot CLI inherits `PATH` for local MCP servers, so `uvx` must be available
on `PATH`. The server writes only MCP protocol messages to stdout, as required
for stdio transport.

## Data source

The tool queries Elering, Estonia's transmission system operator:

```text
https://dashboard.elering.ee/api/nps/price
```

The Estonia series is returned as `data.ee` in EUR/MWh. Elering explains that
Nord Pool day-ahead prices use 15-minute market periods from September 30,
2025:

- <https://dashboard.elering.ee/assets/api-doc.html>
- <https://www.elering.ee/en/transition-15-minute-market-time-unit>

The implementation calculates the requested day in `Europe/Tallinn` and does
not assume exactly 96 intervals. Daylight-saving transitions produce 92 or 100
quarter-hour intervals.

Nord Pool applies separate terms to customer-facing display and data
redistribution. Publishing this source code under the MIT License does not
grant rights to redistribute upstream market data. Review the applicable terms
before exposing price data through a public website or API:

- <https://www.nordpoolgroup.com/en/services/power-market-data-services/faq/faq-premium-container/redistribution/>

## Validation

Run the deterministic test suite without live network access:

```bash
uv run python -m unittest discover -s tests -v
```

Check syntax:

```bash
uv run python -m py_compile nordpool_ee.py mcp_server.py
```

Run the CLI for a live next-day source check:

```bash
uv run nordpool-ee
```

## License

The source code is available under the [MIT License](LICENSE). The license does
not cover Nord Pool or Elering data, trademarks, or third-party content.
