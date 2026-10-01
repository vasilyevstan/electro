"""Local Tapo plug discovery, MAC identity checks, readings, and relay control."""

from __future__ import annotations

import asyncio
import importlib.util
import ipaddress
import json
import logging
import os
import re
import stat
import subprocess
import sys
import tempfile
from contextlib import AsyncExitStack
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
from enum import Enum
from math import isfinite
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Dict, List, Literal, Optional, Union

from nordpool_ee import UTC

if TYPE_CHECKING:
    from kasa import Device, Feature


KEYCHAIN_SERVICE = "tp-tapo-mcp"
DISCOVERY_TIMEOUT = 5
REQUEST_TIMEOUT = 5
UPDATE_TIMEOUT = 20
FAMILY = "SMART.TAPOPLUG"
ReadingStatus = Literal["available", "unsupported", "unavailable"]
ReadingValue = Union[bool, int, float, str]
_MAC = re.compile(r"[0-9a-f]{2}([:-])(?:[0-9a-f]{2}\1){4}[0-9a-f]{2}", re.I)
_METRICS = (
    ("power", "current_consumption", "W", None),
    ("energy_today", "consumption_today", "kWh", "device-local day"),
    ("energy_this_month", "consumption_this_month", "kWh", "device-local month"),
    ("energy_total", "consumption_total", "kWh", "since device reboot"),
    ("voltage", "voltage", "V", None),
    ("current", "current", "A", None),
)
_PRIVATE_FEATURES = {"device_id", "ssid", "mac", "ip", "oem_id"}


class TapoError(Exception):
    """An expected device/configuration failure that is safe to display."""


class _AddressError(TapoError):
    """A failed connection or identity check that permits one rediscovery."""


@dataclass(frozen=True)
class Credentials:
    username: str = field(repr=False)
    password: str = field(repr=False)


@dataclass(frozen=True)
class KnownPlug:
    mac: str
    last_ip: str
    model: str
    alias: Optional[str]
    protocol: Optional[str]
    protocol_supported: bool
    last_seen: str
    capabilities: Optional[List[str]] = None
    capabilities_verified_at: Optional[str] = None


@dataclass(frozen=True)
class ListedPlug:
    mac: str
    last_ip: str
    model: str
    alias: Optional[str]
    protocol: Optional[str]
    last_seen: str
    discovery_status: Literal["seen", "not_seen", "not_checked"]
    access_status: Literal["authentication_not_checked", "unsupported_protocol"]
    capabilities: Optional[List[str]]
    capabilities_verified_at: Optional[str]


@dataclass(frozen=True)
class PlugInventory:
    source: str
    retrieved_at: str
    discovery_performed: bool
    plugs: List[ListedPlug]
    warnings: List[str]


@dataclass(frozen=True)
class Measurement:
    value: Optional[float]
    unit: str
    status: ReadingStatus
    reason: Optional[str]
    period: Optional[str]


@dataclass(frozen=True)
class PlugMeasurements:
    power: Measurement
    energy_today: Measurement
    energy_this_month: Measurement
    energy_total: Measurement
    voltage: Measurement
    current: Measurement


@dataclass(frozen=True)
class OperatingReading:
    id: str
    value: Optional[ReadingValue]
    unit: Optional[str]
    status: ReadingStatus
    reason: Optional[str]


@dataclass(frozen=True)
class PlugState:
    mac: str
    ip: str
    model: str
    alias: Optional[str]
    firmware: Optional[str]
    hardware: Optional[str]
    is_on: bool
    retrieved_at: str
    device_time: Optional[str]
    measurements: PlugMeasurements
    operating_readings: List[OperatingReading]
    capabilities: List[str]
    warnings: List[str]


@dataclass(frozen=True)
class PowerResult:
    mac: str
    requested_on: bool
    before_on: bool
    command_sent: bool
    acknowledged: Optional[bool]
    verified: bool
    state: PlugState
    warnings: List[str]


def ensure_runtime() -> None:
    if sys.version_info < (3, 11):
        raise TapoError("tp-tapo-mcp requires Python 3.11 or newer; other MCPs still support Python 3.10.")
    if importlib.util.find_spec("kasa") is None:
        raise TapoError("Install the optional Tapo client with 'uv sync --locked --extra tapo'.")
    logger = logging.getLogger("kasa")
    if not any(isinstance(handler, logging.NullHandler) for handler in logger.handlers):
        logger.addHandler(logging.NullHandler())
    # Transport diagnostics can contain private device/session data.
    logger.propagate = False


