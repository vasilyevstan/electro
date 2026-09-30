"""Read-only Elering customer API access for household electricity measurements."""

import json
import os
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from email.utils import parsedate_to_datetime
from math import isfinite
from typing import Callable, Dict, List, Literal, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

from nordpool_ee import UTC, current_delivery_date, market_bounds


API_BASE = "https://estfeed.elering.ee"
TOKEN_URL = "https://kc.elering.ee/realms/elering-sso/protocol/openid-connect/token"
KEYCHAIN_SERVICE = "elering-kodala-consumption"
REQUEST_TIMEOUT = 30
REQUEST_SPACING = 5
MAX_RETRY_DELAY = 30
POINT_CACHE_SECONDS = 300
Resolution = Literal["hourly", "15_minutes"]


class ConsumptionError(Exception):
    """An expected, safe-to-display household data failure."""


@dataclass(frozen=True)
class Credentials:
    client_id: str = field(repr=False)
    client_secret: str = field(repr=False)


@dataclass(frozen=True)
class DateWindow:
    start_date: date
    end_date: date
    start: datetime
    end: datetime


@dataclass(frozen=True)
class AccessPeriod:
    start: datetime
    end: Optional[datetime]


@dataclass(frozen=True)
class MeteringPoint:
    eic: str
    periods: Tuple[AccessPeriod, ...]


@dataclass(frozen=True)
class PointReport:
    points: Tuple[MeteringPoint, ...]
    retrieved_at: datetime


@dataclass(frozen=True)
class Reading:
    start: datetime
    consumption_kwh: Optional[Decimal]
    export_kwh: Optional[Decimal]


@dataclass(frozen=True)
class ConsumptionReport:
    window: DateWindow
    eic: str
    resolution: Resolution
    readings: Tuple[Reading, ...]
    retrieved_at: datetime


@dataclass(frozen=True)
class HttpResult:
    status: int
    payload: object
    retry_after: Optional[str]


