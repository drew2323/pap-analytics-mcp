from collections.abc import Iterator
from pathlib import Path

import pytest

from pap_analytics import adapters
from pap_analytics.adapters import base


class Dummy:
    name = "dummy"

    def __init__(self) -> None:
        self.warnings: list[str] = []

    def detect(self, path: Path) -> bool:
        return (path / "DUMMY").exists()


@pytest.fixture(autouse=True)
def clean_registry() -> Iterator[None]:
    saved = dict(base._REGISTRY)
    base._REGISTRY.clear()
    yield
    base._REGISTRY.clear()
    base._REGISTRY.update(saved)


def test_dummy_detected(tmp_path: Path) -> None:
    (tmp_path / "DUMMY").touch()
    d = Dummy()
    adapters.register(d)  # type: ignore[arg-type]
    assert adapters.detect(tmp_path) is d


def test_unknown_lists_tried(tmp_path: Path) -> None:
    adapters.register(Dummy())  # type: ignore[arg-type]
    with pytest.raises(adapters.UnknownSourceError, match="dummy"):
        adapters.detect(tmp_path)
