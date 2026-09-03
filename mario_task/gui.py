"""First-run configuration wizard and subject picker.

Two dialogs, both optional — everything they set can also be given on the
command line or in ``config.json``. They exist so an operator can set up a new
rig, or start a session, without memorising flags.

The layout matches ``controller_validation_task``: the wizard's settings are
split across tabs, every field carries an ``ⓘ`` whose hover text explains what
the setting does, and the labels are just the ``config.json`` key so the dialog
and the file read alike. The one thing that has no counterpart there is the
8x4 level grid, which gets a tab of its own.

All PsychoPy / Qt imports are deferred into the function bodies, so this
module is importable on a headless machine (and its pure helpers are
unit-testable there).
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from html import escape
from pathlib import Path
from typing import Any

from mario_task import savestate
from mario_task import settings as settings_mod
from mario_task.design import (
    ALL_POSSIBLE_LEVELS,
    N_LEVELS_PER_RUN,
    WORLDS,
)
from mario_task.paths import infer_next_session, normalize_session, normalize_subject
from mario_task.settings import _VALID_BACKENDS, BINDABLE_BUTTONS, Settings

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Dialog field table
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Field:
    """One row of a dialog.

    ``key`` is what :func:`settings_from_wizard` looks for; ``label`` is what
    the operator sees. They are deliberately different (labels carry units and
    hints), which is exactly why results must be read back **by label** — see
    :func:`read_dialog_values`.

    ``tip`` is the hover text behind the field's ``ⓘ``. Because the tip is
    there, the label does not have to explain anything: it is just the name of
    the setting (matching ``config.json``, so the dialog and the file read
    alike), and the tip says what it does. Every field carries one —
    ``test_every_wizard_field_has_a_tip`` enforces it.
    """

    key: str
    label: str
    initial: Any = ""
    choices: Sequence[str] | None = None
    tip: str = ""


def read_dialog_values(fields: Sequence[Field], returned: Any) -> dict[str, Any]:
    """Map a PsychoPy dialog result back onto our answer keys.

    ``Dlg.show()`` returns an ``IndexDict`` **keyed by each field's label**
    (older PsychoPy returned a plain positional list). Zipping our key list
    against the result therefore pairs each key with a *label string* rather
    than a value.

    Reading by label handles both shapes and, more importantly, cannot drift
    when a field is added, removed or reordered.

    >>> fields = [Field("max_duration", "max_duration (s)"), Field("fullscreen", "fullscreen")]
    >>> read_dialog_values(fields, {"max_duration (s)": 600, "fullscreen": True})
    {'max_duration': 600, 'fullscreen': True}
    >>> read_dialog_values(fields, [600, True])          # legacy positional
    {'max_duration': 600, 'fullscreen': True}
    """
    if returned is None:
        return {}
    if isinstance(returned, Mapping):
        return {f.key: returned[f.label] for f in fields if f.label in returned}
    # Legacy: a positional sequence in field order.
    return {f.key: value for f, value in zip(fields, returned, strict=False)}


def _add_fields(dlg: Any, fields: Sequence[Field], *, with_tips: bool = True) -> None:
    """Add every field to ``dlg``, keyed by its label.

    ``with_tips=False`` suppresses PsychoPy's own tooltip, which puts the tip
    on the whole input box. The tabbed layout turns it off and hangs the tip
    off an ``ⓘ`` marker instead (see :func:`_add_tabbed_fields`); the inline
    fallback has nowhere to put a marker, so it keeps the box tooltip.
    """
    for f in fields:
        tip = f.tip if with_tips else ""
        if f.choices is not None:
            dlg.addField(f.label, initial=f.initial, choices=list(f.choices), tip=tip)
        else:
            dlg.addField(f.label, initial=f.initial, tip=tip)


def wizard_fields(base: Settings) -> list[Field]:
    """The configuration wizard's fields, pre-filled from ``base``.

    Pure, so the label/key/tip mapping can be tested without opening a dialog.
    The button bindings are included (see :func:`button_fields`); the enabled
    levels are not, because they are a grid rather than a row — see
    :func:`level_fields` and :func:`_LevelGridWidget`.
    """
    return [
        Field(
            "output_root",
            "output_root",
            base.paths.output_root,
            tip=(
                "Where sessions are written. The BIDS tree "
                "sourcedata/sub-<id>/ses-<nn>/ is created underneath it, one "
                "events file and one bk2 movie per gameplay attempt, plus a "
                "session log."
            ),
        ),
        Field(
            "max_duration",
            "max_duration (s)",
            base.task.max_duration_seconds,
            tip=(
                "How long one gameplay run lasts. The run ends at the first "
                "attempt to finish after this many seconds, so a run always "
                "overruns slightly rather than cutting a level in half."
            ),
        ),
        Field(
            "fixation_duration",
            "fixation (s)",
            base.task.fixation_duration_seconds,
            tip=(
                "Seconds of fixation cross shown before each attempt, "
                "including the first one of a run. It gives the recording a "
                "quiet baseline either side of every level."
            ),
        ),
        Field(
            "discovery_enabled",
            "discovery",
            base.task.discovery_enabled,
            tip=(
                "Run the discovery phase, which walks the enabled levels in "
                "order and stops at the first one the subject has not cleared "
                "yet. Progress is remembered in the subject's savestate."
            ),
        ),
        Field(
            "practice_enabled",
            "practice",
            base.task.practice_enabled,
            tip=(
                "Run the practice phase once discovery is complete: levels "
                "come from the subject's design file, drawn from a pool that "
                "is depleted before any level repeats."
            ),
        ),
        Field(
            "questionnaire_enabled",
            "questionnaire",
            base.task.questionnaire_enabled,
            tip=(
                "Ask the Likert flow-ratings questions at the end of every "
                "run. Turn it off for smoke tests where you only want to "
                "check that gameplay and markers work."
            ),
        ),
        Field(
            "fullscreen",
            "fullscreen",
            base.display.fullscreen,
            tip=(
                "Fill the screen and hide the cursor. Turn it off to pilot in "
                "a window next to your other applications."
            ),
        ),
        Field(
            "sync_mode",
            "sync_mode",
            base.sync.mode,
            ["none", "send", "wait"],
            tip=(
                "none: start the run immediately - behavioural piloting. "
                "wait: hold on a 'Waiting for the scanner' screen until the "
                "sync signal arrives. "
                "send: emit the sync signal at run start, e.g. to trigger the "
                "scanner yourself."
            ),
        ),
        Field(
            "sync_backend",
            "sync_backend",
            base.sync.backend,
            ["none", "serial", "parallel", "lsl", "key", "keyboard", "markers"],
            tip=(
                "Where the sync signal goes (send) or comes from (wait). "
                "none means the keyboard, which is the default and needs no "
                "hardware. markers reuses the event-marker backend below, so "
                "one serial port can carry both the start pulse and the "
                "markers."
            ),
        ),
        Field(
            "sync_port",
            "sync_port",
            base.sync.port or "",
            tip=(
                "Device for a serial or parallel sync backend, e.g. "
                "/dev/ttyUSB0 or 0x378. Leave blank for the others. A missing "
                "or dead port never stops a session: it warns and falls back."
            ),
        ),
        Field(
            "sync_signal",
            "sync_signal",
            ",".join(base.sync.signal),
            tip=(
                "What to send, or what to wait for. Comma-separate "
                "alternatives when one trigger key reports under several "
                "names, e.g. 5,percent - any of them starts the run, and only "
                "the first is ever sent. Most MR trigger boxes emit 5 or t."
            ),
        ),
        Field(
            "trigger_backend",
            "trigger_backend",
            base.triggers.backend,
            list(_VALID_BACKENDS),
            tip=(
                "Where per-event markers go. lsl is the recommended default "
                "for iEEG and opens a stream you can watch with 'python -m "
                "mario_task.monitor'; null sends nothing, for offline runs."
            ),
        ),
        Field(
            "trigger_port",
            "trigger_port",
            base.triggers.port or "",
            tip=(
                "Device for a serial or parallel marker backend, e.g. "
                "/dev/ttyACM0 or /dev/parport1. Leave blank for lsl and null."
            ),
        ),
        Field(
            "lsl_stream_name",
            "lsl_stream_name",
            base.triggers.lsl_stream_name,
            tip=(
                "The stream name LabRecorder will show in its list. Only used "
                "when the trigger backend is lsl; change it if two rigs "
                "publish onto the same network."
            ),
        ),
        Field(
            "trigger_every",
            "trigger_every",
            base.triggers.trigger_every,
            tip=(
                "Send one gameplay marker per N emulator frames. 1 is every "
                "frame (60/s); raise it to relieve an amplifier that cannot "
                "keep up. The bk2 movie always records every frame regardless."
            ),
        ),
        Field(
            "on_game_frame",
            "on_game_frame",
            base.triggers.on_game_frame,
            tip=(
                "Emit a marker per emulator frame, thinned by trigger_every. "
                "This is the bulk of the stream; turn it off to keep only the "
                "markers that segment the recording into runs and attempts."
            ),
        ),
        Field(
            "on_game_reset",
            "on_game_reset",
            base.triggers.on_game_reset,
            tip=(
                "Emit a marker when a level starts, i.e. at each emulator "
                "reset. This is what an analyst looks for to find the "
                "beginning of every attempt in the recording."
            ),
        ),
        Field(
            "on_non_game_flip",
            "on_non_game_flip",
            base.triggers.on_non_game_flip,
            tip=(
                "Emit a marker on every non-gameplay window flip: "
                "instructions, fixation and the questionnaire. A steady "
                "heartbeat that shows the display was still running."
            ),
        ),
        Field(
            "rom_file",
            "rom_file",
            base.paths.rom_file,
            tip=(
                "Path to the Super Mario Bros ROM (rom.nes). The ROM is not "
                "shipped with this task; point this at wherever you put your "
                "own copy."
            ),
        ),
        Field(
            "data_root",
            "data_root",
            base.paths.data_root,
            tip=(
                "The gym-retro integration directory holding the level "
                "savestates and scenario.json alongside the ROM. Normally the "
                "folder the ROM itself lives in."
            ),
        ),
        *button_fields(base),
    ]


def level_fields(enabled: Sequence[tuple[int, int]]) -> list[Field]:
    """One checkbox :class:`Field` per NES level, for the no-Qt fallback.

    The Qt path draws :func:`_LevelGridWidget` instead — 32 rows in a single
    column is unreadable, which is why the grid exists. Answers from these
    fields are collected by :func:`_collect_enabled_levels`.
    """
    on = set(enabled)
    return [
        Field(
            _level_field_key(world, level),
            f"Level {world}-{level}",
            (world, level) in on,
            tip=f"Include Level {world}-{level} in discovery and practice.",
        )
        for world, level in ALL_POSSIBLE_LEVELS
    ]


#: Hover text per NES button. What each one does in the game, so an operator
#: who has never played Mario can still bind a pad sensibly.
_BUTTON_TIPS: dict[str, str] = {
    "UP": (
        "Look up, and climb vines. Also moves the highlight up the "
        "questionnaire at the end of a run."
    ),
    "DOWN": (
        "Crouch, and enter pipes. Also moves the highlight down the "
        "questionnaire at the end of a run."
    ),
    "LEFT": (
        "Walk left. Also moves the answer left along the questionnaire's "
        "rating scale."
    ),
    "RIGHT": (
        "Walk right. Also moves the answer right along the questionnaire's "
        "rating scale."
    ),
    "A": (
        "Jump — the button the subject presses most. Also submits the "
        "questionnaire at the end of a run."
    ),
    "B": (
        "Run while held, and throw fireballs as Fire Mario. Usually the pad "
        "button next to A."
    ),
    "START": (
        "Pause the game. Left unbound by default, and best kept that way: a "
        "subject who can pause mid-level produces a recording nobody can "
        "segment."
    ),
    "SELECT": (
        "Does nothing during a level. Left unbound by default; bind it only "
        "if your pad sends something you would rather absorb here."
    ),
}


def button_fields(base: Settings) -> list[Field]:
    """One field per bindable NES button, pre-filled from ``base``.

    The value is the key name PsychoPy reports for that button, lowercased:
    ``up``/``down``/``left``/``right`` for the arrows, the character itself
    for letters and digits. Blank leaves the button unbound.
    """
    return [
        Field(
            _button_field_key(button),
            button,
            base.input.button_map.get(button, ""),
            tip=_BUTTON_TIPS[button],
        )
        for button in BINDABLE_BUTTONS
    ]


def _button_field_key(button: str) -> str:
    return f"button_{button.lower()}"


def collect_button_map(answers: Mapping[str, Any]) -> dict[str, str]:
    """Build ``input.button_map`` from the Controls tab's answers.

    Keys are lowercased and stripped, because ``mario_task.input`` reports
    pyglet symbols lowercased and an operator will type ``X`` as readily as
    ``x``. A button with no answer at all keeps whatever it had; a button
    answered blank is deliberately unbound.

    >>> collect_button_map({"button_a": " X "})["A"]
    'x'
    """
    out: dict[str, str] = {}
    for button in BINDABLE_BUTTONS:
        key = _button_field_key(button)
        if key in answers:
            out[button] = str(answers[key] or "").strip().lower()
    return out


@dataclass(frozen=True)
class Tab:
    """One page of the wizard: a title, a one-line blurb, and its fields."""

    title: str
    blurb: str
    keys: tuple[str, ...]


#: The wizard's tabs, in tab order. Every key in :func:`wizard_fields` must
#: appear in exactly one of them — ``test_every_wizard_field_lands_in_one_tab``
#: enforces that, so adding a field and forgetting to place it is a test
#: failure rather than a field that silently vanishes from the dialog.
#:
#: ``Levels`` names no fields: it carries the level grid, which is a widget
#: rather than a row (see :data:`_WIZARD_EXTRAS`).
WIZARD_TABS: tuple[Tab, ...] = (
    Tab(
        "Session",
        "What one session contains, and where it is written.",
        (
            "output_root",
            "max_duration",
            "fixation_duration",
            "discovery_enabled",
            "practice_enabled",
            "questionnaire_enabled",
        ),
    ),
    Tab(
        "Display",
        "The screen the participant looks at.",
        ("fullscreen",),
    ),
    Tab(
        "Controls",
        "Which keyboard key drives which NES button. The pad is read as a keyboard.",
        tuple(_button_field_key(b) for b in BINDABLE_BUTTONS),
    ),
    Tab(
        "Levels",
        "Which levels discovery walks through, and practice draws from.",
        (),
    ),
    Tab(
        "Scanner sync",
        "How the run and the scanner agree on t=0. Leave as-is outside an MRI.",
        ("sync_mode", "sync_backend", "sync_port", "sync_signal"),
    ),
    Tab(
        "Markers",
        "Per-event triggers for iEEG / EEG / MEG. Leave as-is for offline runs.",
        (
            "trigger_backend",
            "trigger_port",
            "lsl_stream_name",
            "trigger_every",
            "on_game_frame",
            "on_game_reset",
            "on_non_game_flip",
        ),
    ),
    Tab(
        "Game data",
        "Where the ROM and its gym-retro integration files live.",
        ("rom_file", "data_root"),
    ),
)

#: Tab title -> the tab's non-field widget. Only the level grid needs one.
_WIZARD_EXTRAS = ("Levels",)


def wizard_sections(fields: Sequence[Field]) -> list[tuple[Tab, list[Field]]]:
    """Group ``fields`` into :data:`WIZARD_TABS`, in tab order.

    Pure, so the grouping is testable without a display. Raises if a tab names
    a key that :func:`wizard_fields` does not provide.
    """
    by_key = {f.key: f for f in fields}
    sections = []
    for tab in WIZARD_TABS:
        missing = [k for k in tab.keys if k not in by_key]
        if missing:
            raise KeyError(f"tab {tab.title!r} names unknown field(s): {missing}")
        sections.append((tab, [by_key[k] for k in tab.keys]))
    return sections


#: The marker that carries a field's tooltip. A circled *i* rather than a "?",
#: because there is nothing to answer — it is information, not a quiz.
INFO_MARK = "ⓘ"

#: Roughly where a wrapped tooltip line breaks, in characters.
TOOLTIP_WIDTH = 58


def wrap_tooltip(text: str, width: int = TOOLTIP_WIDTH) -> str:
    """Turn a tip into HTML Qt will wrap into a readable block.

    Qt only word-wraps a tooltip it believes is rich text; a plain-text tip is
    laid out on a single line, which for a sentence like these runs off the
    edge of the screen. So we escape the text, break it ourselves — no guessing
    at Qt's CSS subset — and hand back something ``mightBeRichText`` says yes to.

    >>> wrap_tooltip("a b c", width=3)
    '<html>a b<br>c</html>'
    >>> wrap_tooltip("5 < 6")
    '<html>5 &lt; 6</html>'
    """
    words = escape(" ".join(text.split()), quote=False).split(" ")
    lines: list[str] = []
    for word in words:
        if lines and len(lines[-1]) + 1 + len(word) <= width:
            lines[-1] = f"{lines[-1]} {word}"
        else:
            lines.append(word)
    return "<html>" + "<br>".join(lines) + "</html>"


def _qt_widgets() -> Any:
    """The Qt binding PsychoPy's dialogs are built on, or ``None``.

    ``psychopy.gui`` may be backed by wx instead (or by nothing at all on a
    headless box), so every caller has to cope with ``None``.
    """
    try:
        from psychopy.gui import qtgui
    except Exception:  # noqa: BLE001 - no Qt, or no display
        return None
    return getattr(qtgui, "QtWidgets", None)


def _qt_enums() -> Any:
    """PsychoPy's ``Qt`` namespace (alignment, cursor shapes), or ``None``."""
    try:
        from psychopy.gui import qtgui
    except Exception:  # noqa: BLE001 - no Qt, or no display
        return None
    return getattr(qtgui, "Qt", None)


