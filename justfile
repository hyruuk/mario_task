# mario_task developer recipes
# Usage: `just <recipe>`. Cross-platform; `just` itself must be installed (cargo / brew / scoop).

# Default: show available recipes.
default:
    @just --list

# Set up the dev environment (Linux). Use setup_env.ps1 on Windows.
setup:
    bash setup_env.sh

# Run the experiment. Usage: `just run sub01 01`
run subject session:
    bash run.sh {{subject}} {{session}}

# Run the pure-Python test suite (no display, no psychopy/retro side effects).
test:
    uv run pytest tests/ -k "not integration" -v

# Run the integration smoke test (requires DISPLAY + psychopy + retro + ROM).
test-integration:
    uv run pytest tests/ -k integration -v

# Lint.
lint:
    uv run ruff check mario_task tests

# Auto-fix lint issues where possible.
lint-fix:
    uv run ruff check --fix mario_task tests

# Re-resolve dependency lockfile.
lock:
    uv lock
