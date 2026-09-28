#!/usr/bin/env python3
"""MCP server for Estonia's current and next-day electricity prices."""

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, List, Optional

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
    current_delivery_date,
    eur_mwh_to_cents_kwh,
    fetch_prices,
    next_delivery_date,
    summarize,
)


@dataclass(frozen=True)
class PricePoint:
    """One Estonia day-ahead market interval."""

    start: str
    eur_per_mwh: float
    cents_per_kwh: float


@dataclass(frozen=True)
class PriceSummaryResult:
    """Minimum, maximum, and duration-weighted average prices."""

    minimum: PricePoint
    maximum: PricePoint
    average_eur_per_mwh: float
    average_cents_per_kwh: float


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


mcp = MCPServer(
    "estonia-nordpool-prices",
    instructions=(
        "Use the tools to retrieve Estonia's complete current-day or next-day "
        "Nord Pool wholesale electricity prices, or prices for a specific "
        "Estonia-local date and hour."
    ),
)

EXCLUDED_COSTS = [
    "VAT",
    "supplier margin",
    "network charges",
    "excise",
    "other consumer fees",
]


def _price_point(interval: PriceInterval) -> PricePoint:
    return PricePoint(
        start=interval.start.isoformat(),
        eur_per_mwh=float(interval.price_eur_mwh),
        cents_per_kwh=float(eur_mwh_to_cents_kwh(interval.price_eur_mwh)),
    )


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
    summary = summarize(report)
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
        summary=PriceSummaryResult(
            minimum=_price_point(summary.minimum),
            maximum=_price_point(summary.maximum),
            average_eur_per_mwh=float(summary.average_eur_mwh),
            average_cents_per_kwh=float(
                eur_mwh_to_cents_kwh(summary.average_eur_mwh)
            ),
        ),
        current_interval=(
            _price_point(current_interval)
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
    intervals = [
        interval for interval in report.intervals if interval.start.hour == hour
    ]
    if not intervals:
        raise ToolError(
            "No Estonia market intervals exist for {} at hour {:02d}; "
            "this can occur during a daylight-saving transition.".format(
                report.delivery_date.isoformat(), hour
            )
        )

    minimum = min(intervals, key=lambda interval: interval.price_eur_mwh)
    maximum = max(intervals, key=lambda interval: interval.price_eur_mwh)
    average = sum(
        (interval.price_eur_mwh for interval in intervals),
        Decimal("0"),
    ) / Decimal(len(intervals))

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
        summary=PriceSummaryResult(
            minimum=_price_point(minimum),
            maximum=_price_point(maximum),
            average_eur_per_mwh=float(average),
            average_cents_per_kwh=float(eur_mwh_to_cents_kwh(average)),
        ),
    )


@mcp.tool(title="Get Estonia current-day electricity prices")
def get_estonia_current_day_prices() -> EstoniaDayPrices:
    """Get complete current-day Nord Pool prices for Estonia.

    Returns each 15-minute interval in Europe/Tallinn local time and identifies
    the currently active interval. Wholesale prices are in EUR/MWh and euro
    cents/kWh; consumer taxes, supplier margin, network charges, and other fees
    are excluded.
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
    maximum, and average wholesale prices. A repeated daylight-saving hour can
    contain eight intervals; a skipped hour returns a tool error.
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

    Returns each 15-minute interval in Europe/Tallinn local time, with
    wholesale prices in EUR/MWh and euro cents/kWh. Consumer taxes, supplier
    margin, network charges, and other fees are excluded.
    """

    return _get_prices(next_delivery_date())


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
