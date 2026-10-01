"""Read-only household electricity pricing with a private tariff profile."""

import os
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Dict, List, Optional, Union

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import StrictFloat, StrictInt, StrictStr

from elering_consumption import API_BASE, ConsumptionError, DateWindow, EstfeedClient
from electricity_pricing import (
    EnergyInterval,
    PeriodMode,
    PricingError,
    PricingProfile,
    PricingResult,
    QuantityBasis,
    load_profile,
    make_interval,
    period_bounds,
    price_period,
    quote_interval,
    validate_intervals,
)
from nordpool_ee import API_URL, MARKET_INTERVAL, TALLINN, UTC, PriceError, fetch_prices_range


EnergyNumber = Union[StrictStr, StrictFloat, StrictInt]


@dataclass(frozen=True)
class IntervalInput:
    start: str
    end: str
    consumption_kwh: Optional[EnergyNumber] = None
    export_kwh: Optional[EnergyNumber] = None


mcp = MCPServer(
    "electrisity-price",
    instructions=(
        "Calculate EUR electricity costs and export credits using the configured "
        "private tariff history and published market prices. Show excluding-VAT, "
        "VAT and including-VAT amounts separately; outside-VAT export is not a "
        "zero-rated taxable sale. Positive amounts are costs, negative amounts "
        "are credits. Display invoice-derived rate qualifications and profile "
        "version/hash. A calculated total is not a supplier invoice or account "
        "balance. Use actual offset-aware 15-minute delivery intervals; never "
        "distribute hourly/monthly kWh into invented quarters. Keep raw and "
        "billable quantities distinct. Report mode allocates fixed fees by "
        "calendar day and returns unrounded amounts. Monthly mode finalizes "
        "component lines and section VAT separately for each complete month. "
        "Do not sum rounded daily quotes to claim an exact monthly bill. "
        "Missing rates, readings and unpublished prices are errors, not zero. "
        "No forecasts, account payments, interest or appliance control."
    ),
)
_client = EstfeedClient()
_READ_ONLY = ToolAnnotations(
    read_only_hint=True, destructive_hint=False,
    idempotent_hint=True, open_world_hint=True,
)


def tallinn_now() -> datetime:
    return datetime.now(TALLINN)


def _profile() -> PricingProfile:
    value = os.environ.get("ELECTRICITY_PRICING_PROFILE", "")
    if not value.strip():
        raise PricingError("Set ELECTRICITY_PRICING_PROFILE to an absolute private JSON profile path.")
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise PricingError("ELECTRICITY_PRICING_PROFILE must be an absolute path.")
    return load_profile(path)


def _market_prices(first: date, last: date) -> Dict[datetime, Decimal]:
    prices = {}
    current = first
    while current <= last:
        stop = min(current + timedelta(days=30), last)
        reports = fetch_prices_range(current, stop)
        for report in reports:
            for interval in report.intervals:
                timestamp = interval.start.astimezone(UTC)
                if timestamp in prices:
                    raise PricingError("Duplicate market price across range batches.")
                prices[timestamp] = interval.price_eur_mwh
        current = stop + timedelta(days=1)
    return prices


@mcp.tool(title="Quote household electricity for a delivery interval", annotations=_READ_ONLY)
def quote_electricity_interval(
    start: str,
    end: str,
    consumption_kwh: Optional[EnergyNumber] = None,
    export_kwh: Optional[EnergyNumber] = None,
    quantity_basis: QuantityBasis = "raw",
) -> PricingResult:
    """Price one aligned 15-minute interval; timestamps must contain UTC offsets.

    Quantities are interval kWh, preferably decimal strings. Supply at least
    one direction. Raw quantities need both directions when netting applies;
    a single direction can instead be explicitly declared billable. Monthly
    fixed fees are excluded. Published market prices are fetched automatically.
    """
    try:
        profile = _profile()
        interval = make_interval(start, end, consumption_kwh, export_kwh)
        if interval.start.timestamp() % 900 or interval.end - interval.start != MARKET_INTERVAL:
            raise PricingError("Quote exactly one aligned 15-minute delivery interval.")
        validate_intervals(profile, [interval], quantity_basis, interval.start, interval.end, True)
        delivery_date = interval.start.astimezone(TALLINN).date()
        prices = _market_prices(delivery_date, delivery_date)
        result = quote_interval(profile, interval, prices, quantity_basis)
        return replace(
            result, price_source=API_URL, prices_retrieved_at=tallinn_now().isoformat(),
        )
    except (PricingError, ConsumptionError, PriceError) as error:
        raise ToolError(str(error)) from None


