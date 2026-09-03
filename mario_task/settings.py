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

Two independent recording-hardware sections, matching
``controller_validation_task``:

    ``triggers`` — outgoing per-event markers (see :mod:`mario_task.markers`).
    ``sync``     — how the run start is aligned with the recording device
                   (see :mod:`mario_task.sync`).

A run can wait for a scanner and also emit markers, or do neither.
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
from mario_task.markers import TriggerCodes, TriggerEvents

# Bumping this should force a migration path. Keep it boring.
SCHEMA_VERSION = 1

TriggerBackend = Literal["lsl", "serial", "parallel", "null"]
_VALID_BACKENDS: tuple[TriggerBackend, ...] = ("lsl", "serial", "parallel", "null")

SyncMode = Literal["send", "wait", "none"]
SyncBackend = Literal["none", "serial", "parallel", "lsl", "key", "keyboard", "markers"]

#: Sync backends that physically write to a port and therefore need one.
_PORT_BACKENDS = ("serial", "parallel")

#: Sync backends valid per mode. "none" is always allowed and means "no
#: hardware": the keyboard when waiting, nothing at all when sending.
_VALID_SYNC_BACKENDS: dict[str, tuple[str, ...]] = {
    "send": ("none", "serial", "parallel", "lsl", "key", "markers"),
    "wait": ("none", "keyboard", "serial"),
}


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TriggerSettings:
    """Outgoing event markers for iEEG / EEG / MEG.

    Independent of :class:`SyncSettings`: a run can wait for a scanner and
    also emit markers, or do neither.

    ``codes`` says what value each event sends; ``on_*`` says whether it is
    sent at all. Task start / stop always fire — without them a recording
    cannot be segmented — so they have no switch.
    """

    backend: TriggerBackend = "lsl"
    port: str | None = None
    lsl_stream_name: str = "mario_task"
    lsl_stream_type: str = "Markers"
    lsl_stream_source_id: str = "mario_task_markers"
    codes: TriggerCodes = field(default_factory=TriggerCodes)
    # Decimation: emit one gameplay marker per N emulator frames.
    # 1 = every frame (legacy). Raise to throttle a saturated amplifier.
    # The cycling byte value (codes.game_frame_mod) increments per *sent*
    # trigger, not per emulator frame, so the rolling counter still
    # advances at 1/N of the bk2 rate. The .log line `trigger_sent
    # frame=...` records the emulator-frame index of every sent trigger.
    trigger_every: int = 1
    # Per-event switches. on_game_frame is the expensive one (60 markers/s);
    # turn it off and the stream keeps only the lifecycle markers, which is
    # enough to segment a recording into runs and attempts.
    on_game_frame: bool = True
    on_game_reset: bool = True
    on_non_game_flip: bool = True

    def events(self) -> TriggerEvents:
        """The ``on_*`` flags as the object :func:`markers.configure` takes."""
        return TriggerEvents(
            on_game_frame=self.on_game_frame,
            on_game_reset=self.on_game_reset,
            on_non_game_flip=self.on_non_game_flip,
        )


#: gym-retro's NES button order, from ``stable_retro/cores/fceumm.json``.
#: **Position is the contract**: ``emulator.step()`` takes one boolean per
#: entry in exactly this order, and :mod:`mario_task.questionnaire` indexes
#: into it (4=UP, 5=DOWN, 6=LEFT, 7=RIGHT, 8=A) so the questionnaire is
#: navigated with the same keys the game is played with. ``None`` marks a
#: pad slot the console does not have.
NES_BUTTONS: tuple[str | None, ...] = (
    "B", None, "SELECT", "START", "UP", "DOWN", "LEFT", "RIGHT", "A",
    None, None, None,
)

#: The buttons an operator can actually bind, in the order the wizard shows
#: them: movement first, then the two action buttons, then the console keys.
BINDABLE_BUTTONS: tuple[str, ...] = (
    "UP", "DOWN", "LEFT", "RIGHT", "A", "B", "START", "SELECT",
)

#: Key names are what pyglet reports, lowercased — see
#: :func:`mario_task.input._normalize_key`. Arrows are ``up``/``down``/
#: ``left``/``right``; letters and digits are themselves.
#:
#: The default is the classic NES-on-a-keyboard layout, and the one the
#: README documents: arrows to move, Z to run (B), X to jump (A). START and
#: SELECT are deliberately unbound — a subject who can pause mid-level
#: produces a recording nobody can segment.
DEFAULT_BUTTON_MAP: dict[str, str] = {
    "UP": "up",
    "DOWN": "down",
    "LEFT": "left",
    "RIGHT": "right",
    "A": "x",
    "B": "z",
    "START": "",
    "SELECT": "",
}

