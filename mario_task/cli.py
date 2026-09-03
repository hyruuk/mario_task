"""``mario_task`` command-line entry point.

Builds a :class:`RunConfig` from CLI flags + ``config.json`` + env vars,
then hands off to :func:`mario_task.session.run_session`.

Two conveniences make the common case a single word:

* No ``config.json`` (or ``--reconfigure``) opens the config wizard.
* No SUBJECT opens the subject picker; an omitted SESSION becomes the next
  unused number for that subject.

Usage:
    python -m mario_task SUBJECT SESSION
    python -m mario_task --max-duration 30 sub01 01
    python -m mario_task --trigger-backend null sub01 01   # dev / no LSL outlet
    python -m mario_task --sync-mode send --sync-backend serial \
        --sync-port /dev/ttyUSB0 01 001                    # start the scanner

``--eeg-backend`` / ``--eeg-port`` are the old names for
``--trigger-backend`` / ``--trigger-port`` and still work.
"""

from __future__ import annotations

import argparse
import logging
import sys

from dotenv import load_dotenv

from mario_task import settings as settings_mod
from mario_task.paths import (
    BidsPaths,
    infer_next_session,
    make_timestamp,
    normalize_session,
    normalize_subject,
)

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
        "--trigger-backend",
        "--eeg-backend",
        dest="trigger_backend",
        choices=["lsl", "serial", "parallel", "null"],
        default=None,
        help="Transport for outgoing event markers (overrides config.json).",
    )
    p.add_argument(
        "--trigger-port",
        "--eeg-port",
        dest="trigger_port",
        default=None,
        help="Port for the trigger backend (required for serial/parallel).",
    )
    p.add_argument(
        "--trigger-every",
        dest="trigger_every",
        type=int,
        default=None,
        help="Emit one gameplay marker per N emulator frames (1 = every frame).",
    )

    p.add_argument(
        "--sync-mode",
        dest="sync_mode",
        choices=("send", "wait", "none"),
        default=None,
        help="none: start immediately. wait: wait for the sync signal. send: start the scanner.",
    )
    p.add_argument(
        "--sync-backend",
        dest="sync_backend",
        choices=("none", "serial", "parallel", "lsl", "key", "keyboard", "markers"),
        default=None,
        help="Transport for the start signal ('markers' re-uses the trigger port).",
    )
    p.add_argument(
        "--sync-port",
        dest="sync_port",
        default=None,
        help="Port for the sync backend, e.g. /dev/ttyUSB0.",
    )
    p.add_argument(
        "--sync-signal",
        dest="sync_signal",
        type=lambda v: tuple(x.strip() for x in v.split(",") if x.strip()),
        default=None,
        help=(
            "The sync signal: what to send, or what to wait for. "
            "Comma-separated to accept alternatives (e.g. '5,percent')."
        ),
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


#: argparse dests that map onto settings fields (see settings._CLI_KEYS).
_OVERRIDE_DESTS = (
    "output_root",
    "max_duration",
    "fullscreen",
    "trigger_backend",
    "trigger_port",
    "trigger_every",
    "sync_mode",
    "sync_backend",
    "sync_port",
    "sync_signal",
)


def main(argv: list[str] | None = None) -> int:
    # Parsed before mario_task.session is imported: psychopy parses sys.argv
    # at import time (its preferences module installs its own --help), so
    # importing it any earlier would hijack this program's command line.
    parser = _build_parser()
    args = parser.parse_args(argv)

    from mario_task.session import RunConfig, run_session

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s: %(message)s",
    )

    # None means "flag not given", so those keys are dropped and the lower
    # precedence layers (env, config.json, defaults) win.
    cli_overrides = {
        dest: getattr(args, dest)
        for dest in _OVERRIDE_DESTS
        if getattr(args, dest, None) is not None
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

    try:
        settings = settings_mod.load(
            config_path=config_path,
            cli_overrides=cli_overrides,
        )
    except ValueError as exc:
        print(f"Configuration error:\n{exc}", file=sys.stderr)
        print(
            "\nFix config.json, or re-run with --reconfigure to regenerate it.",
            file=sys.stderr,
        )
        return 2

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
        session = (
            normalize_session(args.session)
            if args.session
            else infer_next_session(settings.paths.output_root, subject)
        )

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
        "Launching session: sub-%s ses-%s%s | max=%ds | sync=%s | triggers=%s",
        subject, session,
        " (auto-detected)" if args.session is None else "",
        settings.task.max_duration_seconds,
        settings.sync.mode,
        settings.triggers.backend,
    )
    return run_session(config)


if __name__ == "__main__":
    raise SystemExit(main())
