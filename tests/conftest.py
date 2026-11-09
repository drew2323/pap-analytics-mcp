from __future__ import annotations

from pathlib import Path

import pytest

DATA_DIR = Path(__file__).parent / "data"


@pytest.fixture(scope="session")
def resmed_air11() -> Path:
    """Real ResMed AirSense 11 SD-card/AirCANnect share snapshot (read-only)."""
    return DATA_DIR / "resmed-air11-autoset"


@pytest.fixture(scope="session")
def resmed_air11_str_history() -> Path:
    """Older STR.edf snapshot with 10 daily records (2026-10-16..25)."""
    return DATA_DIR / "resmed-air11-str-20261016"
