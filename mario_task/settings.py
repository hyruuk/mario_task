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
    ``serial``   — TTL byte over a serial port (e.g. ``/dev/ttyACM0``).
    ``parallel`` — Parallel-port bit pattern.
    ``null``     — No marker stream; useful for offline / dev.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field, fields, replace
from pathlib import Path
from typing import Any, Literal, Mapping

from mario_task import savestate
from mario_task.design import (
    ALL_POSSIBLE_LEVELS,
    DEFAULT_ENABLED_LEVELS,
)
from mario_task.markers import TriggerCodes

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
    codes: TriggerCodes = field(default_factory=TriggerCodes)


@dataclass(frozen=True)
class TaskSettings:
    max_duration_seconds: int = 600
    discovery_enabled: bool = True
    practice_enabled: bool = True
    # Levels enabled for discovery (visited in this order) and practice
    # (shuffled per epoch). Each entry is a ``(world, level)`` pair.
    # Default is the canonical 22-level set; you can override in
    # config.json to enable any subset of mario_task.design.ALL_POSSIBLE_LEVELS
    # (including the 8 castle X-4 levels and (2,2)/(7,2)). Practice runs
    # play levels sequentially from the design TSV; one "epoch" in the TSV
    # is one shuffle of enabled_levels, so the pool of unplayed levels is
    # depleted before any level can repeat.
    enabled_levels: tuple[tuple[int, int], ...] = field(
        default_factory=lambda: tuple(DEFAULT_ENABLED_LEVELS)
    )
    fixation_duration_seconds: float = 2.0
    # If True, append a Likert flow-ratings questionnaire at the end of every
    # run. Set False for dev / smoke-test runs where the experimenter just
    # wants to verify gameplay without filling in 12 questions.
    questionnaire_enabled: bool = True


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
        # enabled_levels: tuple[tuple[int, int], ...] → list of [w, l] pairs.
        d["task"]["enabled_levels"] = [
            [int(w), int(l)] for w, l in self.task.enabled_levels
        ]
        return d


