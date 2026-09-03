"""Tests for the settings module: schema, validation, override hierarchy, atomic save."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import import_or_skip

from mario_task import settings
from mario_task.settings import (
    DisplaySettings,
    PathSettings,
    Settings,
    SyncSettings,
    TaskSettings,
    TriggerSettings,
    default_settings,
    load,
    load_from_file,
    save,
)

# ---------------------------------------------------------------------------
# Defaults & roundtrip
# ---------------------------------------------------------------------------


def test_default_settings_are_sane() -> None:
    s = default_settings()
    assert s.triggers.backend == "lsl"
    assert s.triggers.port is None
    assert s.task.max_duration_seconds == 600
    assert s.task.discovery_enabled is True
    assert s.task.practice_enabled is True
    assert len(s.task.enabled_levels) == 22
    assert s.task.fixation_duration_seconds == 2.0
    assert s.task.questionnaire_enabled is True
    assert s.display.fullscreen is True
    assert s.paths.output_root == "output"


def test_default_trigger_codes() -> None:
    """Default codes match the documented lifecycle scheme and mod=8."""
    c = default_settings().triggers.codes
    assert c.task_start == 0
    assert c.task_stop == 1
    assert c.game_reset == 2
    assert c.non_game_flip == 3
    assert c.game_frame_base == 16
    assert c.game_frame_mod == 8


def test_custom_trigger_codes_roundtrip_via_config_json(tmp_path) -> None:
    from mario_task.markers import TriggerCodes

    p = tmp_path / "config.json"
    s = Settings(
        triggers=TriggerSettings(
            codes=TriggerCodes(
                task_start=100, task_stop=101, game_reset=102, non_game_flip=103,
                game_frame_base=128, game_frame_mod=32,
            ),
        ),
    )
    settings.save(p, s)
    loaded = settings.load_from_file(p)
    assert loaded.triggers.codes.task_start == 100
    assert loaded.triggers.codes.game_frame_base == 128
    assert loaded.triggers.codes.game_frame_mod == 32


def test_validate_rejects_lifecycle_code_above_game_frame_base(tmp_path) -> None:
    from mario_task.markers import TriggerCodes

    bad = Settings(triggers=TriggerSettings(
        codes=TriggerCodes(task_start=20, game_frame_base=16),
    ))
    with pytest.raises(ValueError, match="must be < game_frame_base"):
        settings.save(tmp_path / "config.json", bad)


def test_validate_rejects_duplicate_lifecycle_codes(tmp_path) -> None:
    from mario_task.markers import TriggerCodes

    bad = Settings(triggers=TriggerSettings(
        codes=TriggerCodes(task_start=0, task_stop=0),
    ))
    with pytest.raises(ValueError, match="distinct"):
        settings.save(tmp_path / "config.json", bad)


def test_validate_rejects_overflowing_game_frame_range(tmp_path) -> None:
    from mario_task.markers import TriggerCodes

    bad = Settings(triggers=TriggerSettings(
        codes=TriggerCodes(game_frame_base=200, game_frame_mod=100),
    ))
    with pytest.raises(ValueError, match="must be ≤ 256"):
        settings.save(tmp_path / "config.json", bad)


def test_validate_rejects_zero_mod(tmp_path) -> None:
    from mario_task.markers import TriggerCodes

    bad = Settings(triggers=TriggerSettings(
        codes=TriggerCodes(game_frame_mod=0),
    ))
    with pytest.raises(ValueError, match="must be > 0"):
        settings.save(tmp_path / "config.json", bad)


def test_validate_rejects_game_frame_base_below_4(tmp_path) -> None:
    """Need ≥ 4 distinct lifecycle codes below base, so base must be ≥ 4."""
    from mario_task.markers import TriggerCodes

    bad = Settings(triggers=TriggerSettings(
        codes=TriggerCodes(game_frame_base=3),
    ))
    with pytest.raises(ValueError, match="must be ≥ 4"):
        settings.save(tmp_path / "config.json", bad)


def test_validate_accepts_game_frame_base_16_with_mod_16(tmp_path) -> None:
    """Adjacent ranges are fine: lifecycle 0..3, gameplay 16..31, no overlap."""
    from mario_task.markers import TriggerCodes

    ok = Settings(triggers=TriggerSettings(
        codes=TriggerCodes(game_frame_base=16, game_frame_mod=16),
    ))
    settings.save(tmp_path / "config.json", ok)  # no exception


def test_validate_rejects_trigger_every_below_one(tmp_path) -> None:
    bad = Settings(triggers=TriggerSettings(trigger_every=0))
    with pytest.raises(ValueError, match="trigger_every"):
        settings.save(tmp_path / "config.json", bad)


def test_round_trip_trigger_every(tmp_path) -> None:
    cfg_path = tmp_path / "config.json"
    settings.save(cfg_path, Settings(triggers=TriggerSettings(trigger_every=4)))
    loaded = settings.load_from_file(cfg_path)
    assert loaded.triggers.trigger_every == 4


def test_env_var_disables_questionnaire() -> None:
    s = load(
        config_path=None,
        env={"MARIO_QUESTIONNAIRE_ENABLED": "0"},
        cli_overrides=None,
    )
    assert s.task.questionnaire_enabled is False


def test_to_dict_includes_schema_version() -> None:
    d = default_settings().to_dict()
    assert d["schema_version"] == settings.SCHEMA_VERSION


def test_save_then_load_from_file_roundtrip(tmp_path: Path) -> None:
    p = tmp_path / "config.json"
    s = Settings(
        triggers=TriggerSettings(backend="serial", port="/dev/ttyACM0"),
        task=TaskSettings(max_duration_seconds=120, discovery_enabled=False),
    )
    save(p, s)
    loaded = load_from_file(p)
    assert loaded == s


def test_window_size_roundtrips_as_tuple_via_json(tmp_path: Path) -> None:
    p = tmp_path / "config.json"
    s = Settings(display=DisplaySettings(window_size=(800, 600), fullscreen=False))
    save(p, s)
    # On disk it's serialized as a JSON list.
    raw = json.loads(p.read_text())
    assert raw["display"]["window_size"] == [800, 600]
    # Round-tripped back as a tuple.
    loaded = load_from_file(p)
    assert loaded.display.window_size == (800, 600)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def test_validate_rejects_unknown_backend() -> None:
    with pytest.raises(ValueError):
        save("/dev/null", Settings(triggers=TriggerSettings(backend="bluetooth")))  # type: ignore[arg-type]


def test_validate_requires_port_for_serial_backend(tmp_path: Path) -> None:
    s = Settings(triggers=TriggerSettings(backend="serial", port=None))
    with pytest.raises(ValueError):
        save(tmp_path / "config.json", s)


def test_validate_requires_port_for_parallel_backend(tmp_path: Path) -> None:
    s = Settings(triggers=TriggerSettings(backend="parallel", port=None))
    with pytest.raises(ValueError):
        save(tmp_path / "config.json", s)


def test_validate_accepts_null_backend_without_port(tmp_path: Path) -> None:
    s = Settings(triggers=TriggerSettings(backend="null", port=None))
    save(tmp_path / "config.json", s)  # no exception


def test_validate_rejects_zero_duration(tmp_path: Path) -> None:
    s = Settings(task=TaskSettings(max_duration_seconds=0))
    with pytest.raises(ValueError):
        save(tmp_path / "config.json", s)


def test_validate_rejects_both_phases_disabled(tmp_path: Path) -> None:
    s = Settings(task=TaskSettings(discovery_enabled=False, practice_enabled=False))
    with pytest.raises(ValueError):
        save(tmp_path / "config.json", s)


def test_validate_rejects_bad_window_size(tmp_path: Path) -> None:
    s = Settings(display=DisplaySettings(window_size=(0, 600)))
    with pytest.raises(ValueError):
        save(tmp_path / "config.json", s)


def test_unsupported_schema_version_raises(tmp_path: Path) -> None:
    p = tmp_path / "config.json"
    p.write_text(json.dumps({"schema_version": 99}))
    with pytest.raises(ValueError):
        load_from_file(p)


# ---------------------------------------------------------------------------
# Override hierarchy
# ---------------------------------------------------------------------------


def test_load_returns_defaults_when_nothing_configured() -> None:
    s = load(config_path=None, env={}, cli_overrides=None)
    assert s == default_settings()


def test_env_overrides_apply_to_existing_config(tmp_path: Path) -> None:
    p = tmp_path / "config.json"
    save(p, Settings(triggers=TriggerSettings(backend="lsl")))

    s = load(
        config_path=p,
        env={"MARIO_TRIGGER_BACKEND": "serial", "MARIO_TRIGGER_PORT": "/dev/ttyACM0"},
        cli_overrides=None,
    )
    assert s.triggers.backend == "serial"
    assert s.triggers.port == "/dev/ttyACM0"


def test_env_bool_parsing() -> None:
    s = load(
        config_path=None,
        env={"EXP_WIN_FULLSCR": "0"},
        cli_overrides=None,
    )
    assert s.display.fullscreen is False

    s2 = load(config_path=None, env={"EXP_WIN_FULLSCR": "1"}, cli_overrides=None)
    assert s2.display.fullscreen is True


def test_env_window_size_composition() -> None:
    s = load(
        config_path=None,
        env={"EXP_WIN_W": "1280", "EXP_WIN_H": "720"},
        cli_overrides=None,
    )
    assert s.display.window_size == (1280, 720)


def test_cli_overrides_beat_env_and_config(tmp_path: Path) -> None:
    p = tmp_path / "config.json"
    save(p, Settings(task=TaskSettings(max_duration_seconds=300)))

    s = load(
        config_path=p,
        env={"MARIO_MAX_DURATION": "120"},
        cli_overrides={"max_duration": 30},  # explicit CLI flag wins
    )
    assert s.task.max_duration_seconds == 30


def test_partial_config_falls_back_to_defaults_for_missing_keys(tmp_path: Path) -> None:
    p = tmp_path / "config.json"
    # Only sets triggers; everything else should default.
    p.write_text(json.dumps({"schema_version": settings.SCHEMA_VERSION, "triggers": {"backend": "null"}}))
    s = load_from_file(p)
    assert s.triggers.backend == "null"
    assert s.task == TaskSettings()
    assert s.display == DisplaySettings()
    assert s.paths == PathSettings()



# ---------------------------------------------------------------------------
# Scanner sync
#
# The rule throughout: reject a configuration that cannot produce a valid
# run, but never reject one that merely needs hardware to be plugged in —
# mario_task.sync degrades at run time so the same config.json works at the
# scanner and on a desk.
# ---------------------------------------------------------------------------


def test_sync_defaults_to_starting_immediately() -> None:
    s = default_settings().sync
    assert s.mode == "none"
    assert s.backend == "none"
    assert s.signal == ("s",)
    assert s.port is None


def test_sync_roundtrips_through_json(tmp_path: Path) -> None:
    p = tmp_path / "config.json"
    original = Settings(
        sync=SyncSettings(
            mode="wait", backend="serial", port="/dev/ttyUSB0",
            signal=("5", "percent"), n_dummy_scans=2, timeout_seconds=30.0,
        )
    )
    save(p, original)
    assert load_from_file(p).sync == original.sync


def test_a_lone_sync_signal_may_be_written_as_a_bare_string(tmp_path: Path) -> None:
    """What anyone hand-editing config.json will type."""
    p = tmp_path / "config.json"
    p.write_text(
        json.dumps(
            {"schema_version": settings.SCHEMA_VERSION, "sync": {"mode": "wait", "signal": "t"}}
        )
    )
    assert load_from_file(p).sync.signal == ("t",)


def test_a_config_without_a_sync_section_still_loads(tmp_path: Path) -> None:
    """Forward/backward compatibility: configs predate the sync section."""
    p = tmp_path / "config.json"
    p.write_text(json.dumps({"schema_version": settings.SCHEMA_VERSION, "task": {}}))
    assert load_from_file(p).sync == SyncSettings()


def test_an_unknown_sync_mode_is_rejected() -> None:
    with pytest.raises(ValueError, match="sync.mode"):
        settings._validate(Settings(sync=SyncSettings(mode="maybe")))


def test_a_backend_that_cannot_wait_is_rejected_in_wait_mode() -> None:
    # 'lsl' can only send; waiting on it would silently never fire.
    with pytest.raises(ValueError, match="sync.backend"):
        settings._validate(Settings(sync=SyncSettings(mode="wait", backend="lsl")))


def test_a_backend_that_cannot_send_is_rejected_in_send_mode() -> None:
    with pytest.raises(ValueError, match="sync.backend"):
        settings._validate(Settings(sync=SyncSettings(mode="send", backend="keyboard")))


def test_an_empty_sync_signal_is_rejected_when_it_would_be_used() -> None:
    with pytest.raises(ValueError, match="sync.signal"):
        settings._validate(Settings(sync=SyncSettings(mode="send", signal=())))
    # ...but mode 'none' never looks at it.
    settings._validate(Settings(sync=SyncSettings(mode="none", signal=())))


def test_a_missing_port_is_not_a_configuration_error() -> None:
    """sync.configure warns and degrades, so the same config works anywhere."""
    settings._validate(Settings(sync=SyncSettings(mode="send", backend="serial", port=None)))
    settings._validate(Settings(sync=SyncSettings(mode="wait", backend="serial", port=None)))


def test_negative_dummy_scans_and_zero_timeout_are_rejected() -> None:
    with pytest.raises(ValueError, match="n_dummy_scans"):
        settings._validate(Settings(sync=SyncSettings(n_dummy_scans=-1)))
    with pytest.raises(ValueError, match="timeout_seconds"):
        settings._validate(Settings(sync=SyncSettings(timeout_seconds=0)))
    # null means "wait indefinitely", which is allowed.
    settings._validate(Settings(sync=SyncSettings(timeout_seconds=None)))


def test_sync_env_overrides(tmp_path: Path) -> None:
    s = load(
        config_path=None,
        env={
            "MARIO_SYNC_MODE": "wait",
            "MARIO_SYNC_BACKEND": "keyboard",
            "MARIO_SYNC_SIGNAL": "5, percent",
            "MARIO_SYNC_DUMMY_SCANS": "3",
            "MARIO_SYNC_TIMEOUT": "12.5",
        },
        cli_overrides=None,
    )
    assert s.sync.mode == "wait"
    assert s.sync.signal == ("5", "percent")
    assert s.sync.n_dummy_scans == 3
    assert s.sync.timeout_seconds == 12.5


def test_a_blank_sync_timeout_means_wait_forever() -> None:
    s = load(config_path=None, env={"MARIO_SYNC_TIMEOUT": ""}, cli_overrides=None)
    assert s.sync.timeout_seconds is None


def test_an_unparseable_env_var_names_itself() -> None:
    with pytest.raises(ValueError, match="MARIO_SYNC_DUMMY_SCANS"):
        load(config_path=None, env={"MARIO_SYNC_DUMMY_SCANS": "lots"}, cli_overrides=None)


def test_sync_cli_overrides_beat_config(tmp_path: Path) -> None:
    p = tmp_path / "config.json"
    save(p, Settings(sync=SyncSettings(mode="wait")))
    s = load(
        config_path=p,
        env={},
        cli_overrides={"sync_mode": "send", "sync_signal": ("t",), "sync_port": "/dev/ttyUSB0"},
    )
    assert s.sync.mode == "send"
    assert s.sync.signal == ("t",)
    assert s.sync.port == "/dev/ttyUSB0"


# ---------------------------------------------------------------------------
# Trigger params
# ---------------------------------------------------------------------------


def test_trigger_events_default_to_all_on() -> None:
    ev = default_settings().triggers.events()
    assert (ev.on_game_frame, ev.on_game_reset, ev.on_non_game_flip) == (True, True, True)


def test_trigger_event_flags_roundtrip_through_json(tmp_path: Path) -> None:
    p = tmp_path / "config.json"
    save(p, Settings(triggers=TriggerSettings(on_game_frame=False, trigger_every=4)))
    loaded = load_from_file(p)
    assert loaded.triggers.on_game_frame is False
    assert loaded.triggers.on_game_reset is True
    assert loaded.triggers.trigger_every == 4
    assert loaded.triggers.events().on_game_frame is False


def test_trigger_env_and_cli_overrides(tmp_path: Path) -> None:
    s = load(
        config_path=None,
        env={"MARIO_TRIGGER_EVERY": "5", "MARIO_TRIGGER_ON_NON_GAME_FLIP": "0"},
        cli_overrides=None,
    )
    assert s.triggers.trigger_every == 5
    assert s.triggers.on_non_game_flip is False

    s = load(config_path=None, env={}, cli_overrides={"trigger_every": 2})
    assert s.triggers.trigger_every == 2


def test_the_retired_eeg_flag_names_still_land_on_the_trigger_fields() -> None:
    """--eeg-backend / --eeg-port predate the rename; they must keep working."""
    s = load(
        config_path=None,
        env={},
        cli_overrides={"eeg_backend": "serial", "eeg_port": "/dev/ttyACM0"},
    )
    assert s.triggers.backend == "serial"
    assert s.triggers.port == "/dev/ttyACM0"


# ---------------------------------------------------------------------------
# Button mapping
#
# key_set is positional: emulator.step() takes one boolean per entry, and
# questionnaire.py indexes into it (4=UP .. 8=A). Both contracts are tested
# here, because breaking either is silent at run time.
# ---------------------------------------------------------------------------


#: What key_set() produced before it was configurable. Hard-coded on purpose:
#: this is the value the README documents and every existing recording used.
HISTORIC_KEY_SET = ["z", "_", "_", "_", "up", "down", "left", "right", "x", "_", "_", "_"]


def test_the_default_map_reproduces_the_historic_key_set() -> None:
    """Arrows to move, Z to run, X to jump — what the README has always said."""
    assert default_settings().input.key_set() == HISTORIC_KEY_SET


def test_the_tasks_fallback_key_set_matches_the_settings_default() -> None:
    """task.DEFAULT_KEY_SET is derived, not duplicated; prove it stayed put."""
    task = import_or_skip("mario_task.task", reason="needs psychopy + retro")
    assert task.DEFAULT_KEY_SET == HISTORIC_KEY_SET


def test_key_set_puts_each_button_at_its_retro_index() -> None:
    from mario_task.settings import InputSettings

    ks = InputSettings(
        button_map={
            "UP": "i", "DOWN": "k", "LEFT": "j", "RIGHT": "l",
            "A": "space", "B": "shift", "START": "return", "SELECT": "tab",
        }
    ).key_set()
    # Order comes from stable_retro/cores/fceumm.json.
    assert ks[0] == "shift"    # B
    assert ks[2] == "tab"      # SELECT
    assert ks[3] == "return"   # START
    assert ks[4:8] == ["i", "k", "j", "l"]  # UP DOWN LEFT RIGHT
    assert ks[8] == "space"    # A


def test_the_questionnaire_navigation_follows_a_remapped_pad() -> None:
    """questionnaire.py reads key_set[4:9]; remapping must carry through."""
    from mario_task.settings import InputSettings

    ks = InputSettings(
        button_map={**settings.DEFAULT_BUTTON_MAP, "UP": "i", "A": "space"}
    ).key_set()
    assert ks[4] == "i"      # nav_up
    assert ks[8] == "space"  # nav_submit


def test_an_unbound_button_is_never_reported_as_pressed() -> None:
    """START/SELECT default to blank, which must not match a real key."""
    ks = default_settings().input.key_set()
    assert ks[2] == settings.UNBOUND and ks[3] == settings.UNBOUND
    # held_for does `k in pressed_keys`; no pyglet name normalises to "_".
    assert settings.UNBOUND not in ("up", "down", "left", "right", "x", "z")


def test_button_map_roundtrips_through_json(tmp_path: Path) -> None:
    from mario_task.settings import InputSettings

    p = tmp_path / "config.json"
    remapped = {**settings.DEFAULT_BUTTON_MAP, "A": "space", "B": "shift"}
    save(p, Settings(input=InputSettings(button_map=remapped)))
    assert load_from_file(p).input.button_map == remapped


def test_a_partial_button_map_merges_onto_the_default(tmp_path: Path) -> None:
    """Rebinding one button must not silently unbind the other seven."""
    p = tmp_path / "config.json"
    p.write_text(
        json.dumps(
            {
                "schema_version": settings.SCHEMA_VERSION,
                "input": {"button_map": {"A": "space"}},
            }
        )
    )
    loaded = load_from_file(p).input
    assert loaded.button_map["A"] == "space"
    assert loaded.button_map["UP"] == "up"
    assert loaded.button_map["B"] == "z"


def test_a_config_without_an_input_section_still_loads(tmp_path: Path) -> None:
    p = tmp_path / "config.json"
    p.write_text(json.dumps({"schema_version": settings.SCHEMA_VERSION, "task": {}}))
    assert load_from_file(p).input.button_map == settings.DEFAULT_BUTTON_MAP


def test_one_key_on_two_buttons_is_rejected() -> None:
    from mario_task.settings import InputSettings

    bad = {**settings.DEFAULT_BUTTON_MAP, "A": "z"}  # z is already B
    with pytest.raises(ValueError, match="both B and A|both A and B"):
        settings._validate(Settings(input=InputSettings(button_map=bad)))


def test_leaving_a_playable_button_unbound_is_rejected() -> None:
    from mario_task.settings import InputSettings

    bad = {**settings.DEFAULT_BUTTON_MAP, "LEFT": ""}
    with pytest.raises(ValueError, match=r"leaves \['LEFT'\] unbound"):
        settings._validate(Settings(input=InputSettings(button_map=bad)))


def test_start_and_select_may_be_left_unbound() -> None:
    """The default, and deliberately so: a paused subject breaks segmentation."""
    settings._validate(default_settings())
    assert default_settings().input.button_map["START"] == ""


def test_a_button_the_nes_does_not_have_is_rejected() -> None:
    from mario_task.settings import InputSettings

    bad = {**settings.DEFAULT_BUTTON_MAP, "TURBO": "t"}
    with pytest.raises(ValueError, match="TURBO"):
        settings._validate(Settings(input=InputSettings(button_map=bad)))
