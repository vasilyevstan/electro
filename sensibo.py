"""Private inventory and cloud access for Sensibo Sky climate controllers."""

from __future__ import annotations

import argparse
import gzip
import io
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import threading
import time
import zlib
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from math import isfinite
from pathlib import Path
from typing import Callable, Dict, List, Literal, Optional, Tuple, Union
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, OpenerDirector, Request, build_opener


API_BASE = "https://home.sensibo.com/api/v2"
KEYCHAIN_SERVICE = "sensibo-mcp"
REQUEST_TIMEOUT = 20
CACHE_SECONDS = 60
STALE_SECONDS = 600
MAX_RETRY_DELAY = 30
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
UTC = timezone.utc
FIELDS = (
    "id,macAddress,productModel,room,connectionStatus,homekitSupported,"
    "measurements,acState,remoteCapabilities,blockModeChange,"
    "minimumCoolingTemperature,maximumHeatingTemperature,restrictedMode"
)
_METRICS = {
    "temperature": ("C", "measured"),
    "humidity": ("%", "measured"),
    "feelsLike": ("C", "derived"),
    "rssi": ("dBm", "measured"),
}
StateValue = Union[bool, float, str]


class SensiboError(Exception):
    """An expected failure that contains no credentials or private response body."""


@dataclass(frozen=True)
class Credentials:
    api_key: str = field(repr=False)


@dataclass(frozen=True)
class KnownDevice:
    device_id: str
    mac: str
    model: str
    name: Optional[str]
    verified_at: str
    source: str = API_BASE + "/users/me/pods"


@dataclass(frozen=True)
class ModeCapabilities:
    temperatures: Dict[str, List[float]]
    fan_levels: Optional[List[str]]
    swing: Optional[List[str]]
    horizontal_swing: Optional[List[str]]


@dataclass(frozen=True)
class AcState:
    on: Optional[bool]
    mode: Optional[str]
    target_temperature: Optional[float]
    temperature_unit: Optional[str]
    fan_level: Optional[str]
    swing: Optional[str]
    horizontal_swing: Optional[str]
    reported_at: Optional[str]


@dataclass(frozen=True)
class SensorReading:
    metric: str
    value: Optional[float]
    unit: str
    kind: str
    status: Literal["available", "unavailable"]
    measured_at: Optional[str]
    age_seconds: Optional[float]
    stale: Optional[bool]


@dataclass(frozen=True)
class DeviceInfo:
    device_id: str
    mac: str
    model: str
    name: Optional[str]
    identity_verified_at: str
    seen_in_account: bool
    online: Optional[bool]
    last_seen: Optional[str]
    homekit_supported: Optional[bool]
    capabilities: Dict[str, ModeCapabilities]


@dataclass(frozen=True)
class DeviceInventory:
    source: str
    retrieved_at: str
    cache_age_seconds: float
    devices: List[DeviceInfo]
    unenrolled_device_count: int
    warnings: List[str]


@dataclass(frozen=True)
class DeviceState:
    device: DeviceInfo
    source: str
    retrieved_at: str
    cache_age_seconds: float
    ac_state: Optional[AcState]
    measurements: List[SensorReading]
    block_mode_change: Optional[bool]
    minimum_cooling_temperature_c: Optional[float]
    maximum_heating_temperature_c: Optional[float]
    restricted_mode: Optional[bool]
    warnings: List[str]


@dataclass(frozen=True)
class HistoryPoint:
    time: str
    value: Optional[float]


@dataclass(frozen=True)
class SensorHistory:
    metric: str
    unit: str
    kind: str
    samples: List[HistoryPoint]
    available_start: Optional[str]
    available_end: Optional[str]


@dataclass(frozen=True)
class MeasurementHistory:
    device_id: str
    mac: str
    source: str
    requested_days: int
    window_start: str
    window_end: str
    retrieved_at: str
    cache_age_seconds: float
    timezone: str
    series: List[SensorHistory]
    warnings: List[str]


