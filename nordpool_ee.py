#!/usr/bin/env python3
"""Print Estonia's next-day Nord Pool electricity prices via Elering."""

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Any, Callable, Dict, List, Optional, Sequence, TextIO, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo


API_URL = "https://dashboard.elering.ee/api/nps/price"
TALLINN = ZoneInfo("Europe/Tallinn")
UTC = timezone.utc
MARKET_INTERVAL = timedelta(minutes=15)
REQUEST_TIMEOUT_SECONDS = 10

EXIT_UNAVAILABLE = 2
EXIT_NETWORK = 3
EXIT_INVALID_DATA = 4


class PriceError(Exception):
    """Base class for expected price retrieval failures."""


class PricesNotPublishedError(PriceError):
    """Raised when the requested delivery day has no published prices."""


class PriceNetworkError(PriceError):
    """Raised when Elering cannot be reached successfully."""


class PriceDataError(PriceError):
    """Raised when Elering returns invalid or incomplete price data."""


@dataclass(frozen=True)
class PriceInterval:
    start: datetime
    price_eur_mwh: Decimal


@dataclass(frozen=True)
class PriceReport:
    delivery_date: date
    start_utc: datetime
    end_utc: datetime
    intervals: Tuple[PriceInterval, ...]


@dataclass(frozen=True)
class PriceSummary:
    minimum: PriceInterval
    maximum: PriceInterval
    average_eur_mwh: Decimal


def current_delivery_date(now: Optional[datetime] = None) -> date:
    current = now if now is not None else datetime.now(TALLINN)
    if current.tzinfo is None:
        raise ValueError("now must include timezone information")
    return current.astimezone(TALLINN).date()


def next_delivery_date(now: Optional[datetime] = None) -> date:
    return current_delivery_date(now) + timedelta(days=1)


def market_bounds(delivery_date: date) -> Tuple[datetime, datetime]:
    start_local = datetime.combine(delivery_date, time.min, tzinfo=TALLINN)
    end_local = datetime.combine(
        delivery_date + timedelta(days=1), time.min, tzinfo=TALLINN
    )
    return start_local.astimezone(UTC), end_local.astimezone(UTC)


def expected_interval_timestamps(delivery_date: date) -> Tuple[int, ...]:
    start_utc, end_utc = market_bounds(delivery_date)
    timestamps: List[int] = []
    current = start_utc
    while current < end_utc:
        timestamps.append(int(current.timestamp()))
        current += MARKET_INTERVAL
    return tuple(timestamps)


