# Estonia electricity prices CLI and MCP server

`nordpool_ee.py` is a small, dependency-free command-line tool that prints
Estonia's Nord Pool day-ahead wholesale electricity prices for the next
`Europe/Tallinn` calendar day. `mcp_server.py` exposes validated current-day,
next-day and date/hour prices, plus a separate experimental seven-day forecast,
as typed Model Context Protocol tools over stdio.
The separate `elering-kodala-consumption` MCP reads authorized household
electricity consumption and grid export from Elering's customer API.
The separate `electrisity-price` MCP calculates household energy costs and
export credits using an effective-dated private tariff profile.
The separate `sensibo-mcp` reads Sensibo Sky climate telemetry and history,
and controls explicitly selected enrolled devices through Sensibo's cloud API.

Repository: <https://github.com/vasilyevstan/electro>

## Requirements

- Python 3.10 or newer
- Internet access to `dashboard.elering.ee`
- Internet access to `wattcast.eu` for the optional forecast tool (no API key)
- Elering customer API credentials and access to `estfeed.elering.ee` and
  `kc.elering.ee` for household consumption and automatic household pricing;
  pricing supplied readings or individual quantities needs no household credentials
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

## Household electricity pricing MCP

`electrisity-price` is a third **read-only stdio server**. It uses a private
JSON tariff profile, published market prices and either supplied readings or
the existing Elering customer client. It does not alter the other MCPs,
control appliances, use forecasts, learn rates automatically or run a hosted
service.

### Private configuration

Start with [the synthetic example](examples/pricing-profile.example.json),
outside the repository:

```bash
mkdir -p ~/.config/electro/pricing
chmod 700 ~/.config/electro/pricing
cp -n examples/pricing-profile.example.json ~/.config/electro/pricing/profile.json
chmod 600 ~/.config/electro/pricing/profile.json
```

**Replace the invented example rates, tax fractions and dates before using
the profile for real costs.** There is no built-in household tariff or
silent fallback. Keep contracts, invoices, profile history and real replay
fixtures private; only synthetic examples belong in this repository.

```bash
ELECTRICITY_PRICING_PROFILE="$HOME/.config/electro/pricing/profile.json" \
  uv run electrisity-price-mcp
```

The profile specifies EUR, Europe/Tallinn, a version, finite coverage dates,
an explicit rounding policy, source evidence and effective-dated rules.
`from` is inclusive; `until` is exclusive and may be null on an individual
indefinite rule. The overall profile requires a finite coverage end.
Unknown fields, missing sources, rule gaps/overlaps and unsupported structures
are errors. Rate fields are **VAT-exclusive decimal strings**; `vat_rate` is
a fraction, not a percentage. Gross rates are not accepted as a separate
input basis or silently converted.

| Component kind | Meaning of its configured `rate` |
| --- | --- |
| `spot` | EUR/kWh adjustment added to the matching published EUR/MWh price divided by 1000. Import is a cost; export proceeds are negated into customer-cost convention. Use a negative adjustment for a buyback deduction. |
| `unit` | EUR/kWh charge on the selected billable import/export direction, such as excise or balancing fees. |
| `banded` | Net EUR/kWh `band_rates`: `day` and `night`, optionally with both `weekday_peak` and `rest_peak`. |
| `monthly` | Net EUR per calendar month for one stable component/obligation ID. No energy direction. |

The supported network calendar uses Tallinn working weekdays 07:00-22:00 for
day pricing; weekends, statutory holidays and other hours are night.
Configured winter peaks apply November-March: working days 09:00-12:00 and
16:00-20:00; rest days 16:00-20:00. The holiday calendar follows the current
Estonian holiday act, including Good Friday, Easter Sunday and Pentecost;
Easter Monday is not a statutory holiday.

Effective-dated netting rules choose either `gross` or `quarter_hour_net`.
The latter bills `max(import - export, 0)` and `max(export - import, 0)`
**within each quarter**, never across a whole day or month.

