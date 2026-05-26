"""``mario_task`` command-line entry point.

Phase 1: builds a :class:`RunConfig` from CLI flags + ``config.json`` +
env vars, then calls :func:`session.run_session` which runs a single
Level 1-1 attempt for ``max_duration_seconds``.

Phase 2 will wrap this with the first-run config wizard (when
``config.json`` is missing) and the per-session subject picker
(when no subject/session args are passed).

Usage:
    python -m mario_task SUBJECT SESSION
    python -m mario_task --max-duration 30 sub01 01
    python -m mario_task --eeg-backend null sub01 01     # dev / no LSL outlet
"""

from __future__ import annotations

import argparse
import logging
import sys

from dotenv import load_dotenv

from mario_task import settings as settings_mod
from mario_task.paths import BidsPaths, infer_next_session, make_timestamp, normalize_subject
from mario_task.session import RunConfig, run_session

# Load any .env in cwd so env-var overrides apply.
load_dotenv()

log = logging.getLogger(__name__)


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="mario_task",
        description="NES Super Mario Bros experiment runner.",
    )
    p.add_argument(
        "subject",
        nargs="?",
        default=None,
        help=(
            "Subject label, e.g. '01' (BIDS sub- prefix added automatically) "
            "or 'sub-01' (prefix stripped). If omitted, a GUI picker opens "
            "showing existing subjects with their progress."
        ),
    )
    p.add_argument(
        "session",
        nargs="?",
        default=None,
        help=(
            "Session label (e.g. '002'). If omitted, the next available "
            "session number for this subject is used."
        ),
    )
    p.add_argument(
        "--output",
        dest="output_root",
        default=None,
        help="BIDS output root (overrides config.json).",
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
        help="Trigger port (required for serial/parallel).",
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
        "--reconfigure",
        action="store_true",
        help=(
            "Re-launch the first-run config wizard even if config.json "
            "already exists. The existing config.json is overwritten "
            "with whatever you submit; cancel to keep it unchanged."
        ),
    )
    p.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Verbose Python logging (INFO → DEBUG).",
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

    # First-run wizard: if no config.json exists (or --reconfigure was
    # passed), open the GUI to collect the operator's trigger / display
    # / ROM / enabled-levels choices. Cancel → exit 0 with the existing
    # config.json untouched.
    config_path = settings_mod.config_path_default()
    if args.reconfigure or not config_path.exists():
        from mario_task import gui  # local import: psychopy is heavy
        reason = "--reconfigure" if args.reconfigure else "no config.json found"
        log.info("Launching configuration wizard (%s).", reason)
        if gui.run_config_wizard(config_path) is None:
            log.info("Config wizard cancelled; exiting.")
            return 0

    settings = settings_mod.load(
        config_path=config_path,
        cli_overrides=cli_overrides,
    )

    # Subject-picker GUI: if the operator didn't pass a subject on the
    # CLI, open the dialog so they can pick from existing subjects (with
    # progress info) or type a new one. Cancel → exit 0.
    if args.subject is None:
        from mario_task import gui
        picked = gui.pick_subject(settings.paths.output_root)
        if picked is None:
            log.info("Subject picker cancelled; exiting.")
            return 0
        subject, session = picked
    else:
        subject = normalize_subject(args.subject)
        session = args.session or infer_next_session(settings.paths.output_root, subject)

    paths = BidsPaths(
        subject=subject,
        session=session,
        output_root=settings.paths.output_root,
        timestamp=make_timestamp(),
    )

    config = RunConfig(
        subject=subject,
        session=session,
        settings=settings,
        paths=paths,
    )

    log.info(
        "Launching session: sub-%s ses-%s%s (max=%ds, backend=%s)",
        subject, session,
        " (auto-detected)" if args.session is None else "",
        settings.task.max_duration_seconds, settings.triggers.backend,
    )
    return run_session(config)


if __name__ == "__main__":
    raise SystemExit(main())