#: What an unbound button looks like in a ``key_set``. No pyglet key ever
#: normalises to this, so ``held_for`` reports it as never pressed.
UNBOUND = "_"


@dataclass(frozen=True)
class InputSettings:
    """Which keyboard key drives which NES button.

    The task reads the pad as a keyboard, so this is the whole of the
    controller configuration. Bind the keys your gamepad adapter actually
    sends (an fMRI-compatible pad usually presents as a USB keyboard), or
    leave the default to play at a desk.

    A blank value leaves that button unbound, which is what START and SELECT
    are by default.
    """

    button_map: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_BUTTON_MAP))

    def key_set(self) -> list[str]:
        """The positional key list ``emulator.step()`` and the task consume.

        >>> InputSettings().key_set()[:9]
        ['z', '_', '_', '_', 'up', 'down', 'left', 'right', 'x']
        """
        return [
            (self.button_map.get(button) or UNBOUND) if button else UNBOUND
            for button in NES_BUTTONS
        ]


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
class SyncSettings:
    """How the run start is aligned with the recording device.

    ``mode`` says *what happens* at run start:
      * ``"none"`` — start immediately. The default.
      * ``"wait"`` — hold on a waiting screen until the sync signal arrives.
      * ``"send"`` — emit ``signal`` once, to start the recording device.
        This is the fMRI case where the stimulus computer starts the scanner.

    ``backend`` says *over what*, and defaults to ``"none"`` — no hardware:
      * in ``wait`` mode the signal is then expected from the **keyboard**
        (most MR trigger boxes present as a USB keyboard emitting ``5`` or
        ``t``, so this is also the usual scanner setup);
      * in ``send`` mode there is nothing to send to, so the run starts
        immediately with a warning.

    Otherwise: ``serial`` / ``parallel`` write or read a byte on ``port``,
    ``lsl`` and ``key`` send only, ``keyboard`` waits only, and ``markers``
    (send only) re-uses the already-open outgoing marker backend so a single
    serial port can carry both the start signal and the event markers without
    being opened twice.

    ``signal`` is the sync signal itself, and means the same thing in both
    directions: in ``send`` mode it is what goes out (``"s"`` -> the byte 115),
    in ``wait`` mode it is what we listen for. It may list alternatives, which
    is what a keyboard trigger box usually needs — ``["5", "percent"]`` are the
    same physical key with and without shift. Only the first entry is ever
    sent.

    Sync happens once per gameplay run, and decides which screen gates it:
    ``wait`` shows only "Waiting for the scanner" (no "press X when ready"),
    ``send`` prompts first and then starts the recording, ``none`` prompts
    only.

    A ``serial``/``parallel`` port that is unset or will not open degrades to
    the ``"none"`` behaviour above rather than aborting the session.
    """

    mode: SyncMode = "none"
    backend: SyncBackend = "none"
    port: str | None = None
    signal: tuple[str, ...] = ("s",)
    n_dummy_scans: int = 0
    timeout_seconds: float | None = None


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
    sync: SyncSettings = field(default_factory=SyncSettings)
    input: InputSettings = field(default_factory=InputSettings)
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
        d["sync"]["signal"] = list(self.sync.signal)
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


def _validate_input(i: InputSettings) -> None:
    """Raise ``ValueError`` if the button map could not drive the game.

    Two mistakes are worth catching before a subject is in the scanner: a
    button the game needs that nothing can press, and one key bound to two
    buttons (which would press both at once, every time).
    """
    unknown = sorted(set(i.button_map) - set(BINDABLE_BUTTONS))
    if unknown:
        raise ValueError(
            f"input.button_map names {unknown}, which the NES does not have. "
            f"Valid buttons are {list(BINDABLE_BUTTONS)}."
        )

    # The four directions plus A and B are what playing Mario requires;
    # START and SELECT are optional and unbound by default.
    required = ("UP", "DOWN", "LEFT", "RIGHT", "A", "B")
    missing = [b for b in required if not i.button_map.get(b, "").strip()]
    if missing:
        raise ValueError(
            f"input.button_map leaves {missing} unbound, so the subject could "
            f"not play. Bind a key for each, e.g. {{\"{missing[0]}\": \"x\"}}. "
            f"Only START and SELECT may be left blank."
        )

    bound: dict[str, str] = {}
    for button in BINDABLE_BUTTONS:
        key = i.button_map.get(button, "").strip()
        if not key:
            continue
        if key in bound:
            raise ValueError(
                f"input.button_map binds {key!r} to both {bound[key]} and "
                f"{button}; one keypress would press both buttons at once. "
                f"Give each button its own key."
            )
        bound[key] = button


