# Estonia electricity prices CLI and MCP server

`nordpool_ee.py` is a small, dependency-free command-line tool that prints
Estonia's Nord Pool day-ahead wholesale electricity prices for the next
`Europe/Tallinn` calendar day. `mcp_server.py` exposes validated current-day,
next-day and date/hour prices, plus a separate experimental seven-day forecast,
as typed Model Context Protocol tools over stdio.
The separate `elering-kodala-consumption` MCP reads authorized household
electricity consumption and grid export from Elering's customer API.

Repository: <https://github.com/vasilyevstan/electro>

## Requirements

- Python 3.10 or newer
- Internet access to `dashboard.elering.ee`
- Internet access to `wattcast.eu` for the optional forecast tool (no API key)
- Elering customer API credentials and access to `estfeed.elering.ee` and
  `kc.elering.ee` for the separate household-consumption MCP only
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

## Price and forecast MCP server

Start the local stdio server from a checkout:

```bash
uv run nordpool-ee-mcp
```

Or launch it directly from GitHub:

```bash
uvx --from "git+https://github.com/vasilyevstan/electro.git@main" \
  nordpool-ee-mcp
```

It exposes four tools:

```text
get_estonia_current_day_prices
get_estonia_next_day_prices
get_estonia_prices_for_hour
get_estonia_price_forecast
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

### Seven-day forecast (experimental)

`get_estonia_price_forecast()` takes no arguments and uses
[Wattcast's free API](https://wattcast.eu/api). It returns the **next seven full
Estonia calendar days, starting tomorrow**, not a rolling 168-hour period.
A DST transition can make this window 167 or 169 elapsed hours.
The three published-price tools and the standalone CLI continue to use
Elering unchanged.

The tool requests the raw model (`adjusted=false`), without Wattcast's online
level or written-outlook adjustments. It requests a bounded upstream window
and trims by actual timestamps; it never invents missing prices.

| Field | Meaning |
| --- | --- |
| `hourly_prices[].published_price` | Already-published hourly price, relayed through Wattcast from Elering; `prediction` is null |
| `hourly_prices[].prediction` | Hourly `p10`, `p50`, `p90` and provider `horizon_days`; `published_price` is null |
| `basis` | Explicit `published` or `forecast` classification for each hour |
| `daily_summaries` | All seven dates, counts/completeness, minimum/maximum hours, and `average_available_hourly_price` |
| `window_start`, `window_end` | Requested local-time bounds; end is exclusive |
| `available_start`, `available_end`, `missing_hours`, `complete` | Actual coverage, including any gaps |
| `issued_at`, `retrieved_at`, `age_seconds`, `stale` | Model issuance, HTTP retrieval time, and freshness at the call |
| `accuracy` | Separately labeled provider-reported raw live errors and backtests |
| `warnings` | Limitations and any partial, stale, missing-accuracy or undercoverage warnings; display these prominently |

Every published price and forecast quantile contains `excluding_vat` and
`including_vat`, each with `eur_per_mwh` and `cents_per_kwh`. Net values come
from the provider's EUR/MWh fields; cents/kWh and **24% VAT** are calculated
locally. Supplier margin, network charges, excise and other fees remain excluded.
Hour start/end timestamps include Estonia's UTC offset, including both
occurrences of a repeated DST hour.

`p50` is the hourly central median, not an auction result. Daily summaries
average the available hourly point values: published prices where known,
otherwise p50. A mixed day is labeled `mixed`. This average is not necessarily
a daily median or expected price, and **no daily 80% probability band is
inferred by averaging hourly bounds**. Empty dates have null statistics.

Valid partial data is returned with `complete=false`, the missing hours, and
warnings. A forecast issued more than **three hours** ago has `stale=true`.
Both fixed upstream requests are cached in-process for one hour; cached calls
retain original retrieval/issuance timestamps and recalculate freshness and
the requested calendar window. There is no background polling or disk cache.
If the forecast is invalid, unreachable, or has no usable predicted hours in
the requested window, the tool returns an MCP error. An accuracy-only failure
does not hide usable forecasts: `accuracy.status` becomes `unavailable`, with
an explicit reason and warning.

#### How much confidence to place in it

Wattcast's p10-p90 band has a **nominal 80% target**, not guaranteed coverage.
The tool returns the provider's observed raw-model coverage and sample count
by horizon, rather than inventing an accuracy percentage. `horizon_days` means
days after the last settled local day, **not** exactly that many 24-hour periods
after issuance.

The [EE accuracy API](https://wattcast.eu/v1/accuracy?zone=EE&days=14) supplies
live metrics over a reported 14-day window and separate cross-validation
results. MAE is mean absolute hourly error, not a maximum error bound.
VAT-inclusive MAE is the net monetary error scaled by 1.24. Do not compare
live error with a backtest baseline to claim live improvement.

In the research snapshot on 2026-09-28, raw live MAE ranged from
33.51 to 47.09 EUR/MWh across horizons, and nominal-80% band coverage ranged
from 62.8% to 79.2%. The horizon-7 MAE was 44.50 EUR/MWh, equivalent to
4.45 cents/kWh excluding VAT or 5.518 including VAT. These are
**provider-reported observations, not an independent audit or a promise of
future performance**; the tool retrieves current metrics rather than using
these fixed examples.

Use the forecast to compare likely cheaper days and trends; use published
prices to refine exact hours once available. No device-control or trading
automation is included.

The API is free without a key at 60 requests/minute/IP, returns HTTP 429 with
Retry-After on rate limiting, and has no SLA. Display attribution with results:
**Wattcast; prices from Elering (Nord Pool day-ahead); weather from
Open-Meteo.com (CC BY 4.0)**. See the [API terms](https://wattcast.eu/legal#api).

### GitHub Copilot CLI

Register the server in Copilot CLI's user-level configuration so it is
available in every new session:

```bash
copilot mcp add \
  --transport stdio \
  --tools get_estonia_current_day_prices,get_estonia_next_day_prices,get_estonia_prices_for_hour,get_estonia_price_forecast \
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
        "get_estonia_prices_for_hour",
        "get_estonia_price_forecast"
      ]
    }
  }
}
```

Copilot CLI inherits `PATH` for local MCP servers, so `uvx` must be available
on `PATH`. The server writes only MCP protocol messages to stdout, as required
for stdio transport.

## Household consumption MCP

`elering-kodala-consumption` is a **separate, read-only MCP server** using the
[Elering customer portal](https://estfeed.elering.ee/), not the public price
API. It does not change the four price/forecast tools or the standalone CLI.

Run from a checkout:

```bash
uv run elering-kodala-consumption-mcp
```

Or from GitHub:

```bash
uvx --from "git+https://github.com/vasilyevstan/electro.git@main" \
  elering-kodala-consumption-mcp
