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


@pytest.fixture(scope="session")
def ingested_store(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Store dir with the STR history ingested first, then the main set (read it with read_only)."""
    from pap_analytics.config import Config, SourceConfig
    from pap_analytics.ingest import ingest

    store_dir = tmp_path_factory.mktemp("store")
    cfg = Config(sources=[SourceConfig(path=DATA_DIR / "resmed-air11-str-20261016")], store_dir=store_dir)
    ingest(cfg)
    cfg = Config(sources=[SourceConfig(path=DATA_DIR / "resmed-air11-autoset")], store_dir=store_dir)
    ingest(cfg)
    return store_dir


@pytest.fixture(scope="session")
def store(ingested_store: Path):
    from pap_analytics.store import Store

    with Store.open(ingested_store, read_only=True) as s:
        yield s