def load_credentials() -> Credentials:
    names = ("ELERING_CLIENT_ID", "ELERING_CLIENT_SECRET")
    present = [name in os.environ for name in names]
    if any(present):
        if not all(present) or not all(os.environ[name].strip() for name in names):
            raise ConsumptionError(
                "Set both ELERING_CLIENT_ID and ELERING_CLIENT_SECRET, or neither "
                "to use macOS Keychain. Partial/empty credentials are not accepted."
            )
        return Credentials(os.environ[names[0]], os.environ[names[1]])
    if sys.platform != "darwin":
        raise ConsumptionError("Set ELERING_CLIENT_ID and ELERING_CLIENT_SECRET.")

    values = []
    for account in ("client_id", "client_secret"):
        try:
            result = subprocess.run(
                [
                    "/usr/bin/security", "find-generic-password",
                    "-s", KEYCHAIN_SERVICE, "-a", account, "-w",
                ],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                timeout=10, check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            raise ConsumptionError("macOS Keychain could not be read or is locked.") from None
        if result.returncode != 0:
            raise ConsumptionError(
                "Credential unavailable in macOS Keychain service "
                "'elering-kodala-consumption'; check setup and Keychain access."
            )
        try:
            value = result.stdout.decode("utf-8").rstrip("\n")
        except UnicodeDecodeError:
            raise ConsumptionError("Keychain credential has invalid text encoding.") from None
        if not value.strip():
            raise ConsumptionError("Keychain credential is empty.")
        values.append(value)
    return Credentials(values[0], values[1])


def make_window(
    start_date: str, end_date: Optional[str], now: datetime
) -> DateWindow:
    parsed = []
    for value in (start_date, end_date if end_date is not None else start_date):
        try:
            if not isinstance(value, str):
                raise ValueError
            item = date.fromisoformat(value)
            if item.isoformat() != value:
                raise ValueError
        except ValueError:
            raise ConsumptionError("Dates must use YYYY-MM-DD format.") from None
        parsed.append(item)
    first, last = parsed
    if last < first:
        raise ConsumptionError("end_date must not precede start_date.")
    if (last - first).days >= 31:
        raise ConsumptionError("Request at most 31 calendar days per call.")
    if last > current_delivery_date(now):
        raise ConsumptionError("Future dates have no measured consumption; end_date must be today or earlier.")
    try:
        start, _ = market_bounds(first)
        _, end = market_bounds(last)
    except (ValueError, OverflowError):
        raise ConsumptionError("Dates are outside the supported timezone bounds.") from None
    return DateWindow(first, last, start, end)


def interval_length(resolution: Resolution) -> timedelta:
    if resolution not in ("hourly", "15_minutes"):
        raise ConsumptionError("resolution must be hourly or 15_minutes.")
    return timedelta(minutes=60 if resolution == "hourly" else 15)


def _object(value: object, label: str) -> Dict[str, object]:
    if not isinstance(value, dict):
        raise ConsumptionError("Elering returned an invalid {} object.".format(label))
    return value


def _array(value: object, label: str) -> List[object]:
    if not isinstance(value, list):
        raise ConsumptionError("Elering returned an invalid {} array.".format(label))
    return value


def _timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise ConsumptionError("Elering returned an invalid interval/access timestamp.")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.utcoffset() is None:
            raise ValueError
        return result.astimezone(UTC)
    except (ValueError, OverflowError):
        raise ConsumptionError("Elering timestamp must include a valid UTC offset.") from None


def _eic(value: object) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[A-Z0-9-]{16}", value) is None:
        raise ConsumptionError("Elering returned an invalid metering-point EIC.")
    return value


def _energy(value: object) -> Optional[Decimal]:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise ConsumptionError("Elering energy values must be numeric or null.")
    number = Decimal(str(value)) if isinstance(value, float) else Decimal(value)
    if not number.is_finite() or not isfinite(float(number)):
        raise ConsumptionError("Elering energy values must be finite.")
    return number


def normalize_points(payload: object, retrieved_at: datetime) -> PointReport:
    points = []
    seen = set()
    for value in _array(payload, "metering points"):
        point = _object(value, "metering point")
        commodity = point.get("commodityType")
        if commodity == "NATURAL_GAS":
            continue
        if commodity != "ELECTRICITY":
            raise ConsumptionError("Elering returned an unrecognized commodity.")
        eic = _eic(point.get("eic"))
        if eic in seen:
            raise ConsumptionError("Elering returned duplicate metering points.")
        seen.add(eic)
        periods = []
        for value in _array(point.get("periods"), "access periods"):
            period = _object(value, "access period")
            start = _timestamp(period.get("from"))
            end = _timestamp(period["to"]) if period.get("to") is not None else None
            if end is not None and end <= start:
                raise ConsumptionError("Elering returned a reversed access period.")
            periods.append(AccessPeriod(start, end))
        points.append(MeteringPoint(eic, tuple(periods)))
    return PointReport(tuple(points), retrieved_at)


def normalize_readings(
    payload: object, eic: str, start: datetime, end: datetime, resolution: Resolution
) -> Tuple[Reading, ...]:
    meters = _array(payload, "metering data")
    if not meters:
        return ()
    if len(meters) != 1:
        raise ConsumptionError("Elering returned an unexpected number of metering points.")
    meter = _object(meters[0], "metering data")
    if meter.get("meteringPointEic") != eic:
        raise ConsumptionError("Elering returned data for an unexpected metering point.")
    if meter.get("error"):
        raise ConsumptionError(
            "Elering reported a metering-point access/data error inside its response."
        )
    step = interval_length(resolution)
    readings = {}
    for value in _array(meter.get("accountingIntervals"), "accounting intervals"):
        row = _object(value, "accounting interval")
        timestamp = _timestamp(row.get("periodStart"))
        consumption = _energy(row.get("consumptionKwh"))
        export = _energy(row.get("productionKwh"))
        if not start <= timestamp < end:
            continue
        if (timestamp - start) % step != timedelta(0):
            raise ConsumptionError("Elering interval is not aligned to the requested resolution.")
        if timestamp in readings:
            raise ConsumptionError("Elering returned duplicate accounting intervals.")
        readings[timestamp] = Reading(timestamp, consumption, export)
    return tuple(readings[key] for key in sorted(readings))


def _reject_constant(value: str) -> None:
    raise ValueError("Nonstandard JSON numeric constant")


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        raise ConsumptionError("Elering redirected a request; credentials were not forwarded.")


class EstfeedClient:
    def __init__(self, credentials_provider: Callable[[], Credentials] = load_credentials):
        self._credentials_provider = credentials_provider
        self._opener = build_opener(_NoRedirect())
        self._lock = threading.RLock()
        self._last_request: Optional[float] = None
        self._access_token: Optional[str] = None
        self._token_expires = 0.0
        self._points_cache: Optional[Tuple[DateWindow, float, PointReport]] = None

    def _request(self, request: Request) -> HttpResult:
        if self._last_request is not None:
            wait = REQUEST_SPACING - (time.monotonic() - self._last_request)
            if wait > 0:
                time.sleep(wait)
        try:
            with self._opener.open(request, timeout=REQUEST_TIMEOUT) as response:
                status = response.status
                retry_after = response.headers.get("Retry-After")
                body = response.read() if status == 200 else b""
        except HTTPError as error:
            status = error.code
            retry_after = error.headers.get("Retry-After") if error.headers else None
            error.close()
            body = b""
        except (URLError, OSError, TimeoutError):
            raise ConsumptionError("Could not reach Elering; check connectivity and retry.") from None
        finally:
            self._last_request = time.monotonic()
        if status != 200:
            return HttpResult(status, None, retry_after)
        try:
            payload = json.loads(
                body.decode("utf-8"), parse_float=Decimal, parse_constant=_reject_constant
            )
        except (UnicodeDecodeError, ValueError):
            raise ConsumptionError("Elering returned invalid JSON.") from None
        return HttpResult(status, payload, retry_after)

    def _request_with_rate_retry(self, request: Request) -> HttpResult:
        result = self._request(request)
        if result.status != 429:
            return result
        delay = float(REQUEST_SPACING)
        if result.retry_after is not None:
            try:
                delay = float(result.retry_after)
            except ValueError:
                try:
                    retry_at = parsedate_to_datetime(result.retry_after)
                    if retry_at.utcoffset() is None:
                        raise ValueError
                    delay = max(0.0, (retry_at - datetime.now(UTC)).total_seconds())
                except (TypeError, ValueError, OverflowError):
                    raise ConsumptionError("Elering rate-limited the request; retry later.") from None
        if not isfinite(delay) or delay < 0 or delay > MAX_RETRY_DELAY:
            raise ConsumptionError("Elering rate-limited the request beyond the retry budget; retry later.")
        time.sleep(delay)
        result = self._request(request)
        if result.status == 429:
            raise ConsumptionError(
                "Elering rate limit persists; other applications sharing this key may be active."
            )
        return result

    def _token(self) -> str:
        if self._access_token and time.monotonic() < self._token_expires:
            return self._access_token
        credentials = self._credentials_provider()
        body = urlencode(
            {
                "grant_type": "client_credentials",
                "client_id": credentials.client_id,
                "client_secret": credentials.client_secret,
            }
        ).encode("utf-8")
        result = self._request_with_rate_retry(
            Request(
                TOKEN_URL, data=body, method="POST",
                headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
            )
        )
        if result.status != 200:
            raise ConsumptionError(
                "Elering authentication failed (HTTP {}); check the configured client credentials."
                .format(result.status)
            )
        payload = _object(result.payload, "OAuth token")
        token = payload.get("access_token")
        token_type = payload.get("token_type")
        expires = _energy(payload.get("expires_in"))
        if (
            not isinstance(token, str) or not token.strip()
            or "\r" in token or "\n" in token
            or not isinstance(token_type, str) or token_type.lower() != "bearer"
            or expires is None or expires <= 0
        ):
            raise ConsumptionError("Elering returned an invalid OAuth token response.")
        lifetime = float(expires)
        self._access_token = token
        self._token_expires = time.monotonic() + lifetime - min(30, lifetime / 10)
        return token

    def _get(self, path: str, parameters: Dict[str, str]) -> object:
        url = API_BASE + path + "?" + urlencode(parameters)
        for attempt in range(2):
            token = self._token()
            result = self._request_with_rate_retry(
                Request(url, headers={"Accept": "application/json", "Authorization": "Bearer " + token})
            )
            if result.status == 200:
                return result.payload
            if result.status == 401 and attempt == 0:
                self._access_token = None
                self._points_cache = None
                continue
            if result.status in (401, 403):
                raise ConsumptionError(
                    "Elering denied access (HTTP {}); check credential validity and household permissions."
                    .format(result.status)
                )
            raise ConsumptionError("Elering data request failed (HTTP {}).".format(result.status))
        raise ConsumptionError("Elering authentication retry failed.")

    @staticmethod
    def _parameters(start: datetime, end: datetime) -> Dict[str, str]:
        return {
            "startDateTime": start.astimezone(UTC).isoformat().replace("+00:00", "Z"),
            "endDateTime": end.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        }

    @staticmethod
    def _ranges(window: DateWindow) -> List[Tuple[datetime, datetime]]:
        ranges = []
        start = window.start
        while start < window.end:
            end = min(start + timedelta(days=31), window.end)
            ranges.append((start, end))
            start = end
        return ranges

    def list_metering_points(self, window: DateWindow) -> PointReport:
        with self._lock:
            if self._points_cache is not None:
                saved_window, saved_at, report = self._points_cache
                if window == saved_window and time.monotonic() - saved_at < POINT_CACHE_SECONDS:
                    return report
            points: Dict[str, MeteringPoint] = {}
            for start, end in self._ranges(window):
                payload = self._get(
                    "/api/public/v1/metering-point-eics", self._parameters(start, end)
                )
                batch = normalize_points(payload, datetime.now(UTC))
                for point in batch.points:
                    previous = points.get(point.eic)
                    periods = set(point.periods)
                    if previous is not None:
                        periods.update(previous.periods)
                    ordered = sorted(
                        periods,
                        key=lambda period: (
                            period.start, period.end or datetime.max.replace(tzinfo=UTC)
                        ),
                    )
                    points[point.eic] = MeteringPoint(point.eic, tuple(ordered))
            report = PointReport(
                tuple(points[eic] for eic in sorted(points)), datetime.now(UTC)
            )
            self._points_cache = (window, time.monotonic(), report)
            return report

    def get_consumption(self, window: DateWindow, resolution: Resolution) -> ConsumptionReport:
        interval_length(resolution)
        with self._lock:
            points = self.list_metering_points(window).points
            if not points:
                raise ConsumptionError("No authorized electricity metering point exists for this period.")
            if len(points) != 1:
                raise ConsumptionError(
                    "Multiple electricity metering points are authorized; household selection "
                    "is ambiguous. No measurements were requested."
                )
            readings = []
            for start, end in self._ranges(window):
                parameters = self._parameters(start, end)
                parameters.update(
                    meteringPointEics=points[0].eic,
                    resolution="one_hour" if resolution == "hourly" else "fifteen_minutes",
                )
                payload = self._get("/api/public/v1/metering-data", parameters)
                readings.extend(normalize_readings(payload, points[0].eic, start, end, resolution))
            return ConsumptionReport(
                window, points[0].eic, resolution, tuple(readings), datetime.now(UTC)
            )
