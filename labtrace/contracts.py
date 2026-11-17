from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any


class LabError(ValueError):
    def __init__(self, code: str, detail: str):
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")

    def document(self) -> dict[str, str]:
        return {"code": self.code, "detail": self.detail}


IDENTIFIER = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,79}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
WELL = re.compile(r"^[A-P](?:[1-9]|1[0-9]|2[0-4])$")


def identifier(value: Any, field: str) -> str:
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise LabError("invalid_identifier", field)
    return value


def label(value: Any, field: str, limit: int = 160) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise LabError("invalid_label", field)
    return value.strip()


def positive(value: Any, field: str) -> Decimal:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise LabError("invalid_number", field) from None
    if not number.is_finite() or number <= 0:
        raise LabError("invalid_number", field)
    return number


def nonnegative(value: Any, field: str) -> Decimal:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise LabError("invalid_number", field) from None
    if not number.is_finite() or number < 0:
        raise LabError("invalid_number", field)
    return number


def timestamp(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise LabError("invalid_time", field)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise LabError("invalid_time", field) from None
    if parsed.tzinfo is None:
        raise LabError("invalid_time", field)
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def digest_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest_document(value: Any) -> str:
    return digest_bytes(canonical(value).encode("utf-8"))


def object_value(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise LabError("invalid_object", field)
    return value


@dataclass(frozen=True)
class Channel:
    key: str
    unit: str
    minimum: str
    maximum: str
    tolerance: str

    @classmethod
    def parse(cls, value: Any) -> "Channel":
        data = object_value(value, "channel")
        key = identifier(data.get("key"), "channel.key")
        unit = label(data.get("unit"), "channel.unit", 24)
        minimum = nonnegative(data.get("minimum", 0), "channel.minimum")
        maximum = positive(data.get("maximum"), "channel.maximum")
        tolerance = nonnegative(data.get("tolerance", 0), "channel.tolerance")
        if minimum >= maximum:
            raise LabError("invalid_range", key)
        return cls(key, unit, str(minimum), str(maximum), str(tolerance))


@dataclass(frozen=True)
class Protocol:
    key: str
    revision: int
    channels: tuple[Channel, ...]
    required_fields: tuple[str, ...]
    page_limit: int

    @classmethod
    def parse(cls, value: Any) -> "Protocol":
        data = object_value(value, "protocol")
        key = identifier(data.get("key"), "protocol.key")
        revision = data.get("revision")
        if type(revision) is not int or revision < 1:
            raise LabError("invalid_revision", key)
        raw_channels = data.get("channels")
        if not isinstance(raw_channels, list) or not raw_channels:
            raise LabError("missing_channels", key)
        channels = tuple(Channel.parse(item) for item in raw_channels)
        if len({item.key for item in channels}) != len(channels):
            raise LabError("duplicate_channel", key)
        raw_fields = data.get("required_fields", ["sample", "well", "channel", "value", "unit", "measured_at"])
        if not isinstance(raw_fields, list) or not raw_fields:
            raise LabError("invalid_fields", key)
        fields = tuple(identifier(item, "required_field") for item in raw_fields)
        if len(set(fields)) != len(fields):
            raise LabError("duplicate_field", key)
        limit = data.get("page_limit", 1000)
        if type(limit) is not int or not 1 <= limit <= 10000:
            raise LabError("invalid_page_limit", key)
        return cls(key, revision, channels, fields, limit)

    def document(self) -> dict[str, Any]:
        return {"key": self.key, "revision": self.revision,
                "channels": [asdict(item) for item in self.channels],
                "required_fields": list(self.required_fields), "page_limit": self.page_limit}

    def channel(self, key: str) -> Channel:
        for item in self.channels:
            if item.key == key:
                return item
        raise LabError("unknown_channel", key)


@dataclass(frozen=True)
class Instrument:
    key: str
    protocol_key: str
    protocol_revision: int
    formats: tuple[str, ...]
    field_map: dict[str, str]
    trust: int

    @classmethod
    def parse(cls, value: Any) -> "Instrument":
        data = object_value(value, "instrument")
        key = identifier(data.get("key"), "instrument.key")
        protocol = identifier(data.get("protocol_key"), "instrument.protocol_key")
        revision = data.get("protocol_revision")
        if type(revision) is not int or revision < 1:
            raise LabError("invalid_revision", key)
        formats = data.get("formats", ["json"])
        if not isinstance(formats, list) or not formats or any(item not in {"json", "csv", "html"} for item in formats):
            raise LabError("invalid_formats", key)
        field_map = data.get("field_map", {})
        if not isinstance(field_map, dict):
            raise LabError("invalid_field_map", key)
        field_map = {identifier(k, "field_map.key"): identifier(v, "field_map.value") for k, v in field_map.items()}
        if len(set(field_map.values())) != len(field_map):
            raise LabError("duplicate_field_mapping", key)
        trust = data.get("trust", 0)
        if type(trust) is not int or not 0 <= trust <= 100:
            raise LabError("invalid_trust", key)
        return cls(key, protocol, revision, tuple(formats), field_map, trust)

    def document(self) -> dict[str, Any]:
        return {"key": self.key, "protocol_key": self.protocol_key,
                "protocol_revision": self.protocol_revision, "formats": list(self.formats),
                "field_map": self.field_map, "trust": self.trust}


@dataclass(frozen=True)
class Page:
    ordinal: int
    path: str
    format: str
    sha256: str

    @classmethod
    def parse(cls, value: Any) -> "Page":
        data = object_value(value, "page")
        ordinal = data.get("ordinal")
        if type(ordinal) is not int or ordinal < 1:
            raise LabError("invalid_ordinal", str(ordinal))
        path = label(data.get("path"), "page.path", 255)
        candidate = Path(path)
        if candidate.is_absolute() or ".." in candidate.parts or path.startswith(("/", "\\")) or "\\" in path:
            raise LabError("unsafe_page_path", path)
        format_name = data.get("format")
        if format_name not in {"json", "csv", "html"}:
            raise LabError("invalid_format", str(format_name))
        digest = data.get("sha256")
        if not isinstance(digest, str) or not SHA256.fullmatch(digest):
            raise LabError("invalid_digest", path)
        return cls(ordinal, path, format_name, digest)

    def document(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Manifest:
    instrument: str
    batch: str
    protocol_key: str
    protocol_revision: int
    plate: str
    pages: tuple[Page, ...]

    @classmethod
    def parse(cls, value: Any) -> "Manifest":
        data = object_value(value, "manifest")
        instrument = identifier(data.get("instrument"), "manifest.instrument")
        batch = identifier(data.get("batch"), "manifest.batch")
        protocol_key = identifier(data.get("protocol_key"), "manifest.protocol_key")
        revision = data.get("protocol_revision")
        if type(revision) is not int or revision < 1:
            raise LabError("invalid_revision", "manifest")
        plate = identifier(data.get("plate"), "manifest.plate")
        raw_pages = data.get("pages")
        if not isinstance(raw_pages, list) or not raw_pages:
            raise LabError("missing_pages", batch)
        pages = tuple(Page.parse(item) for item in raw_pages)
        ordinals = [item.ordinal for item in pages]
        if sorted(ordinals) != list(range(1, len(pages) + 1)):
            raise LabError("noncontiguous_pages", batch)
        if len({item.path for item in pages}) != len(pages):
            raise LabError("duplicate_page_path", batch)
        return cls(instrument, batch, protocol_key, revision, plate, pages)

    def document(self) -> dict[str, Any]:
        return {"instrument": self.instrument, "batch": self.batch,
                "protocol_key": self.protocol_key, "protocol_revision": self.protocol_revision,
                "plate": self.plate, "pages": [item.document() for item in self.pages]}


@dataclass(frozen=True)
class Observation:
    instrument: str
    batch: str
    ordinal: int
    row_number: int
    plate: str
    sample: str
    well: str
    channel: str
    value: str
    unit: str
    measured_at: str
    identity: str
    status: str
    reason: str
    raw: dict[str, Any]

    def document(self) -> dict[str, Any]:
        return asdict(self)
