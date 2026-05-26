"""Tests for the settings module: schema, validation, override hierarchy, atomic save."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mario_task import settings
from mario_task.settings import (
    DisplaySettings,
    PathSettings,
    Settings,
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
# Platform helpers
# ---------------------------------------------------------------------------


def test_supports_parallel_port_reflects_platform() -> None:
    # Smoke check; the boolean depends on the test machine but the function
    # must always return a bool.
    assert isinstance(settings.supports_parallel_port(), bool)