def _info_mark(qt: Any, parent: Any, tip: str) -> Any:
    """A small ``ⓘ`` whose hover text is ``tip``."""
    mark = qt.QLabel(INFO_MARK, parent=parent)
    mark.setToolTip(wrap_tooltip(tip))
    mark.setStyleSheet("color: palette(mid); font-weight: bold;")
    enums = _qt_enums()
    if enums is not None:
        try:
            mark.setCursor(enums.CursorShape.WhatsThisCursor)
            mark.setAlignment(enums.AlignmentFlag.AlignVCenter)
        except AttributeError:  # PyQt5 flattens the enums
            mark.setCursor(enums.WhatsThisCursor)
            mark.setAlignment(enums.AlignVCenter)
    return mark


def _add_fields_with_marks(dlg: Any, fields: Sequence[Field]) -> None:
    """Add fields to ``dlg``'s own grid, each with an ``ⓘ`` in a third column.

    For the subject picker: three fields, too few to be worth tabs, but they
    still deserve the same hover help as the wizard's. Degrades to PsychoPy's
    own box tooltips when there is no Qt to hang a marker on.
    """
    qt = _qt_widgets()
    if qt is None or not isinstance(getattr(dlg, "layout", None), qt.QGridLayout):
        _add_fields(dlg, fields)
        return
    for f in fields:
        row = dlg.irow
        _add_fields(dlg, [f], with_tips=False)
        if f.tip and dlg.irow == row + 1:
            dlg.layout.addWidget(_info_mark(qt, dlg, f.tip), row, 2)