@dataclass(frozen=True)
class ControlResult:
    device_id: str
    requested: Dict[str, StateValue]
    command_sent: bool
    api_acknowledged: bool
    cloud_state_matches: bool
    physical_effect_verified: bool
    state: DeviceState
    warnings: List[str]


def utc_now() -> datetime:
    return datetime.now(UTC)


def load_credentials() -> Credentials:
    if "SENSIBO_API_KEY" in os.environ:
        value = os.environ["SENSIBO_API_KEY"]
    elif sys.platform == "darwin":
        try:
            result = subprocess.run(
                ["/usr/bin/security", "find-generic-password", "-s", KEYCHAIN_SERVICE,
                 "-a", "api_key", "-w"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=10, check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            raise SensiboError("Sensibo Keychain access failed or is locked.") from None
        if result.returncode != 0:
            raise SensiboError(
                "Set SENSIBO_API_KEY or configure Keychain service 'sensibo-mcp', "
                "account 'api_key'. A local MCP cannot retrieve GitHub secret values."
            )
        try:
            value = result.stdout.decode("utf-8").rstrip("\n")
        except UnicodeDecodeError:
            raise SensiboError("The Sensibo Keychain credential has invalid encoding.") from None
    else:
        raise SensiboError("Set SENSIBO_API_KEY in the MCP process environment.")
    if not value or value != value.strip() or any(character.isspace() for character in value):
        raise SensiboError("SENSIBO_API_KEY must be nonempty and contain no whitespace.")
    return Credentials(value)


def _object(value: object, label: str) -> dict:
    if not isinstance(value, dict):
        raise SensiboError("Sensibo returned an invalid {} object.".format(label))
    return value


def _optional_object(value: object, label: str) -> dict:
    return {} if value is None else _object(value, label)


def _text(value: object, label: str) -> Optional[str]:
    if value is not None and (not isinstance(value, str) or not value):
        raise SensiboError("Sensibo returned an invalid {} string.".format(label))
    return value


def _number(value: object) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SensiboError("Sensibo measurements and temperatures must be numeric or null.")
    try:
        result = float(value)
    except OverflowError:
        raise SensiboError("Sensibo returned a number outside the supported range.") from None
    if not isfinite(result):
        raise SensiboError("Sensibo returned a non-finite number.")
    return result


def _boolean(value: object) -> Optional[bool]:
    if value is not None and type(value) is not bool:
        raise SensiboError("Sensibo returned an invalid boolean.")
    return value


def _timestamp(value: object) -> Optional[datetime]:
    if isinstance(value, dict):
        value = value.get("time")
    if value is None:
        return None
    try:
        if not isinstance(value, str):
            raise ValueError
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.utcoffset() is None:
            raise ValueError
        return result.astimezone(UTC)
    except (ValueError, OverflowError):
        raise SensiboError("Sensibo timestamps must contain a valid UTC offset.") from None


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value is not None else None


def _device_id(value: object) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value) is None:
        raise SensiboError("Use an exact Sensibo device ID from the enrolled inventory.")
    return value


def normalize_mac(value: object) -> str:
    if not isinstance(value, str):
        raise SensiboError("A device MAC address is missing or invalid.")
    if re.fullmatch(r"[0-9a-fA-F]{12}", value):
        compact = value
    elif re.fullmatch(r"[0-9a-fA-F]{2}([:-])(?:[0-9a-fA-F]{2}\1){4}[0-9a-fA-F]{2}", value):
        compact = value.replace(":", "").replace("-", "")
    else:
        raise SensiboError("A device MAC address must have six hexadecimal octets.")
    if compact == "000000000000" or int(compact[:2], 16) & 1:
        raise SensiboError("A device MAC address must be a nonzero unicast address.")
    return ":".join(compact[index:index + 2].upper() for index in range(0, 12, 2))


def _identity(pod: dict, verified_at: datetime) -> KnownDevice:
    model = _text(pod.get("productModel"), "model")
    if model is None:
        raise SensiboError("Sensibo did not identify a device model.")
    room = pod.get("room")
    name = _text(_object(room, "room").get("name"), "room name") if room is not None else None
    return KnownDevice(
        _device_id(pod.get("id")), normalize_mac(pod.get("macAddress")),
        model, name, verified_at.isoformat(),
    )


class Registry:
    def __init__(self, path: Optional[Path] = None):
        self.path = path if path is not None else Path.home() / ".config/electro/sensibo/devices.json"

    @staticmethod
    def _private(path: Path, directory: bool = False) -> None:
        details = path.lstat()
        correct_type = stat.S_ISDIR(details.st_mode) if directory else stat.S_ISREG(details.st_mode)
        if not correct_type or details.st_mode & 0o077:
            raise SensiboError("Sensibo inventory must use a private directory (700) and regular file (600).")
        if hasattr(os, "getuid") and details.st_uid != os.getuid():
            raise SensiboError("The Sensibo inventory must be owned by the current user.")

    def load(self) -> List[KnownDevice]:
        try:
            if self.path.parent.exists() or self.path.parent.is_symlink():
                self._private(self.path.parent, directory=True)
            if not self.path.exists() and not self.path.is_symlink():
                return []
            self._private(self.path)
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError):
            raise SensiboError("Could not read the private Sensibo inventory; it was not replaced.") from None
        document = _object(payload, "inventory")
        if type(document.get("version")) is not int or document["version"] != 1 or not isinstance(document.get("devices"), list):
            raise SensiboError("The Sensibo inventory has an unsupported schema.")
        records = []
        for value in document["devices"]:
            item = _object(value, "inventory entry")
            verified_at = _timestamp(item.get("verified_at"))
            model = _text(item.get("model"), "inventory model")
            if verified_at is None or model is None or item.get("source") != API_BASE + "/users/me/pods":
                raise SensiboError("The Sensibo inventory has an unverified identity.")
            records.append(KnownDevice(
                _device_id(item.get("device_id")), normalize_mac(item.get("mac")),
                model, _text(item.get("name"), "inventory name"), verified_at.isoformat(),
            ))
        self._unique(records)
        return records

    @staticmethod
    def _unique(records: List[KnownDevice]) -> None:
        if len({item.device_id for item in records}) != len(records) or len({item.mac for item in records}) != len(records):
            raise SensiboError("Conflicting Sensibo device IDs or MAC addresses; no identities were reassigned.")

    def save(self, records: List[KnownDevice]) -> None:
        self._unique(records)
        temporary = None
        try:
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            self._private(self.path.parent, directory=True)
            if self.path.exists() or self.path.is_symlink():
                self._private(self.path)
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=self.path.parent, prefix="devices.", suffix=".tmp", delete=False,
            ) as output:
                temporary = output.name
                json.dump({"version": 1, "devices": [asdict(item) for item in sorted(records, key=lambda item: item.device_id)]}, output, indent=2)
                output.write("\n")
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.path)
            temporary = None
        except OSError:
            raise SensiboError("Could not persist the private Sensibo inventory.") from None
        finally:
            if temporary is not None:
                os.unlink(temporary)


