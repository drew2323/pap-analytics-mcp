"""Single registry of the supported ``[thresholds]`` config keys: defaults, bounds and validation."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pap_analytics.analytics.anomalies import DEFAULTS as _ANOMALY
from pap_analytics.analytics.disturbances import DEFAULTS as _DIST
from pap_analytics.analytics.oximetry import DEFAULTS as _OXIMETRY

LARGE_LEAK_LPS = 0.4

# key -> (default, minimum, maximum, integer-only); bounds are inclusive, None = unbounded
SPECS: dict[str, tuple[float, float | None, float | None, bool]] = {
    "large_leak_lps": (LARGE_LEAK_LPS, 0.0, None, False),
    "leak_min_s": (_ANOMALY["leak_min_s"], 0.0, None, False),
    "cluster_min_events": (_ANOMALY["cluster_min_events"], 1, None, True),
    "cluster_window_min": (_ANOMALY["cluster_window_min"], 0.0, None, False),
    "spo2_low_pct": (_ANOMALY["spo2_low_pct"], 0.0, 100.0, False),
    "pressure_max_min": (_ANOMALY["pressure_max_min"], 0.0, None, False),
    "vent_low_ratio": (_ANOMALY["vent_low_ratio"], 0.0, 1.0, False),
    "vent_low_min": (_ANOMALY["vent_low_min"], 0.0, None, False),
    "gap_min": (_ANOMALY["gap_min"], 0.0, None, False),
    "outlier_z": (_ANOMALY["outlier_z"], 0.0, None, False),
    "baseline_nights": (_ANOMALY["baseline_nights"], 1, None, True),
    "baseline_min_nights": (_ANOMALY["baseline_min_nights"], 1, None, True),
    "spo2_max_slope": (_OXIMETRY["spo2_max_slope"], 0.0, None, False),
    "min_valid_run_s": (_OXIMETRY["min_valid_run_s"], 0.0, None, False),
    "baseline_window_s": (_OXIMETRY["baseline_window_s"], 1.0, None, False),
    "desat_min_duration_s": (_OXIMETRY["desat_min_duration_s"], 0.0, None, False),
    "association_window_s": (_OXIMETRY["association_window_s"], 0.0, None, False),
    "pulse_rise_bpm": (_DIST["pulse_rise_bpm"], 1.0, None, False),
    "pulse_rise_min_s": (_DIST["pulse_rise_min_s"], 0.0, None, False),
    "pulse_baseline_s": (_DIST["pulse_baseline_s"], 10.0, None, False),
    "link_window_s": (_DIST["link_window_s"], 0.0, None, False),
    "fl_threshold": (_DIST["fl_threshold"], 0.01, 1.0, False),
    "fl_min_s": (_DIST["fl_min_s"], 0.0, None, False),
    "rera_recovery_ratio": (_DIST["rera_recovery_ratio"], 1.0, None, False),
    "recovery_breath_ratio": (_DIST["recovery_breath_ratio"], 1.0, None, False),
    "reduced_breath_ratio": (_DIST["reduced_breath_ratio"], 0.0, 1.0, False),
    "restless_irregularity": (_DIST["restless_irregularity"], 0.01, None, False),
    "restless_min_s": (_DIST["restless_min_s"], 0.0, None, False),
    "ned_fl_pct": (_DIST["ned_fl_pct"], 1.0, 100.0, False),
    "ned_rera_sigh_ratio": (_DIST["ned_rera_sigh_ratio"], 1.0, None, False),
}


def validate(raw: Mapping[str, Any]) -> dict[str, float]:
    """Check user overrides; raises ``ValueError`` on unknown keys, wrong types or out-of-range values."""
    out: dict[str, float] = {}
    for key, value in raw.items():
        if key not in SPECS:
            raise ValueError(f"thresholds.{key}: unknown key (supported: {', '.join(SPECS)})")
        _, lo, hi, integer = SPECS[key]
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise ValueError(f"thresholds.{key}: number required, got {value!r}")
        if integer and float(value) != int(value):
            raise ValueError(f"thresholds.{key}: integer required, got {value!r}")
        if (lo is not None and value < lo) or (hi is not None and value > hi):
            raise ValueError(f"thresholds.{key}: must be in [{lo}, {hi}], got {value!r}")
        out[key] = float(value)
    return out


def effective(raw: Mapping[str, Any]) -> dict[str, float]:
    """All supported thresholds: defaults overlaid with the overrides."""
    return {**{k: float(v[0]) for k, v in SPECS.items()}, **validate(raw)}
