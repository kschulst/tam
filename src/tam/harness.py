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

    def command(
        self, *, name: str, session_id: str, started: bool, args: list[str]
    ) -> tuple[list[str], bool]:
        """Return (argv, resuming)."""
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
        result[key] = Harness(name=key, start=value["start"], resume=value.get("resume"))
    return result
