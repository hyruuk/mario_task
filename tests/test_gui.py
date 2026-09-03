"""Tests for the display-free helpers behind the wizard and subject picker.

The actual Dlg rendering needs a display, so the wizard + picker functions
are exercised by the integration smoke test; everything here is the pure
part — the field/label/tip table, the tab grouping, the answer-to-Settings
mapping, and the subject-progress readout.

This module imports ``mario_task.gui`` at the top; that module's lazy
import of ``psychopy`` keeps it safe in CI (the actual psychopy import
only happens when the wizard / picker is called).
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from mario_task import gui, savestate
from mario_task import settings as S
from mario_task.design import ALL_LEVELS


# ---------------------------------------------------------------------------
# list_existing_subjects
# ---------------------------------------------------------------------------


def test_list_existing_subjects_empty_when_no_sourcedata(tmp_path: Path) -> None:
    assert gui.list_existing_subjects(tmp_path) == []


def test_list_existing_subjects_finds_sub_dirs(tmp_path: Path) -> None:
    for label in ("01", "02", "pilot1"):
        (tmp_path / "sourcedata" / f"sub-{label}").mkdir(parents=True)
    out = gui.list_existing_subjects(tmp_path)
    assert sorted(out) == ["01", "02", "pilot1"]


def test_list_existing_subjects_orders_by_mtime_desc(tmp_path: Path) -> None:
    src = tmp_path / "sourcedata"
    src.mkdir()
    # Create three subject dirs with increasing mtimes.
    for i, label in enumerate(("01", "02", "03")):
        d = src / f"sub-{label}"
        d.mkdir()
        # Bump mtime so ordering is deterministic.
        atime = mtime = time.time() + i
        os_path = str(d)
        import os
        os.utime(os_path, (atime, mtime))
    out = gui.list_existing_subjects(tmp_path)
    # Newest (highest mtime) first.
    assert out[0] == "03"
    assert out[-1] == "01"


def test_list_existing_subjects_ignores_non_sub_dirs(tmp_path: Path) -> None:
    src = tmp_path / "sourcedata"
    src.mkdir()
    (src / "sub-01").mkdir()
    (src / "garbage").mkdir()
    (src / "sub-bad name").mkdir()  # space → rejected by regex
    out = gui.list_existing_subjects(tmp_path)
    assert out == ["01"]


# ---------------------------------------------------------------------------
# format_subject_progress
# ---------------------------------------------------------------------------


def test_format_subject_progress_fresh_subject(tmp_path: Path) -> None:
    (tmp_path / "sourcedata" / "sub-01").mkdir(parents=True)
    assert "fresh" in gui.format_subject_progress(tmp_path, "01")


def test_format_subject_progress_mid_discovery(tmp_path: Path) -> None:
    sub_dir = tmp_path / "sourcedata" / "sub-01"
    sub_dir.mkdir(parents=True)
    # Pretend the subject has just cleared Level1-3 and is heading to 2-1.
    savestate.save(
        sub_dir / "sub-01_phase-discovery_task-mario_savestate.json",
        {"world": 2, "level": 1},
    )
    text = gui.format_subject_progress(tmp_path, "01")
    assert text.startswith("discovery:")
    assert "Level2-1" in text
    # 3 levels cleared (Level1-1, 1-2, 1-3) of 22 total.
    assert "3/22" in text


def test_format_subject_progress_practice(tmp_path: Path) -> None:
    sub_dir = tmp_path / "sourcedata" / "sub-01"
    sub_dir.mkdir(parents=True)
    savestate.save(
        sub_dir / "sub-01_phase-stable_task-mario_savestate.json",
        {"index": 44},
    )
    text = gui.format_subject_progress(tmp_path, "01")
    assert text.startswith("practice:")
    assert "44" in text
    assert "1100" in text  # 50 reps × 22 levels


def test_format_subject_progress_discovery_done(tmp_path: Path) -> None:
    """world≥9 means discovery complete (and a stable savestate should
    have been written too, but if for some reason only discovery exists
    with world=9 we still report it usefully)."""
    sub_dir = tmp_path / "sourcedata" / "sub-01"
    sub_dir.mkdir(parents=True)
    savestate.save(
        sub_dir / "sub-01_phase-discovery_task-mario_savestate.json",
        {"world": 9, "level": 1},
    )
    text = gui.format_subject_progress(tmp_path, "01")
    assert f"{len(ALL_LEVELS)}/22" in text


def test_format_subject_progress_handles_corrupt_savestate(tmp_path: Path) -> None:
    sub_dir = tmp_path / "sourcedata" / "sub-01"
    sub_dir.mkdir(parents=True)
    (sub_dir / "sub-01_phase-discovery_task-mario_savestate.json").write_text("{not json")
    text = gui.format_subject_progress(tmp_path, "01")
    assert "unreadable" in text


# ---------------------------------------------------------------------------
# infer_default_session is a thin re-export — just spot-check it works
# ---------------------------------------------------------------------------


def test_infer_default_session_starts_at_001(tmp_path: Path) -> None:
    assert gui.infer_default_session(tmp_path, "01") == "001"


# ---------------------------------------------------------------------------
# Level-grid helpers in run_config_wizard
# ---------------------------------------------------------------------------


def test_level_field_key_format() -> None:
    assert gui._level_field_key(1, 1) == "level_1_1"
    assert gui._level_field_key(8, 4) == "level_8_4"


def test_collect_enabled_levels_default_set() -> None:
    """Simulate the wizard returning the default 22 levels checked."""
    from mario_task.design import DEFAULT_ENABLED_LEVELS

    data = {
        gui._level_field_key(w, l): ((w, l) in set(DEFAULT_ENABLED_LEVELS))
        for w in range(1, 9)
        for l in range(1, 5)
    }
    enabled = gui._collect_enabled_levels(data)
    assert enabled == tuple(DEFAULT_ENABLED_LEVELS)


def test_collect_enabled_levels_preserves_canonical_ordering() -> None:
    """No matter which boxes are ticked, the result follows (1-1, 1-2, ..., 8-4) order."""
    data = {gui._level_field_key(w, l): False for w in range(1, 9) for l in range(1, 5)}
    # Tick a few out of order; verify they come out sorted.
    data[gui._level_field_key(5, 2)] = True
    data[gui._level_field_key(1, 1)] = True
    data[gui._level_field_key(8, 4)] = True
    enabled = gui._collect_enabled_levels(data)
    assert enabled == ((1, 1), (5, 2), (8, 4))


def test_collect_enabled_levels_empty_when_all_unchecked() -> None:
    data = {gui._level_field_key(w, l): False for w in range(1, 9) for l in range(1, 5)}
    assert gui._collect_enabled_levels(data) == ()


# ---------------------------------------------------------------------------
# Reading dialog results
#
# psychopy's Dlg.show() returns an IndexDict keyed by each field's *label*,
# so zipping our key list against it pairs every key with a label string —
# which surfaces as "could not convert string to float: 'max_duration (s)'".
# Reading by label handles that and the legacy positional shape both.
# ---------------------------------------------------------------------------


def _labelled_result(fields, **overrides):
    """Build what PsychoPy returns: a dict keyed by field label."""
    result = {f.label: f.initial for f in fields}
    by_key = {f.key: f.label for f in fields}
    for key, value in overrides.items():
        result[by_key[key]] = value
    return result


def test_reads_a_dict_keyed_by_label() -> None:
    fields = gui.wizard_fields(S.default_settings())
    answers = gui.read_dialog_values(fields, _labelled_result(fields))
    assert set(answers) == {f.key for f in fields}
    # The give-away symptom: a value equal to its own label.
    assert answers["max_duration"] != "max_duration (s)"
    assert answers["max_duration"] == S.default_settings().task.max_duration_seconds


def test_reads_a_legacy_positional_list() -> None:
    fields = gui.wizard_fields(S.default_settings())
    answers = gui.read_dialog_values(fields, [f.initial for f in fields])
    assert answers["max_duration"] == S.default_settings().task.max_duration_seconds


def test_cancelled_dialog_yields_no_answers() -> None:
    fields = gui.wizard_fields(S.default_settings())
    assert gui.read_dialog_values(fields, None) == {}


def test_labels_and_keys_are_distinct_but_complete() -> None:
    fields = gui.wizard_fields(S.default_settings())
    assert len({f.key for f in fields}) == len(fields)
    assert len({f.label for f in fields}) == len(fields)
    assert any(f.key != f.label for f in fields)


def test_labels_are_names_not_explanations() -> None:
    """The tip explains; the label just names the setting.

    Keeping labels short is only safe because every field has an ``ⓘ`` — so
    this and ``test_every_wizard_field_has_a_tip`` belong together.
    """
    for f in gui.wizard_fields(S.default_settings()):
        assert len(f.label) <= 16, f"{f.key} label reads like a sentence: {f.label!r}"


def test_wizard_fields_prefill_from_the_given_settings() -> None:
    """Re-opening the wizard must show the rig's current values, not defaults."""
    import dataclasses

    base = S.default_settings()
    base = dataclasses.replace(
        base,
        sync=dataclasses.replace(base.sync, mode="wait", port="/dev/ttyUSB0"),
        task=dataclasses.replace(base.task, max_duration_seconds=42),
    )
    fields = {f.key: f.initial for f in gui.wizard_fields(base)}
    assert fields["max_duration"] == 42
    assert fields["sync_mode"] == "wait"
    assert fields["sync_port"] == "/dev/ttyUSB0"


