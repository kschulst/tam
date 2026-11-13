"""~/.config/tam/config.toml: tam's own settings (currently only default_agent)."""

from __future__ import annotations

import tomllib

from tam import paths


def _file():
    return paths.config_dir() / "config.toml"


def load() -> dict:
    f = _file()
    return tomllib.loads(f.read_text()) if f.exists() else {}


def get(key: str) -> str | None:
    value = load().get(key)
    return value if isinstance(value, str) else None


def set(key: str, value: str) -> None:  # noqa: A001
    data = {**load(), key: value}
    f = _file()
    f.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"{k} = {_quote(v)}" for k, v in data.items() if isinstance(v, str)]
    f.write_text("\n".join(lines) + "\n")


def _quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