@mcp.tool(title="Calculate household electricity costs for a period", annotations=_READ_ONLY)
def calculate_electricity_period(
    start_date: str,
    end_date: Optional[str] = None,
    intervals: Optional[List[IntervalInput]] = None,
    quantity_basis: QuantityBasis = "raw",
    mode: PeriodMode = "report",
    include_daily: bool = False,
) -> PricingResult:
    """Calculate any complete date range: day, week, month or multiple months/year.

    Dates are Tallinn YYYY-MM-DD; end_date is inclusive and defaults to the
    same day. With intervals omitted, fetch the authorized household's raw
    quarter-hour import/export through the existing Elering client. Automatic
    mode requires completed past days. Supplied intervals need both directions,
    exact 15-minute bounds and complete coverage; floats from the consumption
    MCP are accepted, while decimal strings preserve caller precision.

    report returns unrounded variable charges plus calendar-day allocation
    of monthly fees. monthly requires whole calendar months, rounds component
    lines and section VAT for each month, then sums finalized monthly amounts.
    include_daily adds analytical daily allocations, not additive invoice lines.
    Prices are always published market prices, never forecasts.
    """
    try:
        profile = _profile()
        first, last, start, end = period_bounds(start_date, end_date)
        profile.check_coverage(start, end)
        if mode not in ("report", "monthly") or quantity_basis not in ("raw", "billable"):
            raise PricingError("Invalid reporting mode or quantity basis.")
        if mode == "monthly" and (first.day != 1 or (last + timedelta(days=1)).day != 1):
            raise PricingError("Monthly mode requires complete calendar months; use report for days or weeks.")
        readings_retrieved_at = None
        warnings = []
        if intervals is None:
            if quantity_basis != "raw":
                raise PricingError("Automatic Elering readings are raw, not already-billable quantities.")
            if end > tallinn_now().astimezone(UTC):
                raise PricingError("Automatic pricing requires completed past calendar days, not today's unfinished or future readings.")
            report = _client.get_consumption(DateWindow(first, last, start, end), "15_minutes")
            rows = [
                EnergyInterval(row.start, row.start + MARKET_INTERVAL, row.consumption_kwh, row.export_kwh)
                for row in report.readings
            ]
            source = API_BASE + "/api/public/v1/metering-data"
            readings_retrieved_at = report.retrieved_at.astimezone(TALLINN).isoformat()
            warnings.append("Elering readings may be revised; complete coverage does not establish settlement finality.")
        else:
            rows = [
                make_interval(row.start, row.end, row.consumption_kwh, row.export_kwh)
                for row in intervals
            ]
            source = "caller_supplied"
            warnings.append("Supplied readings are caller-provided inputs, not independently verified household measurements.")
        validate_intervals(profile, rows, quantity_basis, start, end, False)
        prices = _market_prices(first, last)
        result = price_period(
            profile, rows, prices, start_date, end_date, quantity_basis, mode, include_daily,
        )
        return replace(
            result, input_source=source, price_source=API_URL,
            readings_retrieved_at=readings_retrieved_at,
            prices_retrieved_at=tallinn_now().isoformat(),
            warnings=result.warnings + warnings,
        )
    except (PricingError, ConsumptionError, PriceError) as error:
        raise ToolError(str(error)) from None


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