def test_a_missing_port_prefills_as_blank_not_none() -> None:
    fields = {f.key: f.initial for f in gui.wizard_fields(S.default_settings())}
    assert fields["sync_port"] == ""
    assert fields["trigger_port"] == ""


# ---------------------------------------------------------------------------
# Folding answers back into Settings
# ---------------------------------------------------------------------------


def test_wizard_answers_map_into_nested_settings() -> None:
    base = S.default_settings()
    result = gui.settings_from_wizard(
        base,
        {
            "output_root": "/data/out",
            "max_duration": 300,
            "fixation_duration": 1.5,
            "discovery_enabled": False,
            "practice_enabled": True,
            "questionnaire_enabled": False,
            "fullscreen": False,
            "sync_mode": "send",
            "sync_backend": "serial",
            "sync_port": "/dev/ttyUSB0",
            "sync_signal": "5, percent",
            "trigger_backend": "serial",
            "trigger_port": "",
            "lsl_stream_name": "rig2",
            "trigger_every": 4,
            "on_game_frame": False,
            "on_game_reset": True,
            "on_non_game_flip": False,
            "rom_file": "/roms/mario.nes",
            "data_root": "/roms",
        },
        enabled_levels=((1, 1), (1, 2)),
    )
    assert result.paths.output_root == "/data/out"
    assert result.task.max_duration_seconds == 300
    assert result.task.fixation_duration_seconds == 1.5
    assert result.task.discovery_enabled is False
    assert result.task.questionnaire_enabled is False
    assert result.task.enabled_levels == ((1, 1), (1, 2))
    assert result.display.fullscreen is False
    assert result.sync.mode == "send"
    assert result.sync.backend == "serial"
    assert result.sync.port == "/dev/ttyUSB0"
    # A comma-separated answer becomes the tuple of alternatives.
    assert result.sync.signal == ("5", "percent")
    assert result.triggers.trigger_every == 4
    assert result.triggers.on_game_frame is False
    assert result.triggers.on_non_game_flip is False
    assert result.paths.rom_file == "/roms/mario.nes"
    # A blank port must become None, not the empty string.
    assert result.triggers.port is None


