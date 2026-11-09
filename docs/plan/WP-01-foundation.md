# WP-01 – Foundation: canonical model, config, adapter protocol, CLI skeleton

**Depends on:** – · **Unblocks:** all other WPs

## Goal
Freeze the shared contracts so WP-02…WP-09 can work in parallel without guessing.

## Scope
1. `src/pap_analytics/model.py` – canonical model exactly per `docs/ARCHITECTURE.md` (frozen dataclasses; numpy arrays for channel values), plus:
   - `SCHEMA_VERSION: int = 1`
   - enums/constants: `Channel names` (`CHANNELS: dict[str, str]` name → unit), `EventType` (StrEnum), `Provenance` (StrEnum: `device`, `derived`)
   - `therapy_day(ts: datetime, tz: ZoneInfo, boundary_hour: int = 12) -> date`
2. `src/pap_analytics/config.py` – `Config` (dataclass) loaded from TOML (`--config`, else `$PAP_CONFIG`, else `~/.config/pap-analytics/config.toml`) with env overrides `PAP_*`:
   - `sources: list[SourceConfig]` (`name`, `path`, `adapter: str = "auto"`, `timezone = "Europe/Prague"`, optional `sync` block: `type="rsync"`, `remote`, `ssh_key_file`, `known_hosts_file`, `interval_s=900` – added by WP-10), `store_dir` (default `~/.local/share/pap-analytics`), `day_boundary_hour = 12`, `http.host = "127.0.0.1"`, `http.port = 8765`, `http.token_file`, thresholds dict (used by WP-06/07/09, defaults defined there).
   - `config.example.toml` at repo root.
3. `src/pap_analytics/adapters/base.py` – Adapter protocol + registry:
4. `src/pap_analytics/cli.py` – subcommand registration mechanism (each WP adds its subcommand in its own module function `register(subparsers)`), global `--config`, `--log-level`.
5. Dependencies: add `numpy` (runtime). Commit `uv.lock`.

## Contract
```python
# model.py
@dataclass(frozen=True)
class Metric: value: float | None; unit: str; provenance: Provenance; method: str = ""

@dataclass(frozen=True)
class Device: id: str; vendor: str; model: str; model_code: str | None; serial: str | None
              firmware: str | None; source_kind: str            # e.g. "resmed-sd"

@dataclass(frozen=True)
class ChannelData: name: str; unit: str; rate_hz: float; start: datetime; values: np.ndarray  # float32, NaN=missing

@dataclass(frozen=True)
class Event: start: datetime; duration_s: float; type: EventType; raw_type: str; provenance: Provenance

@dataclass(frozen=True)
class Session: id: str; device_id: str; therapy_day: date; start: datetime; end: datetime
               complete: bool; fingerprint: str; source_files: tuple[str, ...]
               channels: tuple[ChannelData, ...]; events: tuple[Event, ...]

@dataclass(frozen=True)
class TherapyDay: device_id: str; date: date; usage_s: float | None
                  mask_intervals: tuple[tuple[datetime, datetime], ...]; metrics: dict[str, Metric]

@dataclass(frozen=True)
class Settings: device_id: str; valid_from: date; mode: str | None; values: dict[str, float | str]; raw: dict

# adapters/base.py
@dataclass(frozen=True)
class SessionRef: id: str; therapy_day: date; start: datetime; fingerprint: str; files: tuple[Path, ...]

class Adapter(Protocol):
    name: str                                            # "resmed"
    def detect(self, path: Path) -> bool: ...
    def device(self, path: Path) -> Device: ...
    def scan(self, path: Path, tz: ZoneInfo, boundary_hour: int) -> list[SessionRef]: ...
    def load_session(self, path: Path, ref: SessionRef, tz: ZoneInfo, boundary_hour: int) -> Session: ...
    def therapy_days(self, path: Path, tz: ZoneInfo) -> list[TherapyDay]: ...   # device-reported only
    def settings(self, path: Path, tz: ZoneInfo) -> list[Settings]: ...
    warnings: list[str]                                  # unmapped channels/events, recoverable issues

def register(adapter: Adapter) -> None: ...
def detect(path: Path) -> Adapter: ...                  # raises UnknownSourceError
```
Session `id` = `f"{device_id}:{start:%Y%m%dT%H%M%S}"` (start in UTC). Channel values timestamps: `start + i / rate_hz`.

## Out of scope
EDF parsing, any vendor logic, storage, analytics.

## Files
`model.py`, `config.py`, `adapters/__init__.py`, `adapters/base.py`, `cli.py`, `config.example.toml`, `tests/test_model.py`, `tests/test_config.py`, `tests/test_adapter_registry.py`, `pyproject.toml`, `uv.lock`.

## Acceptance
- `therapy_day`: 2026-11-08 20:30 and 2026-11-09 06:00 (Europe/Prague) → 2026-11-08; 2026-11-09 12:00 → 2026-11-09; correct around DST end 2026-10-25 (test 2026-10-25 02:30 both offsets).
- Config: example file loads; env override `PAP_STORE_DIR` wins over file; missing file → defaults.
- Registry: dummy adapter detected; unknown dir raises `UnknownSourceError` listing tried adapters.
- `uv run pap --help` lists global options.
