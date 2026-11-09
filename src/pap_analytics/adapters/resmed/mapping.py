"""ResMed → canonical mapping tables."""

from __future__ import annotations

from collections.abc import Callable

import numpy as np

from pap_analytics.model import EventType

# raw label -> (canonical channel, scale factor applied to physical values)
CHANNEL_MAP: dict[str, tuple[str, float]] = {
    "Flow.40ms": ("flow", 1.0),
    "Press.40ms": ("pressure_hr", 1.0),
    "Press.2s": ("pressure", 1.0),
    "MaskPress.2s": ("mask_pressure", 1.0),
    "EprPress.2s": ("epap", 1.0),
    "Leak.2s": ("leak", 1.0),
    "RespRate.2s": ("resp_rate", 1.0),
    "TidVol.2s": ("tidal_volume", 1000.0),  # L -> mL
    "MinVent.2s": ("minute_vent", 1.0),
    "TgtVent.2s": ("target_vent", 1.0),
    "IERatio.2s": ("ie_ratio", 1.0),
    "Snore.2s": ("snore", 1.0),
    "FlowLim.2s": ("flow_limitation", 1.0),
    "Ti.2s": ("ti", 1.0),
    "SpO2.1s": ("spo2", 1.0),
    "Pulse.1s": ("pulse", 1.0),
}

# Per-channel sanitising: invalid samples become NaN.
SANITIZERS: dict[str, Callable[[np.ndarray], np.ndarray]] = {
    "spo2": lambda v: np.where((v >= 50) & (v <= 100), v, np.nan),
    "pulse": lambda v: np.where(v > 0, v, np.nan),
}

EVENT_MAP: dict[str, EventType] = {
    "hypopnea": EventType.HYPOPNEA,
    "obstructive apnea": EventType.OBSTRUCTIVE_APNEA,
    "central apnea": EventType.CENTRAL_APNEA,
    "apnea": EventType.APNEA,
    "arousal": EventType.AROUSAL,
    "recording starts": EventType.RECORDING_START,
}


def map_event(text: str) -> EventType | None:
    """Canonical type for an annotation text, or None if unknown."""
    key = text.strip().lower()
    if key in EVENT_MAP:
        return EVENT_MAP[key]
    if "cheyne" in key or key.startswith("csr"):
        return EventType.CSR
    return None


# STR `Mode` code -> canonical mode name (ResMed numbering as used by OSCAR).
# Code 6 = ASV: confirmed on the author's AirSense 11 (firmware with ASV enabled) - the STR record
# has S.AV.* populated and TgtIPAP channels, while S.A.* (AutoSet) fields are only leftovers.
MODE_MAP: dict[int, str] = {
    0: "cpap",
    1: "apap",
    2: "bilevel_t",
    3: "bilevel_s",
    4: "bilevel_st",
    5: "vauto",
    6: "asv",
    7: "asv_auto",
}


def mode_name(code: int | None) -> str | None:
    if code is None:
        return None
    return MODE_MAP.get(code, f"unknown_{code}")
