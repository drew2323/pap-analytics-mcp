from pathlib import Path

import pytest

from pap_analytics.config import ConfigError, load_config

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

def test_example_documents_every_supported_threshold() -> None:
    import re

    from pap_analytics.analytics.thresholds import SPECS

    text = (ROOT / "config.example.toml").read_text().split("[thresholds]")[1]
    documented = dict(re.findall(r"^# (\w+) = ([\d.]+)", text, re.M))
    assert set(documented) == set(SPECS)
    for key, value in documented.items():
        assert float(value) == SPECS[key][0], key

def test_uncommented_example_thresholds_are_valid_and_equal_defaults(tmp_path: Path) -> None:
    import re

    from pap_analytics.analytics.thresholds import effective

    text = re.sub(r"^# (\w+ = )", r"\1", (ROOT / "config.example.toml").read_text(), flags=re.M)
    cfg_file = tmp_path / "c.toml"
    cfg_file.write_text(text.split("[thresholds]")[0] + "[thresholds]" + text.split("[thresholds]")[1])
    cfg = load_config(cfg_file, env={})
    assert cfg.thresholds == effective({})

def test_threshold_override_and_validation(tmp_path: Path) -> None:
    f = tmp_path / "c.toml"
    f.write_text("[thresholds]\nleak_min_s = 60\nlarge_leak_lps = 0.3\n")
    assert load_config(f, env={}).thresholds == {"leak_min_s": 60.0, "large_leak_lps": 0.3}
    bad_values = (
        "nope = 1", "leak_min_s = -1", 'leak_min_s = "x"', "cluster_min_events = 2.5", "vent_low_ratio = 2",
    )
    for bad in bad_values:
        f.write_text(f"[thresholds]\n{bad}\n")
        with pytest.raises(ConfigError):
            load_config(f, env={})

def test_config_show_has_effective_thresholds(tmp_path: Path) -> None:
    from pap_analytics.cli_sync import effective_config

    f = tmp_path / "c.toml"
    f.write_text("[thresholds]\ngap_min = 20\n")
    th = effective_config(load_config(f, env={}))["thresholds"]
    assert th["gap_min"] == 20 and th["leak_min_s"] == 120 and th["large_leak_lps"] == 0.4

def test_context_uses_threshold_override(tmp_path: Path) -> None:
    from pap_analytics.analytics.timeparse import Context

    f = tmp_path / "c.toml"
    f.write_text("[thresholds]\nlarge_leak_lps = 0.25\n")
    assert Context.from_config(load_config(f, env={})).large_leak_lps == 0.25