def _add_tabbed_fields(
    dlg: Any,
    sections: Sequence[tuple[Tab, Sequence[Field]]],
    extras: Mapping[str, Any] | None = None,
) -> bool:
    """Lay ``sections`` out as tabs inside ``dlg``. ``False`` if it can't.

    ``psychopy.gui.Dlg`` is a single-column ``QGridLayout`` with no notion of
    pages, so we drop one level down to Qt: a ``QTabWidget`` spans both columns,
    and each field is added the normal way and then *moved* out of the dialog's
    grid into its page.

    Adding fields through ``dlg.addField`` rather than building the widgets
    ourselves is what keeps this safe: PsychoPy still owns the change signals,
    so ``dlg.show()`` returns the same label-keyed dict it always did, no
    matter where the widget physically sits.

    ``extras`` maps a tab title to a ``factory(parent) -> QWidget`` whose
    result is placed under that tab's fields — how the level grid gets onto
    its own page.
    """
    qt = _qt_widgets()
    if qt is None or not isinstance(getattr(dlg, "layout", None), qt.QGridLayout):
        return False

    extras = extras or {}
    tabs = qt.QTabWidget(parent=dlg)
    dlg.layout.addWidget(tabs, dlg.irow, 0, 1, 2)
    dlg.irow += 1

    for tab, fields in sections:
        page = qt.QWidget(parent=tabs)
        grid = qt.QGridLayout(page)
        grid.setContentsMargins(12, 12, 12, 12)
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(8)
        grid.setColumnMinimumWidth(1, 260)
        grid.setColumnStretch(1, 1)

        blurb = qt.QLabel(tab.blurb, parent=page)
        blurb.setWordWrap(True)
        grid.addWidget(blurb, 0, 0, 1, 3)

        for row, f in enumerate(fields, start=1):
            origin = dlg.irow
            # No PsychoPy tooltip: the whole box lighting up on hover is
            # noise when you are only passing over it on the way to another
            # field. The ⓘ in column 2 is the one thing that reacts.
            _add_fields(dlg, [f], with_tips=False)
            for col in (0, 1):
                item = dlg.layout.itemAtPosition(origin, col)
                widget = item.widget() if item is not None else None
                if widget is None:  # PsychoPy changed its layout under us
                    return False
                dlg.layout.removeWidget(widget)
                grid.addWidget(widget, row, col)
            if f.tip:
                grid.addWidget(_info_mark(qt, page, f.tip), row, 2)
            # addField consumed a row of the dialog's grid; give it back so the
            # OK/Cancel box lands directly under the tabs.
            dlg.irow = origin

        factory = extras.get(tab.title)
        if factory is not None:
            grid.addWidget(factory(page), len(fields) + 1, 0, 1, 3)

        grid.setRowStretch(len(fields) + 2, 1)
        tabs.addTab(page, tab.title)

    return True


