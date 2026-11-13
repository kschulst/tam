from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path


def _xdg(var: str, default: str) -> Path:
    return Path(os.environ.get(var) or Path.home() / default)


def state_dir() -> Path:
    return _xdg("XDG_STATE_HOME", ".local/state") / "tam"


def config_dir() -> Path:
    return _xdg("XDG_CONFIG_HOME", ".config") / "tam"


def state_file() -> Path:
    return state_dir() / "state.json"


def seen_dir() -> Path:
    return state_dir() / "seen"


def claude_dir() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")


def tam_executable() -> str:
    """Path used when tmux or hooks call back into tam.

    Symlinks are not resolved, so a Homebrew upgrade keeps working.
    """
    argv0 = sys.argv[0]
    if os.sep in argv0 and os.path.exists(argv0):
        return os.path.abspath(argv0)
    return shutil.which("tam") or "tam"


def tam_version() -> str:
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("tam")
    except PackageNotFoundError:  # installed without packaging metadata, e.g. Homebrew
        from tam import __version__

        return __version__
