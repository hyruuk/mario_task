"""Likert-scale questionnaire UI for post-run flow ratings.

Port of upstream ``videogame.py:_questionnaire`` (lines 546-704) plus
the question texts from ``src/sessions/game_questionnaires.py``.

The UI is a vertical stack of Likert scales (one per question), with
labels "Disagree" / "Agree" above the top scale. The currently active
question is bolded and its scale line drawn cyan; the operator
navigates with:

    UP / DOWN  — change active question (key_set[4] / key_set[5])
    LEFT / RIGHT  — adjust the response for the active question
    A (= "x")  — submit all answers and exit (key_set[8])

Navigation keys are read from the gameplay key_set so they match the
buttons the player just used during gameplay (no need to switch hand
position). On submit, one ``questionnaire-answer`` event is appended
to ``task._events`` per question; per-keypress value changes are also
logged as ``questionnaire-value-change`` rows for fine-grained
analysis.
"""

from __future__ import annotations

import csv
import time
from pathlib import Path
from typing import TYPE_CHECKING, Generator

# psychopy.visual imports trigger pyglet which needs libGL — fails on
# headless CI (no display). Push the import inside ``run`` so that just
# importing this module (e.g. to call ``_append_to_subject_tsv`` from a
# unit test) doesn't need a graphics stack.

if TYPE_CHECKING:
    from psychopy.visual import Window

    from mario_task.task import MarioTask


# ---------------------------------------------------------------------------
# Default question banks (verbatim from upstream game_questionnaires.py).
# ---------------------------------------------------------------------------

FLOW_RATINGS: list[str] = [
    "I feel just the right amount of challenge.",
    "My thoughts/activities run fluidly and smoothly.",
    "I don’t notice time passing.",
    "I have no difficulty concentrating.",
    "My mind is completely clear.",
    "I am totally absorbed in what I am doing.",
    "The right thoughts/movements occur of their own accord.",
    "I know what I have to do each step of the way.",
    "I feel that I have everything under control.",
    "I am completely lost in thought.",
]

OTHER_RATINGS: list[str] = [
    "I am tired.",
    "I am frustrated.",
]

# Default n-point Likert scale.
DEFAULT_N_POINTS = 7


def build_default_questions(
    include_other: bool = True, n_points: int = DEFAULT_N_POINTS
) -> list[tuple[int, str, int]]:
    """Return the ``(key, text, n_pts)`` tuples used by ``run`` below.

    Keys are 0-indexed positions in the combined list so analysts can
    join questionnaire-answer rows to question texts by index. The
    text strings are what the participant actually reads.
    """
    items = list(FLOW_RATINGS)
    if include_other:
        items += OTHER_RATINGS
    return [(idx, text, n_points) for idx, text in enumerate(items)]


# ---------------------------------------------------------------------------
# The questionnaire generator
# ---------------------------------------------------------------------------


_SUBJECT_TSV_COLUMNS = [
    "subject", "session", "task_name", "run_idx",
    "submit_time", "question_idx", "question_text", "value",
]