def _polish(dlg: Any, *, ok_label: str) -> None:
    """Two cosmetic fixes ``psychopy.gui.Dlg`` does not do for us.

    ``validate()`` is what hides the "fields marked with an asterisk (*) are
    required" banner. Nothing here is required, but the banner is shown at
    construction and only taken down on the first edit — so call it once.

    ``labelButtonOK`` is accepted by ``Dlg.__init__`` and then never applied
    (the assignment is commented out upstream), so the button is renamed here.

    Both are best-effort: a wx-backed or otherwise unfamiliar dialog just keeps
    its defaults.
    """
    for tweak in (lambda: dlg.validate(), lambda: dlg.okBtn.setText(ok_label)):
        try:
            tweak()
        except Exception:  # noqa: BLE001 - cosmetic only, never worth a crash
            pass


def _add_inline_sections(
    dlg: Any,
    sections: Sequence[tuple[Tab, Sequence[Field]]],
    extras: Mapping[str, Sequence[Field]] | None = None,
) -> None:
    """Fallback for :func:`_add_tabbed_fields`: headed sections, one column.

    ``extras`` supplies the fields that stand in for a tab's widget when
    there is no Qt to draw it — the 32 level checkboxes, in practice.
    """
    extras = extras or {}
    for tab, fields in sections:
        dlg.addText(tab.title)
        _add_fields(dlg, fields)
        _add_fields(dlg, extras.get(tab.title, ()))


