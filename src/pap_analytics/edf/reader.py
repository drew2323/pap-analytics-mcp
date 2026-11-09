"""Generic, read-only EDF/EDF+ reader. No vendor mapping, no timezone handling.

Signals are read lazily per label straight from the file (``numpy.memmap``); the file
is only ever opened for reading. ResMed's ``Crc16`` pseudo-signal and EDF+ annotation
signals are not part of ``signals``; all raw headers are in ``raw_signal_headers``.
Header knowledge ported from airbreak-plus ``edf_resscan.py`` (MIT,
https://github.com/m-kozlowski/airbreak-plus).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np

from pap_analytics.edf.crc import crc16

ANNOTATIONS_LABEL = "EDF Annotations"
CRC_LABEL = "Crc16"
_MONTHS = {
    m: i
    for i, m in enumerate(
        ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], 1
    )
}
_STARTDATE = re.compile(r"Startdate\s+(\d{1,2})-([A-Za-z]{3})-(\d{4})")


class EdfError(ValueError):
    """The file is not a readable EDF."""


@dataclass(frozen=True)
class SignalHeader:
    label: str
    unit: str
    phys_min: float
    phys_max: float
    dig_min: int
    dig_max: int
    samples_per_record: int
    rate_hz: float
    prefiltering: str
    transducer: str


@dataclass(frozen=True)
class Annotation:
    onset_s: float
    duration_s: float | None
    text: str
    start: datetime  # naive local, as stored


def _field(raw: bytes) -> str:
    return raw.decode("ascii", errors="replace").strip(" \x00")


def _parse_start(header: bytes, recording: str) -> datetime:
    date_s, time_s = _field(header[168:176]), _field(header[176:184])
    try:
        dd, mm, yy = (int(x) for x in date_s.split("."))
        hh, mi, ss = (int(x) for x in time_s.split("."))
    except ValueError as exc:
        raise EdfError(f"bad start date/time {date_s!r} {time_s!r}") from exc
    year = 1900 + yy if yy >= 85 else 2000 + yy
    m = _STARTDATE.search(recording)
    if m and m.group(2).upper() in _MONTHS:
        dd, mm, year = int(m.group(1)), _MONTHS[m.group(2).upper()], int(m.group(3))
    return datetime(year, mm, dd, hh, mi, ss)


class EdfFile:
    path: Path
    start: datetime
    record_duration_s: float
    n_records: int
    is_edf_plus: bool
    signals: dict[str, SignalHeader]
    raw_signal_headers: dict[str, SignalHeader]
    warnings: list[str]

    def __init__(self) -> None:  # use EdfFile.open
        raise TypeError("use EdfFile.open(path)")

    @classmethod
    def open(cls, path: Path) -> EdfFile:
        path = Path(path)
        self = object.__new__(cls)
        self.path = path
        self.warnings = []
        with path.open("rb") as fh:
            fixed = fh.read(256)
            if len(fixed) < 256:
                raise EdfError(f"{path}: file shorter than EDF header")
            try:
                header_bytes = int(_field(fixed[184:192]))
                declared = int(_field(fixed[236:244]))
                self.record_duration_s = float(_field(fixed[244:252]))
                ns = int(_field(fixed[252:256]))
            except ValueError as exc:
                raise EdfError(f"{path}: malformed EDF header") from exc
            sig = fh.read(ns * 256)
        if len(sig) < ns * 256 or header_bytes < 256 + ns * 256:
            raise EdfError(f"{path}: truncated signal headers")

        self.patient_id = _field(fixed[8:88])
        self.recording_id = _field(fixed[88:168])
        self.start = _parse_start(fixed, self.recording_id)
        reserved = _field(fixed[192:236])
        self.is_edf_plus = reserved.startswith("EDF+")
        self._header_bytes = header_bytes

        def col(offset: int, width: int) -> list[bytes]:
            return [sig[offset + i * width : offset + (i + 1) * width] for i in range(ns)]

        # Column layout: label16 transducer80 unit8 pmin8 pmax8 dmin8 dmax8 prefilter80 spr8 reserved32
        widths = [16, 80, 8, 8, 8, 8, 8, 80, 8, 32]
        offs, o = [], 0
        for w in widths:
            offs.append(o)
            o += w * ns
        cols = [col(off, w) for off, w in zip(offs, widths, strict=True)]
        self.raw_signal_headers = {}
        self._spans: dict[str, tuple[int, int]] = {}  # label -> (byte offset in record, n samples)
        rec_off = 0
        for i in range(ns):
            label = _field(cols[0][i])
            spr = int(_field(cols[8][i]))
            hdr = SignalHeader(
                label=label,
                unit=_field(cols[2][i]),
                phys_min=float(_field(cols[3][i])),
                phys_max=float(_field(cols[4][i])),
                dig_min=int(_field(cols[5][i])),
                dig_max=int(_field(cols[6][i])),
                samples_per_record=spr,
                rate_hz=spr / self.record_duration_s if self.record_duration_s > 0 else 0.0,
                prefiltering=_field(cols[7][i]),
                transducer=_field(cols[1][i]),
            )
            self.raw_signal_headers[label] = hdr
            self._spans[label] = (rec_off, spr)
            rec_off += 2 * spr
        self._record_bytes = rec_off
        self.signals = {
            k: v for k, v in self.raw_signal_headers.items() if k not in (CRC_LABEL, ANNOTATIONS_LABEL)
        }

        size = path.stat().st_size
        actual = (size - header_bytes) // rec_off if rec_off else 0
        if declared < 0:
            self.n_records = actual
        elif declared != actual:
            self.n_records = min(declared, actual)
            self.warnings.append(
                f"declared {declared} records but file holds {actual} complete; using {self.n_records}"
            )
        else:
            self.n_records = declared
        if rec_off and (size - header_bytes) % rec_off:
            self.warnings.append("truncated: trailing partial record ignored")
        return self

    def _records(self) -> np.ndarray:
        if self.n_records == 0:
            return np.zeros((0, self._record_bytes), dtype=np.uint8)
        return np.memmap(
            self.path,
            dtype=np.uint8,
            mode="r",
            offset=self._header_bytes,
            shape=(self.n_records, self._record_bytes),
        )

    def _raw_column(self, label: str) -> np.ndarray:
        off, spr = self._spans[label]
        block = np.ascontiguousarray(self._records()[:, off : off + 2 * spr])
        return block.view("<i2").reshape(self.n_records, spr)

    def read_signal(self, label: str) -> np.ndarray:
        """Physical values as float32 (all records concatenated)."""
        if label not in self.signals:
            raise KeyError(f"no signal {label!r} in {self.path.name}")
        h = self.signals[label]
        dig = self._raw_column(label).reshape(-1).astype(np.float64)
        if h.dig_max == h.dig_min:
            return dig.astype(np.float32)
        scale = (h.phys_max - h.phys_min) / (h.dig_max - h.dig_min)
        return ((dig - h.dig_min) * scale + h.phys_min).astype(np.float32)

    def raw_digital(self, label: str) -> np.ndarray:
        """Unscaled int16 samples (e.g. STR fields where scaling is identity)."""
        if label not in self.signals:
            raise KeyError(f"no signal {label!r} in {self.path.name}")
        return self._raw_column(label).reshape(-1).copy()

    def verify_crc(self) -> list[int]:
        """Indices of records whose ResMed ``Crc16`` does not match. Empty if all fine or no Crc16."""
        if CRC_LABEL not in self._spans:
            return []
        off, _ = self._spans[CRC_LABEL]
        recs = self._records()
        bad = []
        for i in range(self.n_records):
            rec = bytes(recs[i])
            if crc16(rec[:off]) != int.from_bytes(rec[off : off + 2], "little"):
                bad.append(i)
        return bad

    def annotations(self) -> list[Annotation]:
        if ANNOTATIONS_LABEL not in self._spans:
            return []
        off, spr = self._spans[ANNOTATIONS_LABEL]
        recs = self._records()
        out: list[Annotation] = []
        for i in range(self.n_records):
            data = bytes(recs[i, off : off + 2 * spr])
            for tal in (t for t in data.split(b"\x00") if t):
                out.extend(self._parse_tal(tal))
        return out

    def _parse_tal(self, tal: bytes) -> list[Annotation]:
        head, sep, rest = tal.partition(b"\x14")
        m = re.fullmatch(rb"([+-]\d+(?:\.\d+)?)(?:\x15(\d+(?:\.\d+)?))?", head)
        if not sep or m is None:
            self.warnings.append(f"unparseable TAL ignored: {tal[:30]!r}")
            return []
        onset = float(m.group(1))
        duration = float(m.group(2)) if m.group(2) else None
        texts = [t.decode("utf-8", errors="replace") for t in rest.split(b"\x14") if t]
        start = self.start + timedelta(seconds=onset)
        # timekeeping TALs have no text and therefore yield no annotations
        return [Annotation(onset, duration, t, start) for t in texts]