def _append_to_subject_tsv(
    path: Path,
    *,
    subject: str,
    session: str,
    task_name: str,
    run_idx: int,
    questions: list[tuple[int, str, int]],
    responses: list[int],
    submit_time: float,
) -> None:
    """Append one row per question to the subject's cumulative questionnaire TSV.

    Creates the file with a header if it doesn't exist. Subsequent calls
    just append data rows. Tall format (one row per question per
    submission) so analysis can group/filter without column-counting.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    write_header = not path.exists()
    with path.open("a", encoding="utf-8", newline="") as f:
        w = csv.writer(f, delimiter="\t", lineterminator="\n")
        if write_header:
            w.writerow(_SUBJECT_TSV_COLUMNS)
        for (q_idx, q_text, _), value in zip(questions, responses):
            w.writerow([
                subject,
                session,
                task_name,
                run_idx,
                f"{submit_time:.6f}",
                q_idx,
                q_text,
                value,
            ])


def run(
    task: "MarioTask",
    exp_win: "Window",
    ctl_win: "Window | None",
    questions: list[tuple[int, str, int]],
    *,
    subject_tsv_path: Path | None = None,
    subject: str | None = None,
    session: str | None = None,
    run_idx: int | None = None,
) -> Generator[bool, None, None]:
    # Local import: keeps the module headless-importable for unit tests
    # that only exercise the file-writing helpers.
    from psychopy import logging, visual
    """Yield once per frame until the operator submits the questionnaire.

    The generator is driven by the parent :meth:`_TaskBase.run` flip loop
    (same as ``_run``). Yields ``True`` to clear the back-buffer on each
    flip so the bullets and lines redraw cleanly without ghosting.
    """
    if not questions:
        return

    # Reset the background to mid-grey so the white text stays legible.
    exp_win.setColor([0] * 3, colorSpace="rgb")

    # ----- layout in pixels, anchored to the window centre -----
    win_w, win_h = exp_win.size
    y_spacing = max(60, win_h // 12)        # row pitch (px)
    bullet_radius = max(8, win_h // 100)    # ~10 px on 1080p
    line_thickness = max(4, win_h // 200)
    text_height = max(20, win_h // 38)
    extent = win_w * 0.18                   # half-width of the scale line
    scales_block_x = win_w * 0.22           # scale centre column (right side)
    scales_block_y = (len(questions) - 1) / 2.0 * y_spacing
    n_pts_default = questions[0][2]

    # ----- static legend stimuli ("Disagree" / "Agree") -----
    legend_disagree = visual.TextStim(
        exp_win, text="Disagree", units="pix", color="white",
        pos=(scales_block_x - extent, scales_block_y + y_spacing * 0.85),
        wrapWidth=win_w * 0.5,
        height=text_height,
        bold=True,
        anchorHoriz="center", alignText="center",
    )
    legend_agree = visual.TextStim(
        exp_win, text="Agree", units="pix", color="white",
        pos=(scales_block_x + extent, scales_block_y + y_spacing * 0.85),
        wrapWidth=win_w * 0.5,
        height=text_height,
        bold=True,
        anchorHoriz="center", alignText="center",
    )

    # ----- per-question stimuli -----
    active_question = 0
    responses: list[int] = []
    lines: list[visual.Line] = []
    bullets: list[list[visual.Circle]] = []
    texts: list[visual.TextStim] = []

    for q_n, (_, question_text, n_pts) in enumerate(questions):
        default_response = n_pts // 2
        responses.append(default_response)
        x_spacing = extent * 2 / (n_pts - 1)
        y_pos = scales_block_y - q_n * y_spacing

        lines.append(visual.Line(
            exp_win,
            (scales_block_x - extent, y_pos),
            (scales_block_x + extent, y_pos),
            units="pix",
            lineWidth=line_thickness,
            autoLog=False,
            lineColor=(0, -1, -1) if q_n == 0 else (-1, -1, -1),
        ))
        bullets.append([
            visual.Circle(
                exp_win,
                units="pix",
                radius=bullet_radius,
                pos=(scales_block_x - extent + i * x_spacing, y_pos),
                fillColor=(1, 1, 1) if default_response == i else (-1, -1, -1),
                lineColor=(-1, -1, -1),
                lineWidth=line_thickness,
                autoLog=False,
            )
            for i in range(n_pts)
        ])
        texts.append(visual.TextStim(
            exp_win,
            text=question_text,
            units="pix",
            bold=(q_n == active_question),
            color="white",
            pos=(0, y_pos),
            wrapWidth=win_w * 0.4,
            height=text_height,
            anchorHoriz="right",
            alignText="right",
        ))

    # ----- navigation keys come from the player's key_set so the controls
    # stay consistent with the gameplay buttons -----
    nav_up = task.key_set[4]
    nav_down = task.key_set[5]
    nav_left = task.key_set[6]
    nav_right = task.key_set[7]
    nav_submit = task.key_set[8]

    n_flips = 0
    while True:
        task.input.poll(exp_win, task.task_timer)
        new_keys = task.input.new_keys_pressed()

        if nav_up in new_keys and active_question > 0:
            active_question -= 1
        elif nav_down in new_keys and active_question < len(questions) - 1:
            active_question += 1
        elif nav_right in new_keys and responses[active_question] < questions[active_question][2] - 1:
            responses[active_question] += 1
        elif nav_left in new_keys and responses[active_question] > 0:
            responses[active_question] -= 1
        elif nav_submit in new_keys:
            submit_time = time.time()
            for (key, question_text, _), value in zip(questions, responses):
                task._log_event({
                    "trial_type": "questionnaire-answer",
                    "game": task.game_name,
                    "level": task.state_name,
                    "stim_file": task.movie_path,
                    "question": key,
                    "question_text": question_text,
                    "value": value,
                })
            # Also append to the subject-level cumulative TSV so every
            # questionnaire ever filled in for this subject is in one
            # place — convenient for downstream analysis without having
            # to walk every per-task events.tsv.
            if subject_tsv_path is not None and subject is not None and session is not None:
                _append_to_subject_tsv(
                    Path(subject_tsv_path),
                    subject=subject,
                    session=session,
                    task_name=task.name,
                    run_idx=run_idx if run_idx is not None else 0,
                    questions=questions,
                    responses=responses,
                    submit_time=submit_time,
                )
                logging.exp(
                    f"questionnaire answers appended to {subject_tsv_path}"
                )
            logging.exp(f"questionnaire submitted with responses {responses}")
            return
        elif n_flips > 1:
            # No nav input this frame and the UI is already drawn — sleep a
            # tick so we don't burn 100% CPU spinning on dispatch_events.
            time.sleep(0.01)
            continue

        # Log every value change for fine-grained analysis.
        if n_flips > 0:
            task._log_event({
                "trial_type": "questionnaire-value-change",
                "game": task.game_name,
                "level": task.state_name,
                "stim_file": task.movie_path,
                "question": questions[active_question][0],
                "value": responses[active_question],
            })

        # Update visuals.
        for q_n, (txt_stim, line_stim, bullets_q) in enumerate(zip(texts, lines, bullets)):
            txt_stim.bold = q_n == active_question
            line_stim.lineColor = (0, -1, -1) if q_n == active_question else (-1, -1, -1)
            for bullet_n, bullet in enumerate(bullets_q):
                bullet.fillColor = (1, 1, 1) if responses[q_n] == bullet_n else (-1, -1, -1)

        for stim in lines + sum(bullets, []) + texts:
            stim.draw(exp_win)
            if ctl_win is not None:
                stim.draw(ctl_win)
        legend_disagree.draw(exp_win)
        legend_agree.draw(exp_win)
        if ctl_win is not None:
            legend_disagree.draw(ctl_win)
            legend_agree.draw(ctl_win)

        yield True
        n_flips += 1