def _options(value: object) -> Optional[List[str]]:
    if value is None:
        return None
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        raise SensiboError("Sensibo returned invalid control options.")
    return value


def _capabilities(pod: dict) -> Dict[str, ModeCapabilities]:
    if pod.get("remoteCapabilities") is None:
        return {}
    raw_modes = _object(_object(pod["remoteCapabilities"], "capabilities").get("modes"), "modes")
    modes = {}
    for mode, raw in raw_modes.items():
        if not isinstance(mode, str) or not mode:
            raise SensiboError("Sensibo returned an invalid mode.")
        data = _object(raw, "mode capability")
        temperatures = {}
        for unit, specification in _object(data.get("temperatures", {}), "temperatures").items():
            values = _object(specification, "temperature capability").get("values")
            if unit not in ("C", "F") or not isinstance(values, list) or any(value is None for value in values):
                raise SensiboError("Sensibo returned unsupported temperature capabilities.")
            numbers = []
            for value in values:
                number = _number(value)
                if number is None:
                    raise SensiboError("Sensibo returned a null temperature capability.")
                numbers.append(number)
            temperatures[unit] = numbers
        modes[mode] = ModeCapabilities(
            temperatures, _options(data.get("fanLevels")),
            _options(data.get("swing")), _options(data.get("horizontalSwing")),
        )
    return modes