def _validate_sync(s: SyncSettings) -> None:
    """Raise ``ValueError`` if the sync section cannot start a run.

    Deliberately lenient about ports: a missing or dead one is handled at
    run time by :func:`mario_task.sync.configure`, which warns and degrades
    so the same ``config.json`` works at the scanner and on a desk. Only
    genuinely unusable *combinations* are rejected here.
    """
    valid_modes = ("send", "wait", "none")
    if s.mode not in valid_modes:
        raise ValueError(f"sync.mode must be one of {valid_modes}, got {s.mode!r}.")

    if s.mode != "none" and not s.signal:
        raise ValueError(
            "sync.signal is empty: there would be nothing to send, and nothing "
            "a scanner TTL could match. Most MR trigger boxes emit '5' or 't'."
        )

    if s.mode in _VALID_SYNC_BACKENDS:
        valid = _VALID_SYNC_BACKENDS[s.mode]
        if s.backend not in valid:
            raise ValueError(
                f"sync.backend for mode {s.mode!r} must be one of {valid}, "
                f"got {s.backend!r}."
            )

    if s.n_dummy_scans < 0:
        raise ValueError(f"sync.n_dummy_scans must be >= 0, got {s.n_dummy_scans}.")
    if s.timeout_seconds is not None and s.timeout_seconds <= 0:
        raise ValueError(
            f"sync.timeout_seconds must be > 0, got {s.timeout_seconds}. "
            f"Set it to null to wait indefinitely."
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
    if s.triggers.trigger_every < 1:
        raise ValueError(
            f"triggers.trigger_every={s.triggers.trigger_every} must be ≥ 1 "
            f"(1 = a trigger every emulator frame, N = every Nth)."
        )
    _validate_sync(s.sync)
    _validate_input(s.input)
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


def _str_tuple(value: Any) -> tuple[str, ...]:
    """Accept ``"s"`` as readily as ``["5", "percent"]``.

    A single key is the common case, and quoting it as a bare string is what
    anyone hand-editing config.json will do.

    >>> _str_tuple("s")
    ('s',)
    >>> _str_tuple(["5", "percent"])
    ('5', 'percent')
    """
    return (value,) if isinstance(value, str) else tuple(str(v) for v in value)


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

    # A partial button_map is merged onto the default, so a config that
    # only rebinds A and B keeps working arrows.
    input_in = _filter_known(dict(data.get("input", {})), InputSettings)
    button_map = {**DEFAULT_BUTTON_MAP, **(input_in.get("button_map") or {})}
    inputs = InputSettings(button_map={k: str(v) for k, v in button_map.items()})

    sync_in = _filter_known(dict(data.get("sync", {})), SyncSettings)
    # JSON has no tuple; and a lone signal is naturally written as a bare
    # string ("s") by anyone hand-editing config.json.
    if "signal" in sync_in:
        sync_in["signal"] = _str_tuple(sync_in["signal"])
    sync_defaults = {f.name: getattr(SyncSettings(), f.name) for f in fields(SyncSettings)}
    sync = SyncSettings(**{**sync_defaults, **sync_in})

    return Settings(
        triggers=triggers, sync=sync, input=inputs, task=task,
        display=display, paths=paths,
    )


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
    "MARIO_TRIGGER_EVERY": ("triggers", "trigger_every", int),
    "MARIO_TRIGGER_ON_GAME_FRAME": ("triggers", "on_game_frame", "bool"),
    "MARIO_TRIGGER_ON_GAME_RESET": ("triggers", "on_game_reset", "bool"),
    "MARIO_TRIGGER_ON_NON_GAME_FLIP": ("triggers", "on_non_game_flip", "bool"),
    "LSL_STREAM_NAME": ("triggers", "lsl_stream_name", str),
    "LSL_STREAM_TYPE": ("triggers", "lsl_stream_type", str),
    "LSL_STREAM_SOURCE_ID": ("triggers", "lsl_stream_source_id", str),
    # sync
    "MARIO_SYNC_MODE": ("sync", "mode", str),
    "MARIO_SYNC_BACKEND": ("sync", "backend", str),
    "MARIO_SYNC_PORT": ("sync", "port", str),
    "MARIO_SYNC_SIGNAL": ("sync", "signal", "keys"),
    "MARIO_SYNC_DUMMY_SCANS": ("sync", "n_dummy_scans", int),
    "MARIO_SYNC_TIMEOUT": ("sync", "timeout_seconds", "opt_float"),
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


def _parse_opt_float(val: str) -> float | None:
    """Blank means "no timeout" (wait indefinitely), not zero."""
    return None if val.strip() == "" else float(val)


def _parse_keys(val: str) -> tuple[str, ...]:
    """Comma-separated key names, for the one-line-per-setting env format.

    >>> _parse_keys("s")
    ('s',)
    >>> _parse_keys("5, percent")
    ('5', 'percent')
    """
    return tuple(part.strip() for part in val.split(",") if part.strip())


def _apply_env(s: Settings, env: Mapping[str, str]) -> Settings:
    """Return a new Settings with env-var overrides applied."""
    patches: dict[str, dict[str, Any]] = {
        "triggers": {}, "sync": {}, "task": {}, "display": {}, "paths": {},
    }
    for env_key, (section, field_name, kind) in _ENV_KEYS.items():
        if env_key not in env:
            continue
        raw = env[env_key]
        try:
            if kind is str:
                value: Any = raw
            elif kind is int:
                value = int(raw)
            elif kind == "bool":
                value = _parse_bool(raw)
            elif kind == "opt_float":
                value = _parse_opt_float(raw)
            elif kind == "keys":
                value = _parse_keys(raw)
            else:  # pragma: no cover - defensive
                raise AssertionError(f"unknown env kind {kind!r}")
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"Cannot parse environment variable {env_key}={raw!r}: {exc}"
            ) from exc
        patches[section][field_name] = value

    # Compose window size from W+H if both set.
    if "EXP_WIN_W" in env and "EXP_WIN_H" in env:
        patches["display"]["window_size"] = (int(env["EXP_WIN_W"]), int(env["EXP_WIN_H"]))

    if not any(patches.values()):
        return s
    return replace(
        s,
        **{
            name: replace(getattr(s, name), **patch)
            for name, patch in patches.items()
            if patch
        },
    )