```

It exposes only these two tools, annotated read-only:

| Tool | Inputs and result |
| --- | --- |
| `list_household_metering_points` | Optional `start_date` and inclusive `end_date`; defaults to the latest 31 Tallinn calendar days including today. Returns authorized electricity EICs and access periods, not profile data. |
| `get_household_consumption` | Required `start_date`, optional inclusive `end_date` (one day by default), and `resolution`: `hourly` (default) or `15_minutes`. Returns interval import/export and daily/monthly summaries. |

Dates must use `YYYY-MM-DD`, must not be in the future, and each call covers at
most **31 calendar days**. Query successive ranges for longer history.
Metering-point discovery is cached in memory for five minutes for the same
date window; measurements are fetched on demand, without a disk cache.
The data tool requires exactly one authorized electricity metering point in
the requested period. It does not guess between multiple points or aggregate
different households.

### Energy quantities and completeness

Values are **kWh**, not kW, prices or bills. `consumption_kwh` means grid
import; `export_kwh` is the provider's `productionKwh` reading at the grid
connection, not necessarily all solar generation. VAT does not apply to
energy quantities.

The result includes offset-aware Estonia timestamps, requested bounds,
retrieval time, the latest returned reading within that window, intervals,
and separate consumption/export summaries. kWh values are summed with Decimal
arithmetic, not averaged or duration-weighted a second time. DST days have
23/24/25 hours or 92/96/100 quarter-hours.

Each direction's summary reports `total_kwh`, `known_intervals`,
`missing_elapsed_intervals`, `complete`, and `elapsed_complete`. Nulls and
missing intervals are **not zero**; a numeric zero remains zero. Totals cover
known readings and are explicitly partial when readings are absent. An
entirely missing direction has a null total. An entirely unavailable window
returns an MCP error, not a zero-consumption result.

Only completed accounting intervals are returned and summed. Today's running
interval and future intervals within today are listed as `pending_intervals`,
not missing elapsed data. A current-day result is therefore not a final
full-day total. `complete` requires the entire requested period;
`elapsed_complete` refers only to intervals that have finished.

Daily and monthly summaries retain their actual start/end bounds.
`covers_full_calendar_period=false` identifies a requested slice of a month,
not the full month's consumption. Coverage is not a guarantee of settlement:
the supported API schema has no finality flag, and readings can arrive late
or be revised. Historical readings are not labeled stale just because the
requested period is old. This is **not real-time or per-appliance telemetry**.

### Credentials

Create customer API credentials through Elering's portal and authorize only the
intended household. Do not put credentials in source, issue comments, tool
arguments, `.env` files committed to Git, or MCP configuration.

On macOS, store the pair in the login Keychain using these interactive commands.
Keep `-w` as the final argument so `security` prompts instead of putting the
credential value in command history or process arguments:

```bash
security add-generic-password \
  -s elering-kodala-consumption -a client_id -w
