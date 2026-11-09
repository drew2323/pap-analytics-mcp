import json
from pathlib import Path

import pytest

from pap_analytics.cli import main
from pap_analytics.config import ConfigError, load_config

ROOT = Path(__file__).parent.parent

SYNC = """
[[sources]]
name = "aircannect"
path = "/data/src"
[sources.sync]
remote = "pap-sync@hnizdo:/srv/aircannect/"
ssh_key_file = "{key}"
known_hosts_file = "/etc/kh"
"""


def cfg(tmp_path: Path, text: str):
    f = tmp_path / "c.toml"
    f.write_text(text)
    return load_config(f, env={})


def test_example_has_source_name_and_docs_sync_block() -> None:
    c = load_config(ROOT / "config.example.toml", env={})
    assert c.sources[0].name == "aircannect" and c.sources[0].sync is None


def test_sync_block_parsed_with_defaults(tmp_path: Path) -> None:
    c = cfg(tmp_path, SYNC.format(key="/k"))
    sy = c.sources[0].sync
    assert sy and sy.type == "rsync" and sy.interval_s == 900 and sy.ssh_key_file == Path("/k")
    assert sy.remote.endswith("/srv/aircannect/")


@pytest.mark.parametrize(
    ("text", "key"),
    [
        ('[[sources]]\nname="a"\n', "sources[0].path"),
        ('[[sources]]\npath="/x"\ntimezone="Mars/Base"\n', "sources[0].timezone"),
        ('[[sources]]\nname="a"\npath="/x"\n[sources.sync]\ntype="ftp"\n', "sources[0].sync.type"),
        (
            '[[sources]]\nname="a"\npath="/x"\n[sources.sync]\nssh_key_file="k"\nknown_hosts_file="h"\n',
            "sync.remote",
        ),
        (
            '[[sources]]\nname="a"\npath="/x"\n[sources.sync]\nremote="r"\nknown_hosts_file="h"\n',
            "sync.ssh_key_file",
        ),
        (
            '[[sources]]\nname="a"\npath="/x"\n[sources.sync]\nremote="r"\nssh_key_file="k"\n',
            "sync.known_hosts_file",
        ),
        (SYNC.format(key="/k") + "interval_s = 2\n", "sync.interval_s"),
        (
            '[[sources]]\npath="/x"\n[sources.sync]\nremote="r"\nssh_key_file="k"\nknown_hosts_file="h"\n',
            "sources[0].name",
        ),
        ('[[sources]]\nname="a"\npath="/x"\n[[sources]]\nname="a"\npath="/y"\n', "sources.name"),
    ],
)
def test_invalid_config_names_the_key(tmp_path: Path, text: str, key: str) -> None:
    with pytest.raises(ConfigError, match=key.replace("[", r"\[").replace("]", r"\]")):
        cfg(tmp_path, text)


def test_config_show_redacts_secrets(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    key = tmp_path / "key"
    key.write_text("SUPER-SECRET-KEY-MATERIAL")
    tok = tmp_path / "tok"
    tok.write_text("SECRET-TOKEN-VALUE")
    f = tmp_path / "c.toml"
    f.write_text(SYNC.format(key=key) + f'\n[http]\ntoken_file = "{tok}"\n')
    assert main(["--config", str(f), "config", "show"]) == 0
    out = capsys.readouterr().out
    assert "SECRET" not in out
    d = json.loads(out)
    assert d["sources"][0]["sync"]["ssh_key_file"]["status"] == "ok (readable)"
    assert d["sources"][0]["sync"]["known_hosts_file"]["status"] == "MISSING"
    assert d["http"]["token_file"]["status"] == "ok (readable)"
