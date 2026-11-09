"""Pull a source's data into its local ``path`` with rsync over SSH (read-only towards the remote).

Boundaries: copies only; never reads the store lock and never writes to the remote. The data lands in
a temporary sibling directory and is swapped in atomically, so ingest never sees a half-synced tree.
Status (last attempt/success) is kept in ``<store_dir>/sync_state.json`` for ``list_devices``.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from pap_analytics.config import Config, SourceConfig

STATE_FILE = "sync_state.json"


class SyncError(RuntimeError):
    pass


@dataclass
class SyncResult:
    source: str
    ok: bool
    changed: int = 0
    error: str | None = None
    last_ok_at: datetime | None = None
    data_age_s: float | None = None
    messages: list[str] = field(default_factory=list)


def ssh_command(key: Path, known_hosts: Path) -> str:
    return " ".join(
        [
            "ssh", "-i", shlex.quote(str(key)), "-o", "IdentitiesOnly=yes",
            "-o", f"UserKnownHostsFile={shlex.quote(str(known_hosts))}",
            "-o", "StrictHostKeyChecking=yes", "-o", "BatchMode=yes",
        ]
    )  # fmt: skip


def rsync_args(src: SourceConfig, dest: Path, link_dest: Path | None = None) -> list[str]:
    assert src.sync is not None
    sy = src.sync
    args = [
        "rsync",
        "-a",
        "--delete",
        "--timeout=60",
        "--itemize-changes",
        "-e",
        ssh_command(sy.ssh_key_file, sy.known_hosts_file),
    ]
    if link_dest is not None:
        args.append(f"--link-dest={link_dest}")
    return [*args, sy.remote.rstrip("/") + "/", str(dest) + "/"]


def read_state(store_dir: Path) -> dict[str, dict[str, object]]:
    try:
        return json.loads((Path(store_dir) / STATE_FILE).read_text())
    except (FileNotFoundError, ValueError):
        return {}


def _write_state(store_dir: Path, name: str, entry: dict[str, object]) -> None:
    store_dir.mkdir(parents=True, exist_ok=True)
    state = read_state(store_dir)
    state[name] = entry
    tmp = store_dir / (STATE_FILE + ".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    os.replace(tmp, store_dir / STATE_FILE)


def _count_changes(output: str) -> int:
    n = 0
    for line in output.splitlines():
        # itemize lines start with an 11 char flag field; directory-only / unchanged are skipped
        if len(line) > 12 and line[0] in "<>ch*" and line[1] in "fdLDS" and not line.startswith("."):
            if line[1] == "f" or line.startswith("*deleting"):
                n += 1
        elif line.startswith("*deleting"):
            n += 1
    return n


def sync_source(config: Config, src: SourceConfig, *, now: datetime | None = None) -> SyncResult:
    """Sync one source. Failure keeps the last good copy and is reported, never raised."""
    if src.sync is None:
        raise SyncError(f"source {src.label!r} has no [sources.sync] block")
    now = now or datetime.now(UTC)
    prev = read_state(config.store_dir).get(src.label, {})
    prev_ok = prev.get("last_ok_at")
    tmp = src.path.parent / f".{src.path.name}.sync-new"
    old = src.path.parent / f".{src.path.name}.sync-old"
    result = SyncResult(src.label, ok=False)
    try:
        for p in (tmp, old):
            shutil.rmtree(p, ignore_errors=True)
        src.path.parent.mkdir(parents=True, exist_ok=True)
        if src.path.exists():
            # seed the new tree from the current one with hard links so rsync only transfers changes
            shutil.copytree(src.path, tmp, copy_function=os.link)
        else:
            tmp.mkdir()
        proc = subprocess.run(rsync_args(src, tmp), capture_output=True, text=True, timeout=3600)
        if proc.returncode != 0:
            raise SyncError(f"rsync exit {proc.returncode}: {(proc.stderr or proc.stdout).strip()[-500:]}")
        result.changed = _count_changes(proc.stdout)
        if src.path.exists():
            os.rename(src.path, old)
        os.rename(tmp, src.path)
        shutil.rmtree(old, ignore_errors=True)
        result.ok = True
        result.last_ok_at = now
        result.data_age_s = 0.0
    except (SyncError, OSError, subprocess.SubprocessError) as exc:
        shutil.rmtree(tmp, ignore_errors=True)
        if old.exists() and not src.path.exists():  # failed between the two renames: restore
            os.rename(old, src.path)
        result.error = str(exc)
        if isinstance(prev_ok, str):
            result.last_ok_at = datetime.fromisoformat(prev_ok)
            result.data_age_s = (now - result.last_ok_at).total_seconds()
    _write_state(
        config.store_dir,
        src.label,
        {
            "last_sync_at": now.isoformat(),
            "last_sync_ok": result.ok,
            "last_ok_at": result.last_ok_at.isoformat() if result.last_ok_at else None,
            "error": result.error,
            "changed": result.changed,
        },
    )
    return result


def sync_all(config: Config, only: str | None = None) -> list[SyncResult]:
    srcs = [s for s in config.sources if s.sync is not None and (only is None or s.name == only)]
    if only is not None and not srcs:
        raise SyncError(f"no source named {only!r} with a sync block")
    return [sync_source(config, s) for s in srcs]


def sleep_interval(config: Config) -> float:
    return float(min((s.sync.interval_s for s in config.sources if s.sync), default=900))
