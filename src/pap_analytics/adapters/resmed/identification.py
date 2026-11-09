"""Device identification: ``Identification.json`` (AirSense 11) and ``Identification.tgt`` (AirSense 10)."""

from __future__ import annotations

import json
import re
from pathlib import Path

from pap_analytics.adapters.resmed.detect import find
from pap_analytics.model import Device

SOURCE_KIND = "resmed-sd"


def _device(serial: str | None, model: str | None, code: str | None, firmware: str | None) -> Device:
    if not serial:
        raise ValueError("Identification has no serial number")
    return Device(
        id=f"resmed-{serial}",
        vendor="ResMed",
        model=model or "ResMed",
        model_code=code or None,
        serial=serial,
        firmware=firmware or None,
        source_kind=SOURCE_KIND,
    )


def parse_json(text: str) -> Device:
    prof = json.loads(text)["FlowGenerator"]["IdentificationProfiles"]
    product = prof.get("Product", {})
    firmware = prof.get("Software", {}).get("ApplicationIdentifier")
    return _device(
        product.get("SerialNumber"), product.get("ProductName"), product.get("ProductCode"), firmware
    )


def parse_tgt(text: str) -> Device:
    keys = dict(re.findall(r"^#(\w+)[ \t]+(.*?)\s*$", text, re.M))
    return _device(keys.get("SRN"), keys.get("PNA"), keys.get("PCD"), keys.get("PVR"))


def read_device(path: Path) -> Device:
    if (j := find(path, "Identification.json")) is not None:
        return parse_json(j.read_text(encoding="utf-8"))
    if (t := find(path, "Identification.tgt")) is not None:
        return parse_tgt(t.read_text(encoding="utf-8", errors="replace"))
    raise FileNotFoundError(f"no Identification file in {path}")