# ---------------------------------------------------------------------------
# Pure helpers (CI-testable; no psychopy import here)
# ---------------------------------------------------------------------------


_SUB_DIR_RX = re.compile(r"^sub-(?P<label>[A-Za-z0-9][A-Za-z0-9_-]*)$")

#: Sentinel entry in the subject dropdown.
NEW_SUBJECT = "<new subject>"


def list_existing_subjects(output_root: str | Path) -> list[str]:
    """Return subject labels found under ``output_root/sourcedata/sub-*/``.

    Sorted by mtime descending (most recent first), so the operator
    typically sees the subject they were just running at the top of the
    list. Labels are returned WITHOUT the ``sub-`` prefix.
    """
    sourcedata = Path(output_root) / "sourcedata"
    if not sourcedata.is_dir():
        return []
    entries: list[tuple[float, str]] = []
    for child in sourcedata.iterdir():
        if not child.is_dir():
            continue
        m = _SUB_DIR_RX.match(child.name)
        if m:
            entries.append((child.stat().st_mtime, m.group("label")))
    entries.sort(reverse=True)
    return [label for _, label in entries]


def subject_choices(output_root: str | os.PathLike[str]) -> list[str]:
    """Existing subjects plus a ``"<new subject>"`` sentinel, for a dropdown.

    Most-recently-run first, so the subject the operator is part way through
    is the one already selected when the dialog opens.
    """
    return [*list_existing_subjects(output_root), NEW_SUBJECT]


