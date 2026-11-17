from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any

from .contracts import Channel, LabError, identifier, timestamp, WELL


_UNIT_ALIASES = {
    "uM": ("µM", Decimal("1")),
    "µM": ("µM", Decimal("1")),
    "nM": ("µM", Decimal("0.001")),
    "mM": ("µM", Decimal("1000")),
    "ng/mL": ("ng/mL", Decimal("1")),
    "ug/mL": ("ng/mL", Decimal("1000")),
    "µg/mL": ("ng/mL", Decimal("1000")),
    "AU": ("AU", Decimal("1")),
    "%": ("%", Decimal("1")),
}


def decimal_value(value: Any, field: str) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise LabError("invalid_number", field)
    try:
        result = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        raise LabError("invalid_number", field) from None
    if not result.is_finite():
        raise LabError("invalid_number", field)
    return result


def normalized_value(value: Any, unit: Any, channel: Channel) -> Decimal:
    number = decimal_value(value, "value")
    if not isinstance(unit, str) or unit not in _UNIT_ALIASES:
        raise LabError("unknown_unit", str(unit))
    canonical, multiplier = _UNIT_ALIASES[unit]
    if canonical != channel.unit:
        raise LabError("incompatible_unit", f"{unit} -> {channel.unit}")
    return number * multiplier


def measurement(mapped: dict[str, Any], channel: Channel, plate: str) -> dict[str, Any]:
    sample = identifier(mapped.get("sample"), "sample")
    well = mapped.get("well")
    if not isinstance(well, str) or not WELL.fullmatch(well):
        raise LabError("invalid_well", str(well))
    channel_key = identifier(mapped.get("channel"), "channel")
    if channel_key != channel.key:
        raise LabError("channel_mismatch", channel_key)
    value = normalized_value(mapped.get("value"), mapped.get("unit"), channel)
    measured_at = timestamp(mapped.get("measured_at"), "measured_at")
    replicate = mapped.get("replicate", "primary")
    replicate = identifier(str(replicate), "replicate")
    control = mapped.get("control", "sample")
    if control not in {"sample", "positive", "negative", "blank"}:
        raise LabError("invalid_control", str(control))
    identity = "|".join((plate, sample, well, channel_key, replicate))
    return {"sample": sample, "well": well, "channel": channel_key,
            "value": str(value), "unit": channel.unit, "measured_at": measured_at,
            "identity": identity, "replicate": replicate, "control": control}


def within_range(value: str, channel: Channel) -> bool:
    number = Decimal(value)
    return Decimal(channel.minimum) <= number <= Decimal(channel.maximum)


def comparable(left: dict[str, Any], right: dict[str, Any], channel: Channel) -> bool:
    if left["unit"] != right["unit"] or left["identity"] != right["identity"]:
        return False
    return abs(Decimal(left["value"]) - Decimal(right["value"])) <= Decimal(channel.tolerance)


def value_bucket(value: str, precision: str = "0.001") -> str:
    number = Decimal(value)
    step = Decimal(precision)
    if step <= 0:
        raise LabError("invalid_precision", precision)
    return str(number.quantize(step))


def parse_identity(value: str) -> tuple[str, str, str, str, str]:
    parts = value.split("|")
    if len(parts) != 5 or any(not part for part in parts):
        raise LabError("invalid_identity", value)
    return tuple(parts)


def channel_for(channels: tuple[Channel, ...], key: str) -> Channel:
    for channel in channels:
        if channel.key == key:
            return channel
    raise LabError("unknown_channel", key)


def stable_numeric_order(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(rows, key=lambda item: (item["identity"], item["measured_at"],
                                           Decimal(item["value"]), item.get("instrument", "")))
