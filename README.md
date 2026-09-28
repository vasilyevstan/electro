# Estonia electricity prices CLI and MCP server

`nordpool_ee.py` is a small, dependency-free command-line tool that prints
Estonia's Nord Pool day-ahead wholesale electricity prices for the next
`Europe/Tallinn` calendar day. `mcp_server.py` exposes validated current-day
and next-day data as typed Model Context Protocol tools over stdio.

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

It exposes three tools:

```text
get_estonia_current_day_prices
get_estonia_next_day_prices
get_estonia_prices_for_hour
```

The current-day and next-day tools take no arguments and return typed
structured content containing:

- the Estonia bidding area and delivery date
- timezone, currency, source, and interval metadata
- every complete 15-minute interval in EUR/MWh and cents/kWh, with and without VAT
- minimum, maximum, and duration-weighted average prices on both VAT bases
- `hourly_averages` for every elapsed hour, with offset-aware start/end times
- the applied `vat_rate_percent` and excluded consumer costs

The current-day response includes `current_interval`, identifying the active
Estonia quarter-hour price, and `current_hour`, containing the full-hour average
at the time of the call. These are different prices: show both and label the
time period and VAT basis. The next-day response returns `null` for both fields.

`get_estonia_prices_for_hour` accepts:

- `delivery_date`: an Estonia delivery date in `YYYY-MM-DD` format
- `hour`: an Estonia local clock hour from `0` through `23`

It returns all quarter-hour prices in that local hour plus the hourly minimum,
maximum, and average on both VAT bases. A repeated daylight-saving hour contains
eight intervals and two separate `hourly_averages`, distinguished by UTC offset.
Its `summary` averages both occurrences for compatibility. Day responses contain
23, 24, or 25 hourly averages; no missing or repeated hour is fabricated or
collapsed. Requesting a skipped hour returns a tool error.

### VAT and price fields

Each interval (including summary minima/maxima and `current_interval`) includes
`excluding_vat` and `including_vat`, each containing `eur_per_mwh` and
`cents_per_kwh`. Summaries and hourly averages use `average_excluding_vat` and
`average_including_vat` with the same units. The MCP text response includes these
same labeled values as its structured response.

For compatibility, existing interval fields `eur_per_mwh` / `cents_per_kwh`
and summary fields `average_eur_per_mwh` / `average_cents_per_kwh` remain
**VAT-exclusive**. The standalone CLI also remains VAT-exclusive.

The MCP adds Estonia's standard **24% VAT** to the wholesale energy component.
This rate has applied since 1 July 2025 and covers the supported 15-minute price
period. Source: [Estonian Tax and Customs Board](https://www.emta.ee/en/business-client/taxes-and-payment/value-added-tax/vat-rates-and-supply-exempt-tax/standard-vat-rate).
Historical hourly-era and incomplete-day support is unchanged.

VAT-inclusive price = VAT-exclusive price multiplied by `1.24`.
Calculations use decimal arithmetic before serialization, without rounding
individual intervals first. Negative and zero spot prices are retained.

For example, the four 23:00-hour prices on 2026-09-28 were 20.01, 8.77, 7.05,
and 6.05 EUR/MWh. The hourly average is 10.47 EUR/MWh:

```json
{
  "vat_rate_percent": 24,
  "average_excluding_vat": {
    "eur_per_mwh": 10.47,
    "cents_per_kwh": 1.047
  },
  "average_including_vat": {
    "eur_per_mwh": 12.9828,
    "cents_per_kwh": 1.29828
  }
}
```

That is approximately **1.30 cents/kWh including VAT for the full hour**, not
the price for its first quarter-hour. `wholesale_only` still denotes the energy
component, not a final consumer tariff. `excluded_costs` now lists only charges
absent from both VAT bases: supplier margin, network charges, excise, and other
consumer fees.

Expected retrieval failures are returned as MCP tool errors, not successful
responses containing error text.

### GitHub Copilot CLI

Register the server in Copilot CLI's user-level configuration so it is
available in every new session:

```bash
copilot mcp add \
  --transport stdio \
  --tools get_estonia_current_day_prices,get_estonia_next_day_prices,get_estonia_prices_for_hour \
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
      "tools": [
        "get_estonia_current_day_prices",
        "get_estonia_next_day_prices",
        "get_estonia_prices_for_hour"
      ]
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