security add-generic-password \
  -s elering-kodala-consumption -a client_secret -w
```

The server reads these entries without printing them. Missing, empty, locked
or inaccessible Keychain entries produce explicit errors. Do not use
`security`'s unrestricted `-A` access option.

Alternatively, provide **both** `ELERING_CLIENT_ID` and `ELERING_CLIENT_SECRET`
through the process environment using your own secure credential mechanism.
An explicit environment pair takes precedence over Keychain; a partial or
empty pair fails instead of mixing credential sources. Non-macOS hosts need
the environment pair.

GitHub Actions secrets with these same names can store credentials for
separately authorized workflows, but **a local MCP cannot read their values
back from GitHub**. The Keychain/environment credentials are a separate runtime
source. This repository does not add a workflow that uses household secrets.

OAuth uses the official `elering-sso` client-credentials endpoint. Access tokens
are held only in memory and refreshed before expiry. Requests are paced at
least five seconds apart in this process; rate-limit retries are bounded and
respect Retry-After. Other applications/sessions sharing the same key can
still exhaust its limit. Unexpected redirects are rejected, and private
response bodies and tokens are excluded from errors.

### Register the additional MCP

Add this entry to your existing user-level MCP configuration; do not replace
other server entries. The Mac uses Keychain, so this example contains no
credentials:

```json
{
  "mcpServers": {
    "elering-kodala-consumption": {
      "type": "stdio",
      "command": "uvx",
      "args": [
        "--from",
        "git+https://github.com/vasilyevstan/electro.git@main",
        "elering-kodala-consumption-mcp"
      ],
      "tools": [
        "list_household_metering_points",
        "get_household_consumption"
      ],
      "timeout": 120000
    }
  }
}
```

Replace `@main` with a tested commit SHA for an immutable installation.
Keep household EICs, readings, credentials and private API captures out of
public fixtures, documentation and CI logs. Deterministic tests use synthetic
data; live account checks belong on the authorized local machine.

## Data source

The published-price tools query Elering, Estonia's transmission system operator:

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
uv run python -m py_compile nordpool_ee.py mcp_server.py wattcast_forecast.py \
  elering_consumption.py elering_consumption_mcp.py
```

Run the CLI for a live next-day source check:

```bash
uv run nordpool-ee
```

The deterministic suite also covers forecast schema/transport/cache errors,
quantile and VAT arithmetic, partial/stale data, missing accuracy, DST windows,
and MCP text/structured output agreement. Live integration checks must compare
the same forecast issuance, distinguish published from predicted hours, and
report actual coverage and freshness. Faithful reproduction of provider
metrics does not independently validate future predictive accuracy.

Household tests cover credential loading and redaction, OAuth expiry/retries,
rate limits, private upstream errors, date/DST boundaries, null-versus-zero
readings, independent import/export completeness and MCP output. Live checks
should compare the same authorized meter/date window and resolution to the
source, without publishing private response data or claiming independent
verification of the physical meter.

## License

The source code is available under the [MIT License](LICENSE). The license does
not cover Nord Pool, Elering or Wattcast data, trademarks, or third-party content.
