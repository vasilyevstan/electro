#!/usr/bin/env python3
"""MCP server for Estonia's current and next-day electricity prices."""

from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from itertools import groupby
from typing import Annotated, List, Optional, Tuple

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

from nordpool_ee import (
    API_URL,
    MARKET_INTERVAL,
    TALLINN,
    UTC,
    PriceError,
    PriceInterval,
    PriceReport,
    PriceSummary,
    current_delivery_date,
    eur_mwh_to_cents_kwh,
    fetch_prices,
    next_delivery_date,
    summarize,
)


VAT_RATE_PERCENT = Decimal("24")
VAT_MULTIPLIER = Decimal("1") + VAT_RATE_PERCENT / Decimal("100")


@dataclass(frozen=True)
class PriceValues:
    """The same energy price in EUR/MWh and euro cents/kWh."""

    eur_per_mwh: float
    cents_per_kwh: float


@dataclass(frozen=True)
class PricePoint:
    """One market interval with explicit VAT-exclusive and inclusive prices."""

    start: str
    eur_per_mwh: Annotated[float, Field(description="Legacy VAT-exclusive price.")]
    cents_per_kwh: Annotated[float, Field(description="Legacy VAT-exclusive price.")]
    excluding_vat: PriceValues
    including_vat: PriceValues


@dataclass(frozen=True)
class PriceSummaryResult:
    """Minimum, maximum, and duration-weighted average prices."""

    minimum: PricePoint
    maximum: PricePoint
    average_eur_per_mwh: Annotated[
        float, Field(description="Legacy VAT-exclusive average.")
    ]
    average_cents_per_kwh: Annotated[
        float, Field(description="Legacy VAT-exclusive average.")
    ]
    average_excluding_vat: PriceValues
    average_including_vat: PriceValues


@dataclass(frozen=True)
class HourlyAverage:
    """One elapsed hour, identified by offset-aware Estonia-local bounds."""

    hour: int
    start: str
    end: str
    interval_count: int
    average_excluding_vat: PriceValues
    average_including_vat: PriceValues


@dataclass(frozen=True)
class EstoniaDayPrices:
    """Complete Estonia Nord Pool day-ahead prices for one local day."""

    area: str
    delivery_date: str
    timezone: str
    currency: str
    interval_minutes: int
    interval_count: int
    wholesale_only: bool
    excluded_costs: List[str]
    source: str
    intervals: List[PricePoint]
    summary: PriceSummaryResult
    current_interval: Optional[PricePoint]
    vat_rate_percent: float
    hourly_averages: List[HourlyAverage]
    current_hour: Optional[HourlyAverage]


@dataclass(frozen=True)
class EstoniaHourPrices:
    """Estonia Nord Pool prices for one local clock hour."""

    area: str
    delivery_date: str
    hour: int
    timezone: str
    currency: str
    interval_count: int
    wholesale_only: bool
    excluded_costs: List[str]
    source: str
    intervals: List[PricePoint]
    summary: PriceSummaryResult
    vat_rate_percent: float
    hourly_averages: List[HourlyAverage]


mcp = MCPServer(
    "estonia-nordpool-prices",
    instructions=(
        "Use the tools to retrieve Estonia's complete current-day or next-day "
        "Nord Pool wholesale electricity prices, or prices for a specific "
        "Estonia-local date and hour. Show prices both excluding and including "
        "VAT, labeling the basis and time period. For the current price, show "
        "current_hour's average alongside current_interval's 15-minute price; "
        "do not confuse the two. These are energy-component prices, not total "
        "retail costs."
    ),
)

EXCLUDED_COSTS = [
    "supplier margin",
    "network charges",
    "excise",
    "other consumer fees",
]


def _price_values(price_eur_mwh: Decimal) -> PriceValues:
    return PriceValues(
        eur_per_mwh=float(price_eur_mwh),
        cents_per_kwh=float(eur_mwh_to_cents_kwh(price_eur_mwh)),
    )


