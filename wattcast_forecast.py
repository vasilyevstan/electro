"""Validated, cached access to Wattcast's free raw-model Estonia forecasts."""

import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from math import isfinite
from threading import Lock
from time import monotonic
from typing import Dict, List, Optional, Tuple, Union
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from nordpool_ee import REQUEST_TIMEOUT_SECONDS, UTC


FORECAST_URL = (
    "https://wattcast.eu/v1/forecast"
    "?zone=EE&resolution=hour&hours=216&adjusted=false"
)
ACCURACY_URL = "https://wattcast.eu/v1/accuracy?zone=EE&days=14"
CACHE_SECONDS = 3600
HOUR = timedelta(hours=1)


class ForecastError(Exception):
    """An expected forecast transport, availability, or data failure."""


@dataclass(frozen=True)
class PublishedHour:
    start: datetime
    price_eur_mwh: Decimal


@dataclass(frozen=True)
class PredictedHour:
    start: datetime
    p10: Decimal
    p50: Decimal
    p90: Decimal
    horizon_days: int

    @property
    def price_eur_mwh(self) -> Decimal:
        return self.p50


ForecastSlot = Union[PublishedHour, PredictedHour]


@dataclass(frozen=True)
class ForecastReport:
    issued_at: datetime
    retrieved_at: datetime
    model_trained_at: datetime
    hours: Tuple[ForecastSlot, ...]
    attribution: str


@dataclass(frozen=True)
class BacktestScore:
    mae: Decimal
    naive_week_mae: Decimal
    naive_lastknown_mae: Decimal
    sample_count: int
    from_date: date


@dataclass(frozen=True)
class LiveScore:
    mae: Decimal
    coverage_percent: Decimal
    sample_count: int


@dataclass(frozen=True)
class HorizonAccuracy:
    horizon_days: int
    backtest: Optional[BacktestScore]
    live: Optional[LiveScore]


@dataclass(frozen=True)
class AccuracyReport:
    retrieved_at: datetime
    model_trained_at: datetime
    live_days: int
    nominal_coverage_percent: Decimal
    band_method: str
    horizons: Tuple[HorizonAccuracy, ...]


def _object(value: object, label: str) -> Dict[str, object]:
    if not isinstance(value, dict):
        raise ForecastError("{} must be an object".format(label))
    return value


def _array(value: object, label: str) -> List[object]:
    if not isinstance(value, list):
        raise ForecastError("{} must be an array".format(label))
    return value


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ForecastError("{} must be a nonempty string".format(label))
    return value