def default_settings() -> Settings:
    """Return a fresh Settings instance with all defaults."""
    return Settings()


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _validate_codes(c: TriggerCodes) -> None:
    """Enforce the constraints documented on :class:`TriggerCodes`.

    Constraints (all enforced here):
        1. Every code value must fit in a single byte: ``[0, 255]``.
        2. Lifecycle codes must be strictly below ``game_frame_base`` so
           gameplay frame markers (``[base, base+mod)``) can never
           collide with lifecycle markers.
        3. Lifecycle codes must be distinct (otherwise analysts can't
           tell ``TASK_START`` apart from ``GAME_RESET``).
        4. ``game_frame_base`` must be ≥ 4 so all 4 lifecycle codes can
           fit below it.
        5. ``game_frame_mod`` must be > 0 (or ``encode_frame`` would
           divide-by-zero).
        6. ``game_frame_base + game_frame_mod`` must be ≤ 256 so the
           gameplay code range stays inside a byte.
    """
    lifecycle = {
        "task_start": c.task_start,
        "task_stop": c.task_stop,
        "game_reset": c.game_reset,
        "non_game_flip": c.non_game_flip,
    }
    for name, val in lifecycle.items():
        if not (0 <= val <= 255):
            raise ValueError(f"triggers.codes.{name}={val} must be in [0, 255]")
    # Distinctness first — gives a clearer message than "must be < base"
    # when someone accidentally sets two lifecycle codes equal.
    if len(set(lifecycle.values())) != len(lifecycle):
        raise ValueError(
            f"triggers.codes lifecycle values must be distinct, got {lifecycle}."
        )
    if c.game_frame_base < 4:
        raise ValueError(
            f"triggers.codes.game_frame_base={c.game_frame_base} must be ≥ 4 so "
            f"all 4 lifecycle codes (task_start, task_stop, game_reset, "
            f"non_game_flip) can fit below it without collisions."
        )
    if c.game_frame_base > 255:
        raise ValueError(
            f"triggers.codes.game_frame_base={c.game_frame_base} must be in [4, 255]"
        )
    for name, val in lifecycle.items():
        if val >= c.game_frame_base:
            raise ValueError(
                f"triggers.codes.{name}={val} must be < game_frame_base "
                f"({c.game_frame_base}); otherwise gameplay markers (which "
                f"occupy [{c.game_frame_base}, {c.game_frame_base + c.game_frame_mod})) "
                f"would collide with this lifecycle marker."
            )
    if c.game_frame_mod <= 0:
        raise ValueError(
            f"triggers.codes.game_frame_mod={c.game_frame_mod} must be > 0 "
            f"(it's the period of the rolling gameplay-frame counter)."
        )
    if c.game_frame_base + c.game_frame_mod > 256:
        raise ValueError(
            f"game_frame_base ({c.game_frame_base}) + game_frame_mod "
            f"({c.game_frame_mod}) = {c.game_frame_base + c.game_frame_mod} "
            f"must be ≤ 256 so gameplay markers stay within a single byte. "
            f"Either lower game_frame_base or game_frame_mod."
        )


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
    _validate_codes(s.triggers.codes)
    if s.task.max_duration_seconds <= 0:
        raise ValueError(
            f"task.max_duration_seconds must be > 0, got {s.task.max_duration_seconds}"
        )
    if not s.task.enabled_levels:
        raise ValueError("task.enabled_levels must be non-empty.")
    if len(set(s.task.enabled_levels)) != len(s.task.enabled_levels):
        raise ValueError(
            f"task.enabled_levels must not contain duplicates, "
            f"got {s.task.enabled_levels}."
        )
    invalid = [lvl for lvl in s.task.enabled_levels if lvl not in ALL_POSSIBLE_LEVELS]
    if invalid:
        raise ValueError(
            f"task.enabled_levels contains levels that don't exist in NES SMB: "
            f"{invalid}. Valid choices are mario_task.design.ALL_POSSIBLE_LEVELS "
            f"({len(ALL_POSSIBLE_LEVELS)} entries: 8 worlds × 4 levels)."
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


def _filter_known(d: Mapping[str, Any], cls: type) -> dict[str, Any]:
    """Drop keys not declared on the dataclass ``cls``.

    Lets ``config.json`` files written by older versions still load
    after a field is removed (e.g. the old ``n_levels_per_run``). We
    log a warning the first time so the operator notices.
    """
    known = {f.name for f in fields(cls)}
    out: dict[str, Any] = {}
    dropped: list[str] = []
    for k, v in d.items():
        if k in known:
            out[k] = v
        else:
            dropped.append(k)
    if dropped:
        import logging as _stdlib_logging
        _stdlib_logging.getLogger(__name__).info(
            "Ignoring unknown config keys for %s: %s "
            "(maybe a field was renamed/removed; safe to delete from config.json).",
            cls.__name__, dropped,
        )
    return out


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

    triggers_in = dict(data.get("triggers", {}))
    codes_in = triggers_in.pop("codes", None)
    triggers_in = _filter_known(triggers_in, TriggerSettings)
    if codes_in is not None:
        codes = TriggerCodes(**_filter_known(codes_in, TriggerCodes))
    else:
        codes = TriggerCodes()
    triggers_defaults = {f.name: getattr(TriggerSettings(), f.name) for f in fields(TriggerSettings)}
    triggers = TriggerSettings(**{**triggers_defaults, **triggers_in, "codes": codes})

    task_in = _filter_known(dict(data.get("task", {})), TaskSettings)
    # enabled_levels comes in as a list of [w, l] pairs in JSON; coerce
    # to the tuple-of-tuples our dataclass expects.
    if "enabled_levels" in task_in:
        task_in["enabled_levels"] = tuple(
            tuple(pair) for pair in task_in["enabled_levels"]
        )
    task_defaults = {f.name: getattr(TaskSettings(), f.name) for f in fields(TaskSettings)}
    task = TaskSettings(**{**task_defaults, **task_in})

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
    "MARIO_QUESTIONNAIRE_ENABLED": ("task", "questionnaire_enabled", "bool"),
    "MARIO_FIXATION_DURATION": ("task", "fixation_duration_seconds", int),
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
