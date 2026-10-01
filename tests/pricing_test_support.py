import json
from decimal import Decimal
from pathlib import Path

from electricity_pricing import EnergyInterval, parse_profile, period_bounds
from nordpool_ee import MARKET_INTERVAL


def example():
    path = Path(__file__).resolve().parents[1] / "examples" / "pricing-profile.example.json"
    return json.loads(path.read_text())


def rule(rate="0.01", vat="0.20", start="2026-01-01", end=None, tax="taxable"):
    return {
        "from": start, "until": end, "rate": rate, "tax_treatment": tax,
        "vat_rate": vat, "source_ids": ["example"],
    }


def component(name="energy", kind="unit", rate="0.01", section="purchase", vat="0.20"):
    result = {
        "id": name, "kind": kind, "section": section, "label": name,
        "rules": [rule(rate, vat)],
    }
    if kind != "monthly":
        result["direction"] = "import"
    return result


def profile_with(*components, rounding="ROUND_HALF_UP"):
    value = example()
    value["components"] = list(components)
    value["rounding"] = rounding
    return parse_profile(value)


def readings(first="2026-01-01", last=None, imported="1", exported="0"):
    _, _, start, end = period_bounds(first, last)
    return [
        EnergyInterval(start + index * MARKET_INTERVAL, start + (index + 1) * MARKET_INTERVAL,
                       Decimal(imported), Decimal(exported))
        for index in range(int((end - start) / MARKET_INTERVAL))
    ]


def prices_for(rows, value="100"):
    return {row.start: Decimal(value) for row in rows}