def test_wizard_result_validates() -> None:
    base = S.default_settings()
    fields = gui.wizard_fields(base)
    answers = gui.read_dialog_values(fields, _labelled_result(fields, sync_mode="wait"))
    result = gui.settings_from_wizard(base, answers, base.task.enabled_levels)
    S._validate(result)
    assert result.sync.mode == "wait"


def test_blank_answers_fall_back_to_the_base() -> None:
    base = S.default_settings()
    assert gui.settings_from_wizard(base, {}) == base


def test_unticking_every_level_is_caught_before_save() -> None:
    """The wizard re-opens on this rather than saving an unrunnable config."""
    base = S.default_settings()
    result = gui.settings_from_wizard(base, {}, enabled_levels=())
    with pytest.raises(ValueError, match="enabled_levels"):
        S._validate(result)


# ---------------------------------------------------------------------------
# Tooltips and tabs
# ---------------------------------------------------------------------------


def test_every_wizard_field_has_a_tip() -> None:
    """A field with no hover text is a field nobody can set confidently."""
    missing = [f.key for f in gui.wizard_fields(S.default_settings()) if not f.tip.strip()]
    assert missing == []


def test_tips_are_sentences_not_labels_again() -> None:
    # A tip that just restates the label helps nobody; ask for real prose.
    for f in gui.wizard_fields(S.default_settings()):
        assert len(f.tip) > 40, f"{f.key} tip is too short to explain anything"
        assert f.tip.strip().endswith("."), f"{f.key} tip is not a sentence"


def test_every_wizard_field_lands_in_exactly_one_tab() -> None:
    fields = gui.wizard_fields(S.default_settings())
    placed = [key for tab in gui.WIZARD_TABS for key in tab.keys]
    assert sorted(placed) == sorted(f.key for f in fields)
    assert len(placed) == len(set(placed))


