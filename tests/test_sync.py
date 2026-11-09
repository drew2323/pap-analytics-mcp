"""Sync tests use a local directory as the "remote" (rsync treats it as a local copy; no SSH)."""

import json
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from pap_analytics.cli import main
from pap_analytics.config import Config, SourceConfig, SyncConfig
from pap_analytics.sync import STATE_FILE, read_state, rsync_args, sync_all, sync_source

pytestmark = pytest.mark.skipif(shutil.which("rsync") is None, reason="rsync not installed")


def make(tmp_path: Path, remote: Path | None = None) -> tuple[Config, SourceConfig, Path]:
    remote = remote or tmp_path / "remote"
    remote.mkdir(exist_ok=True)
    sy = SyncConfig(str(remote) + "/", tmp_path / "key", tmp_path / "kh")
    src = SourceConfig(path=tmp_path / "local" / "aircannect", name="aircannect", sync=sy)
    return Config(sources=[src], store_dir=tmp_path / "store"), src, remote


def test_first_and_incremental_sync(tmp_path: Path) -> None:
    cfg, src, remote = make(tmp_path)
    (remote / "DATALOG").mkdir()
    (remote / "DATALOG" / "a.edf").write_text("one")
    (remote / "STR.edf").write_text("str")
    r = sync_source(cfg, src)
    assert r.ok and r.changed == 2
    assert (src.path / "DATALOG" / "a.edf").read_text() == "one"
    # incremental: one new, one modified, one deleted
    (remote / "DATALOG" / "b.edf").write_text("two")
    (remote / "STR.edf").write_text("str2")
    (remote / "DATALOG" / "a.edf").unlink()
    r = sync_source(cfg, src)
    assert r.ok and r.changed == 3
    assert sorted(p.name for p in (src.path / "DATALOG").iterdir()) == ["b.edf"]
    assert (src.path / "STR.edf").read_text() == "str2"
    r = sync_source(cfg, src)
    assert r.ok and r.changed == 0
    # no temp dirs left behind
    assert [p.name for p in src.path.parent.iterdir()] == ["aircannect"]


def test_old_tree_is_not_modified_in_place(tmp_path: Path) -> None:
    """The new tree is built beside the old one; the old files are never rewritten."""
    cfg, src, remote = make(tmp_path)
    (remote / "f").write_text("v1")
    sync_source(cfg, src)
    held = src.path / "f"
    ino = held.stat().st_ino
    (remote / "f").write_text("v2-longer")
    sync_source(cfg, src)
    assert held.read_text() == "v2-longer"  # path now points to the new file
    assert (src.path / "f").stat().st_ino != ino


def test_failure_keeps_last_good_copy_and_reports_age(tmp_path: Path) -> None:
    cfg, src, remote = make(tmp_path)
    (remote / "f").write_text("good")
    t0 = datetime(2026, 1, 1, 12, tzinfo=UTC)
    assert sync_source(cfg, src, now=t0).ok
    shutil.rmtree(remote)  # remote vanishes -> rsync fails
    r = sync_source(cfg, src, now=t0 + timedelta(hours=5))
    assert not r.ok and r.error and "rsync exit" in r.error
    assert r.data_age_s == 5 * 3600
    assert (src.path / "f").read_text() == "good"
    assert [p.name for p in src.path.parent.iterdir()] == ["aircannect"]
    st = read_state(cfg.store_dir)["aircannect"]
    assert st["last_sync_ok"] is False and st["last_ok_at"] == t0.isoformat() and st["error"]


def test_first_sync_failure_has_no_copy(tmp_path: Path) -> None:
    cfg, src, remote = make(tmp_path)
    shutil.rmtree(remote)
    r = sync_source(cfg, src)
    assert not r.ok and r.data_age_s is None and not src.path.exists()


def test_rsync_command_is_read_only_pull_with_strict_ssh(tmp_path: Path) -> None:
    _, src, _ = make(tmp_path)
    args = rsync_args(src, tmp_path / "dest")
    ssh = args[args.index("-e") + 1]
    assert "StrictHostKeyChecking=yes" in ssh and "BatchMode=yes" in ssh and "IdentitiesOnly=yes" in ssh
    assert f"UserKnownHostsFile={tmp_path / 'kh'}" in ssh and f"-i {tmp_path / 'key'}" in ssh
    assert "--delete" in args and "--timeout=60" in args
    assert args[-2].endswith("/remote/") and args[-1] == str(tmp_path / "dest") + "/"  # remote is the source


def test_cli_sync_and_ingest_uses_last_good_copy(
    tmp_path: Path, resmed_air11: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    remote = tmp_path / "remote"
    shutil.copytree(resmed_air11, remote)
    f = tmp_path / "c.toml"
    f.write_text(
        f'store_dir = "{tmp_path / "store"}"\n[[sources]]\nname = "aircannect"\npath = "{tmp_path / "src"}"\n'
        f'[sources.sync]\nremote = "{remote}/"\nssh_key_file = "{tmp_path / "k"}"\n'
        f'known_hosts_file = "{tmp_path / "h"}"\n'
    )
    assert main(["--config", str(f), "sync", "--source", "aircannect"]) == 0
    assert main(["--config", str(f), "sync", "--source", "nope"]) == 2
    assert main(["--config", str(f), "ingest", "--no-sync"]) == 0  # ingests the synced copy
    capsys.readouterr()
    shutil.rmtree(remote)
    rc = main(["--config", str(f), "ingest"])  # sync fails, ingest still runs on the copy
    out = capsys.readouterr().out
    assert rc == 1 and "sync of aircannect failed" in out and "last good copy" in out and "unchanged=" in out
    assert json.loads((tmp_path / "store" / STATE_FILE).read_text())["aircannect"]["last_sync_ok"] is False


def test_sync_all_unknown_name(tmp_path: Path) -> None:
    cfg, _, _ = make(tmp_path)
    from pap_analytics.sync import SyncError

    with pytest.raises(SyncError):
        sync_all(cfg, "other")
