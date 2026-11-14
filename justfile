# Tasks for developing tam. `just` lists them.

# List tasks
[private]
default:
    @just --list --unsorted

# Install the tools for developing tam (uv, just, gh), the Python environment and the commit-msg hook
[group('setup')]
setup:
    brew bundle --file Brewfile
    uv sync
    git config core.hooksPath .githooks

# Install tam from Homebrew (kschulst/tap), or upgrade it, and run tam setup
[group('setup')]
install:
    #!/usr/bin/env bash
    set -euo pipefail
    if brew list --formula tam >/dev/null 2>&1; then
        brew update --quiet && { brew upgrade tam || true; }
    else
        brew install kschulst/tap/tam
    fi
    tam setup

# Use tam from this checkout: `tam` runs the source, and tmux keys point to it
[group('dev')]
dev-on:
    uv tool install --editable . --force
    "$HOME/.local/bin/tam" tmux-init
    @echo "tam now runs from {{justfile_directory()}}"

# Go back to the Homebrew tam
[group('dev')]
dev-off:
    uv tool uninstall tam
    /opt/homebrew/bin/tam tmux-init
    @echo "tam now runs from Homebrew"

# Show which tam is active
[group('dev')]
which:
    @command -v tam
    @tam help --print | head -1

# Run tam from the source without installing: `just run sidebar`
[group('dev')]
run *args:
    uv run tam {{args}}

# Everything that must pass before a release
[group('quality')]
check: lint test

# Run the tests (they use an isolated tmux server)
[group('quality')]
test *args:
    uv run pytest {{args}}

# Lint and check formatting
[group('quality')]
lint:
    uv run ruff check src tests scripts
    uv run ruff format --check src tests scripts

# Format the code and fix what ruff can fix
[group('quality')]
fmt:
    uv run ruff format src tests scripts
    uv run ruff check --fix src tests scripts

# Release: version from the commits (feat → minor, fix → patch), or `just release minor`. Tag, GitHub Release, Homebrew formula
[group('release')]
release *bump: check
    uv run python scripts/release.py {{bump}}

# Show the last release and the commits since
[group('release')]
release-status:
    @uv run python scripts/release.py --dry-run