def _format_api_timestamp(value: datetime) -> str:
    return (
        value.astimezone(UTC)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def build_api_url(delivery_date: date) -> str:
    start_utc, end_utc = market_bounds(delivery_date)
    inclusive_end = end_utc - timedelta(milliseconds=1)
    query = urlencode(
        {
            "start": _format_api_timestamp(start_utc),
            "end": _format_api_timestamp(inclusive_end),
        }
    )
    return "{}?{}".format(API_URL, query)


def _reject_nonstandard_json_constant(value: str) -> None:
    raise ValueError("invalid JSON constant: {}".format(value))


def _fetch_payload_url(
    url: str,
    timeout: int = REQUEST_TIMEOUT_SECONDS,
    opener: Optional[Callable[..., Any]] = None,
) -> Dict[str, Any]:
    request = Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "electro-nordpool-ee/1.0",
        },
    )
    open_url = opener if opener is not None else urlopen

    try:
        with open_url(request, timeout=timeout) as response:
            status = getattr(response, "status", None)
            if status is None and hasattr(response, "getcode"):
                status = response.getcode()
            if status is not None and not 200 <= status < 300:
                raise PriceNetworkError(
                    "Elering returned HTTP status {}".format(status)
                )
            body = response.read()
    except HTTPError as exc:
        raise PriceNetworkError(
            "Elering returned HTTP status {}".format(exc.code)
        ) from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise PriceNetworkError("could not reach Elering: {}".format(exc)) from exc

    try:
        decoded = body.decode("utf-8")
        payload = json.loads(
            decoded,
            parse_float=Decimal,
            parse_constant=_reject_nonstandard_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise PriceDataError("Elering returned invalid JSON") from exc

    if not isinstance(payload, dict):
        raise PriceDataError("Elering response must be a JSON object")
    return payload


def fetch_payload(
    delivery_date: date,
    timeout: int = REQUEST_TIMEOUT_SECONDS,
    opener: Optional[Callable[..., Any]] = None,
) -> Dict[str, Any]:
    return _fetch_payload_url(build_api_url(delivery_date), timeout, opener)


def _parse_timestamp(value: Any, record_number: int) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise PriceDataError(
            "Estonia record {} has an invalid timestamp".format(record_number)
        )

    numeric_value = Decimal(str(value)) if isinstance(value, float) else Decimal(value)
    if not numeric_value.is_finite() or numeric_value != numeric_value.to_integral_value():
        raise PriceDataError(
            "Estonia record {} has an invalid timestamp".format(record_number)
        )
    return int(numeric_value)


def _parse_price(value: Any, record_number: int) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise PriceDataError(
            "Estonia record {} has an invalid price".format(record_number)
        )

    price = Decimal(str(value)) if isinstance(value, float) else Decimal(value)
    if not price.is_finite():
        raise PriceDataError(
            "Estonia record {} has an invalid price".format(record_number)
        )
    return price


def normalize_payload(payload: Dict[str, Any], delivery_date: date) -> PriceReport:
    if payload.get("success") is not True:
        raise PriceDataError("Elering response did not indicate success")

    data = payload.get("data")
    if not isinstance(data, dict) or "ee" not in data:
        raise PriceDataError("Elering response is missing data.ee")

    records = data["ee"]
    if not isinstance(records, list):
        raise PriceDataError("Elering data.ee must be a list")
    if not records:
        raise PricesNotPublishedError(
            "prices for {} have not been published".format(delivery_date.isoformat())
        )

    start_utc, end_utc = market_bounds(delivery_date)
    expected_timestamps = set(expected_interval_timestamps(delivery_date))
    intervals_by_timestamp: Dict[int, PriceInterval] = {}

    for record_number, record in enumerate(records, start=1):
        if not isinstance(record, dict):
            raise PriceDataError(
                "Estonia record {} must be an object".format(record_number)
            )

        timestamp = _parse_timestamp(record.get("timestamp"), record_number)
        price = _parse_price(record.get("price"), record_number)

        try:
            start = datetime.fromtimestamp(timestamp, tz=UTC).astimezone(TALLINN)
        except (OverflowError, OSError, ValueError) as exc:
            raise PriceDataError(
                "Estonia record {} has an out-of-range timestamp".format(
                    record_number
                )
            ) from exc

        if start.date() != delivery_date:
            continue
        if timestamp in intervals_by_timestamp:
            raise PriceDataError(
                "Elering returned duplicate Estonia timestamp {}".format(timestamp)
            )

        intervals_by_timestamp[timestamp] = PriceInterval(
            start=start,
            price_eur_mwh=price,
        )

    if not intervals_by_timestamp:
        raise PricesNotPublishedError(
            "prices for {} have not been published".format(delivery_date.isoformat())
        )

    actual_timestamps = set(intervals_by_timestamp)
    missing = expected_timestamps - actual_timestamps
    unexpected = actual_timestamps - expected_timestamps
    if missing or unexpected:
        details = []
        if missing:
            details.append("{} missing".format(len(missing)))
        if unexpected:
            details.append("{} unexpected".format(len(unexpected)))
        raise PriceDataError(
            "Elering returned incomplete Estonia data for {} ({})".format(
                delivery_date.isoformat(), ", ".join(details)
            )
        )

    sorted_intervals = tuple(
        intervals_by_timestamp[timestamp]
        for timestamp in sorted(intervals_by_timestamp)
    )
    return PriceReport(
        delivery_date=delivery_date,
        start_utc=start_utc,
        end_utc=end_utc,
        intervals=sorted_intervals,
    )


def fetch_prices(
    delivery_date: date,
    timeout: int = REQUEST_TIMEOUT_SECONDS,
    opener: Optional[Callable[..., Any]] = None,
) -> PriceReport:
    return normalize_payload(
        fetch_payload(delivery_date, timeout=timeout, opener=opener),
        delivery_date,
    )


def fetch_prices_range(
    first: date,
    last: date,
    timeout: int = REQUEST_TIMEOUT_SECONDS,
    opener: Optional[Callable[..., Any]] = None,
) -> Tuple[PriceReport, ...]:
    """Fetch at most 31 local calendar days, retaining strict per-day validation."""
    if type(first) is not date or type(last) is not date or last < first or (last - first).days >= 31:
        raise PriceDataError("price ranges must contain 1 to 31 calendar days")
    start, _ = market_bounds(first)
    _, end = market_bounds(last)
    query = urlencode({
        "start": _format_api_timestamp(start),
        "end": _format_api_timestamp(end - timedelta(milliseconds=1)),
    })
    payload = _fetch_payload_url("{}?{}".format(API_URL, query), timeout, opener)
    return tuple(
        normalize_payload(payload, first + timedelta(days=offset))
        for offset in range((last - first).days + 1)
    )


def eur_mwh_to_cents_kwh(price_eur_mwh: Decimal) -> Decimal:
    return price_eur_mwh / Decimal("10")


def summarize(report: PriceReport) -> PriceSummary:
    if not report.intervals:
        raise PriceDataError("cannot summarize an empty price report")

    weighted_total = Decimal("0")
    total_seconds = Decimal("0")

    for index, interval in enumerate(report.intervals):
        interval_start_utc = interval.start.astimezone(UTC)
        if index + 1 < len(report.intervals):
            interval_end_utc = report.intervals[index + 1].start.astimezone(UTC)
        else:
            interval_end_utc = report.end_utc

        duration_seconds = Decimal(
            str((interval_end_utc - interval_start_utc).total_seconds())
        )
        if duration_seconds <= 0:
            raise PriceDataError("price intervals are not strictly chronological")

        weighted_total += interval.price_eur_mwh * duration_seconds
        total_seconds += duration_seconds

    minimum = min(report.intervals, key=lambda interval: interval.price_eur_mwh)
    maximum = max(report.intervals, key=lambda interval: interval.price_eur_mwh)
    return PriceSummary(
        minimum=minimum,
        maximum=maximum,
        average_eur_mwh=weighted_total / total_seconds,
    )


def _format_local_start(value: datetime) -> str:
    return value.strftime("%H:%M %Z")


def _format_price_pair(price_eur_mwh: Decimal) -> str:
    return "{:.2f} EUR/MWh ({:.3f} cents/kWh)".format(
        price_eur_mwh,
        eur_mwh_to_cents_kwh(price_eur_mwh),
    )


def render_report(report: PriceReport) -> str:
    summary = summarize(report)
    lines = [
        "Estonia Nord Pool day-ahead prices for {} (Europe/Tallinn)".format(
            report.delivery_date.isoformat()
        ),
        "Wholesale energy only; taxes, supplier margin, and network fees are excluded.",
        "Source: Elering ({})".format(API_URL),
        "",
        "{:<12} {:>12} {:>14}".format(
            "Local start", "EUR/MWh", "cents/kWh"
        ),
        "{:<12} {:>12} {:>14}".format("-" * 11, "-" * 7, "-" * 9),
    ]

    for interval in report.intervals:
        lines.append(
            "{:<12} {:>12.2f} {:>14.3f}".format(
                _format_local_start(interval.start),
                interval.price_eur_mwh,
                eur_mwh_to_cents_kwh(interval.price_eur_mwh),
            )
        )

    lines.extend(
        [
            "",
            "Intervals: {}".format(len(report.intervals)),
            "Minimum: {} at {}".format(
                _format_price_pair(summary.minimum.price_eur_mwh),
                _format_local_start(summary.minimum.start),
            ),
            "Maximum: {} at {}".format(
                _format_price_pair(summary.maximum.price_eur_mwh),
                _format_local_start(summary.maximum.start),
            ),
            "Duration-weighted average: {}".format(
                _format_price_pair(summary.average_eur_mwh)
            ),
        ]
    )
    return "\n".join(lines)


def build_argument_parser() -> argparse.ArgumentParser:
    return argparse.ArgumentParser(
        description=(
            "Fetch Estonia's Nord Pool day-ahead wholesale electricity prices "
            "for the next Europe/Tallinn calendar day."
        )
    )


def main(
    argv: Optional[Sequence[str]] = None,
    stdout: Optional[TextIO] = None,
    stderr: Optional[TextIO] = None,
) -> int:
    output = stdout if stdout is not None else sys.stdout
    error_output = stderr if stderr is not None else sys.stderr
    parser = build_argument_parser()
    parser.parse_args(argv)
    delivery_date = next_delivery_date()

    try:
        report = fetch_prices(delivery_date)
    except PricesNotPublishedError as exc:
        print("Price data unavailable: {}".format(exc), file=error_output)
        return EXIT_UNAVAILABLE
    except PriceNetworkError as exc:
        print("Network error: {}".format(exc), file=error_output)
        return EXIT_NETWORK
    except PriceDataError as exc:
        print("Invalid price data: {}".format(exc), file=error_output)
        return EXIT_INVALID_DATA

    print(render_report(report), file=output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