def test_wizard_sections_group_fields_in_tab_order() -> None:
    fields = gui.wizard_fields(S.default_settings())
    sections = gui.wizard_sections(fields)

    assert [tab.title for tab, _ in sections] == [t.title for t in gui.WIZARD_TABS]
    by_tab = dict(sections)
    sync_tab = next(t for t in gui.WIZARD_TABS if t.title == "Scanner sync")
    assert [f.key for f in by_tab[sync_tab]] == list(sync_tab.keys)


def test_the_levels_tab_carries_a_widget_not_fields() -> None:
    """32 checkboxes are a grid, not 32 rows — the tab holds no Field keys."""
    levels = next(t for t in gui.WIZARD_TABS if t.title == "Levels")
    assert levels.keys == ()
    assert levels.title in gui._WIZARD_EXTRAS


def test_wizard_sections_reject_a_tab_naming_an_unknown_field(monkeypatch) -> None:
    monkeypatch.setattr(gui, "WIZARD_TABS", (gui.Tab("Bogus", "blurb", ("no_such_field",)),))
    with pytest.raises(KeyError, match="no_such_field"):
        gui.wizard_sections(gui.wizard_fields(S.default_settings()))


def test_every_tab_has_a_blurb() -> None:
    for tab in gui.WIZARD_TABS:
        assert tab.blurb.strip()
        assert tab.title.strip()


def test_subject_picker_fields_all_carry_tips(tmp_path: Path) -> None:
    fields = gui.subject_fields(tmp_path)
    assert [f.key for f in fields] == ["picked", "typed", "session"]
    assert all(f.tip.strip() for f in fields)
    # The dropdown tip must name the sentinel it is telling you to pick.
    assert gui.NEW_SUBJECT in fields[0].tip


def test_subject_choices_always_offers_a_new_subject(tmp_path: Path) -> None:
    assert gui.subject_choices(tmp_path) == [gui.NEW_SUBJECT]
    src = tmp_path / "sourcedata"
    (src / "sub-01").mkdir(parents=True)
    assert gui.subject_choices(tmp_path) == ["01", gui.NEW_SUBJECT]


def test_suggest_session_counts_up(tmp_path: Path) -> None:
    assert gui.suggest_session(tmp_path, "01") == "001"
    assert gui.suggest_session(tmp_path, "") == "001"
    (tmp_path / "sourcedata" / "sub-01" / "ses-001").mkdir(parents=True)
    assert gui.suggest_session(tmp_path, "01") == "002"


def test_tabbed_layout_declines_when_there_is_no_qt(monkeypatch) -> None:
    """On a wx (or headless) PsychoPy the wizard must not half-build a dialog."""
    monkeypatch.setattr(gui, "_qt_widgets", lambda: None)
    assert gui._add_tabbed_fields(object(), []) is False


def test_inline_fallback_adds_every_field_under_its_heading() -> None:
    class FakeDlg:
        def __init__(self):
            self.calls = []

        def addText(self, text):
            self.calls.append(("text", text))

        def addField(self, label, initial="", choices=None, tip=""):
            self.calls.append(("field", label, tip))

    dlg = FakeDlg()
    base = S.default_settings()
    fields = gui.wizard_fields(base)
    levels = gui.level_fields(base.task.enabled_levels)
    gui._add_inline_sections(dlg, gui.wizard_sections(fields), extras={"Levels": levels})

    headings = [c[1] for c in dlg.calls if c[0] == "text"]
    labels = [c[1] for c in dlg.calls if c[0] == "field"]
    assert headings == [t.title for t in gui.WIZARD_TABS]
    # The 32 level checkboxes stand in for the grid Qt would have drawn.
    assert sorted(labels) == sorted([f.label for f in fields] + [f.label for f in levels])
    # The tips survive the fallback too.
    assert all(c[2] for c in dlg.calls if c[0] == "field")


def test_level_fields_round_trip_through_the_inline_fallback() -> None:
    base = S.default_settings()
    fields = gui.level_fields(base.task.enabled_levels)
    answers = gui.read_dialog_values(fields, _labelled_result(fields))
    assert gui._collect_enabled_levels(answers) == base.task.enabled_levels


# ---------------------------------------------------------------------------
# The info marker
# ---------------------------------------------------------------------------


def test_tooltips_are_wrapped_into_readable_lines() -> None:
    """Qt lays a plain-text tooltip out on one line; ours must not be one."""
    long_tip = max((f.tip for f in gui.wizard_fields(S.default_settings())), key=len)
    html = gui.wrap_tooltip(long_tip)
    assert html.startswith("<html>") and html.endswith("</html>")
    lines = html[len("<html>") : -len("</html>")].split("<br>")
    assert len(lines) > 1
    assert all(len(line) <= gui.TOOLTIP_WIDTH for line in lines)


