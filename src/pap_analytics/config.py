"""Configuration: TOML file plus ``PAP_*`` environment overrides.

Sources may carry an optional ``[sources.sync]`` block (rsync pull into the local ``path``).
Secrets (SSH key, HTTP token) are referenced by file path only. Validation errors name the key.

Resolution order for the file: explicit path, ``$PAP_CONFIG``, default user path.
A missing file yields defaults. Environment variables win over the file.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

DEFAULT_CONFIG_PATH = Path("~/.config/pap-analytics/config.toml")
DEFAULT_STORE_DIR = Path("~/.local/share/pap-analytics")


class ConfigError(ValueError):
    """Invalid configuration; the message names the offending key."""


@dataclass(frozen=True)
class SyncConfig:
    remote: str
    ssh_key_file: Path
    known_hosts_file: Path
    type: str = "rsync"
    interval_s: int = 900


@dataclass(frozen=True)
class SourceConfig:
    path: Path
    adapter: str = "auto"
    timezone: str = "Europe/Prague"
    name: str = ""
    sync: SyncConfig | None = None

    @property
    def label(self) -> str:
        return self.name or str(self.path)

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


@dataclass(frozen=True)
class HttpConfig:
    host: str = "127.0.0.1"
    port: int = 8765
    token_file: Path | None = None


@dataclass(frozen=True)
class Config:
    sources: list[SourceConfig] = field(default_factory=list)
    store_dir: Path = field(default_factory=lambda: DEFAULT_STORE_DIR.expanduser())
    day_boundary_hour: int = 12
    http: HttpConfig = field(default_factory=HttpConfig)
    thresholds: dict[str, Any] = field(default_factory=dict)


def _path(value: str | os.PathLike[str]) -> Path:
    return Path(value).expanduser()


def _source(i: int, s: dict[str, Any]) -> SourceConfig:
    where = f"sources[{i}]"
    if not isinstance(s.get("path"), str) or not s["path"]:
        raise ConfigError(f"{where}.path: required string")
    tz = s.get("timezone", "Europe/Prague")
    try:
        ZoneInfo(tz)
    except Exception:
        raise ConfigError(f"{where}.timezone: unknown timezone {tz!r}") from None
    sync = None
    if "sync" in s:
        y = s["sync"]
        if not isinstance(y, dict):
            raise ConfigError(f"{where}.sync: must be a table")
        if y.get("type", "rsync") != "rsync":
            raise ConfigError(f"{where}.sync.type: only 'rsync' is supported, got {y['type']!r}")
        for key in ("remote", "ssh_key_file", "known_hosts_file"):
            if not isinstance(y.get(key), str) or not y[key]:
                raise ConfigError(f"{where}.sync.{key}: required string")
        interval = y.get("interval_s", 900)
        if isinstance(interval, bool) or not isinstance(interval, int) or interval < 10:
            raise ConfigError(f"{where}.sync.interval_s: integer >= 10 required, got {interval!r}")
        sync = SyncConfig(
            y["remote"], _path(y["ssh_key_file"]), _path(y["known_hosts_file"]), "rsync", interval
        )
        if not s.get("name"):
            raise ConfigError(f"{where}.name: required when a sync block is present")
    return SourceConfig(
        path=_path(s["path"]),
        adapter=s.get("adapter", "auto"),
        timezone=tz,
        name=s.get("name", ""),
        sync=sync,
    )


def load_config(path: str | os.PathLike[str] | None = None, env: dict[str, str] | None = None) -> Config:
    """Load configuration. ``env`` defaults to ``os.environ`` (injectable for tests)."""
    env = os.environ if env is None else env
    if path is not None:
        cfg_path: Path | None = _path(path)
    elif env.get("PAP_CONFIG"):
        cfg_path = _path(env["PAP_CONFIG"])
    else:
        cfg_path = DEFAULT_CONFIG_PATH.expanduser()

    data: dict[str, Any] = {}
    if cfg_path is not None and cfg_path.is_file():
        with cfg_path.open("rb") as fh:
            data = tomllib.load(fh)
    elif path is not None or env.get("PAP_CONFIG"):
        raise FileNotFoundError(f"config file not found: {cfg_path}")

    sources = [_source(i, s) for i, s in enumerate(data.get("sources", []))]
    names = [s.name for s in sources if s.name]
    for n in set(names):
        if names.count(n) > 1:
            raise ConfigError(f"sources.name: duplicate source name {n!r}")
    http_data = data.get("http", {})
    token_file = env.get("PAP_HTTP_TOKEN_FILE") or http_data.get("token_file")
    http = HttpConfig(
        host=env.get("PAP_HTTP_HOST", http_data.get("host", "127.0.0.1")),
        port=int(env.get("PAP_HTTP_PORT", http_data.get("port", 8765))),
        token_file=_path(token_file) if token_file else None,
    )
    store_dir = env.get("PAP_STORE_DIR") or data.get("store_dir")
    return Config(
        sources=sources,
        store_dir=_path(store_dir) if store_dir else DEFAULT_STORE_DIR.expanduser(),
        day_boundary_hour=int(env.get("PAP_DAY_BOUNDARY_HOUR", data.get("day_boundary_hour", 12))),
        http=http,
        thresholds=dict(data.get("thresholds", {})),
    )