def _ac_state(pod: dict) -> Optional[AcState]:
    if pod.get("acState") is None:
        return None
    state = _object(pod["acState"], "AC state")
    return AcState(
        _boolean(state.get("on")), _text(state.get("mode"), "mode"),
        _number(state.get("targetTemperature")), _text(state.get("temperatureUnit"), "temperature unit"),
        _text(state.get("fanLevel"), "fan level"), _text(state.get("swing"), "swing"),
        _text(state.get("horizontalSwing"), "horizontal swing"), _iso(_timestamp(state.get("timestamp"))),
    )


def _info(known: KnownDevice, pod: Optional[dict]) -> DeviceInfo:
    connection = _optional_object(pod.get("connectionStatus"), "connection") if pod is not None else {}
    return DeviceInfo(
        known.device_id, known.mac, known.model, known.name, known.verified_at,
        pod is not None, _boolean(connection.get("isAlive")), _iso(_timestamp(connection.get("lastSeen"))),
        _boolean(pod.get("homekitSupported")) if pod is not None else None,
        _capabilities(pod) if pod is not None else {},
    )


def _state(known: KnownDevice, pod: dict, retrieved_at: datetime, now: datetime) -> DeviceState:
    info = _info(known, pod)
    raw = _optional_object(pod.get("measurements"), "measurements")
    measured_at = _timestamp(raw.get("time"))
    age = max(0.0, (now - measured_at).total_seconds()) if measured_at is not None else None
    warnings = ["AC state is cloud-reported; it is not independent verification of the physical HVAC unit."]
    if info.online is not True:
        warnings.append("The device is offline or its connectivity is unknown; readings may be historical.")
    if measured_at is None:
        warnings.append("The sensor timestamp is unavailable; freshness is unknown.")
    elif measured_at > now + timedelta(seconds=60):
        raise SensiboError("Sensibo returned a sensor timestamp too far in the future.")
    elif age is not None and age > STALE_SECONDS:
        warnings.append("Sensor readings are older than 10 minutes.")
    readings = []
    for metric, (unit, kind) in _METRICS.items():
        value = _number(raw.get(metric))
        readings.append(SensorReading(
            metric, value, unit, kind, "available" if value is not None else "unavailable",
            _iso(measured_at), age, age > STALE_SECONDS if age is not None else None,
        ))
    if any(item.status == "unavailable" for item in readings):
        warnings.append("Unavailable sensor readings are not zero.")
    if not info.capabilities:
        warnings.append("Remote-control capabilities are unavailable; control is disabled.")
    return DeviceState(
        info, API_BASE, retrieved_at.isoformat(), max(0.0, (now - retrieved_at).total_seconds()),
        _ac_state(pod), readings, _boolean(pod.get("blockModeChange")),
        _number(pod.get("minimumCoolingTemperature")), _number(pod.get("maximumHeatingTemperature")),
        _boolean(pod.get("restrictedMode")), warnings,
    )


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _reject_constant(value: str) -> None:
    raise ValueError("Invalid JSON numeric constant")


