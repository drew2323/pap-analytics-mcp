"""Tiny synthetic EDF writer for edge cases the real data lacks (tests only)."""

from __future__ import annotations

import struct


def _pad(value: object, width: int) -> bytes:
    return str(value).encode("ascii").ljust(width)[:width]


def make_edf(
    *,
    signals: list[tuple[str, list[list[int]]]] | None = None,
    start: str = "08.11.26 20.30.00",
    declared_records: int | None = None,
    record_duration: float = 1.0,
    recording: str = "",
    edf_plus: bool = False,
    tals: list[bytes] | None = None,
    truncate_bytes: int = 0,
) -> bytes:
    """Build an EDF. ``signals``: (label, records) with records = per-record int16 sample lists.

    ``tals``: raw annotation bytes per record (adds an ``EDF Annotations`` signal, 60 samples).
    """
    sigs: list[tuple[str, list[list[int]]]] = list(signals or [])
    n_rec = len(sigs[0][1]) if sigs else len(tals or [])
    ann_spr = 30
    if tals is not None:
        sigs.append(("EDF Annotations", [[0] * ann_spr for _ in tals]))
        n_rec = len(tals)
    ns = len(sigs)
    d, t = start.split(" ")
    header = (
        _pad("0", 8)
        + _pad("X X X X", 80)
        + _pad(recording or "X X X X", 80)
        + _pad(d, 8)
        + _pad(t, 8)
        + _pad(256 + 256 * ns, 8)
        + _pad("EDF+D" if edf_plus else "", 44)
        + _pad(n_rec if declared_records is None else declared_records, 8)
        + _pad(record_duration, 8)
        + _pad(ns, 4)
    )
    assert len(header) == 256
    cols = [[], [], [], [], [], [], [], [], [], []]
    for label, recs in sigs:
        spr = len(recs[0]) if label != "EDF Annotations" else ann_spr
        ann = label == "EDF Annotations"
        row = [
            label,
            "",
            "" if ann else "x",
            "-32768" if ann else "0",
            "32767" if ann else "3276.7",
            "-32768" if ann else "0",
            "32767" if ann else "32767",
            "",
            spr,
            "",
        ]
        for c, v, w in zip(cols, row, [16, 80, 8, 8, 8, 8, 8, 80, 8, 32], strict=True):
            c.append(_pad(v, w))
    body = b"".join(b"".join(c) for c in cols)
    data = bytearray()
    for r in range(n_rec):
        for label, recs in sigs:
            if label == "EDF Annotations":
                assert tals is not None
                data += tals[r].ljust(2 * ann_spr, b"\x00")
            else:
                data += struct.pack(f"<{len(recs[r])}h", *recs[r])
    out = header + body + bytes(data)
    return out[: len(out) - truncate_bytes] if truncate_bytes else out
