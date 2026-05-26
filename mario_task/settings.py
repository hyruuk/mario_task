"""Configuration: schema, defaults, override hierarchy, atomic save.

A single ``config.json`` at the repo root is the sticky source of truth.
It is written by the first-run GUI wizard and read on every launch. For
ad-hoc overrides (debug runs, CI), the following hierarchy applies, with
**later sources winning over earlier ones**:

    1. defaults  (hardcoded in this module)
    2. config.json
    3. environment variables   (MARIO_*, LSL_*, EXP_WIN_*)
    4. CLI flag overrides

The merged result is a :class:`Settings` dataclass. Pure-Python module:
no psychopy, no retro. Safe to import from tests.

Backend choice for triggers:
    ``lsl``      — Lab Streaming Layer (default, recommended for iEEG).
    ``serial``   — TTL byte over a serial port (e.g. ``/dev/ttyACM0``, ``COM3``).
    ``parallel`` — Parallel-port bit pattern (Linux only).
    ``null``     — No marker stream; useful for offline / dev.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Literal, Mapping

from mario_task import savestate

# Bumping this should force a migration path. Keep it boring.
SCHEMA_VERSION = 1

TriggerBackend = Literal["lsl", "serial", "parallel", "null"]
_VALID_BACKENDS: tuple[TriggerBackend, ...] = ("lsl", "serial", "parallel", "null")


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TriggerSettings:
    backend: TriggerBackend = "lsl"
    port: str | None = None
    lsl_stream_name: str = "mario_task"
    lsl_stream_type: str = "Markers"
    lsl_stream_source_id: str = "mario_task_markers"


@dataclass(frozen=True)
class TaskSettings:
    max_duration_seconds: int = 600
    discovery_enabled: bool = True
    practice_enabled: bool = True
    n_levels_per_run: int = 22


@dataclass(frozen=True)
class DisplaySettings:
    fullscreen: bool = True
    screen_index: int | None = None  # None = auto
    window_size: tuple[int, int] | None = None  # None = auto


@dataclass(frozen=True)
class PathSettings:
    rom_file: str = "data/mario.stimuli/SuperMarioBros-Nes/rom.nes"
    data_root: str = "data/mario.stimuli/SuperMarioBros-Nes"
    output_root: str = "output"
    designs_root: str = "data/videogames/mario/designs"


@dataclass(frozen=True)
class Settings:
    """Top-level settings object. All fields are immutable; use :func:`replace`
    or the ``with_*`` helpers to derive a modified copy."""

    triggers: TriggerSettings = field(default_factory=TriggerSettings)
    task: TaskSettings = field(default_factory=TaskSettings)
    display: DisplaySettings = field(default_factory=DisplaySettings)
    paths: PathSettings = field(default_factory=PathSettings)
    schema_version: int = SCHEMA_VERSION

    # ----- export -----

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        # Tuples → lists for JSON compatibility.
        if d["display"]["window_size"] is not None:
            d["display"]["window_size"] = list(d["display"]["window_size"])
        return d


def default_settings() -> Settings:
    """Return a fresh Settings instance with all defaults."""
    return Settings()


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _validate(s: Settings) -> None:
    if s.triggers.backend not in _VALID_BACKENDS:
        raise ValueError(
            f"triggers.backend must be one of {_VALID_BACKENDS}, got {s.triggers.backend!r}"
        )
    if s.triggers.backend in ("serial", "parallel") and not s.triggers.port:
        raise ValueError(
            f"triggers.port must be set when backend={s.triggers.backend!r} "
            f"(e.g. '/dev/ttyACM0', 'COM3', '/dev/parport1')."
        )
    if s.task.max_duration_seconds <= 0:
        raise ValueError(
            f"task.max_duration_seconds must be > 0, got {s.task.max_duration_seconds}"
        )
    if s.task.n_levels_per_run <= 0:
        raise ValueError(
            f"task.n_levels_per_run must be > 0, got {s.task.n_levels_per_run}"
        )
    if not s.task.discovery_enabled and not s.task.practice_enabled:
        raise ValueError(
            "At least one of task.discovery_enabled / task.practice_enabled must be True."
        )
    if (
        s.display.screen_index is not None
        and not isinstance(s.display.screen_index, int)
    ):
        raise ValueError(
            f"display.screen_index must be int or null, got {type(s.display.screen_index).__name__}"
        )
    if s.display.window_size is not None:
        try:
            w, h = s.display.window_size
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"display.window_size must be [width, height], got {s.display.window_size!r}"
            ) from exc
        if w <= 0 or h <= 0:
            raise ValueError(f"display.window_size must have positive dims, got {(w, h)!r}")


# ---------------------------------------------------------------------------
# File I/O
# ---------------------------------------------------------------------------


def _from_dict(data: Mapping[str, Any]) -> Settings:
    """Build a Settings from a (possibly partial) dict; missing fields → defaults.

    Raises ``ValueError`` if the schema_version disagrees.
    """
    version = data.get("schema_version", SCHEMA_VERSION)
    if version != SCHEMA_VERSION:
        raise ValueError(
            f"config.json schema_version={version} is not supported by this build "
            f"(expected {SCHEMA_VERSION}). Delete config.json and re-run the wizard."
        )

    triggers = TriggerSettings(**{**asdict(TriggerSettings()), **data.get("triggers", {})})
    task = TaskSettings(**{**asdict(TaskSettings()), **data.get("task", {})})

    display_in = data.get("display", {})
    ws = display_in.get("window_size")
    if isinstance(ws, list):
        ws = tuple(ws)
    display_kwargs = {**asdict(DisplaySettings()), **display_in}
    display_kwargs["window_size"] = ws  # ensure tuple, not list
    display = DisplaySettings(**display_kwargs)

    paths = PathSettings(**{**asdict(PathSettings()), **data.get("paths", {})})
    return Settings(triggers=triggers, task=task, display=display, paths=paths)


def load_from_file(path: str | os.PathLike[str]) -> Settings:
    """Load Settings from a config.json file. Raises if the file is invalid."""
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    s = _from_dict(data)
    _validate(s)
    return s


def save(path: str | os.PathLike[str], s: Settings) -> None:
    """Atomically write Settings to ``path`` as JSON."""
    _validate(s)
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    savestate.save(path, s.to_dict())


def config_path_default() -> Path:
    """Return ``./config.json`` (in cwd). The cli typically passes its own path."""
    return Path("config.json")


# ---------------------------------------------------------------------------
# Override hierarchy
# ---------------------------------------------------------------------------


_ENV_KEYS = {
    # triggers
    "MARIO_TRIGGER_BACKEND": ("triggers", "backend", str),
    "MARIO_TRIGGER_PORT": ("triggers", "port", str),
    "LSL_STREAM_NAME": ("triggers", "lsl_stream_name", str),
    "LSL_STREAM_TYPE": ("triggers", "lsl_stream_type", str),
    "LSL_STREAM_SOURCE_ID": ("triggers", "lsl_stream_source_id", str),
    # task
    "MARIO_MAX_DURATION": ("task", "max_duration_seconds", int),
    "MARIO_DISCOVERY_ENABLED": ("task", "discovery_enabled", "bool"),
    "MARIO_PRACTICE_ENABLED": ("task", "practice_enabled", "bool"),
    # display
    "EXP_WIN_FULLSCR": ("display", "fullscreen", "bool"),
    "EXP_WIN_SCREEN": ("display", "screen_index", int),
    # paths
    "MARIO_DATA_ROOT": ("paths", "data_root", str),
    "MARIO_OUTPUT_ROOT": ("paths", "output_root", str),
}


def _parse_bool(val: str) -> bool:
    return val.strip().lower() not in ("0", "false", "no", "off", "")


def _apply_env(s: Settings, env: Mapping[str, str]) -> Settings:
    """Return a new Settings with env-var overrides applied."""
    patches: dict[str, dict[str, Any]] = {"triggers": {}, "task": {}, "display": {}, "paths": {}}
    for env_key, (section, field_name, kind) in _ENV_KEYS.items():
        if env_key not in env:
            continue
        raw = env[env_key]
        if kind is str:
            value: Any = raw
        elif kind is int:
            value = int(raw)
        elif kind == "bool":
            value = _parse_bool(raw)
        else:  # pragma: no cover - defensive
            raise AssertionError(f"unknown env kind {kind!r}")
        patches[section][field_name] = value

    # Compose window size from W+H if both set.
    if "EXP_WIN_W" in env and "EXP_WIN_H" in env:
        patches["display"]["window_size"] = (int(env["EXP_WIN_W"]), int(env["EXP_WIN_H"]))

    if not any(patches.values()):
        return s
    new_triggers = replace(s.triggers, **patches["triggers"]) if patches["triggers"] else s.triggers
    new_task = replace(s.task, **patches["task"]) if patches["task"] else s.task
    new_display = replace(s.display, **patches["display"]) if patches["display"] else s.display
    new_paths = replace(s.paths, **patches["paths"]) if patches["paths"] else s.paths
    return replace(
        s,
        triggers=new_triggers,
        task=new_task,
        display=new_display,
        paths=new_paths,
    )


def _apply_cli(s: Settings, cli: Mapping[str, Any]) -> Settings:
    """Return a new Settings with CLI flag overrides applied.

    Recognized keys (any subset; ``None`` values are ignored, treated as
    "not provided"):

        eeg_backend, eeg_port, max_duration, output_root, fullscreen, ctl_win
    """
    triggers_patch: dict[str, Any] = {}
    task_patch: dict[str, Any] = {}
    display_patch: dict[str, Any] = {}
    paths_patch: dict[str, Any] = {}

    if (v := cli.get("eeg_backend")) is not None:
        triggers_patch["backend"] = v
    if (v := cli.get("eeg_port")) is not None:
        triggers_patch["port"] = v
    if (v := cli.get("max_duration")) is not None:
        task_patch["max_duration_seconds"] = int(v)
    if (v := cli.get("output_root")) is not None:
        paths_patch["output_root"] = v
    if (v := cli.get("fullscreen")) is not None:
        display_patch["fullscreen"] = bool(v)

    if not (triggers_patch or task_patch or display_patch or paths_patch):
        return s
    return replace(
        s,
        triggers=replace(s.triggers, **triggers_patch) if triggers_patch else s.triggers,
        task=replace(s.task, **task_patch) if task_patch else s.task,
        display=replace(s.display, **display_patch) if display_patch else s.display,
        paths=replace(s.paths, **paths_patch) if paths_patch else s.paths,
    )


def load(
    config_path: str | os.PathLike[str] | None = None,
    env: Mapping[str, str] | None = None,
    cli_overrides: Mapping[str, Any] | None = None,
) -> Settings:
    """Resolve the effective Settings using the documented hierarchy.

    Args:
        config_path:    Path to ``config.json``. ``None`` means "skip the file
                        layer entirely" (useful for tests). The CLI normally
                        passes the result of :func:`config_path_default`.
        env:            Mapping for env-var overrides; defaults to ``os.environ``.
                        Pass an empty dict in tests to suppress env-var lookup.
        cli_overrides:  Mapping of CLI flag overrides (see :func:`_apply_cli`
                        for recognized keys). ``None`` skips.

    Returns:
        A fully validated :class:`Settings` instance.
    """
    s = default_settings()
    if config_path is not None and Path(config_path).exists():
        s = load_from_file(config_path)
    if env is None:
        env = os.environ
    s = _apply_env(s, env)
    if cli_overrides:
        s = _apply_cli(s, cli_overrides)
    _validate(s)
    return s


def supports_parallel_port() -> bool:
    """Whether this platform supports the ``parallel`` trigger backend.

    Currently Linux only — ``pyparallel`` does not have a working Windows
    backend. The first-run wizard should hide the option when False.
    """
    return sys.platform.startswith("linux")
