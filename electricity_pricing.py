"""Decimal electricity charges from a private, effective-dated tariff profile."""

import hashlib
import json
import re
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN, ROUND_HALF_UP, localcontext
from pathlib import Path
from typing import Dict, List, Literal, Mapping, Optional, Sequence, Tuple, TypeVar

from nordpool_ee import MARKET_INTERVAL, TALLINN, UTC, market_bounds


QuantityBasis = Literal["raw", "billable"]
PeriodMode = Literal["report", "monthly"]
ScenarioMode = Literal["load", "comparison"]
ZERO = Decimal(0)
CENT = Decimal("0.01")
DECIMAL_DIGITS = 24
MIN_DECIMAL_EXPONENT = -18
ROUNDING = {"ROUND_HALF_UP": ROUND_HALF_UP, "ROUND_HALF_EVEN": ROUND_HALF_EVEN}
SOURCE_BASES = {"documented", "invoice_derived", "user_reported", "policy", "synthetic"}
KINDS = {"spot", "unit", "banded", "monthly"}
DAY_NIGHT = {"day", "night"}
PEAK_BANDS = {"weekday_peak", "rest_peak"}


class PricingError(Exception):
    """An expected, safe-to-display pricing or configuration failure."""


def decimal_value(value: object, label: str, nonnegative: bool = False) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise PricingError("{} must be a finite decimal number.".format(label))
    try:
        result = Decimal(str(value))
    except InvalidOperation:
        raise PricingError("{} must be a finite decimal number.".format(label)) from None
    if not result.is_finite():
        raise PricingError("{} must be finite.".format(label))
    if len(result.as_tuple().digits) > DECIMAL_DIGITS or not MIN_DECIMAL_EXPONENT <= result.as_tuple().exponent <= 18 or abs(result) > Decimal("1e15"):
        raise PricingError("{} exceeds the supported decimal precision/range.".format(label))
    if nonnegative and result < 0:
        raise PricingError("{} must not be negative.".format(label))
    return result


def parse_date(value: object) -> date:
    try:
        if not isinstance(value, str):
            raise ValueError
        result = date.fromisoformat(value)
        if result.isoformat() != value:
            raise ValueError
        return result
    except ValueError:
        raise PricingError("Dates must use YYYY-MM-DD format.") from None


def parse_instant(value: str) -> datetime:
    try:
        if any(fraction[6:].strip("0") for fraction in re.findall(r"[.,](\d+)", value)):
            raise ValueError
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.utcoffset() is None:
            raise ValueError
        return result.astimezone(UTC)
    except (AttributeError, TypeError, ValueError, OverflowError):
        raise PricingError("Delivery timestamps must include a valid UTC offset and use at most microsecond precision.") from None


def period_bounds(first: str, last: Optional[str] = None) -> Tuple[date, date, datetime, datetime]:
    start_date = parse_date(first)
    end_date = parse_date(last if last is not None else first)
    if end_date < start_date:
        raise PricingError("end_date must not precede start_date.")
    try:
        start, _ = market_bounds(start_date)
        _, end = market_bounds(end_date)
    except (ValueError, OverflowError):
        raise PricingError("Dates are outside the supported timezone bounds.") from None
    return start_date, end_date, start, end


def _object(value: object, required: set, optional: set, label: str) -> dict:
    if not isinstance(value, dict):
        raise PricingError("{} must be an object.".format(label))
    if required - value.keys() or value.keys() - required - optional:
        raise PricingError("{} has missing or unknown fields.".format(label))
    return value


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 2000:
        raise PricingError("{} must be nonempty text of at most 2000 characters.".format(label))
    return value


def _identifier(value: object) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,79}", value) is None:
        raise PricingError("Profile, component, section and source IDs must be simple identifiers.")
    return value


def _array(value: object, label: str) -> list:
    if not isinstance(value, list):
        raise PricingError("{} must be an array.".format(label))
    return value


def _rate(value: object, label: str) -> Decimal:
    if not isinstance(value, str):
        raise PricingError("Configured rates must be exact decimal strings.")
    return decimal_value(value, label)


@dataclass(frozen=True)
class Source:
    id: str
    basis: str
    description: str
    reference: str


@dataclass(frozen=True)
class TariffRule:
    start: date
    end: Optional[date]
    rate: Decimal
    band_rates: Tuple[Tuple[str, Decimal], ...]
    tax_treatment: str
    vat_rate: Decimal
    source_ids: Tuple[str, ...]


@dataclass(frozen=True)
class Component:
    id: str
    section: str
    label: str
    kind: str
    direction: Optional[str]
    rules: Tuple[TariffRule, ...]


@dataclass(frozen=True)
class NettingRule:
    start: date
    end: Optional[date]
    mode: str
    source_ids: Tuple[str, ...]


