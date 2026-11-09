"""``STR.edf`` daily summary → device-reported ``TherapyDay`` metrics and ``Settings``.

STR has one record per therapy day (24 h, noon to noon). Values < 0 (and OAI/CAI-style
-0.1 sentinels) mean "not available" and become ``None``.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from pap_analytics.adapters.resmed.mapping import mode_name
from pap_analytics.edf import EdfFile
from pap_analytics.model import Metric, Provenance, Settings, TherapyDay

EPOCH = date(1970, 1, 1)

# metric name -> (STR label, unit, scale)
METRIC_LABELS: dict[str, tuple[str, str, float]] = {
    "ahi": ("AHI", "1/h", 1.0),
    "hi": ("HI", "1/h", 1.0),
    "ai": ("AI", "1/h", 1.0),
    "oai": ("OAI", "1/h", 1.0),
    "cai": ("CAI", "1/h", 1.0),
    "uai": ("UAI", "1/h", 1.0),
    "rin": ("RIN", "1/h", 1.0),
    "leak_p50": ("Leak.50", "L/s", 1.0),
    "leak_p95": ("Leak.95", "L/s", 1.0),
    "leak_max": ("Leak.Max", "L/s", 1.0),
    "mask_pressure_p50": ("MaskPress.50", "cmH2O", 1.0),
    "mask_pressure_p95": ("MaskPress.95", "cmH2O", 1.0),
    "mask_pressure_max": ("MaskPress.Max", "cmH2O", 1.0),
    "resp_rate_p50": ("RespRate.50", "1/min", 1.0),
    "resp_rate_p95": ("RespRate.95", "1/min", 1.0),
    "tidal_volume_p50": ("TidVol.50", "mL", 1000.0),
    "tidal_volume_p95": ("TidVol.95", "mL", 1000.0),
    "minute_vent_p50": ("MinVent.50", "L/min", 1.0),
    "minute_vent_p95": ("MinVent.95", "L/min", 1.0),
    "spo2_p50": ("SpO2.50", "%", 1.0),
    "spo2_p95": ("SpO2.95", "%", 1.0),
    "spo2_min": ("SpO2.Min", "%", 1.0),
    "csr_minutes": ("CSR", "min", 1.0),
}

# Common settings (all modes), canonical key -> STR label. Everything `S.*` also goes to `raw`.
COMMON_SETTINGS: dict[str, str] = {
    "epr_level": "S.EPR.Level",
    "epr_type": "S.EPR.EPRType",
    "ramp_enable": "S.RampEnable",
    "ramp_time_min": "S.RampTime",
    "climate_control": "S.ClimateControl",
    "humidity_level": "S.HumLevel",
    "mask_type": "S.Mask",
    "tube_type": "S.Tube",
}
# Mode-specific settings: only the fields of the active mode are meaningful.
MODE_SETTINGS: dict[str, dict[str, str]] = {
    "cpap": {"start_pressure": "S.C.StartPress", "pressure": "S.C.Press"},
    "apap": {
        "start_pressure": "S.A.StartPress",
        "min_pressure": "S.A.MinPress",
        "max_pressure": "S.A.MaxPress",
    },
    "asv": {
        "start_pressure": "S.AV.StartPress",
        "epap": "S.AV.EPAP",
        "min_ps": "S.AV.MinPS",
        "max_ps": "S.AV.MaxPS",
    },
    "asv_auto": {
        "start_pressure": "S.AA.StartPress",
        "min_epap": "S.AA.MinEPAP",
        "max_epap": "S.AA.MaxEPAP",
        "min_ps": "S.AA.MinPS",
        "max_ps": "S.AA.MaxPS",
    },
}


def _num(x: float) -> float:
    return round(float(x), 4)


class StrFile:
    """Thin accessor over the STR EdfFile: per-record values by label."""

    def __init__(self, path: Path) -> None:
        self.edf = EdfFile.open(path)
        self.n = self.edf.n_records
        self._cache: dict[str, list[float]] = {}
        self.warnings = list(self.edf.warnings)

    def has(self, label: str) -> bool:
        return label in self.edf.signals

    def per_record(self, label: str) -> list[list[float]]:
        spr = self.edf.signals[label].samples_per_record
        flat = self.edf.read_signal(label)
        return [[_num(x) for x in flat[i * spr : (i + 1) * spr]] for i in range(self.n)]

    def scalar(self, label: str) -> list[float | None]:
        """One value per record; negative values (not available) → None."""
        if not self.has(label):
            return [None] * self.n
        return [None if v[0] < 0 else v[0] for v in self.per_record(label)]


def read_therapy_days(path: Path, device_id: str, tz: ZoneInfo) -> list[TherapyDay]:
    s = StrFile(path)
    dates = [v[0] for v in s.per_record("Date")]
    duration = s.scalar("Duration")
    mask_on = s.per_record("MaskOn") if s.has("MaskOn") else [[]] * s.n
    mask_off = s.per_record("MaskOff") if s.has("MaskOff") else [[]] * s.n
    cols = {m: s.scalar(lbl) for m, (lbl, _, _) in METRIC_LABELS.items() if s.has(lbl)}
    out: list[TherapyDay] = []
    for i in range(s.n):
        if dates[i] <= 0:
            continue
        day = EPOCH + timedelta(days=int(dates[i]))
        noon = datetime(day.year, day.month, day.day, 12, tzinfo=tz).astimezone(UTC)
        intervals = tuple(
            (noon + timedelta(minutes=a), noon + timedelta(minutes=b))
            for a, b in zip(mask_on[i], mask_off[i], strict=False)
            if a >= 0 and b >= 0 and b >= a
        )
        metrics: dict[str, Metric] = {}
        for name, values in cols.items():
            _, unit, scale = METRIC_LABELS[name]
            v = values[i]
            metrics[name] = Metric(
                None if v is None else _num(v * scale), unit, Provenance.DEVICE, "STR.edf daily summary"
            )
        usage = duration[i]
        out.append(TherapyDay(device_id, day, None if usage is None else usage * 60.0, intervals, metrics))
    return out


def read_settings(path: Path, device_id: str) -> list[Settings]:
    """Settings change-points: one row for the first day and for each day the settings differ."""
    s = StrFile(path)
    dates = [v[0] for v in s.per_record("Date")]
    mode = [None if v is None else int(v) for v in s.scalar("Mode")]
    raw_labels = ["Mode"] + [lbl for lbl in s.edf.signals if lbl.startswith("S.")]
    raw_cols = {lbl: s.per_record(lbl) for lbl in raw_labels if s.has(lbl)}
    out: list[Settings] = []
    prev: tuple | None = None
    for i in range(s.n):
        if dates[i] <= 0:
            continue
        name = mode_name(mode[i])
        values: dict[str, float | str] = {}
        wanted = {**COMMON_SETTINGS, **MODE_SETTINGS.get(name or "", {})}
        for key, lbl in wanted.items():
            if not s.has(lbl):
                continue
            v = raw_cols[lbl][i][0] if lbl in raw_cols else s.per_record(lbl)[i][0]
            if v >= 0:
                values[key] = v
        raw = {lbl: col[i][0] for lbl, col in raw_cols.items()}
        sig = (name, tuple(sorted(values.items())), tuple(sorted(raw.items())))
        if sig == prev:
            continue
        prev = sig
        out.append(Settings(device_id, EPOCH + timedelta(days=int(dates[i])), name, values, raw))
    return out