`tax_treatment` distinguishes `taxable` from `outside_vat`. Outside-VAT
amounts require a zero VAT fraction but are not labeled zero-rated taxable
sales. Export balancing can therefore be taxable even when the energy credit
is outside VAT. Negative spot prices and negative export proceeds are retained.

### Tools

| Tool | Inputs and behavior |
| --- | --- |
| `quote_electricity_interval` | Offset-aware `start`/`end` for exactly one aligned 15-minute interval, `consumption_kwh` and/or `export_kwh`, and `quantity_basis` (`raw` by default, or `billable`). Returns variable costs before VAT, VAT and after VAT; monthly fees are excluded. |
| `calculate_electricity_period` | `start_date`, optional inclusive `end_date`, optional `intervals`, `quantity_basis`, `mode` (`report` or `monthly`) and optional `include_daily`. Supports complete days, weeks, months and multi-month/year ranges. |
| `estimate_electricity_scenario` | `mode="load"` for constant kW or uniformly distributed hypothetical kWh over a run, or `mode="comparison"` for supplied before/after quarter-hour grid profiles. Returns the incremental variable cost, including network, statutory fees and VAT, but not unchanged monthly fees. |

Quantities are interval **kWh**, not power. They are multiplied by their
matching market price without another duration factor. Decimal strings are
preferred; numeric readings from the consumption MCP are also accepted at
their supplied precision. No measured hourly/monthly quantity is distributed
into invented quarters. Only the scenario tool's explicit hypothetical load
mode allocates energy by a declared constant-power/uniform-energy assumption.
Timestamps require UTC offsets and may not lose sub-microsecond precision.

An interval quote can select just one explicitly billable direction. Raw
quotes require both directions whenever netting applies. Period calculations
require both directions for every interval. Missing/null is never zero.
Already-netted billable inputs are not netted again, and cannot contain two
positive directions in one netting interval.

For example, a supplied-quantity quote uses:

```json
{
  "start": "2026-10-01T22:00:00+03:00",
  "end": "2026-10-01T22:15:00+03:00",
  "consumption_kwh": "2",
  "export_kwh": "0",
  "quantity_basis": "raw"
}
```

To calculate a month from the authorized household automatically:

```json
{
  "start_date": "2026-09-01",
  "end_date": "2026-09-30",
  "mode": "monthly",
  "include_daily": true
}
```

Omitting `intervals` selects automatic retrieval. It reuses the existing
Keychain/environment credentials and single-household checks, requests
quarter-hour readings and bounds upstream requests rather than issuing one
request per quoted interval. The separate consumption MCP retains its own
31-day limit; the pricing server composes bounded requests for longer ranges.
Automatic pricing requires completed past calendar days. Today's unfinished
day, missing readings, ambiguous households, unknown tariffs and unpublished
market prices produce explicit tool errors with no partial total.

Alternatively, supply `intervals` containing the consumption MCP's
`start`, `end`, `consumption_kwh` and `export_kwh` fields. An empty list is
not an automatic-retrieval request. Caller-provided quantities are identified
as such, including hypothetical future quantities when the market prices
have actually been published.

### Appliance estimates and before/after scenarios

For a **hypothetical all-grid load**, call `estimate_electricity_scenario`
with an offset-aware start, positive duration in minutes, and exactly one of
`power_kw` or `energy_kwh`:

```json
{
  "mode": "load",
  "start": "2026-08-03T10:07:30+03:00",
  "duration_minutes": "60",
  "power_kw": "2"
}
```

The example represents a constant 2 kW load for one hour, or 2 kWh.
Alternatively, replace `power_kw` with `"energy_kwh": "2"` to explicitly
spread that hypothetical energy evenly across the run. These are scenario
assumptions, **not measurements or inferred appliance consumption**.
All energy in this mode is additional **billable grid import**; it does not
assume the rest of the household consumes zero. Omit `quantity_basis` and
both interval profiles in load mode. Zero power/energy is valid; zero or
negative duration is not.

Partial first/last market quarters are allocated by real elapsed overlap.
UTC arithmetic preserves duration through daylight-saving changes, while
network bands and displayed times use Tallinn time. Derived energy uses at
most 24 significant digits and 18 decimal places; cumulative allocation
preserves the represented total without accumulating rounding drift.
Durations must resolve to whole microseconds and stay within profile coverage.

