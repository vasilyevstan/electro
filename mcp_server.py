#!/usr/bin/env python3
"""MCP server for Estonia's published electricity prices and forecasts."""

from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from itertools import groupby
from math import isfinite
from typing import Annotated, List, Literal, Optional, Tuple

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
    market_bounds,
    next_delivery_date,
    summarize,
)
from wattcast_forecast import (
    ACCURACY_URL,
    FORECAST_URL,
    HOUR,
    AccuracyReport,
    ForecastError,
    ForecastReport,
    ForecastSlot,
    PredictedHour,
    fetch_accuracy,
    fetch_forecast,
)


VAT_RATE_PERCENT = Decimal("24")
VAT_MULTIPLIER = Decimal("1") + VAT_RATE_PERCENT / Decimal("100")
FORECAST_STALE_SECONDS = 3 * 60 * 60


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


@dataclass(frozen=True)
class PriceWithVat:
    excluding_vat: PriceValues
    including_vat: PriceValues


@dataclass(frozen=True)
class HourPrediction:
    p10: PriceWithVat
    p50: PriceWithVat
    p90: PriceWithVat
    horizon_days: int


@dataclass(frozen=True)
class ForecastHourPrices:
    start: str
    end: str
    basis: Literal["published", "forecast"]
    published_price: Optional[PriceWithVat]
    prediction: Optional[HourPrediction]


@dataclass(frozen=True)
class ForecastDaySummary:
    delivery_date: str
    expected_hours: int
    available_hours: int
    published_hours: int
    forecast_hours: int
    complete: bool
    basis: Literal["published", "forecast", "mixed", "unavailable"]
    average_available_hourly_price: Optional[PriceWithVat]
    minimum_hour: Optional[ForecastHourPrices]
    maximum_hour: Optional[ForecastHourPrices]


@dataclass(frozen=True)
class ForecastLiveAccuracy:
    mae: PriceWithVat
    interval_coverage_percent: float
    sample_count: int


@dataclass(frozen=True)
class ForecastBacktestAccuracy:
    mae: PriceWithVat
    naive_week_mae: PriceWithVat
    naive_lastknown_mae: PriceWithVat
    sample_count: int
    from_date: str


@dataclass(frozen=True)
class ForecastHorizonAccuracy:
    horizon_days: int
    live: Optional[ForecastLiveAccuracy]
    backtest: Optional[ForecastBacktestAccuracy]


@dataclass(frozen=True)
class ForecastAccuracy:
    source: str
    status: Literal["available", "unavailable"]
    unavailable_reason: Optional[str]
    evidence: str
    horizon_definition: str
    nominal_interval_coverage_percent: float
    retrieved_at: Optional[str]
    model_trained_at: Optional[str]
    live_window_days: Optional[int]
    band_method: Optional[str]
    horizons: List[ForecastHorizonAccuracy]


@dataclass(frozen=True)
class EstoniaPriceForecast:
    area: str
    timezone: str
    currency: str
    interval_minutes: int
    source: str
    attribution: str
    model_mode: str
    advisory_only: bool
    wholesale_only: bool
    vat_rate_percent: float
    excluded_costs: List[str]
    as_of: str
    window_start: str
    window_end: str
    issued_at: str
    retrieved_at: str
    model_trained_at: str
    age_seconds: float
    stale_after_seconds: int
    stale: bool
    complete: bool
    expected_hours: int
    available_hours: int
    published_hours: int
    forecast_hours: int
    available_start: str
    available_end: str
    missing_hours: List[str]
    hourly_prices: List[ForecastHourPrices]
    daily_summaries: List[ForecastDaySummary]
    accuracy: ForecastAccuracy
    warnings: List[str]


