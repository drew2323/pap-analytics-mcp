"""ResMed SD-card layout detection and case-insensitive file lookup."""

from __future__ import annotations

from pathlib import Path


def find(directory: Path, name: str) -> Path | None:
    """Find an entry without depending on host case rules (FAT/SMB vs. ext4)."""
    if not directory.is_dir():
        return None
    return next((p for p in directory.iterdir() if p.name.lower() == name.lower()), None)


def is_resmed(path: Path) -> bool:
    """STR.edf plus Identification.json/.tgt. DATALOG is optional (STR-only snapshots exist)."""
    return find(path, "STR.edf") is not None and (
        find(path, "Identification.json") is not None or find(path, "Identification.tgt") is not None
    )