For **solar/battery-aware pricing**, supply complete before-and-after grid
profiles instead. The following synthetic example reduces export and
increases import:

```json
{
  "mode": "comparison",
  "quantity_basis": "raw",
  "baseline_intervals": [
    {
      "start": "2026-08-03T10:00:00+03:00",
      "end": "2026-08-03T10:15:00+03:00",
      "consumption_kwh": "0",
      "export_kwh": "2"
    }
  ],
  "scenario_intervals": [
    {
      "start": "2026-08-03T10:00:00+03:00",
      "end": "2026-08-03T10:15:00+03:00",
      "consumption_kwh": "1",
      "export_kwh": "0"
    }
  ]
}
```

Comparison mode takes no load-mode start, duration, power or energy argument.
Both profiles need known import **and** export in every aligned 15-minute
interval over the same window; their bounds define the comparison period.
`quantity_basis` defaults to `raw` and applies to both sides. Historical
gross-flow or quarter-hour netting rules are applied separately to each
profile; explicitly billable inputs are not netted again.

The tool prices **scenario minus baseline**, including lost export credits,
changes in import/export balancing, transmission, statutory fees, excise
and each component's VAT treatment. A negative result is a saving, not an
error. It does not infer solar output, battery state, dispatch or efficiency.
Recharge energy, battery losses and later export changes count only when
represented in the supplied comparison window. Data outside that window is
not silently assumed free or included. Use the existing consumption MCP to
obtain actual baseline readings when appropriate; this tool never retrieves
or substitutes household readings automatically.

In both modes, top-level `amounts`, `lines` and `vat_groups` describe the
**unrounded incremental cost**, not the whole household's bill. `baseline`
(null for a load estimate) and `scenario` show the separately priced variable
costs and quantities. `start`/`end` describe the requested run or comparison;
`accounting_start`/`accounting_end` describe the containing full quarters.
`energy_allocation` records the active overlap and kWh in each load quarter;
`energy_kwh` is null in comparison mode because grid differences do not
establish appliance energy use.

Unchanged monthly fees are excluded. Prices and the profile are shared
across both sides from one request snapshot, with source, version/hash and
invoice-derived qualifications retained. Unpublished prices, missing rates,
incomplete/mismatched profiles or mixed input modes fail explicitly.
There are no forecasts, appliance controls or silent missing-as-zero defaults.
An estimate is not an exact difference between two cent-rounded monthly
invoices; use the existing monthly calculation for complete energy bills.

### Reporting versus invoice arithmetic

`report` returns **unrounded** variable amounts plus calendar-day allocation
of fixed fees within each covered month. This is suitable for days, weeks
and partial months; it is not a standalone supplier bill.

`monthly` requires complete calendar months. It sums unrounded interval
contributions into monthly component lines, rounds those lines, and calculates
and rounds VAT separately per month, contract section and VAT rate. An
annual total sums these finalized monthly results; it does not reround one
annual taxable base.

Each monthly fee is charged once per stable component ID, even if unchanged
financial rules have different source versions. A fixed-fee amount or tax
change inside a billing month is rejected in monthly mode because actual
partial-contract proration is not configured; an explicitly allocated report
remains available. Calendar-day report allocation must not be mistaken for
a supplier's contractual partial-period billing rule.

`include_daily=true` adds analytical daily allocations even in monthly mode.
**Summing rounded displayed days or interval quotes is not an exact monthly
invoice reconstruction.** For composition, supply the underlying interval
readings instead.

Results include exact decimal-string amounts, separate taxable net and
outside-VAT amounts, component lines, VAT groups, raw/billable quantities,
monthly summaries and optional daily allocations. Finalized monthly amounts
have two decimals. Every result includes profile version/hash, source IDs and
evidence basis, input provenance and warnings. Rates based on invoices stay
labeled `invoice_derived`; listing reference invoice months never implies
that a different input or an unbilled period has been invoice-verified.
Complete metering coverage also does not establish final settlement.