def _number(value: object, label: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise ForecastError("{} must be numeric".format(label))
    result = Decimal(str(value)) if isinstance(value, float) else Decimal(value)
    if not result.is_finite() or not isfinite(float(result)):
        raise ForecastError("{} must be finite".format(label))
    return result


def _integer(value: object, label: str, minimum: int = 0) -> int:
    result = _number(value, label)
    if result != result.to_integral_value() or result < minimum:
        raise ForecastError("{} must be an integer >= {}".format(label, minimum))
    return int(result)


def _nonnegative(value: object, label: str) -> Decimal:
    result = _number(value, label)
    if result < 0:
        raise ForecastError("{} must be nonnegative".format(label))
    return result


def _iso_time(value: object, label: str) -> datetime:
    text = _text(value, label)
    try:
        result = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if result.utcoffset() is None:
            raise ValueError("missing UTC offset")
        return result.astimezone(UTC)
    except ValueError as exc:
        raise ForecastError("{} must be an offset-aware ISO timestamp".format(label)) from exc


def _timestamp(value: object, iso: object, label: str) -> datetime:
    seconds = _integer(value, label)
    try:
        result = datetime.fromtimestamp(seconds, tz=UTC)
    except (ValueError, OverflowError, OSError) as exc:
        raise ForecastError("{} is out of range".format(label)) from exc
    if result != _iso_time(iso, label + " ISO"):
        raise ForecastError("{} epoch and ISO timestamps disagree".format(label))
    return result


def normalize_forecast(
    payload: Dict[str, object], retrieved_at: datetime
) -> ForecastReport:
    for key, expected in (
        ("zone", "EE"), ("resolution", "hour"), ("currency", "EUR"), ("unit", "EUR/MWh")
    ):
        if payload.get(key) != expected:
            raise ForecastError("Wattcast {} must be {}".format(key, expected))
    if payload.get("level") is not None or payload.get("adjustments") is not None:
        raise ForecastError("Wattcast returned adjustments instead of the raw model")

    issued_at = _timestamp(payload.get("madeAt"), payload.get("madeAtIso"), "madeAt")
    if issued_at > retrieved_at.astimezone(UTC) + timedelta(minutes=5):
        raise ForecastError("Wattcast issuance timestamp is in the future")
    hours: Dict[datetime, ForecastSlot] = {}
    forecast_count = 0
    for collection in ("known", "forecast"):
        for index, value in enumerate(_array(payload.get(collection), collection)):
            label = "{}[{}]".format(collection, index)
            row = _object(value, label)
            start = _timestamp(row.get("ts"), row.get("startsAt"), label)
            if start.minute or start.second or start.microsecond:
                raise ForecastError("{} is not aligned to an hour".format(label))
            if start in hours:
                raise ForecastError("Wattcast returned a duplicate or conflicting hour")
            if collection == "known":
                hours[start] = PublishedHour(start, _number(row.get("eurMwh"), label))
            else:
                p10 = _number(row.get("p10"), label + ".p10")
                p50 = _number(row.get("p50"), label + ".p50")
                p90 = _number(row.get("p90"), label + ".p90")
                if not p10 <= p50 <= p90:
                    raise ForecastError("{} must have p10 <= p50 <= p90".format(label))
                horizon = _integer(row.get("k"), label + ".k", minimum=1)
                if horizon > 7:
                    raise ForecastError("{} horizon exceeds seven days".format(label))
                hours[start] = PredictedHour(start, p10, p50, p90, horizon)
                forecast_count += 1
    if not forecast_count:
        raise ForecastError("Wattcast returned no forecast hours")
    return ForecastReport(
        issued_at=issued_at,
        retrieved_at=retrieved_at,
        model_trained_at=_iso_time(payload.get("modelTrainedAt"), "modelTrainedAt"),
        hours=tuple(hours[start] for start in sorted(hours)),
        attribution=_text(payload.get("attribution"), "attribution"),
    )


def normalize_accuracy(
    payload: Dict[str, object], retrieved_at: datetime
) -> AccuracyReport:
    if payload.get("zone") != "EE" or payload.get("unit") != "EUR/MWh":
        raise ForecastError("Wattcast accuracy must be EE in EUR/MWh")
    backtest = _object(payload.get("backtest"), "backtest")
    live = _object(payload.get("live"), "live")
    band = _object(payload.get("band"), "band")
    target = _number(band.get("target_coverage"), "target_coverage")
    if target != 80:
        raise ForecastError("Wattcast p10-p90 target coverage must be 80 percent")
    live_days = _integer(payload.get("liveDays"), "liveDays", minimum=1)
    if live_days != 14:
        raise ForecastError("Wattcast accuracy must describe the requested 14-day window")
    horizons = []
    for horizon in range(1, 8):
        key = str(horizon)
        backtest_score = None
        live_score = None
        if key in backtest:
            row = _object(backtest[key], "backtest." + key)
            try:
                from_date = date.fromisoformat(_text(row.get("cv_from"), "cv_from"))
            except ValueError as exc:
                raise ForecastError("Wattcast cv_from must be an ISO date") from exc
            backtest_score = BacktestScore(
                mae=_nonnegative(row.get("cv_mae"), "cv_mae"),
                naive_week_mae=_nonnegative(row.get("naive_week_mae"), "naive_week_mae"),
                naive_lastknown_mae=_nonnegative(
                    row.get("naive_lastknown_mae"), "naive_lastknown_mae"
                ),
                sample_count=_integer(row.get("n_cv"), "n_cv", minimum=1),
                from_date=from_date,
            )
        if key in live:
            row = _object(live[key], "live." + key)
            coverage = _nonnegative(row.get("coverage80"), "coverage80")
            if coverage > 100:
                raise ForecastError("Wattcast coverage80 exceeds 100 percent")
            live_score = LiveScore(
                mae=_nonnegative(row.get("mae"), "live.mae"),
                coverage_percent=coverage,
                sample_count=_integer(row.get("n"), "live.n", minimum=1),
            )
        horizons.append(HorizonAccuracy(horizon, backtest_score, live_score))
    if not any(item.backtest or item.live for item in horizons):
        raise ForecastError("Wattcast returned no accuracy measurements")
    return AccuracyReport(
        retrieved_at=retrieved_at,
        model_trained_at=_iso_time(payload.get("modelTrainedAt"), "modelTrainedAt"),
        live_days=live_days,
        nominal_coverage_percent=target,
        band_method=_text(band.get("method"), "band.method"),
        horizons=tuple(horizons),
    )


def _reject_constant(value: str) -> None:
    raise ValueError("invalid JSON constant: " + value)


def _fetch_json(url: str) -> Tuple[Dict[str, object], datetime]:
    request = Request(
        url, headers={"Accept": "application/json", "User-Agent": "electro-forecast/1.0"}
    )
    try:
        with urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            body = response.read()
    except HTTPError as exc:
        retry_after = exc.headers.get("Retry-After") if exc.headers else None
        detail = "; Retry-After: {}".format(retry_after) if retry_after else ""
        raise ForecastError("Wattcast returned HTTP {}{}".format(exc.code, detail)) from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise ForecastError("Could not reach Wattcast: {}".format(exc)) from exc
    try:
        payload = json.loads(
            body.decode("utf-8"), parse_float=Decimal, parse_constant=_reject_constant
        )
    except (UnicodeDecodeError, ValueError) as exc:
        raise ForecastError("Wattcast returned invalid JSON") from exc
    return _object(payload, "Wattcast response"), datetime.now(UTC)


_forecast_cache: Optional[Tuple[float, ForecastReport]] = None
_accuracy_cache: Optional[Tuple[float, AccuracyReport]] = None
_cache_lock = Lock()


def fetch_forecast() -> ForecastReport:
    global _forecast_cache
    with _cache_lock:
        if _forecast_cache is not None:
            saved_at, report = _forecast_cache
            if monotonic() - saved_at < CACHE_SECONDS:
                return report
        payload, retrieved_at = _fetch_json(FORECAST_URL)
        report = normalize_forecast(payload, retrieved_at)
        _forecast_cache = (monotonic(), report)
        return report


def fetch_accuracy() -> AccuracyReport:
    global _accuracy_cache
    with _cache_lock:
        if _accuracy_cache is not None:
            saved_at, report = _accuracy_cache
            if monotonic() - saved_at < CACHE_SECONDS:
                return report
        payload, retrieved_at = _fetch_json(ACCURACY_URL)
        report = normalize_accuracy(payload, retrieved_at)
        _accuracy_cache = (monotonic(), report)
        return report