def test_wrapping_keeps_every_word_and_escapes_markup() -> None:
    tip = 'run with 5 < 6 & "quotes"'
    html = gui.wrap_tooltip(tip, width=8)
    assert "&lt;" in html and "&amp;" in html
    text = html[len("<html>") : -len("</html>")].replace("<br>", " ")
    assert text.split() == ["run", "with", "5", "&lt;", "6", "&amp;", '"quotes"']


def test_wrapping_does_not_split_a_word_longer_than_the_width() -> None:
    assert gui.wrap_tooltip("/dev/ttyUSB0", width=4) == "<html>/dev/ttyUSB0</html>"


def test_every_tip_survives_wrapping() -> None:
    for f in gui.wizard_fields(S.default_settings()):
        html = gui.wrap_tooltip(f.tip)
        text = html[len("<html>") : -len("</html>")].replace("<br>", " ")
        assert text.split() == gui.wrap_tooltip(f.tip, width=10_000)[6:-7].split()


def test_the_info_mark_is_a_single_character() -> None:
    # It sits in its own narrow grid column; anything longer would push the
    # inputs around.
    assert len(gui.INFO_MARK) == 1


# ---------------------------------------------------------------------------
# The Controls tab
# ---------------------------------------------------------------------------


def test_every_bindable_button_gets_a_field_on_the_controls_tab() -> None:
    from mario_task.settings import BINDABLE_BUTTONS

    controls = next(t for t in gui.WIZARD_TABS if t.title == "Controls")
    fields = gui.button_fields(S.default_settings())
    assert [f.label for f in fields] == list(BINDABLE_BUTTONS)
    assert tuple(f.key for f in fields) == controls.keys


def test_button_fields_prefill_from_the_current_binding() -> None:
    import dataclasses

    from mario_task.settings import DEFAULT_BUTTON_MAP, InputSettings

    base = S.default_settings()
    base = dataclasses.replace(
        base, input=InputSettings(button_map={**DEFAULT_BUTTON_MAP, "A": "space"})
    )
    fields = {f.label: f.initial for f in gui.button_fields(base)}
    assert fields["A"] == "space"
    assert fields["UP"] == "up"
    # An unbound button shows as blank, not as the "_" the key_set uses.
    assert fields["START"] == ""


def test_collect_button_map_normalises_what_the_operator_typed() -> None:
    """Operators type 'X'; pyglet reports 'x'. Meet in the middle."""
    answers = {gui._button_field_key("A"): "  SPACE  "}
    assert gui.collect_button_map(answers) == {"A": "space"}


def test_collect_button_map_ignores_buttons_the_dialog_did_not_ask_about() -> None:
    assert gui.collect_button_map({}) == {}


def test_an_answered_blank_unbinds_the_button() -> None:
    """Blank is a real answer — it is how START and SELECT stay unbound."""
    assert gui.collect_button_map({gui._button_field_key("START"): ""}) == {"START": ""}


def test_remapping_a_button_flows_into_the_key_set() -> None:
    base = S.default_settings()
    fields = gui.wizard_fields(base)
    # The operator swaps jump and run, and types them in upper case.
    returned = _labelled_result(fields)
    returned["A"] = "B"
    returned["B"] = "A"
    answers = gui.read_dialog_values(fields, returned)
    result = gui.settings_from_wizard(base, answers, base.task.enabled_levels)
    S._validate(result)
    assert result.input.button_map["A"] == "b"
    assert result.input.key_set()[8] == "b"   # A sits at index 8
    assert result.input.key_set()[0] == "a"   # B sits at index 0


def test_a_wizard_that_double_binds_a_key_fails_validation() -> None:
    """The wizard shows the message and re-opens rather than saving this."""
    base = S.default_settings()
    fields = gui.wizard_fields(base)
    returned = _labelled_result(fields)
    returned["A"] = "z"  # already bound to B
    answers = gui.read_dialog_values(fields, returned)
    result = gui.settings_from_wizard(base, answers, base.task.enabled_levels)
    with pytest.raises(ValueError, match="one keypress would press both"):
        S._validate(result)


def test_untouched_buttons_keep_their_binding() -> None:
    import dataclasses

    from mario_task.settings import DEFAULT_BUTTON_MAP, InputSettings

    base = dataclasses.replace(
        S.default_settings(),
        input=InputSettings(button_map={**DEFAULT_BUTTON_MAP, "SELECT": "tab"}),
    )
    # An answers dict that mentions no buttons at all.
    result = gui.settings_from_wizard(base, {"fullscreen": False})
    assert result.input.button_map["SELECT"] == "tab"