#: CLI dest -> (section, field). Declarative so the argparse flags in
#: :mod:`mario_task.cli` and the settings fields can be checked against each
#: other by eye. Values of ``None`` mean "flag not given" and are ignored.
_CLI_KEYS: dict[str, tuple[str, str]] = {
    "output_root": ("paths", "output_root"),
    "max_duration": ("task", "max_duration_seconds"),
    "fullscreen": ("display", "fullscreen"),
    "screen_index": ("display", "screen_index"),
    "trigger_backend": ("triggers", "backend"),
    "trigger_port": ("triggers", "port"),
    "trigger_every": ("triggers", "trigger_every"),
    "sync_mode": ("sync", "mode"),
    "sync_backend": ("sync", "backend"),
    "sync_port": ("sync", "port"),
    "sync_signal": ("sync", "signal"),
}

#: Retired flag names kept working. ``--eeg-backend`` / ``--eeg-port`` predate
#: the rename that lined this task's flags up with controller_validation_task;
#: they still parse, and land on the same fields.
_CLI_ALIASES: dict[str, str] = {
    "eeg_backend": "trigger_backend",
    "eeg_port": "trigger_port",
}


def _apply_cli(s: Settings, cli: Mapping[str, Any]) -> Settings:
    """Return a new Settings with CLI flag overrides applied.

    Recognized keys are the dests in :data:`_CLI_KEYS` (plus the aliases in
    :data:`_CLI_ALIASES`). Any subset may be given; ``None`` values mean "flag
    not provided" and leave the lower-precedence layers alone.
    """
    patches: dict[str, dict[str, Any]] = {}
    for name, value in cli.items():
        if value is None:
            continue
        name = _CLI_ALIASES.get(name, name)
        if name not in _CLI_KEYS:
            continue
        section, field_name = _CLI_KEYS[name]
        patches.setdefault(section, {})[field_name] = value

    # max_duration arrives as a string from some callers; the field is an int.
    if "task" in patches and "max_duration_seconds" in patches["task"]:
        patches["task"]["max_duration_seconds"] = int(patches["task"]["max_duration_seconds"])
    if "display" in patches and "fullscreen" in patches["display"]:
        patches["display"]["fullscreen"] = bool(patches["display"]["fullscreen"])

    if not patches:
        return s
    return replace(
        s,
        **{name: replace(getattr(s, name), **patch) for name, patch in patches.items()},
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
