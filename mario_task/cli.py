"""Command-line entry point for ``mario_task``.

Phase 0: minimal stub that loads settings (proves the pure-Python core
imports cleanly) and prints a friendly placeholder. The full CLI lands
in Phase 1 once ``session.py`` and ``gui.py`` are implemented.

The Phase 1 contract will be:

    1. Parse argparse flags (subject, session, output, eeg-backend, ...).
    2. Load .env (via ``python-dotenv``).
    3. ``settings.load(config_path, env, cli_overrides)``.
    4. If config.json doesn't exist, launch ``gui.run_config_wizard()``,
       save it, then exit so the operator can verify.
    5. Otherwise launch ``gui.pick_subject()`` (unless subject/session
       were passed positionally), then call ``session.run_session(cfg)``.
"""

from __future__ import annotations

import argparse
import logging
import sys

from mario_task import settings as settings_mod


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="mario_task",
        description="NES Super Mario Bros experiment runner (Phase 0 stub).",
    )
    p.add_argument("subject", nargs="?", help="Subject label (e.g. sub01). Optional.")
    p.add_argument("session", nargs="?", help="Session label (e.g. 01). Optional.")
    p.add_argument(
        "--output",
        dest="output_root",
        default=None,
        help="Where to write BIDS outputs (overrides config.json).",
    )
    p.add_argument(
        "--max-duration",
        dest="max_duration",
        type=int,
        default=None,
        help="Run duration in seconds (overrides config.json).",
    )
    p.add_argument(
        "--eeg-backend",
        dest="eeg_backend",
        choices=["lsl", "serial", "parallel", "null"],
        default=None,
        help="Trigger backend (overrides config.json).",
    )
    p.add_argument(
        "--eeg-port",
        dest="eeg_port",
        default=None,
        help="Trigger port (serial/parallel only).",
    )
    p.add_argument(
        "--no-fullscreen",
        dest="fullscreen",
        action="store_const",
        const=False,
        default=None,
        help="Run in a windowed mode (debug).",
    )
    p.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Verbose logging.",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s: %(message)s",
    )

    cli_overrides = {
        k: v
        for k, v in {
            "output_root": args.output_root,
            "max_duration": args.max_duration,
            "eeg_backend": args.eeg_backend,
            "eeg_port": args.eeg_port,
            "fullscreen": args.fullscreen,
        }.items()
        if v is not None
    }

    cfg = settings_mod.load(
        config_path=settings_mod.config_path_default(),
        cli_overrides=cli_overrides,
    )

    print("mario_task — Phase 0 stub")
    print("Loaded settings:")
    print(f"  trigger backend : {cfg.triggers.backend}")
    print(f"  trigger port    : {cfg.triggers.port}")
    print(f"  max duration    : {cfg.task.max_duration_seconds} s")
    print(f"  discovery       : {'on' if cfg.task.discovery_enabled else 'off'}")
    print(f"  practice        : {'on' if cfg.task.practice_enabled else 'off'}")
    print(f"  fullscreen      : {cfg.display.fullscreen}")
    print(f"  output root     : {cfg.paths.output_root}")
    if args.subject:
        print(f"  subject         : {args.subject}")
    if args.session:
        print(f"  session         : {args.session}")
    print()
    print("Phase 1 (display + retro + GUI wizard) is not yet implemented.")
    print("To run the unit-test suite: `uv run pytest tests/`.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
