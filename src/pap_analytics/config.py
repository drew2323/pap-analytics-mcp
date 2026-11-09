"""Configuration: TOML file plus ``PAP_*`` environment overrides.

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


@dataclass(frozen=True)
class SourceConfig:
    path: Path
    adapter: str = "auto"
    timezone: str = "Europe/Prague"

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

    sources = [
        SourceConfig(
            path=_path(s["path"]),
            adapter=s.get("adapter", "auto"),
            timezone=s.get("timezone", "Europe/Prague"),
        )
        for s in data.get("sources", [])
    ]
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