def load_credentials() -> Credentials:
    names = ("TAPO_USERNAME", "TAPO_PASSWORD")
    if any(name in os.environ for name in names):
        if not all(os.environ.get(name, "").strip() for name in names):
            raise TapoError("Set both TAPO_USERNAME and TAPO_PASSWORD, or neither to use macOS Keychain.")
        return Credentials(os.environ[names[0]], os.environ[names[1]])
    if sys.platform != "darwin":
        raise TapoError("Set TAPO_USERNAME and TAPO_PASSWORD in the MCP process environment.")
    values = []
    for account in ("username", "password"):
        try:
            result = subprocess.run(
                ["/usr/bin/security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-a", account, "-w"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=10, check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            raise TapoError("macOS Keychain could not be read or is locked.") from None
        if result.returncode != 0:
            raise TapoError(
                "Tapo credentials are unavailable in macOS Keychain service 'tp-tapo-mcp' "
                "(accounts 'username' and 'password'). GitHub secret values cannot be fetched by a local MCP."
            )
        try:
            value = result.stdout.decode("utf-8").rstrip("\n")
        except UnicodeDecodeError:
            raise TapoError("Tapo Keychain credential has invalid text encoding.") from None
        if not value.strip():
            raise TapoError("Tapo Keychain credential is empty.")
        values.append(value)
    return Credentials(values[0], values[1])


def normalize_mac(value: object) -> str:
    if not isinstance(value, str) or _MAC.fullmatch(value.strip()) is None:
        raise TapoError("MAC must contain six hexadecimal octets separated by colons or hyphens.")
    normalized = value.strip().replace("-", ":").upper()
    if normalized == "00:00:00:00:00:00" or int(normalized[:2], 16) & 1:
        raise TapoError("MAC must identify a single device, not a zero or multicast address.")
    return normalized


def _ip(value: object) -> str:
    if not isinstance(value, str):
        raise TapoError("A plug address must be a local IPv4 address.")
    try:
        address = ipaddress.IPv4Address(value)
    except ipaddress.AddressValueError:
        raise TapoError("A plug address must be a local IPv4 address.") from None
    if (
        address.is_global or address.is_loopback or address.is_multicast
        or address.is_unspecified or address.is_reserved
    ):
        raise TapoError("Refusing a non-local or unusable plug address.")
    return str(address)


def _text(value: object, *, nullable: bool = False) -> Optional[str]:
    if nullable and (value is None or isinstance(value, str) and not value.strip()):
        return None
    if not isinstance(value, str) or not value.strip():
        raise TapoError("Plug metadata contains an invalid text field.")
    return value


def _timestamp(value: object) -> str:
    if not isinstance(value, str):
        raise TapoError("Tapo registry contains an invalid timestamp.")
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.utcoffset() is None:
            raise ValueError
    except ValueError:
        raise TapoError("Tapo registry timestamps must include a UTC offset.") from None
    return parsed.astimezone(UTC).isoformat()


class Registry:
    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = path if path is not None else Path.home() / ".config/electro/tapo/devices.json"

    def load(self) -> Dict[str, KnownPlug]:
        try:
            if self.path.is_symlink():
                raise TapoError("The Tapo registry must be a regular private file, not a symlink.")
            if stat.S_IMODE(self.path.stat().st_mode) & 0o077:
                raise TapoError("The Tapo registry must be private; set its permissions to 600.")
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, UnicodeError, json.JSONDecodeError):
            raise TapoError("The Tapo registry could not be read or contains invalid JSON.") from None
        if not isinstance(value, dict) or type(value.get("version")) is not int or value["version"] != 1:
            raise TapoError("The Tapo registry has an unsupported schema version.")
        if not isinstance(value.get("devices"), list):
            raise TapoError("The Tapo registry must contain a devices array.")
        records = {}
        for row in value["devices"]:
            if not isinstance(row, dict):
                raise TapoError("The Tapo registry contains an invalid device entry.")
            mac = normalize_mac(row.get("mac"))
            if mac in records:
                raise TapoError("The Tapo registry contains duplicate MAC identities.")
            model = _text(row.get("model"))
            supported = row.get("protocol_supported")
            capabilities = row.get("capabilities")
            verified_at = row.get("capabilities_verified_at")
            if type(supported) is not bool:
                raise TapoError("The Tapo registry contains an invalid protocol capability.")
            if capabilities is not None and (
                not isinstance(capabilities, list)
                or any(not isinstance(item, str) or not item for item in capabilities)
            ):
                raise TapoError("The Tapo registry contains invalid capabilities.")
            if (capabilities is None) != (verified_at is None):
                raise TapoError("Tapo registry capabilities must have a verification timestamp.")
            if model is None:
                raise TapoError("The Tapo registry must identify each device model.")
            records[mac] = KnownPlug(
                mac, _ip(row.get("last_ip")), model, _text(row.get("alias"), nullable=True),
                _text(row.get("protocol"), nullable=True), supported,
                _timestamp(row.get("last_seen")), capabilities,
                _timestamp(verified_at) if verified_at is not None else None,
            )
        return records

    def save(self, records: Dict[str, KnownPlug]) -> None:
        temporary = None
        try:
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            if self.path.parent.is_symlink() or stat.S_IMODE(self.path.parent.stat().st_mode) & 0o077:
                raise TapoError("The Tapo registry directory must be private; set its permissions to 700.")
            descriptor, temporary = tempfile.mkstemp(prefix=".devices-", suffix=".json", dir=self.path.parent)
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                json.dump(
                    {"version": 1, "devices": [asdict(records[mac]) for mac in sorted(records)]},
                    output, indent=2, allow_nan=False,
                )
                output.write("\n")
            os.replace(temporary, self.path)
        except OSError:
            raise TapoError("The private Tapo registry could not be saved.") from None
        finally:
            if temporary is not None and os.path.exists(temporary):
                try:
                    os.unlink(temporary)
                except OSError:
                    raise TapoError("A private temporary Tapo registry file could not be removed.") from None


def _measurement(
    device: Device, feature_id: str, unit: str, period: Optional[str], energy_failed: bool
) -> Measurement:
    from kasa import KasaException

    feature = device.features.get(feature_id)
    if energy_failed:
        return Measurement(None, unit, "unavailable", "The energy module did not update successfully.", period)
    if feature is None:
        return Measurement(None, unit, "unsupported", "Not exposed by this device/firmware.", period)
    try:
        value = feature.value
        actual_unit = feature.unit
    except (KasaException, AttributeError, ValueError):
        return Measurement(None, unit, "unavailable", "The supported reading could not be obtained.", period)
    if value is None:
        return Measurement(None, unit, "unavailable", "The device returned no measurement.", period)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or actual_unit != unit:
        return Measurement(None, unit, "unavailable", "The device returned an invalid value or unexpected unit.", period)
    try:
        number = float(value)
    except (OverflowError, ValueError):
        number = float("inf")
    if not isfinite(number):
        return Measurement(None, unit, "unavailable", "The device returned a non-finite measurement.", period)
    return Measurement(number, unit, "available", None, period)


def _operating_reading(feature_id: str, feature: Feature) -> OperatingReading:
    from kasa import KasaException

    try:
        value = feature.value
        unit = feature.unit
    except (KasaException, AttributeError, ValueError):
        return OperatingReading(feature_id, None, None, "unavailable", "The supported reading could not be obtained.")
    if isinstance(value, datetime):
        value = value.isoformat()
    elif isinstance(value, Enum):
        value = value.value
    if (
        value is None or not isinstance(value, (bool, int, float, str))
        or (isinstance(value, float) and not isfinite(value))
        or (unit is not None and not isinstance(unit, str))
    ):
        return OperatingReading(feature_id, None, None, "unavailable", "The device returned no usable scalar reading.")
    return OperatingReading(feature_id, value, unit, "available", None)


def snapshot(device: Device, retrieved_at: datetime) -> PlugState:
    from kasa import Feature, KasaException, Module

    info = device.sys_info
    mac = normalize_mac(info.get("mac"))
    is_on = info.get("device_on")
    if type(is_on) is not bool:
        raise TapoError("The plug did not report a valid relay state.")
    energy_failed = False
    energy = device.modules.get(Module.Energy)
    if energy is not None:
        try:
            energy_failed = not bool(energy.data)
        except KasaException:
            energy_failed = True
    measurements = {
        name: _measurement(device, feature_id, unit, period, energy_failed)
        for name, feature_id, unit, period in _METRICS
    }
    metric_ids = {item[1] for item in _METRICS}
    readings = []
    for feature_id, feature in sorted(device.features.items()):
        if (
            feature_id not in metric_ids | _PRIVATE_FEATURES
            and feature.type in (Feature.Type.Sensor, Feature.Type.BinarySensor)
        ):
            readings.append(_operating_reading(feature_id, feature))
    device_time = next(
        (row.value for row in readings if row.id == "device_time" and isinstance(row.value, str)), None
    )
    warnings = []
    if energy_failed:
        warnings.append("The energy module did not update successfully; some capabilities/readings are unverified.")
    if any(row.status == "unavailable" for row in measurements.values()) or any(
        row.status == "unavailable" for row in readings
    ):
        warnings.append("Some supported or unverified readings are unavailable, not zero.")
    if device_time is None:
        warnings.append("The device clock is unavailable; daily/monthly counters retain unspecified device-local boundaries.")
    model = _text(device.model)
    if model is None:
        raise TapoError("The authenticated plug did not report a model.")
    return PlugState(
        mac=mac, ip=_ip(device.host), model=model,
        alias=_text(device.alias, nullable=True),
        firmware=_text(info.get("fw_ver"), nullable=True),
        hardware=_text(info.get("hw_ver"), nullable=True),
        is_on=is_on, retrieved_at=retrieved_at.astimezone(UTC).isoformat(),
        device_time=device_time, measurements=PlugMeasurements(**measurements),
        operating_readings=readings,
        capabilities=sorted({"relay"} | (metric_ids & device.features.keys()) | {row.id for row in readings}),
        warnings=warnings,
    )


class TapoClient:
    def __init__(
        self, registry: Optional[Registry] = None,
        credential_loader: Callable[[], Credentials] = load_credentials,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.registry = registry if registry is not None else Registry()
        self.credential_loader = credential_loader
        self.clock = clock
        self._lock = asyncio.Lock()

    async def _disconnect(self, device: Device) -> None:
        from kasa import KasaException

        try:
            await asyncio.wait_for(device.disconnect(), timeout=REQUEST_TIMEOUT)
        except (KasaException, OSError, asyncio.TimeoutError):
            raise TapoError("The local Tapo session could not be closed cleanly.") from None

    async def _discover(
        self, records: Dict[str, KnownPlug]
    ) -> tuple[Dict[str, KnownPlug], set[str], List[str]]:
        from kasa import Discover, KasaException

        found: Dict[str, KnownPlug] = {}
        addresses: Dict[str, str] = {}
        warnings = []
        conflicts = []
        observed_at = self.clock().astimezone(UTC).isoformat()

        def received(raw: dict) -> None:
            response = raw.get("discovery_response")
            if not isinstance(response, dict):
                warnings.append("A discovery response could not be interpreted.")
                return
            result = response.get("result")
            if not isinstance(result, dict) or result.get("device_type") != FAMILY:
                return
            try:
                meta = raw.get("meta")
                if not isinstance(meta, dict):
                    raise TapoError("Missing discovery source.")
                mac, host = normalize_mac(result.get("mac")), _ip(meta.get("ip"))
                model = _text(result.get("device_model"))
                if model is None:
                    raise TapoError("Missing device model.")
                encryption = result.get("mgt_encrypt_schm", {})
                if not isinstance(encryption, dict):
                    raise TapoError("Invalid discovery protocol metadata.")
                protocol = _text(encryption.get("encrypt_type"), nullable=True)
            except TapoError:
                warnings.append("A Tapo discovery response contained invalid identity or protocol metadata.")
                return
            if (mac in found and found[mac].last_ip != host) or (
                host in addresses and addresses[host] != mac
            ):
                conflicts.append(mac)
                return
            previous = records.get(mac)
            found[mac] = KnownPlug(
                mac, host, model, previous.alias if previous else None,
                protocol, False, observed_at,
                previous.capabilities if previous else None,
                previous.capabilities_verified_at if previous else None,
            )
            addresses[host] = mac

        try:
            devices = await asyncio.wait_for(
                Discover.discover(
                    target="255.255.255.255", discovery_timeout=DISCOVERY_TIMEOUT,
                    timeout=REQUEST_TIMEOUT, on_discovered_raw=received,
                ),
                timeout=DISCOVERY_TIMEOUT + REQUEST_TIMEOUT,
            )
        except (KasaException, OSError, asyncio.TimeoutError):
            raise TapoError("Local Tapo discovery failed or timed out.") from None
        async with AsyncExitStack() as cleanup:
            for device in devices.values():
                cleanup.push_async_callback(self._disconnect, device)
            if conflicts:
                raise TapoError("Discovery reported conflicting MAC/IP identities; the registry was not changed.")
            for mac, record in found.items():
                found[mac] = replace(record, protocol_supported=record.last_ip in devices)
        if not found:
            warnings.append("No Tapo plugs responded to this discovery; that does not establish relay state.")
        merged = {**records, **found}
        if found:
            self.registry.save(merged)
        return merged, set(found), sorted(set(warnings))

    async def list_plugs(self, refresh: bool = True) -> PlugInventory:
        if type(refresh) is not bool:
            raise TapoError("refresh must be a boolean.")
        ensure_runtime()
        async with self._lock:
            records = self.registry.load()
            seen: set[str] = set()
            warnings = []
            if refresh:
                records, seen, warnings = await self._discover(records)
            plugs = []
            for mac, record in sorted(records.items()):
                status: Literal["seen", "not_seen", "not_checked"] = (
                    "seen" if mac in seen else "not_seen" if refresh else "not_checked"
                )
                plugs.append(ListedPlug(
                    mac, record.last_ip, record.model, record.alias, record.protocol,
                    record.last_seen, status,
                    "authentication_not_checked" if record.protocol_supported else "unsupported_protocol",
                    record.capabilities, record.capabilities_verified_at,
                ))
            if any(item.discovery_status == "not_seen" for item in plugs):
                warnings.append("Unseen plugs retain last-known metadata; they are not assumed off or disconnected.")
            if any(item.access_status == "unsupported_protocol" for item in plugs):
                warnings.append("Some last-observed protocols are unsupported; check firmware and the optional Tapo Third-Party Compatibility setting.")
            if not refresh:
                warnings.append("Cached inventory only: reachability, authentication, and capabilities were not refreshed.")
            return PlugInventory("local_tapo", self.clock().astimezone(UTC).isoformat(), refresh, plugs, warnings)

    async def _connect(self, record: KnownPlug, credentials: Credentials) -> Device:
        from kasa import Credentials as KasaCredentials, DeviceType, Discover, KasaException
        from kasa.exceptions import AuthenticationError, UnsupportedDeviceError
        from kasa.smart import SmartDevice

        device = None
        accepted = False
        try:
            device = await asyncio.wait_for(
                Discover.discover_single(
                    record.last_ip, credentials=KasaCredentials(credentials.username, credentials.password),
                    discovery_timeout=DISCOVERY_TIMEOUT, timeout=REQUEST_TIMEOUT,
                ),
                timeout=DISCOVERY_TIMEOUT + REQUEST_TIMEOUT,
            )
            if device is None:
                raise _AddressError("No supported plug responded at the last-known address.")
            try:
                discovered_mac = normalize_mac(device.mac)
            except TapoError:
                raise _AddressError("Discovery did not report a valid MAC for this address.") from None
            if discovered_mac != record.mac:
                raise _AddressError("The old address now belongs to a different device.")
            await asyncio.wait_for(device.update(), timeout=UPDATE_TIMEOUT)
            try:
                authenticated_mac = normalize_mac(device.sys_info.get("mac"))
            except TapoError:
                raise _AddressError("The authenticated device did not report a valid MAC.") from None
            if authenticated_mac != record.mac:
                raise _AddressError("The authenticated MAC differs from the requested identity.")
            if not isinstance(device, SmartDevice) or device.device_type != DeviceType.Plug:
                raise TapoError("The authenticated device is not a supported Tapo socket.")
            accepted = True
            return device
        except AuthenticationError:
            raise TapoError("Tapo authentication failed; check the local credentials and device account.") from None
        except UnsupportedDeviceError:
            raise _AddressError("The last-known address advertised an unsupported device protocol.") from None
        except (OSError, asyncio.TimeoutError):
            raise _AddressError("The plug could not be reached at its last-known address.") from None
        except KasaException:
            raise _AddressError("The plug could not be queried safely at its last-known address.") from None
        finally:
            if device is not None and not accepted:
                await self._disconnect(device)

    async def _open(self, mac: str, credentials: Credentials) -> Device:
        records = self.registry.load()
        record = records.get(mac)
        if record is not None:
            try:
                return await self._connect(record, credentials)
            except _AddressError:
                pass
        records, seen, _ = await self._discover(records)
        if mac not in seen:
            raise TapoError("The requested MAC was not found in bounded local discovery; it may be unreachable.")
        if not records[mac].protocol_supported:
            raise TapoError(
                "The requested plug advertises an unsupported protocol; check Tapo "
                "Third-Party Compatibility and client/firmware support."
            )
        try:
            return await self._connect(records[mac], credentials)
        except _AddressError:
            raise TapoError("The requested plug could not be reached with a verified MAC after rediscovery.") from None

    def _remember(self, state: PlugState) -> None:
        records = self.registry.load()
        previous = records.get(state.mac)
        records[state.mac] = KnownPlug(
            state.mac, state.ip, state.model, state.alias,
            previous.protocol if previous else None, True, state.retrieved_at,
            state.capabilities, state.retrieved_at,
        )
        self.registry.save(records)

    async def _read(self, mac: str, credentials: Credentials) -> PlugState:
        device = await self._open(mac, credentials)
        try:
            return snapshot(device, self.clock())
        finally:
            await self._disconnect(device)

    async def get_plug(self, mac: str) -> PlugState:
        mac = normalize_mac(mac)
        ensure_runtime()
        async with self._lock:
            state = await self._read(mac, self.credential_loader())
            self._remember(state)
            return state

    async def set_power(self, mac: str, on: bool) -> PowerResult:
        mac = normalize_mac(mac)
        if type(on) is not bool:
            raise TapoError("on must be an explicit boolean desired state, not a toggle.")
        ensure_runtime()
        from kasa import KasaException

        async with self._lock:
            credentials = self.credential_loader()
            device = await self._open(mac, credentials)
            acknowledged = False
            command_sent = False
            try:
                before = snapshot(device, self.clock())
                if before.is_on == on:
                    self._remember(before)
                    return PowerResult(mac, on, before.is_on, False, None, True, before, [])
                try:
                    # SmartDevice.set_state retries writes; keep a mutation to one attempt.
                    command_sent = True
                    await asyncio.wait_for(
                        device.protocol.query({"set_device_info": {"device_on": on}}, retry_count=0),
                        timeout=REQUEST_TIMEOUT,
                    )
                    acknowledged = True
                except (KasaException, OSError, asyncio.TimeoutError):
                    acknowledged = False
            finally:
                try:
                    await self._disconnect(device)
                except TapoError:
                    if command_sent:
                        raise TapoError(
                            "The power command may have taken effect, but session cleanup failed "
                            "before verification. No automatic command retry was made; check the plug state."
                        ) from None
                    raise
            try:
                # A fresh connection avoids the client's cached energy update intervals.
                after = await self._read(mac, credentials)
            except TapoError:
                raise TapoError(
                    "The power command may have taken effect, but its outcome could not be verified. "
                    "No automatic command retry was made; check the plug state."
                ) from None
            if after.is_on != on:
                raise TapoError(
                    "The requested relay state was not confirmed by read-back. "
                    "No automatic command retry was made."
                )
            try:
                self._remember(after)
            except TapoError:
                raise TapoError(
                    "The requested relay state was confirmed, but the private inventory "
                    "could not be updated. Do not repeat the power command to repair the registry."
                ) from None
            warnings = [] if acknowledged else [
                "The command acknowledgement was unavailable, but a fresh authenticated read confirmed the requested state."
            ]
            return PowerResult(mac, on, before.is_on, True, acknowledged, True, after, warnings)