mcp = MCPServer(
    "estonia-nordpool-prices",
    instructions=(
        "Use the tools to retrieve Estonia's complete current-day or next-day "
        "Nord Pool wholesale electricity prices, or prices for a specific "
        "Estonia-local date and hour. Show prices both excluding and including "
        "VAT, labeling the basis and time period. For the current price, show "
        "current_hour's average alongside current_interval's 15-minute price; "
        "do not confuse the two. These are energy-component prices, not total "
        "retail costs. Use get_estonia_price_forecast separately for the next "
        "seven full Estonia calendar days. Forecasts are experimental estimates, "
        "not Nord Pool auction results. Distinguish published_price from "
        "prediction.p50 and its p10-p90 range. Show partial/stale warnings and "
        "the actual coverage and issuance time prominently. Accuracy is "
        "provider-reported, not independently verified; nominal 80% ranges "
        "are not a guarantee. Daily averages use available hourly published "
        "prices or p50 values, not a daily prediction interval."
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


def _forecast_price(price: Decimal) -> PriceWithVat:
    net = _price_values(price)
    gross = _price_values(price * VAT_MULTIPLIER)
    if not all(
        isfinite(value)
        for value in (
            net.eur_per_mwh, net.cents_per_kwh,
            gross.eur_per_mwh, gross.cents_per_kwh,
        )
    ):
        raise ForecastError("Forecast price exceeds the supported numeric range")
    return PriceWithVat(excluding_vat=net, including_vat=gross)


def _forecast_hour(slot: ForecastSlot) -> ForecastHourPrices:
    start = slot.start.astimezone(UTC)
    predicted = isinstance(slot, PredictedHour)
    return ForecastHourPrices(
        start=start.astimezone(TALLINN).isoformat(),
        end=(start + HOUR).astimezone(TALLINN).isoformat(),
        basis="forecast" if predicted else "published",
        published_price=None if predicted else _forecast_price(slot.price_eur_mwh),
        prediction=(
            HourPrediction(
                p10=_forecast_price(slot.p10),
                p50=_forecast_price(slot.p50),
                p90=_forecast_price(slot.p90),
                horizon_days=slot.horizon_days,
            )
            if isinstance(slot, PredictedHour) else None
        ),
    )


def _forecast_day(
    delivery_date: date, hours: Tuple[ForecastSlot, ...]
) -> ForecastDaySummary:
    start, end = market_bounds(delivery_date)
    records = tuple(slot for slot in hours if start <= slot.start < end)
    count = len(records)
    expected = int((end - start) / HOUR)
    forecast_count = sum(isinstance(slot, PredictedHour) for slot in records)
    published_count = count - forecast_count
    basis: Literal["published", "forecast", "mixed", "unavailable"]
    if not count:
        basis = "unavailable"
    elif not forecast_count:
        basis = "published"
    elif not published_count:
        basis = "forecast"
    else:
        basis = "mixed"
    return ForecastDaySummary(
        delivery_date=delivery_date.isoformat(),
        expected_hours=expected,
        available_hours=count,
        published_hours=published_count,
        forecast_hours=forecast_count,
        complete=count == expected,
        basis=basis,
        average_available_hourly_price=(
            _forecast_price(
                sum((slot.price_eur_mwh for slot in records), Decimal(0)) / count
            )
            if count else None
        ),
        minimum_hour=(
            _forecast_hour(min(records, key=lambda slot: slot.price_eur_mwh))
            if count else None
        ),
        maximum_hour=(
            _forecast_hour(max(records, key=lambda slot: slot.price_eur_mwh))
            if count else None
        ),
    )


def _forecast_accuracy(
    report: Optional[AccuracyReport], error: Optional[str]
) -> ForecastAccuracy:
    horizons = []
    if report is not None:
        for item in report.horizons:
            live = item.live
            backtest = item.backtest
            horizons.append(
                ForecastHorizonAccuracy(
                    horizon_days=item.horizon_days,
                    live=(
                        ForecastLiveAccuracy(
                            mae=_forecast_price(live.mae),
                            interval_coverage_percent=float(live.coverage_percent),
                            sample_count=live.sample_count,
                        ) if live is not None else None
                    ),
                    backtest=(
                        ForecastBacktestAccuracy(
                            mae=_forecast_price(backtest.mae),
                            naive_week_mae=_forecast_price(backtest.naive_week_mae),
                            naive_lastknown_mae=_forecast_price(backtest.naive_lastknown_mae),
                            sample_count=backtest.sample_count,
                            from_date=backtest.from_date.isoformat(),
                        ) if backtest is not None else None
                    ),
                )
            )
    return ForecastAccuracy(
        source=ACCURACY_URL,
        status="available" if report is not None else "unavailable",
        unavailable_reason=(
            None if report is not None else (error or "Accuracy was not retrieved")
        ),
        evidence="Provider-reported; not independently verified",
        horizon_definition="Days after the last settled local day, not elapsed 24-hour lead times",
        nominal_interval_coverage_percent=80,
        retrieved_at=(
            report.retrieved_at.astimezone(TALLINN).isoformat() if report else None
        ),
        model_trained_at=(
            report.model_trained_at.astimezone(TALLINN).isoformat() if report else None
        ),
        live_window_days=report.live_days if report else None,
        band_method=report.band_method if report else None,
        horizons=horizons,
    )


def build_forecast_result(
    report: ForecastReport,
    current_time: datetime,
    accuracy: Optional[AccuracyReport] = None,
    accuracy_error: Optional[str] = None,
) -> EstoniaPriceForecast:
    first_day = next_delivery_date(current_time)
    start, _ = market_bounds(first_day)
    _, end = market_bounds(first_day + timedelta(days=6))
    expected = int((end - start) / HOUR)
    hours = tuple(slot for slot in report.hours if start <= slot.start < end)
    forecast_count = sum(isinstance(slot, PredictedHour) for slot in hours)
    if not forecast_count:
        raise ForecastError("No usable forecast hours exist in the requested seven-day window")
    available = {slot.start for slot in hours}
    missing = [
        (start + index * HOUR).astimezone(TALLINN).isoformat()
        for index in range(expected) if start + index * HOUR not in available
    ]
    age = (current_time.astimezone(UTC) - report.issued_at).total_seconds()
    stale = age > FORECAST_STALE_SECONDS
    warnings = [
        "Experimental forecasts are not auction results. The p10-p90 range has "
        "nominal 80% coverage, not guaranteed coverage."
    ]
    if missing:
        warnings.append(
            "Partial forecast: {} of {} hours available; daily summaries use only "
            "available hours. Missing hours are not estimated.".format(len(hours), expected)
        )
    if stale:
        warnings.append(
            "Stale forecast: issued {:.1f} hours ago, exceeding the three-hour "
            "freshness threshold.".format(age / 3600)
        )
    if age < 0:
        warnings.append("Provider issuance is ahead of the call time; possible clock skew.")
    try:
        accuracy_result = _forecast_accuracy(accuracy, accuracy_error)
    except ForecastError as exc:
        accuracy = None
        accuracy_result = _forecast_accuracy(None, str(exc))
    if accuracy is None:
        warnings.append("Accuracy unavailable: " + str(accuracy_result.unavailable_reason))
    else:
        missing_live = [str(item.horizon_days) for item in accuracy.horizons if item.live is None]
        undercovered = [
            str(item.horizon_days) for item in accuracy.horizons
            if item.live is not None and item.live.coverage_percent < 80
        ]
        if missing_live:
            warnings.append(
                "No provider-reported live accuracy for horizons: "
                + ", ".join(missing_live)
            )
        if undercovered:
            warnings.append(
                "Provider-reported live range coverage is below the nominal 80% "
                "target at horizons: " + ", ".join(undercovered)
            )
        if accuracy.model_trained_at != report.model_trained_at:
            warnings.append(
                "Forecast and accuracy model timestamps differ; backtests may "
                "describe a different model."
            )
    return EstoniaPriceForecast(
        area="EE",
        timezone="Europe/Tallinn",
        currency="EUR",
        interval_minutes=60,
        source=FORECAST_URL,
        attribution=report.attribution,
        model_mode="raw",
        advisory_only=True,
        wholesale_only=True,
        vat_rate_percent=float(VAT_RATE_PERCENT),
        excluded_costs=list(EXCLUDED_COSTS),
        as_of=current_time.astimezone(TALLINN).isoformat(),
        window_start=start.astimezone(TALLINN).isoformat(),
        window_end=end.astimezone(TALLINN).isoformat(),
        issued_at=report.issued_at.astimezone(TALLINN).isoformat(),
        retrieved_at=report.retrieved_at.astimezone(TALLINN).isoformat(),
        model_trained_at=report.model_trained_at.astimezone(TALLINN).isoformat(),
        age_seconds=age,
        stale_after_seconds=FORECAST_STALE_SECONDS,
        stale=stale,
        complete=not missing,
        expected_hours=expected,
        available_hours=len(hours),
        published_hours=len(hours) - forecast_count,
        forecast_hours=forecast_count,
        available_start=hours[0].start.astimezone(TALLINN).isoformat(),
        available_end=(hours[-1].start + HOUR).astimezone(TALLINN).isoformat(),
        missing_hours=missing,
        hourly_prices=[_forecast_hour(slot) for slot in hours],
        daily_summaries=[
            _forecast_day(first_day + timedelta(days=index), hours)
            for index in range(7)
        ],
        accuracy=accuracy_result,
        warnings=warnings,
    )


@mcp.tool(title="Get Estonia seven-day electricity price forecast")
def get_estonia_price_forecast() -> EstoniaPriceForecast:
    """Get hourly forecasts and daily summaries for the next seven Tallinn days.

    Uses Wattcast's free raw model. Show both VAT bases, distinguish published
    prices from prediction.p50 and p10/p90, and show issuance time, actual
    coverage and all partial/stale warnings. Accuracy is provider-reported;
    nominal 80% ranges are not guaranteed. Daily means combine available
    published prices and hourly p50 values, not daily probability bounds.
    No supplier margin, network charges, excise or other fees are included.
    """
    current_time = tallinn_now()
    try:
        report = fetch_forecast()
        accuracy = None
        accuracy_error = None
        try:
            accuracy = fetch_accuracy()
        except ForecastError as exc:
            accuracy_error = str(exc)
        return build_forecast_result(report, current_time, accuracy, accuracy_error)
    except ForecastError as exc:
        raise ToolError("Could not retrieve Estonia forecast: {}".format(exc)) from exc


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