def format_subject_progress(output_root: str | Path, subject: str) -> str:
    """Return a one-line human summary of a subject's current state.

    Examples::

        "discovery: next Level3-1 (cleared so far: 6/22)"
        "practice: 64 / 1100 levels (run 3 / 50)"
        "fresh — no savestate yet"
    """
    sub_dir = Path(output_root) / "sourcedata" / f"sub-{subject}"
    discovery = sub_dir / f"sub-{subject}_phase-discovery_task-mario_savestate.json"
    stable = sub_dir / f"sub-{subject}_phase-stable_task-mario_savestate.json"

    if stable.exists():
        try:
            st = savestate.load(stable)
        except Exception:  # noqa: BLE001 — never crash the GUI from a corrupt file
            return "practice: (savestate unreadable)"
        idx = int(st.get("index", 0))
        # 50 reps × 22 levels = 1100 entries in the canonical design TSV.
        total = 50 * N_LEVELS_PER_RUN
        run_n = idx // N_LEVELS_PER_RUN + 1 if idx < total else 50
        return f"practice: {idx} / {total} levels (run {run_n} / 50)"

    if discovery.exists():
        try:
            st = savestate.load(discovery)
        except Exception:  # noqa: BLE001
            return "discovery: (savestate unreadable)"
        world, level = int(st.get("world", 1)), int(st.get("level", 1))
        # Count how many distinct levels have been cleared so far. This is
        # what `phases.advance_discovery_state` has walked past — same
        # ordering as discovery progression.
        from mario_task.design import ALL_LEVELS  # local import to avoid cycle

        cleared_count = 0
        for w, ell in ALL_LEVELS:
            if (w, ell) == (world, level):
                break
            cleared_count += 1
        else:
            cleared_count = len(ALL_LEVELS)  # discovery done (world≥9)
        return f"discovery: next Level{world}-{level} (cleared: {cleared_count}/{len(ALL_LEVELS)})"

    return "fresh — no savestate yet"


def infer_default_session(output_root: str | Path, subject: str) -> str:
    """Return ``infer_next_session`` result; kept as a thin re-export so the
    GUI module is the single import surface for cli.py."""
    return infer_next_session(output_root, subject)


def suggest_session(output_root: str | os.PathLike[str], subject: str) -> str:
    """Session number to pre-fill for ``subject`` (``"001"`` if they're new)."""
    return infer_next_session(output_root, subject) if subject else "001"


def subject_fields(output_root: str | os.PathLike[str]) -> list[Field]:
    """The subject picker's fields. Pure, so the tips are testable."""
    choices = subject_choices(output_root)
    return [
        Field(
            "picked",
            "existing subject",
            choices[0],
            choices,
            tip=(
                "Subjects already found under the output root, most recently "
                f"run first. Their progress is listed above. Pick {NEW_SUBJECT} "
                "to type a new one below."
            ),
        ),
        Field(
            "typed",
            "new subject id",
            "",
            tip=(
                "Type an id to start a new subject; anything here overrides "
                "the dropdown. A leading 'sub-' is stripped, so sub-01 and 01 "
                "are the same subject."
            ),
        ),
        Field(
            "session",
            "session",
            "",
            tip=(
                "Leave blank to continue with this subject's next unused "
                "session number. A bare number is zero-padded to three digits "
                "(1 becomes 001); a word such as 'pilot' is kept as typed."
            ),
        ),
    ]