`ROUND_HALF_UP` and `ROUND_HALF_EVEN` are explicit profile choices. Preserve
the supplier's demonstrated rounding stages and document any unproven tie
policy instead of silently using the Decimal default.

Totals represent energy costs and credits, not an account ledger. Interest,
previous balances, payments and amount due are excluded. Reconcile such items
separately when comparing a complete invoice.

### Updating rates and retaining evidence

Document any accepted contract-versus-invoice discrepancy alongside the
private profile, including the written rule, working rule, checked periods
and unresolved qualifications. Preserve repeatable private comparisons of
line amounts, quantities and section VAT. If a future invoice differs, inspect
those differences before changing the profile; do not fit rates automatically.

Append effective-dated changes, retain prior amounts and sources, increment
the profile version and rerun historical fixtures plus the new case. The MCP
loads one immutable profile snapshot per call, so edits take effect on the
next call without a restart or mixed old/new rates. Its hash is provenance,
not a billing-line or monthly-fee occurrence ID.

### Register the pricing MCP

Add an entry without replacing existing servers:

```json
{
  "mcpServers": {
    "electrisity-price": {
      "type": "stdio",
      "command": "uvx",
      "args": [
        "--from",
        "git+https://github.com/vasilyevstan/electro.git@<tested-commit-sha>",
        "electrisity-price-mcp"
      ],
      "env": {
        "ELECTRICITY_PRICING_PROFILE": "~/.config/electro/pricing/profile.json"
      },
      "tools": [
        "quote_electricity_interval",
        "calculate_electricity_period",
        "estimate_electricity_scenario"
      ],
      "timeout": 600000
    }
  }
}
```

Only the profile path belongs in MCP configuration, never its rates, household
identifiers or credentials. This is still a local process: GitHub publishes the
generic executable code but does not host the private profile or household API.

## Sensibo climate MCP

`sensibo-mcp` is a separate stdio server for Sensibo Sky (`skyv2`) controllers.
It uses the official **cloud API**, not a local device API. Internet access to
`home.sensibo.com` and a Sensibo API key are required. Devices are addressed by
stable device IDs, with verified MACs retained privately; DHCP/IP changes do
not affect control. Other device models are rejected during enrollment rather
than assigned guessed capabilities.

### Credentials and explicit enrollment

Generate a key at <https://home.sensibo.com/me/api> while signed into the account
containing the intended devices. Do not paste it into chat, source files, MCP
configuration, or command arguments. If a key has been exposed, rotate it.

On macOS, store it interactively, with `-w` last:

```bash
security add-generic-password -s sensibo-mcp -a api_key -w
```

Alternatively, supply `SENSIBO_API_KEY` securely through the server's process
environment. An explicit environment value takes precedence; empty or
whitespace-containing values fail rather than falling back to Keychain.
GitHub secret `SENSIBO_API_KEY` can retain a separate encrypted copy, but a local
MCP **cannot read secret values back from GitHub**.

From a checkout, enroll the account only after confirming the expected count:

```bash
uv run --locked python -m sensibo --enroll-account --expected-devices 4
```

This is explicit account enrollment, not an HVAC command. It validates the
count, model, and unique device-ID/MAC associations before atomically writing
`~/.config/electro/sensibo/devices.json` (directory `700`, file `600`).
Previously enrolled devices are retained when absent from the account.
Conflicting identities fail rather than being reassigned. New account devices
are never silently authorized for control.

To retain the verified inventory in a GitHub repository secret:

```bash
gh secret set SENSIBO_DEVICES --repo vasilyevstan/electro \
  < ~/.config/electro/sensibo/devices.json
```

The local inventory remains necessary; GitHub is not a runtime secret-fetch
service. Neither credentials nor real device inventory belong in commits,
examples, fixtures, or public logs.

### Tools, readings, and control semantics

