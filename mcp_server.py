#!/usr/bin/env python3
"""MCP server for Estonia's next-day Nord Pool electricity prices."""

from dataclasses import dataclass
from typing import List

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from nordpool_ee import (
    API_URL,
    MARKET_INTERVAL,
    PriceError,
    PriceInterval,
    PriceReport,
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
class EstoniaNextDayPrices:
    """Complete Estonia Nord Pool day-ahead prices for the next local day."""

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


mcp = MCPServer(
    "estonia-nordpool-prices",
    instructions=(
        "Use the tool to retrieve Estonia's complete next-day Nord Pool "
        "wholesale electricity prices in Tallinn local time."
    ),
)


def _price_point(interval: PriceInterval) -> PricePoint:
    return PricePoint(
        start=interval.start.isoformat(),
        eur_per_mwh=float(interval.price_eur_mwh),
        cents_per_kwh=float(eur_mwh_to_cents_kwh(interval.price_eur_mwh)),
    )


def build_price_result(report: PriceReport) -> EstoniaNextDayPrices:
    summary = summarize(report)
    return EstoniaNextDayPrices(
        area="EE",
        delivery_date=report.delivery_date.isoformat(),
        timezone="Europe/Tallinn",
        currency="EUR",
        interval_minutes=int(MARKET_INTERVAL.total_seconds() // 60),
        interval_count=len(report.intervals),
        wholesale_only=True,
        excluded_costs=[
            "VAT",
            "supplier margin",
            "network charges",
            "excise",
            "other consumer fees",
        ],
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
    )


@mcp.tool(title="Get Estonia next-day electricity prices")
def get_estonia_next_day_prices() -> EstoniaNextDayPrices:
    """Get complete next-day Nord Pool prices for Estonia.

    Returns each 15-minute interval in Europe/Tallinn local time, with
    wholesale prices in EUR/MWh and euro cents/kWh. Consumer taxes, supplier
    margin, network charges, and other fees are excluded.
    """

    delivery_date = next_delivery_date()
    try:
        report = fetch_prices(delivery_date)
    except PriceError as exc:
        raise ToolError(
            "Could not retrieve Estonia prices for {}: {}".format(
                delivery_date.isoformat(), exc
            )
        ) from exc
    return build_price_result(report)


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