RuleType = TypeVar("RuleType", TariffRule, NettingRule)


@dataclass(frozen=True)
class PricingProfile:
    id: str
    version: str
    sha256: str
    start: date
    end: date
    rounding: str
    sources: Tuple[Source, ...]
    notes: Tuple[str, ...]
    reference_invoice_months: Tuple[str, ...]
    components: Tuple[Component, ...]
    netting: Tuple[NettingRule, ...]

    def check_coverage(self, start: datetime, end: datetime) -> None:
        first, _ = market_bounds(self.start)
        last, _ = market_bounds(self.end)
        if start < first or end > last:
            raise PricingError(
                "No configured tariff coverage for this period. Profile covers [{}, {}). "
                "Add supported effective-dated rules; rates are not extrapolated."
                .format(self.start, self.end)
            )


def _source_ids(value: object, known: set) -> Tuple[str, ...]:
    values = _array(value, "source_ids")
    if not values or any(not isinstance(item, str) or item not in known for item in values):
        raise PricingError("Every rule needs known source_ids.")
    if len(set(values)) != len(values):
        raise PricingError("Duplicate source_ids.")
    return tuple(values)


def _rule_dates(value: dict) -> Tuple[date, Optional[date]]:
    first = parse_date(value["from"])
    last = parse_date(value["until"]) if value["until"] is not None else None
    if last is not None and last <= first:
        raise PricingError("Rule 'until' must be later than 'from' and is exclusive.")
    return first, last


def _check_rule_coverage(rules: Sequence[RuleType], first: date, last: date) -> None:
    if not rules:
        raise PricingError("Rules must not be empty.")
    previous = None
    cursor = first
    for rule in rules:
        if previous is not None and (previous.end is None or rule.start < previous.end):
            raise PricingError("Rules must be ordered and must not overlap.")
        previous = rule
        if rule.end is not None and rule.end <= first:
            continue
        if rule.start >= last:
            continue
        if rule.start > cursor:
            raise PricingError("A rule gap leaves part of the profile unpriced.")
        cursor = min(rule.end or last, last)
    if cursor < last:
        raise PricingError("Rules do not cover the full profile period.")


def _unique_pairs(pairs: list) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise PricingError("Duplicate JSON field in pricing profile.")
        result[key] = value
    return result


def load_profile(path: Path) -> PricingProfile:
    try:
        with path.open("rb") as stream:
            data = stream.read(2 * 1024 * 1024 + 1)
    except OSError:
        raise PricingError("Cannot read the private pricing profile; check its path and permissions.") from None
    if len(data) > 2 * 1024 * 1024:
        raise PricingError("Pricing profile exceeds 2 MiB.")
    try:
        value = json.loads(data, object_pairs_hook=_unique_pairs)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise PricingError("Pricing profile is not valid UTF-8 JSON.") from None
    return parse_profile(value, hashlib.sha256(data).hexdigest())


