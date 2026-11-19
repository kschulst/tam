"""Agent definitions loaded from TOML."""

from __future__ import annotations

import os
import shlex
import tomllib
from dataclasses import dataclass
from importlib import resources

from tam import paths


@dataclass(frozen=True)
class Harness:
    name: str
    start: str
    resume: str | None = None
    # Resumes one exact session ({id}). Used instead of resume when the id is known,
    # for agents whose resume continues the latest session (codex resume --last).
    resume_id: str | None = None

    def command(
        self, *, name: str, session_id: str, started: bool, args: list[str]
    ) -> tuple[list[str], bool]:
        """Return (argv, resuming)."""
        if session_id and self.resume_id:
            return _render(self.resume_id, name, session_id) + args, True
        if self.resume:
            needs_id = "{id}" in self.resume
            if (needs_id and session_id) or (not needs_id and started):
                return _render(self.resume, name, session_id) + args, True
        return _render(self.start, name, session_id) + args, False


def _render(template: str, name: str, session_id: str) -> list[str]:
    out = []
    for part in shlex.split(template):
        part = part.replace("{name}", name).replace("{id}", session_id)
        out.append(os.path.expanduser(os.path.expandvars(part)))
    return out


def _parse(text: str) -> dict[str, dict]:
    return tomllib.loads(text).get("harness", {})


def load() -> dict[str, Harness]:
    merged: dict[str, dict] = _parse(resources.files("tam").joinpath("harnesses.toml").read_text())
    user = paths.config_dir() / "harnesses.toml"
    if user.exists():
        for key, value in _parse(user.read_text()).items():
            merged[key] = {**merged.get(key, {}), **value}
    result = {}
    for key, value in merged.items():
        if "start" not in value:
            continue
        result[key] = Harness(
            name=key,
            start=value["start"],
            resume=value.get("resume"),
            resume_id=value.get("resume_id"),
        )
    return result


def installed(h: Harness) -> bool:
    """True if the agent's program is on PATH."""
    import shutil

    parts = shlex.split(h.start)
    return bool(parts) and shutil.which(os.path.expanduser(parts[0])) is not None


def programs() -> dict[str, str]:
    """Program name (as tmux reports a pane's command) -> agent name, for every agent."""
    out: dict[str, str] = {}
    for name, h in load().items():
        parts = shlex.split(h.start)
        if parts:
            out.setdefault(os.path.basename(os.path.expanduser(parts[0])), name)
    return out


def installed_names() -> list[str]:
    return [name for name, h in load().items() if installed(h)]


# Preferred defaults when the user has not chosen one.
_PREFERRED = ["claude", "codex"]


def suggested_default(names: list[str]) -> str | None:
    for name in _PREFERRED:
        if name in names:
            return name
    return names[0] if names else None


def default_agent() -> str:
    """The configured default agent if installed, else a suggestion."""
    from tam import config

    names = installed_names()
    chosen = config.get("default_agent")
    if chosen and chosen in names:
        return chosen
    return suggested_default(names) or "claude"