def _price_point(interval: PriceInterval) -> PricePoint:
    excluding_vat = _price_values(interval.price_eur_mwh)
    return PricePoint(
        start=interval.start.isoformat(),
        eur_per_mwh=excluding_vat.eur_per_mwh,
        cents_per_kwh=excluding_vat.cents_per_kwh,
        excluding_vat=excluding_vat,
        including_vat=_price_values(interval.price_eur_mwh * VAT_MULTIPLIER),
    )


def _summary_result(summary: PriceSummary) -> PriceSummaryResult:
    excluding_vat = _price_values(summary.average_eur_mwh)
    return PriceSummaryResult(
        minimum=_price_point(summary.minimum),
        maximum=_price_point(summary.maximum),
        average_eur_per_mwh=excluding_vat.eur_per_mwh,
        average_cents_per_kwh=excluding_vat.cents_per_kwh,
        average_excluding_vat=excluding_vat,
        average_including_vat=_price_values(
            summary.average_eur_mwh * VAT_MULTIPLIER
        ),
    )


def _interval_report(
    report: PriceReport,
    intervals: Tuple[PriceInterval, ...],
) -> PriceReport:
    return replace(
        report,
        start_utc=intervals[0].start.astimezone(UTC),
        end_utc=intervals[-1].start.astimezone(UTC) + MARKET_INTERVAL,
        intervals=intervals,
    )


def _utc_hour_start(interval: PriceInterval) -> datetime:
    return interval.start.astimezone(UTC).replace(minute=0, second=0, microsecond=0)


def _hourly_averages(report: PriceReport) -> List[HourlyAverage]:
    hours = []
    for start_utc, group in groupby(report.intervals, key=_utc_hour_start):
        intervals = tuple(group)
        average = summarize(_interval_report(report, intervals)).average_eur_mwh
        start_local = start_utc.astimezone(TALLINN)
        hours.append(
            HourlyAverage(
                hour=start_local.hour,
                start=start_local.isoformat(),
                end=(start_utc + timedelta(hours=1)).astimezone(TALLINN).isoformat(),
                interval_count=len(intervals),
                average_excluding_vat=_price_values(average),
                average_including_vat=_price_values(average * VAT_MULTIPLIER),
            )
        )
    return hours


def _find_current_interval(
    report: PriceReport,
    current_time: datetime,
) -> Optional[PriceInterval]:
    if current_time.tzinfo is None:
        raise ValueError("current_time must include timezone information")

    current_utc = current_time.astimezone(UTC)
    if current_utc < report.start_utc or current_utc >= report.end_utc:
        return None

    for index, interval in enumerate(report.intervals):
        interval_start = interval.start.astimezone(UTC)
        if index + 1 < len(report.intervals):
            interval_end = report.intervals[index + 1].start.astimezone(UTC)
        else:
            interval_end = report.end_utc
        if interval_start <= current_utc < interval_end:
            return interval
    return None