def parse_profile(value: object, sha256: str = "") -> PricingProfile:
    root = _object(value, {
        "schema_version", "id", "version", "currency", "timezone", "from", "until",
        "rounding", "sources", "components", "netting",
    }, {"notes", "reference_invoice_months"}, "Profile")
    if type(root["schema_version"]) is not int or root["schema_version"] != 1:
        raise PricingError("Unsupported pricing profile schema_version.")
    if root["currency"] != "EUR" or root["timezone"] != "Europe/Tallinn":
        raise PricingError("This engine supports EUR and Europe/Tallinn only.")
    first, last = _rule_dates(root)
    if last is None or first < date(2022, 12, 30):
        raise PricingError("Profile needs a finite coverage end and the supported Estonia holiday calendar.")
    rounding = root["rounding"]
    if not isinstance(rounding, str) or rounding not in ROUNDING:
        raise PricingError("Choose an explicit ROUND_HALF_UP or ROUND_HALF_EVEN policy.")
    sources = []
    for item in _array(root["sources"], "sources"):
        source = _object(item, {"id", "basis", "description", "reference"}, set(), "Source")
        if not isinstance(source["basis"], str) or source["basis"] not in SOURCE_BASES:
            raise PricingError("Unsupported source evidence basis.")
        sources.append(Source(
            _identifier(source["id"]), source["basis"],
            _text(source["description"], "Source description"),
            _text(source["reference"], "Source reference"),
        ))
    source_ids = {source.id for source in sources}
    if len(source_ids) != len(sources):
        raise PricingError("Source IDs must be unique.")
    components = []
    for item in _array(root["components"], "components"):
        component = _object(item, {"id", "section", "label", "kind", "rules"}, {"direction"}, "Component")
        kind = component["kind"]
        if not isinstance(kind, str) or kind not in KINDS:
            raise PricingError("Unsupported component kind.")
        direction = component.get("direction")
        if kind == "monthly":
            if direction is not None:
                raise PricingError("Monthly charges do not have an energy direction.")
        elif direction not in ("import", "export"):
            raise PricingError("Variable components need import or export direction.")
        rules = []
        for rule_value in _array(component["rules"], "Component rules"):
            field_name = "band_rates" if kind == "banded" else "rate"
            rule = _object(rule_value, {
                "from", "until", field_name, "tax_treatment", "vat_rate", "source_ids",
            }, set(), "Tariff rule")
            begin, end = _rule_dates(rule)
            tax = rule["tax_treatment"]
            vat = _rate(rule["vat_rate"], "VAT rate")
            if tax not in ("taxable", "outside_vat") or not ZERO <= vat <= 1:
                raise PricingError("Invalid tax treatment or VAT fraction.")
            if tax == "outside_vat" and vat != 0:
                raise PricingError("Outside-VAT charges cannot have a VAT rate.")
            rate, bands = ZERO, ()
            if kind == "banded":
                raw_bands = rule["band_rates"]
                if not isinstance(raw_bands, dict) or set(raw_bands) not in (DAY_NIGHT, DAY_NIGHT | PEAK_BANDS):
                    raise PricingError("Band rates need day/night and optionally both winter peak bands.")
                bands = tuple((key, _rate(rate, "Band rate")) for key, rate in sorted(raw_bands.items()))
            else:
                rate = _rate(rule["rate"], "Component rate")
            rules.append(TariffRule(begin, end, rate, bands, tax, vat, _source_ids(rule["source_ids"], source_ids)))
        _check_rule_coverage(rules, first, last)
        components.append(Component(
            _identifier(component["id"]), _identifier(component["section"]),
            _text(component["label"], "Component label"), kind, direction, tuple(rules),
        ))
    if not components or len({component.id for component in components}) != len(components):
        raise PricingError("Components must be nonempty and have unique stable IDs.")
    netting = []
    for value in _array(root["netting"], "netting"):
        rule = _object(value, {"from", "until", "mode", "source_ids"}, set(), "Netting rule")
        begin, end = _rule_dates(rule)
        if rule["mode"] not in ("gross", "quarter_hour_net"):
            raise PricingError("Netting mode must be gross or quarter_hour_net.")
        netting.append(NettingRule(begin, end, rule["mode"], _source_ids(rule["source_ids"], source_ids)))
    _check_rule_coverage(netting, first, last)
    months = _array(root.get("reference_invoice_months", []), "reference_invoice_months")
    for month in months:
        if not isinstance(month, str) or len(month) != 7:
            raise PricingError("Reference invoice months must use YYYY-MM.")
        parse_date(month + "-01")
    notes = tuple(_text(note, "Profile note") for note in _array(root.get("notes", []), "notes"))
    if not sha256:
        sha256 = hashlib.sha256(json.dumps(root, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return PricingProfile(
        _identifier(root["id"]), _text(root["version"], "Profile version"), sha256,
        first, last, rounding, tuple(sources), notes, tuple(months),
        tuple(components), tuple(netting),
    )


def _select(rules: Sequence[RuleType], delivery_date: date) -> RuleType:
    for rule in rules:
        if rule.start <= delivery_date and (rule.end is None or delivery_date < rule.end):
            return rule
    raise PricingError("No applicable rule for {}.".format(delivery_date))


def estonian_holidays(year: int) -> set:
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    easter = date(year, (h + l - 7 * m + 114) // 31, (h + l - 7 * m + 114) % 31 + 1)
    return {
        date(year, month, day)
        for month, day in ((1, 1), (2, 24), (5, 1), (6, 23), (6, 24), (8, 20), (12, 24), (12, 25), (12, 26))
    } | {easter - timedelta(days=2), easter, easter + timedelta(days=49)}


def time_band(start: datetime, peaks: bool = False) -> str:
    local = start.astimezone(TALLINN)
    working = local.weekday() < 5 and local.date() not in estonian_holidays(local.year)
    if peaks and local.month in (11, 12, 1, 2, 3):
        if working and (9 <= local.hour < 12 or 16 <= local.hour < 20):
            return "weekday_peak"
        if not working and 16 <= local.hour < 20:
            return "rest_peak"
    return "day" if working and 7 <= local.hour < 22 else "night"


@dataclass(frozen=True)
class EnergyInterval:
    start: datetime
    end: datetime
    consumption_kwh: Optional[Decimal]
    export_kwh: Optional[Decimal]


def make_interval(start: str, end: str, consumption_kwh: object, export_kwh: object) -> EnergyInterval:
    return EnergyInterval(
        parse_instant(start), parse_instant(end),
        decimal_value(consumption_kwh, "consumption_kwh", True) if consumption_kwh is not None else None,
        decimal_value(export_kwh, "export_kwh", True) if export_kwh is not None else None,
    )


@dataclass(frozen=True)
class Amounts:
    excluding_vat_eur: str
    vat_eur: str
    including_vat_eur: str
    taxable_net_eur: str
    outside_vat_eur: str


@dataclass(frozen=True)
class ChargeLine:
    billing_month: str
    component_id: str
    line_id: str
    section: str
    label: str
    band: Optional[str]
    quantity_kwh: Optional[str]
    unrounded_net_eur: str
    net_eur: str
    tax_treatment: str
    vat_rate: str
    rule_ids: List[str]
    source_ids: List[str]


@dataclass(frozen=True)
class VatGroup:
    billing_month: str
    section: str
    vat_rate: str
    taxable_net_eur: str
    vat_eur: str


@dataclass(frozen=True)
class PeriodAmounts:
    start: str
    end: str
    complete_calendar_month: bool
    amounts: Amounts


@dataclass(frozen=True)
class ProfileInfo:
    id: str
    version: str
    sha256: str
    rounding: str
    invoice_derived_rules_used: bool
    reference_invoice_months: List[str]
    sources: List[Source]


@dataclass(frozen=True)
class PricingResult:
    currency: str
    timezone: str
    start: str
    end: str
    mode: str
    quantity_basis: str
    interval_count: int
    complete: bool
    raw_import_kwh: Optional[str]
    raw_export_kwh: Optional[str]
    billable_import_kwh: Optional[str]
    billable_export_kwh: Optional[str]
    fixed_fees: str
    amounts: Amounts
    lines: List[ChargeLine]
    vat_groups: List[VatGroup]
    monthly_periods: List[PeriodAmounts]
    daily_allocated_periods: List[PeriodAmounts]
    profile: ProfileInfo
    input_source: str
    price_source: str
    readings_retrieved_at: Optional[str]
    prices_retrieved_at: Optional[str]
    warnings: List[str]


@dataclass(frozen=True)
class LoadAllocation:
    start: str
    end: str
    active_start: str
    active_end: str
    energy_kwh: str


@dataclass(frozen=True)
class LoadProfile:
    start: datetime
    end: datetime
    intervals: Tuple[EnergyInterval, ...]
    allocation: Tuple[LoadAllocation, ...]


@dataclass(frozen=True)
class ScenarioResult:
    currency: str
    timezone: str
    mode: ScenarioMode
    start: str
    end: str
    accounting_start: str
    accounting_end: str
    interval_count: int
    complete: bool
    energy_kwh: Optional[str]
    energy_allocation: List[LoadAllocation]
    amounts: Amounts
    lines: List[ChargeLine]
    vat_groups: List[VatGroup]
    baseline: Optional[PricingResult]
    scenario: PricingResult
    fixed_fees: str
    profile: ProfileInfo
    input_source: str
    price_source: str
    prices_retrieved_at: Optional[str]
    warnings: List[str]


@dataclass
class _Contribution:
    day: date
    component: Component
    rule: TariffRule
    band: Optional[str]
    quantity: Optional[Decimal]
    amount: Decimal


@dataclass
class _Line:
    component: Component
    month: str
    band: Optional[str]
    tax: str
    vat: Decimal
    quantity: Optional[Decimal]
    amount: Decimal = ZERO
    rules: set = field(default_factory=set)
    sources: set = field(default_factory=set)


def _decimal_text(value: Decimal, finalized: bool = False) -> str:
    if value == 0:
        value = ZERO
    if finalized:
        return format(value, ".2f")
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _local(value: datetime) -> str:
    return value.astimezone(TALLINN).isoformat()


def _month_end(first: date) -> date:
    return (first.replace(day=28) + timedelta(days=4)).replace(day=1)


def _microseconds(value: timedelta) -> int:
    return (value.days * 86400 + value.seconds) * 1000000 + value.microseconds


def make_load(
    profile: PricingProfile, start: str, duration_minutes: object,
    power_kw: object = None, energy_kwh: object = None,
) -> LoadProfile:
    """Allocate an explicitly hypothetical constant load, never meter readings."""
    with localcontext() as context:
        context.prec = 80
        if (power_kw is None) == (energy_kwh is None):
            raise PricingError("Supply exactly one of power_kw or energy_kwh.")
        first = parse_instant(start)
        minutes = decimal_value(duration_minutes, "duration_minutes", True)
        duration = minutes * 60 * 1000000
        if duration <= 0 or duration != duration.to_integral_value():
            raise PricingError("duration_minutes must be positive and resolve to whole microseconds.")
        try:
            end = first + timedelta(microseconds=int(duration))
        except (OverflowError, ValueError):
            raise PricingError("Duration exceeds supported datetime bounds.") from None
        profile.check_coverage(first, end)
        energy = (
            decimal_value(power_kw, "power_kw", True) * minutes / 60
            if power_kw is not None else decimal_value(energy_kwh, "energy_kwh", True)
        )
        quantum = Decimal(1).scaleb(max(MIN_DECIMAL_EXPONENT, energy.adjusted() - DECIMAL_DIGITS + 1))
        rounded_energy = energy.quantize(quantum, rounding=ROUND_HALF_EVEN)
        if energy > 0 and rounded_energy == 0:
            raise PricingError("Computed load energy is below supported quantity precision.")
        energy = decimal_value(_decimal_text(rounded_energy), "Computed energy_kwh", True)
        current = first.replace(minute=first.minute - first.minute % 15, second=0, microsecond=0)
        rows, allocation = [], []
        allocated = ZERO
        while current < end:
            stop = current + MARKET_INTERVAL
            active_start, active_end = max(first, current), min(end, stop)
            # Round cumulative energy so residuals cannot drift or make the last bucket negative.
            cumulative = energy if active_end == end else (
                energy * _microseconds(active_end - first) / duration
            ).quantize(quantum, rounding=ROUND_HALF_EVEN)
            quantity = Decimal(_decimal_text(cumulative - allocated))
            rows.append(EnergyInterval(current, stop, quantity, ZERO))
            allocation.append(LoadAllocation(
                _local(current), _local(stop), _local(active_start), _local(active_end),
                _decimal_text(quantity),
            ))
            allocated = cumulative
            current = stop
        return LoadProfile(first, end, tuple(rows), tuple(allocation))


def _finalize(
    contributions: Sequence[_Contribution], profile: PricingProfile, finalized: bool,
) -> Tuple[Amounts, List[ChargeLine], List[VatGroup]]:
    grouped: Dict[Tuple[str, str, str, str, Decimal], _Line] = {}
    for item in contributions:
        month = item.day.strftime("%Y-%m")
        key = (month, item.component.id, item.band or "", item.rule.tax_treatment, item.rule.vat_rate)
        if key not in grouped:
            grouped[key] = _Line(
                item.component, month, item.band, item.rule.tax_treatment,
                item.rule.vat_rate, ZERO if item.quantity is not None else None,
            )
        line = grouped[key]
        line.amount += item.amount
        if line.quantity is not None and item.quantity is not None:
            line.quantity += item.quantity
        line.rules.add("{}@{}".format(item.component.id, item.rule.start))
        line.sources.update(item.rule.source_ids)
    lines, taxes = [], {}
    taxable, outside = ZERO, ZERO
    for key, line in sorted(grouped.items()):
        value = line.amount.quantize(CENT, rounding=ROUNDING[profile.rounding]) if finalized else line.amount
        if line.tax == "taxable":
            taxable += value
            tax_key = (line.month, line.component.section, line.vat)
            taxes[tax_key] = taxes.get(tax_key, ZERO) + value
        else:
            outside += value
        lines.append(ChargeLine(
            line.month, line.component.id,
            line.component.id + (":" + line.band if line.band else ""),
            line.component.section, line.component.label, line.band,
            _decimal_text(line.quantity) if line.quantity is not None else None,
            _decimal_text(line.amount), _decimal_text(value, finalized),
            line.tax, _decimal_text(line.vat), sorted(line.rules), sorted(line.sources),
        ))
    vat_groups, vat_total = [], ZERO
    for (month, section, rate), subtotal in sorted(taxes.items()):
        vat = subtotal * rate
        if finalized:
            vat = vat.quantize(CENT, rounding=ROUNDING[profile.rounding])
        vat_total += vat
        vat_groups.append(VatGroup(
            month, section, _decimal_text(rate),
            _decimal_text(subtotal, finalized), _decimal_text(vat, finalized),
        ))
    totals = Amounts(*(
        _decimal_text(value, finalized)
        for value in (taxable + outside, vat_total, taxable + outside + vat_total, taxable, outside)
    ))
    return totals, lines, vat_groups


def validate_intervals(
    profile: PricingProfile, intervals: Sequence[EnergyInterval], basis: QuantityBasis,
    start: datetime, end: datetime, single: bool,
) -> Tuple[List[EnergyInterval], set]:
    if basis not in ("raw", "billable"):
        raise PricingError("quantity_basis must be raw or billable.")
    profile.check_coverage(start, end)
    expected = int((end - start) / MARKET_INTERVAL)
    if len(intervals) != expected or expected <= 0:
        raise PricingError("Incomplete interval coverage: expected {}, received {}. Missing energy is not zero.".format(expected, len(intervals)))
    if any(row.start.utcoffset() is None or row.end.utcoffset() is None for row in intervals):
        raise PricingError("Delivery timestamps must include a UTC offset.")
    rows, sources = [], set()
    for index, row in enumerate(sorted(intervals, key=lambda item: item.start.astimezone(UTC))):
        if row.start.utcoffset() is None or row.end.utcoffset() is None:
            raise PricingError("Delivery timestamps must include a UTC offset.")
        first, last = row.start.astimezone(UTC), row.end.astimezone(UTC)
        if first != start + index * MARKET_INTERVAL or last - first != MARKET_INTERVAL:
            raise PricingError("Intervals must be unique, contiguous, aligned 15-minute periods; hourly energy is not distributed.")
        imported = decimal_value(row.consumption_kwh, "consumption_kwh", True) if row.consumption_kwh is not None else None
        exported = decimal_value(row.export_kwh, "export_kwh", True) if row.export_kwh is not None else None
        if (imported is None and exported is None) or (not single and (imported is None or exported is None)):
            raise PricingError("Known import and export are required for period pricing; missing/null values are not zero.")
        rule = _select(profile.netting, first.astimezone(TALLINN).date())
        sources.update(rule.source_ids)
        if rule.mode == "quarter_hour_net":
            if basis == "raw":
                if imported is None or exported is None:
                    raise PricingError("Quarter-hour netting requires both raw flows, or explicitly declared billable kWh.")
                imported, exported = max(imported - exported, ZERO), max(exported - imported, ZERO)
            elif imported is not None and exported is not None and imported > 0 and exported > 0:
                raise PricingError("Already-netted billable flows cannot both be positive in one interval.")
        rows.append(EnergyInterval(first, last, imported, exported))
    return rows, sources


def scenario_bounds(
    profile: PricingProfile, intervals: Sequence[EnergyInterval], basis: QuantityBasis,
) -> Tuple[datetime, datetime]:
    if not intervals:
        raise PricingError("Scenario profiles must contain complete quarter-hour readings, not an empty list.")
    if any(row.start.utcoffset() is None or row.end.utcoffset() is None for row in intervals):
        raise PricingError("Delivery timestamps must include a UTC offset.")
    start = min(row.start.astimezone(UTC) for row in intervals)
    end = max(row.end.astimezone(UTC) for row in intervals)
    if any(value.minute % 15 or value.second or value.microsecond for value in (start, end)):
        raise PricingError("Scenario readings must use aligned 15-minute accounting intervals.")
    validate_intervals(profile, intervals, basis, start, end, False)
    return start, end


def _variable_contributions(
    profile: PricingProfile, rows: Sequence[EnergyInterval], prices: Mapping[datetime, Decimal],
) -> List[_Contribution]:
    normalized = {}
    for timestamp, value in prices.items():
        if not isinstance(timestamp, datetime) or timestamp.utcoffset() is None or timestamp.timestamp() % 900:
            raise PricingError("Market prices need aligned, offset-aware quarter-hour timestamps.")
        timestamp = timestamp.astimezone(UTC)
        if timestamp in normalized:
            raise PricingError("Duplicate market price timestamps.")
        normalized[timestamp] = decimal_value(value, "Market EUR/MWh price") / 1000
    contributions = []
    for row in rows:
        if row.start not in normalized:
            raise PricingError("Published market price is missing for {}.".format(_local(row.start)))
        price = normalized[row.start]
        day = row.start.astimezone(TALLINN).date()
        for component in profile.components:
            if component.kind == "monthly":
                continue
            quantity = row.consumption_kwh if component.direction == "import" else row.export_kwh
            if quantity is None:
                continue
            rule = _select(component.rules, day)
            band = None
            if component.kind == "spot":
                amount = quantity * (price + rule.rate)
                if component.direction == "export":
                    amount = -amount
            elif component.kind == "banded":
                rates = dict(rule.band_rates)
                band = time_band(row.start, "weekday_peak" in rates)
                amount = quantity * rates[band]
            else:
                amount = quantity * rule.rate
            contributions.append(_Contribution(day, component, rule, band, quantity, amount))
    return contributions


def _fixed_contributions(
    profile: PricingProfile, first: date, last: date, finalized: bool,
) -> List[_Contribution]:
    contributions = []
    day = first
    while day <= last:
        month_first = day.replace(day=1)
        month_end = _month_end(day)
        stop = min(month_end, last + timedelta(days=1))
        for component in profile.components:
            if component.kind != "monthly":
                continue
            if finalized:
                active_rules = [
                    rule for rule in component.rules
                    if rule.start < stop and (rule.end is None or rule.end > day)
                ]
                applicable = {
                    (rule.rate, rule.tax_treatment, rule.vat_rate)
                    for rule in active_rules
                }
                if len(applicable) != 1:
                    raise PricingError(
                        "A monthly fixed charge changes inside this billing month. "
                        "Use an allocated report; exact partial-contract proration is not configured."
                    )
                for index, rule in enumerate(active_rules):
                    contributions.append(_Contribution(
                        day, component, rule, None, None, rule.rate if index == 0 else ZERO,
                    ))
                continue
            current = day
            days_in_month = Decimal((month_end - month_first).days)
            while current < stop:
                rule = _select(component.rules, current)
                next_change = min(stop, rule.end or stop)
                covered = Decimal((next_change - current).days)
                amount = rule.rate * covered / days_in_month
                contributions.append(_Contribution(current, component, rule, None, None, amount))
                current = next_change
        day = stop
    return contributions


def _amount_period(
    profile: PricingProfile, first: date, last: date,
    contributions: Sequence[_Contribution], finalized: bool,
) -> PeriodAmounts:
    start, _ = market_bounds(first)
    _, end = market_bounds(last)
    amounts, _, _ = _finalize(contributions, profile, finalized)
    return PeriodAmounts(
        _local(start), _local(end),
        first.day == 1 and last + timedelta(days=1) == _month_end(first),
        amounts,
    )


def _result(
    profile: PricingProfile, original: Sequence[EnergyInterval], rows: Sequence[EnergyInterval],
    basis: QuantityBasis, mode: str, contributions: Sequence[_Contribution], sources: set,
    monthly: List[PeriodAmounts], daily: List[PeriodAmounts],
) -> PricingResult:
    finalized = mode == "monthly"
    amounts, lines, vat = _finalize(contributions, profile, finalized)
    for line in lines:
        sources.update(line.source_ids)
    used_sources = [source for source in profile.sources if source.id in sources]
    inferred = any(source.basis == "invoice_derived" for source in used_sources)
    warnings = list(profile.notes)
    warnings.append("Calculated energy charges, not a supplier invoice or account amount due; payments and interest are excluded.")
    if inferred:
        warnings.append("Invoice-derived pricing is an explicitly configured working assumption, not a verified contract amendment or a guarantee for unbilled periods.")
    if mode != "monthly":
        warnings.append("Unrounded quote/report amounts are not finalized invoice lines. Do not sum rounded daily quotes to reconstruct a monthly bill.")
    if mode == "interval":
        warnings.append("Monthly fixed fees and any unspecified energy direction are excluded from this interval quote.")
    if mode == "variable":
        warnings.append("Monthly fixed fees are excluded from this variable-cost scenario.")
    if daily:
        warnings.append("Daily amounts use calendar-day fixed-fee allocation, even when the requested total uses monthly billing.")

    def total(items: Sequence[EnergyInterval], name: str) -> Optional[str]:
        values = [getattr(item, name) for item in items]
        if any(value is None for value in values):
            return None
        return _decimal_text(sum(values, ZERO))

    return PricingResult(
        "EUR", "Europe/Tallinn", _local(rows[0].start), _local(rows[-1].end), mode,
        basis, len(rows), True,
        total(original, "consumption_kwh") if basis == "raw" else None,
        total(original, "export_kwh") if basis == "raw" else None,
        total(rows, "consumption_kwh"), total(rows, "export_kwh"),
        {"interval": "excluded", "variable": "excluded", "report": "calendar_day_allocated", "monthly": "once_per_calendar_month"}[mode],
        amounts, lines, vat, monthly, daily,
        ProfileInfo(profile.id, profile.version, profile.sha256, profile.rounding, inferred, list(profile.reference_invoice_months), used_sources),
        "caller_supplied", "caller_supplied_published_prices", None, None, warnings,
    )


def quote_interval(
    profile: PricingProfile, interval: EnergyInterval, prices: Mapping[datetime, Decimal],
    quantity_basis: QuantityBasis = "raw",
) -> PricingResult:
    with localcontext() as context:
        context.prec = 80
        if interval.start.utcoffset() is None or interval.end.utcoffset() is None:
            raise PricingError("Delivery timestamps must include a UTC offset.")
        start, end = interval.start.astimezone(UTC), interval.end.astimezone(UTC)
        if start.timestamp() % 900 or end - start != MARKET_INTERVAL:
            raise PricingError("Quote exactly one aligned 15-minute delivery interval; provide interval readings for longer periods.")
        rows, sources = validate_intervals(profile, [interval], quantity_basis, start, end, True)
        contributions = _variable_contributions(profile, rows, prices)
        return _result(profile, [interval], rows, quantity_basis, "interval", contributions, sources, [], [])


def price_scenario(
    profile: PricingProfile, intervals: Sequence[EnergyInterval], prices: Mapping[datetime, Decimal],
    quantity_basis: QuantityBasis = "billable",
    baseline_intervals: Optional[Sequence[EnergyInterval]] = None,
) -> ScenarioResult:
    """Price additional billable imports, or the delta of two supplied grid profiles."""
    with localcontext() as context:
        context.prec = 80
        start, end = scenario_bounds(profile, intervals, quantity_basis)
        rows, sources = validate_intervals(profile, intervals, quantity_basis, start, end, False)
        if baseline_intervals is None:
            if quantity_basis != "billable" or any(row.export_kwh != ZERO for row in rows):
                raise PricingError("Load estimates require additional billable grid import with zero export.")
        elif scenario_bounds(profile, baseline_intervals, quantity_basis) != (start, end):
            raise PricingError("Baseline and scenario must cover exactly the same accounting window.")
        contributions = _variable_contributions(profile, rows, prices)
        scenario = _result(profile, intervals, rows, quantity_basis, "variable", contributions, sources, [], [])
        baseline = None
        delta = list(contributions)
        warnings = list(scenario.warnings)
        if baseline_intervals is not None:
            before, before_sources = validate_intervals(
                profile, baseline_intervals, quantity_basis, start, end, False,
            )
            before_contributions = _variable_contributions(profile, before, prices)
            baseline = _result(
                profile, baseline_intervals, before, quantity_basis, "variable",
                before_contributions, before_sources, [], [],
            )
            delta.extend(
                replace(item, amount=-item.amount,
                        quantity=-item.quantity if item.quantity is not None else None)
                for item in before_contributions
            )
            warnings.extend([
                "Both grid profiles are caller-supplied scenarios, not independently verified measurements or inferred appliance usage.",
                "Solar/battery operation is not modeled. Recharge costs, losses and later export changes count only when represented in the supplied window.",
            ])
        else:
            warnings.append("All supplied load energy is additional billable grid import; no solar, battery or thermostat offset is inferred.")
        warnings.append("Amounts are unrounded incremental variable costs, not the exact difference between cent-rounded monthly invoices. Unchanged monthly fees are excluded.")
        amounts, lines, vat = _finalize(delta, profile, False)
        return ScenarioResult(
            currency="EUR", timezone="Europe/Tallinn",
            mode="comparison" if baseline is not None else "load",
            start=_local(start), end=_local(end),
            accounting_start=_local(start), accounting_end=_local(end),
            interval_count=len(rows), complete=True,
            energy_kwh=None if baseline is not None else scenario.billable_import_kwh,
            energy_allocation=[], amounts=amounts, lines=lines, vat_groups=vat,
            baseline=baseline, scenario=scenario, fixed_fees="excluded", profile=scenario.profile,
            input_source="caller_supplied_grid_scenarios" if baseline is not None else "hypothetical_additional_grid_import",
            price_source="caller_supplied_published_prices", prices_retrieved_at=None,
            warnings=warnings,
        )


def price_period(
    profile: PricingProfile, intervals: Sequence[EnergyInterval], prices: Mapping[datetime, Decimal],
    start_date: str, end_date: Optional[str] = None, quantity_basis: QuantityBasis = "raw",
    mode: PeriodMode = "report", include_daily: bool = False,
) -> PricingResult:
    with localcontext() as context:
        context.prec = 80
        first, last, start, end = period_bounds(start_date, end_date)
        if mode not in ("report", "monthly"):
            raise PricingError("mode must be report or monthly.")
        if mode == "monthly" and (first.day != 1 or (last + timedelta(days=1)).day != 1):
            raise PricingError("Monthly billing requires complete calendar months. Use report for days, weeks or partial months.")
        rows, sources = validate_intervals(profile, intervals, quantity_basis, start, end, False)
        variable = _variable_contributions(profile, rows, prices)
        contributions = variable + _fixed_contributions(profile, first, last, mode == "monthly")
        monthly, daily = [], []
        current = first
        while current <= last:
            stop = min(_month_end(current) - timedelta(days=1), last)
            month_rows = [item for item in contributions if current <= item.day <= stop]
            monthly.append(_amount_period(profile, current, stop, month_rows, mode == "monthly"))
            current = stop + timedelta(days=1)
        if include_daily:
            grouped: Dict[date, List[_Contribution]] = {}
            for item in variable:
                grouped.setdefault(item.day, []).append(item)
            current = first
            while current <= last:
                day_rows = grouped.get(current, []) + _fixed_contributions(profile, current, current, False)
                daily.append(_amount_period(profile, current, current, day_rows, False))
                current += timedelta(days=1)
        return _result(profile, intervals, rows, quantity_basis, mode, contributions, sources, monthly, daily)