def settings_from_wizard(
    base: Settings,
    answers: Mapping[str, Any],
    enabled_levels: Sequence[tuple[int, int]] | None = None,
) -> Settings:
    """Fold a flat dict of wizard answers back into nested :class:`Settings`.

    Kept separate from the dialog so the mapping can be tested without a
    display. Unknown or blank answers fall back to ``base``; ``enabled_levels``
    of ``None`` keeps the ones ``base`` already has.
    """

    def _blank_to_none(value):
        text = str(value).strip() if value is not None else ""
        return text or None

    def _split_keys(value):
        text = str(value).strip() if value is not None else ""
        return tuple(part.strip() for part in text.split(",") if part.strip())

    triggers = replace(
        base.triggers,
        backend=str(answers.get("trigger_backend") or base.triggers.backend),
        port=_blank_to_none(answers.get("trigger_port")),
        lsl_stream_name=str(
            answers.get("lsl_stream_name") or base.triggers.lsl_stream_name
        ),
        trigger_every=int(answers.get("trigger_every", base.triggers.trigger_every)),
        on_game_frame=bool(answers.get("on_game_frame", base.triggers.on_game_frame)),
        on_game_reset=bool(answers.get("on_game_reset", base.triggers.on_game_reset)),
        on_non_game_flip=bool(
            answers.get("on_non_game_flip", base.triggers.on_non_game_flip)
        ),
    )
    sync = replace(
        base.sync,
        mode=str(answers.get("sync_mode") or base.sync.mode),
        backend=str(answers.get("sync_backend") or base.sync.backend),
        port=_blank_to_none(answers.get("sync_port")),
        signal=_split_keys(answers.get("sync_signal")) or base.sync.signal,
    )
    task = replace(
        base.task,
        max_duration_seconds=int(
            answers.get("max_duration", base.task.max_duration_seconds)
        ),
        fixation_duration_seconds=float(
            answers.get("fixation_duration", base.task.fixation_duration_seconds)
        ),
        discovery_enabled=bool(
            answers.get("discovery_enabled", base.task.discovery_enabled)
        ),
        practice_enabled=bool(
            answers.get("practice_enabled", base.task.practice_enabled)
        ),
        questionnaire_enabled=bool(
            answers.get("questionnaire_enabled", base.task.questionnaire_enabled)
        ),
        enabled_levels=(
            tuple(enabled_levels) if enabled_levels is not None
            else base.task.enabled_levels
        ),
    )
    display = replace(
        base.display,
        fullscreen=bool(answers.get("fullscreen", base.display.fullscreen)),
    )
    # A button the dialog did not ask about keeps its current binding; one it
    # asked about and got a blank for is deliberately unbound.
    inputs = replace(
        base.input,
        button_map={**base.input.button_map, **collect_button_map(answers)},
    )
    paths = replace(
        base.paths,
        output_root=str(answers.get("output_root") or base.paths.output_root),
        rom_file=str(answers.get("rom_file") or base.paths.rom_file),
        data_root=str(answers.get("data_root") or base.paths.data_root),
    )
    return replace(
        base,
        triggers=triggers,
        sync=sync,
        input=inputs,
        task=task,
        display=display,
        paths=paths,
    )


# Helpers for the level-grid checkboxes. Kept pure so they can be unit-tested.


def _level_field_key(world: int, level: int) -> str:
    return f"level_{world}_{level}"


def _collect_enabled_levels(
    data: Mapping[str, Any],
    possible_levels: tuple[tuple[int, int], ...] = ALL_POSSIBLE_LEVELS,
) -> tuple[tuple[int, int], ...]:
    """Build the ``enabled_levels`` tuple from a flat ``{key: bool}`` map.

    Used by the no-Qt fallback, where the grid is 32 ordinary checkbox
    fields (:func:`level_fields`). The Qt path reads
    :class:`_LevelGridWidget` directly instead.

    Preserves the canonical world/level ordering (1-1, 1-2, ..., 8-4) so
    discovery walks levels in a predictable order regardless of which
    boxes are ticked.
    """
    enabled: list[tuple[int, int]] = []
    for world, level in possible_levels:
        if data.get(_level_field_key(world, level), False):
            enabled.append((world, level))
    return tuple(enabled)


# ---------------------------------------------------------------------------
# 8×4 level-grid widget (Qt — used by the config wizard)
# ---------------------------------------------------------------------------
# Defined inside the gui module but uses a lazy Qt import so the rest of
# the module (and tests) can import it on a headless box where pyqt6 is
# installed but never instantiated.


def _LevelGridWidget(default_enabled, parent=None):
    """Return a QWidget containing an 8×4 checkbox grid for the 32 NES levels.

    Wraps the Qt class definition in a factory function so the heavy
    PyQt6 import only happens when this is actually called (i.e., when
    the wizard opens). Tests that import :mod:`mario_task.gui` without
    a display don't pay the Qt cost.

    The returned widget exposes ``.get_enabled() -> tuple[tuple[int, int], ...]``
    which collects the ticked boxes in canonical (1-1, 1-2, ..., 8-4) order.
    """
    from PyQt6 import QtCore, QtWidgets

    class _Impl(QtWidgets.QWidget):
        def __init__(self, default_enabled: set, parent=None) -> None:
            super().__init__(parent)
            layout = QtWidgets.QGridLayout(self)
            layout.setContentsMargins(8, 4, 8, 4)
            layout.setHorizontalSpacing(28)
            layout.setVerticalSpacing(2)

            # Column headers: blank, "Level 1", ..., "Level 4".
            for col, level in enumerate(range(1, 5), start=1):
                hdr = QtWidgets.QLabel(f"Level {level}")
                hdr.setStyleSheet("font-weight: bold;")
                hdr.setAlignment(
                    QtCore.Qt.AlignmentFlag.AlignHCenter
                    | QtCore.Qt.AlignmentFlag.AlignVCenter
                )
                layout.addWidget(hdr, 0, col)

            self._checkboxes: dict[tuple[int, int], QtWidgets.QCheckBox] = {}
            for row, world in enumerate(range(1, WORLDS + 1), start=1):
                # Row label.
                lbl = QtWidgets.QLabel(f"World {world}")
                lbl.setStyleSheet("font-weight: bold;")
                layout.addWidget(lbl, row, 0)
                # 4 checkboxes, one per level in this world.
                for col, level in enumerate(range(1, 5), start=1):
                    cb = QtWidgets.QCheckBox()
                    cb.setChecked((world, level) in default_enabled)
                    cb.setToolTip(f"Level {world}-{level}")
                    # Centre the checkbox in its column.
                    container = QtWidgets.QWidget()
                    box = QtWidgets.QHBoxLayout(container)
                    box.setContentsMargins(0, 0, 0, 0)
                    box.addStretch()
                    box.addWidget(cb)
                    box.addStretch()
                    layout.addWidget(container, row, col)
                    self._checkboxes[(world, level)] = cb

        def get_enabled(self) -> tuple[tuple[int, int], ...]:
            """Return ticked levels in canonical (1-1, 1-2, ..., 8-4) order."""
            return tuple(
                (w, l) for (w, l), cb in self._checkboxes.items() if cb.isChecked()
            )

    return _Impl(set(default_enabled), parent)


