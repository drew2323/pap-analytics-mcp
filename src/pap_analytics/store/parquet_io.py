"""Per-channel Parquet files: one float32 column ``v`` plus ``rate_hz``/``start``/``unit`` metadata."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

ROW_GROUP = 65536  # samples; lets window reads skip most of an 8 h, 25 Hz channel


def write_channel(path: Path, values: np.ndarray, rate_hz: float, start: datetime, unit: str) -> int:
    arr = np.ascontiguousarray(values, dtype=np.float32)
    meta = {
        b"rate_hz": repr(float(rate_hz)).encode(),
        b"start": start.astimezone(UTC).isoformat().encode(),
        b"unit": unit.encode(),
    }
    table = pa.table({"v": pa.array(arr, type=pa.float32())}).replace_schema_metadata(meta)
    pq.write_table(table, path, row_group_size=ROW_GROUP, compression="zstd")
    return len(arr)


def read_slice(path: Path, i0: int, i1: int) -> np.ndarray:
    """Samples ``[i0, i1)`` as float32, reading only the overlapping row groups."""
    pf = pq.ParquetFile(path)
    parts: list[np.ndarray] = []
    pos = 0
    for g in range(pf.num_row_groups):
        n = pf.metadata.row_group(g).num_rows
        lo, hi = max(i0, pos), min(i1, pos + n)
        if lo < hi:
            col = pf.read_row_group(g, columns=["v"]).column("v").to_numpy()
            parts.append(col[lo - pos : hi - pos])
        pos += n
        if pos >= i1:
            break
    if not parts:
        return np.empty(0, dtype=np.float32)
    return np.concatenate(parts).astype(np.float32, copy=False)
