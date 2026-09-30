"""Separate read-only MCP for the authorized Elering household."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from math import isfinite
from typing import Dict, List, Optional

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from elering_consumption import (
    API_BASE,
    ConsumptionError,
    ConsumptionReport,
    EstfeedClient,
    Reading,
    Resolution,
    interval_length,
    make_window,
)
from nordpool_ee import TALLINN, UTC


@dataclass(frozen=True)
class MeterAccessPeriod:
    start: str
    end: Optional[str]


@dataclass(frozen=True)
class HouseholdMeter:
    eic: str
    commodity: str
    access_periods: List[MeterAccessPeriod]


@dataclass(frozen=True)
class HouseholdMeters:
    source: str
    timezone: str
    start_date: str
    end_date: str
    retrieved_at: str
    metering_points: List[HouseholdMeter]


@dataclass(frozen=True)
class ConsumptionInterval:
    start: str
    end: str
    consumption_kwh: Optional[float]
    export_kwh: Optional[float]


@dataclass(frozen=True)
class QuantityTotal:
    total_kwh: Optional[float]
    known_intervals: int
    missing_elapsed_intervals: int
    complete: bool
    elapsed_complete: bool


@dataclass(frozen=True)
class EnergySummary:
    expected_intervals: int
    elapsed_intervals: int
    pending_intervals: int
    consumption: QuantityTotal
    export: QuantityTotal


@dataclass(frozen=True)
class ConsumptionPeriod:
    period: str
    start: str
    end: str
    covers_full_calendar_period: bool
    summary: EnergySummary


@dataclass(frozen=True)
class HouseholdConsumption:
    source: str
    metering_point_eic: str
    commodity: str
    timezone: str
    unit: str
    start_date: str
    end_date: str
    window_start: str
    window_end: str
    resolution: str
    interval_minutes: int
    as_of: str
    retrieved_at: str
    latest_returned_reading_start: Optional[str]
    finality: str
    intervals: List[ConsumptionInterval]
    summary: EnergySummary
    daily_summaries: List[ConsumptionPeriod]
    monthly_summaries: List[ConsumptionPeriod]
    missing_intervals: List[str]
    pending_intervals: List[str]
    warnings: List[str]


mcp = MCPServer(
    "elering-kodala-consumption",
    instructions=(
        "Read household electricity consumption/import and grid export in kWh, "
        "not prices or instantaneous power. Use Europe/Tallinn dates and show "
        "both energy directions separately. Never treat null/missing readings "
        "as zero. Display partial totals and pending intervals prominently. "
        "Summaries cover only the requested dates and completed accounting "
        "intervals; a partial month is not a full-month total. Completeness "
        "means reading coverage, not final settlement. Elering readings can "
        "arrive late and be revised. This is not per-appliance telemetry. "
        "Do not apply VAT to kWh or infer total solar generation from grid export."
    ),
)
_client = EstfeedClient()
_READ_ONLY = ToolAnnotations(
    read_only_hint=True, destructive_hint=False,
    idempotent_hint=True, open_world_hint=True,
)


def tallinn_now() -> datetime:
    return datetime.now(TALLINN)


def _local(value: datetime) -> str:
    return value.astimezone(TALLINN).isoformat()


def _number(value: Optional[Decimal]) -> Optional[float]:
    if value is None:
        return None
    result = float(value)
    if not isfinite(result):
        raise ConsumptionError("Energy total exceeds the supported numeric range.")
    return result


def _quantity(values: List[Decimal], expected: int, elapsed: int) -> QuantityTotal:
    return QuantityTotal(
        total_kwh=_number(sum(values, Decimal(0))) if values else None,
        known_intervals=len(values),
        missing_elapsed_intervals=elapsed - len(values),
        complete=len(values) == expected,
        elapsed_complete=len(values) == elapsed,
    )


def _summarize(
    starts: List[datetime], readings: Dict[datetime, Reading],
    step: timedelta, now: datetime,
) -> EnergySummary:
    elapsed = [start for start in starts if start + step <= now]
    consumption = []
    export = []
    for start in elapsed:
        row = readings.get(start)
        if row is not None:
            if row.consumption_kwh is not None:
                consumption.append(row.consumption_kwh)
            if row.export_kwh is not None:
                export.append(row.export_kwh)
    return EnergySummary(
        expected_intervals=len(starts),
        elapsed_intervals=len(elapsed),
        pending_intervals=len(starts) - len(elapsed),
        consumption=_quantity(consumption, len(starts), len(elapsed)),
        export=_quantity(export, len(starts), len(elapsed)),
    )


def _periods(
    starts: List[datetime], readings: Dict[datetime, Reading],
    step: timedelta, now: datetime, monthly: bool,
) -> List[ConsumptionPeriod]:
    groups: Dict[str, List[datetime]] = {}
    for start in starts:
        local = start.astimezone(TALLINN)
        key = local.strftime("%Y-%m" if monthly else "%Y-%m-%d")
        groups.setdefault(key, []).append(start)
    result = []
    for key, group in groups.items():
        start_local = group[0].astimezone(TALLINN)
        end_local = (group[-1] + step).astimezone(TALLINN)
        full = not monthly or (
            start_local.day == 1 and end_local.day == 1
            and end_local.date() > start_local.date()
        )
        result.append(
            ConsumptionPeriod(
                period=key, start=_local(group[0]), end=_local(group[-1] + step),
                covers_full_calendar_period=full,
                summary=_summarize(group, readings, step, now),
            )
        )
    return result


def build_consumption_result(
    report: ConsumptionReport, current_time: datetime
) -> HouseholdConsumption:
    if current_time.utcoffset() is None:
        raise ConsumptionError("Current time must include a UTC offset.")
    now = current_time.astimezone(UTC)
    step = interval_length(report.resolution)
    starts = [
        report.window.start + index * step
        for index in range(int((report.window.end - report.window.start) / step))
    ]
    readings = {row.start: row for row in report.readings}
    summary = _summarize(starts, readings, step, now)
    if not (summary.consumption.known_intervals or summary.export.known_intervals):
        raise ConsumptionError(
            "No measurements for completed accounting intervals are available "
            "in this window. Elering data can arrive late."
        )
    pending = [start for start in starts if start + step > now]
    missing = [start for start in starts if start + step <= now and start not in readings]
    rows = [row for row in report.readings if row.start + step <= now]
    warnings = [
        "Meter readings can arrive late or be revised. Reading coverage does "
        "not establish final settlement; the supported API schema reports no finality flag."
    ]
    if pending:
        warnings.append(
            "{} accounting intervals have not finished; they are excluded "
            "from the returned readings and totals.".format(len(pending))
        )
    for label, quantity in (("consumption", summary.consumption), ("export", summary.export)):
        if quantity.missing_elapsed_intervals:
            warnings.append(
                "{} total is partial or unavailable: {} elapsed readings are "
                "missing/null, not zero.".format(label, quantity.missing_elapsed_intervals)
            )
    monthly = _periods(starts, readings, step, now, monthly=True)
    if any(not period.covers_full_calendar_period for period in monthly):
        warnings.append("Monthly summaries cover the requested date slice, not necessarily a full month.")
    latest = next(
        (row.start for row in reversed(rows)
         if row.consumption_kwh is not None or row.export_kwh is not None),
        None,
    )
    return HouseholdConsumption(
        source=API_BASE + "/api/public/v1/metering-data",
        metering_point_eic=report.eic,
        commodity="ELECTRICITY",
        timezone="Europe/Tallinn",
        unit="kWh",
        start_date=report.window.start_date.isoformat(),
        end_date=report.window.end_date.isoformat(),
        window_start=_local(report.window.start),
        window_end=_local(report.window.end),
        resolution=report.resolution,
        interval_minutes=int(step.total_seconds() / 60),
        as_of=_local(now),
        retrieved_at=_local(report.retrieved_at),
        latest_returned_reading_start=_local(latest) if latest is not None else None,
        finality="not_reported_by_supported_api",
        intervals=[
            ConsumptionInterval(
                start=_local(row.start), end=_local(row.start + step),
                consumption_kwh=_number(row.consumption_kwh),
                export_kwh=_number(row.export_kwh),
            ) for row in rows
        ],
        summary=summary,
        daily_summaries=_periods(starts, readings, step, now, monthly=False),
        monthly_summaries=monthly,
        missing_intervals=[_local(start) for start in missing],
        pending_intervals=[_local(start) for start in pending],
        warnings=warnings,
    )


@mcp.tool(title="List household electricity metering points", annotations=_READ_ONLY)
def list_household_metering_points(
    start_date: Optional[str] = None, end_date: Optional[str] = None
) -> HouseholdMeters:
    """List authorized electricity EICs and access periods, without profile data.

    Dates use Europe/Tallinn YYYY-MM-DD; end_date is inclusive. With neither
    date, use the latest 31 calendar days including today. One supplied start
    date selects that day. No credentials are accepted as tool arguments.
    """
    now = tallinn_now()
    if start_date is None and end_date is not None:
        raise ToolError("start_date is required when end_date is supplied.")
    if start_date is None:
        start_date = (now.date() - timedelta(days=30)).isoformat()
        end_date = now.date().isoformat()
    try:
        window = make_window(start_date, end_date, now)
        report = _client.list_metering_points(window)
        if not report.points:
            raise ConsumptionError("No authorized electricity metering points exist for this period.")
        return HouseholdMeters(
            source=API_BASE + "/api/public/v1/metering-point-eics",
            timezone="Europe/Tallinn", start_date=window.start_date.isoformat(),
            end_date=window.end_date.isoformat(), retrieved_at=_local(report.retrieved_at),
            metering_points=[
                HouseholdMeter(
                    eic=point.eic, commodity="ELECTRICITY",
                    access_periods=[
                        MeterAccessPeriod(_local(period.start), _local(period.end) if period.end else None)
                        for period in point.periods
                    ],
                ) for point in report.points
            ],
        )
    except ConsumptionError as error:
        raise ToolError(str(error)) from None


@mcp.tool(title="Get household electricity consumption and export", annotations=_READ_ONLY)
def get_household_consumption(
    start_date: str, end_date: Optional[str] = None, resolution: Resolution = "hourly"
) -> HouseholdConsumption:
    """Read household import/export in kWh, with daily and monthly summaries.

    Dates are inclusive Europe/Tallinn YYYY-MM-DD, at most 31 days; omitted
    end_date means one day. Resolution is hourly or 15_minutes. Resolve only
    the unique authorized electricity point; never guess between households.
    Show missing/null readings, incomplete totals and pending intervals.
    Monthly totals cover only requested dates. Only completed intervals are
    returned/summed, not the in-progress interval. This is not live power,
    billing, total solar generation, or per-appliance telemetry.
    """
    now = tallinn_now()
    try:
        window = make_window(start_date, end_date, now)
        report = _client.get_consumption(window, resolution)
        return build_consumption_result(report, now)
    except ConsumptionError as error:
        raise ToolError(str(error)) from None


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