def build_price_result(
    report: PriceReport,
    current_time: Optional[datetime] = None,
) -> EstoniaDayPrices:
    hourly_averages = _hourly_averages(report)
    current_interval = (
        _find_current_interval(report, current_time)
        if current_time is not None
        else None
    )
    return EstoniaDayPrices(
        area="EE",
        delivery_date=report.delivery_date.isoformat(),
        timezone="Europe/Tallinn",
        currency="EUR",
        interval_minutes=int(MARKET_INTERVAL.total_seconds() // 60),
        interval_count=len(report.intervals),
        wholesale_only=True,
        excluded_costs=list(EXCLUDED_COSTS),
        source=API_URL,
        intervals=[_price_point(interval) for interval in report.intervals],
        summary=_summary_result(summarize(report)),
        current_interval=(
            _price_point(current_interval)
            if current_interval is not None
            else None
        ),
        vat_rate_percent=float(VAT_RATE_PERCENT),
        hourly_averages=hourly_averages,
        current_hour=(
            next(
                hour
                for hour in hourly_averages
                if hour.start
                == _utc_hour_start(current_interval).astimezone(TALLINN).isoformat()
            )
            if current_interval is not None
            else None
        ),
    )


def tallinn_now() -> datetime:
    return datetime.now(TALLINN)


def _fetch_report(delivery_date: date) -> PriceReport:
    try:
        return fetch_prices(delivery_date)
    except PriceError as exc:
        raise ToolError(
            "Could not retrieve Estonia prices for {}: {}".format(
                delivery_date.isoformat(), exc
            )
        ) from exc


def _get_prices(
    delivery_date: date,
    current_time: Optional[datetime] = None,
) -> EstoniaDayPrices:
    return build_price_result(
        _fetch_report(delivery_date),
        current_time=current_time,
    )


def build_hour_price_result(
    report: PriceReport,
    hour: int,
) -> EstoniaHourPrices:
    intervals = tuple(
        interval for interval in report.intervals if interval.start.hour == hour
    )
    if not intervals:
        raise ToolError(
            "No Estonia market intervals exist for {} at hour {:02d}; "
            "this can occur during a daylight-saving transition.".format(
                report.delivery_date.isoformat(), hour
            )
        )

    hour_report = _interval_report(report, intervals)
    return EstoniaHourPrices(
        area="EE",
        delivery_date=report.delivery_date.isoformat(),
        hour=hour,
        timezone="Europe/Tallinn",
        currency="EUR",
        interval_count=len(intervals),
        wholesale_only=True,
        excluded_costs=list(EXCLUDED_COSTS),
        source=API_URL,
        intervals=[_price_point(interval) for interval in intervals],
        summary=_summary_result(summarize(hour_report)),
        vat_rate_percent=float(VAT_RATE_PERCENT),
        hourly_averages=_hourly_averages(hour_report),
    )


@mcp.tool(title="Get Estonia current-day electricity prices")
def get_estonia_current_day_prices() -> EstoniaDayPrices:
    """Get complete current-day Nord Pool prices for Estonia.

    Returns 15-minute prices and hourly averages with and without VAT, in
    EUR/MWh and euro cents/kWh. Show current_hour's average and the active
    current_interval separately, using Estonia-local time. Supplier margin,
    network charges, excise, and other fees remain excluded.
    """

    current_time = tallinn_now()
    return _get_prices(
        current_delivery_date(current_time),
        current_time=current_time,
    )


@mcp.tool(title="Get Estonia electricity prices for an hour")
def get_estonia_prices_for_hour(
    delivery_date: str,
    hour: Annotated[int, Field(ge=0, le=23)],
) -> EstoniaHourPrices:
    """Get Estonia prices for a specific local date and clock hour.

    Args:
        delivery_date: Estonia delivery date in YYYY-MM-DD format.
        hour: Estonia local clock hour from 0 through 23.

    Returns every 15-minute market interval in the requested hour plus minimum,
    maximum, and average prices with and without VAT. Repeated daylight-saving
    hours include two offset-distinct hourly_averages and eight intervals;
    summary averages both occurrences. A skipped hour returns a tool error.
    Supplier margin, network charges, excise, and other fees remain excluded.
    """

    try:
        requested_date = date.fromisoformat(delivery_date)
    except ValueError as exc:
        raise ToolError("delivery_date must use YYYY-MM-DD format") from exc
    if requested_date.isoformat() != delivery_date:
        raise ToolError("delivery_date must use YYYY-MM-DD format")

    return build_hour_price_result(
        _fetch_report(requested_date),
        hour,
    )


@mcp.tool(title="Get Estonia next-day electricity prices")
def get_estonia_next_day_prices() -> EstoniaDayPrices:
    """Get complete next-day Nord Pool prices for Estonia.

    Returns each 15-minute interval and hourly averages in Europe/Tallinn local
    time, with and without VAT, in EUR/MWh and euro cents/kWh. Supplier margin,
    network charges, excise, and other fees remain excluded.
    """

    return _get_prices(next_delivery_date())


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