| Tool | Behavior |
| --- | --- |
| `list_sensibo_devices()` | Enrolled IDs/MACs, connectivity, and mode-specific capabilities; missing devices remain visible and new account devices are counted but not enrolled |
| `get_sensibo_device(device_id)` | Cloud-reported AC state, measured temperature/humidity, derived feels-like temperature, RSSI, timestamps, and freshness |
| `get_sensibo_measurements(device_id, days=1)` | Provider-available historical samples for 1-7 days, UTC timestamps, actual coverage, and nulls/gaps without interpolation |
| `set_sensibo_ac_state(device_id, ...)` | One explicit desired power/mode/temperature/fan/swing update after fresh identity, connectivity, capability, and policy checks |

Current account reads share a **60-second on-demand cache**. Sensor timestamps
remain the original measurement times; cache age and sensor age are different.
Samples older than ten minutes are marked stale. Unknown timestamps, offline
status, missing readings, and null values are explicit, never fabricated zeros.
Sensor temperatures and feels-like values are **Celsius**, independently of the
AC setpoint's C/F unit. Feels-like temperature is provider-derived; RSSI is
Wi-Fi signal strength in dBm. This version does not infer power consumption,
air quality, or occupancy from Sky readings.

History has a separate short cache and retains its original requested window
and retrieval time. The seven-day request bound is an application limit, not a
promise of provider retention, complete samples, or uninterrupted recording.
There is no background collector, database, subscription, or schedule.

Control accepts `on`, `mode`, `target_temperature`, `temperature_unit`,
`fan_level`, `swing`, and `horizontal_swing`. Provide temperature and its unit
together; allowed values come from that device's selected mode, not a global
list. For example, a dry mode may have no fan-level control. Unspecified
settings are not sent, and incompatible retained settings require an explicit
choice instead of a silent reset. Account/device restrictions remain enforced.

Commands are serialized in this MCP process, but other apps/controllers can
still act independently. Every explicit command can emit infrared, including
a repeated desired state. There are no toggle or bulk-control tools and no
automatic write retries. A timeout, rejected command, or unsuccessful read-back
returns a tool error explaining that the outcome may be uncertain; read the
state before deciding whether another command is needed.

A successful result distinguishes `api_acknowledged` and
`cloud_state_matches` from `physical_effect_verified=false`. Neither cloud
state nor an accepted IR command proves that the HVAC unit received it or that
its compressor is running. Never test physical switching without selecting an
authorized device and explicit safe command.

### Launching and registering Sensibo

From a checkout:

```bash
uv run --locked sensibo-mcp
```

From the published repository:

```bash
uvx --from "git+https://github.com/vasilyevstan/electro.git@main" sensibo-mcp
```

Add a separate entry to the existing user-level `~/.copilot/mcp-config.json`,
preserving its other servers. Pin `@main` to a published commit SHA when a
reproducible launch is required. No credentials are embedded in this entry:

```json
{
  "mcpServers": {
    "sensibo-mcp": {
      "type": "stdio",
      "command": "uvx",
      "args": [
        "--from",
        "git+https://github.com/vasilyevstan/electro.git@main",
        "sensibo-mcp"
      ],
      "tools": [
        "list_sensibo_devices",
        "get_sensibo_device",
        "get_sensibo_measurements",
        "set_sensibo_ac_state"
      ],
      "timeout": 120000
    }
  }
}
```

Run deterministic, synthetic tests without a real account or physical writes:

```bash
uv run --locked python -m unittest discover -s tests -p 'test_sensibo*.py' -v
```

References: [official Sensibo API](https://support.sensibo.com/sensibo.openapi.yaml)
and [Home Assistant's Sensibo integration](https://www.home-assistant.io/integrations/sensibo/).

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
  elering_consumption.py elering_consumption_mcp.py electricity_pricing.py \
  electricity_pricing_mcp.py
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

Pricing tests cover synthetic profile validation, interval weighting, raw/net
flows, holiday/time-band and DST boundaries, negative prices, source status,
rounding stages/ties, fixed-fee identity, daily allocations, annual/monthly
composition, bounded market batches and both MCP input modes. Real invoice
replays belong outside the public repository and CI logs.

## License

The source code is available under the [MIT License](LICENSE). The license does
not cover Nord Pool, Elering or Wattcast data, trademarks, or third-party content.
