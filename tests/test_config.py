from pathlib import Path

import pytest

from pap_analytics.config import load_config

ROOT = Path(__file__).parent.parent


def test_example_loads() -> None:
    cfg = load_config(ROOT / "config.example.toml", env={})
    assert cfg.sources[0].adapter == "auto"
    assert cfg.sources[0].tz.key == "Europe/Prague"
    assert cfg.day_boundary_hour == 12
    assert cfg.http.port == 8765


def test_env_overrides_file(tmp_path: Path) -> None:
    f = tmp_path / "c.toml"
    f.write_text('store_dir = "/from/file"\n[http]\nport = 1\n')
    cfg = load_config(f, env={"PAP_STORE_DIR": "/from/env", "PAP_HTTP_PORT": "9999"})
    assert cfg.store_dir == Path("/from/env")
    assert cfg.http.port == 9999


def test_missing_default_file_gives_defaults(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = load_config(None, env={})
    assert cfg.sources == [] and cfg.http.host == "127.0.0.1" and cfg.day_boundary_hour == 12
    assert cfg.store_dir == tmp_path / ".local/share/pap-analytics"


def test_pap_config_env_selects_file(tmp_path: Path) -> None:
    f = tmp_path / "c.toml"
    f.write_text("day_boundary_hour = 10\n")
    assert load_config(None, env={"PAP_CONFIG": str(f)}).day_boundary_hour == 10


def test_explicit_missing_file_errors(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_config(tmp_path / "nope.toml", env={})