class SensiboClient:
    def __init__(
        self, registry: Optional[Registry] = None,
        credentials_provider: Callable[[], Credentials] = load_credentials,
        opener: Optional[OpenerDirector] = None,
        now: Callable[[], datetime] = utc_now,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.registry = registry if registry is not None else Registry()
        self._credentials_provider = credentials_provider
        self._opener = opener if opener is not None else build_opener(_NoRedirect())
        self._now = now
        self._sleep = sleep
        self._lock = threading.RLock()
        self._snapshot: Optional[Tuple[datetime, Dict[str, dict]]] = None
        self._history: Dict[Tuple[str, int], Tuple[datetime, dict]] = {}

    def _retry_delay(self, value: Optional[str]) -> float:
        if value is None:
            return 1.0
        try:
            delay = float(value)
        except ValueError:
            try:
                target = parsedate_to_datetime(value)
                if target.utcoffset() is None:
                    raise ValueError
                delay = max(0.0, (target - self._now()).total_seconds())
            except (TypeError, ValueError, OverflowError):
                raise SensiboError("Sensibo rate-limited the request; retry later.") from None
        if not isfinite(delay) or not 0 <= delay <= MAX_RETRY_DELAY:
            raise SensiboError("Sensibo rate limit exceeds the retry budget; retry later.")
        return delay

    def _request(self, method: str, path: str, parameters: Optional[dict] = None, body: Optional[dict] = None) -> object:
        credentials = self._credentials_provider()
        query = dict(parameters or {}, apiKey=credentials.api_key)
        request = Request(
            API_BASE + path + "?" + urlencode(query),
            data=json.dumps(body, allow_nan=False).encode("utf-8") if body is not None else None,
            method=method, headers={"Accept": "application/json", "Accept-Encoding": "gzip", "Content-Type": "application/json"},
        )
        for attempt in range(2 if method == "GET" else 1):
            try:
                with self._opener.open(request, timeout=REQUEST_TIMEOUT) as response:
                    content = response.read(MAX_RESPONSE_BYTES + 1)
                    encoding = response.headers.get("Content-Encoding", "").lower()
            except HTTPError as error:
                status = error.code
                retry_after = error.headers.get("Retry-After") if error.headers else None
                error.close()
                if status == 429 and method == "GET" and attempt == 0:
                    self._sleep(self._retry_delay(retry_after))
                    continue
                if status in (401, 403):
                    raise SensiboError("Sensibo denied API access (HTTP {}); check the key and account permissions.".format(status)) from None
                if status == 404:
                    raise SensiboError("Sensibo cannot access this device or endpoint (HTTP 404).") from None
                raise SensiboError("Sensibo request failed (HTTP {}); no successful result was confirmed.".format(status)) from None
            except (URLError, OSError, TimeoutError):
                raise SensiboError("Sensibo could not be reached; no successful result was confirmed.") from None
            if len(content) > MAX_RESPONSE_BYTES:
                raise SensiboError("Sensibo response exceeds the supported size.")
            try:
                if encoding == "gzip":
                    with gzip.GzipFile(fileobj=io.BytesIO(content)) as compressed:
                        content = compressed.read(MAX_RESPONSE_BYTES + 1)
                elif encoding:
                    raise SensiboError("Sensibo returned unsupported content encoding.")
                if len(content) > MAX_RESPONSE_BYTES:
                    raise SensiboError("Sensibo response exceeds the supported size.")
                payload = json.loads(content.decode("utf-8"), parse_constant=_reject_constant)
            except (UnicodeError, ValueError, OSError, EOFError, zlib.error):
                raise SensiboError("Sensibo returned invalid JSON or compressed content.") from None
            document = _object(payload, "API response")
            if document.get("status") != "success" or "result" not in document:
                raise SensiboError("Sensibo did not acknowledge a successful API result.")
            return document["result"]
        raise SensiboError("Sensibo rate limit persists; retry later.")

    def _devices(self, refresh: bool = False) -> Tuple[datetime, Dict[str, dict]]:
        if self._snapshot is not None and not refresh:
            age = (self._now() - self._snapshot[0]).total_seconds()
            if 0 <= age < CACHE_SECONDS:
                return self._snapshot
        result = self._request("GET", "/users/me/pods", {"fields": FIELDS})
        if not isinstance(result, list):
            raise SensiboError("Sensibo returned an invalid device list.")
        retrieved_at = self._now()
        pods = {}
        identities = []
        for value in result:
            pod = _object(value, "device")
            known = _identity(pod, retrieved_at)
            identities.append(known)
            pods[known.device_id] = pod
        Registry._unique(identities)
        self._snapshot = (retrieved_at, pods)
        return self._snapshot

    def _records(self) -> List[KnownDevice]:
        records = self.registry.load()
        if not records:
            raise SensiboError("No Sensibo devices are enrolled. Run 'python -m sensibo --enroll-account --expected-devices 4' during setup.")
        return records

    def _known(self, device_id: str) -> KnownDevice:
        _device_id(device_id)
        for known in self._records():
            if known.device_id == device_id:
                if known.model != "skyv2":
                    raise SensiboError("This MCP currently supports enrolled Sensibo Sky (skyv2) devices.")
                return known
        raise SensiboError("This device ID is not enrolled; no command or device-specific request was sent.")

    @staticmethod
    def _match(known: KnownDevice, pod: dict) -> None:
        if (
            pod.get("id") != known.device_id or normalize_mac(pod.get("macAddress")) != known.mac
            or pod.get("productModel") != known.model
        ):
            raise SensiboError("Sensibo identity no longer matches the enrolled ID/MAC/model; control is blocked.")

    def enroll_account(self, expected_devices: int) -> List[KnownDevice]:
        if type(expected_devices) is not int or expected_devices < 1:
            raise SensiboError("Specify the positive expected number of account devices.")
        with self._lock:
            previous = {item.device_id: item for item in self.registry.load()}
            retrieved_at, pods = self._devices(refresh=True)
            if len(pods) != expected_devices:
                raise SensiboError("The account device count differs from --expected-devices; nothing was enrolled.")
            for pod in pods.values():
                known = _identity(pod, retrieved_at)
                if known.model != "skyv2":
                    raise SensiboError("Only Sensibo Sky (skyv2) is supported; nothing was enrolled.")
                if known.device_id in previous:
                    self._match(previous[known.device_id], pod)
                previous[known.device_id] = known
            records = list(previous.values())
            self.registry.save(records)
            return records

    def list_devices(self) -> DeviceInventory:
        with self._lock:
            records = self._records()
            retrieved_at, pods = self._devices()
            devices = []
            warnings = []
            for known in records:
                pod = pods.get(known.device_id)
                if pod is not None:
                    self._match(known, pod)
                else:
                    warnings.append("An enrolled device is absent from this account response; its inventory identity is retained.")
                devices.append(_info(known, pod))
            unenrolled = len(set(pods) - {item.device_id for item in records})
            if unenrolled:
                warnings.append("Additional account devices are not enrolled and cannot be controlled.")
            return DeviceInventory(
                API_BASE, retrieved_at.isoformat(), max(0.0, (self._now() - retrieved_at).total_seconds()),
                devices, unenrolled, warnings,
            )

    def get_device(self, device_id: str, refresh: bool = False) -> DeviceState:
        with self._lock:
            known = self._known(device_id)
            retrieved_at, pods = self._devices(refresh)
            if device_id not in pods:
                raise SensiboError("The enrolled device is absent from this account response; current data is unavailable.")
            pod = pods[device_id]
            self._match(known, pod)
            return _state(known, pod, retrieved_at, self._now())

    def get_measurements(self, device_id: str, days: int = 1) -> MeasurementHistory:
        if type(days) is not int or not 1 <= days <= 7:
            raise SensiboError("History requests must cover 1-7 days.")
        with self._lock:
            state = self.get_device(device_id)
            key = (device_id, days)
            saved = self._history.get(key)
            if saved is None or not 0 <= (self._now() - saved[0]).total_seconds() < CACHE_SECONDS:
                result = self._request("GET", "/pods/{}/historicalMeasurements".format(device_id), {"days": days})
                saved = (self._now(), _object(result, "historical measurements"))
                self._history[key] = saved
            retrieved_at, history = saved
            start = retrieved_at - timedelta(days=days)
            series = []
            warnings = [
                "This is provider-available history, not a continuous local recording. Retention, gaps, and sampling cadence are not guaranteed.",
                "Samples are not interpolated; missing and null values are not zero. Temperature measurements are Celsius, independently of the AC setpoint unit.",
            ]
            for metric, (unit, kind) in _METRICS.items():
                if metric not in history:
                    continue
                raw_samples = history[metric]
                if not isinstance(raw_samples, list):
                    raise SensiboError("Sensibo returned an invalid history series.")
                points = {}
                for raw in raw_samples:
                    sample = _object(raw, "history sample")
                    timestamp = _timestamp(sample.get("time"))
                    if timestamp is None:
                        raise SensiboError("Sensibo returned a history sample without a timestamp.")
                    value = _number(sample.get("value"))
                    if not start <= timestamp <= retrieved_at:
                        continue
                    if timestamp in points and points[timestamp] != value:
                        raise SensiboError("Sensibo returned conflicting history samples.")
                    points[timestamp] = value
                samples = [HistoryPoint(timestamp.isoformat(), points[timestamp]) for timestamp in sorted(points)]
                series.append(SensorHistory(
                    metric, unit, kind, samples, samples[0].time if samples else None, samples[-1].time if samples else None,
                ))
            if not any(sample.value is not None for item in series for sample in item.samples):
                raise SensiboError("No usable measurements are available in the requested history window.")
            if state.device.online is not True:
                warnings.append("The device is offline or connectivity is unknown; existing history can still be available.")
            if any(sample.value is None for item in series for sample in item.samples):
                warnings.append("The provider returned null history samples.")
            return MeasurementHistory(
                device_id, state.device.mac, API_BASE, days, start.isoformat(), retrieved_at.isoformat(),
                retrieved_at.isoformat(), max(0.0, (self._now() - retrieved_at).total_seconds()), "UTC", series, warnings,
            )

    @staticmethod
    def _updates(
        on: Optional[bool], mode: Optional[str], target_temperature: Optional[float],
        temperature_unit: Optional[str], fan_level: Optional[str],
        swing: Optional[str], horizontal_swing: Optional[str],
    ) -> Dict[str, StateValue]:
        if on is not None and type(on) is not bool:
            raise SensiboError("on must be a boolean, not a toggle or number.")
        values: Dict[str, StateValue] = {}
        for name, value in (
            ("mode", mode), ("temperatureUnit", temperature_unit),
            ("fanLevel", fan_level), ("swing", swing), ("horizontalSwing", horizontal_swing),
        ):
            if value is not None:
                if not isinstance(value, str) or not value:
                    raise SensiboError("Control options must be nonempty strings.")
                values[name] = value
        if on is not None:
            values["on"] = on
        if (target_temperature is None) != (temperature_unit is None):
            raise SensiboError("Supply target_temperature and its explicit temperature_unit together.")
        if target_temperature is not None:
            target = _number(target_temperature)
            if target is None or temperature_unit not in ("C", "F"):
                raise SensiboError("A target temperature needs a valid numeric value and unit C or F.")
            values["targetTemperature"] = target
        if not values:
            raise SensiboError("Supply at least one explicit desired state; empty updates and toggles are not supported.")
        return values

    @staticmethod
    def _validate_update(state: DeviceState, updates: Dict[str, StateValue]) -> None:
        if state.device.online is not True:
            raise SensiboError("The device is offline or connectivity is unknown; no command was sent.")
        if state.restricted_mode is True:
            raise SensiboError("Sensibo marks this device as restricted; no command was sent.")
        current = state.ac_state
        if current is None or not state.device.capabilities:
            raise SensiboError("Current AC state or remote capabilities are unavailable; no command was sent.")
        selected_mode = updates.get("mode", current.mode)
        if not isinstance(selected_mode, str) or selected_mode not in state.device.capabilities:
            raise SensiboError("Choose a mode listed in this device's capabilities.")
        mode_changed = selected_mode != current.mode
        if mode_changed and state.block_mode_change:
            raise SensiboError("Sensibo blocks mode changes for this device.")
        capabilities = state.device.capabilities[selected_mode]
        for name, old, options in (
            ("fanLevel", current.fan_level, capabilities.fan_levels),
            ("swing", current.swing, capabilities.swing),
            ("horizontalSwing", current.horizontal_swing, capabilities.horizontal_swing),
        ):
            if name in updates:
                if options is None or updates[name] not in options:
                    raise SensiboError("The requested {} is unsupported in the selected mode.".format(name))
            elif mode_changed and options is not None and old is not None and old not in options:
                raise SensiboError("The mode change needs an explicit compatible {} rather than silently resetting it.".format(name))
        if "targetTemperature" in updates or (capabilities.temperatures and (mode_changed or updates.get("on") is True)):
            target = updates.get("targetTemperature", current.target_temperature)
            unit = updates.get("temperatureUnit", current.temperature_unit)
            if not isinstance(unit, str) or target not in capabilities.temperatures.get(unit, []):
                raise SensiboError("Choose an explicit temperature and unit from the selected mode's capabilities.")
            if isinstance(target, bool) or not isinstance(target, (int, float)):
                raise SensiboError("A numeric target temperature is required.")
            celsius = target if unit == "C" else (target - 32) * 5 / 9
            if selected_mode == "cool" and state.minimum_cooling_temperature_c is not None and celsius < state.minimum_cooling_temperature_c:
                raise SensiboError("The target is below the device's minimum cooling temperature.")
            if selected_mode == "heat" and state.maximum_heating_temperature_c is not None and celsius > state.maximum_heating_temperature_c:
                raise SensiboError("The target exceeds the device's maximum heating temperature.")

    def set_state(
        self, device_id: str, *, on: Optional[bool] = None, mode: Optional[str] = None,
        target_temperature: Optional[float] = None, temperature_unit: Optional[str] = None,
        fan_level: Optional[str] = None, swing: Optional[str] = None, horizontal_swing: Optional[str] = None,
    ) -> ControlResult:
        updates = self._updates(on, mode, target_temperature, temperature_unit, fan_level, swing, horizontal_swing)
        with self._lock:
            state = self.get_device(device_id, refresh=True)
            self._validate_update(state, updates)
            self._snapshot = None
            try:
                result = self._request("POST", "/pods/{}/acStates".format(device_id), body={"acState": updates})
                if isinstance(result, dict) and "status" in result and result["status"] not in ("success", "Success"):
                    raise SensiboError("Sensibo did not confirm command acceptance.")
                after = self.get_device(device_id, refresh=True)
            except SensiboError as error:
                self._snapshot = None
                raise SensiboError(
                    "Control outcome is uncertain; the command may have applied. Do not automatically retry. "
                    "Read the device state before deciding what to do next. " + str(error)
                ) from None
            reported = after.ac_state
            actual = {} if reported is None else {
                "on": reported.on, "mode": reported.mode, "targetTemperature": reported.target_temperature,
                "temperatureUnit": reported.temperature_unit, "fanLevel": reported.fan_level,
                "swing": reported.swing, "horizontalSwing": reported.horizontal_swing,
            }
            if after.device.online is not True or any(actual.get(name) != value for name, value in updates.items()):
                self._snapshot = None
                raise SensiboError("Sensibo accepted the request, but fresh cloud state did not confirm it. The outcome is uncertain; do not automatically resend.")
            return ControlResult(
                device_id, updates, True, True, True, False, after,
                ["Sensibo accepted the request and its cloud state matches. IR reception, actual HVAC power, and compressor operation are not independently verified."],
            )


def main() -> None:
    parser = argparse.ArgumentParser(description="Explicitly enroll the devices in your Sensibo account; no HVAC commands are sent.")
    parser.add_argument("--enroll-account", action="store_true", required=True)
    parser.add_argument("--expected-devices", type=int, required=True)
    arguments = parser.parse_args()
    try:
        records = SensiboClient().enroll_account(arguments.expected_devices)
    except SensiboError as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(2) from None
    print("Private Sensibo inventory contains {} verified devices. No HVAC commands were sent.".format(len(records)))


if __name__ == "__main__":
    main()