# ---------------------------------------------------------------------------
# Dialogs
# ---------------------------------------------------------------------------


_WIZARD_TITLE = "mario_task - setup"


def run_config_wizard(config_path: str | os.PathLike[str]) -> Settings | None:
    """Ask for the rig's settings and write ``config.json``.

    Returns the saved settings, or ``None`` if the operator cancelled.
    Re-opening the wizard on an existing config pre-fills it with the current
    values, so it doubles as an editor — ``--reconfigure`` is a way to change
    one port without retyping the rest.
    """
    from psychopy import gui as psygui

    base = settings_mod.default_settings()
    p = Path(config_path)
    if p.exists():
        try:
            base = settings_mod.load_from_file(p)
        except (OSError, ValueError) as exc:
            log.warning("Could not read %s (%s); starting from defaults.", p, exc)

    fields = wizard_fields(base)
    sections = wizard_sections(fields)

    grid: Any = None

    def _make_grid(parent):
        nonlocal grid
        grid = _LevelGridWidget(base.task.enabled_levels, parent)
        return grid

    dlg = psygui.Dlg(title=_WIZARD_TITLE)
    tabbed = _add_tabbed_fields(dlg, sections, extras={"Levels": _make_grid})
    level_answer_fields: list[Field] = []
    if not tabbed:
        # No Qt (or PsychoPy moved its layout around): fall back to one long
        # column with headings, the level grid becoming 32 plain checkboxes.
        log.debug("Tabbed layout unavailable; falling back to inline sections.")
        grid = None
        level_answer_fields = level_fields(base.task.enabled_levels)
        dlg = psygui.Dlg(title=_WIZARD_TITLE)
        _add_inline_sections(dlg, sections, extras={"Levels": level_answer_fields})
    _polish(dlg, ok_label="Save")

    returned = dlg.show()
    if returned is None or not getattr(dlg, "OK", True):
        return None

    answers = read_dialog_values([*fields, *level_answer_fields], returned)
    if grid is not None:
        enabled_levels = grid.get_enabled()
    else:
        enabled_levels = _collect_enabled_levels(answers)

    if not enabled_levels:
        # settings._validate would catch this too, but the operator gets a
        # dialog they can correct rather than a traceback.
        psygui.warnDlg(
            prompt=(
                "No levels are enabled. Tick at least one box on the Levels "
                "tab — discovery and practice both draw from it."
            ),
            title="No levels enabled",
        )
        return run_config_wizard(p)

    try:
        settings = settings_from_wizard(base, answers, enabled_levels)
        settings_mod.save(p, settings)
    except ValueError as exc:
        # Show the validation message and reopen, rather than exiting with a
        # traceback at someone who typed a port wrong.
        psygui.warnDlg(prompt=f"{exc}\n\nPlease correct the settings.", title="Invalid settings")
        return run_config_wizard(p)

    log.info("Wrote %s", p)
    return settings


def pick_subject(output_root: str | os.PathLike[str]) -> tuple[str, str] | None:
    """Ask which subject / session to run. Returns ``(subject, session)``.

    Returns ``None`` if the operator cancelled. Existing subjects are listed
    with their progress above the fields, and offered in a dropdown that
    pre-fills their next session number; ``<new subject>`` lets you type one.
    """
    from psychopy import gui as psygui

    existing = list_existing_subjects(output_root)
    if existing:
        info = "\n".join(
            ["Existing subjects (newest first):"]
            + [
                f"  sub-{label}: {format_subject_progress(output_root, label)}"
                for label in existing
            ]
        )
    else:
        info = "(No existing subjects yet — pick <new subject> and type a label.)"

    fields = subject_fields(output_root)

    dlg = psygui.Dlg(title="mario_task - session start")
    dlg.addText(info)
    _add_fields_with_marks(dlg, fields)
    _polish(dlg, ok_label="Start session")
    returned = dlg.show()
    if returned is None or not getattr(dlg, "OK", True):
        return None

    answers = read_dialog_values(fields, returned)
    picked = answers.get("picked", "")
    typed = answers.get("typed", "")
    session = answers.get("session", "")

    subject = normalize_subject(str(typed).strip())
    if not subject:
        if picked == NEW_SUBJECT:
            psygui.warnDlg(prompt="Please type a subject id.", title="No subject")
            return pick_subject(output_root)
        subject = str(picked)

    session = str(session).strip()
    session = normalize_session(session) if session else suggest_session(output_root, subject)
    return subject, session
